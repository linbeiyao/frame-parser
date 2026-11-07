"""列换算函数：给任意一列绑定一个表达式，把原始值换算成工程值。

用法（表达式里可用变量）
------------------------
- `v` / `x` / `value` : 该单元格的原始值
- 可用函数            : abs / round / min / max / pow / int / float / sqrt / log / exp / sin / cos / atan2 / hex / bin / oct
- 可用常量            : pi / e

例：`v * 0.1`、`v / 1000`、`v - 40`、`(v >> 4) & 0xF`、`v * 360 / 65536`

换算结果会作为该列的显示值（再按列的显示进制渲染），
鼠标悬停在单元格上时，会同时看到原始值、换算函数、换算结果和 HEX/DEC/BIN/ASCII 各种进制的值。
"""
from __future__ import annotations

import html
import math

from . import codec
from .field_parser import format_value

#: 常用换算函数快捷按钮：(名称, 表达式)
FUNC_PRESETS: list[tuple[str, str]] = [
    ("原值", "v"),
    ("×0.1", "v * 0.1"),
    ("×0.01", "v * 0.01"),
    ("×0.001", "v * 0.001"),
    ("÷10", "v / 10"),
    ("÷100", "v / 100"),
    ("÷1000", "v / 1000"),
    ("偏置 -40", "v - 40"),
    ("偏置 +40", "v + 40"),
    ("÷10 温度", "v / 10"),
    ("弧度→度", "v * 180 / pi"),
    ("度→弧度", "v * pi / 180"),
    ("编码器→度", "v * 360 / 65536"),
    ("取低 8 位", "v & 0xFF"),
    ("取位 0-3", "(v >> 0) & 0xF"),
    ("取位 4-7", "(v >> 4) & 0xF"),
    ("12 位符号扩展", "v - 4096 if v & 0x800 else v"),
    ("HEX 文本", "hex(int(v))"),
    ("BIN 文本", "bin(int(v))"),
    ("绝对值", "abs(v)"),
]

_TWO32 = 0xFFFFFFFF
_TWO64 = 0xFFFFFFFFFFFFFFFF

SAFE_GLOBALS: dict = {
    "__builtins__": {},
    "abs": abs, "round": round, "min": min, "max": max, "pow": pow,
    "int": int, "float": float, "bool": bool, "len": len,
    "hex": hex, "bin": bin, "oct": oct,
    "sqrt": math.sqrt, "log": math.log, "log10": math.log10, "exp": math.exp,
    "sin": math.sin, "cos": math.cos, "tan": math.tan, "atan2": math.atan2,
    "floor": math.floor, "ceil": math.ceil, "pi": math.pi, "e": math.e,
}


class ConvertError(ValueError):
    """换算表达式执行失败。"""


def apply_convert(expr: str, raw, extra: dict | None = None):
    """按表达式换算；expr 为空时原样返回。失败抛 ConvertError。"""
    if expr is None or not str(expr).strip():
        return raw
    env = {"v": raw, "x": raw, "value": raw, "raw": raw}
    if extra:
        env.update(extra)
    try:
        return eval(str(expr), dict(SAFE_GLOBALS), env)  # noqa: S307 - 已禁用内置函数
    except Exception as exc:  # noqa: BLE001
        raise ConvertError(f"{exc.__class__.__name__}: {exc}") from exc


def signed(value: int, bits: int) -> int:
    """把无符号整数按位宽解释成有符号数。"""
    if value is None:
        return value
    mask = (1 << bits) - 1
    value &= mask
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def is_numeric(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def radix_variants(value) -> dict[str, str]:
    """同一个值的多种进制文本，用于悬停提示。"""
    out: dict[str, str] = {}
    if value is None:
        return out
    if isinstance(value, bool):
        out["DEC"] = "1" if value else "0"
        return out
    if isinstance(value, int):
        out["HEX"] = f"0x{value:X}" if value >= 0 else f"-0x{-value:X}"
        out["DEC"] = str(value)
        out["BIN"] = f"0b{value:b}" if value >= 0 else f"-0b{-value:b}"
        if 0 <= value < 0x110000:
            try:
                ch = chr(value)
                out["ASCII"] = ch if ch.isprintable() else "-"
            except ValueError:
                pass
        if value <= _TWO32:
            out["U32"] = f"0x{value & _TWO32:08X}"
        if value > 0x7FFFFFFF and value <= _TWO64:
            out["I64"] = str(signed(value & _TWO64, 64))
        return out
    if isinstance(value, float):
        out["DEC"] = format(value, ".10g")
        out["FLOAT"] = repr(value)
        try:
            import struct as _struct
            out["HEX(f32)"] = " ".join(
                f"{b:02X}" for b in _struct.pack(">f", value)
            )
        except Exception:  # noqa: BLE001
            pass
        return out
    if isinstance(value, bytes):
        out["HEX"] = codec.bytes_to_text(value, codec.RADIX_HEX)
        out["DEC"] = codec.bytes_to_text(value, codec.RADIX_DEC)
        out["BIN"] = codec.bytes_to_text(value, codec.RADIX_BIN)
        return out
    if isinstance(value, str):
        out["ASCII"] = codec.to_printable(value)
        return out
    return out


def build_tooltip(
    title: str = "",
    raw=None,
    converted=None,
    text: str = "",
    unit: str = "",
    expr: str = "",
    note: str = "",
    extra_rows: list[tuple[str, str]] | None = None,
) -> str:
    """把原始值 / 换算 / 多进制值拼成 HTML 悬停提示。"""
    lines: list[str] = []
    if title:
        lines.append(f"<b>{html.escape(str(title))}</b>")
    if extra_rows:
        for key, value in extra_rows:
            if value not in (None, ""):
                lines.append(f"{html.escape(str(key))}：{html.escape(str(value))}")
    if raw is not None and raw != "":
        variants = radix_variants(raw)
        text_variants = "　".join(f"<b>{k}</b> {html.escape(v)}" for k, v in variants.items())
        lines.append(f"原始值：{html.escape(str(raw))}")
        if text_variants:
            lines.append(text_variants)
    if expr:
        lines.append(f"换算函数：<code>{html.escape(str(expr))}</code>")
    if converted is not None and converted != raw:
        lines.append(f"换算结果：{html.escape(format_value(converted, 'auto'))}")
        variants = radix_variants(converted)
        text_variants = "　".join(f"<b>{k}</b> {html.escape(v)}" for k, v in variants.items())
        if text_variants:
            lines.append(text_variants)
    if text:
        lines.append(f"显示：{html.escape(text)}{html.escape(unit or '')}")
    if note:
        lines.append(f"<span style='color:#c0392b'>{html.escape(note)}</span>")
    return "<br>".join(lines)


def check_expression(expr: str, sample=None) -> tuple[bool, str]:
    """校验表达式是否可用，返回 (是否通过, 提示文本)。"""
    if not expr or not str(expr).strip():
        return True, "未设置换算函数（显示原始值）"
    try:
        result = apply_convert(expr, sample if sample is not None else 1)
    except ConvertError as exc:
        return False, f"表达式有误：{exc}"
    return True, f"样例 v={sample if sample is not None else 1} → {format_value(result, 'auto')}"
