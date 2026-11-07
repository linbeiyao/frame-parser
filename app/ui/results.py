"""结果面板：帧列表 / 字段解析 / 进制视图 / 日志 / JSON。"""
from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QVBoxLayout, QWidget,
)

from .. import codec, exporters, field_parser
from . import theme
from .stream_render import RowStreamer
from .table_base import ColumnSpec, SmartTable
from .util import copy_to_clipboard, mono_font

#: 字段表预先解析多少帧（剩下的滚到哪解析哪；表格本身不设帧数上限）
PREPARSE_FRAMES = 200

#: 黑白主题下用「灰度 + 符号」区分状态，不用红绿
CHECK_COLORS = {"ok": theme.OK_FG, "fail": theme.FAIL_FG, "na": theme.NA_FG}
CHECK_LABELS = {"ok": "✓ 通过", "fail": "✗ 失败", "na": "—"}

FRAME_COLUMNS = [
    ColumnSpec("index", "帧#", width=46, align="center"),
    ColumnSpec("offset", "偏移", width=68, numeric=True, radix="dec", align="center"),
    ColumnSpec("length", "长度", width=58, numeric=True, radix="dec", align="center"),
    ColumnSpec("tag", "Tag/帧头", width=82, align="center"),
    ColumnSpec("check", "校验", width=56, align="center"),
    ColumnSpec("summary", "摘要（前几个字段）", width=300),
    ColumnSpec("hex", "原始 HEX", width=520, monospace=True, simple_tooltip=True),
]

FIELD_COLUMNS = [
    ColumnSpec("frame", "帧#", width=44, numeric=True, radix="dec", align="center"),
    ColumnSpec("name", "字段", width=120),
    ColumnSpec("type", "类型", width=86),
    ColumnSpec("offset", "偏移", width=56, numeric=True, radix="dec", align="center"),
    ColumnSpec("length", "长度", width=56, numeric=True, radix="dec", align="center"),
    ColumnSpec("raw_hex", "原始 HEX", width=150, monospace=True, simple_tooltip=True),
    ColumnSpec("raw_value", "原始值", width=90, numeric=True, radix="dec", align="right"),
    ColumnSpec("value", "解析值 / 工程值", width=150, numeric=True, radix="auto", align="right"),
    ColumnSpec("unit", "单位", width=56, align="center"),
    ColumnSpec("note", "备注 / 换算说明", width=230),
]

BYTE_COLUMNS = (
    [
        ColumnSpec("offset", "偏移", width=64, numeric=True, radix="dec", align="center"),
        ColumnSpec("hex", "HEX", width=52, numeric=True, radix="hex", align="center", monospace=True),
        ColumnSpec("dec", "DEC", width=52, numeric=True, radix="dec", align="center", monospace=True),
        ColumnSpec("bin", "BIN", width=92, numeric=True, radix="bin", align="center", monospace=True),
        ColumnSpec("ascii", "字符", width=48, numeric=True, radix="ascii", align="center", monospace=True),
    ]
    + [
        ColumnSpec(f"b{bit}", f"b{bit}", width=34, numeric=True, radix="bin",
                   align="center", monospace=True, simple_tooltip=True)
        for bit in range(7, -1, -1)
    ]
)

RADIX_ITEMS = [
    (codec.RADIX_HEX, "HEX 十六进制"),
    (codec.RADIX_DEC, "DEC 十进制"),
    (codec.RADIX_BIN, "BIN 二进制"),
    (codec.RADIX_ASCII, "ASCII 文本"),
]

SOURCE_ITEMS = [("all", "全部输入数据"), ("frame", "当前选中帧")]


def _mono_edit(placeholder: str = "") -> QPlainTextEdit:
    edit = QPlainTextEdit()
    edit.setReadOnly(True)
    edit.setFont(mono_font(10))
    edit.setPlaceholderText(placeholder)
    edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    return edit


