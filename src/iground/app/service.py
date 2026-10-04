"""Everything the app window can see and do, independent of HTTP (so it can be tested directly)."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .. import devicebackup, layout, readiness
from .. import manifest as mf
from ..backup import (
    PHOTOS_STATE, BackupOptions, Reporter, SectionResult, _Fixed, backup_sections, prepare, run_backup,
    summarize,
)
from ..migrate import diff_against_source, verify
from .. import selection
from ..photos import PhotosClient, PhotosError
from ..scanner import DEFAULT_EXCLUDES
from ..selection import LOOSE_FILES, UNKNOWN_YEAR, Selection, app_name, photo_year, top_level_groups
from ..sources import (
    ALL_KINDS, APPS, BACKUPS_DIR, DRIVE, MESSAGES, PHOTOS, PHOTOS_DIR, Locations, folder_sources,
)
from ..wizard import FULL_DISK_ACCESS_URL, _readable, list_drives

AUTOMATION_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation"
SECTION_ORDER = (PHOTOS, DRIVE, APPS, MESSAGES, "iphone")
LABELS = {PHOTOS: "Photos", DRIVE: "iCloud Drive", APPS: "App Documents", MESSAGES: "Messages",
          "iphone": "iPhone & WhatsApp"}
MAX_ERRORS = 50


class ServiceError(RuntimeError):
    """Something the user asked for can't be done right now; the message is shown as-is."""


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


# --- jobs --------------------------------------------------------------------


class Job:
    """A backup or verify running in the background, with progress the window polls."""

    def __init__(self, kind: str):
        self.kind = kind  # "backup" | "verify"
        self.state = "running"  # running | stopping | done | stopped | failed
        self.cancel = threading.Event()
        self.sections: Dict[str, Dict[str, Any]] = {}
        self.current: Optional[str] = None
        self.started = time.time()
        self.finished: Optional[float] = None
        self.message = ""
        self.lock = threading.Lock()

    def section(self, key: str, items: int, size: int) -> Dict[str, Any]:
        with self.lock:
            sec = self.sections.get(key)
            if sec is None:
                sec = self.sections[key] = {
                    "done": 0, "total": 0, "bytes": 0, "bytes_total": 0, "skipped": 0, "skipped_bytes": 0,
                    "failed": 0, "item": "", "status": "running", "summary": "", "started": time.time(),
                }
            else:
                sec["status"] = "running"
            sec["total"] += items
            sec["bytes_total"] += size
            self.current = key
            return sec

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            sections = {}
            for key, s in self.sections.items():
                sections[key] = {k: s[k] for k in ("done", "total", "bytes", "bytes_total", "failed", "item",
                                                   "status", "summary")}
                sections[key]["eta"] = self._eta(s) if s["status"] == "running" else None
            return {
                "kind": self.kind, "state": self.state, "current": self.current, "sections": sections,
                "started": self.started, "finished": self.finished, "message": self.message,
            }

    @staticmethod
    def _eta(s: Dict[str, Any]) -> Optional[float]:
        """Seconds left in a section, from the pace of real work (skipped items don't count)."""
        elapsed = time.time() - s["started"]
        if s["bytes_total"]:
            work, left = s["bytes"] - s["skipped_bytes"], s["bytes_total"] - s["bytes"]
        else:
            work, left = s["done"] - s["skipped"], s["total"] - s["done"]
        if work <= 0 or elapsed < 5 or left <= 0:
            return None
        return elapsed / work * left


class JobReporter(Reporter):
    def __init__(self, job: Job):
        self.job = job

    def section(self, label, index, total, items, size, kind=""):
        sec = self.job.section(kind or label, items, size)
        job = self.job

        def progress(event: str, item, detail: str) -> None:
            size_ = getattr(item, "size", 0)
            name = getattr(item, "rel_path", None) or getattr(item, "filename", None) or ""
            with job.lock:
                if event in ("copied", "skipped", "failed", "exported"):
                    sec["done"] += 1
                    sec["bytes"] += size_
                if event == "skipped":
                    sec["skipped"] += 1
                    sec["skipped_bytes"] += size_
                elif event == "failed":
                    sec["failed"] += 1
                elif event in ("download", "copied", "exported"):
                    sec["item"] = name.rsplit("/", 1)[-1]

        return progress

    def section_done(self, result: SectionResult) -> None:
        with self.job.lock:
            sec = self.job.sections.get(result.kind)
            if sec is None:
                return
            # App Documents arrive as one section per app; the row shows them together.
            if not result.ok or sec["status"] != "error":
                sec["status"] = "done" if result.ok else "error"
            sec["summary"] = result.summary
            sec["item"] = ""


