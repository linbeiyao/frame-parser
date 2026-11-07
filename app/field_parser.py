"""字段解析器：按「字段定义表」从帧数据里取出各个字段的值。

字段定义
--------
name       字段名（表达式字段中用这个名字引用前面的字段）
type       uint / int / float / bool / bcd / ascii / bytes / expr
offset     相对帧首的偏移（字节）
length     长度（字节）；**填 0 表示"到帧尾为止"**（变长字段用）
byte_order big / little
scale      系数：工程值 = 原始值 * scale + bias
bias       偏置
bits       位域，如 "0"、"3-5"，只在整数类型上生效
radix      显示进制：auto / hex / dec / bin / ascii
expr       表达式（type=expr 时使用），可引用前面字段名
"""
from __future__ import annotations

import math
import re
import struct
from dataclasses import asdict, dataclass, field

from . import codec

TYPE_UINT = "uint"
TYPE_INT = "int"
TYPE_FLOAT = "float"
TYPE_BOOL = "bool"
TYPE_BCD = "bcd"
TYPE_ASCII = "ascii"
TYPE_BYTES = "bytes"
TYPE_EXPR = "expr"

TYPE_ITEMS: list[tuple[str, str]] = [
    (TYPE_UINT, "无符号整数"),
    (TYPE_INT, "有符号整数"),
    (TYPE_FLOAT, "浮点 float"),
    (TYPE_BOOL, "Bool"),
    (TYPE_BCD, "BCD"),
    (TYPE_ASCII, "ASCII 字符串"),
    (TYPE_BYTES, "原始字节"),
    (TYPE_EXPR, "表达式计算"),
]

TYPE_LABELS = dict(TYPE_ITEMS)

RADIX_CHOICES: list[tuple[str, str]] = [
    ("auto", "自动 / 十进制"),
    (codec.RADIX_HEX, "HEX"),
    (codec.RADIX_DEC, "DEC"),
    (codec.RADIX_BIN, "BIN"),
    (codec.RADIX_ASCII, "ASCII"),
]

NUMERIC_TYPES = {TYPE_UINT, TYPE_INT, TYPE_FLOAT, TYPE_BOOL, TYPE_BCD, TYPE_EXPR}

_SAFE_GLOBALS = {
    "__builtins__": {},
    "abs": abs, "min": min, "max": max, "round": round, "pow": pow,
    "int": int, "float": float, "len": len, "sum": sum, "bool": bool,
    "sqrt": math.sqrt, "log": math.log, "exp": math.exp,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "atan2": math.atan2, "pi": math.pi, "e": math.e,
}

_EXTRA_FIELDS = {"note", "expr", "bits", "radix", "unit"}


@dataclass
class FieldDef:
    name: str = "字段"
    type: str = TYPE_UINT
    offset: int = 0
    length: int = 1
    byte_order: str = "big"
    scale: float = 1.0
    bias: float = 0.0
    unit: str = ""
    bits: str = ""
    radix: str = "auto"
    expr: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "FieldDef":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})

    @property
    def type_label(self) -> str:
        return TYPE_LABELS.get(self.type, self.type)


@dataclass
class ParsedField:
    name: str
    type: str
    type_label: str
    offset: int
    length: int
    raw: bytes
    raw_hex: str
    raw_value: object | None
    value: object | None
    unit: str
    radix: str
    ok: bool = True
    note: str = ""
    frame_index: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "offset": self.offset,
            "length": self.length,
            "raw_hex": self.raw_hex,
            "raw_value": _jsonable(self.raw_value),
            "value": _jsonable(self.value),
            "display": format_value(self.value, self.radix),
            "unit": self.unit,
            "ok": self.ok,
            "note": self.note,
        }