def byte_summary_text(data: bytes, offset: int = 0, limit: int = 72) -> str:
    """一段字节 → "多少字节 + 各进制取值" 的摘要文本（输入区/表格选区共用）。"""
    if not data:
        return ""
    def cut(text: str, size: int = limit) -> str:
        return text if len(text) <= size else text[:size] + " …"
    parts = [f"选中 {len(data)} 字节（偏移 {offset}）" if offset or len(data) == 1
             else f"选中 {len(data)} 字节"]
    parts.append(f"HEX: {cut(codec.bytes_to_text(data, codec.RADIX_HEX))}")
    parts.append(f"DEC: {cut(codec.bytes_to_text(data, codec.RADIX_DEC))}")
    parts.append(f"BIN: {cut(codec.bytes_to_text(data, codec.RADIX_BIN), 48)}")
    parts.append(f"ASCII: {cut(codec.to_printable(data.decode('utf-8', errors='replace')), 32)}")
    if len(data) <= 8:
        parts.append("U16 大端: " + " ".join(
            str(int.from_bytes(data[i:i + 2], "big")) for i in range(0, len(data) - 1, 2)
        ) or "U16 大端: -")
        parts.append("I16 大端: " + " ".join(
            str(int.from_bytes(data[i:i + 2], "big", signed=True)) for i in range(0, len(data) - 1, 2)
        ) or "I16 大端: -")
    return "　│　".join(parts)


