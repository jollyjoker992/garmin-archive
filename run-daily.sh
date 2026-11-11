#!/bin/sh
# Daily pipeline: crawl Garmin -> map to FHIR -> load into the local Medplum.
#
#   ./run-daily.sh            (from cron or launchd; see docs/operations.md)
#
# Each step only processes what changed. The exit code is the worst of the
# steps: 0 ok, 1 partial (retried next run), 2 needs a person, 3 locked.
# A crawl that needs a person (2) or is locked (3) stops the pipeline; a
# partial crawl (1) still maps and loads what it did fetch.
cd "$(dirname "$0")" || exit 2
PYTHON=${PYTHON:-python3}

"$PYTHON" garmin_archive.py run
crawl=$?
if [ "$crawl" -ge 2 ]; then
  exit "$crawl"
fi

"$PYTHON" garmin_fhir.py map
map=$?

"$PYTHON" garmin_fhir.py load
load=$?

worst=$crawl
for code in "$map" "$load"; do
  if [ "$code" -gt "$worst" ]; then worst=$code; fi
done
exit "$worst"
