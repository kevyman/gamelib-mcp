# ADR 0003: consolidated `check_library` validation checks

Status: accepted (2026-07-25)

## Context
- Data-integrity detection grew haphazardly, one MCP tool at a time:
  `detect_farmed_games`, `detect_collapsed_games`, `detect_orphan_games`,
  `detect_stranded_duplicates`, `detect_cross_platform_collapses`,
  `detect_misclassified_dlc`, `revalidate_igdb_matches`, and
  `audit_steam_licenses` each shipped with their own ad-hoc response shape,
  their own severity vocabulary (or none), and their own idea of whether a
  finding was actionable.
- Every one of these tools was report-only except `audit_steam_licenses`
  (which always minted rows) and `detect_farmed_games`/`revalidate_igdb_matches`
  (which took a `dry_run` flag) — three different conventions for "does this
  write," discoverable only by reading each docstring.
- None of them agreed on how to name a repair. A caller (human or agent) had
  to read prose and manually translate a finding into the right follow-up
  tool call (`merge_games`, `update_game`, `split_game`, `delete_game`, …).
- The set kept growing in an unstructured way: nothing stopped a ninth
  `detect_whatever_games` tool from reinventing the same shape yet again, and
  nothing enforced that a new detector's docstring stayed in sync with what
  it actually returned.
- Two real gaps existed with no detector at all: an owned edition SKU nesting
  under an empty, unowned base-game shell (the exact shape `detect_orphan_games`
  reported as an undeletable `phantom_parent` with no path forward) had no
  suggested repair; and several classes of drift (dangling parent links,
  wishlist rows that should have been cleared, playtime that goes backward,
  duplicate purchase imports, price typos, enrichment coverage, sync
  staleness) had no detector whatsoever.

## Decision
1. **One registry tool, not one tool per detector.** `tools/checks.py` defines
   `CHECKS: dict[str, CheckSpec]`, a frozen registry of check ids
   (`category.name`, e.g. `nesting.superseded_base`). `main.py` exposes exactly
   one MCP tool, `check_library`, that selects and runs a subset of the
   registry. The 8 existing detectors above are removed as standalone MCP
   tools; their underlying functions in `tools/admin.py` /
   `data/steam_licenses.py` are kept byte-for-byte (same logic, same unit
   tests) and adapted into findings by `checks.py`. Adding a check is a pure
   registry addition, not a new top-level tool with its own wire schema to
   maintain and keep in sync.

2. **Every finding is report-only with a machine-readable repair pointer.**
   The uniform envelope — `check`, `severity` (`notice`/`warning`/`error`),
   `game_id`, `name`, `message`, `evidence`, `suggested_action` — replaces
   eight bespoke response shapes with one. `suggested_action` names an
   *existing* repair tool and its args (`merge_games`, `update_game`,
   `split_game`, `delete_game`, `set_acquisition`, `refresh_library`, or
   `check_library` itself for the apply-gated checks) whenever a safe,
   concrete suggestion exists; otherwise it is `None`, and the human-facing
   reasoning moves into `message`/`evidence` instead of being silently
   dropped. `check_library` itself never mutates library data as a side
   effect of merely running.

3. **Exactly three checks may write, and only behind `apply`.** `playtime.farming`,
   `extid.igdb_drift`, and `ownership.license_gap` are the only
   `writes_on_apply` checks (the same three write paths the old tools had,
   now uniformly gated). They default to report mode; a write happens only
   when the check's id is explicitly listed in the `apply` parameter, and an
   applied check must also be selected to run. Every other check — including
   all 10 new ones this ADR adds — is permanently report-only; there is no
   escape hatch to make them write later without a new ADR, because
   report-only is what makes it safe to run the full registry unattended.

