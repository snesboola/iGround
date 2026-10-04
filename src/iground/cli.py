"""Command-line interface: `iground scan | migrate | verify | status`."""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional, TextIO

from . import __version__
from . import manifest as mf
from .icloud import ICLOUD_DRIVE, ICloudClient
from .migrate import MigrationError, Migrator, Options, verify
from .scanner import DEFAULT_EXCLUDES, Entry, Summary, scan


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


class ProgressPrinter:
    """Single-line progress on a TTY, plain log lines otherwise."""

    def __init__(self, total_files: int, total_bytes: int, stream: TextIO = sys.stderr, verbose: bool = False):
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.stream = stream
        self.verbose = verbose
        self.tty = stream.isatty()
        self.done_files = 0
        self.done_bytes = 0
        self.start = time.monotonic()
        self._last = 0.0
        self._lock = threading.Lock()

    def __call__(self, event: str, entry: Entry, detail: str) -> None:
        with self._lock:
            if event in ("copied", "skipped", "failed"):
                self.done_files += 1
                self.done_bytes += entry.size
            if event == "failed":
                self._line(f"FAILED  {entry.rel_path}: {detail}")
            elif event == "planned":
                tag = "download+copy" if entry.needs_download else "copy"
                self._line(f"{tag:14} {human(entry.size):>9}  {entry.rel_path}")
            elif self.verbose and event in ("copied", "download"):
                self._line(f"{event:8} {entry.rel_path}")
            self._status()

    def _line(self, text: str) -> None:
        if self.tty:
            self.stream.write("\r\033[K")
        self.stream.write(text + "\n")

    def _status(self, force: bool = False) -> None:
        if not self.tty:
            return
        now = time.monotonic()
        if not force and now - self._last < 0.2:
            return
        self._last = now
        rate = self.done_bytes / max(now - self.start, 1e-6)
        pct = 100.0 * self.done_bytes / self.total_bytes if self.total_bytes else 100.0
        self.stream.write(
            f"\r\033[K{self.done_files}/{self.total_files} files  "
            f"{human(self.done_bytes)}/{human(self.total_bytes)} ({pct:.0f}%)  {human(rate)}/s"
        )
        self.stream.flush()

    def finish(self) -> None:
        if self.tty:
            self.stream.write("\r\033[K")
            self.stream.flush()


def print_summary(s: Summary, out: TextIO) -> None:
    out.write(f"  files:            {s.files:,} ({human(s.total_bytes)})\n")
    out.write(f"  already on Mac:   {s.local_files:,} ({human(s.local_bytes)})\n")
    out.write(f"  iCloud-only:      {s.cloud_files:,} ({human(s.cloud_bytes)}) — will be downloaded\n")
    if s.symlinks:
        out.write(f"  symlinks:         {s.symlinks:,}\n")


def cmd_scan(args: argparse.Namespace, out: TextIO) -> int:
    source = Path(args.source).expanduser()
    if not source.is_dir():
        raise MigrationError(f"source folder not found: {source}")
    summary = Summary()
    top: dict = {}
    for e in scan(source, args.exclude):
        summary.add(e)
        head = e.rel_path.split("/", 1)[0] if "/" in e.rel_path else "(top level files)"
        top[head] = top.get(head, 0) + e.size
    out.write(f"Source: {source}\n")
    print_summary(summary, out)
    if top:
        out.write("\nLargest top-level items:\n")
        for name, size in sorted(top.items(), key=lambda kv: -kv[1])[: args.top]:
            out.write(f"  {human(size):>9}  {name}\n")
    return 0


def cmd_migrate(args: argparse.Namespace, out: TextIO) -> int:
    opts = Options(
        workers=args.workers,
        verify=not args.no_verify,
        evict_after=args.evict_after,
        dry_run=args.dry_run,
        force=args.force,
        download_timeout=args.download_timeout,
        excludes=args.exclude,
    )
    source = Path(args.source).expanduser()
    # Pre-scan only to size the progress bar; the migrator rescans for accuracy.
    pre = Summary()
    if source.is_dir():
        for e in scan(source, args.exclude):
            pre.add(e)
    printer = ProgressPrinter(pre.files + pre.symlinks, pre.total_bytes, verbose=args.verbose)
    migrator = Migrator(source, Path(args.dest), opts, ICloudClient(), printer)
    out.write(f"{'Planning' if args.dry_run else 'Migrating'} {migrator.source} -> {migrator.dest}\n")
    try:
        result = migrator.run()
    except KeyboardInterrupt:
        printer.finish()
        out.write("\nInterrupted. Re-run the same command to resume where it stopped.\n")
        return 130
    printer.finish()

    print_summary(result.planned, out)
    if args.dry_run:
        out.write(f"\nDry run: {result.planned.files + result.planned.symlinks - result.skipped:,} item(s) "
                  f"would be migrated, {result.skipped:,} already done. Nothing was changed.\n")
        return 0
    out.write(
        f"\nCopied {result.copied:,} item(s) ({human(result.bytes_copied)}), "
        f"skipped {result.skipped:,} already migrated, {result.failed:,} failed."
    )
    if args.evict_after:
        out.write(f" Freed {result.evicted:,} file(s) from this Mac (still in iCloud).")
    out.write("\n")
    if result.errors:
        out.write("\nFailures (re-run to retry):\n")
        for rel, msg in result.errors[:50]:
            out.write(f"  {rel}: {msg}\n")
        if len(result.errors) > 50:
            out.write(f"  ... and {len(result.errors) - 50} more (see `iground status --failed`)\n")
        return 1
    return 0


