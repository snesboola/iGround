"""Run a whole backup: every section into one dated folder on the SSD."""

from __future__ import annotations

import socket
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from . import devicebackup, layout
from . import manifest as mf
from .icloud import ICloudClient
from .migrate import MigrationError, Migrator, Options
from .photos import PhotosClient, PhotosError, PhotosExporter
from .scanner import DEFAULT_EXCLUDES, Summary, scan
from .ui import plural
from .sources import (
    ALL_KINDS, APPS, APPS_DIR, BACKUPS_DIR, DRIVE, DRIVE_DIR, MESSAGES, MESSAGES_DIR, PHOTOS,
    PHOTOS_DIR, Locations, folder_sources,
)

PHOTOS_STATE = "Photos"


@dataclass
class BackupOptions:
    kinds: Sequence[str] = ALL_KINDS
    workers: int = 4
    verify: bool = True
    evict_after: bool = False
    dry_run: bool = False
    force: bool = False
    download_timeout: float = 1800
    photos_batch: int = 25
    excludes: Sequence[str] = DEFAULT_EXCLUDES
    cancel: Optional[threading.Event] = None


@dataclass
class SectionResult:
    kind: str
    label: str
    folder: Path
    ok: bool
    summary: str
    errors: List[Tuple[str, str]] = field(default_factory=list)


class Reporter:
    """How a backup run talks to the user. The CLI and the wizard both subclass this."""

    def section(self, label: str, index: int, total: int, items: int, size: int, kind: str = "") -> Callable:
        """A section is starting; return the progress callback for it."""
        return lambda *_: None

    def section_done(self, result: SectionResult) -> None:
        pass


def summarize(path: Path, excludes: Sequence[str] = DEFAULT_EXCLUDES) -> Summary:
    s = Summary()
    for e in scan(path, excludes):
        s.add(e)
    return s


def prepare(loc: Locations, drive: Path, new: bool = False) -> layout.Opened:
    """Open (or create) the dated backup folder, keeping iPhone backups pointed at it."""
    opened = layout.open_backup(drive, new=new)
    if opened.previous is not None:
        devicebackup.repoint(loc, opened.previous, opened.root)
    return opened


def run_backup(
    loc: Locations,
    root: Path,
    opts: BackupOptions,
    reporter: Optional[Reporter] = None,
    photos_client: Optional[PhotosClient] = None,
    icloud_client: Optional[Callable[[bool], ICloudClient]] = None,
) -> List[SectionResult]:
    reporter = reporter or Reporter()
    make_icloud = icloud_client or (lambda managed: ICloudClient() if managed else ICloudClient(brctl=""))
    folders = folder_sources(loc, opts.kinds)
    total = len(folders) + (1 if PHOTOS in opts.kinds else 0)
    results: List[SectionResult] = []
    index = 0

    stopped = lambda: opts.cancel is not None and opts.cancel.is_set()

    if PHOTOS in opts.kinds:
        index += 1
        results.append(_photos(root, opts, reporter, index, total, photos_client or PhotosClient()))

    for src in folders:
        if stopped():
            break
        index += 1
        target = src.dest(root)
        pre = summarize(src.path, opts.excludes)
        progress = reporter.section(src.label, index, total, pre.files + pre.symlinks, pre.total_bytes,
                                    kind=src.kind)
        options = Options(
            workers=opts.workers, verify=opts.verify, evict_after=opts.evict_after and src.icloud_managed,
            dry_run=opts.dry_run, force=opts.force, download_timeout=opts.download_timeout,
            excludes=opts.excludes, cancel=opts.cancel,
        )
        try:
            res = Migrator(src.path, target, options, make_icloud(src.icloud_managed), progress,
                           state_dir=layout.state_dir(root, src.state_key)).run()
        except MigrationError as exc:
            result = SectionResult(src.kind, src.label, target, False, f"stopped: {exc}")
        else:
            if opts.dry_run:
                todo = res.planned.files + res.planned.symlinks - res.skipped
                text = f"{plural(todo, 'file')} to copy" + (
                    f" ({_size(res.planned.cloud_bytes)} to download from iCloud first)" if res.planned.cloud_bytes else "")
            else:
                done = res.copied + res.skipped
                text = f"{plural(done, 'file')} ({_size(res.planned.total_bytes)})"
                if res.failed:
                    text += f", {res.failed:,} could not be copied"
                if res.cancelled:
                    text = f"stopped — {plural(res.cancelled, 'file')} still to copy"
            result = SectionResult(src.kind, src.label, target, res.failed == 0 and not res.cancelled,
                                   text, res.errors)
        reporter.section_done(result)
        results.append(result)

    if not opts.dry_run:
        complete = all(r.ok for r in results) and set(opts.kinds) == set(ALL_KINDS) and not stopped()
        now = datetime.now().isoformat(timespec="seconds")
        changes = {"updated": now, "host": socket.gethostname()}
        if complete:
            changes["completed"] = now
        layout.write_info(root, **changes)
        write_about(loc, root)
    return results


