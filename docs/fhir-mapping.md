# FHIR mapping (version 0.1.2)

How the raw archive (`data/days/YYYY/YYYY-MM-DD/`) becomes FHIR R4 resources.
This is the specification that `garmin_fhir.py map` implements. Nothing here
changes the crawler or the raw archive; the FHIR data is a view that can be
rebuilt from the archive at any time.

## Output

```sh
python3 garmin_fhir.py map            # map days whose source or mapping changed
python3 garmin_fhir.py map --force    # map every day again
```

| File (under the data directory) | Content |
|---|---|
| `fhir/days/YYYY/YYYY-MM-DD.ndjson` | All resources of one day, one per line. |
| `fhir/shared.ndjson` | The Patient and the Devices. |
| `fhir/manifest.json` | Per day: status, resource counts, warnings, source `fetched_at`, mapping version, SHA-256 of the file. |

A day is mapped again when its source was re-crawled (`fetched_at` changed),
when `MAPPING_VERSION` changes, or with `--force`. Provisional days are mapped
too and are replaced once the crawler finalises them. A day that cannot be
mapped as specified is recorded as `failed` with the reason, and the command
exits with code 1.

Resources have no `id`. References between them use the FHIR conditional form
`Type?identifier=<system>|<value>`; the loader resolves them. The workout
DocumentReference carries the archive path of `original.zip` in the extension
`<base>/StructureDefinition/archive-path`; the loader uploads that file as a
Binary and sets `attachment.url`.

The mapper refuses to write a code that is not in the CodeSystem or the
verified LOINC table, or a value whose unit differs from the code's unit.

`tools/fhir-validate` checks files with the validator of `@medplum/core`
5.2.17 (`npm ci`, then `node validate.mjs ../../fhir ../../data/fhir`).

Files:

- [`fhir/CodeSystem-garmin-metrics.json`](../fhir/CodeSystem-garmin-metrics.json):
  101 codes, in 9 groups, for values with no correct LOINC code. Each code has a
  definition, its UCUM unit, its value type and its source field.
- [`fhir/ValueSet-garmin-metrics.json`](../fhir/ValueSet-garmin-metrics.json):
  every selectable code in that CodeSystem.
- [`fhir/examples/`](../fhir/examples/): one example per pattern below, with
  made-up values. All of them, and the CodeSystem and ValueSet, pass the
  validator of `@medplum/core` 5.2.17.

## Rules

1. **LOINC first, never a near miss.** A value uses LOINC only when the LOINC
   concept means the same thing. Every LOINC code below was checked against the
   NLM LOINC search API on 2026-11-11. HRV, stress and sleep score have LOINC
   codes that look right but are not (see "Rejected LOINC codes").
2. **Everything else uses `garmin-metrics`.**
   System: `https://github.com/jollyjoker992/garmin-archive/fhir/CodeSystem/garmin-metrics`.
