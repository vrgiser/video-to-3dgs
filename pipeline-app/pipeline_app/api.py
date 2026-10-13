"""FastAPI application: upload + status + SSE + static mounts."""
from __future__ import annotations

import asyncio
import json
import time
import logging
import os
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import (
    HOST,
    PORT,
    PROJECT_ROOT,
    STATIC_DIR,
    VIEWER_DIR,
)

# Per-process version stamp used to cache-bust settings.json URLs in the viewer.
# Bumped whenever settings.json changes (e.g. via camera-track PUT/DELETE).
_settings_version = str(int(time.time()))
from .events import bus
from .schemas import GeoLocation, JobCreate, JobStatus, Stage
from .storage import JobPaths, init_job_dir, new_job_id, read_status, save_upload, write_status
from .cubemap import is_erp_video
from .worker import (
    _reconcile_orphan_jobs,
    clear_cancel,
    enqueue,
    get_cancel,
    get_job,
    kill_orphaned_subprocesses,
    list_jobs,
    request_cancel,
    set_event_loop,
)

log = logging.getLogger("pipeline")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    set_event_loop(asyncio.get_running_loop())
    # Kill orphaned ffmpeg/colmap/opensplat processes from a previous server
    # before reconciling — otherwise they keep running on the OS while the
    # new server has no record of them, leading to "ghost" jobs.
    killed = kill_orphaned_subprocesses()
    if killed:
        log.warning("Killed %d orphaned subprocess(es) from a previous server", killed)
    reconciled = _reconcile_orphan_jobs()
    if reconciled:
        log.warning("Reconciled %d orphan job(s) on startup", reconciled)
    log.info("Pipeline API ready on http://%s:%s", HOST, PORT)
    yield
    # On shutdown: kill any subprocess the worker is still running so we
    # don't leave orphans for the next server start to clean up.
    killed = kill_orphaned_subprocesses()
    if killed:
        log.warning("Killed %d subprocess(es) on shutdown", killed)


app = FastAPI(title="Pipeline Web App", lifespan=lifespan)

# CORS: allow any localhost:* port during dev (so the SPA can be served on a
# different port than the API). Tighten for production as needed.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=False,
)


# --- API --------------------------------------------------------------------

@app.post("/api/jobs", response_model=JobStatus)
async def create_job(
    file: UploadFile = File(...),
    fps: int = Form(3),
    scale_factor: float = Form(1.0),
    opensplat_iters: int = Form(500),
    lat: Optional[float] = Form(None),
    lon: Optional[float] = Form(None),
) -> JobStatus:
    if fps <= 0 or fps > 30:
        raise HTTPException(400, f"fps must be in (0, 30], got {fps}")
    if scale_factor <= 0 or scale_factor > 4.0:
        raise HTTPException(400, f"scale_factor must be in (0, 4], got {scale_factor}")
    if opensplat_iters <= 0 or opensplat_iters > 100000:
        raise HTTPException(400, f"opensplat_iters must be in (0, 100000]")

    # lat/lon are optional, but if supplied must be in valid range.
    geo = None
    if lat is not None or lon is not None:
        if lat is None or lon is None:
            raise HTTPException(400, "lat and lon must be supplied together")
        if not (-90.0 <= lat <= 90.0):
            raise HTTPException(400, f"lat out of range: {lat}")
        if not (-180.0 <= lon <= 180.0):
            raise HTTPException(400, f"lon out of range: {lon}")
        geo = {"lat": lat, "lon": lon, "source": "manual"}

    job_id = new_job_id()
    paths = init_job_dir(job_id)

    body = await file.read()
    if len(body) == 0:
        raise HTTPException(400, "empty upload")
    save_upload(body, paths.input_video)

    # Detect equirectangular (360°) videos by 2:1 aspect ratio. If so, the
    # worker will run an extra cubemap stage and feed COLMAP 6×N faces
    # instead of raw frames.
    detected_erp = is_erp_video(paths.input_video)

    # EXIF GPS fallback: only if the user didn't supply lat/lon. Does not
    # override manual entries. Failure is silent — many videos lack GPS.
    if geo is None:
        exif_geo = _exif_extract_geo(paths.input_video)
        if exif_geo:
            geo = exif_geo

    status_fields = dict(
        stage=Stage.PENDING.value,
        percent=0.0,
        message="queued",
        fps=fps,
        scale_factor=scale_factor,
        opensplat_iters=opensplat_iters,
        is_erp=detected_erp,
    )
    if geo:
        status_fields["geo"] = geo
    write_status(paths, **status_fields)
    enqueue(job_id)

    return JobStatus(
        id=job_id,
        stage=Stage.PENDING,
        percent=0.0,
        message="queued",
        fps=fps,
        scale_factor=scale_factor,
        opensplat_iters=opensplat_iters,
        is_erp=detected_erp,
        geo=GeoLocation(**geo) if geo else None,
    )


