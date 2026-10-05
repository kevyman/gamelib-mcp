# Changelog

All notable changes to gamelib-mcp. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/) from 1.0.0 on.

## [Unreleased]

## [1.0.0] — 2026-10-05

First tagged release. Everything before this shipped straight from `main`.

### Added
- `MCP_OAUTH_GITHUB_LOGINS`: allowlist GitHub accounts by username as well as
  by numeric id.
- `SECURITY.md` and this changelog.

### Changed
- The three served skills (`game-quality` 3.5.0, `backlog-triage` 2.3.0,
  `bundle-evaluation` 1.3.0) now address "the user" and read every personal
  fact from the library instead of carrying one person's history.
- Caddy reads its site address from `MCP_DOMAIN` in `.env`; the Caddyfile no
  longer names a domain. `docker-compose.yml` refuses to start Caddy without it.
- `deploy.md` is a generic runbook for any VPS; the deploy workflow's clone
  path is `DEPLOY_PATH` (default `~/gamelib-mcp`); `scripts/deploy_stacks_assets.sh`
  reads `GAMELIB_DEPLOY_HOST` / `GAMELIB_DEPLOY_DIR`.
- `.env.example` carries placeholders instead of one deployment's values.

[Unreleased]: https://github.com/kevyman/gamelib-mcp/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/kevyman/gamelib-mcp/releases/tag/v1.0.0