3. **One Observation per metric per day**, or per sleep, per workout, per
   weigh-in. Statistics that only make sense together (sleep stages, a
   workout's figures) are `component`s of one Observation.
4. **Stable identifiers make loading idempotent.** Every resource carries an
   `identifier` with system `https://github.com/jollyjoker992/garmin-archive/fhir/identifier/<kind>`
   and a value built from the date or Garmin ID (`2026-11-09/steps`,
   `activity/12345678901`, `weight/<samplePk>`). The loader upserts by this
   identifier, so re-mapping a day replaces its resources instead of adding copies.
5. **Times carry the offset.** Garmin gives GMT without a zone
   (`2026-11-09T16:34:06.0`), local time without a zone, epoch milliseconds, or
   `YYYY-MM-DD HH:MM:SS`. The mapper converts all of them to ISO 8601 with the
   offset of that moment, taken from the difference between Garmin's GMT and
   local timestamps for the same event (fallback: the `timeZone` of the
   Garmin profile).
6. **Units are UCUM.** Conversions: milliseconds and seconds of duration →
   `min` (one decimal); weight grams → `kg`; speeds stay `m/s`; stride length
   stays `cm`. Garmin's own scores use `{score}`, counts use `{#}`.
7. **One source per value.** Several Garmin files repeat the same number. The
   source column below is the only file read for that value; the duplicates are
   listed under "Not mapped".
8. **Garmin labels are text, not codes.** Values such as `GOOD`, `UNBALANCED`,
   `AEROBIC_BASE` go in `valueCodeableConcept.text` with no coding, because
   Garmin's set of labels is undocumented and open-ended.
9. **Missing means absent.** A `null`, an empty list or HTTP 204 produces no
   Observation and no component. A value of `0` is kept.
10. **Personal data.** The Patient carries the owner's name (decision 3). No
    avatar, GPS coordinates or place names in Observations. The FIT file,
    which contains the GPS track, is loaded as a Binary (decision 2).

## Loading

```sh
python3 garmin_fhir.py load            # load days whose mapped file changed
python3 garmin_fhir.py load --force    # send every day again (safe: upserts)
```

Uses the loader client from `.env` (`GARMIN_MEDPLUM_URL`, `_PROJECT_ID`,
`_CLIENT_ID`, `_CLIENT_SECRET`; see `medplum/README.md`).

1. Puts the `garmin-metrics` CodeSystem and ValueSet on the server when their
   version there differs from the repository.
2. Loads `shared.ndjson` (Patient, Devices), then each changed day, as one FHIR
   transaction of conditional updates (`PUT Observation?identifier=…`). The
   server resolves the conditional references to real ids.
3. Before a day's transaction, uploads each workout's `original.zip` as a
   `Binary` and sets `attachment.url` to it. An unchanged file (same SHA-1)
   reuses the Binary already on the server; a changed one replaces it and the
   old Binary is deleted. If the transaction fails, new uploads are removed.
4. Deletes resources a day had on the server that its new mapping no longer
   produces (conditional delete by identifier).
5. Records each day in `fhir/load-manifest.json`: SHA-256 of the loaded file,
   identifiers, Binary ids. A day whose file is unchanged is skipped. If
   `GARMIN_MEDPLUM_PROJECT_ID` changes (server rebuilt), everything is loaded again.

Medplum limits each client to a number of FHIR "points" per minute: 50,000 by
default (about 9 days of first-time loading), 10,000,000 in the local stack in
`medplum/` (see `medplum/README.md`, "Rate limits"). When a transaction comes
back with `429` entries, the loader waits 60 s and sends the same day again, up
to 10 times. Medplum applies
the entries it accepted, so a refused transaction is partly applied; resending
is safe because every entry is an upsert. HTTP 429/5xx and network errors are
retried with backoff; a 401 gets one new token.

## Querying the loaded data

Days are local calendar days in the account's time zone; every
`effectivePeriod` carries its UTC offset (the examples here use `+07:00`).
Medplum reads a bare date in a search as UTC, so for an account ahead of UTC
`date=ge2026-11-07` misses the morning of Nov 7 and `date=eq2026-11-07` matches
nothing. Always give the account's offset:

```
Observation?code=http://loinc.org|41950-7&date=ge2026-11-07T00:00:00%2B07:00&date=lt2026-11-11T00:00:00%2B07:00&_sort=date
Observation?code=https://github.com/jollyjoker992/garmin-archive/fhir/CodeSystem/garmin-metrics|hrv-rmssd-night-avg&_sort=-date&_count=7
CodeSystem/$lookup?system=https://github.com/jollyjoker992/garmin-archive/fhir/CodeSystem/garmin-metrics&code=body-battery-max
```

## Resources

| Resource | One per | Identifier value | Notes |
|---|---|---|---|
| `Patient` | account | Garmin `userProfilePk` | Name from `GARMIN_FHIR_PATIENT_NAME` (last word = family name). No photo. |
| `Device` | watch | Garmin `deviceId` | Manufacturer Garmin. Observations reference it when the day has exactly one device. The model name is not set (mapping 0.1.x). |
| `Observation` | metric × day / sleep / workout / weigh-in | see tables | `subject` = Patient, `device` = Device. |
| `Binary` | workout | `activity/<id>/original` | The `original.zip` bytes. |
| `DocumentReference` | workout | `activity/<id>/original` | Points at the Binary. `contentType` `application/zip` (FIT has no registered MIME type). |

Categories: `vital-signs` and `activity` from
`http://terminology.hl7.org/CodeSystem/observation-category` where they apply;
no category otherwise (FHIR has none for sleep or stress).

No `meta.profile` is set in this version. The HL7 vital-signs profile and the
Physical Activity IG profiles add constraints the mapper does not check yet.

## Daily Observations

`effectivePeriod` is the local calendar day, 00:00 to 24:00 with offset.
Identifier value: `<date>/<key>`.

| Key | Code | Unit | Source | Components |
|---|---|---|---|---|
| `steps` | LOINC 41950-7 Number of steps in 24 hour | `/d` | `daily_summary` movement.steps.value | `step-goal` |
| `distance` | LOINC 41953-1 Walking distance 24 hour | `m` | `daily_summary` movement.steps.distanceInMeters | |
| `floors-up` | LOINC 93833-2 Flights climbed 24 hour | `{#}` | `daily_summary` movement.floorsAscended.value | `floors-goal` |
| `floors-down` | `floors-descended` | `{#}` | `daily_summary` movement.floorsDescended.value | |
| `calories-total` | LOINC 41979-6 Calories burned in 24 hour | `kcal` | `daily_summary` calories.burnedTotal | |
| `calories-active` | `active-calories` | `kcal` | `daily_summary` calories.burnedActive | |
| `calories-resting` | `resting-calories` | `kcal` | `daily_summary` calories.burnedResting | |
| `moderate-minutes` | LOINC 77592-4 Moderate physical activity | `min` | `daily_summary` intensityMinutes.moderate | `intensity-minutes-goal` |
| `vigorous-minutes` | LOINC 77593-2 Vigorous physical activity | `min` | `daily_summary` intensityMinutes.vigorous | |
| `resting-hr` | LOINC 40443-4 Heart rate --resting | `/min` | `heart_rate` restingHeartRate, else `daily_summary` heartRate.restingValue | `resting-hr-7day-avg` |
| `hr-min` | LOINC 8883-1 Heart rate 24 hour minimum | `/min` | `heart_rate` minHeartRate, else `daily_summary` heartRate.minValue | |
| `hr-max` | LOINC 8873-2 Heart rate 24 hour maximum | `/min` | `heart_rate` maxHeartRate, else `daily_summary` heartRate.maxValue | |
| `hr-samples` | LOINC 8867-4 Heart rate, `valueSampledData` | `/min` | `heart_rate` heartRateValues | see "Time series" |
| `spo2-avg` | LOINC 2708-6 + 59408-5 | `%` | `daily_summary` pulseOx.avgValue | `spo2-daily-min` |
| `spo2-hourly` | LOINC 2708-6 + 59408-5, `valueSampledData` | `%` | `spo2` spO2HourlyAverages | |
| `respiration-avg` | LOINC 9279-1 Respiratory rate | `/min` | `daily_summary` respiration.avgValue | `respiration-daily-min`, `respiration-daily-max` |
| `stress` | `stress-avg` | `{score}` | `daily_summary` stress.avgLevel | `stress-max`, the six `stress-*-duration`, `stress-qualifier` |
| `body-battery` | `body-battery-max` | `{score}` | `daily_summary` bodyBattery.maxValue | `body-battery-min`, `-end`, `-charged`, `-drained` |
| `fitness-age` | `fitness-age` | `a` | `fitness_age` fitnessAge | `fitness-age-achievable`, `vigorous-days-weekly-avg`, `vigorous-minutes-weekly-avg` |

## Sleep Observations

`effectivePeriod` is the sleep window (sleepStart → sleepEnd, epoch ms GMT).
The sleep belongs to the day it ends on. Source: `sleep.json`.

| Key | Code | Unit | Value | Components |
|---|---|---|---|---|
| `sleep` | LOINC 93832-4 Sleep duration | `min` | sleepTimeSeconds | LOINC 93831-6 deep, 93830-8 light, 93829-0 REM, 93828-2 awake; `sleep-nap-duration`, `sleep-unmeasurable-duration`, `sleep-awake-count`, `sleep-avg-hr`, `sleep-avg-stress`, `sleep-avg-respiration`, `sleep-min-respiration`, `sleep-max-respiration`, `sleep-avg-spo2`, `sleep-min-spo2`, `sleep-need` |
| `sleep-score` | `sleep-score` | `{score}` | sleepScores.overall.value | `sleep-score-qualifier`, `sleep-deep-pct`, `sleep-light-pct`, `sleep-rem-pct`, and the seven `sleep-*-qualifier` codes. `derivedFrom` → the `sleep` Observation. |
| `hrv-night` | `hrv-rmssd-night-avg` | `ms` | `hrv` hrvSummary.lastNightAvg (period: `sleepStart/EndTimestampGMT`, else the HRV `start/endTimestampGMT` that older responses give, else `sleep.json`'s window) | `hrv-rmssd-night-5min-high`, `hrv-rmssd-7day-avg`, `hrv-status`, the three `hrv-baseline-*`. `referenceRange` = balancedLow..balancedUpper. |
| `hrv-readings` | `hrv-rmssd-readings`, `valueSampledData` | `ms` | `hrv` hrvReadings | |

## Event Observations

| Key | Code | Time | Source | Components |
|---|---|---|---|---|
| `body-battery/<ms>` | `body-battery-level` | `effectiveDateTime` | `body_battery` bodyBatteryValuesArray (4-6 points a day at irregular times) | |
| `body-battery-event/<ms>` | `body-battery-event-impact` | `effectivePeriod` from eventStartTimeGmt + duration | `body_battery` bodyBatteryActivityEvent | `body-battery-event-type` |
| `weight/<samplePk>` | LOINC 29463-7 Body weight | `effectiveDateTime` = timestampGMT | `weight` dailyWeightSummaries[].allWeightMetrics | `method.text` = sourceType (e.g. MANUAL) |
| `bmi/<date>` | LOINC 39156-5 BMI | `effectiveDateTime` = lastMeasurementDate | `fitness_age` components.bmi.value | Written once per measurement date, although it repeats daily. |

## Workout Observations

One Observation per activity. Identifier value `activity/<activityId>`.
`effectivePeriod` = startTimeGMT → endTimeGMT. Code LOINC 55411-3 Exercise
duration, value = `duration` in `min`. `derivedFrom` → the workout's
DocumentReference. Source: `activities.json`, plus `detail.json` where noted.

Components:

- LOINC 73985-4 Exercise activity: `valueCodeableConcept.text` = activityType.typeKey (`running`)
- LOINC 55425-3 mean heart rate, 55426-1 max heart rate, 55424-6 calories
- LOINC 77592-4 / 77593-2 moderate / vigorous minutes
- `workout-distance`, `-elapsed-duration`, `-moving-duration`, `-steps`
- `workout-avg-speed`, `-max-speed`, `-avg-grade-adjusted-speed`, `-elevation-gain`, `-elevation-loss`
- `workout-avg-power`, `-max-power`, `-normalized-power`
- `workout-avg-cadence`, `-max-cadence`, `-avg-stride-length`, `-avg-ground-contact-time`, `-avg-vertical-oscillation`, `-avg-vertical-ratio`
- `workout-aerobic-training-effect`, `-anaerobic-training-effect`, `-training-effect-label`, `-training-load`
- `workout-body-battery-change`, `workout-sweat-loss`
- `workout-stamina-start`, `-stamina-end`, `-impact-load`, `-feel`, `-rpe` (from `detail.json` summaryDTO)
- `workout-hr-zone-1-time` … `-5-time`, `workout-power-zone-1-time` … `-5-time`

The LOINC 55424-6 / 55425-3 / 55426-1 names say "by Pedometer"; the HL7
Physical Activity IG uses them for wearable workouts, so this mapping does too.

## Time series

`valueSampledData` needs evenly spaced samples. Measured spacing in the
archive:

| Series | Spacing | Encoding |
|---|---|---|
| Heart rate | 120 s, with gaps (longest seen 3.2 h) and occasional nulls | `period` 120000. `effectivePeriod.start` = first sample. Gaps and nulls are filled with `E`. |
| HRV readings | exactly 300 s | `period` 300000 |
| SpO2 hourly | exactly 3600 s | `period` 3600000 |
| Body Battery | irregular, 4-6 points | Not SampledData: one Observation per point. |

`origin` is 0 in the unit of the series; `dimensions` is 1. A series whose
spacing is not a multiple of its period is an error and the day is reported,
not silently resampled.

## Rejected LOINC codes

| Garmin value | Tempting LOINC code | Why not |
|---|---|---|
| Overnight HRV | 80404-7, 112429-6 | Both are SDNN; Garmin reports RMSSD. |
| Stress score | 93038-8 Stress level | Self-reported scale; Garmin's is computed from HRV. |
| Sleep score | 97891-6 Sleep score [R-Outcomes] | Questionnaire score. |
| Resting calories | 69429-9 Metabolic rate --resting | A rate, not a daily total, and Garmin's value is estimated. |
| Running power | 93342-4 Power level --peak during exercise | Exercise-test peak, not an average from a workout. |

## Not mapped

| Data | Reason |
|---|---|
| `steps.json`, `stress.json`, `sleep_summary.json`, `hrv_summary.json` | Duplicates of values taken from `daily_summary`, `sleep` and `hrv`. |
| `profile_settings.json` | Display preferences. Only `timeZone` is used, for offsets. |
| `heart_rate_zones.json` | Settings snapshot taken at crawl time, not a measurement of that day. |
| `workouts.json` | No planned-workout data in the archive. |
| `hydration`, `food_logs`, `weight_goals`, `wellness_activity`, `menstrual_cycle_*` | Empty in the archive so far. To be mapped when real data appears. |
| `sleep.json` sleepNeed (except `actual`), nextSleepNeed, sleepAlignment, feedback and insight phrases | Recommendations and app messages, not measurements. |
| `spo2.json` monitoringEnvironmentValues, `daily_summary` pulseOx.avgAltitudeInMeters | Altitude acclimation context; no consumer for it yet. |
| `daily_summary` hydration | Only goal and zero intake in the archive. |
| Activity names, location names, start/end coordinates, owner name, avatar URLs | Personal data (rule 10). |
| Activity flags (favorite, PR, privacy, has video, deco dive …) | App metadata. |
| Split summaries and laps | Present in the FIT file attached to the workout. |
| Per-second records and GPS track | FHIR has no type for them; they stay in the FIT file. |

## Decisions (2026-11-11)

1. **Canonical URL:** `https://github.com/jollyjoker992/garmin-archive/fhir/...`,
   configurable with `GARMIN_FHIR_BASE`. It must not change once data is loaded.
2. **Workout files:** each `original.zip` is loaded into Medplum as a Binary,
   GPS track included.
3. **Patient name:** set from `GARMIN_FHIR_PATIENT_NAME` (kept in `.env`, not in git).
4. **Time series:** all records are kept, including the daily heart-rate series.
5. **Durations:** minutes.
