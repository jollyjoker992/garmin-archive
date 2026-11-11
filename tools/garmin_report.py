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
  {"headline": "one sentence",
   "summary": "a short paragraph",
   "evaluation": ["finding grounded in the facts", ...],
   "recommendations": ["concrete, actionable step", ...]}

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
        return start, start + dt.timedelta(days=6), f"week {args.week.upper()}"
    if args.month:
        year, month = (int(x) for x in args.month.split("-"))
        start = dt.date(year, month, 1)
        nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
        return start, nxt - dt.timedelta(days=1), start.strftime("%B %Y")
    if args.start and args.end:
        start, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)
        return start, end, f"{start} to {end}"
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
    offset = next((x for x in [None] if x), None)
    sample = observations_by_key([f"{days[-1]['date']}/steps", f"{days[0]['date']}/steps"])
    starts = [o["effectivePeriod"]["start"] for o in sample.values() if o.get("effectivePeriod")]
    offset = starts[0][19:] if starts else "+00:00"
    iso = lambda d: f"{d.isoformat()}T00:00:00{offset}"
    workouts = ma.list_workouts(iso(start), iso(end + dt.timedelta(days=1)))
    length = (end - start).days + 1
    prev_start, prev_end = start - dt.timedelta(days=length), start - dt.timedelta(days=1)
    prev_days = wellness_days(prev_start, prev_end)
    prev_workouts = ma.list_workouts(iso(prev_start), iso(start))
    this, prev = aggregate(days, workouts), aggregate(prev_days, prev_workouts)
    delta = {}
    for k in ("sleep_min", "sleep_score", "hrv_ms", "resting_hr", "stress_avg", "bb_max", "steps"):
        a, b = this[k]["avg"], prev[k]["avg"]
        delta[k] = (rnd(a - b, 0 if k == "steps" else 1) if a is not None and b is not None else None)
        if k == "steps" and delta[k] is not None:
            delta[k] = int(delta[k])
    today = dt.date.today()
    facts = {
        "kind": "wellness", "label": label, "from": start.isoformat(), "to": end.isoformat(), "utc_offset": offset,
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
        ] + ([f"Days from {max(start, today - dt.timedelta(days=1))} on may still change (watch sync)."] if end >= today - dt.timedelta(days=1) else []),
    }
    return facts, {"days": days, "workouts": workouts}


# --------------------------------------------------------------------------
# Charts (matplotlib)
# --------------------------------------------------------------------------


def _plt():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        fail(DEPS_HINT)
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 8, "axes.edgecolor": GRID, "axes.labelcolor": INK2,
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


def _line(ax, x, y, title, unit, color=SERIES[0], invert=False):
    pts = [(a, b) for a, b in zip(x, y) if b is not None]
    if not pts:
        ax.text(0.5, 0.5, "no data", ha="center", va="center", color=MUTED, transform=ax.transAxes)
    else:
        ax.plot([p[0] for p in pts], [p[1] for p in pts], color=color, solid_capstyle="round")
    ax.set_title(f"{title} ({unit})")
    if invert:
        ax.invert_yaxis()


