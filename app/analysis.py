"""按列分析：一帧一行、一个字段一列的"宽表" + 每列统计 + 异常跳变检测。

纯逻辑，不含 Qt：界面（`app/ui/analysis_tab.py`）、自检、命令行都用这一套。

    stats = compute_stats("车速", [121.9, 0.0, 3302.6])
    jumps = find_jumps([1, 2, 3, 99, 4])      # 找出突变点
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from . import field_parser
from .field_parser import FieldDef, ParsedField

#: 宽表里每个字段列的 key：f0, f1, f2 …
def column_key(index: int) -> str:
    return f"f{index}"


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def display_text(value: Any, radix: str = "auto") -> str:
    if value is None:
        return ""
    return field_parser.format_value(value, radix)


# --------------------------------------------------------------------------- #
# 宽表：一帧一行
# --------------------------------------------------------------------------- #
def wide_row(frame, parsed: list[ParsedField], fields: list[FieldDef],
             rows_total: int = 0) -> dict:
    """把一帧的字段解析结果压成一行：{帧#, 偏移, 长度, 校验, f0: 值, f1: 值, …}。"""
    row: dict[str, Any] = {
        "index": frame.index,
        "offset": frame.offset,
        "length": frame.size,
        "check": {"ok": "✓", "fail": "✗", "na": "—"}.get(frame.check_state, frame.check_state),
    }
    text_map: dict[str, str] = {}
    tip_map: dict[str, list[tuple[str, str]]] = {}
    for index, (fd, item) in enumerate(zip(fields, parsed)):
        key = column_key(index)
        row[key] = item.value
        text_map[key] = display_text(item.value, fd.radix)
        tips = [("字段", item.name), ("位置", f"偏移 {item.offset}，长度 {item.length}")]
        if fd.unit:
            tips.append(("单位", fd.unit))
        if fd.scale != 1 or fd.bias:
            tips.append(("换算", f"×{fd.scale}{f' {fd.bias:+}' if fd.bias else ''}"))
        if not item.ok:
            tips.append(("错误", item.note))
        tip_map[key] = tips
    row["_text"] = text_map
    row["_tip"] = tip_map
    row["_tip"]["index"] = [("偏移", f"{frame.offset}"), ("长度", f"{frame.size} 字节"),
                            ("校验", row["check"])]
    # 校验列单独给提示：✗ 的时候直接看到"算出来是多少、帧里是多少"
    row["_tip"]["check"] = [
        ("校验", {"ok": "✓ 通过", "fail": "✗ 失败", "na": "— 未启用校验"}.get(
            frame.check_state, frame.check_state)),
        ("说明", frame.note or "帧尾与当前校验算法一致"),
    ]
    if frame.check_state == "fail":
        row["_fg"] = {"check": "#000000"}
    return row


def byte_row(frame, start: int, count: int) -> dict:
    """原始字节行：不做任何字段定义，直接把帧内第 start 起的 count 个字节铺成列。"""
    return rule_row(frame, [ResolvedColumn(f"偏移 {start + i}", start + i) for i in range(count)])


# --------------------------------------------------------------------------- #
# 列规则：用一个"小窗口"配置多条规则，自由生成要看的列
# --------------------------------------------------------------------------- #
@dataclass
class ResolvedColumn:
    """一条规则展开后的一列：从帧内哪个偏移、取几个字节、什么字节序/进制。"""

    title: str
    offset: int
    width: int = 1
    byte_order: str = "big"
    radix: str = "hex"

    def value_of(self, data: bytes):
        if self.offset < 0 or self.offset + self.width > len(data):
            return None
        return int.from_bytes(data[self.offset:self.offset + self.width], self.byte_order)


@dataclass
class ColumnRule:
    """一条列规则：可以铺连续字节列、生成多字节数值列，或按偏移列表挑列。"""

    name: str = "偏移"
    offset: int = 0
    count: int = 16
    stride: int = 0          # 每列跨多少字节；0 = 按每列字节数紧排
    width: int = 1           # 每列取几个字节
    byte_order: str = "big"
    radix: str = "hex"
    offsets: str = ""        # 显式偏移列表（"0,1,2,8-15"）；填了就忽略 offset/count/stride

    def resolve(self, limit: int = 256) -> list[ResolvedColumn]:
        if self.offsets.strip():
            positions = parse_offsets(self.offsets)
        else:
            step = max(1, int(self.stride) if self.stride else int(self.width) or 1)
            positions = [int(self.offset) + index * step
                         for index in range(max(1, int(self.count)))]
        width = max(1, int(self.width) or 1)
        out: list[ResolvedColumn] = []
        for index, position in enumerate(positions[:limit]):
            if width == 1:
                title = (f"偏移 {position}" if self.name in ("", "偏移")
                         else f"{self.name} {position}")
            else:
                title = f"{self.name}{index + 1} @{position}"
            out.append(ResolvedColumn(title, int(position), width, self.byte_order, self.radix))
        return out

    def describe(self) -> str:
        if self.offsets.strip():
            return f"{self.name}：偏移 {self.offsets}（每列 {self.width} 字节 {self.byte_order}，{self.radix}）"
        step = max(1, int(self.stride) if self.stride else int(self.width) or 1)
        return (f"{self.name}：从 {self.offset} 起 {self.count} 列，步长 {step}，"
                f"每列 {self.width} 字节 {self.byte_order}，显示 {self.radix}")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ColumnRule":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


def parse_offsets(spec: str) -> list[int]:
    """把 "0,1,2,8-15,65" 这种写法解析成偏移列表。"""
    out: list[int] = []
    for part in re.split(r"[,，;、\s]+", (spec or "").strip()):
        if not part:
            continue
        if "-" in part or "~" in part:
            first, second = re.split(r"[-~]", part, maxsplit=1)
            start, end = int(first), int(second)
            if end < start:
                start, end = end, start
            if end - start > 4096:
                raise ValueError(f"区间太大：{part}")
            out.extend(range(start, end + 1))
        else:
            out.append(int(part))
    return out


def resolve_rules(rules: list[ColumnRule], frame_len: int = 0,
                  limit: int = 256) -> tuple[list[ResolvedColumn], list[str]]:
    """把多条规则展开成列，返回 (列, 警告)。"""
    columns: list[ResolvedColumn] = []
    warnings: list[str] = []
    for rule in rules:
        try:
            produced = rule.resolve(limit + 1)      # 多要一列，用来判断"是否被上限截断"
        except ValueError as exc:
            warnings.append(f"{rule.name}：偏移列表无法解析（{exc}）")
            continue
        columns.extend(produced)
        if len(columns) > limit:
            warnings.append(f"列数超过上限 {limit}，多余的已省略")
            columns = columns[:limit]
            break
    if frame_len:
        over = [column for column in columns if column.offset + column.width > frame_len]
        if over:
            warnings.append(f"有 {len(over)} 列超出帧长 {frame_len} 字节（显示为空）")
    return columns, warnings


def rule_row(frame, columns: list[ResolvedColumn]) -> dict:
    """按"列规则"铺出来的一行。"""
    row: dict[str, Any] = {
        "index": frame.index,
        "offset": frame.offset,
        "length": frame.size,
        "check": {"ok": "✓", "fail": "✗", "na": "—"}.get(frame.check_state, frame.check_state),
    }
    text_map: dict[str, str] = {}
    tip_map: dict[str, list[tuple[str, str]]] = {}
    data = frame.data
    for index, column in enumerate(columns):
        key = column_key(index)
        value = column.value_of(data)
        row[key] = value
        if isinstance(value, int):
            text_map[key] = (f"{value:02X}" if column.width == 1
                             else f"{value:0{column.width * 2}X}")
            tip_map[key] = [
                ("帧内偏移", f"{column.offset} ~ {column.offset + column.width - 1}"),
                ("字节", data[column.offset:column.offset + column.width].hex(" ").upper()),
                ("按", f"{column.width} 字节 {column.byte_order}"),
                ("DEC / BIN", f"{value} / {format(value, f'0{column.width * 8}b')}"),
            ]
        else:
            text_map[key] = ""
            tip_map[key] = [("说明", f"偏移 {column.offset} 超出帧长 {len(data)}")]
    row["_text"] = text_map
    row["_tip"] = tip_map
    row["_tip"]["index"] = [("偏移", str(frame.offset)), ("长度", f"{frame.size} 字节"),
                            ("校验", row["check"])]
    row["_tip"]["check"] = [
        ("校验", {"ok": "✓ 通过", "fail": "✗ 失败", "na": "— 未启用校验"}.get(
            frame.check_state, frame.check_state)),
        ("说明", frame.note or "帧尾与当前校验算法一致"),
    ]
    if frame.check_state == "fail":
        row["_fg"] = {"check": "#000000"}
    return row


def byte_columns(byte_count: int) -> list[str]:
    """列 key 列表（f0, f1, …）。"""
    return [column_key(index) for index in range(byte_count)]


#: 常用列规则（对话框里一键套用）
RULE_PRESETS: list[tuple[str, list[ColumnRule]]] = [
    ("前 16 字节（HEX）", [ColumnRule(name="偏移", offset=0, count=16, width=1, radix="hex")]),
    ("整帧字节（0 起 64 列）", [ColumnRule(name="偏移", offset=0, count=64, width=1, radix="hex")]),
    ("每 2 字节大端 int16 ×16", [ColumnRule(name="i16_", offset=0, count=16, width=2,
                                        byte_order="big", radix="dec")]),
    ("每 2 字节小端 int16 ×16", [ColumnRule(name="i16le_", offset=0, count=16, width=2,
                                         byte_order="little", radix="dec")]),
    ("每 4 字节大端 int32 ×8", [ColumnRule(name="i32_", offset=0, count=8, width=4,
                                        byte_order="big", radix="dec")]),
    ("指定偏移（0,1,2,5,7,8-15）", [ColumnRule(name="偏移", offsets="0,1,2,5,7,8-15",
                                          width=1, radix="hex")]),
    ("字节 + 每 2 字节数值（两条规则）", [
        ColumnRule(name="偏移", offset=0, count=8, width=1, radix="hex"),
        ColumnRule(name="u16_", offset=0, count=8, width=2, byte_order="big", radix="dec"),
    ]),
]


# --------------------------------------------------------------------------- #
# 行规则：决定「数据表分析」要看哪些帧（哪几行）
# --------------------------------------------------------------------------- #
#: 行规则类型：key → 界面标签
ROW_KINDS: list[tuple[str, str]] = [
    ("range", "帧号区间"),
    ("step", "采样（每 N 帧取 1）"),
    ("byte", "帧内字节比较"),
    ("check", "校验状态"),
    ("length", "帧长比较"),
    ("contains", "帧内包含内容"),
]
ROW_KIND_LABELS = dict(ROW_KINDS)

#: 比较运算符（帧内字节 / 帧长 用）
ROW_OPS = ["=", "!=", ">", ">=", "<", "<="]
#: 内容规则的运算符：包含 / 不包含
ROW_CONTENT_OPS = [("=", "包含"), ("!=", "不包含")]
#: 校验状态取值
ROW_CHECK_STATES = [("fail", "✗ 失败"), ("ok", "✓ 通过"), ("na", "— 未启用")]
#: 多条行规则的组合方式
ROW_COMBINE = [("all", "同时满足（且）"), ("any", "满足任一（或）")]


def compare_value(got: float, op: str, want: float) -> bool:
    """按运算符比较两个数（未知运算符一律 False）。"""
    if op in ("=", "=="):
        return got == want
    if op == "!=":
        return got != want
    if op == ">":
        return got > want
    if op == ">=":
        return got >= want
    if op == "<":
        return got < want
    if op == "<=":
        return got <= want
    return False


def resolve_row_range(start: int, end: int, total: int) -> tuple[int, int]:
    """把「从哪到哪」解析成闭区间 [lo, hi]。支持负数（-1 = 最后一帧），越界自动钳制。"""
    if total <= 0:
        return (0, -1)
    lo = int(start) if int(start) >= 0 else total + int(start)
    hi = int(end) if int(end) >= 0 else total + int(end)
    if hi < lo:                       # 用户把头和尾写反了也认
        lo, hi = hi, lo
    return (max(0, lo), min(total - 1, hi))


def parse_content(text: str) -> bytes:
    """把「内容」写成 HEX（`EB 90`、`eb90`）或 ASCII（`ascii:SPD`、`'$GNGGA'`）解析成字节。"""
    body = (text or "").strip()
    if not body:
        return b""
    lowered = body.lower()
    if lowered.startswith("ascii:") or lowered.startswith("text:"):
        return body.split(":", 1)[1].encode("utf-8")
    if body.startswith("'") or body.startswith('"'):
        return body.strip("'\"").encode("utf-8")
    from .extract import normalize_hex

    normalized = normalize_hex(body)
    if normalized:
        return bytes.fromhex(normalized.replace(" ", ""))
    return body.encode("utf-8")


@dataclass
class RowRule:
    """一条行规则：从「帧号区间 / 采样 / 帧内字节 / 校验 / 帧长 / 内容」里挑出要看的帧。

    行号（帧#）就是表格「帧#」列里的数字，从 0 开始；`end = -1` 表示最后一帧。
    """

    enabled: bool = True
    kind: str = "range"
    name: str = ""
    #: 帧号区间
    start: int = 0
    end: int = -1
    #: 采样：每 step 帧取 1 帧，相位 phase
    step: int = 1
    phase: int = 0
    #: 帧内字节比较：从帧内第 offset 字节起取 width 字节（byte_order 字节序）与 value 比较
    offset: int = 0
    width: int = 1
    byte_order: str = "big"
    op: str = "="
    value: int = 0
    #: 校验状态：ok / fail / na
    state: str = "fail"
    #: 帧内包含的内容（HEX 或 ascii:xxx）
    text: str = ""

    # ------------------------------------------------------------------ #
    def match(self, frame, total: int = 0) -> bool:
        """这一帧是否命中本规则。frame 需要 .index / .data / .check_state。"""
        if not self.enabled:
            return True
        data = getattr(frame, "data", b"") or b""
        return self._match(int(getattr(frame, "index", 0)), len(data),
                           getattr(frame, "check_state", ""), data, total)

    def match_at(self, source, position: int, total: int = 0) -> bool:
        """在惰性帧视图（app/frames.py 的 FrameSource）上判断第 position 帧。

        只有"帧内字节 / 包含内容"这两类规则才真的去取字节，其余（区间、采样、
        校验状态、帧长）只看索引 —— 几十万帧筛选也就几毫秒。
        """
        if not self.enabled:
            return True
        if self.kind in ("byte", "contains"):
            data = source.data_at(position)
        else:
            data = b""
        return self._match(position, source.size_of(position), source.state_of(position),
                           data, total)

    def _match(self, index: int, size: int, state: str, data: bytes, total: int) -> bool:
        if self.kind == "range":
            lo, hi = resolve_row_range(self.start, self.end, total or (index + 1))
            return lo <= index <= hi
        if self.kind == "step":
            step = max(1, int(self.step) or 1)
            phase = max(0, int(self.phase))
            return (index - phase) % step == 0
        if self.kind == "byte":
            width = max(1, int(self.width) or 1)
            offset = int(self.offset)
            if offset < 0 or offset + width > len(data):
                return False
            got = int.from_bytes(data[offset:offset + width], self.byte_order)
            return compare_value(got, self.op, int(self.value))
        if self.kind == "check":
            return state == self.state
        if self.kind == "length":
            return compare_value(size, self.op, int(self.value))
        if self.kind == "contains":
            pattern = parse_content(self.text)
            if not pattern:
                return True
            hit = pattern in data
            return (not hit) if self.op == "!=" else hit
        return True

    # ------------------------------------------------------------------ #
    def describe(self, total: int = 0) -> str:
        """一句话说明这条规则在挑什么。"""
        if self.kind == "range":
            if total > 0:
                lo, hi = resolve_row_range(self.start, self.end, total)
                end_text = "最后一帧" if int(self.end) < 0 else f"{hi}"
                return f"帧 {lo} ~ {end_text}（共 {hi - lo + 1} 帧）"
            end_text = "最后一帧" if int(self.end) < 0 else str(self.end)
            return f"帧 {self.start} ~ {end_text}"
        if self.kind == "step":
            step = max(1, int(self.step) or 1)
            phase = max(0, int(self.phase))
            return f"每 {step} 帧取 1 帧" + (f"（相位 {phase}）" if phase else "")
        if self.kind == "byte":
            width = max(1, int(self.width) or 1)
            end = int(self.offset) + width - 1
            where = f"帧内 [{self.offset}]" if width == 1 else f"帧内 [{self.offset}..{end}]"
            order = "" if width == 1 else f" {self.byte_order}"
            return (f"{where}{order} {self.op} "
                    f"0x{int(self.value) & ((1 << (8 * width)) - 1):0{width * 2}X}")
        if self.kind == "check":
            label = dict(ROW_CHECK_STATES).get(self.state, self.state)
            return f"校验状态 = {label}"
        if self.kind == "length":
            return f"帧长 {self.op} {self.value} 字节"
        if self.kind == "contains":
            op_label = dict(ROW_CONTENT_OPS).get(self.op, "包含")
            return f"帧内{op_label} {self.text or '（空）'}"
        return ROW_KIND_LABELS.get(self.kind, self.kind)

    def label(self, total: int = 0) -> str:
        return f"{self.name}：{self.describe(total)}" if self.name else self.describe(total)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RowRule":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


def active_row_rules(rules: list[RowRule]) -> list[RowRule]:
    return [rule for rule in (rules or []) if rule.enabled]


def match_row_positions(frames, rules: list[RowRule], combine: str = "all") -> list[int]:
    """返回命中行规则的帧在 frames 里的下标（一条规则都没有 = 全部命中）。

    frames 是惰性帧视图时走 `match_at()`：不为筛选把每一帧都造出来。
    """
    rules = active_row_rules(rules)
    if not rules or not frames:
        return list(range(len(frames)))
    lazy = hasattr(frames, "state_of") and hasattr(frames, "size_of")
    total = int(getattr(frames[-1], "index", len(frames) - 1)) + 1
    out: list[int] = []
    if lazy:
        for position in range(len(frames)):
            hits = [rule.match_at(frames, position, total) for rule in rules]
            if any(hits) if combine == "any" else all(hits):
                out.append(position)
        return out
    for position, frame in enumerate(frames):
        hits = [rule.match(frame, total) for rule in rules]
        passed = any(hits) if combine == "any" else all(hits)
        if passed:
            out.append(position)
    return out


def describe_row_rules(rules: list[RowRule], combine: str = "all",
                       total: int = 0) -> str:
    """把一套行规则说成一句话；没有规则返回空串。"""
    rules = active_row_rules(rules)
    if not rules:
        return ""
    joiner = " 或 " if combine == "any" else " 且 "
    return joiner.join(rule.describe(total) for rule in rules)


#: 常用行规则（对话框里一键套用）
ROW_PRESETS: list[tuple[str, list[RowRule]]] = [
    ("全部帧（不限）", [RowRule(kind="range", start=0, end=-1)]),
    ("前 500 帧", [RowRule(kind="range", start=0, end=499)]),
    ("帧 500 ~ 1500", [RowRule(kind="range", start=500, end=1500)]),
    ("从第 1000 帧到末尾", [RowRule(kind="range", start=1000, end=-1)]),
    ("最后 500 帧", [RowRule(kind="range", start=-500, end=-1)]),
    ("每 10 帧取 1 帧（抽样看趋势）", [RowRule(kind="step", step=10)]),
    ("每 100 帧取 1 帧（长文件概览）", [RowRule(kind="step", step=100)]),
    ("只看校验失败", [RowRule(kind="check", state="fail")]),
    ("只看校验通过", [RowRule(kind="check", state="ok")]),
    ("只看定长 156 帧", [RowRule(kind="length", op="=", value=156)]),
    ("帧长异常（≠156）", [RowRule(kind="length", op="!=", value=156)]),
    ("第 0 字节 ≠ 0xEB（异帧）", [RowRule(kind="byte", offset=0, width=1, op="!=", value=0xEB)]),
    ("第 2 字节 > 150", [RowRule(kind="byte", offset=2, width=1, op=">", value=150)]),
    ("帧号区间 + 每 5 帧取 1（两条规则）", [
        RowRule(kind="range", start=0, end=-1),
        RowRule(kind="step", step=5),
    ]),
]


# --------------------------------------------------------------------------- #
# 每列统计
# --------------------------------------------------------------------------- #
@dataclass
class ColumnStats:
    name: str = ""
    kind: str = "text"           # number | text | empty
    total: int = 0
    valid: int = 0               # 非空个数
    minimum: float | None = None
    maximum: float | None = None
    mean: float | None = None
    median: float | None = None
    stdev: float | None = None
    unique: int = 0
    changes: int = 0             # 相邻取值不同的次数（变化频繁→可能是计数器/噪声）
    first: Any = None
    last: Any = None
    delta: float | None = None   # 首末差（数值列）
    monotonic: str = ""
    max_jump: float | None = None
    jump_index: int | None = None
    top_values: list[tuple[str, int]] = field(default_factory=list)

    @property
    def empty(self) -> int:
        return max(0, self.total - self.valid)

    def to_row(self) -> dict:
        """给统计表用的一行。"""
        def fmt(value, digits: int = 4) -> str:
            if value is None:
                return "—"
            if isinstance(value, float):
                return f"{value:.{digits}g}"
            return str(value)
        return {
            "name": self.name,
            "kind": {"number": "数值", "text": "文本", "empty": "空"}.get(self.kind, self.kind),
            "valid": f"{self.valid}/{self.total}" + (f"（空 {self.empty}）" if self.empty else ""),
            "range": f"{fmt(self.minimum)} ~ {fmt(self.maximum)}" if self.kind == "number" else "—",
            "mean": fmt(self.mean) if self.kind == "number" else "—",
            "median": fmt(self.median) if self.kind == "number" else "—",
            "stdev": fmt(self.stdev, 3) if self.kind == "number" else "—",
            "unique": str(self.unique),
            "changes": str(self.changes),
            "first_last": f"{fmt(self.first, 6)} → {fmt(self.last, 6)}",
            "delta": fmt(self.delta) if self.delta is not None else "—",
            "trend": self.monotonic or "—",
            "max_jump": (f"{fmt(self.max_jump)} @ 帧{self.jump_index}" if self.max_jump is not None
                         else "—"),
        }


def compute_stats(name: str, values: list[Any], total: int | None = None) -> ColumnStats:
    stats = ColumnStats(name=name, total=len(values) if total is None else total)
    present = [v for v in values if v is not None and v != ""]
    stats.valid = len(present)
    if not present:
        stats.kind = "empty"
        return stats

    counter: dict[str, int] = {}
    for value in present:
        key = value if isinstance(value, str) else display_text(value)
        counter[str(key)] = counter.get(str(key), 0) + 1
    stats.unique = len(counter)
    stats.top_values = sorted(counter.items(), key=lambda kv: -kv[1])[:5]
    stats.changes = sum(1 for a, b in zip(present, present[1:]) if a != b)
    stats.first, stats.last = present[0], present[-1]

    numbers = [float(v) for v in present if is_number(v)]
    if len(numbers) < len(present) or not numbers:
        stats.kind = "text"
        return stats

    stats.kind = "number"
    numbers_sorted = sorted(numbers)
    stats.minimum = numbers_sorted[0]
    stats.maximum = numbers_sorted[-1]
    stats.mean = sum(numbers) / len(numbers)
    middle = len(numbers_sorted) // 2
    stats.median = (numbers_sorted[middle] if len(numbers_sorted) % 2
                    else (numbers_sorted[middle - 1] + numbers_sorted[middle]) / 2)
    if len(numbers) > 1:
        variance = sum((x - stats.mean) ** 2 for x in numbers) / (len(numbers) - 1)
        stats.stdev = math.sqrt(variance)
    stats.delta = numbers[-1] - numbers[0]

    increasing = all(b >= a for a, b in zip(numbers, numbers[1:]))
    decreasing = all(b <= a for a, b in zip(numbers, numbers[1:]))
    stats.monotonic = "递增" if increasing and stats.changes else (
        "递减" if decreasing and stats.changes else ("恒定" if not stats.changes else "波动"))

    if len(numbers) > 1:
        jumps = [(abs(b - a), a, b, index + 1) for index, (a, b) in enumerate(zip(numbers, numbers[1:]))]
        stats.max_jump = max(j for j, _a, _b, _i in jumps)
        for jump, _a, _b, index in sorted(jumps, key=lambda item: -item[0]):
            if jump == stats.max_jump:
                stats.jump_index = index
                break
    return stats


def find_jumps(values: list[Any], factor: float = 4.0,
               min_jump: float | None = None) -> list[tuple[int, float, float, float]]:
    """找"突变"：相邻差绝对值 > factor×σ（或超过 min_jump）。

    返回 [(行下标, 前值, 后值, 差值)]，按差值从大到小。
    """
    series = [(index, float(v)) for index, v in enumerate(values) if is_number(v)]
    if len(series) < 3:
        return []
    numbers = [v for _i, v in series]
    mean = sum(numbers) / len(numbers)
    variance = sum((x - mean) ** 2 for x in numbers) / max(1, len(numbers) - 1)
    sigma = math.sqrt(variance)
    threshold = max(min_jump or 0.0, factor * sigma if sigma > 0 else 0.0)
    if threshold <= 0:
        return []
    jumps = []
    for (index, value), (_next_index, next_value) in zip(series, series[1:]):
        diff = next_value - value
        if abs(diff) > threshold:
            jumps.append((_next_index, value, next_value, diff))
    jumps.sort(key=lambda item: -abs(item[3]))
    return jumps


def downsample(values: list[Any], max_points: int = 600) -> list[tuple[int, float]]:
    """点太多时按桶抽稀，但每桶保留最小/最大值，曲线形状不变。

    桶宽按"每桶最多出 2 个点"反推，所以点数大约是 max_points（最多 +2 个桶的取整误差）。
    """
    series = [(index, float(v)) for index, v in enumerate(values) if is_number(v)]
    if len(series) <= max_points:
        return series
    bucket = max(1, len(series) * 2 // max(1, max_points))
    out: list[tuple[int, float]] = []
    for start in range(0, len(series), bucket):
        group = series[start:start + bucket]
        low = min(group, key=lambda item: item[1])
        high = max(group, key=lambda item: item[1])
        out.extend(sorted({low, high}))
    return out


def describe_jumps(jumps: list[tuple[int, float, float, float]], limit: int = 12,
                   label_of=None) -> str:
    """突变清单文本；label_of 可以把"第几个值"换成真正的帧号。"""
    if not jumps:
        return "没有检测到突变（阈值内）"
    lines = []
    for index, before, after, diff in jumps[:limit]:
        label = label_of(index) if label_of else index
        lines.append(f"帧 {label}：{before:.6g} → {after:.6g}（Δ {diff:+.6g}）")
    if len(jumps) > limit:
        lines.append(f"… 共 {len(jumps)} 处突变")
    return "\n".join(lines)
