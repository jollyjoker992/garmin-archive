#!/usr/bin/env python3
"""garmin-fhir: map the raw garmin-archive days to FHIR R4 resources.

Reads ``days/YYYY/YYYY-MM-DD/`` from the archive and writes one NDJSON file of
FHIR resources per day to ``fhir/days/YYYY/YYYY-MM-DD.ndjson`` inside the data
directory, plus ``fhir/shared.ndjson`` (Patient, Devices) and
``fhir/manifest.json``. The rules are in docs/fhir-mapping.md; the custom codes
in fhir/CodeSystem-garmin-metrics.json.

Resources carry no server id. Each has a stable ``identifier``; references
between them use the conditional form ``Type?identifier=system|value``. The
loader (step 4) resolves them and upserts by identifier.

Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

import garmin_archive as ga

MAPPING_VERSION = "0.1.2"
REPO_DIR = Path(__file__).resolve().parent
CODESYSTEM_FILE = REPO_DIR / "fhir" / "CodeSystem-garmin-metrics.json"
DEFAULT_BASE = "https://github.com/jollyjoker992/garmin-archive/fhir"
UCUM = "http://unitsofmeasure.org"
LOINC_SYSTEM = "http://loinc.org"
CATEGORY_SYSTEM = "http://terminology.hl7.org/CodeSystem/observation-category"

# LOINC codes used by the mapping, with the display and the only unit allowed.
# Each was checked against the NLM LOINC API on 2026-11-11.
LOINC = {
    "41950-7": ("Number of steps in 24 hour Measured", "/d"),
    "41953-1": ("Walking distance 24 hour Calculated", "m"),
    "93833-2": ("Flights climbed 24 hour", "{#}"),
    "41979-6": ("Calories burned in 24 hour Calculated", "kcal"),
    "77592-4": ("Moderate physical activity [IPAQ]", "min"),
    "77593-2": ("Vigorous physical activity [IPAQ]", "min"),
    "40443-4": ("Heart rate --resting", "/min"),
    "8883-1": ("Heart rate 24 hour minimum", "/min"),
    "8873-2": ("Heart rate 24 hour maximum", "/min"),
    "8867-4": ("Heart rate", "/min"),
    "2708-6": ("Oxygen saturation in Arterial blood", "%"),
    "59408-5": ("Oxygen saturation in Arterial blood by Pulse oximetry", "%"),
    "9279-1": ("Respiratory rate", "/min"),
    "29463-7": ("Body weight", "kg"),
    "39156-5": ("Body mass index (BMI) [Ratio]", "kg/m2"),
    "93832-4": ("Sleep duration", "min"),
    "93831-6": ("Deep sleep duration", "min"),
    "93830-8": ("Light sleep duration", "min"),
    "93829-0": ("REM sleep duration", "min"),
    "93828-2": ("Nighttime awakening duration", "min"),
    "55411-3": ("Exercise duration", "min"),
    "73985-4": ("Exercise activity", None),
    "55425-3": ("Heart rate unspecified time mean by Pedometer", "/min"),
    "55426-1": ("Heart rate unspecified time maximum by Pedometer", "/min"),
    "55424-6": ("Calories burned in unspecified time Pedometer", "kcal"),
}

log = logging.getLogger("garmin-fhir")


class MappingError(Exception):
    """The raw data of a day cannot be mapped as specified."""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def load_json(path: Path):
    if not path.is_file() or path.stat().st_size == 0:
        return None
    return json.loads(path.read_text())


def epoch_ms(text: str) -> int:
    """Garmin date-time text without zone ('2026-11-09T16:34:06.0',
    '2026-09-30 11:20:41') -> epoch ms, reading it as UTC."""
    text = text.strip().replace(" ", "T")
    if "." in text:
        head, frac = text.split(".", 1)
        text = f"{head}.{(frac + '000000')[:6]}"
    return int(dt.datetime.fromisoformat(text).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def iso(ms: int, offset_ms: int) -> str:
    """Epoch ms + UTC offset in ms -> ISO 8601 with that offset, seconds precision."""
    zone = dt.timezone(dt.timedelta(milliseconds=offset_ms))
    return dt.datetime.fromtimestamp(ms / 1000, zone).isoformat(timespec="seconds")


def num(value, digits: int = 2):
    """Round floats for output; keep integers as integers; None stays None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    rounded = round(float(value), digits)
    return int(rounded) if rounded.is_integer() else rounded


def ms_to_min(value):
    return None if value is None else num(value / 60000, 1)


def s_to_min(value):
    return None if value is None else num(value / 60, 1)