# --- the service -------------------------------------------------------------


class Service:
    def __init__(
        self,
        loc: Optional[Locations] = None,
        volumes: Path = Path("/Volumes"),
        photos_client: Optional[PhotosClient] = None,
        icloud_client: Optional[Callable] = None,
        run: Callable = subprocess.run,
        popen: Callable = subprocess.Popen,
    ):
        self.loc = loc or Locations.default()
        self.volumes = volumes
        self.photos = photos_client or PhotosClient()
        self.icloud_client = icloud_client
        self.run = run
        self.popen = popen
        self.lock = threading.RLock()
        self.drive: Optional[Path] = None
        saved = selection.load(self.loc.config_file)
        self.selection = Selection.from_dict(saved.get("selection", {}))
        self.evict = bool(saved.get("evict", False))
        self.tip_seen = bool(saved.get("tip_seen", False))
        self.job: Optional[Job] = None
        self.overview: Optional[Dict[str, Any]] = None
        self.overview_key: Optional[str] = None
        self.refreshing = False
        self._refresh_again = False
        self.notice: Optional[Dict[str, Any]] = None
        self._notice_id = 0
        self._awake: Optional[subprocess.Popen] = None

    # -- reading state ---------------------------------------------------------

    def drives(self) -> List[Dict[str, Any]]:
        return [
            {"path": str(p), "name": p.name, "free": free, "has_backup": bool(layout.find_backups(p))}
            for p, free in list_drives(self.volumes)
        ]

    def state(self) -> Dict[str, Any]:
        drives = self.drives()
        with self.lock:
            self._auto_select(drives)
            key = str(self.drive) if self.drive else ""
            if self.overview_key != key and not self.refreshing:
                self.refresh()
            overview = self.overview if self.overview_key == key else None
            return {
                "drives": drives,
                "drive": self._drive_info(),
                "backup": self._backup_info(),
                "overview": overview,
                "loading": self.refreshing or overview is None,
                "job": self.job.snapshot() if self.job else None,
                "settings": {**self.selection.to_dict(), "evict": self.evict, "tip_seen": self.tip_seen},
                "notice": self.notice,
            }

    def _auto_select(self, drives: List[Dict[str, Any]]) -> None:
        if self.drive is not None and self.drive.is_dir():
            return
        if self.job and self.job.state in ("running", "stopping"):
            return
        preferred = [d for d in drives if d["has_backup"]] or drives
        self.drive = Path(preferred[0]["path"]) if preferred else None

    def _drive_info(self) -> Optional[Dict[str, Any]]:
        if self.drive is None:
            return None
        try:
            free = shutil.disk_usage(self.drive).free
        except OSError:
            free = 0
        return {"path": str(self.drive), "name": self.drive.name, "free": free}

    def _root(self) -> Optional[Path]:
        if self.drive is None:
            return None
        backups = layout.find_backups(self.drive)
        return backups[-1] if backups else None

    def _backup_info(self) -> Optional[Dict[str, Any]]:
        root = self._root()
        if root is None:
            return None
        info = layout.read_info(root)
        updated = info.get("updated")
        return {
            "path": str(root), "name": root.name, "updated": updated,
            "complete": bool(updated) and info.get("completed") == updated,
        }

    # -- the overview: what's in iCloud and what's on the SSD ------------------

    def refresh(self) -> None:
        """Recompute the overview in the background (it may read the whole Photos library)."""
        with self.lock:
            if self.refreshing:
                self._refresh_again = True
                return
            self.refreshing = True
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self) -> None:
        while True:
            with self.lock:
                drive = self.drive
                self._refresh_again = False
            try:
                overview = self.compute_overview(drive)
            except Exception as exc:  # keep the window alive whatever happens
                traceback.print_exc()
                overview = {"sections": [], "ready": False, "error": str(exc)}
            with self.lock:
                self.overview = overview
                self.overview_key = str(drive) if drive else ""
                if not self._refresh_again:
                    self.refreshing = False
                    return

    def compute_overview(self, drive: Optional[Path]) -> Dict[str, Any]:
        root = None
        if drive is not None:
            backups = layout.find_backups(drive)
            root = backups[-1] if backups else None
        sel = self.selection
        sections = [self._photos_section(root, sel), self._folder_section(DRIVE, root, sel),
                    self._folder_section(APPS, root, sel), self._folder_section(MESSAGES, root, sel),
                    self._iphone_section(root)]
        for s in sections:
            if s["key"] in ALL_KINDS and s["key"] not in sel.kinds and s["status"] not in ("empty", "error"):
                s["status"], s["detail"] = "off", "Not copying — stays in iCloud"
        needed = sum(s.get("needed", 0) for s in sections if s["status"] == "todo")
        free = shutil.disk_usage(drive).free if drive is not None and drive.is_dir() else 0
        remaining = [s for s in sections if s["status"] in ("todo", "error")]
        done = [s for s in sections if s["status"] == "done"]
        on = [s for s in sections if s["key"] in sel.kinds]
        return {
            "sections": sections,
            "ready": root is not None and not remaining and bool(done),
            "remaining": len(remaining),
            "partial": any(s["status"] == "off" or s.get("skipped") for s in sections),
            "photos": next((s.get("count", 0) for s in on if s["key"] == PHOTOS), 0),
            "bytes": sum(s.get("bytes", 0) for s in on),
            "space": {"needed": needed, "free": free, "short": bool(drive) and needed > free},
            "computed": time.time(),
        }

    def _photos_section(self, root: Optional[Path], sel: Selection) -> Dict[str, Any]:
        sec = {"key": PHOTOS, "label": LABELS[PHOTOS], "folder": str(root / PHOTOS_DIR) if root else None}
        if not self.photos.available:
            return {**sec, "status": "empty", "headline": "Needs the Photos app", "detail": ""}
        try:
            items = self.photos.list_items()
        except PhotosError as exc:
            first = str(exc).splitlines()[0] if str(exc) else "Photos didn't respond"
            permission = "-1743" in str(exc) or "Automation" in str(exc) or "authorized" in str(exc).lower()
            return {**sec, "status": "error", "headline": "Can't open Photos",
                    "detail": "Allow Terminal to use Photos, then check again." if permission else first,
                    "action": {"label": "Open Settings", "open": "automation"} if permission else None}
        if not items:
            return {**sec, "status": "empty", "headline": "No photos", "detail": ""}
        years: Dict[str, int] = {}
        for it in items:
            y = photo_year(it.taken_at)
            years[y] = years.get(y, 0) + 1
        order = sorted((y for y in years if y != UNKNOWN_YEAR), reverse=True) + \
            ([UNKNOWN_YEAR] if UNKNOWN_YEAR in years else [])
        sec["choices"] = [{"id": y, "label": "Unknown date" if y == UNKNOWN_YEAR else y, "count": years[y],
                           "on": y not in sel.skip_years} for y in order]
        wanted = [it for it in items if sel.wants_photo(it.taken_at)]
        skipped_years = sum(1 for c in sec["choices"] if not c["on"])
        sec.update(count=len(wanted), skipped=skipped_years,
                   headline=f"{len(wanted):,} photos & videos" if len(wanted) != 1 else "1 photo")
        if not wanted:
            return {**sec, "status": "off", "detail": "No years chosen — photos stay in iCloud"}
        note = f" · {_plural(skipped_years, 'year')} skipped" if skipped_years else ""
        if root is None:
            return {**sec, "status": "todo", "detail": "Not copied yet" + note}
        state = layout.state_dir(root, PHOTOS_STATE)
        check = readiness.photos_check(state, _Fixed(self.photos, wanted), "")
        errors = []
        if mf.Manifest.exists(state):
            with mf.Manifest(state) as manifest:
                errors = [{"name": pid, "error": err} for pid, err in manifest.photo_errors()[:MAX_ERRORS]]
        if check.status == readiness.OK:
            return {**sec, "status": "done", "detail": "All copied" + note, "errors": []}
        return {**sec, "status": "todo", "detail": check.detail.replace(" photos & videos", "") + note,
                "errors": errors}

    def _folder_section(self, kind: str, root: Optional[Path], sel: Selection) -> Dict[str, Any]:
        sources = folder_sources(self.loc, [kind])
        folder = None
        if root is not None:
            folder = str(root / {DRIVE: "iCloud Drive", APPS: "App Documents", MESSAGES: "Messages"}[kind])
        sec = {"key": kind, "label": LABELS[kind], "folder": folder}
        if not sources:
            return {**sec, "status": "empty", "headline": "Nothing to copy", "detail": ""}
        if any(not _readable(s.path) for s in sources):
            return {**sec, "status": "error", "headline": "Needs permission",
                    "detail": "Turn on Full Disk Access for Terminal, then check again.",
                    "action": {"label": "Open Settings", "open": "fda"}}

        files = size = cloud = missing = changed = 0
        errors: List[Dict[str, str]] = []
        choices: List[Dict[str, Any]] = []
        for src in sources:
            excludes = list(DEFAULT_EXCLUDES)
            if kind == DRIVE:
                groups = top_level_groups(src.path)
                for name in sorted(groups, key=lambda n: (n == LOOSE_FILES, n.lower())):
                    g = groups[name]
                    choices.append({"id": name, "label": "Files not in a folder" if name == LOOSE_FILES else name,
                                    "bytes": g.total_bytes, "files": g.files, "on": name not in sel.skip_drive})
                excludes += sel.drive_excludes(src.path)
                picked = [groups[c["id"]] for c in choices if c["on"]]
                s_files = sum(g.files for g in picked)
                s_size = sum(g.total_bytes for g in picked)
                s_cloud = sum(g.cloud_bytes for g in picked)
            else:
                s = summarize(src.path)
                s_files, s_size, s_cloud = s.files, s.total_bytes, s.cloud_bytes
                if kind == APPS:
                    choices.append({"id": app_name(src), "label": app_name(src), "bytes": s.total_bytes,
                                    "files": s.files, "on": sel.wants_app(src)})
                    if not sel.wants_app(src):
                        continue
            files, size, cloud = files + s_files, size + s_size, cloud + s_cloud
            if root is not None:
                state = layout.state_dir(root, src.state_key)
                d = diff_against_source(src.path, src.dest(root), excludes, state_dir=state)
                missing, changed = missing + len(d.missing), changed + len(d.changed)
                if mf.Manifest.exists(state):
                    with mf.Manifest(state) as manifest:
                        errors += [{"name": r.rel_path, "error": r.error or ""} for r in manifest.records(mf.FAILED)]

        skipped = sum(1 for c in choices if not c["on"])
        if choices:
            sec["choices"] = choices
        sec.update(headline=f"{_plural(files, 'file')} · {_size(size)}", bytes=size, skipped=skipped,
                   errors=errors[:MAX_ERRORS])
        unit = "folder" if kind == DRIVE else "app"
        note = f" · {_plural(skipped, unit)} skipped" if skipped else ""
        if choices and skipped == len(choices):
            return {**sec, "status": "off", "headline": "Nothing chosen", "detail": "Not copying — stays in iCloud"}
        if files == 0:
            return {**sec, "status": "empty", "headline": "Nothing to copy", "detail": ""}
        if root is None:
            detail = "Not copied yet" + note
            if cloud:
                detail += f" · {_size(cloud)} to download from iCloud first"
            return {**sec, "status": "todo", "detail": detail, "needed": size}
        if not missing and not changed:
            return {**sec, "status": "done", "detail": "All copied" + note}
        parts = []
        if missing:
            parts.append(f"{_plural(missing, 'file')} not copied yet")
        if changed:
            parts.append(f"{_plural(changed, 'file')} changed since")
        return {**sec, "status": "todo", "detail": " · ".join(parts) + note}

    def _iphone_section(self, root: Optional[Path]) -> Dict[str, Any]:
        sec = {"key": "iphone", "label": LABELS["iphone"], "folder": str(root / BACKUPS_DIR) if root else None}
        relocated = root is not None and devicebackup.is_relocated(self.loc, root)
        backups = devicebackup.list_backups(root / BACKUPS_DIR) if root else []
        sec["relocated"] = relocated or (self.loc.mobilesync_backup.is_symlink())
        sec["devices"] = [
            {"name": b.device, "date": b.last_backup.isoformat() if b.last_backup else None, "encrypted": b.encrypted}
            for b in backups
        ]
        if backups:
            latest = max(backups, key=lambda b: b.last_backup or datetime.min)
            sec["headline"] = latest.device
        else:
            sec["headline"] = "Set to back up here" if relocated else "Not set up"
        check = readiness.backup_check(self.loc, root) if root else None
        if check is not None and check.status == readiness.OK:
            return {**sec, "status": "done", "detail": "Backed up recently"}
        if not relocated:
            return {**sec, "status": "todo",
                    "detail": "Keeps your WhatsApp chats safe",
                    "action": {"label": "Set up", "do": "iphone-setup"}}
        if backups:
            return {**sec, "status": "todo", "detail": "Last backup is over two weeks old — back up again in Finder"}
        return {**sec, "status": "todo", "detail": "Now back up your iPhone in Finder"}

    # -- actions ---------------------------------------------------------------

    def select_drive(self, path: str) -> None:
        p = Path(path).expanduser()
        if not p.is_dir():
            raise ServiceError("That drive isn't available any more.")
        with self.lock:
            self._require_idle()
            self.dismiss()
            self.drive = p
            self.refresh()

    def update_settings(self, kinds: Optional[List[str]] = None, evict: Optional[bool] = None,
                        skip: Optional[Dict[str, List[str]]] = None, tip_seen: Optional[bool] = None) -> None:
        with self.lock:
            if tip_seen is not None:
                self.tip_seen = bool(tip_seen)
                if kinds is None and skip is None and evict is None:  # allowed even while copying
                    self._save_settings()
                    return
            self._require_idle()
            self._ensure_drive()
            sel = Selection.from_dict(self.selection.to_dict())
            if kinds is not None:
                if not isinstance(kinds, list) or any(k not in ALL_KINDS for k in kinds) or not kinds:
                    raise ServiceError("Choose at least one thing to copy.")
                sel.kinds = [k for k in ALL_KINDS if k in kinds]
            if skip is not None:
                if not isinstance(skip, dict):
                    raise ServiceError("Unknown choice.")
                if "drive" in skip:
                    sel.skip_drive = [str(x) for x in skip["drive"]]
                if "apps" in skip:
                    sel.skip_apps = [str(x) for x in skip["apps"]]
                if "years" in skip:
                    sel.skip_years = [str(x) for x in skip["years"]]
            self.selection = sel
            if evict is not None:
                self.evict = bool(evict)
            self._save_settings()
            self.refresh()

    def _save_settings(self) -> None:
        try:
            selection.save(self.loc.config_file, {"selection": self.selection.to_dict(), "evict": self.evict,
                                                  "tip_seen": self.tip_seen})
        except OSError:
            pass  # choices still apply for this session

    def start_backup(self, new: bool = False) -> None:
        with self.lock:
            self._require_idle()
            self._ensure_drive()
            if self.drive is None:
                raise ServiceError("Plug in your SSD first.")
            job = self.job = Job("backup")
            drive, sel, evict = self.drive, Selection.from_dict(self.selection.to_dict()), self.evict
        threading.Thread(target=self._backup_worker, args=(job, drive, sel, evict, new), daemon=True).start()

    def _backup_worker(self, job: Job, drive: Path, sel: Selection, evict: bool, new: bool) -> None:
        self._keep_awake(True)
        try:
            opened = prepare(self.loc, drive, new=new)
            opts = BackupOptions(kinds=sel.kinds, evict_after=evict, cancel=job.cancel, selection=sel)
            results = run_backup(self.loc, opened.root, opts, JobReporter(job),
                                 photos_client=self.photos, icloud_client=self.icloud_client)
            failed = sum(len(r.errors) for r in results)
            if job.cancel.is_set():
                job.state, job.message = "stopped", "Stopped. Everything copied so far is safe — continue any time."
            elif failed:
                job.state, job.message = "done", f"Finished, but {_plural(failed, 'item')} couldn't be copied."
            else:
                job.state, job.message = "done", "Backup updated."
        except Exception as exc:
            traceback.print_exc()
            job.state, job.message = "failed", f"Something went wrong: {exc}"
        finally:
            job.finished = time.time()
            self._keep_awake(False)
            self.refresh()

    def start_verify(self) -> None:
        with self.lock:
            self._require_idle()
            self._ensure_drive()
            root = self._root()
            if root is None:
                raise ServiceError("There's no backup on this drive yet.")
            job = self.job = Job("verify")
        threading.Thread(target=self._verify_worker, args=(job, root), daemon=True).start()

    def _verify_worker(self, job: Job, root: Path) -> None:
        self._keep_awake(True)
        checked = damaged = 0
        try:
            for label, folder, _source, state, kind in backup_sections(root, self.loc):
                if job.cancel.is_set():
                    break
                sec = job.section(kind, 0, 0)
                sec["base"] = sec.get("base", sec["total"])

                def progress(done: int, total: int, sec=sec) -> None:
                    with job.lock:
                        sec["total"] = sec["base"] + total
                        sec["done"] = sec["base"] + done

                res = verify(folder, None, state_dir=state, progress=progress, cancel=job.cancel)
                bad = len(res.mismatched) + len(res.missing_on_dest)
                checked += res.ok + bad
                damaged += bad
                with job.lock:
                    sec["base"] = sec["total"] = sec["done"] = sec["base"] + res.ok + bad
                    sec["failed"] += bad
                    if sec["status"] != "error":
                        sec["status"] = "error" if bad else "done"
                    sec["summary"] = f"{_plural(sec['failed'], 'problem')} found" if sec["failed"] else "No problems"
            if job.cancel.is_set():
                job.state, job.message = "stopped", "Check stopped."
            elif damaged:
                job.state = "done"
                job.message = (f"{_plural(damaged, 'file')} on the SSD {'is' if damaged == 1 else 'are'} damaged "
                               "or missing. Choose Update backup to repair.")
            else:
                job.state, job.message = "done", f"All {checked:,} files checked — no problems found."
        except Exception as exc:
            traceback.print_exc()
            job.state, job.message = "failed", f"Something went wrong: {exc}"
        finally:
            job.finished = time.time()
            self._keep_awake(False)
            self.refresh()

    def stop(self) -> None:
        with self.lock:
            if self.job and self.job.state == "running":
                self.job.state = "stopping"
                self.job.cancel.set()

    def dismiss(self) -> None:
        with self.lock:
            if self.job and self.job.state not in ("running", "stopping"):
                self.job = None

    def iphone(self, action: str) -> None:
        with self.lock:
            self._require_idle()
            self.dismiss()
            self._ensure_drive()
            if action == "setup":
                if self.drive is None:
                    raise ServiceError("Plug in your SSD first.")
                root = self._root() or prepare(self.loc, self.drive).root
                try:
                    devicebackup.relocate(self.loc, root)
                except Exception as exc:
                    raise ServiceError(str(exc)) from exc
                self._notify("Done. Now back up your iPhone in Finder.")
            elif action == "undo":
                devicebackup.undo(self.loc)
                self._notify("iPhone backups will be saved on this Mac again.")
            else:
                raise ServiceError("Unknown action.")
            self.refresh()

    def open(self, target: str) -> None:
        self._ensure_drive()
        root = self._root()
        if target == "fda":
            path = FULL_DISK_ACCESS_URL
        elif target == "automation":
            path = AUTOMATION_URL
        elif target == "backup" and root is not None:
            path = str(root)
        elif target == "about" and root is not None:
            path = str(root / layout.ABOUT_FILE)
        elif target.startswith("/") and root is not None and Path(target).resolve().is_relative_to(root.resolve()):
            path = target
        else:
            raise ServiceError("Nothing to open yet.")
        if shutil.which("open"):
            self.run(["open", path], capture_output=True)

    # -- helpers ---------------------------------------------------------------

    def _ensure_drive(self) -> None:
        if self.drive is None or not self.drive.is_dir():
            self._auto_select(self.drives())

    def _require_idle(self) -> None:
        if self.job and self.job.state in ("running", "stopping"):
            raise ServiceError("iGround is busy — wait for it to finish or stop it first.")

    def _notify(self, text: str) -> None:
        self._notice_id += 1
        self.notice = {"id": self._notice_id, "text": text}

    def _keep_awake(self, on: bool) -> None:
        if on and shutil.which("caffeinate"):
            try:
                self._awake = self.popen(["caffeinate", "-i", "-w", str(os.getpid())])
            except OSError:
                self._awake = None
        elif not on and self._awake is not None:
            try:
                self._awake.terminate()
            except Exception:
                pass
            self._awake = None