# --------------------------------------------------------------------------- #
# 帧列表
# --------------------------------------------------------------------------- #
class FrameListPanel(QWidget):
    frame_selected = pyqtSignal(int)
    #: 选中的帧对应 (起始字节, 字节数) —— 让左侧原文反色定位过去
    bytes_requested = pyqtSignal(int, int)
    #: 筛选 / 显示上限变了 —— 只需要重渲染，不用重新分帧解析
    render_dirty = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._frames = []
        self._summaries: dict[int, str] = {}
        self._visible: list[int] = []          # 过滤后的帧位置（虚拟行 → 帧位置）
        self._visible_frames: list = []
        self._positions: list[int] = []
        self._keyword = ""
        #: 摘要列的行来源（没预解析的帧由主窗口按需解析）
        self.summary_provider: Callable[[int], str] | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        bar = QHBoxLayout()
        self.filter_combo = QComboBox()
        self.filter_combo.addItem("全部帧", "all")
        self.filter_combo.addItem("仅校验失败", "fail")
        self.filter_combo.addItem("仅校验通过", "ok")
        self.filter_combo.addItem("仅不完整帧", "partial")
        self.keyword_edit = QLineEdit()
        self.keyword_edit.setPlaceholderText("关键字过滤（匹配原始 HEX / 摘要 / Tag）")
        self.info_label = QLabel("—")
        bar.addWidget(QLabel("筛选"))
        bar.addWidget(self.filter_combo)
        bar.addWidget(self.keyword_edit, 1)
        bar.addWidget(self.info_label)
        layout.addLayout(bar)

        self.table = SmartTable(FRAME_COLUMNS, self, table_id="frames")
        self.table.status_message.connect(lambda _t: None)
        layout.addWidget(self.table, 1)

        self.filter_combo.currentIndexChanged.connect(self.render_dirty)
        self.keyword_edit.textChanged.connect(self.render_dirty)
        self.table.itemSelectionChanged.connect(self._on_selection)
        self.table.virtual_grew.connect(lambda _loaded, _total: self.update_info())

    # ------------------------------------------------------------------ #
    # 渲染：帧数不设上限，表格用"滑动窗口"按需取行
    # ------------------------------------------------------------------ #
    def set_frames(self, frames, summaries: dict[int, str]) -> None:
        """一次性重建。"""
        self._frames = frames                  # 可能是惰性帧视图，别 list() 展开
        self._summaries = dict(summaries)
        self.begin_render()
        self.render_chunk(self._frames, self._summaries)
        self.end_render()

    def set_frame_source(self, frames) -> None:
        """分帧一做完就知道帧总数（信息栏「共 N 帧」立刻准确）。"""
        self._frames = frames
        self.update_info()

    def begin_render(self) -> None:
        self._visible = []
        self._visible_frames = []
        self._shown = 0
        self._capped = False
        self._positions = []
        self.table.begin_stream()

    def render_chunk(self, frames, summaries: dict[int, str]) -> None:
        """流式喂进来的帧：只挑出符合筛选的帧**位置**，不建行。

        表格是虚拟的（滚到哪补哪），所以这里不再受"最多显示多少帧"限制。
        """
        if not self._frames and frames:             # 兜底：没收到帧源时按喂进来的算
            self._frames = frames
        if summaries and not self._summaries:
            self._summaries = dict(summaries)
        self._positions.extend(self._accepts_all(frames, summaries))
        self.table.refresh_virtual(len(self._positions), reset=False)   # 流式中只增不减
        self.update_info()

    def end_render(self) -> None:
        self._install_virtual()
        self.update_info()
        if self._positions and not self.table.selectedIndexes():
            self.table.select_row_silently(0)   # 静默选中，避免解析完就把左侧原文拽走
            self.emit_selected()

    def _install_virtual(self) -> None:
        """把表格切成虚拟行模式：数据源 = 过滤后的位置表。"""
        self._visible = list(self._positions)
        self.table.begin_virtual(len(self._visible), self.row_for, window=600, chunk=120)

    def row_for(self, start: int, count: int) -> list[dict]:
        """虚拟行回调：给出第 start 行起的 count 行（行号 → 帧位置 → 帧）。"""
        rows = []
        stop = min(len(self._visible), start + max(0, count))
        frames = self._frames
        total = len(frames)
        for row in range(max(0, start), stop):
            position = self._visible[row]
            if not 0 <= position < total:
                continue
            frame = frames[position]
            summary = self._summaries.get(frame.index, "")
            if not summary and self.summary_provider is not None:
                summary = self.summary_provider(position)      # 滚到这里才解析字段
                if summary:
                    self._summaries[frame.index] = summary
            rows.append(self._frame_row(frame, summary))
        return rows

    def _accepts_all(self, frames, summaries: dict[int, str]) -> list[int]:
        """一次筛选一批帧，返回命中的**位置**列表（位置 = 在 frames 里的下标）。"""
        mode = self.filter_combo.currentData()
        keyword = self.keyword_edit.text().strip().lower()
        out: list[int] = []
        lazy = hasattr(frames, "state_of")
        position = 0
        for frame in frames:
            # 惰性视图下 frame.index 就是位置；普通列表也一致
            index = frame.index if not lazy else position
            if self._accepts_frame(frame, summaries.get(index, ""), mode, keyword):
                out.append(index)
            position += 1
        return out

    def _accepts_frame(self, frame, summary: str, mode: str, keyword: str) -> bool:
        if mode == "fail" and frame.check_state != "fail":
            return False
        if mode == "ok" and frame.check_state != "ok":
            return False
        if mode == "partial" and "不完整" not in frame.note and "残帧" not in frame.note:
            return False
        if keyword:
            return (keyword in frame.hex.lower() or keyword in summary.lower()
                    or keyword in frame.tag.lower() or keyword in frame.note.lower())
        return True

    def emit_selected(self) -> None:
        """程序化定位后手动通知下游（不发 bytes_requested，避免和跳转互相触发）。"""
        row = self.table.currentRow()
        if 0 <= row < len(self._visible):
            self.frame_selected.emit(self._visible[row])

    def visible_frames(self) -> list:
        """当前筛选命中的帧（惰性取：只在需要时按位置取帧）。"""
        return [self._frames[position] for position in self._visible
                if 0 <= position < len(self._frames)]

    def row_for_byte(self, start: int, end: int) -> "int | None":
        """字节区间落在表格的第几行（只看索引，不把几十万帧造出来）。"""
        frames = self._frames
        lazy = hasattr(frames, "offset_of")
        total = len(frames)
        for row, position in enumerate(self._visible):
            if not 0 <= position < total:
                continue
            if lazy:
                offset, size = frames.offset_of(position), frames.size_of(position)
            else:
                frame = frames[position]
                offset, size = frame.offset, frame.size
            if offset < end and start < offset + size:
                return row
        return None

    def frame_at_row(self, row: int) -> "object | None":
        if 0 <= row < len(self._visible):
            position = self._visible[row]
            if 0 <= position < len(self._frames):
                return self._frames[position]
        return None

    def rowCount(self) -> int:      # noqa: N802 - 方便外部查询（逻辑总行数）
        return self.table.row_count

    def update_info(self) -> None:
        total = len(self._frames)
        text = f"共 {total} 帧"
        if self._positions and len(self._positions) != total:
            text += f"　│　筛选命中 {len(self._positions)} 帧"
        loaded = self.table.loaded_rows
        if self.table.virtual and loaded < self.table.total_rows:
            text += f"　│　已铺 {loaded} 行，滚到底自动续（共 {self.table.total_rows} 行）"
        else:
            text += "　│　滑动窗口（滚到哪加载哪）"
        if text != self.info_label.text():      # 流式时每块都会调，文本没变就不动
            self.info_label.setText(text)

    def _accepts(self, frame, summary: str) -> bool:
        return self._accepts_frame(frame, summary, self.filter_combo.currentData(),
                                   self.keyword_edit.text().strip().lower())

    def _frame_row(self, frame, summary: str) -> dict:
        check_text = self._check_text(frame.check_state)
        return {
            "index": frame.index, "offset": frame.offset, "length": frame.size,
            "tag": frame.tag, "check": check_text,
            "summary": summary, "hex": frame.hex,
            "_fg": {"check": CHECK_COLORS.get(frame.check_state, theme.FG)},
            "_tip": {"index": [
                ("偏移", f"{frame.offset} (0x{frame.offset:X})"),
                ("长度", f"{frame.size} 字节"),
                ("校验", check_text),
                ("说明", frame.note or "—"),
            ], "check": [
                ("校验", check_text),
                ("说明", frame.note or "帧尾与当前校验算法一致"),
            ]},
        }

    @staticmethod
    def _check_text(state: str) -> str:
        return CHECK_LABELS.get(state, state)

    def _on_selection(self) -> None:
        row = self.table.currentRow()
        if 0 <= row < len(self._visible):
            self.frame_selected.emit(self._visible[row])
            frame = self.frame_at_row(row)
            if frame is not None:
                self.bytes_requested.emit(frame.offset, frame.size)

    def selected_frame_index(self) -> int:
        row = self.table.currentRow()
        if 0 <= row < len(self._visible):
            return self._visible[row]
        return -1

    def _dummy(self) -> None:  # pragma: no cover
        pass


