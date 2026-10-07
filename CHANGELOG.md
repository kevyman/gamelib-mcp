# Changelog

All notable changes to gamelib-mcp. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/) from 1.0.0 on.

## [Unreleased]

## [1.0.1] — 2026-10-07

### Fixed
- The Steam Web API key (and the IsThereAnyDeal key) no longer appear in logs
  or in sync status: the HTTP client's request lines are not logged at INFO,
  and every stored or logged sync failure has `key=`-style query values
  redacted before it reaches `get_sync_status`, `get_integration_status` or
  `/health`.
- `.env.local.example` works for a plain `uv run` as well as Docker: it no
  longer pins the database and session files to the container's `/data`,
  which a laptop user cannot write to.

### Added
- A release workflow: pushing a `v*` tag publishes
  `ghcr.io/kevyman/gamelib-mcp` (amd64 and arm64, tagged with the version and
  `latest`) and opens a GitHub Release. README documents running the image
  without a clone, and `docker-compose.yml` names it beside the local build.
- README: the Claude Code one-liner for connecting to a local server.

## [1.0.0] — 2026-10-05

First tagged release. Everything before this shipped straight from `main`.

### Added
- `SECURITY.md` and this changelog.

### Upgrading an existing deployment
If the clone on the server is not at `~/gamelib-mcp`, add a `DEPLOY_PATH`
repository secret with its absolute path BEFORE merging or pulling 1.0.0.
Without it the deploy workflow stops at its first step (nothing is deployed or
rolled back). The host also needs `sqlite3`, which the workflow now checks.
A local Docker `.env` copied before 1.0.0 needs `MCP_PUBLIC_BASE_URL=http://localhost:8000`
added: compose now requires the value for the Caddy service even when it is
not selected.

### Changed
- The three served skills (`game-quality` 3.5.0, `backlog-triage` 2.3.0,
  `bundle-evaluation` 1.3.0) now address "the user" and read every personal
  fact from the library instead of carrying one person's history.
- Caddy reads its site address from `MCP_PUBLIC_BASE_URL`, the origin the app
  already requires; the Caddyfile no longer names a domain.
- The deploy workflow builds before it recreates containers (a build failure
  never touches the running app), stops the app before inspecting the schema on
  every rollback path, treats a first deploy's fresh database as nothing to
  restore, and fails early when the host lacks `sqlite3` or the clone path.
- `deploy.md` is a generic runbook for any VPS; the deploy workflow's clone
  path is `DEPLOY_PATH` (default `~/gamelib-mcp`); `scripts/deploy_stacks_assets.sh`
  reads `GAMELIB_DEPLOY_HOST` / `GAMELIB_DEPLOY_DIR`.
- `.env.example` carries placeholders instead of one deployment's values.

[Unreleased]: https://github.com/kevyman/gamelib-mcp/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/kevyman/gamelib-mcp/releases/tag/v1.0.1
[1.0.0]: https://github.com/kevyman/gamelib-mcp/releases/tag/v1.0.0
