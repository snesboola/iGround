"""The backup folder on the SSD.

    MySSD/
      iCloud Backup 2026-10-04/        <- named after the day it was last brought up to date
        Photos/2023/01 January/...
        Photos/Albums/<album>/...
        Photos/Favourites/...
        iCloud Drive/...
        App Documents/Pages/...
        Messages/...
        iPhone Backups/...
        About this backup.txt
        .iground/                      <- hidden bookkeeping (manifests, album list)
"""

from __future__ import annotations

import json
import re
import socket
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

from . import manifest as mf

PREFIX = "iCloud Backup "
STATE_DIR = mf.MANIFEST_DIR
INFO_FILE = "backup.json"
ABOUT_FILE = "About this backup.txt"
_NAME_RE = re.compile(r"^iCloud Backup (\d{4}-\d{2}-\d{2})( \(\d+\))?$")


def folder_name(day: date) -> str:
    return f"{PREFIX}{day.isoformat()}"


def is_backup(path: Path) -> bool:
    return (Path(path) / STATE_DIR / INFO_FILE).is_file()


def find_backups(drive: Path) -> List[Path]:
    """Backups directly inside `drive`, oldest first."""
    drive = Path(drive)
    if not drive.is_dir():
        return []
    found = [p for p in drive.iterdir() if p.is_dir() and _NAME_RE.match(p.name) and is_backup(p)]
    return sorted(found, key=lambda p: p.name)


def resolve(path: Path) -> Path:
    """Accept either a backup folder or the drive holding backups (→ the newest one)."""
    path = Path(path).expanduser()
    if is_backup(path):
        return path
    backups = find_backups(path)
    if not backups:
        raise FileNotFoundError(f"no iCloud backup found in {path}")
    return backups[-1]


def state_dir(root: Path, key: str) -> Path:
    return Path(root) / STATE_DIR / key


def read_info(root: Path) -> dict:
    try:
        return json.loads((Path(root) / STATE_DIR / INFO_FILE).read_text())
    except (OSError, ValueError):
        return {}


def write_info(root: Path, **changes) -> dict:
    info = read_info(root)
    info.update(changes)
    path = Path(root) / STATE_DIR / INFO_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(info, indent=2))
    tmp.replace(path)
    return info


@dataclass
class Opened:
    root: Path
    previous: Optional[Path]  # the folder's name before it was renamed to today's date
    created: bool


def open_backup(drive: Path, today: Optional[date] = None, new: bool = False) -> Opened:
    """Find the backup to work on, or create one.

    By default the newest existing backup is brought up to date and renamed to
    today's date, so there is one backup and its name says when it was last
    updated. With `new=True` a separate, complete backup is started instead.
    """
    drive = Path(drive).expanduser()
    today = today or date.today()
    target = drive / folder_name(today)
    backups = find_backups(drive)
    now = datetime.now().isoformat(timespec="seconds")

    if backups and not new:
        latest = backups[-1]
        if latest == target:
            return Opened(latest, None, False)
        if not target.exists():
            latest.rename(target)
            return Opened(target, latest, False)
        # Today's name is taken by an unrelated folder: keep working where we are.
        return Opened(latest, None, False)

    n = 1
    while target.exists():
        n += 1
        target = drive / f"{folder_name(today)} ({n})"
    target.mkdir(parents=True)
    write_info(target, created=now, host=socket.gethostname(), completed=None)
    return Opened(target, None, True)
