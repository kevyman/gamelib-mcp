# Changelog

All notable changes to gamelib-mcp. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/) from 1.0.0 on.

## [Unreleased]

## [1.0.0] — 2026-10-05

First tagged release. Everything before this shipped straight from `main`.

### Added
- `SECURITY.md` and this changelog.

### Upgrading an existing deployment
If the clone on the server is not at `~/gamelib-mcp`, add a `DEPLOY_PATH`
repository secret with its absolute path BEFORE merging or pulling 1.0.0.
Without it the deploy workflow stops at its first step (nothing is deployed or
rolled back). The host also needs `sqlite3`, which the workflow now checks.

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

[Unreleased]: https://github.com/kevyman/gamelib-mcp/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/kevyman/gamelib-mcp/releases/tag/v1.0.0
