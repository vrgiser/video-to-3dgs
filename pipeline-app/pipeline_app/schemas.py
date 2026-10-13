"""Pydantic schemas shared across API and worker."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class Stage(str, Enum):
    PENDING = "pending"
    FFMPEG = "ffmpeg"
    CUBEMAP = "cubemap"
    COLMAP = "colmap"
    OPENSPLAT = "opensplat"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Stage progression order; DONE/FAILED/CANCELLED are terminal
STAGE_ORDER = [
    Stage.FFMPEG,
    Stage.CUBEMAP,
    Stage.COLMAP,
    Stage.OPENSPLAT,
    Stage.DONE,
]


class JobCreate(BaseModel):
    fps: int = 3
    scale_factor: float = 1.0
    opensplat_iters: int = 500
    is_erp: bool = False


class GeoLocation(BaseModel):
    """Geographic pin attached to a job (lat/lon + optional label/source)."""
    lat: float  # -90..90
    lon: float  # -180..180
    label: Optional[str] = None
    source: str = "manual"  # "manual" | "exif"
    captured_at: Optional[datetime] = None


class JobStatus(BaseModel):
    id: str
    stage: Stage = Stage.PENDING
    percent: float = 0.0
    message: str = ""
    created_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    fps: int = 3
    scale_factor: float = 1.0
    source_width: int = 0
    source_height: int = 0
    output_width: int = 0
    output_height: int = 0
    opensplat_iters: int = 500
    trained_iters: int = 0  # cumulative count of OpenSplat iterations actually run
    splat_exists: bool = False
    sog_exists: bool = False  # compressed SOG exists (for pin-globe eligibility)
    is_erp: bool = False
    geo: Optional[GeoLocation] = None  # set if user provided lat/lon (or EXIF detected)


class ProgressUpdate(BaseModel):
    stage: Stage
    percent: float
    message: str = ""
    timestamp: Optional[datetime] = None