def sha1_b64(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode()


# --------------------------------------------------------------------------
# Codes
# --------------------------------------------------------------------------


class Codes:
    """Resolves codes and enforces their units, so the output always matches
    the CodeSystem and the LOINC table above."""

    def __init__(self, base: str, codesystem_file: Path = CODESYSTEM_FILE):
        cs = json.loads(codesystem_file.read_text())
        self.gm_system = f"{base}/CodeSystem/garmin-metrics"
        if cs["url"] != self.gm_system:
            raise MappingError(f"{codesystem_file} has url {cs['url']}, expected {self.gm_system}")
        if cs["version"] != MAPPING_VERSION:
            raise MappingError(f"CodeSystem version {cs['version']} != mapping version {MAPPING_VERSION}")
        self.gm: dict[str, tuple[str, str | None]] = {}
        for group in cs["concept"]:
            for concept in group.get("concept", []):
                props = {p["code"]: p.get("valueString") for p in concept.get("property", [])}
                self.gm[concept["code"]] = (concept["display"], props.get("unit"))

    def resolve(self, ref: str) -> tuple[dict, str | None]:
        """'loinc:41950-7', 'gm:stress-avg' or 'loinc:2708-6+loinc:59408-5'
        -> (CodeableConcept, unit)."""
        codings, units = [], set()
        for part in ref.split("+"):
            kind, code = part.split(":", 1)
            if kind == "loinc":
                if code not in LOINC:
                    raise MappingError(f"LOINC {code} is not in the verified table")
                display, unit = LOINC[code]
                codings.append({"system": LOINC_SYSTEM, "code": code, "display": display})
            elif kind == "gm":
                if code not in self.gm:
                    raise MappingError(f"garmin-metrics has no code {code!r}")
                display, unit = self.gm[code]
                codings.append({"system": self.gm_system, "code": code, "display": display})
            else:
                raise MappingError(f"unknown code kind in {ref!r}")
            units.add(unit)
        if len(units) != 1:
            raise MappingError(f"codings in {ref!r} disagree on the unit: {units}")
        return {"coding": codings}, units.pop()


def quantity(value, unit: str) -> dict:
    return {"value": value, "unit": unit, "system": UCUM, "code": unit}


# --------------------------------------------------------------------------
# Mapping one day
# --------------------------------------------------------------------------


class DayMapper:
    """Builds the FHIR resources of one archived day."""

    def __init__(self, codes: Codes, base: str, day: str, folder: Path, data_dir: Path, fallback_tz: str | None):
        self.codes = codes
        self.base = base
        self.day = day
        self.folder = folder
        self.data_dir = data_dir
        self.fallback_tz = fallback_tz
        self.resources: list[dict] = []
        self.warnings: list[str] = []
        self.user_id: str | None = None
        self.device_ids: set[str] = set()
        self.day_offset_ms: int | None = None
        self.day_start_ms: int | None = None
        self.day_end_ms: int | None = None

    # -- references and identifiers -----------------------------------------

    def ident(self, kind: str, value: str) -> list[dict]:
        return [{"system": f"{self.base}/identifier/{kind}", "value": value}]

    def ref(self, rtype: str, kind: str, value: str) -> dict:
        return {"reference": f"{rtype}?identifier={self.base}/identifier/{kind}|{value}"}

    def subject(self) -> dict:
        if not self.user_id:
            raise MappingError("no Garmin user id found in this day's files")
        return self.ref("Patient", "garmin-user", self.user_id)

    def device_ref(self) -> dict | None:
        if len(self.device_ids) == 1:
            return self.ref("Device", "garmin-device", next(iter(self.device_ids)))
        return None

    # -- builders -----------------------------------------------------------

    def component(self, ref: str, value, *, text: bool = False) -> dict | None:
        if value is None or value == "":
            return None
        code, unit = self.codes.resolve(ref)
        if text:
            if unit is not None:
                raise MappingError(f"{ref} has unit {unit} but is written as text")
            return {"code": code, "valueCodeableConcept": {"text": str(value)}}
        if unit is None:
            raise MappingError(f"{ref} has no unit but is written as a quantity")
        return {"code": code, "valueQuantity": quantity(value, unit)}

    def observation(self, key: str, ref: str, *, effective: dict, value=None, text_value=None,
                    sampled: dict | None = None, category: str | None = None,
                    components: list | None = None, **extra) -> None:
        if value is None and text_value is None and sampled is None:
            return
        code, unit = self.codes.resolve(ref)
        obs = {
            "resourceType": "Observation",
            "identifier": self.ident("observation", key),
            "status": "final",
        }
        if category:
            obs["category"] = [{"coding": [{"system": CATEGORY_SYSTEM, "code": category}]}]
        obs["code"] = code
        obs["subject"] = self.subject()
        obs.update(effective)
        if value is not None:
            if unit is None:
                raise MappingError(f"{ref} has no unit but is written as a quantity")
            obs["valueQuantity"] = quantity(value, unit)
        elif text_value is not None:
            obs["valueCodeableConcept"] = {"text": str(text_value)}
        else:
            obs["valueSampledData"] = {"origin": quantity(0, unit), **sampled}
        device = self.device_ref()
        if device:
            obs["device"] = device
        comps = [c for c in (components or []) if c]
        if comps:
            obs["component"] = comps
        obs.update(extra)
        self.resources.append(obs)

    def sampled(self, points: list[tuple[int, object]], period_ms: int, what: str) -> tuple[dict, int, int] | None:
        """[(epoch ms, value or None)] -> (SampledData fields, first ms, last ms).

        Points must sit on a grid of ``period_ms`` from the first point. Missing
        grid slots and null values become 'E'."""
        points = sorted(p for p in points if p[0] is not None)
        if not points or all(v is None for _, v in points):
            return None
        first = points[0][0]
        slots: dict[int, object] = {}
        for ts, value in points:
            offset = ts - first
            if offset % period_ms:
                raise MappingError(f"{what}: sample at {ts} is not on the {period_ms} ms grid")
            slots[offset // period_ms] = value
        count = max(slots) + 1
        data = " ".join("E" if slots.get(i) is None else str(num(slots[i])) for i in range(count))
        last = first + (count - 1) * period_ms
        return {"period": period_ms, "dimensions": 1, "data": data}, first, last

    # -- the day ------------------------------------------------------------

    def load(self, name: str):
        return load_json(self.folder / name)

    def run(self) -> list[dict]:
        summary = self._daily_summary_row()
        self.summary = summary or {}
        self._find_ids(summary)
        self._set_day_bounds(summary)
        self._daily(summary)
        self._heart_rate()
        self._spo2()
        self._sleep()
        self._hrv()
        self._body_battery()
        self._weight_and_fitness_age()
        self._workouts()
        seen: set[str] = set()
        for r in self.resources:
            key = f"{r['resourceType']}|{r['identifier'][0]['value']}"
            if key in seen:
                raise MappingError(f"two resources with the identifier {key}")
            seen.add(key)
        return self.resources

    def _daily_summary_row(self) -> dict | None:
        data = (self.load("daily_summary.json") or {}).get("data") or []
        rows = [r for r in data if r.get("calendarDate") == self.day]
        return rows[0] if rows else None

    def _find_ids(self, summary: dict | None) -> None:
        candidates = [
            (summary or {}).get("userProfilePk"),
            (self.load("heart_rate.json") or {}).get("userProfilePK"),
            (self.load("sleep.json") or {}).get("userProfilePK"),
            (self.load("hrv.json") or {}).get("userProfilePk"),
        ]
        for act in self.load("activities.json") or []:
            candidates.append(act.get("ownerId"))
        ids = {str(c) for c in candidates if c is not None}
        if len(ids) > 1:
            raise MappingError(f"more than one Garmin user id in the day: {sorted(ids)}")
        self.user_id = ids.pop() if ids else None

        sleep = self.load("sleep.json") or {}
        for src in (sleep.get("sleepNeed"), sleep.get("sleepAlignment")):
            if isinstance(src, dict) and src.get("deviceId") is not None:
                self.device_ids.add(str(src["deviceId"]))
        for event in ((summary or {}).get("bodyBattery") or {}).get("activityEvents") or []:
            if event.get("deviceId") is not None:
                self.device_ids.add(str(event["deviceId"]))
        if len(self.device_ids) > 1:
            self.warnings.append(f"several devices {sorted(self.device_ids)}; Observations carry no device")

    def _set_day_bounds(self, summary: dict | None) -> None:
        chron = (summary or {}).get("wellnessChronology") or {}
        if chron.get("startTimestampGmt") and chron.get("startTimestampLocal"):
            start_gmt = epoch_ms(chron["startTimestampGmt"])
            self.day_offset_ms = epoch_ms(chron["startTimestampLocal"]) - start_gmt
            self.day_start_ms = start_gmt
            self.day_end_ms = epoch_ms(chron["endTimestampGmt"])
            return
        hr = self.load("heart_rate.json") or {}
        if hr.get("startTimestampGMT") and hr.get("startTimestampLocal"):
            self.day_start_ms = epoch_ms(hr["startTimestampGMT"])
            self.day_offset_ms = epoch_ms(hr["startTimestampLocal"]) - self.day_start_ms
            self.day_end_ms = epoch_ms(hr["endTimestampGMT"])
            return
        if not self.fallback_tz:
            raise MappingError("cannot work out the day's UTC offset (no daily summary, no heart rate, no profile time zone)")
        zone = ZoneInfo(self.fallback_tz)
        local_start = dt.datetime.fromisoformat(self.day).replace(tzinfo=zone)
        local_end = (dt.datetime.fromisoformat(self.day) + dt.timedelta(days=1)).replace(tzinfo=zone)
        self.day_start_ms = int(local_start.timestamp() * 1000)
        self.day_end_ms = int(local_end.timestamp() * 1000)
        self.day_offset_ms = int(local_start.utcoffset().total_seconds() * 1000)

    def day_period(self) -> dict:
        return {"effectivePeriod": {"start": iso(self.day_start_ms, self.day_offset_ms),
                                    "end": iso(self.day_end_ms, self.day_offset_ms)}}

    def key(self, name: str) -> str:
        return f"{self.day}/{name}"

    # -- daily summary --------------------------------------------------------

    def _daily(self, s: dict | None) -> None:
        if not s:
            return
        period = self.day_period()
        mv = s.get("movement") or {}
        steps, up, down = mv.get("steps") or {}, mv.get("floorsAscended") or {}, mv.get("floorsDescended") or {}
        cal, im = s.get("calories") or {}, s.get("intensityMinutes") or {}
        o = self.observation
        o(self.key("steps"), "loinc:41950-7", effective=period, value=num(steps.get("value")), category="activity",
          components=[self.component("gm:step-goal", num(steps.get("goal")))])
        o(self.key("distance"), "loinc:41953-1", effective=period, value=num(steps.get("distanceInMeters")), category="activity")
        o(self.key("floors-up"), "loinc:93833-2", effective=period, value=num(up.get("value")), category="activity",
          components=[self.component("gm:floors-goal", num(up.get("goal")))])
        o(self.key("floors-down"), "gm:floors-descended", effective=period, value=num(down.get("value")), category="activity")
        o(self.key("calories-total"), "loinc:41979-6", effective=period, value=num(cal.get("burnedTotal")), category="activity")
        o(self.key("calories-active"), "gm:active-calories", effective=period, value=num(cal.get("burnedActive")), category="activity")
        o(self.key("calories-resting"), "gm:resting-calories", effective=period, value=num(cal.get("burnedResting")), category="activity")
        o(self.key("moderate-minutes"), "loinc:77592-4", effective=period, value=num(im.get("moderate")), category="activity",
          components=[self.component("gm:intensity-minutes-goal", num(im.get("goal")))])
        o(self.key("vigorous-minutes"), "loinc:77593-2", effective=period, value=num(im.get("vigorous")), category="activity")

        ox = s.get("pulseOx") or {}
        o(self.key("spo2-avg"), "loinc:2708-6+loinc:59408-5", effective=period, value=num(ox.get("avgValue")),
          category="vital-signs", components=[self.component("gm:spo2-daily-min", num(ox.get("minValue")))])
        resp = s.get("respiration") or {}
        o(self.key("respiration-avg"), "loinc:9279-1", effective=period, value=num(resp.get("avgValue")),
          category="vital-signs", components=[self.component("gm:respiration-daily-min", num(resp.get("minValue"))),
                                              self.component("gm:respiration-daily-max", num(resp.get("maxValue")))])

        st = s.get("stress") or {}
        o(self.key("stress"), "gm:stress-avg", effective=period, value=num(st.get("avgLevel")), components=[
            self.component("gm:stress-max", num(st.get("maxLevel"))),
            self.component("gm:stress-rest-duration", ms_to_min(st.get("restDurationInMillis"))),
            self.component("gm:stress-low-duration", ms_to_min(st.get("lowStressDurationInMillis"))),
            self.component("gm:stress-medium-duration", ms_to_min(st.get("mediumStressDurationInMillis"))),
            self.component("gm:stress-high-duration", ms_to_min(st.get("highStressDurationInMillis"))),
            self.component("gm:stress-activity-duration", ms_to_min(st.get("activityDurationInMillis"))),
            self.component("gm:stress-uncategorized-duration", ms_to_min(st.get("uncategorizedDurationInMillis"))),
            self.component("gm:stress-qualifier", st.get("qualifier"), text=True),
        ])
        bb = s.get("bodyBattery") or {}
        o(self.key("body-battery"), "gm:body-battery-max", effective=period, value=num(bb.get("maxValue")), components=[
            self.component("gm:body-battery-min", num(bb.get("minValue"))),
            self.component("gm:body-battery-end", num(bb.get("latestValue"))),
            self.component("gm:body-battery-charged", num(bb.get("chargedValue"))),
            self.component("gm:body-battery-drained", num(bb.get("drainedValue"))),
        ])

    # -- heart rate -----------------------------------------------------------

    def _series(self, values, descriptors, key_field: str, idx_field: str, wanted: str) -> list[tuple[int, object]]:
        index = {d.get(key_field): d.get(idx_field) for d in descriptors or []}
        if "timestamp" not in index or wanted not in index:
            raise MappingError(f"series descriptors lack timestamp/{wanted}: {descriptors}")
        ti, vi = index["timestamp"], index[wanted]
        return [(row[ti], row[vi]) for row in values or [] if row and len(row) > max(ti, vi)]

    def _heart_rate(self) -> None:
        hr = self.load("heart_rate.json") or {}
        if hr.get("calendarDate") not in (None, self.day):
            raise MappingError(f"heart_rate.json is for {hr.get('calendarDate')}")
        period = self.day_period()
        o = self.observation
        # Garmin's heart-rate endpoint only keeps intraday detail (samples, min, max) for
        # about four months; the daily summary keeps min/max/resting for every day.
        daily = self.summary.get("heartRate") or {}
        pick = lambda primary, fallback: num(hr.get(primary) if hr.get(primary) is not None else daily.get(fallback))
        o(self.key("resting-hr"), "loinc:40443-4", effective=period, value=pick("restingHeartRate", "restingValue"),
          category="vital-signs",
          components=[self.component("gm:resting-hr-7day-avg", num(hr.get("lastSevenDaysAvgRestingHeartRate")))])
        o(self.key("hr-min"), "loinc:8883-1", effective=period, value=pick("minHeartRate", "minValue"), category="vital-signs")
        o(self.key("hr-max"), "loinc:8873-2", effective=period, value=pick("maxHeartRate", "maxValue"), category="vital-signs")
        if hr.get("heartRateValues"):
            points = self._series(hr["heartRateValues"], hr.get("heartRateValueDescriptors"), "key", "index", "heartrate")
            series = self.sampled(points, 120_000, "heart rate")
            if series:
                data, first, last = series
                o(self.key("hr-samples"), "loinc:8867-4", category="vital-signs", sampled=data,
                  effective={"effectivePeriod": {"start": iso(first, self.day_offset_ms), "end": iso(last, self.day_offset_ms)}})

    def _spo2(self) -> None:
        sp = self.load("spo2.json") or {}
        if not sp.get("spO2HourlyAverages"):
            return
        points = self._series(sp["spO2HourlyAverages"], sp.get("spO2HourlyAveragesDescriptorList"),
                              "spo2ValueDescKey", "spo2ValueDescIndex", "spo2Level")
        series = self.sampled(points, 3_600_000, "SpO2 hourly")
        if series:
            data, first, last = series
            self.observation(self.key("spo2-hourly"), "loinc:2708-6+loinc:59408-5", category="vital-signs", sampled=data,
                             effective={"effectivePeriod": {"start": iso(first, self.day_offset_ms),
                                                            "end": iso(last, self.day_offset_ms)}})

    # -- sleep and HRV ---------------------------------------------------------

    def _sleep_period(self, start_gmt, start_local, end_gmt) -> tuple[dict, int] | None:
        if start_gmt is None or end_gmt is None:
            return None
        offset = (start_local - start_gmt) if start_local is not None else self.day_offset_ms
        return {"effectivePeriod": {"start": iso(start_gmt, offset), "end": iso(end_gmt, offset)}}, offset

    def _sleep(self) -> None:
        s = self.load("sleep.json") or {}
        if s.get("calendarDate") not in (None, self.day) or s.get("sleepTimeSeconds") is None:
            return
        found = self._sleep_period(s.get("sleepStartTimestampGMT"), s.get("sleepStartTimestampLocal"), s.get("sleepEndTimestampGMT"))
        if not found:
            self.warnings.append("sleep has a duration but no start/end; skipped")
            return
        period, _ = found
        c = self.component
        self.observation(self.key("sleep"), "loinc:93832-4", effective=period, value=s_to_min(s["sleepTimeSeconds"]), components=[
            c("loinc:93831-6", s_to_min(s.get("deepSleepSeconds"))),
            c("loinc:93830-8", s_to_min(s.get("lightSleepSeconds"))),
            c("loinc:93829-0", s_to_min(s.get("remSleepSeconds"))),
            c("loinc:93828-2", s_to_min(s.get("awakeSleepSeconds"))),
            c("gm:sleep-nap-duration", s_to_min(s.get("napTimeSeconds"))),
            c("gm:sleep-unmeasurable-duration", s_to_min(s.get("unmeasurableSleepSeconds"))),
            c("gm:sleep-awake-count", num(s.get("awakeCount"))),
            c("gm:sleep-avg-hr", num(s.get("avgHeartRate"))),
            c("gm:sleep-avg-stress", num(s.get("avgSleepStress"))),
            c("gm:sleep-avg-respiration", num(s.get("averageRespirationValue"))),
            c("gm:sleep-min-respiration", num(s.get("lowestRespirationValue"))),
            c("gm:sleep-max-respiration", num(s.get("highestRespirationValue"))),
            c("gm:sleep-avg-spo2", num(s.get("averageSpO2Value"))),
            c("gm:sleep-min-spo2", num(s.get("lowestSpO2Value"))),
            c("gm:sleep-need", num((s.get("sleepNeed") or {}).get("actual"))),
        ])
        scores = s.get("sleepScores") or {}
        sub = lambda name: scores.get(name) or {}
        self.observation(self.key("sleep-score"), "gm:sleep-score", effective=period, value=num(sub("overall").get("value")),
                         derivedFrom=[self.ref("Observation", "observation", self.key("sleep"))], components=[
            c("gm:sleep-score-qualifier", sub("overall").get("qualifierKey"), text=True),
            c("gm:sleep-deep-pct", num(sub("deepPercentage").get("value"))),
            c("gm:sleep-light-pct", num(sub("lightPercentage").get("value"))),
            c("gm:sleep-rem-pct", num(sub("remPercentage").get("value"))),
            c("gm:sleep-duration-qualifier", sub("totalDuration").get("qualifierKey"), text=True),
            c("gm:sleep-deep-qualifier", sub("deepPercentage").get("qualifierKey"), text=True),
            c("gm:sleep-light-qualifier", sub("lightPercentage").get("qualifierKey"), text=True),
            c("gm:sleep-rem-qualifier", sub("remPercentage").get("qualifierKey"), text=True),
            c("gm:sleep-restlessness-qualifier", sub("restlessness").get("qualifierKey"), text=True),
            c("gm:sleep-awake-count-qualifier", sub("awakeCount").get("qualifierKey"), text=True),
            c("gm:sleep-stress-qualifier", sub("stress").get("qualifierKey"), text=True),
        ])

    def _hrv(self) -> None:
        h = self.load("hrv.json") or {}
        summary = h.get("hrvSummary") or {}
        if not summary or summary.get("calendarDate") not in (None, self.day):
            return
        if summary.get("lastNightAvg") is None:
            return          # no overnight measurement (no sleep recorded); nothing to map
        found = None
        # The sleep window: newer responses give sleep*Timestamp*, older ones only the HRV
        # window start*/end*Timestamp*; sleep.json is the last resort.
        for prefix in ("sleep", ""):
            sk = (prefix + "StartTimestampGMT") if prefix else "startTimestampGMT"
            lk = (prefix + "StartTimestampLocal") if prefix else "startTimestampLocal"
            ek = (prefix + "EndTimestampGMT") if prefix else "endTimestampGMT"
            if h.get(sk) and h.get(ek):
                found = self._sleep_period(epoch_ms(h[sk]), epoch_ms(h[lk]) if h.get(lk) else None, epoch_ms(h[ek]))
                break
        if not found:
            s = self.load("sleep.json") or {}
            found = self._sleep_period(s.get("sleepStartTimestampGMT"), s.get("sleepStartTimestampLocal"),
                                       s.get("sleepEndTimestampGMT"))
        if not found:
            self.warnings.append("HRV summary without any sleep window; skipped")
            return
        period, offset = found
        base = summary.get("baseline") or {}
        extra = {}
        if base.get("balancedLow") is not None and base.get("balancedUpper") is not None:
            extra["referenceRange"] = [{"low": quantity(num(base["balancedLow"]), "ms"),
                                        "high": quantity(num(base["balancedUpper"]), "ms"),
                                        "text": "Personal balanced range (Garmin baseline)"}]
        c = self.component
        self.observation(self.key("hrv-night"), "gm:hrv-rmssd-night-avg", effective=period, value=num(summary.get("lastNightAvg")),
                         components=[
                             c("gm:hrv-rmssd-night-5min-high", num(summary.get("lastNight5MinHigh"))),
                             c("gm:hrv-rmssd-7day-avg", num(summary.get("weeklyAvg"))),
                             c("gm:hrv-status", summary.get("status"), text=True),
                             c("gm:hrv-baseline-low-upper", num(base.get("lowUpper"))),
                             c("gm:hrv-baseline-balanced-low", num(base.get("balancedLow"))),
                             c("gm:hrv-baseline-balanced-upper", num(base.get("balancedUpper"))),
                         ], **extra)
        readings = [(epoch_ms(r["readingTimeGMT"]), r.get("hrvValue")) for r in h.get("hrvReadings") or [] if r.get("readingTimeGMT")]
        series = self.sampled(readings, 300_000, "HRV readings") if readings else None
        if series:
            data, first, last = series
            self.observation(self.key("hrv-readings"), "gm:hrv-rmssd-readings", sampled=data,
                             effective={"effectivePeriod": {"start": iso(first, offset), "end": iso(last, offset)}})

    # -- body battery points and events ----------------------------------------

    def _body_battery(self) -> None:
        rows = [r for r in self.load("body_battery.json") or [] if r.get("date") == self.day]
        for row in rows:
            points = self._series(row.get("bodyBatteryValuesArray"), row.get("bodyBatteryValueDescriptorDTOList"),
                                  "bodyBatteryValueDescriptorKey", "bodyBatteryValueDescriptorIndex", "bodyBatteryLevel")
            by_time: dict[int, object] = {}
            for ts, level in points:
                if ts is None or level is None:
                    continue
                if ts in by_time and by_time[ts] != level:
                    self.warnings.append(f"Body Battery has two values at {ts}: {by_time[ts]} and {level}; kept {level}")
                by_time[ts] = level      # Garmin sometimes repeats the midnight point
            for ts, level in sorted(by_time.items()):
                self.observation(f"body-battery/{ts}", "gm:body-battery-level", value=num(level),
                                 effective={"effectiveDateTime": iso(ts, self.day_offset_ms)})
            for event in row.get("bodyBatteryActivityEvent") or []:
                if not event.get("eventStartTimeGmt") or event.get("bodyBatteryImpact") is None:
                    continue
                start = epoch_ms(event["eventStartTimeGmt"])
                offset = event.get("timezoneOffset", self.day_offset_ms)
                end = start + int(event.get("durationInMilliseconds") or 0)
                self.observation(f"body-battery-event/{start}", "gm:body-battery-event-impact",
                                 value=num(event["bodyBatteryImpact"]),
                                 effective={"effectivePeriod": {"start": iso(start, offset), "end": iso(end, offset)}},
                                 components=[self.component("gm:body-battery-event-type", event.get("eventType"), text=True)])

    # -- weight, BMI, fitness age -----------------------------------------------

    def _weight_and_fitness_age(self) -> None:
        w = self.load("weight.json") or {}
        for daily in w.get("dailyWeightSummaries") or []:
            for metric in daily.get("allWeightMetrics") or []:
                grams, ts, pk = metric.get("weight"), metric.get("timestampGMT"), metric.get("samplePk")
                if grams is None or ts is None or pk is None:
                    continue
                offset = (metric["date"] - ts) if metric.get("date") is not None else self.day_offset_ms
                extra = {"method": {"text": metric["sourceType"]}} if metric.get("sourceType") else {}
                self.observation(f"weight/{pk}", "loinc:29463-7", category="vital-signs", value=num(grams / 1000, 2),
                                 effective={"effectiveDateTime": iso(ts, offset)}, **extra)

        fa = self.load("fitness_age.json") or {}
        comp = fa.get("components") or {}
        bmi = comp.get("bmi") or {}
        if bmi.get("value") is not None and bmi.get("lastMeasurementDate"):
            self.observation(f"bmi/{bmi['lastMeasurementDate']}", "loinc:39156-5", category="vital-signs",
                             value=num(bmi["value"]), effective={"effectiveDateTime": bmi["lastMeasurementDate"]})
        c = self.component
        self.observation(self.key("fitness-age"), "gm:fitness-age", effective=self.day_period(),
                         value=num(fa.get("fitnessAge"), 1), components=[
                             c("gm:fitness-age-achievable", num(fa.get("achievableFitnessAge"), 1)),
                             c("gm:vigorous-days-weekly-avg", num((comp.get("vigorousDaysAvg") or {}).get("value"))),
                             c("gm:vigorous-minutes-weekly-avg", num((comp.get("vigorousMinutesAvg") or {}).get("value"))),
                         ])

    # -- workouts -------------------------------------------------------------

    def _workouts(self) -> None:
        for act in self.load("activities.json") or []:
            aid = act.get("activityId")
            if aid is None or not act.get("startTimeGMT") or act.get("duration") is None:
                continue
            start = epoch_ms(act["startTimeGMT"])
            offset = (epoch_ms(act["startTimeLocal"]) - start) if act.get("startTimeLocal") else self.day_offset_ms
            end = epoch_ms(act["endTimeGMT"]) if act.get("endTimeGMT") else start + int(act["duration"] * 1000)
            period = {"start": iso(start, offset), "end": iso(end, offset)}
            folder = self.folder / "activities" / str(aid)
            detail = (load_json(folder / "detail.json") or {}).get("summaryDTO") or {}

            derived = []
            zip_path = folder / "original.zip"
            if zip_path.is_file() and zip_path.stat().st_size:
                doc_key = f"activity/{aid}/original"
                uploaded = (load_json(folder / "detail.json") or {}).get("metadataDTO", {}).get("uploadedDate")
                self.resources.append({
                    "resourceType": "DocumentReference",
                    "identifier": self.ident("document", doc_key),
                    "status": "current",
                    "type": {"text": "Garmin activity recording (FIT file in ZIP)"},
                    "subject": self.subject(),
                    "date": iso(epoch_ms(uploaded), 0) if uploaded else period["start"],
                    "content": [{"attachment": {
                        "extension": [{"url": f"{self.base}/StructureDefinition/archive-path",
                                       "valueString": str(zip_path.relative_to(self.data_dir))}],
                        "contentType": "application/zip",
                        "size": zip_path.stat().st_size,
                        "hash": sha1_b64(zip_path),
                        "title": f"{aid}_ACTIVITY.zip",
                        "creation": iso(start, offset),
                    }}],
                    "context": {"period": period},
                })
                derived = [self.ref("DocumentReference", "document", doc_key)]

            c = self.component
            hrz = [c(f"gm:workout-hr-zone-{i}-time", s_to_min(act.get(f"hrTimeInZone_{i}"))) for i in range(1, 6)]
            pwz = [c(f"gm:workout-power-zone-{i}-time", s_to_min(act.get(f"powerTimeInZone_{i}"))) for i in range(1, 6)]
            extra = {"derivedFrom": derived} if derived else {}
            self.observation(f"activity/{aid}", "loinc:55411-3", category="activity", value=s_to_min(act["duration"]),
                             effective={"effectivePeriod": period}, components=[
                c("loinc:73985-4", (act.get("activityType") or {}).get("typeKey"), text=True),
                c("loinc:55425-3", num(act.get("averageHR"))),
                c("loinc:55426-1", num(act.get("maxHR"))),
                c("loinc:55424-6", num(act.get("calories"))),
                c("loinc:77592-4", num(act.get("moderateIntensityMinutes"))),
                c("loinc:77593-2", num(act.get("vigorousIntensityMinutes"))),
                c("gm:workout-distance", num(act.get("distance"), 1)),
                c("gm:workout-elapsed-duration", s_to_min(act.get("elapsedDuration"))),
                c("gm:workout-moving-duration", s_to_min(act.get("movingDuration"))),
                c("gm:workout-steps", num(act.get("steps"))),
                c("gm:workout-avg-speed", num(act.get("averageSpeed"), 3)),
                c("gm:workout-max-speed", num(act.get("maxSpeed"), 3)),
                c("gm:workout-avg-grade-adjusted-speed", num(act.get("avgGradeAdjustedSpeed"), 3)),
                c("gm:workout-elevation-gain", num(act.get("elevationGain"), 1)),
                c("gm:workout-elevation-loss", num(act.get("elevationLoss"), 1)),
                c("gm:workout-avg-power", num(act.get("avgPower"))),
                c("gm:workout-max-power", num(act.get("maxPower"))),
                c("gm:workout-normalized-power", num(act.get("normPower"))),
                c("gm:workout-avg-cadence", num(act.get("averageRunningCadenceInStepsPerMinute"), 1)),
                c("gm:workout-max-cadence", num(act.get("maxRunningCadenceInStepsPerMinute"), 1)),
                c("gm:workout-avg-stride-length", num(act.get("avgStrideLength"), 1)),
                c("gm:workout-avg-ground-contact-time", num(act.get("avgGroundContactTime"), 1)),
                c("gm:workout-avg-vertical-oscillation", num(act.get("avgVerticalOscillation"), 2)),
                c("gm:workout-avg-vertical-ratio", num(act.get("avgVerticalRatio"), 2)),
                c("gm:workout-aerobic-training-effect", num(act.get("aerobicTrainingEffect"), 1)),
                c("gm:workout-anaerobic-training-effect", num(act.get("anaerobicTrainingEffect"), 1)),
                c("gm:workout-training-effect-label", act.get("trainingEffectLabel"), text=True),
                c("gm:workout-training-load", num(act.get("activityTrainingLoad"), 1)),
                c("gm:workout-body-battery-change", num(act.get("differenceBodyBattery"))),
                c("gm:workout-sweat-loss", num(act.get("waterEstimated"))),
                c("gm:workout-stamina-start", num(detail.get("beginPotentialStamina"))),
                c("gm:workout-stamina-end", num(detail.get("endPotentialStamina"))),
                c("gm:workout-impact-load", num(detail.get("impactLoad"))),
                c("gm:workout-feel", num(detail.get("directWorkoutFeel"))),
                c("gm:workout-rpe", num(detail.get("directWorkoutRpe"))),
                *hrz, *pwz,
            ], **extra)


# --------------------------------------------------------------------------
# Shared resources, manifest, command
# --------------------------------------------------------------------------


def patient_resource(base: str, user_id: str, name: str) -> dict:
    patient = {"resourceType": "Patient",
               "identifier": [{"system": f"{base}/identifier/garmin-user", "value": user_id}],
               "active": True}
    if name:
        parts = name.split()
        human = {"use": "usual", "text": name}
        if len(parts) > 1:
            human["family"] = parts[-1]
            human["given"] = parts[:-1]
        patient["name"] = [human]
    return patient


def device_resource(base: str, device_id: str, user_id: str) -> dict:
    return {"resourceType": "Device",
            "identifier": [{"system": f"{base}/identifier/garmin-device", "value": device_id}],
            "manufacturer": "Garmin",
            "patient": {"reference": f"Patient?identifier={base}/identifier/garmin-user|{user_id}"}}


def write_ndjson(path: Path, resources: list[dict]) -> str:
    body = "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in resources)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(body)
    os.replace(tmp, path)
    return hashlib.sha256(body.encode()).hexdigest()


def load_fhir_manifest(path: Path) -> dict:
    if path.is_file():
        return json.loads(path.read_text())
    return {"mapping_version": MAPPING_VERSION, "days": {}, "user_ids": [], "device_ids": []}


def save_fhir_manifest(path: Path, manifest: dict) -> None:
    manifest["updated_at"] = ga.now_iso()
    manifest["days"] = dict(sorted(manifest["days"].items()))
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=1) + "\n")
    os.replace(tmp, path)


