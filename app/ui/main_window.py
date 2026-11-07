"""主窗口：把输入、分帧设置、字段定义、结果面板组合起来。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from PyQt6.QtCore import QElapsedTimer, QSettings, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QKeySequence
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QGroupBox, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QProgressBar,
    QPushButton, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem, QToolBar,
    QVBoxLayout, QWidget,
)

from .. import (
    __version__, checksum, codec, config_io, exporters, extract, field_parser,
    framer, presets, source,
)
from ..field_parser import FieldDef, ParsedField, summarize
from .. import infer
from .. import rows as rows_mod
from ..pipeline import ParsePipeline, ParseRequest, ParseState, Stage
from .bit_editor import BitEditorPanel
from .analysis_tab import AnalysisPanel
from .extract_dialog import ExtractDialog
from .field_table import FieldTableWidget
from .infer_dialog import InferDialog
from . import theme
from .panels import FrameSettingsPanel, InputPanel, hint_label
from .results import BytesViewPanel, FieldResultPanel, FrameListPanel, JsonPanel, LogPanel
from .table_base import SmartTable
from .util import copy_to_clipboard
FOLLOW_LIMIT = 512 * 1024          # 跟随文件时最多读多少字节
AUTOPARSE_DELAY = 350              # 输入变化后多久自动解析（毫秒）
PROBE_FRAME_LIMIT = 200            # 试算校验时最多用多少帧
PARSE_CACHE = 4000                 # 按需解析缓存多少帧的字段结果（滑动窗口用）


# --------------------------------------------------------------------------- #
# 校验算法试算对话框（逆向未知协议时最省事的功能）
# --------------------------------------------------------------------------- #
class ChecksumProbeDialog(QDialog):
    """把所有校验算法都试一遍，看哪个能对上，一键应用。"""

    def __init__(self, frames, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("试算校验算法")
        self.resize(620, 420)
        self._choice: tuple[str, str] | None = None

        layout = QVBoxLayout(self)
        tip = QLabel(
            f"对当前解析出的 {min(len(frames), PROBE_FRAME_LIMIT)} 帧逐一尝试「帧尾校验」的各种算法，"
            "匹配帧数最多的就是最可能的算法。双击某行即可直接应用。"
        )
        tip.setWordWrap(True)
        layout.addWidget(tip)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["算法", "字节序", "匹配帧数", "首帧计算值 / 帧内值"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("应用选中算法")
        buttons.accepted.connect(self._apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.table.itemDoubleClicked.connect(lambda _i: self._apply())

        self._probe(frames.window(0, PROBE_FRAME_LIMIT)
                    if hasattr(frames, "window") else list(frames)[:PROBE_FRAME_LIMIT])

    def _probe(self, frames) -> None:
        rows = []
        for key, label, size, _default in checksum.ALGOS:
            if not size:
                continue
            for order in ("big", "little"):
                matched = 0
                checked = 0
                detail = ""
                for frame in frames:
                    data = frame.data
                    if len(data) <= size:
                        continue
                    checked += 1
                    body, actual = data[:-size], data[-size:]
                    expect = checksum.compute(key, body, order)
                    if expect == actual:
                        matched += 1
                    elif not detail:
                        detail = f"{expect.hex(' ').upper()} / {actual.hex(' ').upper()}"
                rows.append((matched, checked, key, label, order, detail))
        rows.sort(key=lambda r: (-r[0], r[3]))
        self.table.setRowCount(len(rows))
        for row, (matched, checked, key, label, order, detail) in enumerate(rows):
            values = [
                label, "大端" if order == "big" else "小端",
                f"{matched} / {checked}", detail,
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, (key, order))
                if matched == checked and checked:
                    item.setForeground(Qt.GlobalColor.black)
                self.table.setItem(row, col, item)
        if rows:
            self.table.selectRow(0)

    def _apply(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            self.reject()
            return
        item = self.table.item(row, 0)
        if item is not None:
            self._choice = item.data(Qt.ItemDataRole.UserRole)
        self.accept()

    def choice(self) -> tuple[str, str] | None:
        return self._choice


# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"数据帧解析器 frame_parser v{__version__}")
        self.resize(1560, 960)

        self.settings = QSettings(config_io.ORG_NAME, config_io.APP_NAME)
        self.last_dir = self.settings.value("last_dir", "", type=str)
        self.sample_dir = self.settings.value("sample_dir", str(presets.DATA_DIR), type=str)

        self._data = b""
        self._split: framer.SplitResult | None = None
        self._parsed_by_frame: dict[int, list[ParsedField]] = {}
        self._current_frame = -1
        self._follow_path = ""
        self._follow_stat: tuple[float, int] | None = None
        self._loading = False
        self._source = None            # 大文件分页来源（app/source.py）
        self._syncing = False          # 正在做"表格 ↔ 原文"同步，避免互相触发
        self._frames_shown = 0         # 已经喂给表格的帧数（工具栏计数用）
        self._counter_clock = QElapsedTimer()

        # 流式解析管线 + 脏标记（决定"从哪一步开始重算"）
        self.pipeline = ParsePipeline(self)
        self._dirty: int | None = None
        self._json_dirty = True
        self._bytes_dirty = True
        self._bits_dirty = True
        self._analysis_dirty = True

        self._build_ui()
        self._build_actions()

        self.autoparse_timer = QTimer(self)
        self.autoparse_timer.setSingleShot(True)
        self.autoparse_timer.timeout.connect(self.parse_now)
        self.follow_timer = QTimer(self)
        self.follow_timer.setInterval(600)
        self.follow_timer.timeout.connect(self._follow_tick)

        self.pipeline.stage_changed.connect(self._on_stage_changed)
        self.pipeline.progress.connect(self._on_progress)
        self.pipeline.frames_ready.connect(self._on_frames_ready)
        self.pipeline.chunk_ready.connect(self._on_chunk)
        self.pipeline.finished.connect(self._on_finished)
        self.pipeline.failed.connect(self._on_pipeline_failed)

        self._restore_state()
        if not self.input_panel.raw_text().strip():
            self.load_preset(presets.PRESET_AA55.key)
        else:
            QTimer.singleShot(50, self.parse_now)

    # ------------------------------------------------------------------ #
    # 界面
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        splitter = QSplitter(Qt.Orientation.Vertical)

        top = QSplitter(Qt.Orientation.Horizontal)

        left_tabs = QTabWidget()
        self.input_panel = InputPanel()
        self.frame_settings = FrameSettingsPanel()
        left_tabs.addTab(self.input_panel, "输入数据")
        left_tabs.addTab(self.frame_settings, "分帧设置")
        left_tabs.setMinimumWidth(360)
        top.addWidget(left_tabs)

        right_tabs = QTabWidget()
        self.frame_list = FrameListPanel()
        self.field_result = FieldResultPanel()
        self.analysis = AnalysisPanel()
        self.bytes_view = BytesViewPanel()
        self.bit_editor = BitEditorPanel(self.frame_settings.config)
        self.log_panel = LogPanel()
        self.json_panel = JsonPanel()
        right_tabs.addTab(self.frame_list, "帧列表")
        right_tabs.addTab(self.field_result, "字段解析")
        right_tabs.addTab(self.analysis, "数据表分析")
        right_tabs.addTab(self.bytes_view, "进制视图")
        right_tabs.addTab(self.bit_editor, "位编辑器")
        right_tabs.addTab(self.log_panel, "日志")
        right_tabs.addTab(self.json_panel, "JSON")
        self.right_tabs = right_tabs
        top.addWidget(right_tabs)
        top.setSizes([470, 1090])
        splitter.addWidget(top)

        bottom = QGroupBox("字段定义表（点列标题=整列选择，右键=列颜色；类型/字节序/显示进制用下拉选择）")
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(4, 4, 4, 4)
        self.field_table = FieldTableWidget()
        bottom_layout.addWidget(self.field_table)
        splitter.addWidget(bottom)
        splitter.setSizes([640, 300])

        self.setCentralWidget(splitter)
        self.main_splitter = splitter

        self.status_bytes = QLabel("输入 0 字节")
        self.status_frames = QLabel("帧 0")
        self.status_check = QLabel("校验 —")
        self.status_time = QLabel("耗时 —")
        self.stage_label = QLabel("")
        self.stage_label.setStyleSheet(f"color:{theme.FG_DIM};")
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedWidth(140)
        self.progress_bar.setFixedHeight(14)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setVisible(False)
        for label in (self.stage_label, self.progress_bar, self.status_bytes,
                      self.status_frames, self.status_check, self.status_time):
            self.statusBar().addPermanentWidget(label)
        self.statusBar().showMessage("就绪")

        self.input_panel.changed.connect(self._on_input_changed)
        self.input_panel.import_requested.connect(self.import_data_file)
        self.input_panel.clear_requested.connect(self.clear_all)
        self.frame_settings.changed.connect(self._on_frame_settings_changed)
        self.frame_settings.smart_requested.connect(self.smart_framing)
        self.field_table.changed.connect(self._on_fields_changed)
        self.frame_list.frame_selected.connect(self._on_frame_selected)
        self.frame_list.render_dirty.connect(lambda: self._mark_dirty(Stage.RENDER))
        self.field_result.needs_reparse.connect(lambda: self._mark_dirty(Stage.FIELDS))
        self.bit_editor.status_message.connect(self.statusBar().showMessage)
        self.bit_editor.set_input_requested.connect(self._apply_bit_result)
        self.analysis.status_message.connect(self.statusBar().showMessage)
        self.analysis.fields_requested.connect(self.load_field_definitions)
        self.right_tabs.currentChanged.connect(self._on_tab_changed)
        for table in self._tables():
            table.status_message.connect(self.statusBar().showMessage)
            table.column_style_changed.connect(self._on_column_style_changed)
            table.selection_changed.connect(
                lambda text: self.statusBar().showMessage(text) if text else None
            )
        # 表格 ↔ 左侧原文 双向定位
        for panel in (self.frame_list, self.field_result, self.analysis,
                      self.bytes_view, self.bit_editor):
            panel.bytes_requested.connect(self._jump_input_to_bytes)
        self.input_panel.bytes_selected.connect(self._jump_table_to_bytes)
        self.input_panel.source_exited.connect(self._release_source)

    def _tables(self) -> list[SmartTable]:
        return [
            self.frame_list.table, self.field_result.table, self.analysis.table,
            self.bytes_view.table, self.field_table.table,
        ]

    def _build_actions(self) -> None:
        toolbar = QToolBar("主工具栏")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        menu_file = self.menuBar().addMenu("文件(&F)")
        menu_parse = self.menuBar().addMenu("解析(&P)")
        menu_preset = self.menuBar().addMenu("预设(&R)")
        menu_view = self.menuBar().addMenu("视图(&V)")
        menu_help = self.menuBar().addMenu("帮助(&H)")

        self.act_parse = QAction("立即解析", self)
        self.act_parse.setShortcut(QKeySequence("F5"))
        self.act_parse.setToolTip("按脏标记从对应阶段开始重算（F5）")
        self.act_parse.triggered.connect(self.parse_now)
        toolbar.addAction(self.act_parse)
        menu_parse.addAction(self.act_parse)

        # 「一共解析出多少帧」——放在工具栏最左边，切哪个页签都看得见
        self.frame_counter = QLabel("共 0 帧")
        self.frame_counter.setStyleSheet(
            f"color:{theme.FG_STRONG}; font-weight:bold; font-family:Consolas,monospace;"
            f" border:1px solid {theme.BORDER}; background:{theme.BG_ALT};"
            f" padding:1px 8px; margin-left:6px;"
        )
        self.frame_detail = QLabel("")
        self.frame_detail.setStyleSheet(f"color:{theme.FG_DIM}; margin-right:6px;")
        toolbar.addWidget(self.frame_counter)
        toolbar.addWidget(self.frame_detail)
        toolbar.addSeparator()

        self.act_autoparse = QAction("自动解析（输入即解析）", self)
        self.act_autoparse.setCheckable(True)
        self.act_autoparse.setChecked(True)
        menu_parse.addAction(self.act_autoparse)

        self.act_stream = QAction("流式渲染（分片计算，边算边显示）", self)
        self.act_stream.setCheckable(True)
        self.act_stream.setChecked(True)
        self.act_stream.setToolTip(
            "按脏标记 + 时间片推进：列格式/颜色只重绘，字段改动不重新分帧，结果边算边长出来。\n"
            "关掉则每次一次性算完再刷新（数据很小时更直观）"
        )
        self.act_stream.toggled.connect(self._on_stream_toggled)
        menu_parse.addAction(self.act_stream)
        toolbar.addAction(self.act_stream)

        self.budget_combo = QComboBox()
        for ms in (4, 8, 12, 20, 40):
            self.budget_combo.addItem(f"{ms} ms", ms)
        self.budget_combo.setCurrentIndex(2)
        self.budget_combo.setToolTip("每个时间片算多少毫秒后让出界面：越小越流畅，越大吞吐越高")
        budget_label = QLabel(" 切片 ")
        toolbar.addWidget(budget_label)
        toolbar.addWidget(self.budget_combo)
        self.budget_label = budget_label

        self.act_follow = QAction("跟随文件（实时刷新）", self)
        self.act_follow.setCheckable(True)
        self.act_follow.triggered.connect(self._toggle_follow)
        menu_parse.addAction(self.act_follow)
        toolbar.addAction(self.act_follow)

        self.act_probe = QAction("试算校验算法…", self)
        self.act_probe.triggered.connect(self.probe_checksum)
        menu_parse.addAction(self.act_probe)
        toolbar.addAction(self.act_probe)

        self.act_infer = QAction("自动推断分帧参数…", self)
        self.act_infer.setShortcut(QKeySequence("Ctrl+I"))
        self.act_infer.setToolTip("统计推断帧头 / 帧长 / 长度字段语义 / 校验算法，给出候选并一键应用")
        self.act_infer.triggered.connect(self.infer_frames)
        menu_parse.addAction(self.act_infer)
        toolbar.addAction(self.act_infer)

        menu_parse.addSeparator()
        self.act_clear = QAction("清空全部", self)
        self.act_clear.triggered.connect(self.clear_all)
        menu_parse.addAction(self.act_clear)

        # 文件
        self.act_open = QAction("导入数据文件…", self)
        self.act_open.setShortcut(QKeySequence.StandardKey.Open)
        self.act_open.triggered.connect(self.import_data_file)
        menu_file.addAction(self.act_open)
        toolbar.addAction(self.act_open)

        self.act_extract = QAction("从日志提取数据帧…", self)
        self.act_extract.setShortcut(QKeySequence("Ctrl+E"))
        self.act_extract.setToolTip("用正则 + 行过滤从 JSONL / CSV / 日志里抠出数据段，可按 can_id 只看某一路")
        self.act_extract.triggered.connect(lambda: self.extract_from_log())
        menu_file.addAction(self.act_extract)
        toolbar.addAction(self.act_extract)

        self.menu_samples = menu_file.addMenu("样本数据")
        self._refresh_sample_menu()

        self.act_export_result = QAction("导出解析结果 JSON…", self)
        self.act_export_result.setShortcut(QKeySequence.StandardKey.Save)
        self.act_export_result.triggered.connect(self.export_result)
        menu_file.addAction(self.act_export_result)

        self.act_export_csv = QAction("导出字段表 CSV…", self)
        self.act_export_csv.triggered.connect(lambda: self.export_table_csv(self.field_result.table, "字段解析"))
        menu_file.addAction(self.act_export_csv)

        self.act_export_frame_csv = QAction("导出帧列表 CSV…", self)
        self.act_export_frame_csv.triggered.connect(lambda: self.export_table_csv(self.frame_list.table, "帧列表"))
        menu_file.addAction(self.act_export_frame_csv)

        menu_file.addSeparator()
        self.act_save_cfg = QAction("导出配置…", self)
        self.act_save_cfg.triggered.connect(self.export_config)
        menu_file.addAction(self.act_save_cfg)
        self.act_load_cfg = QAction("导入配置…", self)
        self.act_load_cfg.triggered.connect(self.import_config)
        menu_file.addAction(self.act_load_cfg)
        menu_file.addSeparator()
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self.close)
        menu_file.addAction(act_quit)

        # 预设
        for key, preset in presets.PRESETS.items():
            action = QAction(preset.name, self)
            action.triggered.connect(lambda _=False, k=key: self.load_preset(k))
            menu_preset.addAction(action)
        menu_preset.addSeparator()
        sub_fields = menu_preset.addMenu("只换字段模板")
        for name, fields in presets.FIELD_TEMPLATES.items():
            action = QAction(name, self)
            action.triggered.connect(lambda _=False, f=fields: self.field_table.set_fields(f))
            sub_fields.addAction(action)

        # 视图
        self.act_tooltips = QAction("单元格悬停提示（原始值/换算/多进制）", self)
        self.act_tooltips.setCheckable(True)
        self.act_tooltips.setChecked(True)
        self.act_tooltips.toggled.connect(self._toggle_tooltips)
        menu_view.addAction(self.act_tooltips)

        act_reset_cols = QAction("恢复全部列默认样式", self)
        act_reset_cols.triggered.connect(self._reset_columns)
        menu_view.addAction(act_reset_cols)

        act_copy_json = QAction("复制结果 JSON", self)
        act_copy_json.triggered.connect(lambda: copy_to_clipboard(self.json_panel.view.toPlainText()))
        menu_view.addAction(act_copy_json)

        menu_help.addAction(QAction("使用说明", self, triggered=self.show_help))
        menu_help.addAction(QAction("关于", self, triggered=self.show_about))

    # ------------------------------------------------------------------ #
    # 解析
    # ------------------------------------------------------------------ #
    def _on_input_changed(self) -> None:
        self._mark_dirty(Stage.DECODE)

    def _on_frame_settings_changed(self) -> None:
        self._mark_dirty(Stage.FRAME)

    def _on_fields_changed(self) -> None:
        self._mark_dirty(Stage.FIELDS)

    def _on_pipeline_failed(self, message: str) -> None:
        self.statusBar().showMessage(f"解析失败：{message}")
        self.log_panel.set_text(f"解析失败：{message}")

    def smart_framing(self) -> None:
        """按当前输入数据自动判断帧格式（一行一帧 → 定长/换行；否则自相关）。"""
        text_source = getattr(self, "_source", None)
        if text_source is not None:
            # 大文件：只看开头一段（不为了识别帧格式去读整个文件）
            sample_text = text_source.sample_text(max_lines=2000, max_chars=200000)
            data, _warnings = codec.text_to_bytes_ex(
                sample_text, self.input_panel.radix(),
                self.input_panel.dec_width(), self.input_panel.byte_order(),
            )
        else:
            try:
                data, _warnings = self.input_panel.to_bytes_ex()
            except codec.CodecError as exc:
                QMessageBox.information(self, "智能识别", f"先要有可解析的输入数据：{exc}")
                return
            sample_text = self.input_panel.raw_text()
        if not sample_text.strip():
            QMessageBox.information(self, "智能识别", "输入数据为空。")
            return
        config, reason = infer.suggest_framing(sample_text, data)
        self.frame_settings.set_config(config)
        self._last_smart_note = reason
        self._mark_dirty(Stage.FRAME)
        self.parse_now()
        self.statusBar().showMessage(f"智能识别：{reason}")

    def _mark_dirty(self, stage: int) -> None:
        """标记"从哪一步开始重算"。取最脏的那一级，避免重复劳动。"""
        if self._loading:
            return
        self._dirty = stage if self._dirty is None else min(self._dirty, stage)
        if self.act_autoparse.isChecked():
            self.autoparse_timer.start(AUTOPARSE_DELAY)

    def _consume_dirty(self) -> int:
        stage = self._dirty if self._dirty is not None else Stage.DECODE
        self._dirty = None
        return int(stage)

    # ------------------------------------------------------------------ #
    # 解析：交给流式管线（解码 → 分帧 → 解析字段 → 渲染，按时间片推进）
    # ------------------------------------------------------------------ #
    def pipeline_render_limit(self) -> int:
        """管线一次喂给帧列表多少帧。

        表格是滑动窗口（滚到哪加载哪），所以这里不需要再设"最多显示多少帧"；
        给一个大到不会成为瓶颈的值即可（筛选仍按全部帧算）。
        """
        return 1_000_000

    # ------------------------------------------------------------------ #
    # 按需解析：字段表 / 帧列表摘要 滚到哪解析哪（表格因此不设帧数上限）
    # ------------------------------------------------------------------ #
    def _cached_parse(self, position: int):
        """解析某一帧的字段（带 LRU 缓存；同一个位置只解一次）。"""
        split = self._split
        if split is None:
            return None
        frames = split.frames
        if not 0 <= position < len(frames):
            return None
        frame_index = frames[position].index
        parsed = self._parsed_by_frame.get(frame_index)
        if parsed is not None:
            return parsed
        fields = self.pipeline.state.fields or [fd for fd in self.field_table.fields()]
        if not fields:
            return []
        parsed = field_parser.parse_fields(frames[position].data, fields, frame_index)
        if len(self._parsed_by_frame) > PARSE_CACHE:
            for key in list(self._parsed_by_frame)[:PARSE_CACHE // 4]:
                self._parsed_by_frame.pop(key, None)
        self._parsed_by_frame[frame_index] = parsed
        return parsed

    def field_rows_for(self, position: int, offset: int, count: int) -> list[dict]:
        """字段解析表的行来源：第 position 帧的第 offset 个字段起 count 个。"""
        split = self._split
        if split is None:
            return []
        frames = split.frames
        if not 0 <= position < len(frames):
            return []
        frame = frames[position]
        parsed = self._cached_parse(position) or []
        fields = self.pipeline.state.fields or []
        rows: list[dict] = []
        for index in range(max(0, offset), min(len(parsed), offset + max(0, count))):
            item = parsed[index]
            fd = fields[index] if index < len(fields) else None
            if fd is None:
                continue
            row = rows_mod.field_row(item, fd, frame.offset)
            row["frame"] = frame.index
            row["ok"] = item.ok
            rows.append(row)
        return rows

    def frame_summary(self, position: int) -> str:
        """某一帧的字段摘要（帧列表"摘要"列滚到哪算哪）。"""
        split = self._split
        if split is None:
            return ""
        frames = split.frames
        if not 0 <= position < len(frames):
            return ""
        frame_index = frames[position].index
        cached = self.pipeline.state.summaries.get(frame_index)
        if cached is not None:
            return cached
        parsed = self._cached_parse(position)
        if not parsed:
            return ""
        summary = summarize(parsed)
        self.pipeline.state.summaries[frame_index] = summary
        return summary

    def _build_request(self) -> ParseRequest:
        # 只有"输入面板还在分页模式、而且用的是同一份来源"时才走大文件路径；
        # 用户一旦手输/粘贴/清空，面板会退出分页模式，这里自动回到内存路径
        text_source = getattr(self, "_source", None)
        if text_source is not None and self.input_panel.source is not text_source:
            text_source = None
        return ParseRequest(
            text=self.input_panel.raw_text(),
            source=text_source,                        # 大文件：走分页 + 流式索引
            radix=self.input_panel.radix(),
            dec_width=self.input_panel.dec_width(),
            byte_order=self.input_panel.byte_order(),
            config=self.frame_settings.config(),
            fields=self.field_table.fields(),
            frame_limit=self.field_result.frame_limit(),
            render_limit=self.pipeline_render_limit(),
        )

    def parse_now(self, blocking: bool | None = None) -> None:
        """按脏标记从对应阶段开始重算。blocking=None 时跟随「流式渲染」开关。"""
        stream = self.act_stream.isChecked() if blocking is None else not blocking
        stage = self._consume_dirty()
        request = self._build_request()
        self._prepare_panels(stage)
        if request.source is None and not request.text.strip():
            self._on_finished(self.pipeline.state)
            self.statusBar().showMessage("输入数据为空")
            return
        budget = int(self.budget_combo.currentData() or 12)
        self.pipeline.submit(request, stage, streaming=stream, budget_ms=budget)
        if not stream:
            self._on_finished(self.pipeline.state)

    def _prepare_panels(self, stage: int) -> None:
        """按阶段决定哪些面板要重建：只重渲染时复用字段缓存，字段重算时才丢缓存。"""
        self._keep_cache = stage > Stage.FIELDS
        self._frames_shown = 0
        self.frame_list.begin_render()
        self.field_result.begin_render(self._keep_cache)
        if not self._keep_cache:
            self._json_dirty = True
            self._analysis_dirty = True
        if stage <= Stage.FRAME:
            self._bytes_dirty = True
            self._bits_dirty = True

    # ---------------------------- 管线回调 ---------------------------- #
    def _on_stage_changed(self, name: str) -> None:
        if name:
            self.stage_label.setText(f"{name}…")
            self.progress_bar.setVisible(True)
            self.progress_bar.setRange(0, 0)      # 未知总量时用忙碌条
        else:
            self.stage_label.setText("")
            self.progress_bar.setVisible(False)

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(min(done, total))
            self.progress_bar.setFormat(f"%v/%m")
        else:
            self.progress_bar.setRange(0, 0)

    def _on_frames_ready(self, split: framer.SplitResult) -> None:
        self._split = split
        self._data = self.pipeline.state.data
        # 帧总数在分帧刚做完就知道了 —— 立刻显示，不等全部解析完
        self._frames_shown = 0
        self.frame_list.set_frame_source(split.frames)
        self._refresh_frame_counter()

    def _on_chunk(self, frames, summaries, rows) -> None:
        """每解析完一块就喂给表格 —— 解析与渲染在同一个时间片里完成。"""
        self.frame_list.render_chunk(frames, summaries)
        self.field_result.render_chunk(rows, getattr(self, "_keep_cache", False))
        self._frames_shown = getattr(self, "_frames_shown", 0) + len(frames)
        if not self._counter_clock.isValid() or self._counter_clock.elapsed() >= 100:
            self._counter_clock.restart()      # 最多每 100 ms 刷一次，别为计数拖慢解析
            self._refresh_frame_counter()

    def _split_stats(self) -> dict:
        """分帧统计（帧长分布 / 总字节 / 校验通过失败数）——按 split 记忆化。

        这些属性每个都要把几万~几十万帧遍历一遍；工具栏帧数、状态栏、解析报告都用到，
        不缓存的话一次解析要白扫好几遍（28 万帧 × 若干遍 = 几十秒）。
        """
        split = self._split
        if split is None:
            return {"sizes": [], "total_bytes": 0, "ok": 0, "fail": 0, "frames": 0}
        cached = getattr(self, "_split_stats_cache", None)
        if cached is not None and cached[0] is split:
            return cached[1]
        stats = {
            "sizes": sorted({frame.size for frame in split.frames}),
            "total_bytes": split.total_bytes,
            "ok": split.ok_count,
            "fail": split.fail_count,
            "frames": len(split.frames),
        }
        self._split_stats_cache = (split, stats)
        return stats

    def _refresh_frame_counter(self) -> None:
        """工具栏左侧的「共 N 帧」：始终可见，流式解析时还会继续长。"""
        split = self._split
        total = len(split.frames) if split else 0
        stats = self._split_stats()
        shown = min(getattr(self, "_frames_shown", 0), total) if total else 0
        self.frame_counter.setText(f"共 {total} 帧")
        self.status_frames.setText(f"帧 {total}")
        parts = []
        if total:
            sizes = stats["sizes"]
            parts.append(f"× {sizes[0]} 字节/帧" if len(sizes) == 1
                         else f"平均 {stats['total_bytes'] / total:.1f} 字节/帧")
            if self.pipeline.running and 0 < shown < total:
                parts.append(f"已出 {shown}")
        self.frame_detail.setText("　".join(parts))

        tips = [f"一共解析出 {total} 帧，共 {stats['total_bytes']} 字节" if total
                else "还没有解析出帧"]
        if total:
            sizes = stats["sizes"]
            tips.append("帧长：" + (f"恒定 {sizes[0]} 字节" if len(sizes) == 1
                                   else f"{len(sizes)} 种（{sizes[0]} ~ {sizes[-1]} 字节）"))
            tips.append(f"校验：✓ {stats['ok']} / ✗ {stats['fail']} / "
                        f"— {total - stats['ok'] - stats['fail']}")
            tips.append("帧数 = 「帧列表」的行数 = 「数据表分析」的行数；"
                        "分析页的「行规则」可以只分析其中一段帧")
        text = "\n".join(tips)
        self.frame_counter.setToolTip(text)
        self.frame_detail.setToolTip(text)

    def _on_finished(self, state) -> None:
        if state.split is None:
            self._split = None
            self._frames_shown = 0
            self._refresh_frame_counter()
            return
        self._data = state.data
        self._split = state.split
        self._parsed_by_frame = state.parsed
        self.frame_list.end_render()
        self.field_result.end_render()
        # 字段表改成虚拟行：行 = 帧 × 字段，滚到哪解析哪（不再限制"最多几帧"）
        self.field_result.row_provider = self.field_rows_for
        self.field_result.set_virtual_source(state.split.frames,
                                             len(state.fields or self.field_table.fields()))
        # 帧列表的"摘要"列：没预解析的帧按需解析
        self.frame_list.summary_provider = self.frame_summary

        # 大文件路径下 data 只是"按需视图"，真实字节数看 data_size / source
        data_size = int(getattr(state, "data_size", 0) or len(state.data))
        used_radix = state.radix_used or self.input_panel.effective_radix()
        self.input_panel.set_parsed_info(data_size, used_radix)   # 回填缓存，别再整篇重算
        info = f"已解析 {data_size} 字节"
        if self.input_panel.radix() == codec.RADIX_AUTO and used_radix in codec.RADIX_LABELS:
            info += f"（自动识别为 {codec.RADIX_LABELS[used_radix]}）"
        if state.warnings:
            info += f"，跳过 {len(state.warnings)} 行无法解析的内容"
        if getattr(state, "source", None) is not None:
            info += "　│　大文件模式：按需读取，内存不随文件增长"
        self.input_panel.set_info(info, ok=True)

        self._update_status(data_size, state.split, state.elapsed_ms)
        self.input_panel.set_spans(state.spans)      # 表格→原文定位要用
        self.log_panel.set_text(self._build_log(state))
        self._refresh_visible_lazy_panels()

        hints = self._mismatch_hints(data_size, state.split)
        if hints:
            self.statusBar().showMessage(
                "⚠ " + hints[0] + "　→ 可以试「自动推断分帧参数」(Ctrl+I)"
            )
            self.right_tabs.setCurrentWidget(self.log_panel)
        else:
            soft = self._soft_hints(state.split)
            if soft:
                self.statusBar().showMessage(soft[0])

    # ---------------------------- 惰性面板 ---------------------------- #
    def _on_tab_changed(self, _index: int) -> None:
        self._refresh_visible_lazy_panels()

    def _refresh_visible_lazy_panels(self) -> None:
        """只算"看得见的"：没显示的面板挂起，切过去时再一次性补齐。"""
        widget = self.right_tabs.currentWidget()
        if not self._split:
            return
        index = self.frame_list.selected_frame_index()
        if widget is self.bytes_view and self._bytes_dirty:
            self.bytes_view.set_source(self._data, self._split.frames, index)
            self._bytes_dirty = False
        elif widget is self.bit_editor and self._bits_dirty:
            self.bit_editor.set_source(self._data, self._split.frames, index)
            self._bits_dirty = False
        elif widget is self.json_panel and self._json_dirty:
            self.json_panel.set_text(self._build_json())
            self._json_dirty = False
        elif widget is self.analysis and self._analysis_dirty:
            self.analysis.set_source(self._split.frames, self._parsed_by_frame,
                                     self.pipeline.state.fields)
            self._analysis_dirty = False

    def _build_json(self) -> str:
        return json.dumps(
            config_io.build_result_document(
                self._input_info(self._data, self.pipeline.state.radix_used or "hex"),
                self.frame_settings.config(), self._split, self._parsed_by_frame,
                self._column_states(), limit_frames=200,
            ),
            ensure_ascii=False, indent=2, default=str,
        )

    def _request_full_refresh(self) -> None:
        self._dirty = Stage.DECODE
        self.parse_now(blocking=not self.act_stream.isChecked())

    def _input_info(self, data: bytes, used_radix: str) -> dict:
        return {
            "bytes": len(data),
            "radix_setting": self.input_panel.radix(),
            "radix_used": used_radix,
            "dec_width": self.input_panel.dec_width(),
            "byte_order": self.input_panel.byte_order(),
            "text_preview": self.input_panel.raw_text()[:2000],
        }

    def _build_log(self, state) -> str:
        """解析报告。异常清单在解析时顺手收集（state.problems），这里不再重扫一遍。"""
        cfg = state.config or self.frame_settings.config()
        split = state.split or framer.SplitResult()
        data_size = int(getattr(state, "data_size", 0) or len(state.data))
        used_radix = state.radix_used or self.input_panel.radix()
        frame_limit = state.frame_limit
        source = getattr(state, "source", None)
        lines = [
            "=== 解析结果 ===",
            f"时间        : {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"输入        : {data_size} 字节，进制 {self.input_panel.radix()}"
            f"（实际按 {codec.RADIX_LABELS.get(used_radix, used_radix)} 解析）"
            + (f"，来源 {source.source_label}（分页 + 按需读取）" if source is not None else ""),
            f"分帧方式    : {framer.MODE_LABELS.get(cfg.mode, cfg.mode)}"
            + (f"，帧头 {cfg.header_hex}" if cfg.mode == framer.MODE_HEADER else "")
            + (f"，分隔符 {cfg.delimiter_hex}" if cfg.mode == framer.MODE_DELIMITER else "")
            + (f"，定长 {cfg.fixed_len}" if cfg.mode == framer.MODE_FIXED else "")
            + (f"，跳过前 {cfg.skip_bytes} 字节" if cfg.skip_bytes else ""),
            f"校验算法    : {checksum.ALGO_LABELS.get(cfg.checksum_algo, '无')}"
            + (f"（{'大端' if cfg.checksum_byte_order == 'big' else '小端'}）"
               if cfg.checksum_algo != 'none' else ""),
            f"帧数        : {len(split.frames)} 帧，共 {self._split_stats()['total_bytes']} 字节"
            f"；校验通过 {self._split_stats()['ok']}，失败 {self._split_stats()['fail']}",
            f"字段定义    : {len(state.fields)} 个字段",
        ]
        if frame_limit and len(split.frames) > frame_limit:
            lines.append(
                f"字段表      : 只解析了前 {frame_limit} 帧（在「字段解析」页改「最多 N 帧」可调大；"
                "帧列表、进制视图、导出 JSON 不受影响）"
            )
        lines.append(f"耗时        : {state.elapsed_ms:.2f} ms"
                     + (f"（流式分 {self.pipeline.ticks} 片推进）" if self.pipeline.ticks > 1 else ""))
        hints = self._mismatch_hints(data_size, split)
        if hints:
            lines.append("")
            lines.append("--- ⚠ 分帧方式可能不匹配 ---")
            for hint in hints:
                lines.append(f"  · {hint}")
            lines.append("  · 点工具栏「自动推断分帧参数」(Ctrl+I)，或换用与数据匹配的预设")
        soft = self._soft_hints(split)
        if soft:
            lines.append("")
            lines.append("--- 提示 ---")
            for hint in soft:
                lines.append(f"  · {hint}")
        if getattr(self, "_last_smart_note", ""):
            lines.append("")
            lines.append(f"--- 智能识别帧格式 ---")
            lines.append(f"  · {self._last_smart_note}")
        if state.warnings:
            lines.append("")
            lines.append(f"--- 输入解析警告 ({len(state.warnings)}) ---")
            lines.extend(f"  · {w}" for w in state.warnings[:20])
        if split.warnings:
            lines.append("")
            lines.append(f"--- 分帧警告 ({len(split.warnings)}) ---")
            lines.extend(f"  · {w}" for w in split.warnings[:20])
        if state.problems:
            lines.append("")
            lines.append(f"--- 字段解析异常 ({len(state.problems)}) ---")
            lines.extend(f"  帧#{f} {n}：{note}" for f, n, note in state.problems[:50])
        lines.append("")
        lines.append("--- 调试提示 ---")
        lines.append("  · 列标题左键=整列选择，Ctrl+左键=多列；右键=列颜色 / 显示格式 / 换算函数")
        lines.append("  · 鼠标悬停单元格可看原始值、换算结果与 HEX/DEC/BIN/ASCII")
        lines.append("  · 校验总是不通过时，用「试算校验算法」自动匹配")
        lines.append("  · 日志/CSV 里的数据用「从日志提取…」按正则 + 行过滤抠出来，可只看某个 CAN ID")
        lines.append("  · 选项改变只重算受影响的那一段（列格式/颜色只重绘，字段改动不重新分帧）")
        return "\n".join(lines)

    def _update_status(self, size: int, split: framer.SplitResult, elapsed_ms: float) -> None:
        stats = self._split_stats()
        total = len(split.frames)
        self.status_bytes.setText(f"输入 {size} 字节")
        self.status_frames.setText(f"帧 {total}")
        if stats["ok"] or stats["fail"]:
            self.status_check.setText(f"校验 ✓{stats['ok']} / ✗{stats['fail']}")
            self.status_check.setStyleSheet(
                f"color:{theme.FAIL_FG}; font-weight:bold;" if stats["fail"]
                else f"color:{theme.OK_FG};"
            )
        else:
            self.status_check.setText("校验 —")
            self.status_check.setStyleSheet(f"color:{theme.NA_FG};")
        self.status_time.setText(f"耗时 {elapsed_ms:.2f} ms")
        self._refresh_frame_counter()
        self.statusBar().showMessage(
            f"解析完成：{total} 帧 / {stats['total_bytes']} 字节 / {elapsed_ms:.2f} ms"
        )

    def _on_frame_selected(self, index: int) -> None:
        self._current_frame = index
        self.field_result.set_current_frame(index)
        if self._data:
            self.bit_editor._current = index          # noqa: SLF001 - 位编辑器跟随选中帧
        self._refresh_visible_lazy_panels()
        self.bytes_view.set_current_frame(index)

    def _jump_input_to_bytes(self, start: int, length: int) -> None:
        """表格里选中的东西 → 左侧原文跳到对应字节并反色选中。"""
        if self._syncing or length <= 0:
            return
        self._syncing = True          # 自己设的光标别再反过来触发"原文→表格"定位
        try:
            ok = self.input_panel.highlight_bytes(start, length)
        finally:
            self._syncing = False
        if ok:
            self.statusBar().showMessage(
                f"已在左侧输入数据中反色选中：偏移 {start} 起 {length} 字节"
                f"（0x{start:X} ~ 0x{start + length - 1:X}）"
            )
        else:
            self.statusBar().showMessage(
                f"偏移 {start} 起 {length} 字节：输入不是按行解析的（或已换数据），无法在原文里定位"
            )

    def _jump_table_to_bytes(self, start: int, length: int) -> None:
        """左侧原文里选中的内容 → 帧列表定位到对应帧。"""
        if self._syncing or length <= 0 or not self._split:
            return
        end = start + length
        target = self.frame_list.row_for_byte(start, end)     # 只看索引，不展开几十万帧
        if target is None:
            return
        self._syncing = True
        try:
            self.frame_list.table.select_row_silently(target)
            self.frame_list.emit_selected()
        finally:
            self._syncing = False
        self.statusBar().showMessage(
            f"左侧选中 → 帧列表已定位到第 {target + 1} 行（帧 #{self.frame_list._visible[target]}）"  # noqa: SLF001
        )

    def _on_column_style_changed(self) -> None:
        self.statusBar().showMessage("列样式已更新（会随配置一起保存）")

    def _on_stream_toggled(self, checked: bool) -> None:
        self.budget_combo.setEnabled(checked)
        self.budget_label.setEnabled(checked)
        self.statusBar().showMessage(
            "流式渲染：开（分片计算，边算边显示）" if checked
            else "流式渲染：关（每次一次性算完）"
        )
        # 立刻用新模式把当前现场重跑一遍，方便对比
        self._dirty = Stage.RENDER
        self.parse_now()

    # ------------------------------------------------------------------ #
    # 调试辅助
    # ------------------------------------------------------------------ #
    def probe_checksum(self) -> None:
        if not self._split or not self._split.frames:
            QMessageBox.information(self, "试算校验算法", "请先解析出至少一帧数据。")
            return
        dialog = ChecksumProbeDialog(self._split.frames, self)
        if dialog.exec() and dialog.choice():
            algo, order = dialog.choice()
            cfg = self.frame_settings.config()
            cfg.checksum_algo = algo
            cfg.checksum_byte_order = order
            cfg.checksum_where = framer.CHECK_TAIL
            self.frame_settings.set_config(cfg)
            self._dirty = Stage.FRAME      # 只重新分帧+算字段，输入解码与表格结构都不用动
            self.parse_now()
            self.statusBar().showMessage(
                f"已应用校验算法：{checksum.ALGO_LABELS.get(algo, algo)}"
                f"（{'大端' if order == 'big' else '小端'}）"
            )

    def infer_frames(self) -> None:
        """统计推断分帧参数，一键应用。"""
        text_source = getattr(self, "_source", None)
        if text_source is not None:
            # 大文件：只用开头一段做统计推断（infer 是 O(数据量) 的）
            head_bytes = text_source.decode_bytes(0, min(text_source.total_bytes, 2 * 1024 * 1024))
            if len(head_bytes) < 8:
                QMessageBox.information(self, "自动推断", "数据太少（不足 8 字节），无法推断。")
                return
            dialog = InferDialog(head_bytes, self)
            if not dialog.exec():
                return
            candidate = dialog.chosen()
            if candidate is None:
                return
            self.frame_settings.set_config(candidate.config)
            self._dirty = Stage.FRAME
            self.parse_now()
            self.statusBar().showMessage(
                f"已应用推断结果：{infer.describe_config(candidate.config)}"
                "（大文件按开头 2 MB 推断）"
            )
            return
        if not self._data:
            try:
                self._data, _warnings = self.input_panel.to_bytes_ex()
            except codec.CodecError as exc:
                QMessageBox.information(self, "自动推断", f"先要有可解析的输入数据：{exc}")
                return
        if len(self._data) < 8:
            QMessageBox.information(self, "自动推断", "数据太少（不足 8 字节），无法推断。")
            return
        dialog = InferDialog(self._data, self)
        if not dialog.exec():
            return
        candidate = dialog.chosen()
        if candidate is None:
            return
        self.frame_settings.set_config(candidate.config)
        self._dirty = Stage.FRAME          # 只重新分帧+算字段
        self.parse_now()
        self.statusBar().showMessage(
            f"已应用推断结果：{candidate.summary()}（置信度 {candidate.percent}%）"
        )

    def _apply_bit_result(self, data: bytes) -> None:
        """位编辑器改完的字节作为新的输入数据。"""
        if not data:
            return
        self._loading = True
        try:
            self.input_panel.set_radix(codec.RADIX_HEX)
            self.input_panel.set_raw_text(codec.bytes_to_text(data, codec.RADIX_HEX))
        finally:
            self._loading = False
        self._dirty = Stage.DECODE
        self.parse_now()
        self.statusBar().showMessage(
            f"已把位编辑器结果（{len(data)} 字节）设为输入数据并重新解析"
        )

    def _toggle_follow(self, checked: bool) -> None:
        if checked:
            if not self._follow_path:
                path, _ = QFileDialog.getOpenFileName(self, "选择要跟随的数据文件", self.last_dir or "")
                if not path:
                    self.act_follow.setChecked(False)
                    return
                self._follow_path = path
            self._follow_stat = None
            self.follow_timer.start()
            self.statusBar().showMessage(f"正在跟随文件：{self._follow_path}")
        else:
            self.follow_timer.stop()
            self.statusBar().showMessage("已停止跟随文件")

    def _follow_tick(self) -> None:
        if not self._follow_path:
            return
        path = Path(self._follow_path)
        try:
            stat = path.stat()
        except OSError:
            return
        marker = (stat.st_mtime, stat.st_size)
        if marker == self._follow_stat:
            return
        self._follow_stat = marker
        try:
            with path.open("rb") as handle:
                handle.seek(max(0, stat.st_size - FOLLOW_LIMIT))
                blob = handle.read()
        except OSError as exc:
            self.statusBar().showMessage(f"读取失败：{exc}")
            return
        self._loading = True
        try:
            self.input_panel.set_radix(codec.RADIX_HEX)
            self.input_panel.set_raw_text(codec.bytes_to_text(blob, codec.RADIX_HEX, sep="\n"))
        finally:
            self._loading = False
        self._dirty = Stage.DECODE
        self.parse_now()
        self.statusBar().showMessage(
            f"跟随刷新：{path.name} 共 {stat.st_size} 字节（显示尾部 {len(blob)} 字节）"
        )

    def _toggle_tooltips(self, checked: bool) -> None:
        SmartTable.tooltips_enabled = checked
        for table in self._tables():
            for col in range(table.columnCount()):
                table._paint_column(col)  # noqa: SLF001 - 内部重绘
        self.statusBar().showMessage("已" + ("开启" if checked else "关闭") + "单元格悬停提示")

    def _reset_columns(self) -> None:
        for table in self._tables():
            table.reset_column_style()
        self.statusBar().showMessage("已恢复全部列默认样式")

    # ------------------------------------------------------------------ #
    # 文件 / 配置
    # ------------------------------------------------------------------ #
    def _pick_open(self, title: str, filters: str) -> str:
        path, _ = QFileDialog.getOpenFileName(self, title, self.last_dir or "", filters)
        if path:
            self.last_dir = str(Path(path).parent)
            self.settings.setValue("last_dir", self.last_dir)
        return path

    def _pick_save(self, title: str, default_name: str, filters: str) -> str:
        path, _ = QFileDialog.getSaveFileName(
            self, title, str(Path(self.last_dir or "") / default_name), filters
        )
        if path:
            self.last_dir = str(Path(path).parent)
            self.settings.setValue("last_dir", self.last_dir)
        return path

    def _preset_for_file(self, path: str):
        """找一个"就是照着这个文件做的"预设（按文件名匹配 sample_file）。"""
        name = Path(path).name
        for preset in presets.PRESETS.values():
            if preset.sample_file and Path(preset.sample_file).name == name:
                return preset
        return None

    def load_field_definitions(self, fields: list) -> None:
        """从「数据表分析」的字段模板菜单载入一套字段定义。"""
        if not fields:
            return
        self.field_table.set_fields(fields)
        self._mark_dirty(Stage.FIELDS)
        self.parse_now(blocking=not self.act_stream.isChecked())
        self.statusBar().showMessage(
            f"已载入 {len(fields)} 个字段定义 —— 数据表分析会按字段生成列"
        )

    def _mismatch_hints(self, data_size: int, split: framer.SplitResult) -> list[str]:
        """分帧方式与数据明显不匹配时给出提醒（避免拿着 A 协议去套 B 数据）。"""
        hints: list[str] = []
        if not data_size:
            return hints
        if not split.frames:
            hints.append("没有分出任何帧：帧头/分隔符/定长长度等分帧参数可能不对")
        elif len(split.frames) == 1 and split.total_bytes >= max(64, data_size // 2):
            hints.append(
                f"整段 {data_size} 字节只分出一帧（{split.total_bytes} 字节）："
                "多半是拿 A 协议的分帧设置去套 B 数据了"
            )
        if split.frames and split.fail_count and not split.ok_count:
            hints.append("所有帧校验都失败：校验算法、字节序或校验位置可能不对")
        if split.frames and len(split.frames) == 1 and split.frames[0].check_state == "na":
            hints.append("该帧没有校验结果，无法用校验判断解析是否正确")
        return hints

    def _soft_hints(self, split: framer.SplitResult) -> list[str]:
        """软提示：只是提醒，不打断当前操作（不切页签）。"""
        hints: list[str] = []
        field_count = len(self.field_table.fields())
        if len(split.frames) > 3 and field_count <= 1:
            hints.append(
                f"字段定义表只有 {field_count} 个字段 ——「数据表分析」这张一帧一行的表，列就是字段；"
                "可用「文件 → 预设」或分析页的「字段模板 ▾」一键补齐"
            )
        return hints

    def import_data_file(self) -> None:
        path = self._pick_open(
            "导入数据文件（二进制按 HEX 读取，文本按 ASCII 读取）",
            "数据/日志文件 (*.bin *.dat *.log *.txt *.jsonl *.hex);;所有文件 (*)",
        )
        if not path:
            return
        self.load_data_file(path)

    # ---------------------------- 样本数据 ---------------------------- #
    def _refresh_sample_menu(self) -> None:
        self.menu_samples.clear()
        sample_dir = Path(self.sample_dir) if getattr(self, "sample_dir", "") else presets.DATA_DIR
        files: list[Path] = []
        if sample_dir.is_dir():
            for pattern in ("*.bin", "*.dat", "*.hex", "*.jsonl", "*.csv", "*.txt", "*.log"):
                files.extend(sorted(sample_dir.glob(pattern)))
        seen: set[str] = set()
        unique: list[Path] = []
        for path in files:
            if path.name not in seen:
                seen.add(path.name)
                unique.append(path)
        for path in unique[:40]:
            try:
                size = f"{path.stat().st_size / 1024:.0f} KB"
            except OSError:
                size = "?"
            action = QAction(f"{path.name}   ({size})", self)
            action.triggered.connect(lambda _=False, p=str(path): self.load_data_file(p))
            self.menu_samples.addAction(action)
        if not unique:
            empty = self.menu_samples.addAction(f"（样本目录为空：{sample_dir}）")
            empty.setEnabled(False)
        self.menu_samples.addSeparator()
        self.menu_samples.addAction(QAction("刷新列表", self, triggered=self._refresh_sample_menu))
        self.menu_samples.addAction(QAction("选择样本目录…", self, triggered=self._choose_sample_dir))
        current = QAction(f"当前样本目录：{sample_dir}", self)
        current.setEnabled(False)
        self.menu_samples.addAction(current)

    def _choose_sample_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择样本数据目录", self.sample_dir or "")
        if directory:
            self.sample_dir = directory
            self.settings.setValue("sample_dir", directory)
            self._refresh_sample_menu()
            self.statusBar().showMessage(f"样本目录已切换为：{directory}")

    # ---------------------------- 日志提取 ---------------------------- #
    def extract_from_log(self, path: str = "") -> None:
        dialog = ExtractDialog(self.input_panel.raw_text(), path or self._follow_path, self)
        if not dialog.exec():
            return
        text = dialog.result_text()
        if not text:
            return
        self._loading = True
        try:
            self.input_panel.set_radix(codec.RADIX_HEX)
            self.input_panel.set_raw_text(text)
        finally:
            self._loading = False
        self._dirty = Stage.DECODE
        self.parse_now()
        self.statusBar().showMessage(
            f"已提取 {dialog.frame_count()} 帧并解析 —— {dialog.result_summary()}"
        )
        self._follow_path = dialog.path_edit.text().strip() or self._follow_path

    def load_data_file(self, path: str) -> None:
        self._follow_path = path
        suffix = Path(path).suffix.lower()

        # 如果某个预设就是照着这个文件做的，先把它的分帧设置和字段表套上，
        # 免得出现"数据是 EB 90、字段还是 AA55 预设"的错配
        preset = self._preset_for_file(path)
        if preset is not None:
            self.frame_settings.set_config(framer.FrameConfig.from_dict(preset.frame))
            self.field_table.set_fields(preset.fields)
            self.statusBar().showMessage(
                f"已自动套用与 {Path(path).name} 匹配的预设：{preset.name}"
            )

        # JSONL / CSV：先用规则把数据段抠出来，避免把几十万行原始日志塞进输入框
        if suffix in (".jsonl", ".csv"):
            try:
                text, _truncated = extract.read_text_head(path, 8 * 1024 * 1024)
                auto = extract.auto_extract(text, limit=20000)
            except OSError as exc:
                QMessageBox.critical(self, "读取失败", str(exc))
                return
            if auto.ok:
                self._loading = True
                try:
                    self.input_panel.set_radix(codec.RADIX_HEX)
                    self.input_panel.set_raw_text("\n".join(auto.lines))
                finally:
                    self._loading = False
                self._dirty = Stage.DECODE
                self.parse_now()
                self.statusBar().showMessage(
                    f"已从 {Path(path).name} 自动提取 {len(auto.lines)} 帧"
                    f"（{auto.pattern_name}）；需要按 ID 过滤请用「从日志提取…」"
                )
                return
            if auto.error:
                self.statusBar().showMessage(
                    f"自动提取失败（{auto.error}），已按文本导入；可用「从日志提取…」手动指定规则"
                )

        try:
            size = Path(path).stat().st_size
        except OSError as exc:
            QMessageBox.critical(self, "导入失败", str(exc))
            return

        # 大文本文件：走"mmap + 分页 + 流式解码分帧"，不把上百 MB 全塞进内存
        if suffix not in (".bin", ".dat", ".hex") and size > source.BIG_FILE_BYTES:
            self._release_source()
            text_source = source.TextSource(path=path)
            self._source = text_source
            self._loading = True
            try:
                self.input_panel.set_radix(codec.RADIX_AUTO)
                self.input_panel.set_source(text_source)
            finally:
                self._loading = False
            self._dirty = Stage.DECODE
            self.parse_now()
            self.statusBar().showMessage(
                f"已导入 {Path(path).name}（{size / 1048576:.1f} MB，"
                f"{text_source.line_count:,} 行）—— 分页显示，按需读取"
            )
            return

        self._source = None
        self.input_panel.clear_source()
        try:
            blob = Path(path).read_bytes()
        except OSError as exc:
            QMessageBox.critical(self, "导入失败", str(exc))
            return
        if suffix in (".bin", ".dat", ".hex"):
            text = codec.bytes_to_text(blob[:FOLLOW_LIMIT * 4], codec.RADIX_HEX, sep="\n")
            radix = codec.RADIX_HEX
        else:
            try:
                text = blob.decode("utf-8")
                radix = codec.RADIX_AUTO
            except UnicodeDecodeError:
                text = codec.bytes_to_text(blob[:FOLLOW_LIMIT * 4], codec.RADIX_HEX, sep="\n")
                radix = codec.RADIX_HEX
        self._loading = True
        try:
            self.input_panel.set_radix(radix)
            self.input_panel.set_raw_text(text)
        finally:
            self._loading = False
        self._dirty = Stage.DECODE
        self.parse_now()
        self.statusBar().showMessage(f"已导入 {path}（{len(blob)} 字节）")

    def export_result(self) -> None:
        if not self._split:
            QMessageBox.information(self, "导出", "还没有解析结果。")
            return
        path = self._pick_save("导出解析结果", "frame_result" + config_io.RESULT_SUFFIX, "JSON 文件 (*.json)")
        if not path:
            return
        try:
            document = config_io.build_result_document(
                self._input_info(self._data, self.input_panel.effective_radix()),
                self.frame_settings.config(), self._split, self._parsed_by_frame,
                self._column_states(),
            )
            config_io.dump_result(path, document)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        self.statusBar().showMessage(f"已导出 {path}")

    def export_table_csv(self, table: SmartTable, name: str) -> None:
        if table.rowCount() == 0:
            QMessageBox.information(self, "导出", f"{name}还没有内容。")
            return
        path = self._pick_save(f"导出{name}", f"{name}.csv", "CSV 文件 (*.csv);;文本文件 (*.txt)")
        if not path:
            return
        header = [spec.title for spec in table.specs()]
        # 虚拟行（滑动窗口）下没显示过的行也要能导出：按行现算（逻辑总行数，不是已铺行数）
        rows = [table.row_texts(row) for row in range(table.row_count)]
        delimiter = "\t" if path.lower().endswith(".txt") else ","
        try:
            Path(path).write_text(exporters.export_rows(header, rows, delimiter), encoding="utf-8-sig")
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        self.statusBar().showMessage(f"已导出 {path}")

    def export_config(self) -> None:
        path = self._pick_save("导出配置", "frame" + config_io.CONFIG_SUFFIX, "JSON 文件 (*.json)")
        if not path:
            return
        payload = config_io.build_config_payload(
            self.frame_settings.config(),
            [fd.to_dict() for fd in self.field_table.fields()],
            self.input_panel.input_settings(),
            self._column_states(),
            self.input_panel.raw_text(),
        )
        try:
            config_io.save_config(path, payload)
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        self.statusBar().showMessage(f"配置已导出：{path}")

    def import_config(self) -> None:
        path = self._pick_open("导入配置", "JSON 文件 (*.json);;所有文件 (*)")
        if not path:
            return
        try:
            payload = config_io.load_config(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "导入失败", str(exc))
            return
        self._loading = True
        try:
            if payload.get("frame_config"):
                self.frame_settings.set_config(framer.FrameConfig.from_dict(payload["frame_config"]))
            if payload.get("fields") is not None:
                self.field_table.set_fields(payload["fields"])
            if payload.get("input"):
                self.input_panel.apply_settings(payload["input"])
            if payload.get("input_text"):
                self.input_panel.set_raw_text(payload["input_text"])
        finally:
            self._loading = False
        if payload.get("columns"):
            self._apply_column_states(payload["columns"])
        self._dirty = Stage.DECODE
        self.parse_now()
        self.statusBar().showMessage(f"配置已导入：{path}")

    def _release_source(self) -> None:
        """丢掉当前的大文件来源（并关掉文件句柄，Windows 上不关就删不掉文件）。"""
        old = getattr(self, "_source", None)
        self._source = None
        self.input_panel.clear_source()
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001
                pass

    def load_preset(self, key: str) -> None:
        preset = presets.PRESETS.get(key)
        if preset is None:
            return
        sample, note = preset.resolve_sample()
        self._loading = True
        try:
            self._release_source()                   # 预设走内存路径（样本是先取出来的）
            self.input_panel.apply_settings({
                "radix": preset.radix,
                "dec_width": preset.dec_width,
                "byte_order": preset.input_byte_order,
            })
            self.input_panel.set_raw_text(sample)
            self.frame_settings.set_config(framer.FrameConfig.from_dict(preset.frame))
            self.field_table.set_fields(preset.fields)
        finally:
            self._loading = False
        self._dirty = Stage.DECODE
        self.parse_now()
        message = f"已载入预设：{preset.name}"
        if note:
            message += f"（{note}）"
        elif not sample:
            message += "；没有示例数据，请用「文件 → 样本数据」打开真实样本"
        self.statusBar().showMessage(message)
        self.statusBar().setToolTip(preset.description)

    def clear_all(self) -> None:
        self.pipeline.cancel()
        self.pipeline.state = ParseState()
        self._loading = True
        try:
            self.input_panel.set_raw_text("")
        finally:
            self._loading = False
        self._dirty = None
        self.input_panel.set_spans([])
        self._data = b""
        self._release_source()
        self.input_panel.clear_source()
        self._split = None
        self._parsed_by_frame = {}
        self.frame_list.set_frames([], {})
        self.field_result.set_rows([])
        self.analysis.set_source([], {}, [])
        self.bytes_view.set_source(b"", [], -1)
        self.bit_editor.set_source(b"", [], -1)
        self.log_panel.set_text("")
        self.json_panel.set_text("")
        self._json_dirty = True
        self._bytes_dirty = True
        self._bits_dirty = True
        self._analysis_dirty = True
        self._update_status(0, framer.SplitResult(), 0.0)
        self.statusBar().showMessage("已清空")

    # ------------------------------------------------------------------ #
    # 列样式持久化
    # ------------------------------------------------------------------ #
    def _column_states(self) -> dict:
        states = {table.table_id: table.column_state() for table in self._tables()}
        states["analysis_rules"] = {"rules": self.analysis.rules_payload()}
        states["analysis_row_rules"] = self.analysis.row_rules_payload()
        return states

    def _apply_column_states(self, states: dict) -> None:
        for table in self._tables():
            if table.table_id in (states or {}):
                table.apply_column_state(states[table.table_id])
        payload = (states or {}).get("analysis_rules") or {}
        if payload.get("rules"):
            self.analysis.apply_rules_payload(payload["rules"])
        row_payload = (states or {}).get("analysis_row_rules") or {}
        if row_payload.get("rules"):
            self.analysis.apply_row_rules_payload(row_payload)

    def _restore_state(self) -> None:
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        splitter_state = self.settings.value("splitter")
        if splitter_state:
            self.main_splitter.restoreState(splitter_state)
        columns = self.settings.value("columns", "", type=str)
        if columns:
            try:
                self._apply_column_states(json.loads(columns))
            except Exception:  # noqa: BLE001
                pass
        last = self.settings.value("last_session", "", type=str)
        if last:
            try:
                payload = json.loads(last)
                self._loading = True
                try:
                    if payload.get("frame_config"):
                        self.frame_settings.set_config(
                            framer.FrameConfig.from_dict(payload["frame_config"])
                        )
                    if payload.get("fields") is not None:
                        self.field_table.set_fields(payload["fields"])
                    if payload.get("input"):
                        self.input_panel.apply_settings(payload["input"])
                    if payload.get("input_text"):
                        self.input_panel.set_raw_text(payload["input_text"])
                finally:
                    self._loading = False
            except Exception:  # noqa: BLE001
                pass

    def closeEvent(self, event) -> None:  # noqa: N802
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("splitter", self.main_splitter.saveState())
        try:
            self.settings.setValue("columns", json.dumps(self._column_states(), ensure_ascii=False))
            self.settings.setValue("last_session", json.dumps(config_io.build_config_payload(
                self.frame_settings.config(),
                [fd.to_dict() for fd in self.field_table.fields()],
                self.input_panel.input_settings(),
                {},
                self.input_panel.raw_text(),
            ), ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(event)

    # ------------------------------------------------------------------ #
    # 帮助
    # ------------------------------------------------------------------ #
    def show_help(self) -> None:
        QMessageBox.information(self, "使用说明", HELP_TEXT)

    def show_about(self) -> None:
        QMessageBox.information(
            self, "关于",
            f"<b>数据帧解析器 frame_parser v{__version__}</b><br><br>"
            "多进制（HEX/DEC/BIN/ASCII）× 多帧格式（帧头+长度+校验 / 定长 / 长度前缀 / 分隔符 / TLV / 整包）"
            "的通用数据帧解析工具。<br>"
            "支持列级颜色、列级显示进制、列级换算函数、悬停多进制提示、校验算法试算。<br><br>"
            "PyQt6 实现，可作为二次开发的基础。",
        )


HELP_TEXT = """【输入】
· 进制可选自动识别 / HEX / DEC / BIN / ASCII；HEX 支持 AA5501、AA 55 01、0xAA,0x55、1Ah 混写。
· 多行输入按「逐行投票」识别进制，所以日志里夹着注释行也不会认错；坏行会被跳过并列出警告。
· ASCII 支持 \\r \\n \\t \\0 \\xNN 转义。十进制可指定每值占 1/2/4 字节与字节序。

