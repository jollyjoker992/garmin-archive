#!/usr/bin/env python3
"""Run the daily pipeline (run-daily.sh) on a schedule. Standard library only.

Meant as the long-running process of the Docker container, but works anywhere.

    python3 garmin_scheduler.py            # run once at start (if enabled), then daily

Settings (environment):
  GARMIN_SCHEDULE_TIME   "HH:MM" or a comma-separated list ("06:15,18:15"); default 06:15
  GARMIN_SCHEDULE_TZ     IANA zone for those times; default $TZ, else UTC
  GARMIN_RUN_ON_START    "true" (default) runs the pipeline immediately on start:
                         the backfill on a fresh data directory, a catch-up otherwise
  GARMIN_JOB             command to run; default ./run-daily.sh next to this file

Status is written to <GARMIN_ARCHIVE_DIR>/scheduler-status.json and a heartbeat
to <GARMIN_ARCHIVE_DIR>/.scheduler-heartbeat (used by the container healthcheck).
SIGTERM/SIGINT stop the scheduler; a running job is asked to stop and given
GARMIN_STOP_GRACE seconds (default 25) before it is killed.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import threading
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
log = logging.getLogger("garmin-scheduler")


def parse_times(text: str) -> list[tuple[int, int]]:
    times = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            hour, minute = (int(x) for x in part.split(":"))
        except ValueError:
            raise ValueError(f"bad GARMIN_SCHEDULE_TIME entry {part!r}; use HH:MM") from None
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError(f"bad GARMIN_SCHEDULE_TIME entry {part!r}")
        times.append((hour, minute))
    if not times:
        raise ValueError("GARMIN_SCHEDULE_TIME is empty")
    return sorted(set(times))


def next_run(now: dt.datetime, times: list[tuple[int, int]], zone: ZoneInfo) -> dt.datetime:
    """The first scheduled moment strictly after ``now`` (an aware datetime)."""
    local = now.astimezone(zone)
    for day_offset in range(0, 3):
        day = (local + dt.timedelta(days=day_offset)).date()
        for hour, minute in times:
            candidate = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
            if candidate > local:
                return candidate
    raise AssertionError("no next run found")


class Scheduler:
    def __init__(self, job: list[str], times: list[tuple[int, int]], zone: ZoneInfo, data_dir: Path,
                 run_on_start: bool, grace: float):
        self.job, self.times, self.zone = job, times, zone
        self.data_dir, self.run_on_start, self.grace = data_dir, run_on_start, grace
        self.stop = threading.Event()
        self.child: subprocess.Popen | None = None
        self.status_path = data_dir / "scheduler-status.json"
        self.heartbeat_path = data_dir / ".scheduler-heartbeat"
        self.status = self._load_status()

    def _load_status(self) -> dict:
        try:
            return json.loads(self.status_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save_status(self, **fields) -> None:
        self.status.update(fields)
        tmp = self.status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.status, indent=1) + "\n")
        os.replace(tmp, self.status_path)

    def beat(self) -> None:
        self.heartbeat_path.write_text(dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds") + "\n")

    def handle_signal(self, signum, _frame) -> None:
        log.info("Received %s; stopping", signal.Signals(signum).name)
        self.stop.set()
        if self.child and self.child.poll() is None:
            self.child.terminate()

    def run_job(self, reason: str) -> int:
        started = dt.datetime.now(self.zone)
        log.info("Starting %s (%s)", " ".join(self.job), reason)
        self._save_status(last_start=started.isoformat(timespec="seconds"), running=True, last_reason=reason)
        self.child = subprocess.Popen(self.job, cwd=HERE)
        while True:
            try:
                code = self.child.wait(timeout=30)
                break
            except subprocess.TimeoutExpired:
                self.beat()
                if self.stop.is_set():
                    try:
                        code = self.child.wait(timeout=self.grace)
                    except subprocess.TimeoutExpired:
                        log.warning("Job did not stop within %.0f s; killing it", self.grace)
                        self.child.kill()
                        code = self.child.wait()
                    break
        self.child = None
        ended = dt.datetime.now(self.zone)
        meaning = {0: "ok", 1: "partial, retried next run", 2: "needs attention (configuration or API key)",
                   3: "another run holds the lock"}.get(code, "failed")
        log.log(logging.INFO if code == 0 else logging.WARNING if code in (1, 3) else logging.ERROR,
                "Job finished in %s with exit code %d (%s)", str(ended - started).split(".")[0], code, meaning)
        self._save_status(running=False, last_end=ended.isoformat(timespec="seconds"), last_exit_code=code,
                          last_result=meaning)
        return code

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.handle_signal)
        signal.signal(signal.SIGINT, self.handle_signal)
        self.beat()
        log.info("Scheduler started: daily at %s (%s); run on start: %s",
                 ", ".join(f"{h:02d}:{m:02d}" for h, m in self.times), self.zone.key, self.run_on_start)
        if self.run_on_start and not self.stop.is_set():
            self.run_job("start")
        while not self.stop.is_set():
            target = next_run(dt.datetime.now(dt.timezone.utc), self.times, self.zone)
            self._save_status(next_run=target.isoformat(timespec="seconds"))
            log.info("Next run at %s", target.isoformat(timespec="minutes"))
            while not self.stop.is_set():
                remaining = (target - dt.datetime.now(dt.timezone.utc)).total_seconds()
                if remaining <= 0:
                    break
                self.beat()
                self.stop.wait(min(remaining, 30))
            if not self.stop.is_set():
                self.run_job("schedule")
        log.info("Scheduler stopped")
        return 0


def main() -> int:
    logging.basicConfig(level=os.environ.get("GARMIN_LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)-7s scheduler: %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z",
                        stream=sys.stdout)
    try:
        times = parse_times(os.environ.get("GARMIN_SCHEDULE_TIME", "06:15"))
        zone = ZoneInfo(os.environ.get("GARMIN_SCHEDULE_TZ") or os.environ.get("TZ") or "UTC")
    except Exception as exc:  # bad time or unknown zone
        log.error("Configuration error: %s", exc)
        return 2
    data_dir = Path(os.environ.get("GARMIN_ARCHIVE_DIR", str(HERE / "data"))).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    job = shlex.split(os.environ.get("GARMIN_JOB", str(HERE / "run-daily.sh")))
    run_on_start = os.environ.get("GARMIN_RUN_ON_START", "true").strip().lower() in ("1", "true", "yes")
    grace = float(os.environ.get("GARMIN_STOP_GRACE", "25"))
    return Scheduler(job, times, zone, data_dir, run_on_start, grace).run()


if __name__ == "__main__":
    sys.exit(main())
