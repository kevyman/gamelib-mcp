# OpenCritic Scraping Design

**Date:** 2026-04-07  
**Branch:** feat/platform-tools  
**Status:** Drafted for review

## Problem

The current enrichment design assumes OpenCritic can be queried through a free public API. That assumption is stale. As of April 7, 2026, `api.opencritic.com` requires an API key and points consumers to a paid RapidAPI offering.

This project should not depend on that paid API tier. We still want to populate OpenCritic-derived review columns, so the integration needs to use OpenCritic's public web pages instead.

## Goals

1. Populate OpenCritic review fields without using the paid RapidAPI product
2. Keep scraping out of request-time user flows
3. Match the correct game edition when OpenCritic distinguishes between base game, remake, remaster, or special edition
4. Be polite to OpenCritic with pacing, jitter, and bounded retries
5. Make failures easy to understand through structured logging and deterministic cooldown behavior

## Non-Goals

- Using the RapidAPI OpenCritic product
- Performing OpenCritic scraping synchronously from `get_game_detail` or other request-time paths
- Building a generic web scraping framework for other providers
- Solving every edge case in v1 for obscure title variants; unmatched titles may remain empty and be revisited later

---

## Data Model

OpenCritic data remains stored on the per-platform enrichment row, even though the OpenCritic score is a cross-platform aggregate. This keeps the data available from each platform view and aligns with the rest of the platform-aware enrichment design.

### Required fields

Store these fields in `game_platform_enrichment`:

- `opencritic_id`
- `opencritic_url`
- `opencritic_score`
- `opencritic_tier`
- `opencritic_percent_recommended`
- `opencritic_num_reviews`
- `opencritic_fetched_at`

### Optional future fields

These are intentionally out of scope for v1, but the parser may be designed so they can be added later without changing the matching contract:

- `opencritic_median_score`
- `opencritic_platforms`
- `opencritic_release_date`
- `opencritic_description`

### Cache and cooldown semantics

- Upcoming through 180 days after release: successful fetch TTL `7 days`
- Older than 180 days after release: no automatic refresh after the first successful scrape
- Ambiguous or no-match cooldown: `7 days`
- Transient failure cooldown: `1 day`

For v1, cooldown state can be inferred from timestamp columns and structured log output. If that becomes too coarse, a dedicated status column can be added later.

---

## Source Strategy

Use a dual discovery strategy.

### Primary discovery

Try to discover a game through OpenCritic-controlled pages first. If the site exposes enough server-rendered search results to rely on, use them directly.

### Fallback discovery

If OpenCritic-native discovery is unavailable, too JS-heavy, or does not return a confident match, fall back to constrained web search against OpenCritic game pages, for example:

```text
site:opencritic.com/game "<title>"
```

This fallback is only for discovery. The canonical scraped values must still come from OpenCritic pages, not from search snippets.

### Canonical scrape target

Prefer the OpenCritic `.../export` page when it exposes the required values consistently. Fall back to the main game page if a required field is unavailable on the export page.

When parsing pages, prefer embedded structured state in the HTML over brittle CSS selectors. The parser should normalize HTML-escaped JSON before deserialization.

---

## Matching Strategy

Matching mode is `Hybrid`.

### Auto-accept rules

Accept automatically when:

- The normalized source title and normalized candidate title match exactly
- Punctuation-only differences disappear after normalization
- Minor formatting changes such as apostrophes, colons, ampersands, or roman numeral formatting still resolve to the same normalized title

### Edition-aware rules

Edition-distinguishing tokens are meaningful and should not be ignored. If the source title explicitly includes edition markers, prefer candidates with the same marker set.

Examples:

- A title containing `Remake` should prefer a candidate containing `Remake`
- A title containing `Remaster` should prefer a candidate containing `Remaster`
- A base title should not be silently upgraded to a remake or remaster unless normalization and the remaining evidence clearly indicate the same edition

For v1, maintain an explicit keyword list in code that is small and easy to extend. Start with:

- `remake`
- `remaster`
- `definitive edition`
- `director's cut`
- `complete edition`
- `game of the year edition`
- `anniversary edition`
- `hd`
- `dx`

### Tie-breakers

If multiple candidates remain plausible after normalization and edition filtering, use the following tie-breakers when available:

1. Exact edition token agreement
2. Release year agreement
3. Platform hint agreement
4. Strongest exact normalized title match

Do not choose the "most popular" or highest-scored candidate as a tie-breaker.

### Ambiguity handling

If multiple candidates are still plausible after applying normalization, edition tokens, and available hints, skip enrichment for that title and log `ambiguous`.

### No-match handling

If no candidate survives the matching rules, skip enrichment and log `no_match`.

---

## Scraping Behavior

OpenCritic scraping runs only in the background enrichment pipeline.

### Request flow

For each unenriched or stale platform row:

1. Check release-aware refresh policy and cooldown state
2. Discover candidate OpenCritic URLs
3. Choose one canonical match or skip
4. Fetch the export page
5. Parse required fields
6. Upsert the OpenCritic columns atomically
7. Emit a structured result code

