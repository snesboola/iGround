"""The guided flow: double-click iGround.command (or type `iground`) and answer a few questions."""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import devicebackup, layout, readiness
from .backup import BackupOptions, Reporter, SectionResult, prepare, run_backup, summarize
from .photos import PhotosClient, PhotosError
from .sources import Locations, folder_sources
from .ui import Console, ProgressPrinter, human, plural

FULL_DISK_ACCESS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
STEPS = 4


def list_drives(volumes: Path = Path("/Volumes")) -> List[Tuple[Path, int]]:
    """External drives you can write to. The Mac's own disk shows up as a symlink to / and is skipped."""
    out = []
    try:
        entries = sorted(volumes.iterdir())
    except OSError:
        return out
    for p in entries:
        if p.name.startswith((".", "com.apple.")) or p.is_symlink() or not p.is_dir():
            continue
        if not os.access(p, os.W_OK):
            continue
        try:
            out.append((p, shutil.disk_usage(p).free))
        except OSError:
            continue
    return out


class WizardReporter(Reporter):
    def __init__(self, console: Console):
        self.console = console
        self.printer: Optional[ProgressPrinter] = None

    def section(self, label, index, total, items, size, kind=""):
        self.console.say(f"\n  [{index}/{total}] {label}")
        self.printer = ProgressPrinter(items, size, indent="      ")
        return self.printer

    def section_done(self, result: SectionResult) -> None:
        if self.printer:
            self.printer.finish()
        mark = "✓" if result.ok else "✗"
        self.console.say(f"      {mark} {result.summary}")
        for name, msg in result.errors[:5]:
            self.console.say(f"        · {name}: {msg}")
        if len(result.errors) > 5:
            self.console.say(f"        · …and {len(result.errors) - 5} more")


