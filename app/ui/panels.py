"""输入面板 + 分帧设置面板。"""
from __future__ import annotations

import re

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPalette, QTextCursor
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSizePolicy, QSpinBox,
    QStackedWidget, QVBoxLayout, QWidget,
)

from .. import checksum, codec, framer
from ..source import PAGE_LINES
from . import theme
from .util import copy_to_clipboard, mono_font


def hint_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(f"color:{theme.FG_DIM};")
    return label


#: 十六进制数字串（可带 0x 前缀），用于把字节定位到原文的两位数字上
_HEX_RUN_RE = re.compile(r"(?:0[xX])?([0-9a-fA-F]+)")


class InputPanel(QWidget):
    """原始数据输入：选进制、粘贴/导入数据、看解析字节数；支持按字节反色定位。"""

    changed = pyqtSignal()
    import_requested = pyqtSignal()
    clear_requested = pyqtSignal()
    #: 输入框里选中的内容对应 (起始字节, 字节数) —— 供表格反向定位
    bytes_selected = pyqtSignal(int, int)
    #: 退出分页模式（用户手输/粘贴/清空）—— 主窗口据此释放大文件来源
    source_exited = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        #: 解析结果缓存（由主窗口 set_parsed_info 回填）：避免为显示几个数字整篇重算
        self._total_bytes: int | None = None
        self._radix_cache: str | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        form = QGridLayout()
        form.setHorizontalSpacing(8)
        self.radix_combo = QComboBox()
        for key, label in codec.RADIX_ITEMS:
            self.radix_combo.addItem(label, key)
        self.dec_width_combo = QComboBox()
        for value, label in codec.DEC_WIDTH_ITEMS:
            self.dec_width_combo.addItem(label, value)
        self.order_combo = QComboBox()
        for key, label in codec.BYTE_ORDER_ITEMS:
            self.order_combo.addItem(label, key)

        form.addWidget(QLabel("输入进制"), 0, 0)
        form.addWidget(self.radix_combo, 0, 1)
        form.addWidget(QLabel("十进制宽度"), 0, 2)
        form.addWidget(self.dec_width_combo, 0, 3)
        form.addWidget(QLabel("字节序"), 0, 4)
        form.addWidget(self.order_combo, 0, 5)
        layout.addLayout(form)

        self.editor = QPlainTextEdit()
        self.editor.setFont(mono_font(10))
        self.editor.setPlaceholderText(
            "在此粘贴数据，支持任意分隔/不分隔：\n"
            "  HEX   : AA 55 01 02   /  AA550102  /  0xAA,0x55  /  1Ah\n"
            "  DEC   : 170 85 1 2\n"
            "  BIN   : 10101010 01010101\n"
            "  ASCII : $POS,01,...\\r\\n   （\\r\\n\\t\\xNN 会被还原成真实字节）"
        )
        self.editor.setTabChangesFocus(True)
        palette = self.editor.palette()      # 未聚焦时也保持"黑底白字"的反色选中
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
            palette.setColor(group, QPalette.ColorRole.Highlight, QColor(theme.SEL_BG))
            palette.setColor(group, QPalette.ColorRole.HighlightedText, QColor(theme.SEL_FG))
        self.editor.setPalette(palette)
        layout.addWidget(self.editor, 1)

        row = QHBoxLayout()
        self.import_button = QPushButton("导入数据文件…")
        self.clear_button = QPushButton("清空")
        self.copy_hex_button = QPushButton("复制选区 HEX")
        self.copy_bin_button = QPushButton("复制选区 BIN")
        self.copy_hex_button.setToolTip("把选中的内容按 HEX 复制（不选则复制全部）")
        self.copy_bin_button.setToolTip("把选中的内容按二进制复制（不选则复制全部）")
        self.info_label = QLabel("等待输入")
        self.info_label.setStyleSheet(f"color:{theme.FG};")
        row.addWidget(self.import_button)
        row.addWidget(self.clear_button)
        row.addWidget(self.copy_hex_button)
        row.addWidget(self.copy_bin_button)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addWidget(self.info_label)

        # 选区信息：选中多少字符 / 多少字节 + 各进制取值
        self.selection_label = QLabel("选中内容后这里显示：字符数 · 字节数 · HEX/DEC/BIN/ASCII")
        self.selection_label.setWordWrap(True)
        self.selection_label.setFont(mono_font(9))
        self.selection_label.setStyleSheet(
            f"color:{theme.FG}; background:{theme.BG_ALT}; border:1px solid {theme.BORDER}; padding:3px;"
        )
        self.selection_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.selection_label)

        # 大文件分页条（只在分页模式下显示）
        self._source = None
        self._page = 0
        self._pager = QWidget()
        pager = QHBoxLayout(self._pager)
        pager.setContentsMargins(0, 0, 0, 0)
        self.page_prev = QPushButton("◀ 上一页")
        self.page_next = QPushButton("下一页 ▶")
        self.page_label = QLabel("—")
        self.page_spin = QSpinBox()
        self.page_spin.setRange(1, 1)
        self.page_spin.setPrefix("第 ")
        self.page_spin.setSuffix(" 页")
        self.page_goto = QPushButton("跳转")
        self.page_jump_spin = QSpinBox()
        self.page_jump_spin.setRange(1, 1)
        self.page_jump_spin.setPrefix("行 ")
        self.page_jump = QPushButton("跳到行")
        for widget in (self.page_prev, self.page_next, self.page_label, self.page_spin,
                       self.page_goto, self.page_jump_spin, self.page_jump):
            pager.addWidget(widget)
        pager.addStretch(1)
        self.page_prev.clicked.connect(lambda: self.show_page(self.page - 1))
        self.page_next.clicked.connect(lambda: self.show_page(self.page + 1))
        self.page_goto.clicked.connect(lambda: self.goto_page(self.page_spin.value()))
        self.page_jump.clicked.connect(lambda: self.goto_line(self.page_jump_spin.value()))
        self.page_spin.editingFinished.connect(
            lambda: self.goto_page(self.page_spin.value())
        )
        self._pager.setVisible(False)
        layout.addWidget(self._pager)

        self.radix_combo.currentIndexChanged.connect(self._emit_changed)
        self.dec_width_combo.currentIndexChanged.connect(self._emit_changed)
        self.order_combo.currentIndexChanged.connect(self._emit_changed)
        self.editor.textChanged.connect(self._emit_changed)
        self.editor.selectionChanged.connect(self._update_selection_info)
        self.import_button.clicked.connect(self.import_requested)
        self.clear_button.clicked.connect(self.clear_requested)
        self.copy_hex_button.clicked.connect(lambda: self._copy_selection(codec.RADIX_HEX))
        self.copy_bin_button.clicked.connect(lambda: self._copy_selection(codec.RADIX_BIN))
        for combo in (self.radix_combo, self.dec_width_combo, self.order_combo):
            combo.currentIndexChanged.connect(self._invalidate_cache)
        self.editor.textChanged.connect(self._invalidate_cache)

    # ------------------------------------------------------------------ #
    def selected_text(self) -> str:
        return self.editor.textCursor().selectedText().replace("\u2029", "\n")

    def selection_bytes(self) -> tuple[bytes, str]:
        """把选中内容按当前进制解析成字节，返回 (字节, 错误信息)。"""
        text = self.selected_text().strip()
        if not text:
            return b"", ""
        try:
            data, _warnings = codec.text_to_bytes_ex(
                text, self.radix(), self.dec_width(), self.byte_order()
            )
            return data, ""
        except codec.CodecError as exc:
            return b"", str(exc)

    def _copy_selection(self, radix: str) -> None:
        data, error = self.selection_bytes()
        if error:
            self.set_info(f"选的内容按当前进制解析不了：{error}", ok=False)
            return
        if not data:
            data, _error = self.to_bytes_ex()      # 没选就复制全部
        copy_to_clipboard(codec.bytes_to_text(data, radix, sep=" "))
        self.set_info(f"已复制 {len(data)} 字节（{codec.RADIX_LABELS.get(radix, radix)}）")

    def _update_selection_info(self) -> None:
        text = self.selected_text()
        if not text.strip():
            # 没有选区时只说"全部多少字节"：用缓存/解析结果，别为这一行字整篇解码
            self.selection_label.setText(
                f"未选中　│　全部输入 {self.total_bytes()} 字节　（选中后可看字节数与各进制取值）"
            )
            self.bytes_selected.emit(0, 0)
            return
        data, error = self.selection_bytes()
        chars = len(text)
        start, length = self._selection_byte_range()
        if not error and length:
            self.bytes_selected.emit(start, length)      # 反查：让表格跟着定位
        if error or not data:
            self.selection_label.setText(
                f"选中 {chars} 个字符　│　按 {codec.RADIX_LABELS.get(self.radix(), self.radix())} "
                f"解析不出字节{('：' + error) if error else ''}"
            )
            return
        def cut(text_value: str, limit: int = 96) -> str:
            return text_value if len(text_value) <= limit else text_value[:limit] + " …"
        hex_text = cut(codec.bytes_to_text(data, codec.RADIX_HEX))
        dec_text = cut(codec.bytes_to_text(data, codec.RADIX_DEC))
        bin_text = cut(codec.bytes_to_text(data, codec.RADIX_BIN), 64)
        ascii_text = cut(codec.to_printable(data.decode("utf-8", errors="replace")), 48)
        self.selection_label.setText(
            f"选中 {chars} 字符 = {len(data)} 字节　│　"
            f"HEX: {hex_text}　│　DEC: {dec_text}　│　BIN: {bin_text}　│　ASCII: {ascii_text}"
        )

    # ------------------------------------------------------------------ #
    def _emit_changed(self) -> None:
        radix = self.radix()
        self.dec_width_combo.setEnabled(radix in (codec.RADIX_AUTO, codec.RADIX_DEC))
        self.changed.emit()

    def raw_text(self) -> str:
        """当前文本框里的内容（分页模式下就是当前页）。"""
        return self.editor.toPlainText()

    def set_raw_text(self, text: str) -> None:
        if getattr(self, "_source", None) is not None:
            self.clear_source()          # 手输/粘贴 → 退出分页模式
        self.editor.setPlainText(text)

    # ------------------------------------------------------------------ #
    # 分页模式：超大文件只显示一页（不把几十万行塞进编辑器）
    # ------------------------------------------------------------------ #
    def set_source(self, source) -> None:
        """切到"大文件分页"模式：编辑器只放一页，其余按需从文件读。"""
        self._source = source
        self._page = 0
        self._pager.setVisible(True)
        self.editor.setReadOnly(True)
        self.editor.setPlaceholderText("")
        self.show_page(0)
        self.selection_label.setText(
            f"大文件分页模式：共 {source.line_count:,} 行 / {source.file_size / 1048576:.1f} MB，"
            "编辑器只显示当前页（内存里不驻留全文；点表格里的值会自动翻到对应页并高亮）"
        )

    def clear_source(self) -> None:
        if getattr(self, "_source", None) is None:
            return
        self._source = None
        self._pager.setVisible(False)
        self.editor.setReadOnly(False)
        self.source_exited.emit()

    @property
    def source(self):
        return getattr(self, "_source", None)

    @property
    def page(self) -> int:
        return int(getattr(self, "_page", 0))

    def page_count(self) -> int:
        source = self.source
        if source is None:
            return 1
        return max(1, (source.line_count + PAGE_LINES - 1) // PAGE_LINES)

    def show_page(self, page: int) -> None:
        """翻到第 page 页（0 起）。"""
        source = self.source
        if source is None:
            return
        page = max(0, min(int(page), self.page_count() - 1))
        self._page = page
        start = page * PAGE_LINES
        stop = min(source.line_count, start + PAGE_LINES)
        self.editor.setPlainText(source.window_text(start, PAGE_LINES))
        self.page_label.setText(
            f"第 {start + 1} ~ {stop} 行 / 共 {source.line_count:,} 行"
            f"（第 {page + 1}/{self.page_count()} 页）"
        )
        self.page_spin.setRange(1, self.page_count())
        self.page_spin.setValue(page + 1)
        self.page_prev.setEnabled(page > 0)
        self.page_next.setEnabled(page < self.page_count() - 1)
        self._update_selection_info()

    def goto_page(self, page: int) -> None:
        self.show_page(int(page) - 1)

    def goto_line(self, line: int) -> None:
        source = self.source
        if source is None:
            return
        self.show_page(max(0, int(line) - 1) // PAGE_LINES)

    def set_spans(self, spans: list) -> None:
        """由解析管线给出的"每行文本 ↔ 每行字节"对应表。"""
        self._spans = list(spans or [])
        if not self._spans:
            self.selection_label.setToolTip("输入不是按行解析的，无法把表格里的值定位回原文")

    def _span_for(self, byte_offset: int):
        spans = getattr(self, "_spans", None)
        if not spans:
            return None
        low, high = 0, len(spans) - 1
        while low <= high:                      # 按 byte_start 二分
            mid = (low + high) // 2
            span = spans[mid]
            if byte_offset < span.byte_start:
                high = mid - 1
            elif byte_offset >= span.byte_end:
                low = mid + 1
            else:
                return span
        return None

    def _span_line_text(self, span):
        """取 span 所在那一行的文本（只取一行，不整篇 toPlainText）。

        128 MB 的输入里 `toPlainText()` 一次要 700 ms 左右，而点一个格子要定位两次 ——
        直接用 QTextDocument 的块随机访问（O(log n)）只取那一行，就几乎不花时间了。
        返回 (行文本, span 起点在行内的偏移)；拿不到块时退回整篇切片。
        """
        doc = self.editor.document()
        block = doc.findBlock(int(span.text_start))
        if block.isValid():
            inside = int(span.text_start) - int(block.position())
            if 0 <= inside <= len(block.text()):
                return block.text()[inside:inside + span.text_len], inside
        text = self.raw_text()
        return text[span.text_start:span.text_start + span.text_len], 0

    def _byte_char_range(self, span, radix: str, byte_offset: int):
        """span 里第 k 个字节对应原文的 [起始字符, 结束字符)。"""
        if span is None or not span.contains(byte_offset):
            return None
        text, _inside = self._span_line_text(span)
        base = span.text_start
        k = byte_offset - span.byte_start
        if radix == codec.RADIX_ASCII:
            return base + k, base + k + 1
        if radix in (codec.RADIX_DEC, codec.RADIX_BIN):
            return base, base + span.text_len      # 一个数值可能占多个字节，整行选中
        for match in _HEX_RUN_RE.finditer(text):   # HEX：精确定位到那两位十六进制
            digits = match.group(1)
            count = (len(digits) + 1) // 2
            if k < count:
                start, end = match.start(1), match.end(1)
                if len(digits) % 2:                # 奇数位会补一个前导 0
                    return base + start + (0 if k == 0 else 2 * k - 1), \
                           base + start + (1 if k == 0 else min(len(digits), 2 * k + 1))
                return base + start + 2 * k, base + start + min(len(digits), 2 * k + 2)
            k -= count
        return base, base + span.text_len

    def highlight_bytes(self, start: int, length: int) -> bool:
        """在输入框里反色选中第 start 字节起的 length 个字节，并滚动到可见。"""
        if length <= 0:
            return False
        if self.source is not None:
            return self._highlight_paged(start, length)
        if not getattr(self, "_spans", None):
            return False
        radix = self.effective_radix()
        first = self._span_for(start)
        last = self._span_for(start + length - 1)
        if first is None or last is None:
            return False
        begin = self._byte_char_range(first, radix, start)
        finish = self._byte_char_range(last, radix, start + length - 1)
        if begin is None or finish is None:
            return False
        cursor = QTextCursor(self.editor.document())
        cursor.setPosition(begin[0])
        cursor.setPosition(max(finish[1], begin[1]), QTextCursor.MoveMode.KeepAnchor)
        self.editor.setTextCursor(cursor)
        self.editor.ensureCursorVisible()
        return True

    # ---------------------- 分页模式下的定位 ---------------------- #
    def _page_span(self, line_index: int):
        """把"第几行"包装成一个 LineSpan（text_start 用当前页内的字符位置）。"""
        source = self.source
        if source is None or not source.line_count:
            return None
        in_page = int(line_index) - self.page * PAGE_LINES
        if in_page < 0:
            return None
        block = self.editor.document().findBlockByNumber(in_page)
        if not block.isValid():
            return None
        byte_start, byte_count = source.byte_range_of_line(line_index)
        return codec.LineSpan(byte_start, byte_count, int(block.position()),
                              len(block.text()))

    def _highlight_paged(self, start: int, length: int) -> bool:
        source = self.source
        first_line = source.line_of_byte(start)
        page = first_line // PAGE_LINES
        if page != self.page:
            self.show_page(page)
        radix = self.effective_radix()
        first = self._page_span(first_line)
        if first is None:
            return False
        begin = self._byte_char_range(first, radix, start)
        if begin is None:
            return False
        end_line = source.line_of_byte(start + length - 1)
        document = self.editor.document()
        if end_line // PAGE_LINES == page:
            last = self._page_span(end_line)
            finish = self._byte_char_range(last, radix, start + length - 1) if last else None
        else:                                   # 跨页：选到本页末尾就好
            finish = (document.characterCount() - 1, document.characterCount() - 1)
        end = finish[1] if finish else begin[1]
        cursor = QTextCursor(document)
        cursor.setPosition(begin[0])
        cursor.setPosition(max(end, begin[1]), QTextCursor.MoveMode.KeepAnchor)
        self.editor.setTextCursor(cursor)
        self.editor.ensureCursorVisible()
        return True

    def _selection_byte_range(self) -> tuple[int, int]:
        """当前选区覆盖的 (起始字节, 字节数)。

        先二分找到选区首尾落在哪一行（别遍历几万条 spans），再按行内字符位置算字节下标。
        分页模式下走 `_selection_byte_range_paged()`。
        """
        if self.source is not None:
            return self._selection_byte_range_paged()
        spans = getattr(self, "_spans", None)
        cursor = self.editor.textCursor()
        if not spans or not cursor.hasSelection():
            return (0, 0)
        left, right = cursor.selectionStart(), cursor.selectionEnd()
        first = self._span_for_text_pos(left) or self._span_for_text_pos(left + 1)
        last = self._span_for_text_pos(right - 1) or self._span_for_text_pos(right - 2)
        if first is None or last is None:
            return (0, 0)
        radix = self.effective_radix()
        if radix in (codec.RADIX_DEC, codec.RADIX_BIN):
            return first.byte_start, max(1, last.byte_end - first.byte_start)
        begin = first.byte_start + self._byte_index_at(first, left)
        end = last.byte_start + self._byte_index_at(last, right - 1)
        return begin, max(1, end - begin + 1)

    def _selection_byte_range_paged(self) -> tuple[int, int]:
        """分页模式下：光标所在行（页内块号）→ 全局行号 → 字节范围。"""
        source = self.source
        cursor = self.editor.textCursor()
        if source is None or not cursor.hasSelection():
            return (0, 0)
        document = self.editor.document()
        left, right = cursor.selectionStart(), cursor.selectionEnd()
        first_line = self.page * PAGE_LINES + document.findBlock(left).blockNumber()
        last_line = self.page * PAGE_LINES + document.findBlock(max(0, right - 1)).blockNumber()
        radix = self.effective_radix()
        first_start, first_count = source.byte_range_of_line(first_line)
        last_start, last_count = source.byte_range_of_line(last_line)
        if radix in (codec.RADIX_DEC, codec.RADIX_BIN):
            return first_start, max(1, last_start + last_count - first_start)
        begin = first_start + self._byte_index_in_line(first_line, left)
        end = last_start + self._byte_index_in_line(last_line, right - 1)
        return begin, max(1, end - begin + 1)

    def _byte_index_in_line(self, line_index: int, char_pos: int) -> int:
        """某一行的原文位置 char_pos 对应行内第几个字节。"""
        source = self.source
        radix = self.effective_radix()
        block = self.editor.document().findBlockByNumber(
            int(line_index) - self.page * PAGE_LINES
        )
        if not block.isValid():
            return 0
        text = block.text()
        if radix == codec.RADIX_ASCII:
            return max(0, min(char_pos - int(block.position()), len(text) - 1))
        return self._byte_index_in_text(text, char_pos - int(block.position()))

    def _byte_index_in_text(self, text: str, limit: int) -> int:
        """行内第 limit 个字符处是第几个字节（数它前面出现过多少个十六进制数字）。"""
        limit = max(0, min(int(limit), len(text)))
        digits = 0
        for match in _HEX_RUN_RE.finditer(text):
            if match.start(1) >= limit:
                break
            digits += max(0, min(limit - match.start(1), len(match.group(1))))
        if not digits:
            return 0
        on_digit = bool(re.match(r"[0-9a-fA-F]", text[limit:limit + 1] or ""))
        return digits // 2 if on_digit else (digits - 1) // 2

    def _span_for_text_pos(self, char_pos: int):
        """原文第 char_pos 个字符落在哪一行（spans 按 text_start 有序，二分）。"""
        spans = getattr(self, "_spans", None)
        if not spans or char_pos < 0:
            return None
        low, high = 0, len(spans) - 1
        while low <= high:
            mid = (low + high) // 2
            span = spans[mid]
            if char_pos < span.text_start:
                high = mid - 1
            elif char_pos >= span.text_start + span.text_len:
                low = mid + 1
            else:
                return span
        return None

    def _byte_index_at(self, span, char_pos: int) -> int:
        """span 内、原文位置 char_pos 处是第几个字节（相对 span 起点的下标）。

        按"这个位置之前出现过多少个十六进制数字"算：位置正好落在某位数字上 → 就是那一字节，
        落在分隔符/行尾上 → 归给它前面那个字节。
        """
        radix = self.effective_radix()
        if radix == codec.RADIX_ASCII:
            return max(0, min(char_pos - span.text_start, span.byte_count - 1))
        text, _inside = self._span_line_text(span)
        return self._byte_index_in_text(text, char_pos - span.text_start)

    def radix(self) -> str:
        return self.radix_combo.currentData()

    def set_radix(self, radix: str) -> None:
        index = self.radix_combo.findData(radix)
        if index >= 0:
            self.radix_combo.setCurrentIndex(index)

    def dec_width(self) -> int:
        return int(self.dec_width_combo.currentData() or 0)

    def set_dec_width(self, width: int) -> None:
        index = self.dec_width_combo.findData(int(width))
        if index >= 0:
            self.dec_width_combo.setCurrentIndex(index)

    def byte_order(self) -> str:
        return self.order_combo.currentData()

    def set_byte_order(self, order: str) -> None:
        index = self.order_combo.findData(order)
        if index >= 0:
            self.order_combo.setCurrentIndex(index)

    def effective_radix(self) -> str:
        """当前输入实际用的进制。

        自动识别要扫一遍输入，128 MB 的抓包要 600 ms —— 解析完成后主窗口会把结果
        `set_parsed_info()` 塞进来（大文件来源自己也知道），之后这里不必再扫。
        """
        source = self.source
        if source is not None and getattr(source, "radix_used", ""):
            return source.radix_used
        if self._radix_cache is not None:
            return self._radix_cache
        if source is not None:
            return codec.resolve_radix(source.sample_text(), self.radix())
        return codec.resolve_radix(self.raw_text(), self.radix())

    def to_bytes(self) -> bytes:
        return self.to_bytes_ex()[0]

    def to_bytes_ex(self) -> tuple[bytes, list[str]]:
        """返回 (字节, 警告)。坏行（注释行、表头行等）会被跳过并进入警告列表。"""
        source = self.source
        if source is not None:
            return source.decode_bytes(0, source.total_bytes), list(source.build_warnings)
        return codec.text_to_bytes_ex(
            self.raw_text(), self.radix(), self.dec_width(), self.byte_order()
        )

    def total_bytes(self) -> int:
        """输入的字节数：解析过就直接用解析结果（不再整篇解码）。"""
        if self._total_bytes is not None:
            return self._total_bytes
        source = self.source
        if source is not None:
            return int(source.total_bytes)
        return len(self.to_bytes())

    def set_parsed_info(self, total_bytes: int, radix_used: str = "") -> None:
        """解析管线算完后回填：省掉"为了显示几个数字再整篇解码一遍"。"""
        self._total_bytes = max(0, int(total_bytes))
        if radix_used:
            self._radix_cache = radix_used

    def _invalidate_cache(self) -> None:
        self._total_bytes = None
        self._radix_cache = None

    def input_settings(self) -> dict:
        return {
            "radix": self.radix(),
            "dec_width": self.dec_width(),
            "byte_order": self.byte_order(),
        }

    def apply_settings(self, settings: dict) -> None:
        settings = settings or {}
        if settings.get("radix"):
            self.set_radix(settings["radix"])
        if settings.get("dec_width") is not None:
            self.set_dec_width(int(settings["dec_width"]))
        if settings.get("byte_order"):
            self.set_byte_order(settings["byte_order"])

    def set_info(self, text: str, ok: bool = True) -> None:
        self.info_label.setText(text)
        self.info_label.setStyleSheet(
            f"color:{theme.FG};" if ok else f"color:{theme.FAIL_FG}; font-weight:bold;"
        )


class FrameSettingsPanel(QWidget):
    """分帧方式 + 参数 + 校验设置。"""

    changed = pyqtSignal()
    #: 请求"按当前输入数据自动判断帧格式"（由主窗口实现，因为要用到输入面板的内容）
    smart_requested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        self.mode_combo = QComboBox()
        for key, label in framer.MODE_ITEMS:
            self.mode_combo.addItem(label, key)
        self.skip_spin = QSpinBox()
        self.skip_spin.setRange(0, 1 << 20)
        self.skip_spin.setToolTip("分帧前先跳过这么多字节（抓包文件前面常有时间戳/前导数据）")
        self.smart_button = QPushButton("智能识别帧格式")
        self.smart_button.setToolTip(
            "按输入数据自动判断：\n"
            "· 一行一帧的十六进制 → 直接按每行字节数设定长帧\n"
            "· 行长不一 / 文本行 → 改成按换行分帧\n"
            "· 否则用自相关找帧长与帧头"
        )
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("分帧方式"))
        mode_row.addWidget(self.mode_combo, 1)
        mode_row.addWidget(self.smart_button)
        layout.addLayout(mode_row)

        skip_row = QHBoxLayout()
        skip_row.addWidget(QLabel("跳过前"))
        skip_row.addWidget(self.skip_spin)
        skip_row.addWidget(QLabel("字节"))
        skip_row.addWidget(hint_label("抓包文件前面常有时间戳/前导数据，先跳过再分帧"))
        skip_row.addStretch(1)
        layout.addLayout(skip_row)

        # 帧数上限：0 = 不限（128 MB 抓包约 28 万帧也照分）
        self.max_frames_spin = QSpinBox()
        self.max_frames_spin.setRange(0, 5000000)
        self.max_frames_spin.setSingleStep(10000)
        self.max_frames_spin.setValue(0)
        self.max_frames_spin.setSpecialValueText("不限")
        self.max_frames_spin.setSuffix(" 帧")
        self.max_frames_spin.setToolTip(
            "单次最多分出多少帧：\n"
            "· 0（不限）= 有多少帧就分多少帧，默认如此\n"
            "· 想快速预览超大文件时可以设个上限（比如 20000），分到这里就停，"
            "省内存也更快；达到上限时日志里会明确提示"
        )
        limit_row = QHBoxLayout()
        limit_row.addWidget(QLabel("最多分"))
        limit_row.addWidget(self.max_frames_spin)
        limit_row.addWidget(hint_label(
            "0 = 不限；设上限只影响分帧多少，不影响已分出帧的解析。"
            "右侧各页签的表格都是滑动窗口，不再限制显示多少帧"
        ))
        limit_row.addStretch(1)
        layout.addLayout(limit_row)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._page_header())
        self.stack.addWidget(self._page_fixed())
        self.stack.addWidget(self._page_lenprefix())
        self.stack.addWidget(self._page_delimiter())
        self.stack.addWidget(self._page_tlv())
        self.stack.addWidget(self._page_whole())
        layout.addWidget(self.stack)

        layout.addWidget(self._checksum_group())
        layout.addStretch(1)

        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        for widget in (
            self.header_edit, self.delimiter_edit,
        ):
            widget.textChanged.connect(self.changed)
        for spin in (
            self.len_offset_spin, self.fixed_len_spin, self.tag_size_spin, self.skip_spin,
            self.max_frames_spin,
        ):
            spin.valueChanged.connect(self.changed)
        for combo in (
            self.len_size_combo, self.len_order_combo, self.prefix_size_combo,
            self.prefix_order_combo, self.tlv_len_size_combo, self.tlv_len_order_combo,
            self.check_algo_combo, self.check_order_combo, self.check_where_combo,
        ):
            combo.currentIndexChanged.connect(self.changed)
        for box in (
            self.len_has_header_box, self.len_has_check_box, self.prefix_has_self_box,
            self.include_delimiter_box,
        ):
            box.toggled.connect(self.changed)
        self.check_algo_combo.currentIndexChanged.connect(self._on_algo_changed)
        self.smart_button.clicked.connect(self.smart_requested)

        self._on_mode_changed()

    # ------------------------------------------------------------------ #
    # 各分帧方式的参数页
    # ------------------------------------------------------------------ #
    def _page_header(self) -> QWidget:
        page = QGroupBox("帧头 + 长度字段参数")
        form = QFormLayout(page)
        self.header_edit = QLineEdit("AA 55")
        self.header_edit.setPlaceholderText("十六进制，如 AA 55；留空表示不要求帧头")
        form.addRow("帧头", self.header_edit)

        self.len_offset_spin = QSpinBox()
        self.len_offset_spin.setRange(0, 64)
        self.len_offset_spin.setValue(2)
        self.len_offset_spin.setToolTip("长度字段相对帧首（帧头第一个字节）的偏移")
        form.addRow("长度字段偏移", self.len_offset_spin)

        self.len_size_combo = QComboBox()
        for size in (1, 2, 4, 8):
            self.len_size_combo.addItem(f"{size} 字节", size)
        self.len_size_combo.setCurrentIndex(1)
        form.addRow("长度字段长度", self.len_size_combo)

        self.len_order_combo = QComboBox()
        for key, label in codec.BYTE_ORDER_ITEMS:
            self.len_order_combo.addItem(label, key)
        form.addRow("长度字节序", self.len_order_combo)

        self.len_has_header_box = QCheckBox("长度值包含帧头自身")
        self.len_has_header_box.setChecked(True)
        form.addRow("", self.len_has_header_box)

        self.len_has_check_box = QCheckBox("长度值包含校验字段")
        self.len_has_check_box.setChecked(True)
        form.addRow("", self.len_has_check_box)
        form.addRow("", hint_label(
            "例：帧长 = 帧头(2) + 长度字段(2) + 数据(7) + CRC(2) = 13 时，"
            "两项都勾选，长度字段填 13。"
        ))
        return page

    def _page_fixed(self) -> QWidget:
        page = QGroupBox("定长帧参数")
        form = QFormLayout(page)
        self.fixed_len_spin = QSpinBox()
        self.fixed_len_spin.setRange(1, 65535)
        self.fixed_len_spin.setValue(10)
        form.addRow("每帧字节数", self.fixed_len_spin)
        form.addRow("", hint_label("按固定长度切分；最后不足一帧的部分会标为「尾部残帧」。"))
        return page

    def _page_lenprefix(self) -> QWidget:
        page = QGroupBox("长度前缀参数")
        form = QFormLayout(page)
        self.prefix_size_combo = QComboBox()
        for size in (1, 2, 4):
            self.prefix_size_combo.addItem(f"{size} 字节", size)
        self.prefix_size_combo.setCurrentIndex(1)
        form.addRow("前缀字节数", self.prefix_size_combo)
        self.prefix_order_combo = QComboBox()
        for key, label in codec.BYTE_ORDER_ITEMS:
            self.prefix_order_combo.addItem(label, key)
        form.addRow("前缀字节序", self.prefix_order_combo)
        self.prefix_has_self_box = QCheckBox("长度值包含前缀自身")
        form.addRow("", self.prefix_has_self_box)
        return page

    def _page_delimiter(self) -> QWidget:
        page = QGroupBox("分隔符参数")
        form = QFormLayout(page)
        self.delimiter_edit = QLineEdit("0D 0A")
        self.delimiter_edit.setPlaceholderText("十六进制，如 0D 0A（CRLF）、7E")
        form.addRow("分隔符", self.delimiter_edit)
        self.include_delimiter_box = QCheckBox("分隔符算在帧内")
        self.include_delimiter_box.setChecked(True)
        form.addRow("", self.include_delimiter_box)
        return page

    def _page_tlv(self) -> QWidget:
        page = QGroupBox("TLV 参数")
        form = QFormLayout(page)
        self.tag_size_spin = QSpinBox()
        self.tag_size_spin.setRange(1, 8)
        self.tag_size_spin.setValue(1)
        form.addRow("Tag 字节数", self.tag_size_spin)
        self.tlv_len_size_combo = QComboBox()
        for size in (1, 2, 4):
            self.tlv_len_size_combo.addItem(f"{size} 字节", size)
        form.addRow("Len 字节数", self.tlv_len_size_combo)
        self.tlv_len_order_combo = QComboBox()
        for key, label in codec.BYTE_ORDER_ITEMS:
            self.tlv_len_order_combo.addItem(label, key)
        form.addRow("Len 字节序", self.tlv_len_order_combo)
        form.addRow("", hint_label("每个 TLV 会拆成一帧，Tag 显示在「帧列表」的 Tag 列。"))
        return page

    def _page_whole(self) -> QWidget:
        page = QGroupBox("整包模式")
        form = QFormLayout(page)
        form.addRow("", hint_label("不拆分，整段数据当作一帧；仍可做帧尾校验。"))
        return page

    def _checksum_group(self) -> QGroupBox:
        group = QGroupBox("校验")
        form = QFormLayout(group)
        self.check_algo_combo = QComboBox()
        for key, label, _size, _order in checksum.ALGOS:
            self.check_algo_combo.addItem(label, key)
        form.addRow("算法", self.check_algo_combo)
        self.check_order_combo = QComboBox()
        for key, label in codec.BYTE_ORDER_ITEMS:
            self.check_order_combo.addItem(label, key)
        form.addRow("校验字节序", self.check_order_combo)
        self.check_where_combo = QComboBox()
        for key, label in framer.CHECK_WHERE_ITEMS:
            self.check_where_combo.addItem(label, key)
        form.addRow("校验位置", self.check_where_combo)
        form.addRow("", hint_label("校验失败/通过的结果显示在「帧列表」的校验列；也可整列改颜色。"))
        return group

    # ------------------------------------------------------------------ #
    def _on_mode_changed(self) -> None:
        mode = self.mode_combo.currentData()
        index = {key: i for i, (key, _label) in enumerate(framer.MODE_ITEMS)}.get(mode, 0)
        self.stack.setCurrentIndex(index)
        self.check_where_combo.setEnabled(mode == framer.MODE_HEADER)
        self.changed.emit()

    def _on_algo_changed(self) -> None:
        algo = self.check_algo_combo.currentData()
        order = checksum.algo_default_order(algo)
        found = self.check_order_combo.findData(order)
        if found >= 0:
            self.check_order_combo.setCurrentIndex(found)
        self.check_order_combo.setEnabled(algo not in ("", "none"))

    # ------------------------------------------------------------------ #
    def config(self) -> framer.FrameConfig:
        return framer.FrameConfig(
            mode=self.mode_combo.currentData(),
            fixed_len=self.fixed_len_spin.value(),
            header_hex=self.header_edit.text(),
            len_offset=self.len_offset_spin.value(),
            len_size=int(self.len_size_combo.currentData()),
            len_byte_order=self.len_order_combo.currentData(),
            len_includes_header=self.len_has_header_box.isChecked(),
            len_includes_checksum=self.len_has_check_box.isChecked(),
            checksum_algo=self.check_algo_combo.currentData(),
            checksum_byte_order=self.check_order_combo.currentData(),
            checksum_where=self.check_where_combo.currentData(),
            delimiter_hex=self.delimiter_edit.text(),
            include_delimiter=self.include_delimiter_box.isChecked(),
            prefix_size=int(self.prefix_size_combo.currentData()),
            prefix_byte_order=self.prefix_order_combo.currentData(),
            prefix_includes_self=self.prefix_has_self_box.isChecked(),
            tag_size=self.tag_size_spin.value(),
            value_len_size=int(self.tlv_len_size_combo.currentData()),
            value_len_byte_order=self.tlv_len_order_combo.currentData(),
            skip_bytes=self.skip_spin.value(),
            max_frames=self.max_frames_spin.value(),
        )

    def set_config(self, cfg: framer.FrameConfig) -> None:
        blocked = self.blockSignals(True)
        try:
            index = self.mode_combo.findData(cfg.mode)
            if index >= 0:
                self.mode_combo.setCurrentIndex(index)
            self._on_mode_changed()
            self.fixed_len_spin.setValue(int(cfg.fixed_len))
            self.header_edit.setText(cfg.header_hex)
            self.len_offset_spin.setValue(int(cfg.len_offset))
            self.len_size_combo.setCurrentIndex(max(0, self.len_size_combo.findData(int(cfg.len_size))))
            self.len_order_combo.setCurrentIndex(max(0, self.len_order_combo.findData(cfg.len_byte_order)))
            self.len_has_header_box.setChecked(bool(cfg.len_includes_header))
            self.len_has_check_box.setChecked(bool(cfg.len_includes_checksum))
            self.check_algo_combo.setCurrentIndex(max(0, self.check_algo_combo.findData(cfg.checksum_algo)))
            self.check_order_combo.setCurrentIndex(max(0, self.check_order_combo.findData(cfg.checksum_byte_order)))
            self.check_where_combo.setCurrentIndex(max(0, self.check_where_combo.findData(cfg.checksum_where)))
            self.delimiter_edit.setText(cfg.delimiter_hex)
            self.include_delimiter_box.setChecked(bool(cfg.include_delimiter))
            self.prefix_size_combo.setCurrentIndex(max(0, self.prefix_size_combo.findData(int(cfg.prefix_size))))
            self.prefix_order_combo.setCurrentIndex(max(0, self.prefix_order_combo.findData(cfg.prefix_byte_order)))
            self.prefix_has_self_box.setChecked(bool(cfg.prefix_includes_self))
            self.tag_size_spin.setValue(int(cfg.tag_size))
            self.tlv_len_size_combo.setCurrentIndex(max(0, self.tlv_len_size_combo.findData(int(cfg.value_len_size))))
            self.tlv_len_order_combo.setCurrentIndex(max(0, self.tlv_len_order_combo.findData(cfg.value_len_byte_order)))
            self.skip_spin.setValue(int(cfg.skip_bytes))
            self.max_frames_spin.setValue(int(cfg.max_frames))
        finally:
            self.blockSignals(blocked)
        self._on_algo_changed()
        self._on_mode_changed()