def profile_timezone(data_dir: Path) -> str | None:
    """Time zone from the newest profile_settings.json snapshot, if any."""
    for path in sorted(data_dir.glob("days/*/*/profile_settings.json"), reverse=True):
        tz = (load_json(path) or {}).get("timeZone")
        if tz:
            return tz
    return None


def cmd_map(args) -> int:
    data_dir = Path(ga._env("GARMIN_ARCHIVE_DIR", str(REPO_DIR / "data"))).expanduser()
    base = ga._env("GARMIN_FHIR_BASE", DEFAULT_BASE).rstrip("/")
    patient_name = ga._env("GARMIN_FHIR_PATIENT_NAME")
    archive = json.loads((data_dir / "manifest.json").read_text())
    out_dir = data_dir / "fhir"
    fm_path = out_dir / "manifest.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    fm = load_fhir_manifest(fm_path)
    if fm.get("mapping_version") != MAPPING_VERSION:
        log.info("Mapping version changed %s -> %s: every day will be mapped again",
                 fm.get("mapping_version"), MAPPING_VERSION)
        fm["mapping_version"] = MAPPING_VERSION
    codes = Codes(base)
    fallback_tz = profile_timezone(data_dir)

    days = sorted(d for d, e in archive["days"].items() if e.get("status") in ("final", "provisional"))
    if args.start:
        days = [d for d in days if d >= args.start]
    if args.end:
        days = [d for d in days if d <= args.end]

    mapped = skipped = failed = not_local = 0
    users, devices = set(fm.get("user_ids", [])), set(fm.get("device_ids", []))
    for day in days:
        src = archive["days"][day]
        prev = fm["days"].get(day, {})
        if (not args.force and prev.get("status") == "ok" and prev.get("mapping_version") == MAPPING_VERSION
                and prev.get("source_fetched_at") == src.get("fetched_at") and (out_dir / prev.get("file", "-")).is_file()):
            skipped += 1
            continue
        folder = data_dir / src["path"]
        if not folder.is_dir():
            # Adopted from a remote manifest: the raw files are only in the cloud archive.
            log.warning("%s: raw files are not in the local archive (only on %s); not mapped",
                        day, src.get("adopted_from", "the upload targets"))
            not_local += 1
            continue
        mapper = DayMapper(codes, base, day, folder, data_dir, fallback_tz)
        try:
            resources = mapper.run()
        except (MappingError, KeyError, TypeError, ValueError) as exc:
            failed += 1
            fm["days"][day] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}",
                               "mapping_version": MAPPING_VERSION, "source_fetched_at": src.get("fetched_at")}
            log.error("%s: %s", day, fm["days"][day]["error"])
            continue
        rel = f"days/{day[:4]}/{day}.ndjson"
        digest = write_ndjson(out_dir / rel, resources)
        counts: dict[str, int] = {}
        for r in resources:
            counts[r["resourceType"]] = counts.get(r["resourceType"], 0) + 1
        fm["days"][day] = {"status": "ok", "file": rel, "sha256": digest, "resources": counts,
                           "warnings": mapper.warnings, "mapping_version": MAPPING_VERSION,
                           "source_status": src.get("status"), "source_fetched_at": src.get("fetched_at"),
                           "mapped_at": ga.now_iso()}
        if mapper.user_id:
            users.add(mapper.user_id)
        devices |= mapper.device_ids
        mapped += 1
        for warning in mapper.warnings:
            log.warning("%s: %s", day, warning)
        log.info("%s: %d resources %s", day, len(resources), counts)

    if len(users) > 1:
        log.error("The archive holds more than one Garmin user: %s", sorted(users))
        failed += 1
    shared = []
    if users:
        user = sorted(users)[0]
        shared.append(patient_resource(base, user, patient_name))
        shared += [device_resource(base, d, user) for d in sorted(devices)]
    write_ndjson(out_dir / "shared.ndjson", shared)
    fm["user_ids"], fm["device_ids"] = sorted(users), sorted(devices)
    save_fhir_manifest(fm_path, fm)
    log.info("Done: mapped=%d unchanged=%d not_local=%d failed=%d (output in %s)", mapped, skipped, not_local, failed, out_dir)
    return ga.EXIT_PARTIAL if failed else ga.EXIT_OK


