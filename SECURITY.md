# Security

This tool handles personal health data and credentials (Garmin API key, cloud
storage credentials, Medplum client secrets). Please report a vulnerability
privately through GitHub: **Security → Report a vulnerability** on this
repository. Do not open a public issue for it.

## Design rules the code follows

- Secrets live only in `.env`, `medplum/.env` and `docker/secrets.env`, all
  ignored by git and excluded from the Docker build context. Files the tool
  writes with credentials or tokens are created with mode 600.
- No secret is printed or logged. CI scans the full git history with gitleaks
  on every push and pull request.
- The Garmin key is used only with Garmin's official Personal API; no account
  password is ever handled.
- The cloud copy can be encrypted with rclone crypt (optional, off by default;
  `docs/operations.md`). The key is generated from the system's random source,
  passed to rclone on standard input, never on a command line, and stored only
  in rclone's configuration file, which the tool rewrites with mode 600.
- Agents get a read-only Medplum client; the local Medplum stack listens on
  127.0.0.1 only.

## If you leaked a secret

Revoke it at its source first (Garmin Connect personal API key page, AWS IAM,
Google account → third-party access, `python3 medplum/setup.py project` after
deleting the client in Medplum). Removing it from git history afterwards is not
enough on its own.