# --------------------------------------------------------------------------- #
# 字段解析结果
# --------------------------------------------------------------------------- #
class FieldResultPanel(QWidget):
    #: 「最多 N 帧」变了 —— 需要重算字段（FIELDS 级）
    needs_reparse = pyqtSignal()
    #: 选中的字段对应 (起始字节, 字节数)
    bytes_requested = pyqtSignal(int, int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[dict] = []
        self._current = -1
        self._frames_total = 0
        self._fields_per_frame = 0
        #: 主窗口给的行来源：(帧位置, 行内字段偏移, 数量) -> 行字典列表
        self.row_provider: Callable[[int, int, int], list[dict]] | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        bar = QHBoxLayout()
        self.only_current_box = QCheckBox("仅当前帧")
        self.info_label = QLabel("—")
        bar.addWidget(self.only_current_box)
        bar.addStretch(1)
        bar.addWidget(self.info_label)
        bar.addWidget(QLabel("提示：悬停单元格可看原始值 / 换算 / HEX / DEC / BIN / ASCII"))
        layout.addLayout(bar)

        self.table = SmartTable(FIELD_COLUMNS, self, table_id="fields_result")
        self.table.itemSelectionChanged.connect(self._on_table_selection)
        layout.addWidget(self.table, 1)
        self.only_current_box.toggled.connect(self._refresh)
        self.table.virtual_grew.connect(lambda _loaded, _total: self.update_info())

    def frame_limit(self) -> int:
        """预解析多少帧（剩下的滚到哪解到哪）。"""
        return PREPARSE_FRAMES

    def set_virtual_source(self, frames_or_count, fields_per_frame: int) -> None:
        """表格改成虚拟行：行 = 帧 × 字段，滚到哪就解析哪一帧。"""
        self._frames_total = (len(frames_or_count)
                              if not isinstance(frames_or_count, int) else frames_or_count)
        self._fields_per_frame = max(0, int(fields_per_frame))
        self._rebuild_virtual()

    def _rebuild_virtual(self) -> None:
        if self.only_current_box.isChecked() and self._current >= 0:
            total = self._fields_per_frame
            base = self._current
        else:
            total = self._frames_total * self._fields_per_frame
            base = -1
        self.table.begin_virtual(total, lambda start, count: self._rows_for(start, count, base),
                                 window=800, chunk=200)
        self.update_info()

    def _rows_for(self, start: int, count: int, base_frame: int) -> list[dict]:
        if self.row_provider is None:
            return []
        per = max(1, self._fields_per_frame)
        if base_frame >= 0:                       # 仅当前帧
            return self.row_provider(base_frame, start, count)
        out: list[dict] = []
        first = max(0, start)
        stop = start + max(0, count)
        position = first // per
        offset = first % per
        while position < self._frames_total and len(out) < count:
            take = min(per - offset, count - len(out))
            out.extend(self.row_provider(position, offset, take))
            position += 1
            offset = 0
            if position * per > stop:
                break
        return out[:count]

    def _on_table_selection(self) -> None:
        """选中一个字段 → 把它在数据里的位置告诉主窗口（左侧原文反色定位）。"""
        row = self.table.currentRow()
        if row < 0:
            return
        span = self.table.row_data(row).get("_bytes")
        if span and span[1]:
            self.bytes_requested.emit(int(span[0]), int(span[1]))

    # ------------------------------------------------------------------ #
    # 流式渲染（pipeline 分块喂行；筛选在渲染时进行）
    # ------------------------------------------------------------------ #
    def begin_render(self, keep_cache: bool = False) -> None:
        if not keep_cache:
            self._rows = []
        self.table.begin_stream()

    def render_chunk(self, rows: list[dict], keep_cache: bool = False) -> None:
        if not keep_cache:
            self._rows.extend(rows)
        if self._fields_per_frame:
            self.update_info()          # 虚拟行模式：行不用逐块填
            return
        self.table.append_rows([row for row in rows if self._accepts(row)])
        self.update_info()

    def end_render(self) -> None:
        self.table.end_stream()
        self.update_info()

    def update_info(self) -> None:
        if self._fields_per_frame:
            total_rows = self.table.total_rows or (self._frames_total * self._fields_per_frame)
            loaded = self.table.loaded_rows
            if self.only_current_box.isChecked() and self._current >= 0:
                text = (f"仅当前帧 #{self._current}：{self._fields_per_frame} 个字段（滑动窗口）")
            else:
                text = (f"共 {self._frames_total} 帧 × {self._fields_per_frame} 字段"
                        f" = {total_rows} 行")
                if loaded < total_rows:
                    text += f"　│　已铺 {loaded} 行，滚到底自动续"
            text += "　│　滚到哪解析哪（无上限）"
        else:
            text = f"共 {len(self._rows)} 行"
        if text != self.info_label.text():
            self.info_label.setText(text)

    def _accepts(self, row: dict) -> bool:
        if self.only_current_box.isChecked() and self._current >= 0:
            if row.get("frame") != self._current:
                return False
        return True

    def set_rows(self, rows: list[dict]) -> None:
        """一次性重建（内部仍是分块渲染）。"""
        self.begin_render()
        self.render_chunk(rows)
        self.end_render()

    def set_current_frame(self, index: int) -> None:
        self._current = index
        if self.only_current_box.isChecked():
            self._rebuild_virtual()

    def _refresh(self) -> None:
        if self._fields_per_frame:
            self._rebuild_virtual()
        else:
            self.set_rows(self._rows)


# --------------------------------------------------------------------------- #
# 进制视图：文本块 + 字节对照表
# --------------------------------------------------------------------------- #
def _hex_cell(value, radix: str) -> str:
    if radix == "hex" and isinstance(value, int):
        return f"{value:02X}"
    return field_parser.format_value(value, radix)


def _bin_cell(value, radix: str) -> str:
    if radix == "bin" and isinstance(value, int):
        return format(value, "08b")
    return field_parser.format_value(value, radix)


def _ascii_cell(value, radix: str) -> str:
    if radix == "ascii" and isinstance(value, int):
        return chr(value) if 32 <= value < 127 else "."
    return field_parser.format_value(value, radix)


def _bit_cell(bit: int):
    def formatter(value, radix: str) -> str:
        if isinstance(value, int):
            return "1" if (value >> bit) & 1 else "0"
        return field_parser.format_value(value, radix)
    return formatter


class BytesViewPanel(QWidget):
    """整体转 HEX / DEC / BIN / ASCII 展示，并给出逐字节对照表和位展开。"""

    #: 选中的字节对应 (起始字节, 字节数)
    bytes_requested = pyqtSignal(int, int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._all_data = b""
        self._frames = []
        self._current = -1
        self.streamer = RowStreamer(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        self.source_combo = QComboBox()
        for key, label in SOURCE_ITEMS:
            self.source_combo.addItem(label, key)
        self.radix_combo = QComboBox()
        for key, label in RADIX_ITEMS:
            self.radix_combo.addItem(label, key)
        self.per_line_spin = QSpinBox()
        self.per_line_spin.setRange(1, 64)
        self.per_line_spin.setValue(16)
        self.sep_combo = QComboBox()
        self.sep_combo.addItem("空格分隔", " ")
        self.sep_combo.addItem("逗号分隔", ", ")
        self.sep_combo.addItem("不分隔", "")
        self.upper_box = QCheckBox("大写")
        self.upper_box.setChecked(True)
        self.prefix_box = QCheckBox("0x 前缀")
        self.offset_box = QCheckBox("显示偏移")
        self.offset_box.setChecked(True)
        self.ascii_box = QCheckBox("附加 ASCII")
        self.ascii_box.setChecked(True)
        self.bits_box = QCheckBox("展开位 b7~b0")
        self.bits_box.setChecked(True)
        self.max_spin = QSpinBox()
        self.max_spin.setRange(64, 262144)
        self.max_spin.setSingleStep(256)
        self.max_spin.setValue(2048)
        self.max_spin.setToolTip("表格最多显示多少字节（文本视图同样受限，避免卡顿）")

        grid.addWidget(QLabel("数据源"), 0, 0)
        grid.addWidget(self.source_combo, 0, 1)
        grid.addWidget(QLabel("显示进制"), 0, 2)
        grid.addWidget(self.radix_combo, 0, 3)
        grid.addWidget(QLabel("每行字节"), 0, 4)
        grid.addWidget(self.per_line_spin, 0, 5)
        grid.addWidget(self.sep_combo, 0, 6)
        grid.addWidget(self.upper_box, 0, 7)
        grid.addWidget(self.prefix_box, 0, 8)
        grid.addWidget(self.offset_box, 1, 0)
        grid.addWidget(self.ascii_box, 1, 1)
        grid.addWidget(self.bits_box, 1, 2)
        grid.addWidget(QLabel("最多显示字节"), 1, 3)
        grid.addWidget(self.max_spin, 1, 4)
        grid.addWidget(QLabel("复制为"), 1, 5)
        self.export_combo = QComboBox()
        for key, label in exporters.MODES:
            self.export_combo.addItem(label, key)
        grid.addWidget(self.export_combo, 1, 6)
        self.copy_button = QPushButton("复制")
        grid.addWidget(self.copy_button, 1, 7)
        self.info_label = QLabel("—")
        grid.addWidget(self.info_label, 1, 8)
        layout.addLayout(grid)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.text_view = _mono_edit("整体进制转换结果")
        splitter.addWidget(self.text_view)
        self.table = SmartTable(BYTE_COLUMNS, self, table_id="bytes")
        self.table.selection_summary_provider = self._byte_selection_summary
        self.table.itemSelectionChanged.connect(self._on_table_selection)
        splitter.addWidget(self.table)
        splitter.setSizes([260, 420])
        layout.addWidget(splitter, 1)

        for widget in (self.source_combo, self.radix_combo, self.sep_combo, self.export_combo):
            widget.currentIndexChanged.connect(self.refresh)
        for spin in (self.per_line_spin, self.max_spin):
            spin.valueChanged.connect(self.refresh)
        for box in (self.upper_box, self.prefix_box, self.offset_box,
                    self.ascii_box, self.bits_box):
            box.toggled.connect(self.refresh)
        self.copy_button.clicked.connect(self.copy_exported)

    # ------------------------------------------------------------------ #
    def set_source(self, all_data: bytes, frames, current_index: int) -> None:
        self._all_data = all_data
        self._frames = frames          # 惰性帧视图，别展开
        self._current = current_index
        self.refresh()

    def set_current_frame(self, index: int) -> None:
        self._current = index
        if self.source_combo.currentData() == "frame":
            self.refresh()

    def _current_bytes(self) -> tuple[bytes, str]:
        if self.source_combo.currentData() == "frame":
            for frame in self._frames:
                if frame.index == self._current:
                    return frame.data, f"当前帧 #{frame.index}（偏移 {frame.offset}，{frame.size} 字节）"
            return b"", "当前没有选中帧"
        return self._all_data, f"全部输入数据（{len(self._all_data)} 字节）"

    def _shown_base(self) -> int:
        """当前显示窗口第 0 行对应的绝对字节偏移。"""
        if self.source_combo.currentData() == "frame":
            for frame in self._frames:
                if frame.index == self._current:
                    return frame.offset
            return 0
        return 0

    def _on_table_selection(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        if not rows:
            return
        self.bytes_requested.emit(self._shown_base() + rows[0], len(rows))

    def refresh(self) -> None:
        data, label = self._current_bytes()
        limit = self.max_spin.value()
        truncated = len(data) > limit
        shown = data[:limit]
        self._shown = shown
        self.info_label.setText(
            f"{label}" + (f"，表格/文本仅显示前 {limit} 字节" if truncated else "")
        )
        self.text_view.setPlainText(self._render_text(shown, truncated, len(data)))
        self._render_table(shown)
        for bit in range(8):
            col = self._bit_column(bit)
            if col >= 0:
                self.table.setColumnHidden(col, not self.bits_box.isChecked())

    def _byte_selection_summary(self, rows: list[int], cols: list[int]) -> str:
        """选中若干行 → 报出选中多少字节 + 各进制取值。"""
        shown = getattr(self, "_shown", b"")
        picked = bytes(shown[row] for row in rows if 0 <= row < len(shown))
        if not picked:
            return ""
        return byte_summary_text(picked, offset=(rows[0] if rows else 0))

    def _bit_column(self, bit: int) -> int:
        for index, spec in enumerate(self.table.specs()):
            if spec.key == f"b{bit}":
                return index
        return -1

    def _render_text(self, data: bytes, truncated: bool, total: int) -> str:
        from ..field_parser import format_value  # noqa: F401 - 保持 import 一致性
        radix = self.radix_combo.currentData()
        per_line = self.per_line_spin.value()
        sep = self.sep_combo.currentData()
        upper = self.upper_box.isChecked()
        prefix = self.prefix_box.isChecked()
        show_offset = self.offset_box.isChecked()
        show_ascii = self.ascii_box.isChecked()

        head = ""
        if truncated:
            head = f"… 数据共 {total} 字节，以下只显示前 {len(data)} 字节 …\n\n"

        if radix == codec.RADIX_ASCII:
            return head + codec.to_printable(data.decode("utf-8", errors="replace"))

        if radix == codec.RADIX_HEX:
            unit = (lambda b: f"{b:02X}" if upper else f"{b:02x}")
            width = 2
        elif radix == codec.RADIX_DEC:
            unit = str
            width = 3
        else:
            unit = lambda b: format(b, "08b")  # noqa: E731
            width = 8

        head_prefix = "0x" if prefix and radix == codec.RADIX_HEX else ""
        line_width = per_line * (width + len(head_prefix) + len(sep))
        lines = []
        for offset in range(0, len(data), per_line):
            chunk = data[offset:offset + per_line]
            body = sep.join(head_prefix + unit(b) for b in chunk).ljust(line_width)
            if show_ascii:
                text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
                body += f"  |{text}|"
            if show_offset:
                body = f"{offset:08X}  {body}"
            lines.append(body)
        return head + "\n".join(lines)

    def _render_table(self, data: bytes) -> None:
        rows = []
        for offset, byte in enumerate(data):
            row = {"offset": offset, "hex": byte, "dec": byte, "bin": byte, "ascii": byte}
            fg = {}
            for bit in range(8):
                row[f"b{bit}"] = byte
                fg[f"b{bit}"] = theme.BIT_SET_FG if (byte >> bit) & 1 else theme.BIT_CLR_FG
            row["_fg"] = fg
            rows.append(row)
        for spec in self.table.specs():
            key = spec.key
            if key == "hex":
                spec.formatter = _hex_cell
            elif key == "bin":
                spec.formatter = _bin_cell
            elif key == "ascii":
                spec.formatter = _ascii_cell
            elif key.startswith("b") and key[1:].isdigit():
                spec.formatter = _bit_cell(int(key[1:]))
            elif key == "dec":
                spec.formatter = None
        # 逐字节对照表最多几千行 × 15 列，分片渲染，别一次性塞进去
        self.streamer.start(self.table, rows, chunk=128, budget_ms=8)

    def flush(self) -> None:
        """立即渲染完剩余行（同步模式 / 自检用）。"""
        self.streamer.finish_now()

    def copy_exported(self) -> None:
        data, _label = self._current_bytes()
        limit = self.max_spin.value()
        mode = self.export_combo.currentData()
        text = exporters.export_bytes(data[:limit], mode, self.per_line_spin.value(), "frame")
        copy_to_clipboard(text)
        self.info_label.setText(f"已复制 {len(data[:limit])} 字节（{dict(exporters.MODES).get(mode, mode)}）")


# --------------------------------------------------------------------------- #
# 日志 / JSON
# --------------------------------------------------------------------------- #
class LogPanel(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        bar = QHBoxLayout()
        self.copy_button = QPushButton("复制日志")
        bar.addWidget(self.copy_button)
        bar.addStretch(1)
        layout.addLayout(bar)
        self.view = _mono_edit("解析日志 / 警告 / 统计")
        layout.addWidget(self.view, 1)
        self.copy_button.clicked.connect(
            lambda: copy_to_clipboard(self.view.toPlainText())
        )

    def set_text(self, text: str) -> None:
        self.view.setPlainText(text)


class JsonPanel(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        bar = QHBoxLayout()
        self.copy_button = QPushButton("复制 JSON")
        self.info_label = QLabel("完整解析结果（含每帧字节与字段）")
        bar.addWidget(self.copy_button)
        bar.addWidget(self.info_label, 1)
        layout.addLayout(bar)
        self.view = _mono_edit("解析结果 JSON")
        layout.addWidget(self.view, 1)
        self.copy_button.clicked.connect(lambda: copy_to_clipboard(self.view.toPlainText()))

    def set_text(self, text: str) -> None:
        self.view.setPlainText(text)
