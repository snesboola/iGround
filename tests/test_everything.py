import io
import json
import os
import plistlib
import subprocess
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from iground import cli, devicebackup, layout, readiness
from iground.backup import BackupOptions, prepare, run_backup
from iground.migrate import MigrationError
from iground.photos import PhotoItem, PhotosClient, PhotosError, PhotosExporter, place_file
from iground.sources import Locations, folder_sources, parse_kinds
from iground.ui import Console
from iground.wizard import Wizard, list_drives


class FakePhotos(PhotosClient):
    """Simulates Photos.app: each item exports to one or more files."""

    def __init__(self, items, outputs, fail=(), albums=None):
        self.runner = None
        self.osascript = "/usr/bin/osascript"
        self.items = items
        self.outputs = outputs  # id -> {filename: bytes}
        self.fail = set(fail)
        self.album_list = albums if albums is not None else [
            {"name": "Holiday", "folder": ["Trips"], "items": ["p1", "p3"]},
            {"name": "Mum: 60th", "folder": [], "items": ["p2"]},
        ]
        self.export_calls = 0

    def list_items(self):
        return list(self.items)

    def albums(self):
        return self.album_list

    def export(self, jobs, item_timeout):
        self.export_calls += 1
        res = {}
        for pid, folder in jobs:
            if pid in self.fail:
                res[pid] = "Photos got an error: can't download original"
                continue
            for name, data in self.outputs[pid].items():
                (Path(folder) / name).write_bytes(data)
            res[pid] = None
        return res


def ts(y, m, d):
    return datetime(y, m, d, 12).timestamp()


def make_photos(fail=()):
    items = [
        PhotoItem("p1", ts(2021, 7, 4), "IMG_0001.HEIC", favorite=True),
        PhotoItem("p2", ts(2021, 7, 5), "IMG_0001.HEIC"),  # same name, different photo
        PhotoItem("p3", ts(2023, 1, 9), "IMG_0300.HEIC"),  # Live Photo -> 2 files
        PhotoItem("p4", None, "scan.png"),
    ]
    outputs = {
        "p1": {"IMG_0001.HEIC": b"one"},
        "p2": {"IMG_0001.HEIC": b"two"},
        "p3": {"IMG_0300.HEIC": b"still", "IMG_0300.MOV": b"motion"},
        "p4": {"scan.png": b"png"},
    }
    return FakePhotos(items, outputs, fail)


def write_backup(folder: Path, device: str, when: datetime, encrypted=True):
    folder.mkdir(parents=True)
    with open(folder / "Info.plist", "wb") as fh:
        plistlib.dump({"Device Name": device, "Last Backup Date": when, "Product Type": "iPhone15,2"}, fh)
    with open(folder / "Manifest.plist", "wb") as fh:
        plistlib.dump({"IsEncrypted": encrypted}, fh)
    (folder / "ab").mkdir()
    (folder / "ab" / "abcdef").write_bytes(b"whatsapp chats")