@app.get("/api/jobs")
async def list_all_jobs() -> list[dict]:
    from .worker import reconcile_dead_subprocesses
    reconcile_dead_subprocesses()
    return list_jobs()


@app.get("/api/jobs/{job_id}", response_model=JobStatus)
async def read_job(job_id: str) -> JobStatus:
    data = get_job(job_id)
    if data is None:
        raise HTTPException(404, "job not found")
    paths = JobPaths.for_job(job_id)
    splat_exists = paths.splat_ply.exists()
    return JobStatus(
        id=data["id"],
        stage=Stage(data.get("stage", "pending")),
        percent=float(data.get("percent", 0.0)),
        message=data.get("message", ""),
        fps=int(data.get("fps", 3)),
        scale_factor=float(data.get("scale_factor", 1.0)),
        source_width=int(data.get("source_width", 0)),
        source_height=int(data.get("source_height", 0)),
        output_width=int(data.get("output_width", 0)),
        output_height=int(data.get("output_height", 0)),
        opensplat_iters=int(data.get("opensplat_iters", 500)),
        trained_iters=int(data.get("trained_iters", 0)),
        splat_exists=splat_exists,
        is_erp=bool(data.get("is_erp", False)),
    )


@app.get("/api/jobs/{job_id}/events")
async def stream_events(job_id: str, request: Request) -> StreamingResponse:
    if get_job(job_id) is None:
        raise HTTPException(404, "job not found")

    # Replay last known state immediately on connect
    initial = get_job(job_id) or {}

    async def event_gen() -> AsyncIterator[str]:
        q = bus.subscribe(job_id)
        try:
            # Initial replay
            initial_event = {
                "type": "stage",
                "stage": initial.get("stage", "pending"),
                "percent": initial.get("percent", 0.0),
                "message": initial.get("message", ""),
            }
            yield f"data: {json.dumps(initial_event)}\n\n"

            # Then live stream
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # Heartbeat
                    yield ": heartbeat\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n"
                # Close on terminal states
                if event.get("type") == "stage" and event.get("stage") in (
                    Stage.DONE.value, Stage.FAILED.value, Stage.CANCELLED.value,
                ):
                    # One more yield then exit
                    break
        finally:
            bus.unsubscribe(job_id, q)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict:
    if get_job(job_id) is None:
        raise HTTPException(404, "job not found")
    request_cancel(job_id)
    return {"ok": True, "job_id": job_id}


@app.post("/api/jobs/{job_id}/requeue")
async def requeue_job(job_id: str) -> dict:
    """Re-enqueue a stuck or failed job to continue from where it left off.

    Each stage is idempotent: ffmpeg skips if frames/ exists, COLMAP skips
    if sparse/0/cameras.txt exists, OpenSplat resumes from existing splat.ply
    if present. So this works for:
      - pending: just run from scratch (idempotent re-run)
      - cancelled / failed: retry — already-done stages get skipped
    """
    if get_job(job_id) is None:
        raise HTTPException(404, "job not found")
    paths = JobPaths.for_job(job_id)
    data = read_status(paths)
    stage = data.get("stage")
    if stage not in ("pending", "cancelled", "failed"):
        raise HTTPException(
            400,
            f"job is in stage '{stage}'; use 'resume' (if has splat) or "
            f"'跑 OpenSplat' (if has sparse) instead",
        )
    # Reset stage to pending; clear terminal-state fields so the worker
    # treats this as a fresh run. Stage idempotency handles partial progress.
    write_status(
        paths,
        stage=Stage.PENDING.value,
        percent=0.0,
        message="requeued",
        finished_at=None,
    )
    pos = enqueue(job_id)
    return {"ok": True, "job_id": job_id, "queue_position": pos}


@app.post("/api/jobs/{job_id}/resume")
async def resume_job(job_id: str, extra_iters: int = 10000) -> dict:
    """Re-enqueue a finished/failed job, resuming OpenSplat from the last .ply.

    Only acts on jobs that already produced a workspace/splat.ply OR a job-root
    splat.ply (i.e. stage 3 ran at least once). Extra iterations are appended
    to the previous run via OpenSplat's --resume flag.
    """
    if get_job(job_id) is None:
        raise HTTPException(404, "job not found")
    if extra_iters <= 0 or extra_iters > 100000:
        raise HTTPException(400, f"extra_iters must be in (0, 100000], got {extra_iters}")

    paths = JobPaths.for_job(job_id)
    resume_from = paths.splat_ply if paths.splat_ply.exists() else (
        paths.workspace_dir / "splat.ply"
    )
    if not resume_from.exists():
        raise HTTPException(400, "no splat.ply to resume from — run the job first")

    write_status(
        paths,
        stage=Stage.PENDING.value,
        percent=0.0,
        message=f"resuming (+{extra_iters} iters from {resume_from.name})",
        extra_iters=extra_iters,
        resume_from=str(resume_from.resolve()),
    )
    enqueue(job_id)
    return {"ok": True, "job_id": job_id, "resume_from": str(resume_from), "extra_iters": extra_iters}


