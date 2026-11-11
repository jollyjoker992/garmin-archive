"""Tests for garmin_fhir.py against a small synthetic archive. No network.

    python3 -m unittest discover -s tests -v
"""

import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import garmin_fhir as gf  # noqa: E402

DAY = "2026-01-02"
T0 = 1767286800000          # 2026-01-02T00:00:00+07:00 as epoch ms
H = 3_600_000


def write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj) if not isinstance(obj, bytes) else "")
    if isinstance(obj, bytes):
        path.write_bytes(obj)


def make_day(root: Path, hr_values=None) -> None:
    d = root / "days" / DAY[:4] / DAY
    write(d / "daily_summary.json", {"data": [{
        "calendarDate": DAY, "userProfilePk": 42,
        "wellnessChronology": {"startTimestampGmt": "2026-01-01T17:00:00.0", "startTimestampLocal": "2026-01-02T00:00:00.0",
                               "endTimestampGmt": "2026-01-02T17:00:00.0", "endTimestampLocal": "2026-01-03T00:00:00.0"},
        "movement": {"steps": {"value": 8000, "goal": 7000, "distanceInMeters": 6000.0},
                     "floorsAscended": {"value": 3, "goal": 10}, "floorsDescended": {"value": None}},
        "calories": {"burnedTotal": 2100, "burnedActive": 300, "burnedResting": 1800},
        "intensityMinutes": {"moderate": 20, "vigorous": 0, "goal": 150},
        "stress": {"avgLevel": 30, "maxLevel": 90, "restDurationInMillis": 600000, "qualifier": "balanced"},
        "bodyBattery": {"maxValue": 80, "minValue": 20, "activityEvents": [{"deviceId": 7}]},
        "respiration": {"avgValue": 14, "minValue": 9, "maxValue": 20},
    }]})
    write(d / "heart_rate.json", {
        "calendarDate": DAY, "userProfilePK": 42, "restingHeartRate": 55, "minHeartRate": 50, "maxHeartRate": 150,
        "startTimestampGMT": "2026-01-01T17:00:00.0", "startTimestampLocal": "2026-01-02T00:00:00.0",
        "endTimestampGMT": "2026-01-02T17:00:00.0",
        "heartRateValueDescriptors": [{"key": "timestamp", "index": 0}, {"key": "heartrate", "index": 1}],
        "heartRateValues": hr_values if hr_values is not None else
        [[T0, 60], [T0 + 120000, None], [T0 + 480000, 64]],     # null, then two missing slots
    })
    write(d / "sleep.json", {
        "calendarDate": DAY, "userProfilePK": 42, "sleepTimeSeconds": 25200, "deepSleepSeconds": 3600,
        "lightSleepSeconds": 18000, "remSleepSeconds": 3600, "awakeSleepSeconds": None,
        "sleepStartTimestampGMT": T0 - 2 * H, "sleepStartTimestampLocal": T0 - 2 * H + 7 * H, "sleepEndTimestampGMT": T0 + 5 * H,
        "sleepScores": {"overall": {"value": 80, "qualifierKey": "GOOD"}},
        "sleepNeed": {"actual": 450, "deviceId": 7},
    })
    write(d / "body_battery.json", [{
        "date": DAY,
        "bodyBatteryValueDescriptorDTOList": [
            {"bodyBatteryValueDescriptorKey": "timestamp", "bodyBatteryValueDescriptorIndex": 0},
            {"bodyBatteryValueDescriptorKey": "bodyBatteryLevel", "bodyBatteryValueDescriptorIndex": 1}],
        "bodyBatteryValuesArray": [[T0, 30], [T0, 30], [T0 + 7 * H + 123000, 80]],   # Garmin repeats midnight
        "bodyBatteryActivityEvent": [{"eventStartTimeGmt": "2026-01-01T15:00:00.0", "durationInMilliseconds": 7 * H,
                                      "timezoneOffset": 7 * H, "bodyBatteryImpact": 50, "eventType": "SLEEP"}],
    }])
    write(d / "weight.json", {"dailyWeightSummaries": [{"allWeightMetrics": [
        {"samplePk": 99, "weight": 70500.0, "timestampGMT": T0 + H, "date": T0 + 8 * H, "sourceType": "MANUAL"}]}]})
    write(d / "activities.json", [{
        "activityId": 555, "ownerId": 42, "activityType": {"typeKey": "running"},
        "startTimeGMT": "2026-01-02 10:00:00", "startTimeLocal": "2026-01-02 17:00:00", "endTimeGMT": "2026-01-02 10:30:00",
        "duration": 1800.0, "distance": 5000.0, "averageHR": 150.0, "hrTimeInZone_3": 600.0,
    }])
    write(d / "activities/555/detail.json", {"summaryDTO": {"directWorkoutFeel": 50}, "metadataDTO": {"uploadedDate": "2026-01-02T10:40:00.0"}})
    write(d / "activities/555/original.zip", b"PK\x03\x04zip")
    write(d / "profile_settings.json", {"timeZone": "Asia/Bangkok"})
    write(root / "manifest.json", {"schema_version": 1, "days": {
        DAY: {"status": "final", "path": f"days/{DAY[:4]}/{DAY}", "fetched_at": "2026-01-05T00:00:00Z"}}})


class MapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        for k in [k for k in os.environ if k.startswith("GARMIN_")]:
            del os.environ[k]
        os.environ.update(GARMIN_ARCHIVE_DIR=self.tmp.name, GARMIN_FHIR_PATIENT_NAME="Test Middle Person",
                          GARMIN_LOG_LEVEL="CRITICAL")

    def run_map(self, *args):
        return gf.main(["--env-file", os.devnull, "map", *args])

    def resources(self):
        path = self.root / "fhir/days/2026" / f"{DAY}.ndjson"
        return {r["identifier"][0]["value"]: r for r in map(json.loads, path.read_text().splitlines())}

    def test_maps_a_day(self):
        make_day(self.root)
        self.assertEqual(self.run_map(), 0)
        r = self.resources()

        steps = r[f"{DAY}/steps"]
        self.assertEqual(steps["valueQuantity"], {"value": 8000, "unit": "/d", "system": gf.UCUM, "code": "/d"})
        self.assertEqual(steps["effectivePeriod"], {"start": "2026-01-02T00:00:00+07:00", "end": "2026-01-03T00:00:00+07:00"})
        self.assertEqual(steps["component"][0]["code"]["coding"][0]["code"], "step-goal")
        self.assertTrue(steps["subject"]["reference"].endswith("/identifier/garmin-user|42"))
        self.assertTrue(steps["device"]["reference"].endswith("/identifier/garmin-device|7"))

        # Absent values produce nothing: no floors-down, no SpO2, no awake component.
        self.assertNotIn(f"{DAY}/floors-down", r)
        self.assertNotIn(f"{DAY}/spo2-avg", r)
        sleep = r[f"{DAY}/sleep"]
        self.assertNotIn("93828-2", [c["code"]["coding"][0]["code"] for c in sleep["component"]])
        self.assertEqual(sleep["valueQuantity"]["value"], 420)
        self.assertEqual(sleep["effectivePeriod"]["start"], "2026-01-01T22:00:00+07:00")

        hr = r[f"{DAY}/hr-samples"]["valueSampledData"]
        self.assertEqual((hr["period"], hr["data"]), (120000, "60 E E E 64"))

        stress = r[f"{DAY}/stress"]
        texts = [c.get("valueCodeableConcept", {}).get("text") for c in stress["component"]]
        self.assertIn("balanced", texts)
        rest = [c for c in stress["component"] if c["code"]["coding"][0]["code"] == "stress-rest-duration"][0]
        self.assertEqual(rest["valueQuantity"]["value"], 10)

        self.assertEqual(sum(1 for k in r if k.startswith("body-battery/")), 2)   # repeated point kept once
        bb = r[f"body-battery/{T0 + 7 * H + 123000}"]
        self.assertEqual(bb["effectiveDateTime"], "2026-01-02T07:02:03+07:00")
        event = r[f"body-battery-event/{T0 - 2 * H}"]
        self.assertEqual(event["effectivePeriod"]["end"], "2026-01-02T05:00:00+07:00")

        weight = r["weight/99"]
        self.assertEqual((weight["valueQuantity"]["value"], weight["valueQuantity"]["unit"]), (70.5, "kg"))
        self.assertEqual(weight["effectiveDateTime"], "2026-01-02T01:00:00+07:00")

        doc = r["activity/555/original"]
        zip_bytes = b"PK\x03\x04zip"
        self.assertEqual(doc["content"][0]["attachment"]["hash"], base64.b64encode(hashlib.sha1(zip_bytes).digest()).decode())
        workout = r["activity/555"]
        self.assertEqual(workout["valueQuantity"]["value"], 30)
        self.assertEqual(workout["effectivePeriod"]["start"], "2026-01-02T17:00:00+07:00")
        self.assertIn("DocumentReference?identifier=", workout["derivedFrom"][0]["reference"])
        zone3 = [c for c in workout["component"] if c["code"]["coding"][0]["code"] == "workout-hr-zone-3-time"][0]
        self.assertEqual(zone3["valueQuantity"]["value"], 10)

        shared = [json.loads(line) for line in (self.root / "fhir/shared.ndjson").read_text().splitlines()]
        self.assertEqual(shared[0]["name"][0], {"use": "usual", "text": "Test Middle Person", "family": "Person",
                                                "given": ["Test", "Middle"]})
        self.assertEqual(shared[1]["resourceType"], "Device")

    def test_unchanged_days_are_skipped_and_changed_days_remapped(self):
        make_day(self.root)
        self.run_map()
        out = self.root / "fhir/days/2026" / f"{DAY}.ndjson"
        before = out.stat().st_mtime_ns
        self.run_map()
        self.assertEqual(out.stat().st_mtime_ns, before)

        manifest = json.loads((self.root / "manifest.json").read_text())
        manifest["days"][DAY]["fetched_at"] = "2026-01-06T00:00:00Z"
        (self.root / "manifest.json").write_text(json.dumps(manifest))
        self.run_map()
        fm = json.loads((self.root / "fhir/manifest.json").read_text())
        self.assertEqual(fm["days"][DAY]["source_fetched_at"], "2026-01-06T00:00:00Z")

    def test_hr_min_max_fall_back_to_daily_summary(self):
        make_day(self.root)
        d = self.root / "days" / DAY[:4] / DAY
        hr = json.loads((d / "heart_rate.json").read_text())       # older shape: no intraday detail
        hr.update(minHeartRate=None, maxHeartRate=None, heartRateValues=None, heartRateValueDescriptors=None)
        (d / "heart_rate.json").write_text(json.dumps(hr))
        ds = json.loads((d / "daily_summary.json").read_text())
        ds["data"][0]["heartRate"] = {"minValue": 49, "maxValue": 171, "restingValue": 55}
        (d / "daily_summary.json").write_text(json.dumps(ds))
        self.assertEqual(self.run_map(), 0)
        r = self.resources()
        self.assertEqual((r[f"{DAY}/hr-min"]["valueQuantity"]["value"], r[f"{DAY}/hr-max"]["valueQuantity"]["value"]), (49, 171))
        self.assertNotIn(f"{DAY}/hr-samples", r)

    def test_hrv_without_sleep_fields_uses_hrv_window(self):
        make_day(self.root)
        d = self.root / "days" / DAY[:4] / DAY
        (d / "hrv.json").write_text(json.dumps({            # older response shape: no sleep* fields, no readings
            "hrvSummary": {"calendarDate": DAY, "lastNightAvg": 51, "status": "BALANCED", "weeklyAvg": 50},
            "startTimestampGMT": "2026-01-01T17:07:01.0", "startTimestampLocal": "2026-01-02T00:07:01.0",
            "endTimestampGMT": "2026-01-01T23:24:28.0", "hrvReadings": []}))
        self.assertEqual(self.run_map(), 0)
        hrv = self.resources()[f"{DAY}/hrv-night"]
        self.assertEqual(hrv["valueQuantity"]["value"], 51)
        self.assertEqual(hrv["effectivePeriod"]["start"], "2026-01-02T00:07:01+07:00")
        fm = json.loads((self.root / "fhir/manifest.json").read_text())
        self.assertEqual(fm["days"][DAY]["warnings"], [])

    def test_hrv_without_overnight_value_is_skipped_silently(self):
        make_day(self.root)
        d = self.root / "days" / DAY[:4] / DAY
        (d / "hrv.json").write_text(json.dumps({"hrvSummary": {"calendarDate": DAY, "lastNightAvg": None, "status": "BALANCED"},
                                                "startTimestampGMT": "2026-01-01T23:13:24.0", "endTimestampGMT": "2026-01-01T23:13:26.0"}))
        self.assertEqual(self.run_map(), 0)
        self.assertNotIn(f"{DAY}/hrv-night", self.resources())
        fm = json.loads((self.root / "fhir/manifest.json").read_text())
        self.assertEqual(fm["days"][DAY]["warnings"], [])

    def test_cloud_only_day_is_skipped(self):
        make_day(self.root)
        manifest = json.loads((self.root / "manifest.json").read_text())
        manifest["days"]["2026-01-01"] = {"status": "final", "path": "days/2026/2026-01-01", "adopted_from": "gdrive",
                                          "fetched_at": "2026-01-04T00:00:00Z"}
        (self.root / "manifest.json").write_text(json.dumps(manifest))
        self.assertEqual(self.run_map(), 0)
        fm = json.loads((self.root / "fhir/manifest.json").read_text())
        self.assertNotIn("2026-01-01", fm["days"])
        self.assertEqual(fm["days"][DAY]["status"], "ok")

    def test_off_grid_series_fails_the_day(self):
        make_day(self.root, hr_values=[[T0, 60], [T0 + 90000, 61]])
        self.assertEqual(self.run_map(), 1)
        fm = json.loads((self.root / "fhir/manifest.json").read_text())
        self.assertEqual(fm["days"][DAY]["status"], "failed")
        self.assertIn("grid", fm["days"][DAY]["error"])

    def test_unknown_code_and_wrong_unit_are_refused(self):
        codes = gf.Codes(gf.DEFAULT_BASE)
        with self.assertRaises(gf.MappingError):
            codes.resolve("gm:no-such-code")
        with self.assertRaises(gf.MappingError):
            codes.resolve("loinc:80404-7")          # deliberately not in the verified table
        mapper = gf.DayMapper(codes, gf.DEFAULT_BASE, DAY, self.root, self.root, None)
        with self.assertRaises(gf.MappingError):
            mapper.component("gm:hrv-status", 5)     # a text code written as a number


class HelperTests(unittest.TestCase):
    def test_epoch_ms_formats(self):
        self.assertEqual(gf.epoch_ms("2026-01-01T17:00:00.0"), 1767286800000)
        self.assertEqual(gf.epoch_ms("2026-01-01 17:00:00"), 1767286800000)
        self.assertEqual(gf.epoch_ms("2026-01-01T17:00:00.678"), 1767286800678)

    def test_iso_with_offset(self):
        self.assertEqual(gf.iso(1767286800000, 7 * H), "2026-01-02T00:00:00+07:00")
        self.assertEqual(gf.iso(1767286800000, 0), "2026-01-01T17:00:00+00:00")

    def test_codesystem_matches_mapping_version(self):
        cs = json.loads(gf.CODESYSTEM_FILE.read_text())
        self.assertEqual(cs["version"], gf.MAPPING_VERSION)


if __name__ == "__main__":
    unittest.main()