# --------------------------------------------------------------------------
# Loading into a FHIR server (Medplum)
# --------------------------------------------------------------------------


class LoadError(Exception):
    """A request to the FHIR server failed for good."""


class RateLimited(LoadError):
    """The server's FHIR quota is used up; retry after the window resets."""


QUOTA_WINDOW_S = 60          # Medplum's FHIR interaction quota resets every minute
QUOTA_RETRIES = 10


class FhirClient:
    """OAuth2 client-credentials FHIR client with retries and backoff."""

    def __init__(self, url: str, client_id: str, secret: str, *, max_attempts: int = 5,
                 backoff_base: float = 2.0, backoff_max: float = 60.0, timeout: float = 60.0, sleep=None):
        self.url = url.rstrip("/")
        self.client_id, self.secret = client_id, secret
        self.max_attempts, self.backoff_base, self.backoff_max = max_attempts, backoff_base, backoff_max
        self.timeout = timeout
        self._sleep = sleep or __import__("time").sleep
        self._token = ""
        self.requests = 0

    def _token_request(self) -> str:
        form = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self.client_id,
                                       "client_secret": self.secret}).encode()
        status, body = self._send("POST", "/oauth2/token", form, "application/x-www-form-urlencoded", "application/json",
                                  auth=False)
        if status != 200:
            raise LoadError(f"token request failed: HTTP {status} {body!r:.200}")
        return body["access_token"]

    def request(self, method: str, path: str, body=None, *, raw: bytes | None = None,
                content_type: str = "application/fhir+json", ok=(200, 201)) -> tuple[int, object]:
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        status, payload = self._send(method, path, data, content_type, "application/fhir+json")
        if status not in ok:
            raise LoadError(f"{method} {path.split('?')[0]}: HTTP {status}: {outcome_text(payload)}")
        return status, payload

    def _send(self, method, path, data, content_type, accept, auth=True):
        refreshed = False
        attempt = 0
        while True:
            attempt += 1
            headers = {"Accept": accept, "User-Agent": f"garmin-fhir/{MAPPING_VERSION}"}
            if data is not None:
                headers["Content-Type"] = content_type
            if auth:
                if not self._token:
                    self._token = self._token_request()
                headers["Authorization"] = f"Bearer {self._token}"
            req = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
            self.requests += 1
            retry_after = None
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    return response.status, _decode(response.read())
            except urllib.error.HTTPError as exc:
                status, payload = exc.code, _decode(exc.read())
                if status == 401 and auth and not refreshed:
                    refreshed, self._token = True, ""
                    attempt -= 1
                    continue
                if status != 429 and status < 500:
                    return status, payload
                retry_after = ga.parse_retry_after(exc.headers.get("Retry-After"))
                if status == 429 and retry_after is None:
                    retry_after = QUOTA_WINDOW_S
                reason = f"HTTP {status}: {outcome_text(payload)}"
            except (urllib.error.URLError, OSError) as exc:
                reason = f"{type(exc).__name__}: {exc}"
            if attempt >= self.max_attempts:
                raise LoadError(f"{method} {path.split('?')[0]}: {reason} after {attempt} attempts")
            delay = ga.backoff_delay(attempt, self.backoff_base, self.backoff_max, retry_after)
            log.warning("%s %s: %s (attempt %d/%d), retrying in %.1fs", method, path.split("?")[0], reason,
                        attempt, self.max_attempts, delay)
            self._sleep(delay)


