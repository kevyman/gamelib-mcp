# Preview samples

Saved tool payloads for `scripts/preview_game_cards.py --from-json PATH` (no database needed): a top-level `results` key renders the grid, anything else the detail card.
`discover_taste_match.json` is a real `discover_games` response; `detail_ghost_of_tsushima.json` a real `get_game_detail(media=True)` one (its `similar` block filled in by hand).
Each file's `_note` records its provenance and is dropped before injection; `scripts/render_all_previews.sh` renders both alongside the eval samples.
