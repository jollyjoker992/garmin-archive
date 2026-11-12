"""Tests for the calculations in tools/garmin_report.py. No network, and no
matplotlib/reportlab needed (only the PDF rendering uses them).

    python3 -m unittest discover -s tests -v
"""

import argparse
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
if not (TOOLS / "garmin_report.py").is_file():          # e.g. inside the Docker image, which has no tools/
    raise unittest.SkipTest("tools/garmin_report.py is not present")
sys.path.insert(0, str(TOOLS))
import garmin_report as gr  # noqa: E402


def rows(seconds=1300, speed=2.5, hr0=140, alt=10.0):
    """A synthetic run: constant speed, heart rate rising 1 bpm every 100 s."""
    out = []
    for s in range(seconds):
        out.append({"elapsed_s": s, "distance_m": s * speed, "speed_mps": speed, "heart_rate": hr0 + s // 100,
                    "cadence_spm": 160.0 + (s % 2), "power_w": 200, "altitude_m": alt + (s % 50) / 10})
    return out


class ReportTests(unittest.TestCase):
    def test_km_splits(self):
        splits = gr.km_splits(rows())                     # 1300 s x 2.5 m/s = 3250 m
        self.assertEqual([s["km"] for s in splits], [1, 2, 3, 4])
        self.assertEqual(splits[0]["time_s"], 400)
        self.assertEqual(splits[0]["pace_min_per_km"], 6.67)
        self.assertEqual(splits[-1]["distance_m"], 248)   # last sample at 3247.5 m: a 247.5 m tail split
        self.assertGreater(splits[-1]["avg_hr"], splits[0]["avg_hr"])
        self.assertIsInstance(splits[0]["avg_hr"], int)

    def test_tiny_tail_is_not_a_split(self):
        splits = gr.km_splits(rows(seconds=840))          # 2100 m: tail of 100 m dropped
        self.assertEqual(len(splits), 2)

    def test_formatting(self):
        self.assertEqual(gr.pace_text(8.416), "8:25/km")
        self.assertEqual(gr.pace_text(7.999), "8:00/km")
        self.assertEqual(gr.dur_text(2529.07), "42:09")
        self.assertEqual(gr.dur_text(3725), "1:02:05")

    def test_periods(self):
        ns = lambda **k: argparse.Namespace(**{"week": None, "month": None, "start": None, "end": None, **k})
        self.assertEqual(gr.period_from_args(ns(week="2026-w45"))[:2], (dt.date(2026, 11, 2), dt.date(2026, 11, 8)))
        self.assertEqual(gr.period_from_args(ns(month="2026-12"))[:2], (dt.date(2026, 12, 1), dt.date(2026, 12, 31)))
        self.assertEqual(gr.period_from_args(ns(month="2026-02"))[1], dt.date(2026, 2, 28))

    def test_aggregate(self):
        days = [{"sleep_min": 400, "sleep_score": 80, "hrv_ms": 50, "resting_hr": 55, "stress_avg": 30, "bb_max": 70,
                 "bb_min": 10, "steps": 8000, "step_goal": 7000, "moderate_min": 20, "vigorous_min": 10,
                 "hrv_status": "BALANCED", "deep_min": 60, "rem_min": 90, "spo2_avg": 97, "respiration_avg": 14},
                {"sleep_min": None, "sleep_score": None, "hrv_ms": 60, "resting_hr": 57, "stress_avg": 40, "bb_max": 50,
                 "bb_min": 5, "steps": 4000, "step_goal": 7000, "moderate_min": 0, "vigorous_min": 0,
                 "hrv_status": "LOW", "deep_min": None, "rem_min": None, "spo2_avg": None, "respiration_avg": None}]
        a = gr.aggregate(days, [{"minutes": 30, "distance_m": 5000}])
        self.assertEqual(a["sleep_min"], {"avg": 400.0, "min": 400, "max": 400, "days_with_data": 1})
        self.assertEqual(a["hrv_ms"]["avg"], 55.0)
        self.assertEqual(a["steps"]["total"], 12000)
        self.assertEqual(a["steps"]["days_goal_met"], 1)
        self.assertEqual(a["intensity_minutes_total"], 40)          # 20 + 2 x 10
        self.assertEqual(a["hrv_status_counts"], {"BALANCED": 1, "LOW": 1})
        self.assertEqual(a["workouts"], {"count": 1, "minutes": 30, "distance_km": 5.0})

    def test_partial_today_is_left_out_of_accumulating_averages(self):
        today = dt.date(2026, 11, 12)
        days = [{"date": "2026-11-11", "steps": 6000, "sleep_min": 400, "stress_avg": 30},
                {"date": "2026-11-12", "steps": 1500, "sleep_min": 420, "stress_avg": 20}]
        cleaned = gr.without_partial_today(days, today)
        self.assertEqual(cleaned[0], days[0])
        self.assertEqual((cleaned[1]["steps"], cleaned[1]["stress_avg"], cleaned[1]["sleep_min"]), (None, None, 420))
        a = gr.aggregate([{**d, **{k: d.get(k) for k in ("sleep_score", "hrv_ms")}} for d in cleaned], [])
        self.assertEqual(a["steps"]["avg"], 6000.0)                  # not dragged down by the morning's 1,500
        self.assertEqual(a["sleep_min"]["avg"], 410.0)                # last night's sleep still counts

    def test_vietnamese_labels_numbers_and_dates(self):
        L = gr.Labels("vi")
        self.assertEqual(L.fallback, [])                               # Vietnamese is complete
        self.assertEqual(L("summary"), "Tóm tắt")
        self.assertEqual(L.n(4174.7), "4.174,7")
        self.assertEqual(L.n(2633), "2.633")
        self.assertEqual(L.n(None), "-")
        self.assertEqual(L.hm(376), "6h16")
        self.assertEqual(L.date(dt.date(2026, 11, 2), "chart_weekday"), "T2 02")
        self.assertEqual(L.date(dt.datetime(2026, 9, 30, 18, 20), "long_date"), "Thứ Tư, 30/09/2026, 18:20")
        self.assertEqual(L.value("status", "UNBALANCED"), "mất cân bằng")
        self.assertEqual(L.value("type", "open_water_swimming"), "Open water swimming")   # unknown: prettified

    def test_english_defaults_and_other_languages(self):
        en = gr.Labels()
        self.assertEqual((en.n(4174.7), en.hm(376), en.date(dt.date(2026, 11, 2), "chart_weekday")),
                         ("4,174.7", "6h 16m", "Mon 02"))
        ja = gr.Labels("ja", {"summary": "概要"})
        self.assertEqual(ja("summary"), "概要")
        self.assertIn("details", ja.fallback)
        self.assertNotIn("summary", ja.fallback)
        self.assertEqual(set(gr.LABELS_VI) - set(gr.LABELS_EN), set())   # no stray Vietnamese keys

    def test_sampled_points_skip_missing_readings(self):
        obs = {"effectivePeriod": {"start": "2026-11-11T00:00:00+07:00"},
               "valueSampledData": {"period": 120000, "data": "60 E 62 L 64"}}
        pts = gr.sampled_points(obs)
        self.assertEqual([v for _, v in pts], [60.0, 62.0, 64.0])
        self.assertEqual([t.strftime("%H:%M") for t, _ in pts], ["00:00", "00:04", "00:08"])
        self.assertEqual(gr.sampled_points(None), [])

    def test_compare_with_baseline(self):
        c = gr.compare(62, [50, 55, 60, None, 70])
        self.assertEqual((c["avg"], c["diff"], c["days_lower"], c["days"], c["min"], c["max"]), (58.8, 3.2, 3, 4, 50, 70))
        self.assertEqual(gr.compare(None, [1, 2])["diff"], None)
        self.assertEqual(gr.parse_offset("+07:00").utcoffset(None), dt.timedelta(hours=7))
        self.assertEqual(gr.parse_offset("-03:30").utcoffset(None), dt.timedelta(hours=-3, minutes=-30))

    def test_day_labels_are_complete_in_vietnamese(self):
        missing = [k for k in gr.LABELS_EN if k not in gr.LABELS_VI]
        self.assertEqual(missing, [])
        self.assertEqual(gr.Labels("vi").date(dt.date(2026, 11, 11), "long_day"), "Thứ Tư, 11/11/2026")

    def test_narrative_is_required_and_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "n.json"
            path.write_text(json.dumps({"headline": "h", "summary": "s", "evaluation": ["e"], "recommendations": ["r"]}))
            self.assertEqual(gr.load_narrative(str(path))["headline"], "h")
            path.write_text(json.dumps({"headline": "h", "summary": "s", "evaluation": [], "recommendations": ["r"]}))
            with self.assertRaises(SystemExit):
                gr.load_narrative(str(path))
            path.write_text(json.dumps({"headline": "h", "summary": "s", "evaluation": "e", "recommendations": ["r"]}))
            with self.assertRaises(SystemExit):
                gr.load_narrative(str(path))


if __name__ == "__main__":
    unittest.main()
