"""从日志 / JSONL / CSV 里提取数据帧的对话框。"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QSpinBox, QVBoxLayout,
)

from .. import extract
from .util import mono_font


class ExtractDialog(QDialog):
    """正则 + 行过滤，把日志里的数据段抠出来变成"一行一帧"的 HEX 文本。"""

    def __init__(self, source_text: str = "", default_path: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("从日志 / CSV 提取数据帧")
        self.resize(920, 680)
        self._source_text = source_text or ""
        self._result: extract.ExtractResult | None = None

        layout = QVBoxLayout(self)

        top = QGridLayout()
        top.setHorizontalSpacing(6)
        self.path_edit = QLineEdit(default_path or "")
        self.path_edit.setPlaceholderText("留空则使用「输入数据」里的内容；点右侧选择日志文件（jsonl / csv / txt）")
        browse = QPushButton("选择文件…")
        browse.clicked.connect(self._browse)
        self.mb_spin = QSpinBox()
        self.mb_spin.setRange(1, 512)
        self.mb_spin.setValue(8)
        self.mb_spin.setSuffix(" MB")
        self.mb_spin.setToolTip("只读取文件开头的这么多内容，避免大文件卡住")
        top.addWidget(QLabel("来源"), 0, 0)
        top.addWidget(self.path_edit, 0, 1, 1, 5)
        top.addWidget(browse, 0, 6)
        top.addWidget(QLabel("读取上限"), 0, 7)
        top.addWidget(self.mb_spin, 0, 8)
        layout.addLayout(top)

        rule = QGroupBox("提取规则")
        grid = QGridLayout(rule)
        self.pattern_combo = QComboBox()
        for name, _pattern, _normalize, desc in extract.PATTERN_PRESETS:
            self.pattern_combo.addItem(name)
            self.pattern_combo.setItemData(self.pattern_combo.count() - 1, desc, Qt.ItemDataRole.ToolTipRole)
        self.pattern_edit = QLineEdit()
        self.pattern_edit.setFont(mono_font(10))
        self.group_spin = QSpinBox()
        self.group_spin.setRange(0, 6)
        self.group_spin.setValue(1)
        self.normalize_box = QCheckBox("归一化成 HEX 字节")
        self.normalize_box.setChecked(True)
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(1, 200000)
        self.limit_spin.setValue(5000)
        self.limit_spin.setSuffix(" 帧")

        grid.addWidget(QLabel("规则"), 0, 0)
        grid.addWidget(self.pattern_combo, 0, 1, 1, 3)
        grid.addWidget(QLabel("捕获组"), 0, 4)
        grid.addWidget(self.group_spin, 0, 5)
        grid.addWidget(self.normalize_box, 0, 6)
        grid.addWidget(QLabel("最多提取"), 0, 7)
        grid.addWidget(self.limit_spin, 0, 8)
        grid.addWidget(QLabel("正则"), 1, 0)
        grid.addWidget(self.pattern_edit, 1, 1, 1, 8)

        grid.addWidget(QLabel("行过滤"), 2, 0)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("只处理包含该文本的行，例如 can_id 值 0x1E7（留空=全部行）")
        grid.addWidget(self.filter_edit, 2, 1, 1, 4)
        self.id_combo = QComboBox()
        self.id_combo.setMinimumWidth(150)
        self.id_combo.setToolTip("先点「统计 ID」从 JSONL 里列出出现过的 can_id")
        self.id_button = QPushButton("统计 ID")
        grid.addWidget(self.id_combo, 2, 5, 1, 2)
        grid.addWidget(self.id_button, 2, 7)
        layout.addWidget(rule)

        row = QHBoxLayout()
        self.run_button = QPushButton("提取并预览")
        self.info_label = QLabel("—")
        self.info_label.setWordWrap(True)
        row.addWidget(self.run_button)
        row.addWidget(self.info_label, 1)
        layout.addLayout(row)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(mono_font(10))
        self.preview.setPlaceholderText("提取结果预览（前 20 帧）")
        layout.addWidget(self.preview, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("应用到输入数据")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.pattern_combo.currentIndexChanged.connect(self._on_pattern_changed)
        self.run_button.clicked.connect(self.extract_now)
        self.id_button.clicked.connect(self._collect_ids)
        self.id_combo.activated.connect(self._use_id)
        self.path_edit.textChanged.connect(lambda _t: self._run())
        self.pattern_edit.textChanged.connect(lambda _t: self._run())
        self.filter_edit.textChanged.connect(lambda _t: self._run())
        self.group_spin.valueChanged.connect(lambda _v: self._run())
        self.limit_spin.valueChanged.connect(lambda _v: self._run())
        self.normalize_box.toggled.connect(lambda _v: self._run())
        self.mb_spin.valueChanged.connect(lambda _v: self._run())

        self._on_pattern_changed()

    # ------------------------------------------------------------------ #
    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择日志 / 抓包文件", self.path_edit.text(),
            "日志与抓包 (*.jsonl *.json *.csv *.txt *.log);;所有文件 (*)",
        )
        if path:
            self.path_edit.setText(path)

    def _on_pattern_changed(self) -> None:
        name = self.pattern_combo.currentText()
        for item_name, pattern, normalize, _desc in extract.PATTERN_PRESETS:
            if item_name == name:
                self.pattern_edit.setText(pattern)
                self.normalize_box.setChecked(normalize)
                break
        self._run()

    def _source(self) -> str:
        path = self.path_edit.text().strip()
        if path:
            from pathlib import Path
            if Path(path).is_file():
                text, _truncated = extract.read_text_head(path, self.mb_spin.value() * 1024 * 1024)
                return text
        return self._source_text

    def _run(self) -> None:
        text = self._source()
        if not text.strip():
            self._result = None
            self.info_label.setText("没有可提取的文本：请选择文件，或先在「输入数据」里粘贴内容。")
            self.preview.setPlainText("")
            return
        self._result = extract.extract(
            text,
            self.pattern_edit.text(),
            self.group_spin.value(),
            self.filter_edit.text(),
            self.limit_spin.value(),
            self.normalize_box.isChecked(),
            self.pattern_combo.currentText(),
        )
        if self._result.error:
            self.info_label.setText(f"✘ {self._result.summary()}")
            self.preview.setPlainText("")
            return
        self.info_label.setText(("✔ " if self._result.lines else "✘ ") + self._result.summary())
        self.preview.setPlainText("\n".join(self._result.lines[:20]))

    def extract_now(self) -> None:
        self._run()

    def _collect_ids(self) -> None:
        text = self._source()
        pairs = extract.jsonl_ids(text)
        self.id_combo.clear()
        if not pairs:
            self.info_label.setText("没有从内容里读到 can_id 字段（该功能适用于 JSONL 抓包日志）")
            return
        for key, count in pairs[:50]:
            self.id_combo.addItem(f"{key}  ({count})", key)
        self.info_label.setText(f"共统计到 {len(pairs)} 个 ID，选中一个即可只提取该 ID 的报文")

    def _use_id(self, index: int) -> None:
        key = self.id_combo.itemData(index)
        if key:
            self.filter_edit.setText(str(key))
            self._run()

    def _accept(self) -> None:
        if self._result is None or not self._result.lines:
            self._run()
        if self._result is None or not self._result.lines:
            self.info_label.setText("还没有提取到数据，不能应用。")
            return
        self.accept()

    # ------------------------------------------------------------------ #
    def result_text(self) -> str:
        return "\n".join(self._result.lines) if self._result else ""

    def result_summary(self) -> str:
        return self._result.summary() if self._result else ""

    def frame_count(self) -> int:
        return len(self._result.lines) if self._result else 0