def _decode(raw: bytes):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace")[:500]


def outcome_text(payload) -> str:
    if isinstance(payload, dict) and payload.get("resourceType") == "OperationOutcome":
        return "; ".join(f"{i.get('details', {}).get('text', i.get('code'))}"
                         + (f" @ {i['expression'][0]}" if i.get("expression") else "")
                         for i in payload.get("issue", []))[:500]
    return str(payload)[:300]


def ident_key(resource: dict) -> str:
    """'Observation|<system>|<value>' for a mapped resource."""
    ident = resource["identifier"][0]
    return f"{resource['resourceType']}|{ident['system']}|{ident['value']}"


def conditional_url(key: str) -> str:
    rtype, system, value = key.split("|", 2)
    return f"{rtype}?identifier={urllib.parse.quote(system, safe=':/')}|{urllib.parse.quote(value, safe=':/')}"


def transaction(resources: list[dict]) -> dict:
    return {"resourceType": "Bundle", "type": "transaction", "entry": [
        {"resource": r, "request": {"method": "PUT", "url": conditional_url(ident_key(r))}} for r in resources]}


class Loader:
    def __init__(self, client: FhirClient, data_dir: Path, project_id: str, sleep=None):
        self._sleep = sleep or __import__("time").sleep
        self.client = client
        self.data_dir = data_dir
        self.fhir_dir = data_dir / "fhir"
        self.manifest_path = self.fhir_dir / "load-manifest.json"
        self.project_id = project_id
        self.manifest = self._load_manifest()

    def _load_manifest(self) -> dict:
        fresh = {"project_id": self.project_id, "terminology": {}, "shared": {}, "days": {}}
        if not self.manifest_path.is_file():
            return fresh
        manifest = json.loads(self.manifest_path.read_text())
        if manifest.get("project_id") != self.project_id:
            log.warning("Server project changed (%s -> %s): everything will be loaded again",
                        manifest.get("project_id"), self.project_id)
            return fresh
        return manifest

    def save(self) -> None:
        self.manifest["updated_at"] = ga.now_iso()
        self.manifest["days"] = dict(sorted(self.manifest["days"].items()))
        tmp = self.manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.manifest, indent=1) + "\n")
        os.replace(tmp, self.manifest_path)

    # -- terminology ---------------------------------------------------------

    def sync_terminology(self) -> None:
        for path in (CODESYSTEM_FILE, CODESYSTEM_FILE.with_name("ValueSet-garmin-metrics.json")):
            resource = json.loads(path.read_text())
            resource.pop("id", None)
            rtype, url, version = resource["resourceType"], resource["url"], resource["version"]
            _, bundle = self.client.request("GET", f"/fhir/R4/{rtype}?url={urllib.parse.quote(url, safe='')}")
            entries = (bundle or {}).get("entry") or []
            if entries and entries[0]["resource"].get("version") == version:
                continue
            if entries:
                resource["id"] = entries[0]["resource"]["id"]
                self.client.request("PUT", f"/fhir/R4/{rtype}/{resource['id']}", resource)
            else:
                self.client.request("POST", f"/fhir/R4/{rtype}", resource)
            log.info("%s %s set to version %s on the server", rtype, url, version)
            self.manifest["terminology"][rtype] = version

    # -- one batch of resources ---------------------------------------------

    def load_batch(self, name: str, ndjson: Path, previous: dict) -> dict:
        resources = [json.loads(line) for line in ndjson.read_text().splitlines() if line.strip()]
        binaries = dict(previous.get("binaries") or {})
        uploaded: list[str] = []
        for r in resources:
            if r["resourceType"] == "DocumentReference":
                self._attach_binary(r, binaries, uploaded)
        try:
            _, result = self.client.request("POST", "/fhir/R4", transaction(resources))
        except LoadError:
            for binary_id in uploaded:       # do not leave unreferenced uploads behind
                self._delete(f"/fhir/R4/Binary/{binary_id}")
            raise
        statuses = [e.get("response", {}).get("status", "") for e in (result or {}).get("entry", [])]
        if any(s.startswith("429") for s in statuses):
            # Nothing is lost: every entry is a conditional upsert, so the batch can simply be sent again.
            raise RateLimited(f"{name}: FHIR quota exhausted ({statuses.count('429')} of {len(statuses)} entries refused)")
        if len(statuses) != len(resources) or not all(s.startswith("2") for s in statuses):
            bad = [f"{ident_key(r).split('|')[-1]}={st}" for r, st in zip(resources, statuses) if not st.startswith("2")]
            raise LoadError(f"{name}: transaction returned {len(statuses)} result(s) for {len(resources)} resource(s); "
                            f"failed entries: {bad[:10] or 'none listed'}")

        keys = sorted(ident_key(r) for r in resources)
        for key in sorted(set(previous.get("identifiers") or []) - set(keys)):
            self._delete(f"/fhir/R4/{conditional_url(key)}")
            log.info("%s: deleted %s, no longer produced by the mapping", name, key)
        for doc_key, old in (previous.get("binaries") or {}).items():
            if binaries.get(doc_key, {}).get("id") != old["id"]:
                self._delete(f"/fhir/R4/Binary/{old['id']}")
        created = sum(1 for s in statuses if s.startswith("201"))
        return {"sha256": hashlib.sha256(ndjson.read_bytes()).hexdigest(), "loaded_at": ga.now_iso(),
                "resources": len(resources), "created": created, "updated": len(resources) - created,
                "identifiers": keys, "binaries": binaries}

    def load_with_quota(self, name: str, ndjson: Path, previous: dict) -> dict:
        """load_batch, waiting out the server's quota window when it is exhausted."""
        for attempt in range(1, QUOTA_RETRIES + 1):
            try:
                return self.load_batch(name, ndjson, previous)
            except RateLimited as exc:
                if attempt == QUOTA_RETRIES:
                    raise
                log.warning("%s; waiting %ds for the quota to reset (attempt %d/%d)", exc, QUOTA_WINDOW_S,
                            attempt, QUOTA_RETRIES)
                self._sleep(QUOTA_WINDOW_S)
        raise AssertionError("unreachable")

    def _attach_binary(self, doc: dict, binaries: dict, uploaded: list[str]) -> None:
        attachment = doc["content"][0]["attachment"]
        ext = [e for e in attachment.get("extension", []) if e["url"].endswith("/archive-path")]
        if not ext:
            return
        key = ident_key(doc)
        known = binaries.get(key)
        if known and known.get("hash") == attachment.get("hash"):
            attachment["url"] = f"Binary/{known['id']}"
            return
        path = self.data_dir / ext[0]["valueString"]
        if not path.is_file():
            raise LoadError(f"{key}: archive file {path} is missing")
        if sha1_b64(path) != attachment.get("hash"):
            raise LoadError(f"{key}: {path} does not match the mapped hash; map the day again")
        _, binary = self.client.request("POST", "/fhir/R4/Binary", raw=path.read_bytes(),
                                        content_type=attachment.get("contentType", "application/octet-stream"))
        attachment["url"] = f"Binary/{binary['id']}"
        binaries[key] = {"id": binary["id"], "hash": attachment["hash"], "bytes": path.stat().st_size}
        uploaded.append(binary["id"])

    def _delete(self, path: str) -> None:
        try:
            self.client.request("DELETE", path, ok=(200, 204, 404, 410))
        except LoadError as exc:
            log.warning("cleanup failed: %s", exc)


