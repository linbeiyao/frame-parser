"""帧的"惰性视图"：只存每帧在数据里的位置与状态，字节按需切出来。

为什么要这样
------------
一次抓包可能几十万帧。如果每帧都造一个对象 + 复制一份字节（128 MB 抓包 ≈ 28.7 万帧 ×
156 字节），光这一项就是上百 MB，而且分帧过程本身就要把每一帧都复制一遍。

但界面一屏最多看几千帧，统计/筛选其实只需要「位置、长度、校验状态」。所以：

* 分帧时只记索引（`array('q')` 偏移 + `array('i')` 长度 + `bytearray` 状态）——
  没有校验算法时连切片都不做，分帧几乎零分配；
* 帧字节**随机读取**：`source[i]` 现切现给（`data[offset:offset+size]`），
  最近用过的若干帧做个很小的缓存，避免一屏几千帧反复切；
* 将来换成"文件/分页"数据源时，只要换一个 provider（`__call__(start, count) -> bytes`），
  这里和上层代码都不用改。

对外它就是个序列：`len(source)`、`source[i]`、`for frame in source`、`source[a:b]` 都能用。
"""
from __future__ import annotations

from array import array
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

STATE_OK = "ok"
STATE_FAIL = "fail"
STATE_NA = "na"

STATE_LABELS = {STATE_OK: "通过", STATE_FAIL: "失败", STATE_NA: "—"}

#: 状态在索引里的紧凑编码
_STATE_CODES = {STATE_NA: 0, STATE_OK: 1, STATE_FAIL: 2}
_STATE_NAMES = {0: STATE_NA, 1: STATE_OK, 2: STATE_FAIL}


@dataclass
class Frame:
    """一帧数据。`data` 是这一帧的字节（由 FrameSource 按需切出来）。

    `length` 是索引里记的帧长：正常情况下 `size` 就是 `len(data)`，
    但流式/文件来源还没把字节取出来时，也能靠 `length` 报出正确帧长。
    """

    index: int
    offset: int
    data: bytes = b""
    check_state: str = STATE_NA
    note: str = ""
    tag: str = ""
    length: int = 0

    @property
    def size(self) -> int:
        return len(self.data) or int(self.length)

    @property
    def hex(self) -> str:
        from . import codec

        return codec.bytes_to_text(self.data)


class MemoryProvider:
    """整段字节都在内存里的数据源（粘贴的文本、小文件）。"""

    def __init__(self, data: bytes = b"") -> None:
        self.data = data or b""

    def __call__(self, start: int, count: int) -> bytes:
        return self.data[start:start + count]

    @property
    def size(self) -> int:
        return len(self.data)


class FrameIndexBuilder:
    """分帧过程中"只记索引"的累加器。"""

    def __init__(self) -> None:
        self.offsets = array("q")
        self.sizes = array("i")
        self.states = bytearray()
        self.notes: dict[int, str] = {}
        self.tags: dict[int, str] = {}

    def __len__(self) -> int:
        return len(self.offsets)

    def append(self, offset: int, end: int, state: str = STATE_NA,
               note: str = "", tag: str = "") -> int:
        index = len(self.offsets)
        self.offsets.append(int(offset))
        self.sizes.append(max(0, int(end) - int(offset)))
        self.states.append(_STATE_CODES.get(state, 0))
        if note:
            self.notes[index] = note
        if tag:
            self.tags[index] = tag
        return index

    def extend(self, other: "FrameIndexBuilder", shift: int = 0) -> None:
        base = len(self)
        self.offsets.extend(value + shift for value in other.offsets)
        self.sizes.extend(other.sizes)
        self.states.extend(other.states)
        self.notes.update({base + key: value for key, value in other.notes.items()})
        self.tags.update({base + key: value for key, value in other.tags.items()})

    def shift_offsets(self, delta: int) -> None:
        if delta:
            self.offsets = array("q", (value + delta for value in self.offsets))

    def build(self, provider=None, cache_size: int = 512) -> "FrameSource":
        return FrameSource(provider or MemoryProvider(b""), self.offsets, self.sizes,
                           self.states, self.notes, self.tags, cache_size=cache_size)


