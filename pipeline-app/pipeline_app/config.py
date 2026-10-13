"""Centralized configuration and path resolution."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

# Project root (this file's grandparent)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"

# Runtime data (per-job artifacts)
JOBS_DIR = Path(
    os.environ.get(
        "JOBS_DIR",
        str(Path.home() / ".claude" / "splat-jobs"),
    )
).expanduser().resolve()

# Reuse the self-hosted SuperSplat build
VIEWER_DIR = Path(
    os.environ.get(
        "VIEWER_DIR",
        "/Users/mlens/work/claude/web_v2s/viewer",
    )
).expanduser().resolve()

# Single-file SuperSplat template (viewer + sample SOG inlined). Used to
# generate a self-contained export.html per job, with that job's SOG
# substituted in. Override via env if you keep the template elsewhere.
VIEWER_SINGLE_FILE = Path(
    os.environ.get(
        "VIEWER_SINGLE_FILE",
        str(PROJECT_ROOT / ".." / "viewer" / "export.html"),
    )
).expanduser().resolve()

# Binary discovery: env override wins, otherwise rely on $PATH
def _resolve_binary(env_name: str, fallbacks: list[str]) -> str:
    override = os.environ.get(env_name)
    if override:
        p = Path(override).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
        # Not an absolute override; treat as PATH lookup
        found = shutil.which(override)
        if found:
            return found
    for fb in fallbacks:
        found = shutil.which(fb)
        if found:
            return found
    raise FileNotFoundError(
        f"Could not find {env_name}. Set ${env_name} or install: tried {fallbacks}"
    )


FFMPEG_BIN = _resolve_binary("FFMPEG_BIN", ["ffmpeg"])
FFPROBE_BIN = _resolve_binary("FFPROBE_BIN", ["ffprobe"])
COLMAP_BIN = _resolve_binary("COLMAP_BIN", ["colmap"])


def _resolve_optional_binary(env_name: str, fallbacks: list[str]) -> Optional[str]:
    """Same as _resolve_binary but returns None instead of raising when missing."""
    try:
        return _resolve_binary(env_name, fallbacks)
    except FileNotFoundError:
        return None


# Default to the locally-compiled arm64 binary; can be overridden via env or
# PATH lookup. Missing binary does not block boot (only the opensplat stage
# needs it).
OPEN_SPLAT_BIN: Optional[str] = _resolve_optional_binary(
    "OPEN_SPLAT_BIN",
    ["/Users/mlens/work/claude/splat/opensplat", "opensplat"],
)

# @playcanvas/splat-transform — Node CLI for .ply ↔ .sog conversion.
# Install once with:  npm install -g @playcanvas/splat-transform
# or in any directory and point SPLAT_TRANSFORM_BIN to the local bin.
SPLAT_TRANSFORM_BIN: Optional[str] = _resolve_optional_binary(
    "SPLAT_TRANSFORM_BIN",
    ["splat-transform"],
)

# Server
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8888"))

# MVP defaults — extracted frames per second for ffmpeg stage
DEFAULT_FPS = int(os.environ.get("DEFAULT_FPS", "3"))
DEFAULT_SCALE = os.environ.get("DEFAULT_SCALE", "518:378")

# OpenSplat training iterations (kept low for fast dev iteration)
DEFAULT_OPENSPLAT_ITERS = int(os.environ.get("DEFAULT_OPENSPLAT_ITERS", "500"))

# Stage time limits (seconds). COLMAP and OpenSplat can run for many hours on
# large scenes (especially CPU-only). Override via env if you need longer.
STAGE_TIMEOUTS = {
    "ffmpeg":    int(os.environ.get("STAGE_TIMEOUT_FFMPEG",    "600")),
    "cubemap":   int(os.environ.get("STAGE_TIMEOUT_CUBEMAP",   "1800")),   # 30 min
    "colmap":    int(os.environ.get("STAGE_TIMEOUT_COLMAP",   "14400")),   # 4 h
    "opensplat": int(os.environ.get("STAGE_TIMEOUT_OPENSPLAT","28800")),  # 8 h
}

# Cubemap face size when a video is detected as equirectangular (2:1).
# Each ERP frame becomes 6 pinhole images of this resolution.
CUBEMAP_FACE_SIZE = int(os.environ.get("CUBEMAP_FACE_SIZE", "518"))

# Job root template
JOB_DIR_TEMPLATE = JOBS_DIR / "{job_id}"

# Ensure runtime dirs exist on import (idempotent)
JOBS_DIR.mkdir(parents=True, exist_ok=True)