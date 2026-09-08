from __future__ import annotations

import sys
import time
import unittest
from queue import Full
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.stream_sessions import SharedDecryptSession, SharedDecryptSessionManager, SubscriberQueue


KEEP_ALIVE_COMMAND = [
    sys.executable,
    "-u",
    "-c",
    "import time; time.sleep(60)",
]


class SharedDecryptSessionTests(unittest.TestCase):
    def test_small_pipe_write_is_delivered_before_process_exit(self) -> None:
        session = self.manager.get_or_create(
            key="short-write", group_key="short-write", label="short-write", idle_timeout_seconds=2,
            cmd=[sys.executable, "-u", "-c", "import time; time.sleep(.2); print('frame', flush=True); time.sleep(60)"],
        )
        _, queue = session.subscribe()
        self.assertEqual(queue.get(timeout=2).strip(), b"frame")
        self.assertIsNone(session.proc.poll())

    def setUp(self) -> None:
        self.manager = SharedDecryptSessionManager(cwd=BACKEND_DIR.parent)

    def tearDown(self) -> None:
        self.manager.close_all("test cleanup")

    def create_session(self, key: str = "source-1") -> SharedDecryptSession:
        return self.manager.get_or_create(
            key=key,
            group_key="camera-group",
            label=key,
            idle_timeout_seconds=2,
            cmd=KEEP_ALIVE_COMMAND,
        )

    def test_playback_stop_keeps_recording_source_alive(self) -> None:
        session = self.create_session()
        playback_id, _ = session.subscribe(kind="playback")
        recording_id, _ = session.subscribe(kind="recording", max_pending_bytes=1024 * 1024)

        result = self.manager.close_playback_group("camera-group")
        self.assertTrue(result["closed"])
        self.assertTrue(result["source_kept_alive"])
        self.assertFalse(session.has_consumers("playback"))
        self.assertTrue(session.has_consumers("recording"))
        self.assertIsNone(session.proc.poll())

        session.unsubscribe(playback_id)
        session.unsubscribe(recording_id)

    def test_refresh_creates_new_generation_without_stopping_recording(self) -> None:
        old_session = self.create_session("source-1")
        recording_id, _ = old_session.subscribe(kind="recording", max_pending_bytes=1024 * 1024)

        new_session = self.manager.get_or_create(
            key="source-1",
            group_key="camera-group",
            label="source-1-refreshed",
            idle_timeout_seconds=2,
            cmd=KEEP_ALIVE_COMMAND,
            replace_group=True,
        )
        self.assertIsNot(new_session, old_session)
        self.assertIsNone(old_session.proc.poll())
        self.assertTrue(old_session.has_consumers("recording"))
        self.assertIsNone(new_session.proc.poll())

        old_session.unsubscribe(recording_id)

    def test_non_active_matching_recording_generation_is_reused(self) -> None:
        old_session = self.create_session("recording-source")
        recording_id, _ = old_session.subscribe(kind="recording", max_pending_bytes=1024 * 1024)
        new_session = self.manager.get_or_create(
            key="fresh-playback-source",
            group_key="camera-group",
            label="fresh-playback-source",
            idle_timeout_seconds=2,
            cmd=KEEP_ALIVE_COMMAND,
            replace_group=True,
        )
        reused = self.manager.get_or_create(
            key="recording-source",
            group_key="camera-group",
            label="should-not-start",
            idle_timeout_seconds=2,
            cmd=KEEP_ALIVE_COMMAND,
        )
        self.assertIs(reused, old_session)
        self.assertIsNot(reused, new_session)
        old_session.unsubscribe(recording_id)

    def test_slow_playback_is_dropped_without_stopping_recording(self) -> None:
        session = self.create_session()
        playback_id, _ = session.subscribe(kind="playback", max_pending_bytes=3)
        recording_id, recording_queue = session.subscribe(kind="recording", max_pending_bytes=1024 * 1024)

        session._publish_chunk(b"one")
        session._publish_chunk(b"two")
        self.assertFalse(session.has_consumers("playback"))
        self.assertTrue(session.has_consumers("recording"))
        self.assertEqual(recording_queue.get_nowait(), b"one")

        session.unsubscribe(playback_id)
        session.unsubscribe(recording_id)

    def test_idle_session_stops_after_last_consumer_leaves(self) -> None:
        session = self.manager.get_or_create(
            key="idle-source",
            group_key="idle-group",
            label="idle-source",
            idle_timeout_seconds=1,
            cmd=KEEP_ALIVE_COMMAND,
        )
        subscriber_id, _ = session.subscribe(kind="playback")
        session.unsubscribe(subscriber_id)
        deadline = time.monotonic() + 3
        while session.proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(session.proc.poll())

    def test_recording_overflow_reports_drop_reason(self) -> None:
        session = self.create_session("overflow-source")
        reasons: list[str] = []
        _subscriber_id, _queue = session.subscribe(
            kind="recording",
            max_pending_bytes=3,
            on_drop=reasons.append,
        )
        session._publish_chunk(b"one")
        session._publish_chunk(b"two")
        self.assertFalse(session.has_consumers("recording"))
        self.assertEqual(reasons, ["recording consumer exceeded its pending input limit"])


class SubscriberQueueTests(unittest.TestCase):
    def test_limit_tracks_actual_bytes_and_recovers_after_read(self):
        queue = SubscriberQueue(10)
        for _ in range(10):
            queue.put_nowait(b"x")
        with self.assertRaises(Full):
            queue.put_nowait(b"x")
        self.assertEqual(queue.get_nowait(), b"x")
        queue.put_nowait(b"y")
        self.assertEqual(queue.pending_bytes, 10)

    def test_recording_eof_preserves_full_queue_without_waiting(self):
        queue = SubscriberQueue(3)
        queue.put_nowait(b"abc")
        started = time.monotonic()
        queue.close(preserve_pending=True)
        self.assertLess(time.monotonic() - started, .1)
        with self.assertRaises(Full):
            queue.put_nowait(b"later")
        self.assertEqual(queue.get_nowait(), b"abc")
        self.assertIsNone(queue.get_nowait())
        self.assertEqual(queue.pending_bytes, 0)


if __name__ == "__main__":
    unittest.main()
