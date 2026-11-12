---
name: garmin-reports
description: Produce a PDF report from the owner's Garmin data in the local Medplum - a daily report for one day (heart rate through the day, Body Battery, overnight HRV, sleep stages, stress, compared with the previous 28 days), an activity report for one workout (summary, second-by-second charts of heart rate, pace, power, cadence and running dynamics, splits, zones, evaluation and recommendations) or a wellness report for a week or month (sleep, HRV, resting heart rate, stress, Body Battery, steps, training, evaluation and recommendations). Use when the user asks for a report, a PDF, a weekly/monthly summary, or a write-up of a run, ride or other workout.
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

**One day:** `--date 2026-11-11`. A one-day report is different from a week:
it shows the day itself (heart rate every 2 minutes with sleep and workouts
shaded, Body Battery through the day with what charged or drained it,
overnight HRV every 5 minutes, SpO2 by hour, sleep stages against sleep need,
time at each stress level) and compares the day with the **previous 28 days**
(average, range, how many of the 28 days were lower). Use it for "how was
yesterday / today / 11 November". Today is allowed: last night's sleep, HRV and
resting HR are final, the rest is partial and left out of the comparison.

```sh
python3 tools/garmin_report.py day --date 2026-11-11 --facts /tmp/garmin-report/facts.json
```

(`wellness --from D --to D` with one day is turned into this report.)

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
{"language": "vi",
 "headline": "one sentence: the single most important finding",
 "summary": "3-5 sentences: what happened, with the key numbers",
 "evaluation": ["4-6 findings, each with its numbers and what they mean"],
 "recommendations": ["3-6 concrete next steps, each tied to a finding above"],
 "data_notes": ["optional: the facts' data_notes, translated"]}
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

| Day facts | What it tells you |
|---|---|
| `sleep` | window, minutes vs `need_min`, stages and percentages, Garmin's sub-ratings, vitals during sleep |
| `hrv` | night average vs `balanced_range_ms`, status, spread of the 5-minute readings |
| `heart_rate` | resting vs its 7-day average, day min/max, awake average, `hourly_avg` |
| `stress` | average, peak, Garmin's label, minutes at each level |
| `body_battery` | high/low, charged/drained, `events` that charged or drained it, points through the day |
| `activity`, `workouts` | steps vs goal, calories, intensity minutes, workouts of the day |
| `compared_with_previous_28_days` | for each measure: this day, average, range, difference, `days_lower` of 28 |

## Language

Write the report in the language the person asked for, or the one they wrote
in. Set `"language"` in `narrative.json` (`"en"`, `"vi"`, `"ja"`, ...).

- **English and Vietnamese are built in**: titles, chart labels, table headers,
  weekday and month names, number separators and Garmin values (HRV status,
  activity type, training benefit) all follow the language.
- **Other languages**: run `python3 tools/garmin_report.py labels --lang en`,
  translate every value (keep the keys, the `{placeholders}`, and the order of
  the weekday list, Monday first, and the month lists, January first; set
  `decimal_sep`/`thousands_sep`), and put the result in `"labels"`. The script
  warns about every label left in English.
- **Data notes**: translate the facts' `data_notes` into `"data_notes"`;
  without it the English notes are printed.
- **Fonts**: the default font (DejaVu Sans) covers Latin including Vietnamese,
  Greek and Cyrillic. For Chinese, Japanese, Korean, Thai, Devanagari and other
  scripts add `--font FILE.ttf` (and `--font-bold`); on macOS
  `/System/Library/Fonts/Supplemental/Arial Unicode.ttf` covers many scripts,
  on Linux use a Noto Sans `.ttf`. If the script stops with "has no glyphs
  for ...", pick a font that covers those characters and run it again; never
  drop or transliterate the characters instead. Right-to-left scripts (Arabic,
  Hebrew) are not laid out correctly by the PDF library: say so before making
  such a report.

## 3. Render and hand over

```sh
python3 tools/garmin_report.py activity <activity> --narrative /tmp/garmin-report/narrative.json
python3 tools/garmin_report.py wellness --week 2026-W45 --narrative /tmp/garmin-report/narrative.json
python3 tools/garmin_report.py day --date 2026-11-11 --narrative /tmp/garmin-report/narrative.json
```

The PDF goes to `data/reports/activity-<id>.pdf`,
`data/reports/wellness-<period>.pdf` or `data/reports/day-<date>.pdf` (mode 600, ignored by git); `--out` sets
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
