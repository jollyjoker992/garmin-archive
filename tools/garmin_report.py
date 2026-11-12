#!/usr/bin/env python3
"""PDF reports from the Garmin data in the local Medplum: one activity, or a
wellness period (week, month, or any range).

Reports are made in two steps so that the evaluation is written by a person or
an agent from computed facts, never invented by this script:

  1. facts   python3 tools/garmin_report.py activity <activity> --facts facts.json
             python3 tools/garmin_report.py wellness --week 2026-W45 --facts facts.json
  2. render  python3 tools/garmin_report.py activity <activity> --narrative narrative.json [--out report.pdf]
             python3 tools/garmin_report.py wellness --week 2026-W45 --narrative narrative.json [--out report.pdf]

Period options for wellness: --week YYYY-Www | --month YYYY-MM | --from YYYY-MM-DD --to YYYY-MM-DD.
Activity option: --route adds the GPS route shape (off by default: it shows where you live and run).

narrative.json:
  {"language": "en",                       optional; built in: en, vi
   "headline": "one sentence",
   "summary": "a short paragraph",
   "evaluation": ["finding grounded in the facts", ...],
   "recommendations": ["concrete, actionable step", ...],
   "data_notes": ["..."],                  optional: the facts' data_notes in the report language
   "labels": {"summary": "...", ...}}      optional: titles, axes, table headers for other languages
                                           (python3 tools/garmin_report.py labels --lang en lists them)

Fonts: the PDF and charts embed DejaVu Sans (from matplotlib; covers Latin
including Vietnamese, Greek, Cyrillic). For other scripts pass a TrueType font
with --font FILE.ttf [--font-bold FILE.ttf] or GARMIN_REPORT_FONT(_BOLD); the
script refuses to render text the font cannot show.

Data comes from Medplum through tools/medplum_agent.py (read-only client); the
activity report also decodes the workout's FIT file. Reports are written to
data/reports/ (mode 600) unless --out says otherwise.

Optional dependencies (not part of the standard-library core):
  python3 -m pip install -r tools/requirements-reports.txt
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import medplum_agent as ma  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
REPORT_DIR = ma.CACHE.parent / "reports"
LOINC = "http://loinc.org"
GM = ma.GM_SYSTEM
DEPS_HINT = "reports need matplotlib and reportlab: python3 -m pip install -r tools/requirements-reports.txt"

# Colors: the validated default palette of the dataviz method (light mode, print).
INK, INK2, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e1", "#ffffff"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]          # categorical slots 1-4, fixed order
RAMP = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281"]   # ordinal blue, steps 250-650


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def fail(message: str) -> None:
    print(f"garmin_report: {message}", file=sys.stderr)
    sys.exit(2)


def mean(values):
    values = [v for v in values if v is not None]
    return st.fmean(values) if values else None


def _int(value):
    return None if value is None else int(round(value))


def rnd(value, digits=1):
    return None if value is None else round(value, digits)


def comps(obs: dict) -> dict:
    return {c["code"]["coding"][0]["code"]: (c.get("valueQuantity") or {}).get("value")
            if c.get("valueQuantity") else (c.get("valueCodeableConcept") or {}).get("text")
            for c in obs.get("component") or []}


def pace_text(min_per_km):
    if not min_per_km:
        return "-"
    m = int(min_per_km)
    s = round((min_per_km - m) * 60)
    if s == 60:
        m, s = m + 1, 0
    return f"{m}:{s:02d}/km"


def dur_text(seconds):
    if seconds is None:
        return "-"
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


OBS_FIELDS = ("identifier { value } effectivePeriod { start end } effectiveDateTime valueQuantity { value unit } "
              "referenceRange { low { value } high { value } } "
              "component { code { coding { code } } valueQuantity { value unit } valueCodeableConcept { text } }")


def observations_by_key(keys: list[str]) -> dict[str, dict]:
    """Observations by their identifier value ('2026-11-09/steps'), fetched in batches."""
    out = {}
    for i in range(0, len(keys), 60):
        batch = ",".join(f"{ma.OBS_SYSTEM}|{k}" for k in keys[i:i + 60])
        res = ma.graphql('query($i: String) { ObservationList(identifier: $i, _count: 1000) { %s } }' % OBS_FIELDS,
                         {"i": batch})
        for o in res["data"]["ObservationList"]:
            out[o["identifier"][0]["value"]] = o
    return out


def value(obs: dict | None):
    return ((obs or {}).get("valueQuantity") or {}).get("value")


# --------------------------------------------------------------------------
# Activity facts
# --------------------------------------------------------------------------


def rolling(values, window):
    out, buf, total = [], [], 0.0
    for v in values:
        if v is None:
            out.append(None)
            continue
        buf.append(v)
        total += v
        if len(buf) > window:
            total -= buf.pop(0)
        out.append(total / len(buf))
    return out


def km_splits(rows):
    splits, start = [], 0
    for i, r in enumerate(rows):
        d = r.get("distance_m")
        if d is None:
            continue
        if d >= (len(splits) + 1) * 1000 or i == len(rows) - 1:
            seg = rows[start:i + 1]
            dist = (seg[-1].get("distance_m") or 0) - (seg[0].get("distance_m") or 0)
            secs = (seg[-1]["elapsed_s"] - seg[0]["elapsed_s"]) or 1
            if dist < 200 and splits:          # tiny tail: fold into the previous split
                break
            alts = [x.get("altitude_m") for x in seg if x.get("altitude_m") is not None]
            gain = sum(max(0, b - a) for a, b in zip(alts, alts[1:]))
            cad_key = "cadence_spm" if "cadence_spm" in seg[0] else "cadence_rpm"
            splits.append({
                "km": len(splits) + 1, "distance_m": round(dist), "time_s": round(secs),
                "pace_min_per_km": round(secs / 60 / (dist / 1000), 2) if dist else None,
                "avg_hr": _int(mean(x.get("heart_rate") for x in seg)),
                "max_hr": max((x.get("heart_rate") or 0 for x in seg), default=None) or None,
                "avg_cadence": _int(mean(x.get(cad_key) for x in seg)),
                "avg_power_w": _int(mean(x.get("power_w") for x in seg)),
                "elevation_gain_m": rnd(gain, 1),
            })
            start = i
    return splits


def activity_facts(activity: str, route: bool = False) -> tuple[dict, dict]:
    activity = activity.removeprefix("activity/")
    found = observations_by_key([f"activity/{activity}"])
    if not found:
        fail(f"no workout Observation activity/{activity} in Medplum (see: python3 tools/medplum_agent.py fit list)")
    obs = found[f"activity/{activity}"]
    c = comps(obs)
    data, offset = ma.fit_bytes(activity)
    msgs = ma.decode_fit(data)
    session = (msgs.get("session_mesgs") or [{}])[0]
    rows = ma.fit_records(msgs, offset, gps=route)
    laps = ma.fit_laps(msgs, offset)
    start, end = obs["effectivePeriod"]["start"], obs["effectivePeriod"]["end"]
    day = start[:10]
    sport = str(session.get("sport") or c.get("73985-4") or "activity")

    # Heart-rate zones as the watch defined them for this session.
    zone_rows = []
    for z in msgs.get("time_in_zone_mesgs") or []:
        if z.get("reference_mesg") == "session" and z.get("time_in_hr_zone"):
            bounds = z.get("hr_zone_high_boundary") or []
            times = z["time_in_hr_zone"]
            total = sum(t for t in times if t) or 1
            # Garmin: [below Z1, Z1 .. Z5, above the top boundary (max HR)]
            names = ["Below Z1"] + [f"Z{i}" for i in range(1, len(bounds))] + ["Above max"]
            lows = [0] + list(bounds)
            for i, t in enumerate(times):
                if i >= len(names):
                    break
                hi = bounds[i] if i < len(bounds) else None
                zone_rows.append({"zone": names[i], "from_bpm": lows[i] if i else None, "to_bpm": hi,
                                  "time_s": round(t or 0), "share_pct": round(100 * (t or 0) / total, 1)})
            zone_meta = {"calc": str(z.get("hr_calc_type")), "max_hr": z.get("max_heart_rate"),
                         "resting_hr": z.get("resting_heart_rate"), "threshold_hr": z.get("threshold_heart_rate"),
                         "ftp_w": z.get("functional_threshold_power")}
            break
    else:
        zone_meta = {}

    # Cardiac drift and aerobic decoupling (pace:HR), excluding the first 5 minutes.
    steady = [r for r in rows if (r.get("elapsed_s") or 0) >= 300 and r.get("heart_rate") and (r.get("speed_mps") or 0) > 0.5]
    decoupling = None
    if len(steady) > 120:
        half = len(steady) // 2
        ef = lambda part: mean(r["speed_mps"] for r in part) / mean(r["heart_rate"] for r in part)
        ef1, ef2 = ef(steady[:half]), ef(steady[half:])
        decoupling = {"first_half_speed_per_beat": round(ef1 * 1000, 2), "second_half_speed_per_beat": round(ef2 * 1000, 2),
                      "decoupling_pct": round((ef1 - ef2) / ef1 * 100, 1),
                      "first_half_avg_hr": rnd(mean(r["heart_rate"] for r in steady[:half]), 0),
                      "second_half_avg_hr": rnd(mean(r["heart_rate"] for r in steady[half:]), 0),
                      "note": "speed_per_beat = m/s per 1000 bpm; decoupling > 5 % usually means the effort was not fully aerobic or fatigue/heat built up"}
    splits = km_splits(rows)
    moving = [r["speed_mps"] for r in rows if (r.get("speed_mps") or 0) > 0.5]
    smoothed = [v for v in rolling([r.get("speed_mps") for r in rows], 30) if v and v > 0.5]
    cad_key = "cadence_spm" if any("cadence_spm" in r for r in rows) else "cadence_rpm"
    cadence = [r[cad_key] for r in rows if r.get(cad_key)]

    # Readiness that morning and recent comparable workouts.
    ctx = observations_by_key([f"{day}/{k}" for k in ("sleep", "sleep-score", "hrv-night", "resting-hr", "body-battery")])
    hrv = ctx.get(f"{day}/hrv-night")
    since = (dt.datetime.fromisoformat(start) - dt.timedelta(days=56)).isoformat()
    history = []
    for w in ma.list_workouts(since, start):
        if w["activity"] == activity or (w.get("type") or "") != (c.get("73985-4") or ""):
            continue
        if w.get("minutes") and w.get("distance_m") and w.get("avg_hr"):
            speed = w["distance_m"] / (w["minutes"] * 60)
            history.append({"date": w["start"][:10], "distance_km": round(w["distance_m"] / 1000, 2),
                            "pace_min_per_km": round(1000 / speed / 60, 2), "avg_hr": w["avg_hr"],
                            "speed_per_beat": round(speed / w["avg_hr"] * 1000, 2)})
    this_speed = (session.get("enhanced_avg_speed") or 0)
    this_ef = round(this_speed / session["avg_heart_rate"] * 1000, 2) if session.get("avg_heart_rate") and this_speed else None

    facts = {
        "kind": "activity",
        "activity": activity,
        "type": c.get("73985-4") or sport,
        "start": start, "end": end,
        "summary": {
            "distance_km": rnd((session.get("total_distance") or c.get("workout-distance") or 0) / 1000, 2),
            "elapsed": dur_text(session.get("total_elapsed_time")), "moving": dur_text(session.get("total_timer_time")),
            "avg_pace": pace_text(1000 / this_speed / 60) if this_speed else None,
            "avg_hr": session.get("avg_heart_rate"), "max_hr": session.get("max_heart_rate"),
            "avg_power_w": session.get("avg_power"), "normalized_power_w": session.get("normalized_power"),
            "avg_cadence_spm": round(2 * ((session.get("avg_running_cadence") or 0) + (session.get("avg_fractional_cadence") or 0)), 1)
            if sport in ma.STEP_SPORTS and session.get("avg_running_cadence") else None,
            "elevation_gain_m": session.get("total_ascent"), "elevation_loss_m": session.get("total_descent"),
            "calories_kcal": session.get("total_calories") or c.get("55424-6"),
            "aerobic_training_effect": rnd(session.get("total_training_effect") or c.get("workout-aerobic-training-effect")),
            "anaerobic_training_effect": rnd(session.get("total_anaerobic_training_effect")),
            "training_effect_label": c.get("workout-training-effect-label"),
            "training_load": rnd(session.get("training_load_peak") or c.get("workout-training-load")),
            "avg_ground_contact_ms": rnd(session.get("avg_stance_time"), 0),
            "avg_vertical_ratio_pct": rnd(session.get("avg_vertical_ratio")),
            "avg_step_length_m": rnd((session.get("avg_step_length") or 0) / 1000, 2) or None,
            "body_battery_change": c.get("workout-body-battery-change"),
            "stamina_start_end_pct": [c.get("workout-stamina-start"), c.get("workout-stamina-end")],
            "feel_0_100": session.get("workout_feel"), "rpe_10_100": session.get("workout_rpe"),
            "sweat_loss_ml": c.get("workout-sweat-loss"),
        },
        "hr_zones": {"boundaries": zone_meta, "time": zone_rows},
        "splits_km": splits,
        "laps": laps,
        "variability": {
            "pace_cv_pct_30s": round(st.pstdev(smoothed) / st.fmean(smoothed) * 100, 1) if len(smoothed) > 30 else None,
            "max_speed_mps": rnd(max(moving), 2) if moving else None,
            "cadence_mean": rnd(mean(cadence)), "cadence_sd": rnd(st.pstdev(cadence)) if len(cadence) > 2 else None,
            "hr_first_km": splits[0]["avg_hr"] if splits else None,
            "hr_last_km": splits[-1]["avg_hr"] if splits else None,
            "hr_second_to_last_km_change": (splits[-1]["avg_hr"] - splits[1]["avg_hr"]) if len(splits) > 2 else None,
        },
        "aerobic_decoupling": decoupling,
        "readiness_that_morning": {
            "sleep_min": value(ctx.get(f"{day}/sleep")), "sleep_score": value(ctx.get(f"{day}/sleep-score")),
            "hrv_ms": value(hrv), "hrv_status": comps(hrv or {}).get("hrv-status"),
            "hrv_balanced_range_ms": [((hrv or {}).get("referenceRange") or [{}])[0].get("low", {}).get("value"),
                                      ((hrv or {}).get("referenceRange") or [{}])[0].get("high", {}).get("value")] if hrv else None,
            "resting_hr": value(ctx.get(f"{day}/resting-hr")), "body_battery_max": value(ctx.get(f"{day}/body-battery")),
        },
        "recent_same_type_last_8_weeks": {
            "count": len(history), "this_speed_per_beat": this_ef,
            "median_speed_per_beat": rnd(st.median([h["speed_per_beat"] for h in history]), 2) if history else None,
            "median_pace_min_per_km": rnd(st.median([h["pace_min_per_km"] for h in history]), 2) if history else None,
            "median_avg_hr": rnd(st.median([h["avg_hr"] for h in history]), 0) if history else None,
            "workouts": history[:12],
        },
        "data_notes": [
            "Source: local Medplum (workout Observation and the FIT file); per-second data decoded from the FIT file.",
            "Training effect, training load, stamina and Body Battery are Garmin's own scales, not clinical measures.",
            "speed_per_beat = average speed (m/s) per 1000 bpm average heart rate; higher means more speed for the same cardiac cost.",
        ],
    }
    series = {"rows": rows, "zones": zone_rows, "splits": splits, "history": history, "this_ef": this_ef, "route": route}
    return facts, series


# --------------------------------------------------------------------------
# Wellness facts
# --------------------------------------------------------------------------

DAILY = ("sleep", "sleep-score", "hrv-night", "resting-hr", "stress", "body-battery", "steps", "moderate-minutes",
         "vigorous-minutes", "calories-total", "calories-active", "spo2-avg", "respiration-avg")


def period_from_args(args) -> tuple[dt.date, dt.date, str]:
    if args.week:
        year, week = args.week.upper().split("-W")
        start = dt.date.fromisocalendar(int(year), int(week), 1)
        return start, start + dt.timedelta(days=6), f"week {args.week.upper()}", {"kind": "week", "value": args.week.upper()}
    if args.month:
        year, month = (int(x) for x in args.month.split("-"))
        start = dt.date(year, month, 1)
        nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
        return start, nxt - dt.timedelta(days=1), start.strftime("%B %Y"), {"kind": "month", "value": args.month}
    if args.start and args.end:
        start, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)
        return start, end, f"{start} to {end}", {"kind": "range", "value": f"{start}/{end}"}
    fail("wellness needs --week YYYY-Www, --month YYYY-MM, or --from and --to")


def wellness_days(start: dt.date, end: dt.date) -> list[dict]:
    days = [(start + dt.timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    obs = observations_by_key([f"{d}/{k}" for d in days for k in DAILY])
    out = []
    for d in days:
        g = lambda k: obs.get(f"{d}/{k}")
        sleep, hrv, bb, steps = g("sleep"), g("hrv-night"), g("body-battery"), g("steps")
        sc, hc, bc, stc = comps(sleep or {}), comps(hrv or {}), comps(bb or {}), comps(steps or {})
        rr = ((hrv or {}).get("referenceRange") or [{}])[0]
        out.append({
            "date": d,
            "sleep_min": value(sleep), "deep_min": sc.get("93831-6"), "light_min": sc.get("93830-8"),
            "rem_min": sc.get("93829-0"), "awake_min": sc.get("93828-2"), "sleep_need_min": sc.get("sleep-need"),
            "sleep_score": value(g("sleep-score")),
            "hrv_ms": value(hrv), "hrv_status": hc.get("hrv-status"),
            "hrv_low": (rr.get("low") or {}).get("value"), "hrv_high": (rr.get("high") or {}).get("value"),
            "resting_hr": value(g("resting-hr")), "stress_avg": value(g("stress")),
            "bb_max": value(bb), "bb_min": bc.get("body-battery-min"), "bb_charged": bc.get("body-battery-charged"),
            "bb_drained": bc.get("body-battery-drained"),
            "steps": value(steps), "step_goal": stc.get("step-goal"),
            "moderate_min": value(g("moderate-minutes")), "vigorous_min": value(g("vigorous-minutes")),
            "calories_total": value(g("calories-total")), "calories_active": value(g("calories-active")),
            "spo2_avg": value(g("spo2-avg")), "respiration_avg": value(g("respiration-avg")),
        })
    return out


# Values that keep growing during the day; today's are partial and stay out of averages.
ACCUMULATING = ("steps", "stress_avg", "bb_min", "bb_drained", "bb_charged", "moderate_min", "vigorous_min",
                "calories_total", "calories_active", "spo2_avg", "respiration_avg")


def without_partial_today(days: list[dict], today: dt.date) -> list[dict]:
    return [{**d, **{k: None for k in ACCUMULATING}} if d["date"] == today.isoformat() else d for d in days]


def account_offset() -> str:
    """The account's UTC offset, from the most recent daily Observation."""
    res = ma.graphql('{ ObservationList(code: "http://loinc.org|41950-7", _sort: "-date", _count: 1) { effectivePeriod { start } } }')
    obs = res["data"]["ObservationList"]
    return obs[0]["effectivePeriod"]["start"][19:] if obs else "+00:00"


