# Security policy

gamelib-mcp is a single-user server that sits on the public internet behind
OAuth and holds store sessions for one person's accounts, so vulnerability
reports are taken seriously and handled privately.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository
(**Security → Report a vulnerability**). Do not open a public issue for
anything that could let someone read a library, use a stored store session,
or call tools they are not allowlisted for.

Expect an acknowledgement within a week. Fixes ship as a normal release;
the advisory is published once a fixed version is tagged.

## Scope

In scope: the HTTP surface (`/mcp`, `/admin/*`, `/ingest/*`, `/health`),
the OAuth flow and the GitHub allowlist, the session-ingest links, the
read-only SQL tool's authorizer, the scrape-config healer's host allowlist,
and anything that could reach the SQLite database or the session files.

Out of scope: the third-party sites the server scrapes or calls (report
those to their owners), and findings that need a configuration this project
documents as unsafe (`MCP_AUTH_MODE=disabled` reachable from the internet).

## Supported versions

Only the latest tagged release and `main` receive fixes.

## What the deployment relies on

- `MCP_AUTH_MODE` must be explicit; the server fails closed otherwise.
- Tools are restricted to the GitHub accounts in `MCP_OAUTH_GITHUB_USER_IDS`
  / `MCP_OAUTH_GITHUB_LOGINS`. Numeric IDs are the safer form.
- `/admin/*` needs its own bearer token (`MCP_ADMIN_AUTH_TOKEN`).
- Session files are written owner-only and never echoed back through MCP.
- CI audits the locked dependencies with `pip-audit` on every pull request
  and weekly on `main`; workflow actions and base images are pinned by digest.