@app.post("/api/jobs/{job_id}/stage/{opensplat}")
async def run_opensplat_only(job_id: str, opensplat: str, extra_iters: int = 10000) -> dict:
    """Run only the OpenSplat stage (skip ffmpeg + COLMAP).

    Requires COLMAP sparse output to exist (workspace/sparse/0/cameras.{bin,txt}).
    If splat.ply already exists, OpenSplat runs with --resume (continues training).
    If not, it trains from scratch with `extra_iters` iterations.
    """
    if get_job(job_id) is None:
        raise HTTPException(404, "job not found")
    if opensplat != "opensplat":
        raise HTTPException(400, f"only 'opensplat' is supported as a sub-stage, got {opensplat!r}")
    if extra_iters <= 0 or extra_iters > 100000:
        raise HTTPException(400, f"extra_iters must be in (0, 100000], got {extra_iters}")

    paths = JobPaths.for_job(job_id)
    sparse_txt = paths.workspace_dir / "sparse" / "0" / "cameras.txt"
    sparse_bin = paths.workspace_dir / "sparse" / "0" / "cameras.bin"
    if not sparse_txt.exists() and not sparse_bin.exists():
        raise HTTPException(
            400,
            "no COLMAP sparse output — run ffmpeg + colmap stages first",
        )

    resume_from = None
    msg_extra = ""
    if paths.splat_ply.exists() or (paths.workspace_dir / "splat.ply").exists():
        resume_from = paths.splat_ply if paths.splat_ply.exists() else (
            paths.workspace_dir / "splat.ply"
        )
        msg_extra = f" (resuming from {resume_from.name})"

    write_status(
        paths,
        stage=Stage.PENDING.value,
        percent=0.0,
        message=f"running OpenSplat{msg_extra}",
        extra_iters=extra_iters,
        resume_from=str(resume_from.resolve()) if resume_from else None,
    )
    enqueue(job_id)
    return {
        "ok": True,
        "job_id": job_id,
        "resume_from": str(resume_from) if resume_from else None,
        "extra_iters": extra_iters,
    }


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str) -> dict:
    """Remove a job and all its artifacts. Refuses if a worker is currently
    processing it (so we don't yank the rug out from under an active stage).

    Also kills any ffmpeg/colmap/opensplat whose cmdline still references
    this job — this catches orphan subprocesses from a prior server crash
    that the worker doesn't know about.
    """
    if get_job(job_id) is None:
        raise HTTPException(404, "job not found")
    paths = JobPaths.for_job(job_id)
    if paths.root.exists():
        # Kill any leftover ffmpeg/colmap/opensplat for this job.
        import subprocess as _sp
        for tool in ("ffmpeg", "colmap", "opensplat"):
            try:
                out = _sp.check_output(
                    ["pgrep", "-f", f"{tool}.*splat-jobs/{job_id}/"],
                    stderr=_sp.DEVNULL,
                ).decode().strip()
            except _sp.CalledProcessError:
                continue
            for pid in out.splitlines():
                pid = pid.strip()
                if not pid:
                    continue
                try:
                    _sp.run(["kill", "-TERM", pid], check=False)
                except Exception:
                    pass
        shutil.rmtree(paths.root)
    return {"ok": True, "job_id": job_id}


