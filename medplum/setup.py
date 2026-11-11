#!/usr/bin/env python3
"""Set up the local Medplum stack for garmin-archive. Standard library only.

    python3 setup.py init       write medplum/.env with random passwords (once)
    python3 setup.py project    create the project, access policy, clients and
                                terminology on a running server (idempotent)
    python3 setup.py check      show what exists and test both clients

``project`` writes the client credentials to the repository's .env:
GARMIN_MEDPLUM_URL, GARMIN_MEDPLUM_CLIENT_ID / _SECRET (loader, read-write)
and GARMIN_MEDPLUM_AGENT_CLIENT_ID / _SECRET (agents, read-only).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
STACK_ENV = HERE / ".env"
REPO_ENV = REPO / ".env"
BASE_URL = os.environ.get("GARMIN_MEDPLUM_URL", "http://localhost:8103").rstrip("/")
PROJECT_NAME = "Garmin Archive"
LOADER_CLIENT = "garmin-archive-loader"
AGENT_CLIENT = "garmin-archive-agent"
AGENT_POLICY = "garmin-archive-agent-read-only"
READABLE = ["Patient", "Device", "Observation", "DocumentReference", "Binary", "CodeSystem", "ValueSet"]
TERMINOLOGY = [REPO / "fhir" / "CodeSystem-garmin-metrics.json", REPO / "fhir" / "ValueSet-garmin-metrics.json"]


def read_env(path: Path) -> dict[str, str]:
    out = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def set_env(path: Path, values: dict[str, str], header: str) -> None:
    """Add or replace KEY=VALUE lines in an env file, keeping everything else."""
    lines = path.read_text().splitlines() if path.is_file() else []
    keys = set(values)
    kept = [l for l in lines if l.split("=", 1)[0].strip() not in keys]
    if header and header not in kept:
        kept.append(header)
    kept += [f"{k}={v}" for k, v in values.items()]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(kept) + "\n")
    os.chmod(path, 0o600)


def http(method: str, path: str, *, token: str | None = None, body=None, form: dict | None = None,
         content_type: str | None = None, ok=(200, 201)) -> dict:
    headers = {"Accept": "application/fhir+json, application/json"}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = content_type or "application/fhir+json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            text = response.read().decode()
            status = response.status
    except urllib.error.HTTPError as exc:
        text, status = exc.read().decode(), exc.code
    if status not in ok:
        raise SystemExit(f"{method} {path} -> HTTP {status}: {text[:400]}")
    return json.loads(text) if text else {}


def wait_for_server(timeout: int = 600) -> None:
    deadline = time.time() + timeout
    while True:
        try:
            with urllib.request.urlopen(BASE_URL + "/healthcheck", timeout=5) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        if time.time() > deadline:
            raise SystemExit(f"Medplum at {BASE_URL} is not healthy after {timeout}s; see 'docker compose logs medplum-server'")
        time.sleep(5)


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def exchange(code: str, verifier: str) -> str:
    token = http("POST", "/oauth2/token", form={"grant_type": "authorization_code", "code": code, "code_verifier": verifier})
    return token["access_token"]


def admin_token_for_project(email: str, password: str) -> tuple[str, str]:
    """Return (access token, project id) for the garmin project, creating it if needed."""
    verifier, challenge = pkce()
    login = http("POST", "/auth/login", body={"email": email, "password": password, "scope": "openid",
                                               "codeChallenge": challenge, "codeChallengeMethod": "S256"},
                 content_type="application/json")
    for membership in login.get("memberships", []):
        if membership.get("project", {}).get("display") == PROJECT_NAME:
            chosen = http("POST", "/auth/profile", body={"login": login["login"], "profile": membership["id"]},
                          content_type="application/json")
            return exchange(chosen["code"], verifier), membership["project"]["reference"].split("/")[1]
    # No such project yet: start a login for a new project and create it.
    verifier, challenge = pkce()
    login = http("POST", "/auth/login", body={"email": email, "password": password, "scope": "openid",
                                               "projectId": "new", "codeChallenge": challenge,
                                               "codeChallengeMethod": "S256"}, content_type="application/json")
    created = http("POST", "/auth/newproject", body={"login": login["login"], "projectName": PROJECT_NAME},
                   content_type="application/json")
    token = exchange(created["code"], verifier)
    me = http("GET", "/auth/me", token=token)
    print(f"Created project {PROJECT_NAME!r} ({me['project']['id']})")
    return token, me["project"]["id"]


def search_one(token: str, rtype: str, query: str) -> dict | None:
    bundle = http("GET", f"/fhir/R4/{rtype}?{query}", token=token)
    entries = bundle.get("entry") or []
    return entries[0]["resource"] if entries else None


def ensure_policy(token: str) -> dict:
    policy = {
        "resourceType": "AccessPolicy",
        "name": AGENT_POLICY,
        "resource": [{"resourceType": t, "readonly": True} for t in READABLE],
    }
    existing = search_one(token, "AccessPolicy", f"name={AGENT_POLICY}")
    if existing:
        policy["id"] = existing["id"]
        return http("PUT", f"/fhir/R4/AccessPolicy/{existing['id']}", token=token, body=policy)
    return http("POST", "/fhir/R4/AccessPolicy", token=token, body=policy)


def ensure_client(token: str, project_id: str, name: str, policy: dict | None) -> dict:
    existing = search_one(token, "ClientApplication", f"name={name}")
    if existing:
        return existing
    body = {"name": name, "description": f"garmin-archive: {name}"}
    if policy:
        body["accessPolicy"] = {"reference": f"AccessPolicy/{policy['id']}"}
    client = http("POST", f"/admin/projects/{project_id}/client", token=token, body=body, content_type="application/json")
    print(f"Created client {name} ({client['id']})")
    return client


def ensure_terminology(token: str) -> None:
    for path in TERMINOLOGY:
        resource = json.loads(path.read_text())
        rtype, url = resource["resourceType"], resource["url"]
        resource.pop("id", None)
        existing = search_one(token, rtype, f"url={urllib.parse.quote(url, safe='')}")
        if existing:
            resource["id"] = existing["id"]
            http("PUT", f"/fhir/R4/{rtype}/{existing['id']}", token=token, body=resource)
        else:
            http("POST", f"/fhir/R4/{rtype}", token=token, body=resource)
        print(f"{rtype} {url} version {resource.get('version')} loaded")


def client_token(client_id: str, secret: str) -> str:
    return http("POST", "/oauth2/token", form={"grant_type": "client_credentials", "client_id": client_id,
                                                "client_secret": secret})["access_token"]


def cmd_init() -> int:
    env = read_env(STACK_ENV)
    if env.get("POSTGRES_PASSWORD"):
        print(f"{STACK_ENV} already exists; leaving it unchanged")
        return 0
    set_env(STACK_ENV, {
        "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
        "REDIS_PASSWORD": secrets.token_urlsafe(24),
        "MEDPLUM_ADMIN_EMAIL": "admin@example.com",
        "MEDPLUM_ADMIN_PASSWORD": secrets.token_urlsafe(18),
    }, "# Local Medplum stack secrets (created by setup.py init). Do not commit.")
    print(f"Wrote {STACK_ENV}. Admin login for http://localhost:3000: see MEDPLUM_ADMIN_EMAIL / MEDPLUM_ADMIN_PASSWORD there.")
    return 0


def cmd_project() -> int:
    env = read_env(STACK_ENV)
    if not env.get("MEDPLUM_ADMIN_PASSWORD"):
        raise SystemExit("Run 'python3 setup.py init' and 'docker compose up -d' first")
    wait_for_server()
    token, project_id = admin_token_for_project(env["MEDPLUM_ADMIN_EMAIL"], env["MEDPLUM_ADMIN_PASSWORD"])
    policy = ensure_policy(token)
    loader = ensure_client(token, project_id, LOADER_CLIENT, None)
    agent = ensure_client(token, project_id, AGENT_CLIENT, policy)
    ensure_terminology(token)
    set_env(REPO_ENV, {
        "GARMIN_MEDPLUM_URL": BASE_URL,
        "GARMIN_MEDPLUM_PROJECT_ID": project_id,
        "GARMIN_MEDPLUM_CLIENT_ID": loader["id"],
        "GARMIN_MEDPLUM_CLIENT_SECRET": loader["secret"],
        "GARMIN_MEDPLUM_AGENT_CLIENT_ID": agent["id"],
        "GARMIN_MEDPLUM_AGENT_CLIENT_SECRET": agent["secret"],
    }, "# Local Medplum (written by medplum/setup.py project)")
    print(f"Project {project_id}: loader and agent client credentials written to {REPO_ENV}")
    return 0


def cmd_check() -> int:
    env = read_env(REPO_ENV)
    for key in ("GARMIN_MEDPLUM_CLIENT_ID", "GARMIN_MEDPLUM_AGENT_CLIENT_ID"):
        if not env.get(key):
            raise SystemExit("Run 'python3 setup.py project' first")
    wait_for_server(30)
    loader = client_token(env["GARMIN_MEDPLUM_CLIENT_ID"], env["GARMIN_MEDPLUM_CLIENT_SECRET"])
    agent = client_token(env["GARMIN_MEDPLUM_AGENT_CLIENT_ID"], env["GARMIN_MEDPLUM_AGENT_CLIENT_SECRET"])
    cs = search_one(agent, "CodeSystem", "url=" + urllib.parse.quote(json.loads(TERMINOLOGY[0].read_text())["url"], safe=""))
    print("server       :", BASE_URL, "healthy")
    print("project      :", env.get("GARMIN_MEDPLUM_PROJECT_ID"))
    print("loader token : ok")
    print("agent token  : ok")
    print("CodeSystem   :", f"{cs['url']} v{cs.get('version')}" if cs else "MISSING")
    return 0


def main() -> int:
    commands = {"init": cmd_init, "project": cmd_project, "check": cmd_check}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        print(__doc__)
        return 2
    return commands[sys.argv[1]]()


if __name__ == "__main__":
    sys.exit(main())
