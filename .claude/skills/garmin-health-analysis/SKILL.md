---
name: garmin-health-analysis
description: Analyse the owner's Garmin health and training data stored in the local Medplum FHIR server (sleep, HRV, resting heart rate, stress, Body Battery, steps, SpO2, respiration, weight, workouts, and second-by-second workout data from FIT files: heart rate, pace, power, cadence, running dynamics, GPS) with read-only GraphQL, the Medplum MCP and the FIT decoder. Use for questions like "how did I sleep this month", "is my HRV trending down", "compare training load and recovery", "summarise last week", or any trend, comparison or correlation over the Garmin data.
---

# Garmin health analysis (Medplum, read-only)

The data is FHIR R4 in the local Medplum (http://localhost:8103), loaded from
the raw Garmin archive by `garmin_fhir.py`. Run everything from the repository
root. Everything here is read-only: the agent client cannot change data.

## 1. Check the server

```sh
python3 tools/medplum_agent.py codes hrv >/dev/null && echo ok
```

If it fails with "cannot reach Medplum", the stack is down
(`cd medplum && docker compose up -d`); say so and stop. Do not use the loader
client or the admin login to read data.

## 2. Two ways in

- **GraphQL (preferred for analysis):** `python3 tools/medplum_agent.py graphql '<query>' [--vars '<json>']`.
  Returns compact JSON with only the fields you ask for; pipe it into `python3 -c` for the maths.
- **MCP:** the `garmin-medplum` server in `.mcp.json` exposes `fhir-request`
  (FHIR REST: `GET Observation?...`). Same data, same read-only client. Use it
  when GraphQL through Bash is not available.

## 3. Rules that prevent wrong answers

0. **Medplum is the only source.** Do not read, parse or unzip anything under
   `data/days/` or `data/fhir/` to answer a question, even if you know the
   files are there; not for daily values and not for FIT files. If Medplum or
   the FIT decoder is unavailable, say which and how to fix it (section 1,
   section 6), and stop. Fall back to the raw archive only if the person
   explicitly says so.

1. **Dates carry the account's UTC offset.** Days are local calendar days; read
   the offset from any `effectivePeriod.start` (the examples below use `+07:00`). In GraphQL use
   `_filter: "date ge 2026-11-01T00:00:00+07:00 and date lt 2026-12-01T00:00:00+07:00"`
   (or `date: "ge...+07:00"` for one bound). In REST URLs encode `+` as `%2B`.
   A bare date (`2026-11-07`) is read as UTC and shifts or loses days.
2. **Look up a custom code before interpreting it.** `python3 tools/medplum_agent.py codes <text>`
   prints the definition, unit and value type of `garmin-metrics` codes.
   Stress, Body Battery, sleep score, training effect and fitness age are
   Garmin's own scales, not clinical measures.
3. **Missing is not zero.** A day without an Observation, or a component that is
   absent, means no measurement (watch off, no sync). Report gaps; never fill
   them with 0. A value of 0 that is present is real.
4. **HRV is RMSSD**, compared against the person's own baseline
   (`referenceRange` and the `hrv-baseline-*` components), not population norms.
5. **The last two days may still change** (sync delays); say so when they matter.
   With `GARMIN_INCLUDE_TODAY=true`, today is present too but partial: its
   sleep (last night) is usually complete, its steps, stress and calories are
   not.
6. **Ask for enough rows.** Lists default to a small page; always pass
   `_count: 1000` and page with `_offset` for longer ranges.
7. **Not medical advice.** Describe patterns and how they compare with the
   person's own history. For symptoms or worrying values, suggest a clinician.
8. **Personal data stays local.** Do not send raw data to external services.

## 4. Where each metric is

One Observation per metric per day unless noted. `value` is `valueQuantity.value`.

| Question | Code (system) | Value | Useful components |
|---|---|---|---|
| Steps | `41950-7` (LOINC) | steps/day | `step-goal` |
| Distance walked | `41953-1` (LOINC) | m | |
| Floors | `93833-2` (LOINC) up; `floors-descended` | count | `floors-goal` |
| Calories | `41979-6` (LOINC) total; `active-calories`, `resting-calories` | kcal | |
| Intensity minutes | `77592-4` moderate, `77593-2` vigorous (LOINC) | min | `intensity-minutes-goal` (weekly) |
| Resting heart rate | `40443-4` (LOINC) | /min | `resting-hr-7day-avg` |
| Daily min / max HR | `8883-1` / `8873-2` (LOINC) | /min | |
| Heart-rate samples | `8867-4` (LOINC), `valueSampledData` | /min every 120 s | `data` is space-separated; `E` = gap |
| SpO2 | `2708-6` (LOINC) daily avg and hourly series | % | `spo2-daily-min` |
| Respiration | `9279-1` (LOINC) | /min | `respiration-daily-min/max` |
| Stress | `stress-avg` | 0-100 | `stress-max`, `stress-*-duration` (min), `stress-qualifier` |
| Body Battery | `body-battery-max` (daily); `body-battery-level` (points); `body-battery-event-impact` (events) | 0-100 | `body-battery-min/-end/-charged/-drained` |
| Sleep | `93832-4` (LOINC), period = sleep window, dated by the morning it ends | min | LOINC `93831-6` deep, `93830-8` light, `93829-0` REM, `93828-2` awake; `sleep-need`, `sleep-avg-hr`, `sleep-avg-respiration`, `sleep-avg-spo2`, `sleep-avg-stress`, `sleep-awake-count` |
| Sleep score | `sleep-score` | 0-100 | `sleep-score-qualifier`, `sleep-*-pct`, `sleep-*-qualifier` |
| HRV | `hrv-rmssd-night-avg` | ms | `hrv-status`, `hrv-rmssd-7day-avg`, `hrv-baseline-*`; also `referenceRange` |
| HRV readings | `hrv-rmssd-readings`, `valueSampledData` | ms every 300 s | |
| Weight / BMI | `29463-7` / `39156-5` (LOINC), `effectiveDateTime` | kg / kg/m2 | |
| Fitness age | `fitness-age` | years | `fitness-age-achievable` |
| Workouts (totals; per-second detail: section 6) | `55411-3` (LOINC) Exercise duration, one per workout | min | `73985-4` type (text), `55425-3` avg HR, `55426-1` max HR, `55424-6` kcal, `workout-distance`, `workout-avg-speed`, `workout-avg-power`, `workout-aerobic-training-effect`, `workout-training-load`, `workout-hr-zone-N-time`, ... |

Code systems: LOINC = `http://loinc.org`; custom =
`https://github.com/jollyjoker992/garmin-archive/fhir/CodeSystem/garmin-metrics`
(in GraphQL `code:` arguments write `<system>|<code>`).

## 5. Query recipes (all tested)

Daily series for one metric over a range:

```sh
python3 tools/medplum_agent.py graphql 'query($code: String, $range: String) {
  ObservationList(code: $code, _filter: $range, _sort: "date", _count: 1000) {
    effectivePeriod { start } valueQuantity { value unit } } }' \
  --vars '{"code": "http://loinc.org|41950-7",
           "range": "date ge 2026-11-01T00:00:00+07:00 and date lt 2026-12-01T00:00:00+07:00"}'
```

Several metrics at once (GraphQL aliases), e.g. to correlate:

```graphql
{ steps:  ObservationList(code: "http://loinc.org|41950-7", _sort: "date", _count: 1000) { effectivePeriod { start } valueQuantity { value } }
  rhr:    ObservationList(code: "http://loinc.org|40443-4", _sort: "date", _count: 1000) { effectivePeriod { start } valueQuantity { value } }
  stress: ObservationList(code: "https://github.com/jollyjoker992/garmin-archive/fhir/CodeSystem/garmin-metrics|stress-avg", _sort: "date", _count: 1000) { effectivePeriod { start } valueQuantity { value } } }
```

Sleep with stages (key the components by code):

```graphql
{ ObservationList(code: "http://loinc.org|93832-4", _sort: "-date", _count: 30) {
    effectivePeriod { start end } valueQuantity { value }
    component { code { coding { code } } valueQuantity { value unit } valueCodeableConcept { text } } } }
```

HRV with status and baseline:

```graphql
{ ObservationList(code: "https://github.com/jollyjoker992/garmin-archive/fhir/CodeSystem/garmin-metrics|hrv-rmssd-night-avg", _sort: "date", _count: 1000) {
    effectivePeriod { end } valueQuantity { value }
    referenceRange { low { value } high { value } }
    component { code { coding { code } } valueQuantity { value } valueCodeableConcept { text } } } }
```

Workouts, with the FIT file they came from:

```graphql
{ ObservationList(code: "http://loinc.org|55411-3", _sort: "-date", _count: 100) {
    effectivePeriod { start end } valueQuantity { value }
    component { code { coding { code } } valueQuantity { value unit } valueCodeableConcept { text } }
    derivedFrom { resource { ... on DocumentReference { content { attachment { title size url } } } } } } }
```

Heart-rate samples of one day (`data` is a space-separated string; `E` = no reading):

```graphql
{ ObservationList(code: "http://loinc.org|8867-4", date: "ge2026-11-10T00:00:00+07:00", _sort: "date", _count: 1) {
    effectivePeriod { start } valueSampledData { period data } } }
```

Turning a component list into a dict in Python:

```python
c = {x["code"]["coding"][0]["code"]: (x.get("valueQuantity") or {}).get("value")
     or (x.get("valueCodeableConcept") or {}).get("text") for x in obs["component"]}
```

## 6. Inside a workout: second-by-second data from the FIT file

The workout Observation (`55411-3`) holds only totals and averages. Anything
*within* a workout needs the FIT file: heart-rate drift, pace or power per
kilometre, cadence against speed, time in zones over the course of the run,
recovery between intervals, elevation profile, the GPS route. Medplum stores
each workout's FIT archive as a `Binary`, linked from the Observation through
`derivedFrom` → `DocumentReference`; it does not parse it (FHIR has no type
for per-second tracks), so download and decode it:

```sh
python3 tools/medplum_agent.py fit list --from 2026-09-01T00:00:00+07:00   # workouts, ids, has FIT file
python3 tools/medplum_agent.py fit records <activity> --no-gps > run.csv     # one row per second
python3 tools/medplum_agent.py fit records <activity> --every 60 --format json --no-gps   # one row per minute
python3 tools/medplum_agent.py fit laps <activity>                           # per lap (auto-lap is usually 1 km)
python3 tools/medplum_agent.py fit messages <activity>                       # what else the file contains
```

- `<activity>` is the Garmin activity id, as `fit list` prints it (also the
  part after `activity/` in the Observation identifier).
- The decoder is Garmin's official FIT SDK, an **optional** dependency. If the
  command says it is missing, tell the person to run
  `python3 -m pip install garmin-fit-sdk` (installing it means accepting
  Garmin's FIT Protocol License); do not install it for them without asking,
  and do not work around it by unzipping `data/days/.../original.zip` yourself.
- `records` columns (only those the file has): `time` (local, with offset),
  `elapsed_s`, `distance_m`, `speed_mps`, `pace_min_per_km`, `altitude_m`,
  `heart_rate`, `cadence_spm` (steps/min for running and walking) or
  `cadence_rpm`, `power_w`, `vertical_oscillation_mm`, `vertical_ratio_pct`,
  `ground_contact_ms`, `step_length_mm`, `temperature_c`, `lat`, `lon` (degrees).
  Empty cells mean the sensor had no value at that second (common in the first
  seconds for running dynamics).
- A run of 40 minutes is about 2,500 rows. Do not read them all into the
  conversation: write the CSV to a file and compute with Python, or use
  `--every 30`/`--every 60` for an overview.
- **GPS is a home/route location.** Use `--no-gps` unless the question is
  about the route, and never paste coordinates into an answer unless asked.
- Beat-to-beat HRV is in the file only if the watch was set to record it
  (`hrv_mesgs` in `fit messages`); the 5-minute overnight HRV is in the
  daily data instead.
- Files are cached in `data/.fit-cache/` after the first download.

## 7. How to answer

1. State the date range and how many days actually have data.
2. Give the numbers (averages, ranges, trends) with units, from the query
   results; show the query you used if the person may want to rerun it.
3. Compare with the person's own earlier periods, not with population norms,
   unless asked.
4. Call out gaps, provisional days and Garmin-proprietary scales.