@app.delete("/api/jobs")
async def delete_all_jobs() -> dict:
    """Wipe every job directory. Use with care — this is irreversible.

    Skips jobs that the worker is currently processing (their stage is
    ffmpeg/cubemap/colmap/opensplat). For terminal jobs, also kill any
    leftover ffmpeg/colmap/opensplat subprocesses referencing the job
    before deleting the directory — otherwise the next bulk delete (or
    restart) can leave orphan processes around.
    """
    import subprocess as _sp
    from .worker import JOBS_DIR  # local import to avoid cycle
    active_stages = {"ffmpeg", "cubemap", "colmap", "opensplat"}
    deleted = 0
    skipped_active = 0
    killed = 0
    freed_bytes = 0
    for entry in list(JOBS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        status_path = entry / "status.json"
        try:
            data = json.loads(status_path.read_text()) if status_path.exists() else {}
        except Exception:
            data = {}
        stage = data.get("stage", "pending")
        if stage in active_stages:
            skipped_active += 1
            continue
        # Belt-and-suspenders: kill any ffmpeg/colmap/opensplat whose
        # cmdline still references this job, even if its status is
        # pending/done/etc. Prevents the orphan-subprocess case.
        job_id = entry.name
        for tool in ("ffmpeg", "colmap", "opensplat"):
            try:
                out = _sp.check_output(
                    ["pgrep", "-f", f"{tool}.*splat-jobs/{job_id}/"],
                    stderr=_sp.DEVNULL,
                ).decode().strip()
            except _sp.CalledProcessError:
                continue
            for pid in out.splitlines():
                pid = pid.strip()
                if not pid:
                    continue
                try:
                    _sp.run(["kill", "-TERM", pid], check=False)
                    killed += 1
                except Exception:
                    pass
        for dp, _, fns in os.walk(entry):
            for f in fns:
                try:
                    freed_bytes += os.path.getsize(os.path.join(dp, f))
                except OSError:
                    pass
        shutil.rmtree(entry)
        deleted += 1
    return {
        "ok": True,
        "deleted": deleted,
        "skipped_active": skipped_active,
        "killed": killed,
        "freed_bytes": freed_bytes,
    }


# --- Static: SPA ------------------------------------------------------------

@app.get("/")
async def spa_root() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


class NoCacheStaticFiles(StaticFiles):
    """Static files with `Cache-Control: no-cache` so changes to the SPA
    JS/CSS are picked up immediately without a hard refresh."""
    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp


app.mount("/static", NoCacheStaticFiles(directory=str(STATIC_DIR)), name="static")


# --- Static: per-job artifacts ----------------------------------------------

@app.get("/jobs/{job_id}/frames/{filename}")
async def serve_frame(job_id: str, filename: str) -> FileResponse:
    paths = JobPaths.for_job(job_id)
    if not paths.root.exists():
        raise HTTPException(404, "job not found")
    f = paths.frames_dir / filename
    if not f.is_file() or not f.resolve().is_relative_to(paths.frames_dir.resolve()):
        raise HTTPException(404, "frame not found")
    return FileResponse(f)


@app.get("/jobs/{job_id}/")
async def job_index(job_id: str) -> dict:
    paths = JobPaths.for_job(job_id)
    if not paths.root.exists():
        raise HTTPException(404, "job not found")
    return {
        "frames": sorted(p.name for p in paths.frames_dir.glob("*.jpg")),
        "splat_exists": paths.splat_ply.exists(),
        "settings_exists": paths.settings_json.exists(),
        "logs": sorted(p.name for p in paths.logs_dir.glob("*.log")) if paths.logs_dir.exists() else [],
    }


@app.get("/jobs/{job_id}/splat.ply")
async def serve_splat(job_id: str) -> FileResponse:
    paths = JobPaths.for_job(job_id)
    if not paths.splat_ply.is_file():
        raise HTTPException(404, "splat.ply not ready")
    return FileResponse(paths.splat_ply, media_type="application/octet-stream")


@app.post("/api/jobs/{job_id}/sog")
async def convert_to_sog(job_id: str, force: bool = False) -> dict:
    """Convert splat.ply → splat.sog for faster browser loading.

    Uses @playcanvas/splat-transform (Node.js CLI) to produce a quantized,
    WebP-textured SOG file. SOG files are typically 70-90% smaller than
    the source .ply and stream progressively in the viewer.
    """
    from pathlib import Path
    paths = JobPaths.for_job(job_id)
    if not paths.splat_ply.is_file():
        raise HTTPException(404, "splat.ply not ready")
    if paths.splat_sog.exists() and not force:
        return {"ok": True, "skipped": True, "sog": paths.splat_sog.name}

    from .config import SPLAT_TRANSFORM_BIN
    if SPLAT_TRANSFORM_BIN is None:
        raise HTTPException(
            503,
            "splat-transform CLI not installed; run "
            "`cd /tmp/sogtest && npm install @playcanvas/splat-transform`",
        )

    cmd = [SPLAT_TRANSFORM_BIN, "-w", str(paths.splat_ply), str(paths.splat_sog)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)  # noqa: S603
    if proc.returncode != 0:
        raise HTTPException(
            500, f"splat-transform failed: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    if not paths.splat_sog.is_file():
        raise HTTPException(500, "splat-transform returned 0 but no .sog was produced")

    return {
        "ok": True,
        "skipped": False,
        "sog": paths.splat_sog.name,
        "size_bytes": paths.splat_sog.stat().st_size,
    }


@app.get("/jobs/{job_id}/splat.sog")
async def serve_sog(job_id: str) -> FileResponse:
    paths = JobPaths.for_job(job_id)
    if not paths.splat_sog.is_file():
        raise HTTPException(404, "splat.sog not yet generated — POST /api/jobs/{id}/sog")
    return FileResponse(paths.splat_sog, media_type="application/octet-stream")


@app.get("/jobs/{job_id}/export.html", response_class=Response)
async def export_html(job_id: str) -> Response:
    """Return a self-contained HTML file with this job's SOG inlined.

    Built from the single-file SuperSplat template (viewer.html) with the
    `contentUrl` data URI in the `sse-bootstrap` script tag replaced by a
    base64 encoding of this job's splat.sog. The viewer settings are
    replaced from the job's settings.json if present.

    The HTML can be opened directly in a browser without any server.
    """
    from .config import VIEWER_SINGLE_FILE
    from .storage import build_export_html

    paths = JobPaths.for_job(job_id)
    if not paths.splat_sog.is_file():
        raise HTTPException(404, "splat.sog not ready; only done jobs can be exported")
    if not VIEWER_SINGLE_FILE.is_file():
        raise HTTPException(500, f"Template not found at {VIEWER_SINGLE_FILE}")

    html = build_export_html(VIEWER_SINGLE_FILE, paths)

    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="splat-{job_id[:8]}.html"',
        },
    )


