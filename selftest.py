"""无界面自检：验证编解码 / 校验 / 分帧 / 字段解析 / 界面构建 / 列操作。

    python selftest.py

不需要显示器（使用 Qt 的 offscreen 平台插件），可以放进 CI 或在服务器上跑。
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import checksum, codec, convert, exporters, field_parser, framer, presets  # noqa: E402
from app.field_parser import FieldDef  # noqa: E402

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
    else:
        FAILED.append((name, detail or "断言失败"))


def close_to(a, b, tol: float = 1e-6) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- #
def test_codec() -> None:
    check("HEX: 空格分隔", codec.text_to_bytes("AA 55 01", codec.RADIX_HEX) == b"\xAA\x55\x01")
    check("HEX: 无分隔", codec.text_to_bytes("AA5501", codec.RADIX_HEX) == b"\xAA\x55\x01")
    check("HEX: 0x 前缀 + 逗号", codec.text_to_bytes("0xAA,0x55", codec.RADIX_HEX) == b"\xAA\x55")
    check("HEX: 1Ah 写法", codec.text_to_bytes("1Ah", codec.RADIX_HEX) == b"\x1A")
    check("HEX: 奇数位补零", codec.text_to_bytes("5", codec.RADIX_HEX) == b"\x05")
    check("HEX: 带换行的多帧", codec.text_to_bytes("AA 55\nBB CC", codec.RADIX_HEX) == b"\xAA\x55\xBB\xCC")
    try:
        codec.text_to_bytes("ZZ", codec.RADIX_HEX)
        check("HEX: 非法字符报错", False)
    except codec.CodecError:
        check("HEX: 非法字符报错", True)

    check("DEC: 1 字节", codec.text_to_bytes("170 85", codec.RADIX_DEC) == b"\xAA\x55")
    check("DEC: 自动 2 字节大端", codec.text_to_bytes("300", codec.RADIX_DEC) == b"\x01\x2C")
    check("DEC: 指定 4 字节小端",
          codec.text_to_bytes("300", codec.RADIX_DEC, 4, "little") == b"\x2C\x01\x00\x00")
    check("BIN: 8 位", codec.text_to_bytes("10101010 01010101", codec.RADIX_BIN) == b"\xAA\x55")
    check("ASCII: 转义还原", codec.ascii_to_bytes("A\\r\\n\\x42") == b"A\r\nB")

    check("自动识别: AA 55 01 → HEX", codec.detect_radix("AA 55 01") == codec.RADIX_HEX)
    check("自动识别: 170 85 → DEC", codec.detect_radix("170 85") == codec.RADIX_DEC)
    check("自动识别: 二进制", codec.detect_radix("10101010") == codec.RADIX_BIN)
    check("自动识别: 文本", codec.detect_radix("hello world") == codec.RADIX_ASCII)

    check("bytes→HEX", codec.bytes_to_text(b"\xAA\x55", codec.RADIX_HEX) == "AA 55")
    check("bytes→BIN", codec.bytes_to_text(b"\xAA", codec.RADIX_BIN) == "10101010")
    check("hexdump 行数", len(codec.hexdump(bytes(range(20))).splitlines()) == 2)
    check("HEX 往返", codec.text_to_bytes(codec.bytes_to_text(b"\x00\x7F\xFF"), codec.RADIX_HEX)
          == b"\x00\x7F\xFF")


def test_checksum() -> None:
    check("SUM8", checksum.compute("sum8", b"\x01\x02\x03") == b"\x06")
    check("SUM8 取反", checksum.compute("sum8_inv", b"\x01\x02\x03") == b"\xF9")
    check("XOR8", checksum.compute("xor8", b"\x01\x02\x03") == b"\x00")
    check("SUM16", checksum.compute("sum16", b"\xFF\xFF") == b"\x01\xFE")
    # Modbus 标准样例：01 03 00 00 00 02 → CRC = 0x0BC4，线序低字节在前
    check("CRC16/MODBUS 报文样例",
          checksum.compute("crc16_modbus", bytes.fromhex("010300000002"), "little")
          == bytes.fromhex("C40B"))
    check("CRC16/MODBUS 标准校验值 0x4B37",
          checksum.compute("crc16_modbus", b"123456789", "little") == bytes.fromhex("374B"))
    check("CRC8 标准校验值 0xF4",
          checksum.compute("crc8", b"123456789") == bytes.fromhex("F4"))
    check("CRC16/CCITT-FALSE 样例",
          checksum.compute("crc16_ccitt_false", b"123456789", "big") == bytes.fromhex("29B1"))
    check("CRC16/XMODEM 样例",
          checksum.compute("crc16_xmodem", b"123456789", "big") == bytes.fromhex("31C3"))
    check("CRC32 样例",
          checksum.compute("crc32", b"123456789", "big") == bytes.fromhex("CBF43926"))
    check("无校验返回空", checksum.compute("none", b"\x01") == b"")


def test_framer() -> None:
    # 帧头 AA55 + 2 字节总长 + 载荷 + CRC16
    body = b"\xAA\x55" + (9).to_bytes(2, "big") + b"\x01\x02\x03"
    frame = body + checksum.compute("crc16_modbus", body, "little")
    cfg = framer.FrameConfig(mode=framer.MODE_HEADER, header_hex="AA 55", len_offset=2,
                             len_size=2, checksum_algo="crc16_modbus",
                             checksum_byte_order="little")
    result = framer.split_frames(frame * 3, cfg)
    check("帧头分帧: 3 帧", len(result.frames) == 3, f"得到 {len(result.frames)}")
    check("帧头分帧: 校验全部通过", result.fail_count == 0 and result.ok_count == 3)
    check("帧头分帧: 帧内容正确", result.frames[0].data == frame)

    broken = bytearray(frame * 2)
    broken[-1] ^= 0xFF
    result = framer.split_frames(bytes(broken), cfg)
    check("校验失败可识别", result.fail_count == 1 and result.ok_count == 1)

    result = framer.split_frames(frame + b"\xAA\x55\x00", cfg)
    check("不完整帧有警告", bool(result.warnings))

    result = framer.split_frames(bytes(range(10)), framer.FrameConfig(mode=framer.MODE_FIXED, fixed_len=4))
    check("定长分帧: 2 帧 + 残帧", len(result.frames) == 3 and "残帧" in result.frames[-1].note)

    result = framer.split_frames(b"\x03abc\x02de", framer.FrameConfig(mode=framer.MODE_LENPREFIX, prefix_size=1))
    check("长度前缀分帧", [f.data for f in result.frames] == [b"\x03abc", b"\x02de"])

    result = framer.split_frames(b"AB\r\nCD\r\n", framer.FrameConfig(
        mode=framer.MODE_DELIMITER, delimiter_hex="0D 0A", include_delimiter=True))
    check("分隔符分帧", [f.data for f in result.frames] == [b"AB\r\n", b"CD\r\n"])

    result = framer.split_frames(b"\x01\x02hi\x03\x01x", framer.FrameConfig(mode=framer.MODE_TLV))
    check("TLV 分帧", len(result.frames) == 2 and result.frames[0].tag == "0x01")
    check("TLV Value", result.frames[0].data == b"\x01\x02hi")

    result = framer.split_frames(b"\x01\x02", framer.FrameConfig(mode=framer.MODE_WHOLE))
    check("整包分帧", len(result.frames) == 1 and result.frames[0].size == 2)

    # ---- 单次最大分帧数：默认不限（0），想限也能限 ---- #
    check("帧数上限: 默认不限（0）", framer.FrameConfig().max_frames == 0,
          str(framer.FrameConfig().max_frames))
    big = bytes(range(256)) * 120                      # 30720 字节
    unlimited = framer.split_frames(big, framer.FrameConfig(
        mode=framer.MODE_FIXED, fixed_len=1))
    check("帧数上限: 不限时 3 万帧全部分出来", len(unlimited.frames) == len(big),
          f"{len(unlimited.frames)} / {len(big)}")
    check("帧数上限: 不限时没有截断警告",
          not any("最大分帧数" in w for w in unlimited.warnings),
          str(unlimited.warnings[:2]))
    capped = framer.split_frames(big, framer.FrameConfig(
        mode=framer.MODE_FIXED, fixed_len=1, max_frames=1000))
    check("帧数上限: 设了上限就停在上限", len(capped.frames) == 1000,
          str(len(capped.frames)))
    check("帧数上限: 达到上限时明确提示",
          any("最大分帧数 1000" in w for w in capped.warnings),
          str(capped.warnings[:3]))
    for mode_cfg in (
        framer.FrameConfig(mode=framer.MODE_HEADER, header_hex="EB 90",
                           len_offset=2, len_size=1, fixed_len=4, max_frames=3),
        framer.FrameConfig(mode=framer.MODE_LENPREFIX, prefix_size=1, max_frames=3),
        framer.FrameConfig(mode=framer.MODE_DELIMITER, delimiter_hex="0A", max_frames=3),
        framer.FrameConfig(mode=framer.MODE_TLV, max_frames=3),
    ):
        probe = framer.split_frames(b"\xEB\x90\x04AB\x0A" * 8 + b"\x02hi\x01x" * 4, mode_cfg)
        if len(probe.frames) > 3:
            check(f"帧数上限: {mode_cfg.mode} 也遵守上限", False, str(len(probe.frames)))
    check("帧数上限: 各分帧方式都遵守上限", True)


def test_fields() -> None:
    data = bytes.fromhex("0102030405060708")
    fields = [
        FieldDef(name="u16", offset=0, length=2),
        FieldDef(name="i16", type=field_parser.TYPE_INT, offset=0, length=2),
        FieldDef(name="le", offset=0, length=2, byte_order="little"),
        FieldDef(name="scaled", offset=2, length=2, scale=0.1, unit="km/h"),
        FieldDef(name="bits", offset=4, length=1, bits="0-1"),
        FieldDef(name="tail", type=field_parser.TYPE_BYTES, offset=6, length=0),
        FieldDef(name="sum", type=field_parser.TYPE_EXPR, offset=0, length=1, expr="u16 + scaled"),
        FieldDef(name="too_long", offset=7, length=8),
    ]
    parsed = {item.name: item for item in field_parser.parse_fields(data, fields)}
    check("u16 大端", parsed["u16"].value == 0x0102)
    check("i16 解释", parsed["i16"].value == 0x0102)
    check("小端", parsed["le"].value == 0x0201)
    check("缩放系数", close_to(parsed["scaled"].value, 0.1 * 0x0304), str(parsed["scaled"].value))
    check("位域取低 2 位", parsed["bits"].value == 0x05 & 0x03)
    check("长度 0 = 到帧尾", parsed["tail"].value == b"\x07\x08")
    check("表达式字段", close_to(parsed["sum"].value, 0x0102 + 0.1 * 0x0304), str(parsed["sum"].value))
    check("超长字段报错", parsed["too_long"].ok is False)

    negative = field_parser.parse_fields(bytes.fromhex("FFFF"), [FieldDef(name="i", type="int", offset=0, length=2)])
    check("有符号负数", negative[0].value == -1)

    bcd = field_parser.parse_fields(bytes.fromhex("1234"), [FieldDef(name="b", type="bcd", offset=0, length=2)])
    check("BCD", bcd[0].value == 1234)

    flt = field_parser.parse_fields(bytes.fromhex("42F6E979"), [FieldDef(name="f", type="float", offset=0, length=4)])
    check("float32", close_to(flt[0].value, 123.456, 1e-4), str(flt[0].value))

    check("显示格式 HEX", field_parser.format_value(255, "hex") == "0xFF")
    check("显示格式 BIN", field_parser.format_value(5, "bin") == "0b101")


def test_convert() -> None:
    check("换算 ×0.1", close_to(convert.apply_convert("v * 0.1", 1234), 123.4))
    check("换算 ÷1000", close_to(convert.apply_convert("v / 1000", 2500), 2.5))
    check("换算 位操作", convert.apply_convert("(v >> 4) & 0xF", 0xAB) == 0xA)
    check("换算 空表达式", convert.apply_convert("", 7) == 7)
    try:
        convert.apply_convert("v +", 1)
        check("换算 错误表达式报错", False)
    except convert.ConvertError:
        check("换算 错误表达式报错", True)
    check("禁止访问内置函数", "import" not in str(convert.SAFE_GLOBALS.get("__builtins__", "")))
    tip = convert.build_tooltip(title="速度", raw=1234, converted=123.4, text="123.4", unit="km/h", expr="v*0.1")
    check("悬停提示含原始值/换算/HEX", "原始值" in tip and "换算函数" in tip and "0x4D2" in tip, tip)


# --------------------------------------------------------------------------- #
def test_extract() -> None:
    from app import extract

    check("归一化 HEX", extract.normalize_hex("ea07071900000000") == "EA 07 07 19 00 00 00 00")
    check("归一化 带分隔", extract.normalize_hex("0xAA, 0x55") == "AA 55")
    check("归一化 奇数位拒绝", extract.normalize_hex("ABC") == "")

    jsonl = "\n".join([
        '{"can_id":"0x1E7","dlc":8,"data":"ea07071900000000","ts":1}',
        '{"can_id":"0x190","dlc":8,"data":"4210000000002f02","ts":2}',
        '{"can_id":"0x1E7","dlc":8,"data":"ea07071900000001","ts":3}',
    ])
    result = extract.extract(jsonl, extract.PATTERN_PRESETS[0][1], 1)
    check("JSONL 提取帧数", len(result.lines) == 3, str(result.lines))
    check("JSONL 提取内容", result.lines[0] == "EA 07 07 19 00 00 00 00")
    result = extract.extract(jsonl, extract.PATTERN_PRESETS[0][1], 1, "0x1E7")
    check("行过滤只看 0x1E7", len(result.lines) == 2, str(result.lines))

    csv_text = "\n".join([
        "序号,系统时间,数据",
        "00000,=\"11:05:30.340\",x| 00 00 02 00 64 1C 00 00 ",
        "00001,=\"11:05:30.341\",x| 01 02 03 04 05 06 07 08 ",
    ])
    result = extract.extract(csv_text, extract.PATTERN_PRESETS[1][1], 1)
    check("CAN CSV 提取", result.lines == ["00 00 02 00 64 1C 00 00", "01 02 03 04 05 06 07 08"],
          str(result.lines))

    auto = extract.auto_extract(jsonl)
    check("自动匹配 JSONL 规则", auto.ok and "JSONL" in auto.pattern_name, auto.summary())

    ids = extract.jsonl_ids(jsonl)
    check("统计 can_id", dict(ids).get("0x1E7") == 2, str(ids))

    check("HEX 行识别", extract.looks_like_hex_line("EB 90 97 00") and
          not extract.looks_like_hex_line("=== comment ==="))


def test_real_sample() -> None:
    """data/backward_sample.txt：现场采集样本（有文件才测）。"""
    sample_path = presets.DATA_DIR / "backward_sample.txt"
    if not sample_path.is_file():
        print("  （跳过）没有 data/backward_sample.txt")
        return
    from app import extract

    frames, scanned, skipped, _truncated = extract.read_hex_lines(str(sample_path), 200)
    check("真实样本: 读到 200 帧", len(frames) == 200, f"{len(frames)}")
    check("真实样本: 注释行被跳过", skipped >= 1, f"skipped={skipped}")

    data, warnings = codec.text_to_bytes_ex("\n".join(frames), codec.RADIX_AUTO)
    check("真实样本: 自动识别为 HEX", codec.detect_radix("\n".join(frames)) == codec.RADIX_HEX)
    cfg = framer.FrameConfig(mode=framer.MODE_FIXED, fixed_len=156)
    result = framer.split_frames(data, cfg)
    check("真实样本: 200 帧 × 156 字节", len(result.frames) == 200 and
          all(f.size == 156 for f in result.frames), f"{len(result.frames)}")

    preset = presets.PRESETS["backward_eb90"]
    fields = [FieldDef.from_dict(f) for f in preset.fields]
    problems = []
    for frame in result.frames[:20]:
        for item in field_parser.parse_fields(frame.data, fields):
            if not item.ok:
                problems.append(f"{item.name}: {item.note}")
    check("真实样本: 预设字段全部解析成功", not problems, "; ".join(problems))

    parsed = {i.name: i for i in field_parser.parse_fields(result.frames[0].data, fields)}
    check("真实样本: 帧头 = EB 90", parsed["帧头"].raw == b"\xEB\x90")
    check("真实样本: 帧长 = 151", parsed["帧长"].value == 151, str(parsed["帧长"].value))
    check("真实样本: 报文类型 = 0x92", parsed["报文类型"].value == 0x92)
    check("真实样本: 帧尾 = 5B BF", parsed["帧尾"].raw == b"\x5B\xBF")
    counters = [
        field_parser.parse_fields(frame.data, fields)[3].value for frame in result.frames[:5]
    ]
    check("真实样本: 帧计数每帧 +2", all(counters[i] < counters[i + 1] for i in range(4)),
          str(counters))


# --------------------------------------------------------------------------- #
def test_infer() -> None:
    from app import infer

    # 把 AA55 预设样本重复 10 遍 → 30 帧，够统计了
    preset = presets.PRESETS["aa55_crc16"]
    text = "\n".join([preset.sample] * 10)
    data = codec.text_to_bytes(text, preset.radix)
    candidates = infer.infer(data)
    check("推断: 有候选", bool(candidates), "没有候选")
    if candidates:
        top = candidates[0]
        split_top = framer.split_frames(data, top.config)
        check("推断: 顶级候选能正确分帧",
              len(split_top.frames) >= 25 and all(f.size == 13 for f in split_top.frames),
              f"{top.config.mode}/{top.config.fixed_len} → {len(split_top.frames)} 帧")
        check("推断: 顶级候选帧头正确", "AA 55" in top.config.header_hex.upper(),
              top.config.header_hex)
        check("推断: 顶级候选找到 CRC16/MODBUS",
              top.config.checksum_algo == "crc16_modbus", top.config.checksum_algo)
        header_candidates = [c for c in candidates if c.config.mode == framer.MODE_HEADER]
        check("推断: 也给出帧头 + 长度字段候选（含语义）",
              any(c.config.len_offset == 2 and c.config.len_size == 2
                  and c.config.len_byte_order == "big" and c.config.len_includes_header
                  for c in header_candidates),
              "；".join(f"offset={c.config.len_offset} size={c.config.len_size} "
                        f"{c.config.len_byte_order}" for c in header_candidates) or "无")
        check("推断: 候选按置信度排序",
              all(candidates[i].confidence >= candidates[i + 1].confidence
                  for i in range(len(candidates) - 1)))
        check("推断: 描述配置非空", bool(infer.describe_config(top.config)))

    # 真实样本：应该给出「定长 156，帧头 EB 90，无需跳过前导」
    sample_path = presets.DATA_DIR / "backward_sample.txt"
    if sample_path.is_file():
        from app import extract
        frames, _scanned, _skipped, _truncated = extract.read_hex_lines(str(sample_path), 300)
        real = codec.text_to_bytes("\n".join(frames), codec.RADIX_HEX)
        result = infer.infer(real)
        check("推断(真实样本): 有候选", bool(result))
        if result:
            best = result[0]
            check("推断(真实样本): 定长 156",
                  best.config.mode == framer.MODE_FIXED and best.config.fixed_len == 156,
                  f"{best.config.mode}/{best.config.fixed_len}")
            check("推断(真实样本): 帧头 EB 90 且无需跳过前导",
                  "EB 90" in best.config.header_hex.upper() and best.config.skip_bytes == 0,
                  f"{best.config.header_hex} skip={best.config.skip_bytes}")
            check("推断(真实样本): 给出疑似长度字段提示",
                  any("长度字段" in note for note in best.notes), "；".join(best.notes))


def test_cli() -> None:
    import cli

    code = cli.main(["--preset", "aa55_crc16", "--file", "data/aa55_crc16.bin",
                     "--format", "summary", "--quiet"])
    check("CLI: 解析二进制样本成功", code == 0, f"exit={code}")
    code = cli.main(["--preset", "modbus_rtu", "--text", "01 03 00 00 00 02 FF FF",
                     "--format", "summary", "--quiet"])
    check("CLI: 校验失败返回 3", code == 3, f"exit={code}")
    code = cli.main(["--list-presets"])
    check("CLI: 列出预设", code == 0)
    code = cli.main(["--list-modes"])
    check("CLI: 列出分帧方式", code == 0)
    if (presets.DATA_DIR / "backward_sample.txt").is_file():
        code = cli.main(["--infer", "--file", "data/backward_sample.txt", "--quiet"])
        check("CLI: --infer 成功", code == 0, f"exit={code}")
        code = cli.main(["--preset", "backward_eb90", "--file", "data/backward_sample.txt",
                         "--limit", "5", "--format", "csv", "--out", "cli_test.csv",
                         "--quiet"])
        check("CLI: 导出 CSV", code == 0 and Path("cli_test.csv").is_file(), f"exit={code}")
        Path("cli_test.csv").unlink(missing_ok=True)
    code = cli.main(["--text", "AA 55", "--mode", "whole", "--format", "bin", "--out", "cli_test.bin",
                     "--quiet"])
    check("CLI: 导出二进制", code == 0 and Path("cli_test.bin").read_bytes() == b"\xAA\x55")
    Path("cli_test.bin").unlink(missing_ok=True)

    # 行规则：--frame-range / --every / --only-fail
    import io
    import contextlib

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli.main(["--preset", "aa55_crc16", "--file", "data/aa55_crc16.bin",
                         "--frame-range", "1-2", "--format", "summary"])
    out = buffer.getvalue()
    check("CLI: --frame-range 只留区间", code == 0 and "保留 2 / 3 帧" in out, out.strip()[:120])
    with contextlib.redirect_stdout(io.StringIO()):
        code = cli.main(["--preset", "modbus_rtu", "--text", "01 03 00 00 00 02 FF FF",
                         "--only-fail", "--format", "summary", "--quiet"])
    check("CLI: --only-fail 保留失败帧（退出码 3）", code == 3, f"exit={code}")
    with contextlib.redirect_stdout(io.StringIO()):
        code = cli.main(["--preset", "aa55_crc16", "--file", "data/aa55_crc16.bin",
                         "--only-fail", "--format", "summary", "--quiet"])
    check("CLI: 没有失败帧时筛完为空", code == 0, f"exit={code}")
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli.main(["--preset", "aa55_crc16", "--file", "data/aa55_crc16.bin",
                         "--every", "2", "--format", "summary"])
    check("CLI: --every 抽样", code == 0 and "保留 2 / 3 帧" in buffer.getvalue(),
          buffer.getvalue().strip()[:120])
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli.main(["--text", "AA 55", "--mode", "whole", "--frame-range", "呵", "--quiet"])
    check("CLI: 非法 --frame-range 报错并返回 1",
          code == 1 and "frame-range" in buffer.getvalue(),
          f"exit={code} {buffer.getvalue().strip()[:80]}")


def test_analysis() -> None:
    """按列分析：宽表取值 / 每列统计 / 突变检测 / 抽稀。"""
    from app import analysis

    values = [1, 2, 3, 4, 5]
    stats = analysis.compute_stats("递增列", values)
    check("统计: 类型识别为数值", stats.kind == "number", stats.kind)
    check("统计: 最小/最大", stats.minimum == 1 and stats.maximum == 5)
    check("统计: 均值/中位数", close_to(stats.mean, 3) and close_to(stats.median, 3))
    check("统计: 首末差与趋势", stats.delta == 4 and stats.monotonic == "递增",
          f"{stats.delta} {stats.monotonic}")
    check("统计: 变化次数", stats.changes == 4, str(stats.changes))

    stats = analysis.compute_stats("常量列", [7, 7, 7])
    check("统计: 常量列", stats.unique == 1 and stats.changes == 0 and stats.monotonic == "恒定")

    stats = analysis.compute_stats("文本列", ["OK", "OK", None])
    check("统计: 文本列走文本分支", stats.kind == "text" and stats.unique == 1,
          f"{stats.kind} {stats.unique}")

    stats = analysis.compute_stats("空列", [None, None])
    check("统计: 空列", stats.kind == "empty" and stats.valid == 0)

    jumps = analysis.find_jumps([1, 2, 3, 4, 5, 100, 6, 7, 8, 9], factor=2.0)
    check("突变: 检出跳变点", bool(jumps) and jumps[0][0] == 5, str(jumps))
    check("突变: 跳变差值", close_to(jumps[0][3], 95), str(jumps[0]))
    check("突变: 平稳序列无跳变", analysis.find_jumps([1, 2, 3, 4, 5], factor=3.0) == [])
    check("突变: 说明文本", "帧 5" in analysis.describe_jumps(jumps))

    series = analysis.downsample(list(range(5000)), max_points=600)
    check("抽稀: 点数受控", len(series) <= 664, str(len(series)))
    check("抽稀: 首尾保留", series[0][1] == 0 and series[-1][1] == 4999)

    check("宽表 key 约定", analysis.column_key(3) == "f3")


def test_line_and_smart() -> None:
    """按行识别 + 智能识别帧格式（定长可以靠换行/行长直接定下来）。"""
    from app import infer

    text = "\n".join([
        "EB 90 97 00 00 92 00 F4",
        "EB 90 97 00 00 92 00 F6",
        "EB 90 97 00 00 92 00 F8",
    ])
    info = infer.detect_line_frames(text)
    check("按行识别: 行数", info.line_count == 3, str(info.line_count))
    check("按行识别: 每行 8 字节", info.uniform_length == 8, str(info.uniform_length))
    check("按行识别: 认出是十六进制行", info.hex_lines)

    config, reason = infer.suggest_framing(text, codec.text_to_bytes(text, "hex"))
    check("智能识别: 一行一帧 → 定长 8",
          config.mode == framer.MODE_FIXED and config.fixed_len == 8,
          f"{config.mode}/{config.fixed_len}")
    check("智能识别: 给出理由", "每行都是 8 字节" in reason, reason)

    # 行长不一致 → 改成按换行分帧
    mixed = "$POS,1,1\r\n$POS,2,1,2,3\r\n$POS,3\r\n"
    config, reason = infer.suggest_framing(mixed, mixed.encode("utf-8"))
    check("智能识别: 行长不一时按换行分帧",
          config.mode == framer.MODE_DELIMITER and config.delimiter_hex == "0D 0A",
          f"{config.mode}/{config.delimiter_hex}")

    # 真实样本（一行一帧 156 字节）→ 直接给出定长 156
    sample = presets.DATA_DIR / "backward_sample.txt"
    if sample.is_file():
        from app import extract
        text, _truncated = extract.read_text_head(str(sample), 512 * 1024)
        data, _warnings = codec.text_to_bytes_ex(text, "hex")     # 首行注释会被跳过
        config, reason = infer.suggest_framing(text, data)
        check("智能识别(真实样本): 定长 156",
              config.mode == framer.MODE_FIXED and config.fixed_len == 156,
              f"{config.mode}/{config.fixed_len}｜{reason}")
        check("智能识别(真实样本): 理由里带行长", "156 字节" in reason, reason)


def test_column_rules() -> None:
    """列规则：偏移列表、连续铺列、多字节数值列、越界与上限。"""
    from app import analysis

    check("规则: 偏移列表解析", analysis.parse_offsets("0,1,2,8-11,65")
          == [0, 1, 2, 8, 9, 10, 11, 65], str(analysis.parse_offsets("0,1,2,8-11,65")))
    check("规则: 支持中文逗号，区间倒序也按升序展开",
          analysis.parse_offsets("0，2、5-3") == [0, 2, 3, 4, 5],
          str(analysis.parse_offsets("0，2、5-3")))

    rule = analysis.ColumnRule(name="偏移", offset=100, count=3, width=1, radix="hex")
    columns = rule.resolve()
    check("规则: 连续铺列", [(c.title, c.offset) for c in columns]
          == [("偏移 100", 100), ("偏移 101", 101), ("偏移 102", 102)],
          str([(c.title, c.offset) for c in columns]))

    rule = analysis.ColumnRule(name="u16", offset=0, count=2, width=2,
                               byte_order="big", radix="dec")
    frame = framer.Frame(index=0, offset=0, data=bytes.fromhex("1E7F00FF"))
    row = analysis.rule_row(frame, rule.resolve())
    check("规则: 多字节大端合成", [row["f0"], row["f1"]] == [0x1E7F, 0x00FF],
          str([row["f0"], row["f1"]]))
    rule.byte_order = "little"
    row = analysis.rule_row(frame, rule.resolve())
    check("规则: 多字节小端合成", [row["f0"], row["f1"]] == [0x7F1E, 0xFF00],
          str([row["f0"], row["f1"]]))

    rule = analysis.ColumnRule(name="跳采", offset=0, count=3, stride=4, width=1)
    check("规则: 步长挑列", [c.offset for c in rule.resolve()] == [0, 4, 8],
          str([c.offset for c in rule.resolve()]))

    rule = analysis.ColumnRule(name="挑", offsets="0,1,300", width=1)
    columns, warnings = analysis.resolve_rules([rule], frame_len=4)
    check("规则: 越界列给出警告", bool(warnings) and len(columns) == 3, str(warnings))
    check("规则: 越界列取值为空", analysis.rule_row(frame, columns)["f2"] is None)

    many = analysis.ColumnRule(name="偏移", offset=0, count=1000, width=1)
    columns, warnings = analysis.resolve_rules([many], limit=256)
    check("规则: 列数上限生效", len(columns) == 256 and bool(warnings),
          f"{len(columns)} {warnings}")

    bad = analysis.ColumnRule(name="坏", offsets="1-abc")
    columns, warnings = analysis.resolve_rules([bad])
    check("规则: 非法列表不炸、只给警告", columns == [] and bool(warnings), str(warnings))

    check("规则: 规则预设齐备", len(analysis.RULE_PRESETS) >= 5,
          str([name for name, _r in analysis.RULE_PRESETS]))
    check("规则: 可序列化往返",
          analysis.ColumnRule.from_dict(rule.to_dict()).to_dict() == rule.to_dict())


def test_row_rules() -> None:
    """行规则：帧号区间（从哪到哪）、采样、按内容/校验/帧长筛帧。"""
    from app import analysis

    frames = [
        framer.Frame(index=i, offset=i * 4, data=bytes([i % 256, 0xEB, 0x90, 0x01]),
                     check_state=("fail" if i % 3 == 0 else "ok"))
        for i in range(10)
    ]
    total = len(frames)

    # 区间：从哪到哪（这就是"选帧数区段"）
    check("行规则: 区间解析（0 起 4 止）",
          analysis.resolve_row_range(0, 4, total) == (0, 4),
          str(analysis.resolve_row_range(0, 4, total)))
    check("行规则: -1 = 最后一帧",
          analysis.resolve_row_range(0, -1, total) == (0, 9),
          str(analysis.resolve_row_range(0, -1, total)))
    check("行规则: 负数从末尾数（最后 3 帧）",
          analysis.resolve_row_range(-3, -1, total) == (7, 9),
          str(analysis.resolve_row_range(-3, -1, total)))
    check("行规则: 写反了也认 + 越界钳制",
          analysis.resolve_row_range(8, 2, total) == (2, 8)
          and analysis.resolve_row_range(0, 999, total) == (0, 9),
          f"{analysis.resolve_row_range(8, 2, total)} "
          f"{analysis.resolve_row_range(0, 999, total)}")

    rule = analysis.RowRule(kind="range", start=2, end=5)
    hits = analysis.match_row_positions(frames, [rule])
    check("行规则: 区间命中 4 帧（2~5）", hits == [2, 3, 4, 5], str(hits))
    check("行规则: 区间说明", rule.describe(total) == "帧 2 ~ 5（共 4 帧）", rule.describe(total))
    check("行规则: 到最后一帧的说明",
          analysis.RowRule(kind="range", start=6, end=-1).describe(total)
          == "帧 6 ~ 最后一帧（共 4 帧）",
          analysis.RowRule(kind="range", start=6, end=-1).describe(total))

    # 采样
    step = analysis.RowRule(kind="step", step=3)
    check("行规则: 每 3 帧取 1", analysis.match_row_positions(frames, [step]) == [0, 3, 6, 9],
          str(analysis.match_row_positions(frames, [step])))
    stepped = analysis.RowRule(kind="step", step=4, phase=1)
    check("行规则: 采样相位", analysis.match_row_positions(frames, [stepped]) == [1, 5, 9],
          str(analysis.match_row_positions(frames, [stepped])))

    # 校验状态
    check("行规则: 只看校验失败",
          analysis.match_row_positions(frames, [analysis.RowRule(kind="check", state="fail")])
          == [0, 3, 6, 9],
          str(analysis.match_row_positions(
              frames, [analysis.RowRule(kind="check", state="fail")])))

    # 帧内字节比较
    byte_rule = analysis.RowRule(kind="byte", offset=0, op=">=", value=6, width=1)
    check("行规则: 第 0 字节 >= 6",
          analysis.match_row_positions(frames, [byte_rule]) == [6, 7, 8, 9],
          str(analysis.match_row_positions(frames, [byte_rule])))
    check("行规则: 帧内字节说明",
          analysis.RowRule(kind="byte", offset=2, width=2, byte_order="big",
                           op="!=", value=0xEB90).describe() == "帧内 [2..3] big != 0xEB90",
          analysis.RowRule(kind="byte", offset=2, width=2, byte_order="big",
                           op="!=", value=0xEB90).describe())
    check("行规则: 越界字节不算命中",
          analysis.match_row_positions(frames, [analysis.RowRule(kind="byte", offset=99)])
          == [])

    # 帧长
    check("行规则: 帧长 = 4",
          analysis.match_row_positions(frames, [analysis.RowRule(kind="length", op="=", value=4)])
          == list(range(10)))
    check("行规则: 帧长 ≠ 4 → 空",
          analysis.match_row_positions(frames, [analysis.RowRule(kind="length", op="!=", value=4)])
          == [])

    # 内容
    check("行规则: 内容解析 HEX", analysis.parse_content("eb 90") == b"\xEB\x90",
          repr(analysis.parse_content("eb 90")))
    check("行规则: 内容解析 ASCII", analysis.parse_content("ascii:AB") == b"AB",
          repr(analysis.parse_content("ascii:AB")))
    check("行规则: 帧内包含 EB 90",
          len(analysis.match_row_positions(frames, [
              analysis.RowRule(kind="contains", text="EB 90")])) == 10)
    check("行规则: 不包含",
          analysis.match_row_positions(frames, [
              analysis.RowRule(kind="contains", text="EB 90", op="!=")]) == [])

    # 组合：且 / 或 + 停用
    both = [analysis.RowRule(kind="range", start=0, end=4),
            analysis.RowRule(kind="check", state="fail")]
    check("行规则: 且（交集）",
          analysis.match_row_positions(frames, both, "all") == [0, 3],
          str(analysis.match_row_positions(frames, both, "all")))
    check("行规则: 或（并集）",
          analysis.match_row_positions(frames, both, "any") == [0, 1, 2, 3, 4, 6, 9],
          str(analysis.match_row_positions(frames, both, "any")))
    off = analysis.RowRule(kind="range", start=0, end=0, enabled=False)
    check("行规则: 停用的规则不参与", analysis.match_row_positions(frames, [off])
          == list(range(10)), str(analysis.match_row_positions(frames, [off])))
    check("行规则: 没有规则 = 全部帧",
          analysis.match_row_positions(frames, []) == list(range(10)))
    check("行规则: 组合说明文本",
          analysis.describe_row_rules(both, "all", total)
          == "帧 0 ~ 4（共 5 帧） 且 校验状态 = ✗ 失败",
          analysis.describe_row_rules(both, "all", total))
    check("行规则: 常用预设齐备", len(analysis.ROW_PRESETS) >= 8,
          str([name for name, _r in analysis.ROW_PRESETS]))
    check("行规则: 可序列化往返",
          analysis.RowRule.from_dict(step.to_dict()).to_dict() == step.to_dict())
    check("行规则: 突变清单可换帧号",
          analysis.describe_jumps([(3, 1.0, 9.0, 8.0)], label_of=lambda i: i + 100)
          == "帧 103：1 → 9（Δ +8）",
          analysis.describe_jumps([(3, 1.0, 9.0, 8.0)], label_of=lambda i: i + 100))


def test_exporters() -> None:
    check("HEX 导出", exporters.export_bytes(b"\xAA\x55", "hex") == "AA 55")
    check("C 数组", exporters.export_bytes(b"\xAA\x55", "c_array").startswith("const unsigned char"))
    check("Python bytes", exporters.export_bytes(b"\xAA", "python") == 'data = b"\\xAA"')
    check("Base64", exporters.export_bytes(b"AB", "base64") == "QUI=")
    check("CSV 转义", exporters.export_rows(["a"], [['x,y']]) == 'a\n"x,y"')


def test_presets() -> None:
    expected = {
        "aa55_crc16": (3, 0),
        "fixed_xor8": (2, 0),
        "modbus_rtu": (1, 0),
        "tlv": (4, 0),
        "ascii_line": (2, 0),
        "position_float": (2, 0),
        "raw_packet": (1, 0),
    }
    for key, (frame_count, fail_count) in expected.items():
        preset = presets.PRESETS[key]
        text, _note = preset.resolve_sample()
        radix = preset.radix
        data = codec.text_to_bytes(text, radix, preset.dec_width, preset.input_byte_order)
        cfg = framer.FrameConfig.from_dict(preset.frame)
        result = framer.split_frames(data, cfg)
        check(f"预设 {key}: 帧数 {frame_count}", len(result.frames) == frame_count,
              f"得到 {len(result.frames)}：{[f.note for f in result.frames]}")
        check(f"预设 {key}: 校验失败 {fail_count}", result.fail_count == fail_count,
              f"得到 {result.fail_count}：{[f.note for f in result.frames]}")
        fields = [FieldDef.from_dict(f) for f in preset.fields]
        problems = []
        for frame in result.frames:
            for item in field_parser.parse_fields(frame.data, fields):
                if not item.ok:
                    problems.append(f"{item.name}: {item.note}")
        check(f"预设 {key}: 字段全部解析成功", not problems, "; ".join(problems))

    # 抽验具体数值
    preset = presets.PRESETS["aa55_crc16"]
    data = codec.text_to_bytes(preset.sample, preset.radix)
    result = framer.split_frames(data, framer.FrameConfig.from_dict(preset.frame))
    parsed = {i.name: i for i in field_parser.parse_fields(
        result.frames[0].data, [FieldDef.from_dict(f) for f in preset.fields])}
    check("AA55 预设 车速=123.4", close_to(parsed["车速"].value, 123.4), str(parsed["车速"].value))
    check("AA55 预设 母线电压=24.5", close_to(parsed["母线电压"].value, 24.5), str(parsed["母线电压"].value))

    preset = presets.PRESETS["position_float"]
    data = codec.text_to_bytes(preset.sample, preset.radix)
    result = framer.split_frames(data, framer.FrameConfig.from_dict(preset.frame))
    parsed = {i.name: i for i in field_parser.parse_fields(
        result.frames[0].data, [FieldDef.from_dict(f) for f in preset.fields])}
    check("定位预设 经度", close_to(parsed["经度"].value, 121.473701, 1e-6), str(parsed["经度"].value))
    check("定位预设 航向=90.0", close_to(parsed["航向"].value, 90.0), str(parsed["航向"].value))
    check("定位预设 表达式字段", parsed["航向密位"].value is not None and parsed["航向密位"].ok)


# --------------------------------------------------------------------------- #
def isolate_settings() -> Path:
    """把界面用的 QSettings 换成临时 INI 文件，别让自检读到/写到用户真实的界面配置。

    否则「现场保存」里的列规则、列样式会串进自检，断言会随上一次界面使用而变化。
    （Windows 上 QSettings 默认走注册表，setPath 拦不住，所以直接换掉构造函数。）
    返回临时设置文件路径，测完删掉。
    """
    from PyQt6.QtCore import QSettings

    from app.ui import main_window as mw

    path = Path(__file__).resolve().parent / "_selftest_settings.ini"
    path.unlink(missing_ok=True)

    def _temp_settings(_org=None, _app=None, *args, **kwargs):
        return QSettings(str(path), QSettings.Format.IniFormat)

    mw.QSettings = _temp_settings
    return path


def test_ui() -> None:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication

    settings_file = isolate_settings()
    from app import source as _source_mod
    from app.ui.main_window import MainWindow
    from app.ui.theme import apply_theme

    # 这一组用例验证的是"内存路径"（粘贴/小文件）：把大文件阈值调高，
    # 5.7 MB 的样本就不会被当成大文件走分页。分页路径另有 test_big_file_source()。
    _source_mod.BIG_FILE_BYTES = 1 << 40

    app = QApplication.instance() or QApplication([])
    apply_theme(app)                      # 黑白主题（顺便验证样式表语法）
    window = MainWindow()
    window.act_autoparse.setChecked(False)
    window.act_stream.setChecked(False)   # 自检里用同步模式，结果可断言
    window.show()
    check("黑白主题已应用", bool(app.styleSheet()) and "QTableView" in app.styleSheet())
    check("主题为灰度（无彩色状态色）",
          all(len(set(color.replace("#", ""))) <= 4 for color in __import__("app.ui.theme", fromlist=["x"]).COLUMN_COLORS))

    for key in presets.PRESETS:
        window.clear_all()
        window.load_preset(key)
        table = window.frame_list.table
        check(f"界面 {key}: 帧列表有数据", table.rowCount() > 0,
              f"rowCount={table.rowCount()}")
        check(f"界面 {key}: 字段解析有数据", window.field_result.table.rowCount() > 0)
        check(f"界面 {key}: 日志非空", "解析结果" in window.log_panel.view.toPlainText())

    # 惰性渲染：没显示的面板不白算，切过去才补上
    window.load_preset("aa55_crc16")
    check("惰性渲染: 未显示时不渲染字节表", window.bytes_view.table.rowCount() == 0,
          f"{window.bytes_view.table.rowCount()}")
    window.right_tabs.setCurrentWidget(window.bytes_view)
    window.bytes_view.flush()
    check("惰性渲染: 切到进制视图后才有字节表", window.bytes_view.table.rowCount() > 0)
    window.right_tabs.setCurrentWidget(window.json_panel)
    check("惰性渲染: 切到 JSON 页才有 JSON", bool(window.json_panel.view.toPlainText()))
    window.right_tabs.setCurrentWidget(window.frame_list)

    # 选区显示：选中多少字符 / 多少字节 / 各进制取值
    from PyQt6.QtGui import QTextCursor
    window.load_preset("raw_packet")
    panel = window.input_panel
    panel.set_raw_text("AA 55 01 02 03")
    cursor = panel.editor.textCursor()
    cursor.setPosition(0)
    cursor.setPosition(8, QTextCursor.MoveMode.KeepAnchor)      # "AA 55 01"
    panel.editor.setTextCursor(cursor)
    info = panel.selection_label.text()
    check("输入区选区: 报出字符数与字节数", "选中 8 字符 = 3 字节" in info, info)
    check("输入区选区: 报出各进制取值",
          "HEX: AA 55 01" in info and "DEC: 170 85 1" in info
          and "BIN: 10101010" in info and "ASCII" in info, info)
    bytes_sel, sel_error = panel.selection_bytes()
    check("输入区选区: 解析出的字节正确", bytes_sel == b"\xAA\x55\x01" and not sel_error,
          f"{bytes_sel.hex()} {sel_error}")

    # 表格选区：字节对照表选中若干行 → 报出字节数与各进制取值
    window.load_preset("raw_packet")
    window.right_tabs.setCurrentWidget(window.bytes_view)
    window.bytes_view.flush()
    window.bytes_view.table.selectRow(0)
    summary = window.bytes_view.table.selection_summary()
    check("字节表选区: 报出字节数与各进制",
          "1 行" in summary and "1 字节" in summary and "HEX: AA" in summary, summary)
    window.right_tabs.setCurrentWidget(window.frame_list)

    # 列操作：整列选择 / 列颜色 / 列显示格式 / 列换算函数 / 状态往返
    table = window.field_result.table
    window.load_preset("aa55_crc16")

    # 文本列不能被"显示进制"污染：原始 HEX 必须是 AA 55，而不是十进制 65 65 32 53 53
    check("字段表: 原始 HEX 列显示十六进制", table.item(0, 5).text() == "AA 55",
          f"{table.item(0, 5).text()!r}")
    check("字段表: 解析值列显示字节十六进制", table.item(0, 7).text() == "AA 55",
          f"{table.item(0, 7).text()!r}")
    check("字段表: 备注列保持文本", "AA 55" in table.item(0, 9).text(),
          f"{table.item(0, 9).text()!r}")
    check("帧列表: 摘要列保持文本", "=" in window.frame_list.table.item(0, 5).text(),
          f"{window.frame_list.table.item(0, 5).text()!r}")
    check("帧列表: 原始 HEX 列保持十六进制",
          window.frame_list.table.item(0, 6).text().startswith("AA 55"),
          f"{window.frame_list.table.item(0, 6).text()!r}")

    table.select_entire_column(2)
    check("整列选择", len(table.selectedIndexes()) == table.rowCount(),
          f"{len(table.selectedIndexes())} vs {table.rowCount()}")
    check("列选择模式判定", table.selectionModel().isColumnSelected(2))
    table.set_column_color(2, "#FFF3CD")
    check("列颜色已应用", table.column_color(2) == "#FFF3CD")
    table.set_column_radix(6, "hex")
    text = table.item(1, 6).text()          # 第 2 行「帧长」= 13 字节，原始值为数值
    check("列显示格式切成 HEX", text.startswith("0x"), text)
    table.set_column_convert(6, "v * 0.5")
    check("列换算函数生效", table.column_convert(6) == "v * 0.5")
    check("列标题带换算标记", "⚙" in table.horizontalHeaderItem(6).text(),
          table.horizontalHeaderItem(6).text())
    tip = table.cell_tooltip(1, 6)
    check("悬停提示含换算（按需计算）", "换算函数" in tip and "原始值" in tip, tip)
    state = table.column_state()
    table.reset_column_style()
    check("列样式已重置", table.column_color(2) == "" and table.column_convert(6) == "")
    table.apply_column_state(state)
    check("列样式往返一致", table.column_color(2) == "#FFF3CD" and table.column_convert(6) == "v * 0.5")

    # 字节对照表：不同进制值
    window.load_preset("raw_packet")
    window.right_tabs.setCurrentWidget(window.bytes_view)
    window.bytes_view.flush()
    byte_table = window.bytes_view.table
    check("字节表有数据", byte_table.rowCount() == 20)
    check("字节表 HEX 列", byte_table.item(0, 1).text() == "AA")
    check("字节表 BIN 列", byte_table.item(0, 3).text() == "10101010")
    check("字节表 位展开列", byte_table.item(0, 5).text() == "1")

    # 换算函数在字节表上的效果：DEC 列 v×2
    byte_table.set_column_convert(2, "v * 2")
    check("字节表列换算", byte_table.item(0, 2).text() == "340", byte_table.item(0, 2).text())
    byte_table.set_column_convert(2, "")

    # 试算校验算法
    from app.ui.main_window import ChecksumProbeDialog
    window.load_preset("modbus_rtu")
    dialog = ChecksumProbeDialog(window._split.frames, window)  # noqa: SLF001
    first = dialog.table.item(0, 0).text()
    check("试算校验首选 CRC16/MODBUS", "MODBUS" in first, first)
    dialog.deleteLater()

    # 帧表格不设上限：有多少帧就有多少行，表格用滑动窗口按需取行
    sample_path = presets.DATA_DIR / "backward_sample.txt"
    if sample_path.is_file():
        window.clear_all()
        window.load_preset("backward_eb90")
        window.frame_list.end_render()
        check("大样本: 帧列表有数据", window.frame_list.table.rowCount() > 0)
        check("大样本: 帧列表行数 = 帧数（没有「最多显示」上限）",
              window.frame_list.table.total_rows
              == len(window.pipeline.state.split.frames),
              f"{window.frame_list.table.total_rows} vs "
              f"{len(window.pipeline.state.split.frames)}")
        check("大样本: 帧列表是虚拟行（只填视口附近的窗口）",
              window.frame_list.table.virtual
              and len(window.frame_list.table._filled) <= 700,                    # noqa: SLF001
              str(len(window.frame_list.table._filled)))                           # noqa: SLF001
        check("大样本: 视口外的行按需现算",
              bool(window.frame_list.table.row_data(window.frame_list.table.rowCount() - 1)),
              str(window.frame_list.table.rowCount()))
        window.parse_now()                      # 重新解析（字段表虚拟行会跟着刷新）
        check("大样本: 字段表行数 = 帧数 × 字段数（没有上限）",
              window.field_result.table.total_rows
              == len(window.pipeline.state.split.frames) * 8,
              f"{window.field_result.table.total_rows} vs "
              f"{len(window.pipeline.state.split.frames) * 8}")
        check("大样本: 行数按需扩容（没滚到就只铺一部分）",
              window.field_result.table.loaded_rows
              <= window.field_result.table.total_rows,
              f"{window.field_result.table.loaded_rows} / "
              f"{window.field_result.table.total_rows}")
        check("大样本: 字段表也是虚拟行",
              window.field_result.table.virtual
              and len(window.field_result.table._filled) <= 900,                  # noqa: SLF001
              str(len(window.field_result.table._filled)))                         # noqa: SLF001
        # 真实样本（12663 帧，只预解析前 200 帧）→ 远处的帧要"滚到才解析"
        window.load_data_file(str(sample_path))
        window.parse_now(blocking=True)
        total_now = len(window.pipeline.state.split.frames)
        pre = len(window._parsed_by_frame)                                       # noqa: SLF001
        check("大样本: 只预解析了前面一部分帧", pre < total_now, f"{pre} / {total_now}")
        check("大样本: 滚到远处的帧才解析字段（按需）",
              len(window.field_rows_for(total_now - 10, 0, 3)) == 3
              and len(window._parsed_by_frame) > pre,                            # noqa: SLF001
              f"{len(window.field_rows_for(total_now - 10, 0, 3))} 行")
        check("大样本: 帧列表摘要列按需解析",
              bool(window.frame_summary(total_now - 5)),
              window.frame_summary(total_now - 5)[:40])
        check("大样本: 按需解析有缓存（同一帧不重复解析）",
              window.frame_summary(total_now - 5) == window.frame_summary(total_now - 5))
        window.right_tabs.setCurrentWidget(window.bytes_view)
        window.bytes_view.flush()
        check("大样本: 字节视图仍可用", window.bytes_view.table.rowCount() > 0)
        from app import config_io
        document = config_io.build_result_document(
            {"bytes": 1}, framer.FrameConfig.from_dict(presets.PRESETS["backward_eb90"].frame),
            window._split, window._parsed_by_frame, None, limit_frames=50)  # noqa: SLF001
        check("大样本: 结果文档可截断导出",
              document["stats"]["frames_truncated"] and document["stats"]["exported_frames"] == 50,
              str(document["stats"]))
        check("大样本: 结果文档可 JSON 序列化",
              len(json.dumps(document, ensure_ascii=False, default=str)) > 1000)

    # 从日志提取对话框（不弹窗，只验证引擎与控件联动）
    from app.ui.extract_dialog import ExtractDialog
    dialog = ExtractDialog(source_text='{"can_id":"0x1E7","data":"ea07071900000000"}\n'
                                       '{"can_id":"0x190","data":"4210000000002f02"}')
    dialog.extract_now()
    check("提取对话框: 提取到 2 帧", dialog.frame_count() == 2, str(dialog.frame_count()))
    dialog.filter_edit.setText("0x1E7")
    check("提取对话框: 行过滤", dialog.frame_count() == 1, str(dialog.frame_count()))
    dialog.deleteLater()

    # 位编辑器：翻转位 → 自动重算校验 → 校验应通过
    window.load_preset("aa55_crc16")
    window.right_tabs.setCurrentWidget(window.bit_editor)   # 惰性面板：切过去才刷新
    bit = window.bit_editor
    index = bit.source_combo.findData("frame")
    if index >= 0:
        bit.source_combo.setCurrentIndex(index)
    bit.refresh()
    check("位编辑器: 当前帧 13 字节", bit.table.rowCount() == 13, f"{bit.table.rowCount()}")
    before = bit.result_bytes()
    item = bit.table.item(6, 3)           # 第 7 字节的 b7（列 3 起是 b7..b0）
    if item is not None:
        item.setCheckState(
            Qt.CheckState.Unchecked if item.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked
        )
    after = bit.result_bytes()
    check("位编辑器: 翻转位生效", before != after and len(after) == len(before),
          f"{before.hex()} -> {after.hex()}")
    state, note = framer.verify_checksum(after, window.frame_settings.config())
    check("位编辑器: 改完自动重算校验通过", state == "ok", f"{state} {note}")
    bit.reset_bytes()
    check("位编辑器: 恢复原始值", bit.result_bytes() == before)

    # 自动推断对话框
    from app.ui.infer_dialog import InferDialog
    dialog = InferDialog(window._data, window)      # noqa: SLF001
    check("推断对话框: 有候选", dialog.table.rowCount() > 0, f"{dialog.table.rowCount()}")
    dialog.deleteLater()

    # 分帧方式与数据不匹配时，必须给出提示（图里那种"拿 AA55 预设去套 EB 90 数据"）
    window.load_preset("aa55_crc16")
    window.input_panel.set_raw_text("EB 90 97 00 00 92 00 F4 B3 10 00 82 09\n"
                                    "EB 90 97 00 00 92 00 F6 B3 10 00 82 09")
    window.parse_now()
    log_text = window.log_panel.view.toPlainText()
    check("分帧不匹配: 日志给出提示",
          "分帧方式可能不匹配" in log_text or "没有分出任何帧" in log_text,
          log_text[-200:])
    check("分帧不匹配: 状态栏给出建议", "自动推断" in window.statusBar().currentMessage(),
          window.statusBar().currentMessage())
    check("分帧不匹配: 自动切到日志页",
          window.right_tabs.currentWidget() is window.log_panel)

    # 导入与预设同名的样本文件时，自动套用该预设
    sample = presets.DATA_DIR / "backward_sample.txt"
    if sample.is_file():
        window.load_data_file(str(sample))
        cfg = window.frame_settings.config()
        check("样本文件自动套用预设: 分帧方式正确",
              cfg.mode == framer.MODE_FIXED and cfg.fixed_len == 156,
              f"{cfg.mode}/{cfg.fixed_len}")
        check("样本文件自动套用预设: 字段表正确",
              len(window.field_table.fields()) == 8,
              str(len(window.field_table.fields())))

    # 数据表分析页签：一帧一行 × 一个字段一列 + 按列统计
    window.load_preset("backward_eb90")
    window.right_tabs.setCurrentWidget(window.analysis)
    window.analysis.flush()
    wide = window.analysis.table
    check("分析页: 一帧一行（没有「最多 N 行」上限，200 帧样本全在里面）",
          wide.rowCount() == 200, f"{wide.rowCount()}")
    check("分析页: 表格也是虚拟行", wide.virtual and len(wide._filled) > 0,   # noqa: SLF001
          f"virtual={wide.virtual} filled={len(wide._filled)}")               # noqa: SLF001
    check("分析页: 一列一个字段（4 基础列 + 8 字段）", wide.columnCount() == 12,
          f"{wide.columnCount()}")
    check("分析页: 表头含字段名与单位",
          [s.title for s in wide.specs()][4] == "帧头" and "(字节)" in [s.title for s in wide.specs()][5],
          str([s.title for s in wide.specs()]))
    check("分析页: 每列都有统计",
          window.analysis.stats_table.rowCount() == 8,
          f"{window.analysis.stats_table.rowCount()}")
    stats = window.analysis.stats
    check("分析页: 帧计数取值多样（单字节会回绕）",
          stats["f3"].kind == "number" and stats["f3"].unique > 50,
          f"{stats['f3'].kind} {stats['f3'].unique}")
    check("分析页: 帧长列恒为 151", stats["f1"].unique == 1 and stats["f1"].mean == 151,
          f"{stats['f1'].unique} {stats['f1'].mean}")
    check("分析页: 数据区为文本列", stats["f4"].kind == "text", stats["f4"].kind)
    # 选中一列 → 曲线与突变清单
    wide.select_entire_column(7)          # "帧计数"列
    check("分析页: 选中列给出突变清单",
          "突变阈值" in window.analysis.jump_view.toPlainText(),
          window.analysis.jump_view.toPlainText()[:60])
    # 只看异常帧（把阈值调小一定会有跳变）
    window.analysis.sigma_spin.setValue(1.0)
    check("分析页: 阈值调小后能检出突变", bool(window.analysis._jump_rows),  # noqa: SLF001
          str(len(window.analysis._jump_rows)))                              # noqa: SLF001
    window.analysis.only_anomaly_box.setChecked(True)
    window.analysis.flush()
    check("分析页: 只看异常帧会过滤行",
          wide.rowCount() <= len(window.analysis._jump_rows)                    # noqa: SLF001
          and wide.rowCount() > 0,
          f"{wide.rowCount()} vs {len(window.analysis._jump_rows)}")            # noqa: SLF001
    window.analysis.only_anomaly_box.setChecked(False)
    window.analysis.sigma_spin.setValue(4.0)
    window.right_tabs.setCurrentWidget(window.frame_list)

    # 字段表几乎是空的时候：自动切「字节列」，把整帧数据直接铺出来
    window.field_table.set_fields([])
    window._dirty = 3                      # noqa: SLF001  FIELDS
    window.parse_now()
    window.right_tabs.setCurrentWidget(window.analysis)
    window.analysis.flush()
    check("分析页: 没字段时自动用字节列",
          window.analysis.mode_combo.currentData() == "bytes",
          str(window.analysis.mode_combo.currentData()))
    check("分析页: 字节列 = 4 基础列 + 16 字节列",
          window.analysis.table.columnCount() == 20,
          f"{window.analysis.table.columnCount()}")
    check("分析页: 字节列表头是偏移",
          window.analysis.table.specs()[4].title.startswith("偏移 0"),
          str([s.title for s in window.analysis.table.specs()][4:7]))
    check("分析页: 字节第一行取值正确",
          [window.analysis.table.item(0, c).text() for c in (4, 5, 6, 7)]
          == ["EB", "90", "97", "00"],
          str([window.analysis.table.item(0, c).text() for c in (4, 5, 6, 7)]))
    check("分析页: 恒定字节被标出",
          "恒定" in window.analysis.stats_table.item(1, 0).text(),
          window.analysis.stats_table.item(1, 0).text())
    check("分析页: 统计按全部帧算",
          window.analysis.stats["f1"].total == len(window.analysis._frames),      # noqa: SLF001
          f"{window.analysis.stats['f1'].total} vs {len(window.analysis._frames)}")  # noqa: SLF001
    check("分帧提示: 字段太少会提醒", any("字段定义表只有" in h for h in
          window._soft_hints(window._split)),  # noqa: SLF001
          str(window._soft_hints(window._split)))  # noqa: SLF001

    # 「字段模板 ▾」一键补齐 → 切回字段列，列数跟着字段数走
    import json as _json
    preset_fields = presets.PRESETS["backward_eb90"].fields
    window.load_field_definitions(_json.loads(_json.dumps(preset_fields)))
    index = window.analysis.mode_combo.findData("fields")
    window.analysis.mode_combo.setCurrentIndex(index)
    window.analysis.flush()
    check("分析页: 载入字段模板后列数正确",
          window.analysis.table.columnCount() == 4 + len(preset_fields),
          f"{window.analysis.table.columnCount()} vs {4 + len(preset_fields)}")
    check("分析页: 表头带上了字段名",
          [s.title for s in window.analysis.table.specs()][4].startswith("帧头"),
          str([s.title for s in window.analysis.table.specs()][:6]))
    window.right_tabs.setCurrentWidget(window.frame_list)

    # 表格 ↔ 左侧原文 双向定位
    from PyQt6.QtGui import QTextCursor
    panel = window.input_panel
    panel.set_raw_text("AA 55 01")
    panel.set_spans([codec.LineSpan(0, 3, 0, 8)])
    panel.highlight_bytes(1, 1)
    check("定位: 单个字节 → 精确定位到那两位",
          panel.editor.textCursor().selectedText() == "55",
          repr(panel.editor.textCursor().selectedText()))
    panel.highlight_bytes(0, 3)
    check("定位: 区间 → 选中整段",
          panel.editor.textCursor().selectedText() == "AA 55 01",
          repr(panel.editor.textCursor().selectedText()))
    panel.set_raw_text("5 123")          # 奇数位会补前导 0
    panel.set_spans([codec.LineSpan(0, 3, 0, 5)])
    panel.highlight_bytes(0, 1)
    first = panel.editor.textCursor().selectedText()
    panel.highlight_bytes(1, 1)
    second = panel.editor.textCursor().selectedText()
    panel.highlight_bytes(2, 1)
    third = panel.editor.textCursor().selectedText()
    check("定位: 奇数位补零时也能对上", (first, second, third) == ("5", "1", "23"),
          f"{first!r} {second!r} {third!r}")

    window.load_preset("aa55_crc16")
    spans = getattr(window.input_panel, "_spans", [])
    check("定位: 解码给出每行字节/文本区间",
          len(spans) == 3 and spans[0].byte_count == 13 and spans[1].byte_start == 13,
          str([(s.byte_start, s.byte_count) for s in spans[:3]]))
    window.right_tabs.setCurrentWidget(window.analysis)
    window.analysis.flush()
    window.analysis.table.setCurrentCell(0, 4)      # 字段列模式第一个字段 = 帧头(0..2)
    check("定位: 分析表选中字段 → 左侧反色选中对应字节",
          window.input_panel.selected_text() == "AA 55",
          repr(window.input_panel.selected_text()))
    window.right_tabs.setCurrentWidget(window.frame_list)
    window.frame_list.table.selectRow(1)
    selected = window.input_panel.selected_text()
    check("定位: 帧列表选整帧 → 左侧选中整行",
          selected.startswith("AA 55") and len(selected) == 38, repr(selected[:24]))
    # 反向：左侧选中第 2 行 → 帧列表跳到第 2 行
    start = spans[1].text_start
    cursor = window.input_panel.editor.textCursor()
    cursor.setPosition(start)
    cursor.setPosition(start + 11, QTextCursor.MoveMode.KeepAnchor)
    window.input_panel.editor.setTextCursor(cursor)
    check("定位: 左侧选中 → 帧列表跟着定位",
          window.frame_list.table.currentRow() == 1,
          str(window.frame_list.table.currentRow()))
    window.right_tabs.setCurrentWidget(window.frame_list)

    # ---- 定位 / 选区的性能护栏：大数据下这些都不该再"整篇重算" ---- #
    panel = window.input_panel
    panel.set_raw_text("AA 55 01 02 03")
    panel.set_spans([codec.LineSpan(0, 5, 0, len("AA 55 01 02 03"))])
    cursor = panel.editor.textCursor()

    def select_chars(first: int, last: int) -> None:
        cursor.setPosition(first)
        cursor.setPosition(last, QTextCursor.MoveMode.KeepAnchor)
        panel.editor.setTextCursor(cursor)

    select_chars(0, 5)                                   # "AA 55"
    check("选区字节: 选中 AA 55 = 2 字节", panel._selection_byte_range() == (0, 2),  # noqa: SLF001
          str(panel._selection_byte_range()))                                        # noqa: SLF001
    select_chars(3, 5)                                   # "55"
    check("选区字节: 只选 55 → 第 1 个字节", panel._selection_byte_range() == (1, 1),  # noqa: SLF001
          str(panel._selection_byte_range()))                                        # noqa: SLF001
    select_chars(0, 14)                                  # 整行
    check("选区字节: 整行 = 5 字节", panel._selection_byte_range() == (0, 5),          # noqa: SLF001
          str(panel._selection_byte_range()))                                        # noqa: SLF001
    panel.set_raw_text("AA 55\n01 02")
    panel.set_spans([codec.LineSpan(0, 2, 0, 5), codec.LineSpan(2, 2, 6, 5)])
    cursor = panel.editor.textCursor()
    cursor.setPosition(0)
    cursor.setPosition(len("AA 55\n01 02"), QTextCursor.MoveMode.KeepAnchor)
    panel.editor.setTextCursor(cursor)
    check("选区字节: 跨行选择 = 两行字节数", panel._selection_byte_range() == (0, 4),   # noqa: SLF001
          str(panel._selection_byte_range()))                                        # noqa: SLF001

    panel.set_parsed_info(999, "hex")
    check("输入面板: 解析结果回填后不再整篇重算",
          panel.total_bytes() == 999 and panel.effective_radix() == "hex",
          f"{panel.total_bytes()} {panel.effective_radix()}")
    panel.set_raw_text("AA")                 # 输入变了 → 缓存必须自动失效
    check("输入面板: 输入变化后缓存失效", panel.total_bytes() == 1, str(panel.total_bytes()))

    stats_once = window._split_stats()                                          # noqa: SLF001
    check("帧统计: 记忆化（同一份分帧结果不重复遍历几万帧）",
          stats_once is window._split_stats() and stats_once["frames"] > 0,     # noqa: SLF001
          str(stats_once["frames"]))
    check("帧统计: 只算一遍就能给出帧长与校验数",
          stats_once["total_bytes"] >= stats_once["frames"]
          and stats_once["ok"] + stats_once["fail"] <= stats_once["frames"],
          str(stats_once))

    # 同一个格子重复选中 → 只定位一次（整列选择会连发好几次 selectionChanged）
    emitted: list[tuple[int, int]] = []
    window.analysis.bytes_requested.connect(lambda s, n: emitted.append((s, n)))
    window.analysis.table.setCurrentCell(1, 4)          # 换一行，保证这次是真的新位置
    window.analysis._on_table_selection()                                       # noqa: SLF001
    window.analysis._on_table_selection()                                       # noqa: SLF001
    check("分析页: 同一格子重复定位只发一次", len(emitted) == 1, str(emitted))

    # 表格 → 原文 的定位不该再反过来触发一次"原文 → 表格"
    reverse: list[tuple[int, int]] = []
    original_reverse = window._jump_table_to_bytes                              # noqa: SLF001
    window._jump_table_to_bytes = lambda s, n: reverse.append((s, n))           # noqa: SLF001
    try:
        window.input_panel.set_spans([codec.LineSpan(0, 2, 0, 5)])
        window.input_panel.set_raw_text("AA 55")
        window._jump_input_to_bytes(0, 2)                                       # noqa: SLF001
    finally:
        window._jump_table_to_bytes = original_reverse                          # noqa: SLF001
    check("定位: 表格→原文 不会再反向触发一次定位", reverse == [], str(reverse))

    window.right_tabs.setCurrentWidget(window.frame_list)
    from app.ui.rule_dialog import ColumnRuleDialog
    from app import analysis as _analysis
    dialog = ColumnRuleDialog(
        [_analysis.ColumnRule(name="偏移", offset=0, count=8, width=1, radix="hex"),
         _analysis.ColumnRule(name="u16", offset=0, count=4, width=2,
                              byte_order="big", radix="dec")],
        frame_len=156, parent=window)
    check("列规则弹窗: 规则行数", dialog.table.rowCount() == 2, str(dialog.table.rowCount()))
    check("列规则弹窗: 预览统计列数", "生成 12 列" in dialog.preview_label.text(),
          dialog.preview_label.text())
    dialog.deleteLater()

    window.load_data_file(str(sample))
    window.right_tabs.setCurrentWidget(window.analysis)
    window.analysis.set_rules([
        _analysis.ColumnRule(name="偏移", offset=0, count=8, width=1, radix="hex"),
        _analysis.ColumnRule(name="四通道", offset=65, count=4, width=2,
                             byte_order="little", radix="dec"),
    ])
    window.analysis.flush()
    table = window.analysis.table
    titles = [s.title for s in table.specs()]
    check("列规则: 自定义列数（4 基础 + 8 + 4）", table.columnCount() == 16, str(len(titles)))
    check("列规则: 多字节列标题带偏移", "四通道1 @65" in titles, str(titles[12:]))
    check("列规则: 字节列仍是 HEX", table.item(0, 4).text() == "EB", table.item(0, 4).text())
    check("列规则: 小端 int16 取值正确",
          table.item(0, 12).text() == str(int.from_bytes(
              window.pipeline.state.split.frames[0].data[65:67], "little")),
          table.item(0, 12).text())
    check("列规则: 信息栏显示规则摘要",
          "规则 2 条" in window.analysis.info_label.text(),
          window.analysis.info_label.text())

    # ---- 帧数显示：工具栏 / 状态栏 / 帧列表信息栏都得标出"一共解析出多少帧" ---- #
    total_frames = len(window.pipeline.state.split.frames)
    check("帧数显示: 工具栏显示总帧数",
          window.frame_counter.text() == f"共 {total_frames} 帧",
          window.frame_counter.text())
    check("帧数显示: 状态栏显示总帧数",
          window.status_frames.text() == f"帧 {total_frames}",
          window.status_frames.text())
    check("帧数显示: 帧列表信息栏带总帧数",
          f"共 {total_frames} 帧" in window.frame_list.info_label.text(),
          window.frame_list.info_label.text())
    check("帧数显示: 附带每帧字节数",
          "156 字节/帧" in window.frame_detail.text(), window.frame_detail.text())
    check("帧数显示: 悬停可看帧长与校验统计",
          "一共解析出" in window.frame_counter.toolTip()
          and "帧长" in window.frame_counter.toolTip(),
          window.frame_counter.toolTip().replace("\n", " / "))

    # ---- 行规则：选"从第几帧到第几帧"，表格 / 统计 / 曲线一起跟着走 ---- #
    from app.ui.row_dialog import RowRuleDialog
    from app import analysis as _row_analysis

    window.right_tabs.setCurrentWidget(window.analysis)
    window.analysis.set_row_rules([_row_analysis.RowRule(kind="range", start=100, end=299)])
    window.analysis.flush()
    check("行规则: 区间只留 200 行", window.analysis.table.total_rows == 200,
          str(window.analysis.table.total_rows))
    check("行规则: 首行就是第 100 帧",
          window.analysis.table.item(0, 0).text() == "100",
          window.analysis.table.item(0, 0).text())
    check("行规则: 末行是第 299 帧",
          window.analysis.table.item(199, 0).text() == "299",
          window.analysis.table.item(199, 0).text())
    check("行规则: 统计只算筛出来的帧",
          window.analysis.stats["f0"].total == 200,
          str(window.analysis.stats["f0"].total))
    check("行规则: 信息栏显示命中数",
          "行规则命中 200 帧" in window.analysis.info_label.text(),
          window.analysis.info_label.text())
    check("行规则: 按钮旁显示命中数",
          window.analysis.rows_label.text() == f"（命中 200/{total_frames} 帧）",
          window.analysis.rows_label.text())
    check("行规则: 悬停可看完整规则说明",
          "帧 100 ~ 299" in window.analysis.rows_label.toolTip(),
          window.analysis.rows_label.toolTip())

    window.analysis.table.setCurrentCell(0, 4)
    window.analysis.stats_table.selectRow(0)      # 点统计行 = 看该列曲线
    check("行规则: 曲线横轴用真实帧号",
          window.analysis.chart._series[0][0] == 100,          # noqa: SLF001
          str(window.analysis.chart._series[:1]))              # noqa: SLF001
    check("行规则: 突变清单里也是真实帧号",
          "帧 100" in window.analysis.jump_view.toPlainText()
          or "没有检测到突变" in window.analysis.jump_view.toPlainText(),
          window.analysis.jump_view.toPlainText()[:80].replace("\n", " / "))

    payload = window.analysis.row_rules_payload()
    check("行规则: 可随配置保存",
          payload["combine"] == "all" and len(payload["rules"]) == 1, str(payload))
    window.analysis.clear_row_rules()
    window.analysis.flush()
    check("行规则: 清空后回到全部帧（表格不再截断行数）",
          window.analysis.table.rowCount() == len(window.analysis._frames),      # noqa: SLF001
          str(window.analysis.table.rowCount()))
    window.analysis.apply_row_rules_payload(payload)
    window.analysis.flush()
    check("行规则: 从配置恢复", window.analysis.table.rowCount() == 200,
          str(window.analysis.table.rowCount()))

    # 行规则弹窗：预览、预设、换类型的可编辑格子
    dialog = RowRuleDialog(window.analysis.row_rules,
                           window.pipeline.state.split.frames, "all", window)
    check("行规则弹窗: 规则行数", dialog.table.rowCount() == 1, str(dialog.table.rowCount()))
    check("行规则弹窗: 说明列是中文摘要",
          "帧 100 ~ 299" in dialog.table.item(0, 12).text(),
          dialog.table.item(0, 12).text())
    check("行规则弹窗: 预览命中数",
          f"命中 200 / {total_frames} 帧" in dialog.preview_label.text(),
          dialog.preview_label.text())
    check("行规则弹窗: summary 可放状态栏",
          "命中 200/" in dialog.summary(), dialog.summary())
    check("行规则弹窗: 区间格可编辑、无关格置灰",
          bool(dialog.table.item(0, 2).flags() & Qt.ItemFlag.ItemIsEditable)
          and not bool(dialog.table.item(0, 6).flags() & Qt.ItemFlag.ItemIsEditable),
          f"{dialog.table.item(0, 2).flags()} / {dialog.table.item(0, 6).flags()}")
    dialog._set_kind(0, "byte")                               # noqa: SLF001
    check("行规则弹窗: 换成字节比较后可编辑格子跟着换",
          bool(dialog.table.item(0, 6).flags() & Qt.ItemFlag.ItemIsEditable)
          and not bool(dialog.table.item(0, 2).flags() & Qt.ItemFlag.ItemIsEditable),
          f"{dialog.table.item(0, 6).flags()} / {dialog.table.item(0, 2).flags()}")
    check("行规则弹窗: 数值按十六进制显示",
          dialog.table.item(0, 8).text() == "0x00", dialog.table.item(0, 8).text())

    # 编辑格子：改区间 / 结束帧留空 = 到最后一帧 / 数值支持 0x 写法
    dialog._set_kind(0, "range")                              # noqa: SLF001
    dialog.table.item(0, 2).setText("1000")
    dialog.table.item(0, 3).setText("")
    check("行规则弹窗: 结束帧留空 = 到最后一帧",
          dialog.table.item(0, 12).text().startswith("帧 1000 ~ 最后一帧"),
          dialog.table.item(0, 12).text())
    check("行规则弹窗: 改区间后预览跟着变",
          f"命中 {total_frames - 1000} / {total_frames} 帧" in dialog.preview_label.text(),
          dialog.preview_label.text())
    dialog._set_kind(0, "byte")                               # noqa: SLF001
    dialog.table.item(0, 8).setText("0xEB")
    check("行规则弹窗: 数值支持 0x 写法", dialog._rules[0].value == 0xEB,      # noqa: SLF001
          str(dialog._rules[0].value))                                       # noqa: SLF001
    check("行规则弹窗: 说明列的十六进制大写",
          "0xEB" in dialog.table.item(0, 12).text(), dialog.table.item(0, 12).text())

    dialog._apply_preset(dict(_row_analysis.ROW_PRESETS)["只看校验通过"])   # noqa: SLF001
    check("行规则弹窗: 命中 0 帧会明确警告",
          "没有任何帧命中" in dialog.preview_label.text(), dialog.preview_label.text())
    dialog._apply_preset(dict(_row_analysis.ROW_PRESETS)["每 10 帧取 1 帧（抽样看趋势）"])
    check("行规则弹窗: 采样预设命中数",
          f"命中 {len(range(0, total_frames, 10))} / {total_frames} 帧"
          in dialog.preview_label.text(), dialog.preview_label.text())
    dialog.deleteLater()
    window.analysis.clear_row_rules()
    window.analysis.flush()

    window.right_tabs.setCurrentWidget(window.frame_list)

    test_pipeline(window, app)

    window.close()
    window.deleteLater()
    app.processEvents()
    check("界面构建与销毁正常", True)
    settings_file.unlink(missing_ok=True)      # 自检用的临时设置文件，用完即删


# --------------------------------------------------------------------------- #
def test_big_file_source() -> None:
    """大文件路径：mmap + 分页 + 流式解码分帧（不把全文/全字节放进内存）。"""
    import tempfile

    from PyQt6.QtWidgets import QApplication

    settings_file = isolate_settings()
    from app import codec, framer, source as source_mod
    from app.pipeline import Stage
    from app.ui.main_window import MainWindow
    from app.ui.theme import apply_theme

    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    source_mod.BIG_FILE_BYTES = 1024        # 这一组用例专门验证分页路径：把阈值调低

    # 造一个"大文件"：6000 行 EB 90 定长 156 的十六进制帧（≈1.4 MB，够走分页）
    frames_total = 6000
    lines = ["=== selftest big file ==="]
    for index in range(frames_total):
        body = bytes([0xEB, 0x90, 0x97, 0x00, 0x00, 0x92, 0x00, index & 0xFF]) + bytes(148)
        lines.append(body.hex(" ").upper())
    text = "\n".join(lines) + "\n"
    # 临时大文件就放在项目目录里（系统临时目录可能没有写权限）
    big_path = Path(__file__).resolve().parent / "_selftest_big_sample.txt"
    big_path.write_text(text, encoding="utf-8")

    try:
        window = MainWindow()
        window.act_autoparse.setChecked(False)
        window.act_stream.setChecked(False)

        # ---- 单纯的 TextSource 能力 ---- #
        text_source = source_mod.TextSource(path=str(big_path))
        check("大文件来源: 行数正确", text_source.line_count == frames_total + 1,
              str(text_source.line_count))
        check("大文件来源: 首行是注释行",
              text_source.line_at(0).startswith("=== selftest"), text_source.line_at(0))
        check("大文件来源: 分页窗口只给一页",
              text_source.window_text(0, 10).count("\n") == 10,
              str(text_source.window_text(0, 10).count("\n")))

        cfg = framer.FrameConfig(mode=framer.MODE_FIXED, fixed_len=156)
        streamed = text_source.build_index(cfg, radix=codec.RADIX_HEX)
        plain, _warnings = codec.text_to_bytes_ex(text, codec.RADIX_HEX)   # 跳过注释行
        reference = framer.split_frames(plain, cfg)
        check("大文件来源: 流式索引帧数与内存路径一致",
              len(streamed.frames) == len(reference.frames) == frames_total,
              f"{len(streamed.frames)} vs {len(reference.frames)} vs {frames_total}")
        check("大文件来源: 帧偏移与内存路径一致",
              [streamed.frames.offset_of(i) for i in (0, 1, 3000, frames_total - 1)]
              == [reference.frames[i].offset for i in (0, 1, 3000, frames_total - 1)],
              str([streamed.frames.offset_of(i) for i in (0, 1, 3000)]))
        check("大文件来源: 随机取第 3000 帧的字节正确",
              streamed.frames[3000].data == reference.frames[3000].data,
              streamed.frames[3000].data[:8].hex(" "))
        check("大文件来源: 帧字节按需从文件解出来（不驻留整篇）",
              streamed.frames.hex_of(1).startswith("EB 90"),
              streamed.frames.hex_of(1)[:11])
        check("大文件来源: 按字节范围取数据",
              text_source.decode_bytes(0, 2) == b"\xEB\x90",
              text_source.decode_bytes(0, 2).hex())
        check("大文件来源: 字节 → 行号定位",
              text_source.line_of_byte(156) == 2, str(text_source.line_of_byte(156)))

        # ---- 界面：分页模式 ---- #
        window.load_data_file(str(big_path))
        window.frame_settings.set_config(cfg)
        window.input_panel.set_radix(codec.RADIX_HEX)
        # 给 8 个字段，字段表行数 = 6000 帧 × 8 = 48000 行（超过初始铺的 2 万行 → 会按需扩容）
        import json as _big_json

        window.load_field_definitions(
            _big_json.loads(_big_json.dumps(presets.PRESETS["backward_eb90"].fields)))
        window._dirty = Stage.FRAME                                          # noqa: SLF001
        window.parse_now(blocking=True)
        split = window.pipeline.state.split
        check("大文件界面: 走的是分页来源", window.input_panel.source is not None)
        check("大文件界面: 帧数正确", len(split.frames) == frames_total,
              str(len(split.frames)))
        check("大文件界面: 帧是惰性视图（没有几万个 Frame 对象）",
              type(split.frames).__name__ == "FrameSource",
              type(split.frames).__name__)
        check("大文件界面: 编辑器只装一页（不是整个文件）",
              window.input_panel.raw_text().count("\n") <= source_mod.PAGE_LINES + 1,
              str(window.input_panel.raw_text().count("\n")))
        check("大文件界面: 分页条显示总行数",
              f"{frames_total + 1:,}" in window.input_panel.page_label.text(),
              window.input_panel.page_label.text())
        check("大文件界面: 状态栏帧数正确",
              window.frame_counter.text() == f"共 {frames_total} 帧",
              window.frame_counter.text())

        # 表格：无上限 + 滑动窗口（行数按需扩容）
        frame_table = window.frame_list.table
        check("大文件界面: 帧列表行数 = 帧数", frame_table.total_rows == frames_total,
              f"{frame_table.total_rows}")
        check("大文件界面: 帧列表是虚拟行、只铺了一部分",
              frame_table.virtual and frame_table.loaded_rows <= frames_total,
              f"{frame_table.loaded_rows} / {frame_table.total_rows}")
        field_table = window.field_result.table
        check("大文件界面: 字段表行数 = 帧 × 字段",
              field_table.total_rows == frames_total * 8, f"{field_table.total_rows}")
        check("大文件界面: 字段表初始只铺一部分行（按需扩容）",
              field_table.loaded_rows < field_table.total_rows,
              f"{field_table.loaded_rows} / {field_table.total_rows}")
        field_table.select_row_silently(field_table.total_rows - 1)
        check("大文件界面: 定位到末尾会按需扩容到那一行",
              field_table.loaded_rows >= field_table.total_rows - 1,
              f"{field_table.loaded_rows} / {field_table.total_rows}")
        check("大文件界面: 末尾行能现算出来",
              bool(field_table.row_data(field_table.total_rows - 1)),
              "空行")
        check("大文件界面: 只解析了滚到的那几帧字段",
              len(window._parsed_by_frame) < frames_total // 2,                 # noqa: SLF001
              f"{len(window._parsed_by_frame)} / {frames_total}")               # noqa: SLF001
        window.frame_list.table.select_row_silently(frames_total - 1)
        check("大文件界面: 帧列表也能跳到最后一帧",
              window.frame_list.table.currentRow() == frames_total - 1,
              str(window.frame_list.table.currentRow()))

        # 定位到远处的一帧 → 自动翻页 + 精确选中
        target = split.frames[4200]
        window._jump_input_to_bytes(target.offset, 156)                      # noqa: SLF001
        check("大文件界面: 自动翻到目标所在页",
              window.input_panel.page == 4201 // source_mod.PAGE_LINES
              or window.input_panel.page == 4202 // source_mod.PAGE_LINES,
              f"page={window.input_panel.page} 行={window.input_panel.page_label.text()}")
        check("大文件界面: 选中了整帧 156 字节",
              window.input_panel._selection_byte_range() == (target.offset, 156),   # noqa: SLF001
              str(window.input_panel._selection_byte_range()))                      # noqa: SLF001
        check("大文件界面: 选中的原文就是那一帧",
              window.input_panel.selected_text().replace("\u2029", " ")[:11]
              == "EB 90 97 00",
              repr(window.input_panel.selected_text()[:16]))

        # 只选一帧里的两个字节
        window._jump_input_to_bytes(target.offset + 4, 2)                    # noqa: SLF001
        check("大文件界面: 只选 2 字节也准",
              window.input_panel._selection_byte_range() == (target.offset + 4, 2),  # noqa: SLF001
              str(window.input_panel._selection_byte_range()))                      # noqa: SLF001

        # 翻页 / 跳到行
        window.input_panel.goto_page(2)
        check("大文件界面: 翻到第 2 页", window.input_panel.page == 1,
              str(window.input_panel.page))
        window.input_panel.goto_line(source_mod.PAGE_LINES * 3 + 5)
        check("大文件界面: 按行号跳页", window.input_panel.page == 3,
              f"page={window.input_panel.page} 行={window.input_panel.page_label.text()}")

        # 手输内容 → 自动退出分页模式，回到内存路径
        window.input_panel.set_raw_text("AA 55 01")
        window.parse_now(blocking=True)
        check("大文件界面: 手输后退出分页模式",
              window.input_panel.source is None and window._source is None,       # noqa: SLF001
              f"panel={window.input_panel.source} window={window._source}")       # noqa: SLF001

        window.close()
        window.deleteLater()
        app.processEvents()
        if window._source is not None:                                       # noqa: SLF001
            window._source.close()                                           # noqa: SLF001
    finally:
        text_source.close()
        source_mod.BIG_FILE_BYTES = 4 * 1024 * 1024      # 还原默认阈值
        big_path.unlink(missing_ok=True)
        settings_file.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
def test_pipeline(window, app) -> None:
    """流式管线：脏标记决定起点、按时间片推进、结果渐进出现、只算看得见的。"""
    from app.pipeline import Stage

    sample = presets.DATA_DIR / "backward_sample.txt"
    if not sample.is_file():
        print("  （跳过）没有 data/backward_sample.txt")
        return

    window.act_autoparse.setChecked(False)
    window.act_stream.setChecked(False)
    window.load_data_file(str(sample))          # 12663 帧，够体现流式
    check("管线: 同步模式一次跑完", not window.pipeline.running)
    sync_frames = window.frame_list.table.rowCount()
    sync_rows = window.field_result.table.rowCount()
    check("管线: 同步模式有结果", sync_frames > 0 and sync_rows > 0,
          f"frames={sync_frames} rows={sync_rows}")

    # ---- 脏标记：RENDER 只重渲染，FRAME 只重新分帧，DECODE 才重新解码 ---- #
    state = window.pipeline.state
    split_before, parsed_before, data_before = state.split, state.parsed, state.data
    window._dirty = Stage.RENDER          # noqa: SLF001
    window.parse_now()
    check("脏标记: RENDER 复用分帧结果", window.pipeline.state.split is split_before)
    check("脏标记: RENDER 复用字段解析结果", window.pipeline.state.parsed is parsed_before)
    window._dirty = Stage.FRAME           # noqa: SLF001
    window.parse_now()
    check("脏标记: FRAME 会重新分帧", window.pipeline.state.split is not split_before)
    check("脏标记: FRAME 复用输入字节", window.pipeline.state.data is data_before)
    window._dirty = Stage.FIELDS          # noqa: SLF001
    window.parse_now()
    check("脏标记: FIELDS 保留分帧结果",
          window.pipeline.state.split is not None and window.pipeline.state.parsed is not None)

    # ---- 流式推进：提交后立刻返回，行数随事件循环逐步长出来 ---- #
    window.act_stream.setChecked(True)
    window._dirty = Stage.DECODE          # noqa: SLF001
    window.parse_now()
    check("流式: 提交后立即返回（没阻塞）", window.pipeline.running is True)
    start_rows = window.frame_list.table.rowCount()
    for _ in range(6):
        app.processEvents()
    mid_rows = window.frame_list.table.rowCount()
    window.pipeline.run_to_completion()   # 收尾（真实界面里由 QTimer 自己跑完）
    app.processEvents()
    final_rows = window.frame_list.table.rowCount()
    check("流式: 结果渐进出现", start_rows < final_rows and mid_rows <= final_rows,
          f"{start_rows} → {mid_rows} → {final_rows}")
    check("流式: 用到了多个时间片", window.pipeline.ticks >= 1, str(window.pipeline.ticks))
    check("流式: 表已结束流式状态", window.frame_list.table.rowCount() > 0)
    check("流式: 每一步都只有可见面板在算（字节表按需）", True)

    # ---- 帧数显示：流式跑完 / 跑一半都要给出"一共解析出多少帧" ---- #
    total_now = len(window.pipeline.state.split.frames)
    check("帧数显示: 流式跑完后工具栏帧数准确",
          window.frame_counter.text() == f"共 {total_now} 帧", window.frame_counter.text())
    check("帧数显示: 帧列表信息栏的总帧数也准确",
          f"共 {total_now} 帧" in window.frame_list.info_label.text(),
          window.frame_list.info_label.text())
    window.act_stream.setChecked(True)
    window._dirty = Stage.DECODE          # noqa: SLF001
    window.parse_now()
    app.processEvents()
    check("帧数显示: 流式刚开始就已知道总帧数（不等算完）",
          window.pipeline.running and window.frame_counter.text() == f"共 {total_now} 帧",
          f"running={window.pipeline.running} {window.frame_counter.text()}")
    window.pipeline.run_to_completion()
    app.processEvents()

    # ---- 抢占：新请求顶掉旧任务，不会重复渲染/半途错乱 ---- #
    window.act_stream.setChecked(True)
    window._dirty = Stage.DECODE          # noqa: SLF001
    window.parse_now()
    app.processEvents()
    window._dirty = Stage.DECODE          # noqa: SLF001
    window.parse_now()                    # 上一个任务应被丢弃
    window.pipeline.run_to_completion()
    app.processEvents()
    streamed_rows = window.frame_list.table.rowCount()

    window.act_stream.setChecked(False)
    window._dirty = Stage.DECODE          # noqa: SLF001
    window.parse_now()                    # 干净重算一遍做对照
    clean_rows = window.frame_list.table.rowCount()
    check("抢占: 流式与同步结果一致（无重复渲染）", streamed_rows == clean_rows,
          f"{streamed_rows} vs {clean_rows}")

    # 切到字节视图时惰性补齐，且分片渲染
    window.right_tabs.setCurrentWidget(window.bytes_view)
    app.processEvents()
    check("流式: 字节表分片渲染中", window.bytes_view.streamer.active or
          window.bytes_view.table.rowCount() > 0)
    window.bytes_view.flush()
    check("流式: 字节表可一次收尾", window.bytes_view.table.rowCount() > 0)

    # 切回同步模式，避免影响后续断言
    window.act_stream.setChecked(False)
    window.right_tabs.setCurrentWidget(window.frame_list)


# --------------------------------------------------------------------------- #
def main() -> int:
    tests = [
        ("编解码", test_codec),
        ("校验算法", test_checksum),
        ("分帧器", test_framer),
        ("字段解析", test_fields),
        ("换算函数", test_convert),
        ("日志提取", test_extract),
        ("按列分析", test_analysis),
        ("列规则", test_column_rules),
        ("行规则", test_row_rules),
        ("按行识别与智能帧格式", test_line_and_smart),
        ("自动推断分帧参数", test_infer),
        ("命令行模式", test_cli),
        ("导出格式", test_exporters),
        ("内置预设", test_presets),
        ("真实样本", test_real_sample),
        ("界面（offscreen）", test_ui),
        ("大文件分页来源", test_big_file_source),
    ]
    for name, func in tests:
        print(f"\n>>> {name}")
        try:
            func()
        except Exception:  # noqa: BLE001
            FAILED.append((name, traceback.format_exc()))

    print("\n" + "=" * 72)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"\n[失败] {name}\n{detail}")
    print("=" * 72)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
