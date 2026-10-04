"""Export the iCloud Photos library to plain files on the SSD.

Photos.app is driven via AppleScript: exporting "using originals" makes Photos fetch
the full-resolution original from iCloud when the Mac only holds an optimised
preview. Each item is exported into its own staging folder (Live Photos and
RAW+JPEG pairs produce several files), then moved into `Photos/YYYY/MM/`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import manifest as mf
from .copier import hash_file

STAGING = "staging"
ALBUMS_FILE = "albums.json"
ALBUMS_DIR = "Albums"
FAVOURITES_DIR = "Favourites"
MONTHS = ("January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December")
# Stop before the SSD is completely full: exported sizes are unknown in advance.
MIN_FREE_BYTES = 2 * 1024 ** 3

LIST_JS = """
function run() {
  const items = Application('Photos').mediaItems;
  const ids = items.id(), dates = items.date(), names = items.filename(), favs = items.favorite();
  return JSON.stringify(ids.map((id, i) =>
    [id, dates[i] ? dates[i].getTime() / 1000 : null, names[i] || '', !!favs[i]]));
}
"""

ALBUMS_JS = """
function run() {
  const P = Application('Photos');
  const out = [];
  function walk(container, path) {
    container.albums().forEach(a => {
      try { out.push({name: a.name(), folder: path, items: a.mediaItems.id()}); } catch (e) {}
    });
    container.folders().forEach(f => walk(f, path.concat([f.name()])));
  }
  walk(P, []);
  return JSON.stringify(out);
}
"""

# argv: per-item timeout, then (media item id, staging folder) pairs.
EXPORT_APPLESCRIPT = """
on run argv
    set itemTimeout to (item 1 of argv) as integer
    set jobs to {}
    repeat with i from 2 to (count of argv) by 2
        set end of jobs to {item i of argv, (POSIX file (item (i + 1) of argv)) as alias}
    end repeat
    set results to {}
    tell application "Photos"
        repeat with job in jobs
            set theId to item 1 of job
            try
                with timeout of itemTimeout seconds
                    export {media item id theId} to (item 2 of job) with using originals
                end timeout
                set end of results to "OK" & tab & theId
            on error errMsg
                set end of results to "ERR" & tab & theId & tab & errMsg
            end try
        end repeat
    end tell
    set AppleScript's text item delimiters to linefeed
    return results as text