@app.get("/jobs/{job_id}/settings.json")
async def serve_settings(job_id: str) -> FileResponse:
    paths = JobPaths.for_job(job_id)
    if not paths.settings_json.is_file():
        raise HTTPException(404, "settings.json not ready")
    return FileResponse(paths.settings_json, media_type="application/json")


@app.get("/jobs/{job_id}/logs/{name}")
async def job_log(job_id: str, name: str) -> FileResponse:
    paths = JobPaths.for_job(job_id)
    f = paths.logs_dir / name
    if not f.is_file() or not f.resolve().is_relative_to(paths.logs_dir.resolve()):
        raise HTTPException(404, "log not found")
    return FileResponse(f, media_type="text/plain")


# --- Static mounts (must come AFTER route definitions so they don't shadow) ---


# --- Viewer route: rewrite viewer/index.html with per-job contentUrl --------

@app.get("/viewer/{job_id}", response_class=Response)
async def viewer_page(job_id: str, format: Optional[str] = None) -> Response:
    if get_job(job_id) is None:
        raise HTTPException(404, "job not found")
    index_path = VIEWER_DIR / "index.html"
    if not index_path.is_file():
        raise HTTPException(500, f"SuperSplat index.html not found at {VIEWER_DIR}")

    html = index_path.read_text()

    paths = JobPaths.for_job(job_id)
    # Prefer SOG when (a) requested via ?format=sog and available, or
    # (b) the SOG file already exists and no explicit format was asked for.
    use_sog = False
    if format == "sog" and paths.splat_sog.is_file():
        use_sog = True
    elif format is None and paths.splat_sog.is_file():
        use_sog = True

    if use_sog:
        content_url = f"/jobs/{job_id}/splat.sog"
        content_filename = "splat.sog"
    else:
        content_url = f"/jobs/{job_id}/splat.ply"
        content_filename = "splat.ply"

    bootstrap_json = json.dumps(
        {
            "contentUrl": content_url,
            # Cache-bust settings.json so a re-load after the user edits their
            # camera track doesn't get the previous (browser-cached) version.
            "settingsUrl": f"/jobs/{job_id}/settings.json?v={_settings_version}",
            "contentFilename": content_filename,
        }
    )

    # 1. Replace the bootstrap JSON script tag.
    old_block_pattern = (
        '<script type="application/json" id="sse-bootstrap">'
        '{'
    )
    # Pattern matches the opening of the script tag, captures the closing </script>
    import re
    pattern = re.compile(
        r'<script type="application/json" id="sse-bootstrap">.*?</script>',
        re.DOTALL,
    )
    html = pattern.sub(
        f'<script type="application/json" id="sse-bootstrap">{bootstrap_json}</script>',
        html,
        count=1,
    )

    # 2. Set <base href="/viewer-static/"> so ./index.js and ./index.css resolve
    #    against the static-mounted /viewer-static directory (the build assumes
    #    a relative base; the route /viewer/{job_id} above cannot also host the
    #    mount without shadowing itself).
    html = html.replace('<base href="" />', '<base href="/viewer-static/" />', 1)
    if '<base href="/viewer-static/" />' not in html:
        # Defensive: if the literal doesn't match exactly, inject before <title>
        html = html.replace(
            "<title>",
            '<base href="/viewer-static/" /><title>',
            1,
        )

    # 3. (POC) Camera-track bridge: opens window.getCameraState by enabling the
    #    debug panel and listens for postMessage requests from the parent page.
    #    Also extends the captured camera state with a `target` field so we can
    #    persist it directly into settings.json animTracks (which stores
    #    position+target, not angles+distance).
    bridge = """
<script>
(function () {
    const cfg = window.sse && window.sse.config;
    if (cfg) cfg.debug = true;

    // Reproduce PlayCanvas Quat.setFromEulerAngles + applying forward=(0,0,1).
    // Quaternion (x, y, z, w) rotates a vector v as: v' = v + 2*w*(qv x v) + 2*(qv x (qv x v)).
    // For v=(0,0,1) this simplifies to a closed-form expression.
    function calcTarget(cam) {
        const [px, py, pz] = cam.position;
        const [ex, ey, ez] = cam.angles;          // degrees (PlayCanvas convention)
        const d  = cam.distance;
        const h  = 0.5 * Math.PI / 180;            // half-degree-to-rad
        const sx = Math.sin(ex*h), cx = Math.cos(ex*h);
        const sy = Math.sin(ey*h), cy = Math.cos(ey*h);
        const sz = Math.sin(ez*h), cz = Math.cos(ez*h);
        // PlayCanvas Quat.setFromEulerAngles (XYZ order):
        const qx = sx*cy*cz - cx*sy*sz;
        const qy = cx*sy*cz + sx*cy*sz;
        const qz = cx*cy*sz - sx*sy*cz;
        const qw = cx*cy*cz + sx*sy*sz;
        // Rotate (0,0,1) by q. Optimised formula:
        const tx = 2 * (qy*1 - qz*0);              // = 2*qy
        const ty = 2 * (qz*0 - qx*1);              // = -2*qx
        const tz = 2 * (qx*0 - qy*0);              // = 0
        const fx = 0 + qw*tx + (qy*tz - qz*ty);    // = 2*qw*qy + 2*qx*qz
        const fy = 0 + qw*ty + (qz*tx - qx*tz);    // = -2*qw*qx + 2*qy*qz
        const fz = 1 + qw*tz + (qx*ty - qy*tx);    // = 1 - 2*(qx^2 + qy^2)
        return [px + fx*d, py + fy*d, pz + fz*d];
    }

    function snapWithTarget() {
        if (typeof window.getCameraState !== "function") return null;
        const snap = window.getCameraState();
        if (!snap) return null;
        // Try to read target directly out of the viewer DOM (debug panel renders
        // it as text). Fall back to calcTarget if the panel isn't visible.
        try {
            const root = document.getElementById("sse-debug-panel");
            if (root) {
                const focusEl = root.querySelector('[data-id="focus"]');
                if (focusEl) {
                    const txt = (focusEl.textContent || "").trim();
                    const parts = txt.split(/[\s,]+/).filter(Boolean).map(Number);
                    if (parts.length === 3 && parts.every(Number.isFinite)) {
                        snap.target = parts;
                        return snap;
                    }
                }
            }
        } catch (_) {}
        snap.target = calcTarget(snap);
        return snap;
    }

    function announce() {
        const ok = typeof window.getCameraState === "function";
        window.parent.postMessage({ type: "pipeline:hello", getCameraStateAvailable: ok }, "*");
    }

    window.addEventListener("message", (ev) => {
        const d = ev.data;
        if (!d || typeof d !== "object") return;
        if (d.type === "pipeline:getCameraState") {
            try {
                const snap = snapWithTarget();
                ev.source.postMessage({ type: "pipeline:cameraState", snapshot: snap }, "*");
            } catch (err) {
                ev.source.postMessage({ type: "pipeline:error", message: String(err) }, "*");
            }
        } else if (d.type === "pipeline:setCameraState") {
            try {
                if (typeof window.setCameraState === "function") {
                    window.setCameraState(d.snapshot);
                    ev.source.postMessage({ type: "pipeline:cameraState", snapshot: window.getCameraState() }, "*");
                } else {
                    ev.source.postMessage({ type: "pipeline:error", message: "setCameraState unavailable" }, "*");
                }
            } catch (err) {
                ev.source.postMessage({ type: "pipeline:error", message: String(err) }, "*");
            }
        }
    });

    // Poll briefly until getCameraState exists (debug panel hooks it lazily).
    let tries = 0;
    const t_ = setInterval(() => {
        tries++;
        if (typeof window.getCameraState === "function") {
            clearInterval(t_);
            announce();
        } else if (tries > 60) {
            clearInterval(t_);
            announce();
        }
    }, 250);
})();
</script>
"""
    # Inject just before </body>; fall back to </html> if body not present.
    if "</body>" in html:
        html = html.replace("</body>", bridge + "</body>", 1)
    else:
        html = html.replace("</html>", bridge + "</html>", 1)

    return Response(content=html, media_type="text/html; charset=utf-8")


