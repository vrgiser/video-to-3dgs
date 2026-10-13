# ffmpeg → COLMAP → OpenSplat → SuperSplat 流水线 Web 应用

## 1. 背景

目前要把一段手机视频转成 3D Gaussian Splatting 模型，必须手动拷贝文件、依次执行 ffmpeg → COLMAP → OpenSplat，再把生成的 `.ply` 拖到在线 SuperSplat 编辑器里。所需的本地二进制和参考文档已经存在于 `/Users/mlens/work/claude/` 下（见 `3d/3d-reconstruction-tech-report.md`、`3d/supersplat-guide.md`、`splat/opensplat`、`splat/readme.md`），但还没有一个端到端的应用能把它们串起来，加上队列、进度、取消和自托管的查看器。

目标：一台机器上的 Web 应用，可以：
- 接收视频上传
- 串行跑三阶段（ffmpeg → COLMAP → OpenSplat），每段都有实时进度条
- 给重型作业排队（单 worker，不并发抢占 GPU/CPU）
- 复用已有的自托管 SuperSplat（位于 `/Users/mlens/work/claude/web_v2s/viewer/`），按每个作业的 `.ply` 输出绑定展示
- 允许中途安全取消（revoke 任务 + kill 子进程进程组）

期望产出：开发者把一个 `.mp4` 拖进浏览器，看着三根进度条推进，再在同一个会话里探索结果模型，不必切出 localhost。

## 2. 架构概览（数据流）

```
[浏览器 SPA]
   │  POST /api/jobs  (multipart: 视频)
   │  GET  /api/jobs/{id}/events  (Server-Sent Events)
   │  GET  /viewer/{id}           (改写 viewer/index.html 的 contentUrl 指向 /{id}/splat.ply)
   ▼
[FastAPI 应用 :8000]
   │  入队 Celery 任务 ──────────► [Redis :6379 broker/result] ─────────► [Celery worker : GPU 机]
   │                                                                              │
   │  静态资源: /Users/mlens/work/claude/web_v2s/viewer/*  (SuperSplat: index.html, js, css, settings.json, sample.sog)
   │  静态资源: /Users/mlens/.claude/splat-jobs/{id}/*     (每作业: frames/, workspace/, splat.ply, settings.json)
   ▼                                                                              │
磁盘存储 (按 job_id 组织):                                                        │
   /Users/mlens/.claude/splat-jobs/{id}/                                          │
     input.mp4                                                                    │
     frames/%04d.jpg                              ◄──── ffmpeg -vf "fps=2-5,scale=518:378"
     workspace/{images, database.db, sparse/}    ◄──── colmap automatic_reconstructor
     splat.ply                                    ◄──── /Users/mlens/work/claude/splat/opensplat
     settings.json (每作业相机参数覆盖)                                              │
     logs/{ffmpeg,colmap,opensplat}.log           ◄──── 各子进程 stderr (worker tail → SSE)
```

取消流程：`POST /api/jobs/{id}/cancel` → Celery `revoke(terminate=True)` → 对子进程进程组 SIGTERM/SIGKILL（`start_new_session=True`） → worker 回收僵尸进程，标记作业 FAILED。

## 3. 技术栈与理由

