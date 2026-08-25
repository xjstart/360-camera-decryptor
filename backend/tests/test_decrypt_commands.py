from __future__ import annotations

import sys
import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.decrypt_commands import build_decrypt_command
from app.paths import ConfigError
from app.service import CameraBackendService


OPTIONS = {
    "decrypt_network_chunk_size": 65536,
    "decrypt_max_pending_input_bytes": 524288,
    "decrypt_max_pending_video_bytes": 6291456,
    "decrypt_max_pending_audio_bytes": 524288,
    "decrypt_ffmpeg_threads": 1,
}


class DecryptCommandTests(unittest.TestCase):
    def test_builds_segmented_mp4_recording_command(self) -> None:
        output = Path("recordings/%Y-%m-%d/manual-%Y-%m-%d_%H-%M-%S-abcd1234.mp4")
        command = build_decrypt_command(
            config_id=3,
            payload={"flashUrl": "https://example.test/live", "playKey": "secret", "relaySig": "relay"},
            decrypt_options=OPTIONS,
            output_format="mp4",
            output_path=output,
            segment_seconds=300,
            segment_strftime=True,
            control_stdin=True,
        )
        self.assertIn("--segment-seconds", command)
        self.assertEqual(command[command.index("--segment-seconds") + 1], "300")
        self.assertEqual(command[command.index("--output") + 1], str(output))
        self.assertIn("--control-stdin", command)
        self.assertIn("--segment-strftime", command)
        self.assertIn("--relay-sig", command)

    def test_allows_config_two_without_play_key(self) -> None:
        command = build_decrypt_command(
            config_id=2,
            payload={"flashUrl": "https://example.test/live"},
            decrypt_options=OPTIONS,
        )
        self.assertNotIn("--play-key", command)
        self.assertEqual(command[command.index("--key-type") + 1], "0")

    def test_requires_play_key_for_encrypted_configs(self) -> None:
        for config_id in (0, 1, 3):
            with self.subTest(config_id=config_id), self.assertRaises(ConfigError):
                build_decrypt_command(
                    config_id=config_id,
                    payload={"flashUrl": "https://example.test/live"},
                    decrypt_options=OPTIONS,
                )

    def test_rejects_segmenting_without_mp4_output(self) -> None:
        with self.assertRaises(ConfigError):
            build_decrypt_command(
                config_id=2,
                payload={"flashUrl": "https://example.test/live"},
                decrypt_options=OPTIONS,
                segment_seconds=10,
            )

    def test_path_component_cannot_escape_recording_root(self) -> None:
        self.assertEqual(CameraBackendService.safe_path_component(".."), "unknown")
        self.assertEqual(CameraBackendService.safe_path_component("camera/one"), "camera_one")

    def test_recording_directory_can_be_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.yaml"
            config_path.write_text("server: {}\n", encoding="utf-8")
            service = CameraBackendService(str(config_path))
            expected = root / "custom archive"
            with patch.object(Path, "resolve", side_effect=OSError(22, "virtual cloud drive")):
                actual = service.get_recording_dir(str(expected))
            self.assertEqual(actual, Path(os.path.abspath(expected)))
            self.assertTrue(expected.is_dir())


if __name__ == "__main__":
    unittest.main()
