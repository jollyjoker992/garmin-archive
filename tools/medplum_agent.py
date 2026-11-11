#!/usr/bin/env python3
"""Read-only access to the local Medplum for agents and scripts. Standard library only.

Always uses the *agent* client (GARMIN_MEDPLUM_AGENT_CLIENT_ID / _SECRET from the
repository's .env), whose access policy allows reading and searching only.

    python3 tools/medplum_agent.py token              print an access token
    python3 tools/medplum_agent.py headers            print {"Authorization": "Bearer ..."} (MCP headersHelper)
    python3 tools/medplum_agent.py graphql QUERY      run a GraphQL query, print the JSON result
    python3 tools/medplum_agent.py graphql @file.graphql [--vars '{"from": "..."}']
    python3 tools/medplum_agent.py codes [TEXT]       list garmin-metrics codes (unit, definition), optionally filtered
    python3 tools/medplum_agent.py fit list [--from ISO] [--to ISO]          workouts and whether a FIT file exists
    python3 tools/medplum_agent.py fit records ACTIVITY [--format csv|json] [--every N] [--no-gps] [--out FILE]
    python3 tools/medplum_agent.py fit laps ACTIVITY                        per-lap figures from the FIT file
    python3 tools/medplum_agent.py fit messages ACTIVITY                    what the FIT file contains (message counts)

The fit commands download the workout's FIT archive from Medplum (Binary,
read-only client), cache the .fit in data/.fit-cache/ (mode 600) and decode it
with Garmin's FIT SDK (optional: python3 -m pip install garmin-fit-sdk).

Tokens last one hour. They are cached in data/.medplum-agent-token.json (mode
600) and renewed five minutes before they expire, so frequent calls do not hit
Medplum's auth rate limit.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GM_SYSTEM = "https://github.com/jollyjoker992/garmin-archive/fhir/CodeSystem/garmin-metrics"


def read_env() -> dict[str, str]:
    env = {}
    path = REPO / ".env"
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.removeprefix("export ").partition("=")
                env[key.strip()] = value.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if k.startswith("GARMIN_")})
    return env


ENV = read_env()
BASE = ENV.get("GARMIN_MEDPLUM_URL", "http://localhost:8103").rstrip("/")
CACHE = Path(ENV.get("GARMIN_ARCHIVE_DIR", str(REPO / "data"))).expanduser() / ".medplum-agent-token.json"


def fail(message: str) -> None:
    print(f"medplum_agent: {message}", file=sys.stderr)
    sys.exit(2)


def token() -> str:
    client_id = ENV.get("GARMIN_MEDPLUM_AGENT_CLIENT_ID")
    secret = ENV.get("GARMIN_MEDPLUM_AGENT_CLIENT_SECRET")
    if not client_id or not secret:
        fail("GARMIN_MEDPLUM_AGENT_CLIENT_ID / _SECRET are not set; run 'python3 medplum/setup.py project'")
    try:
        cached = json.loads(CACHE.read_text())
        if cached.get("client_id") == client_id and cached.get("expires_at", 0) - time.time() > 300:
            return cached["access_token"]
    except (OSError, ValueError):
        pass
    form = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": client_id,
                                   "client_secret": secret}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(BASE + "/oauth2/token", data=form), timeout=30) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        fail(f"token request failed: HTTP {exc.code} {exc.read()[:200]!r}")
    except urllib.error.URLError as exc:
        fail(f"cannot reach Medplum at {BASE} ({exc.reason}); is 'docker compose up -d' running in medplum/?")
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(CACHE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"client_id": client_id, "access_token": body["access_token"],
                   "expires_at": time.time() + int(body.get("expires_in", 3600))}, fh)
    return body["access_token"]


def graphql(query: str, variables: dict | None = None) -> dict:
    data = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(BASE + "/fhir/R4/$graphql", data=data, method="POST",
                                 headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        fail(f"GraphQL request failed: HTTP {exc.code} {exc.read()[:500]!r}")


def codes(text: str | None) -> list[dict]:
    result = graphql('query($url: String) { CodeSystemList(url: $url) { version concept { code display '
                     'concept { code display definition property { code valueString } } } } }', {"url": GM_SYSTEM})
    systems = (result.get("data") or {}).get("CodeSystemList") or []
    if not systems:
        fail("garmin-metrics CodeSystem not found on the server")
    rows = []
    for group in systems[0]["concept"]:
        for c in group.get("concept") or []:
            props = {p["code"]: p.get("valueString") for p in c.get("property") or []}
            row = {"group": group["code"], "code": c["code"], "display": c["display"], "unit": props.get("unit"),
                   "valueType": props.get("valueType"), "definition": c["definition"]}
            if not text or text.lower() in json.dumps(row).lower():
                rows.append(row)
    return rows


# --------------------------------------------------------------------------
# Workout FIT files (second-by-second data)
# --------------------------------------------------------------------------

DOC_SYSTEM = GM_SYSTEM.replace("/CodeSystem/garmin-metrics", "/identifier/document")
OBS_SYSTEM = GM_SYSTEM.replace("/CodeSystem/garmin-metrics", "/identifier/observation")
FIT_CACHE = CACHE.parent / ".fit-cache"
SEMICIRCLE = 180 / 2 ** 31
STEP_SPORTS = {"running", "walking", "hiking", "trail_running", "treadmill_running"}
FIT_SDK_HINT = ("the FIT decoder is Garmin's official SDK, an optional dependency: "
                "python3 -m pip install garmin-fit-sdk  (by installing it you accept Garmin's FIT Protocol License)")


def list_workouts(start: str | None = None, end: str | None = None) -> list[dict]:
    flt = " and ".join(f"date {op} {v}" for op, v in (("ge", start), ("lt", end)) if v)
    query = ('query($f: String) { ObservationList(code: "http://loinc.org|55411-3", _filter: $f, _sort: "-date", '
             '_count: 1000) { identifier { value } effectivePeriod { start end } valueQuantity { value } '
             'component { code { coding { code } } valueQuantity { value } valueCodeableConcept { text } } '
             'derivedFrom { reference } } }')
    out = []
    for o in graphql(query, {"f": flt or None})["data"]["ObservationList"]:
        c = {x["code"]["coding"][0]["code"]: (x.get("valueQuantity") or {}).get("value")
             or (x.get("valueCodeableConcept") or {}).get("text") for x in o.get("component") or []}
        out.append({"activity": o["identifier"][0]["value"].split("/", 1)[1], "start": o["effectivePeriod"]["start"],
                    "end": o["effectivePeriod"]["end"], "type": c.get("73985-4"), "minutes": o["valueQuantity"]["value"],
                    "distance_m": c.get("workout-distance"), "avg_hr": c.get("55425-3"),
                    "fit_file": bool(o.get("derivedFrom"))})
    return out


def fit_bytes(activity: str) -> tuple[bytes, str]:
    """The decoded .fit file of a workout (downloaded from Medplum once, then cached) and its UTC offset."""
    import io
    import re
    import zipfile
    activity = activity.removeprefix("activity/")
    obs = graphql('query($i: String) { ObservationList(identifier: $i) { effectivePeriod { start } } }',
                  {"i": f"{OBS_SYSTEM}|activity/{activity}"})["data"]["ObservationList"]
    offset = obs[0]["effectivePeriod"]["start"][19:] if obs else "+00:00"
    cached = FIT_CACHE / f"{activity}.fit"
    if cached.is_file():
        return cached.read_bytes(), offset
    docs = graphql('query($i: String) { DocumentReferenceList(identifier: $i) { content { attachment { url } } } }',
                   {"i": f"{DOC_SYSTEM}|activity/{activity}/original"})["data"]["DocumentReferenceList"]
    if not docs:
        fail(f"no FIT file in Medplum for activity {activity} (see 'fit list')")
    match = re.search(r"(?:Binary/|/storage/)([0-9a-f-]{36})", docs[0]["content"][0]["attachment"]["url"])
    if not match:
        fail(f"cannot find the Binary id in the attachment of activity {activity}")
    req = urllib.request.Request(f"{BASE}/fhir/R4/Binary/{match.group(1)}",
                                 headers={"Authorization": f"Bearer {token()}", "Accept": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=120) as r:
        archive = zipfile.ZipFile(io.BytesIO(r.read()))
    fit_name = next((n for n in archive.namelist() if n.lower().endswith(".fit")), None)
    if not fit_name:
        fail(f"the archive of activity {activity} holds no .fit file: {archive.namelist()}")
    data = archive.read(fit_name)
    FIT_CACHE.mkdir(parents=True, exist_ok=True)
    fd = os.open(cached, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return data, offset


def decode_fit(data: bytes) -> dict:
    try:
        from garmin_fit_sdk import Decoder, Stream
    except ImportError:
        fail(FIT_SDK_HINT)
    messages, errors = Decoder(Stream.from_byte_array(bytearray(data))).read()
    if errors:
        print(f"medplum_agent: FIT decoder reported: {errors}", file=sys.stderr)
    return messages


def _local(ts, offset: str) -> str | None:
    if ts is None:
        return None
    import datetime as dt
    sign = -1 if offset.startswith("-") else 1
    hours, minutes = (int(x) for x in offset[1:].split(":"))
    zone = dt.timezone(sign * dt.timedelta(hours=hours, minutes=minutes))
    return ts.astimezone(zone).isoformat(timespec="seconds")


def fit_records(messages: dict, offset: str, gps: bool = True) -> list[dict]:
    """record messages (usually one per second) in readable units."""
    sport = str(((messages.get("session_mesgs") or [{}])[0]).get("sport", "")).lower()
    records = messages.get("record_mesgs") or []
    t0 = records[0].get("timestamp") if records else None
    rows = []
    for r in records:
        speed = r.get("enhanced_speed", r.get("speed"))
        cadence = r.get("cadence")
        row = {
            "time": _local(r.get("timestamp"), offset),
            "elapsed_s": int((r["timestamp"] - t0).total_seconds()) if r.get("timestamp") and t0 else None,
            "distance_m": r.get("distance"),
            "speed_mps": round(speed, 3) if speed is not None else None,
            "pace_min_per_km": round(1000 / speed / 60, 2) if speed else None,
            "altitude_m": round(v, 1) if (v := r.get("enhanced_altitude", r.get("altitude"))) is not None else None,
            "heart_rate": r.get("heart_rate"),
            "cadence_spm" if sport in STEP_SPORTS else "cadence_rpm":
                (float(round(2 * (cadence + (r.get("fractional_cadence") or 0)), 1)) if sport in STEP_SPORTS else cadence)
                if cadence is not None else None,
            "power_w": r.get("power"),
            "vertical_oscillation_mm": r.get("vertical_oscillation"),
            "vertical_ratio_pct": r.get("vertical_ratio"),
            "ground_contact_ms": r.get("stance_time"),
            "step_length_mm": r.get("step_length"),
            "temperature_c": r.get("temperature"),
        }
        if gps:
            lat, lon = r.get("position_lat"), r.get("position_long")
            row["lat"] = round(lat * SEMICIRCLE, 6) if lat is not None else None
            row["lon"] = round(lon * SEMICIRCLE, 6) if lon is not None else None
        rows.append(row)
    used = {k for row in rows for k, v in row.items() if v is not None}
    return [{k: v for k, v in row.items() if k in used} for row in rows]


def fit_laps(messages: dict, offset: str) -> list[dict]:
    keep = ("total_elapsed_time", "total_timer_time", "total_distance", "enhanced_avg_speed", "enhanced_max_speed",
            "avg_heart_rate", "max_heart_rate", "avg_power", "max_power", "normalized_power", "avg_running_cadence",
            "total_ascent", "total_descent", "total_calories", "avg_vertical_oscillation", "avg_stance_time",
            "avg_step_length", "intensity", "lap_trigger")
    return [{"lap": i + 1, "start": _local(lap.get("start_time"), offset),
             **{k: (str(lap[k]) if k in ("intensity", "lap_trigger") else lap[k]) for k in keep if lap.get(k) is not None}}
            for i, lap in enumerate(messages.get("lap_mesgs") or [])]


def cmd_fit(rest: list[str]) -> int:
    import csv
    if not rest:
        fail("fit needs a sub-command: list | records <activity> | laps <activity> | messages <activity>")
    sub, args = rest[0], rest[1:]
    opt = lambda name: args[args.index(name) + 1] if name in args else None
    if sub == "list":
        print(json.dumps(list_workouts(opt("--from"), opt("--to")), indent=1))
        return 0
    if not args or args[0].startswith("--"):
        fail(f"fit {sub} needs an activity id (see 'fit list')")
    data, offset = fit_bytes(args[0])
    messages = decode_fit(data)
    if sub == "messages":
        counts = sorted(((str(k), len(v)) for k, v in messages.items()), key=lambda kv: -kv[1])
        print(json.dumps({"documented": {k: n for k, n in counts if not k.isdigit()},
                          "undocumented_garmin_message_numbers": {k: n for k, n in counts if k.isdigit()}}, indent=1))
        return 0
    if sub == "laps":
        print(json.dumps(fit_laps(messages, offset), indent=1, default=str))
        return 0
    if sub != "records":
        fail(f"unknown fit sub-command {sub!r}")
    rows = fit_records(messages, offset, gps="--no-gps" not in args)
    every = int(opt("--every") or 1)
    rows = rows[::max(every, 1)]
    out = open(opt("--out"), "w", newline="") if opt("--out") else sys.stdout
    try:
        if (opt("--format") or "csv") == "json":
            json.dump(rows, out, indent=1)
            out.write("\n")
        else:
            writer = csv.DictWriter(out, fieldnames=list(rows[0]) if rows else ["time"])
            writer.writeheader()
            writer.writerows(rows)
    finally:
        if out is not sys.stdout:
            out.close()
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "token":
        print(token())
    elif cmd == "headers":
        print(json.dumps({"Authorization": f"Bearer {token()}"}))
    elif cmd == "graphql":
        if not rest:
            fail("graphql needs a query or @file")
        query = Path(rest[0][1:]).read_text() if rest[0].startswith("@") else rest[0]
        variables = json.loads(rest[rest.index("--vars") + 1]) if "--vars" in rest else None
        result = graphql(query, variables)
        print(json.dumps(result, indent=1, ensure_ascii=False))
        return 1 if result.get("errors") else 0
    elif cmd == "codes":
        print(json.dumps(codes(rest[0] if rest else None), indent=1, ensure_ascii=False))
    elif cmd == "fit":
        return cmd_fit(rest)
    else:
        fail(f"unknown command {cmd!r}; see --help")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except BrokenPipeError:            # output piped into head, less, ...
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