def aggregate(days: list[dict], workouts: list[dict]) -> dict:
    col = lambda k: [d[k] for d in days if d.get(k) is not None]
    stat = lambda k: {"avg": rnd(mean(col(k))), "min": min(col(k)) if col(k) else None,
                      "max": max(col(k)) if col(k) else None, "days_with_data": len(col(k))}
    intensity = sum((d.get("moderate_min") or 0) + 2 * (d.get("vigorous_min") or 0) for d in days)
    goal_days = sum(1 for d in days if d.get("steps") is not None and d.get("step_goal") and d["steps"] >= d["step_goal"])
    return {
        "days": len(days),
        "sleep_min": stat("sleep_min"), "sleep_score": stat("sleep_score"), "deep_min": stat("deep_min"),
        "rem_min": stat("rem_min"), "hrv_ms": stat("hrv_ms"), "resting_hr": stat("resting_hr"),
        "stress_avg": stat("stress_avg"), "bb_max": stat("bb_max"), "bb_min": stat("bb_min"),
        "steps": {**stat("steps"), "total": sum(col("steps")), "days_goal_met": goal_days},
        "intensity_minutes_total": intensity,
        "spo2_avg": stat("spo2_avg"), "respiration_avg": stat("respiration_avg"),
        "hrv_status_counts": {s: sum(1 for d in days if d.get("hrv_status") == s) for s in sorted({d["hrv_status"] for d in days if d.get("hrv_status")})},
        "workouts": {"count": len(workouts), "minutes": rnd(sum(w.get("minutes") or 0 for w in workouts)),
                     "distance_km": rnd(sum((w.get("distance_m") or 0) for w in workouts) / 1000, 2)},
    }


