"""校验算法集合：累加和 / 异或 / CRC。

新增算法只需：
1. 写一个 `def _xxx(data: bytes) -> int` 的纯函数；
2. 在 ALGOS 里登记 (key, 显示名, 字节数, 默认字节序)。
"""
from __future__ import annotations

import zlib

#: (key, 显示名, 校验字节数, 默认字节序)
ALGOS: list[tuple[str, str, int, str]] = [
    ("none", "无校验", 0, "big"),
    ("sum8", "累加和 SUM8 (取低 8 位)", 1, "big"),
    ("sum8_inv", "累加和取反 ~SUM8", 1, "big"),
    ("sum16", "累加和 SUM16", 2, "big"),
    ("xor8", "异或校验 XOR8 / BCC", 1, "big"),
    ("crc8", "CRC-8 (poly 0x07)", 1, "big"),
    ("crc16_modbus", "CRC-16/MODBUS", 2, "little"),
    ("crc16_ccitt_false", "CRC-16/CCITT-FALSE", 2, "big"),
    ("crc16_xmodem", "CRC-16/XMODEM", 2, "big"),
    ("crc32", "CRC-32", 4, "little"),
]

ALGO_LABELS = {key: label for key, label, _, _ in ALGOS}
ALGO_SIZES = {key: size for key, _, size, _ in ALGOS}
ALGO_DEFAULT_ORDER = {key: order for key, _, _, order in ALGOS}


def algo_size(algo: str) -> int:
    return ALGO_SIZES.get(algo, 0)


def algo_default_order(algo: str) -> str:
    return ALGO_DEFAULT_ORDER.get(algo, "big")


# --------------------------------------------------------------------------- #
# 算法实现
# --------------------------------------------------------------------------- #
def _sum8(data: bytes) -> int:
    return sum(data) & 0xFF


def _sum8_inv(data: bytes) -> int:
    return (~sum(data)) & 0xFF


def _sum16(data: bytes) -> int:
    return sum(data) & 0xFFFF


def _xor8(data: bytes) -> int:
    value = 0
    for b in data:
        value ^= b
    return value


def _crc8(data: bytes) -> int:
    crc = 0x00
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def _crc16(data: bytes, poly: int, init: int) -> int:
    crc = init
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _crc16_ccitt_false(data: bytes) -> int:
    return _crc16(data, 0x1021, 0xFFFF)


def _crc16_xmodem(data: bytes) -> int:
    return _crc16(data, 0x1021, 0x0000)


def _crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


_IMPL = {
    "sum8": _sum8,
    "sum8_inv": _sum8_inv,
    "sum16": _sum16,
    "xor8": _xor8,
    "crc8": _crc8,
    "crc16_modbus": _crc16_modbus,
    "crc16_ccitt_false": _crc16_ccitt_false,
    "crc16_xmodem": _crc16_xmodem,
    "crc32": _crc32,
}


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
def compute(algo: str, data: bytes, byte_order: str | None = None) -> bytes:
    """计算校验值并返回其在帧中实际占用的字节（按 byte_order 排列）。"""
    if algo in ("", "none") or algo not in _IMPL:
        return b""
    size = ALGO_SIZES[algo]
    order = byte_order or ALGO_DEFAULT_ORDER[algo]
    return _IMPL[algo](data).to_bytes(size, order)


def to_int(algo: str, data: bytes, byte_order: str | None = None) -> int:
    raw = compute(algo, data, byte_order)
    order = byte_order or ALGO_DEFAULT_ORDER.get(algo, "big")
    return int.from_bytes(raw, order) if raw else 0