def _photos(root: Path, opts: BackupOptions, reporter: Reporter, index: int, total: int,
            client: PhotosClient) -> SectionResult:
    target = root / PHOTOS_DIR
    if not client.available:
        reporter.section("Photos", index, total, 0, 0, kind=PHOTOS)
        result = SectionResult(PHOTOS, "Photos", target, False, "skipped: needs a Mac with the Photos app")
        reporter.section_done(result)
        return result
    try:
        items = client.list_items()
    except PhotosError as exc:
        reporter.section("Photos", index, total, 0, 0, kind=PHOTOS)
        result = SectionResult(PHOTOS, "Photos", target, False, f"could not open your Photos library: {exc}")
        reporter.section_done(result)
        return result

    progress = reporter.section("Photos", index, total, len(items), 0, kind=PHOTOS)
    exporter = PhotosExporter(target, _Fixed(client, items), batch_size=opts.photos_batch,
                              item_timeout=int(opts.download_timeout), progress=progress,
                              state_dir=layout.state_dir(root, PHOTOS_STATE), cancel=opts.cancel)
    try:
        res = exporter.run(dry_run=opts.dry_run)
    except PhotosError as exc:
        result = SectionResult(PHOTOS, "Photos", target, False, f"stopped: {exc}")
    else:
        if opts.dry_run:
            text = f"{res.total - res.skipped:,} of {res.total:,} photos & videos to copy"
        else:
            text = f"{res.exported + res.skipped:,} photos & videos"
            if res.failed:
                text += f", {res.failed:,} could not be copied"
            if not res.album_folders and res.total:
                text += " (albums are listed in .iground/Photos/albums.json; this SSD can't hold album folders)"
            if res.cancelled:
                text = f"stopped — {res.total - res.skipped - res.exported - res.failed:,} still to copy"
        result = SectionResult(PHOTOS, "Photos", target, res.failed == 0 and not res.cancelled, text, res.errors)
    reporter.section_done(result)
    return result


class _Fixed:
    """Wraps a PhotosClient so the library is listed once per run."""

    def __init__(self, client: PhotosClient, items):
        self._client, self._items = client, items

    def list_items(self):
        return list(self._items)

    def __getattr__(self, name):
        return getattr(self._client, name)


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


def _folder_totals(state: Path) -> Tuple[int, int]:
    if not mf.Manifest.exists(state):
        return 0, 0
    with mf.Manifest(state) as manifest:
        counts = manifest.counts()
    files = sum(counts.get(s, {}).get("files", 0) for s in mf.DONE_STATUSES)
    size = sum(counts.get(s, {}).get("bytes", 0) for s in mf.DONE_STATUSES)
    return files, size