| 层 | 选择 | 理由 |
|---|---|---|
| 后端 Web | **FastAPI** | 异步、原生 SSE、静态挂载方便、类型提示 |
| Worker / 队列 | **Celery + Redis** | 标准模式，`revoke(terminate=True)` 取消，`--prefetch-multiplier 1` + `--concurrency 1` 强制单 GPU 槽位 |
| 进度推送 | **Server-Sent Events** | 单向、任意代理透传、比 WebSocket 简单；每个作业一条连接 |
| 子进程 | `asyncio.create_subprocess_exec` 配 `start_new_session=True`；Celery worker 线程另起 reader 任务 tail stderr | 通过 `os.killpg` 安全取消，与 Celery 自身的信号处理解耦 |
| 前端 | **原生 HTML + ES-module JS + 纯 CSS** | 单页、无 React 构建步骤。三个视图：上传、作业列表、查看器。SSE 用 `EventSource`。 |
| 查看器 | 自托管 **SuperSplat**（`/Users/mlens/work/claude/web_v2s/viewer/`） | 已构建好（`index.html` 从 bootstrap JSON 注入 `contentUrl`）。挂为静态，按作业重写 `#sse-bootstrap` 脚本标签 |
| ffmpeg | 系统二进制 `/opt/homebrew/bin/ffmpeg`（macOS）或 `/usr/bin/ffmpeg`（Linux） | 启动时探测，缺失即 fail-fast |
| COLMAP | 系统二进制 `/usr/local/bin/colmap`（或 `$COLMAP_BIN` 环境变量覆盖） | 需 CUDA，单机开发 |
| OpenSplat | **arm64 macOS 二进制 `/Users/mlens/work/claude/splat/opensplat`** | 本地已编译；将来用 `$OPEN_SPLAT_BIN` 切到 Linux worker |
| 存储 | 普通文件系统 `/Users/mlens/.claude/splat-jobs/{job_id}/` | 满足"按 job_id 组织"约束；为查看器加载的 `.ply` 走软链快路径 |

为什么不选 React/TS：用户说"挑更简单路径"；总共就三个小屏；SSE + fetch + DOM 足够。

## 4. 目录结构

```
/Users/mlens/work/claude/web_v2s/pipeline-app/             # 新源码树
├── pyproject.toml                                       # uv/pip; fastapi, celery[redis], redis, python-multipart, uvicorn[standard]
├── README.md
├── .env.example                                         # REDIS_URL, FFMPEG_BIN, COLMAP_BIN, OPEN_SPLAT_BIN, JOBS_DIR, VIEWER_DIR
├── bin/
│   ├── run-api.sh                                       # uvicorn pipeline_app.api:app --port 8000
│   ├── run-worker.sh                                    # celery -A pipeline_app.worker worker -l info --concurrency 1 --prefetch-multiplier 1
│   └── run-redis.sh                                     # docker run -p 6379:6379 redis:7-alpine (或系统 redis-server)
├── pipeline_app/
│   ├── __init__.py
│   ├── config.py                                        # pydantic-settings; 从环境变量解析所有路径
│   ├── api.py                                           # FastAPI: /api/jobs/* + /viewer/{id} 挂载 + 静态挂载
│   ├── worker.py                                        # Celery app; 任务: pipeline_run(job_id), stage_ffmpeg/colmap/opensplat
│   ├── progress.py                                      # ProgressParser: 对 stderr 行做正则 → ProgressUpdate dataclass
│   ├── runner.py                                        # 子进程辅助: run_stage(cmd, log_path, on_update, cancel_event)
│   ├── storage.py                                       # job_paths(job_id): frames/, workspace/, splat.ply, settings.json, logs/
│   ├── events.py                                        # 按 job_id 划分的内存 pub/sub，用于 SSE 扇出（单进程；扩展后切 Redis pubsub）
│   └── schemas.py                                       # JobCreate, JobStatus, ProgressUpdate, Stage 枚举
├── static/
│   ├── index.html                                       # SPA 外壳: 上传表单、作业表、查看器 iframe 槽位
│   ├── app.js                                           # SSE 客户端、进度条渲染、查看器 URL 切换
│   └── app.css                                          # 极简暗色主题; .progress-bar, .job-row, .badge
└── tests/
    ├── test_progress.py                                  # 黄金行 → 各工具期望百分比 (ffmpeg/COLMAP/OpenSplat)
    └── test_api.py                                      # POST 上传、GET 状态、SSE 端到端 (单测用 celery_eager)

# 运行时存储 (首次运行创建)
/Users/mlens/.claude/splat-jobs/
└── {job_id}/                                            # uuid4 十六进制, 32 字符
    ├── input.mp4
    ├── frames/%04d.jpg
    ├── workspace/{images, database.db, sparse/0/...}
    ├── splat.ply
    ├── settings.json                                    # 第 2 阶段后由 COLMAP cameras.txt 派生
    └── logs/{ffmpeg.log, colmap.log, opensplat.log}
```

