# garmin-archive

[![CI](https://github.com/jollyjoker992/garmin-archive/actions/workflows/ci.yml/badge.svg)](https://github.com/jollyjoker992/garmin-archive/actions/workflows/ci.yml)

Backs up your own Garmin Connect data as raw files, one folder per day, then
compresses each day and uploads it to Google Drive and S3 Glacier.

- **Source:** Garmin's official [Personal API](https://developer.garmin.com/gc-developer-program/personal/personal-api)
  (API key, no password login, no scraping).
- **Stored as-is:** every API response is written unmodified. There is no database.
- **Self-healing:** `manifest.json` records every day. Each run crawls whatever is
  missing, failed or not yet settled, however old.
- **Polite:** one request at a time, a minimum gap between requests, exponential
  backoff with jitter, `Retry-After` honoured, and long cool-downs when Garmin
  rate limits.
- **No Python dependencies:** standard library only (Python 3.10+). Uploads use
  [rclone](https://rclone.org).

> **Unofficial.** Not affiliated with or endorsed by Garmin. It uses Garmin's
> official Personal API with your own key; you are responsible for complying
> with Garmin's API terms. The data it produces is for personal use and is not
> medical advice. Licensed under [Apache-2.0](LICENSE); third-party notices are
> in [NOTICE](NOTICE); security policy in [SECURITY.md](SECURITY.md).

## Quick start

With Docker: see [docs/docker.md](docs/docker.md) (`docker compose up -d --build`
backfills, backs up and then runs daily at a set time). Without Docker:

```sh
cp .env.example .env          # then put your key and start date in .env
python3 garmin_archive.py run --dry-run     # show which days would be crawled
python3 garmin_archive.py run --no-upload   # crawl and compress, no upload
python3 garmin_archive.py status            # what is fetched, missing, pending
```

A day takes about 20 requests. At the default pace (`GARMIN_MIN_INTERVAL=1.0`)
that is about 24 seconds a day, roughly 95 minutes for 8 months. With
`GARMIN_MIN_INTERVAL=0.25` it measured about 12 seconds a day (8 months in
about 45 minutes) without any HTTP 429; requests stay sequential and 429 is
still backed off. Endpoints that always return HTTP 204 for an account (for
example the menstrual-cycle ones) can be skipped with `GARMIN_SKIP_ENDPOINTS`.
If a run is interrupted, run it again; it continues where it stopped.

To enable uploads, install rclone, create the two remotes
([docs/operations.md](docs/operations.md#upload-targets)) and set
`GARMIN_GDRIVE_REMOTE` and/or `GARMIN_S3_REMOTE`. Then schedule a daily run
([docs/operations.md](docs/operations.md#scheduling)).

## What a run does

0. **Sync with the cloud.** If upload targets are configured, reads
   `manifest.json` from each target. Days that a target holds as final and
   uploaded, but the local manifest lacks, are adopted: they are not crawled or
   uploaded again. Then lists each target's `archives/` and drops any upload
   record whose file is missing there, so it is uploaded again. A target without
   a manifest yet leaves the local manifest as it is; a target that cannot be
   read is logged and the run exits with code 1.
1. **Plan.** Lists every day from `GARMIN_START_DATE` to yesterday (to today with
   `GARMIN_INCLUDE_TODAY=true`) and keeps the
   ones that are not `final` in the manifest: never fetched, failed, or
   provisional. Newest days go first.
2. **Crawl.** For each day, calls every endpoint and writes the responses to
   `days/YYYY/YYYY-MM-DD/`. A day is written to a temporary folder and swapped in
   only when every request succeeded, so a half-fetched day never replaces a
   good one. The manifest is saved after each day.
3. **Settle.** A day crawled when it is at least `GARMIN_SETTLE_DAYS` (default 2)
   days old is `final`. A younger day is `provisional`: it is kept locally and
   crawled again on later runs, because watches sync sleep, HRV and activities
   late. Only final days are archived and uploaded, so an object in Glacier is
   never rewritten by normal operation.
4. **Archive.** Each final day becomes `archives/YYYY/YYYY-MM-DD.tar.gz`, with
   its SHA-256 in the manifest.
5. **Upload.** All archives not yet on a target are copied there in one batched
   rclone call per target (Google Drive rate-limits bursts of single-file
   calls); the target is then listed and only archives found there with the
   right size are recorded in the manifest. If the local archive is gone, it is copied from
   another target that holds it in readable storage (Google Drive, not
   Glacier). A failed upload is retried on the next run. `manifest.json` is
   uploaded last, to every target.

## Commands

```
python3 garmin_archive.py [--env-file FILE] run    [--from D] [--to D] [--refetch D]...
                                                   [--max-days N] [--no-upload] [--dry-run]
python3 garmin_archive.py [--env-file FILE] status [--from D] [--to D] [--json]
python3 garmin_archive.py [--env-file FILE] restore [--from D] [--to D]
```

| Option | Meaning |
|---|---|
| `--from`, `--to` | Limit the range. Defaults: `GARMIN_START_DATE` and yesterday. |
| `--refetch D` | Crawl day `D` again even if it is final. Its archive is rebuilt and uploaded again. Repeatable. Refused (exit 2) for archived days older than `GARMIN_REFETCH_MAX_AGE_DAYS`, because Garmin no longer serves their intraday detail. |
| `--allow-old-refetch` | Allow `--refetch` of such old archived days anyway; their heart-rate samples, HRV readings and hourly SpO2 are then lost, locally and on the targets. |
| `--max-days N` | Crawl at most `N` days in this run. |
| `--no-upload` | Crawl and archive only. |
| `--dry-run` | Print the days that would be crawled. Makes no request. |
| `status --json` | The same summary as `status`, as JSON for scripts and agents. `status` makes no network call and shows the remote state found by the last run. |
| `restore` | Downloads and unpacks final days that have no local folder (days adopted from the cloud), from a readable target, after checking each archive's SHA-256 against the manifest. Does not call Garmin. |

`run` is the default command, so a bare `python3 garmin_archive.py` works in cron.

### Exit codes

| Code | Meaning | What to do |
|---|---|---|
| 0 | Everything planned was crawled, archived and uploaded. | Nothing. |
| 1 | Some days or uploads failed. They are recorded and retried next run. | Check the log if it repeats. |
| 2 | Configuration error, Garmin rejected the API key, or a refused `--refetch` of an old archived day. | Fix the setting, create a new key, or reconsider the refetch. |
| 3 | Another run holds the lock. | Nothing. |

## Configuration

All settings are environment variables. A `.env` file next to the script (or
`--env-file`) is read too; real environment variables win.

| Variable | Default | Meaning |
|---|---|---|
| `GARMIN_PERSONAL_API_KEY` | required | Personal API key from Garmin Connect. |
| `GARMIN_START_DATE` | 365 days ago | First day to back up. |
| `GARMIN_ARCHIVE_DIR` | `./data` | Root of `days/`, `archives/`, `logs/`, `manifest.json`. |
| `GARMIN_GDRIVE_REMOTE` | unset | rclone destination, for example `gdrive:Backups/garmin`. |
| `GARMIN_S3_REMOTE` | unset | rclone destination, for example `s3garmin:my-bucket/garmin`. |
| `GARMIN_S3_STORAGE_CLASS` | `GLACIER` | Storage class for archives on S3. |
| `GARMIN_SETTLE_DAYS` | `2` | Age in days at which a crawled day is final. |
| `GARMIN_REFETCH_MAX_AGE_DAYS` | `90` | `--refetch` of an archived day older than this is refused without `--allow-old-refetch` (Garmin keeps intraday detail about four months). |
| `GARMIN_INCLUDE_TODAY` | `false` | Also fetch today, as provisional. Gives last night's sleep, HRV and Body Battery the same morning (Garmin dates sleep by the morning it ends). Today is never final, archived or uploaded. |
| `GARMIN_MIN_INTERVAL` | `1.0` | Minimum seconds between requests (up to 25% jitter is added). |
| `GARMIN_MAX_ATTEMPTS` | `5` | Attempts per request. |
| `GARMIN_BACKOFF_BASE` | `2` | Delay before the first retry, in seconds. Doubles each attempt. |
| `GARMIN_BACKOFF_MAX` | `120` | Upper bound of the retry delay. |
| `GARMIN_RATE_LIMIT_BACKOFF` | `30` | First wait after HTTP 429, in seconds. Doubles per attempt, capped at 600. |
| `GARMIN_RATE_LIMIT_COOLDOWN` | `300` | First pause of the crawl when a request stays rate limited. Doubles per pause, capped at 1800. |
| `GARMIN_RATE_LIMIT_COOLDOWNS` | `4` | Pauses in a row before the crawl gives up until the next run. |
| `GARMIN_TIMEOUT` | `30` | Per-request timeout in seconds. |
| `GARMIN_MAX_DAYS_PER_RUN` | `0` | Cap on days crawled per run. `0` means no cap. |
| `GARMIN_SKIP_ENDPOINTS` | unset | Comma-separated endpoint names to skip (names in [docs/data-format.md](docs/data-format.md)). |
| `GARMIN_LOG_LEVEL` | `INFO` | `DEBUG` logs every request. |
| `GARMIN_RCLONE_BIN` | `rclone` | Path to rclone. Cron often needs the full path. |

## Retry and rate limiting

- Requests are sequential. Each waits `GARMIN_MIN_INTERVAL` after the previous one.
- HTTP 5xx, timeouts and connection errors are retried, up to
  `GARMIN_MAX_ATTEMPTS` times. The delay is `GARMIN_BACKOFF_BASE * 2^(attempt-1)`,
  capped at `GARMIN_BACKOFF_MAX`, with jitter (50-100 % of that value).
- HTTP 429 (rate limited) is retried the same number of times with a longer
  delay: `GARMIN_RATE_LIMIT_BACKOFF * 2^(attempt-1)` with jitter, capped at
  10 minutes: by default 15-30 s, 30-60 s, 60-120 s, 120-240 s. A `Retry-After`
  header asking for longer is obeyed, up to 15 minutes.
- HTTP 401 on a data call gets one new bearer token and one more try.
- Other 4xx answers are not retried. The status is recorded for that endpoint
  and the day still completes.
- If a request is still rate limited after all its attempts, the whole crawl
  pauses (`GARMIN_RATE_LIMIT_COOLDOWN`, doubling each time, capped at 30 min:
  by default 5, 10, 20, 30 min) and then crawls the same day again. After
  `GARMIN_RATE_LIMIT_COOLDOWNS` (default 4) pauses without a successful day in
  between, the crawl stops; a successful day resets the count.
- Three days in a row failing for other reasons also stop the crawl. Nothing is
  lost: the next run continues from the manifest.

## FHIR export

`garmin_fhir.py` maps the archived days to FHIR R4 resources for a FHIR server
such as Medplum. It reads only the local archive.

```sh
python3 garmin_fhir.py map      # writes data/fhir/days/YYYY/YYYY-MM-DD.ndjson
python3 garmin_fhir.py load     # upserts changed days into the local Medplum
./run-daily.sh                  # crawl, map and load in one go (for cron)
```

A local Medplum server (Docker, pinned to 5.2.17, localhost only) is set up by
[medplum/README.md](medplum/README.md).

The mapping rules, custom codes and decisions are in
[docs/fhir-mapping.md](docs/fhir-mapping.md). Settings:
`GARMIN_FHIR_PATIENT_NAME`, `GARMIN_FHIR_BASE`.

## Logs

Log lines go to the terminal and to `data/logs/garmin-archive.log` (rotated at
5 MB, 5 files kept). Each crawled day, each retry, each failure and a final
summary line are logged. The key and token are never logged.

## Documentation

- [docs/data-format.md](docs/data-format.md): folder layout, every file, manifest fields.
- [docs/operations.md](docs/operations.md): rclone setup, scheduling, key renewal, restore, troubleshooting.
- [docs/manifest.schema.json](docs/manifest.schema.json): JSON Schema of `manifest.json`.
- [docs/docker.md](docs/docker.md): running everything in Docker (backfill, daily schedule, cloud backup, FHIR load).
- [docs/agents.md](docs/agents.md) also covers PDF reports (`tools/garmin_report.py`): an activity report with second-by-second charts, and weekly/monthly wellness reports.
- [docs/agents.md](docs/agents.md): connecting agents (Claude Code MCP via `.mcp.json`, GraphQL, read-only access) and the analysis skill.
- [docs/fhir-mapping.md](docs/fhir-mapping.md): mapping of the archive to FHIR R4, with the `garmin-metrics` CodeSystem in [fhir/](fhir/).
- [AGENTS.md](AGENTS.md) and [.claude/skills/garmin-archive/SKILL.md](.claude/skills/garmin-archive/SKILL.md): instructions for AI agents that operate the tool or read the data.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The tests run the real command against a local stub of the API. They need no
network and no key.
