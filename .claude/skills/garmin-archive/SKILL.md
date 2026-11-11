---
name: garmin-archive
description: Operate the garmin-archive backup (crawl missing Garmin days, check backup status, fix failed days or uploads) and read the archived raw Garmin health and activity data (sleep, HRV, heart rate, stress, body battery, steps, activities, FIT files). Use when the user asks about their Garmin backup, wants a missing day filled, or wants a question answered from their Garmin data.
---

# garmin-archive

Work from the repository root (the folder with `garmin_archive.py`). Full rules
are in `AGENTS.md`; file meanings are in `docs/data-format.md`; setup and
troubleshooting are in `docs/operations.md`.

## Check the backup

```sh
python3 garmin_archive.py status --json
```

No network, no side effects. Report `failed`, `never_fetched`,
`pending_uploads`, `cloud_only`, `remote_sync` and `last_run.exit_code`.
Days in `cloud_only` are backed up but have no local files; run
`python3 garmin_archive.py restore` before reading or mapping them. Exit code `2` in `last_run` with an
`error` about the API key means the owner must create a new key on Garmin
Connect; you cannot fix that.

## Fill missing days

```sh
python3 garmin_archive.py run --dry-run    # shows the days it would crawl
python3 garmin_archive.py run              # crawls, archives, uploads
```

- About 12-24 seconds per day depending on `GARMIN_MIN_INTERVAL`. For more
  than about 15 days, run it in the background and read
  `data/logs/garmin-archive.log`.
- If the Docker scheduler is running (`docker compose ps garmin-archive`), do
  not crawl from the host; use `docker compose run --rm garmin-archive run`
  only after `docker compose stop garmin-archive`, or let the schedule do it.
- Limit the range with `--from YYYY-MM-DD --to YYYY-MM-DD`. Add `--no-upload`
  to keep it local.
- Exit `1` means some days failed and will be retried by the next run. Run it
  again once; if the same day fails again, read `days[DATE].error` in
  `data/manifest.json` and consult the troubleshooting table.
- Never run two copies, never lower `GARMIN_MIN_INTERVAL`, never edit the
  manifest by hand.
- Ask before `--refetch` on a day that is already uploaded.

## Answer a question from the data

1. Read `data/manifest.json` and pick the days with `status` `final` or
   `provisional`.
2. Open the files under `data/<days[DATE].path>/`. The ones most often needed:

   | Question | File | Where to look |
   |---|---|---|
   | Sleep duration, stages, score | `sleep.json` | `sleepTimeSeconds`, `deepSleepSeconds`, `remSleepSeconds`, `sleepScores.overall.value` |
   | HRV | `hrv_summary.json` | `hrvSummaries[0].lastNightAvg`, `weeklyAvg`, `status` |
   | Resting heart rate | `heart_rate.json` | `restingHeartRate`; samples in `heartRateValues` as `[epoch ms, bpm]` |
   | Steps | `steps.json` | `[0].totalSteps`, `totalDistance` |
   | Stress | `stress.json` | `[0].values.overallStressLevel` |
   | Body battery | `body_battery.json` | `[0].charged`, `drained`, `bodyBatteryValuesArray` |
   | Workouts | `activities.json` | per item: `activityType.typeKey`, `distance` (m), `duration` (s), `averageHR` |
   | Per-second track | `activities/<id>/original.zip` | unzip, parse the `.fit` file |

3. Check that a value is present before using it. A missing file, `[]`, or
   `null` fields mean Garmin had no measurement that day; say so instead of
   treating it as zero.
4. For ranges, loop over the day folders. There is no database and no
   pre-aggregated file.

## Answer a question from the local Medplum

If `medplum/` is running (`docker compose ps` in that folder), the same data is
in FHIR at http://localhost:8103/fhir/R4.

1. Get a token with the read-only agent client from `.env`
   (`GARMIN_MEDPLUM_AGENT_CLIENT_ID` / `_SECRET`, `grant_type=client_credentials`
   at `/oauth2/token`). Never use the loader client to answer questions.
2. Search Observations by code and date. Always give dates with the account's
   UTC offset (see any `effectivePeriod.start`), e.g.
   `date=ge2026-11-07T00:00:00%2B07:00` for UTC+7; bare dates are read as UTC.
3. For a `garmin-metrics` code, call `CodeSystem/$lookup` first and read its
   definition and unit before interpreting the value.
4. Workouts: Observation code LOINC 55411-3; `derivedFrom` points to the
   DocumentReference whose attachment is the FIT archive.

The data is personal health data. Keep it on this machine: do not paste raw
files into external services.