【样本数据】
· 「文件 → 样本数据」直接打开 data/ 目录里的样本：
  aa55_crc16.bin / fixed_xor8.bin / modbus_rtu.bin / tlv.bin / position_float.bin /
  raw_packet.bin / ascii_line.txt / can_sample.jsonl / can_sample.csv / backward_sample.txt。
· backward_sample.txt 是现场采集样本（12663 帧 × 156 字节），预设 ⑧ 就是按它做的：
  帧头 EB 90、字节2 = 总长-5、字节5 = 报文类型 0x92、字节7 = 帧计数(每帧+2)、帧尾 5B BF。
· 「选择样本目录…」可以把样本目录指到自己的日志目录。

【从日志提取数据帧】 Ctrl+E
· 直接把现场抓包日志变成"一行一帧"的可解析数据：
  · JSONL：{"can_id":"0x1E7","dlc":8,"data":"ea07071900000000"} → 规则选「JSONL/JSON 的 data 字段」；
  · CAN 分析仪 CSV：数据列形如 x| 00 00 02 00 64 1C 00 00 → 规则选「CAN 分析仪 CSV 数据列」；
  · 其它日志可用引号内 HEX / 0x 序列 / 连续 HEX 串，或自己写正则 + 捕获组。
· 「行过滤」填 0x1E7 就只提取这一路 CAN ID 的报文；点「统计 ID」会列出日志里出现过的 can_id。
· 导入 .jsonl / .csv 时会自动挑规则提取（避免把几十万行原始日志塞进输入框）；
  文件读取上限默认 8 MB，可调。

