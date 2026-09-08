#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务端解密进程的命令构建逻辑。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from .paths import NODE_DECRYPT_SCRIPT, ConfigError


SUPPORTED_CONFIG_IDS = {0, 1, 2, 3}
SUPPORTED_OUTPUT_FORMATS = {"mpegts", "mp4"}


def build_decrypt_command(
    *,
    config_id: int,
    payload: Mapping[str, Any],
    decrypt_options: Mapping[str, int],
    fps: str = "12",
    output_format: str = "mpegts",
    output_path: Optional[Path] = None,
    segment_seconds: Optional[int] = None,
    segment_strftime: bool = False,
    control_stdin: bool = False,
) -> list[str]:
    """根据播放信息构造 Node + ffmpeg 解密命令。"""
    if config_id not in SUPPORTED_CONFIG_IDS:
        raise ConfigError(f"不支持的 config_id: {config_id}，支持的配置ID为 0-3")
    if output_format not in SUPPORTED_OUTPUT_FORMATS:
        raise ConfigError("format 仅支持 mpegts 或 mp4")
    if segment_seconds is not None and (output_format != "mp4" or output_path is None):
        raise ConfigError("分片录制必须指定 MP4 输出路径")

    flash_url = str(payload.get("flashUrl") or "").strip()
    play_key = str(payload.get("playKey") or "").strip()
    relay_sig = str(payload.get("relaySig") or "").strip()
    if not flash_url:
        raise ConfigError("播放信息缺少 flashUrl，无法启动服务端解密")
    if not NODE_DECRYPT_SCRIPT.is_file():
        raise ConfigError(f"缺少解密脚本: {NODE_DECRYPT_SCRIPT}")

    cmd = [
        "node",
        str(NODE_DECRYPT_SCRIPT),
        "--url",
        flash_url,
        "--fps",
        str(fps),
        "--quiet",
        "--network-chunk-size",
        str(decrypt_options["decrypt_network_chunk_size"]),
        "--min-decoder-buffer-size",
        str(decrypt_options.get("decrypt_min_decoder_buffer_size", 1)),
        "--max-pending-input-bytes",
        str(decrypt_options["decrypt_max_pending_input_bytes"]),
        "--max-pending-video-bytes",
        str(decrypt_options["decrypt_max_pending_video_bytes"]),
        "--max-pending-audio-bytes",
        str(decrypt_options["decrypt_max_pending_audio_bytes"]),
        "--ffmpeg-threads",
        str(decrypt_options["decrypt_ffmpeg_threads"]),
        "--output-format",
        output_format,
    ]

    if config_id in {0, 1, 3}:
        if not play_key:
            raise ConfigError("播放信息缺少 playKey，无法启动服务端解密")
        cmd.extend(["--play-key", play_key, "--key-type", "1" if config_id == 1 else "0"])
    else:
        cmd.extend(["--key-type", "0"])

    if config_id == 3 and relay_sig:
        cmd.extend(["--relay-sig", relay_sig])
    if output_path is not None:
        cmd.extend(["--output", str(output_path)])
    if segment_seconds is not None:
        cmd.extend(["--segment-seconds", str(segment_seconds)])
    if segment_strftime:
        cmd.append("--segment-strftime")
    if control_stdin:
        cmd.append("--control-stdin")
    return cmd


def build_shared_decrypt_command(
    *,
    config_id: int,
    payload: Mapping[str, Any],
    decrypt_options: Mapping[str, int],
    fps: str = "12",
) -> list[str]:
    """构造可被播放和录像共同订阅的 MPEG-TS 解密/编码主干。"""
    return build_decrypt_command(
        config_id=config_id,
        payload=payload,
        decrypt_options=decrypt_options,
        fps=fps,
        output_format="mpegts",
    )


def build_playback_remux_command() -> list[str]:
    """把公共 MPEG-TS 无重编码封装成浏览器 MSE 使用的 fMP4。"""
    # 公共源可能从 GOP 中间加入；1.5 秒覆盖编码端 1 秒 GOP，不能盲目压到 0.5 秒。
    return [
        "ffmpeg",
        "-loglevel",
        "error",
        "-fflags",
        "+genpts",
        "-f",
        "mpegts",
        "-probesize",
        "1048576",
        "-analyzeduration",
        "1500000",
        "-i",
        "pipe:0",
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c",
        "copy",
        "-bsf:a",
        "aac_adtstoasc",
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        "frag_keyframe+empty_moov+default_base_moof+omit_tfhd_offset",
        "-frag_duration",
        "250000",
        "-flush_packets",
        "1",
        "-max_interleave_delta",
        "100000",
        "-f",
        "mp4",
        "pipe:1",
    ]


def build_recording_remux_command(
    *,
    output_path: Path,
    segment_seconds: int,
    segment_strftime: bool = True,
) -> list[str]:
    """把公共 MPEG-TS 无重编码保存成独立 MP4 分片。"""
    cmd = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-fflags",
        "+genpts",
        "-f",
        "mpegts",
        "-probesize",
        "1048576",
        "-analyzeduration",
        "1500000",
        "-i",
        "pipe:0",
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c",
        "copy",
        "-bsf:a",
        "aac_adtstoasc",
        "-f",
        "segment",
        "-segment_time",
        str(segment_seconds),
        "-reset_timestamps",
        "1",
        "-segment_start_number",
        "1",
        "-segment_format",
        "mp4",
        "-segment_format_options",
        "movflags=+faststart",
    ]
    if segment_strftime:
        cmd.extend(["-strftime", "1"])
    cmd.append(str(output_path))
    return cmd