def activity_charts(series: dict, workdir: Path) -> list[tuple[str, Path]]:
    plt = _plt()
    rows = series["rows"]
    t = [r["elapsed_s"] / 60 for r in rows]
    smooth = lambda k, w=15: rolling([r.get(k) for r in rows], w)
    charts = []

    fig, ax = plt.subplots(figsize=(7.2, 2.4))
    zones = [z for z in series["zones"] if z.get("from_bpm") and z.get("to_bpm")]
    _line(ax, t, [r.get("heart_rate") for r in rows], "Heart rate", "bpm")
    hrs = [r["heart_rate"] for r in rows if r.get("heart_rate")]
    lo, hi = (min(hrs) - 5, max(hrs) + 5) if hrs else (40, 200)
    for i, z in enumerate(zones):
        ax.axhspan(z["from_bpm"], z["to_bpm"], color=RAMP[min(i, len(RAMP) - 1)], alpha=0.10, lw=0)
        mid = (max(z["from_bpm"], lo) + min(z["to_bpm"], hi)) / 2
        if z["to_bpm"] > lo and z["from_bpm"] < hi:          # label only zones that are visible
            ax.text(1.005, mid, z["zone"], transform=ax.get_yaxis_transform(), va="center", fontsize=7, color=INK2)
    ax.set_ylim(lo, hi)
    ax.set_xlabel("minutes")
    charts.append(("Heart rate, every second, with your heart-rate zones", _save(fig, workdir / "hr.png")))

    paces = [p for p in smooth("pace_min_per_km") if p]
    if paces:
        srt = sorted(paces)
        lo, hi = srt[int(len(srt) * 0.05)], srt[int(len(srt) * 0.90) - 1]
        fig, ax = plt.subplots(figsize=(7.2, 2.2))
        _line(ax, t, smooth("pace_min_per_km"), "Pace, 15 s average", "min/km, faster is up")
        ax.set_ylim(hi + 0.6, lo - 0.4)                       # inverted: faster is up; slower stretches leave the frame
        ax.set_xlabel("minutes")
        charts.append(("Pace over time", _save(fig, workdir / "pace.png")))

    present = lambda k: any(r.get(k) is not None for r in rows)
    panels = [(k, ttl, unit) for k, ttl, unit in (
        ("altitude_m", "Elevation", "m"), ("cadence_spm", "Cadence", "steps/min"), ("cadence_rpm", "Cadence", "rpm"),
        ("power_w", "Power, 15 s average", "W"), ("ground_contact_ms", "Ground contact time", "ms"),
        ("vertical_oscillation_mm", "Vertical oscillation", "mm"), ("vertical_ratio_pct", "Vertical ratio", "%"),
        ("step_length_mm", "Step length", "mm")) if present(k)]
    for i in range(0, len(panels), 2):
        pair = panels[i:i + 2]
        fig, axes = plt.subplots(1, len(pair), figsize=(7.2, 2.0), squeeze=False)
        for ax, (k, ttl, unit) in zip(axes[0], pair):
            ys = [r.get(k) for r in rows] if k == "altitude_m" else smooth(k)
            _line(ax, t, ys, ttl, unit)
            ax.set_xlabel("minutes")
        charts.append((" and ".join(p[1] for p in pair), _save(fig, workdir / f"panel{i}.png")))

    splits = series["splits"]
    if splits:
        fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.2))
        kms = [str(s["km"]) for s in splits]
        axes[0].bar(kms, [s["pace_min_per_km"] for s in splits], color=SERIES[0], width=0.6, edgecolor=SURFACE, linewidth=1.5)
        axes[0].set_title("Pace per km (min/km)")
        vals = [s["pace_min_per_km"] for s in splits if s["pace_min_per_km"]]
        axes[0].set_ylim(min(vals) * 0.9, max(vals) * 1.05)
        axes[1].bar(kms, [s["avg_hr"] for s in splits], color=SERIES[0], width=0.6, edgecolor=SURFACE, linewidth=1.5)
        axes[1].set_title("Average heart rate per km (bpm)")
        hv = [s["avg_hr"] for s in splits if s["avg_hr"]]
        axes[1].set_ylim(min(hv) - 15, max(hv) + 5)
        for ax in axes:
            ax.set_xlabel("km")
        charts.append(("Splits per kilometre", _save(fig, workdir / "splits.png")))

    zones_t = [z for z in series["zones"] if z["time_s"] > 0 or z["zone"] not in ("Below Z1", "Above max")]
    if zones_t:
        fig, ax = plt.subplots(figsize=(7.2, 1.9))
        labels = [f"{z['zone']} ({z['from_bpm'] or 0}-{z['to_bpm']})" if z.get("to_bpm") else z["zone"] for z in zones_t]
        colors = ([MUTED] if zones_t[0]["zone"] == "Below Z1" else []) + RAMP + [INK]
        ax.barh(labels, [z["time_s"] / 60 for z in zones_t], color=colors[:len(zones_t)], height=0.6)
        for i, z in enumerate(zones_t):
            ax.text(z["time_s"] / 60, i, f"  {z['time_s'] / 60:.1f} min ({z['share_pct']}%)", va="center", fontsize=7, color=INK2)
        ax.invert_yaxis()
        ax.set_title("Time in heart-rate zones (minutes)")
        ax.grid(axis="y", visible=False)
        charts.append(("Time in heart-rate zones", _save(fig, workdir / "zones.png")))

    hist = series["history"]
    if hist and series["this_ef"]:
        fig, ax = plt.subplots(figsize=(7.2, 1.9))
        xs = [dt.date.fromisoformat(h["date"]) for h in hist]
        import matplotlib.dates as mdates
        ax.plot(xs, [h["speed_per_beat"] for h in hist], "o", color=MUTED, markersize=4)
        this_day = dt.date.fromisoformat(series["rows"][0]["time"][:10]) if series["rows"] else None
        ax.plot([this_day], [series["this_ef"]], "o", color=SERIES[0], markersize=7)
        ax.annotate("this workout", (this_day, series["this_ef"]), xytext=(-8, 8), textcoords="offset points",
                    ha="right", fontsize=7, color=INK)
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=6))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
        ax.set_title("Efficiency: speed per heartbeat (m/s per 1000 bpm)")
        charts.append(("Efficiency of this workout (blue) against your workouts of the same type in the previous 8 weeks (grey)",
                       _save(fig, workdir / "trend.png")))

    if series["route"] and any("lat" in r for r in rows):
        pts = [(r["lon"], r["lat"]) for r in rows if r.get("lat") is not None]
        fig, ax = plt.subplots(figsize=(4.0, 4.0))
        ax.plot([p[0] for p in pts], [p[1] for p in pts], color=SERIES[0])
        ax.set_aspect(1 / math.cos(math.radians(pts[0][1])))
        ax.set_xticks([]), ax.set_yticks([])
        ax.set_title("Route shape")
        charts.append(("Route shape (GPS)", _save(fig, workdir / "route.png")))
    return charts