【分帧】
· 帧头+长度字段(+校验)：设帧头、长度字段偏移/字节数/字节序，以及长度值是否包含帧头、是否包含校验。
· 定长帧 / 长度前缀 / 分隔符（如 0D 0A）/ TLV（Tag+Len+Value）/ 整包不拆分。
· 校验支持 SUM8、取反 SUM8、SUM16、XOR8、CRC8、CRC16/MODBUS、CRC16/CCITT-FALSE、CRC16/XMODEM、CRC32，
  位置可选「帧尾」或「紧跟长度字段之后」。不知道用哪种时点「试算校验算法」。

【字段】
· 类型：无符号/有符号整数、浮点、Bool、BCD、ASCII、原始字节、表达式计算。
· 长度填 0 表示「取到帧尾」，用于变长字段。
· 系数/偏置做线性换算：工程值 = 原始值 × 系数 + 偏置；位域如 0-2、3-5。
· 表达式字段可引用前面字段名，例如：航向 * 16.6667。

【表格（帧列表 / 字段解析 / 字节对照表 / 字段定义表通用）】
· 左键点列标题 = 整列选择；Ctrl+左键 = 多列累加选择；Ctrl+C 复制选区/整列。
· 右键列标题 = 整列选择 / 复制整列 / 设置列颜色（含快速配色）/ 本列显示格式（HEX/DEC/BIN/ASCII）/ 换算函数。
· 右键单元格 = 复制单元格 / 复制悬停信息 / 查看换算本列。
· 列的换算函数用 v 表示原始值，例如 v*0.1、v/1000、v-40、(v>>4)&0xF；设置后列标题会显示 ⚙ 标记。
· 鼠标悬停任意数值单元格：显示原始值、换算函数、换算结果，以及 HEX/DEC/BIN/ASCII/U32/I64 等每种进制的值。

