from __future__ import annotations

import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from queue import Queue
from threading import Event
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.recordings import RecordingConflict, RecordingManager


WAIT_FOR_STOP_SCRIPT = r"""
from datetime import datetime
import pathlib
import sys

output = pathlib.Path(datetime.now().astimezone().strftime(sys.argv[1]))
output.parent.mkdir(parents=True, exist_ok=True)
output.write_bytes(b'fake-mp4')
for line in sys.stdin:
    if line.strip() == 'stop':
        raise SystemExit(0)
"""

READ_SHARED_STREAM_SCRIPT = r"""
from datetime import datetime
import pathlib
import sys

output = pathlib.Path(datetime.now().astimezone().strftime(sys.argv[1]))
output.parent.mkdir(parents=True, exist_ok=True)
data = sys.stdin.buffer.read()
output.write_bytes(data or b'empty')
"""


class FakeSourceSession:
    def __init__(self) -> None:
        self.queue: Queue[bytes | None] = Queue()
        self.subscriber_id = 0

    def subscribe(self, **_kwargs):
        self.subscriber_id += 1
        return self.subscriber_id, self.queue

    def unsubscribe(self, _subscriber_id: int, **_kwargs) -> None:
        self.queue.put(None)


class RecordingManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.manager = RecordingManager(cwd=BACKEND_DIR.parent, stop_timeout=2)

    def tearDown(self) -> None:
        self.manager.close_all()
        self.temp_dir.cleanup()

    @staticmethod
    def command_factory(output_pattern: Path) -> list[str]:
        return [sys.executable, "-u", "-c", WAIT_FOR_STOP_SCRIPT, str(output_pattern)]

    def test_start_status_segment_count_and_graceful_stop(self) -> None:
        started = self.manager.start(
            sn="camera-1",
            config_id=0,
            segment_seconds=10,
            output_root=self.root,
            cmd_factory=self.command_factory,
        )
        self.assertEqual(started["state"], "recording")

        deadline = time.monotonic() + 2
        status = self.manager.status("camera-1")
        while status["segment_count"] != 1 and time.monotonic() < deadline:
            time.sleep(0.02)
            status = self.manager.status("camera-1")
        self.assertEqual(status["segment_count"], 1)
        self.assertEqual(Path(status["output_dir"]).name, datetime.now().astimezone().strftime("%Y-%m-%d"))
        self.assertTrue(next(Path(status["output_dir"]).glob("manual-*.mp4")).name.startswith("manual-"))

        stopped, status = self.manager.stop("camera-1")
        self.assertTrue(stopped)
        self.assertEqual(status["state"], "stopped")
        self.assertIsNotNone(status["stopped_at"])

    def test_rejects_second_task_for_same_camera(self) -> None:
        self.manager.start(
            sn="camera-1",
            config_id=0,
            segment_seconds=10,
            output_root=self.root,
            cmd_factory=self.command_factory,
        )
        with self.assertRaises(RecordingConflict):
            self.manager.start(
                sn="camera-1",
                config_id=1,
                segment_seconds=20,
                output_root=self.root,
                cmd_factory=self.command_factory,
            )

    def test_allows_different_cameras_and_stop_is_idempotent(self) -> None:
        for sn in ("camera-1", "camera-2"):
            self.manager.start(
                sn=sn,
                config_id=0,
                segment_seconds=10,
                output_root=self.root,
                cmd_factory=self.command_factory,
            )
        self.assertEqual(self.manager.status("camera-1")["state"], "recording")
        self.assertEqual(self.manager.status("camera-2")["state"], "recording")
        self.assertTrue(self.manager.stop("camera-1")[0])
        self.assertFalse(self.manager.stop("camera-1")[0])
        self.assertEqual(self.manager.status("camera-2")["state"], "recording")

    def test_marks_unexpected_process_failure(self) -> None:
        def failing_factory(_output_pattern: Path) -> list[str]:
            return [sys.executable, "-u", "-c", "import sys; print('decoder failed', file=sys.stderr); sys.exit(3)"]

        self.manager.start(
            sn="camera-fail",
            config_id=0,
            segment_seconds=10,
            output_root=self.root,
            cmd_factory=failing_factory,
        )
        deadline = time.monotonic() + 2
        status = self.manager.status("camera-fail")
        while status["state"] == "recording" and time.monotonic() < deadline:
            time.sleep(0.02)
            status = self.manager.status("camera-fail")
        self.assertEqual(status["state"], "failed")
        self.assertTrue(status["error"])

    def test_forced_stop_fallback_after_graceful_timeout(self) -> None:
        manager = RecordingManager(cwd=BACKEND_DIR.parent, stop_timeout=0.05)

        def ignoring_factory(_output_pattern: Path) -> list[str]:
            return [sys.executable, "-u", "-c", "import time; time.sleep(60)"]

        def kill_process(proc, _label, **_kwargs):
            proc.kill()
            return proc.wait(timeout=2)

        try:
            manager.start(
                sn="camera-timeout",
                config_id=0,
                segment_seconds=10,
                output_root=self.root,
                cmd_factory=ignoring_factory,
            )
            with patch("app.recordings.terminate_process_tree", side_effect=kill_process) as terminate:
                stopped, status = manager.stop("camera-timeout")
            self.assertTrue(stopped)
            self.assertEqual(status["state"], "failed")
            self.assertIn("强制停止", status["error"])
            terminate.assert_called_once()
        finally:
            manager.close_all()

    def test_shared_pipeline_feeds_remux_and_stops_with_eof(self) -> None:
        source = FakeSourceSession()

        def shared_factory(output_pattern: Path) -> list[str]:
            return [sys.executable, "-u", "-c", READ_SHARED_STREAM_SCRIPT, str(output_pattern)]

        started = self.manager.start(
            sn="camera-shared",
            config_id=0,
            segment_seconds=10,
            output_root=self.root,
            cmd_factory=shared_factory,
            pipeline_mode="shared",
            source_session=source,
        )
        self.assertEqual(started["pipeline_mode"], "shared")
        source.queue.put(b"shared-mpegts")
        stopped, status = self.manager.stop("camera-shared")
        self.assertTrue(stopped)
        self.assertEqual(status["state"], "stopped")
        output = next(self.root.glob("*/manual-*.mp4"))
        self.assertEqual(output.read_bytes(), b"shared-mpegts")

    def test_shared_source_ending_marks_recording_failed(self) -> None:
        source = FakeSourceSession()

        def shared_factory(output_pattern: Path) -> list[str]:
            return [sys.executable, "-u", "-c", READ_SHARED_STREAM_SCRIPT, str(output_pattern)]

        self.manager.start(
            sn="camera-source-failed",
            config_id=0,
            segment_seconds=10,
            output_root=self.root,
            cmd_factory=shared_factory,
            pipeline_mode="shared",
            source_session=source,
        )
        source.queue.put(None)
        deadline = time.monotonic() + 2
        status = self.manager.status("camera-source-failed")
        while status["state"] == "recording" and time.monotonic() < deadline:
            time.sleep(0.02)
            status = self.manager.status("camera-source-failed")
        self.assertEqual(status["state"], "failed")
        self.assertIn("公共解密流已结束", status["error"])

    def start_recoverable(self, source, factory):
        self.manager._reconnect_delay = 0.01
        return self.manager.start(
            sn="recovery", config_id=0, segment_seconds=10, output_root=self.root,
            cmd_factory=lambda pattern: [sys.executable, "-u", "-c", READ_SHARED_STREAM_SCRIPT, str(pattern)],
            pipeline_mode="shared", source_session=source, source_factory=factory,
        )

    def wait_status(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = self.manager.status("recovery")
            if predicate(status):
                return status
            time.sleep(0.01)
        self.fail(f"Timed out waiting for recovery: {status}")

    def test_source_reconnect_preserves_segments_and_task_identity(self):
        first, second = FakeSourceSession(), FakeSourceSession()
        started = self.start_recoverable(first, lambda: second)
        first.queue.put(b"first-generation")
        first.queue.put(None)
        status = self.wait_status(lambda s: s["reconnect_count"] == 1 and s["state"] == "recording")
        self.assertEqual(status["recording_id"], started["recording_id"])
        self.assertEqual(status["started_at"], started["started_at"])
        second.queue.put(b"second-generation")
        stopped, status = self.manager.stop("recovery")
        self.assertTrue(stopped)
        self.assertEqual(status["state"], "stopped")
        self.assertEqual(status["segment_count"], 2)
        self.assertEqual({p.read_bytes() for p in self.root.glob("*/manual-*.mp4")},
                         {b"first-generation", b"second-generation"})

    def test_refresh_failures_retry_without_leaking_credentials(self):
        first, second = FakeSourceSession(), FakeSourceSession()
        calls = []
        def refresh():
            calls.append(1)
            if len(calls) <= 2:
                raise RuntimeError("https://private-url/?playKey=secret")
            return second
        self.start_recoverable(first, refresh)
        first.queue.put(None)
        status = self.wait_status(lambda s: s["reconnect_count"] == 3 and s["state"] == "recording")
        self.assertNotIn("secret", str(status))
        self.assertIsNone(status["error"])

    def test_stop_during_refresh_prevents_late_restart_and_duplicate(self):
        first, second = FakeSourceSession(), FakeSourceSession()
        entered, release, returned = Event(), Event(), Event()
        def refresh():
            entered.set()
            release.wait(3)
            returned.set()
            return second
        self.start_recoverable(first, refresh)
        first.queue.put(None)
        try:
            self.assertTrue(entered.wait(2))
            self.assertTrue(self.manager.has_active("recovery"))
            with self.assertRaises(RecordingConflict):
                self.start_recoverable(second, refresh)
            stopped, status = self.manager.stop("recovery")
            self.assertTrue(stopped)
            self.assertEqual(status["state"], "stopped")
        finally:
            release.set()
        self.assertTrue(returned.wait(2))
        time.sleep(0.05)
        self.assertEqual(second.subscriber_id, 0)
        self.assertEqual(self.manager.status("recovery")["state"], "stopped")

    def test_stop_during_backoff_cancels_refresh(self):
        first = FakeSourceSession()
        calls = []
        self.start_recoverable(first, lambda: calls.append(1))
        self.manager._reconnect_delay = 60
        first.queue.put(None)
        self.wait_status(lambda s: s["state"] == "reconnecting")
        self.assertEqual(self.manager.stop("recovery")[1]["state"], "stopped")
        self.assertEqual(calls, [])

    def test_source_closing_before_subscription_retries(self):
        first, closed, second = FakeSourceSession(), FakeSourceSession(), FakeSourceSession()
        def subscribe(**kwargs):
            raise RuntimeError("decrypt session already closed")
        closed.subscribe = subscribe
        sources = iter([closed, second])
        self.start_recoverable(first, lambda: next(sources))
        first.queue.put(None)
        self.wait_status(lambda s: s["reconnect_count"] == 2 and s["state"] == "recording")

    def test_disk_failure_after_source_eof_does_not_reconnect(self):
        source = FakeSourceSession()
        calls = []
        self.manager.start(
            sn="recovery", config_id=0, segment_seconds=10, output_root=self.root,
            cmd_factory=lambda pattern: [sys.executable, "-u", "-c",
                "import sys; sys.stdin.buffer.read(); print('No space left on device', file=sys.stderr); sys.exit(1)"],
            pipeline_mode="shared", source_session=source, source_factory=lambda: calls.append(1),
        )
        source.queue.put(None)
        status = self.wait_status(lambda s: s["state"] == "failed")
        self.assertIn("No space left", status["error"])
        self.assertEqual(calls, [])

    def test_subscriber_overflow_is_not_silently_retried(self):
        first = FakeSourceSession()
        callbacks = []
        original_subscribe = first.subscribe
        def subscribe(**kwargs):
            callbacks.append(kwargs["on_drop"])
            return original_subscribe(**kwargs)
        first.subscribe = subscribe
        calls = []
        self.start_recoverable(first, lambda: calls.append(1))
        callbacks[0]("too slow")
        first.queue.put(None)
        status = self.wait_status(lambda s: s["state"] == "failed")
        self.assertIn("缓存溢出", status["error"])
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