class Wizard:
    def __init__(
        self,
        console: Console,
        loc: Optional[Locations] = None,
        volumes: Path = Path("/Volumes"),
        photos_client: Optional[PhotosClient] = None,
        icloud_client: Optional[Callable] = None,
        run: Callable = subprocess.run,
        popen: Callable = subprocess.Popen,
    ):
        self.c = console
        self.loc = loc or Locations.default()
        self.volumes = volumes
        self.photos = photos_client or PhotosClient()
        self.icloud_client = icloud_client
        self.run = run
        self.popen = popen

    # -- steps ---------------------------------------------------------------

    def start(self) -> int:
        c = self.c
        c.heading("iGround — move everything from iCloud to your SSD")
        c.say("Your photos, iCloud Drive, app documents, messages and iPhone backups (with WhatsApp)")
        c.say("are copied into one dated folder on your SSD. Nothing is deleted from iCloud.")

        drive = self.pick_drive()
        if drive is None:
            c.say("\nNo problem — run iGround again when your SSD is plugged in.")
            return 1
        if not self.check_access():
            return 1
        if not self.review(drive):
            c.say("\nNothing was copied. Run iGround again whenever you're ready.")
            return 1

        opened = prepare(self.loc, drive)
        self.keep_awake()
        c.heading(f"Step 3 of {STEPS} · Copying to “{opened.root.name}”")
        c.say("   You can close this window at any time — run iGround again and it carries on where it stopped.")
        results = run_backup(self.loc, opened.root, BackupOptions(), WizardReporter(c),
                             photos_client=self.photos, icloud_client=self.icloud_client)

        self.iphone_step(opened.root)
        return self.finish(opened.root, results)

    def pick_drive(self) -> Optional[Path]:
        c = self.c
        c.heading(f"Step 1 of {STEPS} · Choose your SSD")
        while True:
            drives = list_drives(self.volumes)
            if len(drives) == 1:
                path, free = drives[0]
                if c.yes(f"   Use “{path.name}” ({human(free)} free)?"):
                    return path
            elif drives:
                labels = [f"{p.name}  ({human(free)} free)" for p, free in drives]
                return drives[c.choose("   Which drive?", labels)][0]
            else:
                c.say("   I can't see an external drive.")
            answer = c.ask("   Plug in your SSD and press Return — or drag a folder here, or type q to quit: ")
            if answer.lower() in ("q", "quit"):
                return None
            if answer:
                path = Path(answer.strip("'\"").replace("\\ ", " ")).expanduser()
                if path.is_dir():
                    return path
                c.say(f"   “{answer}” isn't a folder I can find.")

    def check_access(self) -> bool:
        """Messages and iPhone backups need Full Disk Access; explain how to grant it."""
        protected = [p for p in (self.loc.messages, self.loc.mobilesync_backup.parent) if p.exists()]
        while True:
            denied = [p for p in protected if not _readable(p)]
            if not denied:
                return True
            c = self.c
            c.say("\n   macOS needs your permission before iGround can read Messages and iPhone backups:")
            c.say("   System Settings → Privacy & Security → Full Disk Access → turn on Terminal.")
            c.say("   (Terminal may need to be restarted afterwards — then run iGround again.)")
            self._quiet(["open", FULL_DISK_ACCESS_URL])
            answer = c.ask("   Press Return when done, s to skip Messages & iPhone backups, or q to quit: ").lower()
            if answer in ("q", "quit"):
                return False
            if answer == "s":
                return True

    def review(self, drive: Path) -> bool:
        c = self.c
        c.heading(f"Step 2 of {STEPS} · What's in your iCloud")
        c.say("   Looking… (this can take a minute)")
        rows = []
        files_bytes = cloud_bytes = 0
        if self.photos.available:
            try:
                n = len(self.photos.list_items())
                rows.append(("Photos", f"{n:,} photos & videos"))
            except PhotosError as exc:
                rows.append(("Photos", f"can't open the Photos library ({str(exc).splitlines()[0]})"))
        apps = 0
        for src in folder_sources(self.loc):
            s = summarize(src.path)
            files_bytes += s.total_bytes
            cloud_bytes += s.cloud_bytes
            if src.kind == "apps":
                apps += s.total_bytes
                continue
            rows.append((src.label, f"{plural(s.files, 'file')}, {human(s.total_bytes)}"))
        if apps:
            rows.append(("App documents", human(apps)))
        for name, text in rows:
            c.say(f"   • {name:16} {text}")
        if cloud_bytes:
            c.say(f"   {human(cloud_bytes)} of this is only in iCloud right now and will be downloaded first.")

        free = shutil.disk_usage(drive).free
        c.say(f"\n   Your SSD “{drive.name}” has {human(free)} free.")
        if files_bytes > free:
            c.say(f"   ⚠ That's less than your files alone ({human(files_bytes)}), before photos.")
            if not c.yes("   Continue anyway?", default=False):
                return False

        backups = layout.find_backups(drive)
        today = layout.folder_name(datetime.now().date())
        if backups:
            c.say(f"   Your earlier backup “{backups[-1].name}” will be brought up to date and renamed “{today}”.")
        else:
            c.say(f"   Everything goes into a new folder: “{today}”.")

        if self.loc.messages.exists() and self._app_running("Messages"):
            if c.yes("   Messages is open. Quit it so your messages are copied cleanly?"):
                self._quiet(["osascript", "-e", 'quit app "Messages"'])

        c.say("\n   Copying can take several hours the first time (photos are downloaded in full quality).")
        c.say("   Keep the Mac plugged in; it will be kept awake until iGround finishes.")
        return c.yes("   Start now?")

    def iphone_step(self, root: Path) -> None:
        c = self.c
        c.heading(f"Step 4 of {STEPS} · iPhone & WhatsApp")
        if devicebackup.is_relocated(self.loc, root):
            c.say("   ✓ Your iPhone already backs up to this SSD. Back it up again in Finder before you downgrade.")
            return
        c.say("   WhatsApp's iCloud backup can't be copied, but a backup of your iPhone includes all your chats.")
        if not c.yes("   Save your iPhone backups on this SSD from now on?"):
            return
        try:
            c.say("   " + devicebackup.relocate(self.loc, root).replace("\n", "\n   "))
        except Exception as exc:  # never lose the copy results over this step
            c.say(f"   ✗ Couldn't set this up: {exc}")
            return
        c.say("\n   Now back up your iPhone:")
        c.say("     1. Connect it to this Mac (SSD still plugged in) and select it in the Finder sidebar.")
        c.say("     2. Choose “Back up all of the data on your iPhone to this Mac”.")
        c.say("     3. Tick “Encrypt local backup” and pick a password you'll remember.")
        c.say("     4. Click “Back Up Now”.")

    def finish(self, root: Path, results: List[SectionResult]) -> int:
        c = self.c
        c.heading("Result")
        report = readiness.build_report(self.loc, root, photos_client=self.photos)
        for check in report.checks:
            if check.status == readiness.MANUAL:
                continue
            mark = "✓" if check.status == readiness.OK else "✗"
            c.say(f"   {mark} {check.name}: {check.detail}")
            if check.fix and check.status != readiness.OK:
                c.say(f"       → {check.fix}")
        c.say(f"\n   Your backup: {root}")
        if report.ready:
            c.say("   🎉 Everything is safely on your SSD. “About this backup.txt” in that folder explains")
            c.say("   how to free up iCloud space and downgrade your plan.")
        else:
            c.say("   Almost there — finish the ✗ items above, then run iGround again to check.")
            c.say("   Don't delete anything from iCloud until every line has a ✓.")
        if c.yes("\n   Open the backup in Finder?"):
            self._quiet(["open", str(root)])
        return 0 if report.ready else 1

    # -- helpers ---------------------------------------------------------------

    def keep_awake(self) -> None:
        if shutil.which("caffeinate"):
            try:
                self.popen(["caffeinate", "-i", "-w", str(os.getpid())])
            except OSError:
                pass

    def _app_running(self, name: str) -> bool:
        if not shutil.which("pgrep"):
            return False
        return self.run(["pgrep", "-x", name], capture_output=True).returncode == 0

    def _quiet(self, cmd) -> None:
        if shutil.which(cmd[0]):
            try:
                self.run(cmd, capture_output=True)
            except OSError:
                pass


def _readable(path: Path) -> bool:
    try:
        with os.scandir(path) as it:
            next(it, None)
        return True
    except PermissionError:
        return False
    except OSError:
        return True
