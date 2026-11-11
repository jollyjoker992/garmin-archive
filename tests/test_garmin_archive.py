"""Tests run against a local stub of the Garmin Personal API. No network needed.

    python3 -m unittest discover -s tests -v
"""

import datetime as dt
import json
import os
import shutil
import sys
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import garmin_archive as ga  # noqa: E402


class StubGarmin(BaseHTTPRequestHandler):
    """Answers every GET with a small JSON body; behaviour is tuned per test."""

    fail_next: list[int] = []      # status codes to return before succeeding
    always_fail_path: str | None = None
    always_429 = False
    token_status = 200
    hits: list[str] = []

    def log_message(self, *args):
        pass

    def _reply(self, status, body=b"", headers=None):
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        cls = type(self)
        cls.hits.append("POST " + self.path)
        if cls.token_status != 200:
            return self._reply(cls.token_status)
        self._reply(200, b"stub-token")

    def do_GET(self):
        cls = type(self)
        cls.hits.append(self.path)
        if cls.always_fail_path and cls.always_fail_path in self.path:
            return self._reply(503)
        if cls.always_429:
            return self._reply(429)
        if cls.fail_next:
            return self._reply(cls.fail_next.pop(0), headers={"Retry-After": "0"})
        if "menstrualcycle" in self.path:
            return self._reply(204)
        if "search/activities" in self.path:
            has = "startDate=2026-01-02" in self.path
            body = [{"activityId": 77, "activityType": {"typeKey": "running"}}] if has else []
            return self._reply(200, json.dumps(body).encode())
        if "download-service" in self.path:
            return self._reply(200, b"PK\x03\x04fake-zip")
        self._reply(200, json.dumps({"path": self.path}).encode())


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), StubGarmin)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        StubGarmin.fail_next = []
        StubGarmin.always_fail_path = None
        StubGarmin.always_429 = False
        StubGarmin.token_status = 200
        StubGarmin.hits = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved_env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.saved_env)))
        for key in [k for k in os.environ if k.startswith("GARMIN_")]:
            del os.environ[key]
        os.environ.update(
            GARMIN_PERSONAL_API_KEY="test-key",
            GARMIN_ARCHIVE_DIR=self.tmp.name,
            GARMIN_BASE_URL=f"http://127.0.0.1:{self.server.server_address[1]}",
            GARMIN_MIN_INTERVAL="0",
            GARMIN_BACKOFF_BASE="0",
            GARMIN_RATE_LIMIT_BACKOFF="0",
            GARMIN_RATE_LIMIT_COOLDOWN="0",
            GARMIN_MAX_ATTEMPTS="3",
            GARMIN_LOG_LEVEL="CRITICAL",
        )

    def run_cli(self, *args):
        return ga.main(["--env-file", os.devnull, *args])

    def manifest(self):
        return json.loads((Path(self.tmp.name) / "manifest.json").read_text())


