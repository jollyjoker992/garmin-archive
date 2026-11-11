#!/usr/bin/env python3
"""Read-only access to the local Medplum for agents and scripts. Standard library only.

Always uses the *agent* client (GARMIN_MEDPLUM_AGENT_CLIENT_ID / _SECRET from the
repository's .env), whose access policy allows reading and searching only.

    python3 tools/medplum_agent.py token              print an access token
    python3 tools/medplum_agent.py headers            print {"Authorization": "Bearer ..."} (MCP headersHelper)
    python3 tools/medplum_agent.py graphql QUERY      run a GraphQL query, print the JSON result
    python3 tools/medplum_agent.py graphql @file.graphql [--vars '{"from": "..."}']
    python3 tools/medplum_agent.py codes [TEXT]       list garmin-metrics codes (unit, definition), optionally filtered

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
    else:
        fail(f"unknown command {cmd!r}; see --help")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
