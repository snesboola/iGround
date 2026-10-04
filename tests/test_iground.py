import io
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from iground import cli, icloud
from iground import manifest as mf
from iground.migrate import MigrationError, Migrator, Options, verify
from iground.scanner import Kind, State, scan


class FakeCloud(icloud.ICloudClient):
    """Simulates iCloud: `remote` maps real paths to content still in the cloud."""

    def __init__(self, remote=None):
        super().__init__(brctl="/usr/bin/brctl")
        self.remote = dict(remote or {})
        self.downloads = []
        self.evictions = []

    def download(self, path, timeout):
        self.downloads.append(Path(path))
        content = self.remote.pop(Path(path))
        Path(path).write_bytes(content)
        stub = Path(path).with_name(f".{Path(path).name}.icloud")
        if stub.exists():
            stub.unlink()

    def evict(self, path):
        self.evictions.append(Path(path))


def write_placeholder(path: Path, size: int) -> None:
    stub = path.with_name(f".{path.name}.icloud")
    stub.parent.mkdir(parents=True, exist_ok=True)
    with open(stub, "wb") as fh:
        plistlib.dump({"NSURLName": path.name, "NSURLFileSizeKey": size}, fh, fmt=plistlib.FMT_BINARY)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.src = root / "CloudDocs"
        self.dst = root / "SSD" / "iCloud Drive"
        (self.src / "Docs" / "Deep").mkdir(parents=True)
        (self.src / "a.txt").write_text("alpha")
        (self.src / "Docs" / "b.txt").write_text("bravo" * 1000)
        (self.src / "Docs" / "Deep" / "c.bin").write_bytes(os.urandom(50_000))
        (self.src / ".DS_Store").write_text("junk")
        self.remote_path = self.src / "Docs" / "remote.pdf"
        self.remote_bytes = b"%PDF" + os.urandom(10_000)
        write_placeholder(self.remote_path, len(self.remote_bytes))
        self.cloud = FakeCloud({self.remote_path: self.remote_bytes})

    def tearDown(self):
        self.tmp.cleanup()

    def migrate(self, **kw):
        return Migrator(self.src, self.dst, Options(**kw), self.cloud).run()


class ScanTests(Base):
    def test_scan_finds_files_and_placeholders(self):
        entries = {e.rel_path: e for e in scan(self.src)}
        self.assertEqual(set(entries), {"a.txt", "Docs/b.txt", "Docs/Deep/c.bin", "Docs/remote.pdf"})
        remote = entries["Docs/remote.pdf"]
        self.assertEqual(remote.state, State.PLACEHOLDER)
        self.assertEqual(remote.size, len(self.remote_bytes))
        self.assertEqual(remote.path, self.remote_path)

    def test_exclude_glob_skips_folder(self):
        rels = {e.rel_path for e in scan(self.src, [".DS_Store", "Deep"])}
        self.assertNotIn("Docs/Deep/c.bin", rels)

    def test_anchored_exclude_only_matches_top_level(self):
        (self.src / "Deep").mkdir()
        (self.src / "Deep" / "x.txt").write_text("top")
        rels = {e.rel_path for e in scan(self.src, [".DS_Store", "/Deep"])}
        self.assertNotIn("Deep/x.txt", rels)
        self.assertIn("Docs/Deep/c.bin", rels)  # same name deeper down is kept

    def test_stale_placeholder_ignored_when_real_file_exists(self):
        write_placeholder(self.src / "a.txt", 5)
        entries = [e for e in scan(self.src) if e.rel_path == "a.txt"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].state, State.LOCAL)

    def test_dataless_flag(self):
        st = mock.Mock(st_flags=icloud.SF_DATALESS)
        self.assertTrue(icloud.is_dataless(st))
        self.assertFalse(icloud.is_dataless(mock.Mock(st_flags=0)))