【数据表分析】—— 一帧一行 × 一个字段一列，按列分析
· 左：宽表，帧# / 偏移 / 长度 / 校验 + 每个字段一列（列标题带单位）。
· 右：每列统计 —— 有效（含空值数）/ 范围 / 均值 / 中位数 / σ / 不同值 / 变化次数 / 首→末 / 首末差 / 趋势 / 最大跳变。
· 右下：点列标题（或统计表里的一行）→ 该列随帧号变化的折线图 + 突变清单（哪一帧从多少跳到多少）。
· 突变阈值默认 4σ 可调，判为突变的格子灰底标出；可勾「只看异常帧」直接过滤出问题帧。
· 「字段列 ▾」选择这张表显示哪些字段；「导出宽表 CSV」把一帧一行导成 Excel 可直读的表。
· 列格式/列颜色/列换算函数与其它表一致（例如给车速列加 v*0.1 立刻变工程值）。
· 典型流程：看统计找异常列 → 点它看曲线 → 用突变清单定位帧 → 回「字段解析/位编辑器」细看字节。

【进制视图】
· 整段数据或选中帧整体转 HEX / DEC / BIN / ASCII，可调每行字节数、分隔符、大小写、0x 前缀、偏移列、ASCII 附列。
· 下方「字节对照表」逐字节并排显示 偏移 / HEX / DEC / BIN / 字符 / b7~b0 位展开；每个位是独立一列，可整列选色。
· 「复制为」支持 HEX 文本、C 数组、Python bytes、Python 列表、十进制数组、二进制、hexdump、Base64。

