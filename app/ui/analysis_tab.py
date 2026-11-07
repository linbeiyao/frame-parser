"""数据表分析页签：一帧一行 × 一个字段一列，并按列做统计/找突变/看曲线。

* 左侧宽表：每行一帧，列 = 帧#/偏移/长度/校验 + 各字段（可以勾选显示哪些字段列）
* 右侧：每列统计表（有效/范围/均值/中位数/σ/不同值/变化次数/首末差/趋势/最大跳变）
  + 选中列的折线图 + 突变清单
* 突变按 σ 阈值自动标记（表格里灰底），可以「只看异常帧」
* 所有渲染都走流式分片，不阻塞界面；列格式/颜色/换算函数与其它表一致
"""
from __future__ import annotations

import time

from PyQt6.QtCore import QPointF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QHBoxLayout, QLabel,
    QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QSplitter,
    QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from .. import analysis, exporters, field_parser
from ..analysis import ColumnStats, compute_stats, find_jumps
from . import theme
from .stream_render import RowStreamer
from .table_base import ColumnSpec, SmartTable
from .util import copy_to_clipboard, mono_font

#: 统计最多计算多少帧（再大就只统计前面这些帧，避免百万帧卡住）
STATS_CAP = 50000
#: 帧数不超过这个值就一次性算完取值；超过就分片算（每片多少毫秒）
SCAN_SYNC_LIMIT = 4000
SCAN_SLICE_MS = 8

BASE_COLUMNS = [
    ColumnSpec("index", "帧#", width=52, numeric=True, radix="dec", align="center"),
    ColumnSpec("offset", "偏移", width=64, numeric=True, radix="dec", align="center"),
    ColumnSpec("length", "长度", width=54, numeric=True, radix="dec", align="center"),
    ColumnSpec("check", "校验", width=48, align="center"),
]

COLUMN_MODES = [
    ("fields", "字段列（按字段定义）"),
    ("bytes", "字节列（原始整帧）"),
]

STATS_HEADERS = ["字段", "类型", "有效", "范围", "均值", "中位数", "σ",
                 "不同值", "变化", "首 → 末", "首末差", "趋势", "最大跳变"]


