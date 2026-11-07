"""把字节导出成各种常用文本格式（复制/贴到代码或文档里）。"""
from __future__ import annotations

import base64

from . import codec

MODES: list[tuple[str, str]] = [
    ("hex", "HEX 文本  (AA 55 01)"),
    ("hex_prefix", "HEX 带前缀  (0xAA 0x55)"),
    ("c_array", "C 数组  {0xAA, 0x55}"),
    ("python", "Python bytes  b'\\xAA\\x55'"),
    ("python_list", "Python 列表  [0xAA, 0x55]"),
    ("dec_list", "十进制数组  {170, 85}"),
    ("bin", "二进制  (10101010)"),
    ("hexdump", "hexdump  (偏移+HEX+ASCII)"),
    ("base64", "Base64"),
    ("ascii", "ASCII 转义文本"),
]


def export_bytes(data: bytes, mode: str = "hex", per_line: int = 16, name: str = "data") -> str:
    if not data:
        return ""
    if mode == "hex":
        return _wrapped([f"{b:02X}" for b in data], per_line, joiner=" ")
    if mode == "hex_prefix":
        return _wrapped([f"0x{b:02X}" for b in data], per_line, joiner=" ")
    if mode == "c_array":
        body = _wrapped([f"0x{b:02X}" for b in data], per_line, indent="    ")
        return f"const unsigned char {name}[{len(data)}] = {{\n{body}\n}};"
    if mode == "python":
        body = _wrapped([f"\\x{b:02X}" for b in data], per_line)
        return f"data = b\"{body}\""
    if mode == "python_list":
        body = _wrapped([f"0x{b:02X}" for b in data], per_line, indent="    ")
        return f"{name} = [\n{body}\n]"
    if mode == "dec_list":
        body = _wrapped([str(b) for b in data], per_line, indent="    ")
        return f"const unsigned char {name}[{len(data)}] = {{\n{body}\n}};"
    if mode == "bin":
        return _wrapped([format(b, "08b") for b in data], max(1, per_line // 2), joiner=" ")
    if mode == "hexdump":
        return codec.hexdump(data)
    if mode == "base64":
        return base64.b64encode(data).decode("ascii")
    if mode == "ascii":
        return codec.to_printable(data.decode("utf-8", errors="replace"))
    raise ValueError(f"未知导出格式：{mode}")


def _wrapped(items: list[str], per_line: int, indent: str = "", joiner: str = ", ") -> str:
    per_line = max(1, per_line)
    lines = []
    for index in range(0, len(items), per_line):
        lines.append(indent + joiner.join(items[index:index + per_line]))
    return "\n".join(lines)


def export_rows(header: list[str], rows: list[list[str]], delimiter: str = ",") -> str:
    """把表格导成 CSV/TSV 文本。"""
    def cell(value: str) -> str:
        text = "" if value is None else str(value)
        if delimiter in text or '"' in text or "\n" in text:
            return '"' + text.replace('"', '""') + '"'
        return text

    lines = [delimiter.join(cell(h) for h in header)]
    lines.extend(delimiter.join(cell(v) for v in row) for row in rows)
    return "\n".join(lines)
