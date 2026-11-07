"""分阶段流式解析管线。

为什么要这么写
--------------
一次解析可能是「12663 帧 × 8 字段 + 上万行表格」。同步整包做完，界面会白屏卡住；
而选项一变就整包重算更浪费——列的显示格式/颜色/换算函数只需要重绘，
字段定义只需要重算字段，只有分帧设置变了才需要重新分帧。

所以：
1. **脏标记定起点**：`Stage.DECODE < FRAME < FIELDS < RENDER`，谁变了就从谁开始，
   下游复用缓存（`ParseState`）；
2. **时间片推进**：主循环是个生成器，每处理一小块就让出一次，由 QTimer 按预算
   （默认 12ms）继续拉取，界面始终保持响应；
3. **解析与渲染同片**：一块帧解析完立刻发信号给表格追加行 —— 表是"长出来"的，
   而不是等全部算完再一次性塞进去；
4. **可抢占**：新请求进来直接关掉旧生成器（generation 计数），不做无用功。

对外两种跑法：
    submit(req, streaming=False) / run_to_completion()   —— 一次跑完（自检、脚本）
    submit(req, streaming=True)                          —— 交给 QTimer 分片跑（界面）
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from . import codec, field_parser, framer, rows as rows_mod
from .field_parser import FieldDef, ParsedField

#: 每片处理多少帧（解析 + 渲染同一个片）
CHUNK_FRAMES = 200
#: 默认每片时间预算（毫秒）
DEFAULT_BUDGET_MS = 12


class Stage(IntEnum):
    """脏标记层级：数字越小，要重算的东西越多。"""

    DECODE = 1      # 输入数据 / 进制变了
    FRAME = 2       # 分帧设置变了
    FIELDS = 3      # 字段定义变了
    RENDER = 4      # 只看显示：筛选、显示上限、列样式


STAGE_LABELS = {
    Stage.DECODE: "解码输入",
    Stage.FRAME: "分帧",
    Stage.FIELDS: "解析字段",
    Stage.RENDER: "渲染结果",
}


@dataclass
class ParseRequest:
    """一次解析请求。"""

    text: str = ""
    data: bytes = b""              # 已经解码好的字节（给了就不再解码文本）
    #: 大文件用：按行随机访问的文本来源（app/source.py），走"流式解码 + 索引分帧"
    source: object | None = None
    radix: str = codec.RADIX_AUTO
    dec_width: int = 0
    byte_order: str = "big"
    config: framer.FrameConfig = field(default_factory=framer.FrameConfig)
    fields: list[FieldDef] = field(default_factory=list)
    frame_limit: int = 200         # 字段表最多解析/建行多少帧
    render_limit: int = 3000       # 帧列表最多渲染多少帧


@dataclass
class ParseState:
    """管线缓存：支持"只重算下游"。"""

    data: bytes = b""
    radix_used: str = ""
    warnings: list[str] = field(default_factory=list)
    split: framer.SplitResult | None = None
    parsed: dict[int, list[ParsedField]] = field(default_factory=dict)
    summaries: dict[int, str] = field(default_factory=dict)
    rows_by_frame: dict[int, list[dict]] = field(default_factory=dict)
    problems: list[tuple[int, str, str]] = field(default_factory=list)
    fields: list[FieldDef] = field(default_factory=list)
    config: framer.FrameConfig | None = None
    frame_limit: int = 0
    elapsed_ms: float = 0.0
    #: 每行文本 ↔ 每行字节 的对应表（点表格里的值可跳回左侧原文）
    spans: list = field(default_factory=list)
    stages_done: set[int] = field(default_factory=set)
    #: 大文件路径：按行随机访问的文本来源（分页显示、按需取字节）
    source: object | None = None
    #: 真实的输入字节数（source 路径下 data 只是按需视图，长度要靠它）
    data_size: int = 0


class ParsePipeline(QObject):
    """把「解码 → 分帧 → 解析字段 → 渲染」跑成流式的调度器。"""

    stage_changed = pyqtSignal(str)                 # 当前阶段名（'' = 空闲）
    progress = pyqtSignal(int, int)                 # 当前阶段 已完成 / 总数
    frames_ready = pyqtSignal(object)               # framer.SplitResult
    chunk_ready = pyqtSignal(object, object, object)  # 帧块, 摘要表, 字段行
    finished = pyqtSignal(object)                   # ParseState
    failed = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.state = ParseState()
        self._timer = QTimer(self)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._tick)
        self._generator = None
        self._generation = 0
        self._budget_ms = DEFAULT_BUDGET_MS
        self._ticks = 0
        self.running = False

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def submit(self, request: ParseRequest, stage: int = Stage.DECODE,
               streaming: bool = True, budget_ms: int = DEFAULT_BUDGET_MS) -> int:
        """提交一次解析。会自动取消上一个未完成的任务。"""
        self._generation += 1
        self._timer.stop()
        self._close_generator()
        self._budget_ms = max(1, int(budget_ms))
        self._ticks = 0
        self.running = True
        generation = self._generation
        self._generator = self._run(request, int(stage), generation)
        if streaming:
            self._timer.start()
        else:
            self.run_to_completion()
        return generation

    def run_to_completion(self) -> ParseState:
        """一次跑完（阻塞）。自检与脚本用。"""
        if self._generator is None:
            return self.state
        while True:
            try:
                next(self._generator)
            except StopIteration:
                break
            except Exception as exc:  # noqa: BLE001
                self.failed.emit(str(exc))
                break
        self._finish()
        return self.state

    def cancel(self) -> None:
        self._timer.stop()
        self._close_generator()
        self.running = False
        self.stage_changed.emit("")

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def ticks(self) -> int:
        """本次跑了多少个时间片（自检里用来确认"真的是流式"）。"""
        return self._ticks

    # ------------------------------------------------------------------ #
    # 时间片调度
    # ------------------------------------------------------------------ #
    def _tick(self) -> None:
        if self._generator is None:
            self._finish()
            return
        self._ticks += 1
        deadline = time.perf_counter() + self._budget_ms / 1000.0
        try:
            while time.perf_counter() < deadline:
                next(self._generator)
        except StopIteration:
            self._finish()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
            self._finish()

    def _close_generator(self) -> None:
        if self._generator is not None:
            close = getattr(self._generator, "close", None)
            if close is not None:
                close()
            self._generator = None

    def _finish(self) -> None:
        self._timer.stop()
        self._generator = None
        if self.running:
            self.running = False
            self.stage_changed.emit("")
            self.finished.emit(self.state)

    # ------------------------------------------------------------------ #
    # 主生成器：每完成一小块就 yield 一次，把控制权交回调度器
    # ------------------------------------------------------------------ #
    def _run(self, request: ParseRequest, stage: int, generation: int):
        started = time.perf_counter()
        state = self.state if stage > Stage.DECODE else ParseState()

        # ---------------- 解码（文本按行分片，边解边出结果） ---------------- #
        from .source import LazyData          # 局部导入，避免模块级循环

        used_source = False
        if stage <= Stage.DECODE or (request.source is not None and stage <= Stage.FRAME):
            if request.source is not None:
                # 大文件：mmap + 流式解码 + 流式分帧，只留索引（不驻留全文/全字节）
                used_source = True
                source = request.source
                state.source = source
                self.stage_changed.emit("读取 + 解码 + 分帧")
                last_progress = [0.0]

                def _progress(done: int, total: int) -> None:
                    now = time.perf_counter()
                    if now - last_progress[0] > 0.05 or done >= total:
                        last_progress[0] = now
                        self.progress.emit(done, total)

                state.config = request.config
                state.split = source.build_index(
                    request.config, request.radix, request.dec_width, request.byte_order,
                    on_progress=_progress,
                )
                state.data = LazyData(source)
                state.data_size = int(source.total_bytes)
                state.radix_used = source.radix_used
                state.spans = []
                state.parsed = {}
                state.summaries = {}
                state.rows_by_frame = {}
                state.problems = []
                state.stages_done.add(int(Stage.DECODE))
                state.stages_done.add(int(Stage.FRAME))
                self.frames_ready.emit(state.split)
                self.progress.emit(1, 1)
                yield None
            elif request.data:
                state.data = request.data
                state.data_size = len(request.data)
                state.radix_used = "binary"
            else:
                text = request.text
                use = codec.resolve_radix(text, request.radix)
                state.radix_used = use
                if use == codec.RADIX_ASCII:
                    state.data = codec.ascii_to_bytes(text)
                    state.spans = [codec.LineSpan(0, len(state.data), 0, len(text))]
                else:
                    raw_lines = text.splitlines(keepends=True)
                    total_lines = max(1, len(raw_lines))
                    step = max(2000, total_lines // 20)
                    # 每行在原文里的起始位置（用于把"字节偏移"换算成"文本位置"）
                    line_starts: list[int] = []
                    offset = 0
                    for raw in raw_lines:
                        line_starts.append(offset)
                        offset += len(raw)
                    out = bytearray()
                    warnings: list[str] = []
                    spans: list = []
                    for start in range(0, total_lines, step):
                        part = "".join(raw_lines[start:start + step])
                        if part.strip():
                            base = len(out)
                            local: list = []
                            chunk, chunk_warnings = codec.text_to_bytes_ex(
                                part, use, request.dec_width, request.byte_order, spans=local
                            )
                            for span in local:      # 换算成整段文本/整段字节的绝对位置
                                span.byte_start += base
                                span.text_start += line_starts[start]
                            out += chunk
                            spans.extend(local)
                            warnings.extend(chunk_warnings)
                        state.data = bytes(out)
                        state.spans = spans
                        state.data_size = len(out)
                        self.progress.emit(min(start + step, total_lines), total_lines)
                        yield None
                    state.warnings = warnings
            state.stages_done.add(int(Stage.DECODE))
            self.progress.emit(1, 1)
            yield None

        # ---------------- 分帧 ---------------- #
        if stage <= Stage.FRAME and not used_source:
            self.stage_changed.emit(STAGE_LABELS[Stage.FRAME])
            state.config = request.config
            state.split = framer.split_frames(state.data, state.config)
            state.parsed = {}
            state.summaries = {}
            state.rows_by_frame = {}
            state.problems = []
            state.stages_done.add(int(Stage.FRAME))
            self.frames_ready.emit(state.split)
            self.progress.emit(1, 1)
            yield None

        split = state.split or framer.SplitResult()

        # ---------------- 解析字段（分片） ---------------- #
        if stage <= Stage.FIELDS:
            self.stage_changed.emit(STAGE_LABELS[Stage.FIELDS])
            state.fields = list(request.fields)
            state.frame_limit = int(request.frame_limit)
            state.parsed = {}
            state.summaries = {}
            state.rows_by_frame = {}
            state.problems = []
            # 帧列表要铺满 render_limit，但字段只解析前 frame_limit 帧（贵的是字段和表格行）
            render_total = min(len(split.frames), max(0, int(request.render_limit)))
            parse_upto = min(render_total, max(0, state.frame_limit))
            # 自适应片长：先给个"保守的小片"（首屏快、第一次停顿短），
            # 之后按上一片实际耗时 ×0.6 / ×1.5 动态收敛到预算
            chunk_frames = max(4, min(CHUNK_FRAMES, 320 // max(1, len(state.fields))))
            budget = self._budget_ms / 1000.0
            start = 0
            while start < render_total:
                if generation != self._generation:
                    return
                end = min(start + chunk_frames, render_total)
                chunk = split.frames[start:end]
                mark = time.perf_counter()
                summaries: dict[int, str] = {}
                rows: list[dict] = []
                for frame in chunk:
                    if frame.index < parse_upto:
                        parsed = field_parser.parse_fields(frame.data, state.fields, frame.index)
                        state.parsed[frame.index] = parsed
                        summary = field_parser.summarize(parsed)
                        state.summaries[frame.index] = summary
                        frame_rows = [
                            rows_mod.field_row(item, fd, frame.offset)
                            for fd, item in zip(state.fields, parsed)
                        ]
                        state.rows_by_frame[frame.index] = frame_rows
                        rows.extend(frame_rows)
                        for item in parsed:
                            if not item.ok:
                                state.problems.append((item.frame_index, item.name, item.note))
                    else:
                        summary = state.summaries.get(frame.index, "")
                    summaries[frame.index] = summary
                state.stages_done.add(int(Stage.FIELDS))
                self.progress.emit(end, render_total)
                self.chunk_ready.emit(chunk, summaries, rows)
                # 这一片实际花掉多少时间，下一片就按它调大/调小
                cost = time.perf_counter() - mark
                if cost > budget * 1.3:
                    chunk_frames = max(4, int(chunk_frames * 0.6))
                elif cost < budget * 0.4:
                    chunk_frames = min(CHUNK_FRAMES,
                                       max(chunk_frames + 4, int(chunk_frames * 1.5)))
                start = end
                yield None

        # ---------------- 只重渲染（复用缓存） ---------------- #
        elif stage <= Stage.RENDER:
            self.stage_changed.emit(STAGE_LABELS[Stage.RENDER])
            total = min(len(split.frames), max(0, int(request.render_limit)))
            for start in range(0, total, CHUNK_FRAMES):
                if generation != self._generation:
                    return
                chunk = split.frames[start:min(start + CHUNK_FRAMES, total)]
                summaries = {f.index: state.summaries.get(f.index, "") for f in chunk}
                rows: list[dict] = []
                for frame in chunk:
                    rows.extend(state.rows_by_frame.get(frame.index, []))
                self.progress.emit(min(start + len(chunk), total), total)
                self.chunk_ready.emit(chunk, summaries, rows)
                yield None

        state.elapsed_ms = (time.perf_counter() - started) * 1000
        self.state = state
