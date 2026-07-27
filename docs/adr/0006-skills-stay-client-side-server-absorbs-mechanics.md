# ADR 0006: Gaming skills stay client-side; the server absorbs their mechanics

Status: accepted (2026-07-27)

Answers issue #114 (spike: should skills be served from the MCP server
itself?). The bundle-evaluation skill issue should cite this ADR for where
that skill lives.

## Context

The gaming workflows (game-quality, backlog-triage; bundle-evaluation
planned) live client-side as Claude skills — a folder with a SKILL.md and
bundled scripts, installed per machine. Two pressures motivated the spike:
every client needs its own install, and the skills drift from the server's
actual tool surface.

**The drift is not hypothetical.** As of this investigation the installed
skills still reference `get_taste_profile`, `get_backlog_stats`,
`get_spending_stats`, `get_series_breakdown`, `get_wishlist_deals`, and
`search_games_batch` — pre-ADR-0004 names that survive only as internal
implementation functions. The wire surface consolidated them into
`get_stats(report=...)`, `get_wishlist`, and `search_games` in July 2026;
the skills were never updated. Clients muddle through by finding the
equivalent tool, at the cost of failed lookups and guesswork on every run.

**What "run from the MCP" can and cannot mean.** MCP cannot execute a skill
server-side — a skill is instructions the *client* model follows. The real
question is which server primitive can *distribute* the workflow, and what
hosts honor.

### Host support as of 2026-07-27

- **Claude Code**: client skills (`~/.claude/skills`, project `.claude/skills`,
  plugins) trigger *automatically* by description match. MCP prompts are
  surfaced as slash commands (`/mcp__<server>__<prompt>`) — user-invoked
  only. MCP resources are readable/@-mentionable on demand; nothing
  auto-loads them. No support for any server-distributed-skills mechanism.
- **claude.ai** (registered client, ADR 0004 probe): custom connectors
  support tools, prompts, and resources per the published connector docs;
  prompts and resources are user/model-pulled, never auto-triggered.
  claude.ai also has its own client-side Skills (capabilities) — a second
  *install target*, not a distribution channel; installing there doubles the
  drift surface rather than removing it.
- **chatgpt.com** (the other registered client): tools only in practice; no
  skills concept. Any distribution mechanism chosen must degrade to nothing
  gracefully there.
- **Emerging standard**: the MCP Skills Over MCP working group's SEP-2640
  ("Skills Extension", Extensions Track, **draft**) serves skills as plain
  resources under `skill://<skill-path>/<file>` URIs with an optional
  `skill://index.json` discovery index, explicitly scoped to "servers
  shipping skills that describe their own tools" — exactly this case. It
  introduces no new protocol methods and works on 2025-11-25 as-is. **No
  host implements it yet.** ADR 0005's adoption rule (stable + a registered
  client speaks it) applies unchanged.

### The two asymmetries that decide the split

1. **Triggering is client-side magic.** The best property of the current
   setup is that "is Hades II any good?" fires game-quality without anyone
   remembering it exists. No server primitive reproduces that today:
   prompts must be invoked by name, resources must be pulled. Moving a
   skill wholesale server-side trades away the UX that makes it work.
2. **The mechanics are server-shaped.** The skills bundle deterministic
   scripts (`craft_score.py`'s sample-adjusted Steam sentiment,
   `fit_check.py`'s tag-affinity check) and *mandate client web search* for
   Steam review counts because the client sandbox can't reach Steam — while
   this server already scrapes Steam reviews (the `steam_reviews` healable
   scraper) and holds the taste profile, anchor candidates, and play-pace
   data behind 3–4 separate tool calls. The parts of the skills that drift
   are precisely the parts describing server plumbing.

## Decision

1. **Skills stay client-side.** The triggering layer, judgment methodology
   (anchors, genre calibration, context gates), and verdict formats remain
   Claude skills. This is the only form any host auto-triggers today.
   New gaming workflows (bundle-evaluation included) follow the same split.
2. **This repo becomes the canonical home of the skill text.** The skill
   folders move into the repository and version with the server; installed
   copies (`~/.claude/skills`, claude.ai Skills) are downstream copies of
   the repo, stamped with a version in frontmatter. Fixing the current
   pre-ADR-0004 tool-name drift is part of that move.
3. **The server absorbs the skills' mechanical core as tools** (own issue,
   ADR 0004 conventions: mode selectors, strictest annotation, bounded
   responses — and mindful of the 29-tool budget):
   - *Craft scoring*: the sample-adjusted sentiment formula computed
     server-side, using the server's own Steam review data where fresh, so
     the skill stops shipping a script and stops mandating a web search for
     numbers the server already has.
   - *Assessment context*: one gathering call returning what game-quality's
     Step 0 currently assembles from four (detail + taste profile + anchor
     candidates + play pace).
   The skills shrink toward pure methodology; the plumbing that drifted
   migrates to the side that versions with the server by definition.
