# Running in Docker

One image runs everything: the Garmin crawler (backfill and daily), the cloud
backup (Google Drive, S3 Glacier), the FHIR mapping and the load into the local
Medplum, plus a scheduler that triggers it every day. The root
`docker-compose.yml` starts it next to the Medplum stack from `medplum/`.

| Piece | File |
|---|---|
| Image (Python 3.12 slim, rclone 1.76.0, tzdata, non-root user `app`) | `Dockerfile` |
| Commands inside the container | `docker/entrypoint.sh` |
| Daily scheduler | `garmin_scheduler.py` |
| Services, mounts, settings | `docker-compose.yml` (includes `medplum/docker-compose.yml`) |

Nothing secret is in the image: `.env`, `data/`, `.git` and the Medplum folder
are excluded from the build context (`.dockerignore`). Credentials are passed
at runtime.

## What the container needs

| What | How | Notes |
|---|---|---|
| Garmin key, targets, Medplum clients | `env_file: .env` | The same `.env` as on the host. The compose file overrides the host-only values (`GARMIN_ARCHIVE_DIR`, `GARMIN_RCLONE_BIN`, `GARMIN_MEDPLUM_URL`). |
| Data directory | `./data` mounted at `/data` | Raw archive, manifests, FHIR output, logs. Change with `GARMIN_DATA_DIR`. |
| rclone config | `~/.config/rclone` mounted at `/config/rclone`, **writable** | rclone writes renewed Google Drive tokens back. Change with `RCLONE_CONFIG_DIR`. With encryption on, the crypt remotes and their key are in this file too. |
| S3 credentials | `docker/secrets.env` | See below. |
| Medplum | same compose project | Reached as `http://medplum-server:8103`. |

### S3 credentials for the container

On macOS the AWS profile may read its key from the Keychain
(`credential_process = /usr/bin/security ...`), which a Linux container cannot
use (`error in credential_process: exit status 127`). Give the container its own
key, limited to the backup bucket:

```sh
docker/create-s3-key.sh <bucket> <aws-profile>          # e.g. garmin-archive-<account-id> iam_<you>
```

It creates the IAM user `garmin-archive-backup` with a policy for that one
bucket (list, put, get, delete, restore, multipart) and writes its key to
`docker/secrets.env` (mode 600, ignored by git) without printing it. The file
sets `RCLONE_CONFIG_S3GARMIN_*` variables, which override the `s3garmin`
remote inside the container only; the host keeps using its own profile. To
write the file by hand, copy `docker/secrets.env.example`.

## Start

```sh
docker compose up -d --build                    # Medplum + garmin-archive scheduler
docker compose logs -f garmin-archive
```

On start the scheduler runs the full pipeline once (`GARMIN_RUN_ON_START=true`):
on an empty data directory that is the backfill; otherwise it catches up on
missed days and pending uploads. It then runs every day at
`GARMIN_SCHEDULE_TIME` (default `06:15`, several times allowed: `06:15,18:15`)
in `GARMIN_TZ` (an IANA zone such as `Asia/Bangkok`; default `UTC`). Put these in the repository's `.env`
(Compose reads it for the variables in `docker-compose.yml`) or export them in
the shell.

Sleep is dated by the morning it ends, and the crawler normally stops at
yesterday, so last night's sleep would arrive a day later. For it the same
morning, set `GARMIN_INCLUDE_TODAY=true` and schedule a morning run, for
example `GARMIN_SCHEDULE_TIME=07:00,22:00`: the 07:00 run fetches last night's
sleep, the 22:00 run fills in the rest of the day. Today stays provisional
(local and in Medplum only) and is uploaded once settled.

Each run is `run-daily.sh`: crawl, archive, upload (one batched rclone call per
target), map, load. The exit code is logged and saved in
`data/scheduler-status.json` (`last_exit_code`, `last_result`, `next_run`).
The container health check fails if the scheduler stops writing its heartbeat.

## One-off commands

```sh
docker compose run --rm garmin-archive run              # one full pipeline run now, then exit
docker compose run --rm garmin-archive status
docker compose run --rm garmin-archive crawl --from 2026-03-15 --no-upload
docker compose run --rm garmin-archive restore
docker compose run --rm garmin-archive map --force
docker compose run --rm garmin-archive load
docker compose run --rm garmin-archive rclone lsf gdrive:Backups/garmin
```

Do not run a one-off command that crawls while the scheduler is running a job:
the data directory has a lock file, but locks are not reliable across the
macOS/Linux boundary of a bind mount. Stop the scheduler first
(`docker compose stop garmin-archive`). For the same reason, run the pipeline
either from the container or from the host's cron/launchd, not both.

## Stop, update, remove

```sh
docker compose stop garmin-archive          # a running job gets 25 s to stop; the next run resumes it
docker compose up -d --build garmin-archive # after pulling new code
docker compose down                         # stops everything; data and Medplum volumes stay
```

`docker compose down -v` also deletes the Medplum volumes (all loaded FHIR
data). The raw archive in `data/` and the cloud copies are not affected, and
`run` reloads Medplum from the archive.

The root compose file uses the project name `garmin-medplum`, the same as
`medplum/docker-compose.yml`, so both manage the same Medplum containers and
volumes. Do not change it: a different name would start a second, empty Medplum.

## On another machine or a server

1. Install Docker; clone the repository.
2. Copy `.env` and `medplum/.env` (or run `python3 medplum/setup.py init` and,
   once Medplum is up, `python3 medplum/setup.py project`).
3. Configure rclone there (`rclone config create gdrive drive scope=drive.file`,
   `rclone config create s3garmin s3 provider=AWS ...`) or copy `rclone.conf`.
4. Create `docker/secrets.env` (above).
5. `docker compose up -d --build`. With an empty `data/`, the first run adopts
   everything the targets already hold (see `docs/operations.md`, "New machine")
   and crawls only what is missing; `docker compose run --rm garmin-archive restore`
   brings the raw files back from Google Drive.

On Linux, files in the bind-mounted `data/` are owned by uid 1000. To use your
own uid, build with `--build-arg APP_UID=$(id -u) --build-arg APP_GID=$(id -g)`.
