#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Flask app and shared service layer for the backend."""

from __future__ import annotations

import atexit
import os
import re
import time
import json
import shutil
import subprocess
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Dict, Optional

import requests
import yaml
from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context
from werkzeug.middleware.proxy_fix import ProxyFix

from .api_client import CameraAPIRequest
from .decrypt_commands import (
    SUPPORTED_CONFIG_IDS,
    SUPPORTED_OUTPUT_FORMATS,
    build_decrypt_command,
    build_playback_remux_command,
    build_recording_remux_command,
    build_shared_decrypt_command,
)
from .paths import BACKEND_DIR, ROOT_DIR, WEB_DIR, ConfigError
from .recordings import RecordingConflict, RecordingManager
from .stream_sessions import SharedDecryptSessionManager, terminate_process_tree


class CameraBackendService:
    """摄像机播放信息服务。

    这一层只负责“配置、认证、播放信息、缓存、go2rtc 配置生成”等业务逻辑，
    不直接关心 Flask 请求对象，也不直接管理 Node/ffmpeg 子进程。这样路由层、
    流进程层和业务层边界更清楚，后面排查问题时不会一团混在一起。
    """

    def __init__(self, config_path: Optional[str] = None):
        self.data_dir = BACKEND_DIR / "data"
        self.example_config_path = BACKEND_DIR / "config.example.yaml"
        default_config_path = self.data_dir / "config.yaml"
        legacy_config_path = BACKEND_DIR / "configs" / "config.yaml"
        selected_config_path = config_path or (
            default_config_path if default_config_path.exists() or not legacy_config_path.exists() else legacy_config_path
        )
        self.config_path = Path(selected_config_path)
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._lock = Lock()

    def ensure_config_file(self) -> None:
        """确保运行配置存在；首次启动时从模板复制一份。"""
        if self.config_path.exists():
            return
        if self.config_path == self.data_dir / "config.yaml" and self.example_config_path.exists():
            self.data_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.example_config_path, self.config_path)
            return

    def load_config(self) -> Dict[str, Any]:
        """读取 YAML 配置。

        这里每次读取文件，便于用户修改 config.yaml 后刷新页面立即生效；如果后续
        配置变大，再考虑加文件 mtime 缓存。
        """
        self.ensure_config_file()
        if not self.config_path.exists():
            raise ConfigError(f"配置文件不存在: {self.config_path}")
        with self.config_path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}

    def get_request_interval(self) -> float:
        return float(self.load_config().get("request_interval", 2))

    def get_play_info_cache_dir(self) -> Path:
        server_config = self.load_config().get("server", {})
        configured = (server_config.get("play_info_cache_dir") or "").strip()
        cache_dir = Path(configured) if configured else self.data_dir / "play_info_cache"
        if not cache_dir.is_absolute():
            cache_dir = ROOT_DIR / cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir

    def get_recording_dir(self, override: Optional[str] = None) -> Path:
        """返回录像根目录；网页可以为当前录像覆盖默认配置。"""
        server_config = self.load_config().get("server", {})
        configured = str(override or server_config.get("recording_dir") or "").strip()
        configured = os.path.expandvars(os.path.expanduser(configured))
        recording_dir = Path(configured) if configured else self.data_dir / "recordings"
        if not recording_dir.is_absolute():
            recording_dir = ROOT_DIR / recording_dir
        # 不使用 Path.resolve()：部分 Windows 云盘/虚拟盘虽然可以正常读写，
        # 但不支持查询底层卷的真实路径，会因此抛出 WinError 1005。
        recording_dir = Path(os.path.abspath(os.path.normpath(os.fspath(recording_dir))))
        recording_dir.mkdir(parents=True, exist_ok=True)
        return recording_dir

    @staticmethod
    def safe_path_component(value: str, fallback: str = "unknown") -> str:
        normalized = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("_")
        return fallback if normalized in {"", ".", ".."} else normalized

    def get_decrypt_stream_options(self) -> Dict[str, int]:
        """读取服务端解密管线的限流/超时配置。

        这些参数会传给 Node 解密器，用来控制网络读取、输入队列、ffmpeg 队列和
        空闲回收时间，避免失败流无限吃 CPU/内存。
        """
        server_config = self.load_config().get("server", {})
        defaults = {
            "decrypt_network_chunk_size": 64 * 1024,
            "decrypt_max_pending_input_bytes": 512 * 1024,
            "decrypt_max_pending_video_bytes": 12 * 1024 * 1024,
            "decrypt_max_pending_audio_bytes": 1024 * 1024,
            "decrypt_ffmpeg_threads": 1,
            "decrypt_idle_timeout_seconds": 10,
            "recording_max_pending_input_bytes": 16 * 1024 * 1024,
        }
        options: Dict[str, int] = {}
        for key, default in defaults.items():
            raw_value = server_config.get(key, default)
            try:
                options[key] = max(1, int(raw_value))
            except (TypeError, ValueError):
                options[key] = default
        return options

    def share_decrypt_session_between_playback_and_recording(self) -> bool:
        raw_value = self.load_config().get("server", {}).get(
            "share_decrypt_session_between_playback_and_recording",
            True,
        )
        if raw_value is None:
            return True
        if isinstance(raw_value, str):
            return raw_value.strip().lower() not in {"0", "false", "no", "off"}
        return bool(raw_value)

    def list_cameras(self) -> list[Dict[str, Any]]:
        """返回前端展示所需的摄像机列表，不暴露 Cookie 等敏感配置。"""
        config = self.load_config()
        return [
            {
                "name": camera.get("name", ""),
                "sn": camera.get("sn", ""),
                "enabled": camera.get("enabled", True),
                "api_version": camera.get("api_version", "v2").lower(),
            }
            for camera in config.get("cameras", [])
        ]

    def find_camera(self, sn: str) -> Dict[str, Any]:
        for camera in self.list_cameras():
            if camera.get("sn") == sn:
                if not camera.get("enabled", True):
                    raise ConfigError(f"摄像机 {sn} 已被禁用")
                return camera
        raise ConfigError(f"配置中未找到摄像机 SN: {sn}")

    def _extract_auth_cookies(self, config: Dict[str, Any]) -> Dict[str, str]:
        """兼容多种 Cookie 写法，统一整理成 requests 可使用的字典。"""
        auth_cookies: Dict[str, str] = {}
        cookie_config = config.get("cookie")

        if isinstance(cookie_config, list):
            for item in cookie_config:
                if not isinstance(item, dict):
                    continue
                for key, value in item.items():
                    key_text = str(key).strip()
                    value_text = str(value or "").strip()
                    if key_text and value_text:
                        auth_cookies[key_text] = value_text
            return auth_cookies

        if isinstance(cookie_config, dict):
            for key, value in cookie_config.items():
                key_text = str(key).strip()
                value_text = str(value or "").strip()
                if key_text and value_text:
                    auth_cookies[key_text] = value_text
            return auth_cookies

        if isinstance(cookie_config, str) and cookie_config.strip():
            for item in cookie_config.split(";"):
                item = item.strip()
                if "=" not in item:
                    continue
                key, value = item.split("=", 1)
                key_text = key.strip()
                value_text = value.strip()
                if key_text and value_text:
                    auth_cookies[key_text] = value_text
            return auth_cookies

        for key in ("Q", "T", "jia_web_sid", "__NS_Q", "__NS_T", "__guid", "__DC_gid"):
            value_text = str(config.get(key) or "").strip()
            if value_text:
                auth_cookies[key] = value_text
        return auth_cookies

    def _build_api_client(self, config: Dict[str, Any]) -> CameraAPIRequest:
        """根据配置创建 360 API 客户端，并校验必要认证字段。"""
        api = CameraAPIRequest(verbose=False)
        auth_cookies = self._extract_auth_cookies(config)
        required_fields = ["Q", "T", "jia_web_sid"]
        missing = [name for name in required_fields if not auth_cookies.get(name)]

        if not missing:
            api.set_cookies(auth_cookies)
            return api

        # 兼容旧版整段 cookie 配置，避免已有部署立即失效。
        cookie = config.get("cookie")
        if isinstance(cookie, str) and cookie.strip():
            api.set_cookie_from_string(cookie.strip())
            return api

        raise ConfigError(
            "config.yaml 中缺少认证字段，请在 cookie 中填写 Q、T、jia_web_sid"
            if len(missing) == len(required_fields)
            else f"config.yaml 中缺少认证字段: {', '.join(missing)}"
        )
        return api

    def _get_cache_file_path(self, sn: str) -> Path:
        """把摄像机 SN 转成安全文件名，避免特殊字符逃出缓存目录。"""
        safe_sn = self.safe_path_component(sn)
        return self.get_play_info_cache_dir() / f"{safe_sn}.json"

    def _load_persisted_play_info(self, sn: str) -> Optional[Dict[str, Any]]:
        """读取磁盘播放信息缓存；失败时只记录日志，不中断主流程。"""
        cache_file = self._get_cache_file_path(sn)
        if not cache_file.exists():
            return None
        try:
            with cache_file.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            app.logger.warning("failed to load persisted play-info cache for sn=%s: %s", sn, exc)
            return None
        if not isinstance(payload, dict):
            return None
        payload.setdefault("cache_source", "persisted")
        return payload

    def _save_persisted_play_info(self, sn: str, payload: Dict[str, Any]) -> None:
        """原子写入播放信息缓存，降低写到一半时文件损坏的概率。"""
        cache_file = self._get_cache_file_path(sn)
        tmp_file = cache_file.with_suffix(".json.tmp")
        try:
            with tmp_file.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            tmp_file.replace(cache_file)
        except OSError as exc:
            app.logger.warning("failed to persist play-info cache for sn=%s: %s", sn, exc)
            try:
                if tmp_file.exists():
                    tmp_file.unlink()
            except OSError:
                pass

    def _remember_play_info(self, sn: str, payload: Dict[str, Any], cached_at: Optional[float] = None) -> Dict[str, Any]:
        """同时更新内存缓存与磁盘缓存。"""
        normalized_payload = dict(payload)
        cache_time = cached_at if cached_at is not None else time.time()
        with self._lock:
            self._cache[sn] = {"cached_at": cache_time, "payload": dict(normalized_payload)}
        self._save_persisted_play_info(sn, normalized_payload)
        return normalized_payload

    def _upstream_stream_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Referer": "https://my.jia.360.cn/",
        }

    def _can_open_stream(self, payload: Dict[str, Any]) -> bool:
        """探测缓存里的 flashUrl 是否仍可打开，过期时触发强制刷新。"""
        flash_url = (payload or {}).get("flashUrl")
        if not flash_url:
            return False
        try:
            upstream = requests.get(
                flash_url,
                headers=self._upstream_stream_headers(),
                stream=True,
                timeout=(5, 10),
            )
        except requests.RequestException as exc:
            app.logger.warning("cached flashUrl probe failed for sn=%s: %s", payload.get("camera_sn", ""), exc)
            return False
        try:
            return upstream.status_code < 400
        finally:
            upstream.close()

    def _fetch_from_remote(self, sn: str, camera: Dict[str, Any], api: CameraAPIRequest) -> Dict[str, Any]:
        preferred_version = camera.get("api_version", "v2").lower()
        result = api.fetch_play_info(sn, preferred_version=preferred_version)
        if result and result.get("errorCode") == 0:
            request_meta = result.get("request_meta", {})
            result["api_version"] = request_meta.get("version", preferred_version)
            return result

        app.logger.warning(
            "play-info upstream failed for sn=%s camera=%s result=%s attempts=%s",
            sn,
            camera.get("name", ""),
            result,
            result.get("request_attempts", []),
        )
        return result

    def get_play_info(self, sn: str, force_refresh: bool = False) -> Dict[str, Any]:
        """获取播放信息，优先使用短期内存缓存和长期磁盘缓存。"""
        now = time.time()
        cache_ttl = int(self.load_config().get("server", {}).get("play_info_cache_seconds", 30))

        persisted_payload: Optional[Dict[str, Any]] = None

        if not force_refresh:
            with self._lock:
                cached = self._cache.get(sn)
                if cached and now - cached["cached_at"] < cache_ttl:
                    return dict(cached["payload"])

            persisted_payload = self._load_persisted_play_info(sn)
            if persisted_payload:
                # 本地文件作为长期缓存，避免每次请求都打到上游 play-info 接口。
                return self._remember_play_info(sn, persisted_payload, cached_at=now)

        camera = self.find_camera(sn)
        payload = self._fetch_from_remote(sn, camera, self._build_api_client(self.load_config()))
        if payload.get("errorCode") != 0:
            if persisted_payload:
                app.logger.warning(
                    "play-info refresh failed for sn=%s, falling back to persisted cache result=%s",
                    sn,
                    payload,
                )
                return self._remember_play_info(sn, persisted_payload, cached_at=now)
            return payload

        payload["camera_name"] = camera.get("name", "")
        payload["camera_sn"] = sn
        payload["fetched_at"] = int(now)
        payload["cache_source"] = "remote"
        return self._remember_play_info(sn, payload, cached_at=now)

    def get_play_info_for_stream(self, sn: str, force_refresh: bool = False) -> Dict[str, Any]:
        payload = self.get_play_info(sn, force_refresh=force_refresh)
        if force_refresh or payload.get("errorCode") != 0:
            return payload
        if self._can_open_stream(payload):
            return payload

        app.logger.warning("persisted play-info could not open stream for sn=%s, refreshing upstream", sn)
        refreshed = self.get_play_info(sn, force_refresh=True)
        if refreshed.get("errorCode") == 0:
            refreshed["cache_source"] = "remote_refresh"
        return refreshed

    def get_stream_url(self, sn: str, force_refresh: bool = False) -> str:
        payload = self.get_play_info(sn, force_refresh=force_refresh)
        flash_url = payload.get("flashUrl")
        if payload.get("errorCode") != 0 or not flash_url:
            raise ConfigError(payload.get("errorMsg", "未获取到 flashUrl"))
        return flash_url

    def build_go2rtc_stream_name(self, camera: Dict[str, Any]) -> str:
        raw_name = (camera.get("name") or camera.get("sn") or "camera").strip().lower()
        normalized = re.sub(r"[^a-z0-9]+", "_", raw_name).strip("_")
        sn_suffix = (camera.get("sn") or "unknown").lower()[-6:]
        if normalized:
            return f"{normalized}_{sn_suffix}"
        return f"camera_{sn_suffix}"

    def build_go2rtc_stream_entry(self, camera: Dict[str, Any], public_base_url: str, mode: str = "raw", config_id: int = 0) -> Dict[str, Any]:
        stream_name = self.build_go2rtc_stream_name(camera)
        if mode == "decrypted":
            source_url = f"{public_base_url}/api/decrypted-stream/{config_id}/{camera['sn']}"
            go2rtc_source = f"{source_url}#input=mpegts"
        else:
            source_url = f"{public_base_url}/api/go2rtc/stream/{camera['sn']}"
            go2rtc_source = f"{source_url}#input=flv"
        return {
            "name": camera.get("name", ""),
            "sn": camera.get("sn", ""),
            "stream_name": stream_name,
            "source_url": source_url,
            "go2rtc_source": go2rtc_source,
            "mode": mode,
        }

    def build_go2rtc_config(self, public_base_url: str, sn: Optional[str] = None, mode: str = "raw", config_id: int = 0) -> Dict[str, Any]:
        if sn:
            cameras = [self.find_camera(sn)]
        else:
            cameras = [camera for camera in self.list_cameras() if camera.get("enabled", True)]

        streams: Dict[str, list[str]] = {}
        items = []
        for camera in cameras:
            entry = self.build_go2rtc_stream_entry(camera, public_base_url, mode=mode, config_id=config_id)
            streams[entry["stream_name"]] = [entry["go2rtc_source"]]
            items.append(entry)

        yaml_text = yaml.safe_dump(
            {"streams": streams},
            allow_unicode=True,
            sort_keys=False,
        )
        return {
            "public_base_url": public_base_url,
            "streams": items,
            "yaml": yaml_text,
            "count": len(items),
            "mode": mode,
            "note": (
                "当前输出用于 go2rtc 拉取服务端解密后的 MPEG-TS 流。"
                if mode == "decrypted"
                else "当前输出用于 go2rtc 拉取后端代理的 FLV 流。"
            ),
        }

    def sync_camera(self, camera: Dict[str, Any], force_refresh: bool = True) -> Dict[str, Any]:
        sn = camera.get("sn", "")
        if not sn:
            return {"success": False, "camera_name": camera.get("name", ""), "camera_sn": "", "errorMsg": "SN 号为空，跳过"}

        payload = self.get_play_info(sn, force_refresh=force_refresh)
        if payload.get("errorCode") != 0:
            return {
                "success": False,
                "camera_name": camera.get("name", ""),
                "camera_sn": sn,
                "errorCode": payload.get("errorCode"),
                "errorMsg": payload.get("errorMsg", "获取播放信息失败"),
            }

        result = {
            "success": True,
            "camera_name": payload.get("camera_name", camera.get("name", "")),
            "camera_sn": payload.get("camera_sn", sn),
            "api_version": payload.get("api_version", camera.get("api_version", "v2")),
            "payload": payload,
        }
        return result

    def sync_all_cameras(self, force_refresh: bool = True) -> Dict[str, Any]:
        cameras = [camera for camera in self.list_cameras() if camera.get("enabled", True)]
        interval = self.get_request_interval()
        results = []
        success_count = 0

        for index, camera in enumerate(cameras):
            if index > 0 and interval > 0:
                time.sleep(interval)
            result = self.sync_camera(camera, force_refresh=force_refresh)
            results.append(result)
            if result.get("success"):
                success_count += 1

        return {
            "total": len(cameras),
            "success": success_count,
            "failed": len(cameras) - success_count,
            "request_interval": interval,
            "results": results,
        }


