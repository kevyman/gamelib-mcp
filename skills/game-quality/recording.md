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

## story

story (3.4) is THE STORY block: creator lore with a citation on every sentence.
Wire shape:

    story = {
      "sentences": [{"text": "<≤240 chars>", "sources": [1, 2]}, …],   # 1–4
      "sources": [{"url": "https://…", "kind": "press", "title": "<≤120, optional>"}, …],  # 1–6
    }

`sources` on a sentence are 1-based numbers into `story.sources`. The server
checks STRUCTURE only, never truth: both lists are required and non-empty;
unknown keys are rejected; every sentence must cite at least one source that
exists (a sentence citing none, or a number past the list, is rejected); every
url must start with http:// or https:// and be ≤300 chars (rejected, not
shortened — a truncated url is a broken citation); kind is one of "press",
"studio" (a developer or publisher post), "store" (a store page), "wiki",
"social"; sentence text truncates at 240 chars and a title at 120. A source no
sentence cites is dropped and the numbers re-based. Over-cap lists are
rejected.

The honesty rules, which the server cannot check:

- Every sentence ends in a citation to a url you FETCHED and rests on that
  page's RAW text — never a search snippet or a fetch tool's summary, which
  can paraphrase inside quotation marks and truncate silently (3.4.2). A
  sentence you cannot cite is deleted, not hedged.
- A person-level lineage claim ("the Life is Strange writer wrote this") needs
  a NAMED person in at least one non-store, non-wiki source; a shared studio
  name only ever supports "same studio".
- A wiki-only claim is dropped unless the primary source it cites was fetched
  and confirms it.
- No "same team" or team-size claims unless a source states them; roles are
  worded as the source words them; secondhand claims say "reported".
- The store blurb's "from the creators of…" is NOT a source for a people
  claim: the server already shows it, attributed, as "The store says"
  (package.pedigree.store_claim). Don't restate it in the story.
- What qualifies (3.4.1): at least one sentence about a NAMED person or about
  how the game was made. Business and sales facts alone (a parent company's
  stake, units sold, a listing) are not a story — omit the block and say
  "No sourced story found" in chat; never pad it.
- The research is mandatory for every candidate (SKILL.md Step 0 item 5); the
  block is omitted only when the searches found no qualifying story, and
  the omission is said in chat.
- why_care is authored independently of the story and is never dropped
  because the story was omitted; the only coupling is no verbatim repetition.
- He reads these as fact: write for him, name his game, no hype.
