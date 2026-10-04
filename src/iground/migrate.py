"""The migration engine: scan → download → copy → verify → (optionally) evict."""

from __future__ import annotations

import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from . import manifest as mf
from .copier import copy_file, copy_symlink, hash_file
from .icloud import ICloudClient
from .scanner import DEFAULT_EXCLUDES, Entry, Kind, State, Summary, scan

# Keep a little headroom on the SSD rather than filling it to the last byte.
SPACE_HEADROOM = 256 * 1024 * 1024


class MigrationError(RuntimeError):
    pass


@dataclass
class Options:
    workers: int = 4
    verify: bool = True
    evict_after: bool = False
    dry_run: bool = False
    force: bool = False
    download_timeout: float = 1800.0
    excludes: Sequence[str] = DEFAULT_EXCLUDES


@dataclass
class Result:
    planned: Summary = field(default_factory=Summary)
    skipped: int = 0
    copied: int = 0
    evicted: int = 0
    failed: int = 0
    bytes_copied: int = 0
    errors: List[Tuple[str, str]] = field(default_factory=list)


# progress(event, entry, detail): event is one of "download", "copied", "skipped", "failed"
Progress = Callable[[str, Entry, str], None]


def check_paths(source: Path, dest: Path) -> Tuple[Path, Path]:
    source = Path(source).expanduser().resolve()
    dest = Path(dest).expanduser().resolve()
    if not source.is_dir():
        raise MigrationError(f"source folder not found: {source}")
    if source == dest or dest.is_relative_to(source) or source.is_relative_to(dest):
        raise MigrationError("source and destination must not contain one another")
    return source, dest


def already_done(entry: Entry, dest: Path, manifest: mf.Manifest) -> bool:
    rec = manifest.get(entry.rel_path)
    if rec is None or rec.status not in mf.DONE_STATUSES or rec.size != entry.size:
        return False
    # A legacy placeholder's own mtime is not the real file's, so only size can be compared.
    if entry.state is not State.PLACEHOLDER and rec.mtime_ns != entry.mtime_ns:
        return False
    target = dest / entry.rel_path
    if entry.kind is Kind.SYMLINK:
        return target.is_symlink()
    try:
        return target.stat().st_size == entry.size
    except FileNotFoundError:
        return False


