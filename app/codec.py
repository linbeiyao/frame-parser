"""进制编解码：文本 <-> bytes。

支持 HEX(十六进制) / DEC(十进制) / BIN(二进制) / ASCII(文本) 四种进制，
可以自动识别进制、自动拼接被分隔符拆散的字节、支持 0xAA / 1Ah / 1010b 等写法。

所有解析失败统一抛出 CodecError，界面层直接展示 message 即可。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

RADIX_AUTO = "auto"
RADIX_HEX = "hex"
RADIX_DEC = "dec"
RADIX_BIN = "bin"
RADIX_ASCII = "ascii"

#: (取值, 界面显示名)
RADIX_ITEMS: list[tuple[str, str]] = [
    (RADIX_AUTO, "自动识别"),
    (RADIX_HEX, "HEX 十六进制"),
    (RADIX_DEC, "DEC 十进制"),
    (RADIX_BIN, "BIN 二进制"),
    (RADIX_ASCII, "ASCII 文本"),
]

#: 十进制输入时每个数值占几个字节；0 = 自动
DEC_WIDTH_ITEMS: list[tuple[int, str]] = [
    (0, "自动 (按最大值)"),
    (1, "1 字节 (u8)"),
    (2, "2 字节 (u16)"),
    (4, "4 字节 (u32)"),
]

BYTE_ORDER_ITEMS: list[tuple[str, str]] = [
    ("big", "大端 MSB first"),
    ("little", "小端 LSB first"),
]

RADIX_LABELS = dict(RADIX_ITEMS)
BYTE_ORDER_LABELS = dict(BYTE_ORDER_ITEMS)

_SEP_RE = re.compile(r"[\s,;|]+")
#: 十六进制允许额外的 '-' 分隔（AA-BB-CC 这种日志很常见）
_HEX_STRIP_RE = re.compile(r"[\s,;:|-]+")
_HEX_OTHER_SEP_RE = re.compile(r"[\s,;:|-]")
_HEX_PREFIX_RE = re.compile(r"0[xX]")
_BIN_PREFIX_RE = re.compile(r"0[bB]")
_ESCAPE_RE = re.compile(r"\\(x[0-9a-fA-F]{2}|[rnt0\\])")
_HEX_DIGITS = set("0123456789abcdefABCDEF")


def hex_fast_line(line: str) -> bytes | None:
    """整行都是十六进制时走 bytes.fromhex 的 C 级快速路径（几百万字节从秒级降到毫秒级）。

    能处理：'AA 55 01'、'AA5501'、'0xAA,0x55'、'aa-55'。做不到的（带 h 后缀、
    奇数位需要补零、含非法字符）返回 None，交给逐 token 的慢路径报出准确错误。
    """
    # ① 最快：空格分隔的纯十六进制 —— bytes.fromhex 本身就是 C 实现且自动忽略空格
    try:
        return bytes.fromhex(line)
    except ValueError:
        pass
    # ② 连写 / 其它分隔符 / 0x 前缀
    if "h" in line or "H" in line or "_" in line:
        return None
    body = line
    if "x" in body or "X" in body:
        body = _HEX_PREFIX_RE.sub("", body)
    if " " in body:
        body = body.replace(" ", "")
    if _HEX_OTHER_SEP_RE.search(body):
        body = _HEX_STRIP_RE.sub("", body)
    if not body or len(body) % 2:
        return None
    try:
        return bytes.fromhex(body)
    except ValueError:
        return None


class CodecError(ValueError):
    """输入数据无法按指定进制解析。"""


@dataclass
class LineSpan:
    """一行文本 ↔ 它解码出来的那段字节（用于"点表格里的值 → 跳回左侧原文并反色选中"）。"""

    byte_start: int
    byte_count: int
    text_start: int
    text_len: int

    @property
    def byte_end(self) -> int:
        return self.byte_start + self.byte_count

    def contains(self, byte_offset: int) -> bool:
        return self.byte_start <= byte_offset < self.byte_end


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #
def clean_tokens(text: str) -> list[str]:
    """按空白 / 逗号 / 分号 / 竖线切分，去掉空片段。"""
    text = (text or "").strip()
    if not text:
        return []
    return [t for t in _SEP_RE.split(text) if t]


def detect_radix(text: str) -> str:
    """猜测输入文本所用的进制。

    单行按内容判断；多行按"逐行投票"取多数（这样日志里的注释行不会把整体带偏）。
    歧义时（如 "10 20 30"）按 HEX 处理。
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return RADIX_HEX
    if len(lines) > 1:
        votes: dict[str, int] = {}
        for line in lines[:60]:
            guessed = _detect_single_line(line)
            votes[guessed] = votes.get(guessed, 0) + 1
        priority = {RADIX_HEX: 3, RADIX_DEC: 2, RADIX_BIN: 1, RADIX_ASCII: 0}
        return max(votes.items(), key=lambda kv: (kv[1], priority.get(kv[0], 0)))[0]
    return _detect_single_line(lines[0])


