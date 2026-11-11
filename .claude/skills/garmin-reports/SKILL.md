---
name: garmin-reports
description: Produce a PDF report from the owner's Garmin data in the local Medplum - an activity report for one workout (summary, second-by-second charts of heart rate, pace, power, cadence and running dynamics, splits, zones, evaluation and recommendations) or a wellness report for a week or month (sleep, HRV, resting heart rate, stress, Body Battery, steps, training, evaluation and recommendations). Use when the user asks for a report, a PDF, a weekly/monthly summary, or a write-up of a run, ride or other workout.
---

# Garmin reports (PDF)

One generator, `tools/garmin_report.py`, two report kinds, always in three
steps. The script computes the facts and draws the PDF; **you** write the
evaluation and recommendations from those facts. Never skip step 2 and never
put a claim in the narrative that the facts do not support.

Data comes from the local Medplum with the read-only client (see the
`garmin-health-analysis` skill and its rule 0: not from `data/days/`).

## 0. Before you start

- Medplum must be running: `python3 tools/medplum_agent.py codes hrv >/dev/null && echo ok`.
- The report needs matplotlib, reportlab and (for activities) Garmin's FIT SDK.
  If the script stops with "reports need matplotlib and reportlab" or the FIT
  SDK hint, tell the person to run
  `python3 -m pip install -r tools/requirements-reports.txt`
  (installing garmin-fit-sdk means accepting Garmin's FIT Protocol License).
  Do not install it for them without asking.
- Work in a temporary folder for the facts and narrative files, e.g.
  `mkdir -p /tmp/garmin-report`.

## 1. Pick the subject and compute the facts

**Activity:** find the activity id by date with
`python3 tools/medplum_agent.py fit list --from 2026-09-01T00:00:00+07:00`
(use the account's UTC offset; ask which one if several match).

```sh
python3 tools/garmin_report.py activity <activity> --facts /tmp/garmin-report/facts.json
```

**Wellness:** a week (`--week 2026-W45`, ISO week, Monday to Sunday), a month
(`--month 2026-10`) or a range (`--from 2026-10-01 --to 2026-10-14`). Without a
clear period, use the last complete week or month and say so. The ISO week of a
date: `python3 -c "import datetime as d; print('%d-W%02d' % d.date(2026,11,11).isocalendar()[:2])"`.

```sh
python3 tools/garmin_report.py wellness --week 2026-W45 --facts /tmp/garmin-report/facts.json
```

Read the whole facts file (6-8 KB; per-second data is not in it, only the
results computed from it).

## 2. Write the narrative

Write `/tmp/garmin-report/narrative.json`:

```json
{"headline": "one sentence: the single most important finding",
 "summary": "3-5 sentences: what happened, with the key numbers",
 "evaluation": ["4-6 findings, each with its numbers and what they mean"],
 "recommendations": ["3-6 concrete next steps, each tied to a finding above"]}
```

Rules:

- **Every number must come from the facts file.** Round sensibly, keep units,
  use local dates. If something is missing (no FIT file, no HRV that night,
  days without data), say so instead of guessing.
- **Compare with the person, not with the population**: the HRV balanced range,
  the previous period (`change_vs_previous_avg`), the recent workouts of the
  same type (`recent_same_type_last_8_weeks`). Population figures only where
  they are standard (WHO 150 intensity minutes per week).
- **Recommendations are specific and doable**: a target zone or heart-rate cap,
  a bedtime, a number of sessions, a cadence range. No diagnosis, no
  supplements or medication; for worrying patterns (resting HR rising for days,
  HRV LOW for days, very short sleep) suggest rest and, if unwell, a clinician.
- Plain words. Explain a term the first time (aerobic decoupling, HRV).

What to look at:

| Activity facts | What it tells you |
|---|---|
| `summary` | distance, time, pace, HR, power, training effect and load |
| `hr_zones.time` | intensity distribution; an easy or base run should be mostly Z2 (and some Z3), threshold work Z4 |
| `splits_km`, `variability.pace_cv_pct_30s` | pacing: even, positive (slowing) or negative split; walk breaks |
| `aerobic_decoupling.decoupling_pct` | under 5 % = aerobically steady; above = drift from intensity, heat, dehydration or fatigue |
| `variability.hr_second_to_last_km_change` | cardiac drift without the warm-up |
| `summary` running dynamics | cadence (steps/min), ground contact, vertical ratio, step length |
| `readiness_that_morning` | sleep, HRV vs balanced range, resting HR, Body Battery before the session |
| `recent_same_type_last_8_weeks` | speed per heartbeat vs. the median: is fitness changing? |

| Wellness facts | What it tells you |
|---|---|
| `summary.sleep_*`, `days[].sleep_need_min` | sleep quantity vs. need, score, deep/REM |
| `summary.hrv_ms`, `hrv_status_counts`, `days[].hrv_low/high` | recovery against the personal baseline |
| `summary.resting_hr` | rising = fatigue, illness or stress; falling = adaptation |
| `summary.stress_avg`, `bb_max`, `bb_min` | daytime load and recharge |
| `summary.steps`, `intensity_minutes_total`, `workouts` | movement and training volume |
| `change_vs_previous_avg`, `best_and_worst` | direction of travel and outliers |

## 3. Render and hand over

```sh
python3 tools/garmin_report.py activity <activity> --narrative /tmp/garmin-report/narrative.json
python3 tools/garmin_report.py wellness --week 2026-W45 --narrative /tmp/garmin-report/narrative.json
```

The PDF goes to `data/reports/activity-<id>.pdf` or
`data/reports/wellness-<period>.pdf` (mode 600, ignored by git); `--out` sets
another path. Tell the person the path and the headline. To check the layout,
`pdftoppm -r 60 -png <pdf> /tmp/garmin-report/page` and look at the pages.

- An activity report has: KPI tiles, summary, heart rate every second with the
  zones, pace, elevation, cadence, power, ground contact, vertical oscillation
  and ratio, step length (whatever the device recorded), splits per km, time
  in zones, efficiency against recent workouts, tables of splits, laps and
  further measures, evaluation, recommendations, data notes.
- A wellness report has: KPI tiles with the change against the previous
  period, summary, sleep stages against sleep need, sleep score, HRV with the
  balanced range, resting HR, stress, Body Battery range, steps against goal,
  workout minutes, a day-by-day table, workouts, evaluation, recommendations,
  data notes.

## Privacy

The PDF is personal health data. Keep it on this machine unless the person
asks to share it. `--route` adds the GPS route shape to an activity report;
use it only when asked (it shows where the person lives and runs).
