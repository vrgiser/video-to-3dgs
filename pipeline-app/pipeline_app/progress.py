"""Per-tool progress parsers. Each parser takes a stderr line + initial
context (e.g. expected frame count) and returns (percent, message) or None."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class ParsedProgress:
    percent: float
    message: str = ""


class ProgressParser:
    """Base class. Subclass per tool."""

    def __init__(self) -> None:
        self._last_percent: float = 0.0

    def parse(self, line: str) -> Optional[ParsedProgress]:
        raise NotImplementedError

    def done(self) -> ParsedProgress:
        return ParsedProgress(percent=100.0, message="done")


# ----- ffmpeg ----------------------------------------------------------------

# frame=  123 fps= 30 q=29.0 size=    256kB time=00:00:05.00 bitrate=...
_FFMPEG_FRAME_RE = re.compile(r"frame=\s*(\d+)")
_FFMPEG_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")


class FFmpegParser(ProgressParser):
    """Parse ffmpeg stderr progress lines.

    The ffmpeg vf filterchain (`-vf fps=N,scale=W:H`) caps output at N frames
    per second of input. With Duration=D seconds and fps=N, expected frames
    ≈ N * D.
    """

    def __init__(self, duration_seconds: float, fps: int) -> None:
        super().__init__()
        self.duration = max(duration_seconds, 1.0)
        self.expected_frames = max(int(self.duration * fps), 1)

    def parse(self, line: str) -> Optional[ParsedProgress]:
        m = _FFMPEG_FRAME_RE.search(line)
        if not m:
            return None
        current = int(m.group(1))
        pct = min(100.0, current / self.expected_frames * 100.0)
        if pct - self._last_percent < 0.5:
            return None  # throttle: avoid flooding SSE with sub-percent updates
        self._last_percent = pct
        return ParsedProgress(percent=pct, message=f"frame {current}/{self.expected_frames}")


# ----- COLMAP ----------------------------------------------------------------

# COLMAP 4.x automatic_reconstructor log lines (glued to a single prefix by
# glog; we look at the right side).
_COLMAP_PHASE_EX = re.compile(r"===\s*(\w[\w\s]*?)\s*===")
_COLMAP_PROCESSED_EX = re.compile(r"Processed file \[(\d+)/(\d+)\]")
_COLMAP_REGISTER_EX = re.compile(r"Registering image #(\d+)\s*\(num_reg_frames=(\d+)\)")
_COLMAP_BA_EX = re.compile(r"Bundle adjustment|Retriangulation|Global\s*bundle", re.I)


class ColmapParser(ProgressParser):
    """Parse COLMAP automatic_reconstructor progress.

    - Feature extraction: `Processed file [N/M]`  →  0%  → 30%
    - Feature matching:   `=== Feature matching ===`  →  30% → 60%
    - Image registration: `Registering image #X (num_reg_frames=N)` → 60% → 95%
    - Bundle adjustment:  `Bundle adjustment` lines  → 95% → 100%
    """

    # Phase weights (must sum to 1.0)
    PHASE_WEIGHTS = {
        "extract": (0.0, 0.30),
        "match":   (0.30, 0.60),
        "register": (0.60, 0.95),
        "finalize": (0.95, 1.0),
    }

    def __init__(self, expected_images: int = 0) -> None:
        super().__init__()
        self.phase = "extract"  # current phase
        self.total_images = max(expected_images, 1)
        self.registered = 0  # max num_reg_frames seen
        self.seen_matching = False
        self.seen_registering = False

    def parse(self, line: str) -> Optional[ParsedProgress]:
        # Detect phase transitions via "=== ... ===" markers
        m = _COLMAP_PHASE_EX.search(line)
        if m:
            phase_name = m.group(1).lower()
            if "feature extraction" in phase_name:
                self.phase = "extract"
            elif "matching" in phase_name:
                self.phase = "match"
                self.seen_matching = True
            elif "reconstruction" in phase_name or "reconstruct" in phase_name:
                # automatic_reconstructor ends with "=== Reconstructing ... ==="
                self.phase = "register"

        m = _COLMAP_PROCESSED_EX.search(line)
        if m and self.phase in ("extract", "match"):
            current, total = int(m.group(1)), int(m.group(2))
            if total > 0:
                self.total_images = total
            lo, hi = self.PHASE_WEIGHTS["extract"] if self.phase == "extract" else self.PHASE_WEIGHTS["match"]
            frac = current / max(total, 1)
            # extract advances within extract range; matching blocks then jump to match range
            if self.phase == "extract":
                pct = lo + (hi - lo) * frac
            else:
                pct = lo + (hi - lo) * frac
            if pct - self._last_percent >= 0.5:
                self._last_percent = pct
                return ParsedProgress(percent=pct, message=f"phase:{self.phase} {current}/{total}")

        m = _COLMAP_REGISTER_EX.search(line)
        if m:
            self.seen_registering = True
            self.registered = max(self.registered, int(m.group(2)))
            lo, hi = self.PHASE_WEIGHTS["register"]
            frac = self.registered / max(self.total_images, 1)
            pct = lo + (hi - lo) * min(frac, 1.0)
            if pct - self._last_percent >= 0.5:
                self._last_percent = pct
                return ParsedProgress(percent=pct, message=f"registered {self.registered}/{self.total_images}")

        if _COLMAP_BA_EX.search(line) and self.seen_registering:
            # Bundle adjustment marks the tail end of the run
            pct = 99.0
            if pct - self._last_percent >= 0.5:
                self._last_percent = pct
                return ParsedProgress(percent=pct, message="bundle adjustment")

        return None


# ----- OpenSplat -------------------------------------------------------------

# Example lines from the arm64 binary:
#   "Step 10: 0.15787 [20%]"
#   "Wrote /path/to/splat_25.ply"
_OPEN_SPLAT_STEP_EX = re.compile(r"Step\s+(\d+):\s*[\d.]+\s*\[(\d+)%\]")


class OpenSplatParser(ProgressParser):
    """Parse OpenSplat training progress.

    `Step N: loss [P%]` lines already include a percentage — pass through.
    `Wrote <path>` lines indicate a checkpoint save (advance by 1%).

    OpenSplat's "Step N" is the per-run iteration counter (starts at 1 each
    run, even with --resume). To show cumulative progress that includes the
    already-trained portion of a resumed model, we accept a `prior_trained`
    parameter and report percent as (prior + step) / (prior + total_iters).
    """

    def __init__(self, total_iters: int = 30000, prior_trained: int = 0) -> None:
        super().__init__()
        self.total_iters = max(total_iters, 1)
        self.prior_trained = max(prior_trained, 0)

    def parse(self, line: str) -> Optional[ParsedProgress]:
        m = _OPEN_SPLAT_STEP_EX.search(line)
        if m:
            step = int(m.group(1))
            # `step` is OpenSplat's per-run counter (starts at 1 even with --resume).
            # Show the current run's progress, but include the cumulative
            # position so the user knows how far along the *whole* model is.
            cumulative_step = self.prior_trained + step
            cumulative_total = self.prior_trained + self.total_iters
            pct = min(100.0, cumulative_step / cumulative_total * 100.0)
            if pct - self._last_percent >= 0.5 or step >= self.total_iters:
                self._last_percent = pct
                loss_match = re.search(r"Step\s+\d+:\s*([\d.]+)", line)
                loss = loss_match.group(1) if loss_match else ""
                msg = (
                    f"step {step}/{self.total_iters}"
                    f" (cumulative {cumulative_step}/{cumulative_total})"
                    f" loss={loss}"
                )
                return ParsedProgress(percent=pct, message=msg)
        return None