## 5. 关键文件清单

| 文件 | 作用 |
|---|---|
| `pipeline_app/config.py` | 集中路径解析：`JOBS_DIR`、`VIEWER_DIR`、`FFMPEG_BIN`、`COLMAP_BIN`、`OPEN_SPLAT_BIN`。启动时二进制缺失立即 fail-fast。 |
| `pipeline_app/api.py` | FastAPI app；路由：`POST /api/jobs`、`GET /api/jobs/{id}`、`GET /api/jobs/{id}/events` (SSE)、`POST /api/jobs/{id}/cancel`、`GET /viewer/{id}` (改写 bootstrap 并返回 viewer)、`GET /viewer-assets/{id}/{path}` (每作业的 splat/settings，软链或拷贝)。挂载 `/static` 提供前端，挂载 `/viewer` 提供 SuperSplat 构建产物。 |
| `pipeline_app/worker.py` | Celery `App`；单一任务 `pipeline_run(job_id)` 串联三个阶段并推送进度。`acks_late=True`、`worker_prefetch_multiplier=1`、`concurrency=1`（worker CLI 标志，不写在代码里）。 |
| `pipeline_app/runner.py` | `async run_stage(cmd, log_path, parser, on_update, cancel)`：以 `start_new_session=True` 启动子进程，逐行 tail stderr，调用 `parser.parse(line)` → `on_update(percent, message)`。取消时 `os.killpg(pgid, SIGTERM)`，5 秒后 SIGKILL。 |
| `pipeline_app/progress.py` | 每个工具一个 `ProgressParser`。ffmpeg: `frame= *(\d+)` 与 `Duration` 估算对比。COLMAP: 匹配 `Registering image ... (\d+)/(\d+)` 与 mapper 阶段行。OpenSplat: 匹配 `Iteration (\d+)/(\d+)` 与 `Saving snapshot`（每 `--save-every`）。返回 `(percent, label)` 0-100。 |
| `pipeline_app/storage.py` | `job_paths(job_id)` 返回类型化路径；`init_job(job_id, video_bytes)`；`read_settings(job_id)` 解析 `workspace/sparse/0/cameras.txt` 以填充每作业的 `settings.json`（用第一张注册图像的位姿作为 `cameras[0].initial`）。 |
| `pipeline_app/events.py` | `EventBus`：每作业一个 `asyncio.Queue`（或包裹为 asyncio 的线程安全 `queue.Queue`）。Worker 通过 `bus.publish(job_id, update)` 发布（同步垫片用 `asyncio.run_coroutine_threadsafe` 注入 API 事件循环）。API 的 SSE 处理器按请求订阅。 |
| `pipeline_app/schemas.py` | Pydantic 模型：`JobCreate`（仅上传）、`JobStatus {id, stage, percent, message, created_at, finished_at}`、`Stage(Enum)=ffmpeg|colmap|opensplat|done|failed|cancelled`、`ProgressUpdate`。 |
| `static/index.html` + `app.js` + `app.css` | 单页：上传组件（拖放）、作业表含每阶段进度条和 Cancel 按钮、"打开查看器"按钮（在 iframe 或新标签中打开 `/viewer/{id}`）。每个进行中作业一个 `EventSource('/api/jobs/{id}/events')`。 |
| `bin/run-*.sh` | 便捷启动脚本；`run-worker.sh` 传 `--concurrency 1 --prefetch-multiplier 1 -E` 以保证取消真的能抢占。 |

## 6. 流水线实现细节

### 6.1 阶段 1 — ffmpeg

```python
# runner.py 调用
cmd = [
    FFMPEG_BIN, "-y", "-i", str(input_mp4),
    "-vf", "fps=3,scale=518:378",   # 与 splat/readme.md 一致
    "-q:v", "2", str(frames_dir / "%04d.jpg"),
]
```

