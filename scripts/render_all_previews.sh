#!/usr/bin/env bash
# Render every widget preview variant and screenshot it, offline.
#
#   scripts/render_all_previews.sh            # -> /tmp/gl-previews
#   OUT=/some/dir IMG_CACHE=/path/to/img scripts/render_all_previews.sh
#
# HTML goes to $OUT/html, PNGs to $OUT (<variant>-<width>.png). Inline
# variants are shot at 360 and 760 px, fullscreen variants at 760 only. The
# game cards render from the saved payloads in scripts/preview_samples/ (no
# database). Needs Node with a resolvable `playwright` module (see
# scripts/screenshot_widgets.mjs). Ends with the measured-height table.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${OUT:-/tmp/gl-previews}"
IMG_CACHE="${IMG_CACHE:-/tmp/claude-0/-home-user-gamelib-mcp/e1ccfc03-cae7-5be8-96fb-b7b0b565e6c9/scratchpad/canvas/img}"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="python3"
SAMPLES="$ROOT/scripts/preview_samples"
HTML="$OUT/html"
mkdir -p "$HTML"

inline=()
fullscreen=()

run() {  # a preview script, quiet unless it fails
  local log
  if ! log="$("$PY" "$@" 2>&1)"; then echo "$log" >&2; exit 1; fi
}

eval_card() {  # eval_card NAME KIND ARGS...
  local name="$1" kind="$2"; shift 2
  run "$ROOT/scripts/preview_eval_card.py" "$@" -o "$HTML/$name.html"
  if [[ "$kind" == full ]]; then fullscreen+=("$HTML/$name.html"); else inline+=("$HTML/$name.html"); fi
}

game_cards() {  # game_cards NAME KIND ARGS...
  local name="$1" kind="$2"; shift 2
  run "$ROOT/scripts/preview_game_cards.py" "$@" -o "$HTML/$name.html"
  if [[ "$kind" == full ]]; then fullscreen+=("$HTML/$name.html"); else inline+=("$HTML/$name.html"); fi
}

for n in 0 1 2 3 4; do
  for theme in light dark; do
    eval_card "eval$n-$theme" inline "$n" --theme "$theme"
  done
done
eval_card eval0-dark-fullscreen full 0 --theme dark --fullscreen
eval_card eval1-dark-fullscreen full 1 --theme dark --fullscreen
eval_card eval0-dark-expanded inline 0 --theme dark --expanded

for theme in light dark; do
  game_cards "grid-$theme" inline --from-json "$SAMPLES/discover_taste_match.json" --theme "$theme"
  game_cards "detail-$theme" inline --from-json "$SAMPLES/detail_ghost_of_tsushima.json" --theme "$theme"
done
game_cards grid-dark-fullscreen full --from-json "$SAMPLES/discover_taste_match.json" \
  --theme dark --display fullscreen
game_cards detail-dark-fullscreen full --from-json "$SAMPLES/detail_ghost_of_tsushima.json" \
  --theme dark --display fullscreen

cache_args=()
if [[ -d "$IMG_CACHE" ]]; then
  cache_args=(--img-cache "$IMG_CACHE")
else
  echo "render_all_previews: no image cache at $IMG_CACHE; images will hit the network" >&2
fi

table="$OUT/heights.txt"
{
  node "$ROOT/scripts/screenshot_widgets.mjs" --out "$OUT" "${cache_args[@]}" \
    --width 360,760 "${inline[@]}"
  node "$ROOT/scripts/screenshot_widgets.mjs" --out "$OUT" "${cache_args[@]}" \
    --width 760 "${fullscreen[@]}"
} > "$table"

echo "wrote $(ls "$OUT"/*.png | wc -l) PNGs to $OUT"
awk 'BEGIN { printf "%-26s %5s %7s %11s\n", "variant", "width", "height", "scrollWidth" }
     { sub("height=", "", $3); sub("scrollWidth=", "", $4)
       printf "%-26s %5s %7s %11s\n", $1, $2, $3, $4 }' "$table"
