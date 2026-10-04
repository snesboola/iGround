import http.client
import json
import os
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path

from iground import layout
from iground.app.server import AppServer
from iground.app.service import Service, ServiceError
from iground.photos import PhotosError

from test_everything import Home, make_photos, write_backup


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


class AppHome(Home):
    def setUp(self):
        super().setUp()
        self.svc = Service(self.loc, self.volumes, self.photos, run=lambda *a, **k: None, popen=lambda *a, **k: None)

    def settle(self):
        """Wait for the background overview refresh to finish and return a fresh state."""
        def fresh():
            st = self.svc.state()
            return not self.svc.refreshing and st["overview"] is not None
        wait_for(fresh)
        return self.svc.state()

    def sections(self):
        return {s["key"]: s for s in self.settle()["overview"]["sections"]}

    def run_job(self, start):
        start()
        wait_for(lambda: self.svc.job.state in ("done", "stopped", "failed"))
        return self.svc.job


class ServiceTests(AppHome):
    def test_overview_before_any_backup(self):
        st = self.settle()
        self.assertEqual(st["drive"]["name"], "MySSD")
        self.assertIsNone(st["backup"])
        secs = {s["key"]: s for s in st["overview"]["sections"]}
        self.assertEqual(list(secs), ["photos", "drive", "apps", "messages", "iphone"])
        self.assertEqual(secs["photos"]["headline"], "4 photos & videos")
        self.assertEqual(secs["drive"]["status"], "todo")
        self.assertEqual([c["label"] for c in secs["apps"]["choices"]], ["Pages", "Notes"])
        self.assertEqual([c["id"] for c in secs["drive"]["choices"]], ["Docs"])
        self.assertEqual([c["label"] for c in secs["photos"]["choices"]], ["2023", "2021", "Unknown date"])
        self.assertEqual(secs["iphone"]["action"]["do"], "iphone-setup")
        self.assertFalse(st["overview"]["ready"])

    def test_full_journey(self):
        job = self.run_job(self.svc.start_backup)
        self.assertEqual(job.state, "done", job.message)
        snap = job.snapshot()
        self.assertEqual(snap["sections"]["photos"]["done"], 4)
        self.assertEqual(snap["sections"]["apps"]["total"], 2)  # both apps rolled into one row
        secs = self.sections()
        self.assertEqual({k: s["status"] for k, s in secs.items()},
                         {"photos": "done", "drive": "done", "apps": "done", "messages": "done", "iphone": "todo"})
        self.assertEqual(self.svc.state()["overview"]["remaining"], 1)

        self.svc.iphone("setup")
        self.assertIn("back up your iPhone", self.svc.state()["notice"]["text"])
        root = layout.resolve(self.drive)
        self.assertEqual(self.sections()["iphone"]["detail"], "Now back up your iPhone in Finder")
        write_backup(root / "iPhone Backups" / "0000", "Sam's iPhone", datetime.now())
        self.svc.refresh()
        st = self.settle()
        self.assertTrue(st["overview"]["ready"], st["overview"])
        self.assertTrue(st["backup"]["complete"])

    def test_stop_then_continue(self):
        gate = threading.Event()
        original = self.photos.export

        def slow_export(jobs, timeout):
            gate.wait(5)
            return original(jobs, timeout)

        self.photos.export = slow_export
        self.svc.start_backup()
        wait_for(lambda: self.svc.job.current == "photos")
        self.svc.stop()
        self.assertEqual(self.svc.job.state, "stopping")
        gate.set()
        wait_for(lambda: self.svc.job.state in ("stopped", "done"))
        self.assertEqual(self.svc.job.state, "stopped")
        self.assertFalse(self.svc.state()["backup"]["complete"])
        self.assertEqual(self.sections()["drive"]["status"], "todo")  # never reached

        self.photos.export = original
        job = self.run_job(self.svc.start_backup)
        self.assertEqual(job.state, "done")
        self.assertEqual(self.sections()["drive"]["status"], "done")

    def test_verify_finds_damage_and_update_repairs(self):
        self.run_job(self.svc.start_backup)
        root = layout.resolve(self.drive)
        (root / "iCloud Drive" / "Docs" / "cv.pdf").write_bytes(b"XX")  # same size, different bytes
        job = self.run_job(self.svc.start_verify)
        self.assertEqual(job.state, "done")
        self.assertIn("1 file on the SSD is damaged", job.message)
        self.assertEqual(job.snapshot()["sections"]["drive"]["status"], "error")
        self.assertEqual(self.sections()["drive"]["status"], "todo")
        self.run_job(self.svc.start_backup)
        self.assertEqual((root / "iCloud Drive" / "Docs" / "cv.pdf").read_bytes(), b"cv")
        job = self.run_job(self.svc.start_verify)
        self.assertIn("no problems", job.message)

    def test_settings_turn_sections_off(self):
        self.svc.update_settings(kinds=["photos", "drive"])
        secs = self.sections()
        self.assertEqual(secs["messages"]["status"], "off")
        self.run_job(self.svc.start_backup)
        self.assertFalse((layout.resolve(self.drive) / "Messages").exists())
        self.assertFalse(self.settle()["overview"]["ready"])
        with self.assertRaises(ServiceError):
            self.svc.update_settings(kinds=[])

    def test_busy_and_missing_drive_are_explained(self):
        os.rename(self.drive, self.volumes / ".gone")
        self.svc.state()
        with self.assertRaisesRegex(ServiceError, "Plug in"):
            self.svc.start_backup()
        with self.assertRaisesRegex(ServiceError, "no backup"):
            self.svc.start_verify()

    def test_photos_permission_problem(self):
        def denied():
            raise PhotosError("Not authorized to send Apple events to Photos. (-1743)")
        self.photos.list_items = denied
        sec = self.sections()["photos"]
        self.assertEqual(sec["status"], "error")
        self.assertEqual(sec["action"]["open"], "automation")

    def test_open_only_inside_backup(self):
        with self.assertRaises(ServiceError):
            self.svc.open("backup")  # nothing yet
        self.run_job(self.svc.start_backup)
        self.settle()
        self.svc.open("backup")
        with self.assertRaises(ServiceError):
            self.svc.open("/etc")

    def test_drive_choice(self):
        other = self.volumes / "Other"
        other.mkdir()
        self.assertEqual(self.settle()["drive"]["name"], "MySSD")
        self.svc.select_drive(str(other))
        self.assertEqual(self.settle()["drive"]["name"], "Other")
        with self.assertRaises(ServiceError):
            self.svc.select_drive("/nope")


