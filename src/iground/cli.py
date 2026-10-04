"""Command-line interface.

    iground audit   [DEST]          what's in iCloud, and what it will take
    iground migrate DEST            copy everything (Drive, app folders, Photos, Messages)
    iground iphone-backup DEST      make Finder back up iPhones (incl. WhatsApp) to the SSD
    iground ready   DEST            is it safe to downgrade iCloud storage?
    iground verify  DEST            re-check every checksum on the SSD
    iground status  DEST            progress and failures
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional, TextIO

from . import __version__, devicebackup, readiness
from . import manifest as mf
from .icloud import ICloudClient
from .migrate import MigrationError, Migrator, Options, verify
from .photos import PhotosClient, PhotosError, PhotosExporter
from .scanner import DEFAULT_EXCLUDES, Summary, scan
from .sources import (
    ALL_KINDS, APPS_DIR, DRIVE_DIR, MESSAGES, MESSAGES_DIR, PHOTOS, PHOTOS_DIR, Locations, folder_sources, parse_kinds,
)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


class ProgressPrinter:
    """Single-line progress on a TTY, plain log lines otherwise. Works for files and photos."""

    def __init__(self, total_items: int, total_bytes: int = 0, stream: TextIO = sys.stderr, verbose: bool = False):
        self.total_items = total_items
        self.total_bytes = total_bytes
        self.stream = stream
        self.verbose = verbose
        self.tty = stream.isatty()
        self.done_items = 0
        self.done_bytes = 0
        self.start = time.monotonic()
        self._last = 0.0
        self._lock = threading.Lock()

    def __call__(self, event: str, item, detail: str) -> None:
        name = getattr(item, "rel_path", None) or getattr(item, "filename", None) or getattr(item, "id", "?")
        size = getattr(item, "size", 0)
        with self._lock:
            if event in ("copied", "skipped", "failed", "exported"):
                self.done_items += 1
                self.done_bytes += size
            if event == "failed":
                self._line(f"FAILED  {name}: {detail}")
            elif event == "planned":
                tag = "download+copy" if getattr(item, "needs_download", False) else "copy"
                self._line(f"  {tag:14} {human(size):>9}  {name}" if size else f"  export          {name}")
            elif self.verbose and event in ("copied", "download", "exported"):
                self._line(f"  {event:8} {name}")
            self._status()

    def _line(self, text: str) -> None:
        if self.tty:
            self.stream.write("\r\033[K")
        self.stream.write(text + "\n")

    def _status(self) -> None:
        if not self.tty:
            return
        now = time.monotonic()
        if now - self._last < 0.2:
            return
        self._last = now
        line = f"\r\033[K  {self.done_items:,}/{self.total_items:,}"
        if self.total_bytes:
            rate = self.done_bytes / max(now - self.start, 1e-6)
            pct = 100.0 * self.done_bytes / self.total_bytes
            line += f"  {human(self.done_bytes)}/{human(self.total_bytes)} ({pct:.0f}%)  {human(rate)}/s"
        self.stream.write(line)
        self.stream.flush()

    def finish(self) -> None:
        if self.tty:
            self.stream.write("\r\033[K")
            self.stream.flush()


def summarize(path: Path, excludes) -> Summary:
    s = Summary()
    for e in scan(path, excludes):
        s.add(e)
    return s


def messages_running(runner=subprocess.run) -> bool:
    if not shutil.which("pgrep"):
        return False
    return runner(["pgrep", "-x", "Messages"], capture_output=True).returncode == 0


# --- commands ----------------------------------------------------------------


def cmd_audit(args: argparse.Namespace, out: TextIO) -> int:
    loc = Locations.default()
    kinds = parse_kinds(args.only)
    total = cloud = 0
    out.write("What iGround will move off iCloud\n\n")
    for src in folder_sources(loc, kinds):
        s = summarize(src.path, args.exclude)
        total += s.total_bytes
        cloud += s.cloud_bytes
        extra = f", {human(s.cloud_bytes)} only in iCloud (will be downloaded)" if s.cloud_bytes else ""
        out.write(f"  {src.label:40} {s.files:>8,} files  {human(s.total_bytes):>9}{extra}\n")
    if PHOTOS in kinds:
        client = PhotosClient()
        if not client.available:
            out.write("  Photos                                   (needs macOS; skipped)\n")
        else:
            try:
                n = len(client.list_items())
                out.write(f"  {'Photos library':40} {n:>8,} items  (originals downloaded from iCloud during export)\n")
            except PhotosError as exc:
                out.write(f"  Photos: could not read library: {exc}\n")
    out.write(f"\n  Files total: {human(total)} ({human(cloud)} to download first). Add your Photos library size on top.\n")

    backups = devicebackup.list_backups(loc.mobilesync_backup)
    out.write(f"\niPhone/iPad backups on this Mac ({loc.mobilesync_backup}): ")
    out.write(", ".join(f"{b.device} ({b.last_backup:%Y-%m-%d})" if b.last_backup else b.device for b in backups)
              or "none")
    out.write("\n  WhatsApp chats are only safe off-iCloud inside such a backup — see `iground iphone-backup`.\n")

    if args.dest:
        dest = Path(args.dest).expanduser()
        if dest.exists():
            free = shutil.disk_usage(dest).free
            verdict = "enough for the files" if free > total else "NOT enough even for the files"
            out.write(f"\nSSD free space: {human(free)} — {verdict}.\n")
    return 0


def cmd_migrate(args: argparse.Namespace, out: TextIO) -> int:
    loc = Locations.default()
    kinds = parse_kinds(args.only)
    dest = Path(args.dest).expanduser()
    if not args.dry_run and not dest.parent.exists():
        raise MigrationError(f"{dest.parent} does not exist — is the SSD connected?")
    failed = 0

    if MESSAGES in kinds and not args.dry_run and messages_running():
        out.write("Note: Messages is open. Quit it for a consistent copy of your message database.\n")

    for src in folder_sources(loc, kinds):
        target = src.dest(dest)
        out.write(f"\n== {src.label} -> {target}\n")
        pre = summarize(src.path, args.exclude)
        printer = ProgressPrinter(pre.files + pre.symlinks, pre.total_bytes, verbose=args.verbose)
        opts = Options(
            workers=args.workers,
            verify=not args.no_verify,
            evict_after=args.evict_after and src.icloud_managed,
            dry_run=args.dry_run,
            force=args.force,
            download_timeout=args.download_timeout,
            excludes=args.exclude,
        )
        client = ICloudClient() if src.icloud_managed else ICloudClient(brctl="")
        try:
            result = Migrator(src.path, target, opts, client, printer).run()
        except MigrationError as exc:
            printer.finish()
            out.write(f"  skipped: {exc}\n")
            failed += 1
            continue
        printer.finish()
        if args.dry_run:
            todo = result.planned.files + result.planned.symlinks - result.skipped
            out.write(f"  would copy {todo:,} item(s) ({human(result.planned.cloud_bytes)} to download first), "
                      f"{result.skipped:,} already done\n")
            continue
        out.write(f"  copied {result.copied:,} ({human(result.bytes_copied)}), "
                  f"already done {result.skipped:,}, failed {result.failed:,}")
        if opts.evict_after:
            out.write(f", freed {result.evicted:,} from this Mac")
        out.write("\n")
        for rel, msg in result.errors[:20]:
            out.write(f"    {rel}: {msg}\n")
        failed += result.failed

    if PHOTOS in kinds:
        target = dest / PHOTOS_DIR
        out.write(f"\n== Photos library -> {target}\n")
        client = PhotosClient()
        if not client.available:
            out.write("  skipped: exporting Photos requires macOS\n")
            failed += 1
        else:
            printer = ProgressPrinter(0, verbose=args.verbose)
            exporter = PhotosExporter(target, client, batch_size=args.photos_batch,
                                      item_timeout=int(args.download_timeout), progress=printer)
            try:
                res = exporter.run(dry_run=args.dry_run)
            except PhotosError as exc:
                printer.finish()
                out.write(f"  stopped: {exc}\n")
                failed += 1
            else:
                printer.finish()
                if args.dry_run:
                    out.write(f"  would export {res.total - res.skipped:,} of {res.total:,} items "
                              f"({res.skipped:,} already done)\n")
                else:
                    out.write(f"  exported {res.exported:,} item(s) as {res.files:,} file(s) ({human(res.bytes)}), "
                              f"already done {res.skipped:,}, failed {res.failed:,}\n")
                    if not res.albums_saved:
                        out.write("  note: album list could not be read; albums.json has favourites only\n")
                    for name, msg in res.errors[:20]:
                        out.write(f"    {name}: {msg}\n")
                    failed += res.failed

    if args.dry_run:
        out.write("\nDry run — nothing was changed.\n")
        return 0
    if failed:
        out.write("\nSome items failed. Re-run the same command to retry them (finished items are skipped).\n")
        return 1
    out.write(f'\nDone. Next: `iground iphone-backup "{dest}"`, then `iground ready "{dest}"`.\n')
    return 0


def cmd_iphone_backup(args: argparse.Namespace, out: TextIO) -> int:
    loc = Locations.default()
    if args.undo:
        out.write(devicebackup.undo(loc) + "\n")
        return 0
    out.write(devicebackup.relocate(loc, Path(args.dest).expanduser()) + "\n")
    out.write(
        "\nNow back up each iPhone/iPad (this is what keeps your WhatsApp chats safe):\n"
        "  1. Connect the device to this Mac with the SSD plugged in, open Finder and select the device.\n"
        "  2. Choose 'Back up all of the data on your iPhone to this Mac'.\n"
        "  3. Tick 'Encrypt local backup' (needed for passwords, Health and Wi-Fi data; remember the password!).\n"
        "  4. Click 'Back Up Now'. Repeat regularly — keep the SSD connected while backing up.\n"
    )
    return 0


def cmd_ready(args: argparse.Namespace, out: TextIO) -> int:
    loc = Locations.default()
    report = readiness.build_report(loc, Path(args.dest), parse_kinds(args.only))
    icons = {readiness.OK: "[OK]  ", readiness.MISSING: "[TODO]", readiness.MANUAL: "[NOTE]"}
    for c in report.checks:
        out.write(f"{icons[c.status]} {c.name}: {c.detail}\n")
        if c.fix:
            out.write(f"         → {c.fix}\n")
    if report.ready:
        out.write("\nREADY: everything iGround can move is on the SSD. Follow the notes above, then downgrade.\n")
        return 0
    out.write("\nNOT READY: finish the [TODO] items before deleting anything from iCloud.\n")
    return 1


def _targets(dest: Path, loc: Locations):
    """(label, ssd folder, source folder or None) for everything migrated under DEST."""
    sources = {src.dest(dest): src for src in folder_sources(loc)}
    found = []
    candidates = [dest / DRIVE_DIR, dest / PHOTOS_DIR, dest / MESSAGES_DIR]
    if (dest / APPS_DIR).is_dir():
        candidates += sorted(p for p in (dest / APPS_DIR).iterdir() if p.is_dir())
    for path in candidates:
        if mf.Manifest.exists(path):
            src = sources.get(path)
            found.append((src.label if src else path.relative_to(dest).as_posix(), path,
                          src.path if src else None))
    return found


def cmd_verify(args: argparse.Namespace, out: TextIO) -> int:
    dest = Path(args.dest).expanduser()
    targets = _targets(dest, Locations.default())
    if not targets:
        raise MigrationError(f"nothing migrated found in {dest}")
    bad = 0
    for label, path, source in targets:
        res = verify(path, None if args.no_source else source, args.exclude)
        problems = len(res.mismatched) + len(res.missing_on_dest) + len(res.not_migrated)
        bad += problems
        out.write(f"{label}: {res.ok:,} OK, {len(res.mismatched):,} corrupted, "
                  f"{len(res.missing_on_dest):,} missing on SSD, {len(res.not_migrated):,} not yet migrated\n")
        for tag, items in (("CORRUPTED", res.mismatched), ("MISSING", res.missing_on_dest),
                           ("NOT MIGRATED", res.not_migrated)):
            for rel in items[:20]:
                out.write(f"    {tag}: {rel}\n")
    if bad:
        out.write("\nRe-run `iground migrate` to repair: corrupted and missing files are copied again.\n")
    return 1 if bad else 0


def cmd_status(args: argparse.Namespace, out: TextIO) -> int:
    dest = Path(args.dest).expanduser()
    targets = _targets(dest, Locations.default())
    if not targets:
        raise MigrationError(f"nothing migrated found in {dest}")
    for label, path, _ in targets:
        with mf.Manifest(path) as manifest:
            counts = manifest.counts()
            photo_statuses = manifest.photo_statuses()
            out.write(f"{label}\n")
            if photo_statuses:
                exported = sum(1 for s in photo_statuses.values() if s == mf.EXPORTED)
                out.write(f"  {exported:,} items exported, {len(photo_statuses) - exported:,} failed\n")
            for status in (mf.VERIFIED, mf.COPIED, mf.EVICTED, mf.FAILED):
                if status in counts:
                    c = counts[status]
                    out.write(f"  {status:9} {c['files']:>8,} files  {human(c['bytes']):>10}\n")
            if args.failed:
                for rec in manifest.records(mf.FAILED):
                    out.write(f"    {rec.rel_path}: {rec.error}\n")
                for pid, err in manifest.photo_errors():
                    out.write(f"    photo {pid}: {err}\n")
    return 0


# --- argument parsing --------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="iground",
        description="Move everything off iCloud onto an external SSD so you can downgrade your iCloud plan.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    only_help = f"comma-separated subset of: {', '.join(ALL_KINDS)} (default: all)"

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--only", metavar="KINDS", help=only_help)
        sp.add_argument("--exclude", action="append", default=list(DEFAULT_EXCLUDES), metavar="GLOB",
                        help="skip files/folders matching GLOB (repeatable)")

    sp = sub.add_parser("audit", help="show what's in iCloud and what migrating it involves")
    sp.add_argument("dest", nargs="?", help="optional SSD folder, to compare free space")
    add_common(sp)
    sp.set_defaults(func=cmd_audit)

    sp = sub.add_parser("migrate", help="copy iCloud Drive, app folders, Photos and Messages to the SSD")
    sp.add_argument("dest", help="folder on the SSD, e.g. /Volumes/MySSD/iCloud")
    add_common(sp)
    sp.add_argument("--dry-run", action="store_true", help="list what would happen; change nothing")
    sp.add_argument("--workers", type=int, default=4, help="parallel file downloads/copies (default 4)")
    sp.add_argument("--no-verify", action="store_true", help="skip re-reading each copy to check its checksum")
    sp.add_argument("--evict-after", action="store_true",
                    help="after a verified copy, remove the iCloud Drive download from this Mac (stays in iCloud)")
    sp.add_argument("--download-timeout", type=float, default=1800, metavar="SECONDS",
                    help="max wait per file/photo for iCloud to download it (default 1800)")
    sp.add_argument("--photos-batch", type=int, default=25, metavar="N",
                    help="photos exported per Photos.app call (default 25)")
    sp.add_argument("--force", action="store_true", help="proceed even if the SSD looks too small")
    sp.add_argument("-v", "--verbose", action="store_true", help="log every file")
    sp.set_defaults(func=cmd_migrate)

    sp = sub.add_parser("iphone-backup",
                        help="make Finder back up iPhones/iPads (incl. WhatsApp) to the SSD instead of iCloud")
    sp.add_argument("dest", nargs="?", default="", help="the same SSD folder used for `migrate`")
    sp.add_argument("--undo", action="store_true", help="point Finder back at the Mac's own backup folder")
    sp.set_defaults(func=cmd_iphone_backup)

    sp = sub.add_parser("ready", help="check whether it's safe to downgrade your iCloud storage")
    sp.add_argument("dest")
    sp.add_argument("--only", metavar="KINDS", help=only_help)
    sp.set_defaults(func=cmd_ready)

    sp = sub.add_parser("verify", help="re-check every file on the SSD against its recorded checksum")
    sp.add_argument("dest")
    sp.add_argument("--exclude", action="append", default=list(DEFAULT_EXCLUDES), metavar="GLOB")
    sp.add_argument("--no-source", action="store_true", help="don't look for files not yet migrated")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("status", help="show migration progress recorded on the SSD")
    sp.add_argument("dest")
    sp.add_argument("--failed", action="store_true", help="list failed items with their errors")
    sp.set_defaults(func=cmd_status)
    return p


def main(argv: Optional[List[str]] = None, out: TextIO = sys.stdout) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "iphone-backup" and not args.undo and not args.dest:
        parser.error("iphone-backup needs the SSD folder (or --undo)")
    try:
        return args.func(args, out)
    except (MigrationError, PhotosError, ValueError) as exc:
        sys.stderr.write(f"iground: error: {exc}\n")
        return 2
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted. Re-run the same command to resume where it stopped.\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
