import io
import os
import plistlib
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from iground import cli, devicebackup, icloud, readiness
from iground import manifest as mf
from iground.migrate import MigrationError
from iground.photos import PhotoItem, PhotosClient, PhotosError, PhotosExporter, place_file
from iground.sources import Locations, folder_sources, friendly_container_name, parse_kinds


class FakePhotos(PhotosClient):
    """Simulates Photos.app: each item exports to one or more files."""

    def __init__(self, items, outputs, fail=()):
        self.runner = None
        self.osascript = "/usr/bin/osascript"
        self.items = items
        self.outputs = outputs  # id -> {filename: bytes}
        self.fail = set(fail)
        self.export_calls = 0

    def list_items(self):
        return list(self.items)

    def albums(self):
        return [{"name": "Holiday", "folder": ["Trips"], "items": ["p1"]}]

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


class Home(unittest.TestCase):
    """A fake macOS home folder with iCloud Drive, app folders, Messages and backups."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.loc = Locations(root / "home")
        self.ssd = root / "SSD" / "iCloud"
        (root / "SSD").mkdir()
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

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()


class SourcesTests(Home):
    def test_all_sources_discovered(self):
        labels = [(s.kind, s.dest_rel) for s in folder_sources(self.loc)]
        self.assertEqual(labels, [
            ("drive", "iCloud Drive"),
            ("apps", "iCloud App Folders/com.apple.Pages"),
            ("apps", "iCloud App Folders/com.example.Notes"),
            ("messages", "Messages"),
        ])

    def test_parse_kinds(self):
        self.assertEqual(list(parse_kinds("photos, drive")), ["photos", "drive"])
        with self.assertRaises(ValueError):
            parse_kinds("whatsapp")

    def test_friendly_name(self):
        self.assertEqual(friendly_container_name("iCloud~net~whatsapp~WhatsApp"), "net.whatsapp.WhatsApp")


class PhotosTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name) / "Photos"

    def tearDown(self):
        self.tmp.cleanup()

    def test_export_files_by_date_and_handles_name_clash(self):
        client = make_photos()
        res = PhotosExporter(self.dest, client, batch_size=2, min_free=0).run()
        self.assertEqual((res.total, res.exported, res.failed, res.files), (4, 4, 0, 5))
        self.assertEqual((self.dest / "2021/07/IMG_0001.HEIC").read_bytes(), b"one")
        self.assertEqual((self.dest / "2021/07/IMG_0001 (2).HEIC").read_bytes(), b"two")
        self.assertTrue((self.dest / "2023/01/IMG_0300.MOV").exists())
        self.assertTrue((self.dest / "Unknown date/scan.png").exists())
        self.assertEqual(int((self.dest / "2023/01/IMG_0300.HEIC").stat().st_mtime), int(ts(2023, 1, 9)))
        self.assertFalse((self.dest / ".iground" / "staging").exists())
        albums = __import__("json").loads((self.dest / "albums.json").read_text())
        self.assertEqual(albums["albums"][0]["files"], ["2021/07/IMG_0001.HEIC"])
        self.assertEqual(albums["favorites"], ["2021/07/IMG_0001.HEIC"])

    def test_resume_and_retry_failures(self):
        client = make_photos(fail={"p3"})
        res = PhotosExporter(self.dest, client, min_free=0).run()
        self.assertEqual((res.exported, res.failed), (3, 1))
        client.fail.clear()
        res = PhotosExporter(self.dest, client, min_free=0).run()
        self.assertEqual((res.exported, res.skipped, res.failed), (1, 3, 0))
        # Nothing duplicated by the second run.
        self.assertEqual(len(list((self.dest / "2021/07").iterdir())), 2)

    def test_dry_run(self):
        client = make_photos()
        res = PhotosExporter(self.dest, client, min_free=0).run(dry_run=True)
        self.assertEqual((res.total, res.skipped), (4, 0))
        self.assertEqual(client.export_calls, 0)
        self.assertFalse(self.dest.exists())

    def test_stops_when_ssd_full(self):
        with self.assertRaises(PhotosError):
            PhotosExporter(self.dest, make_photos(), min_free=10 ** 18).run()

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


def write_backup(folder: Path, device: str, when: datetime, encrypted=True):
    folder.mkdir(parents=True)
    with open(folder / "Info.plist", "wb") as fh:
        plistlib.dump({"Device Name": device, "Last Backup Date": when, "Product Type": "iPhone15,2"}, fh)
    with open(folder / "Manifest.plist", "wb") as fh:
        plistlib.dump({"IsEncrypted": encrypted}, fh)
    (folder / "ab").mkdir()
    (folder / "ab" / "abcdef").write_bytes(b"whatsapp chats")


class DeviceBackupTests(Home):
    def test_relocate_moves_existing_backups_and_symlinks(self):
        old = self.loc.mobilesync_backup
        write_backup(old / "0000-AAAA", "Sam's iPhone", datetime.now())
        msg = devicebackup.relocate(self.loc, self.ssd)
        self.assertIn("iPhone Backups", msg)
        self.assertTrue(old.is_symlink())
        self.assertTrue(devicebackup.is_relocated(self.loc, self.ssd))
        backups = devicebackup.list_backups(self.ssd / "iPhone Backups")
        self.assertEqual([(b.device, b.encrypted) for b in backups], [("Sam's iPhone", True)])
        self.assertFalse((self.ssd / "iPhone Backups" / ".iground").exists())
        self.assertTrue(old.with_name("Backup.before-iground").is_dir())  # originals kept
        self.assertIn("Already set up", devicebackup.relocate(self.loc, self.ssd))

        devicebackup.undo(self.loc)
        self.assertFalse(old.is_symlink())
        self.assertTrue((old / "0000-AAAA" / "Info.plist").exists())

    def test_relocate_without_existing_backups(self):
        devicebackup.relocate(self.loc, self.ssd)
        self.assertTrue(self.loc.mobilesync_backup.is_symlink())

    def test_refuses_foreign_symlink(self):
        self.loc.mobilesync_backup.parent.mkdir(parents=True)
        os.symlink("/somewhere/else", self.loc.mobilesync_backup)
        with self.assertRaises(MigrationError):
            devicebackup.relocate(self.loc, self.ssd)


class CLIFlowTests(Home):
    def run_cli(self, *argv, photos=None):
        out = io.StringIO()
        photos = photos or self.photos
        icloud_factory = lambda *a, **kw: icloud.ICloudClient(**kw) if kw else FakeICloud()
        with mock.patch("iground.cli.PhotosClient", return_value=photos), \
                mock.patch("iground.readiness.PhotosClient", return_value=photos), \
                mock.patch("iground.cli.ICloudClient", side_effect=icloud_factory), \
                mock.patch("iground.cli.messages_running", return_value=False):
            code = cli.main(list(argv), out=out)
        return code, out.getvalue()

    def setUp(self):
        super().setUp()
        self.photos = make_photos()

    def test_full_journey_to_ready(self):
        code, out = self.run_cli("audit", str(self.ssd.parent))
        self.assertEqual(code, 0, out)
        self.assertIn("App folder com.apple.Pages", out)
        self.assertIn("4 items", out)

        code, out = self.run_cli("migrate", str(self.ssd), "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertFalse(self.ssd.exists())

        code, out = self.run_cli("ready", str(self.ssd))
        self.assertEqual(code, 1)
        self.assertIn("NOT READY", out)

        code, out = self.run_cli("migrate", str(self.ssd))
        self.assertEqual(code, 0, out)
        self.assertEqual((self.ssd / "iCloud Drive/Docs/cv.pdf").read_bytes(), b"cv")
        self.assertTrue((self.ssd / "iCloud App Folders/com.apple.Pages/Documents/Essay.pages").exists())
        self.assertTrue((self.ssd / "Messages/Attachments/pic.jpg").exists())
        self.assertTrue((self.ssd / "Photos/2023/01/IMG_0300.MOV").exists())

        # Everything copied, but no phone backup yet -> still not ready.
        code, out = self.run_cli("ready", str(self.ssd))
        self.assertEqual(code, 1, out)
        self.assertIn("[TODO] iPhone / iPad backup", out)
        self.assertIn("[OK]   Photos", out)

        code, out = self.run_cli("iphone-backup", str(self.ssd))
        self.assertEqual(code, 0, out)
        write_backup(self.ssd / "iPhone Backups" / "0000-AAAA", "iPhone", datetime.now())

        code, out = self.run_cli("ready", str(self.ssd))
        self.assertEqual(code, 0, out)
        self.assertIn("READY", out)
        self.assertIn("WhatsApp", out)

        code, out = self.run_cli("verify", str(self.ssd))
        self.assertEqual(code, 0, out)
        code, out = self.run_cli("status", str(self.ssd))
        self.assertIn("4 items exported", out)

        # New photo in the library and an edited file -> not ready again.
        self.photos.items.append(PhotoItem("p5", ts(2024, 2, 2), "new.HEIC"))
        self.photos.outputs["p5"] = {"new.HEIC": b"new"}
        (self.loc.drive / "Docs" / "cv.pdf").write_bytes(b"cv v2!")
        code, out = self.run_cli("ready", str(self.ssd))
        self.assertEqual(code, 1)
        self.assertIn("1 item of 5 not exported", out)
        self.assertIn("changed since copied", out)

    def test_stale_backup_not_ready(self):
        write_backup(self.ssd / "iPhone Backups" / "X", "Old iPhone", datetime.now() - timedelta(days=60))
        report = readiness.build_report(self.loc, self.ssd, ["drive"], self.photos)
        backup = [c for c in report.checks if c.name.startswith("iPhone")][0]
        self.assertEqual(backup.status, readiness.MISSING)
        self.assertIn("older than", backup.detail)

    def test_only_flag_and_failed_photo_exit_code(self):
        code, out = self.run_cli("migrate", str(self.ssd), "--only", "photos", photos=make_photos(fail={"p2"}))
        self.assertEqual(code, 1, out)
        self.assertFalse((self.ssd / "iCloud Drive").exists())
        code, out = self.run_cli("status", str(self.ssd), "--failed")
        self.assertIn("photo p2", out)

    def test_bad_kind_is_usage_error(self):
        code, _ = self.run_cli("migrate", str(self.ssd), "--only", "whatsapp")
        self.assertEqual(code, 2)


class FakeICloud(icloud.ICloudClient):
    def __init__(self):
        super().__init__(brctl="/usr/bin/brctl")

    def download(self, path, timeout):
        pass

    def evict(self, path):
        pass


if __name__ == "__main__":
    unittest.main()
