# Pipeline — 视频 → 3D Gaussian Splatting

> 一台机器上的端到端 Web 应用：上传视频 → 自动跑 `ffmpeg` 抽帧 → `COLMAP` 稀疏重建 → `OpenSplat` 三维重建 → 内嵌 SuperSplat 查看器探索成果。

## 这是什么

把一段手机视频拖进浏览器，看着三根进度条推进，再在同一个会话里旋转/裁剪/导出最终的高斯泼溅模型——不用切出 localhost。

![overview](docs/screenshot.png)

## 特性

- **单进程零依赖运行**（MVP）：FastAPI + 内存队列 + 单 worker 线程，不依赖 Redis/Celery
- **三阶段流水线**：ffmpeg → COLMAP → OpenSplat，每段都有实时进度（SSE）
- **可中途取消**：SIGTERM 子进程进程组，5 秒后 SIGKILL
- **阶段断点续传**：每阶段先检查输出目录已存在则跳过
- **自托管 SuperSplat 查看器**：服务端动态注入 `contentUrl`，按作业绑定 `.ply`/`.sog`
- **单文件导出**：下载一个 10MB 自包含 HTML（含 viewer + SOG 模型），离线打开
- **作业地理位置标记**：上传时可填 lat/lon，存储到 `JobStatus.geo`
- **RESTful API + SSE**：前端用原生 ES module（无 React 构建步骤）

## 架构

```
[浏览器 SPA]
   │  POST /api/jobs           (multipart: 视频 + lat/lon)
   │  GET  /api/jobs/{id}/events  (Server-Sent Events)
   │  GET  /viewer/{id}        (改写 viewer/index.html 的 bootstrap JSON)
   ▼
[FastAPI :8888]
   │  入队到内存 Queue ──► [Worker 线程，concurrency=1]
   │                          │
   │                          ├─ ffmpeg        (frames/%04d.jpg)
   │                          ├─ COLMAP        (workspace/sparse/0/)
   │                          └─ OpenSplat     (splat.ply → splat.sog)
   │
   │  静态资源: viewer/        (SuperSplat: index.html, index.js, index.css, settings.json)
   │  静态资源: viewer/export.html  (单文件离线导出模板)
   │  静态资源: pipeline-app/static/  (SPA 外壳)
   ▼
磁盘存储 (按 job_id 组织):
   ~/.claude/splat-jobs/{id}/
     ├── input.mp4
     ├── frames/ workspace/ splat.ply splat.sog settings.json
     └── logs/{ffmpeg,colmap,opensplat}.log
```

## 快速开始

### 系统要求

| 工具 | 必需 | 说明 |
|---|---|---|
| Python | ≥ 3.9 | 推荐 3.11+ |
| ffmpeg + ffprobe | ✓ | `brew install ffmpeg` |
| COLMAP | ✓ | `brew install colmap`；CUDA 可选 |
| OpenSplat | ✓ | arm64 macOS 二进制；Linux 需自行编译并通过 `OPEN_SPLAT_BIN` 指定 |
| SplatTransform | 可选 | `npm i -g @playcanvas/splat-transform`（用于 `.ply ↔ .sog` 转换） |

### 安装

```bash
git clone <repo-url> pipeline
cd pipeline/pipeline-app
uv sync               # 或 pip install -e ".[dev]"
```

### 启动

```bash
cd pipeline-app
./bin/run.sh
# → http://127.0.0.1:8888
```

打开浏览器，把 `.mp4` 拖进上传区即可。

## 使用

1. **上传**：把视频拖到首页 `#view-upload` 区域；可选填 FPS / 缩放倍数 / OpenSplat 迭代次数 / 地理位置
2. **监控**：作业列表页（`?tab=list`）会显示实时进度条和当前阶段消息
3. **查看**：作业完成后点 "查看 3D" 在 iframe 里探索模型；点 "新标签打开" 全屏模式
4. **导出 HTML**：点 "导出 HTML" 下载一个 10MB 自包含文件（viewer + 模型），离线可打开
5. **取消**：在任意阶段点 "取消"；SSE 在 6s 内收到 `stage: cancelled`

## 目录结构

