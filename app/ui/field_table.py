"""字段定义表：编辑「字段名 / 类型 / 偏移 / 长度 / 字节序 / 系数 / 位域 / 显示进制 / 表达式」。"""
from __future__ import annotations

import json
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import field_parser
from ..field_parser import FieldDef
from . import theme
from .table_base import ColumnSpec, SmartTable

TYPE_CHOICES = list(field_parser.TYPE_ITEMS)
ORDER_CHOICES = [("big", "大端"), ("little", "小端")]
RADIX_CHOICES = [(key, label) for key, label in field_parser.RADIX_CHOICES]

COLUMNS = [
    ColumnSpec("name", "字段名", width=110),
    ColumnSpec("type", "类型", width=100),
    ColumnSpec("offset", "偏移", width=54, align="center"),
    ColumnSpec("length", "长度", width=54, align="center"),
    ColumnSpec("byte_order", "字节序", width=74),
    ColumnSpec("scale", "系数", width=62, align="center"),
    ColumnSpec("bias", "偏置", width=58, align="center"),
    ColumnSpec("unit", "单位", width=56, align="center"),
    ColumnSpec("bits", "位域", width=56, align="center"),
    ColumnSpec("radix", "显示进制", width=104),
    ColumnSpec("expr", "表达式", width=180),
    ColumnSpec("note", "备注", width=200),
]

TEXT_KEYS = ("name", "offset", "length", "scale", "bias", "unit", "bits", "expr", "note")
COMBO_KEYS = {"type": TYPE_CHOICES, "byte_order": ORDER_CHOICES, "radix": RADIX_CHOICES}
NUMERIC_TEXT_KEYS = {"offset", "length"}
FLOAT_TEXT_KEYS = {"scale", "bias"}


