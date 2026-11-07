"""自动推断分帧参数对话框：列出一批候选帧格式，挑一个应用。"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import framer, infer
from . import theme
from .util import mono_font


class InferDialog(QDialog):
    """对当前数据做统计推断，给出帧头/帧长/长度字段/校验的候选。"""

    def __init__(self, data: bytes, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("自动推断分帧参数")
        self.resize(940, 620)
        self._chosen: infer.Candidate | None = None
        self._candidates: list[infer.Candidate] = []

        layout = QVBoxLayout(self)
        tip = QLabel(
            "对当前数据做统计：找重复出现的帧头、推算帧间隔与长度字段语义、试所有校验算法，"
            "并列出跨帧恒定的字节位置。双击某一行即可应用它的分帧设置。"
        )
        tip.setWordWrap(True)
        layout.addWidget(tip)

        splitter = QSplitter(Qt.Orientation.Vertical)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["置信度", "候选帧格式", "帧数", "消耗字节", "推断说明"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setColumnWidth(0, 70)
        self.table.setColumnWidth(1, 250)
        self.table.setColumnWidth(2, 70)
        self.table.setColumnWidth(3, 90)
        splitter.addWidget(self.table)

        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setFont(mono_font(10))
        self.detail.setPlaceholderText("选中一行看详细推断说明 / 对应的分帧配置")
        splitter.addWidget(self.detail)
        splitter.setSizes([380, 200])
        layout.addWidget(splitter, 1)

        self.info_label = QLabel("—")
        layout.addWidget(self.info_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("应用选中的帧格式")
        buttons.accepted.connect(self._apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.table.itemSelectionChanged.connect(self._show_detail)
        self.table.itemDoubleClicked.connect(lambda _i: self._apply())

        self._run(data)

    # ------------------------------------------------------------------ #
    def _run(self, data: bytes) -> None:
        try:
            self._candidates = infer.infer(data)
        except Exception as exc:  # noqa: BLE001
            self._candidates = []
            self.info_label.setText(f"推断失败：{exc}")
            return
        self.table.setRowCount(len(self._candidates))
        for row, candidate in enumerate(self._candidates):
            values = [
                f"{candidate.percent}%",
                candidate.title,
                str(candidate.frame_count),
                str(candidate.consumed),
                "；".join(candidate.notes[:2]),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, row)
                if col == 0 and candidate.percent >= 70:   # 黑白主题：高置信度用加粗
                    font = QFont(item.font())
                    font.setBold(True)
                    item.setFont(font)
                    item.setForeground(Qt.GlobalColor.black)
                self.table.setItem(row, col, item)
        if self._candidates:
            self.table.selectRow(0)
            self.info_label.setText(
                f"共 {len(self._candidates)} 个候选；最可能是「{self._candidates[0].title}」"
                f"（{self._candidates[0].percent}%）"
            )
        else:
            self.info_label.setText(
                f"数据只有 {len(data)} 字节，没有推断出稳定结构；可以先用「整包」逐个字节看"
            )

    def _show_detail(self) -> None:
        row = self.table.currentRow()
        if not (0 <= row < len(self._candidates)):
            return
        candidate = self._candidates[row]
        lines = [
            f"候选：{candidate.title}",
            f"置信度：{candidate.percent}%　帧数：{candidate.frame_count}　消耗字节：{candidate.consumed}",
            "",
            "推断说明：",
            *[f"  · {note}" for note in candidate.notes],
            "",
            "对应分帧配置：",
        ]
        config = candidate.config
        lines.append(f"  模式　　　：{framer.MODE_LABELS.get(config.mode, config.mode)}")
        if config.mode == framer.MODE_FIXED:
            lines.append(f"  帧长　　　：{config.fixed_len} 字节")
        if config.header_hex:
            lines.append(f"  帧头　　　：{config.header_hex}")
        if config.mode == framer.MODE_HEADER:
            lines.append(
                f"  长度字段　：偏移 {config.len_offset}，{config.len_size} 字节，"
                f"{'大端' if config.len_byte_order == 'big' else '小端'}；"
                f"含帧头={'是' if config.len_includes_header else '否'}，"
                f"含校验={'是' if config.len_includes_checksum else '否'}"
            )
        if config.mode == framer.MODE_DELIMITER:
            lines.append(f"  分隔符　　：{config.delimiter_hex}")
        if config.mode == framer.MODE_TLV:
            lines.append(f"  TLV　　　 ：Tag {config.tag_size} 字节，Len {config.value_len_size} 字节")
        lines.append(f"  校验　　　：{config.checksum_algo}"
                     f"（{'大端' if config.checksum_byte_order == 'big' else '小端'}）")
        if config.skip_bytes:
            lines.append(f"  跳过　　　：前 {config.skip_bytes} 字节")
        self.detail.setPlainText("\n".join(lines))

    def _apply(self) -> None:
        row = self.table.currentRow()
        if 0 <= row < len(self._candidates):
            self._chosen = self._candidates[row]
            self.accept()
        else:
            self.reject()

    def chosen(self) -> infer.Candidate | None:
        return self._chosen
