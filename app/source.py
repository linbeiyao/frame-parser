"""按行随机访问的文本来源：文件用 mmap，不把整篇读进内存。

解决的问题
----------
128 MB 的抓包文本放进 `QPlainTextEdit` 要占几百 MB，解码后又要在内存里留一份字节流。
这里改成：

* **文件走 mmap**：只在需要时读某个字节区间（操作系统负责缓存），
  行首位置记成 `array('q')`（28 万行 ≈ 2.3 MB）；
* **流式解码 + 流式分帧**：一行一行解码、一块一块喂给 `FrameStreamer`，
  只记"每行解出多少字节"和"帧索引"，**不留全文、也不留全部字节**；
* **按需取字节**：`decode_bytes(byte_start, count)` 先二分定位到行，
  只解那几行再切片 —— 于是"点表格跳回原文"和"取某一帧的字节"都是随机读取；
* **编辑器分页**：`window_text(start, count)` 只给当前页的文本。

内存占用因此和文件大小基本脱钩：128 MB 的抓包只剩十几 MB 的索引。
"""
from __future__ import annotations

import mmap
from array import array
from bisect import bisect_right
from collections.abc import Iterator
from pathlib import Path

from . import codec, framer

#: 编辑器一页显示多少行
PAGE_LINES = 2000
#: 流式解码时每多少行交给分帧器一次
CHUNK_LINES = 2000
#: 大于这个大小（字节）的文件就走"分页 + 按需解码"的路子
BIG_FILE_BYTES = 4 * 1024 * 1024


