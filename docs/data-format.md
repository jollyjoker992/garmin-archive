# Data format

Everything lives under `GARMIN_ARCHIVE_DIR` (default `./data`).

```
data/
  manifest.json                     index of every day (see below)
  days/2026/2026-09-30/             raw responses for one calendar day
    _day.json                       what was fetched for this day, and when
    daily_summary.json  sleep.json  hrv.json  ...
    activities.json                 activities that started on this day
    activities/12345678901/
      detail.json  workouts.json  original.zip
  archives/2026/2026-09-30.tar.gz   the day folder, compressed (final days only)
  logs/garmin-archive.log           rotating log
  .token.json  .lock                bearer-token cache (mode 0600) and run lock
```

On the upload targets the layout is `<remote>/archives/YYYY/YYYY-MM-DD.tar.gz`
plus `<remote>/manifest.json`. Each archive unpacks to a single `YYYY-MM-DD/`
folder with the same content as the local day folder.

## Rules that hold for every file

- A `.json` file is the exact response body from Garmin. Nothing is reformatted,
  merged or removed. Field names and units are Garmin's.
- A day is a **calendar date in the account's local time**, as Garmin defines
  `calendarDate`. Sleep for date `D` is the night that ends on the morning of `D`.
- If Garmin answers with an empty body (HTTP 204), no file is written. The
  manifest and `_day.json` record `"http": 204` for that endpoint.
- A file can exist and still hold no data: `[]`, `{"data":[]}`, or an object
  whose fields are all `null`. That is how Garmin answers for days without a
  measurement.
- Timestamps inside the files are Garmin's (most are epoch milliseconds or
  `...GMT` / `...Local` pairs). Timestamps written by this tool (`fetched_at`
  and so on) are UTC, ISO 8601.

## Files in a day folder

`{d}` is the day. All paths are relative to `https://personal.connectapi.garmin.com`.

| File | Endpoint | Content |
|---|---|---|
| `daily_summary.json` | `/usersummary-service/v2/dailysummaries?startDate={d}&endDate={d}` | Calories, heart rate, stress, body battery, intensity minutes, SpO2, respiration, movement. |
| `steps.json` | `/usersummary-service/stats/steps/daily/{d}/{d}` | Step count, distance, step goal. |
| `stress.json` | `/usersummary-service/stats/stress/daily/{d}/{d}` | Overall stress level and seconds per stress band. |
| `hydration.json` | `/usersummary-service/stats/hydration/daily/{d}/{d}` | Logged fluid intake. |
| `sleep.json` | `/sleep-service/sleep/dailySleep?date={d}` | Sleep stages, score, SpO2, respiration, sleep need. |
| `sleep_summary.json` | `/sleep-service/sleep/dailySleepSummariesByDate?startDate={d}&endDate={d}` | Sleep summary including naps. |
| `body_battery.json` | `/wellness-service/wellness/bodyBattery/reports/daily?startDate={d}&endDate={d}` | Charged, drained and the time series. |
| `heart_rate.json` | `/wellness-service/wellness/dailyHeartRate?date={d}` | Resting, min, max and timestamped samples. |
| `hrv.json` | `/hrv-service/hrv/{d}` | Overnight HRV readings and summary. |
| `hrv_summary.json` | `/hrv-service/hrv/daily/{d}/{d}` | Nightly average, weekly average, baseline, status. |
| `spo2.json` | `/wellness-service/wellness/daily/spo2acclimation/{d}` | SpO2 averages and samples. |
| `fitness_age.json` | `/fitnessage-service/fitnessage/v2/{d}` | Fitness age and its components. |
| `weight.json` | `/weight-service/weight/range/{d}/{d}?includeAll=true` | Weigh-ins. |
| `weight_goals.json` | `/goal-service/goal/user/effective/weightgoal/{d}/{d}` | Weight goal in effect. |
| `food_logs.json` | `/nutrition-service/food/logs/{d}` | Logged meals. |
| `wellness_activity.json` | `/wellnessactivity-service/activity/summary/range/{d}/{d}` | Wellness activity summaries. |
| `menstrual_cycle_summary.json` | `/periodichealth-service/menstrualcycle/summary/{d}` | Cycle summary. Usually HTTP 204, so no file. |
| `menstrual_cycle_day_view.json` | `/periodichealth-service/menstrualcycle/dayview/{d}?todayDate={d}` | Cycle day view. Usually HTTP 204, so no file. |
| `profile_settings.json` | `/userprofile-service/userprofile/settings` | **Snapshot at crawl time**, not the value on that day. |
| `heart_rate_zones.json` | `/biometric-service/heartRateZones/v2` | **Snapshot at crawl time**, not the value on that day. |
| `activities.json` | `/activitylist-service/activities/search/activities?startDate={d}&endDate={d}` | List of activities that started on the day, about 95 fields each. `[]` when there were none. |
| `activities/<id>/detail.json` | `/activity-service/activity/<id>` | Activity summary, splits, metadata. |
| `activities/<id>/workouts.json` | `/activity-service/activity/<id>/workouts` | Workout steps attached to the activity. |
| `activities/<id>/original.zip` | `/download-service/files/activity/<id>` | ZIP holding the original FIT file: the full per-second recording. |