class Migrator:
    def __init__(
        self,
        source: Path,
        dest: Path,
        options: Optional[Options] = None,
        client: Optional[ICloudClient] = None,
        progress: Optional[Progress] = None,
    ) -> None:
        self.source, self.dest = check_paths(source, dest)
        self.options = options or Options()
        self.client = client or ICloudClient()
        self.progress = progress or (lambda *_: None)
        self._lock = threading.Lock()

    def run(self) -> Result:
        opts = self.options
        if opts.evict_after and not self.client.available:
            raise MigrationError("--evict-after needs the macOS `brctl` tool, which was not found")
        if not opts.dry_run:
            self.dest.mkdir(parents=True, exist_ok=True)

        entries = list(scan(self.source, opts.excludes))
        result = Result()
        for e in entries:
            result.planned.add(e)

        if opts.dry_run:
            todo = entries
            if mf.Manifest.exists(self.dest):
                with mf.Manifest(self.dest) as manifest:
                    todo = [e for e in entries if not already_done(e, self.dest, manifest)]
            result.skipped = len(entries) - len(todo)
            for e in todo:
                self.progress("planned", e, "")
            return result

        with mf.Manifest(self.dest) as manifest:
            manifest.set_meta("source", str(self.source))
            todo = []
            for e in entries:
                if already_done(e, self.dest, manifest):
                    result.skipped += 1
                    self.progress("skipped", e, "")
                else:
                    todo.append(e)

            needed = sum(e.size for e in todo)
            free = shutil.disk_usage(self.dest).free
            if needed + SPACE_HEADROOM > free and not opts.force:
                raise MigrationError(
                    f"not enough space on destination: need {needed} bytes, {free} free "
                    "(use --force to try anyway)"
                )

            with ThreadPoolExecutor(max_workers=max(1, opts.workers)) as pool:
                futures = {pool.submit(self._process, e, manifest): e for e in todo}
                for fut in as_completed(futures):
                    entry = futures[fut]
                    try:
                        copied_bytes, evicted = fut.result()
                    except Exception as exc:  # one bad file must not stop the whole migration
                        msg = str(exc) or exc.__class__.__name__
                        manifest.put(entry.rel_path, entry.kind.value, entry.size, entry.mtime_ns, mf.FAILED, error=msg)
                        result.failed += 1
                        result.errors.append((entry.rel_path, msg))
                        self.progress("failed", entry, msg)
                    else:
                        result.copied += 1
                        result.bytes_copied += copied_bytes
                        result.evicted += int(evicted)
                        self.progress("copied", entry, "")
        return result

    def _process(self, entry: Entry, manifest: mf.Manifest) -> Tuple[int, bool]:
        target = self.dest / entry.rel_path
        if entry.kind is Kind.SYMLINK:
            copy_symlink(entry.path, target)
            manifest.put(entry.rel_path, entry.kind.value, 0, entry.mtime_ns, mf.COPIED)
            return 0, False

        if entry.needs_download:
            self.progress("download", entry, "")
            self.client.download(entry.path, self.options.download_timeout)

        before = os.stat(entry.path)
        sha = copy_file(entry.path, target)
        after = os.stat(entry.path)
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise MigrationError("file changed while it was being copied; re-run to pick it up")

        status = mf.COPIED
        if self.options.verify:
            if hash_file(target) != sha:
                raise MigrationError("checksum mismatch after copy (destination disk problem?)")
            status = mf.VERIFIED
        manifest.put(entry.rel_path, entry.kind.value, after.st_size, after.st_mtime_ns, status, sha256=sha)

        evicted = False
        if self.options.evict_after and status == mf.VERIFIED:
            self.client.evict(entry.path)
            manifest.set_status(entry.rel_path, mf.EVICTED)
            evicted = True
        return after.st_size, evicted


@dataclass
class VerifyResult:
    ok: int = 0
    mismatched: List[str] = field(default_factory=list)
    missing_on_dest: List[str] = field(default_factory=list)
    not_migrated: List[str] = field(default_factory=list)


def verify(dest: Path, source: Optional[Path] = None, excludes: Sequence[str] = DEFAULT_EXCLUDES) -> VerifyResult:
    """Re-hash every migrated file on the SSD against the manifest.

    With `source`, also report files in iCloud Drive that were never migrated.
    """
    dest = Path(dest).expanduser().resolve()
    if not mf.Manifest.exists(dest):
        raise MigrationError(f"no iGround manifest found in {dest}")
    out = VerifyResult()
    with mf.Manifest(dest) as manifest:
        for rec in manifest.records():
            if rec.status not in mf.DONE_STATUSES:
                continue
            target = dest / rec.rel_path
            if rec.kind == Kind.SYMLINK.value:
                if target.is_symlink():
                    out.ok += 1
                else:
                    out.missing_on_dest.append(rec.rel_path)
                continue
            if not target.is_file():
                out.missing_on_dest.append(rec.rel_path)
            elif rec.sha256 and hash_file(target) != rec.sha256:
                out.mismatched.append(rec.rel_path)
                manifest.set_status(rec.rel_path, mf.FAILED, "checksum mismatch during verify")
            else:
                out.ok += 1
        if source is not None:
            seen = {r.rel_path for r in manifest.records() if r.status in mf.DONE_STATUSES}
            seen.update(out.mismatched)  # already reported above
            for e in scan(Path(source).expanduser(), excludes):
                if e.rel_path not in seen:
                    out.not_migrated.append(e.rel_path)
    return out