4. **Serve the canonical skill text as MCP resources now**, using the
   SEP-2640 `skill://` URI convention and `skill://index.json` — plain
   resources on the stable 2025-11-25 revision, so no ADR 0005 conflict.
   This is the pragmatic middle ground issue #114's open question asked
   about: a claude.ai or Claude Code session on a machine without the
   skill can pull the methodology on demand (and the server `instructions`
   can point at it), and the layout is already the emerging standard's
   shape. Adopt the extension's `skills/list`-style surface only when it
   reaches official status AND a registered client honors it.
5. **Assessment recording is follow-up work shaped by this ADR** (own
   issues): a table recording verdict *components* (adjusted craft score,
   review n, fit call, anchors cited, price seen, verdict, timestamp),
   surfaced as read-only context in `get_game_detail` (past verdicts,
   repeat-ask detection) plus a calibration report comparing verdicts
   against subsequent acquisition/playtime/ratings. **Verdicts never feed
   `tag_affinity` or `discover_games` scoring** — affinity stays grounded
   in actual ratings and playtime; mining model output back into ranking
   creates a self-reinforcement loop. Verdict-driven wishlist promotion is
   deferred: wishlist writes stay manual for now (and can only ever target
   the internal `game_wishlist` — Steam and DekuDeals have no wishlist
   write APIs; those syncs are one-way inbound).

### Migration sketch

1. Move skill folders into the repo; document the install step; stamp
   versions in frontmatter. Fix the stale tool references against the
   post-ADR-0004 surface while moving.
2. Serve them as `skill://` resources + `index.json`; point to the index
   from server `instructions`.
3. Add the craft-scoring and assessment-context tools; slim the skills'
   Step 0/Step 1 to call them.
4. Assessment recording + calibration report (own issue). Wishlist
   promotion from verdicts (own issue, deferred).
5. Watch SEP-2640; expose the official skills surface when the ADR 0005
   adoption bar is met.

## Rejected (and why)

- **Whole skills as MCP prompts** — loses automatic triggering (prompts are
  user-invoked slash commands on every host that surfaces them at all);
  prompts are flat text and cannot ship the scripts; and always-advertised
  prompt text is paid by hosts that will never use it. Prompts remain an
  option later as thin *entry points* that reference the resources, if
  on-demand invocation from claude.ai proves clumsy.
- **Building on SEP-2640's discovery surface now** — draft status, zero
  host support; ADR 0005 forbids building for spec features no registered
  client drives. Serving plain resources in its URI shape is the entire
  concession to it.
- **claude.ai Skills upload as the cross-client answer** — a second install
  target doubles drift; it distributes nothing.
- **One server tool that returns the verdict** — the judgment layer
  (anchor reasoning, genre calibration, context gates, honest hedging)
  is model work, not a deterministic function; live-event checks and
  unwishlisted-price lookups still need client web search; and ADR 0004's
  rejected list already warns against the mega-tool shape. Tools carry data
  and deterministic scoring; skills carry judgment.
- **Feeding recorded verdicts into affinity/discovery scoring** — the
  feedback-loop problem above. Read-only context and calibration only.

## Consequences

### Positive
- The drift class that actually occurred (plumbing references going stale)
  is eliminated structurally: plumbing lives server-side, and the skill
  text itself versions with the server as its canonical home.
- Craft scoring gets cheaper and more reliable — server-held review data
  replaces a mandatory client web search when fresh.
- Any connected client can pull the methodology on demand via resources;
  chatgpt.com loses nothing it ever had.
- Pre-positioned for skills-over-MCP at zero protocol risk.

### Negative
- Automatic triggering still requires a per-machine skill install until
  hosts implement a skills-over-MCP mechanism; the resource copy helps an
  uninstalled session only when someone thinks to pull it.
- Repo copy and installed copies can still diverge between installs —
  mitigated by the version stamp and canonical-home rule, not solved.
- Two more tools on a deliberately small 29-tool surface (ADR 0004's
  budget discipline applies to the additions).
- The skills become useless without the server reachable — accepted; every
  step of these workflows already depends on the MCP.