【位编辑器】
· 一帧一表，偏移 / HEX / DEC / b7…b0，点某个位的复选框就翻转它。
· 改完自动按当前分帧设置重算校验，校验状态实时显示；被改动的字节整行灰底标记。
· 列标题右键 →「快捷操作」：b7 整列置 1 / 清 0 / 取反、本列全部取反、恢复原始值。
· 结果可复制 HEX、保存 .bin、或「设为输入数据」立刻重新解析看字段变化。

【自动推断分帧参数】Ctrl+I
· 自相关找帧长 → 定位帧头相位（前导字节自动跳过）→ 还原长度字段位置与语义 →
  试所有校验算法 → 列出跨帧恒定/变化的字节位置（含"疑似长度字段"提示）。
· 输出带置信度的候选列表，双击应用。命令行用 --infer 打印同样内容。

【命令行模式】cli.bat / python cli.py --help
· cli.bat --infer --file data\backward_sample.txt
· cli.bat --preset aa55_crc16 --file data\aa55_crc16.bin --format csv --out frames.csv
· cli.bat --auto-extract --file log.jsonl --extract-filter 0x1E7 --mode fixed --fixed-len 8 --out out.csv
· 行规则：--frame-range 500-1500（也可 1000- 到末尾、-500- 最后 500 帧）、--every 100、
  --only-fail / --only-ok；筛完之后的表格 / JSON / CSV / 二进制导出都只含这些帧。