# --- Viewer static assets. Mounted at /viewer-static/ rather than /viewer/
#     so the /viewer/{job_id} route above isn't shadowed by the catch-all. ---
app.mount("/viewer-static", StaticFiles(directory=str(VIEWER_DIR)), name="viewer-static")


# --- Camera track (Phase B) ---------------------------------------------------
# GET    /api/jobs/{id}/camera-track       → {keyframes, duration, startMode, ...} | 404
# PUT    /api/jobs/{id}/camera-track       → save keyframes (body: {keyframes: [{position, target, fov}]})
# DELETE /api/jobs/{id}/camera-track       → remove the camera track (also flips startMode back to "default")

@app.get("/api/jobs/{job_id}/camera-track")
async def get_camera_track(job_id: str) -> dict:
    """Return the user-authored camera track for a job.

    Body shape (200):
        {
          "startMode": "animTrack" | "default",
          "keyframes": [
            {"position": [x,y,z], "target": [x,y,z], "fov": 60},
            ...
          ],
          "duration": 15.0,
          "frameRate": 1,
          "loopMode": "repeat",
          "name": "user-track"
        }
    Returns 404 if no track exists.
    """
    from .storage import JobPaths
    if get_job(job_id) is None:
        raise HTTPException(404, "job not found")
    paths = JobPaths.for_job(job_id)
    if not paths.settings_json.is_file():
        raise HTTPException(404, "settings.json not ready")
    cfg = json.loads(paths.settings_json.read_text())
    at = (cfg.get("animTracks") or [])
    if not at:
        raise HTTPException(404, "no camera track")
    tr = at[0]
    kf = tr.get("keyframes", {})
    n = len(kf.get("times", []))
    pos = kf.get("values", {}).get("position", [])
    tgt = kf.get("values", {}).get("target", [])
    fov = kf.get("values", {}).get("fov", [])
    # Re-nest the flat arrays back into triples for the API consumer
    keyframes = []
    for i in range(n):
        keyframes.append({
            "position": [pos[i*3], pos[i*3+1], pos[i*3+2]] if i*3+2 < len(pos) else None,
            "target":   [tgt[i*3], tgt[i*3+1], tgt[i*3+2]] if i*3+2 < len(tgt) else None,
            "fov":      fov[i] if i < len(fov) else 60,
        })
    return {
        "name":      tr.get("name", "user-track"),
        "duration":  tr.get("duration", n * 5),
        "frameRate": tr.get("frameRate", 1),
        "loopMode":  tr.get("loopMode", "repeat"),
        "interpolation": tr.get("interpolation", "spline"),
        "smoothness":    tr.get("smoothness", 1),
        "startMode":     cfg.get("startMode", "default"),
        "keyframes":     keyframes,
    }