def write_about(loc: Locations, root: Path) -> Path:
    """A plain-text guide at the top of the backup, for whoever opens the SSD later."""
    info = layout.read_info(root)
    updated = info.get("updated") or info.get("created") or ""
    try:
        when = datetime.fromisoformat(updated).strftime("%-d %B %Y at %H:%M")
    except ValueError:
        when = updated
    lines = [f"iCloud Backup — {when}", "", "What's in this folder", ""]

    state = layout.state_dir(root, PHOTOS_STATE)
    if mf.Manifest.exists(state):
        with mf.Manifest(state) as manifest:
            n = sum(1 for s in manifest.photo_statuses().values() if s == mf.EXPORTED)
        _, size = _folder_totals(state)
        lines += [f"  Photos            {n:,} photos & videos ({_size(size)}), in folders by year and month.",
                  "                    Your albums are in Photos/Albums and favourites in Photos/Favourites.",
                  "                    These are the full-quality originals; edits made in Photos aren't applied."]
    for kind, name, text in (
        (DRIVE, DRIVE_DIR, "exactly as they were in iCloud Drive (incl. Desktop & Documents)."),
        (APPS, APPS_DIR, "documents apps kept in iCloud (Pages, Numbers, …), one folder per app."),
        (MESSAGES, MESSAGES_DIR, "your Messages history, for restoring onto a Mac (open with Messages)."),
    ):
        files = size = 0
        for src in folder_sources(loc, [kind]):
            f, s = _folder_totals(layout.state_dir(root, src.state_key))
            files, size = files + f, size + s
        if files:
            lines.append(f"  {name:17} {plural(files, 'file')} ({_size(size)}) — {text}")

    backups = devicebackup.list_backups(root / BACKUPS_DIR)
    if backups:
        desc = ", ".join(f"{b.device} ({b.last_backup:%-d %b %Y})" if b.last_backup else b.device for b in backups)
        lines.append(f"  {BACKUPS_DIR:17} {desc} — includes WhatsApp. Restore by connecting the phone and "
                     "choosing 'Restore Backup…' in Finder.")
    else:
        lines.append(f"  {BACKUPS_DIR:17} none yet — run iGround and follow the iPhone step, so your WhatsApp "
                     "chats are kept too.")

    status = "Complete — every file was copied and checked." if info.get("completed") == updated and updated \
        else "Not finished yet — run iGround again to complete it."
    lines += ["", f"Status: {status}", "",
              "Keeping it up to date",
              "  Run iGround again at any time. Only new and changed items are copied, and this folder is",
              "  renamed to the date of the latest update. Please don't edit files inside this folder.",
              "",
              "Freeing up iCloud space (only once iGround says everything is safely copied)",
              "  Deleting from iCloud removes things from ALL your devices. Ideally keep a second copy of",
              "  this SSD first.",
              "  1. iPhone backups: Settings → [your name] → iCloud → iCloud Backup → turn off, then delete",
              "     the old backup in Settings → [your name] → iCloud → Manage Account Storage → Backups.",
              "  2. Photos: delete them from iCloud Photos (or Manage Account Storage → Photos → Turn Off &",
              "     Delete), then empty “Recently Deleted”.",
              "  3. iCloud Drive: delete what you don't need there, and empty “Recently Deleted”.",
              "  4. WhatsApp: Settings → Chats → Chat Backup → turn off “Include Videos” (or the backup).",
              "  5. Check Manage Account Storage, then choose a smaller plan.",
              ""]
    path = root / layout.ABOUT_FILE
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def backup_sections(root: Path, loc: Locations):
    """(label, folder on SSD, source folder or None, state dir, kind) for each section stored in a backup."""
    sources = {src.state_key: src for src in folder_sources(loc)}
    state_root = Path(root) / layout.STATE_DIR
    found = []
    if state_root.is_dir():
        for state in sorted(state_root.iterdir()):
            if not mf.Manifest.exists(state):
                continue
            src = sources.get(state.name)
            rel = state.name.replace("--", "/")
            if src is not None:
                kind = src.kind
            elif state.name == PHOTOS_STATE:
                kind = PHOTOS
            elif rel.startswith(APPS_DIR + "/"):
                kind = APPS
            elif rel == DRIVE_DIR:
                kind = DRIVE
            else:
                kind = MESSAGES
            found.append((src.label if src else rel, Path(root) / rel, src.path if src else None, state, kind))
    return found