service = CameraBackendService(os.environ.get("CAMERA_CONFIG_PATH"))
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_port=1)
decrypt_session_manager = SharedDecryptSessionManager(logger=app.logger, cwd=ROOT_DIR)
recording_manager = RecordingManager(logger=app.logger, cwd=ROOT_DIR)
atexit.register(decrypt_session_manager.close_all)
atexit.register(recording_manager.close_all)


def add_cors_headers(response: Response) -> Response:
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET,OPTIONS,POST"
    response.headers["Cache-Control"] = "no-store"
    return response


def get_public_base_url() -> str:
    server_config = service.load_config().get("server", {})
    configured_base_url = (os.environ.get("CAMERA_PUBLIC_BASE_URL") or server_config.get("public_base_url") or "").strip()
    if configured_base_url:
        return configured_base_url.rstrip("/")
    forwarded_proto = request.headers.get("X-Forwarded-Proto", request.scheme).split(",")[0].strip()
    forwarded_host = request.headers.get("X-Forwarded-Host", request.host).split(",")[0].strip()
    return f"{forwarded_proto}://{forwarded_host}".rstrip("/")


def build_go2rtc_response(sn: Optional[str] = None, mode: str = "raw", config_id: int = 0) -> Dict[str, Any]:
    return service.build_go2rtc_config(get_public_base_url(), sn=sn, mode=mode, config_id=config_id)


