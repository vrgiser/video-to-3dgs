"""Subprocess helpers: run a CLI stage with log capture, progress parsing,
and cancellation via process group kill."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional

from .progress import FFmpegParser, ParsedProgress, ProgressParser


CancelFn = Callable[[], bool]


class StageRunner:
    """Run one CLI stage, parse progress from stderr, support cancellation.

    Adds a heartbeat that fires every `heartbeat_seconds` (default 15s) so
    the UI knows the subprocess is still alive even when stdout/stderr is
    silent (OpenSplat training loops are mostly quiet between Step lines).
    """

    def __init__(
        self,
        cmd: list[str],
        log_path: Path,
        parser: ProgressParser,
        on_progress: Callable[[ParsedProgress], None],
        cancel: Optional[CancelFn] = None,
        timeout_seconds: Optional[int] = None,
        heartbeat_seconds: int = 15,
    ) -> None:
        self.cmd = cmd
        self.log_path = log_path
        self.parser = parser
        self.on_progress = on_progress
        self.cancel = cancel or (lambda: False)
        self.timeout_seconds = timeout_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self._process: Optional[subprocess.Popen] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_stop = threading.Event()
        self._started_at: float = 0.0
        self._last_progress_at: float = 0.0

    def run(self) -> int:
        """Spawn process; returns exit code. Raises on cancellation."""
        log_fh = open(self.log_path, "wb")

        self._process = subprocess.Popen(
            self.cmd,
            stdout=subprocess.PIPE,
            # Merge stdout into stderr so a single reader thread catches
            # everything (some tools — notably OpenSplat — emit progress
            # on stdout, not stderr).
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            bufsize=1,
        )

        import time

        self._started_at = time.time()
        self._last_progress_at = self._started_at
        self._heartbeat_stop.clear()

        def _tail() -> None:
            assert self._process and self._process.stdout
            for raw in self._process.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip()
                log_fh.write((line + "\n").encode())
                log_fh.flush()
                parsed = self.parser.parse(line)
                if parsed is not None:
                    self._last_progress_at = time.time()
                    try:
                        self.on_progress(parsed)
                    except Exception:
                        pass

        def _heartbeat() -> None:
            while not self._heartbeat_stop.is_set():
                self._heartbeat_stop.wait(self.heartbeat_seconds)
                if self._heartbeat_stop.is_set():
                    return
                idle = time.time() - self._last_progress_at
                if idle < self.heartbeat_seconds:
                    continue
                # Only fire heartbeat if there's been real activity (subprocess alive).
                if not self._process or self._process.poll() is not None:
                    return
                elapsed = int(time.time() - self._started_at)
                # Don't advance percent on heartbeat — just signal "still running".
                try:
                    self.on_progress(ParsedProgress(
                        percent=self.parser._last_percent,  # type: ignore[attr-defined]
                        message=f"still running ({elapsed}s elapsed)",
                    ))
                except Exception:
                    pass

        self._stderr_thread = threading.Thread(target=_tail, daemon=True)
        self._heartbeat_thread = threading.Thread(target=_heartbeat, daemon=True)
        self._stderr_thread.start()
        self._heartbeat_thread.start()

        try:
            return self._wait()
        finally:
            self._heartbeat_stop.set()
            log_fh.close()

    def _wait(self) -> int:
        assert self._process is not None
        proc = self._process
        # Poll loop: check cancel every 0.5s, also support timeout
        import time

        deadline = (
            time.time() + self.timeout_seconds if self.timeout_seconds else None
        )
        while True:
            ret = proc.poll()
            if ret is not None:
                # Drain stderr thread
                if self._stderr_thread:
                    self._stderr_thread.join(timeout=2.0)
                return ret
            if self.cancel():
                self._kill_tree()
                if self._stderr_thread:
                    self._stderr_thread.join(timeout=2.0)
                raise CancelledError(f"cancelled while running: {self.cmd[0]}")
            if deadline and time.time() > deadline:
                self._kill_tree()
                if self._stderr_thread:
                    self._stderr_thread.join(timeout=2.0)
                raise TimeoutError(f"timeout after {self.timeout_seconds}s: {self.cmd[0]}")
            time.sleep(0.5)

    def _kill_tree(self) -> None:
        assert self._process is not None
        try:
            os.killpg(self._process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        # Wait briefly for graceful shutdown
        import time

        for _ in range(10):
            if self._process.poll() is not None:
                return
            time.sleep(0.5)
        # Escalate
        try:
            os.killpg(self._process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


class CancelledError(Exception):
    """Raised when a stage is cancelled mid-run."""