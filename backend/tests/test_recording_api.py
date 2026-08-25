from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.paths import ConfigError
from app.recordings import RecordingConflict
from app.service import app


class FakeRecordingManager:
    def __init__(self) -> None:
        self.active = False
        self.status_value = {"sn": "camera-1", "state": "idle"}

    def has_active(self, _sn: str) -> bool:
        return self.active

    def start(self, **kwargs):
        if self.active:
            raise RecordingConflict("already recording")
        self.active = True
        self.status_value = {
            "sn": kwargs["sn"],
            "recording_id": "test-id",
            "state": "recording",
            "config_id": kwargs["config_id"],
            "segment_seconds": kwargs["segment_seconds"],
            "started_at": "2026-01-01T00:00:00Z",
            "stopped_at": None,
            "segment_count": 0,
            "recording_dir": str(kwargs["output_root"]),
            "output_dir": str(kwargs["output_root"] / "2026-01-01"),
            "relative_output_dir": str(kwargs["output_root"] / "2026-01-01"),
            "error": None,
        }
        # 同时验证命令工厂能够接受服务端生成的输出路径。
        kwargs["cmd_factory"](
            kwargs["output_root"] / "%Y-%m-%d" / "manual-%Y-%m-%d_%H-%M-%S-abcd1234.mp4"
        )
        return dict(self.status_value)

    def status(self, sn: str):
        return dict(self.status_value) if self.status_value["sn"] == sn else {"sn": sn, "state": "idle"}

    def stop(self, sn: str):
        if not self.active:
            return False, {"sn": sn, "state": "idle"}
        self.active = False
        self.status_value["state"] = "stopped"
        self.status_value["stopped_at"] = "2026-01-01T00:01:00Z"
        return True, dict(self.status_value)


class RecordingApiTests(unittest.TestCase):
    def setUp(self) -> None:
        app.config.update(TESTING=True)
        self.client = app.test_client()
        self.fake_manager = FakeRecordingManager()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.payload = {
            "errorCode": 0,
            "flashUrl": "https://example.test/live",
            "playKey": "secret",
            "relaySig": "relay",
        }
        self.patches = [
            patch("app.service.recording_manager", self.fake_manager),
            patch("app.service.service.find_camera", return_value={"sn": "camera-1", "enabled": True}),
            patch("app.service.get_decrypt_payload", return_value=self.payload),
            patch("app.service.service.get_recording_dir", return_value=Path(self.temp_dir.name)),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temp_dir.cleanup()

    def test_start_status_stop_flow(self) -> None:
        response = self.client.post(
            "/api/recordings/start",
            json={"sn": "camera-1", "config_id": 0, "segment_seconds": 300},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["recording"]["state"], "recording")

        response = self.client.get("/api/recordings/status?sn=camera-1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["recording"]["recording_id"], "test-id")

        response = self.client.post("/api/recordings/camera-1/stop")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["stopped"])

        response = self.client.post("/api/recordings/camera-1/stop")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["stopped"])

    def test_rejects_duplicate_and_invalid_parameters(self) -> None:
        self.fake_manager.active = True
        response = self.client.post(
            "/api/recordings/start",
            json={"sn": "camera-1", "config_id": 0, "segment_seconds": 300},
        )
        self.assertEqual(response.status_code, 409)

        self.fake_manager.active = False
        for body in (
            {"sn": "camera-1", "config_id": 4, "segment_seconds": 300},
            {"sn": "camera-1", "config_id": 0, "segment_seconds": 9},
            {"sn": "camera-1", "config_id": 0, "segment_seconds": 86401},
            {"sn": "camera-1", "config_id": True, "segment_seconds": 300},
            {"sn": "camera-1", "config_id": 0, "segment_seconds": False},
            {"sn": "camera-1", "config_id": 0, "segment_seconds": 300, "recording_dir": 123},
            {"config_id": 0, "segment_seconds": 300},
        ):
            with self.subTest(body=body):
                response = self.client.post("/api/recordings/start", json=body)
                self.assertEqual(response.status_code, 400)

    def test_rejects_unknown_camera(self) -> None:
        with patch("app.service.service.find_camera", side_effect=ConfigError("配置中未找到摄像机")):
            response = self.client.post(
                "/api/recordings/start",
                json={"sn": "unknown", "config_id": 0, "segment_seconds": 300},
            )
        self.assertEqual(response.status_code, 400)

    def test_accepts_segment_duration_boundaries(self) -> None:
        for segment_seconds in (10, 86400):
            self.fake_manager.active = False
            with self.subTest(segment_seconds=segment_seconds):
                response = self.client.post(
                    "/api/recordings/start",
                    json={"sn": "camera-1", "config_id": 0, "segment_seconds": segment_seconds},
                )
                self.assertEqual(response.status_code, 201)
                self.assertEqual(response.get_json()["recording"]["segment_seconds"], segment_seconds)

    def test_uses_default_30_minutes_and_custom_recording_path(self) -> None:
        custom_path = str(Path(self.temp_dir.name) / "camera archive")
        response = self.client.post(
            "/api/recordings/start",
            json={"sn": "camera-1", "config_id": 0, "recording_dir": custom_path},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["recording"]["segment_seconds"], 1800)

    def test_recording_settings(self) -> None:
        response = self.client.get("/api/recordings/settings")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["default_segment_seconds"], 1800)


if __name__ == "__main__":
    unittest.main()
