"""ERP (equirectangular panorama) utilities."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

# Forward, up, right unit vectors for each cubemap face (looking outward).
# World convention: +Y up, right-handed.
# `h_faces` and `v_faces` are subsets for the 4-face ("horizontal only")
# pipeline used to keep COLMAP's bundle adjustment well-conditioned — the
# 6-face cubemap puts 6 cameras at the same world position (no parallax
# between them), which makes BA's covariance matrix non-positive-definite.
FACES: dict[str, tuple[tuple[float, float, float], ...]] = {
    "+x": (( 1, 0, 0), (0, 1, 0), (0, 0, -1)),
    "-x": ((-1, 0, 0), (0, 1, 0), (0, 0,  1)),
    "+y": (( 0, 1, 0), (0, 0,-1), (1, 0, 0)),
    "-y": (( 0,-1, 0), (0, 0, 1), (1, 0, 0)),
    "+z": (( 0, 0, 1), (0, 1, 0), (1, 0, 0)),
    "-z": (( 0, 0,-1), (0, 1, 0), (-1,0, 0)),
}
H_FACES = ("+x", "-x", "+z", "-z")  # exclude the up/down poles


def erp_to_cubemap(erp_rgb: np.ndarray, face_size: int) -> dict[str, np.ndarray]:
    """Convert (H, W, 3) ERP image into 6 face images (face_size, face_size, 3).

    ERP convention (this module):
        u in [0,1] → longitude θ in [-π, π]; v in [0,1] → latitude φ in [+π/2, -π/2].
        Top row of ERP (v=0) is +Y world (north pole); bottom row (v=1) is -Y.
    """
    H, W, _ = erp_rgb.shape
    out: dict[str, np.ndarray] = {}

    # Pixel centers in [-1, 1] across the face.
    # +u is to the right of the face, +v is up.
    u = (np.arange(face_size) + 0.5) / face_size * 2 - 1
    v = (np.arange(face_size) + 0.5) / face_size * 2 - 1
    uu, vv = np.meshgrid(u, v)
    uu = uu.ravel()
    vv = vv.ravel()

    for name, (fwd, up, right) in FACES.items():
        fwd = np.array(fwd, dtype=np.float64)
        up = np.array(up, dtype=np.float64)
        right = np.array(right, dtype=np.float64)
        d = uu[:, None] * right + vv[:, None] * up + fwd[None, :]
        d /= np.linalg.norm(d, axis=1, keepdims=True)
        x, y, z = d[:, 0], d[:, 1], d[:, 2]

        theta = np.arctan2(z, x)
        phi = np.arcsin(np.clip(y, -1.0, 1.0))
        erp_u = (theta + np.pi) / (2 * np.pi)
        erp_v = (np.pi / 2 - phi) / np.pi

        # Bilinear sample with horizontal wrap (longitude wraps)
        fx = erp_u * W - 0.5
        fy = erp_v * H - 0.5
        ix = np.floor(fx).astype(np.int64)
        iy = np.floor(fy).astype(np.int64)
        tx = (fx - ix).astype(np.float32)[:, None]
        ty = (fy - iy).astype(np.float32)[:, None]
        ix = ix % W
        ix1 = (ix + 1) % W
        iy0 = np.clip(iy, 0, H - 1)
        iy1 = np.clip(iy + 1, 0, H - 1)

        p00 = erp_rgb[iy0, ix]
        p10 = erp_rgb[iy0, ix1]
        p01 = erp_rgb[iy1, ix]
        p11 = erp_rgb[iy1, ix1]
        sample = (
            (1 - tx) * (1 - ty) * p00
            + tx * (1 - ty) * p10
            + (1 - tx) * ty * p01
            + tx * ty * p11
        )
        out[name] = np.clip(sample, 0, 255).astype(np.uint8).reshape(face_size, face_size, 3)
    return out


def is_erp_video(video_path: Path, tolerance: float = 0.05) -> bool:
    """True if the video's pixel aspect ratio is ~2:1 (ERP convention)."""
    import subprocess
    try:
        out = subprocess.check_output(  # noqa: S603
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=s=x:p=0",
                str(video_path),
            ],
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).decode().strip()
    except Exception:
        return False
    if "x" not in out:
        return False
    # Take the first "WxH"-shaped token (defensive against extra fields).
    import re as _re
    m = _re.search(r"(\d+)\s*x\s*(\d+)", out)
    if not m:
        return False
    w, h = int(m.group(1)), int(m.group(2))
    if h == 0:
        return False
    ratio = w / h
    return (2.0 - tolerance) < ratio < (2.0 + tolerance)


def convert_frame(erp_path: Path, out_dir: Path, face_size: int, horizontal_only: bool = False) -> list[Path]:
    """Convert one ERP JPEG into cubemap face JPEGs. Returns the written file paths.

    `horizontal_only=True` drops the +Y and -Y faces (the poles), which are
    the most distorted and confuse bundle adjustment in COLMAP when 6 cameras
    are co-located per frame. Recommended for ERP→3DGS pipelines.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    img = Image.open(erp_path).convert("RGB")
    erp_rgb = np.asarray(img, dtype=np.float32)
    all_faces = erp_to_cubemap(erp_rgb, face_size)
    written: list[Path] = []
    face_names = H_FACES if horizontal_only else FACES.keys()
    for name in face_names:
        out_path = out_dir / f"{erp_path.stem}_{name}.jpg"
        Image.fromarray(all_faces[name]).save(out_path, quality=92)
        written.append(out_path)
    return written