class FieldTableWidget(QWidget):
    """字段定义编辑器。内部维护 FieldDef 模型，界面改动实时回写。"""

    changed = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._fields: list[FieldDef] = []
        self._loading = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        buttons = QHBoxLayout()
        self.add_button = QPushButton("添加字段")
        self.insert_button = QPushButton("插入到下方")
        self.remove_button = QPushButton("删除选中行")
        self.up_button = QPushButton("上移")
        self.down_button = QPushButton("下移")
        self.copy_button = QPushButton("复制行")
        self.clear_button = QPushButton("清空")
        self.import_button = QPushButton("导入字段 JSON")
        self.export_button = QPushButton("导出字段 JSON")
        for button in (
            self.add_button, self.insert_button, self.remove_button, self.up_button,
            self.down_button, self.copy_button, self.clear_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self.import_button)
        buttons.addWidget(self.export_button)
        layout.addLayout(buttons)

        self.table = SmartTable(COLUMNS, self, table_id="fields")
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        self.table.status_message.connect(self.changed)
        layout.addWidget(self.table, 1)

        tip = QLabel(
            "提示：长度填 <b>0</b> 表示「取到帧尾」（变长字段）；类型选「表达式计算」时在表达式列写公式，"
            "可引用前面字段名，例如 <code>航向 * 16.6667</code>。列标题左键=整列选择，右键=列颜色。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet(f"color:{theme.FG_DIM};")
        layout.addWidget(tip)

        self.add_button.clicked.connect(self.add_field)
        self.insert_button.clicked.connect(self.insert_field)
        self.remove_button.clicked.connect(self.remove_selected)
        self.up_button.clicked.connect(lambda: self.move_selected(-1))
        self.down_button.clicked.connect(lambda: self.move_selected(1))
        self.copy_button.clicked.connect(self.duplicate_selected)
        self.clear_button.clicked.connect(self.clear_all)
        self.import_button.clicked.connect(self.import_json)
        self.export_button.clicked.connect(self.export_json)
        self.table.itemChanged.connect(self._on_item_changed)
        self._rebuild()

    # ------------------------------------------------------------------ #
    # 模型 -> 界面
    # ------------------------------------------------------------------ #
    def _rebuild(self) -> None:
        self._loading = True
        rows: list[list[QTableWidgetItem | None]] = []
        for fd in self._fields:
            cells: list[QTableWidgetItem | None] = []
            for spec in COLUMNS:
                value = getattr(fd, spec.key)
                if spec.key in COMBO_KEYS:
                    combo = QComboBox()
                    for key, label in COMBO_KEYS[spec.key]:
                        combo.addItem(label, key)
                    index = combo.findData(value)
                    combo.setCurrentIndex(index if index >= 0 else 0)
                    combo.currentIndexChanged.connect(
                        lambda _idx, row=len(rows), key=spec.key, box=combo: self._on_combo(row, key, box)
                    )
                    cells.append(self._cell(""))
                    self.table.setCellWidget(len(rows), self._col_index(spec.key), combo)
                else:
                    text = self._format_text(spec.key, value)
                    cells.append(self._cell(text, editable=True))
            rows.append(cells)
        # set_item_rows 会重建 item，需在之后重新挂 combo（顺序问题），改为先设行再挂
        self.table.set_item_rows(rows)
        self._attach_combos()
        self._loading = False
        self.table.resizeColumnToContents(2)
        self.table.resizeColumnToContents(3)
        self.changed.emit()

    def _attach_combos(self) -> None:
        for row, fd in enumerate(self._fields):
            for key, choices in COMBO_KEYS.items():
                col = self._col_index(key)
                combo = QComboBox()
                for choice_key, label in choices:
                    combo.addItem(label, choice_key)
                index = combo.findData(getattr(fd, key))
                combo.setCurrentIndex(index if index >= 0 else 0)
                combo.currentIndexChanged.connect(
                    lambda _idx, r=row, k=key, box=combo: self._on_combo(r, k, box)
                )
                self.table.setCellWidget(row, col, combo)

    def _col_index(self, key: str) -> int:
        for index, spec in enumerate(COLUMNS):
            if spec.key == key:
                return index
        raise KeyError(key)

    def _cell(self, text: str, editable: bool = True) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        if not editable:
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        else:
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsEditable
            )
        return item

    def _format_text(self, key: str, value) -> str:
        if key in NUMERIC_TEXT_KEYS:
            return str(int(value or 0))
        if key in FLOAT_TEXT_KEYS:
            number = float(value or 0)
            return str(int(number)) if number == int(number) else str(number)
        return "" if value is None else str(value)

    # ------------------------------------------------------------------ #
    # 界面 -> 模型
    # ------------------------------------------------------------------ #
    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading:
            return
        row, col = item.row(), item.column()
        if row >= len(self._fields):
            return
        key = COLUMNS[col].key
        if key not in TEXT_KEYS:
            return
        text = item.text().strip()
        try:
            if key in NUMERIC_TEXT_KEYS:
                setattr(self._fields[row], key, int(text or 0))
            elif key in FLOAT_TEXT_KEYS:
                setattr(self._fields[row], key, float(text or 0))
            else:
                setattr(self._fields[row], key, text)
            item.setForeground(self.table.palette().text())
            item.setBackground(self.table.palette().base())
        except ValueError:
            item.setForeground(QBrush(QColor(theme.FAIL_FG)))
            item.setBackground(QBrush(QColor(theme.ERROR_BG)))
            item.setToolTip(f"「{COLUMNS[col].title}」需要数字，当前为 {text!r}")
            return
        self.changed.emit()

    def _on_combo(self, row: int, key: str, combo: QComboBox) -> None:
        if self._loading or row >= len(self._fields):
            return
        setattr(self._fields[row], key, combo.currentData())
        self.changed.emit()

    # ------------------------------------------------------------------ #
    # 行操作
    # ------------------------------------------------------------------ #
    def _selected_rows(self) -> list[int]:
        return sorted({index.row() for index in self.table.selectedIndexes()})

    def _current_row(self) -> int:
        rows = self._selected_rows()
        if rows:
            return rows[-1]
        return max(0, len(self._fields) - 1)

    def add_field(self) -> None:
        self._fields.append(self._default_field(len(self._fields)))
        self._rebuild()

    def insert_field(self) -> None:
        row = self._current_row()
        self._fields.insert(row + 1, self._default_field(row + 1))
        self._rebuild()

    def remove_selected(self) -> None:
        rows = self._selected_rows()
        if not rows:
            return
        for row in reversed(rows):
            if 0 <= row < len(self._fields):
                self._fields.pop(row)
        self._rebuild()

    def move_selected(self, delta: int) -> None:
        row = self._current_row()
        target = row + delta
        if not (0 <= row < len(self._fields)) or not (0 <= target < len(self._fields)):
            return
        self._fields[row], self._fields[target] = self._fields[target], self._fields[row]
        self._rebuild()
        self.table.selectRow(target)

    def duplicate_selected(self) -> None:
        row = self._current_row()
        if not (0 <= row < len(self._fields)):
            return
        clone = FieldDef.from_dict(self._fields[row].to_dict())
        clone.name = clone.name + "_copy"
        self._fields.insert(row + 1, clone)
        self._rebuild()

    def clear_all(self) -> None:
        if not self._fields:
            return
        if QMessageBox.question(self, "清空字段表", "确定清空所有字段定义？") != QMessageBox.StandardButton.Yes:
            return
        self._fields = []
        self._rebuild()

    def _default_field(self, index: int) -> FieldDef:
        offset = 0
        for fd in self._fields:
            offset = max(offset, fd.offset + max(1, fd.length))
        return FieldDef(name=f"字段{index + 1}", type=field_parser.TYPE_UINT,
                        offset=offset, length=1)

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def fields(self) -> list[FieldDef]:
        return [FieldDef.from_dict(fd.to_dict()) for fd in self._fields]

    def set_fields(self, fields: list[dict] | list[FieldDef]) -> None:
        self._fields = [
            fd if isinstance(fd, FieldDef) else FieldDef.from_dict(fd) for fd in (fields or [])
        ]
        self._rebuild()

    def column_state(self) -> dict:
        return self.table.column_state()

    def apply_column_state(self, state: dict) -> None:
        self.table.apply_column_state(state)

    def import_json(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "导入字段定义", "", "JSON 文件 (*.json);;所有文件 (*)")
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data = data.get("fields", [])
            self.set_fields(data)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "导入失败", f"{exc}")

    def export_json(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "导出字段定义", "fields.json", "JSON 文件 (*.json)"
        )
        if not path:
            return
        try:
            Path(path).write_text(
                json.dumps([fd.to_dict() for fd in self._fields], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "导出失败", f"{exc}")