def build_decrypt_group_key(config_id: int, sn: str) -> str:
    """同一摄像机和同一解密配置共用一个进程组标识。"""
    return json.dumps({"config_id": config_id, "sn": sn}, sort_keys=True, ensure_ascii=True)


def build_decrypt_source_key(config_id: int, sn: str, fps: str, payload: Dict[str, Any], *, shared: bool) -> str:
    """公共源身份不包含最终封装格式；独立模式仍区分自己的输出代次。"""
    return json.dumps(
        {
            "pipeline": "shared" if shared else "independent",
            "config_id": config_id,
            "sn": sn,
            "fps": fps,
            "flash_url": payload.get("flashUrl") or "",
            "play_key": payload.get("playKey") or "",
            "relay_sig": (payload.get("relaySig") or "") if config_id == 3 else "",
        },
        sort_keys=True,
        ensure_ascii=True,
    )


def get_decrypt_payload(sn: str, *, force_refresh: bool = False) -> Dict[str, Any]:
    payload = service.get_play_info_for_stream(sn, force_refresh=force_refresh)
    if payload.get("errorCode") != 0:
        raise ConfigError(payload.get("errorMsg", "未获取到播放信息"))
    return payload


@app.after_request
def apply_default_headers(response: Response) -> Response:
    return add_cors_headers(response)