class MigrateTests(Base):
    def test_full_migration_copies_and_verifies(self):
        res = self.migrate()
        self.assertEqual((res.copied, res.failed), (4, 0))
        self.assertEqual((self.dst / "Docs" / "remote.pdf").read_bytes(), self.remote_bytes)
        self.assertEqual((self.dst / "Docs/Deep/c.bin").read_bytes(), (self.src / "Docs/Deep/c.bin").read_bytes())
        self.assertFalse((self.dst / ".DS_Store").exists())
        self.assertEqual(self.cloud.downloads, [self.remote_path])
        self.assertEqual(
            (self.dst / "a.txt").stat().st_mtime_ns, (self.src / "a.txt").stat().st_mtime_ns
        )
        with mf.Manifest(mf.default_state_dir(self.dst)) as m:
            self.assertEqual(m.counts()[mf.VERIFIED]["files"], 4)

    def test_rerun_resumes_and_picks_up_changes(self):
        self.migrate()
        res = self.migrate()
        self.assertEqual((res.copied, res.skipped), (0, 4))
        (self.src / "a.txt").write_text("alpha v2")
        os.utime(self.src / "a.txt", ns=(1, 1))
        res = self.migrate()
        self.assertEqual((res.copied, res.skipped), (1, 3))
        self.assertEqual((self.dst / "a.txt").read_text(), "alpha v2")

    def test_dry_run_changes_nothing(self):
        events = []
        res = Migrator(self.src, self.dst, Options(dry_run=True), self.cloud,
                       lambda ev, e, d: events.append((ev, e.rel_path))).run()
        self.assertFalse(self.dst.exists())
        self.assertEqual(self.cloud.downloads, [])
        self.assertEqual(len([e for e in events if e[0] == "planned"]), 4)
        self.assertEqual(res.planned.cloud_files, 1)

    def test_failed_download_does_not_stop_others(self):
        self.cloud.remote.clear()  # download will KeyError
        res = self.migrate()
        self.assertEqual((res.copied, res.failed), (3, 1))
        with mf.Manifest(mf.default_state_dir(self.dst)) as m:
            self.assertEqual(m.get("Docs/remote.pdf").status, mf.FAILED)
        self.cloud.remote[self.remote_path] = self.remote_bytes
        res = self.migrate()
        self.assertEqual((res.copied, res.failed, res.skipped), (1, 0, 3))

    def test_evict_after_only_after_verified_copy(self):
        res = self.migrate(evict_after=True)
        self.assertEqual(res.evicted, 4)
        with mf.Manifest(mf.default_state_dir(self.dst)) as m:
            self.assertEqual(m.counts()[mf.EVICTED]["files"], 4)
        res = self.migrate(evict_after=True, verify=False)
        self.assertEqual(res.copied, 0)  # already done; nothing re-evicted

    def test_evict_requires_brctl(self):
        self.cloud.brctl = None
        with self.assertRaises(MigrationError):
            self.migrate(evict_after=True)

    def test_refuses_nested_destination(self):
        with self.assertRaises(MigrationError):
            Migrator(self.src, self.src / "backup", Options(), self.cloud)

    def test_space_check(self):
        with mock.patch("iground.migrate.shutil.disk_usage", return_value=mock.Mock(free=10)):
            with self.assertRaises(MigrationError):
                self.migrate()

    def test_symlinks_recreated(self):
        os.symlink("a.txt", self.src / "link")
        self.migrate()
        self.assertEqual(os.readlink(self.dst / "link"), "a.txt")
        self.assertEqual([e.kind for e in scan(self.src) if e.rel_path == "link"], [Kind.SYMLINK])

    def test_no_partial_files_left(self):
        self.migrate()
        leftovers = [p for p in self.dst.rglob("*") if p.name.endswith(".iground-partial")]
        self.assertEqual(leftovers, [])


class VerifyTests(Base):
    def test_verify_detects_corruption_missing_and_unmigrated(self):
        self.migrate()
        res = verify(self.dst, self.src)
        self.assertEqual((res.ok, res.mismatched, res.missing_on_dest, res.not_migrated), (4, [], [], []))

        (self.dst / "a.txt").write_text("bitrot")
        (self.dst / "Docs" / "b.txt").unlink()
        (self.src / "new.txt").write_text("new")
        res = verify(self.dst, self.src)
        self.assertEqual(res.mismatched, ["a.txt"])
        self.assertEqual(res.missing_on_dest, ["Docs/b.txt"])
        self.assertEqual(res.not_migrated, ["new.txt"])

        # A corrupted file is marked failed, so the next migrate re-copies it.
        res = self.migrate()
        self.assertEqual((self.dst / "a.txt").read_text(), "alpha")


class ICloudClientTests(unittest.TestCase):
    def test_download_polls_until_local(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "f.txt"
            calls = []

            def runner(cmd, **kw):
                calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, "", "")

            def sleep(_):
                target.write_text("arrived")

            client = icloud.ICloudClient(brctl="brctl", runner=runner, sleep=sleep)
            client.download(target, timeout=10)
            self.assertEqual(calls, [["brctl", "download", str(target)]])
            self.assertTrue(target.exists())

    def test_download_times_out(self):
        t = [0.0]

        def clock():
            t[0] += 5
            return t[0]

        ok = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", "")
        client = icloud.ICloudClient(brctl="brctl", runner=ok, sleep=lambda _: None, clock=clock)
        with self.assertRaises(icloud.DownloadError):
            client.download(Path("/nonexistent/never"), timeout=20)

    def test_brctl_error_surfaces(self):
        bad = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "no such item")
        client = icloud.ICloudClient(brctl="brctl", runner=bad)
        with self.assertRaisesRegex(icloud.DownloadError, "no such item"):
            client.download(Path("/nonexistent/x"), timeout=1)


if __name__ == "__main__":
    unittest.main()