4. **The edition-becomes-canonical supersession stance.** When a store
   replaces a base game with an owned edition SKU (e.g. "Burnout Paradise:
   The Ultimate Box" is the only owned row, nested under an empty, unowned
   "Burnout Paradise" shell), the previously-undeletable `phantom_parent`
   finding gets a concrete resolution: `nesting.superseded_base` suggests
   `merge_games(source_game_id=<shell>, target_game_id=<heir>)`, where the
   heir is the owned child with the most playtime, then the most store
   identifiers, then the lowest id (a deterministic tie-break). This relies
   on `merge_games` already promoting a nested target that absorbs its own
   parent to primary — decision 9 of ADR 0002 plus the merge's
   `target_promoted_to_primary` behavior — so no new write path was needed,
   only a new finding that points at the existing one. A parent with at least
   one owned child is reported *exclusively* under `nesting.superseded_base`;
   `nesting.phantom_parent` takes only `owned_child_count == 0`. The same
   underlying detector (`detect_orphan_games`) backs both ids — they are a
   split of one query's results, not two independent scans, so the two
   findings can never disagree about which bucket a given parent belongs to.

5. **Offline by default, network checks opt-in.** `checks=None` (the default)
   runs every check whose `network` is `None` — zero HTTP calls guaranteed.
   A network check (`network="igdb"` or `"steam+steamspy"`) only runs when
   named explicitly in `checks` or when `include_network=True`; an
   unconfigured credential/session lands the check in `checks_skipped`
   (`unconfigured:igdb` / `unconfigured:steam_session`) rather than raising.
   This flips `nesting.misclassified`'s old default (it used to probe Steam
   by default) specifically so the *consolidated* tool's default run stays
   network-free — a caller who wants the deeper probe opts in per-call via
   `options`.

6. **Ten new checks fill the gaps ADR 0002 and production experience
   surfaced**: `nesting.superseded_base` (decision 4 above),
   `identity.unlinked_edition` (an edition-suffixed sibling that never got
   linked into its family), `nesting.dangling_parent` (a broken parent
   chain — missing row, self-reference, or a parent that is itself nested),
   `wishlist.already_owned` (the fulfillment sweep missed a row or it was
   hand-edited), `playtime.snapshot_regression` (a `play_history` total that
   went backward — cumulative totals must be monotonic),
   `playtime.orphan_switch_summary` (Parental Controls playtime with no
   matching library game), `spend.duplicate_purchase` and
   `spend.price_anomaly` (import/typo hygiene over acquisition data),
   `enrich.coverage` (library-wide tags/igdb_id/cover/hltb_main gaps), and
   `sync.staleness` (a platform that stopped syncing, or synced but has
   current playtime ahead of its latest snapshot — divergence, not snapshot
   age, since snapshots only write on change and an idle library is
   healthy). All ten are offline, permanently
   report-only, and most resolve to `None` suggested_action with the
   reasoning folded into `message`/`evidence` — these are judgment calls
   (which purchase row to clear, whether two rows really are the same game)
   that a human should make, not a repair a tool should auto-select.

7. **Suppressions are tool config, not library data.** `suppress`/
   `unsuppress` persist `{check, game_id}` pairs in the `meta` KV
   (`check_suppressions`) and post-filter findings on every future run. This
   does not violate the report-only stance: it changes what `check_library`
   *shows*, never what the library *contains*.

8. **The check-id vocabulary is frozen API, drift-guarded.** `main.py`'s
   `check_library` docstring documents every registered id with a one-liner;
   `tests/test_tool_registration.py` asserts every `CHECKS` id appears in that
   docstring, so adding a check without documenting it fails CI — the same
   discipline `query.py`'s `TABLE_ANNOTATIONS` guard already applies to
   `get_db_schema`.

## Consequences
### Positive
- Tool surface shrinks from 58 to 51 (net: -8 detector tools, +1
  `check_library`) while check *coverage* grows (8 migrated + 10 new = 18
  checks in the registry).
- One envelope, one selection model, one apply-gating rule, one suppression
  mechanism — a caller learns `check_library` once and gets every check's
  behavior for free, instead of re-deriving each tool's dry-run/mint/apply
  convention from its docstring.
- The "owned edition behind an empty shell" shape — previously a dead-end
  `phantom_parent` with a prose-only remediation hint — now carries a
  concrete, correctly-targeted `merge_games` suggestion.
- Adding check #19 is additive: a new `CheckSpec` in the registry plus a
  docstring one-liner, not a new top-level tool, model, and test file.

