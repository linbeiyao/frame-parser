"""分帧器：把一段字节流按不同「帧格式」拆成一个个数据帧。

支持的分帧方式
--------------
- header    帧头 + 长度字段 (+ 校验)，最常见的自定义协议
- fixed     定长帧
- lenprefix 长度前缀 + 数据
- delimiter 分隔符分帧（\\r\\n、0x7E 等）
- tlv       TLV 帧（Tag + Len + Value）
- whole     整包不拆分（单帧，可校验）

每种方式都可以附加校验：校验字段可位于帧尾，也可紧跟长度字段之后。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from . import checksum, codec
from .frames import (  # noqa: F401 - Frame/STATE_* 从这里再导出，保持对外接口不变
    STATE_FAIL, STATE_LABELS, STATE_NA, STATE_OK, Frame, FrameIndexBuilder,
    FrameSource, MemoryProvider, SplitResult,
)

MODE_HEADER = "header"
MODE_FIXED = "fixed"
MODE_LENPREFIX = "lenprefix"
MODE_DELIMITER = "delimiter"
MODE_TLV = "tlv"
MODE_WHOLE = "whole"

MODE_ITEMS: list[tuple[str, str]] = [
    (MODE_HEADER, "帧头 + 长度字段 (+ 校验)"),
    (MODE_FIXED, "定长帧"),
    (MODE_LENPREFIX, "长度前缀 + 数据"),
    (MODE_DELIMITER, "分隔符分帧"),
    (MODE_TLV, "TLV 帧 (Tag+Len+Value)"),
    (MODE_WHOLE, "整包不拆分"),
]

MODE_LABELS = dict(MODE_ITEMS)

CHECK_TAIL = "tail"
CHECK_AFTER_LEN = "after_len"

CHECK_WHERE_ITEMS: list[tuple[str, str]] = [
    (CHECK_TAIL, "位于帧尾"),
    (CHECK_AFTER_LEN, "紧跟长度字段之后"),
]


class FrameConfigError(ValueError):
    """分帧参数不合法。"""


@dataclass
class FrameConfig:
    mode: str = MODE_HEADER

    # 定长帧
    fixed_len: int = 8

    # 帧头
    header_hex: str = "AA 55"

    # 长度字段（相对帧首的偏移）
    len_offset: int = 2
    len_size: int = 2
    len_byte_order: str = "big"
    len_includes_header: bool = True
    len_includes_checksum: bool = True

    # 校验
    checksum_algo: str = "none"
    checksum_byte_order: str = "big"
    checksum_where: str = CHECK_TAIL

    # 分隔符
    delimiter_hex: str = "0D 0A"
    include_delimiter: bool = True

    # 长度前缀
    prefix_size: int = 2
    prefix_byte_order: str = "big"
    prefix_includes_self: bool = False

    # TLV
    tag_size: int = 1
    value_len_size: int = 1
    value_len_byte_order: str = "big"

    # 通用：抓包文件前面常有时间戳/前导数据，可以先跳过再分帧
    skip_bytes: int = 0
    #: 单次最多分出多少帧；**0 = 不限**（128 MB 抓包约 27 万帧也照分）
    max_frames: int = 0

    # ------------------------------------------------------------------ #
    @property
    def header(self) -> bytes:
        return codec.hex_to_bytes(codec.clean_tokens(self.header_hex))

    @property
    def delimiter(self) -> bytes:
        return codec.hex_to_bytes(codec.clean_tokens(self.delimiter_hex))

    @property
    def checksum_size(self) -> int:
        return checksum.algo_size(self.checksum_algo)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "FrameConfig":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})

    def validate(self) -> None:
        if self.skip_bytes < 0:
            raise FrameConfigError("跳过字节数不能为负")
        if self.mode == MODE_FIXED and self.fixed_len <= 0:
            raise FrameConfigError("定长帧长度必须大于 0")
        if self.mode == MODE_HEADER:
            if self.len_size not in (1, 2, 4, 8):
                raise FrameConfigError("长度字段字节数只能是 1/2/4/8")
            if self.len_offset < 0:
                raise FrameConfigError("长度字段偏移不能为负数")
            if self.header and self.len_offset + self.len_size > len(self.header) and self.checksum_where == CHECK_AFTER_LEN:
                if self.len_offset + self.len_size + self.checksum_size > len(self.header):
                    raise FrameConfigError("校验字段位置超出行头范围")
        if self.mode == MODE_DELIMITER and not self.delimiter:
            raise FrameConfigError("分隔符不能为空")
        if self.mode == MODE_LENPREFIX and self.prefix_size not in (1, 2, 4):
            raise FrameConfigError("长度前缀字节数只能是 1/2/4")
        if self.mode == MODE_TLV:
            if not (1 <= self.tag_size <= 8):
                raise FrameConfigError("Tag 字节数只能是 1~8")
            if self.value_len_size not in (1, 2, 4):
                raise FrameConfigError("Len 字节数只能是 1/2/4")


# Frame / SplitResult / STATE_* 现在定义在 app/frames.py（惰性帧视图），这里只是再导出，
# 所以 `framer.Frame(...)`、`framer.SplitResult()` 这些老写法都还能用。


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def _check_frame(data: bytes, start: int, end: int, cfg: FrameConfig) -> tuple[str, str]:
    """对 [start, end) 这段做校验；没配校验算法时连切片都不做。"""
    if not cfg.checksum_size:
        return STATE_NA, ""
    return verify_checksum(data[start:end], cfg)


def verify_checksum(frame: bytes, cfg: FrameConfig) -> tuple[str, str]:
    """返回 (状态, 说明)。状态取 ok / fail / na。"""
    size = cfg.checksum_size
    if not size:
        return STATE_NA, ""
    if cfg.checksum_where == CHECK_TAIL:
        if len(frame) <= size:
            return STATE_FAIL, f"帧长 {len(frame)} 字节，不足以容纳 {size} 字节校验"
        body, actual = frame[:-size], frame[-size:]
    else:
        start = cfg.len_offset + cfg.len_size
        if start + size > len(frame):
            return STATE_FAIL, "校验字段超出帧长范围"
        body = frame[:start] + frame[start + size:]
        actual = frame[start:start + size]

    expect = checksum.compute(cfg.checksum_algo, body, cfg.checksum_byte_order)
    if expect == actual:
        return STATE_OK, ""
    return STATE_FAIL, (
        f"校验失败：计算值 {expect.hex(' ').upper()} ≠ 帧内 {actual.hex(' ').upper()}"
    )


# --------------------------------------------------------------------------- #
# 各分帧方式
# --------------------------------------------------------------------------- #
def _cap_reached(cfg: FrameConfig, count: int) -> bool:
    """是否已经到「单次最大分帧数」上限（max_frames = 0 表示不限）。"""
    return bool(cfg.max_frames) and count >= int(cfg.max_frames)


def _split_header(data: bytes, cfg: FrameConfig, result: SplitResult) -> None:
    header = cfg.header
    pos = 0
    while pos < len(data) and not _cap_reached(cfg, result.count):
        start = data.find(header, pos) if header else pos
        if start < 0:
            rest = len(data) - pos
            if rest:
                result.warnings.append(f"偏移 {pos} 之后未找到帧头，剩余 {rest} 字节被忽略")
            break
        if start > pos:
            result.warnings.append(f"跳过帧头前的 {start - pos} 字节（偏移 {pos} ~ {start - 1}）")

        len_pos = start + cfg.len_offset
        if len_pos + cfg.len_size > len(data):
            result.warnings.append(f"偏移 {start} 处帧头之后长度字段不完整，剩余数据被忽略")
            result.used_bytes = start
            return
        length_value = int.from_bytes(
            data[len_pos:len_pos + cfg.len_size], cfg.len_byte_order
        )
        total = length_value
        if not cfg.len_includes_header:
            total += len(header)
        if not cfg.len_includes_checksum:
            total += cfg.checksum_size

        if total <= 0:
            result.warnings.append(f"偏移 {start} 处长度字段为 {length_value}，无法分帧，已停止")
            result.used_bytes = start
            return

        end = start + total
        if end > len(data):
            rest = len(data) - start
            result.add(start, len(data), STATE_NA,
                       note=f"数据不完整：需要 {total} 字节，实际只有 {rest} 字节")
            result.warnings.append(f"偏移 {start} 处帧不完整（{rest}/{total} 字节）")
            result.used_bytes = len(data)
            return

        state, note = _check_frame(data, start, end, cfg)
        result.add(start, end, state, note)
        pos = end
    result.used_bytes = pos
    if _cap_reached(cfg, result.count):
        result.warnings.append(f"已达到单次最大分帧数 {cfg.max_frames}，停止解析（可把上限调大或设为 0=不限）")


def _split_fixed(data: bytes, cfg: FrameConfig, result: SplitResult) -> None:
    size = cfg.fixed_len
    for offset in range(0, len(data), size):
        if _cap_reached(cfg, result.count):
            result.warnings.append(f"已达到单次最大分帧数 {cfg.max_frames}，停止解析（可把上限调大或设为 0=不限）")
            break
        end = min(offset + size, len(data))
        if end - offset < size:
            result.add(offset, end, STATE_NA,
                       note=f"尾部残帧：只有 {end - offset}/{size} 字节")
            result.warnings.append(f"尾部残帧 {end - offset}/{size} 字节（偏移 {offset}）")
            break
        state, note = _check_frame(data, offset, end, cfg)
        result.add(offset, end, state, note)
    result.used_bytes = len(data)


def _split_lenprefix(data: bytes, cfg: FrameConfig, result: SplitResult) -> None:
    pos = 0
    while pos < len(data) and not _cap_reached(cfg, result.count):
        if pos + cfg.prefix_size > len(data):
            result.warnings.append(f"偏移 {pos} 处长度前缀不完整，剩余被忽略")
            break
        value = int.from_bytes(data[pos:pos + cfg.prefix_size], cfg.prefix_byte_order)
        total = value if cfg.prefix_includes_self else value + cfg.prefix_size
        if total <= 0:
            result.warnings.append(f"偏移 {pos} 处长度为 {value}，无法分帧，已停止")
            break
        end = pos + total
        if end > len(data):
            rest = len(data) - pos
            result.add(pos, len(data), STATE_NA,
                       note=f"数据不完整：需要 {total} 字节，实际只有 {rest} 字节")
            result.warnings.append(f"偏移 {pos} 处帧不完整（{rest}/{total} 字节）")
            pos = len(data)
            break
        state, note = _check_frame(data, pos, end, cfg)
        result.add(pos, end, state, note)
        pos = end
    result.used_bytes = pos


def _split_delimiter(data: bytes, cfg: FrameConfig, result: SplitResult) -> None:
    delim = cfg.delimiter
    start = 0
    while not _cap_reached(cfg, result.count):
        pos = data.find(delim, start)
        if pos < 0:
            rest = len(data) - start
            if rest:
                result.add(start, len(data), STATE_NA, note="末尾没有分隔符（残帧）")
                result.warnings.append(f"偏移 {start} 起 {rest} 字节末尾缺少分隔符")
            result.used_bytes = len(data)
            return
        end = pos + (len(delim) if cfg.include_delimiter else 0)
        if end > start:
            state, note = _check_frame(data, start, end, cfg)
            result.add(start, end, state, note)
        start = pos + len(delim)
    result.used_bytes = start


def _split_tlv(data: bytes, cfg: FrameConfig, result: SplitResult) -> None:
    pos = 0
    while pos < len(data) and not _cap_reached(cfg, result.count):
        need = cfg.tag_size + cfg.value_len_size
        if pos + need > len(data):
            rest = len(data) - pos
            if rest:
                result.warnings.append(f"偏移 {pos} 处 TLV 头不完整，剩余 {rest} 字节被忽略")
            break
        tag = int.from_bytes(data[pos:pos + cfg.tag_size], "big")
        len_pos = pos + cfg.tag_size
        vlen = int.from_bytes(
            data[len_pos:len_pos + cfg.value_len_size], cfg.value_len_byte_order
        )
        end = len_pos + cfg.value_len_size + vlen
        tag_text = f"0x{tag:0{cfg.tag_size * 2}X}"
        if end > len(data):
            rest = len(data) - pos
            result.add(pos, len(data), STATE_NA,
                       note=f"Value 不完整：需要 {vlen} 字节，实际只有 {rest - need} 字节",
                       tag=tag_text)
            result.warnings.append(f"偏移 {pos} 处 TLV 不完整（Tag=0x{tag:X}, Len={vlen}）")
            pos = len(data)
            break
        state, note = _check_frame(data, pos, end, cfg)
        result.add(pos, end, state, note, tag=tag_text)
        pos = end
    result.used_bytes = pos


def _split_whole(data: bytes, cfg: FrameConfig, result: SplitResult) -> None:
    if not data:
        return
    state, note = verify_checksum(data, cfg)
    result.add(0, len(data), state, note)
    result.used_bytes = len(data)


_DISPATCH = {
    MODE_HEADER: _split_header,
    MODE_FIXED: _split_fixed,
    MODE_LENPREFIX: _split_lenprefix,
    MODE_DELIMITER: _split_delimiter,
    MODE_TLV: _split_tlv,
    MODE_WHOLE: _split_whole,
}


def split_frames(data: bytes, cfg: FrameConfig) -> SplitResult:
    """按配置分帧。参数错误抛 FrameConfigError / CodecError。

    返回的 `result.frames` 是**惰性序列**：只存每帧的位置/长度/校验状态，
    帧字节用的时候才切出来（几十万帧也不会把内存吃满）。
    """
    cfg.validate()
    result = SplitResult()
    if not data:
        result.warnings.append("输入数据为空")
        return result
    result.set_data(data)                 # 帧字节的来源（惰性切片用）

    base = 0
    if cfg.skip_bytes > 0:
        base = min(int(cfg.skip_bytes), len(data))
        data = data[base:]
        result.warnings.append(f"按设置跳过了前 {base} 字节")
        if not data:
            result.warnings.append("跳过之后没有剩余数据")
            return result

    _DISPATCH[cfg.mode](data, cfg, result)

    if base:
        # 偏移量仍按原始数据报，方便和抓包工具对齐（只改索引，不用把帧都造出来）
        result.index.shift_offsets(base)
        result.used_bytes += base
    return result


# --------------------------------------------------------------------------- #
# 流式分帧：边喂字节边出帧（给"文件很大、不整篇读进内存"的场景用）
# --------------------------------------------------------------------------- #
class FrameStreamer:
    """增量分帧器：`feed(chunk)` 随便切，内部维护跨块的剩余字节。

    和 `split_frames()` 的结果一致（自检里有"分块喂 vs 一次喂"的对照），
    但它不需要把整段数据放在内存里：切出来的帧立刻交给 `on_frame` 回调，
    回调里记索引/算校验，然后这块缓冲区就丢掉了。
    """

    #: 兼容不同分帧方式的"跨块保留"策略：保留多少尾部字节以防帧被切开
    def __init__(self, cfg: "FrameConfig", on_frame=None) -> None:
        cfg.validate()
        self.cfg = cfg
        self.on_frame = on_frame or (lambda offset, end, state, note, tag: None)
        self.warnings: list[str] = []
        self.buffer = bytearray()
        self.base = 0                 # buffer[0] 在整段数据里的绝对偏移
        self.count = 0                # 已切出的帧数
        self.used_bytes = 0
        self._finished = False
        self._skip_left = max(0, int(cfg.skip_bytes))
        self._skipped_reported = False
        self._header_gap_reported = False
        self._stopped = False
        self._delimiter = cfg.delimiter
        self._header = cfg.header

    # ------------------------------------------------------------------ #
    def feed(self, chunk: bytes) -> None:
        if not chunk or self._stopped or self._finished:
            return
        self.buffer += chunk
        self._drain()

    def finish(self) -> None:
        """数据喂完了：处理尾巴（残帧/不完整帧）。"""
        if self._finished or self._stopped:
            return
        self._finished = True
        cfg = self.cfg
        mode = cfg.mode
        remaining = len(self.buffer)
        if mode == MODE_WHOLE:
            if remaining:
                data = bytes(self.buffer)
                state, note = verify_checksum(data, cfg)
                self._emit(0, remaining, state, note)
            self.used_bytes = self.base + remaining
            return
        if not remaining:
            self.used_bytes = self.base
            return
        if mode == MODE_FIXED:
            size = cfg.fixed_len
            self._emit(0, remaining, STATE_NA, f"尾部残帧：只有 {remaining}/{size} 字节")
            self.warnings.append(f"尾部残帧 {remaining}/{size} 字节（偏移 {self.base}）")
        elif mode == MODE_DELIMITER:
            self._emit(0, remaining, STATE_NA, "末尾没有分隔符（残帧）")
            self.warnings.append(f"偏移 {self.base} 起 {remaining} 字节末尾缺少分隔符")
        elif mode == MODE_HEADER:
            # 和一次性分帧保持一致：只有"长度字段读得到、但整帧没到齐"才算不完整帧
            need = cfg.len_offset + cfg.len_size
            start = self.buffer.find(self._header) if self._header else 0
            if start >= 0 and len(self.buffer) - start >= need:
                length_value = int.from_bytes(
                    self.buffer[start + cfg.len_offset:start + need], cfg.len_byte_order
                )
                total = length_value
                if not cfg.len_includes_header:
                    total += len(self._header)
                if not cfg.len_includes_checksum:
                    total += cfg.checksum_size
                if total > len(self.buffer) - start:
                    rest = len(self.buffer) - start
                    self._emit(start, len(self.buffer), STATE_NA,
                               f"数据不完整：需要 {total} 字节，实际只有 {rest} 字节")
                    self.warnings.append(
                        f"偏移 {self.base + start} 处帧不完整（{rest}/{total} 字节）"
                    )
            elif start >= 0:
                self.warnings.append(
                    f"偏移 {self.base + start} 处帧头之后长度字段不完整，剩余数据被忽略"
                )
            else:
                self.warnings.append(f"偏移 {self.base} 之后未找到帧头，剩余 {remaining} 字节被忽略")
        elif mode == MODE_LENPREFIX:
            self._emit(0, remaining, STATE_NA,
                       f"数据不完整：长度前缀只解析出 {remaining} 字节")
            self.warnings.append(f"偏移 {self.base} 处帧不完整（{remaining} 字节）")
        elif mode == MODE_TLV:
            need = cfg.tag_size + cfg.value_len_size
            if remaining >= need:
                vlen = int.from_bytes(self.buffer[cfg.tag_size:need], cfg.value_len_byte_order)
                self._emit(0, remaining, STATE_NA,
                           f"Value 不完整：需要 {vlen} 字节，实际只有 {remaining - need} 字节")
                self.warnings.append(
                    f"偏移 {self.base} 处 TLV 不完整（Len={vlen}）"
                )
            else:
                self.warnings.append(f"偏移 {self.base} 处 TLV 头不完整，剩余 {remaining} 字节被忽略")
        self.used_bytes = self.base + remaining
        self.buffer.clear()
        self.base = self.used_bytes

    # ------------------------------------------------------------------ #
    def _emit(self, start: int, end: int, state: str, note: str = "", tag: str = "") -> None:
        offset = self.base + start
        self.count += 1
        self.on_frame(offset, self.base + end, state, note, tag)
        self.used_bytes = self.base + end

    def _cap_reached(self) -> bool:
        if _cap_reached(self.cfg, self.count):
            if not any("最大分帧数" in w for w in self.warnings):
                self.warnings.append(
                    f"已达到单次最大分帧数 {self.cfg.max_frames}，停止解析"
                    "（可把上限调大或设为 0=不限）"
                )
            self._stopped = True
            return True
        return False

    def _consume(self, count: int) -> None:
        """丢掉已经处理完的前 count 字节。"""
        if count <= 0:
            return
        del self.buffer[:count]
        self.base += count

    def _drain(self) -> None:
        cfg = self.cfg
        # 跳过前导字节
        if self._skip_left:
            take = min(self._skip_left, len(self.buffer))
            self._skip_left -= take
            self._consume(take)
            if not self._skipped_reported and self._skip_left == 0 and self.base:
                self.warnings.append(f"按设置跳过了前 {self.base} 字节")
                self._skipped_reported = True
            if self._skip_left:
                return
        mode = cfg.mode
        handler = {
            MODE_FIXED: self._drain_fixed,
            MODE_DELIMITER: self._drain_delimiter,
            MODE_LENPREFIX: self._drain_lenprefix,
            MODE_HEADER: self._drain_header,
            MODE_TLV: self._drain_tlv,
            MODE_WHOLE: lambda: None,
        }[mode]
        while not self._stopped:
            before = len(self.buffer)
            handler()
            if self._cap_reached() or len(self.buffer) == before:
                break

    # ------------------------------------------------------------------ #
    def _drain_fixed(self) -> None:
        size = self.cfg.fixed_len
        while len(self.buffer) >= size:
            if self.cfg.checksum_size:
                state, note = verify_checksum(bytes(self.buffer[:size]), self.cfg)
            else:
                state, note = STATE_NA, ""
            self._emit(0, size, state, note)
            self._consume(size)

    def _drain_delimiter(self) -> None:
        cfg = self.cfg
        while True:
            pos = self.buffer.find(self._delimiter)
            if pos < 0:
                return
            end = pos + (len(self._delimiter) if cfg.include_delimiter else 0)
            if end > 0:
                state, note = _check_frame(bytes(self.buffer[:end]), 0, end, cfg)
                self._emit(0, end, state, note)
            self._consume(pos + len(self._delimiter))

    def _drain_lenprefix(self) -> None:
        cfg = self.cfg
        while len(self.buffer) >= cfg.prefix_size:
            value = int.from_bytes(self.buffer[:cfg.prefix_size], cfg.prefix_byte_order)
            total = value if cfg.prefix_includes_self else value + cfg.prefix_size
            if total <= 0:
                self.warnings.append(f"偏移 {self.base} 处长度为 {value}，无法分帧，已停止")
                self._stopped = True
                return
            if len(self.buffer) < total:
                return                      # 等下一块
            state, note = _check_frame(bytes(self.buffer[:total]), 0, total, cfg)
            self._emit(0, total, state, note)
            self._consume(total)

    def _drain_header(self) -> None:
        cfg = self.cfg
        header = self._header
        while True:
            if not self.buffer:
                return
            start = self.buffer.find(header) if header else 0
            if start < 0:
                # 帧头可能跨在下一块的头上：留 len(header)-1 字节
                keep = max(0, len(header) - 1)
                drop = max(0, len(self.buffer) - keep)
                if drop:
                    if not self._header_gap_reported:
                        self.warnings.append(
                            f"跳过帧头前的 {drop} 字节（偏移 {self.base} ~ {self.base + drop - 1}）"
                        )
                        self._header_gap_reported = True
                    self._consume(drop)
                return
            if start > 0:
                if not self._header_gap_reported:
                    self.warnings.append(
                        f"跳过帧头前的 {start} 字节（偏移 {self.base} ~ {self.base + start - 1}）"
                    )
                    self._header_gap_reported = True
                self._consume(start)
                continue
            need = cfg.len_offset + cfg.len_size
            if len(self.buffer) < need:
                return                      # 长度字段还没到齐
            length_value = int.from_bytes(self.buffer[cfg.len_offset:need], cfg.len_byte_order)
            total = length_value
            if not cfg.len_includes_header:
                total += len(header)
            if not cfg.len_includes_checksum:
                total += cfg.checksum_size
            if total <= 0:
                self.warnings.append(
                    f"偏移 {self.base} 处长度字段为 {length_value}，无法分帧，已停止"
                )
                self._stopped = True
                return
            if len(self.buffer) < total:
                return                      # 整帧还没到齐
            state, note = _check_frame(bytes(self.buffer[:total]), 0, total, cfg)
            self._emit(0, total, state, note)
            self._consume(total)

    def _drain_tlv(self) -> None:
        cfg = self.cfg
        need = cfg.tag_size + cfg.value_len_size
        while True:
            if len(self.buffer) < need:
                return
            tag = int.from_bytes(self.buffer[:cfg.tag_size], "big")
            len_pos = cfg.tag_size
            vlen = int.from_bytes(self.buffer[len_pos:need], cfg.value_len_byte_order)
            end = need + vlen
            if len(self.buffer) < end:
                return
            tag_text = f"0x{tag:0{cfg.tag_size * 2}X}"
            state, note = _check_frame(bytes(self.buffer[:end]), 0, end, cfg)
            self._emit(0, end, state, note, tag=tag_text)
            self._consume(end)


def split_stream(chunks, cfg: FrameConfig, data: bytes | None = None) -> SplitResult:
    """把一串字节块增量分帧成 SplitResult（结果与 split_frames 一致）。

    `data` 给了就作为帧字节的来源（内存里已经有整段数据时）；
    没给的话由调用方之后 `set_provider()` 指定（比如按需从文件里解出来）。
    """
    result = SplitResult()
    if data is not None:
        result.set_data(data)
    streamer = FrameStreamer(cfg, on_frame=result.add)
    for chunk in chunks:
        if chunk:
            streamer.feed(chunk)
            if streamer._stopped:            # noqa: SLF001 - 到上限就停
                break
    streamer.finish()
    result.warnings.extend(streamer.warnings)
    result.used_bytes = streamer.used_bytes
    return result