@app.route("/api/<path:_path>", methods=["OPTIONS"])
def api_options(_path: str) -> Response:
    return Response(status=204)


@app.route("/api/health")
def health() -> Response:
    return jsonify({"ok": True, "service": "360-camera-backend"})


@app.route("/api/cameras")
def cameras() -> Response:
    try:
        return jsonify({"cameras": service.list_cameras()})
    except ConfigError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/play-info")
def play_info() -> Response:
    sn = (request.args.get("sn") or "").strip()
    if not sn:
        return jsonify({"errorCode": -1, "errorMsg": "缺少 sn 参数"}), 400
    try:
        payload = service.get_play_info(sn, force_refresh=request.args.get("refresh") == "1")
    except ConfigError as exc:
        return jsonify({"errorCode": -1, "errorMsg": str(exc)}), 400
    if payload.get("errorCode") != 0:
        return jsonify(payload), 502

    payload = dict(payload)
    payload["sourceFlashUrl"] = payload.get("flashUrl")
    payload["flashUrl"] = f"{get_public_base_url()}/api/stream/{sn}"
    payload["proxyMode"] = "stream_proxy"
    payload["backendDecryptReady"] = True
    payload["backendDecryptUrl"] = f"{get_public_base_url()}/api/decrypted-stream/0/{sn}"
    payload["backendDecryptNote"] = "后端已支持 Node+ffmpeg 服务端解密流，前端可直接测试 MPEG-TS 输出。"
    return jsonify(payload)