def _detect_single_line(text: str) -> str:
    tokens = clean_tokens(text)
    if not tokens:
        return RADIX_HEX

    if any(t.lower().startswith("0x") for t in tokens):
        return RADIX_HEX

    def _is_hex_token(t: str) -> bool:
        body = t[:-1] if t.lower().endswith("h") else t
        return bool(body) and set(body) <= _HEX_DIGITS

    if all(_is_hex_token(t) for t in tokens):
        if any(c in "abcdefABCDEF" for t in tokens for c in t):
            return RADIX_HEX
        # 全部都是两位 —— 典型的 "AA 55 01" 十六进制写法
        if all(len(t) == 2 for t in tokens):
            return RADIX_HEX

    if all(set(t) <= {"0", "1"} and len(t) >= 8 and len(t) % 8 == 0 for t in tokens):
        return RADIX_BIN

    if all(t.lstrip("+-").isdigit() for t in tokens):
        return RADIX_DEC

    return RADIX_ASCII


def resolve_radix(text: str, radix: str = RADIX_AUTO) -> str:
    return detect_radix(text) if radix == RADIX_AUTO else radix


# --------------------------------------------------------------------------- #
# 文本 -> bytes
# --------------------------------------------------------------------------- #
def ascii_to_bytes(text: str) -> bytes:
    """文本转字节，支持 \\r \\n \\t \\0 \\xNN 转义。"""
    def _sub(m: re.Match) -> str:
        body = m.group(1)
        if body.startswith("x") or body.startswith("X"):
            return chr(int(body[1:], 16))
        return {"r": "\r", "n": "\n", "t": "\t", "0": "\0", "\\": "\\"}[body]

    return _ESCAPE_RE.sub(_sub, text).encode("utf-8", errors="replace")


def hex_to_bytes(tokens: list[str]) -> bytes:
    parts: list[str] = []
    for tok in tokens:
        body = _HEX_PREFIX_RE.sub("", tok.strip())
        if body.lower().endswith("h"):
            body = body[:-1]
        body = body.replace("-", "")      # 容忍 AA-BB-CC 写法
        if not body:
            continue
        if not set(body) <= _HEX_DIGITS:
            raise CodecError(f"非法的十六进制片段：{tok!r}（含非 0-9/A-F 字符）")
        if len(body) % 2:  # "5" -> "05"；"123" -> "0123"
            body = "0" + body
        parts.append(body)
    return bytes.fromhex("".join(parts))


def bin_to_bytes(tokens: list[str]) -> bytes:
    parts: list[str] = []
    for tok in tokens:
        body = _BIN_PREFIX_RE.sub("", tok.strip()).removesuffix("b")
        if not body:
            continue
        if not set(body) <= {"0", "1"}:
            raise CodecError(f"非法的二进制片段：{tok!r}（只允许 0/1）")
        if len(body) % 8:
            raise CodecError(f"二进制片段位数必须是 8 的整数倍：{tok!r}（{len(body)} 位）")
        for i in range(0, len(body), 8):
            parts.append(body[i:i + 8])
    return bytes(int(p, 2) for p in parts)


def dec_to_bytes(tokens: list[str], width: int = 0, byte_order: str = "big") -> bytes:
    """十进制数值序列转字节；width=0 时按最大值自动选择 1/2/4/8 字节。"""
    values: list[int] = []
    for tok in tokens:
        body = _HEX_PREFIX_RE.sub("", tok.strip())
        body = body.split("/")[0] if "#" in body else body  # 兼容 "12#2" 之类的抄录
        try:
            value = int(body, 10)
        except ValueError as exc:
            raise CodecError(f"非法的十进制数值：{tok!r}（{exc}）") from exc
        if value < 0:
            raise CodecError(f"暂不支持负数：{tok!r}")
        if value > 0xFFFFFFFFFFFFFFFF:
            raise CodecError(f"数值超出 64 位范围：{tok!r}")
        values.append(value)
    if not values:
        return b""
    if not width:
        top = max(values)
        width = 1 if top <= 0xFF else 2 if top <= 0xFFFF else 4 if top <= 0xFFFFFFFF else 8
    for value in values:
        if value > (1 << (width * 8)) - 1:
            raise CodecError(f"数值 {value} 超出 {width} 字节范围")
    return b"".join(v.to_bytes(width, byte_order) for v in values)


def text_to_bytes(
    text: str,
    radix: str = RADIX_AUTO,
    dec_width: int = 0,
    byte_order: str = "big",
) -> bytes:
    """把界面里的文本按指定进制解析成 bytes（严格模式：遇到坏行即报错）。"""
    data, _warnings = text_to_bytes_ex(text, radix, dec_width, byte_order, skip_bad_lines=False)
    return data


