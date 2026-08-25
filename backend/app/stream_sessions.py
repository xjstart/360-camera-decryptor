#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务端解密流进程管理。

一次服务端解密播放会启动一个 Node 进程，Node 内部再启动 ffmpeg 做 MPEG-TS
封装。这里统一管理这棵进程树、多个浏览器订阅者，以及失败重试时的旧进程回收。
"""

from __future__ import annotations

import os
import signal
import subprocess
from queue import Empty, Full, Queue
from threading import Lock, Thread, Timer
from pathlib import Path
from typing import Any, Callable, Optional


LoggerCallable = Callable[[str, tuple[Any, ...]], None]


def terminate_process_tree(
    proc: subprocess.Popen[Any],
    label: str,
    *,
    logger: Optional[LoggerCallable] = None,
    timeout: float = 3.0,
) -> Optional[int]:
    """尽量回收整个子进程树，避免 Node 退出后 ffmpeg 继续残留。

    `subprocess.Popen(..., start_new_session=True)` 会让 Node 成为新进程组组长。
    因此这里优先对进程组发送信号，而不是只杀 Node 本身；这样 ffmpeg、下载器等
    子进程也会一起退出。
    """

    if proc.poll() is not None:
        return proc.returncode

    def warn(message: str, *args: Any) -> None:
        if logger:
            logger(message, args)

    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            warn("%s failed to terminate Windows process tree: %s", label, exc)
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            warn("%s still running after taskkill", label)
        return proc.poll()

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return proc.poll()
    except OSError as exc:
        warn("%s failed to send SIGTERM to process group: %s", label, exc)
        proc.terminate()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        warn("%s did not exit after SIGTERM, sending SIGKILL", label)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            warn("%s failed to send SIGKILL to process group: %s", label, exc)
            proc.kill()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            warn("%s still running after SIGKILL", label)

    return proc.poll()


class SharedDecryptSession:
    """一个可被多个 HTTP 响应共享的 Node+ffmpeg 解密管线。"""

    def __init__(
        self,
        key: str,
        group_key: str,
        proc: subprocess.Popen[Any],
        label: str,
        idle_timeout_seconds: int,
        *,
        logger: Optional[Any] = None,
    ):
        self.key = key
        # group_key 按“摄像机 + 解密配置”归组，重试启动新流时用它关闭旧流。
        self.group_key = group_key
        self.proc = proc
        self.label = label
        self.logger = logger
        self.idle_timeout_seconds = max(1, idle_timeout_seconds)
        self._lock = Lock()
        self._subscribers: dict[int, Queue[Optional[bytes]]] = {}
        self._next_subscriber_id = 0
        self._idle_timer: Optional[Timer] = None
        self._closed = False
        self._return_code: Optional[int] = None
        Thread(target=self._log_stderr, daemon=True).start()
        Thread(target=self._fanout_stdout, daemon=True).start()

    def subscribe(self) -> tuple[int, Queue[Optional[bytes]]]:
        """注册一个浏览器/客户端订阅者，并返回它自己的输出队列。"""
        queue: Queue[Optional[bytes]] = Queue(maxsize=8)
        with self._lock:
            if self._closed:
                raise RuntimeError("decrypt session already closed")
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None
            self._next_subscriber_id += 1
            subscriber_id = self._next_subscriber_id
            self._subscribers[subscriber_id] = queue
        return subscriber_id, queue

    def unsubscribe(self, subscriber_id: int) -> None:
        """注销订阅者；无人观看后延迟停止进程，给播放器短暂重连留余地。"""
        should_schedule_idle = False
        with self._lock:
            queue = self._subscribers.pop(subscriber_id, None)
            if queue is not None:
                self._signal_queue_end(queue)
            should_schedule_idle = not self._closed and not self._subscribers and self.proc.poll() is None
            if should_schedule_idle and self._idle_timer is None:
                self._idle_timer = Timer(self.idle_timeout_seconds, self._terminate_if_idle)
                self._idle_timer.daemon = True
                self._idle_timer.start()
        if should_schedule_idle and self.logger:
            self.logger.info(
                "%s has no subscribers, will stop after %ss idle timeout",
                self.label,
                self.idle_timeout_seconds,
            )

    def close(self, reason: str = "closed") -> Optional[int]:
        """主动关闭会话，用于失败重试、服务退出或替换同组旧流。"""
        if self.logger:
            self.logger.info("%s closing decrypt session: %s", self.label, reason)
        return_code = terminate_process_tree(self.proc, self.label, logger=self._log_warning)
        with self._lock:
            self._close_locked(return_code)
        return return_code

    def return_code(self) -> Optional[int]:
        with self._lock:
            return self._return_code

    def is_closed(self) -> bool:
        with self._lock:
            return self._closed

    def _log_warning(self, message: str, args: tuple[Any, ...]) -> None:
        if self.logger:
            self.logger.warning(message, *args)

    def _log_stderr(self) -> None:
        if proc_stderr := self.proc.stderr:
            try:
                for raw_line in iter(proc_stderr.readline, b""):
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if line and self.logger:
                        self.logger.warning("%s: %s", self.label, line)
            finally:
                proc_stderr.close()

    def _fanout_stdout(self) -> None:
        try:
            assert self.proc.stdout is not None
            while True:
                chunk = self.proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                self._publish_chunk(chunk)
        finally:
            if self.proc.stdout is not None:
                self.proc.stdout.close()
            return_code = terminate_process_tree(self.proc, self.label, logger=self._log_warning)
            with self._lock:
                self._close_locked(return_code)
            if return_code not in (0, None) and self.logger:
                self.logger.warning("%s exited with code %s", self.label, return_code)

    def _publish_chunk(self, chunk: bytes) -> None:
        stale_subscribers: list[int] = []
        with self._lock:
            items = list(self._subscribers.items())
        for subscriber_id, queue in items:
            try:
                queue.put_nowait(chunk)
            except Full:
                stale_subscribers.append(subscriber_id)
        for subscriber_id in stale_subscribers:
            if self.logger:
                self.logger.warning("%s subscriber=%s is too slow, dropping it", self.label, subscriber_id)
            self.unsubscribe(subscriber_id)

    def _terminate_if_idle(self) -> None:
        with self._lock:
            self._idle_timer = None
            if self._closed or self._subscribers or self.proc.poll() is not None:
                return
        if self.logger:
            self.logger.info("%s idle timeout reached, stopping decrypt process", self.label)
        self.close("idle timeout")

    def _close_locked(self, return_code: Optional[int]) -> None:
        if self._closed:
            if return_code is not None and self._return_code is None:
                self._return_code = return_code
            return
        self._closed = True
        self._return_code = return_code
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None
        subscribers = list(self._subscribers.values())
        self._subscribers.clear()
        for queue in subscribers:
            self._signal_queue_end(queue)

    @staticmethod
    def _signal_queue_end(queue: Queue[Optional[bytes]]) -> None:
        try:
            queue.put_nowait(None)
        except Full:
            try:
                queue.get_nowait()
            except Empty:
                pass
            try:
                queue.put_nowait(None)
            except Full:
                pass


class SharedDecryptSessionManager:
    """保存所有解密会话，并处理同组重试时的旧进程替换。"""

    def __init__(self, *, logger: Optional[Any] = None, cwd: Optional[Path] = None) -> None:
        self._lock = Lock()
        self._logger = logger
        # 子进程工作目录固定在项目根目录，避免从不同目录启动 Flask 时找不到缓存文件。
        self._cwd = cwd
        self._sessions: dict[str, SharedDecryptSession] = {}
        self._group_index: dict[str, str] = {}

    def get_or_create(
        self,
        *,
        key: str,
        group_key: str,
        label: str,
        idle_timeout_seconds: int,
        cmd: list[str],
        replace_group: bool = False,
    ) -> SharedDecryptSession:
        """获取或创建会话。

        replace_group=True 时，只要同一摄像机/配置已有旧会话且 key 不一致，就会先
        主动关闭旧会话。这正是“播放失败后重新启动新流”需要的资源回收行为。
        """

        with self._lock:
            self._drop_dead_sessions_locked()

            session = self._sessions.get(key)
            if session and not session.is_closed() and session.proc.poll() is None and not replace_group:
                return session
            if session and replace_group:
                session.close("replaced by an explicit retry")
                self._sessions.pop(key, None)
                if self._group_index.get(group_key) == key:
                    self._group_index.pop(group_key, None)

            old_key = self._group_index.get(group_key)
            old_session = self._sessions.get(old_key or "")
            if replace_group and old_session and old_key != key:
                old_session.close("replaced by a fresh stream")
                self._sessions.pop(old_key or "", None)
                self._group_index.pop(group_key, None)

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=os.fspath(self._cwd or os.getcwd()),
                start_new_session=True,
            )
            session = SharedDecryptSession(
                key,
                group_key,
                proc,
                label,
                idle_timeout_seconds,
                logger=self._logger,
            )
            self._sessions[key] = session
            self._group_index[group_key] = key
            return session

    def close_group(self, group_key: str, reason: str = "closed by request") -> bool:
        """按摄像机/配置关闭会话，供前端停止按钮显式调用。"""
        with self._lock:
            key = self._group_index.pop(group_key, None)
            session = self._sessions.pop(key or "", None)
        if not session:
            return False
        session.close(reason)
        return True

    def close_all(self, reason: str = "server shutdown") -> int:
        """关闭全部会话，主要用于测试或未来接入服务退出钩子。"""
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._group_index.clear()
        for session in sessions:
            session.close(reason)
        return len(sessions)

    def _drop_dead_sessions_locked(self) -> None:
        for key, session in list(self._sessions.items()):
            if session.is_closed() or session.proc.poll() is not None:
                self._sessions.pop(key, None)
                if self._group_index.get(session.group_key) == key:
                    self._group_index.pop(session.group_key, None)