class RunTests(Base):
    def test_crawls_archives_and_fills_gaps(self):
        self.assertEqual(self.run_cli("run", "--from", "2026-01-01", "--to", "2026-01-03"), 0)
        days = self.manifest()["days"]
        self.assertEqual(sorted(days), ["2026-01-01", "2026-01-02", "2026-01-03"])
        self.assertTrue(all(d["status"] == "final" for d in days.values()))

        day = Path(self.tmp.name) / "days/2026/2026-01-02"
        self.assertTrue((day / "sleep.json").is_file())
        self.assertTrue((day / "_day.json").is_file())
        self.assertEqual((day / "activities/77/original.zip").read_bytes(), b"PK\x03\x04fake-zip")
        self.assertFalse((day / "menstrual_cycle_summary.json").exists())  # 204: recorded, no file
        self.assertEqual(days["2026-01-02"]["endpoints"]["menstrual_cycle_summary"]["http"], 204)
        self.assertEqual(days["2026-01-02"]["activities"][0]["id"], 77)

        archive = Path(self.tmp.name) / days["2026-01-02"]["archive"]["file"]
        with tarfile.open(archive) as tar:
            self.assertIn("2026-01-02/activities/77/detail.json", tar.getnames())
        self.assertEqual(ga.sha256_file(archive), days["2026-01-02"]["archive"]["sha256"])

        # A second run has nothing to do and makes no API calls.
        StubGarmin.hits = []
        self.assertEqual(self.run_cli("run", "--from", "2026-01-01", "--to", "2026-01-03"), 0)
        self.assertEqual(StubGarmin.hits, [])

        # Widening the range only fetches the new day.
        self.assertEqual(self.run_cli("run", "--from", "2025-12-31", "--to", "2026-01-03"), 0)
        self.assertFalse(any("2026-01-0" in h for h in StubGarmin.hits))
        self.assertTrue(any("2025-12-31" in h for h in StubGarmin.hits))
        self.assertIn("2025-12-31", self.manifest()["days"])

    def test_retries_then_succeeds(self):
        StubGarmin.fail_next = [429, 503]
        self.assertEqual(self.run_cli("run", "--from", "2026-01-01", "--to", "2026-01-01"), 0)
        self.assertEqual(self.manifest()["days"]["2026-01-01"]["status"], "final")

    def test_rate_limited_day_cools_down_and_is_retried(self):
        # Three 429s exhaust one request's attempts (GARMIN_MAX_ATTEMPTS=3); after a
        # cool-down the same day is crawled again and succeeds.
        StubGarmin.fail_next = [429, 429, 429]
        self.assertEqual(self.run_cli("run", "--from", "2026-01-01", "--to", "2026-01-02"), 0)
        days = self.manifest()["days"]
        self.assertEqual((days["2026-01-01"]["status"], days["2026-01-02"]["status"]), ("final", "final"))
        self.assertEqual(days["2026-01-02"]["attempts"], 2)         # newest first: 01-02 hit the 429s
        self.assertNotIn("error", days["2026-01-02"])

    def test_persistent_rate_limit_stops_after_cooldowns(self):
        os.environ["GARMIN_RATE_LIMIT_COOLDOWNS"] = "2"
        StubGarmin.always_429 = True
        self.assertEqual(self.run_cli("run", "--from", "2026-01-01", "--to", "2026-01-02"), 1)
        days = self.manifest()["days"]
        self.assertEqual(days["2026-01-02"]["attempts"], 3)         # first try + 2 cool-downs
        self.assertIn("HTTP 429", days["2026-01-02"]["error"])
        self.assertNotIn("2026-01-01", days)                         # crawl stopped, resumes next run

    def test_failed_day_is_recorded_and_retried(self):
        StubGarmin.always_fail_path = "/hrv-service/hrv/2026-01-01"
        self.assertEqual(self.run_cli("run", "--from", "2026-01-01", "--to", "2026-01-02"), 1)
        days = self.manifest()["days"]
        self.assertEqual(days["2026-01-01"]["status"], "failed")
        self.assertEqual(days["2026-01-02"]["status"], "final")
        self.assertFalse((Path(self.tmp.name) / "days/2026/2026-01-01").exists())

        StubGarmin.always_fail_path = None
        self.assertEqual(self.run_cli("run", "--from", "2026-01-01", "--to", "2026-01-02"), 0)
        entry = self.manifest()["days"]["2026-01-01"]
        self.assertEqual((entry["status"], entry["attempts"]), ("final", 2))
        self.assertNotIn("error", entry)

    def test_recent_day_stays_provisional_and_unarchived(self):
        yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        self.assertEqual(self.run_cli("run", "--from", yesterday), 0)
        entry = self.manifest()["days"][yesterday]
        self.assertEqual(entry["status"], "provisional")
        self.assertNotIn("archive", entry)

    def test_refetch_of_old_archived_day_is_refused(self):
        os.environ["GARMIN_REFETCH_MAX_AGE_DAYS"] = "30"
        old = (dt.date.today() - dt.timedelta(days=40)).isoformat()
        self.assertEqual(self.run_cli("run", "--from", old, "--to", old), 0)
        before = self.manifest()["days"][old]
        sleep_file = Path(self.tmp.name) / "days" / old[:4] / old / "sleep.json"
        sleep_file.write_text('{"marker": "archived detail"}')     # stands in for detail Garmin no longer serves
        StubGarmin.hits = []

        self.assertEqual(self.run_cli("run", "--from", old, "--to", old, "--refetch", old), 2)
        self.assertEqual(StubGarmin.hits, [])                        # Garmin not called
        self.assertEqual(sleep_file.read_text(), '{"marker": "archived detail"}')
        self.assertEqual(self.manifest()["days"][old]["archive"], before["archive"])

        self.assertEqual(self.run_cli("run", "--from", old, "--to", old, "--refetch", old, "--allow-old-refetch"), 0)
        self.assertNotIn("marker", sleep_file.read_text())

    def test_refetch_allowed_for_recent_or_never_archived_days(self):
        os.environ["GARMIN_REFETCH_MAX_AGE_DAYS"] = "30"
        recent = (dt.date.today() - dt.timedelta(days=5)).isoformat()
        never = (dt.date.today() - dt.timedelta(days=200)).isoformat()
        self.run_cli("run", "--from", recent, "--to", recent)
        self.assertEqual(self.run_cli("run", "--from", recent, "--to", recent, "--refetch", recent), 0)
        self.assertEqual(self.run_cli("run", "--from", never, "--to", never, "--refetch", never), 0)

    def test_today_is_excluded_by_default(self):
        today = dt.date.today().isoformat()
        self.assertEqual(self.run_cli("run", "--to", today), 2)       # refused without the setting
        yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        self.assertEqual(self.run_cli("run", "--from", yesterday), 0)
        self.assertNotIn(today, self.manifest()["days"])

    def test_include_today_crawls_today_as_provisional(self):
        os.environ.update(GARMIN_INCLUDE_TODAY="true", GARMIN_SETTLE_DAYS="0")   # even with no settle time
        today = dt.date.today().isoformat()
        self.assertEqual(self.run_cli("run", "--from", today), 0)
        entry = self.manifest()["days"][today]
        self.assertEqual(entry["status"], "provisional")             # today is never final
        self.assertNotIn("archive", entry)                           # so never archived or uploaded
        self.assertEqual(self.run_cli("run", "--from", today, "--refetch", today), 0)
        self.assertEqual(self.manifest()["days"][today]["attempts"], 2)

    def test_rejected_key_exits_2(self):
        for status in (401, 403, 404):                   # 404 = key deleted on Garmin Connect
            with self.subTest(status=status):
                StubGarmin.token_status = status
                (Path(self.tmp.name) / ".token.json").unlink(missing_ok=True)
                self.assertEqual(self.run_cli("run", "--from", "2026-01-01", "--to", "2026-01-01"), 2)
                self.assertIn(f"rejected the Personal API key (HTTP {status}", self.manifest()["last_run"]["error"])

    def test_refetch_replaces_archive(self):
        self.run_cli("run", "--from", "2026-01-01", "--to", "2026-01-01")
        first = self.manifest()["days"]["2026-01-01"]
        self.assertEqual(self.run_cli("run", "--from", "2026-01-01", "--to", "2026-01-01",
                                      "--refetch", "2026-01-01", "--allow-old-refetch"), 0)
        second = self.manifest()["days"]["2026-01-01"]
        self.assertEqual(second["attempts"], 2)
        self.assertGreaterEqual(second["fetched_at"], first["fetched_at"])
        self.assertTrue((Path(self.tmp.name) / second["archive"]["file"]).is_file())

    def use_fake_rclone(self):
        root = Path(self.tmp.name)
        self.gdrive, self.s3 = root / "remote-gdrive", root / "remote-s3"
        self.calls = root / "rclone-calls.txt"
        os.environ.update(GARMIN_RCLONE_BIN=str(Path(__file__).with_name("fake_rclone.py")),
                          GARMIN_GDRIVE_REMOTE=str(self.gdrive), GARMIN_S3_REMOTE=str(self.s3),
                          FAKE_RCLONE_LOG=str(self.calls))

    def test_upload_uses_rclone_and_is_recorded(self):
        self.use_fake_rclone()
        self.assertEqual(self.run_cli("run", "--from", "2026-01-01", "--to", "2026-01-01"), 0)
        uploads = self.manifest()["days"]["2026-01-01"]["uploads"]
        self.assertEqual(uploads["s3"]["remote"], f"{self.s3}/archives/2026/2026-01-01.tar.gz")
        self.assertEqual(uploads["s3"]["storage_class"], "GLACIER")
        self.assertIn("gdrive", uploads)
        for remote in (self.gdrive, self.s3):
            self.assertTrue((remote / "archives/2026/2026-01-01.tar.gz").is_file())
            self.assertTrue((remote / "manifest.json").is_file())
        lines = self.calls.read_text().splitlines()
        batches = [l for l in lines if l.startswith("copy ") and "--files-from" in l]
        self.assertEqual(len(batches), 2)                            # one batched call per target
        self.assertTrue(any("--s3-storage-class GLACIER" in l for l in batches))
        self.assertTrue(any(l.startswith("copyto") and l.split()[2].endswith("manifest.json") and "STANDARD" in l for l in lines))

        # Second run: nothing to upload again.
        self.calls.write_text("")
        self.run_cli("run", "--from", "2026-01-01", "--to", "2026-01-01")
        self.assertFalse(any(l.startswith("copy ") for l in self.calls.read_text().splitlines()))

    def test_new_machine_adopts_remote_manifest(self):
        self.use_fake_rclone()
        self.run_cli("run", "--from", "2026-01-01", "--to", "2026-01-02")
        # Simulate a new machine: the local data directory is gone, the remotes stay.
        root = Path(self.tmp.name)
        for name in ("days", "archives", "manifest.json"):
            path = root / name
            shutil.rmtree(path) if path.is_dir() else path.unlink()
        StubGarmin.hits = []
        self.calls.write_text("")
        self.assertEqual(self.run_cli("run", "--from", "2026-01-01", "--to", "2026-01-03"), 0)
        days = self.manifest()["days"]
        # The two uploaded days are adopted, not crawled or uploaded again; only the new day is crawled.
        self.assertEqual(days["2026-01-01"]["adopted_from"], "gdrive")
        self.assertEqual(set(days["2026-01-01"]["uploads"]), {"gdrive", "s3"})
        self.assertFalse(any("2026-01-01" in h or "2026-01-02" in h for h in StubGarmin.hits))
        self.assertTrue(any("2026-01-03" in h for h in StubGarmin.hits))
        batches = [l for l in self.calls.read_text().splitlines() if l.startswith("copy ")]
        self.assertEqual(len(batches), 2)                           # 2026-01-03, one batch per target
        for remote in (self.gdrive, self.s3):
            self.assertTrue((remote / "archives/2026/2026-01-03.tar.gz").is_file())
        self.assertEqual(set(days["2026-01-03"]["uploads"]), {"gdrive", "s3"})
        status = json.loads(self._status_json())
        self.assertEqual(status["cloud_only"], ["2026-01-01", "2026-01-02"])

    def test_restore_brings_back_cloud_only_days(self):
        self.use_fake_rclone()
        self.run_cli("run", "--from", "2026-01-01", "--to", "2026-01-02")
        root = Path(self.tmp.name)
        original = (root / "days/2026/2026-01-02/sleep.json").read_bytes()
        for name in ("days", "archives", "manifest.json"):
            path = root / name
            shutil.rmtree(path) if path.is_dir() else path.unlink()
        StubGarmin.hits = []
        self.assertEqual(self.run_cli("restore", "--from", "2026-01-01", "--to", "2026-01-02"), 0)
        self.assertEqual((root / "days/2026/2026-01-02/sleep.json").read_bytes(), original)
        self.assertTrue((root / "days/2026/2026-01-02/activities/77/original.zip").is_file())
        self.assertEqual(StubGarmin.hits, [])                       # Garmin is not called
        self.assertEqual(json.loads(self._status_json())["cloud_only"], [])

    def test_restore_rejects_a_corrupt_download(self):
        self.use_fake_rclone()
        self.run_cli("run", "--from", "2026-01-01", "--to", "2026-01-01")
        root = Path(self.tmp.name)
        shutil.rmtree(root / "days")
        shutil.rmtree(root / "archives")
        remote_copy = self.gdrive / "archives/2026/2026-01-01.tar.gz"
        remote_copy.write_bytes(b"x" * remote_copy.stat().st_size)  # same size, wrong content
        self.assertEqual(self.run_cli("restore", "--from", "2026-01-01", "--to", "2026-01-01"), 1)
        self.assertFalse((root / "days/2026/2026-01-01").exists())

    def _status_json(self):
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.run_cli("status", "--from", "2026-01-01", "--to", "2026-01-03", "--json")
        return out.getvalue()

    def test_missing_remote_file_is_uploaded_again(self):
        self.use_fake_rclone()
        self.run_cli("run", "--from", "2026-01-01", "--to", "2026-01-01")
        (self.s3 / "archives/2026/2026-01-01.tar.gz").unlink()      # someone deleted it on S3
        self.calls.write_text("")
        self.assertEqual(self.run_cli("run", "--from", "2026-01-01", "--to", "2026-01-01"), 0)
        self.assertTrue((self.s3 / "archives/2026/2026-01-01.tar.gz").is_file())
        self.assertEqual(self.manifest()["remote_sync"]["s3"]["dropped"], 1)
        batches = [l for l in self.calls.read_text().splitlines() if l.startswith("copy ")]
        self.assertEqual(len(batches), 1)                            # only to s3, gdrive still has it
        self.assertIn(str(self.s3), batches[0])

    def test_missing_local_archive_is_copied_from_other_target(self):
        self.use_fake_rclone()
        self.run_cli("run", "--from", "2026-01-01", "--to", "2026-01-01")
        root = Path(self.tmp.name)
        shutil.rmtree(root / "archives")                            # local archive and day folder gone
        shutil.rmtree(root / "days")
        (self.s3 / "archives/2026/2026-01-01.tar.gz").unlink()      # and missing on S3
        self.calls.write_text("")
        self.assertEqual(self.run_cli("run", "--from", "2026-01-01", "--to", "2026-01-01"), 0)
        copies = [l.split() for l in self.calls.read_text().splitlines() if l.startswith("copyto") and ".tar.gz" in l]
        self.assertEqual(copies[0][1], f"{self.gdrive}/archives/2026/2026-01-01.tar.gz")
        self.assertTrue((self.s3 / "archives/2026/2026-01-01.tar.gz").is_file())

    def test_partly_refused_batch_records_only_what_arrived(self):
        self.use_fake_rclone()
        os.environ["FAKE_RCLONE_SKIP"] = "2026-01-02"
        self.assertEqual(self.run_cli("run", "--from", "2026-01-01", "--to", "2026-01-02"), 1)
        days = self.manifest()["days"]
        self.assertEqual(set(days["2026-01-01"]["uploads"]), {"gdrive", "s3"})
        self.assertNotIn("uploads", days["2026-01-02"])
        del os.environ["FAKE_RCLONE_SKIP"]
        self.assertEqual(self.run_cli("run", "--from", "2026-01-01", "--to", "2026-01-02"), 0)
        self.assertEqual(set(self.manifest()["days"]["2026-01-02"]["uploads"]), {"gdrive", "s3"})

    def test_rebuilt_identical_archive_keeps_uploads(self):
        self.use_fake_rclone()
        self.run_cli("run", "--from", "2026-01-01", "--to", "2026-01-01")
        shutil.rmtree(Path(self.tmp.name) / "archives")             # day folder still there
        self.calls.write_text("")
        self.assertEqual(self.run_cli("run", "--from", "2026-01-01", "--to", "2026-01-01"), 0)
        self.assertFalse(any(l.startswith("copyto") and ".tar.gz" in l for l in self.calls.read_text().splitlines()))

    def test_no_remote_manifest_falls_back_to_local(self):
        self.use_fake_rclone()
        self.assertEqual(self.run_cli("run", "--from", "2026-01-01", "--to", "2026-01-01"), 0)
        sync = self.manifest()["remote_sync"]
        self.assertEqual((sync["gdrive"]["remote_manifest"], sync["gdrive"]["adopted"]), (False, 0))

    def test_s3_style_missing_manifest_exit_0_is_no_manifest(self):
        self.use_fake_rclone()
        os.environ["FAKE_RCLONE_MISSING_EXIT"] = "0"
        self.assertEqual(self.run_cli("run", "--from", "2026-01-01", "--to", "2026-01-01"), 0)
        self.assertEqual(self.manifest()["remote_sync"]["s3"]["remote_manifest"], False)

    def test_unreadable_remote_keeps_local_state_and_exits_1(self):
        self.use_fake_rclone()
        self.run_cli("run", "--from", "2026-01-01", "--to", "2026-01-01")
        os.environ["FAKE_RCLONE_FAIL"] = "1"
        self.assertEqual(self.run_cli("run", "--from", "2026-01-01", "--to", "2026-01-02"), 1)
        days = self.manifest()["days"]
        self.assertEqual(days["2026-01-02"]["status"], "final")      # crawling still happened
        self.assertIn("gdrive", days["2026-01-01"]["uploads"])       # upload records kept
        self.assertIn("error", self.manifest()["remote_sync"]["gdrive"])