class MiniChart(QWidget):
    """黑白折线图：看选中列随帧号的变化，突变点用空心圈标出。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(140)
        self._series: list[tuple[int, float]] = []
        self._jumps: set[int] = set()
        self._name = ""
        self._unit = ""

    def set_series(self, name: str, series: list[tuple[int, float]],
                   jumps: set[int], unit: str = "") -> None:
        self._name, self._series, self._jumps, self._unit = name, series, jumps, unit
        self.update()

    def clear(self) -> None:
        self.set_series("", [], set(), "")

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(8, 8, -8, -8)
        painter.fillRect(self.rect(), QColor(theme.BG))
        painter.setPen(QPen(QColor(theme.BORDER), 1))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))

        if len(self._series) < 2:
            painter.setPen(QPen(QColor(theme.FG_DIM), 1))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                             "选中一列数值字段后这里显示曲线")
            return

        values = [v for _i, v in self._series]
        low, high = min(values), max(values)
        span = (high - low) or 1.0
        count = len(self._series)
        # 网格
        painter.setPen(QPen(QColor(theme.BG_ALT), 1, Qt.PenStyle.DotLine))
        for step in range(1, 4):
            y = rect.top() + rect.height() * step / 4
            painter.drawLine(rect.left(), int(y), rect.right(), int(y))

        def point(index_in_series: int, value: float) -> QPointF:
            x = rect.left() + rect.width() * (index_in_series / max(1, count - 1))
            y = rect.bottom() - rect.height() * ((value - low) / span)
            return QPointF(x, y)

        polygon = QPolygonF([point(i, v) for i, (_idx, v) in enumerate(self._series)])
        painter.setPen(QPen(QColor(theme.FG_STRONG), 1))
        painter.drawPolyline(polygon)

        # 突变点
        painter.setPen(QPen(QColor(theme.FG_STRONG), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for position, (_idx, value) in enumerate(self._series):
            if self._series[position][0] in self._jumps:
                painter.drawEllipse(point(position, value), 3, 3)

        painter.setPen(QPen(QColor(theme.FG), 1))
        painter.drawText(rect.adjusted(4, 0, -4, 0), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
                         f"{self._name}  最大 {high:.6g}{self._unit}  最小 {low:.6g}{self._unit}")
        painter.drawText(rect.adjusted(4, 0, -4, 0),
                         Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft,
                         f"帧 {self._series[0][0]}")


class AnalysisPanel(QWidget):
    """一帧一行的数据表 + 按列分析。

    注意：宽表的字段列**来自「字段定义表」**，字段表里定义了几个字段，这张表就有几列。
    """

    status_message = pyqtSignal(str)
    #: 请求主窗口载入一套字段定义（字段模板 / 预设自带字段）
    fields_requested = pyqtSignal(list)
    #: 选中的格子对应 (起始字节, 字节数) —— 左侧原文反色定位
    bytes_requested = pyqtSignal(int, int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._frames: list = []
        self._parsed: dict = {}
        self._fields: list = []
        self._values: dict[str, list] = {}
        self._stats: dict[str, ColumnStats] = {}
        self._jumps: dict[str, list] = {}
        self._jump_rows: set[int] = set()
        self._current_key = ""
        self._column_specs: list[ColumnSpec] = []
        self.streamer = RowStreamer(self)
        # 取值分片推进用的定时器（帧太多时不阻塞界面）
        self._scan_timer = QTimer(self)
        self._scan_timer.setSingleShot(False)
        self._scan_timer.timeout.connect(self._on_scan_tick)
        self._scan_frames: list = []
        self._scan_columns: list = []
        self._scan_index = 0
        self.byte_mode = False
        self.stats_limit = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        bar = QHBoxLayout()
        self.mode_combo = QComboBox()
        for key, label in COLUMN_MODES:
            self.mode_combo.addItem(label, key)
        self.mode_combo.setToolTip(
            "字段列：列 = 字段定义表里的字段\n"
            "字节列：不用定义字段，直接按帧内字节铺开（看整帧原始数据）"
        )
        self.mode_combo.currentIndexChanged.connect(lambda _i: self._on_mode_changed())

        self.rules: list = [
            analysis.ColumnRule(name="偏移", offset=0, count=16, width=1, radix="hex")
        ]
        self.rules_button = QPushButton("列规则…")
        self.rules_button.setToolTip(
            "用一个弹窗自由配置要显示的列：\n"
            "· 铺连续字节（起始偏移 + 列数 + 步长）\n"
            "· 生成多字节数值列（每列 2/4/8 字节 + 字节序）\n"
            "· 直接写偏移列表，如 0,1,2,8-15\n"
            "支持多条规则叠加"
        )
        self.rules_button.clicked.connect(self.open_rule_dialog)
        self.rules_label = QLabel("")
        self.rules_label.setStyleSheet(f"color:{theme.FG_DIM};")
        self._resolved: list = []
        self._rule_warnings: list[str] = []

        # 行规则：决定看哪些帧（行），同时作用于表格 / 统计 / 曲线 / 突变
        self.row_rules: list = [analysis.RowRule(kind="range", start=0, end=-1)]
        self.row_combine = "all"
        self.rows_button = QPushButton("行规则…")
        self.rows_button.setToolTip(
            "用一个弹窗自由决定要看哪些帧（哪几行）：\n"
            "· 帧号区间：从第几帧到第几帧（-1 = 最后一帧）\n"
            "· 采样：每 N 帧取 1 帧\n"
            "· 帧内字节比较：如第 2 字节 > 150、第 0 字节 ≠ 0xEB\n"
            "· 校验状态 / 帧长 / 帧内包含内容\n"
            "支持多条规则（且 / 或），筛选结果同时作用于统计、曲线和突变"
        )
        self.rows_button.clicked.connect(self.open_row_dialog)
        self.rows_label = QLabel("")
        self.rows_label.setStyleSheet(f"color:{theme.FG_DIM};")
        self._positions: list[int] = []
        self._stats_positions: list[int] = []
        self._labels: list[int] = []
        self._row_positions_cache: list[int] = []
        self._display_position: list[int] = []
        self._jump_positions: dict[str, set[int]] = {}
        self._jump_keys: frozenset = frozenset()

        # 表格不设"最多多少行"：用滑动窗口，滚到哪加载哪（几十万帧也能一路滚）
        self.sigma_spin = QDoubleSpinBox()
        self.sigma_spin.setRange(1.0, 20.0)
        self.sigma_spin.setSingleStep(0.5)
        self.sigma_spin.setValue(4.0)
        self.sigma_spin.setSuffix(" σ")
        self.sigma_spin.setToolTip("相邻帧差值超过多少倍标准差算突变")
        self.sigma_spin.valueChanged.connect(self._recompute_jumps)

        self.only_anomaly_box = QCheckBox("只看异常帧")
        self.only_anomaly_box.toggled.connect(lambda _v: self._render_rows())

        self.fields_button = QToolButton()
        self.fields_button.setText("字段列 ▾")
        self.fields_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.fields_menu = QMenu(self.fields_button)
        self.fields_button.setMenu(self.fields_menu)

        self.template_button = QToolButton()
        self.template_button.setText("字段模板 ▾")
        self.template_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.template_button.setToolTip("这张表的列＝字段定义表里的字段；这里可一键套用预设/模板的字段")
        self.template_menu = QMenu(self.template_button)
        self.template_button.setMenu(self.template_menu)
        self._build_template_menu()

        self.recompute_button = QPushButton("重算统计")
        self.recompute_button.clicked.connect(self.rebuild)
        self.export_button = QPushButton("导出宽表 CSV")
        self.export_button.clicked.connect(self.export_csv)
        self.copy_button = QPushButton("复制统计")
        self.copy_button.clicked.connect(self.copy_stats)

        self.info_label = QLabel("—")
        self.info_label.setStyleSheet(f"color:{theme.FG_DIM};")

        bar.addWidget(self.mode_combo)
        bar.addWidget(self.rules_button)
        bar.addWidget(self.rules_label)
        bar.addWidget(self.rows_button)
        bar.addWidget(self.rows_label)
        bar.addWidget(QLabel("突变阈值"))
        bar.addWidget(self.sigma_spin)
        bar.addWidget(self.only_anomaly_box)
        bar.addWidget(self.fields_button)
        bar.addWidget(self.template_button)
        bar.addWidget(self.recompute_button)
        bar.addWidget(self.export_button)
        bar.addWidget(self.copy_button)
        bar.addStretch(1)
        bar.addWidget(self.info_label)
        layout.addLayout(bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.table = SmartTable(BASE_COLUMNS, self, table_id="analysis")
        self.table.column_clicked.connect(self._on_column_clicked)
        self.table.status_message.connect(self.status_message)
        self.table.virtual_grew.connect(lambda _loaded, _total: self._update_info(
            self.byte_mode, self.stats_limit))
        self.table.itemSelectionChanged.connect(self._on_table_selection)
        splitter.addWidget(self.table)

        right = QSplitter(Qt.Orientation.Vertical)

        stats_box = QWidget()
        stats_layout = QVBoxLayout(stats_box)
        stats_layout.setContentsMargins(0, 0, 0, 0)
        stats_layout.addWidget(QLabel("每列统计（点一行看该列曲线；点表头可整列选中）"))
        self.stats_table = QTableWidget(0, len(STATS_HEADERS))
        self.stats_table.setHorizontalHeaderLabels(STATS_HEADERS)
        self.stats_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.stats_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.stats_table.setAlternatingRowColors(True)
        self.stats_table.verticalHeader().setVisible(False)
        self.stats_table.itemSelectionChanged.connect(self._on_stats_selected)
        self.stats_table.setMinimumHeight(150)
        stats_layout.addWidget(self.stats_table, 1)
        right.addWidget(stats_box)

        self.chart = MiniChart()
        right.addWidget(self.chart)

        self.jump_view = QPlainTextEdit()
        self.jump_view.setReadOnly(True)
        self.jump_view.setFont(mono_font(9))
        self.jump_view.setMaximumHeight(120)
        self.jump_view.setPlaceholderText("选中一列后，这里列出该列的突变帧")
        right.addWidget(self.jump_view)
        right.setSizes([320, 220, 140])
        splitter.addWidget(right)
        splitter.setSizes([760, 620])
        layout.addWidget(splitter, 1)
        self._apply_mode_state()

    # ------------------------------------------------------------------ #
    # 数据来源
    # ------------------------------------------------------------------ #
    def set_source(self, frames, parsed_by_frame: dict, fields: list) -> None:
        # frames 可能是惰性帧视图（FrameSource）：不要 list() 展开，几万帧展开就前功尽弃
        self._frames = frames if isinstance(frames, list) else frames
        self._parsed = parsed_by_frame or {}
        self._fields = list(fields)
        self._rebuild_field_menu()
        self.rebuild()

    def _build_template_menu(self) -> None:
        """一键套用字段定义：预设自带的字段表 + 通用模板。"""
        from .. import presets

        self.template_menu.clear()
        self.template_menu.addAction("说明：宽表的列 = 字段定义表里的字段").setEnabled(False)
        self.template_menu.addSeparator()
        for _key, preset in presets.PRESETS.items():
            if not preset.fields:
                continue
            action = self.template_menu.addAction(
                f"{preset.name}　（{len(preset.fields)} 个字段）"
            )
            action.triggered.connect(
                lambda _checked=False, f=list(preset.fields): self.fields_requested.emit(f)
            )
        self.template_menu.addSeparator()
        for name, fields in presets.FIELD_TEMPLATES.items():
            if not fields:
                continue
            action = self.template_menu.addAction(f"模板：{name}　（{len(fields)} 个字段）")
            action.triggered.connect(
                lambda _checked=False, f=list(fields): self.fields_requested.emit(f)
            )

    def _rebuild_field_menu(self) -> None:
        self.fields_menu.clear()
        self.fields_menu.addAction("全选", lambda: self._set_all_fields(True))
        self.fields_menu.addAction("全不选", lambda: self._set_all_fields(False))
        self.fields_menu.addSeparator()
        for index, fd in enumerate(self._fields):
            key = analysis.column_key(index)
            hidden = key in getattr(self, "_hidden_fields", set())
            action = self.fields_menu.addAction(f"{index + 1}. {fd.name}")
            action.setCheckable(True)
            action.setChecked(not hidden)
            action.toggled.connect(
                lambda checked, k=key: self._toggle_field(k, checked)
            )

    def _set_all_fields(self, visible: bool) -> None:
        self._hidden_fields = set() if visible else {
            analysis.column_key(i) for i in range(len(self._fields))
        }
        self._rebuild_field_menu()
        self.rebuild()

    def _toggle_field(self, key: str, visible: bool) -> None:
        hidden = set(getattr(self, "_hidden_fields", set()))
        hidden.discard(key) if visible else hidden.add(key)
        self._hidden_fields = hidden
        self._render_rows()          # 只影响显示，不用重算统计

    def _visible_keys(self) -> list[str]:
        if self._mode() == "bytes":
            return list(getattr(self, "_keys", []))
        hidden = getattr(self, "_hidden_fields", set())
        return [analysis.column_key(i) for i in range(len(self._fields))
                if analysis.column_key(i) not in hidden]

    # ------------------------------------------------------------------ #
    # 列模式（字段列 / 字节列）
    # ------------------------------------------------------------------ #
    def _mode(self) -> str:
        return self.mode_combo.currentData() or "fields"

    def _apply_mode_state(self) -> None:
        byte_mode = self._mode() == "bytes"
        self.rules_button.setEnabled(byte_mode)
        self.rules_label.setEnabled(byte_mode)
        self.fields_button.setEnabled(not byte_mode)
        self.template_button.setEnabled(not byte_mode)

    # -------------------------- 列规则 -------------------------- #
    def open_rule_dialog(self) -> None:
        """弹出"列规则"小窗口，配好多条规则后直接应用。"""
        from .rule_dialog import ColumnRuleDialog

        frame_len = len(self._frames[0].data) if self._frames else 0
        dialog = ColumnRuleDialog(self.rules, frame_len, self)
        if dialog.exec():
            self.set_rules(dialog.rules())

    def set_rules(self, rules: list) -> None:
        if not rules:
            return
        self.rules = list(rules)
        index = self.mode_combo.findData("bytes")
        if index >= 0 and self._mode() != "bytes":
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(index)
            self.mode_combo.blockSignals(False)
            self._apply_mode_state()
        self.rebuild()
        self.status_message.emit(
            f"列规则已应用：{len(self.rules)} 条规则 → "
            f"{len(self._resolved)} 列"
        )

    def rules_payload(self) -> list[dict]:
        return [rule.to_dict() for rule in self.rules]

    def apply_rules_payload(self, payload: list) -> None:
        if not payload:
            return
        self.rules = [analysis.ColumnRule.from_dict(item) for item in payload]
        if self._frames:
            self.rebuild()

    # -------------------------- 行规则 -------------------------- #
    def open_row_dialog(self) -> None:
        """弹出"行规则"小窗口：选帧号区间 / 采样 / 按内容筛帧。"""
        from .row_dialog import RowRuleDialog

        dialog = RowRuleDialog(self.row_rules, self._frames, self.row_combine, self)
        if dialog.exec():
            self.set_row_rules(dialog.row_rules(), dialog.combine())
            self.status_message.emit("行规则已应用：" + dialog.summary())

    def set_row_rules(self, rules: list, combine: str | None = None) -> None:
        """设置行规则（空列表 = 不限帧，全部显示）。"""
        self.row_rules = [analysis.RowRule.from_dict(rule.to_dict())
                          for rule in (rules or [])]
        if combine:
            self.row_combine = combine
        if self._frames:
            self.rebuild()

    def clear_row_rules(self) -> None:
        self.row_rules = []
        if self._frames:
            self.rebuild()

    def row_rules_payload(self) -> dict:
        return {"combine": self.row_combine,
                "rules": [rule.to_dict() for rule in self.row_rules]}

    def apply_row_rules_payload(self, payload: dict) -> None:
        if not payload:
            return
        rules = [analysis.RowRule.from_dict(item) for item in (payload.get("rules") or [])]
        self.row_rules = rules
        self.row_combine = payload.get("combine") or "all"
        if self._frames:
            self.rebuild()

    @property
    def row_hits(self) -> int:
        """行规则命中的帧数。"""
        return len(getattr(self, "_positions", []))

    def _on_mode_changed(self, *_args) -> None:
        self._apply_mode_state()
        self._current_key = ""
        self.chart.clear()
        self.jump_view.setPlainText("")
        self.rebuild()

    def _auto_pick_mode(self) -> None:
        """字段定义几乎是空的 → 自动切到「字节列」，先把整帧数据直接铺出来。"""
        if self._mode() == "fields" and len(self._fields) <= 1 and self._frames:
            index = self.mode_combo.findData("bytes")
            if index >= 0:
                self.mode_combo.blockSignals(True)
                self.mode_combo.setCurrentIndex(index)
                self.mode_combo.blockSignals(False)
                self._apply_mode_state()

    # ------------------------------------------------------------------ #
    # 重构：取值 → 统计（全部帧）→ 突变 → 渲染（窗口显示）
    # ------------------------------------------------------------------ #
    def rebuild(self) -> None:
        """重建取值/统计/表格。

        帧多的时候不一次性算完：取值分片推进（每片 ~8 ms），界面照常响应；
        `flush()` 可以强制一次算完（切页签、自检、导出时用）。
        """
        self._stop_scan()
        if not self._frames:
            self.table.begin_stream()
            self.table.end_stream()
            self.stats_table.setRowCount(0)
            self.chart.clear()
            self.info_label.setText("暂无数据")
            self.rows_label.setText("")
            return

        self._auto_pick_mode()
        byte_mode = self._mode() == "bytes"
        self._last_locate = None

        # 行规则：先筛出要看的帧（表格、统计、曲线、突变都用这一套）
        self._positions = analysis.match_row_positions(
            self._frames, self.row_rules, self.row_combine
        )
        stats_limit = min(len(self._positions), STATS_CAP)
        self._stats_positions = self._positions[:stats_limit]
        self.byte_mode = byte_mode
        self.stats_limit = stats_limit

        if byte_mode:
            frame_len = len(self._frames[0].data) if self._frames else 0
            self._resolved, self._rule_warnings = analysis.resolve_rules(
                self.rules, frame_len, limit=256
            )
            self._keys = analysis.byte_columns(len(self._resolved))
            self._names = [column.title for column in self._resolved]
            self._value_plan = tuple(
                (column.offset, max(1, column.width), column.byte_order)
                for column in self._resolved
            )
        else:
            self._keys = [analysis.column_key(i) for i in range(len(self._fields))]
            self._names = [fd.name for fd in self._fields]
            self._resolved = []
            self._value_plan = tuple(
                (fd.offset, fd.length, fd.type) for fd in self._fields
            )

        # 取值这一段的"内容签名"：没变就直接复用上次的结果，不再重扫几万帧
        signature = (byte_mode, self._keys, self._value_plan, self._stats_positions)
        if self._values and signature == getattr(self, "_value_signature", None):
            self._finish_rebuild()
            return

        self._value_signature = signature
        self._values = {key: [] for key in self._keys}
        self._scan_frames = [self._frames[position] for position in self._stats_positions]
        self._scan_index = 0
        self._scan_columns = [self._values[key] for key in self._keys]
        self._labels = list(self._stats_positions)     # 帧# = 在 frames 里的位置（惰性视图下不必取帧）

        if len(self._scan_frames) <= SCAN_SYNC_LIMIT:
            self._scan_values(limit_frames=None)
            self._finish_rebuild()
            return
        # 大数据：先给个进度，然后分片算
        self.info_label.setText(
            f"共 {len(self._frames)} 帧　│　正在算 {stats_limit} 帧的列统计…"
        )
        self._scan_timer.start(0)

    def _stop_scan(self) -> None:
        timer = getattr(self, "_scan_timer", None)
        if timer is not None and timer.isActive():
            timer.stop()

    def _scan_values(self, limit_frames: int | None = 256) -> bool:
        """把一段帧的取值追加进各列（按时间片预算推进）。返回是否已算完。"""
        frames = self._scan_frames
        total = len(frames)
        columns = self._scan_columns
        deadline = None if limit_frames is None else time.perf_counter() + SCAN_SLICE_MS / 1000
        while self._scan_index < total:
            end = total if limit_frames is None else min(total, self._scan_index + limit_frames)
            slice_frames = frames[self._scan_index:end]
            if self.byte_mode:
                for frame in slice_frames:
                    data = frame.data
                    length = len(data)
                    for position, (offset, width, order) in enumerate(self._value_plan):
                        column = columns[position]
                        if width == 1:
                            column.append(data[offset] if offset < length else None)
                        elif offset + width <= length:
                            column.append(int.from_bytes(data[offset:offset + width], order))
                        else:
                            column.append(None)
            else:
                parsed_by_frame = self._parsed
                for frame in slice_frames:
                    parsed = parsed_by_frame.get(frame.index)
                    if not parsed:
                        for column in columns:
                            column.append(None)
                        continue
                    for position, column in enumerate(columns):
                        item = parsed[position] if position < len(parsed) else None
                        column.append(item.value if item else None)
            self._scan_index = end
            if deadline is not None and time.perf_counter() >= deadline:
                return False
        return True

    def _on_scan_tick(self) -> None:
        if self._scan_values():
            self._scan_timer.stop()
            self._finish_rebuild()
        else:
            done = self._scan_index
            total = len(self._scan_frames)
            self.info_label.setText(
                f"共 {len(self._frames)} 帧　│　正在算列统计… {done * 100 // max(1, total)}%"
                f"（{done}/{total} 帧）"
            )

    def _finish_rebuild(self) -> None:
        stats_limit = self.stats_limit
        self._stats = {}
        for index, key in enumerate(self._keys):
            self._stats[key] = compute_stats(self._names[index], self._values[key], stats_limit)
        self._recompute_jumps()
        self._fill_stats_table()
        self._update_info(self.byte_mode, stats_limit)

    def scan_pending(self) -> bool:
        """取值还没算完？"""
        timer = getattr(self, "_scan_timer", None)
        return bool(timer is not None and timer.isActive())

    def _update_info(self, byte_mode: bool, stats_limit: int) -> None:
        total = len(self._frames)
        hit = len(self._positions)
        parts = [f"共 {total} 帧"]
        if hit != total:
            parts.append(f"行规则命中 {hit} 帧（{hit * 100.0 / max(1, total):.2f}%）")
        if stats_limit >= hit:
            parts.append("统计基于" + ("全部" if hit == total else "筛选后的")
                         + f" {hit} 帧")
        else:
            parts.append(f"统计基于{'前' if hit == total else '筛选后的前'} {stats_limit} 帧"
                         + (f"（共命中 {hit} 帧）" if hit != total else ""))
        loaded = self.table.loaded_rows
        if self.table.virtual and loaded < hit:
            parts.append(f"已铺 {loaded} 行，滚到底自动续（共 {hit} 行）")
        else:
            parts.append(f"表格滑动窗口 {hit} 行（无上限）")
        row_text = analysis.describe_row_rules(self.row_rules, self.row_combine, total)
        if row_text:
            self.rows_label.setText(f"（命中 {hit}/{total} 帧）")
            self.rows_label.setToolTip("行规则：" + row_text)
        else:
            self.rows_label.setText("")
            self.rows_label.setToolTip("")
        if byte_mode:
            offsets = [column.offset for column in self._resolved]
            span = f"偏移 {min(offsets)}~{max(offsets)}" if offsets else "无列"
            parts.append(f"规则 {len(self.rules)} 条 → {len(self._resolved)} 列（{span}）")
            self.rules_label.setText(f"（{len(self.rules)} 条规则 / {len(self._resolved)} 列）")
            if self._rule_warnings:
                parts.append("⚠ " + "；".join(self._rule_warnings))
        else:
            parts.append(f"字段列 {len(self._visible_keys())} 个")
            self.rules_label.setText("")
            if len(self._fields) <= 1:
                parts.append("⚠ 字段表只有 1 个字段，可点「字段模板 ▾」补齐，或切「自定义列」")
        self.info_label.setText("　│　".join(parts))

    def _recompute_jumps(self) -> None:
        factor = self.sigma_spin.value()
        self._jumps = {}
        self._jump_rows = set()
        self._jump_positions = {}          # 列 key → 突变帧位置集合（虚拟行按需查）
        for key, values in self._values.items():
            if self._stats.get(key) and self._stats[key].kind == "number":
                jumps = find_jumps(values, factor)
                # find_jumps 给的是"第几个值"，换算成 frames 里的下标
                mapped = [(self._stats_positions[index] if index < len(self._stats_positions)
                           else index, before, after, diff)
                          for index, before, after, diff in jumps]
                self._jumps[key] = mapped
                positions = {position for position, _b, _a, _d in mapped}
                self._jump_positions[key] = positions
                self._jump_rows.update(positions)
        self._jump_keys = frozenset(self._jump_positions)
        self._render_rows()
        if self._current_key:
            self._show_column_detail(self._current_key)

    def _fill_stats_table(self) -> None:
        keys = self._visible_keys()
        byte_mode = self._mode() == "bytes"
        self.stats_table.setRowCount(len(keys))
        for row, key in enumerate(keys):
            index = int(key[1:])
            stats = self._stats[key]
            data = stats.to_row()
            title = stats.name
            if byte_mode:
                title = f"{stats.name}"
                if stats.unique == 1 and stats.valid:
                    title += f"　= {stats.top_values[0][0]}（恒定）"
                elif stats.unique > 1:
                    title += f"（{stats.unique} 种取值）"
            values = [title] + [data[name] for name in (
                "kind", "valid", "range", "mean", "median", "stdev", "unique",
                "changes", "first_last", "delta", "trend", "max_jump")]
            for col, text in enumerate(values):
                item = QTableWidgetItem(str(text))
                item.setData(Qt.ItemDataRole.UserRole, key)
                if col == 0:
                    item.setToolTip("点击可看该列曲线")
                self.stats_table.setItem(row, col, item)
        self.stats_table.resizeColumnsToContents()

    # ------------------------------------------------------------------ #
    # 渲染宽表（流式分片）
    # ------------------------------------------------------------------ #
    def _render_rows(self) -> None:
        specs = self._build_columns()
        signature = tuple((spec.key, spec.title, spec.width, spec.radix) for spec in specs)
        if signature != getattr(self, "_column_signature", None):
            # 只有列结构真的变了才重建列（否则用户手设的列格式会被反复重置）
            self._column_signature = signature
            self.table.set_columns(specs)
        self._column_specs = specs
        rows = self._build_rows()
        self.streamer.start(self.table, rows, chunk=64, budget_ms=8)

    @staticmethod
    def _column_formatter(column) -> object:
        """按列宽生成格式化函数：1 字节显示 EB，多字节显示 1E7 这种紧凑十六进制。"""
        width = max(1, int(getattr(column, "width", 1)))

        def formatter(value, radix: str) -> str:
            if not isinstance(value, int):
                return field_parser.format_value(value, radix)
            if radix == "hex":
                return f"{value:02X}" if width == 1 else f"{value:0{width * 2}X}"
            if radix == "bin":
                return format(value, f"0{width * 8}b")
            return str(value)
        return formatter

    def _build_columns(self) -> list[ColumnSpec]:
        columns = list(BASE_COLUMNS)
        byte_mode = self._mode() == "bytes"
        for index, key in enumerate(getattr(self, "_keys", [])):
            if not byte_mode and key in getattr(self, "_hidden_fields", set()):
                continue
            name = self._names[index]
            stats = self._stats.get(key)
            if byte_mode:
                column = self._resolved[index]
                title = name
                if column.width == 1 and stats and stats.unique == 1 and stats.valid:
                    title += f" = {stats.top_values[0][0]}"          # 恒定字节直接标出来
                columns.append(ColumnSpec(
                    key, title, width=64 if column.width == 1 else 92,
                    numeric=True, radix=column.radix, align="center",
                    monospace=True, formatter=self._column_formatter(column),
                    tooltip=(f"帧内偏移 {column.offset}"
                             + (f"（{column.width} 字节 {column.byte_order}）" if column.width > 1 else "")
                             + "；点列标题看它随帧号的变化"),
                ))
            else:
                fd = self._fields[index]
                columns.append(ColumnSpec(
                    key, f"{fd.name}" + (f" ({fd.unit})" if fd.unit else ""),
                    width=max(80, min(160, 60 + len(fd.name) * 8)),
                    numeric=True, radix=fd.radix if fd.radix != "auto" else "auto",
                    align="right", tooltip=f"第 {index + 1} 列｜类型 {fd.type_label}",
                ))
        return columns

    def _row_positions(self) -> list[int]:
        """表格要显示的行 → 对应的帧位置（"只看异常帧"时只留突变帧）。

        表格是滑动窗口：这里给出完整的行→帧位置表（整数列表，几十万也就几 MB），
        行数据滚到哪才现算哪一行。
        """
        positions = getattr(self, "_positions", None)
        if positions is None:
            positions = list(range(len(self._frames)))
        if not self.only_anomaly_box.isChecked():
            return list(positions)
        return [position for position in positions if position in self._jump_rows]

    def row_for(self, start: int, count: int) -> list[dict]:
        """虚拟行回调：算第 start 行起的 count 行。"""
        rows = []
        positions = self._row_positions_cache
        stop = min(len(positions), start + max(0, count))
        for row in range(max(0, start), stop):
            position = positions[row]
            frame = self._frames[position] if 0 <= position < len(self._frames) else None
            if frame is None:
                continue
            if self.byte_mode:
                row_data = analysis.rule_row(frame, self._resolved)
            else:
                parsed = self._parsed.get(frame.index, [])
                row_data = analysis.wide_row(frame, parsed, self._fields)
            background = {}
            for index, key in enumerate(getattr(self, "_keys", [])):
                if key not in self._jump_keys:
                    continue
                stats = self._stats.get(key)
                if stats and stats.kind == "number" and position in self._jump_positions.get(key, ()):
                    background[key] = theme.CHANGED_BG       # 该帧在这列是突变点 → 灰底
            if background:
                row_data.setdefault("_bg", {}).update(background)
            rows.append(row_data)
        return rows

    def _install_virtual(self) -> None:
        self._row_positions_cache = self._row_positions()
        self._display_position = list(self._row_positions_cache)
        self.table.begin_virtual(len(self._row_positions_cache), self.row_for,
                                 window=400, chunk=80)

    def _render_rows(self) -> None:
        specs = self._build_columns()
        signature = tuple((spec.key, spec.title, spec.width, spec.radix) for spec in specs)
        if signature != getattr(self, "_column_signature", None):
            self._column_signature = signature
            self.table.set_columns(specs)
        self._column_specs = specs
        self._install_virtual()

    # ------------------------------------------------------------------ #
    # 选中列 → 统计详情 + 曲线 + 突变清单
    # ------------------------------------------------------------------ #
    def _column_index_of(self, col: int) -> int:
        if col < len(BASE_COLUMNS):
            return -1
        return col - len(BASE_COLUMNS)

    def _on_column_clicked(self, col: int) -> None:
        index = self._column_index_of(col)
        if index < 0:
            self.chart.clear()
            self.jump_view.setPlainText("选中一个字段列（帧#/偏移/长度/校验列没有曲线）")
            return
        self._show_column_detail(analysis.column_key(index))

    def _on_table_selection(self) -> None:
        """选中宽表里的格子 → 告诉主窗口这段字节在哪（左侧原文反色定位）。

        整列/整片选择会让 Qt 连续发好几次 selectionChanged，这里按"落在哪一段字节"
        去重，避免同一个位置反复做定位（大数据下那是白花的钱）。
        """
        row, col = self.table.currentRow(), self.table.currentColumn()
        if row < 0 or col < len(BASE_COLUMNS):
            return
        positions = getattr(self, "_display_position", [])
        if not (0 <= row < len(positions)):
            return
        position = positions[row]
        if not (0 <= position < len(self._frames)):
            return
        frame = self._frames[position]
        index = col - len(BASE_COLUMNS)
        span = None
        if self._mode() == "bytes":
            if index < len(self._resolved):
                column = self._resolved[index]
                span = (frame.offset + column.offset, max(1, column.width))
        elif index < len(self._fields):
            fd = self._fields[index]
            span = (frame.offset + max(0, fd.offset), max(1, fd.length or 1))
        if span is None or span == getattr(self, "_last_locate", None):
            return
        self._last_locate = span
        self.bytes_requested.emit(*span)

    def _on_stats_selected(self) -> None:
        row = self.stats_table.currentRow()
        if row < 0:
            return
        item = self.stats_table.item(row, 0)
        if item is not None:
            self._show_column_detail(item.data(Qt.ItemDataRole.UserRole))

    def _show_column_detail(self, key: str) -> None:
        self._current_key = key
        index = int(key[1:])
        if index >= len(getattr(self, "_names", [])):
            return
        name = self._names[index]
        unit = ""
        extra = ""
        if self._mode() == "fields" and index < len(self._fields):
            fd = self._fields[index]
            unit = f" {fd.unit}" if fd.unit else ""
        elif self._mode() == "bytes" and index < len(self._resolved):
            column = self._resolved[index]
            extra = (f"　｜　帧内偏移 {column.offset}"
                     + (f"，{column.width} 字节 {column.byte_order}" if column.width > 1 else ""))
        values = self._values.get(key, [])
        series = analysis.downsample(values)
        # 曲线/突变的横轴用"帧#"（第几个值 → 帧号）
        labels = getattr(self, "_labels", [])
        series = [(labels[index] if index < len(labels) else index, value)
                  for index, value in series]
        jumps = {self._frame_label(position) for position, _b, _a, _d
                 in self._jumps.get(key, [])}
        self.chart.set_series(name, series, jumps, unit)
        stats = self._stats.get(key)
        head = f"【{name}】突变阈值 {self.sigma_spin.value():g}σ，共 {len(self._jumps.get(key, []))} 处"
        if stats and stats.unique == 1 and stats.valid:
            head += f"　｜　该列整段恒定 = {stats.top_values[0][0]}"
        if self._positions and len(self._positions) != len(self._frames):
            head += f"　｜　只统计行规则命中的 {len(self._positions)} 帧"
        self.jump_view.setPlainText(
            head + extra + "\n"
            + analysis.describe_jumps(self._jumps.get(key, []), label_of=self._frame_label)
        )

    def _frame_label(self, position: int) -> int:
        """frames 下标 → 帧号（找不到就原样返回）。"""
        if 0 <= position < len(self._frames):
            return int(getattr(self._frames[position], "index", position))
        return position

    # ------------------------------------------------------------------ #
    def copy_stats(self) -> None:
        keys = self._visible_keys()
        header = "\t".join(STATS_HEADERS)
        rows = []
        for key in keys:
            index = int(key[1:])
            data = self._stats[key].to_row()
            rows.append("\t".join([f"{index + 1}. {self._stats[key].name}"] + [str(data[name]) for name in (
                "kind", "valid", "range", "mean", "median", "stdev", "unique",
                "changes", "first_last", "delta", "trend", "max_jump")]))
        copy_to_clipboard("\n".join([header] + rows))
        self.status_message.emit(f"已复制 {len(keys)} 列的统计结果")

    def export_csv(self) -> None:
        if self.table.row_count == 0:
            QMessageBox.information(self, "导出", "还没有可导出的数据。")
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出宽表 CSV", "frames_wide.csv",
                                              "CSV 文件 (*.csv)")
        if not path:
            return
        header = [spec.title for spec in self.table.specs()]
        # 表格是滑动窗口，不能靠"读控件里的文本"导出：按行现算（滚过没滚过都一样）
        rows = []
        total = self.table.row_count
        block = 200
        for start in range(0, total, block):
            for row in self.row_for(start, min(block, total - start)):
                rows.append(self.table.texts_for_row(row))
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            handle.write(exporters.export_rows(header, rows))
        self.status_message.emit(f"已导出宽表 {path}（{len(rows)} 行）")

    # ------------------------------------------------------------------ #
    def begin_render(self) -> None:
        self.table.begin_stream()

    def end_render(self) -> None:
        self.table.end_stream()

    def flush(self) -> None:
        """把没算完的取值一次算完（切页签 / 自检 / 导出前调用）。"""
        if self.scan_pending():
            self._scan_timer.stop()
            self._scan_values(limit_frames=None)
            self._finish_rebuild()
        self.streamer.finish_now()

    @property
    def stats(self) -> dict[str, ColumnStats]:
        return self._stats