- 日志文件：`logs/ffmpeg.log`（ffmpeg 默认把进度写到 stderr）
- 进度正则：`frame=\s*(\d+)`；按 `ffprobe -v error -show_entries format=duration -of csv=p=0 input.mp4` × 3 FPS 估算的预期帧数归一化（截断 0-100）
- 错误处理：返回码非零 → 发布 `{stage: failed, message: "ffmpeg exit 1"}`，并把最后 50 行日志一起带上 SSE 事件
- 取消：SIGTERM → ffmpeg 写出部分输出，退出码 255；runner 在取消场景下把任何非零退出视为良性

### 6.2 阶段 2 — COLMAP

```python
cmd = [
    COLMAP_BIN, "automatic_reconstructor",
    "--workspace_path", str(workspace),
    "--image_path", str(workspace / "images"),
    # 可选性能参数（来自 splat/readme.md）:
    # "--num_threads", str(cpu_count()),
    # SIFT 默认值在开发场景够用
]
```

- COLMAP 写出 `workspace/{database.db, sparse/0,cameras.txt,images.txt,points3D.txt,...}`
- Worker 把 `workspace/images → frames/` 用软链（或拷贝）让 COLMAP 看到稳定目录
- 日志文件：`logs/colmap.log`。COLMAP 输出很啰嗦，关键里程碑：
  - `Extracting features... (\d+)/(\d+)` → 0-30%
  - `Matching features... (\d+)/(\d+)` → 30-60%
  - `Registering images... (\d+)/(\d+)` → 60-90%
  - `Bundle adjustment` / 完成 → 90-100%
- 兜底心跳：若 2 分钟内没有任何逐图进度行，每分钟 `0.5%` 心跳，避免看起来卡死
- 失败 → 退出码非零或运行后 `sparse/0/` 缺失 → 标记失败，附最后 80 行日志

### 6.3 阶段 3 — OpenSplat

```python
cmd = [
    OPEN_SPLAT_BIN,                          # /Users/mlens/work/claude/splat/opensplat
    str(workspace),
    "-n", "500",                             # 采样迭代次数；生产环境提到 30000
    "-s", "50",                              # 每 N 步存一次 → 进度可从输出文件 mtime 推算
    "-o", str(workspace / "splat.ply"),
]
```

- 复用 `/Users/mlens/work/claude/splat/opensplat` 的 macOS 二进制（前文 `file` 输出已确认为 Mach-O arm64）。Linux 端用 `$OPEN_SPLAT_BIN` 环境变量覆盖。
- 进度解析：
  - `Iteration\s+(\d+)/(\d+)` → 百分比直接取 `current/total`
  - `Saving snapshot` → 百分比 += 1/N（慢但可见）
  - 与 COLMAP 类似的兜底心跳
- 成功时：`workspace/splat.ply` 重命名为作业根目录的 `splat.ply`；根据 `workspace/sparse/0/cameras.txt` 写出每作业的 `settings.json`（提取首个相机位姿作为 `cameras[0].initial`）
- 发 `stage: done, percent: 100`；SSE 处理器关闭流

### 6.4 通用错误处理

- 每阶段：stderr → log 文件，通过 `subprocess.PIPE`；runner 用 `await loop.run_in_executor(None, proc.stderr.readline)` 逐行读取，不阻塞事件循环
- 所有异常都被捕获，作业标记 `failed` 并附 message，SSE 流发最后一个 `event: end`
- Worker 捕获 `SoftTimeLimitExceeded`（按阶段设 `time_limit`）：ffmpeg 600 s；COLMAP 3600 s；OpenSplat 7200 s

## 7. 队列与并发设计

- **Worker**：`--concurrency 1 --prefetch-multiplier 1`（单 GPU/CPU 槽位）。多 Celery 进程会破坏约束；文档注明操作员必须只跑一个
- **优先级**：用 Celery `priority_steps=[0,1,2]`；默认用户作业 = 0，手动重跑 = 0；暂时无 UI 控制（将来再加）。优先级通过 `task.apply_async(priority=...)` 接入
- **取消**：
  1. `POST /api/jobs/{id}/cancel` → API 调用 `celery_app.control.revoke(task_id, terminate=True, signal='SIGTERM')`
  2. Runner 在模块级字典里为每个作业注册一个 `asyncio.Event`；取消时 `os.killpg(pgid, SIGTERM)`（子进程以 `start_new_session=True` 启动）
  3. Runner 等 5 秒，然后对组 SIGKILL
  4. Worker 任务返回 `state: CANCELLED`；API 通过 SSE 发 `{stage: cancelled}`
