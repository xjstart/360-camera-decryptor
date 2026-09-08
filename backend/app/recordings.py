#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立录像任务的生命周期管理。"""

from __future__ import annotations

import os
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Queue
from threading import Event, Lock, Thread
from typing import Any, Optional

from .stream_sessions import terminate_process_tree


ACTIVE_STATES = {"recording", "reconnecting", "stopping"}


class RecordingConflict(RuntimeError):
    """同一摄像机已经存在活动录像任务。"""


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_recording_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


@dataclass
class RecordingTask:
    sn: str
    recording_id: str
    config_id: int
    segment_seconds: int
    output_root: Path
    file_glob: str
    proc: subprocess.Popen[Any]
    pipeline_mode: str = "independent"
    source_session: Optional[Any] = None
    source_subscriber_id: Optional[int] = None
    source_queue: Optional[Queue[Optional[bytes]]] = None
    state: str = "recording"
    started_at: str = field(default_factory=utc_now_text)
    stopped_at: Optional[str] = None
    error: Optional[str] = None
    stop_requested: bool = False
    stderr_tail: list[str] = field(default_factory=list)
    stop_event: Event = field(default_factory=Event)
    source_factory: Optional[Any] = None
    cmd_factory: Optional[Any] = None
    output_pattern: Optional[Path] = None
    max_pending_input_bytes: int = 16 * 1024 * 1024
    source_ended: bool = False
    reconnect_count: int = 0
    last_reconnect_reason: Optional[str] = None


