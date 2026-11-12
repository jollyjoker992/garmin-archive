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

Workout detail (one row per second from the FIT file):

```sh
python3 tools/medplum_agent.py fit list
python3 tools/medplum_agent.py fit records <activity> --no-gps > run.csv
python3 tools/medplum_agent.py fit laps <activity>
```

The FIT archive is downloaded from Medplum (`Binary`, read-only client) and
decoded with Garmin's FIT SDK, an optional install
(`python3 -m pip install garmin-fit-sdk`, under Garmin's FIT Protocol License).

Date filters must carry the account's UTC offset (e.g. `+07:00`); ranges use
`_filter: "date ge <from> and date lt <to>"`. Medplum's GraphQL `date`
argument takes one value only.

## PDF reports

```sh
python3 -m pip install -r tools/requirements-reports.txt     # once: matplotlib, reportlab, garmin-fit-sdk
python3 tools/garmin_report.py activity <activity> --facts facts.json
python3 tools/garmin_report.py activity <activity> --narrative narrative.json   # -> data/reports/activity-<id>.pdf
python3 tools/garmin_report.py wellness --week 2026-W45 --facts facts.json      # or --month 2026-10, --from/--to
python3 tools/garmin_report.py wellness --week 2026-W45 --narrative narrative.json
```

The script computes the facts and draws the charts; the evaluation and
recommendations in `narrative.json` are written by a person or an agent from
those facts (the `garmin-reports` skill describes how).

Reports can be in any language: set `"language"` in `narrative.json`. English
and Vietnamese labels are built in; for others, translate the output of
`python3 tools/garmin_report.py labels` into `"labels"`. The PDF embeds DejaVu
Sans (Latin incl. Vietnamese, Greek, Cyrillic); pass `--font FILE.ttf` for other
scripts. The script refuses to render characters the font cannot show.

## Skills

- `.claude/skills/garmin-health-analysis/SKILL.md`: analysing the data (where each
  metric is, rules that prevent wrong answers, tested GraphQL recipes).
- `.claude/skills/garmin-reports/SKILL.md`: PDF reports for an activity or a
  week/month of wellness.
- `.claude/skills/garmin-archive/SKILL.md`: operating the backup (status,
  filling gaps, restore).

Claude Code picks both up automatically in this repository.