· 退出码 0=成功 1=出错 3=解析成功但有校验失败；输出格式 table/json/csv/hex/hexdump/bin/summary。

【位置联动】表格 ↔ 左侧原文 双向定位
· 在任意表格里点格子 / 整列 / 整帧 → 左侧「输入数据」自动滚动到对应字节并**反色选中**（黑底白字）。
  例如点分析表的「帧尾」字段 → 左侧选中那两字节；点字节列「偏移 103」 → 精确选中那两位十六进制。
· 在左侧输入框里选中一段 → 「帧列表」自动跳到对应帧。
· 原理：解析时记录了"每行文本 ↔ 每行字节"的区间，所以能精确换算回原文位置。

【一共解析出多少帧 —— 四处都能看到】
· 工具栏最左边：**共 12663 帧　× 156 字节/帧**（黑框白底、加粗，切哪个页签都在；
  帧总数在分帧一做完就显示，流式解析过程中还在长时会补一句「已出 N」；
  悬停可看：总字节数、帧长是恒定还是几种、校验 ✓/✗/— 各多少）。
· 状态栏右侧：`帧 12663`（还有 输入字节、校验、耗时）。
· 「帧列表」信息栏：`显示 3000 / 共 12663 帧`（左边是显示上限，右边才是总数）。
· 「数据表分析」信息栏：`共 12663 帧 │ 行规则命中 201 帧（1.59%） │ 统计基于筛选后的 201 帧 │ …`。
· 日志页也会给 `帧数 : 12663 帧，共 1968156 字节`。

