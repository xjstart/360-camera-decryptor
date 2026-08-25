# 后端说明

后端现在分成两层：

- `backend/app/api_client.py`
  负责和 360 平台接口通信
- `backend/app/service.py`
  负责配置读取、播放信息缓存、批量同步、Flask API 和流代理

保留的入口文件：

- `backend/server.py`

这样做的目的是让核心逻辑集中，同时不破坏原来的启动方式。

## 主要接口

- `GET /api/cameras`
- `GET /api/play-info?sn=...`
- `GET /api/stream/<sn>`
- `GET /api/go2rtc/stream/<sn>`
- `GET /api/go2rtc/config?sn=...`
- `GET /api/decrypted-stream/<sn>`
- `POST /api/recordings/start`
- `GET /api/recordings/settings`
- `GET /api/recordings/status?sn=...`
- `POST /api/recordings/<sn>/stop`
- `POST /api/play-info/sync`

## MP4 分片录像

开始录像：

```bash
curl -X POST "http://127.0.0.1:5000/api/recordings/start" \
  -H "Content-Type: application/json" \
  -d '{"sn":"3601Q0700624502","config_id":0,"segment_seconds":1800,"recording_dir":"F:\\homemonitor\\360home"}'
```

`segment_seconds` 默认 `1800`（30 分钟），允许范围为 `10-86400`。`recording_dir` 可覆盖服务端默认保存根目录。同一摄像机已有活动录像时返回 HTTP `409`，不同摄像机可以并行录制。

录像会按服务端本地日期创建目录，文件名形如：

```text
<recording_dir>/2026-08-22/manual-2026-08-22_16-46-39-d715ae70.mp4
```

查询默认设置：

```bash
curl "http://127.0.0.1:5000/api/recordings/settings"
```

查询状态：

```bash
curl "http://127.0.0.1:5000/api/recordings/status?sn=3601Q0700624502"
```

停止录像：

```bash
curl -X POST "http://127.0.0.1:5000/api/recordings/3601Q0700624502/stop"
```

停止接口是幂等的。状态可能是 `idle`、`recording`、`stopping`、`stopped` 或 `failed`。录像默认保存在 `backend/data/recordings`，可通过 `server.recording_dir` 修改。

## go2rtc 接入

推荐优先让 `go2rtc` 直接拉取后端解密后的 MPEG-TS：

```yaml
streams:
  living_room_624502:
    - https://your-backend.example.com/api/decrypted-stream/3601Q0700624502#input=mpegts
```

如果你只想走原始流代理，也可以继续使用 FLV：

```yaml
streams:
  living_room:
    - https://your-backend.example.com/api/go2rtc/stream/3601Q0700624502#input=flv
```

如果想让后端直接生成片段，可以请求：

```bash
curl "http://127.0.0.1:5000/api/go2rtc/config?sn=3601Q0700624502&mode=decrypted&format=yaml"
```

说明：

- `/api/go2rtc/stream/<sn>` 是 `/api/stream/<sn>` 的语义化别名，方便在 `go2rtc.yaml` 中引用。
- `/api/decrypted-stream/<sn>` 会启动 Node + wasm 解密器，把加密 FLV 直接解码后再转成 MPEG-TS 输出。
- 当前 `MPEG-TS` 输出已包含视频和音频，样本验证结果为 `H.264 + AAC`。
- `/api/go2rtc/config` 默认返回 JSON，其中包含 `yaml` 字段和每个摄像机对应的 `go2rtc_source`，支持 `mode=raw` 和 `mode=decrypted`。
- 当前服务端解密方案本质上是“把播放器使用的 wasm 解密核心搬到 Node 后端”，还不是纯 Python/Go 重写版算法。

## 常用命令

```bash
cd backend
pip install -r requirements.txt
mkdir -p data
python server.py
```

如果 `data/config.yaml` 不存在，服务启动时会自动用 `backend/config.example.yaml` 复制生成一份。

`data/config.yaml` 只需要在 `cookie` 列表里填写 `Q`、`T`、`jia_web_sid` 三个认证字段。