### Negative / revisit triggers
- The 8 migrated detectors' *logic* is frozen in `tools/admin.py` /
  `data/steam_licenses.py` on purpose (their unit tests test that logic
  directly), which means `checks.py`'s adapters are a second place that must
  stay in sync with each detector's return shape. A detector's return-shape
  change requires updating its adapter in the same commit — nothing enforces
  this today beyond the adapter tests in `tests/test_checks.py`.
- The `nesting.superseded_base`/`nesting.phantom_parent` split depends on
  `detect_orphan_games`'s `owned_child_count` field staying accurate; if that
  detector's query ever changes shape without updating both consuming
  adapters, a phantom parent could silently double-report or vanish from
  both ids. A future audit tool (or an assertion in `tests/test_checks.py`)
  could guard the invariant "every phantom parent appears in exactly one of
  the two ids" more directly if this area sees more churn.
- If a fourth check ever needs to write, it needs the same `writes_on_apply`
  treatment as the existing three — there is no reason to expect this stays
  at three forever, but each addition should be deliberate (a new ADR
  amendment, not a silent flag flip), since report-only-by-default is the
  property that makes `check_library()` safe to run with no arguments at all.

## Amendment (2026-07-25): first-sweep corrections

The first full production sweep (~3,092 owned games) found the registry
sound but four of its decisions too coarse. All four are refinements, not
reversals; the report-only stance and the three apply-gated ids are unchanged.

1. **The phantom-parent split is three-way, not two-way.** Decision 4 assumed
   an owned child of an unowned parent means "the edition superseded the base
   game." Production says otherwise: owning DLC *without* its base game is a
   legitimate state (an Epic giveaway, a route pack for an unowned Train Sim
   World), and merging there renames a base-game row to a DLC title and
   flattens its siblings underneath it. `nesting.superseded_base` now requires
   an EDITION heir — `content_type = "edition"`, or a name that is an
   edition-suffixed form of the parent's under
   `normalize_edition_comparison_title`. The DLC shape moves to a new
   report-only id, `ownership.dlc_without_base`, so the three ids still
   partition every phantom parent exactly once (`owned_child_count == 0` →
   `nesting.phantom_parent`; an edition heir → `nesting.superseded_base`;
   otherwise → `ownership.dlc_without_base`).

2. **A name difference is not automatically a wrong IGDB link.**
   `extid.igdb_drift` compared library and IGDB names under
   `normalize_series_gap_title` only, so correct edition→base links ("Nioh 2 -
   The Complete Edition" → "Nioh 2", "Mass Effect (2007)" → "Mass Effect")
   reported as drift — and an `apply` would have thrown away good enrichment
   for nothing. Both names now also go through
   `normalize_edition_comparison_title`; agreement there classifies the row
   `drift_kind="edition_suffix"`, which is counted in the summary and excluded
   from findings (and therefore from resets) unless
   `options.include_edition_suffix` asks for it.

3. **`include_network` widens only the default selection.** It used to union
   every network check into an explicit `checks` list, so asking for
   `ownership.license_gap` also ran `extid.igdb_drift` and
   `identity.cross_store_collapse`. Naming a network check in `checks` was
   always sufficient to run it; `include_network` is now inert when `checks`
   is given.

4. **A bundle split is not a duplicate purchase.** `spend.duplicate_purchase`
   grouped on the acquisition tuple and then accepted any same-FAMILY pair,
   which is precisely the shape `split_bundle_acquisition` writes (a base game
   and its DLC each carrying the bundle's per-item share). Family pairs whose
   rows share a `bundle_name` are no longer reported; same-game and same-name
   pairs — the real signal — still are.

Two related fixes outside the registry, both surfaced by the same sweep:
`ownership.license_gap`'s mint path no longer flags `delisted=1` when the
store lookup SUCCEEDED (absence from `GetOwnedGames` also covers
never-launched apps), and after an apply the healed licenses come back as
notice-level findings naming the minted `game_id` instead of repeating "is
absent from the library". The `delisted` column also gained its first manual
write path (`add_game_to_platform(delisted=...)`, pinned via
`game_platforms.manual_overrides`), because a report-only registry needs the
column it writes to be correctable by hand.
