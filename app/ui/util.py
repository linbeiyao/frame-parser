"""界面通用工具：字体、剪贴板、尺寸常量。"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QGuiApplication

MONO_FAMILIES = ["Consolas", "Cascadia Mono", "JetBrains Mono", "DejaVu Sans Mono", "Courier New"]
UI_FAMILIES = ["Microsoft YaHei UI", "Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Sans Serif"]


def mono_font(size: int = 10) -> QFont:
    font = QFont()
    font.setFamilies(MONO_FAMILIES)
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPointSize(size)
    return font


def ui_font(size: int = 9) -> QFont:
    font = QFont()
    font.setFamilies(UI_FAMILIES)
    font.setPointSize(size)
    return font


def copy_to_clipboard(text: str) -> None:
    clipboard = QGuiApplication.clipboard()
    if clipboard is not None:
        clipboard.setText(text or "")


def align_flag(name: str) -> Qt.AlignmentFlag:
    return {
        "left": Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
        "center": Qt.AlignmentFlag.AlignCenter,
        "right": Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
    }.get(name, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
