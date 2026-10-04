"""macOS iCloud Drive integration: detecting cloud-only files, downloading, evicting."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

# st_flags bit set by APFS on "dataless" files whose contents live only in iCloud.
SF_DATALESS = 0x40000000

MOBILE_DOCUMENTS = Path.home() / "Library" / "Mobile Documents"
ICLOUD_DRIVE = MOBILE_DOCUMENTS / "com~apple~CloudDocs"

PLACEHOLDER_SUFFIX = ".icloud"


def is_dataless(st: os.stat_result) -> bool:
    """True when the file exists locally only as a cloud stub (modern macOS)."""
    return bool(getattr(st, "st_flags", 0) & SF_DATALESS)


def is_placeholder_name(name: str) -> bool:
    """Legacy placeholders look like '.Report.pdf.icloud' next to where the real file goes."""
    return name.startswith(".") and name.endswith(PLACEHOLDER_SUFFIX) and len(name) > len(PLACEHOLDER_SUFFIX) + 1


def placeholder_real_name(name: str) -> str:
    return name[1 : -len(PLACEHOLDER_SUFFIX)]


def placeholder_size(path: Path) -> int:
    """Read the real file size recorded inside a legacy .icloud placeholder plist (0 if unknown)."""
    try:
        with open(path, "rb") as fh:
            data = plistlib.load(fh)
        return int(data.get("NSURLFileSizeKey", 0))
    except Exception:
        return 0


class DownloadError(RuntimeError):
    pass


Runner = Callable[..., subprocess.CompletedProcess]


class ICloudClient:
    """Thin wrapper around `brctl`, the iCloud Drive command-line tool shipped with macOS."""

    def __init__(
        self,
        brctl: Optional[str] = None,
        runner: Runner = subprocess.run,
        poll_interval: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.brctl = brctl if brctl is not None else shutil.which("brctl")
        self.runner = runner
        self.poll_interval = poll_interval
        self.sleep = sleep
        self.clock = clock

    @property
    def available(self) -> bool:
        return bool(self.brctl)

    def _run(self, *args: str) -> None:
        if not self.brctl:
            return
        result = self.runner([self.brctl, *args], capture_output=True, text=True)
        if result.returncode != 0:
            raise DownloadError((result.stderr or result.stdout or "brctl failed").strip())

    def is_local(self, path: Path) -> bool:
        """True once `path` exists with its contents on local disk."""
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return False
        return not is_dataless(st)

    def download(self, path: Path, timeout: float) -> None:
        """Ask iCloud to download `path` and block until it is materialised.

        `path` is the real file path (not the `.icloud` placeholder). Without brctl
        we rely on the fact that reading a dataless file makes macOS fetch it.
        """
        if self.is_local(path):
            return
        if not self.available:
            if path.exists():
                return  # dataless file: reading it during copy triggers the download
            raise DownloadError(f"{path} is not downloaded and brctl is unavailable")
        self._run("download", str(path))
        deadline = self.clock() + timeout
        while not self.is_local(path):
            if self.clock() >= deadline:
                raise DownloadError(f"timed out after {timeout:.0f}s waiting for iCloud download")
            self.sleep(self.poll_interval)

    def evict(self, path: Path) -> None:
        """Remove the local copy, keeping the file in iCloud (frees space on the Mac)."""
        if not self.available:
            raise DownloadError("brctl is unavailable; cannot evict")
        self._run("evict", str(path))