@app.route("/api/play-info/sync", methods=["POST"])
def sync_all_play_info() -> Response:
    try:
        summary = service.sync_all_cameras(force_refresh=request.args.get("refresh", "1") == "1")
    except ConfigError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(summary), 200 if summary.get("failed", 0) == 0 else 207


@app.route("/api/go2rtc/config")
def go2rtc_config() -> Response:
    sn = (request.args.get("sn") or "").strip() or None
    response_format = (request.args.get("format") or "json").strip().lower()
    mode = (request.args.get("mode") or "raw").strip().lower()
    config_id = int(request.args.get("config_id", "0"))
    if mode not in {"raw", "decrypted"}:
        return jsonify({"error": "mode 仅支持 raw 或 decrypted"}), 400
    try:
        payload = build_go2rtc_response(sn=sn, mode=mode, config_id=config_id)
    except ConfigError as exc:
        return jsonify({"error": str(exc)}), 400

    if response_format == "yaml":
        return Response(payload["yaml"], content_type="text/yaml; charset=utf-8")
    return jsonify(payload)


@app.route("/api/stream/<sn>")
def proxy_stream(sn: str) -> Response:
    try:
        remote_url = service.get_stream_url(sn, force_refresh=request.args.get("refresh") == "1")
    except ConfigError as exc:
        return jsonify({"error": str(exc)}), 400

    upstream_headers = service._upstream_stream_headers()
    if request.headers.get("User-Agent"):
        upstream_headers["User-Agent"] = request.headers["User-Agent"]
    if request.headers.get("Accept"):
        upstream_headers["Accept"] = request.headers["Accept"]

    def open_upstream(url: str):
        return requests.get(url, headers=upstream_headers, stream=True, timeout=(10, 60))

    try:
        upstream = open_upstream(remote_url)
    except requests.RequestException as exc:
        app.logger.warning("stream open failed for sn=%s with cached play-info, refreshing: %s", sn, exc)
        try:
            remote_url = service.get_stream_url(sn, force_refresh=True)
            upstream = open_upstream(remote_url)
        except ConfigError as config_exc:
            return jsonify({"error": str(config_exc)}), 400
        except requests.RequestException as refresh_exc:
            return jsonify({"error": f"上游流请求失败: {refresh_exc}"}), 502

    if upstream.status_code >= 400:
        details = upstream.text[:400]
        upstream.close()
        app.logger.warning("stream open returned status=%s for sn=%s, refreshing play-info", upstream.status_code, sn)
        try:
            remote_url = service.get_stream_url(sn, force_refresh=True)
            upstream = open_upstream(remote_url)
        except ConfigError as config_exc:
            return jsonify({"error": str(config_exc)}), 400
        except requests.RequestException as refresh_exc:
            return jsonify({"error": f"上游流请求失败: {refresh_exc}"}), 502
        if upstream.status_code >= 400:
            details = upstream.text[:400]
            upstream.close()
            return jsonify({"error": "上游流返回错误", "status_code": upstream.status_code, "details": details}), 502

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    response = Response(
        stream_with_context(generate()),
        status=upstream.status_code,
        content_type=upstream.headers.get("Content-Type", "video/x-flv"),
    )
    if upstream.headers.get("Content-Length"):
        response.headers["Content-Length"] = upstream.headers["Content-Length"]
    return response


