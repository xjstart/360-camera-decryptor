# 项目结构

当前项目按职责拆成 4 个区域：

```text
360-camera-decryptor/
├── backend/
│   ├── app/                  # 后端核心代码
│   │   ├── api_client.py     # 360 平台 API 低层请求
│   │   ├── cli.py            # CLI 入口逻辑
│   │   ├── decrypt_commands.py # Node 解密命令构建
│   │   ├── recordings.py       # MP4 分片录像任务管理
│   │   └── service.py        # Flask 服务与批量同步逻辑
│   ├── server.py             # Flask 服务兼容入口
│   ├── config.example.yaml   # 配置模板
│   ├── data/                 # 配置与运行期缓存
│   │   ├── config.yaml       # 本地配置
│   │   ├── play_info_cache/  # 播放信息持久化缓存
│   │   └── recordings/       # 运行期 MP4 录像分片
├── frontend/                 # 前端调试页与播放器资源
├── docs/                     # 文档
│   ├── PROJECT_STRUCTURE.md
│   ├── backend-api.md
│   └── decryption-analysis.md
├── save_web/                 # 抓包/页面留档
└── README.md                 # 总览入口
```

整理原则：

- `backend/app/` 只放可复用的核心逻辑
- `backend/*.py` 只保留必要入口，避免目录膨胀
- `docs/` 统一放分析和使用说明
- `frontend/` 只放前端资源，不掺杂后端说明