The names in the first column, without `.json`, are the endpoint names used in
`GARMIN_SKIP_ENDPOINTS`, in the manifest and in `_day.json`. `activities` skips
the list and everything under `activities/`.

### `_day.json`

Written by the tool so that an archive explains itself without the manifest:

```json
{
 "date": "2026-09-30",
 "fetched_at": "2026-11-11T01:19:16Z",
 "tool_version": "1.0.0",
 "endpoints": {
  "sleep": {"http": 200, "bytes": 3140, "file": "sleep.json"},
  "menstrual_cycle_summary": {"http": 204, "bytes": 0}
 },
 "activities": [
  {"id": 12345678901, "type": "running", "files": {
    "detail":   {"http": 200, "bytes": 5659,  "file": "activities/12345678901/detail.json"},
    "workouts": {"http": 200, "bytes": 166,   "file": "activities/12345678901/workouts.json"},
    "original": {"http": 200, "bytes": 86993, "file": "activities/12345678901/original.zip"}}}
 ]
}
```

## What Garmin keeps, and for how long

Observed on 2026-11-11 (crawling 2026-03-15 to 2026-11-10 in one run):

| Data | Available back to |
|---|---|
| Daily values: steps, calories, stress, respiration, SpO2 daily, resting/min/max HR (daily summary), sleep stages and score, nightly HRV, Body Battery points, workouts with FIT files | the start of the account's data |
| Intraday detail: heart-rate samples and the heart-rate endpoint's min/max, 5-minute HRV readings, hourly SpO2 | about four months (here: from 2026-07-01) |

For older days Garmin returns the intraday fields as `null`. The archive keeps
what was available when the day was crawled, so crawling daily preserves the
detail. Re-crawling an old day (`--refetch`) replaces its folder with what
Garmin returns now, which can be less, and re-uploads that copy over the
previous one on every target. Do not `--refetch` days older than about four
months.

## manifest.json

The single source of truth for what exists. It is rewritten atomically after
every crawled day and every upload. The JSON Schema is in
[manifest.schema.json](manifest.schema.json).

```json
{
 "schema_version": 1,
 "tool_version": "1.0.0",
 "created_at": "2026-11-11T01:17:20Z",
 "updated_at": "2026-11-11T01:19:16Z",
 "last_run": {
  "started_at": "2026-11-11T01:18:50Z", "finished_at": "2026-11-11T01:19:16Z",
  "exit_code": 0, "days_planned": 1, "days_crawled": 1, "days_failed": 0,
  "days_deferred": 0, "archived": 1, "uploaded": 2, "upload_failures": 0,
  "requests": 24, "error": null
 },
 "days": {
  "2026-09-30": {
   "status": "final",
   "attempts": 1,
   "fetched_at": "2026-11-11T01:19:16Z",
   "path": "days/2026/2026-09-30",
   "files": 23,
   "bytes": 142591,
   "endpoints": { "...": "same as _day.json" },
   "activities": [ "... same as _day.json" ],
   "archive": {
    "file": "archives/2026/2026-09-30.tar.gz",
    "bytes": 100796,
    "sha256": "0f1e2d3c...",
    "created_at": "2026-11-11T01:19:16Z"
   },
   "uploads": {
    "gdrive": {"remote": "gdrive:Backups/garmin/archives/2026/2026-09-30.tar.gz",
               "sha256": "0f1e2d3c...", "uploaded_at": "2026-11-11T01:20:13Z"},
    "s3":     {"remote": "s3garmin:my-bucket/garmin/archives/2026/2026-09-30.tar.gz",
               "sha256": "0f1e2d3c...", "uploaded_at": "2026-11-11T01:20:13Z",
               "storage_class": "GLACIER"}
   }
  }
 }
}
```

### Remote state

Every run with upload targets starts by reading each target (see README,
"What a run does", step 0) and records the result:

```json
"remote_sync": {
 "gdrive": {"checked_at": "2026-11-11T03:50:13Z", "remote_manifest": true, "remote_archives": 4,
            "adopted": 4, "confirmed": 0, "dropped": 0},
 "s3":     {"checked_at": "2026-11-11T03:50:13Z", "error": "rclone exited 1: ..."}
}
```

A day taken over from a target's manifest carries `"adopted_from": "gdrive"`.
It has its `archive` and `uploads` records but no local folder until
`restore`.

### Day status

| `status` | Meaning | Next run |
|---|---|---|
| (day absent) | Never fetched. | Crawled if inside the range. |
| `provisional` | Fetched while younger than `GARMIN_SETTLE_DAYS`. Folder exists, no archive. | Crawled again. |
| `final` | Fetched when old enough. Archived, then uploaded. | Left alone. |
| `failed` | The last crawl did not finish. `error` and `failed_at` say why. No folder unless an older provisional copy exists. | Crawled again. |

A day is fully backed up when `status` is `final`, `archive` exists, and
`uploads.<target>.sha256` equals `archive.sha256` for every configured target.

Per-endpoint `http` values: `200` with a `file` means data was stored; `204`
means Garmin had nothing; a `4xx` value means Garmin refused that endpoint for
that day and it was skipped.
