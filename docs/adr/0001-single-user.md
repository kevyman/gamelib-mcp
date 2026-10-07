# ADR 0001: gamelib-mcp is single-user by design

Status: accepted (2026-07-02)

## Context
- Every identity input is process-level: STEAM_ID, PSN/Nintendo/Epic session
  material, DEKUDEALS_WISHLIST_URL are env vars or mounted files owned by one
  person. MCP_OAUTH_GITHUB_USER_IDS may list several GitHub accounts, but they
  authorize access to the *same* library (household semantics), not per-user data.
- Every table assumes one owner: ratings, tag_affinity, game_wishlist,
  nintendo_play_summary, and meta (hardware_preference, sync timestamps) have
  no user dimension.
- The audit (docs/audit/2026-07-01-repo-audit-and-roadmap.md, item 7) asked us
  to either write this down or plan a user_id column now.

## Decision
Single-user (single-library) is an explicit non-goal. New features MUST NOT
add per-user parameters, tables, or auth distinctions; they may assume "the
user" is the deployment owner.

## Consequences
- Positive: tool signatures, queries, migrations, and caching stay simple;
  the OAuth allowlist remains an access-control list, not an identity system.
- Negative / revisit triggers: a second person wanting their *own* ratings,
  taste profile, or wishlist requires superseding this ADR. The retrofit
  surface at that point: a users table; user_id on ratings, tag_affinity,
  game_wishlist, meta-per-user keys; per-user env/session storage for
  platform credentials; and AuthMiddleware mapping GitHub identity → user_id.
  Nothing else in the data model (games, game_platforms, enrichment) is
  per-user — the split is preferences-vs-catalog, and the catalog stays shared.
- The simplest multi-person accommodation — several GitHub IDs sharing one
  household library — already works today and is unaffected.