@app.route("/api/go2rtc/stream/<sn>")
def go2rtc_stream(sn: str) -> Response:
    return proxy_stream(sn)


@app.route("/api/decrypted-stream/<config_id>/<sn>")
def decrypted_stream(config_id: str, sn: str) -> Response:
    remux_proc: Optional[subprocess.Popen[Any]] = None
    try:
        config_id_int = int(config_id)
        force_refresh = request.args.get("refresh") == "1"
        # 前端失败后重试会带 refresh=1，此时必须替换同摄像机/配置的旧进程，
        # 否则旧 Node/ffmpeg 可能继续消耗 CPU 和内存。
        replace_group = force_refresh or request.args.get("replace") == "1"
        payload = get_decrypt_payload(sn, force_refresh=force_refresh)
        fps = (request.args.get("fps") or "12").strip()
        output_format = (request.args.get("format") or "mpegts").strip().lower()
        if output_format not in SUPPORTED_OUTPUT_FORMATS:
            raise ConfigError("format 仅支持 mpegts 或 mp4")
        decrypt_options = service.get_decrypt_stream_options()
        shared_pipeline = service.share_decrypt_session_between_playback_and_recording()
        if shared_pipeline:
            cmd = build_shared_decrypt_command(
                config_id=config_id_int,
                payload=payload,
                decrypt_options=decrypt_options,
                fps=fps,
            )
            session_key = build_decrypt_source_key(config_id_int, sn, fps, payload, shared=True)
        else:
            cmd = build_decrypt_command(
                config_id=config_id_int,
                payload=payload,
                decrypt_options=decrypt_options,
                fps=fps,
                output_format=output_format,
            )
            session_key = json.dumps(
                {
                    "source": build_decrypt_source_key(config_id_int, sn, fps, payload, shared=False),
                    "output_format": output_format,
                },
                sort_keys=True,
                ensure_ascii=True,
            )
        group_key = build_decrypt_group_key(config_id_int, sn)
        label = f"decrypt-source[{config_id_int}/{sn}]" if shared_pipeline else f"decrypted-stream[{config_id_int}/{sn}]"
        session = decrypt_session_manager.get_or_create(
            key=session_key,
            group_key=group_key,
            label=label,
            idle_timeout_seconds=decrypt_options["decrypt_idle_timeout_seconds"],
            cmd=cmd,
            replace_group=replace_group,
        )
        subscriber_id, subscriber_queue = session.subscribe(kind="playback")
        if shared_pipeline and output_format == "mp4":
            try:
                remux_proc = subprocess.Popen(
                    build_playback_remux_command(),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=os.fspath(ROOT_DIR),
                    start_new_session=True,
                )
            except Exception:
                session.unsubscribe(subscriber_id)
                raise
    except ConfigError as exc:
        return jsonify({"error": str(exc)}), 400
    except ValueError:
        return jsonify({"error": "config_id 必须是整数"}), 400
    except OSError as exc:
        return jsonify({"error": f"启动解密或封装进程失败: {exc}"}), 500
    except RuntimeError as exc:
        return jsonify({"error": f"解密流会话不可用: {exc}"}), 503

    if remux_proc is not None:
        def feed_remux_input() -> None:
            try:
                assert remux_proc is not None and remux_proc.stdin is not None
                while True:
                    chunk = subscriber_queue.get()
                    if chunk is None:
                        break
                    remux_proc.stdin.write(chunk)
                    remux_proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                app.logger.warning("playback-remux[%s/%s] input failed: %s", config_id_int, sn, exc)
            finally:
                if remux_proc is not None and remux_proc.stdin is not None:
                    try:
                        remux_proc.stdin.close()
                    except OSError:
                        pass

        def log_remux_stderr() -> None:
            assert remux_proc is not None
            if remux_proc.stderr is None:
                return
            try:
                for raw_line in iter(remux_proc.stderr.readline, b""):
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if line:
                        app.logger.warning("playback-remux[%s/%s]: %s", config_id_int, sn, line)
            finally:
                remux_proc.stderr.close()

        Thread(target=feed_remux_input, daemon=True).start()
        Thread(target=log_remux_stderr, daemon=True).start()

    def generate():
        try:
            if remux_proc is not None:
                assert remux_proc.stdout is not None
                while chunk := remux_proc.stdout.read(64 * 1024):
                    yield chunk
            else:
                while True:
                    chunk = subscriber_queue.get()
                    if chunk is None:
                        break
                    yield chunk
        finally:
            session.unsubscribe(subscriber_id)
            if remux_proc is not None:
                if remux_proc.stdout is not None:
                    remux_proc.stdout.close()
                terminate_process_tree(
                    remux_proc,
                    f"playback-remux[{config_id_int}/{sn}]",
                    logger=lambda message, args: app.logger.warning(message, *args),
                )
            return_code = session.return_code()
            if return_code not in (0, None):
                app.logger.warning("decrypted-stream[%s/%s] exited with code %s", config_id_int, sn, return_code)

    content_type = "video/mp4" if output_format == "mp4" else "video/mp2t"
    return Response(stream_with_context(generate()), content_type=content_type)


