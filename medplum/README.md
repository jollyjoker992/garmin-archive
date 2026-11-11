# Local Medplum

A Medplum FHIR server on this machine, for the FHIR view of the Garmin archive.
It is based on Medplum's official `docker-compose.full-stack.yml`, pinned to
**Medplum 5.2.17** and reachable only from localhost.

| Service | Address | Notes |
|---|---|---|
| FHIR API | http://localhost:8103/fhir/R4 | |
| Admin app | http://localhost:3000 | Sign in with `MEDPLUM_ADMIN_EMAIL` / `MEDPLUM_ADMIN_PASSWORD` from `medplum/.env`. That user is a Medplum super admin. |
| MCP | http://localhost:8103/mcp/stream (and `/mcp/sse`) | Beta feature, enabled with `MEDPLUM_MCP_ENABLED`. |
| Postgres, Valkey | not published | Reachable only inside the compose network. |

## First start

```sh
cd medplum
python3 setup.py init       # writes medplum/.env with random passwords
docker compose up -d        # first start runs migrations; allow a few minutes
python3 setup.py project    # creates the project, policy, clients, terminology
python3 setup.py check      # verifies both clients and the CodeSystem
```

`setup.py project` is idempotent. It creates, or finds again:

| What | Name | Purpose |
|---|---|---|
| Project | `Garmin Archive` | Holds all Garmin resources. Owned by the admin user. |
| ClientApplication | `garmin-archive-loader` | Read-write. Used by the loader. |
| AccessPolicy | `garmin-archive-agent-read-only` | Read and search only, on Patient, Device, Observation, DocumentReference, Binary, CodeSystem, ValueSet. |
| ClientApplication | `garmin-archive-agent` | Uses that policy. For AI agents and other read-only consumers. |
| CodeSystem, ValueSet | `garmin-metrics` (version = mapping version, 0.1.2 now) | Loaded from `fhir/`, so `$lookup`, `$validate-code` and `$expand` work. `garmin_fhir.py load` updates them when the version changes. |

The client credentials are written to the repository's `.env`:
`GARMIN_MEDPLUM_URL`, `GARMIN_MEDPLUM_PROJECT_ID`,
`GARMIN_MEDPLUM_CLIENT_ID` / `_SECRET` (loader) and
`GARMIN_MEDPLUM_AGENT_CLIENT_ID` / `_SECRET` (agent). Both `.env` files are
mode `600` and ignored by git.

A client gets an access token with the OAuth2 client-credentials grant:

```sh
curl -s http://localhost:8103/oauth2/token \
  -d grant_type=client_credentials -d client_id=$ID -d client_secret=$SECRET
```

## Verified on this setup (2026-11-11)

- `/healthcheck` reports `5.2.17-4e75ed3`, Postgres and Redis up.
- Terminology: `CodeSystem/$lookup` returns a garmin-metrics code's display,
  definition and unit; `$validate-code` rejects an unknown code; `ValueSet/$expand`
  returns all 101 codes.
- The agent client can search and read, and gets HTTP 403 on any write, both
  over the FHIR API and through the MCP `fhir-request` tool.
- A transaction of conditional updates (`PUT Observation?identifier=...`) with
  conditional references (`Patient?identifier=...`) creates on the first run
  (201) and updates in place on the second (200, no duplicates); references
  are resolved to `Patient/<id>`.
- Binary upload and byte-identical download work, for the loader and (read) the agent.

## Rate limits

Medplum's defaults are sized for a shared cloud service. For this single-user,
localhost-only server the compose file raises them; rate limiting stays on, so
a runaway loop is still bounded.

| Setting | Medplum default | Here |
|---|---|---|
| `MEDPLUM_DEFAULT_FHIR_QUOTA` (FHIR points per minute, per client) | 50,000 | 10,000,000 |
| Project FHIR quota (10 × the above, derived) | 500,000 | 100,000,000 |
| `MEDPLUM_DEFAULT_RATE_LIMIT` (HTTP requests per minute) | 60,000 | 600,000 |
| `MEDPLUM_DEFAULT_AUTH_RATE_LIMIT` (token/auth requests per minute) | 10 | 300 |

The password-login limit is left at Medplum's default. Measured on
2026-11-11: loading one day for the first time costs about 5,600 points, and
ten back-to-back forced reloads of five days (about 1,650 upserts, 10 s) used
268,010 points, 2.7 % of the per-client minute. A first load of a year of data
fits within one minute's quota. Check usage with
`GET /fhir/R4/Project/<id>/$rate-limits` (admin token) or in the app under
Project Admin → Rate Limits.

## Day-to-day

```sh
docker compose ps                   # status
docker compose logs -f medplum-server
docker compose stop                 # stop, keep data
docker compose start
```

Data lives in two Docker volumes: `garmin-medplum_postgres-data` (all FHIR
resources) and `garmin-medplum_binary-data` (uploaded FIT archives). Everything
in them can be rebuilt from the raw archive with the loader, so they need no
backup of their own.

To start over: `docker compose down -v` (deletes both volumes), then
`docker compose up -d` and `python3 setup.py project`. The passwords in
`medplum/.env` are reused; the client ids and secrets in `.env` are replaced.

## Upgrading Medplum

Change the two image tags `medplum/medplum-server:5.2.17` and
`medplum/medplum-app:5.2.17` together, then `docker compose up -d`. The server
migrates the database on start. Read the release notes first:
https://github.com/medplum/medplum/releases

## Notes

- The server image runs as uid 1000 and has no shell. The one-shot
  `binary-permissions` container gives the Binary volume to that uid; without
  it, Binary uploads fail with `EACCES`.
- Medplum creates six default AccessPolicies in every project; they are not
  ours and are left alone.
- Signing in to MCP through OAuth in a browser logs in as a user, with that
  user's access. For read-only agent access, use the agent client's token
  instead; `docs/agents.md` shows how (Claude Code `.mcp.json` with
  `headersHelper`).
