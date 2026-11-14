#!/usr/bin/env python3
"""garmin-archive: back up raw Garmin Connect data, one folder per day.

Crawls the Garmin Personal API, stores every raw response under
``days/YYYY/YYYY-MM-DD/``, compresses settled days to
``archives/YYYY/YYYY-MM-DD.tar.gz`` and uploads them with rclone to Google
Drive and S3 (Glacier). ``manifest.json`` records what was fetched, archived
and uploaded, so every run fills whatever is still missing.

Standard library only. See README.md and docs/ for details.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import fcntl
import getpass
import gzip
import hashlib
import http.client
import json
import logging
import logging.handlers
import os
import random
import secrets
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path

VERSION = "1.0.0"
MANIFEST_SCHEMA_VERSION = 1
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
USER_AGENT = f"garmin-archive/{VERSION}"

# One request per endpoint per day. ``{d}`` is the calendar date (YYYY-MM-DD).
# The response body is stored unmodified as ``<name>.json``.
DAILY_ENDPOINTS: list[tuple[str, str]] = [
    ("daily_summary", "/usersummary-service/v2/dailysummaries?startDate={d}&endDate={d}"),
    ("steps", "/usersummary-service/stats/steps/daily/{d}/{d}"),
    ("stress", "/usersummary-service/stats/stress/daily/{d}/{d}"),
    ("hydration", "/usersummary-service/stats/hydration/daily/{d}/{d}"),
    ("sleep", "/sleep-service/sleep/dailySleep?date={d}"),
    ("sleep_summary", "/sleep-service/sleep/dailySleepSummariesByDate?startDate={d}&endDate={d}"),
    ("body_battery", "/wellness-service/wellness/bodyBattery/reports/daily?startDate={d}&endDate={d}"),
    ("heart_rate", "/wellness-service/wellness/dailyHeartRate?date={d}"),
    ("hrv", "/hrv-service/hrv/{d}"),
    ("hrv_summary", "/hrv-service/hrv/daily/{d}/{d}"),
    ("spo2", "/wellness-service/wellness/daily/spo2acclimation/{d}"),
    ("fitness_age", "/fitnessage-service/fitnessage/v2/{d}"),
    ("weight", "/weight-service/weight/range/{d}/{d}?includeAll=true"),
    ("weight_goals", "/goal-service/goal/user/effective/weightgoal/{d}/{d}"),
    ("food_logs", "/nutrition-service/food/logs/{d}"),
    ("wellness_activity", "/wellnessactivity-service/activity/summary/range/{d}/{d}"),
    ("menstrual_cycle_summary", "/periodichealth-service/menstrualcycle/summary/{d}"),
    ("menstrual_cycle_day_view", "/periodichealth-service/menstrualcycle/dayview/{d}?todayDate={d}"),
]

# Current-state endpoints with no date parameter. Fetched once per run and
# written into every day folder crawled in that run (a snapshot taken at crawl
# time, not the historical value for that day).
SNAPSHOT_ENDPOINTS: list[tuple[str, str]] = [
    ("profile_settings", "/userprofile-service/userprofile/settings"),
    ("heart_rate_zones", "/biometric-service/heartRateZones/v2"),
]

ACTIVITY_SEARCH = "/activitylist-service/activities/search/activities?start={start}&limit={limit}&startDate={d}&endDate={d}"
ACTIVITY_PAGE_SIZE = 100
ACTIVITY_DETAIL = "/activity-service/activity/{id}"
ACTIVITY_WORKOUTS = "/activity-service/activity/{id}/workouts"
ACTIVITY_DOWNLOAD = "/download-service/files/activity/{id}"

EXIT_OK, EXIT_PARTIAL, EXIT_CONFIG, EXIT_LOCKED = 0, 1, 2, 3
RATE_LIMIT_BACKOFF_CAP = 600.0      # longest wait between two attempts of one request after HTTP 429
RATE_LIMIT_COOLDOWN_CAP = 1800.0    # longest pause of the whole crawl after a request stays rate limited

log = logging.getLogger("garmin-archive")


class ConfigError(Exception):
    """Bad or missing configuration."""


class AuthError(Exception):
    """The API key or bearer token was rejected."""


class FetchError(Exception):
    """A request failed after all retry attempts."""

    def __init__(self, message: str, rate_limited: bool = False):
        super().__init__(message)
        self.rate_limited = rate_limited


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ without overriding existing vars."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _env_num(name: str, default: float, cast=float):
    raw = _env(name)
    if not raw:
        return cast(default)
    try:
        value = cast(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not a number") from exc
    if value < 0:
        raise ConfigError(f"{name} must not be negative")
    return value


def parse_date(value: str, name: str = "date") -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError(f"{name}={value!r} is not a YYYY-MM-DD date") from exc


@dataclass
class Config:
    api_key: str
    data_dir: Path
    start_date: dt.date
    settle_days: int
    min_interval: float
    max_attempts: int
    backoff_base: float
    backoff_max: float
    rate_limit_backoff: float
    rate_limit_cooldown: float
    rate_limit_cooldowns: int
    timeout: float
    max_days_per_run: int
    include_today: bool
    refetch_max_age_days: int
    skip_endpoints: set[str]
    gdrive_remote: str
    s3_remote: str
    s3_storage_class: str
    rclone_bin: str
    require_encryption: bool
    base_url: str
    log_level: str
    today: dt.date = field(default_factory=dt.date.today)

    @classmethod
    def from_env(cls) -> "Config":
        today = dt.date.today()
        start = _env("GARMIN_START_DATE")
        known = {name for name, _ in DAILY_ENDPOINTS + SNAPSHOT_ENDPOINTS} | {"activities"}
        skip = {s.strip() for s in _env("GARMIN_SKIP_ENDPOINTS").split(",") if s.strip()}
        if skip - known:
            raise ConfigError(
                f"GARMIN_SKIP_ENDPOINTS has unknown names: {sorted(skip - known)}; known: {sorted(known)}"
            )
        cfg = cls(
            api_key=_env("GARMIN_PERSONAL_API_KEY"),
            data_dir=Path(_env("GARMIN_ARCHIVE_DIR", str(Path(__file__).resolve().parent / "data"))).expanduser(),
            start_date=parse_date(start, "GARMIN_START_DATE") if start else today - dt.timedelta(days=365),
            settle_days=_env_num("GARMIN_SETTLE_DAYS", 2, int),
            min_interval=_env_num("GARMIN_MIN_INTERVAL", 1.0),
            max_attempts=_env_num("GARMIN_MAX_ATTEMPTS", 5, int),
            backoff_base=_env_num("GARMIN_BACKOFF_BASE", 2.0),
            backoff_max=_env_num("GARMIN_BACKOFF_MAX", 120.0),
            rate_limit_backoff=_env_num("GARMIN_RATE_LIMIT_BACKOFF", 30.0),
            rate_limit_cooldown=_env_num("GARMIN_RATE_LIMIT_COOLDOWN", 300.0),
            rate_limit_cooldowns=_env_num("GARMIN_RATE_LIMIT_COOLDOWNS", 4, int),
            timeout=_env_num("GARMIN_TIMEOUT", 30.0),
            max_days_per_run=_env_num("GARMIN_MAX_DAYS_PER_RUN", 0, int),
            include_today=_env("GARMIN_INCLUDE_TODAY", "false").lower() in ("1", "true", "yes"),
            refetch_max_age_days=_env_num("GARMIN_REFETCH_MAX_AGE_DAYS", 90, int),
            skip_endpoints=skip,
            gdrive_remote=_env("GARMIN_GDRIVE_REMOTE").rstrip("/"),
            s3_remote=_env("GARMIN_S3_REMOTE").rstrip("/"),
            s3_storage_class=_env("GARMIN_S3_STORAGE_CLASS", "GLACIER"),
            rclone_bin=_env("GARMIN_RCLONE_BIN", "rclone"),
            require_encryption=_env("GARMIN_REQUIRE_ENCRYPTION", "false").lower() in ("1", "true", "yes"),
            base_url=_env("GARMIN_BASE_URL", "https://personal.connectapi.garmin.com").rstrip("/"),
            log_level=_env("GARMIN_LOG_LEVEL", "INFO").upper(),
            today=today,
        )
        if cfg.max_attempts < 1:
            raise ConfigError("GARMIN_MAX_ATTEMPTS must be at least 1")
        return cfg

    @property
    def manifest_path(self) -> Path:
        return self.data_dir / "manifest.json"

    def day_dir(self, day: str) -> Path:
        return self.data_dir / "days" / day[:4] / day

    def archive_rel(self, day: str) -> str:
        return f"archives/{day[:4]}/{day}.tar.gz"

    def targets(self) -> dict[str, str]:
        """Configured upload targets: name -> rclone remote path."""
        out = {}
        if self.gdrive_remote:
            out["gdrive"] = self.gdrive_remote
        if self.s3_remote:
            out["s3"] = self.s3_remote
        return out


def setup_logging(cfg: Config) -> None:
    level = getattr(logging, cfg.log_level, None)
    if not isinstance(level, int):
        raise ConfigError(f"GARMIN_LOG_LEVEL={cfg.log_level!r} is not a log level")
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%dT%H:%M:%S%z")
    log.setLevel(level)
    for handler in list(log.handlers):
        log.removeHandler(handler)
        handler.close()
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    log.addHandler(stream)
    log_dir = cfg.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    rotating = logging.handlers.RotatingFileHandler(
        log_dir / "garmin-archive.log", maxBytes=5_000_000, backupCount=5
    )
    rotating.setFormatter(fmt)
    log.addHandler(rotating)


# --------------------------------------------------------------------------
# HTTP client: throttling, retries with backoff, token handling
# --------------------------------------------------------------------------


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def jwt_expiry(token: str) -> float:
    """Return the ``exp`` claim of a JWT as a unix timestamp, or 0 if unreadable."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except (IndexError, KeyError, ValueError, TypeError):
        return 0.0


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
        return max(0.0, (when - dt.datetime.now(when.tzinfo)).total_seconds())
    except (TypeError, ValueError):
        return None