@app.route("/api/recordings/start", methods=["POST"])
def start_recording() -> Response:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "请求体必须是 JSON 对象"}), 400
    sn = str(body.get("sn") or "").strip()
    if not sn:
        return jsonify({"error": "缺少 sn 参数"}), 400
    raw_config_id = body.get("config_id", 0)
    raw_segment_seconds = body.get("segment_seconds", 1800)
    raw_recording_dir = body.get("recording_dir")
    if isinstance(raw_config_id, bool) or isinstance(raw_segment_seconds, bool):
        return jsonify({"error": "config_id 和 segment_seconds 必须是整数"}), 400
    if raw_recording_dir is not None and not isinstance(raw_recording_dir, str):
        return jsonify({"error": "recording_dir 必须是路径字符串"}), 400
    try:
        config_id = int(raw_config_id)
        segment_seconds = int(raw_segment_seconds)
    except (TypeError, ValueError):
        return jsonify({"error": "config_id 和 segment_seconds 必须是整数"}), 400
    if config_id not in SUPPORTED_CONFIG_IDS:
        return jsonify({"error": "config_id 仅支持 0-3"}), 400
    if segment_seconds < 10 or segment_seconds > 86400:
        return jsonify({"error": "segment_seconds 必须在 10-86400 之间"}), 400

    try:
        service.find_camera(sn)
        if recording_manager.has_active(sn):
            raise RecordingConflict(f"摄像机 {sn} 已在录制")
        payload = get_decrypt_payload(sn)
        decrypt_options = service.get_decrypt_stream_options()
        shared_pipeline = service.share_decrypt_session_between_playback_and_recording()
        start_options: Dict[str, Any] = {}
        if shared_pipeline:
            fps = "12"
            source_session = decrypt_session_manager.get_or_create(
                key=build_decrypt_source_key(config_id, sn, fps, payload, shared=True),
                group_key=build_decrypt_group_key(config_id, sn),
                label=f"decrypt-source[{config_id}/{sn}]",
                idle_timeout_seconds=decrypt_options["decrypt_idle_timeout_seconds"],
                cmd=build_shared_decrypt_command(
                    config_id=config_id,
                    payload=payload,
                    decrypt_options=decrypt_options,
                    fps=fps,
                ),
            )

            def cmd_factory(output_pattern: Path) -> list[str]:
                return build_recording_remux_command(
                    output_path=output_pattern,
                    segment_seconds=segment_seconds,
                    segment_strftime=True,
                )

            start_options = {
                "pipeline_mode": "shared",
                "source_session": source_session,
                "max_pending_input_bytes": decrypt_options["recording_max_pending_input_bytes"],
            }
        else:
            def cmd_factory(output_pattern: Path) -> list[str]:
                return build_decrypt_command(
                    config_id=config_id,
                    payload=payload,
                    decrypt_options=decrypt_options,
                    fps="12",
                    output_format="mp4",
                    output_path=output_pattern,
                    segment_seconds=segment_seconds,
                    segment_strftime=True,
                    control_stdin=True,
                )

        status = recording_manager.start(
            sn=sn,
            config_id=config_id,
            segment_seconds=segment_seconds,
            output_root=service.get_recording_dir(raw_recording_dir),
            cmd_factory=cmd_factory,
            **start_options,
        )
    except RecordingConflict as exc:
        return jsonify({"error": str(exc), "recording": recording_manager.status(sn)}), 409
    except ConfigError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": f"共享解密流会话不可用: {exc}"}), 503
    except (OSError, ValueError) as exc:
        return jsonify({"error": f"录像保存路径或进程不可用: {exc}"}), 500
    return jsonify({"ok": True, "recording": status}), 201