end run
"""


class PhotosError(RuntimeError):
    pass


@dataclass
class PhotoItem:
    id: str
    taken_at: Optional[float]  # seconds since epoch
    filename: str
    favorite: bool = False

    def folder(self) -> str:
        if self.taken_at is None:
            return "Unknown date"
        d = datetime.fromtimestamp(self.taken_at)
        return f"{d:%Y}/{d:%m} {MONTHS[d.month - 1]}"


class PhotosClient:
    """Talks to Photos.app with `osascript`. Swapped for a fake in tests."""

    def __init__(self, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
        self.runner = runner
        self.osascript = shutil.which("osascript")

    @property
    def available(self) -> bool:
        return bool(self.osascript)

    def _run(self, script: str, args: Sequence[str] = (), language: str = "AppleScript",
             timeout: Optional[float] = None) -> str:
        if not self.osascript:
            raise PhotosError("osascript not found: exporting Photos requires macOS")
        suffix = ".js" if language == "JavaScript" else ".applescript"
        with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False) as fh:
            fh.write(script)
        try:
            result = self.runner(
                [self.osascript, "-l", language, fh.name, *args],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise PhotosError("timed out talking to Photos") from exc
        finally:
            os.unlink(fh.name)
        if result.returncode != 0:
            msg = (result.stderr or result.stdout).strip()
            if "-1743" in msg or "Not authorized" in msg:
                msg += ("\nAllow your terminal to control Photos in System Settings → "
                        "Privacy & Security → Automation, then re-run.")
            raise PhotosError(msg)
        return result.stdout

    def list_items(self) -> List[PhotoItem]:
        raw = json.loads(self._run(LIST_JS, language="JavaScript", timeout=3600) or "[]")
        return [PhotoItem(i, t, n, bool(f)) for i, t, n, f in raw]

    def albums(self) -> List[dict]:
        return json.loads(self._run(ALBUMS_JS, language="JavaScript", timeout=3600) or "[]")

    def export(self, jobs: Sequence[Tuple[str, Path]], item_timeout: int) -> Dict[str, Optional[str]]:
        """Export each (id, folder) job. Returns id -> None on success or an error message."""
        args = [str(item_timeout)]
        for pid, folder in jobs:
            args += [pid, str(folder)]
        out = self._run(EXPORT_APPLESCRIPT, args, timeout=item_timeout * len(jobs) + 120)
        results: Dict[str, Optional[str]] = {}
        last = None
        for line in out.splitlines():
            parts = line.split("\t", 2)
            if parts[0] == "OK" and len(parts) >= 2:
                results[parts[1]] = None
                last = parts[1]
            elif parts[0] == "ERR" and len(parts) >= 2:
                results[parts[1]] = parts[2] if len(parts) > 2 else "export failed"
                last = parts[1]
            elif last is not None and results.get(last):
                results[last] += " " + line.strip()  # error message spilling over lines
        for pid, _ in jobs:
            results.setdefault(pid, "Photos returned no result for this item")
        return results


@dataclass
class PhotosResult:
    total: int = 0
    skipped: int = 0
    exported: int = 0
    failed: int = 0
    files: int = 0
    bytes: int = 0
    errors: List[Tuple[str, str]] = field(default_factory=list)
    albums_saved: bool = False  # album list could be read from Photos
    album_folders: bool = False  # Albums/ and Favourites/ folders were built


# progress(event, item, detail): "exported" | "failed" | "planned" | "skipped"
PhotoProgress = Callable[[str, PhotoItem, str], None]


def place_file(src: Path, folder: Path) -> Tuple[Path, str]:
    """Move an exported file into `folder`, never overwriting a different file. Returns (path, sha256)."""
    folder.mkdir(parents=True, exist_ok=True)
    digest = hash_file(src)
    candidate = folder / src.name
    n = 1
    while candidate.exists():
        if hash_file(candidate) == digest:  # identical file from an earlier, interrupted run
            src.unlink()
            return candidate, digest
        n += 1
        candidate = folder / f"{src.stem} ({n}){src.suffix}"
    os.replace(src, candidate)
    return candidate, digest


class PhotosExporter:
    def __init__(
        self,
        dest: Path,
        client: Optional[PhotosClient] = None,
        batch_size: int = 25,
        item_timeout: int = 1800,
        progress: Optional[PhotoProgress] = None,
        min_free: int = MIN_FREE_BYTES,
        state_dir: Optional[Path] = None,
    ) -> None:
        self.dest = Path(dest)
        self.state_dir = Path(state_dir) if state_dir else mf.default_state_dir(self.dest)
        self.client = client or PhotosClient()
        self.batch_size = max(1, batch_size)
        self.item_timeout = item_timeout
        self.progress = progress or (lambda *_: None)
        self.min_free = min_free

    def pending(self, items: Sequence[PhotoItem]) -> List[PhotoItem]:
        if not mf.Manifest.exists(self.state_dir):
            return list(items)
        with mf.Manifest(self.state_dir) as manifest:
            statuses = manifest.photo_statuses()
        return [it for it in items if statuses.get(it.id) != mf.EXPORTED]

    def run(self, dry_run: bool = False) -> PhotosResult:
        items = self.client.list_items()
        result = PhotosResult(total=len(items))
        todo = self.pending(items)
        result.skipped = len(items) - len(todo)
        if dry_run:
            for it in todo:
                self.progress("planned", it, "")
            return result

        self.dest.mkdir(parents=True, exist_ok=True)
        # Staging lives on the SSD next to the photos so moving files into place is a rename.
        staging = self.dest / f".{STAGING}-iground"
        with mf.Manifest(self.state_dir) as manifest:
            manifest.set_meta("source", "Photos library")
            for start in range(0, len(todo), self.batch_size):
                free = shutil.disk_usage(self.dest).free
                if free < self.min_free:
                    raise PhotosError(f"SSD almost full ({free} bytes free); stopping Photos export")
                batch = todo[start:start + self.batch_size]
                self._export_batch(batch, staging, manifest, result)
            shutil.rmtree(staging, ignore_errors=True)
            result.albums_saved, result.album_folders = self._save_albums(items, manifest)
        return result

    def _export_batch(self, batch: Sequence[PhotoItem], staging: Path,
                      manifest: mf.Manifest, result: PhotosResult) -> None:
        jobs = []
        for n, it in enumerate(batch):
            folder = staging / str(n)
            shutil.rmtree(folder, ignore_errors=True)
            folder.mkdir(parents=True)
            jobs.append((it.id, folder))
        try:
            outcomes = self.client.export(jobs, self.item_timeout)
        except PhotosError as exc:
            outcomes = {it.id: str(exc) for it in batch}

        for it, (_, folder) in zip(batch, jobs):
            error = outcomes.get(it.id)
            exported = sorted(p for p in folder.rglob("*") if p.is_file()) if folder.exists() else []
            if error is None and not exported:
                error = "Photos reported success but produced no file"
            if error is not None:
                manifest.put_photo(it.id, mf.FAILED, it.taken_at, error=error)
                result.failed += 1
                result.errors.append((it.filename or it.id, error))
                self.progress("failed", it, error)
                shutil.rmtree(folder, ignore_errors=True)
                continue
            rel_files = []
            for f in exported:
                final, sha = place_file(f, self.dest / it.folder())
                if it.taken_at is not None:
                    os.utime(final, (it.taken_at, it.taken_at))
                st = final.stat()
                rel = final.relative_to(self.dest).as_posix()
                manifest.put(rel, "file", st.st_size, st.st_mtime_ns, mf.VERIFIED, sha256=sha)
                rel_files.append(rel)
                result.files += 1
                result.bytes += st.st_size
            manifest.put_photo(it.id, mf.EXPORTED, it.taken_at, files=rel_files)
            result.exported += 1
            self.progress("exported", it, "")
            shutil.rmtree(folder, ignore_errors=True)

    def _save_albums(self, items: Sequence[PhotoItem], manifest: mf.Manifest) -> Tuple[bool, bool]:
        """Mirror albums and favourites as browsable folders, plus albums.json for the record.

        The folders contain hard links: they look like ordinary photos in Finder but
        take no extra space. Returns (album list read, folders built).
        """
        files = manifest.photo_files()
        try:
            albums = self.client.albums()
        except Exception:
            albums = None
        album_docs = [
            {
                "name": a.get("name") or "Untitled",
                "folder": a.get("folder", []),
                "files": [f for pid in a.get("items", []) for f in files.get(pid, [])],
            }
            for a in (albums or [])
        ]
        favourites = sorted(f for it in items if it.favorite for f in files.get(it.id, []))
        doc = {"generated": datetime.now().isoformat(timespec="seconds"),
               "favourites": favourites, "albums": album_docs}
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_dir / (ALBUMS_FILE + ".tmp")
        tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
        os.replace(tmp, self.state_dir / ALBUMS_FILE)

        wanted: Dict[Path, Path] = {}
        for a in album_docs:
            folder = self.dest / ALBUMS_DIR
            for part in [*a["folder"], a["name"]]:
                folder = folder / safe_name(part)
            add_links(wanted, folder, [self.dest / f for f in a["files"]])
        add_links(wanted, self.dest / FAVOURITES_DIR, [self.dest / f for f in favourites])
        built = sync_links(wanted, [self.dest / ALBUMS_DIR, self.dest / FAVOURITES_DIR])
        return albums is not None, built


def safe_name(name: str) -> str:
    name = str(name).replace("/", "-").replace(":", "-").strip().lstrip(".")
    return name or "Untitled"


def add_links(wanted: Dict[Path, Path], folder: Path, originals: Sequence[Path]) -> None:
    """Plan one link per original inside `folder`, giving clashing names a ' (2)' suffix."""
    used = set()
    for original in originals:
        name, n = original.name, 1
        while name.lower() in used:
            n += 1
            name = f"{original.stem} ({n}){original.suffix}"
        used.add(name.lower())
        wanted[folder / name] = original


def sync_links(wanted: Dict[Path, Path], roots: Sequence[Path]) -> bool:
    """Make the hard links in `roots` match `wanted`. Files that aren't ours are left alone."""
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                target = wanted.get(path)
                keep = target is not None and target.exists() and os.path.samefile(path, target)
                if not keep and path.stat().st_nlink > 1:
                    path.unlink()  # a link we made earlier; the original photo is untouched
        prune_empty_dirs(root)
    for link, original in wanted.items():
        if link.exists() or not original.exists():
            continue
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(original, link)
        except OSError:
            # The SSD's format (e.g. exFAT) has no hard links: albums.json is the fallback.
            for root in roots:
                prune_empty_dirs(root)
            return False
    return True


def prune_empty_dirs(root: Path) -> None:
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
    try:
        root.rmdir()
    except OSError:
        pass