【数据表分析】—— 一帧一行 × 一个字段（或自定义列）一列
· 列模式：字段列（按字段定义）/ 自定义列（用「列规则」自由配）。字段表为空时自动用自定义列。
· 「列规则…」弹小窗口，可叠加多条规则：起始偏移 + 列数 + 步长（+ 每列 2/4/8 字节 + 字节序），
  或直接写偏移列表（0,1,2,8-15）。带实时预览与「常用规则 ▾」一键套用，规则随配置一起保存。
  例：规则1 铺前 8 个字节（HEX）；规则2 从 65 起 4 列每列 2 字节小端 → 直接看四通道数值列。
· 字节列的恒定值直接写在列标题上（如 偏移 5 = 146），变化列标（N 种取值）。
· 「行规则…」弹小窗口，决定**看哪些帧（哪几行）**，同样可叠加多条规则、可「且 / 或」：
  帧号区间（起始 0 / 结束 -1 = 全部，-1 就是最后一帧，也支持 -500 这种倒数写法）、
  采样（每 N 帧取 1）、帧内字节比较（第 2 字节 > 150、第 0 字节 ≠ 0xEB）、
  校验状态（只看 ✗ 失败）、帧长比较、帧内包含内容（HEX 或 ascii:xxx）。
  筛选结果同时作用于表格、统计、曲线和突变检测，信息栏与按钮旁会显示「命中 N/M 帧」。
