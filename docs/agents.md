# Connecting agents to the health data

The local Medplum (see `medplum/README.md`) serves the Garmin data as FHIR R4
over REST, GraphQL and MCP. Agents use the **read-only agent client**
(`GARMIN_MEDPLUM_AGENT_CLIENT_ID` / `_SECRET` in `.env`). Its access policy
allows read and search on Patient, Device, Observation, DocumentReference,
Binary, CodeSystem and ValueSet; every write is refused (HTTP 403, or
`Forbidden` in GraphQL and MCP).

| Interface | Address | For |
|---|---|---|
| MCP | `http://localhost:8103/mcp/stream` (streamable HTTP), `/mcp/sse` | Agents with MCP support (Claude Code, Codex, Cursor, Copilot, ...). Tools: `fhir-request`, `search`, `fetch`. |
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

## Other agents (Antigravity, Codex, Cursor, Copilot)

Agents that can run shell commands do not need MCP at all: the skills use
`python3 tools/medplum_agent.py graphql|codes|fit`, which gets and renews the
read-only token by itself. MCP is optional.

To connect MCP anyway, give the agent the read-only token in an environment
variable. A token lasts one hour, so set it right before starting the agent and
restart the agent when it expires:

```sh
export GARMIN_MEDPLUM_TOKEN="$(python3 tools/medplum_agent.py token)"
```

| Agent | Config (not committed; add it yourself) |
|---|---|
| Codex | `~/.codex/config.toml`, or `.codex/config.toml` in a trusted project: `[mcp_servers.garmin-medplum]` with `url = "http://localhost:8103/mcp/stream"` and `bearer_token_env_var = "GARMIN_MEDPLUM_TOKEN"`. Codex also documents `http_headers_helper` (a command that prints JSON headers, like Claude Code's `headersHelper`); `python3 tools/medplum_agent.py headers` prints that format, but this has not been tested here. |
| Cursor | `.cursor/mcp.json`: `{"mcpServers": {"garmin-medplum": {"url": "http://localhost:8103/mcp/stream", "headers": {"Authorization": "Bearer ${env:GARMIN_MEDPLUM_TOKEN}"}}}}` |
| GitHub Copilot CLI | Reads the repository's `.mcp.json` after you trust the folder, but not `headersHelper`; put the server in `~/.copilot/mcp-config.json` with `"type": "http"`, the URL and `"headers": {"Authorization": "Bearer <token>"}`. |
| Antigravity CLI | `~/.gemini/config/mcp_config.json`: `{"mcpServers": {"garmin-medplum": {"serverUrl": "http://localhost:8103/mcp/stream", "headers": {"Authorization": "Bearer <token>"}}}}`. The token is written into the file, so replace it when it expires. Use this user-level file, not the workspace `.agents/mcp_config.json`, which would put the token in the repository. |
| Gemini CLI | Paid plans only since June 2026: `gemini mcp add --scope user --transport http --header "Authorization: Bearer $GARMIN_MEDPLUM_TOKEN" garmin-medplum http://localhost:8103/mcp/stream`. |

These formats come from each vendor's documentation (checked November 2026)
and change often; check them there if something does not connect. Do not
commit a file that contains a token.

Do not sign in to MCP with the browser OAuth flow for agent use: that logs in
as the admin user, who can change data.

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
python3 tools/garmin_report.py day --date 2026-11-11 --facts facts.json          # one day vs the previous 28 days
python3 tools/garmin_report.py day --date 2026-11-11 --narrative narrative.json  # -> data/reports/day-2026-11-11.pdf
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

The skills are in `.agents/skills/`, the folder most agents read
(`SKILL.md` format: a `name` and `description` header, then instructions):

- `.agents/skills/garmin-health-analysis/SKILL.md`: analysing the data (where each
  metric is, rules that prevent wrong answers, tested GraphQL recipes).
- `.agents/skills/garmin-reports/SKILL.md`: PDF reports for an activity, a day,
  or a week/month of wellness.
- `.agents/skills/garmin-archive/SKILL.md`: operating the backup (status,
  filling gaps, restore).

| Agent | Finds them through |
|---|---|
| Claude Code | `.claude/skills`, a link to `.agents/skills` |
| Antigravity CLI, Codex, Cursor, Gemini CLI, Copilot CLI and coding agent | `.agents/skills` |
| GitHub Copilot in VS Code | `.github/skills` only: add `ln -s ../.agents/skills .github/skills` locally if you use it |

`AGENTS.md` holds the rules every agent must follow (Medplum, not `data/`;
never re-crawl old days; no secrets). Claude Code reads it through
`CLAUDE.md`; Antigravity, Codex, Cursor and Copilot read it directly; Gemini
CLI through the committed `.gemini/settings.json` (`context.fileName`).

### Antigravity CLI (and Gemini CLI)

Since 18 June 2026 Gemini CLI no longer serves free, Google AI Pro and Ultra
accounts ("This client is no longer supported for Gemini Code Assist for
individuals"); Google's replacement is Antigravity CLI (`agy`, see
https://antigravity.google/docs/cli/gcli-migration). It reads `AGENTS.md` and
`.agents/skills/` as they are, so nothing in this repository needs changing.

1. Start Medplum, then run `agy` in the repository folder.
2. Ask, for example, "How did I sleep last week?". Shell commands outside its
   sandbox ask for approval; allow `python3 tools/medplum_agent.py ...` and
   `python3 tools/garmin_report.py ...`. MCP is not needed.
3. Check that it answers from Medplum and does not open files under
   `data/days/`.

Gemini CLI on a paid plan works the same way; trust the folder so it loads
`.gemini/settings.json`, and check with `/memory show` and `/skills list`.

On Windows, Git checks out the `.claude/skills` link as a plain file unless
symlinks are enabled (`git config core.symlinks true` and Developer Mode).
