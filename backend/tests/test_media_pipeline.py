"""真实 FFmpeg + 本地 WASM 回归；不访问摄像机，不下载二进制。"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from queue import Queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app.decrypt_commands import build_playback_remux_command, build_recording_remux_command
from app.recordings import RecordingManager

WASM = ROOT / "backend/.cache/libffmpeg.js"


@unittest.skipUnless(all(shutil.which(cmd) for cmd in ("node", "ffmpeg", "ffprobe")) and WASM.is_file(),
                     "requires Node, FFmpeg, ffprobe and cached libffmpeg.js")
class MediaPipelineTests(unittest.TestCase):
    def test_recording_reconnect_produces_two_complete_real_mp4_files(self):
        class Source:
            def __init__(self):
                self.queue = Queue()
            def subscribe(self, **kwargs):
                return 1, self.queue
            def unsubscribe(self, *args, **kwargs):
                self.queue.put(None)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transport = self.run_command([
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=12",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000", "-t", "4",
                "-c:v", "libx264", "-preset", "ultrafast", "-g", "12", "-c:a", "aac",
                "-f", "mpegts", "pipe:1",
            ])
            first, second = Source(), Source()
            manager = RecordingManager(reconnect_delay=0.01)
            try:
                manager.start(
                    sn="media-recovery", config_id=0, segment_seconds=30, output_root=root,
                    cmd_factory=lambda pattern: build_recording_remux_command(output_path=pattern, segment_seconds=30),
                    pipeline_mode="shared", source_session=first, source_factory=lambda: second,
                )
                first.queue.put(transport)
                first.queue.put(None)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    status = manager.status("media-recovery")
                    if status["state"] == "recording" and status["reconnect_count"] == 1:
                        break
                    time.sleep(0.01)
                self.assertEqual(status["reconnect_count"], 1)
                self.assertEqual(status["state"], "recording")
                second.queue.put(transport)
                self.assertEqual(manager.stop("media-recovery")[1]["state"], "stopped")
                files = list(root.glob("*/manual-*.mp4"))
                self.assertEqual(len(files), 2)
                for file in files:
                    streams = self.streams(file)
                    video = next(s for s in streams if s["codec_type"] == "video")
                    self.assertEqual(int(video["nb_read_frames"]), 48)
                    self.assertAlmostEqual(float(video["duration"]), 4.0, places=1)
                    self.assertTrue(any(s["codec_type"] == "audio" for s in streams))
            finally:
                manager.close_all()

    def run_command(self, command, **kwargs):
        return subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, **kwargs).stdout

    def streams(self, file):
        return json.loads(self.run_command([
            "ffprobe", "-v", "error", "-count_frames", "-show_streams", "-of", "json", str(file)
        ]))["streams"]

    def test_long_input_and_different_source_frame_rates_do_not_truncate_or_deadlock(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for fps, seconds, audio in [(12, 60, "aac"), (25, 30, "aac"), (6, 30, "pcm_alaw")]:
                with self.subTest(fps=fps, seconds=seconds, audio=audio):
                    source, output = root / f"input-{fps}.flv", root / f"output-{fps}.ts"
                    self.run_command([
                        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate={fps}",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000", "-t", str(seconds),
                        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-g", str(fps),
                        "-c:a", audio, "-f", "flv", str(source),
                    ])
                    self.run_command([
                        "node", str(ROOT / "backend/decrypt_stream.js"), "--input-file", str(source),
                        "--libffmpeg-path", str(WASM), "--fps", "12", "--output", str(output), "--quiet",
                    ])
                    streams = self.streams(output)
                    self.assertLessEqual(abs(int(streams[0]["nb_read_frames"]) - seconds * 12), 1)
                    self.assertLess(abs(float(streams[0]["duration"]) - seconds), .1)
                    self.assertLess(abs(float(streams[1]["duration"]) - seconds), .3)

    def test_live_source_without_data_fails_with_timeout(self):
        release = threading.Event()

        class StalledSource(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "video/x-flv")
                self.end_headers()
                self.wfile.flush()
                release.wait(10)

        server = ThreadingHTTPServer(("127.0.0.1", 0), StalledSource)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = subprocess.run([
                "node", str(ROOT / "backend/decrypt_stream.js"),
                "--url", f"http://127.0.0.1:{server.server_port}/stream",
                "--libffmpeg-path", str(WASM), "--stall-timeout-ms", "500", "--quiet",
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("无进展", result.stderr.decode("utf-8", errors="replace"))
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_fragmented_flv_playback_and_recording_keep_all_video_frames(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "input.flv"
            self.run_command([
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=12",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000", "-t", "4",
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-g", "12",
                "-c:a", "aac", "-f", "flv", str(source),
            ])
            self.assertLess(source.stat().st_size, 512 * 1024)  # 旧启动阈值下无法输出
            for chunk_size in (13, 4096, 65536):
                with self.subTest(chunk_size=chunk_size):
                    transport = root / f"output-{chunk_size}.ts"
                    self.run_command([
                        "node", str(ROOT / "backend/decrypt_stream.js"), "--input-file", str(source),
                        "--libffmpeg-path", str(WASM), "--network-chunk-size", str(chunk_size),
                        "--output", str(transport), "--quiet",
                    ])
                    streams = self.streams(transport)
                    self.assertEqual(int(streams[0]["nb_read_frames"]), 48)
                    self.assertEqual(streams[1]["codec_name"], "aac")
                    self.assertLess(abs(float(streams[0]["duration"]) - float(streams[1]["duration"])), .2)

            data = transport.read_bytes()
            playback = root / "playback.mp4"
            playback.write_bytes(self.run_command(build_playback_remux_command(), input=data))
            self.assertEqual(int(self.streams(playback)[0]["nb_read_frames"]), 48)
            self.run_command(build_recording_remux_command(
                output_path=root / "recording-%03d.mp4", segment_seconds=2, segment_strftime=False,
            ), input=data)
            recordings = sorted(root.glob("recording-*.mp4"))
            self.assertGreaterEqual(len(recordings), 2)
            self.assertEqual(sum(int(self.streams(file)[0]["nb_read_frames"]) for file in recordings), 48)
            # 已经在录像的公共源会从 GOP 中间接入新观众；探测必须等到下一份 SPS/IDR。
            for fraction in (.07, .18, .29, .42, .57):
                with self.subTest(late_join=fraction):
                    playback.write_bytes(self.run_command(build_playback_remux_command(), input=data[int(len(data) * fraction):]))
                    self.assertGreater(int(self.streams(playback)[0]["nb_read_frames"]), 0)


if __name__ == "__main__":
    unittest.main()
