"""`iground ready`: is everything safely on the SSD so the iCloud plan can be downgraded?"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Sequence

from . import devicebackup, layout
from . import manifest as mf
from .migrate import diff_against_source
from .photos import PhotosClient, PhotosError
from .sources import ALL_KINDS, PHOTOS, Locations, folder_sources

OK, MISSING, MANUAL = "ok", "missing", "manual"
BACKUP_MAX_AGE = timedelta(days=14)


@dataclass
class Check:
    name: str
    status: str  # OK | MISSING | MANUAL
    detail: str
    fix: Optional[str] = None


@dataclass
class Report:
    checks: List[Check] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return all(c.status != MISSING for c in self.checks)


MANUAL_STEPS = [
    Check("WhatsApp", MANUAL,
          "WhatsApp's own iCloud backup cannot be copied off iCloud. Your chats are protected by the "
          "iPhone backup on the SSD instead.",
          "Optional: in WhatsApp → Settings → Chats → Chat Backup, turn off 'Include Videos' so the "
          "iCloud backup stays small after you downgrade; export any must-keep chats (chat → Export Chat)."),
    Check("iCloud device backups", MANUAL,
          "Once a recent iPhone backup is on the SSD, iCloud Backup is no longer needed.",
          "iPhone: Settings → [your name] → iCloud → iCloud Backup → turn off, then delete the old backup "
          "under Settings → [your name] → iCloud → Manage Account Storage → Backups."),
    Check("Not covered", MANUAL,
          "iCloud Mail, Notes, Contacts, Calendars, Reminders, Keychain and Shared Albums stay in iCloud "
          "(usually small). Check what's left in Settings → [your name] → iCloud → Manage Account Storage."),
    Check("Freeing iCloud space", MANUAL,
          "Only after this report says READY: delete photos/files from iCloud, then downgrade.",
          "Deleting from iCloud Photos or iCloud Drive deletes on ALL devices. Keep the SSD (and ideally a "
          "second copy of it) before deleting, and empty 'Recently Deleted' to actually free the space."),
]


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def build_report(
    loc: Locations,
    dest: Path,
    kinds: Sequence[str] = ALL_KINDS,
    photos_client: Optional[PhotosClient] = None,
) -> Report:
    dest = Path(dest).expanduser()
    report = Report()
    migrate_cmd = "Run iGround again to copy what's missing"

    if PHOTOS in kinds:
        report.checks.append(photos_check(layout.state_dir(dest, "Photos"), photos_client or PhotosClient(),
                                           migrate_cmd))

    for src in folder_sources(loc, kinds):
        diff = diff_against_source(src.path, src.dest(dest), state_dir=layout.state_dir(dest, src.state_key))
        if not diff.missing and not diff.changed:
            report.checks.append(Check(src.label, OK, f"{_plural(diff.on_ssd, 'file')} copied"))
        else:
            parts = []
            if diff.missing:
                parts.append(f"{_plural(len(diff.missing), 'file')} not copied yet (e.g. {diff.missing[0]})")
            if diff.changed:
                parts.append(f"{_plural(len(diff.changed), 'file')} changed since they were copied")
            report.checks.append(Check(src.label, MISSING, "; ".join(parts), migrate_cmd))

    report.checks.append(backup_check(loc, dest))
    report.checks.extend(MANUAL_STEPS)
    return report


def photos_check(state: Path, client: PhotosClient, migrate_cmd: str) -> Check:
    if not client.available:
        return Check("Photos", MISSING, "cannot read the Photos library (macOS + Photos.app required)")
    try:
        items = client.list_items()
    except PhotosError as exc:
        return Check("Photos", MISSING, f"could not read the Photos library: {exc}")
    exported = {}
    if mf.Manifest.exists(state):
        with mf.Manifest(state) as manifest:
            exported = manifest.photo_statuses()
    missing = [it for it in items if exported.get(it.id) != mf.EXPORTED]
    if not missing:
        return Check("Photos", OK, f"all {len(items):,} photos & videos copied")
    return Check("Photos", MISSING, f"{len(missing):,} of {len(items):,} photos & videos not copied yet",
                 migrate_cmd)


def backup_check(loc: Locations, dest: Path) -> Check:
    name = "iPhone backup (incl. WhatsApp)"
    setup = "Connect your iPhone, open Finder, select it and click 'Back Up Now'"
    backups = devicebackup.list_backups(devicebackup.ssd_backup_dir(dest))
    if not backups:
        return Check(name, MISSING, "no iPhone backup on the SSD yet", setup)
    now = datetime.now()
    lines, stale = [], False
    for b in backups:
        when = b.last_backup
        if when is not None and when.tzinfo is not None:
            when = when.astimezone().replace(tzinfo=None)
        age_ok = when is not None and now - when <= BACKUP_MAX_AGE
        stale |= not age_ok
        enc = {True: "encrypted", False: "NOT encrypted", None: "?"}[b.encrypted]
        lines.append(f"{b.device} — {when:%Y-%m-%d} ({enc})" if when else f"{b.device} — date unknown")
    detail = "; ".join(lines)
    if not devicebackup.is_relocated(loc, dest):
        detail += " — note: new iPhone backups are not set to go to this SSD"
    if stale:
        return Check(name, MISSING, detail + f"; older than {BACKUP_MAX_AGE.days} days",
                     "Connect each device and click 'Back Up Now' in Finder")
    return Check(name, OK, detail)
