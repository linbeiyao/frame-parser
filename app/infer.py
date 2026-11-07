"""自动推断分帧参数：协议未知时先猜出帧头、帧长、长度字段语义、校验算法。

思路（全部基于统计 + 一致性强弱打分，不做任何协议假设）：

1. **周期性**：统计出现次数最多的 1/2/4 字节片段，如果它出现的位置间隔基本恒定，
   那它就是帧头，间隔就是帧长（定长帧）；
2. **长度字段**：在帧头之后 0~8 字节的范围内试各种「偏移 / 字节数 / 字节序」，
   看哪个位置上的数值 + 固定偏差 == 帧间隔，从而还原长度字段的位置与语义；
3. **校验**：把推断出的帧喂给所有校验算法，能全中的那个就是校验算法；
4. **恒定字节**：跨帧取值永不变的字节位置（帧头、类型、帧尾、保留位），
   其中数值接近帧长的位置会被标为「疑似长度字段」。

结果是一份带置信度和说明的候选列表，界面里点一下就能应用，命令行里用 --infer 打印。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from . import checksum, codec, framer

MAX_SAMPLES = 400          # 最多用多少帧做统计
MAX_POSITIONS = 20000      # 单次扫描出现位置的上限


@dataclass
class Candidate:
    title: str
    config: framer.FrameConfig
    frame_count: int
    confidence: float
    notes: list[str] = field(default_factory=list)
    consumed: int = 0

    @property
    def percent(self) -> int:
        return int(round(self.confidence * 100))

    def summary(self) -> str:
        mode = framer.MODE_LABELS.get(self.config.mode, self.config.mode)
        text = f"{self.title}｜{mode}｜{self.frame_count} 帧"
        if self.config.mode == framer.MODE_FIXED:
            text += f" × {self.config.fixed_len} 字节"
        if self.config.checksum_algo != "none":
            text += f"｜校验 {checksum.ALGO_LABELS.get(self.config.checksum_algo, self.config.checksum_algo)}"
        return text


# --------------------------------------------------------------------------- #
def _detect_periods(sample: bytes, minimum: int = 4, maximum: int = 512,
                    samples: int = 400, threshold: float = 0.85) -> list[tuple[int, float]]:
    """自相关找周期：按周期的整数倍取点，比较 data[k*P] 与 data[(k+1)*P]。

    这样比较的永远是"相邻帧里同一位置的字节"，不受帧首偏移影响；
    比"统计最常见片段"可靠：帧头每帧只出现一次，出现次数比不过帧内重复数据。
    """
    limit = min(len(sample), 400000)
    if limit < minimum * 6:
        return []
    found: list[tuple[int, float]] = []
    for period in range(minimum, maximum + 1):
        count = min(samples, (limit - period) // period)
        if count < 6:
            continue
        hits = sum(1 for k in range(count) if sample[k * period] == sample[(k + 1) * period])
        ratio = hits / count
        if ratio >= threshold:
            found.append((period, ratio))
    if not found:
        return []
    # 只保留"基本周期"：如果 P 和 2P 都通过，说明 P 才是真周期
    primitive: list[tuple[int, float]] = []
    for period, ratio in sorted(found, key=lambda item: item[0]):
        if any(period % kept[0] == 0 for kept in primitive):
            continue
        primitive.append((period, ratio))
    primitive.sort(key=lambda item: -item[1])
    return primitive[:4]


def _find_header_at_period(sample: bytes, period: int) -> tuple[int, bytes, float] | None:
    """已知帧长 period，找一个偏移，使该处 2~4 字节前缀在每个周期上都一模一样。"""
    best: tuple[int, bytes, float] | None = None
    for offset in range(min(period, 128)):
        if offset + period * 3 > len(sample):
            break
        for size in (2, 3, 4, 1):
            if offset + size > len(sample):
                continue
            token = sample[offset:offset + size]
            total = 0
            hits = 0
            position = offset
            while position + size <= len(sample) and total < 2000:
                total += 1
                if sample[position:position + size] == token:
                    hits += 1
                position += period
            if total < 4:
                continue
            ratio = hits / total
            if ratio >= 0.98:
                score = ratio - offset * 0.0005 + (0.02 if size == 2 else 0.0)
                if best is None or score > best[2]:
                    best = (offset, token, score)
                break       # 该偏移已经找到可用前缀，换下一个偏移
    if best is None:
        return None
    return best[0], best[1], best[2]


def _make_fixed_candidate(data: bytes, period: int, offset: int, token: bytes,
                          ratio: float) -> Candidate:
    cfg = framer.FrameConfig(mode=framer.MODE_FIXED, fixed_len=period,
                             header_hex=_hex(token) if token else "",
                             checksum_algo="none", skip_bytes=offset)
    split = framer.split_frames(data, cfg)
    valid = [f for f in split.frames if f.size == period]
    confidence = 0.45 + 0.3 * min(1.0, ratio) + (0.18 if not offset else 0.0) \
        + 0.07 * min(1.0, len(valid) / 50)
    notes = [
        f"自相关检测到周期 {period} 字节（逐字节相同率 {ratio * 100:.0f}%），"
        f"帧头 {_hex(token)}（{len(token)} 字节）在每个周期起点都一致",
    ]
    if offset:
        notes.append(f"第一帧之前有 {offset} 字节前导数据，已自动跳过")
    algo, order, algo_hits = _probe_checksum(valid, cfg)
    if algo != "none" and algo_hits >= max(2, len(valid[:60]) * 0.9):
        cfg.checksum_algo = algo
        cfg.checksum_byte_order = order
        confidence = min(1.0, confidence + 0.15)
        notes.append(f"帧尾校验匹配 {checksum.ALGO_LABELS.get(algo, algo)}"
                     f"（{'大端' if order == 'big' else '小端'}，{algo_hits} 帧全中）")
    else:
        notes.append("帧尾没匹配到任何标准校验算法，可能无校验或使用私有校验")
    const, varying = _constant_positions(valid)
    if const:
        head = const[:16]
        notes.append("跨帧恒定字节位置：" + ", ".join(str(i) for i in head)
                     + (" …" if len(const) > 16 else ""))
        notes.extend(_length_field_notes(valid, head, period, len(token)))
    if varying:
        notes.append(f"变化字节 {len(varying)} 个（偏移 "
                     + ", ".join(str(i) for i in varying[:12])
                     + (" …）" if len(varying) > 12 else "）"))
    return Candidate(
        title=f"定长帧 {period} 字节（帧头 {_hex(token) if token else '无'}）",
        config=cfg, frame_count=len(valid), confidence=min(1.0, confidence),
        consumed=len(data), notes=notes,
    )


# --------------------------------------------------------------------------- #
# 按行识别（"一行一帧"的日志最省事：直接数每行多少字节就是帧长）
# --------------------------------------------------------------------------- #
@dataclass
class LineInfo:
    line_count: int = 0
    sampled: int = 0
    uniform_length: int = 0      # 每行字节数一致时给出该长度
    lengths: Counter = field(default_factory=Counter)
    hex_lines: bool = False
    newline: str = "\n"

    def describe(self) -> str:
        if not self.line_count:
            return "没有多行数据"
        kinds = "、".join(f"{size} 字节×{count}"
                         for size, count in self.lengths.most_common(4))
        return (f"共 {self.line_count} 行（采样 {self.sampled} 行），"
                f"行长分布：{kinds}，换行符 {'CRLF' if self.newline == chr(13) + chr(10) else 'LF'}")


def detect_line_frames(text: str, sample_lines: int = 2000) -> LineInfo:
    """看输入是不是"一行一帧"：逐行数出每行多少字节。"""
    info = LineInfo()
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if len(lines) < 3:
        return info
    info.line_count = len(lines)
    info.newline = "\r\n" if "\r\n" in text else "\n"
    hex_hits = 0
    lengths: Counter = Counter()
    for line in lines[:sample_lines]:
        data = codec.hex_fast_line(line)
        if data is not None:
            hex_hits += 1
            lengths[len(data)] += 1
    info.sampled = min(len(lines), sample_lines)
    info.lengths = lengths
    info.hex_lines = bool(lengths) and hex_hits >= info.sampled * 0.8
    if lengths:
        common, count = lengths.most_common(1)[0]
        if count >= hex_hits * 0.9:          # 九成以上的行长度一致 → 就是定长
            info.uniform_length = common
    return info


def suggest_framing(text: str, data: bytes) -> tuple[framer.FrameConfig, str]:
    """按数据猜一套最合适的帧格式，返回 (配置, 理由)。

    优先级：一行一帧（定长/换行）→ 自相关周期 → 帧头+长度字段 → 整包。
    """
    info = detect_line_frames(text)
    if info.line_count >= 3:
        if info.hex_lines and info.uniform_length:
            return (framer.FrameConfig(mode=framer.MODE_FIXED,
                                       fixed_len=info.uniform_length,
                                       checksum_algo="none", header_hex=""),
                    f"{info.describe()}。每行都是 {info.uniform_length} 字节 —— 直接按「定长帧 "
                    f"{info.uniform_length}」切，不用再猜")
        delim = "0D 0A" if info.newline == "\r\n" else "0A"
        return (framer.FrameConfig(mode=framer.MODE_DELIMITER, delimiter_hex=delim,
                                   include_delimiter=False, checksum_algo="none", header_hex=""),
                f"{info.describe()}。行长不一致（或不是十六进制行）—— 改按换行分帧（{delim}）")

    if not data:
        return framer.FrameConfig(mode=framer.MODE_WHOLE, checksum_algo="none"), "没有数据"

    candidates = infer(data, max_candidates=3)
    strong = [c for c in candidates if c.confidence >= 0.6]
    if strong:
        best = strong[0]
        return best.config, f"自相关找到周期结构：{best.title}（置信度 {best.percent}%）"

    periods = _detect_periods(data)
    if periods:
        period, ratio = periods[0]
        located = _find_header_at_period(data, period)
        if located:
            offset, token, _score = located
            return (framer.FrameConfig(mode=framer.MODE_FIXED, fixed_len=period,
                                       header_hex=_hex(token), skip_bytes=offset,
                                       checksum_algo="none"),
                    f"自相关检测到周期 {period} 字节（相同率 {ratio * 100:.0f}%），"
                    f"帧头 {_hex(token)}" + (f"，前导 {offset} 字节" if offset else ""))

    return (framer.FrameConfig(mode=framer.MODE_WHOLE, checksum_algo="none", header_hex=""),
            "没找到周期性结构，先用「整包」逐个字节看")


def _frequent_tokens(data: bytes, size: int, top: int = 10, minimum: int = 3):
    counter: Counter = Counter()
    for index in range(0, len(data) - size + 1):
        counter[data[index:index + size]] += 1
    return [(token, count) for token, count in counter.most_common(top) if count >= minimum]


def _positions(data: bytes, token: bytes, limit: int = MAX_POSITIONS) -> list[int]:
    out: list[int] = []
    start = data.find(token)
    while start >= 0 and len(out) < limit:
        out.append(start)
        start = data.find(token, start + 1)
    return out


def _delta_stats(positions: list[int]):
    if len(positions) < 2:
        return None
    deltas = [b - a for a, b in zip(positions, positions[1:])]
    counter = Counter(deltas)
    value, count = counter.most_common(1)[0]
    return value, count, len(deltas)


def _hex(token: bytes) -> str:
    return codec.bytes_to_text(token, codec.RADIX_HEX)


def _probe_checksum(frames: list, config: framer.FrameConfig) -> tuple[str, str, int]:
    """在帧尾位置试所有校验算法，返回 (算法, 字节序, 命中帧数)。"""
    sample = [f.data for f in frames[:60]]
    best = ("none", config.checksum_byte_order, 0)
    for key, _label, size, _default in checksum.ALGOS:
        if not size:
            continue
        usable = [chunk for chunk in sample if len(chunk) > size]
        if len(usable) < 2:
            continue
        for order in ("big", "little"):
            hits = sum(
                1 for chunk in usable
                if checksum.compute(key, chunk[:-size], order) == chunk[-size:]
            )
            if hits > best[2]:
                best = (key, order, hits)
    return best


def _constant_positions(frames: list, minimum_frames: int = 5) -> tuple[list[int], list[int]]:
    """返回 (恒定字节位置, 变化字节位置)。"""
    sample = [f.data for f in frames[:min(len(frames), 60)]]
    if len(sample) < minimum_frames:
        return [], []
    size = min(len(chunk) for chunk in sample)
    const: list[int] = []
    varying: list[int] = []
    for index in range(size):
        values = {chunk[index] for chunk in sample}
        if len(values) == 1:
            const.append(index)
        else:
            varying.append(index)
    return const, varying


def _length_field_notes(frames: list, const: list[int], frame_len: int,
                        header_len: int = 0) -> list[str]:
    """在恒定字节里找"数值接近帧长"的位置，大概率就是长度字段（跳过帧头内部）。"""
    if not frames:
        return []
    data = frames[0].data
    notes: list[str] = []
    for index in const:
        if index < header_len:
            continue
        value = data[index]
        if 0 < value <= frame_len and 0 <= frame_len - value <= 16:
            notes.append(
                f"偏移 {index} 恒为 {value}，与帧长 {frame_len} 差 {frame_len - value} —— 疑似长度字段"
            )
    for index in const:
        if index < header_len or index + 2 > len(data):
            continue
        value = int.from_bytes(data[index:index + 2], "big")
        if frame_len < value <= frame_len + 16 or (0 < value < frame_len and frame_len - value <= 4):
            notes.append(f"偏移 {index} 的 2 字节值 {value} 接近帧长 {frame_len} —— 疑似长度字段（大端）")
    return notes[:4]


def describe_config(config: framer.FrameConfig) -> str:
    """把配置压成一行，方便命令行输出。"""
    parts = [f"mode={config.mode}"]
    if config.skip_bytes:
        parts.append(f"skip={config.skip_bytes}")
    if config.header_hex:
        parts.append(f"header={config.header_hex}")
    if config.mode == framer.MODE_FIXED:
        parts.append(f"fixed_len={config.fixed_len}")
    if config.mode == framer.MODE_HEADER:
        parts.append(f"len@offset{config.len_offset}/{config.len_size}B/{config.len_byte_order}")
        parts.append(f"len_has_header={config.len_includes_header}")
        parts.append(f"len_has_checksum={config.len_includes_checksum}")
    if config.mode == framer.MODE_DELIMITER:
        parts.append(f"delimiter={config.delimiter_hex}")
    if config.mode == framer.MODE_TLV:
        parts.append(f"tag={config.tag_size}B, len={config.value_len_size}B")
    if config.checksum_algo != "none":
        parts.append(f"checksum={config.checksum_algo}/{config.checksum_byte_order}/{config.checksum_where}")
    return ", ".join(parts)


# --------------------------------------------------------------------------- #
def _try_tlv(data: bytes) -> Candidate | None:
    """按 Tag+Len(1字节) 走一遍，能刚好走完就认为像 TLV。"""
    for tag_size in (1, 2):
        for len_size in (1, 2):
            pos = 0
            count = 0
            while pos + tag_size + len_size <= len(data) and count < 5000:
                length = int.from_bytes(data[pos + tag_size:pos + tag_size + len_size], "big")
                if length <= 0 or length > 4096:
                    break
                pos += tag_size + len_size + length
                count += 1
            if count >= 3 and pos >= len(data) - 1:
                cfg = framer.FrameConfig(mode=framer.MODE_TLV, tag_size=tag_size,
                                         value_len_size=len_size, value_len_byte_order="big")
                return Candidate(
                    title=f"TLV：Tag {tag_size} 字节 + Len {len_size} 字节",
                    config=cfg, frame_count=count, confidence=0.6, consumed=pos,
                    notes=["按 Tag/Len/Value 刚好能走完数据，像 TLV 帧"],
                )
    return None


def infer(data: bytes, max_candidates: int = 6) -> list[Candidate]:
    """返回按置信度排序的分帧参数候选。"""
    if len(data) < 8:
        return []
    candidates: list[Candidate] = []
    delimiter_candidates: list[Candidate] = []
    sample = data[:256 * 1024]

    # ---------------- 分隔符（先记下，只有当周期性结构不明显时才采纳） ---------------- #
    for token, label in ((b"\r\n", "0D 0A"), (b"\n", "0A"), (b"\x7e", "7E")):
        count = sample.count(token)
        if count >= 4:
            cfg = framer.FrameConfig(mode=framer.MODE_DELIMITER, delimiter_hex=label,
                                     include_delimiter=True, checksum_algo="none",
                                     header_hex="")
            delimiter_candidates.append(Candidate(
                title=f"分隔符分帧：{label}",
                config=cfg, frame_count=count, confidence=0.5,
                consumed=len(sample),
                notes=[f"数据里出现 {count} 次分隔符 {label}，像 ASCII 行协议"],
            ))
            break

    # ---------------- ① 自相关找周期（最可靠） ---------------- #
    for period, ratio in _detect_periods(sample):
        located = _find_header_at_period(sample, period)
        if located is None:
            continue
        offset, token, _score = located
        candidates.append(_make_fixed_candidate(data, period, offset, token, ratio))

    # ---------------- ② 周期性帧头（补充：帧头不一定占满周期） ---------------- #
    seen: set[bytes] = set()
    periodic: list[tuple[bytes, int, list[int], int, float]] = []
    for size, top in ((2, 25), (1, 8), (3, 12), (4, 15)):
        for token, _count in _frequent_tokens(sample, size, top):
            if token in seen:
                continue
            seen.add(token)
            positions = _positions(sample, token)
            if len(positions) < 4:
                continue
            stats = _delta_stats(positions)
            if not stats:
                continue
            delta, hits, total = stats
            if delta < 4 or delta > max(8, len(sample) // 4):
                continue
            ratio = hits / total
            if ratio >= 0.7:
                periodic.append((token, delta, positions, hits, ratio))
    # 同间隔下只保留最短的那个片段（"EB 90" 比 "EB 90 97 00" 更像真帧头）
    primitive: list[tuple[bytes, int, list[int], int, float]] = []
    for item in sorted(periodic, key=lambda entry: (len(entry[0]), -entry[4], -len(entry[2]))):
        token, delta = item[0], item[1]
        if any(token.startswith(kept[0]) and kept[1] == delta for kept in primitive):
            continue
        primitive.append(item)
    periodic = primitive

    # 选代表 token：定长用最短的；找长度字段要优先长的（真帧头一般 >= 2 字节，
    # 否则 1 字节的"伪帧头"（如长度字节本身）会占满名额、把真帧头挤掉）
    by_len_asc = sorted(periodic, key=lambda e: (len(e[0]), -e[4], -len(e[2])))
    by_len_desc = sorted(periodic, key=lambda e: (-len(e[0]), -e[4], -len(e[2])))
    fixed_tokens = by_len_asc[:3]
    length_tokens = by_len_desc[:2] + [t for t in by_len_asc[:2] if t not in by_len_desc[:2]]
    budget = 24

    for token, delta, positions, hits, ratio in fixed_tokens:
        if any(item.config.mode == framer.MODE_FIXED and item.config.fixed_len == delta
               and item.config.skip_bytes == (positions[0] if positions[0] < delta else 0)
               for item in candidates):
            continue      # 自相关已经给出同周期候选，跳过频率法重复项
        # ---- 定长候选 ---- #
        cfg = framer.FrameConfig(mode=framer.MODE_FIXED, fixed_len=delta,
                                 header_hex=_hex(token), checksum_algo="none",
                                 skip_bytes=positions[0] if positions and positions[0] < delta else 0)
        split = framer.split_frames(data, cfg)
        valid = [f for f in split.frames if f.size == delta]
        confidence = 0.45 * ratio + 0.25 * min(1.0, len(valid) / 50)
        if not positions[0]:
            confidence += 0.18          # 帧头就在数据开头，比"要跳过前导字节"的更可信
        notes = [
            f"帧头 {_hex(token)} 出现 {len(positions)} 次，间隔恒定 {delta} 字节（{hits}/{len(positions) - 1}）",
        ]
        if positions and positions[0] > 0:
            notes.append(f"第一帧之前有 {positions[0]} 字节前导数据，已自动跳过")
        algo, order, algo_hits = _probe_checksum(valid, cfg)
        if algo != "none" and algo_hits >= max(2, len(valid[:60]) * 0.9):
            cfg.checksum_algo = algo
            cfg.checksum_byte_order = order
            confidence = min(1.0, confidence + 0.15)
            notes.append(f"帧尾校验匹配 {checksum.ALGO_LABELS.get(algo, algo)}"
                         f"（{'大端' if order == 'big' else '小端'}，{algo_hits} 帧全中）")
        else:
            notes.append("帧尾没匹配到任何标准校验算法，可能无校验或使用私有校验")
        const, varying = _constant_positions(valid)
        if const:
            head = const[:16]
            notes.append("跨帧恒定字节位置：" + ", ".join(str(i) for i in head)
                         + (" …" if len(const) > 16 else ""))
            notes.extend(_length_field_notes(valid, head, delta, len(token)))
        if varying:
            notes.append(f"变化字节 {len(varying)} 个（偏移 "
                         + ", ".join(str(i) for i in varying[:12])
                         + (" …）" if len(varying) > 12 else "）"))
        candidates.append(Candidate(
            title=f"定长帧 {delta} 字节（帧头 {_hex(token)}）",
            config=cfg, frame_count=len(valid), confidence=min(1.0, confidence),
            consumed=len(data), notes=notes,
        ))

    # ---- 帧头 + 长度字段候选（用长 token 当代表，样本窗口内搜索，控制耗时） ---- #
    for token, _delta, positions, _hits, _ratio in length_tokens:
        if budget <= 0:
            break
        for len_offset in range(0, 9):
            for len_size in (1, 2, 4):
                for order in ("big", "little"):
                    if budget <= 0:
                        break
                    pairs = []
                    for first, second in zip(positions, positions[1:]):
                        at = first + len_offset
                        if at + len_size > len(sample) or second > len(sample):
                            continue
                        value = int.from_bytes(sample[at:at + len_size], order)
                        pairs.append((value, second - first))
                    if len(pairs) < 4:
                        continue
                    for semantic, add, inc_header in (
                        ("长度值 = 帧总长", 0, True),
                        ("长度值从长度字段起算", len_offset, False),
                    ):
                        if len_offset and add != len_offset and add != 0:
                            continue
                        matched = sum(1 for value, delta_i in pairs if value + add == delta_i)
                        if matched / len(pairs) < 0.8 or matched < 4:
                            continue
                        header = sample[positions[0]:positions[0] + len_offset] if len_offset else token
                        if len_offset and len(header) != len_offset:
                            continue
                        cfg2 = framer.FrameConfig(
                            mode=framer.MODE_HEADER,
                            header_hex=_hex(header) if header else "",
                            len_offset=len_offset, len_size=len_size, len_byte_order=order,
                            len_includes_header=inc_header, len_includes_checksum=True,
                            checksum_algo="none",
                            skip_bytes=positions[0] if positions and positions[0] > 0 else 0,
                        )
                        split2 = framer.split_frames(sample, cfg2)
                        valid2 = split2.frames
                        if len(valid2) < 3:
                            continue
                        budget -= 1
                        algo2, order2, hits2 = _probe_checksum(valid2, cfg2)
                        extra = ""
                        conf = 0.5 + 0.3 * (matched / len(pairs))
                        if algo2 != "none" and hits2 >= max(2, len(valid2[:60]) * 0.9):
                            cfg2.checksum_algo = algo2
                            cfg2.checksum_byte_order = order2
                            conf = min(1.0, conf + 0.2)
                            extra = f"，校验 {checksum.ALGO_LABELS.get(algo2, algo2)}"
                        candidates.append(Candidate(
                            title=(f"帧头 {_hex(header)} + {len_size} 字节长度字段（偏移 {len_offset}，"
                                   f"{'大端' if order == 'big' else '小端'}）"),
                            config=cfg2, frame_count=len(valid2), confidence=min(1.0, conf),
                            consumed=split2.total_bytes,
                            notes=[f"{semantic}：{matched}/{len(pairs)} 组帧间隔吻合{extra}"],
                        ))
                        break   # 同一 (offset, size, order) 只留一种语义

    tlv = _try_tlv(sample)
    if tlv is not None:
        tlv.config.header_hex = ""
        candidates.append(tlv)

    # 周期性结构不明显时，才把分隔符候选摆上来（否则数据里偶然出现 0x0A 会误导）
    if not any(item.confidence >= 0.7 for item in candidates):
        candidates.extend(delimiter_candidates)

    # ---------------- 兜底：整包 ---------------- #
    candidates.append(Candidate(
        title="整包（不拆分）",
        config=framer.FrameConfig(mode=framer.MODE_WHOLE, checksum_algo="none", header_hex=""),
        frame_count=1, confidence=0.1, consumed=len(data),
        notes=["没有找到周期性结构，先用整包逐个字节看"],
    ))

    # 去重 + 排序
    unique: dict[str, Candidate] = {}
    for item in candidates:
        key = f"{item.config.mode}|{item.config.fixed_len}|{item.config.header_hex}|" \
              f"{item.config.len_offset}|{item.config.len_size}|{item.config.delimiter_hex}"
        if key not in unique or unique[key].confidence < item.confidence:
            unique[key] = item
    # 已经有很可信的候选时，把那些"凑巧周期"的低分候选丢掉，别干扰判断
    if any(item.confidence >= 0.75 for item in unique.values()):
        pool = [item for item in unique.values() if item.confidence >= 0.5]
    else:
        pool = list(unique.values())
    ordered = sorted(pool, key=lambda c: (-c.confidence, -c.frame_count,
                                          len(c.config.header_hex)))
    return ordered[:max_candidates]
