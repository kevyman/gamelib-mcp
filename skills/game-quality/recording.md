# Recording a verdict: field-level authoring rules

Companion to `SKILL.md` Step 4. The `record_assessment` tool's own description
carries only what a caller needs to reach the right row safely (identity
resolution, the `resolution` block, the same-day replace rule); the authoring
rules below are methodology and live here, with the skill that produced the
verdict. The server validates and caps everything described here regardless of
whether this file was read — over-cap lists are rejected, long text truncated.

Fetch this file with `get_skill(skill="game-quality", path="recording.md")`.

## The verdict components

`verdict` (required) is the Step 4 line: `"buy_now"`, `"wishlist_for_sale"`,
`"try_demo"`, `"skip"`, or `"play_what_you_own"`. Everything else is optional
and should mirror the verdict block you just delivered: summary (the one-liner,
300 chars), craft_adjusted (the 0-1 adjusted score — NOT the raw percentage,
which is craft_positive_pct 0-100), review_count, recent_trajectory,
opencritic_score, fit_call (the same four strings get_assessment_context's
fit.suggested_call uses), anchors_cited (up to 8 names or {name, game_id}
objects — use the game_ids from the anchors block), flags (up to 8 short
strings), price_seen + price_currency + price_platform, target_price (the
"wishlist at €X" threshold), instead_game_id (the game pointed at by "play what
you own instead: X"), steam_appid, and context (e.g. "bundle: Humble Choice
2026-08"). assessed_at backfills a past verdict (ISO 8601, UTC); it defaults to
now.

## Methodology provenance

skill, skill_version and model record the METHODOLOGY behind the call: the
skill you followed ("game-quality"), the version in ITS frontmatter, and the
model identifier YOUR ENVIRONMENT declares — all three DECLARED ONLY. Copy
them; never guess, never answer from training memory, and omit any your
environment does not state (the server never fills them in, and NULL correctly
means "unknown"). ChatGPT-family clients should record the model FAMILY they
are told, not a guessed router variant. They group
get_stats(report="calibration")'s by_methodology / by_model.

## Presentation fields

elevator_pitch, craft_note, for_you_if, not_for_you_if and comparisons are the
PRESENTATION of the verdict — your own writing, stored with it and rendered on
the evaluation card. elevator_pitch is one synthesized, spoiler-free line (420
chars). craft_note is one line of craft context the chips can't carry — the
critic spread, the recurring knock, the review-bomb caveat (200 chars).
for_you_if / not_for_you_if take up to 4 bullets each (200 chars each), and
each bullet must be GROUNDED IN HIS DATA ("you put 244h into Slay the Spire",
"you abandoned both survival crafters you tried"), never generic genre talk.
comparisons takes up to 6 {name, relation, note, game_id} objects tracing
lineage, with relation one of "better_version", "similar", "ancestor",
"descendant" or "cheaper_substitute"; pass game_id when the library already
resolved that game (a name is matched exactly or not at all). Over-cap lists
are rejected; long text is truncated.

## why_care

why_care takes up to 3 {kind, text} objects — the one-line reasons this game is
worth a look BEFORE the verdict, with kind one of "people" (the credits behind
it: "the Bloodborne combat lead directs this"), "studio" ("Larian's first game
since Baldur's Gate 3"), "anticipation" ("nine years after the last one") or
"moment" ("the first Metroidvania to ship with day-one Steam Deck verified").
Text is capped at 160 chars. SOURCEABLE CLAIMS ONLY: this renders as fact on
the card, so write what you could point at, never a guess about who worked on
what — the server fetches the developer and their previous games itself
(package.pedigree) and never fetches credits, which is exactly the gap this
fills.