class TextSource:
    """文本来源：文件（mmap）或已经在内存里的字符串。"""

    def __init__(self, path: str | Path | None = None, text: str | None = None) -> None:
        self.path = str(path) if path else ""
        self._text = text
        self._mmap: mmap.mmap | None = None
        self._handle = None
        self._line_starts = array("q")      # 每行在文件里的起始字节
        self._encoding = "utf-8"
        self.byte_starts = array("q")       # 每行解码出来的起始字节号（解码后填）
        self.byte_counts = array("i")       # 每行解码出多少字节
        self.radix_used = ""
        self.radix_setting = ""
        self.dec_width = 0
        self.byte_order = "big"
        self.total_bytes = 0
        self.build_warnings: list[str] = []
        self._closed = False
        if self.path:
            self._open_file()
        else:
            self._index_text(text or "")

    def close(self) -> None:
        """释放文件句柄（Windows 上不关掉就删不掉文件）。"""
        self._closed = True
        self.byte_starts = array("q")
        self.byte_counts = array("i")
        if self._mmap is not None:
            try:
                self._mmap.close()
            except (BufferError, ValueError):
                pass
            self._mmap = None
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None

    @property
    def closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------------ #
    # 打开 / 建行索引
    # ------------------------------------------------------------------ #
    def _open_file(self) -> None:
        self._handle = open(self.path, "rb")
        size = Path(self.path).stat().st_size
        if size == 0:
            self._mmap = None
            return
        self._mmap = mmap.mmap(self._handle.fileno(), 0, access=mmap.ACCESS_READ)
        self._index_bytes(self._mmap)
        self._encoding = self._guess_encoding()

    def _index_bytes(self, blob) -> None:
        """扫一遍找换行（`find` 是 C 级，28 万行几十毫秒）。"""
        starts = self._line_starts
        starts.append(0)
        pos = 0
        size = len(blob)
        while pos < size:
            found = blob.find(b"\n", pos)
            if found < 0:
                break
            pos = found + 1
            starts.append(pos)
        # 末尾没有换行时，最后一行也要算上
        if starts[-1] >= size and len(starts) > 1:
            starts.pop()

    def _index_text(self, text: str) -> None:
        starts = self._line_starts
        starts.append(0)
        offset = 0
        for line in text.splitlines(keepends=True):
            offset += len(line)
            starts.append(offset)
        if starts[-1] >= len(text) and len(starts) > 1:
            starts.pop()
        self._encoding = "utf-8"

    def _guess_encoding(self) -> str:
        head = self._read_file_bytes(0, min(65536, self.file_size))
        for encoding in ("utf-8", "gbk", "latin-1"):
            try:
                head.decode(encoding)
                return encoding
            except UnicodeDecodeError:
                continue
        return "latin-1"

    def _read_file_bytes(self, start: int, count: int) -> bytes:
        if not self._mmap or self._closed:
            return b""
        return self._mmap[start:start + count]

    # ------------------------------------------------------------------ #
    # 基本信息
    # ------------------------------------------------------------------ #
    @property
    def file_size(self) -> int:
        if self._mmap is not None:
            return len(self._mmap)
        return len(self._text.encode("utf-8")) if self._text else 0

    @property
    def line_count(self) -> int:
        return len(self._line_starts)

    @property
    def source_label(self) -> str:
        if self.path:
            return f"{Path(self.path).name}（{self.file_size / 1048576:.1f} MB，按需读取）"
        return f"内存文本（{self.file_size / 1024:.0f} KB）"

    # ------------------------------------------------------------------ #
    # 按行读取
    # ------------------------------------------------------------------ #
    def line_at(self, index: int) -> str:
        if self._closed or not 0 <= index < self.line_count:
            return ""
        start = self._line_starts[index]
        end = (self._line_starts[index + 1] if index + 1 < self.line_count
               else self.file_size)
        if self._mmap is not None:
            raw = self._mmap[start:end]
            return raw.decode(self._encoding, errors="replace").rstrip("\r\n")
        return self._slice_text(start, end)

    def _slice_text(self, start: int, end: int) -> str:
        text = self._text or ""
        # 内存文本的行首是按字符记的（不是字节），这里直接用字符下标
        return text[start:end].rstrip("\r\n") if self.path == "" else text[start:end]

    def iter_lines(self, start: int = 0, stop: int | None = None) -> Iterator[str]:
        stop = self.line_count if stop is None else min(stop, self.line_count)
        for index in range(max(0, start), max(0, stop)):
            yield self.line_at(index)

    def window_text(self, start: int, count: int = PAGE_LINES) -> str:
        """一页文本（编辑器只显示这一页）。"""
        start = max(0, min(int(start), max(0, self.line_count - 1)))
        stop = min(self.line_count, start + max(1, int(count)))
        lines = [self.line_at(index) for index in range(start, stop)]
        return "\n".join(lines) + ("\n" if lines else "")

    def sample_text(self, max_lines: int = 200, max_chars: int = 20000) -> str:
        """开头一小段（识别进制/智能分帧用，不读整个文件）。"""
        out: list[str] = []
        total = 0
        for line in self.iter_lines(0, max_lines):
            out.append(line)
            total += len(line) + 1
            if total >= max_chars:
                break
        return "\n".join(out)

    # ------------------------------------------------------------------ #
    # 字节 ↔ 行
    # ------------------------------------------------------------------ #
    def byte_range_of_line(self, line_index: int) -> tuple[int, int]:
        if not 0 <= line_index < len(self.byte_starts):
            return (0, 0)
        return (int(self.byte_starts[line_index]), int(self.byte_counts[line_index]))

    def line_of_byte(self, byte_offset: int) -> int:
        """字节偏移落在第几行（二分）。"""
        if not len(self.byte_starts):
            return 0
        position = bisect_right(self.byte_starts, max(0, int(byte_offset))) - 1
        return max(0, min(position, len(self.byte_starts) - 1))

    def decode_bytes(self, start: int, count: int) -> bytes:
        """按解码后的字节范围随机取字节：只解涉及的那几行。"""
        if count <= 0 or not len(self.byte_starts) or self._closed:
            return b""
        first = self.line_of_byte(start)
        out = bytearray()
        line = first
        have = int(self.byte_starts[first])
        while line < self.line_count and have < start + count:
            data = self._decode_line(line)
            out += data
            have += len(data)
            line += 1
        skip = start - int(self.byte_starts[first])
        return bytes(out[skip:skip + count])

    def _decode_line(self, line_index: int) -> bytes:
        raw = self.line_at(line_index)
        if self.radix_used == codec.RADIX_ASCII:
            return (raw + "\n").encode("utf-8")
        data, error, _lead, _length = codec.decode_line(
            raw, self.radix_used or codec.RADIX_HEX, self.dec_width, self.byte_order
        )
        return b"" if error else data

    def provider(self):
        """给 FrameSource 用的"按字节范围取数据"回调。"""
        def fetch(start: int, count: int) -> bytes:
            return self.decode_bytes(start, count)
        return fetch

    # ------------------------------------------------------------------ #
    # 流式解码 + 流式分帧（不驻留全文与全部字节）
    # ------------------------------------------------------------------ #
    def build_index(self, cfg: framer.FrameConfig, radix: str = "",
                    dec_width: int = 0, byte_order: str = "big",
                    chunk_lines: int = CHUNK_LINES,
                    on_progress=None, on_chunk=None) -> framer.SplitResult:
        """一边读一边解码一边分帧，只留索引。

        `on_progress(done_lines, total_lines)` 用来更新进度条；
        `on_chunk(frames, done_lines)` 可以在解析过程中先给界面喂一部分结果。
        """
        result = framer.SplitResult()
        self.radix_setting = radix or codec.RADIX_AUTO
        self.dec_width = int(dec_width)
        self.byte_order = byte_order or "big"
        used = codec.resolve_radix(self.sample_text(), self.radix_setting)
        self.radix_used = used
        self.byte_starts = array("q")
        self.byte_counts = array("i")
        self.total_bytes = 0
        self.build_warnings = []

        if not self.line_count:
            result.warnings.append("输入数据为空")
            return result

        streamer = framer.FrameStreamer(cfg, on_frame=result.add)
        buffer = bytearray()
        total = self.line_count
        bad = 0
        ascii_mode = used == codec.RADIX_ASCII
        for start in range(0, total, chunk_lines):
            stop = min(total, start + chunk_lines)
            for line_index in range(start, stop):
                raw = self.line_at(line_index)
                self.byte_starts.append(self.total_bytes)
                if ascii_mode:
                    data = (raw + "\n").encode("utf-8")
                    error = ""
                else:
                    data, error, _lead, _length = codec.decode_line(
                        raw, used, self.dec_width, self.byte_order
                    )
                if error:
                    bad += 1
                    if len(self.build_warnings) < 50:
                        self.build_warnings.append(f"第 {line_index + 1} 行已跳过：{error}")
                    data = b""
                self.byte_counts.append(len(data))
                if data:
                    buffer += data
                    self.total_bytes += len(data)
            if buffer:
                streamer.feed(buffer)
                buffer.clear()
            if on_progress is not None:
                on_progress(stop, total)
            if on_chunk is not None and len(result.frames):
                on_chunk(result, stop)
            if streamer._stopped:            # noqa: SLF001 - 到帧数上限就停
                break
        streamer.finish()
        result.warnings.extend(streamer.warnings)
        result.warnings.extend(self.build_warnings)
        result.used_bytes = streamer.used_bytes
        result.set_provider(self.provider())
        if bad:
            result.warnings.append(f"共跳过 {bad} 行无法解析的内容")
        return result