def wellness_charts(series: dict, workdir: Path) -> list[tuple[str, Path]]:
    plt = _plt()
    days = series["days"]
    xs = [dt.date.fromisoformat(d["date"]) for d in days]
    many = len(days) > 10
    import matplotlib.dates as mdates

    def fmt(ax):
        if many:                                   # month: a handful of dates, sized to the panel width
            wide = ax.get_position().width > 0.6
            ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=8 if wide else 4))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
        else:
            ax.xaxis.set_major_locator(mdates.DayLocator())
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %d"))
        ax.tick_params(axis="x", labelsize=7)
    charts = []
    width = 0.7

    fig, ax = plt.subplots(figsize=(7.2, 2.4))
    bottom = [0.0] * len(days)
    for (key, label), color in zip((("deep_min", "Deep"), ("light_min", "Light"), ("rem_min", "REM"), ("awake_min", "Awake")), SERIES):
        vals = [(d.get(key) or 0) / 60 for d in days]
        ax.bar(xs, vals, bottom=bottom, color=color, width=width, label=label, edgecolor=SURFACE, linewidth=1.2)
        bottom = [b + v for b, v in zip(bottom, vals)]
    need = [(d.get("sleep_need_min") or 0) / 60 or None for d in days]
    if any(need):
        ax.plot(xs, need, "_", color=INK, markersize=12, markeredgewidth=1.5, label="Sleep need")
    ax.set_title("Sleep per night (hours, by stage)")
    ax.legend(frameon=False, fontsize=7, ncol=5, loc="upper left", bbox_to_anchor=(0, -0.18))
    fmt(ax)
    charts.append(("Sleep and its stages", _save(fig, workdir / "sleep.png")))

    fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.1))
    _line(axes[0], xs, [d.get("sleep_score") for d in days], "Sleep score", "0-100")
    axes[0].plot([x for x, d in zip(xs, days) if d.get("sleep_score") is not None],
                 [d["sleep_score"] for d in days if d.get("sleep_score") is not None], "o", color=SERIES[0], markersize=3)
    lows = [d.get("hrv_low") for d in days]
    highs = [d.get("hrv_high") for d in days]
    if any(lows) and any(highs):
        axes[1].fill_between(xs, [l or float("nan") for l in lows], [h or float("nan") for h in highs], color=SERIES[0], alpha=0.12, lw=0, label="balanced range")
    _line(axes[1], xs, [d.get("hrv_ms") for d in days], "Overnight HRV", "ms, RMSSD")
    axes[1].plot([x for x, d in zip(xs, days) if d.get("hrv_ms") is not None], [d["hrv_ms"] for d in days if d.get("hrv_ms") is not None], "o", color=SERIES[0], markersize=3)
    if any(lows):
        axes[1].legend(frameon=False, fontsize=7, loc="lower left")
    for ax in axes:
        fmt(ax)
    charts.append(("Sleep score and overnight HRV", _save(fig, workdir / "score_hrv.png")))

    fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.1))
    _line(axes[0], xs, [d.get("resting_hr") for d in days], "Resting heart rate", "bpm")
    axes[0].plot([x for x, d in zip(xs, days) if d.get("resting_hr")], [d["resting_hr"] for d in days if d.get("resting_hr")], "o", color=SERIES[0], markersize=3)
    axes[1].bar(xs, [d.get("stress_avg") or 0 for d in days], color=SERIES[0], width=width, edgecolor=SURFACE, linewidth=1.2)
    axes[1].set_title("Average stress (0-100)")
    for ax in axes:
        fmt(ax)
    charts.append(("Resting heart rate and stress", _save(fig, workdir / "rhr_stress.png")))

    fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.1))
    lo = [d.get("bb_min") for d in days]
    hi = [d.get("bb_max") for d in days]
    axes[0].bar(xs, [(h or 0) - (l or 0) for h, l in zip(hi, lo)], bottom=[l or 0 for l in lo], color=SERIES[0], width=width, edgecolor=SURFACE, linewidth=1.2)
    axes[0].set_ylim(0, 100)
    axes[0].set_title("Body Battery, daily low to high (0-100)")
    axes[1].bar(xs, [d.get("steps") or 0 for d in days], color=SERIES[0], width=width, edgecolor=SURFACE, linewidth=1.2, label="Steps")
    goals = [d.get("step_goal") for d in days]
    if any(goals):
        axes[1].plot(xs, [g or float("nan") for g in goals], "_", color=INK, markersize=10, markeredgewidth=1.5, label="Goal")
        axes[1].legend(frameon=False, fontsize=7, ncol=2, loc="upper left", bbox_to_anchor=(0, -0.22))
    axes[1].set_title("Steps")
    for ax in axes:
        fmt(ax)
    charts.append(("Body Battery and steps", _save(fig, workdir / "bb_steps.png")))

    per_day = {d["date"]: 0.0 for d in days}
    for w in series["workouts"]:
        if w["start"][:10] in per_day:
            per_day[w["start"][:10]] += w.get("minutes") or 0
    fig, ax = plt.subplots(figsize=(7.2, 1.8))
    ax.bar(xs, list(per_day.values()), color=SERIES[0], width=width, edgecolor=SURFACE, linewidth=1.2)
    ax.set_title("Workout minutes per day")
    if not any(per_day.values()):
        ax.set_ylim(0, 60)
        ax.text(0.5, 0.5, "no workouts recorded in this period", ha="center", va="center", color=MUTED,
                transform=ax.transAxes)
    fmt(ax)
    charts.append(("Training", _save(fig, workdir / "training.png")))
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
    return n