def backoff_delay(attempt: int, base: float, cap: float, retry_after: float | None = None) -> float:
    """Exponential backoff with jitter; a server Retry-After wins if longer."""
    delay = min(cap, base * (2 ** (attempt - 1)))
    delay *= random.uniform(0.5, 1.0)
    if retry_after is not None:
        delay = max(delay, min(retry_after, 900.0))
    return delay


class Client:
    """Throttled, retrying Garmin Personal API client."""

    def __init__(self, cfg: Config, sleep=time.sleep):
        self.cfg = cfg
        self._sleep = sleep
        self._next_ok = 0.0
        self._token = ""
        self._token_exp = 0.0
        self.requests = 0
        self._key_hash = hashlib.sha256(cfg.api_key.encode()).hexdigest()
        self._token_file = cfg.data_dir / ".token.json"

    # -- pacing ------------------------------------------------------------

    def _throttle(self) -> None:
        wait = self._next_ok - time.monotonic()
        if wait > 0:
            self._sleep(wait)
        interval = self.cfg.min_interval
        self._next_ok = time.monotonic() + interval + random.uniform(0, interval * 0.25)

    # -- token -------------------------------------------------------------

    def token(self, force: bool = False) -> str:
        if not force and not self._token:
            self._load_cached_token()
        if not force and self._token and self._token_exp - time.time() > 3600:
            return self._token

        def build():
            return urllib.request.Request(
                self.cfg.base_url + TOKEN_PATH,
                method="POST",
                headers={"Authorization": f"ApiKey {self.cfg.api_key}", "User-Agent": USER_AGENT},
            )

        status, body = self._send(build, "token exchange", auth=False)
        if status in (401, 403, 404):
            # 404 is what Garmin answers for a deleted key ("Personal API Key with ID ... does not exist").
            detail = body.decode(errors="replace").strip()[:200]
            raise AuthError(
                f"Garmin rejected the Personal API key (HTTP {status}{': ' + detail if detail else ''}). "
                "It is wrong, deleted or expired; create a new key on Garmin Connect and update "
                "GARMIN_PERSONAL_API_KEY."
            )
        if status != 200 or not body.strip():
            raise FetchError(f"token exchange returned HTTP {status}")
        text = body.decode().strip()
        if text.startswith("{"):
            text = json.loads(text).get("access_token", "")
        if not text:
            raise FetchError("token exchange returned no token")
        self._token = text
        self._token_exp = jwt_expiry(text) or time.time() + 3600
        self._save_cached_token()
        log.info("Obtained bearer token, valid until %s",
                 dt.datetime.fromtimestamp(self._token_exp, dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"))
        return self._token

    def _load_cached_token(self) -> None:
        try:
            cached = json.loads(self._token_file.read_text())
            if cached.get("key_sha256") == self._key_hash:
                self._token, self._token_exp = cached["token"], float(cached["exp"])
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _save_cached_token(self) -> None:
        fd = os.open(self._token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"token": self._token, "exp": self._token_exp, "key_sha256": self._key_hash}, fh)

    # -- requests ----------------------------------------------------------

    def get(self, path: str) -> tuple[int, bytes]:
        """GET an API path. Returns (status, body) for 2xx and non-retryable 4xx."""

        def build():
            return urllib.request.Request(
                self.cfg.base_url + path,
                headers={"Authorization": f"Bearer {self.token()}", "User-Agent": USER_AGENT},
            )

        return self._send(build, path.split("?")[0])

    def _send(self, build, what: str, auth: bool = True) -> tuple[int, bytes]:
        refreshed = False
        attempt = 0
        while True:
            attempt += 1
            request = build()
            self._throttle()
            self.requests += 1
            status: int | None = None
            retry_after = None
            try:
                with urllib.request.urlopen(request, timeout=self.cfg.timeout) as response:
                    body = response.read()
                    log.debug("%s -> HTTP %s, %d bytes", what, response.status, len(body))
                    return response.status, body
            except urllib.error.HTTPError as exc:
                status = exc.code
                body = exc.read()
                retry_after = parse_retry_after(exc.headers.get("Retry-After"))
                if status == 401 and auth:
                    if refreshed:
                        raise AuthError(f"{what}: HTTP 401 even with a fresh bearer token") from exc
                    log.warning("%s: HTTP 401, obtaining a new bearer token", what)
                    refreshed = True
                    attempt -= 1
                    self.token(force=True)
                    continue
                if status != 429 and status < 500:
                    return status, body
                reason = f"HTTP {status}"
            except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
                reason = f"{type(exc).__name__}: {exc}"

            if attempt >= self.cfg.max_attempts:
                raise FetchError(f"{what}: {reason} after {attempt} attempts", rate_limited=status == 429)
            if status == 429:
                delay = backoff_delay(attempt, self.cfg.rate_limit_backoff, RATE_LIMIT_BACKOFF_CAP, retry_after)
            else:
                delay = backoff_delay(attempt, self.cfg.backoff_base, self.cfg.backoff_max, retry_after)
            log.warning("%s: %s (attempt %d/%d), retrying in %.1fs",
                        what, reason, attempt, self.cfg.max_attempts, delay)
            self._sleep(delay)


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------


def load_manifest(cfg: Config) -> dict:
    if cfg.manifest_path.exists():
        manifest = json.loads(cfg.manifest_path.read_text())
        if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            raise ConfigError(
                f"{cfg.manifest_path} has schema_version {manifest.get('schema_version')}, "
                f"this tool writes {MANIFEST_SCHEMA_VERSION}"
            )
        return manifest
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "tool_version": VERSION,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "last_run": None,
        "days": {},
    }


