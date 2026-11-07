"""frame_parser 命令行模式：不开界面，直接把抓包/日志转成表格或 JSON。

    python cli.py --list-presets
    python cli.py --list-modes
    python cli.py --infer --file data/backward_sample.txt
    python cli.py --preset backward_eb90 --file data/backward_sample.txt --limit 50
    python cli.py --preset aa55_crc16 --file data/aa55_crc16.bin --out frames.csv
    python cli.py --auto-extract --file logs/sample.jsonl --extract-filter 0x1E7 \
                  --mode fixed --fixed-len 8 --out can_1e7.csv
    python cli.py --probe --file data/fixed_xor8.bin --mode fixed --fixed-len 10
    python cli.py --preset backward_eb90 --file data/backward_sample.txt --frame-range 500-1500
    python cli.py --preset backward_eb90 --file data/backward_sample.txt --every 100 --only-fail

退出码：0 成功；1 出错；3 解析成功但有帧校验失败（方便写脚本判断）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import (  # noqa: E402
    analysis, checksum, codec, config_io, extract, field_parser, framer, presets,
)
from app.field_parser import FieldDef  # noqa: E402

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CHECK_FAIL = 3

RADIX_CHOICES = ["auto", "hex", "dec", "bin", "ascii"]
MODE_CHOICES = [key for key, _label in framer.MODE_ITEMS]
ALGO_CHOICES = [key for key, _label, _size, _order in checksum.ALGOS]
FORMAT_CHOICES = ["table", "json", "csv", "hex", "hexdump", "bin", "summary"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="frame_parser 命令行模式：多进制 / 多帧格式的数据帧解析",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    source = parser.add_argument_group("数据来源")
    source.add_argument("--file", "-f", help="输入文件（二进制按 HEX 读，可按 --auto-extract 从日志提取）")
    source.add_argument("--text", "-t", help="直接给一段文本数据")
    source.add_argument("--stdin", action="store_true", help="从标准输入读数据")
    source.add_argument("--radix", choices=RADIX_CHOICES, default="auto", help="输入进制，默认自动识别")
    source.add_argument("--dec-width", type=int, default=0, choices=[0, 1, 2, 4],
                        help="十进制输入时每个值占几字节（0=自动）")
    source.add_argument("--byte-order", choices=["big", "little"], default="big", help="十进制输入的字节序")

    extract_group = parser.add_argument_group("从日志提取（JSONL / CSV / 文本）")
    extract_group.add_argument("--auto-extract", action="store_true", help="自动挑规则提取数据段")
    extract_group.add_argument("--extract-pattern", help="自定义正则")
    extract_group.add_argument("--extract-group", type=int, default=1, help="捕获组序号（默认 1）")
    extract_group.add_argument("--extract-filter", default="", help="行过滤关键字，例如 can_id 0x1E7")
    extract_group.add_argument("--extract-limit", type=int, default=20000, help="最多提取多少帧")
    extract_group.add_argument("--read-mb", type=int, default=8,
                               help="读取文件开头上限（MB；0 = 读整个文件，默认 8）")

    frame = parser.add_argument_group("分帧设置")
    frame.add_argument("--preset", help="套用内置预设（见 --list-presets）")
    frame.add_argument("--mode", choices=MODE_CHOICES, help="分帧方式")
    frame.add_argument("--fixed-len", type=int, help="定长帧长度")
    frame.add_argument("--header", help="帧头 HEX，例如 'AA 55'")
    frame.add_argument("--len-offset", type=int, help="长度字段偏移")
    frame.add_argument("--len-size", type=int, choices=[1, 2, 4, 8], help="长度字段字节数")
    frame.add_argument("--len-order", choices=["big", "little"], help="长度字段字节序")
    frame.add_argument("--len-no-header", action="store_true", help="长度值不含帧头")
    frame.add_argument("--len-no-checksum", action="store_true", help="长度值不含校验")
    frame.add_argument("--delimiter", help="分隔符 HEX，例如 '0D 0A'")
    frame.add_argument("--no-delimiter-in-frame", action="store_true", help="分隔符不算在帧内")
    frame.add_argument("--prefix-size", type=int, choices=[1, 2, 4], help="长度前缀字节数")
    frame.add_argument("--prefix-order", choices=["big", "little"], help="长度前缀字节序")
    frame.add_argument("--prefix-self", action="store_true", help="长度前缀包含自身")
    frame.add_argument("--tag-size", type=int, help="TLV Tag 字节数")
    frame.add_argument("--tlv-len-size", type=int, choices=[1, 2, 4], help="TLV Len 字节数")
    frame.add_argument("--tlv-len-order", choices=["big", "little"], help="TLV Len 字节序")
    frame.add_argument("--skip-bytes", type=int, help="分帧前跳过多少字节")
    frame.add_argument("--checksum", choices=ALGO_CHOICES, help="校验算法")
    frame.add_argument("--checksum-order", choices=["big", "little"], help="校验字节序")
    frame.add_argument("--checksum-where", choices=["tail", "after_len"], help="校验位置")

    rows = parser.add_argument_group("行规则（挑哪些帧；帧号同界面「帧#」，从 0 开始，-1 = 最后一帧）")
    rows.add_argument("--frame-range", metavar="A-B",
                      help="只看帧号 A ~ B 这一段：500-1500 / 1000-（到末尾）/ -500-（最后 500 帧）")
    rows.add_argument("--every", type=int, metavar="N", help="每 N 帧取 1 帧（抽样看趋势）")
    rows.add_argument("--only-fail", action="store_true", help="只保留校验失败的帧")
    rows.add_argument("--only-ok", action="store_true", help="只保留校验通过的帧")
    rows.add_argument("--max-frames", type=int, metavar="N",
                      help="单次最多分出多少帧；0 = 不限（默认）。想快速预览超大抓包时设个上限")

    fields = parser.add_argument_group("字段与输出")
    fields.add_argument("--fields", help="字段定义 JSON 文件（界面里「导出字段 JSON」生成）")
    fields.add_argument("--out", "-o", help="输出文件；不写就打印到屏幕")
    fields.add_argument("--format", choices=FORMAT_CHOICES, default="table", help="输出格式")
    fields.add_argument("--csv-mode", choices=["frames", "fields"], default="frames",
                        help="CSV 布局：frames=一帧一行（字段做列），fields=一个字段一行")
    fields.add_argument("--limit", type=int, default=0, help="最多处理/输出多少帧（0=全部）")
    fields.add_argument("--quiet", "-q", action="store_true", help="只在出错时输出")

    tools = parser.add_argument_group("分析工具")
    tools.add_argument("--infer", action="store_true", help="只做分帧参数推断并打印候选")
    tools.add_argument("--probe", action="store_true", help="试算校验算法并打印匹配排名")
    tools.add_argument("--list-presets", action="store_true", help="列出内置预设")
    tools.add_argument("--list-modes", action="store_true", help="列出分帧方式与校验算法")
    return parser


# --------------------------------------------------------------------------- #
def open_text_source(args):
    """大文本文件走 mmap 分页来源（不整篇读进内存）；其余情况返回 None。"""
    if not args.file or (args.auto_extract or args.extract_pattern):
        return None
    path = Path(args.file)
    if not path.is_file() or path.suffix.lower() in (".bin", ".dat", ".hex"):
        return None
    if args.radix == "hex":
        return None
    size = path.stat().st_size
    if args.read_mb > 0 and size <= max(args.read_mb, 1) * 1024 * 1024:
        return None                      # 文件不大，照旧整篇读（行为完全不变）
    from app import source as source_mod

    return source_mod.TextSource(path=str(path))


def load_payload(args) -> tuple[bytes, str]:
    """返回 (字节, 来源说明)。"""
    if args.text:
        text, source = args.text, "命令行文本"
    elif args.stdin:
        text, source = sys.stdin.read(), "标准输入"
    elif args.file:
        path = Path(args.file)
        if not path.is_file():
            raise SystemExit(f"[错误] 文件不存在：{path}")
        # --read-mb 0 = 读整个文件（128 MB 抓包也能一次读完）
        limit = 0 if args.read_mb <= 0 else args.read_mb * 1024 * 1024
        size_mb = path.stat().st_size / 1048576
        # .bin/.dat/.hex 是原始二进制：直接转成 HEX 文本再交给解析器
        if path.suffix.lower() in (".bin", ".dat", ".hex") or args.radix == "hex":
            with path.open("rb") as handle:
                blob = handle.read(limit) if limit else handle.read()
            truncated = path.stat().st_size > len(blob)
            text = codec.bytes_to_text(blob, codec.RADIX_HEX, sep="\n")
            args.radix = "hex"
            source = (f"{path.name}（二进制 {len(blob)} 字节"
                      f"{'（已截断）' if truncated else ''}，按 HEX 读取）")
        else:
            text, truncated = extract.read_text_head(str(path), limit)
            read_note = "全部" if not limit else f"{args.read_mb} MB"
            source = (f"{path.name}（{size_mb:.1f} MB，读取 {read_note}"
                      f"{'（已截断）' if truncated else ''}）")
    else:
        raise SystemExit("[错误] 需要 --file / --text / --stdin 之一；用 --help 看用法。")

    if args.auto_extract or args.extract_pattern:
        if args.extract_pattern:
            pattern = args.extract_pattern
            name = "自定义正则"
            normalize = True
        else:
            rule = extract.PATTERN_PRESETS[0]
            name, pattern, normalize, _desc = rule
        result = extract.extract(text, pattern, args.extract_group, args.extract_filter,
                                 args.extract_limit, normalize, name)
        if not result.ok:
            raise SystemExit(f"[错误] 提取失败：{result.summary()}")
        if not args.quiet:
            print(f"[提取] {result.summary()}")
        text = "\n".join(result.lines)
        args.radix = "hex"

    data, warnings = codec.text_to_bytes_ex(
        text, args.radix, args.dec_width, args.byte_order, skip_bad_lines=True
    )
    if not args.quiet:
        for warning in warnings[:10]:
            print(f"[警告] {warning}")
    if not data:
        raise SystemExit("[错误] 解析后没有数据，检查 --radix 是否选对。")
    used = codec.resolve_radix(text, args.radix)
    return data, f"{source}｜{len(data)} 字节｜进制 {used}"


def build_config(args) -> framer.FrameConfig:
    config = framer.FrameConfig()
    if args.preset:
        preset = presets.PRESETS.get(args.preset)
        if preset is None:
            raise SystemExit(f"[错误] 没有这个预设：{args.preset}（用 --list-presets 看可用的）")
        config = framer.FrameConfig.from_dict(preset.frame)
    overrides = {
        "mode": args.mode, "fixed_len": args.fixed_len, "header_hex": args.header,
        "len_offset": args.len_offset, "len_size": args.len_size, "len_byte_order": args.len_order,
        "delimiter_hex": args.delimiter, "prefix_size": args.prefix_size,
        "prefix_byte_order": args.prefix_order, "tag_size": args.tag_size,
        "value_len_size": args.tlv_len_size, "value_len_byte_order": args.tlv_len_order,
        "skip_bytes": args.skip_bytes, "checksum_algo": args.checksum,
        "checksum_byte_order": args.checksum_order, "checksum_where": args.checksum_where,
    }
    for key, value in overrides.items():
        if value is not None:
            setattr(config, key, value)
    if args.len_no_header:
        config.len_includes_header = False
    if args.len_no_checksum:
        config.len_includes_checksum = False
    if args.no_delimiter_in_frame:
        config.include_delimiter = False
    if args.prefix_self:
        config.prefix_includes_self = True
    if args.max_frames is not None:
        config.max_frames = max(0, int(args.max_frames))
    if args.limit:                       # --limit 同时限制分帧多少（0 = 全部）
        config.max_frames = args.limit
    return config


def load_fields(args) -> list[FieldDef]:
    if args.fields:
        data = json.loads(Path(args.fields).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("fields", [])
        print(f"[字段] 从 {args.fields} 读了 {len(data)} 个字段定义")
        return [FieldDef.from_dict(item) for item in data]
    if args.preset:
        preset = presets.PRESETS.get(args.preset)
        if preset:
            print(f"[字段] 使用预设「{preset.name}」自带的 {len(preset.fields)} 个字段")
            return [FieldDef.from_dict(item) for item in preset.fields]
    return []


# --------------------------------------------------------------------------- #
def display_width(text: str) -> int:
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def render_table(headers: list[str], rows: list[list[str]], limit: int = 40) -> str:
    widths = [display_width(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], display_width(cell))
    lines = ["  ".join(pad(h, widths[i]) for i, h in enumerate(headers))]
    lines.append("-" * (sum(widths) + 2 * (len(widths) - 1)))
    for row in rows[:limit]:
        lines.append("  ".join(pad(cell, widths[i]) for i, cell in enumerate(row)))
    if len(rows) > limit:
        lines.append(f"… 共 {len(rows)} 行，只显示前 {limit} 行")
    return "\n".join(lines)


def run(args) -> int:
    if args.list_presets:
        print("内置预设：")
        for key, preset in presets.PRESETS.items():
            print(f"  {key:18s} {preset.name}")
            print(f"  {'':18s} {preset.description}")
        return EXIT_OK
    if args.list_modes:
        print("分帧方式：")
        for key, label in framer.MODE_ITEMS:
            print(f"  {key:12s} {label}")
        print("\n校验算法：")
        for key, label, size, order in checksum.ALGOS:
            print(f"  {key:20s} {label}（{size} 字节，默认 {order}）")
        print("\n输入进制：auto / hex / dec / bin / ascii")
        return EXIT_OK

    text_source = open_text_source(args)
    if text_source is not None:
        data, source = b"", text_source.source_label
    else:
        data, source = load_payload(args)
    if not args.quiet:
        print(f"[输入] {source}")

    if args.infer:
        from app import infer as infer_mod
        sample_data = data
        if text_source is not None:      # 大文件：用开头一段做推断（别为此读整个文件）
            sample_text = text_source.sample_text(max_lines=2000, max_chars=200000)
            sample_data, _warn = codec.text_to_bytes_ex(sample_text, args.radix)
        candidates = infer_mod.infer(sample_data)
        if not candidates:
            print("[推断] 数据太少，没有候选。")
            return EXIT_ERROR
        print(f"\n[推断] 共 {len(candidates)} 个候选（按置信度排序）：")
        for index, candidate in enumerate(candidates, 1):
            print(f"\n  {index}. {candidate.title}　置信度 {candidate.percent}%　"
                  f"{candidate.frame_count} 帧")
            print(f"     配置：{infer_mod.describe_config(candidate.config)}")
            for note in candidate.notes:
                print(f"     · {note}")
        best = candidates[0]
        if best.config.mode == framer.MODE_FIXED:
            print(f"\n[建议] 直接跑：python cli.py --file <数据> --mode fixed "
                  f"--fixed-len {best.config.fixed_len}"
                  + (f" --header \"{best.config.header_hex}\"" if best.config.header_hex else ""))
        elif best.config.mode == framer.MODE_HEADER:
            print(f"\n[建议] 直接跑：python cli.py --file <数据> --mode header "
                  f"--header \"{best.config.header_hex}\" --len-offset {best.config.len_offset} "
                  f"--len-size {best.config.len_size} --len-order {best.config.len_byte_order}")
        return EXIT_OK

    config = build_config(args)
    try:
        if text_source is not None:
            # 大文件：mmap + 流式解码 + 流式分帧，只留索引（内存不随文件增长）
            if not args.quiet:
                print(f"[大文件] {text_source.source_label}，"
                      f"{text_source.line_count:,} 行 —— 边读边解码边分帧")
            split = text_source.build_index(config, args.radix)
        else:
            split = framer.split_frames(data, config)
    except (framer.FrameConfigError, codec.CodecError) as exc:
        raise SystemExit(f"[错误] 分帧失败：{exc}") from exc

    row_rule_note = apply_row_rules(split, args)     # 行规则：先筛帧，后面所有输出都按筛完的算
    if row_rule_note and not args.quiet:
        print(row_rule_note)

    if args.probe:
        sample = [f.data for f in split.frames[:80]]
        rows = []
        for key, label, size, _order in checksum.ALGOS:
            if not size:
                continue
            for order in ("big", "little"):
                usable = [chunk for chunk in sample if len(chunk) > size]
                if not usable:
                    continue
                hits = sum(1 for chunk in usable
                           if checksum.compute(key, chunk[:-size], order) == chunk[-size:])
                rows.append([label, "大端" if order == "big" else "小端",
                             f"{hits} / {len(usable)}"])
        rows.sort(key=lambda row: -int(row[2].split("/")[0]))
        print(f"\n[试算校验] 基于 {len(sample)} 帧的帧尾校验：")
        print(render_table(["算法", "字节序", "匹配帧数"], rows, limit=len(rows)))
        return EXIT_OK

    fields = load_fields(args)
    parsed_by_frame: dict[int, list] = {}
    for frame in split.frames:
        parsed_by_frame[frame.index] = field_parser.parse_fields(frame.data, fields, frame.index)

    if not args.quiet:
        # 只有"帧头"这一种分帧方式才真正依赖帧头，别的模式打出来会误导
        head = (f"，帧头 {config.header_hex}"
                if config.mode == framer.MODE_HEADER and config.header_hex else "")
        print(f"[分帧] {framer.MODE_LABELS.get(config.mode, config.mode)}" + head
              + f"｜{len(split.frames)} 帧 / {split.total_bytes} 字节"
              f"｜校验通过 {split.ok_count}，失败 {split.fail_count}")
        for warning in split.warnings[:10]:
            print(f"[警告] {warning}")

    exit_code = EXIT_CHECK_FAIL if split.fail_count else EXIT_OK

    # ---------------- 输出 ---------------- #
    if args.format == "bin":
        payload = b"".join(frame.data for frame in split.frames)
        return write_out(args, payload, binary=True, exit_code=exit_code)
    if args.format == "hex":
        text = "\n".join(codec.bytes_to_text(frame.data, codec.RADIX_HEX) for frame in split.frames)
        return write_out(args, text, exit_code=exit_code)
    if args.format == "hexdump":
        text = "\n\n".join(f"#{frame.index} @{frame.offset}\n{codec.hexdump(frame.data)}"
                           for frame in split.frames)
        return write_out(args, text, exit_code=exit_code)
    if args.format == "json":
        document = config_io.build_result_document(
            {"source": source, "radix": args.radix}, config, split, parsed_by_frame, None,
            limit_frames=args.limit or None,
        )
        text = json.dumps(document, ensure_ascii=False, indent=2, default=str)
        return write_out(args, text, exit_code=exit_code)
    if args.format == "csv":
        text = render_csv(split, parsed_by_frame, fields, args.csv_mode)
        return write_out(args, text, exit_code=exit_code)
    if args.format == "summary":
        return write_out(args, render_summary(split, parsed_by_frame, fields), exit_code=exit_code)

    text = render_table(*wide_table(split, parsed_by_frame, fields),
                        limit=args.limit or 40)
    return write_out(args, text, exit_code=exit_code)


def wide_table(split, parsed_by_frame, fields):
    headers = ["帧#", "偏移", "长度", "校验"] + [fd.name for fd in fields]
    rows = []
    for frame in split.frames:
        parsed = parsed_by_frame.get(frame.index, [])
        row = [str(frame.index), str(frame.offset), str(frame.size),
               {"ok": "通过", "fail": "失败", "na": "—"}.get(frame.check_state, frame.check_state)]
        for item in parsed:
            row.append(field_parser.format_value(item.value, item.radix) + (item.unit or ""))
        rows.append(row)
    return headers, rows


def render_csv(split, parsed_by_frame, fields, csv_mode: str) -> str:
    from app import exporters
    if csv_mode == "fields":
        headers = ["帧#", "字段", "类型", "偏移", "长度", "原始HEX", "原始值", "解析值", "单位", "备注"]
        rows = []
        for frame in split.frames:
            for item in parsed_by_frame.get(frame.index, []):
                rows.append([
                    str(frame.index), item.name, item.type_label, str(item.offset), str(item.length),
                    item.raw_hex,
                    "" if item.raw_value is None else str(item.raw_value),
                    field_parser.format_value(item.value, item.radix), item.unit, item.note,
                ])
        return exporters.export_rows(headers, rows)
    headers, rows = wide_table(split, parsed_by_frame, fields)
    return exporters.export_rows(headers, rows)


def render_summary(split, parsed_by_frame, fields) -> str:
    lines = [
        f"帧数        : {len(split.frames)}",
        f"总字节      : {split.total_bytes}",
        f"校验        : 通过 {split.ok_count} / 失败 {split.fail_count}",
        f"字段数      : {len(fields)}",
    ]
    for frame in split.frames[:5]:
        parsed = parsed_by_frame.get(frame.index, [])
        text = "  ".join(f"{item.name}={field_parser.format_value(item.value, item.radix)}"
                         for item in parsed if item.value is not None)
        lines.append(f"  帧#{frame.index} @{frame.offset} ({frame.size}B) {text}")
    return "\n".join(lines)


def write_out(args, payload, binary: bool = False, exit_code: int = EXIT_OK) -> int:
    if args.out:
        path = Path(args.out)
        if binary:
            path.write_bytes(payload)
        else:
            path.write_text(payload, encoding="utf-8-sig" if path.suffix.lower() == ".csv" else "utf-8")
        if not args.quiet:
            print(f"[输出] 已写入 {path}（{len(payload)} {'字节' if binary else '字符'}）")
    else:
        # 没有 --out 时，数据本身就是结果：始终打印（--quiet 只压日志）
        print()
        print(payload if isinstance(payload, str) else f"<{len(payload)} 字节二进制>")
    return exit_code


def build_row_rules(args) -> list:
    """把 --frame-range / --every / --only-fail / --only-ok 变成行规则。"""
    rules: list = []
    text = (getattr(args, "frame_range", "") or "").strip()
    if text:
        match = re.match(r"^(-?\d*)\s*[-~到]\s*(-?\d*)$", text)
        if not match:
            raise SystemExit(f"[错误] --frame-range 写法应为 A-B（如 500-1500、1000-、-500-），收到：{text}")
        first, second = match.group(1), match.group(2)
        start = int(first) if first not in ("", "-") else 0
        end = int(second) if second not in ("", "-") else -1
        rules.append(analysis.RowRule(kind="range", start=start, end=end))
    if getattr(args, "every", 0):
        rules.append(analysis.RowRule(kind="step", step=max(1, int(args.every))))
    if getattr(args, "only_fail", False):
        rules.append(analysis.RowRule(kind="check", state="fail"))
    if getattr(args, "only_ok", False):
        rules.append(analysis.RowRule(kind="check", state="ok"))
    return rules


def apply_row_rules(split, args) -> str:
    """按行规则过滤 split.frames（换成"筛完的惰性视图"），返回给用户看的一句话。"""
    rules = build_row_rules(args)
    if not rules:
        return ""
    total = len(split.frames)
    positions = analysis.match_row_positions(split.frames, rules, "all")
    split.replace_frames(split.frames.select(positions))
    kept = len(split.frames)
    return (f"[行规则] {analysis.describe_row_rules(rules, 'all', total)}"
            f" → 保留 {kept} / {total} 帧（去掉 {total - kept} 帧）")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code)
            return EXIT_ERROR
        return int(exc.code or 0)
    except KeyboardInterrupt:
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