```
.
├── README.md                  # 本文件
├── PLAN.md                    # 完整方案（pipeline-app/PLAN.md）
├── pipeline-app/              # 主应用源码
│   ├── bin/run.sh             # uvicorn 启动
│   ├── pipeline_app/
│   │   ├── api.py             # FastAPI 路由 + 静态挂载
│   │   ├── worker.py          # 队列 + worker 线程 + 三阶段
│   │   ├── runner.py          # 子进程封装（取消/超时）
│   │   ├── progress.py        # ffmpeg/COLMAP/OpenSplat 进度解析
│   │   ├── storage.py         # 作业路径 + 状态持久化
│   │   ├── events.py          # 跨线程 SSE 事件总线
│   │   ├── schemas.py         # Pydantic 模型
│   │   ├── config.py          # 路径解析 + 二进制探测
│   │   └── cubemap.py         # ERP 视频 → 6 面 cubemap
│   ├── static/                # SPA 外壳（vanilla ES module）
│   ├── tests/                 # pytest 冒烟测试
│   ├── PLAN.md                # 设计文档
│   └── README.md              # pipeline-app 专属说明
└── viewer/                    # 自托管 SuperSplat 构建产物
    ├── index.html             # viewer 模板
    ├── index.js               # viewer 主体（3MB）
    ├── index.css
    ├── settings.json          # viewer 默认设置
    └── export.html            # 单文件离线导出模板（10MB）
```

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8888` | 监听端口 |
| `JOBS_DIR` | `~/.claude/splat-jobs` | 运行时作业存储目录 |
| `VIEWER_DIR` | `<repo>/viewer` | SuperSplat 构建产物目录 |
| `VIEWER_SINGLE_FILE` | `<repo>/viewer/export.html` | 单文件导出模板 |
| `FFMPEG_BIN` / `FFPROBE_BIN` | PATH 探测 | ffmpeg 二进制 |
| `COLMAP_BIN` | PATH 探测 | COLMAP 二进制 |
| `OPEN_SPLAT_BIN` | `/Users/mlens/work/claude/splat/opensplat` | OpenSplat 二进制 |
| `SPLAT_TRANSFORM_BIN` | PATH 探测 | `.ply ↔ .sog` 转换器 |
| `SHOWCASE_DIR` | *(已弃用)* | 原独立 showcase 页面，已移除 |
| `DEFAULT_FPS` | `3` | 上传时默认抽帧率 |
| `DEFAULT_SCALE` | `518:378` | 上传时默认输出分辨率 |
| `STAGE_TIMEOUT_*` | 各阶段 | 各阶段超时（秒） |

## 测试

```bash
cd pipeline-app
.venv/bin/python -m pytest tests/ -v
```

13 个冒烟测试，0.15s 跑完，覆盖：
- `/viewer-static/*` 静态挂载存在（防 mount 缺失）
- `/static/*` SPA 挂载
- `/viewer/{id}` route 没被 mount shadow（双向验证）
- `viewer/` 目录关键文件存在
- `viewer/export.html` 模板存在且未截断

## 端到端性能参考

| 场景 | 耗时 |
|---|---|
| 5 秒视频 @ 2fps（10 帧）+ OpenSplat 500 iter | ~5 秒 |
| 60 秒视频 @ 2fps（120 帧）+ COLMAP | 30 秒 – 5 分钟 |
| OpenSplat 30000 iter（生产质量） | 20 分钟 – 2 小时（取决于 GPU） |

## 路线图

- [ ] Celery + Redis worker（替代内存队列，多机扩展）
- [ ] SplatTransform 集成（前端更快加载）
- [ ] 多作业并发（多 GPU 场景）
- [ ] 持久化进度历史（SQLite 替代 status.json）
- [ ] 完整 CI（lint + test + docker image）

## 致谢

- [SuperSplat](https://github.com/playcanvas/supersplat) by PlayCanvas — 自托管的 3DGS 编辑器
- [OpenSplat](https://github.com/pierotofy/OpenSplat) — 3DGS 训练
- [COLMAP](https://colmap.github.io/) — SfM / MVS
- [PlayCanvas Engine](https://github.com/playcanvas/engine) — 渲染引擎

## License

待定