def save_manifest(cfg: Config, manifest: dict) -> None:
    manifest["updated_at"] = now_iso()
    manifest["tool_version"] = VERSION
    manifest["days"] = dict(sorted(manifest["days"].items()))
    tmp = cfg.manifest_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=1) + "\n")
    os.replace(tmp, cfg.manifest_path)


def date_range(start: dt.date, end: dt.date) -> list[str]:
    return [(start + dt.timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def is_settled(cfg: Config, day: str) -> bool:
    """A day is settled once it is at least ``settle_days`` in the past. Today is never settled."""
    age = (cfg.today - dt.date.fromisoformat(day)).days
    return age >= max(cfg.settle_days, 1)


def plan_days(cfg: Config, manifest: dict, start: dt.date, end: dt.date, refetch: set[str]) -> list[str]:
    """Days to crawl, newest first: never fetched, failed, provisional, or forced."""
    todo = [
        day
        for day in date_range(start, end)
        if day in refetch or manifest["days"].get(day, {}).get("status") != "final"
    ]
    todo.extend(sorted(refetch - set(todo)))
    return sorted(set(todo), reverse=True)


# --------------------------------------------------------------------------
# Crawling one day
# --------------------------------------------------------------------------


def crawl_day(client: Client, cfg: Config, day: str, snapshots: dict[str, tuple[int, bytes]]) -> dict:
    """Fetch everything for one calendar day into its folder.

    Returns the manifest fields describing the crawl. Raises FetchError or
    AuthError without touching an existing folder for that day.
    """
    final_dir = cfg.day_dir(day)
    tmp_dir = final_dir.with_name(f".tmp-{day}")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True)
    endpoints: dict[str, dict] = {}
    activities: list[dict] = []

    def store(name: str, rel: str, status: int, body: bytes) -> dict:
        record = {"http": status, "bytes": len(body) if 200 <= status < 300 else 0}
        if 200 <= status < 300 and body:
            path = tmp_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            record["file"] = rel
        elif status >= 400:
            log.warning("%s %s: HTTP %s, recorded as unavailable", day, name, status)
        return record

    try:
        for name, template in DAILY_ENDPOINTS:
            if name in cfg.skip_endpoints:
                continue
            status, body = client.get(template.format(d=day))
            endpoints[name] = store(name, f"{name}.json", status, body)

        for name, path in SNAPSHOT_ENDPOINTS:
            if name in cfg.skip_endpoints:
                continue
            if name not in snapshots:
                snapshots[name] = client.get(path)
            endpoints[name] = store(name, f"{name}.json", *snapshots[name])

        if "activities" not in cfg.skip_endpoints:
            listed: list[dict] = []
            pages: list[bytes] = []
            while True:
                status, body = client.get(
                    ACTIVITY_SEARCH.format(start=len(listed), limit=ACTIVITY_PAGE_SIZE, d=day)
                )
                if status != 200:
                    raise FetchError(f"{day} activity search: HTTP {status}")
                page = json.loads(body or b"[]")
                listed.extend(page)
                pages.append(body)
                if len(page) < ACTIVITY_PAGE_SIZE:
                    break
            raw_list = pages[0] if len(pages) == 1 else json.dumps(listed).encode()
            endpoints["activities"] = store("activities", "activities.json", 200, raw_list)
            for item in listed:
                aid = item["activityId"]
                record = {"id": aid, "type": (item.get("activityType") or {}).get("typeKey"), "files": {}}
                for label, template, filename in (
                    ("detail", ACTIVITY_DETAIL, "detail.json"),
                    ("workouts", ACTIVITY_WORKOUTS, "workouts.json"),
                    ("original", ACTIVITY_DOWNLOAD, "original.zip"),
                ):
                    status, body = client.get(template.format(id=aid))
                    record["files"][label] = store(
                        f"activity {aid} {label}", f"activities/{aid}/{filename}", status, body
                    )
                activities.append(record)

        fetched_at = now_iso()
        meta = {
            "date": day,
            "fetched_at": fetched_at,
            "tool_version": VERSION,
            "endpoints": endpoints,
            "activities": activities,
        }
        (tmp_dir / "_day.json").write_text(json.dumps(meta, indent=1) + "\n")
        files = [p for p in tmp_dir.rglob("*") if p.is_file()]
        total = sum(p.stat().st_size for p in files)
        shutil.rmtree(final_dir, ignore_errors=True)
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp_dir, final_dir)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    return {
        "fetched_at": fetched_at,
        "path": str(final_dir.relative_to(cfg.data_dir)),
        "files": len(files),
        "bytes": total,
        "endpoints": endpoints,
        "activities": activities,
    }


