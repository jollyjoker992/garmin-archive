#!/bin/sh
# Container entrypoint for garmin-archive.
#   schedule (default)  run on start, then daily at GARMIN_SCHEDULE_TIME
#   run | backfill      one full pipeline run (crawl, archive, upload, map, load), then exit
#   crawl [args]        garmin_archive.py run [args]   (e.g. --no-upload, --from D)
#   status | restore    garmin_archive.py status|restore [args]
#   map | load [args]   garmin_fhir.py map|load [args]
#   anything else       executed as is (e.g. "rclone lsf gdrive:", "sh")
set -e
cd /app
cmd="${1:-schedule}"
[ $# -gt 0 ] && shift
case "$cmd" in
  schedule)       exec python3 garmin_scheduler.py ;;
  run|backfill)   exec ./run-daily.sh ;;
  crawl)          exec python3 garmin_archive.py run "$@" ;;
  status|restore) exec python3 garmin_archive.py "$cmd" "$@" ;;
  map|load)       exec python3 garmin_fhir.py "$cmd" "$@" ;;
  *)              exec "$cmd" "$@" ;;
esac
