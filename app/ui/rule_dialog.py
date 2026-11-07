"""列规则设置对话框：用多条规则自由决定「数据表分析」显示哪些列。

例如：
* 一条规则铺前 16 个字节（HEX）
* 再加一条「从 65 起 4 列、每列 2 字节大端」→ 看四通道 int16
* 或者直接写偏移列表 `0,1,2,8-15`
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QMenu, QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem, QToolButton,
    QVBoxLayout,
)

from ..analysis import RULE_PRESETS, ColumnRule, resolve_rules
from . import theme
from .util import mono_font

HEADERS = ["列名", "起始偏移", "列数", "步长", "每列字节", "字节序", "显示进制", "偏移列表（可替代前几列）"]
WIDTHS = [90, 76, 60, 60, 76, 82, 86, 200]
WIDTH_CHOICES = [1, 2, 4, 8]
ORDER_CHOICES = [("big", "大端"), ("little", "小端")]
RADIX_CHOICES = [("hex", "HEX"), ("dec", "DEC"), ("bin", "BIN"), ("ascii", "ASCII")]


class ColumnRuleDialog(QDialog):
    """编辑一组列规则，并实时预览会生成哪些列。"""

    def __init__(self, rules: list[ColumnRule], frame_len: int = 0, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("列规则设置")
        self.resize(940, 520)
        self._frame_len = frame_len
        self._rules: list[ColumnRule] = [ColumnRule.from_dict(r.to_dict()) for r in rules] or [
            ColumnRule()
        ]

        layout = QVBoxLayout(self)
        tip = QLabel(
            "规则按顺序生成列：每行一条规则。<b>偏移列表</b>填了就按它挑列（如 <code>0,1,2,8-15</code>），"
            "忽略「起始偏移/列数/步长」；<b>每列字节</b>大于 1 时会按字节序合成一个数值列（可看 int16/int32）。"
        )
        tip.setWordWrap(True)
        layout.addWidget(tip)

        bar = QHBoxLayout()
        self.add_button = QPushButton("添加规则")
        self.insert_button = QPushButton("插入到下方")
        self.remove_button = QPushButton("删除选中")
        self.up_button = QPushButton("上移")
        self.down_button = QPushButton("下移")
        self.preset_button = QToolButton()
        self.preset_button.setText("常用规则 ▾")
        self.preset_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.preset_menu = QMenu(self.preset_button)
        self._build_preset_menu()
        self.preset_button.setMenu(self.preset_menu)
        for widget in (self.add_button, self.insert_button, self.remove_button,
                       self.up_button, self.down_button, self.preset_button):
            bar.addWidget(widget)
        bar.addStretch(1)
        layout.addLayout(bar)

        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        for index, width in enumerate(WIDTHS):
            self.table.setColumnWidth(index, width)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        layout.addWidget(self.table, 1)

        self.preview_label = QLabel("—")
        self.preview_label.setWordWrap(True)
        self.preview_label.setStyleSheet(f"color:{theme.FG};")
        layout.addWidget(self.preview_label)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(mono_font(9))
        self.preview.setMaximumHeight(96)
        layout.addWidget(self.preview)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("应用这些列")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.add_button.clicked.connect(lambda: self._add_row(len(self._rules)))
        self.insert_button.clicked.connect(
            lambda: self._add_row(max(0, self._current_row() + 1))
        )
        self.remove_button.clicked.connect(self._remove_row)
        self.up_button.clicked.connect(lambda: self._move(-1))
        self.down_button.clicked.connect(lambda: self._move(1))
        self.table.itemChanged.connect(self._on_item_changed)
        self._reload()

    # ------------------------------------------------------------------ #
    def _build_preset_menu(self) -> None:
        self.preset_menu.clear()
        for name, rules in RULE_PRESETS:
            action = self.preset_menu.addAction(name)
            action.triggered.connect(
                lambda _checked=False, r=rules: self._apply_preset(r)
            )
        self.preset_menu.addSeparator()
        self.preset_menu.addAction("追加为一条新规则", self._append_preset)
        self._pending_preset: list[ColumnRule] = []

    def _apply_preset(self, rules: list[ColumnRule]) -> None:
        self._rules = [ColumnRule.from_dict(r.to_dict()) for r in rules]
        self._reload()

    def _append_preset(self) -> None:
        if self._pending_preset:
            self._rules.extend(ColumnRule.from_dict(r.to_dict()) for r in self._pending_preset)
            self._reload()

    # ------------------------------------------------------------------ #
    def _reload(self) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._rules))
        for row, rule in enumerate(self._rules):
            values = [
                rule.name, str(rule.offset), str(rule.count), str(rule.stride),
                str(rule.width), rule.byte_order, rule.radix, rule.offsets,
            ]
            for col, text in enumerate(values):
                if col == 5:
                    combo = QComboBox()
                    for key, label in ORDER_CHOICES:
                        combo.addItem(label, key)
                    combo.setCurrentIndex(max(0, combo.findData(rule.byte_order)))
                    combo.currentIndexChanged.connect(
                        lambda _i, r=row, c=combo: self._set_combo(r, "byte_order", c)
                    )
                    self.table.setCellWidget(row, col, combo)
                    self.table.setItem(row, col, QTableWidgetItem(""))
                elif col == 6:
                    combo = QComboBox()
                    for key, label in RADIX_CHOICES:
                        combo.addItem(label, key)
                    combo.setCurrentIndex(max(0, combo.findData(rule.radix)))
                    combo.currentIndexChanged.connect(
                        lambda _i, r=row, c=combo: self._set_combo(r, "radix", c)
                    )
                    self.table.setCellWidget(row, col, combo)
                    self.table.setItem(row, col, QTableWidgetItem(""))
                else:
                    item = QTableWidgetItem(text)
                    if col == 7:
                        item.setToolTip("例如 0,1,2,8-15；填了就按它挑列")
                    self.table.setItem(row, col, item)
        self.table.blockSignals(False)
        self._update_preview()

    def _set_combo(self, row: int, attr: str, combo: QComboBox) -> None:
        if 0 <= row < len(self._rules):
            setattr(self._rules[row], attr, combo.currentData())
            self._update_preview()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        row, col = item.row(), item.column()
        if not (0 <= row < len(self._rules)):
            return
        text = item.text().strip()
        rule = self._rules[row]
        try:
            if col == 0:
                rule.name = text or "偏移"
            elif col == 1:
                rule.offset = int(text or 0)
            elif col == 2:
                rule.count = max(1, int(text or 1))
            elif col == 3:
                rule.stride = max(0, int(text or 0))
            elif col == 4:
                rule.width = max(1, int(text or 1))
            elif col == 7:
                rule.offsets = text
            item.setForeground(self.table.palette().text())
        except ValueError:
            item.setForeground(Qt.GlobalColor.black)
            item.setToolTip("这里要填数字")
            return
        self._update_preview()

    def _update_preview(self) -> None:
        try:
            columns, warnings = resolve_rules(self._rules, self._frame_len, limit=256)
        except ValueError as exc:
            self.preview_label.setText(f"✘ 规则有误：{exc}")
            self.preview.setPlainText("")
            return
        offsets = [column.offset for column in columns]
        span = f"偏移 {min(offsets)} ~ {max(offsets)}" if offsets else "无"
        text = f"共 {len(self._rules)} 条规则 → 生成 {len(columns)} 列（{span}）"
        if warnings:
            text += "　⚠ " + "；".join(warnings)
        self.preview_label.setText(text)
        head = [column.title + (f"（{column.width}字节/{column.byte_order}）"
                                if column.width > 1 else "") for column in columns[:24]]
        more = f"\n… 还有 {len(columns) - 24} 列" if len(columns) > 24 else ""
        self.preview.setPlainText("　".join(head) + more)

    # ------------------------------------------------------------------ #
    def _current_row(self) -> int:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        return rows[-1] if rows else len(self._rules) - 1

    def _add_row(self, at: int) -> None:
        self._rules.insert(max(0, min(at, len(self._rules))), ColumnRule(name="偏移", count=8))
        self._reload()
        self.table.selectRow(max(0, min(at, self.table.rowCount() - 1)))

    def _remove_row(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            if 0 <= row < len(self._rules):
                self._rules.pop(row)
        if not self._rules:
            self._rules = [ColumnRule()]
        self._reload()

    def _move(self, delta: int) -> None:
        row = self._current_row()
        target = row + delta
        if not (0 <= row < len(self._rules)) or not (0 <= target < len(self._rules)):
            return
        self._rules[row], self._rules[target] = self._rules[target], self._rules[row]
        self._reload()
        self.table.selectRow(target)

    # ------------------------------------------------------------------ #
    def rules(self) -> list[ColumnRule]:
        return [ColumnRule.from_dict(rule.to_dict()) for rule in self._rules]
