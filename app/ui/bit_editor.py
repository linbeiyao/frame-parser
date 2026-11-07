"""位编辑器：点一下翻转某个 bit，看帧内容、字段和校验怎么变。

没有协议文档时特别有用：改一个位 → 看下位机/回传数据的变化 → 反推这个位的含义。
支持整列置 1 / 清 0 / 取反（列标题右键「快捷操作」），以及按当前分帧设置重算校验。
"""
from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel,
    QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from .. import checksum, codec, framer
from . import theme
from .table_base import ColumnSpec, SmartTable
from .util import copy_to_clipboard, mono_font

CHANGED_COLOR = theme.CHANGED_BG
BIT_COLUMNS = [f"b{bit}" for bit in range(7, -1, -1)]

COLUMNS = [
    ColumnSpec("offset", "偏移", width=64, align="center"),
    ColumnSpec("hex", "HEX", width=54, align="center", monospace=True),
    ColumnSpec("dec", "DEC", width=54, align="center", monospace=True),
] + [
    ColumnSpec(key, key, width=34, align="center") for key in BIT_COLUMNS
]

MAX_VIEW_BYTES = 4096


class BitEditorPanel(QWidget):
    """位编辑器面板。"""

    set_input_requested = pyqtSignal(bytes)
    status_message = pyqtSignal(str)
    #: 选中的字节对应 (起始字节, 字节数) —— 左侧原文反色定位
    bytes_requested = pyqtSignal(int, int)

    def __init__(self, config_provider: Callable[[], framer.FrameConfig], parent=None) -> None:
        super().__init__(parent)
        self._config_provider = config_provider
        self._source = b""
        self._original = b""
        self._bytes = bytearray()
        self._base_offset = 0
        self._label = ""
        self._updating = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        bar = QHBoxLayout()
        self.source_combo = QComboBox()
        self.source_combo.addItem("当前选中帧", "frame")
        self.source_combo.addItem("全部输入数据", "all")
        self.offset_spin = QSpinBox()
        self.offset_spin.setRange(0, 1 << 20)
        self.offset_spin.setToolTip("从第几个字节开始编辑（相对当前数据源）")
        self.count_spin = QSpinBox()
        self.count_spin.setRange(8, MAX_VIEW_BYTES)
        self.count_spin.setSingleStep(16)
        self.count_spin.setValue(256)
        self.count_spin.setSuffix(" 字节")
        self.auto_checksum_box = QCheckBox("改完自动重算校验")
        self.auto_checksum_box.setChecked(True)

        bar.addWidget(QLabel("数据源"))
        bar.addWidget(self.source_combo)
        bar.addWidget(QLabel("起始偏移"))
        bar.addWidget(self.offset_spin)
        bar.addWidget(QLabel("显示"))
        bar.addWidget(self.count_spin)
        bar.addWidget(self.auto_checksum_box)
        bar.addStretch(1)
        layout.addLayout(bar)

        buttons = QHBoxLayout()
        self.reset_button = QPushButton("恢复原始值")
        self.invert_button = QPushButton("所有位取反")
        self.clear_button = QPushButton("全部清 0")
        self.checksum_button = QPushButton("重算校验")
        self.copy_button = QPushButton("复制结果 HEX")
        self.save_button = QPushButton("保存为 .bin")
        self.apply_button = QPushButton("设为输入数据（仅此段）")
        for button in (self.reset_button, self.invert_button, self.clear_button,
                       self.checksum_button, self.copy_button, self.save_button,
                       self.apply_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.info_label = QLabel("—")
        self.info_label.setStyleSheet(f"color:{theme.FG};")
        layout.addWidget(self.info_label)

        self.table = SmartTable(COLUMNS, self, table_id="bits", row_numbers=False)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.DoubleClicked
        )
        self.table.header_menu_hook = self._column_actions
        self.table.selection_summary_provider = self._byte_selection_summary
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._on_table_selection)
        layout.addWidget(self.table, 1)

        self.result_view = QPlainTextEdit()
        self.result_view.setReadOnly(True)
        self.result_view.setFont(mono_font(10))
        self.result_view.setMaximumHeight(120)
        self.result_view.setPlaceholderText("编辑后的 HEX 结果")
        layout.addWidget(self.result_view)

        self.source_combo.currentIndexChanged.connect(self.refresh)
        self.offset_spin.valueChanged.connect(self.refresh)
        self.count_spin.valueChanged.connect(self.refresh)
        self.reset_button.clicked.connect(self.reset_bytes)
        self.invert_button.clicked.connect(self.invert_all)
        self.clear_button.clicked.connect(self.clear_all_bits)
        self.checksum_button.clicked.connect(self.recompute_checksum)
        self.copy_button.clicked.connect(self.copy_result)
        self.save_button.clicked.connect(self.save_binary)
        self.apply_button.clicked.connect(self._apply_to_input)
        self.auto_checksum_box.toggled.connect(lambda _v: self._after_change())

    # ------------------------------------------------------------------ #
    # 数据源
    # ------------------------------------------------------------------ #
    def set_source(self, all_data: bytes, frames, current_index: int) -> None:
        self._frames = frames          # 惰性帧视图，用到哪一帧才取哪一帧
        self._current = current_index
        self._all_data = all_data
        self.refresh()

    def _pick_source(self) -> tuple[bytes, str, int]:
        if self.source_combo.currentData() == "frame":
            for frame in getattr(self, "_frames", []):
                if frame.index == getattr(self, "_current", -1):
                    return frame.data, f"当前帧 #{frame.index}", frame.offset
            return b"", "当前没有选中帧", 0
        return getattr(self, "_all_data", b""), "全部输入数据", 0

    def refresh(self) -> None:
        data, label, base = self._pick_source()
        self._source = data
        self._label = label
        if bytes(self._bytes) != data or self._base_offset != base:
            self._original = data
            self._bytes = bytearray(data)
        self._base_offset = base
        self._render_table()
        self._after_change()

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #
    def _render_table(self) -> None:
        self._updating = True
        try:
            start = min(self.offset_spin.value(), max(0, len(self._bytes) - 1))
            count = min(self.count_spin.value(), max(0, len(self._bytes) - start))
            rows: list[list[QTableWidgetItem | None]] = []
            for index in range(start, start + count):
                byte = self._bytes[index]
                offset_item = QTableWidgetItem(str(self._base_offset + index))
                offset_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                hex_item = QTableWidgetItem(f"{byte:02X}")
                hex_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                hex_item.setFont(mono_font(10))
                dec_item = QTableWidgetItem(str(byte))
                dec_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                cells: list[QTableWidgetItem | None] = [offset_item, hex_item, dec_item]
                for key in BIT_COLUMNS:
                    bit = int(key[1:])
                    item = QTableWidgetItem("")
                    item.setFlags(
                        Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                        | Qt.ItemFlag.ItemIsUserCheckable
                    )
                    item.setCheckState(
                        Qt.CheckState.Checked if (byte >> bit) & 1 else Qt.CheckState.Unchecked
                    )
                    item.setToolTip(f"偏移 {self._base_offset + index} 的 b{bit}"
                                   f"（置 1 = +{1 << bit}）")
                    cells.append(item)
                rows.append(cells)
            self.table.set_item_rows(rows)
        finally:
            self._updating = False
        self._mark_changed()

    def _mark_changed(self) -> None:
        start = min(self.offset_spin.value(), max(0, len(self._bytes) - 1))
        for row in range(self.table.rowCount()):
            index = start + row
            if index >= len(self._bytes):
                break
            changed = index < len(self._original) and self._bytes[index] != self._original[index]
            for col in range(3):
                item = self.table.item(row, col)
                if item is not None:
                    item.setBackground(
                        QColor(CHANGED_COLOR) if changed else QColor(Qt.GlobalColor.transparent)
                    )

    # ------------------------------------------------------------------ #
    # 编辑
    # ------------------------------------------------------------------ #
    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating:
            return
        col = item.column()
        if col < 3:
            return
        key = COLUMNS[col].key
        if not key.startswith("b"):
            return
        bit = int(key[1:])
        row = item.row()
        index = self.offset_spin.value() + row
        if index >= len(self._bytes):
            return
        checked = item.checkState() == Qt.CheckState.Checked
        byte = self._bytes[index]
        new_byte = (byte | (1 << bit)) if checked else (byte & ~(1 << bit))
        if new_byte == byte:
            return
        self._bytes[index] = new_byte
        self._updating = True
        try:
            hex_item = self.table.item(row, 1)
            dec_item = self.table.item(row, 2)
            if hex_item is not None:
                hex_item.setText(f"{new_byte:02X}")
            if dec_item is not None:
                dec_item.setText(str(new_byte))
        finally:
            self._updating = False
        self._mark_changed()
        bit_name = f"偏移 {self._base_offset + index} 的 b{bit}"
        self.status_message.emit(f"{bit_name} → {'1' if checked else '0'}（字节变为 0x{new_byte:02X}）")
        self._after_change(auto_checksum=True)

    def _after_change(self, auto_checksum: bool = False) -> None:
        changed = sum(
            1 for index, byte in enumerate(self._bytes)
            if index >= len(self._original) or self._original[index] != byte
        )
        if changed and auto_checksum and self.auto_checksum_box.isChecked():
            self._patch_checksum(silent=True)
        state, note = framer.verify_checksum(bytes(self._bytes), self._config_provider())
        state_text = {"ok": "✓ 校验通过", "fail": "✗ 校验失败", "na": "无校验"}.get(state, state)
        text = f"{self._label}｜{len(self._bytes)} 字节｜已修改 {changed} 个字节｜{state_text}"
        if note:
            text += f"（{note}）"
        self.info_label.setText(text)
        self.info_label.setStyleSheet(
            f"color:{theme.FAIL_FG}; font-weight:bold;" if state == "fail"
            else f"color:{theme.FG};"
        )
        self.result_view.setPlainText(self._result_text())

    def _result_text(self) -> str:
        data = bytes(self._bytes)
        limit = 4096
        text = codec.bytes_to_text(data[:limit], codec.RADIX_HEX, sep=" ")
        if len(data) > limit:
            text += f"\n… 共 {len(data)} 字节，只显示前 {limit} 字节（复制/保存得到的是完整数据）"
        return text

    # ------------------------------------------------------------------ #
    # 批量操作
    # ------------------------------------------------------------------ #
    def reset_bytes(self) -> None:
        self._bytes = bytearray(self._original)
        self._render_table()
        self._after_change()
        self.status_message.emit("已恢复原始值")

    def invert_all(self) -> None:
        for index in range(len(self._bytes)):
            self._bytes[index] ^= 0xFF
        self._render_table()
        self._after_change(auto_checksum=True)
        self.status_message.emit("已把所有位取反")

    def clear_all_bits(self) -> None:
        start = self.offset_spin.value()
        count = min(self.count_spin.value(), max(0, len(self._bytes) - start))
        for index in range(start, start + count):
            self._bytes[index] = 0
        self._render_table()
        self._after_change(auto_checksum=True)
        self.status_message.emit(f"已把偏移 {start} 起的 {count} 字节清 0")

    def set_bit_column(self, bit: int, value: int) -> None:
        start = self.offset_spin.value()
        count = min(self.count_spin.value(), max(0, len(self._bytes) - start))
        for index in range(start, start + count):
            if value:
                self._bytes[index] |= (1 << bit)
            else:
                self._bytes[index] &= ~(1 << bit)
        self._render_table()
        self._after_change(auto_checksum=True)
        self.status_message.emit(f"已把 b{bit} 整列设为 {value}")

    def invert_bit_column(self, bit: int) -> None:
        start = self.offset_spin.value()
        count = min(self.count_spin.value(), max(0, len(self._bytes) - start))
        for index in range(start, start + count):
            self._bytes[index] ^= (1 << bit)
        self._render_table()
        self._after_change(auto_checksum=True)
        self.status_message.emit(f"已把 b{bit} 整列取反")

    def _column_actions(self, menu, col: int) -> None:
        key = COLUMNS[col].key
        if key.startswith("b"):
            bit = int(key[1:])
            menu.addAction(f"把 b{bit} 整列置 1", lambda: self.set_bit_column(bit, 1))
            menu.addAction(f"把 b{bit} 整列清 0", lambda: self.set_bit_column(bit, 0))
            menu.addAction(f"把 b{bit} 整列取反", lambda: self.invert_bit_column(bit))
        elif key == "hex":
            menu.addAction("本列全部取反", self.invert_all)
            menu.addAction("本列全部清 0", self.clear_all_bits)
        menu.addAction("恢复原始值", self.reset_bytes)

    # ------------------------------------------------------------------ #
    # 校验
    # ------------------------------------------------------------------ #
    def _patch_checksum(self, silent: bool = False) -> bool:
        cfg = self._config_provider()
        size = checksum.algo_size(cfg.checksum_algo)
        if not size:
            if not silent:
                QMessageBox.information(self, "重算校验", "当前分帧设置里没有选择校验算法。")
            return False
        data = self._bytes
        if len(data) <= size:
            return False
        if cfg.checksum_where == framer.CHECK_TAIL:
            body = bytes(data[:-size])
            data[-size:] = checksum.compute(cfg.checksum_algo, body, cfg.checksum_byte_order)
        else:
            start = cfg.len_offset + cfg.len_size
            if start + size > len(data):
                return False
            body = bytes(data[:start]) + bytes(data[start + size:])
            data[start:start + size] = checksum.compute(cfg.checksum_algo, body, cfg.checksum_byte_order)
        if not silent:
            self._render_table()
            self._after_change()
            self.status_message.emit(
                f"已按 {checksum.ALGO_LABELS.get(cfg.checksum_algo, cfg.checksum_algo)} 重算校验字节"
            )
        return True

    def recompute_checksum(self) -> None:
        self._patch_checksum(silent=False)

    # ------------------------------------------------------------------ #
    # 输出
    # ------------------------------------------------------------------ #
    def _on_table_selection(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        if not rows:
            return
        self.bytes_requested.emit(
            self._base_offset + self.offset_spin.value() + rows[0], len(rows)
        )

    def result_bytes(self) -> bytes:
        return bytes(self._bytes)

    def _byte_selection_summary(self, rows: list[int], cols: list[int]) -> str:
        """选中若干字节 → 报出字节数与各进制取值（单位选一行时还会报位组成）。"""
        from .results import byte_summary_text
        start = self.offset_spin.value()
        picked = bytes(
            self._bytes[start + row] for row in rows if 0 <= start + row < len(self._bytes)
        )
        if not picked:
            return ""
        text = byte_summary_text(picked, offset=self._base_offset + start + (rows[0] if rows else 0))
        if len(picked) == 1:
            byte = picked[0]
            text += "　│　位: " + " ".join(
                f"b{bit}={'1' if (byte >> bit) & 1 else '0'}" for bit in range(7, -1, -1)
            )
        return text

    def copy_result(self) -> None:
        copy_to_clipboard(codec.bytes_to_text(self.result_bytes(), codec.RADIX_HEX))
        self.status_message.emit(f"已复制编辑结果（{len(self._bytes)} 字节）")

    def save_binary(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "保存编辑结果", "edited.bin", "二进制 (*.bin);;所有文件 (*)")
        if not path:
            return
        try:
            with open(path, "wb") as handle:
                handle.write(self.result_bytes())
        except OSError as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        self.status_message.emit(f"已保存 {path}")

    def _apply_to_input(self) -> None:
        self.set_input_requested.emit(self.result_bytes())