def build_pdf(out: Path, title: str, subtitle: str, tiles: list[tuple[str, str]], narrative: dict,
              charts: list[tuple[str, Path]], tables: list[tuple[str, list[list]]], notes: list[str]) -> None:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import (Image, KeepTogether, ListFlowable, ListItem, Paragraph, SimpleDocTemplate,
                                        Spacer, Table, TableStyle)
    except ImportError:
        fail(DEPS_HINT)
    from xml.sax.saxutils import escape

    ss = getSampleStyleSheet()
    base = ParagraphStyle("base", parent=ss["Normal"], fontName="Helvetica", fontSize=9.5, leading=13.5, textColor=colors.HexColor(INK))
    h1 = ParagraphStyle("h1", parent=base, fontName="Helvetica-Bold", fontSize=18, leading=22, spaceAfter=2)
    sub = ParagraphStyle("sub", parent=base, fontSize=9, textColor=colors.HexColor(INK2), spaceAfter=10)
    h2 = ParagraphStyle("h2", parent=base, fontName="Helvetica-Bold", fontSize=12, leading=16, spaceBefore=12, spaceAfter=5)
    lead = ParagraphStyle("lead", parent=base, fontName="Helvetica-Bold", fontSize=10.5, leading=14.5, spaceAfter=4)
    cap = ParagraphStyle("cap", parent=base, fontSize=8, textColor=colors.HexColor(INK2), spaceAfter=8)
    small = ParagraphStyle("small", parent=base, fontSize=7.5, leading=10, textColor=colors.HexColor(INK2))
    tile_v = ParagraphStyle("tv", parent=base, fontName="Helvetica-Bold", fontSize=13, leading=16)
    tile_l = ParagraphStyle("tl", parent=base, fontSize=7.5, leading=9, textColor=colors.HexColor(INK2))
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
    story += [tt, Spacer(1, 6), p("Summary", h2), p(narrative["headline"], lead), p(narrative["summary"])]

    story.append(p("Details", h2))
    for caption, path in charts:
        from reportlab.lib.utils import ImageReader
        w, h = ImageReader(str(path)).getSize()
        width = 174 * mm if w / h > 1.5 else 90 * mm
        story.append(KeepTogether([Image(str(path), width=width, height=width * h / w), p(caption, cap)]))

    for caption, rows in tables:
        if len(rows) < 2:
            continue
        data = [[p(c, small) for c in r] for r in rows]
        t = Table(data, repeatRows=1, hAlign="LEFT")
        t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor(INK2)),
                               ("LINEBELOW", (0, 1), (-1, -1), 0.3, colors.HexColor(GRID)),
                               ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        story += [KeepTogether([p(caption, h2), t])]

    bullet = lambda items: ListFlowable([ListItem(p(i), leftIndent=10, value="•") for i in items],
                                        bulletType="bullet", start="•", leftIndent=12, bulletFontSize=8)
    story += [p("Evaluation", h2), bullet(narrative["evaluation"]),
              p("Recommendations", h2), bullet(narrative["recommendations"]),
              p("Data notes", h2)] + [p(n, small) for n in notes + [
                  "This report describes patterns in wearable data. It is not medical advice; see a clinician for symptoms or concerns."]]

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor(MUTED))
        canvas.drawString(18 * mm, 10 * mm, f"garmin-archive report - generated {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"page {doc.page}")
        canvas.restoreState()

    out.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(out), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=16 * mm, title=title, author="garmin-archive", subject=subtitle)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    os.chmod(out, 0o600)


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def fmt_day(iso: str) -> str:
    return dt.datetime.fromisoformat(iso).strftime("%a %d %b %Y, %H:%M")