def cmd_verify(args: argparse.Namespace, out: TextIO) -> int:
    source = None if args.no_source else Path(args.source).expanduser()
    if source is not None and not source.is_dir():
        source = None
    res = verify(Path(args.dest), source, args.exclude)
    out.write(f"Verified OK:          {res.ok:,}\n")
    out.write(f"Checksum mismatch:    {len(res.mismatched):,}\n")
    out.write(f"Missing on SSD:       {len(res.missing_on_dest):,}\n")
    if source is not None:
        out.write(f"Not yet migrated:     {len(res.not_migrated):,}\n")
    for label, items in (("MISMATCH", res.mismatched), ("MISSING", res.missing_on_dest), ("NOT MIGRATED", res.not_migrated)):
        for rel in items[:50]:
            out.write(f"  {label}: {rel}\n")
    return 0 if not (res.mismatched or res.missing_on_dest or res.not_migrated) else 1


def cmd_status(args: argparse.Namespace, out: TextIO) -> int:
    dest = Path(args.dest).expanduser()
    if not mf.Manifest.exists(dest):
        raise MigrationError(f"no iGround manifest found in {dest}")
    with mf.Manifest(dest) as manifest:
        out.write(f"Destination: {dest}\nSource:      {manifest.get_meta('source') or '?'}\n\n")
        counts = manifest.counts()
        for status in (mf.VERIFIED, mf.COPIED, mf.EVICTED, mf.FAILED, mf.PENDING):
            if status in counts:
                c = counts[status]
                out.write(f"  {status:9} {c['files']:>8,} files  {human(c['bytes']):>10}\n")
        if args.failed:
            out.write("\nFailed files:\n")
            for rec in manifest.records(mf.FAILED):
                out.write(f"  {rec.rel_path}: {rec.error}\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="iground",
        description="Move your iCloud Drive onto an external SSD — safely, verifiably, resumably.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def add_source(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--source", default=str(ICLOUD_DRIVE),
                        help="folder to migrate (default: your iCloud Drive)")
        sp.add_argument("--exclude", action="append", default=list(DEFAULT_EXCLUDES), metavar="GLOB",
                        help="skip files/folders matching GLOB (repeatable)")

    sp = sub.add_parser("scan", help="show what is in iCloud Drive and how much must be downloaded")
    add_source(sp)
    sp.add_argument("--top", type=int, default=15, help="number of top-level items to list")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("migrate", help="copy iCloud Drive to the SSD (downloads cloud-only files first)")
    sp.add_argument("dest", help="destination folder on the SSD, e.g. '/Volumes/MySSD/iCloud Drive'")
    add_source(sp)
    sp.add_argument("--dry-run", action="store_true", help="list what would be copied; change nothing")
    sp.add_argument("--workers", type=int, default=4, help="parallel downloads/copies (default 4)")
    sp.add_argument("--no-verify", action="store_true", help="skip re-reading each copy to check its checksum")
    sp.add_argument("--evict-after", action="store_true",
                    help="after a verified copy, remove the local download from this Mac (file stays in iCloud)")
    sp.add_argument("--download-timeout", type=float, default=1800, metavar="SECONDS",
                    help="max wait per file for iCloud to download it (default 1800)")
    sp.add_argument("--force", action="store_true", help="proceed even if the SSD looks too small")
    sp.add_argument("-v", "--verbose", action="store_true", help="log every file")
    sp.set_defaults(func=cmd_migrate)

    sp = sub.add_parser("verify", help="re-check every copied file on the SSD against its recorded checksum")
    sp.add_argument("dest")
    add_source(sp)
    sp.add_argument("--no-source", action="store_true", help="don't compare against iCloud Drive for unmigrated files")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("status", help="summarise a migration recorded on the SSD")
    sp.add_argument("dest")
    sp.add_argument("--failed", action="store_true", help="list failed files with their errors")
    sp.set_defaults(func=cmd_status)
    return p


def main(argv: Optional[List[str]] = None, out: TextIO = sys.stdout) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args, out)
    except MigrationError as exc:
        sys.stderr.write(f"iground: error: {exc}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
