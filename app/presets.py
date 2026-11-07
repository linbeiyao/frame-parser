"""内置预设：帧格式 + 字段定义 + 可直接点开就能看到效果的示例数据。

示例数据是用同一条构帧代码现场算出来的，所以校验一定对得上，
可以放心用来验证界面里的分帧/校验/字段设置。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from . import checksum, codec
from .field_parser import (
    TYPE_ASCII, TYPE_BYTES, TYPE_EXPR, TYPE_FLOAT, TYPE_INT, TYPE_UINT,
)

H = codec.RADIX_HEX
A = codec.RADIX_ASCII

#: 样本目录（make_samples.py 生成 / 也可以放自己的抓包文件）
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@dataclass
class Preset:
    key: str
    name: str
    description: str
    radix: str = H
    dec_width: int = 0
    input_byte_order: str = "big"
    frame: dict = field(default_factory=dict)
    fields: list[dict] = field(default_factory=list)
    sample: str = ""
    #: sample 为空时，从 data/<sample_file> 读取前 sample_lines 帧
    sample_file: str = ""
    sample_lines: int = 200

    def hex_sample(self, data: bytes, sep: str = " ") -> str:
        return codec.bytes_to_text(data, codec.RADIX_HEX, sep=sep)

    def resolve_sample(self) -> tuple[str, str]:
        """返回 (示例文本, 说明)。"""
        if self.sample:
            return self.sample, ""
        if not self.sample_file:
            return "", ""
        path = DATA_DIR / self.sample_file
        if not path.is_file():
            return "", f"找不到样本文件 data/{self.sample_file}，请先运行 make_samples.py"
        from .extract import read_hex_lines
        frames, scanned, skipped, truncated = read_hex_lines(str(path), self.sample_lines)
        if not frames:
            return "", f"data/{self.sample_file} 里没有可用的 HEX 行"
        note = f"已从 data/{self.sample_file} 读取前 {len(frames)} 帧"
        if truncated:
            note += f"（文件共扫描 {scanned} 行）"
        if skipped:
            note += f"，跳过 {skipped} 行非 HEX 内容"
        return "\n".join(frames), note


def _f(name, type_, offset, length, **kw) -> dict:
    base = {
        "name": name, "type": type_, "offset": offset, "length": length,
        "byte_order": "big", "scale": 1.0, "bias": 0.0, "unit": "",
        "bits": "", "radix": "auto", "expr": "", "note": "",
    }
    base.update(kw)
    return base


def _lines(frames: list[bytes]) -> str:
    """示例文本统一排成「一行一帧」，方便肉眼对照，也让"按行定位/按行分帧"直接可用。"""
    return "\n".join(codec.bytes_to_text(frame, codec.RADIX_HEX) for frame in frames)


# --------------------------------------------------------------------------- #
# 1) 帧头 AA55 + 长度 + CRC16/MODBUS
# --------------------------------------------------------------------------- #
def _build_aa55() -> list[bytes]:
    out: list[bytes] = []
    for seq, status, speed, volt in (
        (1, 0x01, 1234, 2450),   # 123.4 km/h, 24.50 V
        (2, 0x05, 0, 2380),
        (3, 0x81, 567, 2412),
    ):
        payload = (
            seq.to_bytes(2, "big") + bytes([status])
            + speed.to_bytes(2, "big") + volt.to_bytes(2, "big")
        )
        total = 2 + 2 + len(payload) + 2
        body = b"\xAA\x55" + total.to_bytes(2, "big") + payload
        out.append(body + checksum.compute("crc16_modbus", body, "little"))
    return out


PRESET_AA55 = Preset(
    key="aa55_crc16",
    name="① 帧头 AA55 + 长度 + CRC16/MODBUS",
    description="最常见的自定义协议：2 字节帧头、2 字节总长（含帧头和校验）、帧尾 2 字节 CRC16。",
    frame={
        "mode": "header", "header_hex": "AA 55",
        "len_offset": 2, "len_size": 2, "len_byte_order": "big",
        "len_includes_header": True, "len_includes_checksum": True,
        "checksum_algo": "crc16_modbus", "checksum_byte_order": "little",
        "checksum_where": "tail",
    },
    fields=[
        _f("帧头", TYPE_BYTES, 0, 2, radix=H, note="固定 AA 55"),
        _f("帧长", TYPE_UINT, 2, 2, unit="字节"),
        _f("序号", TYPE_UINT, 4, 2),
        _f("状态字", TYPE_UINT, 6, 1, radix=H),
        _f("车速", TYPE_UINT, 7, 2, scale=0.1, unit="km/h"),
        _f("母线电压", TYPE_UINT, 9, 2, scale=0.01, unit="V"),
        _f("CRC16", TYPE_BYTES, 11, 2, radix=H),
    ],
    sample=_lines(_build_aa55()),
)


# --------------------------------------------------------------------------- #
# 2) 定长帧 10 字节 + XOR8
# --------------------------------------------------------------------------- #
def _build_fixed() -> list[bytes]:
    out: list[bytes] = []
    for dev, speed, direction, oil, temp, status in (
        (0x0101, 150, 1, 125, 45, 0x03),
        (0x0102, 0, 0, 30, 12, 0x01),
    ):
        body = (
            dev.to_bytes(2, "big") + speed.to_bytes(2, "big") + bytes([direction])
            + oil.to_bytes(2, "big") + bytes([(temp + 40) & 0xFF, status])
        )
        out.append(body + checksum.compute("xor8", body))
    return out


PRESET_FIXED = Preset(
    key="fixed_xor8",
    name="② 定长帧 10 字节 + XOR8",
    description="没有帧头长度字段，每帧固定 10 字节，最后 1 字节异或校验；含位域、有符号、偏置示例。",
    frame={"mode": "fixed", "fixed_len": 10, "checksum_algo": "xor8",
           "checksum_byte_order": "big", "checksum_where": "tail"},
    fields=[
        _f("设备ID", TYPE_UINT, 0, 2, radix=H),
        _f("轮速", TYPE_UINT, 2, 2, scale=0.01, unit="m/s"),
        _f("行驶方向", TYPE_UINT, 4, 1, bits="0-1", note="位域 0-1：0 停 1 前 2 后"),
        _f("液压压力", TYPE_UINT, 5, 2, scale=0.1, unit="bar"),
        _f("油温", TYPE_INT, 7, 1, bias=-40, unit="℃"),
        _f("状态字", TYPE_UINT, 8, 1, radix=H),
        _f("XOR8", TYPE_BYTES, 9, 1, radix=H),
    ],
    sample=_lines(_build_fixed()),
)


# --------------------------------------------------------------------------- #
# 3) Modbus RTU
# --------------------------------------------------------------------------- #
def _build_modbus() -> bytes:
    body = bytes.fromhex("0103000000 02".replace(" ", ""))
    return body + checksum.compute("crc16_modbus", body, "little")


PRESET_MODBUS = Preset(
    key="modbus_rtu",
    name="③ Modbus RTU 读保持寄存器",
    description="整包不拆分 + 帧尾 CRC16/MODBUS（低字节在前），用于对照仪表/PLC 报文。",
    frame={"mode": "whole", "checksum_algo": "crc16_modbus",
           "checksum_byte_order": "little", "checksum_where": "tail"},
    fields=[
        _f("从站地址", TYPE_UINT, 0, 1, radix=H),
        _f("功能码", TYPE_UINT, 1, 1, radix=H, note="03=读保持寄存器"),
        _f("起始地址", TYPE_UINT, 2, 2, radix=H),
        _f("寄存器数量", TYPE_UINT, 4, 2),
        _f("CRC16", TYPE_BYTES, 6, 2, radix=H),
    ],
    sample=codec.bytes_to_text(_build_modbus(), H),
)


# --------------------------------------------------------------------------- #
# 4) TLV
# --------------------------------------------------------------------------- #
def _build_tlv() -> list[bytes]:
    items = [
        (0x01, bytes([0x11, 0x22, 0x33, 0x44])),
        (0x02, b"OK"),
        (0x03, codec.ascii_to_bytes("hello")),
        (0x10, bytes(range(1, 9))),
    ]
    out: list[bytes] = []
    for tag, value in items:
        out.append(bytes([tag, len(value)]) + value)
    return out


PRESET_TLV = Preset(
    key="tlv",
    name="④ TLV 帧 (Tag 1B + Len 1B + Value)",
    description="每个 TLV 拆成一帧；字段「长度填 0」表示取到帧尾，适合变长 Value。",
    frame={"mode": "tlv", "tag_size": 1, "value_len_size": 1,
           "value_len_byte_order": "big", "checksum_algo": "none"},
    fields=[
        _f("Tag", TYPE_UINT, 0, 1, radix=H),
        _f("Len", TYPE_UINT, 1, 1, unit="字节"),
        _f("Value", TYPE_BYTES, 2, 0, radix=H, note="长度 0 = 取到帧尾，可整列切成 DEC/BIN/ASCII 看"),
    ],
    sample=_lines(_build_tlv()),
)


# --------------------------------------------------------------------------- #
# 5) ASCII 行
# --------------------------------------------------------------------------- #
PRESET_ASCII = Preset(
    key="ascii_line",
    name="⑤ ASCII 行 ($POS…, \\r\\n 分隔)",
    description="输入按 ASCII 文本解析，用 0D 0A 分隔符分帧；ASCII 转义 \\r\\n 会被还原成真实字节。",
    radix=A,
    frame={"mode": "delimiter", "delimiter_hex": "0D 0A",
           "include_delimiter": True, "checksum_algo": "none"},
    fields=[
        _f("原始行", TYPE_ASCII, 0, 0, note="长度 0 = 取到帧尾"),
    ],
    sample="$POS,01,1,1234.5678,2345.6789,90.00\r\n$POS,02,1,1200.0000,2300.0000,180.00\r\n",
)


# --------------------------------------------------------------------------- #
# 6) 定位数据（浮点 + 表达式）
# --------------------------------------------------------------------------- #
def _build_position() -> list[bytes]:
    out: list[bytes] = []
    for seq_lon_lat in (
        (121.473701, 31.230416, 9000, 1234),
        (121.474002, 31.230911, 9050, 0),
    ):
        lon, lat, heading, speed = seq_lon_lat
        payload = struct.pack(">dd", lon, lat) + heading.to_bytes(2, "big") + speed.to_bytes(2, "big")
        total = 2 + 2 + len(payload) + 2
        body = b"\xAA\x55" + total.to_bytes(2, "big") + payload
        out.append(body + checksum.compute("crc16_modbus", body, "little"))
    return out


PRESET_POSITION = Preset(
    key="position_float",
    name="⑥ 定位数据 (float64 经纬度 + 表达式)",
    description="演示浮点字段、缩放系数，以及用表达式引用前面字段（航向 → 密位）。",
    frame={
        "mode": "header", "header_hex": "AA 55",
        "len_offset": 2, "len_size": 2, "len_byte_order": "big",
        "len_includes_header": True, "len_includes_checksum": True,
        "checksum_algo": "crc16_modbus", "checksum_byte_order": "little",
        "checksum_where": "tail",
    },
    fields=[
        _f("帧头", TYPE_BYTES, 0, 2, radix=H),
        _f("帧长", TYPE_UINT, 2, 2, unit="字节"),
        _f("经度", TYPE_FLOAT, 4, 8, note="float64 大端"),
        _f("纬度", TYPE_FLOAT, 12, 8),
        _f("航向", TYPE_UINT, 20, 2, scale=0.01, unit="°"),
        _f("航向密位", TYPE_EXPR, 0, 0, expr="航向 * 16.6667", unit="mil"),
        _f("速度", TYPE_UINT, 22, 2, scale=0.1, unit="m/s"),
        _f("CRC16", TYPE_BYTES, 24, 2, radix=H),
    ],
    sample=_lines(_build_position()),
)


# --------------------------------------------------------------------------- #
# 7) 整包查看
# --------------------------------------------------------------------------- #
PRESET_RAW = Preset(
    key="raw_packet",
    name="⑦ 整包查看（不拆分，手工核对字节）",
    description="不做分帧，整段数据当成一帧，配合「进制视图」逐字节 HEX/DEC/BIN 对照。",
    frame={"mode": "whole", "checksum_algo": "none"},
    fields=[
        _f("帧头", TYPE_BYTES, 0, 2, radix=H),
        _f("帧类型", TYPE_UINT, 2, 1, radix=H),
        _f("数据长度", TYPE_UINT, 3, 1, unit="字节"),
        _f("载荷", TYPE_BYTES, 4, 0, radix=H, note="长度 0 = 取到帧尾"),
    ],
    sample="AA 55 01 10 01 02 03 04 05 06 07 08 09 0A 0B 0C 0D 0E 0F 10",
)


# --------------------------------------------------------------------------- #
# 8) 现场采集样本：data/backward_sample.txt（固定 156 字节帧）
# --------------------------------------------------------------------------- #
PRESET_BACKWARD = Preset(
    key="backward_eb90",
    name="⑧ 现场样本 backward_sample.txt (帧头 EB 90, 定长 156)",
    description="data/backward_sample.txt 的真实格式：12663 帧全部 156 字节，"
                "帧头 EB 90、字节2 = 总长-5、字节5 = 报文类型 0x92、字节7 = 帧计数(每帧+2)、帧尾 5B BF。"
                "8 个字段只覆盖了确定的位置，其余用「数据区」整段看。",
    frame={"mode": "fixed", "fixed_len": 156, "checksum_algo": "none"},
    fields=[
        _f("帧头", TYPE_BYTES, 0, 2, radix=H, note="恒定 EB 90"),
        _f("帧长", TYPE_UINT, 2, 1, unit="字节", note="值 151 = 总长 156 - 5（从第 5 字节起计）"),
        _f("报文类型", TYPE_UINT, 5, 1, radix=H, note="恒定 0x92"),
        _f("帧计数", TYPE_UINT, 7, 1, note="逐帧 +2，用来判断有没有丢帧"),
        _f("数据区", TYPE_BYTES, 8, 57, radix=H, note="偏移 8~64"),
        _f("四通道数据", TYPE_BYTES, 65, 32, radix=H,
           note="4 组 8 字节（偏移 65/73/81/89），数值逐帧小幅变化，疑似 4 路测距/角度，"
                "可整列切 DEC/BIN 看，或加列换算函数"),
        _f("尾部标志", TYPE_UINT, 153, 1, radix=H, note="逐帧变化，疑似校验/状态字节，可用试算校验确认"),
        _f("帧尾", TYPE_BYTES, 154, 2, radix=H, note="恒定 5B BF"),
    ],
    sample_file="backward_sample.txt",
    sample_lines=200,
)


PRESETS: dict[str, Preset] = {
    p.key: p for p in (
        PRESET_AA55, PRESET_FIXED, PRESET_MODBUS, PRESET_TLV,
        PRESET_ASCII, PRESET_POSITION, PRESET_RAW, PRESET_BACKWARD,
    )
}


# --------------------------------------------------------------------------- #
# 字段模板（只换字段定义表，不动分帧设置）
# --------------------------------------------------------------------------- #
FIELD_TEMPLATES: dict[str, list[dict]] = {
    "空表": [],
    "整型 u8/u16/u32": [
        _f("u8_0", TYPE_UINT, 0, 1),
        _f("u16_1", TYPE_UINT, 1, 2),
        _f("u32_3", TYPE_UINT, 3, 4),
    ],
    "浮点 + 缩放 + 偏置": [
        _f("f32", TYPE_FLOAT, 0, 4),
        _f("u16×0.1", TYPE_UINT, 4, 2, scale=0.1, unit="kPa"),
        _f("i16 偏置-40", TYPE_INT, 6, 2, bias=-40, unit="℃"),
    ],
    "同一字节三种进制": [
        _f("字节0 (HEX)", TYPE_UINT, 0, 1, radix="hex"),
        _f("字节0 (DEC)", TYPE_UINT, 0, 1, radix="dec"),
        _f("字节0 (BIN)", TYPE_UINT, 0, 1, radix="bin"),
        _f("全部 (HEX)", TYPE_BYTES, 0, 0, radix="hex"),
    ],
}
