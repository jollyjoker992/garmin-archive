"""Tests for garmin_scheduler.py. No network.

    python3 -m unittest discover -s tests -v
"""

import datetime as dt
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import garmin_scheduler as gs  # noqa: E402

BKK = ZoneInfo("Asia/Bangkok")


class TimeTests(unittest.TestCase):
    def test_parse_times(self):
        self.assertEqual(gs.parse_times("18:15, 06:15,06:15"), [(6, 15), (18, 15)])
        for bad in ("", "6", "25:00", "06:60", "ab:cd"):
            with self.assertRaises(ValueError):
                gs.parse_times(bad)

    def test_next_run_same_day_and_next_day(self):
        now = dt.datetime(2026, 11, 11, 5, 0, tzinfo=BKK)
        self.assertEqual(gs.next_run(now, [(6, 15)], BKK), dt.datetime(2026, 11, 11, 6, 15, tzinfo=BKK))
        now = dt.datetime(2026, 11, 11, 6, 15, tzinfo=BKK)              # exactly at the time: next day
        self.assertEqual(gs.next_run(now, [(6, 15)], BKK), dt.datetime(2026, 11, 12, 6, 15, tzinfo=BKK))
        now = dt.datetime(2026, 11, 11, 7, 0, tzinfo=BKK)
        self.assertEqual(gs.next_run(now, [(6, 15), (18, 15)], BKK), dt.datetime(2026, 11, 11, 18, 15, tzinfo=BKK))

    def test_next_run_uses_the_schedule_zone(self):
        now_utc = dt.datetime(2026, 11, 10, 23, 30, tzinfo=dt.timezone.utc)   # 06:30 in Bangkok
        nxt = gs.next_run(now_utc, [(6, 15)], BKK)
        self.assertEqual(nxt, dt.datetime(2026, 11, 12, 6, 15, tzinfo=BKK))
        self.assertEqual(nxt.astimezone(dt.timezone.utc).hour, 23)

    def test_next_run_across_dst(self):
        ny = ZoneInfo("America/New_York")
        now = dt.datetime(2026, 3, 8, 1, 0, tzinfo=ny)                 # DST starts 02:00 that day
        self.assertEqual(gs.next_run(now, [(6, 15)], ny).utcoffset(), dt.timedelta(hours=-4))


class RunTests(unittest.TestCase):
    def test_runs_on_start_records_status_and_stops_on_sigterm(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "ran"
            env = {**os.environ, "GARMIN_ARCHIVE_DIR": tmp, "GARMIN_SCHEDULE_TIME": "03:00",
                   "GARMIN_SCHEDULE_TZ": "Asia/Bangkok", "GARMIN_RUN_ON_START": "true",
                   "GARMIN_JOB": f"sh -c 'touch {marker}; exit 1'"}
            proc = subprocess.Popen([sys.executable, str(Path(gs.__file__))], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            status_path = Path(tmp) / "scheduler-status.json"
            deadline = time.time() + 20
            while time.time() < deadline:
                if status_path.exists() and "next_run" in json.loads(status_path.read_text()):
                    break
                time.sleep(0.1)
            proc.send_signal(signal.SIGTERM)
            out, _ = proc.communicate(timeout=20)
            self.assertEqual(proc.returncode, 0, out)
            self.assertTrue(marker.exists())
            status = json.loads(status_path.read_text())
            self.assertEqual((status["last_exit_code"], status["running"]), (1, False))
            self.assertTrue(status["next_run"].endswith("+07:00"))
            self.assertTrue((Path(tmp) / ".scheduler-heartbeat").exists())
            self.assertIn("Scheduler stopped", out)

    def test_bad_configuration_exits_2(self):
        env = {**os.environ, "GARMIN_SCHEDULE_TIME": "25:99"}
        proc = subprocess.run([sys.executable, str(Path(gs.__file__))], env=env, capture_output=True, text=True,
                              timeout=20)
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