· 每列统计（有效/范围/均值/中位数/σ/不同值/变化次数/首末/趋势/最大跳变）**按行规则筛出来的全部帧算**，
  表格显示行数另有限制（最多 N 行 —— 从筛出来的帧里取前 N 行）。
· 点列标题 → 该列折线图 + 突变清单；突变格子灰底标出，可「只看异常帧」。
· 「字段模板 ▾」一键套用预设/模板的字段定义；「导出宽表 CSV」导出一帧一行给 Excel。

【大文件与性能】
· **表格没有"最多多少帧"这回事**：帧列表行数 = 帧数，分析表行数 = 行规则命中的帧数，
  字段表行数 = 帧数 × 字段数；表格是**滑动窗口**（只填视口附近的行，滚到哪算哪），
  行数初始铺 2 万行、滚到底自动续，所以"无上限"也不会把内存吃满。
  导出 / 复制 / 定位都按需取行，不受"滚过没滚过"影响。
· 字节对照表最多显示 2048 字节（那是字节数），列规则最多 256 列，分析页统计默认取前 5 万帧
  （用「行规则…」可以卡任意区间）。
· **帧数不设上限**：「分帧设置 → 最多分」默认 0 = 不限，128 MB 抓包（28.7 万帧）一次分完；
  想快速预览超大文件时可设上限，达到上限会在日志里提示。
· **超过 4 MB 的文本文件自动进入「大文件模式」**：文件用 mmap 打开、只建行索引，
  边读边解码边分帧，帧只存索引、字节按需取；左侧输入框只显示当前一页
  （◀ 上一页 / 下一页 / 第 N 页 / 跳到行），点表格里的值会自动翻到对应页并反色选中。
· 实测 128 MB / 28.7 万帧：导入+解析 0.8 秒；点表格里的格子 10 ms；定位（含翻页）10 ms；
  帧列表滚到底（28.7 万行铺满）约 1 秒；私有内存 85 MB → 全滚开 118 MB。
· 后端不做限制：筛选、进制视图、导出 JSON 都不受显示上限影响。

【流式渲染（工具栏可开关，「切片」调每片毫秒数）】
· 按脏标记分级重算：改列格式/颜色只重绘那一列；改字段定义不重新分帧；改分帧设置才重新分帧；
  只有输入数据/进制变了才重新解码。
· 每个时间片算一小块（默认 12 ms）就让出界面，解析完一块立刻把行追加到表格 —— 结果是"长出来"的。
· 片长自适应：一片超时就缩小、很快就算完就放大，自动收敛到切片预算。
· 改了参数会直接丢弃上一个没跑完的任务，不会重复渲染。
· 进制视图 / 位编辑器 / JSON 是惰性的：切到那个页签才生成，其余时候不白算。
· 悬停提示改成悬停时才计算，省掉"给每一格预生成 HTML"的开销。
实测（1.9 MB / 12663 帧 / 8 字段）：同步模式首屏 1510 ms、一次停顿 1510 ms；
流式 12 ms 首屏 184 ms、最长停顿 76 ms。

【选区信息】
· 在输入框里选中一段：下方立刻显示 选中多少字符 = 多少字节，以及 HEX / DEC / BIN / ASCII 各是多少。
· 在表格里选中若干格或整列：状态栏显示选中格数/行数，字节对照表与位编辑器会报"选中多少字节"
  和对应各进制取值（单字节还会列出 b7~b0 位组成）。
· 输入区还有「复制选区 HEX」「复制选区 BIN」按钮（未选中则复制全部）。


【别踩的坑】
· 分帧设置（帧头/帧长/校验）和字段定义表必须与当前数据是同一套协议。
  打开 `data/` 里的样本时会自动套用同名预设；换了数据源记得换预设或按 Ctrl+I 推断。
· 如果出现"整段只分出 1 帧""所有帧校验都失败"，看「日志」页的 ⚠ 提示 —— 基本就是协议错配。
· 「原始 HEX / 摘要 / 备注」这类文本列永远按原文显示；只有数值列才会跟随「本列显示格式」。

【其它调试】
· 自动解析：输入即解析（可关）；F5 立即解析。
· 跟随文件：选择日志/抓包文件后实时刷新（只读文件尾部 512 KB），适合边跑边看。
· 试算校验算法：把当前帧喂给所有校验算法，列出匹配帧数，双击即可应用。
· 帧筛选：全部 / 仅校验失败 / 仅校验通过 / 仅不完整帧 + 关键字过滤。
· 日志页签给出字节数、帧数、校验统计、耗时、输入/分帧/字段异常清单。
· 导出：解析结果 JSON、字段表 CSV、帧列表 CSV、配置 JSON（含列颜色/格式/换算函数与输入数据）。
· 退出时自动记住整个现场，下次打开自动恢复。
"""