def wellness_facts(start: dt.date, end: dt.date, label: str) -> tuple[dict, dict]:
    days = wellness_days(start, end)
    offset = account_offset()
    iso = lambda d: f"{d.isoformat()}T00:00:00{offset}"
    today = dt.date.today()
    workouts = ma.list_workouts(iso(start), iso(end + dt.timedelta(days=1)))
    length = (end - start).days + 1
    prev_start, prev_end = start - dt.timedelta(days=length), start - dt.timedelta(days=1)
    prev_days = wellness_days(prev_start, prev_end)
    prev_workouts = ma.list_workouts(iso(prev_start), iso(start))
    this = aggregate(without_partial_today(days, today), workouts)
    prev = aggregate(without_partial_today(prev_days, today), prev_workouts)
    with_data = [d["date"] for d in days if any(d.get(k) is not None for k in ("sleep_min", "steps", "resting_hr"))]
    future = [d["date"] for d in days if d["date"] > today.isoformat()]
    coverage = {"days_in_period": len(days), "days_with_data": len(with_data),
                "first_day_with_data": with_data[0] if with_data else None,
                "today_partial": today.isoformat() if start <= today <= end else None, "future_days": len(future)}
    delta = {}
    for k in ("sleep_min", "sleep_score", "hrv_ms", "resting_hr", "stress_avg", "bb_max", "steps"):
        a, b = this[k]["avg"], prev[k]["avg"]
        delta[k] = (rnd(a - b, 0 if k == "steps" else 1) if a is not None and b is not None else None)
        if k == "steps" and delta[k] is not None:
            delta[k] = int(delta[k])
    today = dt.date.today()
    facts = {
        "kind": "wellness", "label": label, "from": start.isoformat(), "to": end.isoformat(), "utc_offset": offset,
        "in_progress": end >= today, "coverage": coverage,
        "summary": this, "previous_period": {"from": prev_start.isoformat(), "to": prev_end.isoformat(), **prev},
        "change_vs_previous_avg": delta,
        "best_and_worst": {
            "best_sleep_score": max((d for d in days if d.get("sleep_score") is not None), key=lambda d: d["sleep_score"], default={}).get("date"),
            "worst_sleep_score": min((d for d in days if d.get("sleep_score") is not None), key=lambda d: d["sleep_score"], default={}).get("date"),
            "highest_stress": max((d for d in days if d.get("stress_avg") is not None), key=lambda d: d["stress_avg"], default={}).get("date"),
        },
        "days": days,
        "workouts": [{k: w[k] for k in ("activity", "start", "type", "minutes", "distance_m", "avg_hr")} for w in workouts],
        "data_notes": [
            "Source: local Medplum (daily Observations). Sleep is dated by the morning it ends.",
            "HRV is RMSSD compared with the personal baseline (hrv_low..hrv_high), not population norms.",
            "Stress, Body Battery and sleep score are Garmin's own 0-100 scales.",
            "Intensity minutes = moderate + 2 x vigorous (Garmin's rule; WHO suggests 150 per week).",
        ] + ([f"Days from {max(start, today - dt.timedelta(days=1))} on may still change (watch sync)."] if end >= today - dt.timedelta(days=1) else [])
          + ([f"The period is still in progress: {coverage['days_with_data']} of {len(days)} days have data. Today ({today}) "
              "counts for last night's sleep, HRV and resting heart rate; its steps, stress, Body Battery low, intensity "
              "minutes and calories are partial and left out of the averages."] if coverage["today_partial"] else [])
          + ([f"Data starts on {coverage['first_day_with_data']}; earlier days of the period have no data."]
             if coverage["first_day_with_data"] and coverage["first_day_with_data"] > start.isoformat() else [])
          + (["The previous period has no data, so there is no comparison."] if not prev["sleep_min"]["days_with_data"]
             and not prev["steps"]["days_with_data"] else []),
    }
    return facts, {"days": days, "workouts": workouts, "from": start, "to": end}


# --------------------------------------------------------------------------
# Language: every visible string of the PDF and the charts
# --------------------------------------------------------------------------

LABELS_EN = {
    "summary": "Summary", "details": "Details", "evaluation": "Evaluation", "recommendations": "Recommendations",
    "data_notes": "Data notes",
    "disclaimer": "This report describes patterns in wearable data. It is not medical advice; see a clinician for symptoms or concerns.",
    "footer": "garmin-archive report - generated {when}", "page": "page {n}",
    "no_data": "no data", "minutes": "minutes", "min": "min", "and": "and",
    "weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "weekdays_long": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "months": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    "months_long": ["January", "February", "March", "April", "May", "June", "July", "August", "September",
                    "October", "November", "December"],
    "long_date": "{wd} {d:02d} {mon} {y}, {hh:02d}:{mi:02d}",
    "chart_weekday": "{wd} {d:02d}", "chart_date": "{d:02d} {mon}",
    "hm": "{h}h {m:02d}m",
    "decimal_sep": ".", "thousands_sep": ",",
    # activity report
    "activity_title": "Activity report: {type}, {when}",
    "activity_subtitle": "{start} to {end} ({offset}) - Garmin activity {id}",
    "t_distance": "distance", "t_moving": "moving time", "t_pace": "average pace", "t_hr": "average / max HR",
    "t_te": "aerobic / anaerobic TE", "t_load": "training load", "t_calories": "calories",
    "t_power": "avg / normalized power",
    "c_hr": "Heart rate", "c_hr_caption": "Heart rate, every second, with your heart-rate zones",
    "c_pace": "Pace, 15 s average", "u_pace": "min/km, faster is up", "c_pace_caption": "Pace over time",
    "c_elev": "Elevation", "c_cadence": "Cadence", "u_spm": "steps/min", "c_power": "Power, 15 s average",
    "c_gct": "Ground contact time", "c_vo": "Vertical oscillation", "c_vr": "Vertical ratio", "c_step": "Step length",
    "c_split_pace": "Pace per km (min/km)", "c_split_hr": "Average heart rate per km (bpm)",
    "c_splits_caption": "Splits per kilometre",
    "c_zones": "Time in heart-rate zones (minutes)", "c_zones_caption": "Time in heart-rate zones",
    "z_below": "Below Z1", "z_above": "Above max",
    "c_eff": "Efficiency: speed per heartbeat (m/s per 1000 bpm)", "c_eff_this": "this workout",
    "c_eff_caption": "Efficiency of this workout (blue) against your workouts of the same type in the previous 8 weeks (grey)",
    "c_route": "Route shape", "c_route_caption": "Route shape (GPS)",
    "tb_splits": "Splits per kilometre", "tb_laps": "Laps (as recorded by the watch)", "tb_more": "More measures",
    "h_km": "km", "h_time": "time", "h_pace": "pace", "h_avg_hr": "avg HR", "h_max_hr": "max HR",
    "h_cadence": "cadence", "h_power": "power", "h_elev": "elev. +", "h_lap": "lap", "h_start": "start",
    "h_distance": "distance", "h_avg_power": "avg power", "h_measure": "measure", "h_value": "value",
    "m_elapsed": "elapsed time", "m_elev": "elevation gain / loss", "m_cadence": "average cadence",
    "m_dynamics": "ground contact / vertical ratio / step length", "m_benefit": "primary benefit",
    "m_decoupling": "aerobic decoupling (after 5 min)", "m_pace_cv": "pace variability (30 s, CV)",
    "m_stamina": "stamina start / end", "m_bb": "Body Battery change", "m_feel": "feel / RPE",
    "m_sweat": "sweat loss (estimate)", "m_sleep": "that morning: sleep / score", "m_hrv": "that morning: HRV / status",
    "m_rhr": "that morning: resting HR / Body Battery max",
    # wellness report
    "wellness_title": "Wellness report: {label}", "in_progress": "(in progress)",
    "p_week": "week {value}", "p_month": "{month} {year}", "p_range": "{start} to {end}",
    "wellness_subtitle": "{start} to {end} - compared with {pstart} to {pend}",
    "w_sleep": "average sleep", "w_score": "average sleep score", "w_hrv": "average overnight HRV",
    "w_rhr": "average resting HR", "w_stress": "average stress", "w_bb": "average Body Battery high",
    "w_steps": "steps per day", "w_workouts": "workouts / intensity minutes", "vs_prev": "({delta} vs previous)",
    "c_sleep": "Sleep per night (hours, by stage)", "s_deep": "Deep", "s_light": "Light", "s_rem": "REM",
    "s_awake": "Awake", "s_need": "Sleep need", "c_sleep_caption": "Sleep and its stages",
    "c_score": "Sleep score", "c_hrv": "Overnight HRV", "u_hrv": "ms, RMSSD", "balanced": "balanced range",
    "c_score_hrv_caption": "Sleep score and overnight HRV", "c_rhr": "Resting heart rate",
    "c_stress": "Average stress (0-100)", "c_rhr_stress_caption": "Resting heart rate and stress",
    "c_bb": "Body Battery, daily low to high (0-100)", "c_steps": "Steps", "goal": "Goal",
    "c_bb_steps_caption": "Body Battery and steps", "c_workout_min": "Workout minutes per day",
    "no_workouts": "no workouts recorded in this period", "c_training_caption": "Training",
    "tb_daily": "Day by day", "tb_workouts": "Workouts", "h_date": "date", "h_sleep": "sleep", "h_score": "score",
    "h_hrv": "HRV", "h_rhr": "RHR", "h_stress": "stress", "h_bb": "BB low-high", "h_steps": "steps", "h_type": "type",
    "h_minutes": "minutes", "today_partial": "today, partial",
    # Garmin values shown in the report
    "status.BALANCED": "balanced", "status.UNBALANCED": "unbalanced", "status.LOW": "low", "status.POOR": "poor",
    "status.NONE": "none",
    "type.running": "Running", "type.walking": "Walking", "type.cycling": "Cycling", "type.hiking": "Hiking",
    "type.swimming": "Swimming", "type.lap_swimming": "Pool swimming", "type.strength_training": "Strength training",
    "type.treadmill_running": "Treadmill running", "type.trail_running": "Trail running", "type.yoga": "Yoga",
    "benefit.AEROBIC_BASE": "Aerobic base", "benefit.TEMPO": "Tempo", "benefit.LACTATE_THRESHOLD": "Lactate threshold",
    "benefit.VO2MAX": "VO2 max", "benefit.ANAEROBIC_CAPACITY": "Anaerobic capacity", "benefit.SPEED": "Speed",
    "benefit.RECOVERY": "Recovery",
}