@app.put("/api/jobs/{job_id}/camera-track")
async def put_camera_track(job_id: str, request: Request) -> dict:
    """Replace the camera track with the supplied keyframes.

    Body:
        {
          "keyframes": [
            {"position": [x,y,z], "target": [x,y,z], "fov": 60.0},
            ...
          ],
          "duration": 15.0,            // optional, default = N * 5
          "frameRate": 1,               // optional
          "loopMode":  "repeat",        // optional
          "interpolation": "spline",    // optional
          "smoothness": 1,              // optional
          "name": "user-track"          // optional
        }

    Saves to settings.json atomically (atomic temp + rename), preserves
    unrelated settings (background, postEffectSettings, etc.), and syncs
    cameras[0].initial from the first keyframe so the viewer doesn't snap
    on the very first frame.
    """
    from .storage import JobPaths
    if get_job(job_id) is None:
        raise HTTPException(404, "job not found")
    payload = await request.json()
    kfs = payload.get("keyframes") or []
    if not isinstance(kfs, list) or not kfs:
        raise HTTPException(400, "keyframes must be a non-empty list")
    if len(kfs) > 50:
        raise HTTPException(400, "too many keyframes (max 50)")
    for i, k in enumerate(kfs):
        for f in ("position", "target"):
            v = k.get(f)
            if not (isinstance(v, list) and len(v) == 3 and all(isinstance(x, (int, float)) for x in v)):
                raise HTTPException(400, f"keyframes[{i}].{f} must be [x,y,z] with numbers")
        if "fov" not in k or not isinstance(k["fov"], (int, float)):
            raise HTTPException(400, f"keyframes[{i}].fov must be a number")

    paths = JobPaths.for_job(job_id)
    cfg = json.loads(paths.settings_json.read_text()) if paths.settings_json.is_file() else {
        "version": 2,
        "tonemapping": "none",
        "highPrecisionRendering": False,
        "background": {"color": [0, 0, 0]},
        "postEffectSettings": {},
        "cameras": [],
        "annotations": [],
        "startMode": "default",
        "animTracks": [],
    }
    n = len(kfs)
    duration = float(payload.get("duration") or (n * 5))
    frameRate = int(payload.get("frameRate") or 1)
    loopMode = payload.get("loopMode") or "repeat"
    interpolation = payload.get("interpolation") or "spline"
    smoothness = payload.get("smoothness", 1)
    name = payload.get("name") or "user-track"

    pos_flat, tgt_flat, fov_flat = [], [], []
    for k in kfs:
        pos_flat.extend(k["position"])
        tgt_flat.extend(k["target"])
        fov_flat.append(float(k["fov"]))
    times = [i * (duration / n) for i in range(n)]

    cfg["animTracks"] = [{
        "name": name,
        "duration": duration,
        "frameRate": frameRate,
        "loopMode": loopMode,
        "interpolation": interpolation,
        "smoothness": smoothness,
        "keyframes": {
            "times":  times,
            "values": {"position": pos_flat, "target": tgt_flat, "fov": fov_flat},
        },
    }]
    cfg["startMode"] = "animTrack"

    # Sync cameras[0].initial from the first keyframe (nested triples).
    if not cfg.get("cameras"):
        cfg["cameras"] = [{}]
    cfg["cameras"][0]["initial"] = {
        "position": kfs[0]["position"],
        "target":   kfs[0]["target"],
        "fov":      float(kfs[0]["fov"]),
    }

    tmp = paths.settings_json.with_suffix(".json.part")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(paths.settings_json)

    # Bust any browser-cached settings.json URL.
    global _settings_version
    _settings_version = str(int(time.time()))

    return {"ok": True, "job_id": job_id, "keyframes": n, "duration": duration}