- **Worker 崩溃重排队**：`acks_late=True`；若 worker 死掉，作业回到 PENDING 并会被重新拉取（因阶段在运行前先检查输出目录，所以幂等）
- **阶段断点续传**：每阶段先检查 `if output_path.exists(): skip` —— 这让用户取消 OpenSplat 后通过重排队可以续跑

## 8. 前端页面 / 路由

SPA 路由（用 hash 路由简化，不引路由库）：

| Hash 路由 | 视图 |
|---|---|
| `#/` | 上传表单 + 最近作业列表 |
| `#/jobs/{id}` | 单作业详情：阶段进度条、日志尾部（最近 200 行，轮询）、Cancel / Open Viewer 按钮 |
| `#/viewer/{id}` | 全屏 iframe 指向 `/viewer/{id}`（自身就是 SuperSplat 页） |

API 表面：
- `POST /api/jobs`（multipart `file`）→ `{id, status}`
- `GET /api/jobs` → 列表
- `GET /api/jobs/{id}` → 状态 JSON
- `GET /api/jobs/{id}/events` → SSE（事件：`progress`、`log`、`stage`、`end`）
- `POST /api/jobs/{id}/cancel` → 202
- `GET /api/jobs/{id}/logs/{stage}` → 最近 N 行（轮询兜底）

FastAPI 静态挂载：
- `/static/*` → `pipeline_app/static/`
- `/viewer/*` → `/Users/mlens/work/claude/web_v2s/viewer/`（SuperSplat 构建产物；含 `index.sog` 让默认页仍可用）
- `/jobs/{id}/*` → 每作业目录（frames、splat.ply、settings.json）

## 9. 查看器集成策略

位于 `/Users/mlens/work/claude/web_v2s/viewer/` 的自托管 SuperSplat 已经支持**单一注入点**：靠近 `index.html` 顶部有一个 `<script type="application/json" id="sse-bootstrap">{"contentUrl":"index.sog"}</script>`。注释也明确说："`renderViewerHtml` replaces the contents of this block to supply asset urls and inline settings"。

**策略**（不修改 SuperSplat 源码）：
1. FastAPI 路由 `GET /viewer/{job_id}` 把 `VIEWER_DIR/index.html` 当字符串模板读
2. 服务端在返回前替换两处：
   - `<script type="application/json" id="sse-bootstrap">{...}</script>` 块 → 新 JSON，含 `contentUrl: "/jobs/{job_id}/splat.ply"`、`settingsUrl: "/jobs/{job_id}/settings.json"`、`contentFilename: "splat.ply"`
   - `<base href="">` → `<base href="/viewer/">` 让 `./index.js`、`./index.css` 解析回原 viewer 目录（由 `/viewer` 静态挂载提供）。这是保持现有构建不动的小窍门
3. 每作业的 `settings.json` 在阶段 3 中由 COLMAP 第一个相机派生，模板与现有的 `/Users/mlens/work/claude/web_v2s/viewer/settings.json` 一致（version、tonemapping、postEffectSettings、animTracks、cameras、annotations、startMode）
4. FastAPI 把 `/jobs/{job_id}/splat.ply` 解析为磁盘文件。进行中作业文件还没生成 → API 返回 404，SPA 在 `stage === done` 之前隐藏"打开查看器"按钮

为什么软链而非拷贝：`jobs/{id}/splat.ply → ../../../actual-path` 的软链让上传目录保持只读，并允许 OpenSplat 完成后 `os.replace` 真实 `.ply` 而不破坏软链。开发期先用硬 `shutil.copyfile`（本地盘 200 MB-2 GB 可承受），并文档化软链升级路径。