def render_activity(facts: dict, series: dict, narrative: dict, out: Path) -> None:
    import tempfile
    s = facts["summary"]
    with tempfile.TemporaryDirectory() as tmp:
        charts = activity_charts(series, Path(tmp))
        tiles = [("distance", f"{s['distance_km']} km"), ("moving time", s["moving"]), ("average pace", s["avg_pace"] or "-"),
                 ("average / max HR", f"{s['avg_hr']} / {s['max_hr']} bpm"),
                 ("aerobic / anaerobic TE", f"{s['aerobic_training_effect']} / {s['anaerobic_training_effect']}"),
                 ("training load", str(s["training_load"])), ("calories", f"{s['calories_kcal']} kcal"),
                 ("avg / normalized power", f"{s['avg_power_w']} / {s['normalized_power_w']} W" if s.get("avg_power_w") else "-")]
        splits = [["km", "time", "pace", "avg HR", "max HR", "cadence", "power", "elev. +"]] + [
            [x["km"], dur_text(x["time_s"]), pace_text(x["pace_min_per_km"]), x["avg_hr"], x["max_hr"], x["avg_cadence"],
             x["avg_power_w"], x["elevation_gain_m"]] for x in facts["splits_km"]]
        laps = [["lap", "start", "distance", "time", "avg HR", "max HR", "avg power"]] + [
            [l["lap"], (l.get("start") or "")[11:19], f"{(l.get('total_distance') or 0) / 1000:.2f} km", dur_text(l.get("total_timer_time")),
             l.get("avg_heart_rate"), l.get("max_heart_rate"), l.get("avg_power")] for l in facts["laps"]]
        r = facts["readiness_that_morning"]
        dec = facts["aerobic_decoupling"] or {}
        details = [["measure", "value"],
                   ["elapsed time", s["elapsed"]], ["elevation gain / loss", f"{s['elevation_gain_m']} / {s['elevation_loss_m']} m"],
                   ["average cadence", f"{s['avg_cadence_spm']} steps/min" if s.get("avg_cadence_spm") else "-"],
                   ["ground contact / vertical ratio / step length",
                    f"{s['avg_ground_contact_ms']} ms / {s['avg_vertical_ratio_pct']} % / {s['avg_step_length_m']} m"],
                   ["primary benefit", s.get("training_effect_label") or "-"],
                   ["aerobic decoupling (after 5 min)", f"{dec.get('decoupling_pct')} %" if dec else "-"],
                   ["pace variability (30 s, CV)", f"{facts['variability']['pace_cv_pct_30s']} %"],
                   ["stamina start / end", " / ".join(f"{v}%" for v in s["stamina_start_end_pct"] if v is not None) or "-"],
                   ["Body Battery change", s.get("body_battery_change")],
                   ["feel / RPE", f"{s.get('feel_0_100')} / {s.get('rpe_10_100')}"],
                   ["sweat loss (estimate)", f"{s['sweat_loss_ml']} ml" if s.get("sweat_loss_ml") else "-"],
                   ["that morning: sleep / score", f"{r['sleep_min']} min / {r['sleep_score']}"],
                   ["that morning: HRV / status", f"{r['hrv_ms']} ms / {r['hrv_status']}"],
                   ["that morning: resting HR / Body Battery max", f"{r['resting_hr']} bpm / {r['body_battery_max']}"]]
        build_pdf(out, f"Activity report: {str(facts['type']).replace('_', ' ').capitalize()}, {fmt_day(facts['start'])}",
                  f"{facts['start'][:16].replace('T', ' ')} to {facts['end'][11:16]} ({facts['start'][19:]}) - Garmin activity {facts['activity']}",
                  tiles, narrative, charts,
                  [("Splits per kilometre", splits), ("Laps (as recorded by the watch)", laps), ("More measures", details)],
                  facts["data_notes"])


