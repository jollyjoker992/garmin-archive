# garmin-archive: crawler, cloud backup, FHIR mapping/loading and daily scheduler.
# Standard-library Python plus rclone; no secrets are baked in (see docs/docker.md).
FROM rclone/rclone:1.76.0 AS rclone

FROM python:3.15-slim-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone

ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --gid "$APP_GID" app && useradd --uid "$APP_UID" --gid app --create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data /config/rclone && chown app:app /data /config/rclone

WORKDIR /app
COPY --chown=app:app garmin_archive.py garmin_fhir.py garmin_scheduler.py run-daily.sh ./
COPY --chown=app:app fhir/ ./fhir/
COPY --chown=app:app docker/entrypoint.sh /usr/local/bin/garmin-entrypoint
COPY --chown=app:app tests/ ./tests/

ENV GARMIN_ARCHIVE_DIR=/data \
    GARMIN_RCLONE_BIN=rclone \
    RCLONE_CONFIG=/config/rclone/rclone.conf \
    PYTHONUNBUFFERED=1 \
    TZ=UTC \
    GARMIN_SCHEDULE_TIME=06:15

USER app
VOLUME ["/data"]
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
  CMD python3 -c "import os,sys,time; p='/data/.scheduler-heartbeat'; sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 180 else 1)"
ENTRYPOINT ["garmin-entrypoint"]
CMD ["schedule"]