### Fields parsed in v1

The parser should extract:

- OpenCritic numeric ID
- Canonical OpenCritic URL
- Top critic score
- Tier
- Percent recommended
- Number of reviews

If one optional field is missing, keep processing. If a required field is missing or malformed, treat that as `parse_failed`.

### Failure behavior

- `no_match`: leave OpenCritic columns null; retry after no-match cooldown
- `ambiguous`: leave OpenCritic columns null; retry after ambiguity cooldown
- `parse_failed`: leave existing cached values untouched; retry later
- `http_error`: leave existing cached values untouched; retry using transient-failure cooldown
- `rate_limited`: back off and retry within the current cycle up to the retry cap

---

## Politeness And Reliability

Scraping must be intentionally paced.

### Request identity

Use a dedicated user agent for this project that clearly identifies the client.

### Refresh eligibility

Successful OpenCritic records should not all be refreshed on a fixed global TTL.

Rules:

- If the game is upcoming, keep refreshing on the short success TTL
- If the game was released within the last 180 days, keep refreshing on the short success TTL
- If the game is older than 180 days and already has a successful OpenCritic scrape, do not auto-refresh it again
- Old games without a successful scrape are still eligible until they succeed or hit a cooldown condition

This policy is intentionally biased toward one-time enrichment for old catalog titles, since their OpenCritic scores are unlikely to change in meaningful ways.

### Base pacing

Apply a normal delay of roughly:

- `2s + 0-1s jitter`

This pacing should be global per host, not per game, so multiple workers cannot burst accidentally.

### Retry policy

Use exponential backoff with jitter for transient failures such as:

- `429`
- `5xx`
- Connection resets
- Timeouts

Recommended retry schedule:

- Retry 1: `4s + jitter`
- Retry 2: `8s + jitter`
- Retry 3: `16s + jitter`

Cap retries per item per background cycle at `3`.

### Concurrency

OpenCritic scraping should run with effectively single-host pacing. If the enrichment system becomes parallel elsewhere, the OpenCritic step still needs a shared limiter so aggregate request rate stays polite.

---

## Module Shape

Implement a dedicated `data/opencritic.py` module with four responsibilities:

- `discover_candidates(title)` for OpenCritic-first and search-fallback discovery
- `choose_match(game, candidates)` for normalized and edition-aware matching
- `fetch_opencritic_record(url)` for polite HTTP with pacing, jitter, and retries
- `parse_opencritic_record(html)` for extracting required fields from export or main pages

Expose one orchestration entrypoint for the enrichment job:

- `enrich_opencritic_for_game_platform(...)`

That entrypoint should:

- check cache and cooldown state
- attempt discovery
- resolve a canonical match or skip
- fetch and parse OpenCritic data
- write OpenCritic fields and timestamps
- return a structured status code for logs and metrics

This keeps parsing, discovery, matching, and persistence separable and testable.

---

## Background Pipeline Integration

OpenCritic remains a background enrichment phase and does not run synchronously from request handlers.

For v1:

- Process all relevant `game_platform` rows missing fresh OpenCritic data
- Order work by playtime or another existing priority signal already used by enrichment
- Respect the OpenCritic-specific pacing and retry policy regardless of the wider enrichment worker configuration

This keeps request latency clean and isolates scraper failures from user-facing flows.

---

## Testing

Avoid tests that depend on live OpenCritic pages.

### Unit tests

Cover:

- Title normalization
- Edition-keyword extraction
- Hybrid matcher decisions
- Ambiguity decisions

### Fixture-based parser tests

Store representative saved HTML fixtures and test parsing against them:

- Standard exact match with all required fields
- Punctuation-heavy title
- Remake or remaster title
- Export page missing one optional field
- Export page shape drift that should produce `parse_failed`

### Integration tests

Mock HTTP responses and assert:

- cached records are not refetched before TTL
- `429` triggers retry and backoff behavior
- ambiguous titles are skipped and cooled down
- no-match titles are skipped and cooled down
- successful parse writes the expected DB fields

---

## Risks

### OpenCritic page shape drift

HTML structure may change. Mitigation: prefer structured embedded state and keep parser logic narrow.

### Discovery coverage gaps

Some titles may not be discoverable cleanly through OpenCritic-first lookup. Mitigation: constrained search fallback and explicit `no_match` logging.

### False positives on editions

Overly loose normalization could map a base game to a remake or remaster. Mitigation: explicit edition-keyword list and hybrid ambiguity skipping.

### Worker throughput

Polite pacing will make OpenCritic enrichment relatively slow. Mitigation: background-only execution and long success TTL.

---

## Decision Summary

- Do not use RapidAPI
- Scrape public OpenCritic pages
- Use OpenCritic-first discovery with constrained search fallback
- Run scraping in background enrichment only
- Use hybrid matching
- Match exact editions when possible
- Maintain an explicit small edition-keyword list in code
- Apply global per-host pacing, jitter, and bounded exponential backoff
