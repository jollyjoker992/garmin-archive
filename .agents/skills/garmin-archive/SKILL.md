---
name: garmin-archive
description: Operate the garmin-archive backup - crawl missing Garmin days, check backup and upload status, fix failed days or uploads, restore cloud-only days. Use when the user asks about the backup itself. NOT for questions about the health or workout data (sleep, HRV, heart rate, workouts, FIT files): those go to the garmin-health-analysis skill, which reads the data through Medplum.
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
- Ask before `--refetch` on a day that is already uploaded. For days older
  than 90 days it is refused (exit 2); do not add `--allow-old-refetch`
  yourself: Garmin no longer serves their intraday detail. To redo FHIR output,
  run `python3 garmin_fhir.py map --force` and `load`, which only read the archive.

## Questions about the health data

Do not answer them from `data/`. Use the **garmin-health-analysis** skill: it
reads the data through Medplum with the read-only client, and gets workout
detail with `python3 tools/medplum_agent.py fit ...`. The raw folder is for
operating and debugging the backup only (layout in `docs/data-format.md`).

The data is personal health data. Keep it on this machine: do not paste raw
files into external services.
