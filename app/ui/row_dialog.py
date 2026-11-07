"""行规则设置对话框：用多条规则自由决定「数据表分析」显示 / 统计哪些帧（哪些行）。

和「列规则」是一对：

* 列规则决定**看哪些字节**（哪几列）
* 行规则决定**看哪些帧**（哪几行）—— 帧号从哪到哪、每 N 帧取 1、只留校验失败的、
  帧长不对的、某个字节等于某个值的……

帧号就是表格「帧#」列里的数字（从 0 开始），`-1` 表示最后一帧，所以
「起始 0 / 结束 -1」= 全部帧。
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QMenu, QPushButton, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout,
)

from ..analysis import (
    ROW_CHECK_STATES, ROW_COMBINE, ROW_KINDS, ROW_OPS, ROW_PRESETS, RowRule,
    describe_row_rules, match_row_positions,
)
from . import theme

HEADERS = ["启用", "规则", "起始帧", "结束帧", "每N帧", "相位",
           "帧内偏移", "比较", "数值", "取几字节（字节序）", "校验状态", "内容",
           "这条规则在挑什么"]
WIDTHS = [48, 122, 66, 66, 66, 52, 74, 62, 84, 132, 92, 150, 250]
#: 每列只对哪些规则类型可编辑：{列号: 允许的规则类型}
CELL_KINDS = {
    2: {"range"}, 3: {"range"},
    4: {"step"}, 5: {"step"},
    6: {"byte"}, 7: {"byte", "length", "contains"},
    8: {"byte", "length"}, 9: {"byte"},
    10: {"check"}, 11: {"contains"},
}
#: 取几字节 + 字节序（一个下拉同时定这两件事）
WIDTH_ORDER_CHOICES = [
    ((1, "big"), "1 字节"),
    ((2, "big"), "2 字节 大端"),
    ((2, "little"), "2 字节 小端"),
    ((4, "big"), "4 字节 大端"),
    ((4, "little"), "4 字节 小端"),
    ((8, "big"), "8 字节 大端"),
    ((8, "little"), "8 字节 小端"),
]
CONTENT_OP_CHOICES = [("=", "包含"), ("!=", "不包含")]
CELL_TIPS = {
    2: "从第几帧开始（0 = 第一帧；也可以写负数，-500 = 倒数第 500 帧）",
    3: "-1 = 最后一帧；填 499 = 到第 499 帧结束",
    4: "每 N 帧保留 1 帧；1 = 不采样",
    5: "相位：从第几帧开始算周期，一般填 0",
    6: "帧内第几个字节（0 = 第一个字节）",
    7: "比较方式：= != > >= < <=",
    8: "可以写 0xEB、235、0b1010",
    9: "取几个字节当一个整数来比较，并指定大端 / 小端",
    11: "HEX 写法（EB 90 / eb90）或 ascii:SPD / '$GNGGA'",
}


def try_parse_int(text: str) -> int | None:
    """严格解析整数：支持 0xEB / 235 / 0b1010 / -5；解析不了返回 None。"""
    body = (text or "").strip().replace("_", "").replace(" ", "")
    if not body:
        return None
    sign = 1
    if body.startswith("-"):
        sign, body = -1, body[1:]
    elif body.startswith("+"):
        body = body[1:]
    try:
        if body.lower().startswith("0x"):
            return sign * int(body[2:], 16)
        if body.lower().startswith("0b"):
            return sign * int(body[2:], 2)
        return sign * int(body, 10)
    except ValueError:
        return None


def parse_int(text: str, default: int = 0) -> int:
    """宽松解析整数，解析不了用 default。"""
    value = try_parse_int(text)
    return default if value is None else value


class RowRuleDialog(QDialog):
    """编辑一组行规则，并实时预览会命中多少帧。"""

    def __init__(self, rules: list[RowRule], frames=None, combine: str = "all",
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("行规则设置（挑哪些帧）")
        self.resize(1300, 560)
        # frames 可能是惰性帧视图：预览时按需取帧，不整批展开
        self._frames = frames if frames is not None else []
        self._total = len(self._frames)
        self._rules: list[RowRule] = [
            RowRule.from_dict(rule.to_dict()) for rule in (rules or [])
        ] or [RowRule(kind="range", start=0, end=-1)]
        self._loading = True

        layout = QVBoxLayout(self)
        tip = QLabel(
            "规则决定每一帧是留还是丢，<b>帧号 = 表格「帧#」列的数字（从 0 开始）</b>，"
            "<code>-1</code> 表示最后一帧 —— 所以「起始 0 / 结束 -1」就是全部帧。<br>"
            "只有该规则用得上的格子可以编辑（其余置灰）。"
            "<b>筛选结果同时作用于表格、统计、曲线和突变检测。</b>"
        )
        tip.setWordWrap(True)
        layout.addWidget(tip)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("多条规则："))
        self.combine_combo = QComboBox()
        for key, label in ROW_COMBINE:
            self.combine_combo.addItem(label, key)
        self.combine_combo.setCurrentIndex(max(0, self.combine_combo.findData(combine)))
        self.combine_combo.setToolTip("同时满足 = 取交集（更严）；满足任一 = 取并集（更宽）")
        bar.addWidget(self.combine_combo)
        bar.addSpacing(12)

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

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("按这些规则筛选")
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
        self.combine_combo.currentIndexChanged.connect(lambda _i: self._update_preview())
        self._reload()

    # ------------------------------------------------------------------ #
    def _build_preset_menu(self) -> None:
        self.preset_menu.clear()
        self.preset_menu.addAction("说明：套用会替换掉当前所有规则").setEnabled(False)
        self.preset_menu.addSeparator()
        for name, rules in ROW_PRESETS:
            self.preset_menu.addAction(
                name, lambda _checked=False, r=rules: self._apply_preset(r)
            )

    def _apply_preset(self, rules: list[RowRule]) -> None:
        self._rules = [RowRule.from_dict(rule.to_dict()) for rule in rules]
        self._reload()

    # ------------------------------------------------------------------ #
    def _reload(self) -> None:
        self._loading = True
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):        # 先清掉旧的下拉框
            for col in range(self.table.columnCount()):
                if self.table.cellWidget(row, col) is not None:
                    self.table.removeCellWidget(row, col)
        self.table.clearContents()
        self.table.setRowCount(len(self._rules))
        for row, rule in enumerate(self._rules):
            self._fill_row(row, rule)
        self.table.blockSignals(False)
        self._loading = False
        self._update_preview()

    def _fill_row(self, row: int, rule: RowRule) -> None:
        # 0 启用
        enabled = QTableWidgetItem("")
        enabled.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
                         | Qt.ItemFlag.ItemIsSelectable)
        enabled.setCheckState(Qt.CheckState.Checked if rule.enabled
                              else Qt.CheckState.Unchecked)
        enabled.setToolTip("取消勾选 = 这条规则暂时不用（参数保留）")
        self.table.setItem(row, 0, enabled)

        # 1 规则类型
        kind_combo = QComboBox()
        for key, label in ROW_KINDS:
            kind_combo.addItem(label, key)
        kind_combo.setCurrentIndex(max(0, kind_combo.findData(rule.kind)))
        kind_combo.currentIndexChanged.connect(
            lambda _i, r=row, c=kind_combo: self._set_kind(r, c.currentData())
        )
        self.table.setCellWidget(row, 1, kind_combo)

        texts = {
            2: str(rule.start), 3: str(rule.end), 4: str(rule.step), 5: str(rule.phase),
            6: str(rule.offset), 7: rule.op, 8: self._value_text(rule), 11: rule.text,
        }
        for col in range(2, 12):
            if col == 7 and rule.kind == "contains":
                self._combo_cell(row, col, CONTENT_OP_CHOICES, rule.op, "op")
                continue
            if col == 9 and rule.kind == "byte":
                self._combo_cell(row, col, WIDTH_ORDER_CHOICES,
                                 (max(1, rule.width), rule.byte_order), "width_order")
                continue
            if col == 10 and rule.kind == "check":
                self._combo_cell(row, col, ROW_CHECK_STATES, rule.state, "state")
                continue
            item = QTableWidgetItem(texts.get(col, ""))
            if rule.kind in CELL_KINDS.get(col, set()):
                item.setToolTip(CELL_TIPS.get(col, ""))
            else:
                item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                item.setForeground(Qt.GlobalColor.gray)
            self.table.setItem(row, col, item)

        # 12 说明（只读）
        note = QTableWidgetItem(rule.describe(self._total))
        note.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
        note.setForeground(Qt.GlobalColor.darkGray)
        self.table.setItem(row, 12, note)

    def _combo_cell(self, row: int, col: int, choices, current, attr: str) -> None:
        combo = QComboBox()
        for key, label in choices:
            combo.addItem(label, key)
        index = combo.findData(current)
        if index < 0 and attr == "width_order":
            index = combo.findData((int(current[0]), "big"))
        combo.setCurrentIndex(max(0, index))
        combo.currentIndexChanged.connect(
            lambda _i, r=row, c=combo, a=attr: self._set_combo(r, a, c.currentData())
        )
        self.table.setCellWidget(row, col, combo)
        blank = QTableWidgetItem("")
        blank.setFlags(Qt.ItemFlag.NoItemFlags)
        self.table.setItem(row, col, blank)

    @staticmethod
    def _value_text(rule: RowRule) -> str:
        if rule.kind == "byte":
            width = max(1, int(rule.width) or 1)
            return f"0x{int(rule.value) & ((1 << (8 * width)) - 1):0{width * 2}X}"
        return str(rule.value)

    # ------------------------------------------------------------------ #
    def _set_kind(self, row: int, kind: str) -> None:
        if self._loading or not (0 <= row < len(self._rules)):
            return
        self._rules[row].kind = kind
        self._reload()
        self.table.selectRow(row)

    def _set_combo(self, row: int, attr: str, value) -> None:
        if self._loading or not (0 <= row < len(self._rules)):
            return
        rule = self._rules[row]
        if attr == "width_order":
            width, order = value
            rule.width, rule.byte_order = int(width), str(order)
        else:
            setattr(rule, attr, value)
        self._sync_note(row)
        self._update_preview()

    def _sync_note(self, row: int) -> None:
        note = self.table.item(row, 12)
        if note is not None and 0 <= row < len(self._rules):
            note.setText(self._rules[row].describe(self._total))

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading:
            return
        row, col = item.row(), item.column()
        if not (0 <= row < len(self._rules)):
            return
        rule = self._rules[row]
        text = item.text().strip()
        if col == 0:
            rule.enabled = item.checkState() == Qt.CheckState.Checked
            self._sync_note(row)
            self._update_preview()
            return
        number = try_parse_int(text)
        if col == 3 and not text:
            rule.end = -1                     # 结束帧留空 = 到最后一帧
        elif col in (2, 3, 4, 5, 6, 8) and number is None:
            item.setToolTip("这里要填数字（可写 0xEB / 235 / 0b1010）")
            return
        if col == 7 and rule.kind != "contains" and text not in ROW_OPS:
            item.setToolTip("比较方式只能是 " + " ".join(ROW_OPS))
            return
        if col == 2:
            rule.start = number
        elif col == 3:
            rule.end = -1 if not text else number     # 留空 = -1 = 最后一帧
        elif col == 4:
            rule.step = max(1, number)
        elif col == 5:
            rule.phase = max(0, number)
        elif col == 6:
            rule.offset = max(0, number)
        elif col == 7:
            rule.op = text
        elif col == 8:
            rule.value = number
        elif col == 11:
            rule.text = text
        item.setForeground(self.table.palette().text())
        self._sync_note(row)
        self._update_preview()

    # ------------------------------------------------------------------ #
    def _update_preview(self) -> None:
        combine = self.combine_combo.currentData() or "all"
        if not [rule for rule in self._rules if rule.enabled]:
            self.preview_label.setText("没有启用的规则 → 全部帧都会显示")
            return
        if not self._total:
            self.preview_label.setText("（还没有数据，先解析一次再看命中情况）")
            return
        positions = match_row_positions(self._frames, self._rules, combine)
        hit = len(positions)
        percent = hit * 100.0 / self._total
        indexes = [self._frames[p].index for p in positions]
        sample = ""
        if indexes:
            head = ", ".join(str(i) for i in indexes[:6])
            tail = ", ".join(str(i) for i in indexes[-3:]) if hit > 6 else ""
            sample = f"　例：{head}" + (f" … {tail}" if tail else "")
        text = f"命中 {hit} / {self._total} 帧（{percent:.2f}%）" + sample
        if hit == 0:
            text += "　⚠ 没有任何帧命中，表格会是空的"
        if combine == "any" and len([r for r in self._rules if r.enabled]) > 1:
            text += "　（满足任一 = 并集）"
        self.preview_label.setText(text)

    # ------------------------------------------------------------------ #
    def _current_row(self) -> int:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        return rows[-1] if rows else len(self._rules) - 1

    def _add_row(self, at: int) -> None:
        self._rules.insert(max(0, min(at, len(self._rules))),
                           RowRule(kind="range", start=0, end=-1))
        self._reload()
        self.table.selectRow(max(0, min(at, self.table.rowCount() - 1)))

    def _remove_row(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            if 0 <= row < len(self._rules):
                self._rules.pop(row)
        if not self._rules:
            self._rules = [RowRule(kind="range", start=0, end=-1)]
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
    def row_rules(self) -> list[RowRule]:
        return [RowRule.from_dict(rule.to_dict()) for rule in self._rules]

    def combine(self) -> str:
        return self.combine_combo.currentData() or "all"

    def summary(self) -> str:
        """给状态栏用的一句话：命中多少帧 + 规则描述。"""
        combine = self.combine()
        text = describe_row_rules(self._rules, combine, self._total)
        if not text:
            return f"全部 {self._total} 帧"
        hit = len(match_row_positions(self._frames, self._rules, combine))
        return f"{text} → 命中 {hit}/{self._total} 帧"
