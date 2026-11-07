"""配置 / 结果 的 JSON 读写，以及 QSettings 键名常量。"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

from . import __version__, codec
from .framer import FrameConfig

ORG_NAME = "DebugScript"
APP_NAME = "FrameParser"

CONFIG_SUFFIX = ".framecfg.json"
RESULT_SUFFIX = ".result.json"


# --------------------------------------------------------------------------- #
# 工程配置（分帧设置 + 字段表 + 输入设置 + 列样式）
# --------------------------------------------------------------------------- #
def build_config_payload(
    frame_config: FrameConfig,
    fields: list[dict],
    input_settings: dict,
    column_states: dict | None = None,
    input_text: str = "",
) -> dict:
    return {
        "tool": APP_NAME,
        "version": __version__,
        "type": "frame_parser_config",
        "frame_config": frame_config.to_dict(),
        "fields": fields,
        "input": input_settings,
        "columns": column_states or {},
        "input_text": input_text,
    }


def save_json(path: str | Path, payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    Path(path).write_text(text, encoding="utf-8")


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_config(path: str | Path, payload: dict) -> None:
    save_json(path, payload)


def load_config(path: str | Path) -> dict:
    data = load_json(path)
    if not isinstance(data, dict):
        raise ValueError("配置文件格式不正确：根节点必须是对象")
    return data


# --------------------------------------------------------------------------- #
# 解析结果导出
# --------------------------------------------------------------------------- #
def build_result_document(
    input_info: dict,
    frame_config: FrameConfig,
    split_result,
    parsed_by_frame: dict[int, list],
    columns: dict | None = None,
    limit_frames: int | None = None,
) -> dict:
    """构造解析结果文档。

    limit_frames：只导出前 N 帧（界面预览用；导出文件时不传就是全部）。
    每帧只给 raw_hex / raw_dec / raw_ascii，不再重复存 raw_bin（可由 hex 推出，省一大半体积）。
    """
    selected = split_result.frames
    truncated = False
    if limit_frames is not None and len(selected) > limit_frames:
        selected = selected[:limit_frames]
        truncated = True

    frames = []
    for frame in selected:
        parsed = parsed_by_frame.get(frame.index, [])
        frames.append({
            "index": frame.index,
            "offset": frame.offset,
            "length": frame.size,
            "tag": frame.tag,
            "check": frame.check_state,
            "note": frame.note,
            "raw_hex": codec.bytes_to_text(frame.data, codec.RADIX_HEX),
            "raw_dec": list(frame.data),
            "raw_ascii": frame.data.decode("utf-8", errors="replace"),
            "fields": [item.to_dict() for item in parsed],
        })
    return {
        "tool": APP_NAME,
        "version": __version__,
        "type": "frame_parser_result",
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "input": input_info,
        "frame_config": frame_config.to_dict(),
        "columns": columns or {},
        "stats": {
            "frame_count": len(split_result.frames),
            "exported_frames": len(frames),
            "frames_truncated": truncated,
            "total_bytes": split_result.total_bytes,
            "check_ok": split_result.ok_count,
            "check_fail": split_result.fail_count,
        },
        "warnings": list(split_result.warnings),
        "frames": frames,
    }


def dump_result(path: str | Path, document: dict) -> None:
    save_json(path, document)
