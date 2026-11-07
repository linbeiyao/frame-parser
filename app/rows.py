"""把解析结果转成"表格行"的纯函数（不含 Qt，方便管线/命令行/测试共用）。

行的约定见 SmartTable.set_rows：
    {列key: 原始值}  +  `_fg` / `_bg`（颜色覆盖）、`_text`（列格式为 auto 时的文本）、
    `_tip`（追加到悬停提示的信息）
"""
from __future__ import annotations

from . import field_parser
from .field_parser import FieldDef, ParsedField
from .ui import theme      # 只有颜色常量，不含 Qt 依赖


def field_row(item: ParsedField, fd: FieldDef, frame_offset: int = 0) -> dict:
    """一个字段 → 字段解析表的一行。"""
    note = item.note
    if not note:
        if fd.type == field_parser.TYPE_EXPR and fd.expr:
            note = f"= {fd.expr}"
        elif fd.bits:
            note = f"位域 {fd.bits}"
        else:
            note = fd.note

    tips: list[tuple[str, str]] = [
        ("字段", item.name),
        ("类型", item.type_label),
        ("位置", f"偏移 {item.offset}，长度 {item.length}"),
    ]
    if fd.expr:
        tips.append(("表达式", fd.expr))
    if fd.bits:
        tips.append(("位域", fd.bits))
    if fd.note:
        tips.append(("备注", fd.note))

    row = {
        "frame": item.frame_index,
        "name": item.name,
        "type": item.type_label,
        "offset": item.offset,
        "length": item.length,
        "raw_hex": item.raw_hex,
        "raw_value": item.raw_value,
        "value": item.value,
        "unit": item.unit,
        "note": note,
        "ok": item.ok,
        #: (绝对字节偏移, 字节数) —— 界面据此在左侧原文里反色定位
        "_bytes": (frame_offset + item.offset, item.length),
        "_text": {"value": field_parser.format_value(item.value, item.radix)},
        "_tip": {"value": tips, "frame": [("帧号", item.frame_index)]},
    }
    if not item.ok:
        row["_fg"] = {"note": theme.FAIL_FG, "value": theme.FAIL_FG, "name": theme.FAIL_FG}
        row["_bg"] = {"note": theme.ERROR_BG, "value": theme.ERROR_BG}
    return row


def field_rows(parsed: list[ParsedField], fields: list[FieldDef]) -> list[dict]:
    return [field_row(item, fd) for fd, item in zip(fields, parsed)]