def render_wellness(facts: dict, series: dict, narrative: dict, out: Path) -> None:
    import tempfile
    s, d = facts["summary"], facts["change_vs_previous_avg"]
    sign = lambda v, unit="": "" if v is None else f" ({'+' if v >= 0 else ''}{v}{unit} vs previous)"
    hm = lambda m: "-" if m is None else f"{int(m // 60)}h {int(m % 60):02d}m"
    with tempfile.TemporaryDirectory() as tmp:
        charts = wellness_charts(series, Path(tmp))
        tiles = [("average sleep" + sign(d["sleep_min"], " min"), hm(s["sleep_min"]["avg"])),
                 ("average sleep score" + sign(d["sleep_score"]), str(s["sleep_score"]["avg"])),
                 ("average overnight HRV" + sign(d["hrv_ms"], " ms"), f"{s['hrv_ms']['avg']} ms"),
                 ("average resting HR" + sign(d["resting_hr"], " bpm"), f"{s['resting_hr']['avg']} bpm"),
                 ("average stress" + sign(d["stress_avg"]), str(s["stress_avg"]["avg"])),
                 ("average Body Battery high" + sign(d["bb_max"]), str(s["bb_max"]["avg"])),
                 ("steps per day" + sign(d["steps"]), f"{round(s['steps']['avg'] or 0):,}"),
                 ("workouts / intensity minutes", f"{s['workouts']['count']} / {s['intensity_minutes_total']}")]
        daily = [["date", "sleep", "score", "HRV", "RHR", "stress", "BB low-high", "steps"]] + [
            [x["date"][5:], hm(x["sleep_min"]), x["sleep_score"], f"{x['hrv_ms']} ({(x['hrv_status'] or '').lower()})" if x["hrv_ms"] else "-",
             x["resting_hr"], x["stress_avg"], f"{x['bb_min']}-{x['bb_max']}" if x["bb_max"] is not None else "-",
             f"{x['steps']:,}" if x["steps"] is not None else "-"] for x in facts["days"]]
        workouts = [["date", "type", "minutes", "distance", "avg HR"]] + [
            [w["start"][:16].replace("T", " "), w["type"], w["minutes"], f"{(w['distance_m'] or 0) / 1000:.2f} km", w["avg_hr"]]
            for w in facts["workouts"]]
        build_pdf(out, f"Wellness report: {facts['label']}",
                  f"{facts['from']} to {facts['to']} - compared with {facts['previous_period']['from']} to {facts['previous_period']['to']}",
                  tiles, narrative, charts, [("Day by day", daily), ("Workouts", workouts)], facts["data_notes"])


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
    args = parser.parse_args(argv)

    if args.kind == "activity":
        facts, series = activity_facts(args.activity, route=args.route)
        default = REPORT_DIR / f"activity-{facts['activity']}.pdf"
    else:
        start, end, label = period_from_args(args)
        facts, series = wellness_facts(start, end, label)
        tag = args.week or args.month or f"{start}_{end}"
        default = REPORT_DIR / f"wellness-{tag}.pdf"

    if args.facts:
        Path(args.facts).write_text(json.dumps(facts, indent=1, ensure_ascii=False, default=str) + "\n")
        print(f"facts written to {args.facts}")
        return 0
    narrative = load_narrative(args.narrative)
    out = Path(args.out) if args.out else default
    (render_activity if args.kind == "activity" else render_wellness)(facts, series, narrative, out)
    print(f"report written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