# --------------------------------------------------------------------------
# Archiving and uploading
# --------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_archive(cfg: Config, day: str) -> dict:
    """Compress a day folder to archives/YYYY/YYYY-MM-DD.tar.gz (reproducible)."""
    src = cfg.day_dir(day)
    rel = cfg.archive_rel(day)
    dest = cfg.data_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")

    def normalise(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = 0
        return info

    with tmp.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            for path in sorted(p for p in src.rglob("*") if p.is_file()):
                tar.add(path, arcname=f"{day}/{path.relative_to(src).as_posix()}", filter=normalise)
    os.replace(tmp, dest)
    return {"file": rel, "bytes": dest.stat().st_size, "sha256": sha256_file(dest), "created_at": now_iso()}


def rclone_copy(cfg: Config, target: str, src: Path | str, dest: str, storage_class: str | None = None) -> None:
    cmd = [cfg.rclone_bin, "copyto", str(src), dest, "--retries", "3", "--low-level-retries", "20"]
    if target == "s3":
        cmd += ["--s3-storage-class", storage_class or cfg.s3_storage_class]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
        raise RuntimeError(f"rclone exited {result.returncode}: {' | '.join(tail)}")


RCLONE_DIR_NOT_FOUND, RCLONE_FILE_NOT_FOUND = 3, 4
COLD_STORAGE = {"GLACIER", "DEEP_ARCHIVE"}


def _rclone(cfg: Config, *args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run([cfg.rclone_bin, *args, "--retries", "3", "--low-level-retries", "20"],
                          capture_output=True, text=True, timeout=timeout)


def _rclone_error(result: subprocess.CompletedProcess) -> str:
    tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
    return f"rclone exited {result.returncode}: {' | '.join(tail)}"


def rjoin(remote: str, rel: str) -> str:
    """Path of ``rel`` under an rclone remote; a bare ``name:`` takes no slash."""
    return f"{remote}{rel}" if remote.endswith(":") else f"{remote}/{rel}"


def fetch_remote_manifest(cfg: Config, target: str, remote: str) -> dict | None:
    """The manifest.json stored on a target, or None if there is none yet."""
    tmp = cfg.data_dir / f".remote-manifest-{target}.json"
    tmp.unlink(missing_ok=True)
    result = _rclone(cfg, "copyto", rjoin(remote, "manifest.json"), str(tmp))
    if result.returncode in (RCLONE_DIR_NOT_FOUND, RCLONE_FILE_NOT_FOUND):
        return None
    if result.returncode != 0:
        raise RuntimeError(_rclone_error(result))
    if not tmp.is_file():
        # rclone exits 0 without writing anything when the S3 object does not exist.
        return None
    try:
        remote_manifest = json.loads(tmp.read_text())
    finally:
        tmp.unlink(missing_ok=True)
    if remote_manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise RuntimeError(f"remote manifest has schema_version {remote_manifest.get('schema_version')}, "
                           f"this tool reads {MANIFEST_SCHEMA_VERSION}")
    return remote_manifest


def list_remote_archives(cfg: Config, remote: str) -> dict[str, int]:
    """{'archives/YYYY/YYYY-MM-DD.tar.gz': size} for every archive on a target."""
    result = _rclone(cfg, "lsjson", "--recursive", "--files-only", rjoin(remote, "archives"))
    if result.returncode == RCLONE_DIR_NOT_FOUND:
        return {}
    if result.returncode != 0:
        raise RuntimeError(_rclone_error(result))
    return {f"archives/{item['Path']}": item["Size"] for item in json.loads(result.stdout or "[]")}


def _read_remote(cfg: Config, target: str, remote: str, attempts: int = 3):
    """Remote manifest and archive listing, retrying temporary failures such as
    Google Drive's rateLimitExceeded."""
    for attempt in range(1, attempts + 1):
        try:
            return fetch_remote_manifest(cfg, target, remote), list_remote_archives(cfg, remote)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            if attempt == attempts:
                raise
            delay = backoff_delay(attempt + 2, cfg.backoff_base, cfg.backoff_max)
            log.warning("Reading %s failed (%s); retrying in %.0fs", target, exc, delay)
            time.sleep(delay)
    raise AssertionError("unreachable")


def sync_remote_state(cfg: Config, manifest: dict) -> int:
    """Bring the local manifest in line with what each upload target holds.

    For every target: read its manifest.json (if any) and adopt the days it has
    as final and uploaded that are missing locally; record uploads it confirms
    for archives identical to the local ones; then list the target's archives
    and drop any upload record whose file is not there (it is uploaded again).
    Without a remote manifest, the local manifest is used as it is.
    Returns the number of targets that could not be read.
    """
    targets = cfg.targets()
    if not targets:
        return 0
    if not shutil.which(cfg.rclone_bin):
        log.error("rclone binary %r not found; cannot read remote state", cfg.rclone_bin)
        return len(targets)
    failures = 0
    state = manifest.setdefault("remote_sync", {})
    for target, remote in targets.items():
        try:
            remote_manifest, listing = _read_remote(cfg, target, remote)
        except (RuntimeError, subprocess.TimeoutExpired, OSError, ValueError) as exc:
            failures += 1
            state[target] = {"checked_at": now_iso(), "error": str(exc)}
            log.error("Cannot read the state of %s (%s); using the local manifest for it", target, exc)
            continue

        adopted = confirmed = dropped = 0
        for day, theirs in ((remote_manifest or {}).get("days") or {}).items():
            archive = theirs.get("archive")
            upload = (theirs.get("uploads") or {}).get(target)
            if theirs.get("status") != "final" or not archive or not upload or upload.get("sha256") != archive.get("sha256"):
                continue
            ours = manifest["days"].get(day)
            if ours is None or (ours.get("status") != "final" and not cfg.day_dir(day).is_dir()):
                entry = {k: v for k, v in theirs.items() if k != "uploads"}
                entry["uploads"] = {target: upload}
                entry["adopted_from"] = target
                manifest["days"][day] = entry
                adopted += 1
            elif (ours.get("archive") or {}).get("sha256") == archive["sha256"]:
                if (ours.get("uploads") or {}).get(target) != upload:
                    ours.setdefault("uploads", {})[target] = upload
                    confirmed += 1

        for day, entry in manifest["days"].items():
            upload = (entry.get("uploads") or {}).get(target)
            if not upload:
                continue
            archive = entry.get("archive") or {}
            if listing.get(archive.get("file")) != archive.get("bytes"):
                entry["uploads"].pop(target)
                dropped += 1
                log.warning("%s is recorded as uploaded to %s, but %s is not there; it will be uploaded again",
                            day, target, archive.get("file"))

        state[target] = {"checked_at": now_iso(), "remote_manifest": remote_manifest is not None,
                         "remote_archives": len(listing), "adopted": adopted, "confirmed": confirmed,
                         "dropped": dropped}
        log.info("Remote state of %s: %s; %d archive(s) there; adopted %d day(s), confirmed %d upload(s), "
                 "%d upload record(s) dropped", target,
                 "manifest found" if remote_manifest is not None else "no manifest, using local",
                 len(listing), adopted, confirmed, dropped)
    save_manifest(cfg, manifest)
    return failures


def upload_source(cfg: Config, entry: dict, target: str | None) -> str | None:
    """Where to copy a day's archive from: the local file, or another target
    that holds it in readable (not cold) storage."""
    local = cfg.data_dir / entry["archive"]["file"]
    if local.is_file():
        return str(local)
    for other, upload in (entry.get("uploads") or {}).items():
        if other != target and upload.get("sha256") == entry["archive"]["sha256"] \
                and upload.get("storage_class", "STANDARD") not in COLD_STORAGE:
            return upload["remote"]
    return None


def _record_upload(cfg: Config, entry: dict, target: str, dest: str) -> None:
    entry.setdefault("uploads", {})[target] = {"remote": dest, "sha256": entry["archive"]["sha256"],
                                               "uploaded_at": now_iso()}
    if target == "s3":
        entry["uploads"][target]["storage_class"] = cfg.s3_storage_class


def batch_copy(cfg: Config, target: str, remote: str, rel_paths: list[str]) -> str | None:
    """Copy local archives to a target in ONE rclone call. Returns an error text or None.

    One process resolves the destination folders once and paces itself against
    the provider's limits (Google Drive answers bursts of single-file calls with
    rateLimitExceeded)."""
    listing = cfg.data_dir / ".upload-list.txt"
    listing.write_text("".join(f"{p}\n" for p in rel_paths))
    args = ["copy", str(cfg.data_dir), remote, "--files-from", str(listing), "--no-traverse",
            "--transfers", "4", "--checkers", "4", "--retries", "5", "--low-level-retries", "20"]
    if target == "s3":
        args += ["--s3-storage-class", cfg.s3_storage_class]
    try:
        result = subprocess.run([cfg.rclone_bin, *args], capture_output=True, text=True, timeout=7200)
    finally:
        listing.unlink(missing_ok=True)
    return None if result.returncode == 0 else _rclone_error(result)


def upload_pending(cfg: Config, manifest: dict) -> tuple[int, int]:
    """Upload every archive not yet on each target. Returns (uploaded, failed).

    Archives present locally go up in one batched rclone call per target; the
    target is then listed and only archives found there with the right size are
    recorded as uploaded. An archive missing locally is copied from another
    target that holds it in readable storage."""
    targets = cfg.targets()
    if not targets:
        log.info("No upload target configured (GARMIN_GDRIVE_REMOTE / GARMIN_S3_REMOTE); skipping uploads")
        return 0, 0
    if not shutil.which(cfg.rclone_bin):
        log.error("rclone binary %r not found; install rclone or set GARMIN_RCLONE_BIN", cfg.rclone_bin)
        return 0, 1
    uploaded = failed = 0
    for target, remote in targets.items():
        local_batch: list[tuple[str, dict]] = []
        remote_sourced: list[tuple[str, dict, str]] = []
        for day, entry in manifest["days"].items():
            archive = entry.get("archive")
            if entry.get("status") != "final" or not archive:
                continue
            dest = rjoin(remote, archive["file"])
            done = (entry.get("uploads") or {}).get(target)
            if done and done.get("sha256") == archive["sha256"] and done.get("remote") == dest:
                continue
            source = upload_source(cfg, entry, target)
            if source is None:
                failed += 1
                log.error("%s: archive is neither local nor on a readable target; cannot upload it to %s "
                          "(restore it, or run with --refetch %s)", day, target, day)
            elif source == str(cfg.data_dir / archive["file"]):
                local_batch.append((day, entry))
            else:
                remote_sourced.append((day, entry, source))

        if local_batch:
            log.info("Uploading %d archive(s) to %s", len(local_batch), target)
            error = batch_copy(cfg, target, remote, [e["archive"]["file"] for _, e in local_batch])
            try:
                present = list_remote_archives(cfg, remote)
            except (RuntimeError, subprocess.TimeoutExpired, OSError, ValueError) as exc:
                present = {}
                error = error or f"cannot list {target} after uploading: {exc}"
            done_now = 0
            for day, entry in local_batch:
                if present.get(entry["archive"]["file"]) == entry["archive"]["bytes"]:
                    _record_upload(cfg, entry, target, rjoin(remote, entry["archive"]["file"]))
                    done_now += 1
                else:
                    failed += 1
            uploaded += done_now
            save_manifest(cfg, manifest)
            if done_now < len(local_batch):
                log.error("Uploaded %d of %d archive(s) to %s; the rest is retried next run (%s)",
                          done_now, len(local_batch), target, error or "missing on the target after the copy")
            else:
                log.info("Uploaded %d archive(s) to %s", done_now, target)

        for day, entry, source in remote_sourced:
            dest = rjoin(remote, entry["archive"]["file"])
            try:
                rclone_copy(cfg, target, source, dest)
            except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
                failed += 1
                log.error("Copy of %s from another target to %s failed: %s", day, target, exc)
                continue
            uploaded += 1
            _record_upload(cfg, entry, target, dest)
            save_manifest(cfg, manifest)
            log.info("Copied %s to %s from %s", day, dest, source)
    return uploaded, failed


def upload_manifest(cfg: Config) -> int:
    """Copy manifest.json to every target (S3 copy uses STANDARD, it changes daily)."""
    failed = 0
    if not shutil.which(cfg.rclone_bin):
        return 0
    for target, remote in cfg.targets().items():
        try:
            rclone_copy(cfg, target, cfg.manifest_path, rjoin(remote, "manifest.json"), storage_class="STANDARD")
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            failed += 1
            log.error("Upload of manifest.json to %s failed: %s", target, exc)
    return failed


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def summarise(cfg: Config, manifest: dict, start: dt.date, end: dt.date) -> dict:
    days = manifest["days"]
    wanted = date_range(start, end)
    by_status = {"final": 0, "provisional": 0, "failed": 0}
    for day in wanted:
        status = days.get(day, {}).get("status")
        if status in by_status:
            by_status[status] += 1
    missing = [d for d in wanted if d not in days]
    finals = [d for d, e in days.items() if e.get("status") == "final"]
    pending = {
        target: sorted(
            d for d in finals
            if not days[d].get("archive")
            or days[d].get("uploads", {}).get(target, {}).get("sha256") != days[d]["archive"]["sha256"]
        )
        for target in cfg.targets()
    }
    return {
        "data_dir": str(cfg.data_dir),
        "range": {"start": start.isoformat(), "end": end.isoformat(), "days": len(wanted)},
        "final": by_status["final"],
        "provisional": by_status["provisional"],
        "failed": sorted(d for d in wanted if days.get(d, {}).get("status") == "failed"),
        "never_fetched": missing,
        "not_archived": sorted(d for d in finals if not days[d].get("archive")),
        "pending_uploads": pending,
        "cloud_only": sorted(d for d in finals if not cfg.day_dir(d).is_dir()
                             and not (days[d].get("archive") and (cfg.data_dir / days[d]["archive"]["file"]).is_file())),
        "remote_sync": manifest.get("remote_sync", {}),
        "local_bytes": sum(e.get("bytes", 0) for e in days.values()),
        "last_run": manifest.get("last_run"),
    }


def compress_ranges(days: list[str]) -> str:
    """['2026-01-01','2026-01-02','2026-01-05'] -> '2026-01-01..2026-01-02, 2026-01-05'."""
    if not days:
        return "none"
    out, start, prev = [], days[0], days[0]
    for day in days[1:] + [None]:
        if day and dt.date.fromisoformat(day) - dt.date.fromisoformat(prev) == dt.timedelta(days=1):
            prev = day
            continue
        out.append(start if start == prev else f"{start}..{prev}")
        start = prev = day
    return ", ".join(out)


def extract_archive(cfg: Config, day: str, archive_path: Path) -> None:
    """Unpack archives/YYYY/YYYY-MM-DD.tar.gz into days/YYYY/YYYY-MM-DD/ (atomically)."""
    year_dir = cfg.day_dir(day).parent
    tmp = year_dir / f".restore-{day}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        with tarfile.open(archive_path) as tar:
            for member in tar.getmembers():
                if not (member.isfile() or member.isdir()) or not (member.name == day or member.name.startswith(f"{day}/")) \
                        or ".." in Path(member.name).parts:
                    raise RuntimeError(f"unexpected entry {member.name!r} in {archive_path.name}")
            if hasattr(tarfile, "data_filter"):
                tar.extractall(tmp, filter="data")
            else:  # Python without extraction filters; members were checked above
                tar.extractall(tmp)
        shutil.rmtree(cfg.day_dir(day), ignore_errors=True)
        os.replace(tmp / day, cfg.day_dir(day))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_restore(cfg: Config, args) -> int:
    """Bring back the raw files of final days that exist only in the cloud."""
    start, end = resolve_range(cfg, args)
    manifest = load_manifest(cfg)
    sync_failures = sync_remote_state(cfg, manifest) if cfg.targets() else 0
    todo = [d for d in date_range(start, end)
            if manifest["days"].get(d, {}).get("status") == "final" and manifest["days"][d].get("archive")
            and not cfg.day_dir(d).is_dir()]
    log.info("Restore: %d day(s) without a local folder in %s..%s", len(todo), start, end)
    restored = failed = 0
    for day in todo:
        entry = manifest["days"][day]
        archive = entry["archive"]
        local = cfg.data_dir / archive["file"]
        try:
            if not (local.is_file() and sha256_file(local) == archive["sha256"]):
                source = upload_source(cfg, entry, None)
                if source is None:
                    raise RuntimeError("no readable copy (Glacier copies need an S3 restore first; see docs/operations.md)")
                local.parent.mkdir(parents=True, exist_ok=True)
                result = _rclone(cfg, "copyto", source, str(local), timeout=1800)
                if result.returncode != 0:
                    raise RuntimeError(_rclone_error(result))
                if sha256_file(local) != archive["sha256"]:
                    local.unlink(missing_ok=True)
                    raise RuntimeError(f"downloaded archive from {source} does not match the manifest checksum")
            extract_archive(cfg, day, local)
        except (RuntimeError, subprocess.TimeoutExpired, OSError, tarfile.TarError) as exc:
            failed += 1
            log.error("%s: restore failed: %s", day, exc)
            continue
        restored += 1
        log.info("%s restored to %s", day, cfg.day_dir(day))
    log.info("Done: restored=%d failed=%d", restored, failed)
    return EXIT_PARTIAL if failed or sync_failures else EXIT_OK


def cmd_status(cfg: Config, args) -> int:
    manifest = load_manifest(cfg)
    start, end = resolve_range(cfg, args)
    summary = summarise(cfg, manifest, start, end)
    summary["encryption"] = target_encryption(cfg)
    if args.json:
        print(json.dumps(summary, indent=1))
        return EXIT_OK
    print(f"Data directory : {summary['data_dir']}")
    print(f"Range          : {start} .. {end} ({summary['range']['days']} days)")
    print(f"Final          : {summary['final']}")
    print(f"Provisional    : {summary['provisional']}")
    print(f"Failed         : {compress_ranges(summary['failed'])}")
    print(f"Never fetched  : {len(summary['never_fetched'])} ({compress_ranges(summary['never_fetched'])})")
    print(f"Not archived   : {compress_ranges(summary['not_archived'])}")
    for target, days in summary["pending_uploads"].items():
        print(f"Pending {target:7}: {len(days)} ({compress_ranges(days)})")
    if not summary["pending_uploads"]:
        print("Uploads        : no target configured")
    for target, encrypted in summary["encryption"].items():
        print(f"Encrypted {target:5}: {'unknown (rclone config not readable)' if encrypted is None else 'yes' if encrypted else 'no'}")
    print(f"Only in cloud  : {len(summary['cloud_only'])} ({compress_ranges(summary['cloud_only'])})")
    for target, info in summary["remote_sync"].items():
        if info.get("error"):
            print(f"Remote {target:8}: read failed at {info['checked_at']}: {info['error']}")
        else:
            print(f"Remote {target:8}: checked {info['checked_at']}, "
                  f"{'manifest found' if info.get('remote_manifest') else 'no manifest'}, "
                  f"{info.get('remote_archives', 0)} archives, adopted {info.get('adopted', 0)}, "
                  f"dropped {info.get('dropped', 0)}")
    print(f"Local raw size : {summary['local_bytes'] / 1e6:.1f} MB")
    last = summary["last_run"]
    if last:
        print(f"Last run       : {last['finished_at']} exit={last['exit_code']} "
              f"crawled={last['days_crawled']} failed={last['days_failed']} requests={last['requests']}")
    return EXIT_OK


def check_refetch_age(cfg: Config, manifest: dict, refetch: set[str], allowed: bool) -> None:
    """Refuse to re-crawl old days that are already archived.

    Garmin serves intraday detail (heart-rate samples, HRV readings, hourly SpO2)
    for only about four months. Re-crawling an older day would replace its
    archived folder with Garmin's thinner answer and re-upload that copy over
    the good one on every target."""
    cutoff = cfg.today - dt.timedelta(days=cfg.refetch_max_age_days)
    risky = sorted(d for d in refetch
                   if dt.date.fromisoformat(d) < cutoff and (manifest["days"].get(d) or {}).get("archive"))
    if risky and not allowed:
        raise ConfigError(
            f"refusing to re-crawl {', '.join(risky)}: older than {cfg.refetch_max_age_days} days and already "
            "archived. Garmin no longer serves intraday detail for such days, so the archived copy (and the "
            "uploaded ones) would be replaced by less data. Use --allow-old-refetch if that is really intended.")
    if risky:
        log.warning("Re-crawling old archived day(s) %s on request (--allow-old-refetch); intraday detail "
                    "Garmin no longer serves will be lost, here and on the upload targets", ", ".join(risky))


# --------------------------------------------------------------------------
# Optional encryption of the cloud copy (rclone crypt)
# --------------------------------------------------------------------------

CRYPT_REMOTES = {"gdrive": "garmin-gdrive-crypt", "s3": "garmin-s3-crypt"}


def rclone_remote_types(cfg: Config) -> dict[str, str] | None:
    """{remote name: backend type} from rclone's configuration (local, no
    network). None when it cannot be read. The dump holds secrets: never print it."""
    try:
        result = subprocess.run([cfg.rclone_bin, "config", "dump"], capture_output=True, text=True, timeout=60)
        dump = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if not isinstance(dump, dict):
        return None
    return {name: str((section or {}).get("type", "")) for name, section in dump.items()}


def _remote_name(remote: str) -> str | None:
    """The rclone remote name of ``name:path``; None for a local path."""
    name, sep, _ = remote.partition(":")
    return name if sep and "/" not in name else None


def target_encryption(cfg: Config) -> dict[str, bool | None]:
    """Per upload target: True if its rclone remote is a crypt remote, False if
    not, None if rclone's configuration cannot be read."""
    targets = cfg.targets()
    if not targets:
        return {}
    types = rclone_remote_types(cfg)
    out: dict[str, bool | None] = {}
    for target, remote in targets.items():
        name = _remote_name(remote)
        if name is None:
            out[target] = False
        elif os.environ.get(f"RCLONE_CONFIG_{name.upper().replace('-', '_')}_TYPE"):
            out[target] = os.environ[f"RCLONE_CONFIG_{name.upper().replace('-', '_')}_TYPE"] == "crypt"
        else:
            out[target] = None if types is None else types.get(name) == "crypt"
    return out


def encrypted_location(target: str, remote: str) -> str:
    """Default folder for the encrypted copy: a sibling of the plain one."""
    name = _remote_name(remote)
    if name is None:
        return f"{remote}-encrypted"
    path = remote.partition(":")[2].strip("/")
    if not path:
        return f"{name}:garmin-archive-encrypted"
    if target == "s3" and "/" not in path:           # only a bucket: stay inside it
        return f"{name}:{path}/garmin-archive-encrypted"
    return f"{name}:{path}-encrypted"


def rclone_config_path(cfg: Config) -> Path:
    result = subprocess.run([cfg.rclone_bin, "config", "file"], capture_output=True, text=True, timeout=60)
    lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    if result.returncode != 0 or not lines:
        raise ConfigError("cannot find rclone's configuration file (`rclone config file` failed)")
    return Path(lines[-1]).expanduser()


def cmd_encrypt(cfg: Config, args) -> int:
    """``init``: create a key and the crypt remotes. ``recover``: recreate the
    crypt remotes from a saved key. The key never appears on a command line."""
    targets = cfg.targets()
    if not targets:
        raise ConfigError("no upload target configured (GARMIN_GDRIVE_REMOTE / GARMIN_S3_REMOTE)")
    if not shutil.which(cfg.rclone_bin):
        raise ConfigError(f"rclone binary {cfg.rclone_bin!r} not found; install rclone or set GARMIN_RCLONE_BIN")
    types = rclone_remote_types(cfg)
    if types is None:
        raise ConfigError("cannot read rclone's configuration (`rclone config dump` failed)")
    wraps: dict[str, str] = {}
    for item in args.wrap:
        target, sep, location = item.partition("=")
        if target not in targets or not sep or not location:
            raise ConfigError(f"--wrap must be TARGET=REMOTE:PATH with TARGET one of {sorted(targets)}; got {item!r}")
        wraps[target] = location.rstrip("/")

    existing = sorted(name for target, name in CRYPT_REMOTES.items() if target in targets and name in types)
    if args.action == "init" and existing:
        raise ConfigError(f"rclone remote {', '.join(existing)} already exists, so a key was already created. "
                          "To add a target with the same key use `encrypt recover`; to start over, first remove "
                          "the remote with `rclone config delete` (its key is lost unless you saved it)")
    plan: dict[str, tuple[str, str]] = {}            # target -> (crypt remote name, wrapped location)
    for target, remote in targets.items():
        name = CRYPT_REMOTES[target]
        if name in types:
            print(f"{target}: rclone remote {name} already exists; left as it is")
            continue
        if target in wraps:
            location = wraps[target]
        elif types.get(_remote_name(remote) or "") == "crypt" or _remote_name(remote) == name:
            raise ConfigError(f"{target} is set to {remote}, which is (or should be) an encrypted remote; give the "
                              f"plain location that holds the encrypted files with --wrap {target}=REMOTE:PATH")
        else:
            location = encrypted_location(target, remote)
        plan[target] = (name, location)
    if not plan:
        print("Nothing to create.")
        return EXIT_OK

    if args.action == "init":
        key = base64.b32encode(secrets.token_bytes(32)).decode().rstrip("=").lower()     # 256 bits
    else:
        key = (getpass.getpass("Encryption key: ") if sys.stdin.isatty() else sys.stdin.readline()).strip()
        if not key or any(c.isspace() for c in key):
            raise ConfigError("the encryption key is empty or contains spaces")
    result = subprocess.run([cfg.rclone_bin, "obscure", "-"], input=key, capture_output=True, text=True, timeout=60)
    obscured = result.stdout.strip()
    if result.returncode != 0 or not obscured:
        raise ConfigError("`rclone obscure` failed; rclone's configuration was not changed")

    path = rclone_config_path(cfg)
    path = path.resolve() if path.exists() else path
    text = path.read_text() if path.exists() else ""
    if "RCLONE_ENCRYPT_V0:" in text[:500]:
        raise ConfigError(f"{path} is itself encrypted, so this command cannot add to it. Create the remote by hand: "
                          "`rclone config`, new remote, type crypt (see docs/operations.md)")
    if text and not text.endswith("\n"):
        text += "\n"
    for name, location in plan.values():
        text += (f"\n[{name}]\ntype = crypt\nremote = {location}\npassword = {obscured}\n"
                 "filename_encryption = standard\ndirectory_name_encryption = true\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".garmin-archive.tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
        fh.write(text)
    os.replace(tmp, path)
    types = rclone_remote_types(cfg) or {}
    broken = sorted(name for name, _ in plan.values() if types.get(name) != "crypt")
    if broken:
        raise ConfigError(f"wrote {path} but rclone does not list {', '.join(broken)} as a crypt remote; check the file")

    print(f"Added to {path}:")
    for target, (name, location) in plan.items():
        print(f"  {target}: {name}:  ->  encrypted files in {location}")
    if args.action == "init":
        print("\nENCRYPTION KEY (shown only now):\n")
        print(f"  {key}\n")
        print("Save it in a password manager or on paper before going on. Without this key\n"
              "and without this machine's rclone configuration, the cloud copy cannot be read.")
    else:
        for target, (name, _) in plan.items():
            listing = _rclone(cfg, "lsf", f"{name}:", "--max-depth", "1", timeout=300)
            if listing.returncode == 0 and "manifest.json" in listing.stdout.split():
                print(f"{target}: key verified, manifest.json is readable")
            else:
                print(f"WARNING: {target}: no readable manifest.json in {name}: . Either the key is wrong (then run "
                      f"`rclone config delete {name}` and recover again) or nothing was uploaded there yet.")
    print("\nTo use the encrypted remotes, set in .env:")
    for target, (name, _) in plan.items():
        print(f"  GARMIN_{target.upper()}_REMOTE={name}:")
    print("  GARMIN_REQUIRE_ENCRYPTION=true")
    if args.action == "init":
        print("\nThe next run uploads every archive again, encrypted. The plain copies already in\n"
              "the cloud are not touched; delete them yourself once a restore from the encrypted\n"
              "copy has worked (docs/operations.md).")
    else:
        print("\nThen run `restore` to download and unpack the days.")
    return EXIT_OK


def resolve_range(cfg: Config, args) -> tuple[dt.date, dt.date]:
    start = parse_date(args.start, "--from") if args.start else cfg.start_date
    last = cfg.today if cfg.include_today else cfg.today - dt.timedelta(days=1)
    end = parse_date(args.end, "--to") if args.end else last
    if end > last:
        raise ConfigError("--to must be before today (today is still incomplete); "
                          "set GARMIN_INCLUDE_TODAY=true to fetch today as provisional")
    if start > end:
        raise ConfigError(f"start date {start} is after end date {end}")
    return start, end


def cmd_run(cfg: Config, args) -> int:
    start, end = resolve_range(cfg, args)
    refetch = {parse_date(d, "--refetch").isoformat() for d in args.refetch}
    latest = cfg.today if cfg.include_today else cfg.today - dt.timedelta(days=1)
    if any(d > latest.isoformat() for d in refetch):
        raise ConfigError(f"--refetch dates must be on or before {latest}")
    manifest = load_manifest(cfg)
    check_refetch_age(cfg, manifest, refetch, args.allow_old_refetch)
    if cfg.require_encryption and not args.dry_run and not args.no_upload:
        plain = sorted(t for t, encrypted in target_encryption(cfg).items() if not encrypted)
        if plain:
            raise ConfigError(f"GARMIN_REQUIRE_ENCRYPTION is set but {', '.join(plain)} is not an encrypted (crypt) "
                              "rclone remote; nothing was uploaded. See `encrypt init` in docs/operations.md")
    sync_failures = 0
    if not args.dry_run and cfg.targets():
        sync_failures = sync_remote_state(cfg, manifest)
    todo = plan_days(cfg, manifest, start, end, refetch)
    limit = args.max_days if args.max_days is not None else cfg.max_days_per_run
    deferred = 0
    if limit and len(todo) > limit:
        deferred = len(todo) - limit
        todo = todo[:limit]

    log.info("garmin-archive %s: range %s..%s, %d day(s) to crawl%s", VERSION, start, end, len(todo),
             f" ({deferred} deferred by the per-run limit)" if deferred else "")
    if args.dry_run:
        print(f"Would crawl {len(todo)} day(s): {compress_ranges(sorted(todo))}")
        return EXIT_OK
    if todo and not cfg.api_key:
        raise ConfigError("GARMIN_PERSONAL_API_KEY is not set")

    started = now_iso()
    client = Client(cfg)
    snapshots: dict[str, tuple[int, bytes]] = {}
    crawled = failures = streak = 0
    fatal: str | None = None

    cooldowns = 0
    index = 0
    while index < len(todo):
        day = todo[index]
        index += 1
        entry = manifest["days"].setdefault(day, {})
        entry["attempts"] = entry.get("attempts", 0) + 1
        try:
            result = crawl_day(client, cfg, day, snapshots)
        except AuthError as exc:
            fatal = str(exc)
            entry.update(status=entry.get("status", "failed"), error=fatal)
            save_manifest(cfg, manifest)
            log.error("%s", fatal)
            break
        except FetchError as exc:
            if not exc.rate_limited or cooldowns >= cfg.rate_limit_cooldowns:
                exc_to_record = exc
            else:
                cooldowns += 1
                pause = min(RATE_LIMIT_COOLDOWN_CAP, cfg.rate_limit_cooldown * 2 ** (cooldowns - 1))
                log.warning("%s: Garmin is still rate limiting (%s); pausing the crawl for %.0f s, then retrying "
                            "this day (cool-down %d/%d)", day, exc, pause, cooldowns, cfg.rate_limit_cooldowns)
                time.sleep(pause)
                index -= 1          # same day again
                continue
            failures += 1
            streak += 1
            if entry.get("status") != "provisional":
                entry["status"] = "failed"
            entry["error"] = str(exc_to_record)
            entry["failed_at"] = now_iso()
            save_manifest(cfg, manifest)
            log.error("%s failed: %s", day, exc_to_record)
            if exc_to_record.rate_limited:
                log.error("Garmin kept rate limiting after %d cool-downs; stopping the crawl. "
                          "The next run resumes from the manifest.", cooldowns)
                break
            if streak >= 3:
                log.error("Three days failed in a row; stopping the crawl. The next run resumes from the manifest.")
                break
            continue
        except (ValueError, OSError) as exc:
            failures += 1
            streak += 1
            if entry.get("status") != "provisional":
                entry["status"] = "failed"
            entry["error"] = str(exc)
            entry["failed_at"] = now_iso()
            save_manifest(cfg, manifest)
            log.error("%s failed: %s", day, exc)
            if streak >= 3:
                log.error("Three days failed in a row; stopping the crawl. The next run resumes from the manifest.")
                break
            continue

        streak = 0
        cooldowns = 0               # progress made: a later rate limit gets the full set of cool-downs again
        crawled += 1
        if day in refetch or entry.get("archive"):
            old = entry.pop("archive", None)
            entry.pop("uploads", None)
            if old:
                (cfg.data_dir / old["file"]).unlink(missing_ok=True)
        entry.pop("error", None)
        entry.pop("failed_at", None)
        entry.update(result, status="final" if is_settled(cfg, day) else "provisional")
        save_manifest(cfg, manifest)
        log.info("[%d/%d] %s %s: %d files, %d bytes, %d activit%s", index, len(todo), day, entry["status"],
                 entry["files"], entry["bytes"], len(entry["activities"]),
                 "y" if len(entry["activities"]) == 1 else "ies")

    archived = 0
    for day, entry in manifest["days"].items():
        if entry.get("status") != "final":
            continue
        archive = entry.get("archive")
        if archive and (cfg.data_dir / archive["file"]).exists():
            continue
        if not cfg.day_dir(day).is_dir():
            if not archive:
                log.error("%s is final but its folder %s is gone; run with --refetch %s", day, cfg.day_dir(day), day)
                failures += 1
            continue
        if archive:
            log.warning("%s: archive file is missing locally, rebuilding it", day)
        entry["archive"] = build_archive(cfg, day)
        if archive and entry["archive"]["sha256"] != archive["sha256"]:
            # Archives are reproducible; only a different archive invalidates the uploads.
            entry.pop("uploads", None)
        archived += 1
    if archived:
        save_manifest(cfg, manifest)
        log.info("Archived %d day(s)", archived)

    uploaded = upload_failed = 0
    if not args.no_upload:
        uploaded, upload_failed = upload_pending(cfg, manifest)

    exit_code = EXIT_CONFIG if fatal else (EXIT_PARTIAL if failures or upload_failed or sync_failures else EXIT_OK)
    manifest["last_run"] = {
        "started_at": started,
        "finished_at": now_iso(),
        "exit_code": exit_code,
        "days_planned": len(todo),
        "days_crawled": crawled,
        "days_failed": failures,
        "days_deferred": deferred,
        "archived": archived,
        "uploaded": uploaded,
        "upload_failures": upload_failed,
        "remote_sync_failures": sync_failures,
        "requests": client.requests,
        "error": fatal,
    }
    save_manifest(cfg, manifest)
    if not args.no_upload and cfg.targets():
        if upload_manifest(cfg) and exit_code == EXIT_OK:
            exit_code = EXIT_PARTIAL
    log.info("Done: crawled=%d failed=%d archived=%d uploaded=%d upload_failures=%d requests=%d exit=%d",
             crawled, failures, archived, uploaded, upload_failed, client.requests, exit_code)
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="garmin_archive.py",
        description="Back up raw Garmin Connect data per day, compress it and upload it. "
                    "Configuration comes from environment variables (see README.md).",
    )
    parser.add_argument("--env-file", help="file with KEY=VALUE settings (default: .env next to the script)")
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command")

    def add_range(p):
        p.add_argument("--from", dest="start", metavar="YYYY-MM-DD", help="first day (default: GARMIN_START_DATE)")
        p.add_argument("--to", dest="end", metavar="YYYY-MM-DD", help="last day (default: yesterday)")

    run = sub.add_parser("run", help="crawl missing days, archive settled days, upload (default command)")
    add_range(run)
    run.add_argument("--refetch", action="append", default=[], metavar="YYYY-MM-DD",
                     help="crawl this day again even if it is final (repeatable)")
    run.add_argument("--allow-old-refetch", action="store_true",
                     help="allow --refetch of archived days older than GARMIN_REFETCH_MAX_AGE_DAYS (loses intraday detail)")
    run.add_argument("--max-days", type=int, help="crawl at most this many days (overrides GARMIN_MAX_DAYS_PER_RUN)")
    run.add_argument("--no-upload", action="store_true", help="crawl and archive only")
    run.add_argument("--dry-run", action="store_true", help="print the days that would be crawled and exit")

    restore = sub.add_parser("restore", help="download and unpack final days that exist only in the cloud")
    add_range(restore)

    status = sub.add_parser("status", help="show what is fetched, missing and pending upload")
    add_range(status)
    status.add_argument("--json", action="store_true", help="machine-readable output")

    encrypt = sub.add_parser("encrypt", help="optional: encrypt the cloud copy with rclone crypt")
    encrypt.add_argument("action", choices=["init", "recover"],
                         help="init: create a key and the encrypted remotes; recover: recreate them from a saved key")
    encrypt.add_argument("--wrap", action="append", default=[], metavar="TARGET=REMOTE:PATH",
                         help="where the encrypted files of a target (gdrive, s3) are stored "
                              "(default: next to the plain copy, with -encrypted added)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not any(a in ("run", "status", "restore", "encrypt") for a in argv) and not any(a in ("-h", "--help", "--version") for a in argv):
        argv.append("run")
    args = parser.parse_args(argv)
    load_env_file(Path(args.env_file) if args.env_file else Path(__file__).resolve().parent / ".env")
    try:
        cfg = Config.from_env()
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        if args.command == "status":
            return cmd_status(cfg, args)
        setup_logging(cfg)
        with open(cfg.data_dir / ".lock", "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log.error("Another garmin-archive run holds %s; exiting", cfg.data_dir / ".lock")
                return EXIT_LOCKED
            if args.command == "encrypt":
                return cmd_encrypt(cfg, args)
            return cmd_restore(cfg, args) if args.command == "restore" else cmd_run(cfg, args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG


if __name__ == "__main__":
    sys.exit(main())