def make_source(text: str = "", path: str | Path | None = None) -> TextSource:
    """小数据用内存文本，大文件用 mmap（调用方按文件大小决定）。"""
    return TextSource(path=path) if path else TextSource(text=text)


class LazyData:
    """像 `bytes` 一样用，但字节是按需从 TextSource 解出来的。

    给"进制视图 / 位编辑器 / 长度判断"这类只需要看一眼的地方用：
    `len()`、`data[i]`、`data[a:b]`、`data.hex()` 都能用，而大文件不会整篇驻留。
    真正要遍历全部字节的地方（导出、分帧）请直接用 `TextSource`。
    """

    #: hex() 最多允许打多少字节（再大就不是给人看的了）
    HEX_LIMIT = 64 * 1024

    def __init__(self, source: TextSource) -> None:
        self.source = source

    def __len__(self) -> int:
        return int(self.source.total_bytes)

    def __getitem__(self, key):
        if isinstance(key, slice):
            start, stop, step = key.indices(len(self))
            data = self.source.decode_bytes(start, max(0, stop - start))
            return data[::step] if step != 1 else data
        position = key + len(self) if key < 0 else key
        if not 0 <= position < len(self):
            raise IndexError(f"字节下标越界：{key}")
        return self.source.decode_bytes(position, 1)[0]

    def __iter__(self):
        step = 65536
        for start in range(0, len(self), step):
            yield from self.source.decode_bytes(start, min(step, len(self) - start))

    def hex(self, sep: str = " ") -> str:
        if len(self) > self.HEX_LIMIT:
            raise ValueError(f"数据太大（{len(self)} 字节），请用 TextSource 分块取")
        return self.source.decode_bytes(0, len(self)).hex(sep).upper()

    def __repr__(self) -> str:      # pragma: no cover - 调试用
        return f"<LazyData {len(self)} 字节，来自 {self.source.source_label}>"