def text_to_bytes_ex(
    text: str,
    radix: str = RADIX_AUTO,
    dec_width: int = 0,
    byte_order: str = "big",
    skip_bad_lines: bool = True,
    spans: list[LineSpan] | None = None,
) -> tuple[bytes, list[str]]:
    """按行解析，返回 (字节, 警告列表)。

    跳过式解析（默认）可以容忍日志里的注释行、表头行，
    比如样本文件开头的 "=== field capture sample ... ==="。

    传入 spans 列表时，会顺手记录"每行文本 ↔ 每行字节"的对应关系，
    供界面反查（点表格里的值跳到左侧原文）。
    """
    use = resolve_radix(text, radix)
    warnings: list[str] = []
    if use == RADIX_ASCII:
        data = ascii_to_bytes(text)
        if spans is not None:
            spans.append(LineSpan(0, len(data), 0, len(text)))
        return data, warnings

    out = bytearray()
    text_pos = 0
    for number, raw in enumerate((text or "").splitlines(keepends=True), 1):
        chunk = raw.strip()
        if not chunk:
            text_pos += len(raw)
            continue
        lead = raw.index(chunk[0]) if chunk else 0
        start_byte = len(out)
        try:
            if use == RADIX_HEX:
                fast = hex_fast_line(chunk)      # 绝大多数行都走这条 C 级路径
                if fast is not None:
                    out += fast
                    if spans is not None:
                        spans.append(LineSpan(start_byte, len(fast),
                                               text_pos + lead, len(chunk)))
                    text_pos += len(raw)
                    continue
            tokens = clean_tokens(chunk)
            if not tokens:
                text_pos += len(raw)
                continue
            out += _parse_tokens(tokens, use, dec_width, byte_order)
            if spans is not None:
                spans.append(LineSpan(start_byte, len(out) - start_byte,
                                      text_pos + lead, len(chunk)))
        except CodecError as exc:
            if not skip_bad_lines:
                raise
            if len(warnings) < 50:
                warnings.append(f"第 {number} 行已跳过：{exc}")
            elif len(warnings) == 50:
                warnings.append("（更多坏行已省略）")
        text_pos += len(raw)
    return bytes(out), warnings


def _parse_tokens(tokens: list[str], radix: str, dec_width: int, byte_order: str) -> bytes:
    if radix == RADIX_HEX:
        return hex_to_bytes(tokens)
    if radix == RADIX_BIN:
        return bin_to_bytes(tokens)
    if radix == RADIX_DEC:
        return dec_to_bytes(tokens, dec_width, byte_order)
    raise CodecError(f"不支持的进制：{radix}")


def decode_line(raw: str, radix: str = RADIX_HEX, dec_width: int = 0,
                byte_order: str = "big") -> tuple[bytes, str, int, int]:
    """解一行文本，返回 (字节, 错误, 行内起始偏移, 有效内容长度)。

    给"流式解码"用：一行一行喂，不需要先把整篇文本读进内存。
    坏行返回 b"" + 错误说明（调用方决定跳过还是报错）。
    """
    chunk = (raw or "").strip()
    if not chunk:
        return b"", "", 0, 0
    lead = raw.index(chunk[0]) if chunk else 0
    try:
        if radix == RADIX_HEX:
            fast = hex_fast_line(chunk)
            if fast is not None:
                return fast, "", lead, len(chunk)
        tokens = clean_tokens(chunk)
        if not tokens:
            return b"", "", lead, len(chunk)
        return _parse_tokens(tokens, radix, dec_width, byte_order), "", lead, len(chunk)
    except CodecError as exc:
        return b"", str(exc), lead, len(chunk)


# --------------------------------------------------------------------------- #
# bytes -> 文本
# --------------------------------------------------------------------------- #
def bytes_to_text(
    data: bytes,
    radix: str = RADIX_HEX,
    sep: str = " ",
    upper: bool = True,
    prefix: bool = False,
    escape: bool = True,
) -> str:
    if not data:
        return ""
    if radix == RADIX_HEX:
        fmt = "{:02X}" if upper else "{:02x}"
        head = "0x" if prefix else ""
        return sep.join(head + fmt.format(b) for b in data)
    if radix == RADIX_DEC:
        return sep.join(str(b) for b in data)
    if radix == RADIX_BIN:
        return sep.join(format(b, "08b") for b in data)
    if radix == RADIX_ASCII:
        text = data.decode("utf-8", errors="replace")
        return to_printable(text) if escape else text
    raise CodecError(f"不支持的进制：{radix}")


def to_printable(text: str) -> str:
    """把不可见字符转成 \\r \\n \\xNN 形式，便于在界面里显示。"""
    out: list[str] = []
    for ch in text:
        if ch == "\r":
            out.append("\\r")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 32 or ord(ch) == 127:
            out.append(f"\\x{ord(ch):02X}")
        else:
            out.append(ch)
    return "".join(out)


def hexdump(data: bytes, width: int = 16, base: int = 0) -> str:
    """标准 hexdump：偏移 + 十六进制 + ASCII。"""
    if not data:
        return "(空)"
    lines: list[str] = []
    for off in range(0, len(data), width):
        chunk = data[off:off + width]
        hexpart = " ".join(f"{b:02X}" for b in chunk).ljust(width * 3 - 1)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{base + off:08X}  {hexpart}  |{text}|")
    return "\n".join(lines)
