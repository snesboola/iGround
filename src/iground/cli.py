"""Command line.

    iground                      open the iGround app (same as double-clicking iGround.command)
    iground guided               the same journey as questions in the terminal

    iground backup  DRIVE        copy everything into DRIVE/iCloud Backup YYYY-MM-DD
    iground ready   DRIVE        is it safe to downgrade iCloud storage?
    iground iphone-backup DRIVE  keep iPhone backups (incl. WhatsApp) on the SSD
    iground audit  [DRIVE]       what's in iCloud and how big it is
    iground verify  DRIVE        re-check every file's checksum
    iground status  DRIVE        progress and failures

DRIVE can be the SSD itself (the newest backup on it is used) or a backup folder.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import List, Optional, TextIO

from . import __version__, devicebackup, layout, readiness
from . import manifest as mf
from .backup import BackupOptions, Reporter, SectionResult, backup_sections, prepare, run_backup, summarize
from .migrate import MigrationError, verify
from .photos import PhotosClient, PhotosError
from .scanner import DEFAULT_EXCLUDES
from .sources import ALL_KINDS, PHOTOS, Locations, folder_sources, parse_kinds
from .ui import Console, ProgressPrinter, human
from .wizard import Wizard


class CLIReporter(Reporter):
    def __init__(self, out: TextIO, verbose: bool):
        self.out, self.verbose = out, verbose
        self.printer: Optional[ProgressPrinter] = None

    def section(self, label, index, total, items, size, kind=""):
        self.out.write(f"\n[{index}/{total}] {label}\n")
        self.printer = ProgressPrinter(items, size, verbose=self.verbose)
        return self.printer

    def section_done(self, result: SectionResult) -> None:
        if self.printer:
            self.printer.finish()
        self.out.write(f"  {'✓' if result.ok else '✗'} {result.summary}\n")
        for name, msg in result.errors[:20]:
            self.out.write(f"    {name}: {msg}\n")


def resolve_backup(path: str) -> Path:
    try:
        return layout.resolve(Path(path))
    except FileNotFoundError as exc:
        raise MigrationError(str(exc)) from exc


def cmd_backup(args: argparse.Namespace, out: TextIO) -> int:
    loc = Locations.default()
    drive = Path(args.drive).expanduser()
    if layout.is_backup(drive):
        drive = drive.parent
    if not drive.is_dir():
        raise MigrationError(f"{drive} not found — is the SSD plugged in?")
    opts = BackupOptions(
        kinds=parse_kinds(args.only), workers=args.workers, verify=not args.no_verify,
        evict_after=args.evict_after, dry_run=args.dry_run, force=args.force,
        download_timeout=args.download_timeout, photos_batch=args.photos_batch, excludes=args.exclude,
    )
    if args.dry_run:
        existing = layout.find_backups(drive)
        root = existing[-1] if existing and not args.new else drive / "(new backup)"
        out.write(f"Dry run for {root} — nothing will be changed.\n")
    else:
        root = prepare(loc, drive, new=args.new).root
        out.write(f"Backing up to {root}\n")
    results = run_backup(loc, root, opts, CLIReporter(out, args.verbose))
    if args.dry_run:
        return 0
    if all(r.ok for r in results):
        out.write(f'\nDone. Check with: iground ready "{drive}"\n')
        return 0
    out.write("\nSome items weren't copied. Run the same command again to retry them.\n")
    return 1


def cmd_audit(args: argparse.Namespace, out: TextIO) -> int:
    loc = Locations.default()
    kinds = parse_kinds(args.only)
    total = cloud = 0
    out.write("What's in your iCloud\n\n")
    if PHOTOS in kinds:
        client = PhotosClient()
        if not client.available:
            out.write("  Photos: needs a Mac with the Photos app\n")
        else:
            try:
                out.write(f"  {'Photos':32} {len(client.list_items()):>8,} photos & videos\n")
            except PhotosError as exc:
                out.write(f"  Photos: could not open the library: {exc}\n")
    for src in folder_sources(loc, kinds):
        s = summarize(src.path, args.exclude)
        total += s.total_bytes
        cloud += s.cloud_bytes
        extra = f"  ({human(s.cloud_bytes)} only in iCloud)" if s.cloud_bytes else ""
        out.write(f"  {src.label:32} {s.files:>8,} files  {human(s.total_bytes):>9}{extra}\n")
    out.write(f"\n  Files: {human(total)}, of which {human(cloud)} must be downloaded first (photos come on top).\n")
    backups = devicebackup.list_backups(loc.mobilesync_backup)
    out.write("  iPhone backups on this Mac: " + (", ".join(
        f"{b.device} ({b.last_backup:%Y-%m-%d})" if b.last_backup else b.device for b in backups) or "none") + "\n")
    if args.drive:
        drive = Path(args.drive).expanduser()
        if drive.exists():
            free = shutil.disk_usage(drive).free
            verdict = "enough for the files" if free > total else "NOT enough even for the files"
            out.write(f"\n  SSD free space: {human(free)} — {verdict}.\n")
    return 0


def cmd_iphone_backup(args: argparse.Namespace, out: TextIO) -> int:
    loc = Locations.default()
    if args.undo:
        out.write(devicebackup.undo(loc) + "\n")
        return 0
    drive = Path(args.drive).expanduser()
    root = layout.resolve(drive) if (layout.is_backup(drive) or layout.find_backups(drive)) \
        else prepare(loc, drive).root
    out.write(devicebackup.relocate(loc, root) + "\n")
    out.write(
        "\nNow back up your iPhone:\n"
        "  1. Connect it to this Mac (SSD plugged in) and select it in the Finder sidebar.\n"
        "  2. Choose 'Back up all of the data on your iPhone to this Mac'.\n"
        "  3. Tick 'Encrypt local backup' and pick a password you'll remember.\n"
        "  4. Click 'Back Up Now'.\n"
    )
    return 0


def cmd_ready(args: argparse.Namespace, out: TextIO) -> int:
    root = resolve_backup(args.drive)
    report = readiness.build_report(Locations.default(), root, parse_kinds(args.only))
    out.write(f"{root}\n\n")
    marks = {readiness.OK: "✓", readiness.MISSING: "✗", readiness.MANUAL: "·"}
    for c in report.checks:
        out.write(f"{marks[c.status]} {c.name}: {c.detail}\n")
        if c.fix:
            out.write(f"    → {c.fix}\n")
    if report.ready:
        out.write("\nREADY: everything is on the SSD. Follow the notes above, then downgrade.\n")
        return 0
    out.write("\nNOT READY: finish the ✗ items before deleting anything from iCloud.\n")
    return 1


def cmd_verify(args: argparse.Namespace, out: TextIO) -> int:
    root = resolve_backup(args.drive)
    bad = 0
    for label, folder, source, state, _ in backup_sections(root, Locations.default()):
        res = verify(folder, None if args.no_source else source, args.exclude, state_dir=state)
        problems = len(res.mismatched) + len(res.missing_on_dest) + len(res.not_migrated)
        bad += problems
        out.write(f"{'✓' if not problems else '✗'} {label}: {res.ok:,} OK, {len(res.mismatched):,} damaged, "
                  f"{len(res.missing_on_dest):,} missing, {len(res.not_migrated):,} not copied yet\n")
        for tag, items in (("damaged", res.mismatched), ("missing", res.missing_on_dest),
                           ("not copied", res.not_migrated)):
            for rel in items[:20]:
                out.write(f"    {tag}: {rel}\n")
    if bad:
        out.write("\nRun iGround again to repair: damaged and missing files are copied again.\n")
    return 1 if bad else 0


def cmd_status(args: argparse.Namespace, out: TextIO) -> int:
    root = resolve_backup(args.drive)
    info = layout.read_info(root)
    out.write(f"{root}\n  last updated: {info.get('updated', 'never')}"
              f"{'  (complete)' if info.get('completed') and info.get('completed') == info.get('updated') else ''}\n")
    for label, _, _, state, _ in backup_sections(root, Locations.default()):
        with mf.Manifest(state) as manifest:
            counts = manifest.counts()
            photos = manifest.photo_statuses()
            done = sum(counts.get(s, {}).get("files", 0) for s in mf.DONE_STATUSES)
            size = sum(counts.get(s, {}).get("bytes", 0) for s in mf.DONE_STATUSES)
            failed = counts.get(mf.FAILED, {}).get("files", 0)
            if photos:
                exported = sum(1 for s in photos.values() if s == mf.EXPORTED)
                failed = len(photos) - exported
                out.write(f"  {label:32} {exported:>8,} photos & videos  {human(size):>9}")
            else:
                out.write(f"  {label:32} {done:>8,} files            {human(size):>9}")
            out.write(f"   {failed:,} failed\n" if failed else "\n")
            if args.failed:
                for rec in manifest.records(mf.FAILED):
                    out.write(f"      {rec.rel_path}: {rec.error}\n")
                for pid, err in manifest.photo_errors():
                    out.write(f"      photo {pid}: {err}\n")
    return 0


def cmd_app(args: argparse.Namespace, out: TextIO) -> int:
    from .app.server import serve

    serve(port=args.port, open_browser=not args.no_browser)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="iground",
        description="Move everything from iCloud to an external SSD so you can downgrade your iCloud plan. "
                    "Run with no arguments for the guided version.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("app", help="open the iGround app window (the default)")
    sp.add_argument("--port", type=int, default=0, help=argparse.SUPPRESS)
    sp.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    sp.set_defaults(func=cmd_app)

    sp = sub.add_parser("guided", help="step-by-step backup with questions in the terminal")
    sp.set_defaults(func=lambda args, out: Wizard(Console(out)).start())
    only_help = f"comma-separated subset of: {', '.join(ALL_KINDS)} (default: all)"
    drive_help = "the SSD (e.g. /Volumes/MySSD) or a backup folder on it"

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--only", metavar="KINDS", help=only_help)
        sp.add_argument("--exclude", action="append", default=list(DEFAULT_EXCLUDES), metavar="GLOB",
                        help="skip files/folders matching GLOB (repeatable)")

    sp = sub.add_parser("backup", aliases=["migrate"], help="copy everything into a dated folder on the SSD")
    sp.add_argument("drive", help=drive_help)
    add_common(sp)
    sp.add_argument("--new", action="store_true",
                    help="start a separate full backup instead of updating the latest one")
    sp.add_argument("--dry-run", action="store_true", help="show what would be copied; change nothing")
    sp.add_argument("--workers", type=int, default=4, help="parallel file copies (default 4)")
    sp.add_argument("--no-verify", action="store_true", help="skip re-reading each copy to check it")
    sp.add_argument("--evict-after", action="store_true",
                    help="after copying, remove iCloud Drive downloads from this Mac (they stay in iCloud)")
    sp.add_argument("--download-timeout", type=float, default=1800, metavar="SECONDS",
                    help="max wait per file/photo for iCloud to download it (default 1800)")
    sp.add_argument("--photos-batch", type=int, default=25, metavar="N", help=argparse.SUPPRESS)
    sp.add_argument("--force", action="store_true", help="proceed even if the SSD looks too small")
    sp.add_argument("-v", "--verbose", action="store_true", help="log every file")
    sp.set_defaults(func=cmd_backup)

    sp = sub.add_parser("ready", help="check whether it's safe to downgrade your iCloud storage")
    sp.add_argument("drive", help=drive_help)
    sp.add_argument("--only", metavar="KINDS", help=only_help)
    sp.set_defaults(func=cmd_ready)

    sp = sub.add_parser("iphone-backup", help="keep iPhone backups (incl. WhatsApp) on the SSD")
    sp.add_argument("drive", nargs="?", default="", help=drive_help)
    sp.add_argument("--undo", action="store_true", help="put iPhone backups back on the Mac")
    sp.set_defaults(func=cmd_iphone_backup)

    sp = sub.add_parser("audit", help="show what's in iCloud and how big it is")
    sp.add_argument("drive", nargs="?", help="optional SSD, to compare free space")
    add_common(sp)
    sp.set_defaults(func=cmd_audit)

    sp = sub.add_parser("verify", help="re-check every file in a backup against its checksum")
    sp.add_argument("drive", help=drive_help)
    sp.add_argument("--exclude", action="append", default=list(DEFAULT_EXCLUDES), metavar="GLOB")
    sp.add_argument("--no-source", action="store_true", help="don't look for files not yet copied")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("status", help="show what a backup contains")
    sp.add_argument("drive", help=drive_help)
    sp.add_argument("--failed", action="store_true", help="list items that failed, with reasons")
    sp.set_defaults(func=cmd_status)
    return p


def main(argv: Optional[List[str]] = None, out: TextIO = sys.stdout) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command is None:
            args = parser.parse_args(["app"])
        if args.command == "iphone-backup" and not args.undo and not args.drive:
            parser.error("iphone-backup needs the SSD (or --undo)")
        return args.func(args, out)
    except (MigrationError, PhotosError, ValueError) as exc:
        sys.stderr.write(f"iground: {exc}\n")
        return 2
    except KeyboardInterrupt:
        sys.stderr.write("\nStopped. Run iGround again to carry on where it left off.\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
