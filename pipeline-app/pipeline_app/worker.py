"""In-process job queue and worker thread.

MVP design: a single worker thread consumes jobs from queue.Queue.
Concurrency=1 (per the plan: GPU/CPU bound; one job at a time).

Replace with Celery + Redis later by swapping `enqueue()` to
`celery_app.send_task()` and `run_pipeline()` to a Celery task body.
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import (
    COLMAP_BIN,
    CUBEMAP_FACE_SIZE,
    DEFAULT_FPS,
    DEFAULT_OPENSPLAT_ITERS,
    FFMPEG_BIN,
    FFPROBE_BIN,
    JOBS_DIR,
    OPEN_SPLAT_BIN,
    STAGE_TIMEOUTS,
)
from .cubemap import convert_frame, is_erp_video
from .events import bus
from .progress import ColmapParser, FFmpegParser, OpenSplatParser, ParsedProgress
from .runner import CancelledError, StageRunner
from .schemas import Stage as StageEnum
from .storage import JobPaths, read_ply_iteration_count, read_status, write_status

DEFAULT_SCALE_FACTOR = 1.0

# --- Cancellation registry (per-job threading.Event) -------------------------

_cancel_events: dict[str, threading.Event] = {}
_cancel_lock = threading.Lock()

def register_cancel(job_id: str) -> threading.Event:
    ev = threading.Event()
    with _cancel_lock:
        _cancel_events[job_id] = ev
    return ev


def get_cancel(job_id: str) -> threading.Event:
    with _cancel_lock:
        ev = _cancel_events.get(job_id)
        if ev is None:
            ev = threading.Event()
            _cancel_events[job_id] = ev
        return ev


def clear_cancel(job_id: str) -> None:
    with _cancel_lock:
        _cancel_events.pop(job_id, None)


def request_cancel(job_id: str) -> bool:
    ev = get_cancel(job_id)
    ev.set()
    return True


# --- Event loop reference (for cross-thread bus publish) --------------------

_loop_ref: dict[str, asyncio.AbstractEventLoop] = {}

def set_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    _loop_ref["loop"] = loop


def _publish(job_id: str, event: dict) -> None:
    loop = _loop_ref.get("loop")
    if loop is None:
        return  # No API connected; drop
    bus.publish_threadsafe(job_id, event, loop)


# --- Job queue --------------------------------------------------------------

_job_queue: "queue.Queue[str]" = queue.Queue()
_worker_thread: Optional[threading.Thread] = None
# Ordered list of queued job ids (parallel to _job_queue for position display)
_pending_order: list[str] = []
_active_job: Optional[str] = None  # id of the job the worker is currently processing
_queue_lock = threading.Lock()


def _queue_position(job_id: str) -> int:
    """Return 1-based position of job in queue, or 0 if active/not queued."""
    with _queue_lock:
        if _active_job == job_id:
            return 0  # currently being processed
        if job_id in _pending_order:
            return _pending_order.index(job_id) + 1
        return 0


def enqueue(job_id: str) -> int:
    """Enqueue a job. Starts the worker thread on first call.

    Returns the 1-based queue position (0 means "running now").
    """
    global _worker_thread
    with _queue_lock:
        # Already running? Return position 0.
        if _active_job == job_id:
            return 0
        # Already in queue? Return existing position.
        if job_id in _pending_order:
            return _pending_order.index(job_id) + 1
        _pending_order.append(job_id)
        pos = len(_pending_order)

    register_cancel(job_id)
    _job_queue.put(job_id)

    if _worker_thread is None or not _worker_thread.is_alive():
        _worker_thread = threading.Thread(target=_worker_main, daemon=True, name="pipeline-worker")
        _worker_thread.start()
    return pos


def _worker_main() -> None:
    while True:
        job_id = _job_queue.get()
        with _queue_lock:
            _pending_order.remove(job_id) if job_id in _pending_order else None
            global _active_job
            _active_job = job_id
        try:
            run_pipeline(job_id)
        except Exception as e:
            # Last-ditch safety net; run_pipeline already handles stage errors
            try:
                paths = JobPaths.for_job(job_id)
                write_status(paths, stage=StageEnum.FAILED.value, percent=0.0,
                             message=f"worker crash: {e}", finished_at=_now_iso())
                _publish(job_id, {"type": "stage", "stage": StageEnum.FAILED.value,
                                  "percent": 0.0, "message": str(e), "ts": _now_iso()})
            except Exception:
                pass
        finally:
            with _queue_lock:
                _active_job = None
            clear_cancel(job_id)
            _job_queue.task_done()


# --- Pipeline execution -----------------------------------------------------

def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _emit_progress(job_id: str, stage: StageEnum, parsed: ParsedProgress) -> None:
    event = {
        "type": "progress",
        "stage": stage.value,
        "percent": round(parsed.percent, 1),
        "message": parsed.message,
        "ts": _now_iso(),
    }
    _publish(job_id, event)
    # Also persist to disk
    paths = JobPaths.for_job(job_id)
    write_status(paths, stage=stage.value, percent=event["percent"],
                 message=parsed.message)


def _emit_stage(job_id: str, stage: StageEnum, message: str = "") -> None:
    event = {
        "type": "stage",
        "stage": stage.value,
        "percent": 0.0 if stage not in (StageEnum.DONE,) else 100.0,
        "message": message,
        "ts": _now_iso(),
    }
    _publish(job_id, event)
    paths = JobPaths.for_job(job_id)
    write_status(paths, stage=stage.value, percent=event["percent"],
                 message=message,
                 finished_at=_now_iso() if stage in (StageEnum.DONE, StageEnum.FAILED, StageEnum.CANCELLED) else None)


def _ffprobe_duration(video_path: Path) -> float:
    """Get video duration via ffprobe. Returns 0.0 on failure."""
    try:
        out = subprocess.check_output(
            [FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            stderr=subprocess.DEVNULL,
        )
        return float(out.strip() or "0")
    except Exception:
        return 0.0


def _ffprobe_resolution(video_path: Path) -> tuple[int, int]:
    """Get video width/height via ffprobe. Returns (0, 0) on failure."""
    try:
        out = subprocess.check_output(
            [FFPROBE_BIN, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=s=x:p=0", str(video_path)],
            stderr=subprocess.DEVNULL,
        )
        w, h = out.strip().split(b"x")
        return int(w), int(h)
    except Exception:
        return 0, 0


def _stage_ffmpeg(
    job_id: str,
    paths: JobPaths,
    fps: int,
    scale_factor: float,
    is_erp: bool = False,
) -> None:
    """Extract frames with ffmpeg. Skips if frames/ already populated.

    For ERP videos we always extract at native resolution (scale_factor is
    ignored) so the cubemap stage has the best source data to sample from.
    """
    if paths.frames_dir.exists() and any(paths.frames_dir.iterdir()):
        _emit_progress(job_id, StageEnum.FFMPEG, ParsedProgress(100.0, "already extracted"))
        return

    paths.frames_dir.mkdir(exist_ok=True)
    duration = _ffprobe_duration(paths.input_video)
    src_w, src_h = _ffprobe_resolution(paths.input_video)

    # Compute output dimensions from scale_factor (preserve source aspect ratio).
    # Snap to even numbers (most encoders prefer multiples of 2).
    out_w = out_h = 0
    vf = f"fps={fps}"
    if not is_erp and scale_factor != 1.0 and src_w > 0 and src_h > 0:
        out_w = max(2, round(src_w * scale_factor / 2) * 2)
        out_h = max(2, round(src_h * scale_factor / 2) * 2)
        vf += f",scale={out_w}:{out_h}"

    # Persist the resolved output dims so the UI can show them.
    write_status(
        paths,
        source_width=src_w,
        source_height=src_h,
        output_width=out_w or src_w,
        output_height=out_h or src_h,
    )

    parser = FFmpegParser(duration_seconds=duration, fps=fps)

    cmd = [
        FFMPEG_BIN, "-y", "-i", str(paths.input_video),
        "-vf", vf,
        "-q:v", "2", str(paths.frames_dir / "%04d.jpg"),
    ]

    _emit_stage(job_id, StageEnum.FFMPEG, "starting ffmpeg")

    def on_progress(p: ParsedProgress) -> None:
        _emit_progress(job_id, StageEnum.FFMPEG, p)

    runner = StageRunner(
        cmd=cmd,
        log_path=paths.logs_dir / "ffmpeg.log",
        parser=parser,
        on_progress=on_progress,
        cancel=lambda: get_cancel(job_id).is_set(),
        timeout_seconds=STAGE_TIMEOUTS["ffmpeg"],
    )

    try:
        ret = runner.run()
    except CancelledError:
        raise
    if ret != 0:
        raise RuntimeError(f"ffmpeg exited with code {ret}")


def _ensure_colmap_images_symlink(paths: JobPaths, is_erp: bool) -> None:
    """For ERP jobs, COLMAP stores image filenames relative to
    `workspace/images/`, and OpenSplat loads them from the same directory.
    We point that directory at our cubemap dir.
    """
    if not is_erp:
        return
    images_link = paths.workspace_dir / "images"
    target = paths.cubemap_dir.resolve()
    if images_link.is_symlink() or images_link.exists():
        if images_link.is_symlink():
            images_link.unlink()
        else:
            shutil.rmtree(images_link)
    os.symlink(target, images_link)


def _stage_colmap(
    job_id: str,
    paths: JobPaths,
    is_erp: bool = False,
    face_size: int = 0,
) -> None:
    """Run COLMAP on the input images (cubemap faces if ERP, else raw frames).

    Skips if sparse/0/cameras.txt already exists (idempotent resume).

    For ERP videos, use explicit feature_extractor + exhaustive_matcher +
    mapper commands with a fixed SIMPLE_PINHOLE camera matching the cubemap
    face geometry (90° FOV, principal point at the center).
    """
    sparse_dir = paths.workspace_dir / "sparse" / "0"
    cameras_txt = sparse_dir / "cameras.txt"
    cameras_bin = sparse_dir / "cameras.bin"
    if cameras_txt.exists() or cameras_bin.exists():
        # Make sure we have a text-format version (OpenSplat and our parser
        # both consume .txt). Idempotent.
        if not cameras_txt.exists() and cameras_bin.exists():
            _convert_colmap_to_txt(sparse_dir)
        _emit_progress(job_id, StageEnum.COLMAP, ParsedProgress(100.0, "already reconstructed"))
        return

    paths.workspace_dir.mkdir(exist_ok=True)
    image_path = paths.cubemap_dir if is_erp else paths.frames_dir

    # Hint total image count to the parser so progress goes 0→100%
    expected = sum(1 for _ in image_path.glob("*.jpg"))
    parser = ColmapParser(expected_images=expected)

    if is_erp and face_size > 0:
        # 90° FOV pinhole: focal = face/2, principal at center
        focal = face_size // 2
        cx = cy = face_size // 2
        camera_params = f"{focal},{cx},{cy}"

        # COLMAP stores image filenames as relative paths to
        # `workspace/images/` in images.txt. OpenSplat later loads images
        # from that exact directory, so we symlink it to our cubemap dir
        # (which lives at job_root/cubemap, one level up).
        _ensure_colmap_images_symlink(paths, is_erp=True)

        db = paths.workspace_dir / "database.db"
        colmap_log = paths.logs_dir / "colmap.log"
        colmap_log_fh = open(colmap_log, "wb")
        # COLMAP mapper refuses to run if its output directory doesn't exist
        # yet. Create `workspace/sparse/` so it can populate `sparse/0/`.
        (paths.workspace_dir / "sparse").mkdir(parents=True, exist_ok=True)
        _emit_stage(job_id, StageEnum.COLMAP, "starting colmap (erp pipeline)")

        def _run_colmap(args: list[str]) -> None:
            """Run a COLMAP subcommand, stream stdout+stderr to colmap.log."""
            colmap_log_fh.write(f"\n$ {' '.join(args)}\n".encode())
            colmap_log_fh.flush()
            proc = subprocess.Popen(  # noqa: S603
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
            )
            assert proc.stdout is not None
            for raw in proc.stdout:
                colmap_log_fh.write(raw)
                colmap_log_fh.flush()
            ret = proc.wait()
            if ret != 0:
                raise subprocess.CalledProcessError(ret, args)

        try:
            # Step 1: feature extraction with fixed intrinsics
            _emit_progress(job_id, StageEnum.COLMAP, ParsedProgress(5.0, f"feature_extractor on {expected} cubemap faces"))
            _run_colmap([
                COLMAP_BIN, "feature_extractor",
                "--database_path", str(db),
                "--image_path", str(image_path),
                "--ImageReader.camera_model", "SIMPLE_PINHOLE",
                "--ImageReader.camera_params", camera_params,
                "--FeatureExtraction.use_gpu", "0",
                "--FeatureExtraction.num_threads", "4",
            ])
            # Step 2: sequential matching. With cubemap faces (4 cameras
            # co-located per frame), exhaustive matching across all pairs
            # is wasteful and the initial-pair search gets confused by
            # zero-parallax same-frame face pairs. Sequential matching
            # only matches adjacent frames, where the camera actually
            # moved → guaranteed parallax → good initial pairs.
            _emit_progress(job_id, StageEnum.COLMAP, ParsedProgress(40.0, f"sequential_matcher on {expected} images"))
            _run_colmap([
                COLMAP_BIN, "sequential_matcher",
                "--database_path", str(db),
                "--SequentialMatching.overlap", "10",
                "--SequentialMatching.quadratic_overlap", "1",
                "--FeatureMatching.use_gpu", "0",
                "--FeatureMatching.num_threads", "4",
            ])
            # Step 3: mapper
            _emit_progress(job_id, StageEnum.COLMAP, ParsedProgress(70.0, "mapper (SfM)"))
            _run_colmap([
                COLMAP_BIN, "mapper",
                "--database_path", str(db),
                "--image_path", str(image_path),
                "--output_path", str(paths.workspace_dir / "sparse"),
            ])
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"colmap (erp pipeline) failed: {e}") from e
        finally:
            colmap_log_fh.close()

        _emit_progress(job_id, StageEnum.COLMAP, ParsedProgress(100.0, "colmap erp pipeline complete"))

        if not cameras_txt.exists() and not cameras_bin.exists():
            raise RuntimeError("colmap finished but sparse/0/ has no cameras — likely no images registered")
        if not cameras_txt.exists():
            _convert_colmap_to_txt(sparse_dir)
        return

    # Non-ERP path: use the simple automatic_reconstructor wrapper.
    # COLMAP needs workspace/images/ — symlink the extracted frames in.
    images_link = paths.workspace_dir / "images"
    if images_link.is_symlink() or images_link.exists():
        if images_link.is_symlink():
            images_link.unlink()
        elif images_link.is_dir():
            shutil.rmtree(images_link)
        else:
            images_link.unlink()
    os.symlink(paths.frames_dir.resolve(), images_link)

    cmd = [
        COLMAP_BIN, "automatic_reconstructor",
        "--workspace_path", str(paths.workspace_dir),
        "--image_path", str(images_link),
    ]

    _emit_stage(job_id, StageEnum.COLMAP, "starting colmap")

    def on_progress(p: ParsedProgress) -> None:
        _emit_progress(job_id, StageEnum.COLMAP, p)

    runner = StageRunner(
        cmd=cmd,
        log_path=paths.logs_dir / "colmap.log",
        parser=parser,
        on_progress=on_progress,
        cancel=lambda: get_cancel(job_id).is_set(),
        timeout_seconds=STAGE_TIMEOUTS["colmap"],
    )

    try:
        ret = runner.run()
    except CancelledError:
        raise

    if ret != 0:
        raise RuntimeError(f"colmap exited with code {ret}")
    if not cameras_txt.exists() and not cameras_bin.exists():
        raise RuntimeError("colmap finished but sparse/0/ has no cameras — likely no images registered")

    if not cameras_txt.exists():
        _convert_colmap_to_txt(sparse_dir)


def _stage_cubemap(
    job_id: str,
    paths: JobPaths,
    face_size: int,
) -> None:
    """Convert each ERP frame in frames/ into 6 cubemap faces in cubemap/.

    Idempotent: if cubemap/ already has files, skip.
    """
    if paths.cubemap_dir.exists() and any(paths.cubemap_dir.iterdir()):
        _emit_progress(job_id, StageEnum.CUBEMAP, ParsedProgress(100.0, "already cubemapped"))
        return

    erp_frames = sorted(paths.frames_dir.glob("*.jpg"))
    if not erp_frames:
        raise RuntimeError("no ERP frames to convert — ffmpeg stage produced nothing")

    paths.cubemap_dir.mkdir(parents=True, exist_ok=True)
    _emit_stage(job_id, StageEnum.CUBEMAP, f"converting {len(erp_frames)} ERP frames → cubemap")

    for i, erp_path in enumerate(erp_frames, 1):
        if get_cancel(job_id).is_set():
            raise CancelledError("cancelled during cubemap conversion")
        try:
            # 4-face cubemap (drop +Y/-Y poles) keeps BA well-conditioned
            # by avoiding 6 co-located cameras per frame.
            convert_frame(erp_path, paths.cubemap_dir, face_size, horizontal_only=True)
        except Exception as e:
            raise RuntimeError(f"cubemap conversion failed on {erp_path.name}: {e}") from e
        if i % 5 == 0 or i == len(erp_frames):
            pct = 100.0 * i / len(erp_frames)
            _emit_progress(
                job_id, StageEnum.CUBEMAP,
                ParsedProgress(pct, f"cubemap {i}/{len(erp_frames)} (4-face, {face_size}px)"),
            )


def _convert_colmap_to_txt(sparse_dir: Path) -> None:
    """Run `colmap model_converter` to produce .txt siblings of any .bin files."""
    subprocess.run(  # noqa: S603 — controlled invocation
        [
                COLMAP_BIN, "model_converter",
                "--input_path", str(sparse_dir),
                "--output_path", str(sparse_dir),
                "--output_type", "TXT",
            ],
            check=False, capture_output=True,
        )


def _stage_opensplat(
    job_id: str,
    paths: JobPaths,
    iters: int,
    resume_from: Optional[Path] = None,
    prior_trained: int = 0,
) -> None:
    """Run OpenSplat on the COLMAP workspace. Optionally resume from a .ply.

    On success the produced .ply is moved to job root for the viewer.
    """
    if paths.splat_ply.exists() and resume_from is None:
        # Already finalized in a prior run and not asked to resume → skip.
        _emit_progress(job_id, StageEnum.OPENSPLAT, ParsedProgress(100.0, "already reconstructed"))
        return

    if OPEN_SPLAT_BIN is None:
        raise RuntimeError(
            "OpenSplat binary not found. Set $OPEN_SPLAT_BIN or install at "
            "/Users/mlens/work/claude/splat/opensplat"
        )

    # For ERP jobs, ensure the workspace/images → cubemap symlink is in
    # place so OpenSplat can find the cubemap faces.
    _ensure_colmap_images_symlink(paths, is_erp=bool(read_status(paths).get("is_erp")))

    # If a workspace/splat.ply exists but no job-root one, treat it as our
    # source-of-truth for resume (worker was killed before finalizing).
    if resume_from is None:
        ws_ply = paths.workspace_dir / "splat.ply"
        if ws_ply.exists():
            resume_from = ws_ply

    parser = OpenSplatParser(total_iters=iters, prior_trained=prior_trained)

    cmd = [
        OPEN_SPLAT_BIN,
        str(paths.workspace_dir),
        "-n", str(iters),
        "-s", "-1",                             # disable intermediate snapshots; only final .ply
        "-o", str(paths.workspace_dir / "splat.ply"),
    ]
    if resume_from is not None and resume_from.exists():
        cmd += ["--resume", str(resume_from.resolve())]
        _emit_stage(job_id, StageEnum.OPENSPLAT, f"resuming opensplat from {resume_from.name}")
    else:
        _emit_stage(job_id, StageEnum.OPENSPLAT, "starting opensplat")

    def on_progress(p: ParsedProgress) -> None:
        _emit_progress(job_id, StageEnum.OPENSPLAT, p)

    runner = StageRunner(
        cmd=cmd,
        log_path=paths.logs_dir / "opensplat.log",
        parser=parser,
        on_progress=on_progress,
        cancel=lambda: get_cancel(job_id).is_set(),
        timeout_seconds=STAGE_TIMEOUTS["opensplat"],
    )

    try:
        ret = runner.run()
    except CancelledError:
        raise

    if ret != 0:
        raise RuntimeError(f"opensplat exited with code {ret}")

    produced = paths.workspace_dir / "splat.ply"
    if not produced.exists():
        raise RuntimeError("opensplat finished but splat.ply not produced")

    try:
        produced.replace(paths.splat_ply)
    except Exception:
        shutil.copyfile(produced, paths.splat_ply)


def _write_settings_from_colmap(paths: JobPaths) -> None:
    """Generate per-job settings.json from COLMAP cameras + first registered image.

    Mirrors the schema of /Users/mlens/work/claude/web_v2s/viewer/settings.json.
    """
    cameras_txt = paths.workspace_dir / "sparse" / "0" / "cameras.txt"
    images_txt = paths.workspace_dir / "sparse" / "0" / "images.txt"
    if not cameras_txt.exists() or not images_txt.exists():
        return  # best-effort; skip if COLMAP didn't produce these

    # Parse cameras.txt: lines "CAMERA_ID MODEL PARAMS..."
    # Pick first SIMPLE_PINHOLE or PINHOLE camera for intrinsics
    cam_intrinsics = None
    for line in cameras_txt.read_text().splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        model = parts[1]
        if model in ("SIMPLE_PINHOLE", "PINHOLE"):
            try:
                width, height = int(parts[2]), int(parts[3])
                fx = float(parts[4])
                fy = float(parts[5]) if model == "PINHOLE" else fx
                cx = float(parts[5]) if model == "SIMPLE_PINHOLE" else float(parts[6])
                cy = float(parts[6]) if model == "SIMPLE_PINHOLE" else float(parts[7])
                cam_intrinsics = (width, height, fx, fy, cx, cy)
                break
            except (ValueError, IndexError):
                continue

    # Parse images.txt: first IMAGE_ID, QW QX QY QZ TX TY TZ, NAME
    # Then an empty line, then POINTS2D. We only need the first image's pose.
    camera_pose = None
    lines_raw = images_txt.read_text().splitlines()
    for i, line in enumerate(lines_raw):
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split()
        if len(parts) >= 10:
            try:
                qw, qx, qy, qz = map(float, parts[1:5])
                tx, ty, tz = map(float, parts[5:8])
                # COLMAP stores world-to-camera; flip to camera-to-world for SuperSplat
                # For simplicity in MVP, use a neutral initial pose
                camera_pose = (qw, qx, qy, qz, tx, ty, tz)
                break
            except (ValueError, IndexError):
                continue

    settings = {
        "version": 2,
        "tonemapping": "none",
        "highPrecisionRendering": False,
        "background": {"color": [0, 0, 0]},
        "postEffectSettings": {
            "sharpness": {"enabled": False, "amount": 0},
            "bloom": {"enabled": False, "intensity": 0.1, "blurLevel": 2},
            "grading": {
                "enabled": False, "brightness": 1, "contrast": 1,
                "saturation": 1, "tint": [1, 1, 1],
            },
            "vignette": {"enabled": False, "intensity": 0.5, "inner": 0.3, "outer": 0.75, "curvature": 1},
            "fringing": {"enabled": False, "intensity": 0.5},
        },
        "animTracks": [],
        "cameras": [
            {
                "initial": {
                    # Reasonable defaults — viewer lets user orbit freely regardless
                    "position": [-1.0, 0.5, -2.0],
                    "target": [0, 0, 0],
                    "fov": 60,
                }
            }
        ],
        "annotations": [],
        "startMode": "default",
    }
    if cam_intrinsics:
        w, h, fx, fy, cx, cy = cam_intrinsics
        # fov (deg) from focal length in pixels (assuming horizontal fov)
        if w > 0:
            fov = 2 * (180 / 3.141592653589793) * (0.5 * (w / fx)) ** -1 * 1  # placeholder, computed below
            # Actually: fov_h = 2 * atan(w / (2*fx)) in radians → degrees
            import math
            fov = 2 * math.degrees(math.atan(w / (2.0 * fx)))
            settings["cameras"][0]["initial"]["fov"] = round(fov, 2)

    # Preserve user-authored camera track on re-train / re-colmap: if
    # settings.json already exists (e.g. the user has saved an animTracks
    # entry), merge our derived intrinsics + initial pose into the existing
    # document instead of overwriting it.
    if paths.settings_json.is_file():
        existing = json.loads(paths.settings_json.read_text())
        # Keep user's animTracks, startMode, annotations
        settings["animTracks"]  = existing.get("animTracks",  [])
        settings["startMode"]   = existing.get("startMode",   "animTrack" if settings["animTracks"] else "default")
        settings["annotations"] = existing.get("annotations", [])

    paths.settings_json.write_text(json.dumps(settings, indent=2))


def run_pipeline(job_id: str) -> None:
    """Run all stages sequentially: ffmpeg → colmap → opensplat.

    Supports resume: if status.json has `resume_from`, stage 3 picks up
    from that .ply and runs `extra_iters` (or `opensplat_iters`) more
    iterations, leaving stages 1+2 untouched.
    """
    paths = JobPaths.for_job(job_id)
    status = read_status(paths)
    fps = int(status.get("fps", DEFAULT_FPS))
    scale_factor = float(status.get("scale_factor", DEFAULT_SCALE_FACTOR))
    opensplat_iters = int(status.get("opensplat_iters", DEFAULT_OPENSPLAT_ITERS))
    extra_iters = int(status.get("extra_iters", 0)) or None
    resume_from = Path(status["resume_from"]) if status.get("resume_from") else None
    is_erp = bool(status.get("is_erp", False))
    # Cumulative training counter — survives resumes & stage3 re-runs.
    trained_so_far = int(status.get("trained_iters", 0))

    # If resuming, skip ffmpeg/colmap and only re-run opensplat.
    is_resume = resume_from is not None

    # Decide the delt the user picked.
    # Default: opensplat_iters (a fresh upload's full target).
    # extra_iters > 0: user picked this from the +续训 button — it's how many
    # MORE iterations they want, NOT the absolute total.
    delta_iters = extra_iters if (extra_iters and extra_iters > 0) else opensplat_iters

    # OpenSplat's --resume + -n N semantics: N is the ABSOLUTE target iteration
    # count (the saved .ply carries the previous iteration in its header; the
    # loop runs while step <= numIters, where step starts at saved_iter + 1).
    # So if the user already trained 1000 iters and picks "续训 9000", we must
    # pass -n (1000 + 9000) = 19000 so the resume loop actually runs 9000 steps.
    if is_resume:
        prior = trained_so_far or opensplat_iters
        target_iters = prior + delta_iters
    else:
        target_iters = delta_iters

    try:
        if not is_resume:
            _stage_ffmpeg(job_id, paths, fps, scale_factor, is_erp=is_erp)
            if is_erp:
                _stage_cubemap(job_id, paths, CUBEMAP_FACE_SIZE)
            _stage_colmap(job_id, paths, is_erp=is_erp,
                          face_size=CUBEMAP_FACE_SIZE if is_erp else 0)
            # _write_settings_from_colmap preserves user-authored animTracks on
            # re-runs, but we still skip it entirely on re-train to avoid
            # touching settings.json unnecessarily.
            if not paths.settings_json.is_file():
                _write_settings_from_colmap(paths)

        _stage_opensplat(
            job_id, paths, target_iters, resume_from=resume_from, prior_trained=trained_so_far,
        )

        # Update trained_iters to reflect cumulative training. The .ply's
        # "comment Generated by opensplat at iteration N" is written from
        # `numIters` (the -n argument), NOT the actual training step count —
        # a smoke-test resume with -n 5 will produce a .ply labeled
        # "at iteration 5" while actually running 0 iterations. So the .ply
        # header is misleading; we track the true cumulative total in
        # status.json instead.
        new_total = target_iters
        if new_total > 0:
            write_status(paths, trained_iters=new_total, opensplat_iters=new_total)

        # Auto-convert splat.ply → splat.sog (≈88% smaller, preferred for
        # browser streaming). Failure here doesn't fail the job — the .ply
        # is already usable; the UI just falls back to it.
        _try_convert_to_sog(paths)

        _emit_stage(job_id, StageEnum.DONE, "all stages complete")
    except CancelledError:
        _emit_stage(job_id, StageEnum.CANCELLED, "cancelled")
    except Exception as e:
        _emit_stage(job_id, StageEnum.FAILED, str(e))


_STEP_RE = re.compile(r"Step\s+(\d+)\s*:")


def _last_step_count(paths: JobPaths) -> Optional[int]:
    """Parse the OpenSplat log and return the last `Step N:` N seen, or None."""
    log = paths.logs_dir / "opensplat.log"
    if not log.exists():
        return None
    last = None
    for line in log.read_text(errors="replace").splitlines():
        m = _STEP_RE.search(line)
        if m:
            last = int(m.group(1))
    return last


def _try_convert_to_sog(paths: JobPaths) -> None:
    """Convert splat.ply → splat.sog in the background of job completion.

    Uses @playcanvas/splat-transform (Node.js CLI). Errors are non-fatal
    because splat.ply is still viewable as a fallback.

    Skips if the .sog is already up-to-date with the .ply (mtime check).
    On +续训, the .ply mtime advances past the old .sog, so we re-run.
    """
    from .config import SPLAT_TRANSFORM_BIN
    if SPLAT_TRANSFORM_BIN is None:
        return
    if not paths.splat_ply.is_file():
        return
    if paths.splat_sog.exists() and paths.splat_sog.stat().st_mtime >= paths.splat_ply.stat().st_mtime:
        return  # SOG already up-to-date
    # Remove stale SOG so the viewer doesn't load a stale one mid-rebuild.
    paths.splat_sog.unlink(missing_ok=True)
    try:
        result = subprocess.run(  # noqa: S603
            [SPLAT_TRANSFORM_BIN, str(paths.splat_ply), str(paths.splat_sog)],
            capture_output=True, text=True, timeout=900,
        )
        if result.returncode != 0:
            log_path = paths.logs_dir / "sog_convert.log"
            log_path.write_text(
                f"splat-transform exit {result.returncode}\n"
                f"stdout: {result.stdout}\n"
                f"stderr: {result.stderr}\n"
            )
    except Exception as e:
        log_path = paths.logs_dir / "sog_convert.log"
        log_path.write_text(f"splat-transform exception: {e}\n")


def _reconcile_orphan_jobs() -> int:
    """On startup, mark any job whose stage is non-terminal and has no live
    subprocess as `cancelled`. Returns the count reconciled.

    Without this, jobs that were active when the server died (or got SIGTERM'd
    directly without going through the API) leave phantom "still running" rows
    in the UI forever.

    Important: we only mark a job as cancelled if its stage subprocess is
    actually dead. If ffmpeg/colmap/opensplat is still running on the OS (e.g.
    a long bundle-adjustment), we leave the status alone — the worker will
    re-attach to it once the queue dispatches the job.
    """
    import subprocess as _sp

    def _any_subprocess_alive(job_id: str) -> bool:
        """True if ffmpeg/colmap/opensplat for this job is still running."""
        try:
            out = _sp.check_output(
                ["pgrep", "-f", f"splat-jobs/{job_id}/"],
                stderr=_sp.DEVNULL,
            ).decode().strip()
            return bool(out)
        except _sp.CalledProcessError:
            return False

    reconciled = 0
    for status_path in JOBS_DIR.glob("*/status.json"):
        try:
            data = json.loads(status_path.read_text())
        except Exception:
            continue
        stage = data.get("stage", "pending")
        # Pending jobs occupy no resources (no subprocess spawned yet) and the
        # user's intent is preserved — leave them alone so they can be
        # re-enqueued. Only reconcile jobs that had actually started a stage.
        if stage not in ("ffmpeg", "cubemap", "colmap", "opensplat"):
            continue
        job_id = status_path.parent.name
        # Don't reconcile if a stage subprocess is still running on the OS.
        if _any_subprocess_alive(job_id):
            continue
        write_status(
            JobPaths.for_job(job_id),
            stage=StageEnum.CANCELLED.value,
            percent=0.0,
            message="cancelled (server restart, no live process)",
            finished_at=_now_iso(),
        )
        reconciled += 1
    return reconciled


# Reconcile jobs whose subprocess died unexpectedly (e.g., killed via pkill
# or OOM). Called by the API when listing, so the UI doesn't show ghost rows.
def reconcile_dead_subprocesses() -> int:
    """For each non-terminal job, if there's no live worker-owned subprocess,
    mark it failed. Returns the number reconciled.

    Detection: look at the stage log mtime. If it hasn't grown in `cutoff`
    seconds AND the job is not currently being processed by the worker, mark
    failed. This catches the case where the subprocess was killed externally
    (SIGTERM -15) and the worker is unaware.

    Important — COLMAP's bundle-adjustment and OpenSplat's densification
    phases can be silent for many minutes, so the cutoff is generous (10 min).
    We also skip the active job — if the worker is processing it, we trust it.
    """
    import time as _t
    CUTOFF_SECONDS = 600  # 10 minutes
    reconciled = 0
    now = _t.time()
    cutoff = now - CUTOFF_SECONDS

    with _queue_lock:
        active = _active_job

    for status_path in JOBS_DIR.glob("*/status.json"):
        try:
            data = json.loads(status_path.read_text())
        except Exception:
            continue
        stage = data.get("stage", "pending")
        if stage not in ("ffmpeg", "cubemap", "colmap", "opensplat"):
            continue
        job_id = status_path.parent.name
        # Don't reconcile the active job — the worker is the source of truth
        if job_id == active:
            continue
        log_name = {
            "ffmpeg":   "ffmpeg.log",
            "cubemap":  "cubemap.log",
            "colmap":   "colmap.log",
            "opensplat":"opensplat.log",
        }[stage]
        log_path = status_path.parent / "logs" / log_name
        if not log_path.exists():
            continue
        mtime = log_path.stat().st_mtime
        if mtime < cutoff:
            write_status(
                JobPaths.for_job(job_id),
                stage=StageEnum.FAILED.value,
                percent=data.get("percent", 0.0),
                message=f"subprocess died (log inactive since {int(now - mtime)}s)",
                finished_at=_now_iso(),
            )
            reconciled += 1
    return reconciled


def kill_orphaned_subprocesses() -> int:
    """Kill any ffmpeg/colmap/opensplat processes that are running but NOT
    associated with the currently-active job. Used at uvicorn shutdown so
    orphans from a previous server don't keep running and burn GPU/CPU.

    A process is "associated" if its command line references the active
    job's workspace path. Anything else is considered orphaned.
    """
    import subprocess as _sp

    with _queue_lock:
        active = _active_job

    if active:
        active_path = f"splat-jobs/{active}/"
    else:
        active_path = None

    killed = 0
    for tool in ("ffmpeg", "colmap", "opensplat"):
        try:
            # pgrep -lf gives "PID command". We grep for our tool AND
            # splat-jobs/ but exclude the active job's path.
            out = _sp.check_output(["pgrep", "-f", f"{tool}.*splat-jobs/"]).decode().strip()
        except _sp.CalledProcessError:
            continue
        for line in out.splitlines():
            pid = line.strip()
            if not pid:
                continue
            try:
                cmdline = open(f"/proc/{pid}/cmdline").read().replace(b"\x00", b" ").decode(errors="replace") if Path("/proc").exists() else ""
            except Exception:
                cmdline = ""
            # On macOS /proc doesn't exist; fall back to `ps -p`.
            if not cmdline:
                try:
                    cmdline = _sp.check_output(["ps", "-p", pid, "-o", "command="]).decode().strip()
                except Exception:
                    cmdline = ""
            # Keep if it references the active job; otherwise it's an orphan.
            if active_path and active_path in cmdline:
                continue
            try:
                _sp.run(["kill", "-TERM", pid], check=False)
                killed += 1
            except Exception:
                pass
    return killed


def list_jobs() -> list[dict]:
    """List all known jobs by scanning JOBS_DIR for status.json files.

    Newest first (by `updated_at` from status.json). Falls back to filesystem
    mtime if the field is missing (e.g. very old jobs).
    """
    def _sort_key(p: Path) -> float:
        try:
            return json.loads(p.read_text()).get("updated_at", p.stat().st_mtime)
        except Exception:
            return p.stat().st_mtime

    out = []
    for status_path in sorted(
        JOBS_DIR.glob("*/status.json"),
        key=_sort_key,
        reverse=True,
    ):
        try:
            data = json.loads(status_path.read_text())
            job_id = status_path.parent.name
            data["id"] = job_id
            data.setdefault("stage", "pending")
            data.setdefault("percent", 0.0)
            # Cheap stats for the UI: are intermediate / final artifacts ready?
            data["splat_exists"] = (status_path.parent / "splat.ply").exists()
            data["sog_exists"] = (status_path.parent / "splat.sog").exists()
            # Does the user have a saved camera track (animTracks non-empty)?
            settings_p = status_path.parent / "settings.json"
            data["track_exists"] = False
            if settings_p.is_file():
                try:
                    _s = json.loads(settings_p.read_text())
                    data["track_exists"] = bool(_s.get("animTracks"))
                except Exception:
                    pass
            data["has_sparse"] = (
                (status_path.parent / "workspace" / "sparse" / "0" / "cameras.bin").exists()
                or (status_path.parent / "workspace" / "sparse" / "0" / "cameras.txt").exists()
            )
            # geo eligibility: a job carries has_geo if it has a lat/lon pin.
            data["has_geo"] = bool(data.get("geo")) and \
                isinstance(data["geo"], dict) and \
                data["geo"].get("lat") is not None and \
                data["geo"].get("lon") is not None
            # Always show the .ply's actual iteration count, not the stored
            # trained_iters (which can drift from retroactive scripts).
            ply_iter = read_ply_iteration_count(JobPaths.for_job(job_id))
            if ply_iter is not None:
                data["ply_iter_count"] = ply_iter
            # Queue position
            data["queue_position"] = _queue_position(job_id)
            out.append(data)
        except Exception:
            continue
    return out


def get_job(job_id: str) -> Optional[dict]:
    paths = JobPaths.for_job(job_id)
    if not paths.root.exists():
        return None
    data = read_status(paths)
    data["id"] = job_id
    data.setdefault("stage", "pending")
    data.setdefault("percent", 0.0)
    return data