LABELS_VI = {
    "summary": "Tóm tắt", "details": "Chi tiết", "evaluation": "Đánh giá", "recommendations": "Khuyến nghị",
    "data_notes": "Ghi chú dữ liệu",
    "disclaimer": "Báo cáo này mô tả các xu hướng trong dữ liệu từ thiết bị đeo. Đây không phải lời khuyên y tế; "
                  "hãy gặp bác sĩ nếu bạn có triệu chứng hoặc lo ngại.",
    "footer": "Báo cáo garmin-archive - tạo lúc {when}", "page": "trang {n}",
    "no_data": "không có dữ liệu", "minutes": "phút", "min": "phút", "and": "và",
    "weekdays": ["T2", "T3", "T4", "T5", "T6", "T7", "CN"],
    "weekdays_long": ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"],
    "months": ["Th1", "Th2", "Th3", "Th4", "Th5", "Th6", "Th7", "Th8", "Th9", "Th10", "Th11", "Th12"],
    "months_long": ["Tháng 1", "Tháng 2", "Tháng 3", "Tháng 4", "Tháng 5", "Tháng 6", "Tháng 7", "Tháng 8",
                    "Tháng 9", "Tháng 10", "Tháng 11", "Tháng 12"],
    "long_date": "{wdl}, {d:02d}/{m:02d}/{y}, {hh:02d}:{mi:02d}",
    "chart_weekday": "{wd} {d:02d}", "chart_date": "{d:02d}/{m:02d}",
    "hm": "{h}h{m:02d}",
    "decimal_sep": ",", "thousands_sep": ".",
    "activity_title": "Báo cáo buổi tập: {type}, {when}",
    "activity_subtitle": "{start} đến {end} ({offset}) - hoạt động Garmin {id}",
    "t_distance": "quãng đường", "t_moving": "thời gian di chuyển", "t_pace": "pace trung bình",
    "t_hr": "nhịp tim TB / tối đa", "t_te": "hiệu quả tập hiếu khí / kỵ khí", "t_load": "tải tập luyện",
    "t_calories": "năng lượng tiêu hao", "t_power": "công suất TB / chuẩn hóa",
    "c_hr": "Nhịp tim", "c_hr_caption": "Nhịp tim từng giây, cùng các vùng nhịp tim của bạn",
    "c_pace": "Pace, trung bình 15 giây", "u_pace": "phút/km, nhanh hơn ở phía trên", "c_pace_caption": "Pace theo thời gian",
    "c_elev": "Độ cao", "c_cadence": "Nhịp bước", "u_spm": "bước/phút", "c_power": "Công suất, trung bình 15 giây",
    "c_gct": "Thời gian tiếp đất", "c_vo": "Dao động dọc", "c_vr": "Tỷ lệ dọc", "c_step": "Độ dài bước",
    "c_split_pace": "Pace mỗi km (phút/km)", "c_split_hr": "Nhịp tim trung bình mỗi km (bpm)",
    "c_splits_caption": "Chia theo từng kilômét",
    "c_zones": "Thời gian trong các vùng nhịp tim (phút)", "c_zones_caption": "Thời gian trong các vùng nhịp tim",
    "z_below": "Dưới Z1", "z_above": "Trên mức tối đa",
    "c_eff": "Hiệu suất: tốc độ trên mỗi nhịp tim (m/s trên 1000 bpm)", "c_eff_this": "buổi tập này",
    "c_eff_caption": "Hiệu suất của buổi tập này (xanh) so với các buổi tập cùng loại trong 8 tuần trước (xám)",
    "c_route": "Hình dạng tuyến đường", "c_route_caption": "Hình dạng tuyến đường (GPS)",
    "tb_splits": "Chia theo từng kilômét", "tb_laps": "Các vòng (theo đồng hồ ghi)", "tb_more": "Các chỉ số khác",
    "h_km": "km", "h_time": "thời gian", "h_pace": "pace", "h_avg_hr": "NT TB", "h_max_hr": "NT tối đa",
    "h_cadence": "nhịp bước", "h_power": "công suất", "h_elev": "độ cao +", "h_lap": "vòng", "h_start": "bắt đầu",
    "h_distance": "quãng đường", "h_avg_power": "công suất TB", "h_measure": "chỉ số", "h_value": "giá trị",
    "m_elapsed": "tổng thời gian", "m_elev": "độ cao lên / xuống", "m_cadence": "nhịp bước trung bình",
    "m_dynamics": "tiếp đất / tỷ lệ dọc / độ dài bước", "m_benefit": "lợi ích chính",
    "m_decoupling": "lệch nhịp hiếu khí (sau 5 phút)", "m_pace_cv": "độ dao động pace (30 giây, CV)",
    "m_stamina": "sức bền đầu / cuối", "m_bb": "thay đổi Body Battery", "m_feel": "cảm nhận / RPE",
    "m_sweat": "lượng mồ hôi mất (ước tính)", "m_sleep": "sáng hôm đó: giấc ngủ / điểm",
    "m_hrv": "sáng hôm đó: HRV / trạng thái", "m_rhr": "sáng hôm đó: nhịp tim nghỉ / Body Battery cao nhất",
    "wellness_title": "Báo cáo sức khỏe: {label}", "in_progress": "(đang diễn ra)",
    "p_week": "tuần {value}", "p_month": "{month} năm {year}", "p_range": "{start} đến {end}",
    "wellness_subtitle": "{start} đến {end} - so với {pstart} đến {pend}",
    "w_sleep": "giấc ngủ trung bình", "w_score": "điểm giấc ngủ trung bình", "w_hrv": "HRV ban đêm trung bình",
    "w_rhr": "nhịp tim nghỉ trung bình", "w_stress": "mức căng thẳng trung bình",
    "w_bb": "Body Battery cao nhất trung bình", "w_steps": "số bước mỗi ngày",
    "w_workouts": "buổi tập / phút cường độ", "vs_prev": "({delta} so với kỳ trước)",
    "c_sleep": "Giấc ngủ mỗi đêm (giờ, theo giai đoạn)", "s_deep": "Ngủ sâu", "s_light": "Ngủ nông", "s_rem": "REM",
    "s_awake": "Thức giấc", "s_need": "Nhu cầu ngủ", "c_sleep_caption": "Giấc ngủ và các giai đoạn",
    "c_score": "Điểm giấc ngủ", "c_hrv": "HRV ban đêm", "u_hrv": "ms, RMSSD", "balanced": "khoảng cân bằng",
    "c_score_hrv_caption": "Điểm giấc ngủ và HRV ban đêm", "c_rhr": "Nhịp tim nghỉ",
    "c_stress": "Mức căng thẳng trung bình (0-100)", "c_rhr_stress_caption": "Nhịp tim nghỉ và mức căng thẳng",
    "c_bb": "Body Battery thấp-cao mỗi ngày (0-100)", "c_steps": "Số bước", "goal": "Mục tiêu",
    "c_bb_steps_caption": "Body Battery và số bước", "c_workout_min": "Số phút tập luyện mỗi ngày",
    "no_workouts": "không có buổi tập nào trong giai đoạn này", "c_training_caption": "Tập luyện",
    "tb_daily": "Từng ngày", "tb_workouts": "Các buổi tập", "h_date": "ngày", "h_sleep": "giấc ngủ", "h_score": "điểm",
    "h_hrv": "HRV", "h_rhr": "NT nghỉ", "h_stress": "căng thẳng", "h_bb": "BB thấp-cao", "h_steps": "số bước",
    "h_type": "loại", "h_minutes": "phút", "today_partial": "hôm nay, chưa đủ ngày",
    "status.BALANCED": "cân bằng", "status.UNBALANCED": "mất cân bằng", "status.LOW": "thấp", "status.POOR": "kém",
    "status.NONE": "không có",
    "type.running": "Chạy bộ", "type.walking": "Đi bộ", "type.cycling": "Đạp xe", "type.hiking": "Leo núi",
    "type.swimming": "Bơi", "type.lap_swimming": "Bơi hồ", "type.strength_training": "Tập sức mạnh",
    "type.treadmill_running": "Chạy máy", "type.trail_running": "Chạy địa hình", "type.yoga": "Yoga",
    "benefit.AEROBIC_BASE": "Nền tảng hiếu khí", "benefit.TEMPO": "Tempo", "benefit.LACTATE_THRESHOLD": "Ngưỡng lactate",
    "benefit.VO2MAX": "VO2 max", "benefit.ANAEROBIC_CAPACITY": "Năng lực kỵ khí", "benefit.SPEED": "Tốc độ",
    "benefit.RECOVERY": "Phục hồi",
}

