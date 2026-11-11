# Connecting agents to the health data

The local Medplum (see `medplum/README.md`) serves the Garmin data as FHIR R4
over REST, GraphQL and MCP. Agents use the **read-only agent client**
(`GARMIN_MEDPLUM_AGENT_CLIENT_ID` / `_SECRET` in `.env`). Its access policy
allows read and search on Patient, Device, Observation, DocumentReference,
Binary, CodeSystem and ValueSet; every write is refused (HTTP 403, or
`Forbidden` in GraphQL and MCP).

| Interface | Address | For |
|---|---|---|
| MCP | `http://localhost:8103/mcp/stream` (streamable HTTP), `/mcp/sse` | Agents with MCP support (Claude Code, other MCP clients). Tools: `fhir-request`, `search`, `fetch`. |
| GraphQL | `POST http://localhost:8103/fhir/R4/$graphql` | Analysis: ask for exactly the fields needed. |
| REST | `http://localhost:8103/fhir/R4/` | Anything else. |

Tokens come from `POST /oauth2/token` with `grant_type=client_credentials` and
last one hour. `tools/medplum_agent.py` does this, caches the token in
`data/.medplum-agent-token.json` (mode 600) and renews it five minutes before
it expires.

## Claude Code (MCP)

The repository's `.mcp.json` declares the server:

```json
{
  "mcpServers": {
    "garmin-medplum": {
      "type": "http",
      "url": "http://localhost:8103/mcp/stream",
      "headersHelper": "python3 tools/medplum_agent.py headers"
    }
  }
}
```

`headersHelper` is run by Claude Code at the start of each session, on
reconnect, and again after a 401/403; it prints
`{"Authorization": "Bearer <token>"}`. It runs in the repository folder, must
finish within 10 seconds (a cached token takes about 0.1 s), and gets no
secret-looking environment variables, which is why the script reads the client
secret from `.env` itself. The file holds no secret and is safe to commit.

**One-time setup, interactive:**

1. Start Medplum (`cd medplum && docker compose up -d`).
2. Run `claude` in the repository folder. Accept the **workspace trust** prompt
   (Claude Code does not run a project's `headersHelper` before that) and
   **approve** the `garmin-medplum` server when asked.
3. Check with `/mcp` in the session, or `claude mcp get garmin-medplum` in a
   shell: status should be connected.

Then ask, for example: "Using garmin-medplum, list my resting heart rate for
the last 14 days."

Before step 2 the server shows `Pending approval`, and a non-interactive run
(`claude -p`) reports `headersHelper not run — this workspace has no persisted
trust`. Both are expected until the folder is trusted once.

## Other MCP clients

Any client that can send a static header works for an hour at a time:

```sh
python3 tools/medplum_agent.py token     # paste as "Authorization: Bearer <token>"
```

Prefer a client that can run a command for headers, like Claude Code's
`headersHelper`. Do not sign in to MCP with the browser OAuth flow for agent
use: that logs in as the admin user, who can change data.

## GraphQL and scripts

```sh
python3 tools/medplum_agent.py graphql '{ ObservationList(code: "http://loinc.org|40443-4", _sort: "-date", _count: 7) { effectivePeriod { start } valueQuantity { value unit } } }'
python3 tools/medplum_agent.py codes stress      # meaning and unit of custom codes
```

Date filters must carry the account's UTC offset (e.g. `+07:00`); ranges use
`_filter: "date ge <from> and date lt <to>"`. Medplum's GraphQL `date`
argument takes one value only.

## Skills

- `.claude/skills/garmin-health-analysis/SKILL.md`: analysing the data (where each
  metric is, rules that prevent wrong answers, tested GraphQL recipes).
- `.claude/skills/garmin-archive/SKILL.md`: operating the backup (status,
  filling gaps, restore).

Claude Code picks both up automatically in this repository.