class UnitTests(unittest.TestCase):
    def test_backoff_grows_and_respects_cap_and_retry_after(self):
        for attempt, ceiling in ((1, 2), (2, 4), (3, 8), (10, 120)):
            delay = ga.backoff_delay(attempt, 2, 120)
            self.assertTrue(ceiling / 2 <= delay <= ceiling, (attempt, delay))
        self.assertEqual(ga.backoff_delay(1, 2, 120, retry_after=45), 45)
        self.assertEqual(ga.backoff_delay(1, 2, 120, retry_after=99999), 900)

    def test_compress_ranges(self):
        self.assertEqual(ga.compress_ranges([]), "none")
        self.assertEqual(
            ga.compress_ranges(["2026-01-01", "2026-01-02", "2026-01-05"]),
            "2026-01-01..2026-01-02, 2026-01-05",
        )

    def test_env_file_does_not_override(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write("# comment\nGA_TEST_A='from file'\nexport GA_TEST_B=two\n")
        self.addCleanup(os.unlink, fh.name)
        os.environ["GA_TEST_A"] = "from env"
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("GA_TEST_A", "GA_TEST_B")])
        ga.load_env_file(Path(fh.name))
        self.assertEqual(os.environ["GA_TEST_A"], "from env")
        self.assertEqual(os.environ["GA_TEST_B"], "two")


if __name__ == "__main__":
    unittest.main()