BUILT_IN_LABELS = {"en": LABELS_EN, "vi": LABELS_VI}


class Labels:
    """Label lookup for one report: English, then the built-in language, then the narrative's own labels."""

    def __init__(self, language: str = "en", extra: dict | None = None):
        self.language = (language or "en").lower()
        self.table = {**LABELS_EN, **BUILT_IN_LABELS.get(self.language.split("-")[0], {}), **(extra or {})}
        self.fallback = [] if self.language.startswith("en") else sorted(
            k for k in LABELS_EN if k not in BUILT_IN_LABELS.get(self.language.split("-")[0], {}) and k not in (extra or {}))

    def __call__(self, key: str, **kw) -> str:
        text = self.table[key]
        return text.format(**kw) if kw else text

    def value(self, prefix: str, raw) -> str:
        """Translate a Garmin value (status, activity type, benefit) or prettify it."""
        if raw is None:
            return "-"
        return self.table.get(f"{prefix}.{raw}") or str(raw).replace("_", " ").capitalize()

    def date(self, d: dt.date | dt.datetime, template: str = "chart_date") -> str:
        hh = getattr(d, "hour", 0)
        mi = getattr(d, "minute", 0)
        return self.table[template].format(wd=self.table["weekdays"][d.weekday()], wdl=self.table["weekdays_long"][d.weekday()],
                                           d=d.day, m=d.month, mon=self.table["months"][d.month - 1], y=d.year, hh=hh, mi=mi)

    def n(self, value, unit: str = "") -> str:
        """A number with the language's separators ('-' when missing)."""
        if value is None:
            return "-"
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{value}{unit}"
        text = f"{value:,}" if isinstance(value, int) or not float(value).is_integer() else f"{int(value):,}"
        dec, thou = self.table.get("decimal_sep", "."), self.table.get("thousands_sep", ",")
        text = text.replace(",", "\0").replace(".", dec).replace("\0", thou)
        return f"{text}{unit}"

    def hm(self, minutes) -> str:
        return "-" if minutes is None else self.table["hm"].format(h=int(minutes // 60), m=int(minutes % 60))

    def all_text(self) -> str:
        return " ".join(" ".join(v) if isinstance(v, list) else str(v) for v in self.table.values())


# --------------------------------------------------------------------------
# Fonts: a TrueType font that covers the report's language, for PDF and charts
# --------------------------------------------------------------------------


def font_files(regular: str | None, bold: str | None) -> tuple[Path, Path]:
    """--font/--font-bold, else GARMIN_REPORT_FONT(_BOLD), else DejaVu Sans shipped with matplotlib
    (Latin incl. Vietnamese, Greek, Cyrillic). For Chinese, Japanese, Korean, Thai, Arabic ... pass a
    TrueType (.ttf) font that covers the script, e.g. a Noto Sans *.ttf."""
    regular = regular or os.environ.get("GARMIN_REPORT_FONT")
    bold = bold or os.environ.get("GARMIN_REPORT_FONT_BOLD")
    if not regular:
        try:
            import matplotlib
        except ImportError:
            fail(DEPS_HINT)
        ttf = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
        regular, bold = str(ttf / "DejaVuSans.ttf"), bold or str(ttf / "DejaVuSans-Bold.ttf")
    regular_path, bold_path = Path(regular).expanduser(), Path(bold or regular).expanduser()
    for p in (regular_path, bold_path):
        if not p.is_file():
            fail(f"font file not found: {p}")
        if p.suffix.lower() not in (".ttf", ".ttc"):
            fail(f"{p} is not a TrueType font (.ttf); reportlab cannot embed OpenType/CFF fonts")
    return regular_path, bold_path


def check_font_covers(font: Path, text: str) -> None:
    """Fail with the exact characters a font lacks, instead of printing boxes in the PDF."""
    try:
        from reportlab.pdfbase.ttfonts import TTFont
    except ImportError:
        fail(DEPS_HINT)
    covered = set(TTFont("probe", str(font)).face.charToGlyph)
    missing = sorted({c for c in text if ord(c) > 32 and ord(c) not in covered})
    if missing:
        fail(f"the font {font.name} has no glyphs for {''.join(missing[:40])!r}"
             f"{' ...' if len(missing) > 40 else ''}; pass a TrueType font that covers the language with "
             "--font FILE.ttf (and --font-bold), or set GARMIN_REPORT_FONT")


# --------------------------------------------------------------------------
# Charts (matplotlib)
# --------------------------------------------------------------------------


def _plt(fonts: tuple[Path, Path]):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
    except ImportError:
        fail(DEPS_HINT)
    for f in fonts:
        font_manager.fontManager.addfont(str(f))
    family = font_manager.FontProperties(fname=str(fonts[0])).get_name()
    plt.rcParams.update({
        "font.family": family, "font.size": 8, "axes.edgecolor": GRID, "axes.labelcolor": INK2,
        "axes.titlesize": 9, "axes.titleweight": "bold", "axes.titlecolor": INK, "axes.titlelocation": "left",
        "xtick.color": INK2, "ytick.color": INK2, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
        "axes.spines.top": False, "axes.spines.right": False, "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
        "lines.linewidth": 1.4, "savefig.dpi": 200,
    })
    return plt


def _save(fig, path: Path) -> Path:
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight", facecolor=SURFACE)
    import matplotlib.pyplot as plt
    plt.close(fig)
    return path


def _title(ax, text):
    import textwrap
    fig_w = ax.figure.get_size_inches()[0] * ax.get_position().width
    ax.set_title(textwrap.fill(text, width=max(24, int(fig_w * 11))))


def _line(ax, x, y, title, unit, L, color=SERIES[0]):
    pts = [(a, b) for a, b in zip(x, y) if b is not None]
    if not pts:
        ax.text(0.5, 0.5, L("no_data"), ha="center", va="center", color=MUTED, transform=ax.transAxes)
    else:
        ax.plot([p[0] for p in pts], [p[1] for p in pts], color=color, solid_capstyle="round")
    _title(ax, f"{title} ({unit})")


def _date_axis(ax, L, template, maxticks=None):
    import matplotlib.dates as mdates
    from matplotlib.ticker import FuncFormatter
    if maxticks:
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=maxticks))
    else:
        ax.xaxis.set_major_locator(mdates.DayLocator())
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: L.date(mdates.num2date(v), template)))
    ax.tick_params(axis="x", labelsize=7)