def cmd_load(args) -> int:
    data_dir = Path(ga._env("GARMIN_ARCHIVE_DIR", str(REPO_DIR / "data"))).expanduser()
    url, cid, secret = (ga._env("GARMIN_MEDPLUM_URL"), ga._env("GARMIN_MEDPLUM_CLIENT_ID"),
                        ga._env("GARMIN_MEDPLUM_CLIENT_SECRET"))
    project_id = ga._env("GARMIN_MEDPLUM_PROJECT_ID")
    if not (url and cid and secret and project_id):
        log.error("GARMIN_MEDPLUM_URL, _PROJECT_ID, _CLIENT_ID and _CLIENT_SECRET must be set "
                  "(run medplum/setup.py project)")
        return ga.EXIT_CONFIG
    fm_path = data_dir / "fhir" / "manifest.json"
    if not fm_path.is_file():
        log.error("%s not found; run 'garmin_fhir.py map' first", fm_path)
        return ga.EXIT_CONFIG
    fm = json.loads(fm_path.read_text())
    client = FhirClient(url, cid, secret)
    loader = Loader(client, data_dir, project_id)
    try:
        loader.sync_terminology()
        shared = data_dir / "fhir" / "shared.ndjson"
        digest = hashlib.sha256(shared.read_bytes()).hexdigest()
        if args.force or loader.manifest["shared"].get("sha256") != digest:
            loader.manifest["shared"] = loader.load_with_quota("shared", shared, loader.manifest["shared"])
            loader.save()
            log.info("shared: Patient and %d Device(s) loaded", loader.manifest["shared"]["resources"] - 1)
    except LoadError as exc:
        log.error("%s", exc)
        loader.save()
        return ga.EXIT_PARTIAL

    loaded = skipped = failed = 0
    for day, entry in sorted(fm["days"].items()):
        if (args.start and day < args.start) or (args.end and day > args.end) or entry.get("status") != "ok":
            continue
        ndjson = data_dir / "fhir" / entry["file"]
        previous = loader.manifest["days"].get(day, {})
        if not args.force and previous.get("sha256") == entry["sha256"] and previous.get("status") == "ok":
            skipped += 1
            continue
        try:
            result = loader.load_with_quota(day, ndjson, previous)
        except LoadError as exc:
            failed += 1
            loader.manifest["days"][day] = {**previous, "status": "failed", "error": str(exc), "failed_at": ga.now_iso()}
            loader.save()
            log.error("%s: %s", day, exc)
            if failed >= 3 and loaded == 0:
                log.error("Three days failed and none loaded; stopping")
                break
            continue
        loader.manifest["days"][day] = {"status": "ok", **result}
        loader.save()
        loaded += 1
        log.info("%s: %d resources (%d created, %d updated)", day, result["resources"], result["created"], result["updated"])
    loader.save()
    log.info("Done: loaded=%d unchanged=%d failed=%d requests=%d", loaded, skipped, failed, client.requests)
    return ga.EXIT_PARTIAL if failed else ga.EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="garmin_fhir.py", description="Map archived Garmin days to FHIR R4 and load them.")
    parser.add_argument("--env-file", help="file with KEY=VALUE settings (default: .env next to the script)")
    sub = parser.add_subparsers(dest="command", required=True)
    m = sub.add_parser("map", help="map days whose source or mapping changed")
    ld = sub.add_parser("load", help="load mapped days that changed into the FHIR server")
    for p in (m, ld):
        p.add_argument("--from", dest="start", metavar="YYYY-MM-DD")
        p.add_argument("--to", dest="end", metavar="YYYY-MM-DD")
        p.add_argument("--force", action="store_true", help="process every day in range again")
    args = parser.parse_args(argv)
    ga.load_env_file(Path(args.env_file) if args.env_file else REPO_DIR / ".env")
    logging.basicConfig(level=ga._env("GARMIN_LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    try:
        return cmd_map(args) if args.command == "map" else cmd_load(args)
    except (MappingError, FileNotFoundError) as exc:
        log.error("%s", exc)
        return ga.EXIT_CONFIG


if __name__ == "__main__":
    sys.exit(main())