class FrameSource(Sequence):
    """帧的惰性序列：索引常驻，字节按需取。"""

    def __init__(self, provider, offsets, sizes, states: bytes | bytearray,
                 notes: dict | None = None, tags: dict | None = None,
                 cache_size: int = 512) -> None:
        self._provider = provider
        self._offsets = offsets
        self._sizes = sizes
        self._states = states
        # 注意用 `is not None`：notes/tags 可能是"暂时还是空、后面还会往里加"的同一个字典，
        # 写成 `notes or {}` 会把空字典换成新字典，之后加进去的备注/标签就丢了。
        self._notes = notes if notes is not None else {}
        self._tags = tags if tags is not None else {}
        self._cache_size = max(0, int(cache_size))
        self._cache: dict[int, bytes] = {}
        self._total_bytes: int | None = None
        self._counts: tuple[int, int] | None = None

    # ------------------------------------------------------------------ #
    # 序列协议
    # ------------------------------------------------------------------ #
    def __len__(self) -> int:
        return len(self._offsets)

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            if step == 1:
                return [self._frame(i) for i in range(start, stop)]
            return [self._frame(i) for i in range(start, stop, step)]
        position = index
        if position < 0:
            position += len(self)
        if not 0 <= position < len(self):
            raise IndexError(f"帧下标越界：{index}（共 {len(self)} 帧）")
        return self._frame(position)

    def __iter__(self) -> Iterator[Frame]:
        for position in range(len(self)):
            yield self._frame(position)

    # ------------------------------------------------------------------ #
    def _frame(self, position: int) -> Frame:
        offset = self._offsets[position]
        return Frame(
            index=position, offset=offset, data=self.data_at(position),
            check_state=_STATE_NAMES.get(self._states[position], STATE_NA),
            note=self._notes.get(position, ""), tag=self._tags.get(position, ""),
            length=int(self._sizes[position]),
        )

    def data_at(self, position: int) -> bytes:
        """第 position 帧的字节（带小缓存，避免同一屏反复切片）。"""
        cached = self._cache.get(position)
        if cached is not None:
            return cached
        offset = self._offsets[position]
        size = self._sizes[position]
        data = self._provider(int(offset), int(size))
        if self._cache_size:
            if len(self._cache) >= self._cache_size:
                self._cache.pop(next(iter(self._cache)))
            self._cache[position] = data
        return data

    def offset_of(self, position: int) -> int:
        return int(self._offsets[position])

    def size_of(self, position: int) -> int:
        return int(self._sizes[position])

    def state_of(self, position: int) -> str:
        return _STATE_NAMES.get(self._states[position], STATE_NA)

    def note_of(self, position: int) -> str:
        return self._notes.get(position, "")

    def tag_of(self, position: int) -> str:
        return self._tags.get(position, "")

    def hex_of(self, position: int) -> str:
        """只要 HEX 文本时别造 Frame 对象（导出/日志用得上）。"""
        from . import codec

        return codec.bytes_to_text(self.data_at(position))

    def window(self, start: int, count: int) -> list[Frame]:
        """取一个窗口（表格一屏、导出前 N 帧等），越界自动截断。"""
        start = max(0, int(start))
        stop = min(len(self), start + max(0, int(count)))
        return [self._frame(position) for position in range(start, stop)]

    def positions(self) -> range:
        return range(len(self))

    def select(self, positions) -> "FrameSource":
        """按位置挑一个子集（行规则筛帧用），仍是惰性视图。"""
        chosen = [int(p) for p in positions]
        offsets = array("q", (self._offsets[p] for p in chosen))
        sizes = array("i", (self._sizes[p] for p in chosen))
        states = bytearray(self._states[p] for p in chosen)
        notes = {new: self._notes[old] for new, old in enumerate(chosen)
                 if old in self._notes}
        tags = {new: self._tags[old] for new, old in enumerate(chosen)
                if old in self._tags}
        return FrameSource(self._provider, offsets, sizes, states, notes, tags,
                           cache_size=self._cache_size)

    def materialize(self) -> list[Frame]:
        """全部实例化（小数据/自检/导出用；大文件别调）。"""
        return [self._frame(position) for position in range(len(self))]

    # ------------------------------------------------------------------ #
    # 常用聚合（不用把帧造出来）
    # ------------------------------------------------------------------ #
    @property
    def total_bytes(self) -> int:
        if self._total_bytes is None:
            self._total_bytes = int(sum(self._sizes))
        return self._total_bytes

    @property
    def ok_count(self) -> int:
        self._ensure_counts()
        return self._counts[0]

    @property
    def fail_count(self) -> int:
        self._ensure_counts()
        return self._counts[1]

    def _ensure_counts(self) -> None:
        if self._counts is None:
            ok = sum(1 for code in self._states if code == 1)
            fail = sum(1 for code in self._states if code == 2)
            self._counts = (ok, fail)

    @property
    def size_histogram(self) -> dict[int, int]:
        """帧长 → 帧数（帧长分布，工具栏/报告用）。"""
        histogram: dict[int, int] = {}
        for size in self._sizes:
            histogram[size] = histogram.get(size, 0) + 1
        return histogram


@dataclass
class SplitResult:
    """分帧结果：索引 + 警告 + 字节来源。`frames` 是惰性序列。"""

    warnings: list[str] = field(default_factory=list)
    used_bytes: int = 0
    index: FrameIndexBuilder = field(default_factory=FrameIndexBuilder)
    provider: object | None = None
    _source: FrameSource | None = None

    def add(self, offset: int, end: int, state: str = STATE_NA,
            note: str = "", tag: str = "") -> int:
        return self.index.append(offset, end, state, note, tag)

    @property
    def count(self) -> int:
        """已记录的帧数（不触发惰性视图构建，分帧循环里用它）。"""
        return len(self.index)

    def set_data(self, data: bytes) -> None:
        """告诉结果"帧字节从这里切"。"""
        self.provider = MemoryProvider(data)
        self._source = None

    def set_provider(self, provider) -> None:
        self.provider = provider
        self._source = None

    @property
    def frames(self) -> FrameSource:
        if self._source is None:
            self._source = self.index.build(self.provider)
        return self._source

    def replace_frames(self, source: FrameSource) -> None:
        """整批换掉（行规则筛帧用），索引随之重建。"""
        builder = FrameIndexBuilder()
        for position in range(len(source)):
            builder.append(source.offset_of(position),
                           source.offset_of(position) + source.size_of(position),
                           source.state_of(position), source.note_of(position),
                           source.tag_of(position))
        self.index = builder
        self.provider = source._provider          # noqa: SLF001 - 同一个字节来源
        self._source = source

    @property
    def total_bytes(self) -> int:
        return self.frames.total_bytes

    @property
    def ok_count(self) -> int:
        return self.frames.ok_count

    @property
    def fail_count(self) -> int:
        return self.frames.fail_count

    def hex_lines(self) -> Iterator[str]:
        for position in range(len(self.frames)):
            yield self.frames.hex_of(position)