def activity_charts(series: dict, workdir: Path, L: Labels, fonts) -> list[tuple[str, Path]]:
    plt = _plt(fonts)
    rows = series["rows"]
    t = [r["elapsed_s"] / 60 for r in rows]
    smooth = lambda k, w=15: rolling([r.get(k) for r in rows], w)
    zone_name = lambda z: L("z_below") if z == "Below Z1" else L("z_above") if z == "Above max" else z
    charts = []

    fig, ax = plt.subplots(figsize=(7.2, 2.4))
    zones = [z for z in series["zones"] if z.get("from_bpm") and z.get("to_bpm")]
    _line(ax, t, [r.get("heart_rate") for r in rows], L("c_hr"), "bpm", L)
    hrs = [r["heart_rate"] for r in rows if r.get("heart_rate")]
    lo, hi = (min(hrs) - 5, max(hrs) + 5) if hrs else (40, 200)
    for i, z in enumerate(zones):
        ax.axhspan(z["from_bpm"], z["to_bpm"], color=RAMP[min(i, len(RAMP) - 1)], alpha=0.10, lw=0)
        mid = (max(z["from_bpm"], lo) + min(z["to_bpm"], hi)) / 2
        if z["to_bpm"] > lo and z["from_bpm"] < hi:          # label only zones that are visible
            ax.text(1.005, mid, zone_name(z["zone"]), transform=ax.get_yaxis_transform(), va="center", fontsize=7, color=INK2)
    ax.set_ylim(lo, hi)
    ax.set_xlabel(L("minutes"))
    charts.append((L("c_hr_caption"), _save(fig, workdir / "hr.png")))

    paces = [p for p in smooth("pace_min_per_km") if p]
    if paces:
        srt = sorted(paces)
        lo, hi = srt[int(len(srt) * 0.05)], srt[int(len(srt) * 0.90) - 1]
        fig, ax = plt.subplots(figsize=(7.2, 2.2))
        _line(ax, t, smooth("pace_min_per_km"), L("c_pace"), L("u_pace"), L)
        ax.set_ylim(hi + 0.6, lo - 0.4)                       # inverted: faster is up; slower stretches leave the frame
        ax.set_xlabel(L("minutes"))
        charts.append((L("c_pace_caption"), _save(fig, workdir / "pace.png")))

    present = lambda k: any(r.get(k) is not None for r in rows)
    panels = [(k, ttl, unit) for k, ttl, unit in (
        ("altitude_m", L("c_elev"), "m"), ("cadence_spm", L("c_cadence"), L("u_spm")), ("cadence_rpm", L("c_cadence"), "rpm"),
        ("power_w", L("c_power"), "W"), ("ground_contact_ms", L("c_gct"), "ms"),
        ("vertical_oscillation_mm", L("c_vo"), "mm"), ("vertical_ratio_pct", L("c_vr"), "%"),
        ("step_length_mm", L("c_step"), "mm")) if present(k)]
    for i in range(0, len(panels), 2):
        pair = panels[i:i + 2]
        fig, axes = plt.subplots(1, len(pair), figsize=(7.2, 2.0), squeeze=False)
        for ax, (k, ttl, unit) in zip(axes[0], pair):
            ys = [r.get(k) for r in rows] if k == "altitude_m" else smooth(k)
            _line(ax, t, ys, ttl, unit, L)
            ax.set_xlabel(L("minutes"))
        charts.append((f" {L('and')} ".join(p[1] for p in pair), _save(fig, workdir / f"panel{i}.png")))

    splits = series["splits"]
    if splits:
        fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.2))
        kms = [str(s["km"]) for s in splits]
        axes[0].bar(kms, [s["pace_min_per_km"] for s in splits], color=SERIES[0], width=0.6, edgecolor=SURFACE, linewidth=1.5)
        _title(axes[0], L("c_split_pace"))
        vals = [s["pace_min_per_km"] for s in splits if s["pace_min_per_km"]]
        axes[0].set_ylim(min(vals) * 0.9, max(vals) * 1.05)
        axes[1].bar(kms, [s["avg_hr"] for s in splits], color=SERIES[0], width=0.6, edgecolor=SURFACE, linewidth=1.5)
        _title(axes[1], L("c_split_hr"))
        hv = [s["avg_hr"] for s in splits if s["avg_hr"]]
        axes[1].set_ylim(min(hv) - 15, max(hv) + 5)
        for ax in axes:
            ax.set_xlabel("km")
        charts.append((L("c_splits_caption"), _save(fig, workdir / "splits.png")))

    zones_t = [z for z in series["zones"] if z["time_s"] > 0 or z["zone"] not in ("Below Z1", "Above max")]
    if zones_t:
        fig, ax = plt.subplots(figsize=(7.2, 1.9))
        labels = [f"{zone_name(z['zone'])} ({z['from_bpm'] or 0}-{z['to_bpm']})" if z.get("to_bpm") else zone_name(z["zone"])
                  for z in zones_t]
        colors = ([MUTED] if zones_t[0]["zone"] == "Below Z1" else []) + RAMP + [INK]
        ax.barh(labels, [z["time_s"] / 60 for z in zones_t], color=colors[:len(zones_t)], height=0.6)
        for i, z in enumerate(zones_t):
            ax.text(z["time_s"] / 60, i, f"  {z['time_s'] / 60:.1f} {L('min')} ({z['share_pct']}%)", va="center",
                    fontsize=7, color=INK2)
        ax.invert_yaxis()
        _title(ax, L("c_zones"))
        ax.grid(axis="y", visible=False)
        charts.append((L("c_zones_caption"), _save(fig, workdir / "zones.png")))

    hist = series["history"]
    if hist and series["this_ef"]:
        fig, ax = plt.subplots(figsize=(7.2, 1.9))
        xs = [dt.date.fromisoformat(h["date"]) for h in hist]
        ax.plot(xs, [h["speed_per_beat"] for h in hist], "o", color=MUTED, markersize=4)
        this_day = dt.date.fromisoformat(series["rows"][0]["time"][:10]) if series["rows"] else None
        ax.plot([this_day], [series["this_ef"]], "o", color=SERIES[0], markersize=7)
        ax.annotate(L("c_eff_this"), (this_day, series["this_ef"]), xytext=(-8, 8), textcoords="offset points",
                    ha="right", fontsize=7, color=INK)
        _date_axis(ax, L, "chart_date", maxticks=6)
        _title(ax, L("c_eff"))
        charts.append((L("c_eff_caption"), _save(fig, workdir / "trend.png")))

    if series["route"] and any("lat" in r for r in rows):
        pts = [(r["lon"], r["lat"]) for r in rows if r.get("lat") is not None]
        fig, ax = plt.subplots(figsize=(4.0, 4.0))
        ax.plot([p[0] for p in pts], [p[1] for p in pts], color=SERIES[0])
        ax.set_aspect(1 / math.cos(math.radians(pts[0][1])))
        ax.set_xticks([]), ax.set_yticks([])
        _title(ax, L("c_route"))
        charts.append((L("c_route_caption"), _save(fig, workdir / "route.png")))
    return charts


def wellness_charts(series: dict, workdir: Path, L: Labels, fonts) -> list[tuple[str, Path]]:
    plt = _plt(fonts)
    days = series["days"]
    xs = [dt.date.fromisoformat(d["date"]) for d in days]
    many = len(days) > 10
    period = (series["from"] - dt.timedelta(days=0.6), series["to"] + dt.timedelta(days=0.6))

    def fmt(ax):
        ax.set_xlim(*period)
        if many:                                   # month: a handful of dates, sized to the panel width
            _date_axis(ax, L, "chart_date", maxticks=8 if ax.get_position().width > 0.6 else 4)
        else:
            _date_axis(ax, L, "chart_weekday")
    charts = []
    width = 0.7

    fig, ax = plt.subplots(figsize=(7.2, 2.4))
    bottom = [0.0] * len(days)
    for (key, label), color in zip((("deep_min", L("s_deep")), ("light_min", L("s_light")), ("rem_min", L("s_rem")),
                                    ("awake_min", L("s_awake"))), SERIES):
        vals = [(d.get(key) or 0) / 60 for d in days]
        ax.bar(xs, vals, bottom=bottom, color=color, width=width, label=label, edgecolor=SURFACE, linewidth=1.2)
        bottom = [b + v for b, v in zip(bottom, vals)]
    need = [(d.get("sleep_need_min") or 0) / 60 or None for d in days]
    if any(need):
        ax.plot(xs, need, "_", color=INK, markersize=12, markeredgewidth=1.5, label=L("s_need"))
    _title(ax, L("c_sleep"))
    ax.legend(frameon=False, fontsize=7, ncol=5, loc="upper left", bbox_to_anchor=(0, -0.18))
    fmt(ax)
    charts.append((L("c_sleep_caption"), _save(fig, workdir / "sleep.png")))

    fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.1))
    _line(axes[0], xs, [d.get("sleep_score") for d in days], L("c_score"), "0-100", L)
    axes[0].plot([x for x, d in zip(xs, days) if d.get("sleep_score") is not None],
                 [d["sleep_score"] for d in days if d.get("sleep_score") is not None], "o", color=SERIES[0], markersize=3)
    lows = [d.get("hrv_low") for d in days]
    highs = [d.get("hrv_high") for d in days]
    if any(lows) and any(highs):
        axes[1].fill_between(xs, [l or float("nan") for l in lows], [h or float("nan") for h in highs], color=SERIES[0],
                             alpha=0.12, lw=0, label=L("balanced"))
    _line(axes[1], xs, [d.get("hrv_ms") for d in days], L("c_hrv"), L("u_hrv"), L)
    axes[1].plot([x for x, d in zip(xs, days) if d.get("hrv_ms") is not None],
                 [d["hrv_ms"] for d in days if d.get("hrv_ms") is not None], "o", color=SERIES[0], markersize=3)
    if any(lows):
        axes[1].legend(frameon=False, fontsize=7, loc="lower left")
    for ax in axes:
        fmt(ax)
    charts.append((L("c_score_hrv_caption"), _save(fig, workdir / "score_hrv.png")))

    fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.1))
    _line(axes[0], xs, [d.get("resting_hr") for d in days], L("c_rhr"), "bpm", L)
    axes[0].plot([x for x, d in zip(xs, days) if d.get("resting_hr")], [d["resting_hr"] for d in days if d.get("resting_hr")],
                 "o", color=SERIES[0], markersize=3)
    axes[1].bar(xs, [d.get("stress_avg") or 0 for d in days], color=SERIES[0], width=width, edgecolor=SURFACE, linewidth=1.2)
    _title(axes[1], L("c_stress"))
    for ax in axes:
        fmt(ax)
    charts.append((L("c_rhr_stress_caption"), _save(fig, workdir / "rhr_stress.png")))

    fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.1))
    lo = [d.get("bb_min") for d in days]
    hi = [d.get("bb_max") for d in days]
    axes[0].bar(xs, [(h or 0) - (l or 0) for h, l in zip(hi, lo)], bottom=[l or 0 for l in lo], color=SERIES[0],
                width=width, edgecolor=SURFACE, linewidth=1.2)
    axes[0].set_ylim(0, 100)
    _title(axes[0], L("c_bb"))
    axes[1].bar(xs, [d.get("steps") or 0 for d in days], color=SERIES[0], width=width, edgecolor=SURFACE, linewidth=1.2,
                label=L("c_steps"))
    goals = [d.get("step_goal") for d in days]
    if any(goals):
        axes[1].plot(xs, [g or float("nan") for g in goals], "_", color=INK, markersize=10, markeredgewidth=1.5, label=L("goal"))
        axes[1].legend(frameon=False, fontsize=7, ncol=2, loc="upper left", bbox_to_anchor=(0, -0.22))
    _title(axes[1], L("c_steps"))
    for ax in axes:
        fmt(ax)
    charts.append((L("c_bb_steps_caption"), _save(fig, workdir / "bb_steps.png")))

    per_day = {d["date"]: 0.0 for d in days}
    for w in series["workouts"]:
        if w["start"][:10] in per_day:
            per_day[w["start"][:10]] += w.get("minutes") or 0
    fig, ax = plt.subplots(figsize=(7.2, 1.8))
    ax.bar(xs, list(per_day.values()), color=SERIES[0], width=width, edgecolor=SURFACE, linewidth=1.2)
    _title(ax, L("c_workout_min"))
    if not any(per_day.values()):
        ax.set_ylim(0, 60)
        ax.text(0.5, 0.5, L("no_workouts"), ha="center", va="center", color=MUTED, transform=ax.transAxes)
    fmt(ax)
    charts.append((L("c_training_caption"), _save(fig, workdir / "training.png")))
    return charts