class ChoiceTests(AppHome):
    def setUp(self):
        super().setUp()
        (self.loc.drive / "Old Projects").mkdir()
        (self.loc.drive / "Old Projects" / "big.mov").write_bytes(b"m" * 1000)
        (self.loc.drive / "todo.txt").write_text("loose")

    def test_skipped_things_are_not_copied_and_dont_block_ready(self):
        self.svc.update_settings(skip={"drive": ["Old Projects"], "apps": ["Notes"], "years": ["2021"]})
        secs = self.sections()
        self.assertEqual(secs["drive"]["skipped"], 1)
        self.assertIn("1 folder skipped", secs["drive"]["detail"])
        self.assertEqual(secs["photos"]["count"], 2)  # 2023 Live Photo + unknown-date scan
        self.assertEqual([c["on"] for c in secs["apps"]["choices"]], [True, False])

        job = self.run_job(self.svc.start_backup)
        self.assertEqual(job.state, "done", job.message)
        root = layout.resolve(self.drive)
        self.assertTrue((root / "iCloud Drive" / "Docs" / "cv.pdf").exists())
        self.assertTrue((root / "iCloud Drive" / "todo.txt").exists())
        self.assertFalse((root / "iCloud Drive" / "Old Projects").exists())
        self.assertFalse((root / "App Documents" / "Notes").exists())
        self.assertFalse((root / "Photos" / "2021").exists())
        self.assertTrue((root / "Photos" / "2023").exists())

        self.svc.iphone("setup")
        write_backup(root / "iPhone Backups" / "0000", "iPhone", datetime.now())
        self.svc.refresh()
        st = self.settle()
        self.assertTrue(st["overview"]["ready"], [(s["key"], s["status"], s["detail"]) for s in st["overview"]["sections"]])
        self.assertTrue(st["overview"]["partial"])

    def test_loose_files_and_whole_categories(self):
        self.svc.update_settings(kinds=["drive"], skip={"drive": ["__files__"]})
        secs = self.sections()
        self.assertEqual(secs["photos"]["status"], "off")
        self.assertEqual(secs["drive"]["choices"][-1]["label"], "Files not in a folder")
        self.run_job(self.svc.start_backup)
        root = layout.resolve(self.drive)
        self.assertFalse((root / "iCloud Drive" / "todo.txt").exists())
        self.assertTrue((root / "iCloud Drive" / "Old Projects" / "big.mov").exists())
        self.assertFalse((root / "Photos").exists())

    def test_choices_are_remembered(self):
        self.svc.update_settings(kinds=["photos", "drive"], skip={"years": ["2021"]}, evict=True)
        again = Service(self.loc, self.volumes, self.photos)
        st = again.state()
        self.assertEqual(st["settings"]["kinds"], ["drive", "photos"])
        self.assertEqual(st["settings"]["skip"]["years"], ["2021"])
        self.assertTrue(st["settings"]["evict"])

    def test_changing_choices_is_instant_and_does_not_reread_icloud(self):
        self.settle()
        calls = []
        original = self.photos.list_items
        self.photos.list_items = lambda: (calls.append(1), original())[1]
        self.svc.update_settings(kinds=["photos", "drive"], skip={"years": ["2021"]})
        st = self.svc.state()  # no waiting: the answer already reflects the change
        secs = {s["key"]: s for s in st["overview"]["sections"]}
        self.assertEqual(secs["messages"]["status"], "off")
        self.assertEqual(secs["photos"]["count"], 2)
        self.assertEqual(calls, [])  # Photos library wasn't read again
        self.svc.refresh()  # an explicit refresh does re-read it
        self.settle()
        self.assertEqual(len(calls), 1)

    def test_tip_dismissal_is_remembered(self):
        self.assertFalse(self.svc.state()["settings"]["tip_seen"])
        self.svc.update_settings(tip_seen=True)
        self.assertTrue(Service(self.loc, self.volumes, self.photos).state()["settings"]["tip_seen"])

    def test_bad_choices_rejected(self):
        with self.assertRaises(ServiceError):
            self.svc.update_settings(kinds=["whatsapp"])
        with self.assertRaises(ServiceError):
            self.svc.update_settings(skip="everything")