@app.route("/api/recordings/settings")
def recording_settings() -> Response:
    try:
        recording_dir = service.get_recording_dir()
    except (ConfigError, OSError, ValueError) as exc:
        return jsonify({"error": f"读取录像设置失败: {exc}"}), 500
    return jsonify(
        {
            "recording_dir": str(recording_dir),
            "default_segment_seconds": 1800,
            "directory_layout": "YYYY-MM-DD/manual-YYYY-MM-DD_HH-MM-SS-<id>.mp4",
        }
    )


@app.route("/api/recordings/status")
def recording_status() -> Response:
    sn = (request.args.get("sn") or "").strip()
    if not sn:
        return jsonify({"error": "缺少 sn 参数"}), 400
    return jsonify({"recording": recording_manager.status(sn)})


@app.route("/api/recordings/<sn>/stop", methods=["POST"])
def stop_recording(sn: str) -> Response:
    stopped, status = recording_manager.stop(sn)
    return jsonify({"ok": True, "stopped": stopped, "recording": status})


@app.route("/api/decrypted-stream/<config_id>/<sn>/stop", methods=["POST"])
def stop_decrypted_stream(config_id: str, sn: str) -> Response:
    """前端显式停止后端解密进程，避免只停播放器但后端继续跑。"""
    try:
        config_id_int = int(config_id)
    except ValueError:
        return jsonify({"error": "config_id 必须是整数"}), 400

    result = decrypt_session_manager.close_playback_group(
        build_decrypt_group_key(config_id_int, sn),
        reason="stopped by frontend",
    )
    return jsonify({"ok": True, **result})


@app.route("/")
def index() -> Response:
    return send_from_directory(WEB_DIR, "index.html")


@app.route("/<path:filename>")
def static_files(filename: str) -> Response:
    file_path = WEB_DIR / filename
    if file_path.is_file():
        return send_from_directory(WEB_DIR, filename)
    return jsonify({"error": f"文件不存在: {filename}"}), 404


def main() -> None:
    app.run(
        host=os.environ.get("CAMERA_BACKEND_HOST", "0.0.0.0"),
        port=int(os.environ.get("CAMERA_BACKEND_PORT", "5000")),
        debug=os.environ.get("CAMERA_BACKEND_DEBUG", "0") == "1",
        threaded=True,
    )


if __name__ == "__main__":
    main()