# --------------------------------------------------------------------------
# PDF (reportlab)
# --------------------------------------------------------------------------


def load_narrative(path: str) -> dict:
    try:
        n = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        fail(f"cannot read the narrative {path}: {exc}")
    missing = [k for k in ("headline", "summary", "evaluation", "recommendations") if not n.get(k)]
    if missing:
        fail(f"the narrative lacks {missing}; write it from the facts first (see the module docstring)")
    for k in ("evaluation", "recommendations"):
        if not isinstance(n[k], list):
            fail(f"narrative.{k} must be a list of strings")
    if "data_notes" in n and not isinstance(n["data_notes"], list):
        fail("narrative.data_notes must be a list of strings")
    if "labels" in n and not isinstance(n["labels"], dict):
        fail("narrative.labels must be an object of label key -> text (see: garmin_report.py labels)")
    return n


def build_pdf(out: Path, title: str, subtitle: str, tiles: list[tuple[str, str]], narrative: dict,
              charts: list[tuple[str, Path]], tables: list[tuple[str, list[list]]], notes: list[str],
              L: Labels, fonts: tuple[Path, Path]) -> None:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import (Image, KeepTogether, ListFlowable, ListItem, Paragraph, SimpleDocTemplate,
                                        Spacer, Table, TableStyle)
    except ImportError:
        fail(DEPS_HINT)
    from xml.sax.saxutils import escape

    pdfmetrics.registerFont(TTFont("Body", str(fonts[0])))
    pdfmetrics.registerFont(TTFont("Body-Bold", str(fonts[1])))
    pdfmetrics.registerFontFamily("Body", normal="Body", bold="Body-Bold", italic="Body", boldItalic="Body-Bold")
    regular, bold = "Body", "Body-Bold"

    ss = getSampleStyleSheet()
    base = ParagraphStyle("base", parent=ss["Normal"], fontName=regular, fontSize=9.5, leading=13.5, textColor=colors.HexColor(INK))
    h1 = ParagraphStyle("h1", parent=base, fontName=bold, fontSize=18, leading=23, spaceAfter=2)
    sub = ParagraphStyle("sub", parent=base, fontSize=9, textColor=colors.HexColor(INK2), spaceAfter=10)
    h2 = ParagraphStyle("h2", parent=base, fontName=bold, fontSize=12, leading=16, spaceBefore=12, spaceAfter=5)
    lead = ParagraphStyle("lead", parent=base, fontName=bold, fontSize=10.5, leading=14.5, spaceAfter=4)
    cap = ParagraphStyle("cap", parent=base, fontSize=8, textColor=colors.HexColor(INK2), spaceAfter=8)
    small = ParagraphStyle("small", parent=base, fontSize=7.5, leading=10, textColor=colors.HexColor(INK2))
    small_b = ParagraphStyle("small_b", parent=small, fontName=bold, textColor=colors.HexColor(INK))
    tile_v = ParagraphStyle("tv", parent=base, fontName=bold, fontSize=13, leading=16)
    tile_l = ParagraphStyle("tl", parent=base, fontSize=7.5, leading=9.5, textColor=colors.HexColor(INK2))
    p = lambda text, style=base: Paragraph(escape(str(text)), style)

    story = [p(title, h1), p(subtitle, sub)]
    cells = [[[p(v, tile_v), p(l, tile_l)] for l, v in tiles[i:i + 4]] for i in range(0, len(tiles), 4)]
    for row in cells:
        while len(row) < 4:
            row.append("")
    tt = Table(cells, colWidths=[43 * mm] * 4)
    tt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f4f3f0")),
                            ("LINEBELOW", (0, 0), (-1, -1), 3, colors.white), ("LINEAFTER", (0, 0), (-1, -1), 3, colors.white),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 6),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("LEFTPADDING", (0, 0), (-1, -1), 7)]))
    story += [tt, Spacer(1, 6), p(L("summary"), h2), p(narrative["headline"], lead), p(narrative["summary"])]

    story.append(p(L("details"), h2))
    from reportlab.lib.utils import ImageReader
    for caption, path in charts:
        w, h = ImageReader(str(path)).getSize()
        width = 174 * mm if w / h > 1.5 else 90 * mm
        story.append(KeepTogether([Image(str(path), width=width, height=width * h / w), p(caption, cap)]))

    for caption, rows in tables:
        if len(rows) < 2:
            continue
        data = [[p(c, small_b) for c in rows[0]]] + [[p(c, small) for c in r] for r in rows[1:]]
        t = Table(data, repeatRows=1, hAlign="LEFT")
        t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor(INK2)),
                               ("LINEBELOW", (0, 1), (-1, -1), 0.3, colors.HexColor(GRID)), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        story += [KeepTogether([p(caption, h2), t])]

    bullet = lambda items: ListFlowable([ListItem(p(i), leftIndent=10, value="•") for i in items],
                                        bulletType="bullet", start="•", leftIndent=12, bulletFontSize=8,
                                        bulletFontName=regular)
    story += [p(L("evaluation"), h2), bullet(narrative["evaluation"]),
              p(L("recommendations"), h2), bullet(narrative["recommendations"]),
              p(L("data_notes"), h2)] + [p(n, small) for n in notes + [L("disclaimer")]]

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(regular, 7)
        canvas.setFillColor(colors.HexColor(MUTED))
        canvas.drawString(18 * mm, 10 * mm, L("footer", when=dt.datetime.now().strftime("%Y-%m-%d %H:%M")))
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, L("page", n=doc.page))
        canvas.restoreState()

    out.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(out), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=16 * mm, title=title, author="garmin-archive", subject=subtitle,
                            lang=L.language)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    os.chmod(out, 0o600)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def prepare(narrative: dict, font: str | None, font_bold: str | None) -> tuple[Labels, tuple[Path, Path], list[str]]:
    L = Labels(narrative.get("language", "en"), narrative.get("labels"))
    if L.fallback:
        print(f"garmin_report: no built-in labels for language {L.language!r}; {len(L.fallback)} label(s) stay in English "
              f"(e.g. {', '.join(L.fallback[:5])}). Translate them into narrative.labels (see: garmin_report.py labels).",
              file=sys.stderr)
    fonts = font_files(font, font_bold)
    text = " ".join([narrative["headline"], narrative["summary"], *narrative["evaluation"], *narrative["recommendations"],
                     *narrative.get("data_notes", []), L.all_text()])
    for f in set(fonts):
        check_font_covers(f, text)
    return L, fonts, narrative.get("data_notes") or []