class ServerTests(AppHome):
    def setUp(self):
        super().setUp()
        self.server = AppServer(self.svc, token="secret")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res.status, data

    def test_page_needs_token(self):
        self.assertEqual(self.request("GET", "/")[0], 403)
        status, page = self.request("GET", "/?t=secret")
        self.assertEqual(status, 200)
        self.assertIn(b'const TOKEN = "secret"', page)

    def test_api_needs_token_and_local_host(self):
        self.assertEqual(self.request("GET", "/api/state")[0], 403)
        self.assertEqual(self.request("POST", "/api/backup", {})[0], 403)
        evil = {"X-IGround-Token": "secret", "Host": "evil.example:80"}
        self.assertEqual(self.request("GET", "/api/state", headers=evil)[0], 403)
        status, data = self.request("GET", "/api/state", headers={"X-IGround-Token": "secret"})
        self.assertEqual(status, 200)
        self.assertIn("drives", json.loads(data))

    def test_actions_and_errors(self):
        auth = {"X-IGround-Token": "secret", "Content-Type": "application/json"}
        status, data = self.request("POST", "/api/verify", {}, auth)
        self.assertEqual(status, 409)
        self.assertIn("no backup", json.loads(data)["error"])
        status, data = self.request("POST", "/api/backup", {}, auth)
        self.assertEqual(status, 200)
        wait_for(lambda: self.svc.job.state == "done")
        self.assertEqual(self.request("POST", "/api/nope", {}, auth)[0], 404)


if __name__ == "__main__":
    unittest.main()
