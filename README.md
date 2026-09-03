# 360智能摄像机视频流工具

本项目现在主要包含两部分：

- `backend/`
  负责获取播放信息、批量同步、流代理和本地配置
- `frontend/`
  负责前端调试页、播放器资源和静态资源

![](./preview.jpg)

## 快速开始

本地需安装 `uv`、Node.js 和 FFmpeg。Python 及 Python 包由 `uv` 自动管理：

```powershell
uv sync
uv run python backend/server.py
```

打开 `http://127.0.0.1:5000/` 即可使用前端页面。

首次启动时，如果 `backend/data/config.yaml` 不存在，后端会自动用 `backend/config.example.yaml` 复制生成一份。

`backend/data/config.yaml` 不会被 Git 跟踪。请填写浏览器 Cookie 中的 `Q`、`T`、`jia_web_sid` 三个值，并把摄像机条目的 `sn` 改成真实序列号。

常用命令：

```bash
uv run python backend/server.py
```

验证环境和运行测试：

```powershell
uv run python --version
node --version
ffmpeg -version
uv run python -m unittest discover -s backend/tests
```

如果你在用 Python 拉 Node.js 解密流，并且 `ffmpeg` 转码时出现内存持续上涨，可以先从 `backend/data/config.yaml` 的 `server` 段调小这几个参数：

```yaml
server:
  decrypt_network_chunk_size: 65536
  decrypt_max_pending_input_bytes: 524288
  decrypt_max_pending_video_bytes: 6291456
  decrypt_max_pending_audio_bytes: 524288
  decrypt_ffmpeg_threads: 1
  share_decrypt_session_between_playback_and_recording: true
  recording_max_pending_input_bytes: 16777216
```

前五个值限制 Node 输入缓存、Node 到 `ffmpeg` 的待写入队列及编码线程数。共享开关默认开启：同一摄像机、解密配置、帧率和播放凭证下，播放与录像只执行一次拉流、WASM 解密和 H.264/AAC 编码，再分别无重编码封装为前端 fMP4 和录像 MP4 分片。`recording_max_pending_input_bytes` 限制录像 remux 的 TS 输入缓存；持续跟不上时只会让该录像失败，不阻塞播放器。需要回退旧行为时可将共享开关设为 `false`。

## 接入 go2rtc

后端现在可以直接生成 `go2rtc` 可用的配置片段：

```bash
curl "http://127.0.0.1:5000/api/go2rtc/config?sn=3601Q0700624502&mode=decrypted&format=yaml"
```

返回结果可以直接粘进你的 `go2rtc.yaml`，形态类似：

```yaml
streams:
  one_624502:
    - http://127.0.0.1:5000/api/decrypted-stream/3601Q0700624502#input=mpegts
```

这条解密流现在会输出带音频的 `MPEG-TS`，当前实测样本可识别为 `H.264 + AAC`。

如果你想保留旧模式，也可以改成 `mode=raw`，让 `go2rtc` 继续拉 `/api/go2rtc/stream/<sn>` 对应的原始 FLV。

前端页面里的 `go2rtc 接入` 面板也会优先生成服务端解密版 YAML。

## MP4 分片录像

页面的“录像控制”面板会自动选择配置中的第一台摄像机，并使用同一个按钮开始或停止录像。同一时间只会运行一个该摄像机的录像任务。

- 分片时长以秒为单位，默认 `1800`（30 分钟），允许范围为 `10` 到 `86400`
- 页面可以直接设置录像保存根目录；留空配置时默认使用 `backend/data/recordings`
- 文件按日期保存，例如 `F:\homemonitor\360home\2026-08-22\manual-2026-08-22_16-46-39-d715ae70.mp4`
- 每个分片包含 H.264 视频和 AAC 音频；切片发生在关键帧处，实际时长可能比设置值略长
- 默认与播放器共用解密和编码主干；停止或刷新播放器不会中断录像，公共主干故障则会同时影响两者

可以在 `backend/data/config.yaml` 中修改录像根目录：

```yaml
server:
  recording_dir: ""
  share_decrypt_session_between_playback_and_recording: true
  recording_max_pending_input_bytes: 16777216
```

留空时使用 `backend/data/recordings`，页面填写的保存路径会覆盖这项默认配置并保存在当前浏览器中。Docker Compose 已把默认目录挂载到宿主机相同位置；容器部署时页面应填写容器内可写路径。

## 目录入口

- 后端说明: [docs/backend-api.md](docs/backend-api.md)
- 项目结构: [docs/PROJECT_STRUCTURE.md](docs/PROJECT_STRUCTURE.md)
- 解密分析: [docs/decryption-analysis.md](docs/decryption-analysis.md)

## 核心路径

- 后端核心代码: `backend/app/`
- 后端入口: `backend/server.py`
- 前端调试页: `frontend/index.html`
- 原始留档资料: `save_web/`

## 说明

本项目仅用于学习和研究目的，请遵守相关法律法规。