def _jsonable(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return str(value)
        return round(value, 10)
    if isinstance(value, bytes):
        return codec.bytes_to_text(value, codec.RADIX_HEX)
    if isinstance(value, str) or value is None:
        return value
    return str(value)


# --------------------------------------------------------------------------- #
# 位域 / 显示格式化
# --------------------------------------------------------------------------- #
def parse_bits(spec: str) -> tuple[int, int] | None:
    """ "3" -> (3,3)；"3-5" -> (3,5)；"4:6" -> (4,6)；空 -> None """
    spec = (spec or "").strip()
    if not spec:
        return None
    m = re.fullmatch(r"(\d+)\s*(?:[-~:]\s*(\d+))?", spec)
    if not m:
        raise ValueError(f"位域写法无法识别：{spec!r}（示例：0、3-5）")
    low = int(m.group(1))
    high = int(m.group(2)) if m.group(2) else low
    if high < low:
        low, high = high, low
    if high > 63:
        raise ValueError(f"位域超出 64 位：{spec!r}")
    return low, high


def extract_bits(value: int, spec: str) -> int:
    parsed = parse_bits(spec)
    if not parsed:
        return value
    low, high = parsed
    return (value >> low) & ((1 << (high - low + 1)) - 1)


def _trim_float(value: float) -> str:
    text = f"{value:.10g}"
    return text


def format_value(value, radix: str = "auto", length: int = 0) -> str:
    """把解析结果按指定进制渲染成字符串（供表格按列切换显示格式使用）。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        if radix == codec.RADIX_HEX:
            return f"0x{value:X}" if value >= 0 else f"-0x{-value:X}"
        if radix == codec.RADIX_BIN:
            return f"0b{value:b}" if value >= 0 else f"-0b{-value:b}"
        if radix == codec.RADIX_ASCII:
            return "".join(chr(b) if 32 <= b < 127 else "." for b in value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big"))
        return str(value)
    if isinstance(value, float):
        return _trim_float(value)
    if isinstance(value, str):
        if radix in (codec.RADIX_HEX, codec.RADIX_DEC, codec.RADIX_BIN):
            data = value.encode("utf-8", errors="replace")
            return codec.bytes_to_text(data, radix, sep=" ")
        return codec.to_printable(value)
    if isinstance(value, (bytes, bytearray)):
        if radix == codec.RADIX_DEC:
            return codec.bytes_to_text(bytes(value), codec.RADIX_DEC)
        if radix == codec.RADIX_BIN:
            return codec.bytes_to_text(bytes(value), codec.RADIX_BIN)
        if radix == codec.RADIX_ASCII:
            return codec.bytes_to_text(bytes(value), codec.RADIX_ASCII)
        return codec.bytes_to_text(bytes(value), codec.RADIX_HEX)
    return str(value)


def safe_name(name: str) -> str:
    ident = re.sub(r"\W", "_", name or "")
    if not ident or ident[0].isdigit():
        ident = "f_" + ident
    return ident


# --------------------------------------------------------------------------- #
# 单个字段解析
# --------------------------------------------------------------------------- #
def _bcd_to_int(raw: bytes) -> tuple[int, str]:
    digits: list[str] = []
    note = ""
    for byte in raw:
        for nibble in (byte >> 4, byte & 0x0F):
            if nibble == 0x0F:      # 常见做法：0xF 表示结束/空位
                note = note or "含 0xF 空位，已忽略"
                continue
            if nibble > 9:
                raise ValueError(f"BCD 非法半字节 0x{nibble:X}")
            digits.append(str(nibble))
    return int("".join(digits) or "0"), note


def parse_field(data: bytes, fd: FieldDef, env: dict, frame_index: int = 0) -> ParsedField:
    length = fd.length
    if length <= 0:                      # 0 = 到帧尾
        length = max(0, len(data) - fd.offset)
    raw = data[fd.offset:fd.offset + length]
    result = ParsedField(
        name=fd.name, type=fd.type, type_label=fd.type_label,
        offset=fd.offset, length=length, raw=raw,
        raw_hex=codec.bytes_to_text(raw, codec.RADIX_HEX),
        raw_value=None, value=None, unit=fd.unit, radix=fd.radix,
        frame_index=frame_index,
    )

    if fd.offset < 0:
        result.ok, result.note = False, "偏移为负"
        return result
    if fd.offset > len(data):
        result.ok, result.note = False, f"偏移 {fd.offset} 超出帧长 {len(data)}"
        return result
    if len(raw) < length:
        result.ok = False
        result.note = f"数据不足：需要 {length} 字节，实际只有 {len(raw)} 字节"
        return result

    try:
        note = ""
        if fd.type in (TYPE_UINT, TYPE_INT):
            signed = fd.type == TYPE_INT
            raw_int = int.from_bytes(raw, fd.byte_order, signed=signed)
            if fd.bits:
                raw_int = extract_bits(raw_int, fd.bits)
            result.raw_value = raw_int
            result.value = _apply_scale(raw_int, fd)
        elif fd.type == TYPE_BOOL:
            raw_int = int.from_bytes(raw, fd.byte_order)
            if fd.bits:
                raw_int = extract_bits(raw_int, fd.bits)
            result.raw_value = raw_int
            result.value = bool(raw_int)
        elif fd.type == TYPE_FLOAT:
            if len(raw) not in (4, 8):
                raise ValueError(f"浮点长度只能是 4 或 8 字节，当前 {len(raw)}")
            fmt = ("<" if fd.byte_order == "little" else ">") + ("f" if len(raw) == 4 else "d")
            raw_value = struct.unpack(fmt, raw)[0]
            result.raw_value = raw_value
            result.value = _apply_scale(raw_value, fd)
        elif fd.type == TYPE_BCD:
            raw_int, note = _bcd_to_int(raw)
            result.raw_value = raw_int
            result.value = _apply_scale(raw_int, fd)
            result.note = note
        elif fd.type == TYPE_ASCII:
            text = raw.decode("utf-8", errors="replace").strip("\x00").strip()
            result.value = text
        elif fd.type == TYPE_BYTES:
            result.value = raw
        elif fd.type == TYPE_EXPR:
            value = _eval_expr(fd.expr, env)
            result.value = value
            result.raw_value = value if isinstance(value, (int, float)) else None
        else:
            raise ValueError(f"未知字段类型：{fd.type}")
    except Exception as exc:  # noqa: BLE001 - 单字段错误不影响其他字段
        result.ok = False
        result.note = str(exc) or exc.__class__.__name__
        return result

    env[safe_name(fd.name)] = result.value
    env[safe_name(fd.name) + "_raw"] = result.raw_value
    return result


def _apply_scale(value, fd: FieldDef):
    if fd.scale == 1 and fd.bias == 0:
        return value
    out = value * fd.scale + fd.bias
    if isinstance(value, int) and fd.scale == int(fd.scale) and fd.bias == int(fd.bias) and (fd.scale != 1 or fd.bias != 0):
        if isinstance(out, float) and out.is_integer() and abs(fd.scale) >= 1:
            return int(out)
    return out


def _eval_expr(expr: str, env: dict):
    expr = (expr or "").strip()
    if not expr:
        raise ValueError("表达式为空")
    return eval(expr, dict(_SAFE_GLOBALS), dict(env))  # noqa: S307 - 已限制内置函数


# --------------------------------------------------------------------------- #
# 整帧解析
# --------------------------------------------------------------------------- #
def parse_fields(data: bytes, fields: list[FieldDef], frame_index: int = 0) -> list[ParsedField]:
    env: dict = {}
    out: list[ParsedField] = []
    for fd in fields:
        parsed = parse_field(data, fd, env, frame_index)
        out.append(parsed)
    return out


def parse_all(frames, fields: list[FieldDef]) -> list[ParsedField]:
    out: list[ParsedField] = []
    for frame in frames:
        out.extend(parse_fields(frame.data, fields, frame.index))
    return out


def summarize(parsed: list[ParsedField], limit: int = 3) -> str:
    """帧列表里用的摘要：取前几个数值字段。"""
    parts: list[str] = []
    for item in parsed:
        if not item.ok or item.value is None:
            continue
        if isinstance(item.value, (bytes, str)) and not isinstance(item.value, str):
            continue
        text = format_value(item.value, item.radix)
        if not text:
            continue
        parts.append(f"{item.name}={text}{item.unit}")
        if len(parts) >= limit:
            break
    return "  ".join(parts)


__all__ = [
    "FieldDef", "ParsedField", "TYPE_ITEMS", "TYPE_LABELS", "RADIX_CHOICES",
    "parse_field", "parse_fields", "parse_all", "format_value", "summarize",
    "safe_name", "extract_bits", "parse_bits", "NUMERIC_TYPES",
]