def render_activity(facts: dict, series: dict, narrative: dict, out: Path, font=None, font_bold=None) -> None:
    import tempfile
    L, fonts, notes = prepare(narrative, font, font_bold)
    s = facts["summary"]
    dash = L.n
    with tempfile.TemporaryDirectory() as tmp:
        charts = activity_charts(series, Path(tmp), L, fonts)
        tiles = [(L("t_distance"), dash(s["distance_km"], " km")), (L("t_moving"), s["moving"]), (L("t_pace"), s["avg_pace"] or "-"),
                 (L("t_hr"), f"{dash(s['avg_hr'])} / {dash(s['max_hr'])} bpm"),
                 (L("t_te"), f"{dash(s['aerobic_training_effect'])} / {dash(s['anaerobic_training_effect'])}"),
                 (L("t_load"), dash(s["training_load"])), (L("t_calories"), dash(s["calories_kcal"], " kcal")),
                 (L("t_power"), f"{dash(s['avg_power_w'])} / {dash(s['normalized_power_w'])} W" if s.get("avg_power_w") else "-")]
        splits = [[L("h_km"), L("h_time"), L("h_pace"), L("h_avg_hr"), L("h_max_hr"), L("h_cadence"), L("h_power"), L("h_elev")]] + [
            [x["km"], dur_text(x["time_s"]), pace_text(x["pace_min_per_km"]), dash(x["avg_hr"]), dash(x["max_hr"]),
             dash(x["avg_cadence"]), dash(x["avg_power_w"]), dash(x["elevation_gain_m"])] for x in facts["splits_km"]]
        laps = [[L("h_lap"), L("h_start"), L("h_distance"), L("h_time"), L("h_avg_hr"), L("h_max_hr"), L("h_avg_power")]] + [
            [l["lap"], (l.get("start") or "")[11:19], dash(round((l.get("total_distance") or 0) / 1000, 2), " km"),
             dur_text(l.get("total_timer_time")), dash(l.get("avg_heart_rate")), dash(l.get("max_heart_rate")),
             dash(l.get("avg_power"))] for l in facts["laps"]]
        r = facts["readiness_that_morning"]
        dec = facts["aerobic_decoupling"] or {}
        details = [[L("h_measure"), L("h_value")],
                   [L("m_elapsed"), s["elapsed"]], [L("m_elev"), f"{dash(s['elevation_gain_m'])} / {dash(s['elevation_loss_m'])} m"],
                   [L("m_cadence"), dash(s.get("avg_cadence_spm"), " " + L("u_spm"))],
                   [L("m_dynamics"), f"{dash(s['avg_ground_contact_ms'], ' ms')} / {dash(s['avg_vertical_ratio_pct'], ' %')} / {dash(s['avg_step_length_m'], ' m')}"],
                   [L("m_benefit"), L.value("benefit", s.get("training_effect_label"))],
                   [L("m_decoupling"), dash(dec.get("decoupling_pct"), " %")],
                   [L("m_pace_cv"), dash(facts["variability"]["pace_cv_pct_30s"], " %")],
                   [L("m_stamina"), " / ".join(dash(v, "%") for v in s["stamina_start_end_pct"] if v is not None) or "-"],
                   [L("m_bb"), dash(s.get("body_battery_change"))],
                   [L("m_feel"), f"{dash(s.get('feel_0_100'))} / {dash(s.get('rpe_10_100'))}"],
                   [L("m_sweat"), dash(s.get("sweat_loss_ml"), " ml")],
                   [L("m_sleep"), f"{L.hm(r['sleep_min'])} / {dash(r['sleep_score'])}"],
                   [L("m_hrv"), f"{dash(r['hrv_ms'], ' ms')} / {L.value('status', r['hrv_status'])}"],
                   [L("m_rhr"), f"{dash(r['resting_hr'], ' bpm')} / {dash(r['body_battery_max'])}"]]
        start = dt.datetime.fromisoformat(facts["start"])
        build_pdf(out, L("activity_title", type=L.value("type", facts["type"]), when=L.date(start, "long_date")),
                  L("activity_subtitle", start=facts["start"][:16].replace("T", " "), end=facts["end"][11:16],
                    offset=facts["start"][19:], id=facts["activity"]),
                  tiles, narrative, charts,
                  [(L("tb_splits"), splits), (L("tb_laps"), laps), (L("tb_more"), details)],
                  notes or facts["data_notes"], L, fonts)


def period_label(facts: dict, L: Labels) -> str:
    kind, value = (facts.get("period") or {}).get("kind"), (facts.get("period") or {}).get("value")
    if kind == "week":
        return L("p_week", value=value)
    if kind == "month":
        year, month = (int(x) for x in value.split("-"))
        return L("p_month", month=L.table["months_long"][month - 1], year=year)
    return L("p_range", start=facts["from"], end=facts["to"])


def render_wellness(facts: dict, series: dict, narrative: dict, out: Path, font=None, font_bold=None) -> None:
    import tempfile
    L, fonts, notes = prepare(narrative, font, font_bold)
    s, d = facts["summary"], facts["change_vs_previous_avg"]
    sign = lambda v, unit="": "" if v is None else " " + L("vs_prev", delta=("+" if v >= 0 else "") + L.n(v, unit))
    dash = L.n
    with tempfile.TemporaryDirectory() as tmp:
        charts = wellness_charts(series, Path(tmp), L, fonts)
        tiles = [(L("w_sleep") + sign(d["sleep_min"], " min"), L.hm(s["sleep_min"]["avg"])),
                 (L("w_score") + sign(d["sleep_score"]), dash(s["sleep_score"]["avg"])),
                 (L("w_hrv") + sign(d["hrv_ms"], " ms"), dash(s["hrv_ms"]["avg"], " ms")),
                 (L("w_rhr") + sign(d["resting_hr"], " bpm"), dash(s["resting_hr"]["avg"], " bpm")),
                 (L("w_stress") + sign(d["stress_avg"]), dash(s["stress_avg"]["avg"])),
                 (L("w_bb") + sign(d["bb_max"]), dash(s["bb_max"]["avg"])),
                 (L("w_steps") + sign(d["steps"]), "-" if s["steps"]["avg"] is None else L.n(round(s["steps"]["avg"]))),
                 (L("w_workouts"), f"{s['workouts']['count']} / {L.n(s['intensity_minutes_total'])}")]
        today = (facts.get("coverage") or {}).get("today_partial")
        daily = [[L("h_date"), L("h_sleep"), L("h_score"), L("h_hrv"), L("h_rhr"), L("h_stress"), L("h_bb"), L("h_steps")]] + [
            [L.date(dt.date.fromisoformat(x["date"]), "chart_date") + (f" ({L('today_partial')})" if x["date"] == today else ""),
             L.hm(x["sleep_min"]), dash(x["sleep_score"]),
             f"{L.n(x['hrv_ms'])} ({L.value('status', x['hrv_status'])})" if x["hrv_ms"] else "-",
             dash(x["resting_hr"]), dash(x["stress_avg"]),
             f"{dash(x['bb_min'])}-{x['bb_max']}" if x["bb_max"] is not None else "-",
             L.n(x["steps"])]
            for x in facts["days"] if not today or x["date"] <= today]
        workouts = [[L("h_date"), L("h_type"), L("h_minutes"), L("h_distance"), L("h_avg_hr")]] + [
            [w["start"][:16].replace("T", " "), L.value("type", w["type"]), dash(w["minutes"]),
             L.n(round((w["distance_m"] or 0) / 1000, 2), " km"), dash(w["avg_hr"])] for w in facts["workouts"]]
        build_pdf(out, L("wellness_title", label=period_label(facts, L)) + (f" {L('in_progress')}" if facts.get("in_progress") else ""),
                  L("wellness_subtitle", start=facts["from"], end=facts["to"], pstart=facts["previous_period"]["from"],
                    pend=facts["previous_period"]["to"]),
                  tiles, narrative, charts, [(L("tb_daily"), daily), (L("tb_workouts"), workouts)],
                  notes or facts["data_notes"], L, fonts)


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="garmin_report.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="kind", required=True)
    a = sub.add_parser("activity")
    a.add_argument("activity")
    a.add_argument("--route", action="store_true", help="include the GPS route shape")
    w = sub.add_parser("wellness")
    w.add_argument("--week")
    w.add_argument("--month")
    w.add_argument("--from", dest="start")
    w.add_argument("--to", dest="end")
    for s in (a, w):
        g = s.add_mutually_exclusive_group(required=True)
        g.add_argument("--facts", metavar="FILE", help="write the computed facts as JSON (step 1)")
        g.add_argument("--narrative", metavar="FILE", help="render the PDF with this narrative (step 2)")
        s.add_argument("--out", help="PDF path (default: data/reports/...)")
        s.add_argument("--font", help="TrueType font for the report language (default: DejaVu Sans from matplotlib)")
        s.add_argument("--font-bold", help="bold TrueType font (default: the bold DejaVu Sans, or --font)")
    lb = sub.add_parser("labels", help="print every label as JSON, to translate into narrative.labels")
    lb.add_argument("--lang", default="en", help="start from this built-in language (en, vi)")
    args = parser.parse_args(argv)

    if args.kind == "labels":
        print(json.dumps(Labels(args.lang).table, indent=1, ensure_ascii=False))
        return 0
    if args.kind == "activity":
        facts, series = activity_facts(args.activity, route=args.route)
        default = REPORT_DIR / f"activity-{facts['activity']}.pdf"
    else:
        start, end, label, period = period_from_args(args)
        facts, series = wellness_facts(start, end, label)
        facts["period"] = period
        tag = args.week or args.month or f"{start}_{end}"
        default = REPORT_DIR / f"wellness-{tag}.pdf"

    if args.facts:
        Path(args.facts).write_text(json.dumps(facts, indent=1, ensure_ascii=False, default=str) + "\n")
        print(f"facts written to {args.facts}")
        return 0
    narrative = load_narrative(args.narrative)
    out = Path(args.out) if args.out else default
    (render_activity if args.kind == "activity" else render_wellness)(facts, series, narrative, out, args.font, args.font_bold)
    print(f"report written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