class RecordingManager:
    """保证每台摄像机最多运行一个独立录像进程。"""

    def __init__(self, *, logger: Optional[Any] = None, cwd: Optional[Path] = None, stop_timeout: float = 10.0, reconnect_delay: float = 2.0):
        self._lock = Lock()
        self._logger = logger
        self._cwd = cwd
        self._stop_timeout = stop_timeout
        self._reconnect_delay = reconnect_delay
        self._tasks: dict[str, RecordingTask] = {}

    def has_active(self, sn: str) -> bool:
        with self._lock:
            task = self._tasks.get(sn)
            return bool(task and task.state in ACTIVE_STATES)

    def start(
        self,
        *,
        sn: str,
        config_id: int,
        segment_seconds: int,
        output_root: Path,
        cmd_factory: Any,
        pipeline_mode: str = "independent",
        source_session: Optional[Any] = None,
        source_factory: Optional[Any] = None,
        max_pending_input_bytes: int = 16 * 1024 * 1024,
    ) -> dict[str, Any]:
        """创建目录并启动录像；cmd_factory 接收输出文件模式。"""
        if pipeline_mode not in {"shared", "independent"}:
            raise ValueError(f"不支持的录像管线模式: {pipeline_mode}")
        if pipeline_mode == "shared" and source_session is None:
            raise ValueError("共享录像模式缺少解密流会话")
        with self._lock:
            current = self._tasks.get(sn)
            if current and current.state in ACTIVE_STATES:
                raise RecordingConflict(f"摄像机 {sn} 已在录制")

            recording_id = build_recording_id()
            local_now = datetime.now().astimezone()
            self._ensure_date_directory(output_root, local_now)
            suffix = recording_id.rsplit("-", 1)[-1]
            file_glob = f"manual-*-{suffix}.mp4"
            strftime_root = Path(str(output_root).replace("%", "%%"))
            output_pattern = strftime_root / "%Y-%m-%d" / f"manual-%Y-%m-%d_%H-%M-%S-{suffix}.mp4"
            cmd = cmd_factory(output_pattern)
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                cwd=os.fspath(self._cwd or os.getcwd()),
                start_new_session=True,
            )

            task = RecordingTask(
                sn=sn,
                recording_id=recording_id,
                config_id=config_id,
                segment_seconds=segment_seconds,
                output_root=output_root,
                file_glob=file_glob,
                proc=proc,
                pipeline_mode=pipeline_mode,
                source_session=source_session,
                source_factory=source_factory,
                cmd_factory=cmd_factory,
                output_pattern=output_pattern,
                max_pending_input_bytes=max_pending_input_bytes,
            )
            if pipeline_mode == "shared":
                def on_source_drop(reason: str) -> None:
                    with self._lock:
                        if not task.stop_requested and task.error is None:
                            task.error = f"录像输入缓存溢出: {reason}"

                try:
                    subscriber_id, source_queue = source_session.subscribe(
                        kind="recording",
                        max_pending_bytes=max_pending_input_bytes,
                        on_drop=on_source_drop,
                    )
                except Exception:
                    terminate_process_tree(proc, f"recording[{sn}]", logger=self._log_warning)
                    raise
                task.source_subscriber_id = subscriber_id
                task.source_queue = source_queue
            self._tasks[sn] = task

        Thread(target=self._monitor, args=(task,), daemon=True).start()
        Thread(target=self._maintain_date_directories, args=(task,), daemon=True).start()
        if self._logger:
            self._logger.info("recording[%s] started id=%s segment=%ss", sn, recording_id, segment_seconds)
        return self._serialize(task)

    def status(self, sn: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(sn)
            if task is None:
                return {"sn": sn, "state": "idle"}
            return self._serialize(task)

    def stop(self, sn: str) -> tuple[bool, dict[str, Any]]:
        with self._lock:
            task = self._tasks.get(sn)
            if task is None or task.state not in ACTIVE_STATES:
                return False, {"sn": sn, "state": "idle"} if task is None else self._serialize(task)
            if task.state == "stopping":
                return False, self._serialize(task)
            task.state = "stopping"
            task.stop_requested = True
            task.stop_event.set()

        graceful = False
        try:
            if task.pipeline_mode == "shared":
                if task.source_session is not None and task.source_subscriber_id is not None:
                    task.source_session.unsubscribe(task.source_subscriber_id, preserve_pending=True)
                    task.source_subscriber_id = None
            elif task.proc.stdin and task.proc.stdin.writable():
                task.proc.stdin.write(b"stop\n")
                task.proc.stdin.flush()
            task.proc.wait(timeout=self._stop_timeout)
            graceful = True
        except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
            with self._lock:
                task.error = task.error or "录像未能正常收尾，已强制停止；最后一个分片可能不完整"
            terminate_process_tree(task.proc, f"recording[{sn}]", logger=self._log_warning)
        finally:
            if task.proc.stdin:
                try:
                    task.proc.stdin.close()
                except OSError:
                    pass

        if self._logger:
            self._logger.info("recording[%s] stop requested graceful=%s", sn, graceful)
        # monitor 通常已更新终态；这里保证 API 返回时状态已经稳定。
        self._finalize(task, task.proc.poll())
        return True, self.status(sn)

    def close_all(self) -> int:
        with self._lock:
            sns = [sn for sn, task in self._tasks.items() if task.state in ACTIVE_STATES]
        for sn in sns:
            self.stop(sn)
        return len(sns)

    def _monitor(self, task: RecordingTask) -> None:
        while True:
            stderr_thread = Thread(target=self._drain_stderr, args=(task,), daemon=True)
            stderr_thread.start()
            feeder = None
            if task.pipeline_mode == "shared":
                feeder = Thread(target=self._feed_shared_stream, args=(task,), daemon=True)
                feeder.start()
            return_code = task.proc.wait()
            if task.source_session is not None and task.source_subscriber_id is not None:
                task.source_session.unsubscribe(task.source_subscriber_id)
            # Old workers must finish before task.proc/session can be replaced.
            if feeder is not None:
                feeder.join()
            stderr_thread.join()
            task.source_subscriber_id = None
            if task.proc.stdin:
                try:
                    task.proc.stdin.close()
                except OSError:
                    pass
            disk_failure = any(marker in line.lower() for line in task.stderr_tail for marker in (
                "no space left", "permission denied", "input/output error", "error writing", "disk full",
            ))
            if disk_failure and not task.error:
                task.error = "录像写入失败: " + task.stderr_tail[-1]
            if (task.source_ended and task.source_factory is not None
                    and not task.error and not disk_failure and not task.stop_requested):
                if self._reconnect(task):
                    continue
            if task.source_ended and not task.stop_requested and not task.error:
                task.error = "公共解密流已结束"
            self._finalize(task, return_code)
            return

    def _reconnect(self, task: RecordingTask) -> bool:
        """Keep the logical recording alive; refresh outside the lock so stop stays responsive."""
        with self._lock:
            if task.stop_requested:
                return False
            task.state = "reconnecting"
            task.last_reconnect_reason = "公共解密流已结束，正在刷新播放信息并续录"
        failures = 0
        while not task.stop_event.wait(min(30.0, self._reconnect_delay * (2 ** min(failures, 4)))):
            with self._lock:
                task.reconnect_count += 1
            try:
                source = task.source_factory()
                if task.stop_event.is_set():
                    return False
                def on_drop(reason: str) -> None:
                    with self._lock:
                        if not task.stop_requested and task.error is None:
                            task.error = f"录像输入缓存溢出: {reason}"
                subscriber_id, queue = source.subscribe(
                    kind="recording", max_pending_bytes=task.max_pending_input_bytes, on_drop=on_drop,
                )
            except Exception as exc:
                # Do not expose refreshed URLs or credentials in status/logs.
                with self._lock:
                    task.last_reconnect_reason = f"刷新解密源失败（{type(exc).__name__}），稍后重试"
                failures += 1
                continue
            with self._lock:
                if task.stop_requested:
                    source.unsubscribe(subscriber_id)
                    return False
                suffix = task.recording_id.rsplit("-", 1)[-1]
                pattern = task.output_pattern.with_name(
                    f"manual-%Y-%m-%d_%H-%M-%S-r{task.reconnect_count:04d}-{suffix}.mp4"
                )
                try:
                    self._ensure_date_directory(task.output_root)
                    proc = subprocess.Popen(
                        task.cmd_factory(pattern), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE, cwd=os.fspath(self._cwd or os.getcwd()), start_new_session=True,
                    )
                except Exception as exc:
                    source.unsubscribe(subscriber_id)
                    task.error = f"续录进程启动失败（{type(exc).__name__}）"
                    return False
                task.proc = proc
                task.source_session = source
                task.source_subscriber_id = subscriber_id
                task.source_queue = queue
                task.source_ended = False
                task.stderr_tail.clear()
                task.state = "recording"
            return True
        return False

    def _feed_shared_stream(self, task: RecordingTask) -> None:
        """把公共 MPEG-TS 送入录像 remux；结束订阅即向 ffmpeg 发送 EOF。"""
        try:
            if task.source_queue is None or task.proc.stdin is None:
                return
            while True:
                chunk = task.source_queue.get()
                if chunk is None:
                    if not task.stop_requested:
                        with self._lock:
                            if task.error is None and task.proc.poll() is None:
                                task.source_ended = True
                    break
                task.proc.stdin.write(chunk)
                task.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            if not task.stop_requested:
                with self._lock:
                    if task.error is None:
                        task.error = f"录像 remux 输入失败: {exc}"
        finally:
            if task.source_session is not None and task.source_subscriber_id is not None:
                task.source_session.unsubscribe(task.source_subscriber_id)
                task.source_subscriber_id = None
            if task.proc.stdin:
                try:
                    task.proc.stdin.close()
                except OSError:
                    pass

    def _drain_stderr(self, task: RecordingTask) -> None:
        if not task.proc.stderr:
            return
        try:
            for raw_line in iter(task.proc.stderr.readline, b""):
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                with self._lock:
                    task.stderr_tail.append(line)
                    del task.stderr_tail[:-20]
                if self._logger:
                    self._logger.warning("recording[%s]: %s", task.sn, line)
        finally:
            task.proc.stderr.close()

    def _finalize(self, task: RecordingTask, return_code: Optional[int]) -> None:
        if return_code is None:
            return
        with self._lock:
            if task.stopped_at is not None:
                return
            task.stopped_at = utc_now_text()
            task.stop_event.set()
            if task.error:
                task.state = "failed"
            elif return_code == 0:
                task.state = "stopped"
            else:
                task.state = "failed"
                task.error = task.stderr_tail[-1] if task.stderr_tail else f"录像进程异常退出，返回码 {return_code}"

    def _serialize(self, task: RecordingTask) -> dict[str, Any]:
        try:
            segment_count = sum(1 for path in task.output_root.glob(f"*/{task.file_glob}") if path.is_file())
        except OSError:
            segment_count = 0
        current_output_dir = task.output_root / datetime.now().astimezone().strftime("%Y-%m-%d")
        return {
            "sn": task.sn,
            "recording_id": task.recording_id,
            "state": task.state,
            "config_id": task.config_id,
            "pipeline_mode": task.pipeline_mode,
            "segment_seconds": task.segment_seconds,
            "started_at": task.started_at,
            "stopped_at": task.stopped_at,
            "segment_count": segment_count,
            "recording_dir": str(task.output_root),
            "output_dir": str(current_output_dir),
            "relative_output_dir": str(current_output_dir),
            "error": task.error,
            "reconnect_count": task.reconnect_count,
            "last_reconnect_reason": task.last_reconnect_reason,
        }

    @staticmethod
    def _ensure_date_directory(output_root: Path, now: Optional[datetime] = None) -> None:
        local_now = now or datetime.now().astimezone()
        (output_root / local_now.strftime("%Y-%m-%d")).mkdir(parents=True, exist_ok=True)

    def _maintain_date_directories(self, task: RecordingTask) -> None:
        """跨午夜前预建次日目录，让 strftime 分片落到正确日期。"""
        while not task.stop_event.is_set():
            try:
                local_now = datetime.now().astimezone()
                self._ensure_date_directory(task.output_root, local_now)
                next_midnight = (local_now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                wait_seconds = max(1.0, (next_midnight - local_now).total_seconds() - 5.0)
                if task.stop_event.wait(wait_seconds):
                    return
                self._ensure_date_directory(task.output_root, next_midnight)
                if task.stop_event.wait(10):
                    return
            except OSError as exc:
                if self._logger:
                    self._logger.warning("recording[%s] failed to create date directory: %s", task.sn, exc)
                if task.stop_event.wait(60):
                    return

    def _log_warning(self, message: str, args: tuple[Any, ...]) -> None:
        if self._logger:
            self._logger.warning(message, *args)
