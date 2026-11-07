"""表格基类 SmartTable —— 帧列表 / 字段解析 / 字节对照表 / 字段定义表 全部复用。

四个关键能力
------------
1. **整列选择**：点击列标题选中整列，Ctrl+点击可多列累加/取消，右键有整列菜单；
2. **列颜色**  ：列标题右键可给整列设置/清除背景色（含快速配色），颜色随配置保存；
3. **列显示格式**：数值列可整列切换 HEX / DEC / BIN / ASCII 显示；
4. **列换算函数**：给整列绑定一个表达式（v 为原始值，如 `v*0.1`），显示值即为换算结果；
   鼠标悬停单元格时，提示里同时给出 原始值 / 换算函数 / 换算结果 / HEX / DEC / BIN / ASCII。

另外支持单元格级前景色覆盖（校验失败标红之类）与 Ctrl+C 复制选区/整行/整列。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtGui import QActionGroup, QBrush, QColor, QKeySequence
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QColorDialog, QHeaderView, QMenu,
    QTableWidget, QTableWidgetItem, QTableWidgetSelectionRange, QToolTip,
)

from .. import convert, field_parser
from . import theme
from .convert_dialog import ConvertDialog
from .util import align_flag, copy_to_clipboard, mono_font

RADIX_CHOICES: list[tuple[str, str]] = list(field_parser.RADIX_CHOICES)

#: 列「快速配色」：黑白主题下只给灰度色阶（仍可在取色器里自选彩色）
DEFAULT_COLORS = list(theme.COLUMN_COLORS)

ERROR_COLOR = theme.FAIL_FG
ERROR_BG = theme.ERROR_BG

#: 虚拟行（滑动窗口）参数：窗口至少多大、一次补多少行、最少留多少行
VIRTUAL_WINDOW = 600
VIRTUAL_CHUNK = 120
VIRTUAL_MIN_WINDOW = 200
#: Qt 的表格要给每一行留元数据（2 百多万行 ≈ 144 MB），所以行数是"滚到才扩容"：
#: 一开始只给这么多行，滚到接近末尾再翻倍，一直滚到底才铺满整个范围。
VIRTUAL_INITIAL_ROWS = 20000
VIRTUAL_GROW_TAIL = 400


@dataclass
class ColumnSpec:
    key: str
    title: str
    width: int = 110
    numeric: bool = False                 # 允许整列切换进制 / 换算
    radix: str = "auto"                   # 初始显示格式（只有 numeric 列才会生效）
    color: str = ""                       # 初始背景色 #RRGGBB
    align: str = "left"
    monospace: bool = False
    convert: str = ""                     # 初始换算函数
    unit: str = ""
    simple_tooltip: bool = False          # 悬停只显示简短提示（列很多时提速）
    formatter: Callable[[Any, str], str] | None = None
    tooltip: str = ""


class SmartTable(QTableWidget):
    """带整列操作的表格。"""

    column_style_changed = pyqtSignal()
    status_message = pyqtSignal(str)
    column_clicked = pyqtSignal(int)
    #: 选中变化时给出摘要（多少字节 / 各进制取值）
    selection_changed = pyqtSignal(str)
    #: 虚拟行的"已铺行数"变了（滚到底自动续）→ (已铺, 总行数)
    virtual_grew = pyqtSignal(int, int)

    #: 面板可提供"选中摘要"的附加内容：provider(rows, cols) -> str
    selection_summary_provider: Callable[[list[int], list[int]], str] | None = None

    #: 是否给单元格加悬停提示（多进制 + 换算结果）
    tooltips_enabled = True

    #: 面板可挂一个「本列快捷操作」子菜单构造函数：hook(submenu, col)，自己处理 triggered
    header_menu_hook: Callable[[QMenu, int], None] | None = None

    def __init__(self, columns: list[ColumnSpec], parent=None, table_id: str = "",
                 row_numbers: bool = False) -> None:
        super().__init__(parent)
        self.table_id = table_id
        self._row_numbers = row_numbers
        self._specs: list[ColumnSpec] = list(columns)
        self._radix: dict[str, str] = {s.key: s.radix for s in self._specs}
        self._colors: dict[str, str] = {s.key: s.color for s in self._specs if s.color}
        self._convert: dict[str, str] = {s.key: s.convert for s in self._specs if s.convert}
        self._rows: list[dict] | dict[int, dict] = []
        #: 虚拟行模式（滑动窗口）的状态
        self._total_rows: int | None = None
        self._row_filler = None
        self._row_window = VIRTUAL_WINDOW
        self._row_chunk = VIRTUAL_CHUNK
        self._filled: set[int] = set()
        self._item_mode = False
        self._setup()

    # ------------------------------------------------------------------ #
    # 初始化
    # ------------------------------------------------------------------ #
    def _setup(self) -> None:
        self.setColumnCount(len(self._specs))
        self.setHorizontalHeaderLabels([s.title for s in self._specs])
        for index, spec in enumerate(self._specs):
            self.setColumnWidth(index, spec.width)

        header = self.horizontalHeader()
        header.setSectionsClickable(True)
        header.setStretchLastSection(True)
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._header_menu)
        header.sectionClicked.connect(self._header_clicked)

        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.setWordWrap(False)
        self.verticalHeader().setVisible(self._row_numbers)
        self.verticalHeader().setDefaultSectionSize(22)
        self.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._cell_menu)
        self.itemSelectionChanged.connect(self._emit_selection_summary)

    # ------------------------------------------------------------------ #
    # 选中摘要：选了多少字节、各进制分别是多少
    # ------------------------------------------------------------------ #
    def selection_summary(self) -> str:
        indexes = self.selectedIndexes()
        if not indexes:
            return ""
        rows = sorted({index.row() for index in indexes})
        cols = sorted({index.column() for index in indexes})
        if len(cols) == 1 and self.selectionModel().isColumnSelected(cols[0]):
            text = f"已选中整列「{self._specs[cols[0]].title}」共 {len(rows)} 行"
        elif len(rows) == 1:
            text = f"已选中 {len(indexes)} 格（第 {rows[0] + 1} 行）"
        else:
            text = f"已选中 {len(indexes)} 格（{len(rows)} 行 × {len(cols)} 列）"
        if self.selection_summary_provider is not None:
            try:
                extra = self.selection_summary_provider(rows, cols)
            except Exception as exc:  # noqa: BLE001
                extra = f"（摘要计算失败：{exc}）"
            if extra:
                text += "　│　" + extra
        return text

    def _emit_selection_summary(self) -> None:
        self.selection_changed.emit(self.selection_summary())

    # ------------------------------------------------------------------ #
    # 数据填充
    # ------------------------------------------------------------------ #
    def set_rows(self, rows: list[dict]) -> None:
        """一次性填充（内部 = begin_stream + append_rows + end_stream）。"""
        self.begin_stream()
        self.append_rows(rows)
        self.end_stream()

    # ------------------------------------------------------------------ #
    # 流式填充：分块追加 + 渐进渲染（配合 pipeline 的时间片调度）
    # ------------------------------------------------------------------ #
    def begin_stream(self) -> None:
        """开始流式填充：清空表格，准备逐块追加。"""
        self._item_mode = False
        self._total_rows = None
        self._row_filler = None
        self._filled = set()
        self._rows = []
        self.setRowCount(0)

    def append_rows(self, rows: list[dict]) -> int:
        """追加一批行，返回起始行号。整批只触发一次重绘（块内不刷屏）。"""
        if not rows:
            return len(self._rows)
        start = len(self._rows)
        self.setUpdatesEnabled(False)
        try:
            self.setRowCount(start + len(rows))
            for offset, row in enumerate(rows):
                row_index = start + offset
                self._rows.append(row)
                self._write_row_items(row_index, row)
        finally:
            self.setUpdatesEnabled(True)
        return start

    def end_stream(self) -> None:
        self.viewport().update()

    @property
    def row_count(self) -> int:
        """逻辑行数（虚拟行模式下就是"一共多少行"，不是 Qt 当前铺出来多少行）。"""
        return self._total_rows if self.virtual else len(self._rows)

    #: 别名，语义更直白：total_rows = 逻辑总行数，loaded_rows = 已经铺出来的行数
    @property
    def total_rows(self) -> int:
        return self.row_count

    def set_columns(self, columns: list[ColumnSpec]) -> None:
        """换一套列定义（列数可变，例如宽表按规则/字段动态生成）。

        进制以 spec 为准（换了规则就该按新规则显示）；列颜色与换算函数是用户手设的，按 key 保留。
        """
        self._specs = list(columns)
        keys = {spec.key for spec in self._specs}
        self._colors = {k: v for k, v in self._colors.items() if k in keys}
        self._convert = {k: v for k, v in self._convert.items() if k in keys}
        for spec in self._specs:
            self._radix[spec.key] = spec.radix
            if spec.color and spec.key not in self._colors:
                self._colors[spec.key] = spec.color
            if spec.convert and spec.key not in self._convert:
                self._convert[spec.key] = spec.convert
        self.setColumnCount(len(self._specs))
        self.setHorizontalHeaderLabels([spec.title for spec in self._specs])
        for index, spec in enumerate(self._specs):
            self.setColumnWidth(index, spec.width)
            self._refresh_header(index)
        self._item_mode = False
        self._rows = []
        self.setRowCount(0)

    def set_item_rows(self, rows: list[list[QTableWidgetItem | None]]) -> None:
        """填充自带 QTableWidgetItem 的表格（可编辑表用，例如字段定义表）。"""
        self._item_mode = True
        self._rows = []
        self.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c in range(len(self._specs)):
                item = row[c] if c < len(row) else None
                self.setItem(r, c, item if item is not None else QTableWidgetItem(""))
        self._paint_all()

    def row_data(self, row: int) -> dict:
        """取某一行的原始字典（含 `_bytes` / `_tip` 等附加信息）。

        虚拟行模式下视口外的行没填，这里会**按需向面板要一行**（导出 / 复制要用）。
        """
        if self.virtual:
            if row in self._rows:
                return self._rows[row]
            if self._row_filler is not None and 0 <= row < (self._total_rows or 0):
                rows = self._row_filler(row, 1) or []
                return rows[0] if rows else {}
            return {}
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return {}

    def ensure_row_loaded(self, row: int) -> None:
        """要选中/定位到第 row 行时，先确保这一行真的铺出来了（滚动窗口按需扩容）。"""
        if not self.virtual or row < 0:
            return
        total = self._total_rows or 0
        if row >= total:
            return
        if row >= self.loaded_rows:
            self._grow_capacity(row)
            self._ensure_window()

    def select_row_silently(self, row: int) -> None:
        """程序化选行但不发选中信号（避免和"跳转同步"互相触发）。"""
        self.ensure_row_loaded(row)
        blocked = self.blockSignals(True)
        try:
            self.selectRow(row)
        finally:
            self.blockSignals(blocked)

    # ------------------------------------------------------------------ #
    # 渲染：文本 + 颜色 + 悬停提示
    # ------------------------------------------------------------------ #
    def _display_for(self, spec: ColumnSpec, raw: Any, radix: str, expr: str):
        """返回 (显示文本, 换算后的值, 错误信息)。"""
        value = raw
        error = ""
        if expr and spec.numeric:
            try:
                value = convert.apply_convert(expr, raw)
            except convert.ConvertError as exc:
                return str(raw), raw, str(exc)
        if spec.formatter is not None:
            try:
                return spec.formatter(value, radix), value, error
            except Exception as exc:  # noqa: BLE001
                return "", value, str(exc)
        return field_parser.format_value(value, radix, 0), value, error

    def _tip_for(self, spec: ColumnSpec, raw, value, text: str, expr: str,
                 error: str, row: dict, r: int) -> str:
        if not self.tooltips_enabled:
            return ""
        extra = []
        tip_map = row.get("_tip") or {}
        entry = tip_map.get(spec.key)
        if isinstance(entry, list):
            extra = entry
        elif isinstance(entry, str):
            extra = [("说明", entry)]
        if spec.simple_tooltip and not expr:
            parts = [f"{spec.title}：{text}"]
            if raw is not None and raw != "":
                parts.append(f"原始值 {raw}")
            if error:
                parts.append(error)
            return "\n".join(parts)
        if not spec.numeric and not expr and not extra:
            return text if error else ""
        return convert.build_tooltip(
            title=f"{spec.title}（第 {r + 1} 行）",
            raw=raw, converted=value, text=text, unit=spec.unit,
            expr=expr, note=error, extra_rows=extra,
        )

    def _tooltip_for_cell(self, r: int, c: int) -> str:
        """悬停时才算提示：省掉"给每一格预先生成 HTML"的巨大开销。"""
        item = self.item(r, c)
        if item is None:
            return ""
        spec = self._specs[c]
        row = self._row_dict(r)
        if not spec.numeric:
            return self._tip_for(spec, None, None, item.text(), "", "", row, r)
        raw = item.data(Qt.ItemDataRole.UserRole)
        radix = self._radix.get(spec.key, spec.radix)
        expr = self._convert.get(spec.key, "")
        text, value, error = self._display_for(spec, raw, radix, expr)
        return self._tip_for(spec, raw, value, text, expr, error, row, r)

    def cell_tooltip(self, row: int, col: int) -> str:
        """取某格的悬停提示文本（悬停时按需计算，也可用于"复制悬停信息"）。"""
        return self._tooltip_for_cell(row, col)

    # ------------------------------------------------------------------ #
    # 虚拟行（滑动窗口）：行数可以几十万，只填视口附近的那几行
    # ------------------------------------------------------------------ #
    def begin_virtual(self, total_rows: int, filler, window: int = VIRTUAL_WINDOW,
                      chunk: int = VIRTUAL_CHUNK) -> None:
        """虚拟行模式。

        `filler(start, count) -> list[dict]`：按需给出第 start 行起的若干行；
        表格自己只保留视口附近的窗口，滚动时自动补齐、远处的丢掉 ——
        于是"有多少帧就有多少行"，不用再设"最多显示多少帧"。
        """
        self._item_mode = False
        self._total_rows = max(0, int(total_rows))
        self._row_filler = filler
        self._row_window = max(VIRTUAL_MIN_WINDOW, int(window))
        self._row_chunk = max(1, int(chunk))
        self._rows = {}
        self._filled = set()
        self._capacity = min(self._total_rows, VIRTUAL_INITIAL_ROWS)
        blocked = self.blockSignals(True)
        try:
            self.setRowCount(self._capacity)
        finally:
            self.blockSignals(blocked)
        self._fill_window(0, min(self._capacity, self._row_window))

    def end_virtual(self) -> None:
        self._total_rows = None
        self._row_filler = None
        self._filled = set()
        self._capacity = 0
        self._rows = []
        self.setRowCount(0)

    @property
    def virtual(self) -> bool:
        return self._total_rows is not None

    @property
    def loaded_rows(self) -> int:
        """当前已经"铺出来"的行数（滚到底会自动续上，最多到总行数）。"""
        return int(getattr(self, "_capacity", 0))

    def _grow_capacity(self, needed: int) -> bool:
        """行数不够了就翻倍（Qt 的行元数据很贵，一次铺满几百万行不划算）。"""
        total = self._total_rows or 0
        capacity = int(getattr(self, "_capacity", 0))
        if capacity >= total:
            return False
        target = max(capacity * 2, min(total, needed + self._row_window * 2))
        self._capacity = min(total, max(target, capacity + 1))
        blocked = self.blockSignals(True)
        try:
            self.setRowCount(self._capacity)
            self.updateGeometries()               # 让滚动条范围立刻跟上
        finally:
            self.blockSignals(blocked)
        self.virtual_grew.emit(self._capacity, total)
        return True

    def _visible_range(self) -> tuple[int, int]:
        bound = max(0, int(getattr(self, "_capacity", 0)) - 1)
        if bound < 0:
            return (0, -1)
        first = self.rowAt(1)                     # 跳过表头
        if first < 0:
            first = self.verticalScrollBar().value()
        last = self.rowAt(max(1, self.viewport().height() - 2))
        if last < 0:
            last = min(bound, first + max(1, self.viewport().height() // 22))
        return (max(0, min(first, bound)), max(0, min(last, bound)))

    def _fill_window(self, first: int, last: int) -> None:
        """补齐 [first, last] 这段行，并把远处已填的行丢掉（内存不随滚动增长）。"""
        filler = self._row_filler
        if filler is None or self._total_rows is None or last < first:
            return
        total = self._total_rows
        first, last = max(0, first), min(total - 1, last)
        capacity = int(getattr(self, "_capacity", 0))
        if last >= capacity - VIRTUAL_GROW_TAIL and capacity < total:
            self._grow_capacity(last)             # 快滚到已铺范围的末尾了 → 续上
            capacity = int(self._capacity)
        last = min(last, capacity - 1)
        missing = [row for row in range(first, last + 1) if row not in self._filled]
        if missing:
            self.setUpdatesEnabled(False)
            try:
                start = missing[0]
                while start <= missing[-1]:
                    count = min(self._row_chunk, missing[-1] - start + 1)
                    self._fill_block(start, count, filler)
                    start += count
            finally:
                self.setUpdatesEnabled(True)
        keep_from = max(0, first - self._row_window * 2)
        keep_to = min(capacity - 1, last + self._row_window * 2)
        stale = [row for row in self._filled if row < keep_from or row > keep_to]
        if stale:
            self.setUpdatesEnabled(False)
            try:
                for row in stale:
                    for col in range(len(self._specs)):
                        self.takeItem(row, col)
                    self._filled.discard(row)
                    self._rows.pop(row, None)
            finally:
                self.setUpdatesEnabled(True)

    def _fill_block(self, start: int, count: int, filler) -> None:
        total = self._total_rows or 0
        for offset, row in enumerate(filler(start, count) or []):
            row_index = start + offset
            if row_index >= total:
                break
            self._rows[row_index] = row
            self._filled.add(row_index)
            self._write_row_items(row_index, row)

    def _write_row_items(self, row_index: int, row: dict) -> None:
        for col, spec in enumerate(self._specs):
            item = QTableWidgetItem("")
            item.setData(Qt.ItemDataRole.UserRole, row.get(spec.key))
            item.setTextAlignment(align_flag(spec.align))
            if spec.monospace:
                item.setFont(mono_font(10))
            self.setItem(row_index, col, item)
            self._paint_cell(row_index, col)

    def _ensure_window(self) -> None:
        if not self.virtual:
            return
        total = self._total_rows or 0
        bar = self.verticalScrollBar()
        # 滚动条已经到底（或视口快接近已铺范围末尾）→ 把行数续上
        if self.loaded_rows < total:
            at_end = bar.maximum() > 0 and bar.value() >= bar.maximum() - 2
            first, last = self._visible_range()
            if at_end or last >= self.loaded_rows - VIRTUAL_GROW_TAIL:
                self._grow_capacity(max(bar.value(), last if last > 0 else 0))
        first, last = self._visible_range()
        if last < first:
            return
        half = self._row_window // 2
        self._fill_window(max(0, first - half), last + half)

    def scrollContentsBy(self, dx: int, dy: int) -> None:  # noqa: N802
        super().scrollContentsBy(dx, dy)
        self._ensure_window()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._ensure_window()

    def refresh_virtual(self, total_rows: int | None = None, reset: bool = True) -> None:
        """行数变了（换了筛选 / 规则）时更新行数。

        流式解析过程中行数是**只增不减**的（`reset=False`）：只把总行数改掉，
        已经填好的行不清空，省得每个数据块都重填一遍窗口。
        """
        if total_rows is not None and self._total_rows is not None:
            total = max(0, int(total_rows))
            grew = total > self._total_rows
            self._total_rows = total
            if reset or not grew:
                self._capacity = min(self._total_rows, VIRTUAL_INITIAL_ROWS)
                self._filled = set()
                self._rows = {}
            else:
                self._capacity = min(max(self._capacity, 1), self._total_rows)
            if reset:
                blocked = self.blockSignals(True)
                try:
                    self.setRowCount(self._capacity)
                finally:
                    self.blockSignals(blocked)
        self._ensure_window()

    def viewportEvent(self, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.ToolTip and self.tooltips_enabled:
            index = self.indexAt(event.pos())
            if index.isValid():
                text = self._tooltip_for_cell(index.row(), index.column())
                if text:
                    QToolTip.showText(event.globalPos(), text, self)
            return True
        return super().viewportEvent(event)

    def texts_for_row(self, row_dict: dict) -> list[str]:
        """按当前列格式（进制 / 换算函数 / 专用格式化）把一个行字典渲染成文本。

        导出、复制都走这里，所以**虚拟行（滑动窗口）下也能导出没显示过的行**。
        """
        texts: list[str] = []
        for spec in self._specs:
            raw = (row_dict or {}).get(spec.key)
            override = (row_dict.get("_text") or {}).get(spec.key) if row_dict else None
            if not spec.numeric:
                texts.append(str(override) if override is not None
                             else ("" if raw is None else str(raw)))
                continue
            radix = self._radix.get(spec.key, spec.radix)
            expr = self._convert.get(spec.key, "")
            text, _value, _error = self._display_for(spec, raw, radix, expr)
            if radix == "auto" and override is not None:
                text = str(override)
            texts.append(text)
        return texts

    def row_texts(self, row: int) -> list[str]:
        """第 row 行的文本（虚拟行模式下会按需算这一行）。"""
        return self.texts_for_row(self.row_data(row))

    def _row_dict(self, row_index: int) -> dict:
        """行字典：普通模式按列表下标取，虚拟模式是稀疏字典（没填的返回空）。"""
        if self.virtual:
            return self._rows.get(row_index, {})
        return self._rows[row_index] if 0 <= row_index < len(self._rows) else {}

    def _paint_cell(self, r: int, c: int) -> None:
        item = self.item(r, c)
        if item is None:
            return
        spec = self._specs[c]
        row = self._row_dict(r)
        expr = self._convert.get(spec.key, "") if spec.numeric else ""
        error = ""
        text = ""

        if not self._item_mode:
            raw = item.data(Qt.ItemDataRole.UserRole)
            # 只有"数值列"才套用列显示格式；文本列（原始 HEX / 摘要 / 备注…）永远原样显示，
            # 否则文本会被当成字节转成十进制，出现 "EB 90 → 235 144" 这种怪象
            radix = self._radix.get(spec.key, spec.radix) if spec.numeric else "auto"
            text, value, error = self._display_for(spec, raw, radix, expr)
            if radix == "auto":
                override = (row.get("_text") or {}).get(spec.key)
                if override is not None:
                    text = str(override)
            item.setText(text)
            # 悬停提示改成"悬停时才算"（viewportEvent），这里不再预生成 HTML
            if not self.tooltips_enabled and item.toolTip():
                item.setToolTip("")

        color = self._colors.get(spec.key, "")
        bg_map = row.get("_bg") or {}
        fg_map = row.get("_fg") or {}
        if spec.key in bg_map:
            item.setBackground(QBrush(QColor(bg_map[spec.key])))
        elif color:
            item.setBackground(QBrush(QColor(color)))
        else:
            item.setBackground(QBrush())
        if spec.key in fg_map:
            item.setForeground(QBrush(QColor(fg_map[spec.key])))
        elif error:
            item.setForeground(QBrush(QColor(ERROR_COLOR)))
            item.setBackground(QBrush(QColor(ERROR_BG)))
        else:
            item.setForeground(QBrush())

    def _paint_column(self, col: int) -> None:
        for r in range(self.rowCount()):
            self._paint_cell(r, col)

    def _paint_all(self) -> None:
        for c in range(len(self._specs)):
            self._paint_column(c)

    def set_cell_color(self, row: int, col: int, color: str, foreground: bool = True) -> None:
        item = self.item(row, col)
        if item is None:
            return
        brush = QBrush(QColor(color)) if color else QBrush()
        item.setForeground(brush) if foreground else item.setBackground(brush)

    # ------------------------------------------------------------------ #
    # 列样式：颜色 / 进制 / 换算函数
    # ------------------------------------------------------------------ #
    def column_radix(self, col: int) -> str:
        return self._radix.get(self._specs[col].key, self._specs[col].radix)

    def column_radix_by_key(self, key: str) -> str:
        return self._radix.get(key, "auto")

    def column_convert_by_key(self, key: str) -> str:
        return self._convert.get(key, "")

    def set_column_radix(self, col: int, radix: str, notify: bool = True) -> None:
        self._radix[self._specs[col].key] = radix
        self._paint_column(col)
        if notify:
            self.column_style_changed.emit()

    def column_color(self, col: int) -> str:
        return self._colors.get(self._specs[col].key, "")

    def set_column_color(self, col: int, color: str, notify: bool = True) -> None:
        spec = self._specs[col]
        if color:
            self._colors[spec.key] = color
        else:
            self._colors.pop(spec.key, None)
        self._paint_column(col)
        if notify:
            self.column_style_changed.emit()

    def column_convert(self, col: int) -> str:
        return self._convert.get(self._specs[col].key, "")

    def set_column_convert(self, col: int, expr: str, notify: bool = True) -> None:
        spec = self._specs[col]
        expr = (expr or "").strip()
        if expr and expr != "v":
            self._convert[spec.key] = expr
        else:
            self._convert.pop(spec.key, None)
        self._refresh_header(col)
        self._paint_column(col)
        if notify:
            self.column_style_changed.emit()

    def _refresh_header(self, col: int) -> None:
        spec = self._specs[col]
        item = self.horizontalHeaderItem(col)
        if item is None:
            return
        expr = self._convert.get(spec.key, "")
        mark = ""
        if expr:
            mark = f"  ⚙v→{expr}" if len(expr) <= 18 else "  ⚙已换算"
        item.setText(spec.title + mark)
        tips = [spec.tooltip] if spec.tooltip else []
        if expr:
            tips.append(f"换算函数：{expr}")
        tips.append("左键点标题=整列选择；右键=列颜色/显示格式/换算函数")
        item.setToolTip("\n".join(t for t in tips if t))

    def _sample_value(self, col: int):
        for r in range(self.rowCount()):
            item = self.item(r, col)
            if item is None:
                continue
            raw = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                return raw
        return 1

    # ------------------------------------------------------------------ #
    # 列状态持久化
    # ------------------------------------------------------------------ #
    def column_state(self) -> dict:
        return {
            "colors": dict(self._colors),
            "radix": dict(self._radix),
            "convert": dict(self._convert),
            "widths": {self._specs[c].key: self.columnWidth(c) for c in range(len(self._specs))},
        }

    def apply_column_state(self, state: dict) -> None:
        if not state:
            return
        self._colors = {k: v for k, v in (state.get("colors") or {}).items() if v}
        for key, value in (state.get("radix") or {}).items():
            self._radix[key] = value
        self._convert = {k: v for k, v in (state.get("convert") or {}).items() if v}
        for index in range(len(self._specs)):
            self._refresh_header(index)
        widths = state.get("widths") or {}
        for index, spec in enumerate(self._specs):
            if spec.key in widths:
                self.setColumnWidth(index, int(widths[spec.key]))
        self._paint_all()

    def reset_column_style(self) -> None:
        self._colors = {s.key: s.color for s in self._specs if s.color}
        self._radix = {s.key: s.radix for s in self._specs}
        self._convert = {s.key: s.convert for s in self._specs if s.convert}
        for index, spec in enumerate(self._specs):
            self.setColumnWidth(index, spec.width)
            self._refresh_header(index)
        self._paint_all()
        self.column_style_changed.emit()

    # ------------------------------------------------------------------ #
    # 整列选择 / 右键菜单
    # ------------------------------------------------------------------ #
    def select_entire_column(self, col: int, additive: bool = False) -> None:
        if self.rowCount() == 0:
            self.status_message.emit(f"列「{self._specs[col].title}」暂无数据")
            return
        rng = QTableWidgetSelectionRange(0, col, self.rowCount() - 1, col)
        if not additive:
            self.setCurrentCell(0, col)      # 先设当前格，再整列选中（setCurrentCell 会清空选区）
            self.clearSelection()
        self.setRangeSelected(rng, True)
        self.status_message.emit(
            f"已整列选择「{self._specs[col].title}」，共 {self.rowCount()} 行（Ctrl+点击可多列）"
        )
        self.column_clicked.emit(col)

    def _header_clicked(self, col: int) -> None:
        if col < 0:
            return
        mods = QApplication.keyboardModifiers()
        if mods & Qt.KeyboardModifier.ControlModifier:
            rng = QTableWidgetSelectionRange(0, col, max(0, self.rowCount() - 1), col)
            already = self.selectionModel().isColumnSelected(col)
            self.setRangeSelected(rng, not already)
            self.column_clicked.emit(col)
        else:
            self.select_entire_column(col)      # 内部会发 column_clicked

    def _header_menu(self, pos) -> None:
        header = self.horizontalHeader()
        col = header.logicalIndexAt(pos)
        if col < 0:
            return
        spec = self._specs[col]
        menu = QMenu(self)
        act_select = menu.addAction(f"整列选择「{spec.title}」")
        act_copy = menu.addAction("复制整列内容")
        menu.addSeparator()

        act_color = menu.addAction("设置本列颜色…")
        act_clear_color = menu.addAction("清除本列颜色")
        quick = menu.addMenu("快速配色")
        quick_actions = []
        for color in DEFAULT_COLORS:
            action = quick.addAction(color)
            action.setData(color)
            quick_actions.append(action)

        radix_actions = []
        if spec.numeric:
            sub = menu.addMenu("本列显示格式")
            group = QActionGroup(sub)
            group.setExclusive(True)
            for key, label in RADIX_CHOICES:
                action = sub.addAction(label)
                action.setCheckable(True)
                action.setChecked(self.column_radix(col) == key)
                action.setData(key)
                group.addAction(action)
                radix_actions.append(action)

        menu.addSeparator()
        expr = self.column_convert(col)
        label = f"换算函数：{expr}" if expr else "换算函数：无"
        act_expr_info = menu.addAction(label)
        act_expr_info.setEnabled(False)
        act_expr = menu.addAction("设置本列换算函数…" if spec.numeric else "（该列不支持换算）")
        act_expr.setEnabled(spec.numeric)
        act_expr_clear = menu.addAction("清除本列换算函数")
        act_expr_clear.setEnabled(bool(expr))
        quick_func = menu.addMenu("常用换算")
        quick_func.setEnabled(spec.numeric)
        func_actions = []
        for name, expression in convert.FUNC_PRESETS:
            action = quick_func.addAction(f"{name}   ({expression})")
            action.setData(expression)
            func_actions.append(action)

        menu.addSeparator()
        act_fit = menu.addAction("列宽自适应内容")
        act_default = menu.addAction("恢复默认列样式")

        if self.header_menu_hook is not None:
            menu.addSeparator()
            extra = menu.addMenu(f"「{spec.title}」快捷操作")
            try:
                self.header_menu_hook(extra, col)
            except Exception as exc:  # noqa: BLE001
                broken = extra.addAction(f"（快捷操作不可用：{exc}）")
                broken.setEnabled(False)

        chosen = menu.exec(header.mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_select:
            self.select_entire_column(col)
        elif chosen is act_copy:
            self.copy_column(col)
        elif chosen is act_color:
            color = QColorDialog.getColor(
                QColor(self.column_color(col) or "#FFFFFF"), self, f"选择「{spec.title}」列颜色"
            )
            if color.isValid():
                self.set_column_color(col, color.name())
                self.status_message.emit(f"「{spec.title}」列颜色已设为 {color.name()}")
        elif chosen is act_clear_color:
            self.set_column_color(col, "")
            self.status_message.emit(f"已清除「{spec.title}」列颜色")
        elif chosen in quick_actions:
            self.set_column_color(col, chosen.data())
            self.status_message.emit(f"「{spec.title}」列颜色已设为 {chosen.data()}")
        elif chosen in radix_actions:
            self.set_column_radix(col, chosen.data())
            self.status_message.emit(
                f"「{spec.title}」列显示格式 → {dict(RADIX_CHOICES).get(chosen.data(), chosen.data())}"
            )
        elif chosen is act_expr:
            dialog = ConvertDialog(spec.title, expr, self._sample_value(col), self)
            if dialog.exec():
                self.set_column_convert(col, dialog.expression())
                now = self.column_convert(col)
                self.status_message.emit(
                    f"「{spec.title}」换算函数已设为 {now}" if now
                    else f"已清除「{spec.title}」的换算函数"
                )
        elif chosen is act_expr_clear:
            self.set_column_convert(col, "")
            self.status_message.emit(f"已清除「{spec.title}」的换算函数")
        elif chosen in func_actions:
            self.set_column_convert(col, chosen.data())
            self.status_message.emit(f"「{spec.title}」换算函数 → {chosen.data()}")
        elif chosen is act_fit:
            self.resizeColumnToContents(col)
        elif chosen is act_default:
            self.reset_column_style()

    def _cell_menu(self, pos) -> None:
        item = self.itemAt(pos)
        if item is None:
            return
        col = item.column()
        spec = self._specs[col]
        menu = QMenu(self)
        act_cell = menu.addAction("复制单元格文本")
        act_tip = menu.addAction("复制悬停信息")
        act_row = menu.addAction("复制整行")
        act_col = menu.addAction("复制整列")
        menu.addSeparator()
        act_detail = menu.addAction("查看/换算本列…")
        act_select_col = menu.addAction("整列选择")
        act_color = menu.addAction("设置本列颜色…")
        chosen = menu.exec(self.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_cell:
            copy_to_clipboard(item.text())
            self.status_message.emit("已复制单元格文本")
        elif chosen is act_tip:
            copy_to_clipboard(self.cell_tooltip(item.row(), col))
            self.status_message.emit("已复制悬停信息（含原始值/换算/多进制值）")
        elif chosen is act_row:
            copy_to_clipboard(self.row_text(item.row()))
            self.status_message.emit(f"已复制第 {item.row() + 1} 行")
        elif chosen is act_col:
            self.copy_column(col)
        elif chosen is act_detail:
            dialog = ConvertDialog(spec.title, self.column_convert(col), item.data(Qt.ItemDataRole.UserRole), self)
            if dialog.exec():
                self.set_column_convert(col, dialog.expression())
        elif chosen is act_select_col:
            self.select_entire_column(col)
        elif chosen is act_color:
            color = QColorDialog.getColor(QColor(self.column_color(col) or "#FFFFFF"), self, "选择列颜色")
            if color.isValid():
                self.set_column_color(col, color.name())

    # ------------------------------------------------------------------ #
    # 复制
    # ------------------------------------------------------------------ #
    def row_text(self, row: int) -> str:
        cells = [self.item(row, c) for c in range(len(self._specs))]
        return "\t".join(c.text() if c is not None else "" for c in cells)

    def column_text(self, col: int) -> str:
        return "\n".join(
            (self.item(r, col).text() if self.item(r, col) is not None else "")
            for r in range(self.rowCount())
        )

    def copy_column(self, col: int) -> None:
        copy_to_clipboard(self.column_text(col))
        self.status_message.emit(
            f"已复制整列「{self._specs[col].title}」（{self.rowCount()} 行）到剪贴板"
        )

    def copy_columns(self, cols: list[int]) -> None:
        lines = []
        for r in range(self.rowCount()):
            lines.append("\t".join(
                (self.item(r, c).text() if self.item(r, c) is not None else "") for c in cols
            ))
        copy_to_clipboard("\n".join(lines))
        self.status_message.emit(f"已复制 {len(cols)} 列 × {self.rowCount()} 行到剪贴板")

    def copy_selection(self) -> None:
        indexes = sorted(self.selectedIndexes(), key=lambda i: (i.row(), i.column()))
        if not indexes:
            return
        cols = sorted({idx.column() for idx in indexes})
        if len(cols) == 1:
            self.copy_column(cols[0])
            return
        rows: dict[int, list[str]] = {}
        for idx in indexes:
            rows.setdefault(idx.row(), []).append(
                self.item(idx.row(), idx.column()).text() if self.item(idx.row(), idx.column()) else ""
            )
        copy_to_clipboard("\n".join("\t".join(v) for v in rows.values()))
        self.status_message.emit(f"已复制 {len(indexes)} 个单元格（{len(cols)} 列）")

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_selection()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------ #
    def spec(self, col: int) -> ColumnSpec:
        return self._specs[col]

    def specs(self) -> list[ColumnSpec]:
        return list(self._specs)
