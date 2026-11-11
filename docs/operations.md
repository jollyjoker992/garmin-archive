# Operations

## The Garmin API key

1. Sign in to Garmin Connect on the web and open
   <https://connect.garmin.com/app/settings/personalApiToken>.
2. Create a key. Choose the longest expiry (one year). Garmin shows the key once.
3. Put it in `.env` as `GARMIN_PERSONAL_API_KEY=...`, or export it. Keep `.env`
   at mode `600`. It is in `.gitignore`.

The tool exchanges the key for a bearer token that lasts 24 hours and caches it
in `data/.token.json` (mode `600`). There are no refresh tokens; a new token is
requested when the cached one has less than an hour left.

**Renewal is manual.** A key cannot be renewed by a script. Garmin emails about
a week before expiry. When the key stops working, runs exit with code `2` and
log `Garmin rejected the Personal API key`. Create a new key, update `.env`,
and run again. Days missed in between are filled automatically.

## Upload targets

Uploads use rclone. Install it (`brew install rclone`, or see
<https://rclone.org/install/>). A target is skipped when its variable is unset.

### Google Drive

```sh
rclone config create gdrive drive scope=drive.file
```

A browser window opens for the Google sign-in. The `drive.file` scope lets
rclone see only the files it created. On a machine without a browser, run
`rclone authorize "drive"` on another machine and paste the result, as rclone
prompts. Then:

```sh
GARMIN_GDRIVE_REMOTE=gdrive:Backups/garmin
```

### S3 Glacier

Create a bucket, and an IAM user or role limited to it. Bucket names are global
across all AWS accounts; a common name is usually taken (`BucketAlreadyExists`,
and then `AccessDenied` on anything you try with it, because it belongs to
someone else). Adding your account id makes it unique, e.g.
`garmin-archive-<account-id>`. Block public access on it:

```sh
aws s3api create-bucket --bucket <bucket> --region <region> --create-bucket-configuration LocationConstraint=<region>
aws s3api put-public-access-block --bucket <bucket> --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```


```json
{
 "Version": "2012-10-17",
 "Statement": [
  {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": "arn:aws:s3:::my-bucket"},
  {"Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject"], "Resource": "arn:aws:s3:::my-bucket/garmin/*"}
 ]
}
```

```sh
rclone config create s3garmin s3 provider=AWS env_auth=true profile=<aws-profile> region=ap-southeast-1 no_check_bucket=true
```

`env_auth=true` makes rclone use the standard AWS credentials of that profile
in `~/.aws/credentials` (or `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`). No
secret is stored in the rclone config. Check write access before the first run:

```sh
rclone touch s3garmin:<bucket>/garmin/.write-test && rclone lsf s3garmin:<bucket>/garmin \
  && rclone deletefile s3garmin:<bucket>/garmin/.write-test
```

Then:

```sh
GARMIN_S3_REMOTE=s3garmin:my-bucket/garmin
GARMIN_S3_STORAGE_CLASS=GLACIER
```

Storage classes:

| Class | Retrieval | Minimum storage charge |
|---|---|---|
| `GLACIER` (Flexible Retrieval) | minutes to hours, after a restore request | 90 days |
| `DEEP_ARCHIVE` | about 12 hours, after a restore request | 180 days |
| `GLACIER_IR` (Instant Retrieval) | immediate | 90 days |

Archives are uploaded once, when a day is final, so the minimum-storage charge
only matters if you use `--refetch` on a day that is already uploaded.
`manifest.json` is uploaded on every run with class `STANDARD`.

A day without an activity compresses to about 10 KB; a day with a run is about
100 KB. A year is in the range of 10 to 30 MB.

### Check the targets

```sh
rclone lsf gdrive:Backups/garmin/archives/2026/ | tail
rclone lsf s3garmin:my-bucket/garmin/archives/2026/ | tail
python3 garmin_archive.py status
```

## Scheduling

The simplest option is the Docker scheduler (`docs/docker.md`): it runs the
whole pipeline at `GARMIN_SCHEDULE_TIME` every day. Use one scheduler only:
Docker, or cron/launchd on the host below.

Run once a day. The run that happens on day `D` crawls `D-1` as provisional and
finalises `D-2`. A missed run needs no action: the next one fills the gap.

To also keep the local Medplum current, schedule `./run-daily.sh` instead of
`garmin_archive.py`: it crawls, maps and loads, and exits with the worst code
of the three steps. Medplum must be running (`docker compose up -d` in
`medplum/`); if it is not, the load step fails with code 1 and the next run
loads what was missed.

### cron

```cron
15 6 * * * cd /path/to/garmin-archive && /usr/bin/python3 garmin_archive.py >> data/logs/cron.out 2>&1
# or, with the FHIR pipeline:
15 6 * * * /path/to/garmin-archive/run-daily.sh >> /path/to/garmin-archive/data/logs/cron.out 2>&1
```

Cron has a minimal `PATH`. If rclone is not found, set
`GARMIN_RCLONE_BIN=/opt/homebrew/bin/rclone` in `.env`.

### launchd (macOS)

Save as `~/Library/LaunchAgents/com.user.garmin-archive.plist`, fix the two
paths, then `launchctl load ~/Library/LaunchAgents/com.user.garmin-archive.plist`.
Unlike cron, launchd runs a missed job after the Mac wakes up.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.user.garmin-archive</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/path/to/garmin-archive/garmin_archive.py</string>
  </array>
  <key>WorkingDirectory</key><string>/path/to/garmin-archive</string>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>6</integer><key>Minute</key><integer>15</integer></dict>
  <key>StandardOutPath</key><string>/path/to/garmin-archive/data/logs/launchd.out</string>
  <key>StandardErrorPath</key><string>/path/to/garmin-archive/data/logs/launchd.out</string>
</dict>
</plist>
```

### Alerting

The exit code is the signal: `1` means something will be retried, `2` means a
person must act (usually a new API key). `python3 garmin_archive.py status --json`
gives `failed`, `never_fetched`, `pending_uploads` and `last_run` for a monitor.

## First backfill

```sh
python3 garmin_archive.py run --dry-run      # check the range
python3 garmin_archive.py run                # crawl it
```

About 24 seconds per day at the default pace (`GARMIN_MIN_INTERVAL=1.0`), about
12 seconds with `GARMIN_MIN_INTERVAL=0.25`. Measured on 2026-11-11: 234 days
crawled in 54 minutes at 0.25 s with no HTTP 429 (4,136 requests); the upload
of 240 archives then took about 2 minutes to S3 and, batched, about 15 minutes
to Google Drive; mapping and loading into Medplum about 1 minute. In Docker,
`docker compose up -d` runs the backfill on start (`docs/docker.md`). To spread
a long backfill over
several nights, set `GARMIN_MAX_DAYS_PER_RUN=60`; each run then takes the 60
newest missing days. Days before you owned a device still cost about 20
requests each and produce empty files, so set `GARMIN_START_DATE` to your first
day of data.

## Restore

```sh
# Google Drive
rclone copy gdrive:Backups/garmin/archives/2026 ./restore/2026

# S3 Glacier: request a restore first, wait, then copy
rclone backend restore s3garmin:my-bucket/garmin/archives/2026 -o priority=Bulk -o lifetime=7
rclone copy s3garmin:my-bucket/garmin/archives/2026 ./restore/2026

tar xzf restore/2026/2026-09-30.tar.gz        # gives ./2026-09-30/
shasum -a 256 restore/2026/2026-09-30.tar.gz  # compare with archive.sha256 in the manifest
```

## New machine, or lost data folder

The manifest on the upload targets is the record of what is backed up.

```sh
cp .env.example .env                        # same key, same GARMIN_GDRIVE_REMOTE / GARMIN_S3_REMOTE
python3 garmin_archive.py run               # adopts every day the targets hold; crawls only the rest
python3 garmin_archive.py status            # "Only in cloud" lists the adopted days
python3 garmin_archive.py restore           # optional: bring their raw files back (from Google Drive)
```

Adopted days are neither crawled from Garmin nor uploaded again, which also
avoids rewriting objects in Glacier. They have no local folder until
`restore`, so `garmin_fhir.py map` skips them with a warning until then.
`restore` reads from Google Drive; objects in S3 Glacier need an S3 restore
request first (see "Restore" above).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Exit `2`, `GARMIN_PERSONAL_API_KEY is not set` | The variable is missing. With cron, check the working directory holds `.env`, or pass `--env-file`. |
| Exit `2`, `Garmin rejected the Personal API key` | The key expired or was deleted. Create a new one. |
| `HTTP 429 ... retrying` | Garmin is rate limiting. Each request backs off (15 s up to 4 min); nothing to do. |
| `pausing the crawl for 300 s, then retrying this day` | A request stayed rate limited; the crawl cools down (5, 10, 20, 30 min) and continues with the same day. |
| `Garmin kept rate limiting after 4 cool-downs; stopping the crawl` | Wait; the next run resumes from the manifest. Raise `GARMIN_MIN_INTERVAL` or set `GARMIN_MAX_DAYS_PER_RUN`. |
| A day stays `failed` | Read its `error` in the manifest. If one endpoint keeps returning 5xx, add it to `GARMIN_SKIP_ENDPOINTS`. |
| `Cannot read the state of gdrive (... rateLimitExceeded)` | Google Drive throttles rclone's shared client id, mostly right after setup. The read is retried with backoff; if it persists, create your own Google client id (https://rclone.org/drive/#making-your-own-client-id). |
| `BucketAlreadyExists`, then `AccessDenied` | The S3 bucket name belongs to another AWS account. Pick a unique name (see "S3 Glacier") and update `GARMIN_S3_REMOTE`. |
| `rclone binary 'rclone' not found` | Install rclone or set `GARMIN_RCLONE_BIN` to its full path. |
| `Uploaded 200 of 224 archive(s) to gdrive; the rest is retried next run (...)` | The batched upload did not complete; the rclone error is in brackets. Test with `rclone lsf <remote>`. Only archives found on the target are recorded, so nothing is marked uploaded by mistake. |
| `Copy of ... from another target to ... failed` | A local archive was missing and copying it from the other target failed; it is retried next run. |
| Data for a final day looks incomplete (watch synced very late) | `python3 garmin_archive.py run --refetch YYYY-MM-DD`. For a habit of late syncs, raise `GARMIN_SETTLE_DAYS`. |
| `is final but its folder ... is gone` | The day folder was deleted before it was archived. Use `--refetch` for that day. |
| `manifest.json` lost | Nothing to do: the next run reads the manifest from the targets and adopts what they hold. Without targets, a new run crawls the whole range again. |
| `Cannot read the state of gdrive/s3` | The target could not be read at the start of the run (network, credentials). The run continues on the local manifest and exits 1; check with `rclone lsf <remote>`. |
| `recorded as uploaded ... but ... is not there` | The archive was deleted on the target. It is uploaded again in the same run. |
| Exit `3` | Another run is still going. A backfill can take over an hour. |

Deleting a day folder after it is archived is safe. Deleting the local archive
of an uploaded day is safe too; the manifest still records the upload.
