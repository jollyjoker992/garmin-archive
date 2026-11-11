"""Tests for the FIT helpers in tools/medplum_agent.py. No network, no FIT SDK needed.

    python3 -m unittest discover -s tests -v
"""

import datetime as dt
import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
if not (TOOLS / "medplum_agent.py").is_file():          # e.g. inside the Docker image, which has no tools/
    raise unittest.SkipTest("tools/medplum_agent.py is not present")
sys.path.insert(0, str(TOOLS))
import medplum_agent as ma  # noqa: E402

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 9, 30, 11, 20, 41, tzinfo=UTC)


def run_messages(sport="running"):
    return {
        "session_mesgs": [{"sport": sport}],
        "record_mesgs": [
            {"timestamp": T0, "position_lat": 119304647, "position_long": 238609294, "distance": 0.0,
             "enhanced_speed": 2.5, "enhanced_altitude": 4.19, "heart_rate": 94, "cadence": 80,
             "fractional_cadence": 0.5, "power": 210, "stance_time": 250.0},
            {"timestamp": T0 + dt.timedelta(seconds=1), "distance": 2.5, "enhanced_speed": 0.0, "heart_rate": 95},
        ],
        "lap_mesgs": [{"start_time": T0, "total_distance": 1000.0, "avg_heart_rate": 140, "intensity": "active",
                       "unknown_field": 1}],
    }


class FitTests(unittest.TestCase):
    def test_records_units_and_local_time(self):
        rows = ma.fit_records(run_messages(), "+07:00")
        first, second = rows
        self.assertEqual(first["time"], "2026-09-30T18:20:41+07:00")
        self.assertEqual((first["elapsed_s"], second["elapsed_s"]), (0, 1))
        self.assertEqual(first["cadence_spm"], 161.0)                 # 2 x (80 + 0.5) steps/min
        self.assertEqual(first["pace_min_per_km"], 6.67)              # 1000 / 2.5 m/s / 60
        self.assertIsNone(second["pace_min_per_km"])                  # standing still: no pace
        self.assertAlmostEqual(first["lat"], 10.0, places=5)          # semicircles -> degrees
        self.assertAlmostEqual(first["lon"], 20.0, places=5)
        self.assertEqual(first["altitude_m"], 4.2)

    def test_no_gps_and_unused_columns_dropped(self):
        rows = ma.fit_records(run_messages(), "+07:00", gps=False)
        self.assertNotIn("lat", rows[0])
        self.assertNotIn("temperature_c", rows[0])                    # never present in this file

    def test_cycling_cadence_stays_rpm(self):
        rows = ma.fit_records(run_messages("cycling"), "+00:00")
        self.assertEqual(rows[0]["cadence_rpm"], 80)
        self.assertNotIn("cadence_spm", rows[0])

    def test_laps(self):
        laps = ma.fit_laps(run_messages(), "+07:00")
        self.assertEqual(laps, [{"lap": 1, "start": "2026-09-30T18:20:41+07:00", "total_distance": 1000.0,
                                 "avg_heart_rate": 140, "intensity": "active"}])


if __name__ == "__main__":
    unittest.main()
