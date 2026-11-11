# Agent guide: garmin-archive

Read this before operating the tool or reading its data. Human documentation:
`README.md`, `docs/data-format.md`, `docs/operations.md`.

## What this is

One Python file, `garmin_archive.py` (standard library only, Python 3.10+). It
backs up the owner's Garmin Connect data through Garmin's official Personal API
into raw per-day folders, compresses settled days and uploads them with rclone.
State lives in one file: `<data dir>/manifest.json`.

The data directory is `GARMIN_ARCHIVE_DIR`, default `./data`. It holds personal
health data and is not in git.

## Safe commands

```sh
python3 garmin_archive.py status --json     # read-only, no network
python3 garmin_archive.py run --dry-run     # read-only, no network
python3 garmin_archive.py run --no-upload   # calls Garmin, writes local files only
python3 garmin_archive.py run               # syncs with the targets' manifests, crawls, uploads
python3 garmin_archive.py restore           # downloads cloud-only days (no Garmin calls)
python3 -m unittest discover -s tests -v    # offline tests
```

`status --json` returns:

```json
{
 "data_dir": "/abs/path/data",
 "range": {"start": "2026-03-15", "end": "2026-11-10", "days": 241},
 "final": 238, "provisional": 1,
 "failed": ["2026-06-02"],
 "never_fetched": ["2026-05-01"],
 "not_archived": [],
 "pending_uploads": {"gdrive": [], "s3": ["2026-11-08"]},
 "local_bytes": 12345678,
 "last_run": {"finished_at": "...", "exit_code": 0, "days_crawled": 2, "days_failed": 0, "requests": 41}
}
```

Exit codes of `run`: `0` complete, `1` partial (retried next run), `2`
configuration or rejected API key (needs a person), `3` another run is active.

## Reading the data

- The manifest on the upload targets is authoritative for what is backed up;
  `run` merges it into the local one first. A day with `adopted_from` exists
  only in the cloud until `restore` (status lists it under `cloud_only`).
- To find a day: `manifest.json` → `days["YYYY-MM-DD"]`. Use it only when
  `status` is `final` or `provisional`; `path` is the folder, `endpoints` lists
  which files exist.
- Files are Garmin's raw responses. File names, endpoints and meanings are
  tabulated in `docs/data-format.md`. Do not assume a field exists: days
  without a measurement return `[]`, `{"data":[]}`, all-`null` objects, or no
  file at all (`"http": 204`).
- A day is a calendar date in the account's local time. `sleep.json` for date
  `D` is the night ending on the morning of `D`.
- `profile_settings.json` and `heart_rate_zones.json` are snapshots taken at
  crawl time, not historical values.
- Activities: `activities.json` is the list for the day;
  `activities/<id>/original.zip` contains the FIT file with per-second samples.
- If the day folder is gone but `archive` exists, extract
  `<data dir>/<archive.file>`; it unpacks to `YYYY-MM-DD/`.

## FHIR

`python3 garmin_fhir.py map` (local only, no network) writes FHIR R4 NDJSON to
`<data dir>/fhir/days/YYYY/YYYY-MM-DD.ndjson`, with `fhir/shared.ndjson`
(Patient, Device) and `fhir/manifest.json`. `docs/fhir-mapping.md` specifies the mapping. Values with no
correct LOINC code use the CodeSystem in `fhir/CodeSystem-garmin-metrics.json`
(system `https://github.com/jollyjoker992/garmin-archive/fhir/CodeSystem/garmin-metrics`).
Read a code's `definition` and `unit` property before interpreting its value.
Never substitute LOINC 80404-7 / 112429-6 for HRV or 93038-8 for stress: the
mapping explains why. To add a metric: add the concept to the CodeSystem (with
definition, unit and garminField), map it in `garmin_fhir.py`, bump
`MAPPING_VERSION` and the CodeSystem `version` together, and validate with
`tools/fhir-validate`.

## Medplum (local)

`medplum/` runs Medplum 5.2.17 in Docker on localhost (FHIR API
http://localhost:8103/fhir/R4, MCP http://localhost:8103/mcp/stream). See
`medplum/README.md`. To read the data as an agent, get a token with the
**agent** client (`GARMIN_MEDPLUM_AGENT_CLIENT_ID` / `_SECRET` in `.env`,
client-credentials grant); it is read-only. Use the loader client only for
loading (`python3 garmin_fhir.py load`, or `./run-daily.sh` for crawl + map + load).
Look up a custom code with
`GET CodeSystem/$lookup?system=<garmin-metrics url>&code=<code>`.

To analyse the data, follow `.claude/skills/garmin-health-analysis/SKILL.md`
and use `python3 tools/medplum_agent.py` (read-only: `graphql`, `codes`,
`token`). MCP setup for Claude Code (`.mcp.json`) is in `docs/agents.md`.

Date searches must carry the account's UTC offset, e.g.
`date=ge2026-11-07T00:00:00%2B07:00` for UTC+7 (read it from any
`effectivePeriod.start`); Medplum reads a bare date as UTC and then misses or
shifts days. Query examples are in `docs/fhir-mapping.md`
("Querying the loaded data").

## Docker

`Dockerfile` + root `docker-compose.yml` run the pipeline in a container with a
daily scheduler (`garmin_scheduler.py`); see `docs/docker.md`. The compose
project name must stay `garmin-medplum` (same Medplum volumes). Never run a
crawl on the host while the container's scheduler runs a job.

## Rules

- Never print, log, commit or send `GARMIN_PERSONAL_API_KEY`, `.env` or
  `data/.token.json`. Never commit anything under `data/`.
- Do not edit `manifest.json` by hand. To redo a day, use `run --refetch DATE`.
- Do not call Garmin in parallel or lower `GARMIN_MIN_INTERVAL` to speed up a
  backfill. Rate limits are unpublished; the pacing is deliberate.
- `--refetch` on an uploaded day rewrites its object on S3 Glacier, which can
  incur an early-deletion charge. Ask the owner first.
- Do not switch to the unofficial `garminconnect` / `garth` libraries. They log
  in with the account password and get accounts rate-limited.
- Uploading sends health data to external services. Only use the remotes the
  owner configured.
- Never print or commit `medplum/.env` or the `GARMIN_MEDPLUM_*` secrets.
  `docker compose down -v` deletes all loaded data; ask first.

## Changing the code

- Endpoints are the tables `DAILY_ENDPOINTS` and `SNAPSHOT_ENDPOINTS` at the top
  of `garmin_archive.py`. Adding a row adds a file to every day crawled from
  then on; document it in `docs/data-format.md`. Old days are not re-crawled.
- All HTTP goes through `Client._send` (throttle, retry, backoff). Keep it that way.
- A change to the manifest layout needs a new `MANIFEST_SCHEMA_VERSION`, a
  migration, and an update to `docs/manifest.schema.json`.
- Keep it standard-library only. Run the tests before committing.
