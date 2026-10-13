# Pipeline Web App

视频 → 3D Gaussian Splatting 端到端 Web 流水线（视频上传 → ffmpeg 抽帧 → COLMAP 稀疏重建 → OpenSplat 三维重建 → SuperSplat 查看器）。

## 当前状态：MVP 三层均已实现**

| 阶段 | 状态 |
|---|---|
| FastAPI 应用 + 静态挂载（`/`, `/static`, `/viewer-static`） | ✅ |
| 内存任务队列 + 单 worker 线程（concurrency=1） | ✅ |
| 视频 multipart 上传 → 保存到 `~/.claude/splat-jobs/{id}/input.mp4` | ✅ |
| ffmpeg 抽帧 + 实时进度解析 | ✅ |
| COLMAP `automatic_reconstructor` + 实时进度解析（含 `model_converter`） | ✅ |
| OpenSplat 三维重建 + 实时进度解析（Metal GPU on macOS） | ✅ |
| 取消作业（SIGTERM 子进程进程组） | ✅ |
| 阶段断点续传（每阶段检查输出是否已存在） | ✅ |
| Server-Sent Events 进度流 | ✅ |
| 每作业 `settings.json` 派生自 COLMAP `cameras.txt` | ✅ |
| 自托管 SuperSplat 查看器集成（`/viewer/{id}`） | ✅ |
| 单页前端（SSE 驱动进度条 + 抽帧缩略图 + 查看器 iframe） | ✅ |

## 快速开始

```bash
cd /Users/mlens/work/claude/web_v2s/pipeline-app
./bin/run.sh
# → http://127.0.0.1:8888
```

依赖（已安装）：`fastapi`、`uvicorn[standard]`、`python-multipart`、`pydantic`。

系统要求：
- `ffmpeg` / `ffprobe`（Homebrew）
- `colmap`（Homebrew；CUDA 可选，本机 CPU-only 已能工作）
- `opensplat`（默认 `/Users/mlens/work/claude/splat/opensplat` arm64 macOS，用 Metal GPU；可通过 `OPEN_SPLAT_BIN` 覆盖）

## 端到端验证

用 5 秒真实视频片段跑完整流水线：

```
[   0.01s] stage    ffmpeg      100.0%  frame 10/10
[   0.48s] stage    colmap        0.0%  starting colmap
[   1.67s] progress colmap        0.7%  registered 2/10
[   1.68s] progress colmap       99.0%  bundle adjustment
[   3.16s] stage    opensplat     0.0%  starting opensplat
[   5.18s] stage    done        100.0%  all stages complete
```

总耗时 5 秒（10 帧小数据 + Metal GPU）。真实场景：
- 60 秒视频 @ 2fps（120 帧）+ COLMAP：30 秒-5 分钟
- OpenSplat 30000 次迭代：20 分钟-2 小时（取决于 GPU）

## 存储布局

```
~/.claude/splat-jobs/
└── {job_id}/
    ├── input.mp4              # 原始上传
    ├── frames/%04d.jpg        # ffmpeg 输出
    ├── workspace/             # COLMAP 工作目录
    │   ├── images → ../frames/  (软链)
    │   ├── database.db
    │   └── sparse/0/
    │       ├── cameras.{bin,txt}
    │       ├── images.{bin,txt}
    │       ├── points3D.{bin,txt}
    │       └── frames.{bin,txt}
    ├── splat.ply              # OpenSplat 最终输出
    ├── settings.json          # 每作业查看器设置
    ├── logs/
    │   ├── ffmpeg.log
    │   ├── colmap.log
    │   └── opensplat.log
    └── status.json
```

## 架构要点

- **不依赖 Redis/Celery**：MVP 用 `queue.Queue` + 单 worker 线程跑在同一进程。换 Celery 时只需替换 `worker.enqueue()` 为 `celery_app.send_task()`。
- **取消通过 `os.killpg`**：子进程以 `start_new_session=True` 启动，整个进程组一并 kill。
- **参数合并**：`status.json` 写入用合并语义，fps/scale_factor/opensplat_iters 不会被进度事件覆盖。
- **COLMAP 输出 bin/txt 双格式**：mapper 写 .bin，通过 `colmap model_converter` 转 .txt 供 OpenSplat 使用。
- **OpenSplat 输出位置**：从 `workspace/splat.ply` 移到 `{job_id}/splat.ply`，对前端稳定。
- **SuperSplat 查看器**：服务端动态改写 `index.html` 的 `<base href>` 与 `#sse-bootstrap` JSON，注入每个作业的 `contentUrl` 和 `settingsUrl`，前端通过 `/viewer/{id}` iframe 加载。

## 文件清单

```
pipeline-app/
├── PLAN.md                                # 完整方案
├── README.md                              # 本文件
├── pyproject.toml
├── bin/run.sh                             # uvicorn 启动
├── pipeline_app/
│   ├── __init__.py
│   ├── config.py                          # 路径解析 + 二进制探测
│   ├── schemas.py                         # Pydantic 模型
│   ├── storage.py                         # 作业路径 + 状态持久化
│   ├── events.py                          # 跨线程 SSE 事件总线
│   ├── progress.py                        # ffmpeg/COLMAP/OpenSplat 进度解析
│   ├── runner.py                          # 子进程封装（含取消、超时）
│   ├── worker.py                          # 队列 + worker 线程 + 三阶段流水线
│   └── api.py                             # FastAPI 应用（含查看器路由）
├── static/
│   ├── index.html
│   ├── app.js                             # SSE + UI
│   └── app.css
└── tests/
    └── (待写)
```

## 下一步（可选增强）

1. **Celery + Redis**：用专用 worker 进程，便于多机扩展
2. **断点续传 UI**：作业详情页加"重跑 OpenSplat"按钮（仅 stage 3）
3. **SplatTransform**：把 .ply 转 .splat/.ksplat（更小，浏览器侧加载更快）
4. **多作业并发**：当前 worker 串行，单 GPU 合理；多 GPU 可开多个 worker
5. **持久化进度记录**：当前 status.json 保留最近一次，更细粒度历史可入 SQLite
6. **测试**：单元测试 + 端到端冒烟