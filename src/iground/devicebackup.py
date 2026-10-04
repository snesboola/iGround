"""Keep iPhone/iPad backups (which include WhatsApp chats) on the SSD instead of iCloud.

WhatsApp's own iCloud backup and iCloud device backups cannot be downloaded to a
Mac. A local Finder backup of the phone contains the same data, so the
replacement for both is: make Finder write its backups to the SSD.

Finder always writes to ~/Library/Application Support/MobileSync/Backup, so that
folder is replaced by a symlink pointing at `<SSD>/iPhone Backups`.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from . import manifest as mf
from .icloud import ICloudClient
from .migrate import MigrationError, Migrator, Options
from .sources import BACKUPS_DIR, Locations

OLD_SUFFIX = ".before-iground"


@dataclass
class DeviceBackup:
    device: str
    product: str
    last_backup: Optional[datetime]
    encrypted: Optional[bool]
    path: Path


def list_backups(folder: Path) -> List[DeviceBackup]:
    out = []
    if not folder.is_dir():
        return out
    for d in sorted(folder.iterdir()):
        info = d / "Info.plist"
        if not info.is_file():
            continue
        try:
            with open(info, "rb") as fh:
                data = plistlib.load(fh)
        except Exception:
            continue
        encrypted = None
        try:
            with open(d / "Manifest.plist", "rb") as fh:
                encrypted = bool(plistlib.load(fh).get("IsEncrypted"))
        except Exception:
            pass
        out.append(DeviceBackup(
            device=str(data.get("Device Name") or data.get("Display Name") or d.name),
            product=str(data.get("Product Type") or ""),
            last_backup=data.get("Last Backup Date"),
            encrypted=encrypted,
            path=d,
        ))
    return out


def ssd_backup_dir(dest: Path) -> Path:
    return Path(dest) / BACKUPS_DIR


def is_relocated(loc: Locations, dest: Path) -> bool:
    link = loc.mobilesync_backup
    return link.is_symlink() and Path(os.path.realpath(link)) == ssd_backup_dir(dest).resolve()


def relocate(loc: Locations, dest: Path) -> str:
    """Move existing backups to the SSD and point Finder's backup folder there."""
    link = loc.mobilesync_backup
    target = ssd_backup_dir(dest).expanduser()
    if is_relocated(loc, dest):
        return f"Already set up: Finder backs up to {target}"
    if link.is_symlink():
        raise MigrationError(f"{link} already points to {os.readlink(link)}; run with --undo first")

    target.mkdir(parents=True, exist_ok=True)
    moved = 0
    if link.is_dir() and any(link.iterdir()):
        result = Migrator(link, target, Options(excludes=(".DS_Store",)), ICloudClient(brctl="")).run()
        if result.failed:
            raise MigrationError(
                f"{result.failed} file(s) of the existing backups could not be copied; nothing was changed. "
                f"First error: {result.errors[0][0]}: {result.errors[0][1]}"
            )
        moved = result.copied + result.skipped
    # Finder treats every sub-folder as a backup, so don't leave the manifest there.
    shutil.rmtree(target / mf.MANIFEST_DIR, ignore_errors=True)

    if link.exists():
        old = link.with_name(link.name + OLD_SUFFIX)
        if old.exists():
            old = link.with_name(f"{link.name}{OLD_SUFFIX}-{int(time.time())}")
        link.rename(old)
        note = f"\nYour previous backups were copied ({moved} files) and the originals kept at {old} — " \
               "delete that folder once Finder lists your backups, to free space on the Mac."
    else:
        link.parent.mkdir(parents=True, exist_ok=True)
        note = ""
    os.symlink(target, link)
    return f"Finder will now back up iPhones/iPads to {target}{note}"


def undo(loc: Locations) -> str:
    link = loc.mobilesync_backup
    if not link.is_symlink():
        return "Nothing to undo: Finder's backup folder is not redirected."
    link.unlink()
    old = link.with_name(link.name + OLD_SUFFIX)
    if old.is_dir():
        old.rename(link)
        return f"Restored the original backup folder at {link}"
    link.mkdir()
    return f"Finder will back up to {link} again (backups already on the SSD were left there)."
