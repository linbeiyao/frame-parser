"""从日志文件（JSONL / CSV / 文本）里把数据帧抠出来。

现场抓包往往不是"纯字节流"，而是各种日志格式：

    can_20260725_185810.jsonl
        {"can_id":"0x1E7","dlc":8,"data":"ea07071900000000","ts":1785005890511}

    CAN 分析仪 CSV
        00000,="11:05:30.340",0x64815,ch2,...,0x0021,数据帧,标准帧,0x08,x| 00 00 00 00 00 00 00 00

本模块用「正则 + 行过滤」把里面的数据段取出来，归一化成每行一帧的 HEX 文本，
再丢给解析器；可以按 can_id / 关键字先筛行。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

#: (显示名, 正则, 是否归一化成 HEX 字节, 说明)
PATTERN_PRESETS: list[tuple[str, str, bool, str]] = [
    ("JSONL/JSON 的 data 字段", r'"data"\s*:\s*"([^"]+)"', True,
     "形如 {\"can_id\":\"0x1E7\",\"data\":\"ea07071900000000\"} 的抓包日志"),
    ("CAN 分析仪 CSV 数据列", r"x\|\s*((?:[0-9A-Fa-f]{2}[\s,]*)+)", True,
     "周立功/CANalyst 导出的 CSV，数据列形如 x| 00 00 02 00 64 1C 00 00"),
    ("引号内的十六进制串", r"['\"]([0-9A-Fa-f][0-9A-Fa-f \t,:-]{2,})['\"]", True,
     "任意日志里被引号包起来的 HEX 数据"),
    ("0x 前缀序列", r"((?:0[xX][0-9A-Fa-f]{1,2}[ ,;\t]*)+)", True,
     "形如 0xAA 0x55 0x01 的日志"),
    ("连续 HEX 串（≥4 位）", r"(?<![0-9A-Fa-f])([0-9A-Fa-f]{4,})(?![0-9A-Fa-f])", True,
     "形如 ea07071900000000 的裸 HEX 串（注意可能误匹配编号/时间戳）"),
    ("每行一整帧（原样保留）", r"^(.+)$", False,
     "每行就是一帧文本，不做归一化，按 ASCII 解析"),
]

PATTERN_NAMES = [item[0] for item in PATTERN_PRESETS]

_HEX_ONLY_RE = re.compile(r"[^0-9A-Fa-f]")


@dataclass
class ExtractResult:
    lines: list[str] = field(default_factory=list)
    matched_lines: int = 0
    total_lines: int = 0
    skipped: int = 0
    pattern_name: str = ""
    pattern: str = ""
    group: int = 1
    line_filter: str = ""
    normalize: bool = True
    truncated: bool = False
    note: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.lines)

    def summary(self) -> str:
        if self.error:
            return f"提取失败：{self.error}"
        parts = [
            f"规则「{self.pattern_name}」",
            f"共 {self.total_lines} 行 / 匹配 {self.matched_lines} 行 / 得到 {len(self.lines)} 帧",
        ]
        if self.line_filter:
            parts.append(f"行过滤「{self.line_filter}」")
        if self.skipped:
            parts.append(f"跳过 {self.skipped} 条无法归一化的数据")
        if self.truncated:
            parts.append("文件较大，只读取了前面一部分")
        if self.note:
            parts.append(self.note)
        return "，".join(parts)


# --------------------------------------------------------------------------- #
# 文本读取
# --------------------------------------------------------------------------- #
def read_text_head(path: str, max_bytes: int = 8 * 1024 * 1024) -> tuple[str, bool]:
    """读取文件（默认最多 8 MB），自动在 UTF-8 / GBK 之间选择，返回 (文本, 是否被截断)。"""
    size = os.path.getsize(path)
    limit = max_bytes if max_bytes and max_bytes > 0 else size
    with open(path, "rb") as handle:
        blob = handle.read(limit)
    truncated = size > len(blob)

    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        for trim in range(0, 9):
            chunk = blob[:len(blob) - trim] if trim else blob
            try:
                return chunk.decode(encoding), truncated
            except UnicodeDecodeError:
                continue
    return blob.decode("latin-1", errors="replace"), truncated


# --------------------------------------------------------------------------- #
# 归一化 / 提取
# --------------------------------------------------------------------------- #
def normalize_hex(text: str) -> str:
    """把任意写法的十六进制片段归一化成 'AA 55 01'；无法归一化返回空串。"""
    body = re.sub(r"0[xX]", "", text or "")
    body = _HEX_ONLY_RE.sub("", body)
    if not body or len(body) % 2:
        return ""
    return " ".join(body.upper()[i:i + 2] for i in range(0, len(body), 2))


def extract(
    text: str,
    pattern: str,
    group: int = 1,
    line_filter: str = "",
    limit: int = 20000,
    normalize: bool = True,
    pattern_name: str = "自定义",
) -> ExtractResult:
    result = ExtractResult(pattern=pattern, group=group, line_filter=line_filter,
                           normalize=normalize, pattern_name=pattern_name)
    if not pattern:
        result.error = "正则为空"
        return result
    try:
        regex = re.compile(pattern, re.MULTILINE)
    except re.error as exc:
        result.error = f"正则语法错误：{exc}"
        return result

    keyword = (line_filter or "").strip().lower()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if keyword and keyword not in line.lower():
            continue
        result.total_lines += 1
        match = regex.search(line)
        if not match or match.lastindex is None or match.lastindex < group:
            continue
        result.matched_lines += 1
        value = match.group(group)
        if normalize:
            value = normalize_hex(value)
            if not value:
                result.skipped += 1
                continue
        else:
            value = value.strip()
            if not value:
                result.skipped += 1
                continue
        result.lines.append(value)
        if len(result.lines) >= limit:
            result.truncated = True
            result.note = f"已达到上限 {limit} 帧"
            break
    return result


def jsonl_ids(text: str, field_name: str = "can_id", limit: int = 200) -> list[tuple[str, int]]:
    """统计 JSONL 里某个字段出现过的值（方便挑选 can_id 过滤条件）。"""
    counter: dict[str, int] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and field_name in obj:
            key = str(obj[field_name])
            counter[key] = counter.get(key, 0) + 1
        if len(counter) >= limit:
            break
    return sorted(counter.items(), key=lambda kv: -kv[1])


def auto_extract(text: str, limit: int = 20000) -> ExtractResult:
    """按优先级自动挑一条最合适的规则，用于"导入日志文件"一键出帧。"""
    best: ExtractResult | None = None
    for name, pattern, normalize, _desc in PATTERN_PRESETS:
        candidate = extract(text, pattern, 1, "", limit, normalize, name)
        if not candidate.lines:
            continue
        clean = candidate.skipped == 0
        if clean and len(candidate.lines) >= 3:
            return candidate
        if best is None or len(candidate.lines) > len(best.lines):
            best = candidate
    if best is not None:
        best.note = (best.note + "；" if best.note else "") + "自动匹配的规则可能不准确，建议用「从日志提取…」手动确认"
    return best or ExtractResult(error="没有匹配到任何可用数据，请手动指定正则")


# --------------------------------------------------------------------------- #
# 直接读文件（一行一帧的 HEX 文本，例如 data/backward_sample.txt）
# --------------------------------------------------------------------------- #
_HEX_LINE_RE = re.compile(r"^[0-9A-Fa-f]{2}(?:[\s,;:|]+[0-9A-Fa-f]{2})*[\s,;:|]*$")


def looks_like_hex_line(line: str) -> bool:
    line = (line or "").strip()
    return bool(line) and bool(_HEX_LINE_RE.match(line))


def read_hex_lines(path: str, max_lines: int = 200, max_bytes: int = 8 * 1024 * 1024):
    """从文件里读前 max_lines 行"一行一帧"的 HEX 文本。

    返回 (帧文本列表, 扫描到的总行数, 跳过的非 HEX 行数, 是否被截断)。
    """
    text, truncated = read_text_head(path, max_bytes)
    frames: list[str] = []
    scanned = 0
    skipped = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        scanned += 1
        if looks_like_hex_line(line):
            if len(frames) < max_lines:
                frames.append(line)
        else:
            skipped += 1
    if max_lines and scanned > len(frames) + skipped:
        truncated = True
    return frames, scanned, skipped, truncated
