# Preview samples

Saved tool payloads for `scripts/preview_game_cards.py --from-json PATH` (no database needed): a top-level `results` key renders the grid, anything else the detail card.
`discover_taste_match.json` is a real `discover_games` response; `detail_ghost_of_tsushima.json` a real `get_game_detail(media=True)` one (its `similar` block filled in by hand).
Each file's `_note` records its provenance and is dropped before injection; `scripts/render_all_previews.sh` renders both alongside the eval samples.

## Images

`scripts/render_all_previews.sh` never fetches art. Set `IMG_CACHE=/path/to/dir` to serve covers, screenshots and posters from a local directory (`scripts/screenshot_widgets.mjs --img-cache`: a request maps to a file by its basename, then `cov_<basename>`, then `got_<basename>`; a YouTube poster by `<video id>_hqdefault.jpg`). Leave it unset and every image is a 1x1 grey placeholder: the run stays offline and the measured heights are unchanged, only the art is missing.