class Home(unittest.TestCase):
    """A fake Mac: home folder with iCloud Drive, app folders and Messages, plus an SSD."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.loc = Locations(root / "home")
        self.volumes = root / "Volumes"
        self.drive = self.volumes / "MySSD"
        self.drive.mkdir(parents=True)
        os.symlink("/", self.volumes / "Macintosh HD")
        md = self.loc.mobile_documents
        (self.loc.drive / "Docs").mkdir(parents=True)
        (self.loc.drive / "Docs" / "cv.pdf").write_bytes(b"cv")
        (md / "com~apple~Pages" / "Documents").mkdir(parents=True)
        (md / "com~apple~Pages" / "Documents" / "Essay.pages").write_bytes(b"essay")
        (md / "iCloud~com~example~Notes" / "Documents").mkdir(parents=True)
        (md / "iCloud~com~example~Notes" / "Documents" / "n.txt").write_text("note")
        (self.loc.messages / "Attachments").mkdir(parents=True)
        (self.loc.messages / "chat.db").write_bytes(b"sqlite")
        (self.loc.messages / "Attachments" / "pic.jpg").write_bytes(b"jpg")
        self.env = mock.patch.dict(os.environ, {"IGROUND_HOME": str(self.loc.home)})
        self.env.start()
        self.photos = make_photos()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def patched(self):
        photos = self.photos
        return [
            mock.patch("iground.backup.PhotosClient", return_value=photos),
            mock.patch("iground.readiness.PhotosClient", return_value=photos),
            mock.patch("iground.cli.PhotosClient", return_value=photos),
        ]

    def run_cli(self, *argv):
        out = io.StringIO()
        patches = self.patched()
        for p in patches:
            p.start()
        try:
            code = cli.main(list(argv), out=out)
        finally:
            for p in patches:
                p.stop()
        return code, out.getvalue()


class SourcesTests(Home):
    def test_sources_have_friendly_names(self):
        got = [(s.kind, s.dest_rel) for s in folder_sources(self.loc)]
        self.assertEqual(got, [
            ("drive", "iCloud Drive"),
            ("apps", "App Documents/Pages"),
            ("apps", "App Documents/Notes"),
            ("messages", "Messages"),
        ])

    def test_clashing_app_names_use_full_name(self):
        (self.loc.mobile_documents / "iCloud~org~other~Notes").mkdir()
        names = [s.dest_rel for s in folder_sources(self.loc, ["apps"])]
        self.assertIn("App Documents/com.example.Notes", names)
        self.assertIn("App Documents/org.other.Notes", names)

    def test_parse_kinds(self):
        self.assertEqual(list(parse_kinds("photos, drive")), ["photos", "drive"])
        with self.assertRaises(ValueError):
            parse_kinds("whatsapp")


class LayoutTests(Home):
    def test_one_dated_folder_renamed_on_update(self):
        first = layout.open_backup(self.drive, today=date(2026, 9, 1))
        self.assertTrue(first.created)
        self.assertEqual(first.root.name, "iCloud Backup 2026-09-01")
        same = layout.open_backup(self.drive, today=date(2026, 9, 1))
        self.assertEqual(same.root, first.root)
        later = layout.open_backup(self.drive, today=date(2026, 10, 4))
        self.assertEqual(later.root.name, "iCloud Backup 2026-10-04")
        self.assertEqual(later.previous, first.root)
        self.assertFalse(first.root.exists())
        self.assertEqual(layout.resolve(self.drive), later.root)

    def test_new_backup_is_separate(self):
        a = layout.open_backup(self.drive, today=date(2026, 10, 4))
        b = layout.open_backup(self.drive, today=date(2026, 10, 4), new=True)
        self.assertEqual(b.root.name, "iCloud Backup 2026-10-04 (2)")
        self.assertTrue(a.root.exists())

    def test_resolve_errors_without_backup(self):
        with self.assertRaises(FileNotFoundError):
            layout.resolve(self.drive)


class PhotosTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name) / "Photos"
        self.state = Path(self.tmp.name) / ".iground" / "Photos"

    def tearDown(self):
        self.tmp.cleanup()

    def export(self, client, **kw):
        return PhotosExporter(self.dest, client, min_free=0, state_dir=self.state, **kw).run()

    def test_month_folders_albums_and_favourites(self):
        res = self.export(make_photos(), batch_size=2)
        self.assertEqual((res.total, res.exported, res.failed, res.files), (4, 4, 0, 5))
        self.assertTrue(res.album_folders)
        july = self.dest / "2021" / "07 July"
        self.assertEqual((july / "IMG_0001.HEIC").read_bytes(), b"one")
        self.assertEqual((july / "IMG_0001 (2).HEIC").read_bytes(), b"two")
        self.assertTrue((self.dest / "2023/01 January/IMG_0300.MOV").exists())
        self.assertTrue((self.dest / "Unknown date/scan.png").exists())
        self.assertEqual(int((self.dest / "2023/01 January/IMG_0300.HEIC").stat().st_mtime), int(ts(2023, 1, 9)))
        # Albums and favourites are hard links: real-looking files, no extra space.
        holiday = self.dest / "Albums" / "Trips" / "Holiday"
        self.assertEqual(sorted(p.name for p in holiday.iterdir()),
                         ["IMG_0001.HEIC", "IMG_0300.HEIC", "IMG_0300.MOV"])
        self.assertTrue(os.path.samefile(holiday / "IMG_0001.HEIC", july / "IMG_0001.HEIC"))
        self.assertTrue((self.dest / "Albums" / "Mum- 60th" / "IMG_0001 (2).HEIC").exists())
        self.assertTrue(os.path.samefile(self.dest / "Favourites" / "IMG_0001.HEIC", july / "IMG_0001.HEIC"))
        # Bookkeeping stays out of the Photos folder.
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()),
                         ["2021", "2023", "Albums", "Favourites", "Unknown date"])
        doc = json.loads((self.state / "albums.json").read_text())
        self.assertEqual(doc["favourites"], ["2021/07 July/IMG_0001.HEIC"])

    def test_album_changes_are_synced_without_touching_other_files(self):
        client = make_photos()
        self.export(client)
        mine = self.dest / "Albums" / "Trips" / "my notes.txt"
        mine.write_text("mine")
        client.album_list = [{"name": "Holiday", "folder": ["Trips"], "items": ["p3"]}]
        self.export(client)
        holiday = self.dest / "Albums" / "Trips" / "Holiday"
        self.assertEqual(sorted(p.name for p in holiday.iterdir()), ["IMG_0300.HEIC", "IMG_0300.MOV"])
        self.assertFalse((self.dest / "Albums" / "Mum- 60th").exists())
        self.assertTrue(mine.exists())
        self.assertTrue((self.dest / "2021/07 July/IMG_0001.HEIC").exists())

    def test_resume_and_retry_failures(self):
        client = make_photos(fail={"p3"})
        res = self.export(client)
        self.assertEqual((res.exported, res.failed), (3, 1))
        client.fail.clear()
        res = self.export(client)
        self.assertEqual((res.exported, res.skipped, res.failed), (1, 3, 0))
        self.assertEqual(len(list((self.dest / "2021/07 July").iterdir())), 2)

    def test_no_hard_links_falls_back_gracefully(self):
        with mock.patch("iground.photos.os.link", side_effect=OSError("not supported")):
            res = self.export(make_photos())
        self.assertFalse(res.album_folders)
        self.assertFalse((self.dest / "Albums").exists())
        self.assertTrue((self.state / "albums.json").exists())

    def test_dry_run(self):
        client = make_photos()
        res = PhotosExporter(self.dest, client, min_free=0, state_dir=self.state).run(dry_run=True)
        self.assertEqual((res.total, res.skipped), (4, 0))
        self.assertEqual(client.export_calls, 0)
        self.assertFalse(self.dest.exists())

    def test_stops_when_ssd_full(self):
        with self.assertRaises(PhotosError):
            PhotosExporter(self.dest, make_photos(), min_free=10 ** 18, state_dir=self.state).run()

    def test_place_file_reuses_identical_file(self):
        folder = self.dest / "x"
        folder.mkdir(parents=True)
        (folder / "a.jpg").write_bytes(b"same")
        src = self.dest / "a.jpg"
        src.write_bytes(b"same")
        final, _ = place_file(src, folder)
        self.assertEqual(final, folder / "a.jpg")
        self.assertFalse(src.exists())

    def test_osascript_output_parsing(self):
        out = "OK\tid1\nERR\tid2\tcan't get\noriginal\n"
        runner = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, out, "")
        client = PhotosClient(runner=runner)
        client.osascript = "osascript"
        res = client.export([("id1", Path("/a")), ("id2", Path("/b")), ("id3", Path("/c"))], 60)
        self.assertIsNone(res["id1"])
        self.assertEqual(res["id2"], "can't get original")
        self.assertIn("no result", res["id3"])

    def test_permission_error_explained(self):
        runner = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "Not authorized to send Apple events (-1743)")
        client = PhotosClient(runner=runner)
        client.osascript = "osascript"
        with self.assertRaisesRegex(PhotosError, "Automation"):
            client.list_items()


class DeviceBackupTests(Home):
    def test_relocate_moves_existing_backups_and_symlinks(self):
        root = layout.open_backup(self.drive).root
        old = self.loc.mobilesync_backup
        write_backup(old / "0000-AAAA", "Sam's iPhone", datetime.now())
        devicebackup.relocate(self.loc, root)
        self.assertTrue(old.is_symlink())
        self.assertTrue(devicebackup.is_relocated(self.loc, root))
        backups = devicebackup.list_backups(root / "iPhone Backups")
        self.assertEqual([(b.device, b.encrypted) for b in backups], [("Sam's iPhone", True)])
        self.assertEqual(sorted(p.name for p in (root / "iPhone Backups").iterdir()), ["0000-AAAA"])
        self.assertTrue(old.with_name("Backup.before-iground").is_dir())
        self.assertIn("Already set up", devicebackup.relocate(self.loc, root))

        devicebackup.undo(self.loc)
        self.assertFalse(old.is_symlink())
        self.assertTrue((old / "0000-AAAA" / "Info.plist").exists())

    def test_link_follows_renamed_backup_folder(self):
        first = prepare(self.loc, self.drive).root
        devicebackup.relocate(self.loc, first)
        with mock.patch("iground.layout.date") as fake_date:
            fake_date.today.return_value = date.today() + timedelta(days=30)
            later = prepare(self.loc, self.drive).root
        self.assertNotEqual(first, later)
        self.assertTrue(devicebackup.is_relocated(self.loc, later))

    def test_refuses_foreign_symlink(self):
        root = layout.open_backup(self.drive).root
        self.loc.mobilesync_backup.parent.mkdir(parents=True)
        os.symlink("/somewhere/else", self.loc.mobilesync_backup)
        with self.assertRaises(MigrationError):
            devicebackup.relocate(self.loc, root)


class BackupFolderTests(Home):
    def test_backup_folder_is_tidy(self):
        root = prepare(self.loc, self.drive).root
        results = run_backup(self.loc, root, BackupOptions(), photos_client=self.photos)
        self.assertTrue(all(r.ok for r in results), results)
        self.assertEqual(root.name, f"iCloud Backup {date.today().isoformat()}")
        visible = sorted(p.name for p in root.iterdir() if not p.name.startswith("."))
        self.assertEqual(visible, ["About this backup.txt", "App Documents", "Messages", "Photos", "iCloud Drive"])
        self.assertEqual((root / "iCloud Drive/Docs/cv.pdf").read_bytes(), b"cv")
        self.assertTrue((root / "App Documents/Pages/Documents/Essay.pages").exists())
        self.assertTrue((root / "Photos/2023/01 January/IMG_0300.HEIC").exists())
        hidden = [p for p in root.rglob(".*") if p.relative_to(root).parts[0] != ".iground"]
        self.assertEqual(hidden, [])
        about = (root / "About this backup.txt").read_text()
        self.assertIn("4 photos & videos", about)
        self.assertIn("Status: Complete", about)
        self.assertIn("Freeing up iCloud space", about)


class CLIFlowTests(Home):
    def test_full_journey_to_ready(self):
        code, out = self.run_cli("audit", str(self.drive))
        self.assertEqual(code, 0, out)
        self.assertIn("Pages documents", out)

        code, out = self.run_cli("backup", str(self.drive), "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(list(self.drive.iterdir()), [])

        code, out = self.run_cli("backup", str(self.drive))
        self.assertEqual(code, 0, out)
        root = layout.resolve(self.drive)

        code, out = self.run_cli("ready", str(self.drive))
        self.assertEqual(code, 1, out)
        self.assertIn("✗ iPhone backup", out)
        self.assertIn("✓ Photos: all 4 photos & videos copied", out)

        code, out = self.run_cli("iphone-backup", str(self.drive))
        self.assertEqual(code, 0, out)
        write_backup(root / "iPhone Backups" / "0000-AAAA", "iPhone", datetime.now())

        code, out = self.run_cli("ready", str(self.drive))
        self.assertEqual(code, 0, out)
        self.assertIn("READY", out)

        code, out = self.run_cli("verify", str(self.drive))
        self.assertEqual(code, 0, out)
        code, out = self.run_cli("status", str(root))
        self.assertIn("4 photos & videos", out)
        self.assertIn("(complete)", out)

        # Something new appears -> not ready until the next backup run.
        self.photos.items.append(PhotoItem("p5", ts(2024, 2, 2), "new.HEIC"))
        self.photos.outputs["p5"] = {"new.HEIC": b"new"}
        (self.loc.drive / "Docs" / "cv.pdf").write_bytes(b"cv v2!")
        code, out = self.run_cli("ready", str(self.drive))
        self.assertEqual(code, 1)
        self.assertIn("1 of 5 photos & videos not copied yet", out)
        self.assertIn("changed since they were copied", out)
        code, out = self.run_cli("backup", str(self.drive))
        self.assertEqual(code, 0, out)
        self.assertEqual((root / "iCloud Drive/Docs/cv.pdf").read_bytes(), b"cv v2!")
        self.assertEqual(len(layout.find_backups(self.drive)), 1)

    def test_stale_backup_not_ready(self):
        root = prepare(self.loc, self.drive).root
        write_backup(root / "iPhone Backups" / "X", "Old iPhone", datetime.now() - timedelta(days=60))
        report = readiness.build_report(self.loc, root, ["drive"], self.photos)
        backup = [c for c in report.checks if c.name.startswith("iPhone")][0]
        self.assertEqual(backup.status, readiness.MISSING)

    def test_failed_photo_exit_code_and_status(self):
        self.photos = make_photos(fail={"p2"})
        code, out = self.run_cli("backup", str(self.drive), "--only", "photos")
        self.assertEqual(code, 1, out)
        code, out = self.run_cli("status", str(self.drive), "--failed")
        self.assertIn("photo p2", out)

    def test_errors_are_friendly(self):
        code, _ = self.run_cli("backup", str(self.drive), "--only", "whatsapp")
        self.assertEqual(code, 2)
        code, _ = self.run_cli("ready", str(self.drive))
        self.assertEqual(code, 2)


class WizardTests(Home):
    def wizard(self, answers):
        answers = list(answers)
        self.asked = []

        def ask(prompt):
            self.asked.append(prompt)
            return answers.pop(0)

        out = io.StringIO()
        ran = []
        run = lambda cmd, **kw: (ran.append(cmd), subprocess.CompletedProcess(cmd, 1, b"", b""))[1]
        w = Wizard(Console(out, ask), self.loc, self.volumes, self.photos, run=run, popen=lambda *a, **k: None)
        return w, out, ran

    def test_lists_only_external_drives(self):
        self.assertEqual([p.name for p, _ in list_drives(self.volumes)], ["MySSD"])

    def test_guided_journey(self):
        # use drive? start? iPhone backups on SSD? open in Finder?
        w, out, ran = self.wizard(["", "", "y", "n"])
        code = w.start()
        text = out.getvalue()
        self.assertEqual(code, 1, text)  # not ready: the iPhone hasn't been backed up yet
        root = layout.resolve(self.drive)
        self.assertTrue((root / "Photos/2021/07 July/IMG_0001.HEIC").exists())
        self.assertIn("Step 4 of 4", text)
        self.assertIn("✓ Photos: all 4 photos & videos copied", text)
        self.assertIn("✗ iPhone backup", text)
        self.assertIn("Back Up Now", text)
        self.assertTrue(devicebackup.is_relocated(self.loc, root))

        # After backing up the phone, running again says everything is safe and opens Finder.
        write_backup(root / "iPhone Backups" / "0000-AAAA", "iPhone", datetime.now())
        w, out, ran = self.wizard(["", "", "y"])
        code = w.start()
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("will be brought up to date", out.getvalue())
        self.assertIn("Everything is safely on your SSD", out.getvalue())

    def test_declining_changes_nothing(self):
        w, out, _ = self.wizard(["", "n"])
        self.assertEqual(w.start(), 1)
        self.assertEqual(list(self.drive.iterdir()), [])

    def test_no_drive_lets_you_quit(self):
        os.rename(self.drive, self.volumes / ".hidden")
        w, out, _ = self.wizard(["q"])
        self.assertEqual(w.start(), 1)
        self.assertIn("can't see an external drive", out.getvalue())

    def test_typed_folder_is_accepted(self):
        os.rename(self.drive, self.volumes / ".hidden")
        folder = Path(self.tmp.name) / "Elsewhere"
        folder.mkdir()
        w, out, _ = self.wizard([f"'{folder}' ", "n"])
        self.assertEqual(w.start(), 1)
        self.assertIn("Elsewhere", out.getvalue())


if __name__ == "__main__":
    unittest.main()