若将来 SuperSplat 升级并把 `renderViewerHtml` 暴露成 JS 函数（注释暗示会如此），改用客户端注入：直接送未改动的 `/viewer/index.html`，让 `static/app.js` 在 module 脚本导入 `index.js` 之前改写 `#sse-bootstrap` 的文本内容。

## 10. 端到端验证步骤

macOS 本地冒烟测试（arm64 OpenSplat 二进制就在本地）：

1. **静态冒烟**：`bash bin/run-redis.sh && bash bin/run-worker.sh & bash bin/run-api.sh`。`curl http://localhost:8000/` 返回 SPA；`curl http://localhost:8000/viewer/index.html` 返回未改动的 SuperSplat 构建产物
2. **样例数据**：复制 `/Users/mlens/work/claude/splat/examples/images/` → 造一个伪作业（跳过阶段 1+2，把 frames 和预生成的 `workspace/sparse/0/` 放好）；验证仅阶段 3 能产出 `splat.ply` 与 `settings.json`
3. **正常路径**：
   - POST 一个 5-10 秒 `.mp4` 到 `/api/jobs` → 断言返回 `id`
   - 打开 SSE 流 → 期望 3 次阶段切换；每次阶段进度 0→100；总时长受用户简述中范围限制（ffmpeg < 30 s，COLMAP 5-30 min，OpenSplat 20 min-2 h —— 开发期用 `-n 500` 控制在 5 分钟内）
   - 浏览器访问 `/viewer/{id}` → SuperSplat 加载，`.ply` 可达，可轨道操作相机
5. **取消**：
   - 提交一个会让 OpenSplat 跑 ~3 分钟的作业
   - 在 t=30 s 时 POST `/api/jobs/{id}/cancel`；期望 SSE 在 6 s 内发 `stage: cancelled`；`ps` 看无遗留 `opensplat` 或 `colmap` 进程；作业目录保留供排查
6. **失败透出**：
   - 提交一个非视频文件 → ffmpeg 几秒内退出码 2；SSE 显示 `stage: failed` 并附 stderr 尾部
   - 把 `OPEN_SPLAT_BIN` 指到缺失路径 → 阶段 3 缺失二进制检查立即失败；UI 在错误信息里显示该路径
7. **回放**：调用 POST `/api/jobs/{id}/requeue`（一个额外小路由）→ 阶段 1 因 `frames/` 存在而跳过；COLMAP 因 `sparse/0/` 存在而跳过；OpenSplat 重新跑覆盖 `splat.ply`。验证断点续传
8. **负载**：连续提交 3 个作业；断言第二个和第三个在作业列表里处于 `PENDING`，第一个跑完后按 FIFO 顺序排空

### 关键实现文件

- `/Users/mlens/work/claude/web_v2s/pipeline-app/pipeline_app/api.py`
- `/Users/mlens/work/claude/web_v2s/pipeline-app/pipeline_app/worker.py`
- `/Users/mlens/work/claude/web_v2s/pipeline-app/pipeline_app/runner.py`
- `/Users/mlens/work/claude/web_v2s/pipeline-app/pipeline_app/progress.py`
- `/Users/mlens/work/claude/web_v2s/pipeline-app/static/app.js`

### 复用的已有工具/路径（不重复造轮子）

- `/Users/mlens/work/claude/splat/opensplat` — OpenSplat 二进制（arm64 macOS）
- `/Users/mlens/work/claude/splat/examples/` — 用于快速冒烟的样例 frames + sparse/
- `/Users/mlens/work/claude/web_v2s/viewer/` — 自托管 SuperSplat（index.html、index.js、index.css、settings.json、index.sog）
- `/Users/mlens/work/claude/splat/readme.md` — 三命令流水线的规范来源
- `/Users/mlens/work/claude/3d/supersplat-guide.md` — 格式 / 嵌入模式参考
- `/Users/mlens/work/claude/3d/3d-reconstruction-tech-report.md` — 为什么走这条流水线；可作 README 背景材料