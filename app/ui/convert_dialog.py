"""换算函数编辑对话框。"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QVBoxLayout,
)

from .. import convert
from . import theme


class ConvertDialog(QDialog):
    """给某一列设置换算函数：v 为原始值。"""

    def __init__(self, column_title: str, expr: str = "", sample=None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"列换算函数 —— {column_title}")
        self.resize(560, 380)
        self._sample = sample

        layout = QVBoxLayout(self)
        tip = QLabel(
            f"对「{column_title}」列的每个单元格做换算。表达式里用 <b>v</b> 表示原始值，"
            "可用 abs / round / min / max / pow / int / float / sqrt / log / sin / cos / atan2 / hex / bin，"
            "常量 pi、e。留空表示不做换算。"
        )
        tip.setWordWrap(True)
        layout.addWidget(tip)

        row = QHBoxLayout()
        row.addWidget(QLabel("换算函数 v →"))
        self.expr_edit = QLineEdit(expr or "")
        self.expr_edit.setPlaceholderText("例如  v * 0.1    /    v / 1000    /    (v >> 4) & 0xF")
        row.addWidget(self.expr_edit, 1)
        layout.addLayout(row)

        self.preview = QLabel()
        self.preview.setStyleSheet(f"color:{theme.FG};")
        layout.addWidget(self.preview)

        group = QGroupBox("常用换算（点一下即填入）")
        grid = QGridLayout(group)
        for index, (name, expression) in enumerate(convert.FUNC_PRESETS):
            button = QPushButton(name)
            button.setToolTip(expression)
            button.clicked.connect(lambda _=False, e=expression: self.expr_edit.setText(e))
            grid.addWidget(button, index // 5, index % 5)
        layout.addWidget(group)
        layout.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        clear = buttons.addButton("清除换算函数", QDialogButtonBox.ButtonRole.ResetRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        clear.clicked.connect(self._clear)
        layout.addWidget(buttons)

        self.expr_edit.textChanged.connect(self._update_preview)
        self._update_preview()

    # ------------------------------------------------------------------ #
    def _clear(self) -> None:
        self.expr_edit.setText("")
        self.accept()

    def _update_preview(self) -> None:
        ok, message = convert.check_expression(self.expr_edit.text(), self._sample)
        self.preview.setText(("✔ " if ok else "✘ ") + message)
        self.preview.setStyleSheet(
            f"color:{theme.FG};" if ok else f"color:{theme.FAIL_FG}; font-weight:bold;"
        )

    def expression(self) -> str:
        return self.expr_edit.text().strip()

    def set_sample(self, sample) -> None:
        self._sample = sample
        self._update_preview()
