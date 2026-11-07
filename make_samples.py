"""生成 data/ 目录下的样本文件（可重复运行）。

    python make_samples.py

生成的样本分两类：
1. 协议样本 —— 由内置预设的构帧代码现场算出，校验一定对得上；
2. 日志样本 —— 仿照现场抓包格式的 can_sample.jsonl / can_sample.csv，
   用来验证「从日志提取数据帧」功能（正则 + 行过滤）。
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app import codec, presets  # noqa: E402

DATA_DIR = ROOT / "data"

CAN_IDS = ["0x1E7", "0x190", "0x290", "0x390", "0x490", "0x70D", "0x704", "0x308"]


def write_protocol_samples() -> list[str]:
    made: list[str] = []
    for key, preset in presets.PRESETS.items():
        data = codec.text_to_bytes(preset.sample, preset.radix, preset.dec_width,
                                   preset.input_byte_order)
        if preset.radix == codec.RADIX_ASCII:
            path = DATA_DIR / f"{key}.txt"
            path.write_bytes(data)
        else:
            path = DATA_DIR / f"{key}.bin"
            path.write_bytes(data)
        made.append(f"{path.name:28s} {len(data):6d} 字节   {preset.name}")
    return made


def _can_payload(index: int, can_id: str) -> str:
    """造一组像真实整车 CAN 报文的数据（数值缓慢变化）。"""
    ramp = index % 64
    table = {
        "0x1E7": [0xEA, 0x07, 0x07, 0x19, ramp, 0x00, 0x00, 0x00],
        "0x190": [0x42, 0x10, ramp, 0x00, 0x00, (ramp * 3) & 0xFF, 0x2F, 0x02],
        "0x290": [0x4E, 0x00, 0x14, 0x00, 0x4A, 0x00, 0x60, 0x00],
        "0x390": [0x48, 0x00, 0x31, 0x00, 0x1C, 0x00, ramp, 0x00],
        "0x490": [0x00] * 8,
        "0x308": [0x01, ramp, 0x02, 0x00, 0x1C, 0x00, 0x00, 0x00],
    }
    if can_id in ("0x70D", "0x704"):
        return f"{(index * 5) & 0xFF:02x}"
    return "".join(f"{b:02x}" for b in table.get(can_id, [0] * 8))


def write_can_jsonl(count: int = 120) -> str:
    rng = random.Random(20260810)
    path = DATA_DIR / "can_sample.jsonl"
    ts = 1785005890511
    lines = []
    for index in range(count):
        can_id = rng.choice(CAN_IDS)
        payload = _can_payload(index, can_id)
        ts += rng.randint(1, 3)
        lines.append(json.dumps(
            {"can_id": can_id, "dlc": len(payload) // 2, "data": payload, "ts": ts},
            ensure_ascii=False, separators=(",", ":"),
        ))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"{path.name:28s} {count:6d} 行     仿 CAN 抓包 JSONL（可用行过滤 can_id 只看某一帧）"


def write_can_csv(count: int = 120) -> str:
    """仿 CAN 分析仪导出的 CSV（数据列形如 x| 00 00 02 00 64 1C 00 00）。"""
    rng = random.Random(8810)
    path = DATA_DIR / "can_sample.csv"
    rows = ["序号,系统时间,时间戳,CAN通道,传输方向,ID号,帧类型,帧格式,长度,数据"]
    ms = 340
    for index in range(count):
        can_id = rng.choice(CAN_IDS)
        payload = _can_payload(index, can_id)
        ms += rng.randint(1, 3)
        stamp = f"11:05:{ms // 1000:02d}.{ms % 1000:03d}"
        spaced = " ".join(payload[i:i + 2] for i in range(0, len(payload), 2)).upper()
        rows.append(
            f"{index:05d},=\"{stamp}\",0x{0x64815 + index:X},ch2,接收,{can_id[2:].upper().rjust(4, '0')},"
            f"数据帧,标准帧,0x{len(payload) // 2:02X},x| {spaced} "
        )
    path.write_text("\n".join(rows) + "\n", encoding="utf-8-sig")
    return f"{path.name:28s} {count:6d} 行     仿 CAN 分析仪 CSV（x| 数据列）"


def write_readme(entries: list[str]) -> None:
    extra = []
    for name in ("backward_sample.txt", "can_sample.jsonl", "can_sample.csv"):
        path = DATA_DIR / name
        if path.is_file():
            extra.append(f"  {name:24s} {path.stat().st_size:9d} 字节")
    text = "\n".join([
        "frame_parser 样本数据目录",
        "=" * 60,
        "",
        "【协议样本】由内置预设的构帧代码现场生成，校验一定对得上，",
        "  在界面里「文件 → 样本数据」可直接打开：",
        "",
        *[f"  {line}" for line in entries],
        "",
        "【日志样本】用来验证「从日志提取数据帧」（正则 + 行过滤）：",
        "  · can_sample.jsonl —— 规则选「JSONL/JSON 的 data 字段」，",
        "    行过滤填 0x1E7 就只看这个 CAN ID 的报文；",
        "  · can_sample.csv  —— 规则选「CAN 分析仪 CSV 数据列」。",
        "",
        "【现场采集样本】backward_sample.txt（预设 ⑧）：",
        "  12663 帧 × 156 字节，帧头 EB 90、字节2 = 总长-5、字节5 = 报文类型 0x92、",
        "  字节7 = 帧计数(每帧+2)、字节65/73/81/89 各 8 字节通道数据、帧尾 5B BF。",
        "  用「定长帧 156」分帧即可；文件第一行注释会被自动跳过。",
        "",
        "【切换样本目录】文件 → 样本数据 → 选择样本目录…（例如指到自己的日志目录）",
        "",
        "【重新生成】python make_samples.py",
        "",
    ])
    if extra:
        text += "\n当前目录里的日志/现场样本：\n" + "\n".join(extra) + "\n"
    (DATA_DIR / "README.txt").write_text(text, encoding="utf-8")


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    entries = write_protocol_samples()
    entries.append(write_can_jsonl())
    entries.append(write_can_csv())
    write_readme(entries)
    print(f"样本已生成到 {DATA_DIR}")
    for line in entries:
        print("  " + line)
    print(f"  README.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
