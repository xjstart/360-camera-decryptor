from __future__ import annotations

import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
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
            self.assertEqual(status["state"], "stopped")
            terminate.assert_called_once()
        finally:
            manager.close_all()


if __name__ == "__main__":
    unittest.main()