@app.delete("/api/jobs/{job_id}/camera-track")
async def delete_camera_track(job_id: str) -> dict:
    """Remove the camera track (empties animTracks, flips startMode to default).

    Keeps the rest of settings.json intact.
    """
    from .storage import JobPaths
    if get_job(job_id) is None:
        raise HTTPException(404, "job not found")
    paths = JobPaths.for_job(job_id)
    if not paths.settings_json.is_file():
        raise HTTPException(404, "settings.json not ready")
    cfg = json.loads(paths.settings_json.read_text())
    cfg["animTracks"] = []
    cfg["startMode"] = "default"
    tmp = paths.settings_json.with_suffix(".json.part")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(paths.settings_json)
    global _settings_version
    _settings_version = str(int(time.time()))
    return {"ok": True, "job_id": job_id}


def _exif_extract_geo(video_path) -> Optional[dict]:
    """Shell out to exiftool to read GPSLatitude/GPSLongitude from a video file.

    Returns {"lat": float, "lon": float, "source": "exif"} on success, or
    None on any failure (missing binary, no GPS tag, malformed output).
    Silently degrades — many videos lack GPS, and that's fine.
    """
    import shutil
    import subprocess as _sp
    exiftool = shutil.which("exiftool")
    if not exiftool:
        return None
    try:
        out = _sp.check_output(
            [exiftool, "-j", "-n", "-GPSLatitude", "-GPSLongitude", str(video_path)],
            stderr=_sp.DEVNULL,
            timeout=10,
        )
    except Exception:
        return None
    try:
        data = json.loads(out)
    except Exception:
        return None
    if not isinstance(data, list) or not data:
        return None
    rec = data[0]
    lat = rec.get("GPSLatitude")
    lon = rec.get("GPSLongitude")
    if lat is None or lon is None:
        return None
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
        return None
    return {"lat": lat_f, "lon": lon_f, "source": "exif"}


class GeoUpdate(BaseModel):
    lat: float
    lon: float
    label: Optional[str] = None


@app.put("/api/jobs/{job_id}/geo")
async def update_job_geo(job_id: str, body: GeoUpdate) -> dict:
    """Set/replace the geographic pin on a job. Marks source='manual'."""
    if get_job(job_id) is None:
        raise HTTPException(404, "job not found")
    if not (-90.0 <= body.lat <= 90.0):
        raise HTTPException(400, f"lat out of range: {body.lat}")
    if not (-180.0 <= body.lon <= 180.0):
        raise HTTPException(400, f"lon out of range: {body.lon}")
    paths = JobPaths.for_job(job_id)
    geo = {"lat": body.lat, "lon": body.lon, "source": "manual"}
    if body.label:
        geo["label"] = body.label
    write_status(paths, geo=geo)
    return {"ok": True, "job_id": job_id, "geo": geo}


@app.delete("/api/jobs/{job_id}/geo")
async def clear_job_geo(job_id: str) -> dict:
    """Remove the geo pin from a job."""
    if get_job(job_id) is None:
        raise HTTPException(404, "job not found")
    paths = JobPaths.for_job(job_id)
    existing = read_status(paths)
    existing.pop("geo", None)
    tmp = paths.status_json.with_suffix(".json.part")
    tmp.write_text(json.dumps(existing, default=str, indent=2))
    tmp.replace(paths.status_json)
    return {"ok": True, "job_id": job_id}


