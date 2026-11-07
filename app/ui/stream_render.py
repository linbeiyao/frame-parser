"""把一批行按时间片喂给表格：渐进渲染，避免一次性塞上万行把界面卡住。"""
from __future__ import annotations

import time

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

DEFAULT_CHUNK = 200
DEFAULT_BUDGET_MS = 8


class RowStreamer(QObject):
    """配合 SmartTable.begin_stream/append_rows/end_stream 的分片渲染器。

    典型用法（表格自己持有，重渲染时直接 start 即可）::

        self.streamer.start(self.table, rows)      # 开始（自动清表）
        self.streamer.finished.connect(...)        # 渲染完
    """

    finished = pyqtSignal()
    progress = pyqtSignal(int, int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._tick)
        self._table = None
        self._rows: list = []
        self._index = 0
        self._chunk = DEFAULT_CHUNK
        self._budget_ms = DEFAULT_BUDGET_MS

    # ------------------------------------------------------------------ #
    def start(self, table, rows, chunk: int = DEFAULT_CHUNK,
              budget_ms: int = DEFAULT_BUDGET_MS, reset: bool = True) -> None:
        self.stop()
        self._table = table
        self._rows = list(rows)
        self._index = 0
        self._chunk = max(16, int(chunk))
        self._budget_ms = max(1, int(budget_ms))
        if reset:
            table.begin_stream()
        if not self._rows:
            table.end_stream()
            self.finished.emit()
            return
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def finish_now(self) -> None:
        """不管时间片，直接把剩下的全部塞进去（同步模式 / 退出前）。"""
        self._timer.stop()
        if self._table is None:
            return
        self._flush(rest_all=True)
        self._table.end_stream()
        self.finished.emit()

    @property
    def active(self) -> bool:
        return self._timer.isActive()

    @property
    def done_ratio(self) -> float:
        if not self._rows:
            return 1.0
        return self._index / len(self._rows)

    # ------------------------------------------------------------------ #
    def _tick(self) -> None:
        if self._table is None:
            self.stop()
            return
        deadline = time.perf_counter() + self._budget_ms / 1000.0
        while self._index < len(self._rows) and time.perf_counter() < deadline:
            self._flush()
        if self._index >= len(self._rows):
            self.stop()
            self._table.end_stream()
            self.finished.emit()

    def _flush(self, rest_all: bool = False) -> None:
        if rest_all:
            part = self._rows[self._index:]
        else:
            part = self._rows[self._index:self._index + self._chunk]
        if not part:
            return
        self._table.append_rows(part)
        self._index += len(part)
        self.progress.emit(self._index, len(self._rows))
