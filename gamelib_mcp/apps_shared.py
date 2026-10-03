"""Blocks shared by the two MCP Apps widgets (apps.py, apps_eval.py).

Each constant is a verbatim slice of widget HTML — CSS rules or JS functions —
that both widgets splice into their own document at the same position. The
widgets stay self-contained ON THE WIRE: every ``ui://`` resource is still one
standalone HTML string with no build step, no CDN and no cross-resource fetch.
Only the Python source is shared, so a fix to the trailer stage or the carousel
lands in both widgets at once instead of being hand-ported (and forgotten).

Splice, never reformat: the constants carry their own indentation and trailing
newline, and a widget's HTML is the literal chunks and these constants
concatenated in order. What is deliberately NOT here is anything the two
widgets genuinely disagree on — the grid's cover plates, its stacking overlay,
the evaluation card's verdict stamp — that stays local to its widget.
``tests/test_apps_eval.py::WidgetDriftTests`` fails if a block of any size
worth sharing reappears in both files instead.

Design system (docs/specs/2026-10-03-widget-ux-redesign.md §1): every color,
radius, border width, shadow and font is a ``--gl-*`` custom property defined
ONCE in ``TOKENS_CSS`` as ``var(<host token>, <fallback>)``. The host's
``hostContext.styles.variables`` (Claude's theme tokens) win when present; the
``light-dark()`` fallbacks keep ChatGPT, Goose and the offline preview
rendering, and follow ``hostContext.theme`` (via ``color-scheme``) or, with no
host at all, ``prefers-color-scheme``. Widget CSS references ``--gl-*`` names
only. Type is three sizes (16 / 14 / 12px) in two weights (400 / 600);
nothing renders below 12px.
"""

import json

from .platforms_registry import PLATFORMS
from .tools.acquisition import PURCHASE_SOURCES

# ---- Labels (generated) -----------------------------------------------------
# Every platform field on the wire (suggested_platform, platforms[].platform,
# ownership.platforms, price.platform) is a registry name; the widgets render
# these labels instead of the raw id. tests/test_apps.py::LabelTests fails when
# a registry name or a record_assessment verdict literal has no explicit entry.
_PLATFORM_DISPLAY: dict[str, str] = {
    "steam": "Steam",
    "epic": "Epic Games",
    "gog": "GOG",
    "switch2": "Switch 2",
    "ps5": "PS5",
    "xbox": "Xbox",
    "itchio": "itch.io",
    "ea": "EA app",
    "ubisoft": "Ubisoft Connect",
    "other": "Other",
}


def _humanize(raw: str) -> str:
    """Python twin of the JS ``humanize``: underscores out, words title-cased."""
    return " ".join(word[:1].upper() + word[1:] for word in raw.replace("_", " ").split())


def _platform_labels() -> dict[str, str]:
    labels: dict[str, str] = {}
    for spec in PLATFORMS:
        text = _PLATFORM_DISPLAY.get(spec.name, _humanize(spec.name))
        labels[spec.name] = text
        # Aliases ("nintendo", "origin", "uplay") are accepted inputs, not wire
        # values — mapped anyway so a stray one still reads as its platform.
        for alias in spec.aliases:
            labels.setdefault(alias, text)
    return labels


PLATFORM_LABELS: dict[str, str] = _platform_labels()

# record_assessment's verdict Literal (main.py) == tools/assessment.py's
# ASSESSMENT_VERDICTS; a test pins all three together.
VERDICT_LABELS: dict[str, str] = {
    "buy_now": "Buy now",
    "wishlist_for_sale": "Wishlist for a sale",
    "try_demo": "Try the demo",
    "skip": "Skip",
    "play_what_you_own": "Play what you own",
}

# ownership.purchase_source is tools/acquisition.py's closed PURCHASE_SOURCES
# vocabulary. The card reads it as "paid €12.00 via <label>", so the labels
# are phrased to follow "via"; tests/test_apps.py::LabelTests fails when a
# vocabulary value has no explicit entry (unknown ones would humanize).
_PURCHASE_SOURCE_DISPLAY: dict[str, str] = {
    "steam": "Steam",
    "gog": "GOG",
    "epic": "Epic Games Store",
    "eshop": "Nintendo eShop",
    "psn": "PlayStation Store",
    "xbox": "Xbox Store",
    "humble": "Humble",
    "fanatical": "Fanatical",
    "itchio": "itch.io",
    "ea": "EA app",
    "ubisoft": "Ubisoft Store",
    "key_reseller": "a key reseller",
    "physical": "a physical copy",
    "gift": "a gift",
    "free": "a free giveaway",
    "subscription": "a subscription",
    "other": "another store",
}


def _purchase_source_labels() -> dict[str, str]:
    return {
        source: _PURCHASE_SOURCE_DISPLAY.get(source, _humanize(source))
        for source in sorted(PURCHASE_SOURCES)
    }


PURCHASE_SOURCE_LABELS: dict[str, str] = _purchase_source_labels()

# Provider prefixes as they appear in error strings and enrichment reasons.
PROVIDER_LABELS: dict[str, str] = {
    "steam": "Steam",
    "igdb": "IGDB",
    "hltb": "HowLongToBeat",
    "opencritic": "OpenCritic",
    "metacritic": "Metacritic",
    "protondb": "ProtonDB",
    "youtube": "YouTube",
    "itad": "IsThereAnyDeal",
    "steamspy": "SteamSpy",
    "backloggd": "Backloggd",
}

# ---- Design tokens, reset, accessibility ------------------------------------
# The ONE place a color/radius/shadow/font value is written. Widget CSS uses
# the --gl-* names only. Raw color values appear in exactly three places:
# this block, the verdict stamp rule (apps_eval.py, built from tokens) and the
# cover plate (its gradient is generated per name in coverNode; its ink is the
# --gl-plate-* tokens below). tests/test_apps.py::DesignSystemTests pins that.
TOKENS_CSS = r"""  :root {
    color-scheme: light dark;
    --gl-text: var(--color-text-primary, light-dark(#141413, #FAF9F5));
    --gl-text-2: var(--color-text-secondary, light-dark(#3D3D3A, #C2C0B6));
    --gl-muted: var(--color-text-tertiary, light-dark(#73726C, #9C9A92));
    --gl-surface: var(--color-background-primary, light-dark(#FFFFFF, #30302E));
    --gl-inset: var(--color-background-secondary, light-dark(#F5F4ED, #262624));
    --gl-border: var(--color-border-tertiary, light-dark(rgba(31, 30, 29, 0.15), rgba(222, 220, 209, 0.15)));
    --gl-border-strong: var(--color-border-primary, light-dark(rgba(31, 30, 29, 0.4), rgba(222, 220, 209, 0.4)));
    --gl-good: var(--color-text-success, light-dark(#265B19, #7AB948));
    --gl-ok: var(--color-text-warning, light-dark(#5A4815, #D1A041));
    --gl-bad: var(--color-text-danger, light-dark(#7F2C28, #EE8884));
    --gl-good-bg: var(--color-background-success, light-dark(#E9F1DC, #1B4614));
    --gl-ok-bg: var(--color-background-warning, light-dark(#F6EEDF, #483A0F));
    --gl-bad-bg: var(--color-background-danger, light-dark(#F7ECEC, #602A28));
    --gl-good-edge: var(--color-border-success, light-dark(#437426, #599130));
    --gl-ok-edge: var(--color-border-warning, light-dark(#805C1F, #A87829));
    --gl-bad-edge: var(--color-border-danger, light-dark(#A73D39, #CD5C58));
    --gl-inverse-bg: var(--color-background-inverse, light-dark(#141413, #FAF9F5));
    --gl-inverse-text: var(--color-text-inverse, light-dark(#FFFFFF, #141413));
    /* Theming exception — the media stage. A media stage is dark in both
       themes by design — it frames video and screenshots — so the stage, its
       veil and scrim, the type on it and the glyph shadow never follow the
       host theme. (The other two exceptions: the verdict stamp, and the cover
       plate's ink below.) The plain --gl-shadow-ink value is the fallback;
       @supports below derives it from --gl-stage. */
    --gl-stage: #0d0b07;
    --gl-stage-veil: rgba(12, 10, 6, 0.32);
    --gl-scrim: rgba(12, 10, 6, 0.5);
    --gl-on-stage: #ffffff;
    --gl-on-stage-dim: rgba(255, 255, 255, 0.78);
    --gl-shadow-ink: rgba(13, 11, 7, 0.5);
    /* Cover-plate ink: the lettering on the name-seeded gradient plate that
       stands in for missing art. The plate is generated art, not a themed
       surface, so its ink is white with a soft shadow in both themes. */
    --gl-plate-ink: rgba(255, 255, 255, 0.92);
    --gl-plate-shadow: rgba(0, 0, 0, 0.35);
    --gl-r-xs: var(--border-radius-xs, 4px);
    --gl-r-sm: var(--border-radius-sm, 6px);
    --gl-r-md: var(--border-radius-md, 8px);
    --gl-r-lg: var(--border-radius-lg, 10px);
    --gl-r-full: var(--border-radius-full, 9999px);
    --gl-bw: var(--border-width-regular, 0.5px);
    --gl-shadow: var(--shadow-sm, 0 1px 3px 0 rgba(0, 0, 0, 0.1), 0 1px 2px -1px rgba(0, 0, 0, 0.1));
    --gl-shadow-md: var(--shadow-md, 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -2px rgba(0, 0, 0, 0.1));
    --gl-font: var(--font-sans, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif);
    /* Exactly three sizes (16 / 14 / 12px). Clamped: a host token below 12px
       never shrinks the type under the floor ("nothing renders below 12px"). */
    --gl-h: max(12px, var(--font-heading-md-size, 16px));
    --gl-body: max(12px, var(--font-text-sm-size, 14px));
    --gl-cap: max(12px, var(--font-text-xs-size, 12px));
    --gl-h-lh: var(--font-heading-md-line-height, 1.4);
    --gl-body-lh: var(--font-text-sm-line-height, 1.4);
    --gl-cap-lh: var(--font-text-xs-line-height, 1.4);
    --gl-regular: var(--font-weight-normal, 400);
    --gl-strong: var(--font-weight-semibold, 600);
    /* Horizontal safe-area insets (set by applyHostContext); the strips'
       scroll-padding reads them. */
    --gl-safe-left: 0px;
    --gl-safe-right: 0px;
  }
  @supports (color: color-mix(in srgb, red 50%, transparent)) {
    :root { --gl-shadow-ink: color-mix(in srgb, var(--gl-stage) 50%, transparent); }
  }
  :root[data-theme="light"] { color-scheme: light; }
  :root[data-theme="dark"] { color-scheme: dark; }
"""

# Box-sizing reset, the transparent page and the body type. The 12px gutter is
# the base the bridge adds safe-area insets to.
RESET_CSS = r"""  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { background: transparent; }
  body {
    font-family: var(--gl-font);
    font-size: var(--gl-body);
    line-height: var(--gl-body-lh);
    font-weight: var(--gl-regular);
    color: var(--gl-text);
    padding: 12px;
    -webkit-font-smoothing: antialiased;
    -webkit-text-size-adjust: 100%;
  }
  button { font: inherit; color: inherit; }
"""

# Focus rings, hit areas and reduced motion. The ring is the text color: a
# solid ring well past 3:1 against the surface in either theme (the 0.4-alpha
# border token was not).
#
# Hit-area rule: every interactive element carries an invisible ::after that
# extends its tap target past its visual box — >=32px on pointer devices, >=44px
# on html.touch — and the extension NEVER reaches a sibling's own box. A chip
# row's gap is therefore at least twice the extension on that axis
# (.chips: 8px rows / 6px columns against -4px / -3px; on touch 12px / 8px
# against -6px / -4px), and on touch the chips themselves grow to a 32px
# visual height so 32 + 2x6 = 44 is met by the chip's own height plus the
# extension, not by overlapping the next row. The host element must be a
# containing block (positioned, or transformed like the stamp); .btn,
# .disclosure and role=button cards are made relative here. A role=button card
# can hold link chips, so its extension sits BEHIND its children (z-index -1
# inside the card's own stacking context) and never steals their taps.
A11Y_CSS = r"""  :focus-visible { outline: 2px solid var(--gl-text); outline-offset: 2px; }
  :focus:not(:focus-visible) { outline: none; }
  .card[role="button"] { position: relative; z-index: 0; }
  a.chip::after, .btn::after, .disclosure::after, .thumb::after, .fs-btn::after,
  .car-nav::after, .overlay-close::after, .card[role="button"]::after, .hero-pill::after,
  .stamp[role="button"]::after, button.stamp::after {
    content: "";
    position: absolute;
    inset: -4px;
  }
  .card[role="button"]::after { z-index: -1; }
  .chips a.chip::after { inset: -4px -3px; }
  html.touch .chip, html.touch .hero-pill { min-height: 32px; }
  html.touch a.chip::after, html.touch .btn::after, html.touch .disclosure::after,
  html.touch .thumb::after, html.touch .fs-btn::after, html.touch .car-nav::after,
  html.touch .overlay-close::after, html.touch .card[role="button"]::after,
  html.touch .hero-pill::after, html.touch .stamp[role="button"]::after,
  html.touch button.stamp::after {
    inset: -6px;
  }
  html.touch .chips a.chip::after { inset: -6px -4px; }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
      animation: none !important;
      transition: none !important;
      scroll-behavior: auto !important;
    }
    .card:hover, .thumb:hover, .play-badge:hover span, .play-badge:focus-visible span {
      transform: none !important;
    }
  }
"""

# The 2:3 cover frame. ``.cover-fallback``'s own type size stays per-widget:
# the grid's plates are larger than the evaluation card's.
COVER_CSS = r"""  .cover-wrap { position: relative; aspect-ratio: 2 / 3; }
  .cover-wrap img, .cover-fallback {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }
"""

# Panel surface, section eyebrow, footnote and the empty state.
PANEL_CSS = r"""  .panel {
    background: var(--gl-surface);
    border: var(--gl-bw) solid var(--gl-border);
    border-radius: var(--gl-r-md);
    box-shadow: var(--gl-shadow);
    padding: 14px;
  }
  .section-title {
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    font-weight: var(--gl-strong);
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--gl-muted);
    margin-bottom: 8px;
  }
  .note {
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    color: var(--gl-muted);
    margin-top: 8px;
    font-variant-numeric: tabular-nums;
  }
  .empty {
    color: var(--gl-muted);
    padding: 20px;
    text-align: center;
  }
"""

# ---- Components CSS: chip, match bar, skeleton, controls, notice -----------
# The ONE chip for every score. Color encodes quality tier only; the brand is
# the label text.
CHIP_CSS = r"""  .chips { display: flex; gap: 8px 6px; flex-wrap: wrap; align-items: center; }
  html.touch .chips { gap: 12px 8px; }
  .chip {
    position: relative;
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    column-gap: 6px;
    row-gap: 0;
    max-width: 100%;
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    font-weight: var(--gl-regular);
    padding: 3px 8px;
    border-radius: var(--gl-r-sm);
    border: var(--gl-bw) solid var(--gl-border);
    background: var(--gl-inset);
    color: var(--gl-text);
    font-variant-numeric: tabular-nums;
    text-decoration: none;
  }
  .chip .lbl { color: var(--gl-text-2); white-space: nowrap; }
  .chip b { font-weight: var(--gl-strong); }
  .chip .aux { color: var(--gl-text-2); white-space: nowrap; }
  .chip.tier-good { background: var(--gl-good-bg); border-color: var(--gl-good-edge); color: var(--gl-good); }
  .chip.tier-ok { background: var(--gl-ok-bg); border-color: var(--gl-ok-edge); color: var(--gl-ok); }
  .chip.tier-bad { background: var(--gl-bad-bg); border-color: var(--gl-bad-edge); color: var(--gl-bad); }
  .chip.tier-none { background: var(--gl-inset); color: var(--gl-text); }
  .chip.tier-good .lbl, .chip.tier-ok .lbl, .chip.tier-bad .lbl,
  .chip.tier-good .aux, .chip.tier-ok .aux, .chip.tier-bad .aux { color: inherit; }
  .chip .meter {
    width: 28px;
    height: 4px;
    border-radius: var(--gl-r-full);
    background: color-mix(in srgb, currentColor 22%, transparent);
    overflow: hidden;
    flex: none;
  }
  .chip .meter-fill { display: block; height: 100%; background: currentColor; }
  .chip .ext { color: inherit; }
  a.chip { cursor: pointer; }
  a.chip:hover { border-color: var(--gl-border-strong); }
"""

# Labelled 4px taste-match bar.
MATCH_BAR_CSS = r"""  .match { display: flex; flex-direction: column; gap: 4px; font-size: var(--gl-cap); line-height: var(--gl-cap-lh); }
  .match b { font-weight: var(--gl-strong); font-variant-numeric: tabular-nums; }
  .match .track {
    display: block;
    height: 4px;
    border-radius: var(--gl-r-full);
    background: var(--gl-inset);
    overflow: hidden;
  }
  .match .fill { display: block; height: 100%; background: var(--gl-inverse-bg); }
"""

# Loading placeholders drawn in the real layouts' shapes (grid, evaluation
# card, detail card); the pulse only runs when the viewer allows motion.
SKELETON_CSS = r"""  .skel { display: flex; flex-direction: column; gap: 12px; max-width: 760px; }
  .skel-grid { max-width: none; gap: 10px; }
  .skel-eval { margin: 0 auto; width: 100%; }
  .skel-detail { max-width: 720px; }
  .sk-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(142px, 1fr)); gap: 12px; }
  .sk-card, .sk-panel {
    background: var(--gl-surface);
    border: var(--gl-bw) solid var(--gl-border);
    border-radius: var(--gl-r-md);
    box-shadow: var(--gl-shadow);
    overflow: hidden;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .sk-body { padding: 10px 12px 12px; display: flex; flex-direction: column; gap: 8px; }
  .sk-panel { padding: 14px; }
  .sk-row { display: flex; gap: 14px; align-items: flex-start; }
  .sk-col { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 8px; padding-top: 4px; }
  .sk { background: var(--gl-inset); border-radius: var(--gl-r-sm); }
  .sk-cover { aspect-ratio: 2 / 3; border-radius: 0; }
  .sk-thumb { flex: 0 0 84px; aspect-ratio: 2 / 3; }
  .skel-detail .sk-thumb { flex-basis: 120px; }
  .sk-head { height: 12px; width: 60%; max-width: 340px; }
  .sk-line { height: 12px; }
  .sk-line.short { width: 55%; }
  .sk-line.wide { height: 16px; width: 70%; }
  .sk-bar { height: 4px; border-radius: var(--gl-r-full); }
  .sk-chips { display: flex; gap: 6px; flex-wrap: wrap; }
  .sk-chip { width: 76px; height: 22px; }
  .sk-card .sk-chip { width: 64px; }
  .sk-stamp { flex: 0 0 96px; height: 40px; border-radius: var(--gl-r-md); }
  .sk-facts { margin-top: 12px; padding-top: 12px; border-top: var(--gl-bw) solid var(--gl-border); }
  .sk-media { aspect-ratio: 16 / 9; border-radius: var(--gl-r-md); }
  .sk-media-row { display: flex; flex-direction: column; gap: 10px; }
  .sk-thumbs { display: flex; gap: 10px; overflow: hidden; }
  .sk-shot { flex: none; width: 116px; height: 66px; }
  @media (min-width: 600px) {
    .skel-eval .sk-media-row { display: grid; grid-template-columns: minmax(0, 1fr) 296px; }
    .skel-eval .sk-thumbs { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; align-content: start; }
    .skel-eval .sk-shot { width: auto; height: auto; aspect-ratio: 16 / 9; }
  }
  @media (max-width: 559px) {
    .skel-detail .sk-thumb { flex-basis: 84px; }
  }
  @media (prefers-reduced-motion: no-preference) {
    .sk { animation: gl-pulse 1.6s ease-in-out infinite; }
  }
  @keyframes gl-pulse { 50% { opacity: 0.5; } }
"""

# Buttons, the disclosure toggle and the muted failure notice.
CONTROLS_CSS = r"""  .btn, .disclosure {
    position: relative;
    min-height: 40px;
    padding: 0 16px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    border-radius: var(--gl-r-md);
    border: var(--gl-bw) solid var(--gl-border-strong);
    background: var(--gl-surface);
    color: var(--gl-text);
    font-size: var(--gl-body);
    font-weight: var(--gl-strong);
    cursor: pointer;
  }
  .btn.primary { background: var(--gl-inverse-bg); color: var(--gl-inverse-text); border-color: transparent; }
  html.touch .btn, html.touch .disclosure { min-height: 44px; }
  .disclosure { width: 100%; }
  .disclosure .chev { transition: transform 0.15s ease; }
  .disclosure[aria-expanded="true"] .chev { transform: rotate(180deg); }
  .disclosure-body { display: flex; flex-direction: column; gap: 12px; margin-top: 12px; }
  .disclosure-body[hidden] { display: none; }
  .notice {
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    color: var(--gl-muted);
    text-align: center;
    padding: 4px 0;
  }
"""

# All component CSS in splice order (one line per widget, not five).
COMPONENTS_CSS = CHIP_CSS + MATCH_BAR_CSS + SKELETON_CSS + CONTROLS_CSS

# ---- Media CSS: hero stage, strips, thumbs ----------------------------------
# The 16:9 trailer/screenshot stage, its poster, play badge and link pill.
HERO_CSS = r"""  .hero {
    position: relative;
    border: var(--gl-bw) solid var(--gl-border);
    border-radius: var(--gl-r-md);
    overflow: hidden;
    background: var(--gl-stage);
    aspect-ratio: 16 / 9;
  }
  .hero-media {
    display: block;
    width: 100%;
    height: 100%;
    border: 0;
    object-fit: cover;
    background: var(--gl-stage);
  }
  .play-badge {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--gl-stage-veil);
    border: 0;
    padding: 0;
    cursor: pointer;
    -webkit-tap-highlight-color: transparent;
  }
  .play-badge span {
    width: 56px;
    height: 56px;
    border-radius: var(--gl-r-full);
    background: var(--gl-surface);
    color: var(--gl-text);
    box-shadow: var(--gl-shadow-md);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: var(--gl-h);
    padding-left: 4px;
    transition: transform 0.12s ease;
  }
  .play-badge:hover span, .play-badge:focus-visible span { transform: scale(1.06); }
  .hero-pill {
    position: absolute;
    right: 10px;
    bottom: 10px;
    z-index: 2;
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    font-weight: var(--gl-strong);
    padding: 4px 10px;
    border-radius: var(--gl-r-full);
    border: var(--gl-bw) solid var(--gl-border);
    background: var(--gl-surface);
    color: var(--gl-text);
    box-shadow: var(--gl-shadow);
    cursor: pointer;
  }
  .hero-missing {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 100%;
    height: 100%;
    color: var(--gl-on-stage-dim);
    font-size: var(--gl-cap);
  }
"""

# Sideways-scrolling strip shared by thumbs, similar games and pedigree.
STRIP_CSS = r"""  .strip {
    display: flex;
    gap: 10px;
    overflow-x: auto;
    padding: 2px 2px 6px;
    scrollbar-width: thin;
    scroll-snap-type: x proximity;
    scroll-padding-inline: calc(2px + var(--gl-safe-left)) calc(2px + var(--gl-safe-right));
    overscroll-behavior-x: contain;
  }
  .strip > * { scroll-snap-align: start; }
  /* Trailing spacer: the last item can scroll fully into view (clear of the
     safe area) instead of ending flush against a clipped edge. */
  .strip::after { content: ""; flex: 0 0 max(2px, var(--gl-safe-right)); }
"""

# The click-to-enlarge screenshot button filling the stage.
SHOT_BTN_CSS = r"""  .shot-btn {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    padding: 0;
    border: 0;
    background: var(--gl-stage);
    cursor: zoom-in;
    -webkit-tap-highlight-color: transparent;
  }
"""

# The fullscreen button and the thumb strip's thumbnails.
MEDIA_STRIP_CSS = r"""  .fs-btn {
    position: absolute;
    right: 10px;
    top: 10px;
    z-index: 3;
    width: 32px;
    height: 32px;
    border-radius: var(--gl-r-sm);
    border: var(--gl-bw) solid var(--gl-border);
    background: var(--gl-surface);
    color: var(--gl-text);
    font-size: var(--gl-h);
    line-height: 1;
    box-shadow: var(--gl-shadow);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .thumbs { margin-top: 10px; }
  .thumb {
    position: relative;
    flex: none;
    padding: 0;
    border: var(--gl-bw) solid var(--gl-border);
    border-radius: var(--gl-r-sm);
    overflow: hidden;
    background: var(--gl-inset);
    cursor: pointer;
    -webkit-tap-highlight-color: transparent;
    transition: transform 0.12s ease;
  }
  .thumb img { display: block; width: 116px; height: 66px; object-fit: cover; }
  html:not(.no-hover) .thumb:hover { transform: translateY(-1px); }
  .thumb.sel { box-shadow: 0 0 0 2px var(--gl-text); }
  .thumb-play {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--gl-stage-veil);
    color: var(--gl-on-stage);
    font-size: var(--gl-h);
    text-shadow: 0 1px 3px var(--gl-shadow-ink);
  }
  .thumb-text {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 116px;
    height: 66px;
    font-size: var(--gl-cap);
    font-weight: var(--gl-strong);
    color: var(--gl-text-2);
    background: var(--gl-inset);
  }
"""

# ---- Similar-games / pedigree / tags CSS ------------------------------------
# Mini cover cards used by both the similar row and the pedigree row: cover,
# name, year and one chip row — nothing else.
SIMILAR_CSS = r"""  .sim {
    flex: none;
    width: 112px;
    border: var(--gl-bw) solid var(--gl-border);
    border-radius: var(--gl-r-md);
    background: var(--gl-surface);
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }
  .sim .cover-wrap { border-bottom: var(--gl-bw) solid var(--gl-border); }
  .sim-body { padding: 8px; display: flex; flex-direction: column; gap: 4px; }
  .sim-name {
    font-size: var(--gl-cap);
    font-weight: var(--gl-strong);
    line-height: var(--gl-cap-lh);
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .sim-year { font-size: var(--gl-cap); line-height: var(--gl-cap-lh); color: var(--gl-muted); font-variant-numeric: tabular-nums; }
"""

# The chip row on a small card (similar, studio, lineage, anchors): the same
# scoreChip as every other score ("You 9/10", "Critics 84", "Played 132h",
# "Status Completed"), just tighter — at most three per card. These chips are
# not links, so the touch rule that grows tappable chips to 32px is undone.
TAG_CSS = r"""  .tags, html.touch .tags { gap: 4px; }
  .tags .chip { padding: 1px 6px; column-gap: 4px; font-variant-numeric: tabular-nums; }
  html.touch .tags .chip { min-height: 0; }
"""

# "From the studio" header and publisher lines.
PEDIGREE_CSS = r"""  .ped-head { font-weight: var(--gl-strong); }
  .ped-pub { font-size: var(--gl-cap); line-height: var(--gl-cap-lh); color: var(--gl-muted); margin-top: 2px; }
  .ped-strip { margin-top: 8px; }
"""

# ---- Overlay / carousel / toast CSS -----------------------------------------
# The anchored overlay backdrop (each widget styles its own panel).
OVERLAY_CSS = r"""  .overlay {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    z-index: 10;
    background: var(--gl-scrim);
    opacity: 0;
    transition: opacity 0.16s ease;
  }
  .overlay.open { opacity: 1; }
"""

# The screenshot lightbox: stage, arrows, counter, close button.
CAROUSEL_CSS = r"""  .car-stage {
    position: relative;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--gl-stage);
    /* Holds the frame open while the full-res image loads (and if it never
       does) — a zero-height stage would swallow the arrows and the close. */
    min-height: 180px;
    /* Let the browser keep vertical scrolling while horizontal drags are ours. */
    touch-action: pan-y;
  }
  .car-img { display: block; width: 100%; max-height: 74vh; object-fit: contain; }
  .car-nav, .overlay-close {
    position: absolute;
    z-index: 2;
    width: 36px;
    height: 36px;
    border-radius: var(--gl-r-full);
    border: var(--gl-bw) solid var(--gl-border);
    background: var(--gl-surface);
    color: var(--gl-text);
    font-size: var(--gl-h);
    line-height: 1;
    box-shadow: var(--gl-shadow-md);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .car-nav { top: 50%; transform: translateY(-50%); }
  .car-prev { left: 10px; }
  .car-next { right: 10px; }
  .car-count {
    position: absolute;
    left: 50%;
    bottom: 10px;
    z-index: 2;
    transform: translateX(-50%);
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    font-weight: var(--gl-strong);
    padding: 2px 10px;
    border-radius: var(--gl-r-full);
    background: var(--gl-surface);
    color: var(--gl-text);
    font-variant-numeric: tabular-nums;
  }
  .carousel .fs-btn { right: 56px; }
  .overlay-close { top: 10px; right: 10px; }
"""

# The bottom toast that explains a host-blocked link; the URL is selectable.
TOAST_CSS = r"""  .toast {
    position: fixed;
    left: 50%;
    bottom: 14px;
    z-index: 20;
    transform: translateX(-50%) translateY(8px);
    max-width: calc(100% - 28px);
    padding: 10px 14px;
    background: var(--gl-surface);
    color: var(--gl-text);
    border: var(--gl-bw) solid var(--gl-border-strong);
    border-radius: var(--gl-r-md);
    box-shadow: var(--gl-shadow-md);
    text-align: center;
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.15s ease, transform 0.15s ease;
  }
  .toast.show { opacity: 1; transform: translateX(-50%); pointer-events: auto; }
  .toast-url {
    margin-top: 4px;
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    color: var(--gl-text-2);
    overflow-wrap: anywhere;
    user-select: all;
    -webkit-user-select: all;
  }
"""

# ---- The host bridge and its link-out fallback ------------------------------
# The hand-rolled MCP Apps postMessage bridge (spec 2026-01-26): request/
# notify, the inbound notification router, and the hostContext store with
# applyHostContext (theme, token variables, host fonts, safe areas, touch /
# hover, display mode). Opens the IIFE both widgets live inside.
BRIDGE_JS = r"""(function () {
  "use strict";

  /* ---------- MCP Apps bridge (spec 2026-01-26, hand-rolled) ---------- */
  var nextId = 1;
  var pending = {};
  var REQUEST_TIMEOUT_MS = 30000;
  function post(msg) { window.parent.postMessage(msg, "*"); }
  /* Resolves the host's result, or undefined on an error answer or when no
     answer comes within timeoutMs (default 30s) — the pending entry is
     deleted either way, so an unanswered request never leaks. Callers that
     race their own shorter timer keep working: the loser is just ignored. */
  function request(method, params, timeoutMs) {
    return new Promise(function (resolve) {
      var id = nextId++;
      var timer = setTimeout(function () {
        if (!pending[id]) return;
        delete pending[id];
        resolve(undefined);
      }, timeoutMs || REQUEST_TIMEOUT_MS);
      pending[id] = function (result) {
        clearTimeout(timer);
        resolve(result);
      };
      post({ jsonrpc: "2.0", id: id, method: method, params: params });
    });
  }
  function notify(method, params) {
    post({ jsonrpc: "2.0", method: method, params: params || {} });
  }
  window.addEventListener("message", function (ev) {
    if (ev.source !== window.parent) return;                     // only the host speaks
    var m = ev.data;
    if (!m || m.jsonrpc !== "2.0") return;
    if (m.id !== undefined && m.method === undefined) {          // response
      var cb = pending[m.id];
      if (cb) { delete pending[m.id]; cb(m.error ? undefined : m.result); }
      return;
    }
    switch (m.method) {
      case "ui/notifications/tool-result": handleToolResult(m.params); return;
      case "ui/notifications/tool-input": handleToolInput(m.params); return;
      case "ui/notifications/tool-input-partial": return;        // streaming args: ignored
      case "ui/notifications/tool-cancelled": handleToolCancelled(m.params); return;
      case "ui/notifications/host-context-changed": applyHostContext(m.params); return;
      case "ui/resource-teardown":
        teardown();
        if (m.id !== undefined) post({ jsonrpc: "2.0", id: m.id, result: {} });
        return;
    }
    if (m.id !== undefined) {                                    // unknown host request
      post({ jsonrpc: "2.0", id: m.id,
             error: { code: -32601, message: "Method not found" } });
    }
  });

  /* ---------- host context: theme, tokens, fonts, safe areas ---------- */
  /* Called with ui/initialize's hostContext and with every
     host-context-changed payload. Updates are partial: merge, never replace. */
  var hostContext = {};
  var BASE_GUTTER = 12;
  var tornDown = false;               // set by teardown(); the view is gone
  var hostFontsCss = null;            // the fonts string last injected
  var appliedHostVars = {};           // custom properties we set from styles.variables
  var HOST_TOKEN_PREFIXES = ["--color-", "--font-", "--border-", "--shadow-"];
  function isHostToken(name) {
    return HOST_TOKEN_PREFIXES.some(function (p) { return name.indexOf(p) === 0; });
  }
  function mediaQueryMatches(query) {
    try { return !!(window.matchMedia && window.matchMedia(query).matches); } catch (e) { return false; }
  }
  /* No deviceCapabilities from the host (ChatGPT, Goose, preview, or before
     ui/initialize answers): ask the browser instead. */
  function applyInputFallback() {
    var docEl = document.documentElement;
    docEl.classList.toggle("touch", mediaQueryMatches("(pointer: coarse)"));
    docEl.classList.toggle("no-hover", mediaQueryMatches("(hover: none)"));
  }
  applyInputFallback();
  function applyHostContext(ctx) {
    if (tornDown || !ctx || typeof ctx !== "object") return;
    Object.keys(ctx).forEach(function (k) { hostContext[k] = ctx[k]; });
    var docEl = document.documentElement;
    if (ctx.theme === "light" || ctx.theme === "dark") {
      docEl.dataset.theme = ctx.theme;
      docEl.style.colorScheme = ctx.theme;
    } else if (ctx.theme !== undefined) {
      // A theme we don't know: follow the viewer's prefers-color-scheme.
      delete docEl.dataset.theme;
      docEl.style.colorScheme = "";
    }
    var styles = ctx.styles || {};
    var vars = styles.variables;
    if (vars && typeof vars === "object") {
      /* A variables map is the host's whole current set (a theme switch sends
         a fresh one): a token we set earlier that it no longer carries is
         removed so the --gl-* fallback shows instead of a stale value. */
      var nextVars = {};
      Object.keys(vars).forEach(function (name) {
        var value = vars[name];
        if (name.indexOf("--") === 0 && value !== null && value !== undefined && value !== "") {
          docEl.style.setProperty(name, String(value));
          nextVars[name] = true;
        }
      });
      Object.keys(appliedHostVars).forEach(function (name) {
        if (nextVars[name]) return;
        if (isHostToken(name)) docEl.style.removeProperty(name);
        else nextVars[name] = true;
      });
      appliedHostVars = nextVars;
    }
    /* One <style id="host-fonts">, rewritten only when the string changes:
       re-injecting identical @font-face rules re-triggers font loading. */
    if (styles.css && typeof styles.css.fonts === "string" && styles.css.fonts !== hostFontsCss) {
      hostFontsCss = styles.css.fonts;
      var fonts = document.getElementById("host-fonts");
      if (!fonts) {
        fonts = document.createElement("style");
        fonts.id = "host-fonts";
        document.head.appendChild(fonts);
      }
      fonts.textContent = styles.css.fonts;
    }
    var insets = ctx.safeAreaInsets;
    if (insets && typeof insets === "object") {
      [["top", "Top"], ["right", "Right"], ["bottom", "Bottom"], ["left", "Left"]].forEach(function (side) {
        var extra = Math.max(0, Number(insets[side[0]]) || 0);
        document.body.style["padding" + side[1]] = (BASE_GUTTER + extra) + "px";
        if (side[0] === "left" || side[0] === "right") {
          docEl.style.setProperty("--gl-safe-" + side[0], extra + "px");
        }
      });
    }
    var device = ctx.deviceCapabilities;
    if (device && typeof device === "object") {
      docEl.classList.toggle("touch", !!device.touch);
      docEl.classList.toggle("no-hover", device.hover === false);
    } else if (!hostContext.deviceCapabilities) {
      applyInputFallback();
    }
    if (ctx.displayMode) docEl.setAttribute("data-display-mode", String(ctx.displayMode));
    reportSize();
  }
"""

# Link-out through the host, because the widget sandbox usually blocks
# window.open, plus the toast shown when even ui/open-link is refused.
EXTERNAL_LINK_JS = r"""  /* External links. The sandbox usually lacks allow-popups, so window.open
     and target=_blank fail silently (Firefox included) — the reliable route
     is the host's ui/open-link. Try native only when the host didn't declare
     openLinks (synchronously, inside the click gesture), then always fall
     through to ui/open-link even undeclared: hosts often implement it
     without declaring it, and an unsupporting one just answers
     method-not-found — which we surface as a hint instead of silence. */
  var hostCaps = {};
  function openLink(url) {
    if (!url) return;
    if (!hostCaps.openLinks) {
      try { if (window.open(url, "_blank", "noopener")) return; } catch (e) { /* sandboxed */ }
    }
    Promise.race([
      request("ui/open-link", { url: url }),
      new Promise(function (resolve) { setTimeout(function () { resolve("timeout"); }, 2500); }),
    ]).then(function (res) {
      if (res === undefined) flashLinkHint(url); // explicit host error
    });
  }
  var hintTimer = null;
  function flashLinkHint(url) {
    var t = document.querySelector(".toast");
    if (!t) {
      t = el("div", "toast");
      t.setAttribute("role", "status");
      document.body.appendChild(t);
    }
    t.textContent = "";
    t.appendChild(el("div", null, "This host blocked the link."));
    if (url) t.appendChild(el("div", "toast-url", url));
    t.classList.add("show");
    clearTimeout(hintTimer);
    hintTimer = setTimeout(function () { t.classList.remove("show"); }, 6000);
  }
"""

# structuredContent-or-text result unwrapping, plus the tool-input and
# tool-cancelled handlers the bridge routes to.
TOOL_RESULT_JS = r"""  var lastToolInput = null;
  var gotResult = false;
  function resultData(result) {
    var data = result && result.structuredContent;
    if (!data && result && result.content) {
      var text = (result.content.find(function (c) { return c.type === "text"; }) || {}).text;
      try { data = JSON.parse(text); } catch (e) { /* leave undefined */ }
    }
    return data;
  }
  /* True when #root holds rendered content — anything but the skeleton or an
     earlier notice. Decides whether a late notice replaces or joins. */
  function rootShowsContent() {
    for (var n = root.firstElementChild; n; n = n.nextElementSibling) {
      if (!n.classList.contains("skel") && !n.classList.contains("notice")) return true;
    }
    return false;
  }
  function handleToolResult(result) {
    gotResult = true;
    var data = resultData(result);
    if (data) {
      render(data);
      return;
    }
    // Never leave the skeleton pulsing forever over a result we can't read.
    if (!rootShowsContent()) root.textContent = "";
    notice(root, "Couldn't read the result");
    reportSize();
  }
  /* The arguments arrive before the result: keep them (Phase B builds the
     grid's header line from them) and pick the matching skeleton. */
  function handleToolInput(params) {
    lastToolInput = (params && params.arguments) || {};
    if (!gotResult) showSkeleton();
  }
  /* Content already on screen stays (with the notice under it); only a
     skeleton — nothing real yet — is replaced. */
  function handleToolCancelled() {
    gotResult = true;                               // keeps the skeleton out
    if (!rootShowsContent()) root.textContent = "";
    notice(root, "Cancelled before the result arrived.");
    reportSize();
  }
"""

# ---- DOM + cover helpers ----------------------------------------------------
# ``root``/``el``/``list``/``num`` — the whole DOM helper vocabulary.
DOM_HELPERS_JS = r"""  var root = document.getElementById("root");

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function list(v) { return Array.isArray(v) ? v : []; }
  function num(v) {
    if (v === null || v === undefined || v === "") return null;
    var n = Number(v);
    return isFinite(n) ? n : null;
  }
"""

# ---- Shared components JS ---------------------------------------------------
# Raw ids never render: ``label(kind, raw)`` maps registry names, verdict
# literals and provider prefixes, and humanizes anything unknown.
LABELS_JS = (
    "  /* ---------- labels (generated: platforms_registry, verdict literals, purchase sources) ---------- */\n"
    "  var PLATFORM_LABELS = " + json.dumps(PLATFORM_LABELS, sort_keys=True) + ";\n"
    "  var VERDICT_LABELS = " + json.dumps(VERDICT_LABELS) + ";\n"
    "  var PROVIDER_LABELS = " + json.dumps(PROVIDER_LABELS, sort_keys=True) + ";\n"
    "  var PURCHASE_SOURCE_LABELS = " + json.dumps(PURCHASE_SOURCE_LABELS, sort_keys=True) + ";\n"
    + r"""  var LABEL_MAPS = {
    platform: PLATFORM_LABELS, verdict: VERDICT_LABELS, provider: PROVIDER_LABELS,
    purchase_source: PURCHASE_SOURCE_LABELS,
  };
  function humanize(raw) {
    return String(raw).replace(/_+/g, " ").trim().replace(/\s+/g, " ")
      .replace(/(^|\s)(\S)/g, function (all, space, ch) { return space + ch.toUpperCase(); });
  }
  function label(kind, raw) {
    if (raw === null || raw === undefined || raw === "") return "";
    var key = String(raw);
    var map = LABEL_MAPS[kind] || {};
    if (Object.prototype.hasOwnProperty.call(map, key)) return map[key];
    var lower = key.toLowerCase();
    if (Object.prototype.hasOwnProperty.call(map, lower)) return map[lower];
    return humanize(key);
  }
"""
)

# Hours, counts, money, plurals — one copy of each.
NUMBERS_JS = r"""  /* ---------- numbers ---------- */
  /* "27h", "2.3h"; the "~" marks an estimate (HLTB), never a measured total. */
  function hoursLabel(h, estimate) {
    var n = num(h);
    if (n == null) return null;
    return (estimate ? "~" : "") + (n >= 10 ? Math.round(n) : Math.round(n * 10) / 10) + "h";
  }
  /* "114k reviews": a count always carries its noun. */
  function compactCount(v, word) {
    var n = num(v);
    if (n == null) return null;
    var text = n >= 1000000 ? (Math.round(n / 100000) / 10) + "M"
      : n >= 10000 ? Math.round(n / 1000) + "k"
      : n >= 1000 ? (Math.round(n / 100) / 10) + "k"
      : String(Math.round(n));
    return word ? text + " " + word + (n === 1 ? "" : "s") : text;
  }
  var CURRENCY_SIGNS = { EUR: "€", USD: "$", GBP: "£" };
  function money(amount, currency) {
    var n = num(amount);
    if (n == null) return null;
    var code = String(currency || "").toUpperCase();
    var value = n.toFixed(2);
    if (CURRENCY_SIGNS[code]) return CURRENCY_SIGNS[code] + value;
    return code ? value + " " + code : value;
  }
  /* A count is singular only when it is exactly one AND not a floor
     ("1+ games" stays plural). */
  function plural(n, word, truncated) {
    return n + (truncated ? "+" : "") + " " + word + (n === 1 && !truncated ? "" : "s");
  }
"""

# The one score chip, its tier functions, and the Steam chip built on it.
SCORE_CHIP_JS = r"""  /* ---------- score chip ---------- */
  /* Providers use negative sentinels for "no score yet" — never show those. */
  function realScore(n) { return n != null && n >= 0; }
  /* Metacritic's games thresholds: good >=75, ok 50-74, bad <50. */
  function mcTier(n) { return n >= 75 ? "good" : n >= 50 ? "ok" : "bad"; }
  /* OpenCritic: the real tier when the payload has one, else the score
     approximation (mighty 84 / strong 75 / fair 65 / weak). */
  function ocTier(n, tierName) {
    var t = String(tierName || "").toLowerCase();
    if (["mighty", "strong", "fair", "weak"].indexOf(t) < 0) {
      t = n >= 84 ? "mighty" : n >= 75 ? "strong" : n >= 65 ? "fair" : "weak";
    }
    return t === "mighty" || t === "strong" ? "good" : t === "fair" ? "ok" : "bad";
  }
  /* Steam's nine summary phrases, most-specific first, as 1..9 steps. */
  var STEAM_STEPS = [
    ["overwhelmingly positive", 9], ["very positive", 8], ["mostly positive", 6],
    ["positive", 7], ["mixed", 5], ["overwhelmingly negative", 1],
    ["very negative", 2], ["mostly negative", 4], ["negative", 3],
  ];
  function steamStep(desc) {
    var d = String(desc || "").toLowerCase();
    for (var i = 0; i < STEAM_STEPS.length; i++) {
      if (d.indexOf(STEAM_STEPS[i][0]) >= 0) return STEAM_STEPS[i][1];
    }
    return null;
  }
  function steamTier(desc) {
    var s = steamStep(desc);
    return s == null ? "none" : s >= 6 ? "good" : s === 5 ? "ok" : "bad";
  }
  function craftTier(pct) { return pct >= 75 ? "good" : pct >= 50 ? "ok" : "bad"; }
  /* His own 0-10 rating: good >=7, ok >=5, else bad. */
  function ratingTier(n) { return n >= 7 ? "good" : n >= 5 ? "ok" : "bad"; }
  /* ProtonDB: native/platinum/gold good, silver ok, bronze/borked bad. */
  function protonTier(tierName) {
    var t = String(tierName || "").toLowerCase();
    if (t === "native" || t === "platinum" || t === "gold") return "good";
    if (t === "silver") return "ok";
    return t === "bronze" || t === "borked" ? "bad" : "none";
  }
  /* {label, value, tier, title, url, meter (0-100), aux, cls} → one chip.
     Color is the quality tier only; the brand is the label text. */
  function scoreChip(opts) {
    var tier = opts.tier || "none";
    var chip = el(opts.url ? "a" : "span", "chip tier-" + tier + (opts.cls ? " " + opts.cls : ""));
    chip.appendChild(el("span", "lbl", opts.label));
    // label → meter → value: when a narrow card wraps the chip, the meter
    // stays on the label's line and the value takes the next one whole.
    var pct = num(opts.meter);
    if (pct != null) {
      var meter = el("span", "meter");
      meter.setAttribute("aria-hidden", "true");
      var fill = el("span", "meter-fill");
      fill.style.width = Math.max(0, Math.min(100, pct)) + "%";
      meter.appendChild(fill);
      chip.appendChild(meter);
    }
    if (opts.value !== undefined && opts.value !== null && opts.value !== "") {
      chip.appendChild(el("b", null, String(opts.value)));
    }
    if (opts.aux) chip.appendChild(el("span", "aux", opts.aux));
    if (opts.title) chip.title = opts.title;
    if (opts.url) {
      chip.href = opts.url;
      chip.setAttribute("data-link", "");
      var ext = el("span", "ext", "↗");
      ext.setAttribute("aria-hidden", "true");
      chip.appendChild(ext);
      chip.addEventListener("click", function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        openLink(opts.url);
      });
    }
    return chip;
  }
  /* The library chips on a small card (similar, studio, lineage, anchors) are
     the same chip as every score: "You 9/10", "Critics 84", "Played 132h",
     "Unplayed", "Status Completed". chipRow keeps at most three. */
  function youChip(rating) {
    var n = num(rating);
    if (n == null) return null;
    return scoreChip({ label: "You", value: n + "/10", tier: ratingTier(n), title: "Your rating" });
  }
  function criticsChip(score) {
    var n = num(score);
    if (!realScore(n)) return null;
    return scoreChip({ label: "Critics", value: Math.round(n), tier: mcTier(n), title: "Critic score" });
  }
  function playedChip(hours, unplayed) {
    var n = num(hours);
    if (n != null && n > 0) {
      return scoreChip({ label: "Played", value: hoursLabel(n), title: "Your playtime" });
    }
    return unplayed ? scoreChip({ label: "Unplayed", title: "In your library, never played" }) : null;
  }
  var STATUS_CHIPS = {
    completed: ["Completed", "good"],
    evergreen: ["Evergreen", "good"],
    abandoned: ["Abandoned", "bad"],
    playing: ["Playing", "none"],
  };
  function statusChip(status) {
    var s = STATUS_CHIPS[status];
    return s ? scoreChip({ label: "Status", value: s[0], tier: s[1] }) : null;
  }
  function chipRow(chips) {
    var row = el("div", "chips tags");
    chips.filter(Boolean).slice(0, 3).forEach(function (c) { row.appendChild(c); });
    return row.childNodes.length ? row : null;
  }
  /* The phrase always rides with the meter — the meter alone says nothing. */
  function steamChip(desc, url) {
    if (!desc) return null;
    var step = steamStep(desc);
    var phrase = String(desc).toLowerCase();
    phrase = phrase.charAt(0).toUpperCase() + phrase.slice(1);
    return scoreChip({
      label: "Steam", value: phrase, tier: steamTier(desc),
      meter: step == null ? null : Math.round((step / 9) * 100),
      title: step == null ? "Steam reviews" : "Steam reviews: " + step + " of 9 on Steam's scale",
      url: url,
    });
  }
"""

# The labelled taste-match bar.
MATCH_BAR_JS = r"""  function matchBar(percent) {
    var p = Math.max(0, Math.min(100, Math.round(num(percent) || 0)));
    var wrap = el("div", "match");
    wrap.setAttribute("role", "meter");
    wrap.setAttribute("aria-valuemin", "0");
    wrap.setAttribute("aria-valuemax", "100");
    wrap.setAttribute("aria-valuenow", String(p));
    wrap.setAttribute("aria-label", "Taste match");
    wrap.appendChild(el("b", null, p + "% match"));
    var track = el("span", "track");
    var fill = el("span", "fill");
    fill.style.width = p + "%";
    track.appendChild(fill);
    wrap.appendChild(track);
    return wrap;
  }
"""

# Shape-matched loading placeholders. ``showSkeleton`` asks the widget's own
# ``skeletonKind()`` which shape to draw.
SKELETON_JS = r"""  /* Each placeholder is the real layout in grey: grid = header line + cards
     (cover, title, match bar, one chip row); eval = header panel (cover,
     title, stamp, score chips, the facts row), the pitch's two lines, the
     media stage; detail = identity panel (cover, title + 3 lines, chip row)
     and the media stage. */
  function skeleton(kind) {
    function sk(cls) { return el("div", "sk " + cls); }
    function lines(parent, n) {
      for (var i = 0; i < n; i++) parent.appendChild(sk("sk-line" + (i === n - 1 ? " short" : "")));
    }
    function chips(parent, n, cls) {
      var row = el("div", "sk-chips" + (cls ? " " + cls : ""));
      for (var k = 0; k < n; k++) row.appendChild(sk("sk-chip"));
      parent.appendChild(row);
    }
    /* The stage plus its thumbs: beside it in a 3x3 grid on a wide
       evaluation card, a strip under it otherwise (as the real panels). */
    function mediaBlock(parent) {
      var media = el("div", "sk-panel");
      var row = el("div", "sk-media-row");
      row.appendChild(sk("sk-media"));
      var thumbs = el("div", "sk-thumbs");
      for (var t = 0; t < (kind === "eval" ? 9 : 6); t++) thumbs.appendChild(sk("sk-shot"));
      row.appendChild(thumbs);
      media.appendChild(row);
      parent.appendChild(media);
    }
    var wrap = el("div", "skel skel-" + kind);
    wrap.setAttribute("role", "status");
    wrap.setAttribute("aria-busy", "true");
    wrap.setAttribute("aria-label", "Loading");
    if (kind === "grid") {
      wrap.appendChild(sk("sk-head"));
      var cards = el("div", "sk-cards");
      for (var c = 0; c < 4; c++) {
        var card = el("div", "sk-card");
        card.appendChild(sk("sk-cover"));
        var body = el("div", "sk-body");
        body.appendChild(sk("sk-line"));
        body.appendChild(sk("sk-bar"));
        chips(body, 2);
        card.appendChild(body);
        cards.appendChild(card);
      }
      wrap.appendChild(cards);
      return wrap;
    }
    var panel = el("div", "sk-panel");
    var row = el("div", "sk-row");
    row.appendChild(sk("sk-thumb"));
    var col = el("div", "sk-col");
    col.appendChild(sk("sk-line wide"));
    col.appendChild(sk("sk-line short"));
    if (kind === "eval") {
      chips(col, 3);
      row.appendChild(col);
      row.appendChild(sk("sk-stamp"));
      panel.appendChild(row);
      chips(panel, 3, "sk-facts");
      wrap.appendChild(panel);
      var pitch = el("div", "sk-panel");
      lines(pitch, 2);
      wrap.appendChild(pitch);
    } else {
      chips(col, 3);
      lines(col, 2);
      row.appendChild(col);
      panel.appendChild(row);
      wrap.appendChild(panel);
    }
    mediaBlock(wrap);
    return wrap;
  }
  function showSkeleton() {
    root.textContent = "";
    root.appendChild(skeleton(skeletonKind()));
    reportSize();
  }
"""

# Host display modes: request, the granted answer, and canFullscreen().
DISPLAY_MODE_JS = r"""  /* ---------- display mode ---------- */
  function currentDisplayMode() { return hostContext.displayMode || "inline"; }
  function canFullscreen() {
    var modes = hostContext.availableDisplayModes;
    return Array.isArray(modes) && modes.indexOf("fullscreen") >= 0;
  }
  /* Resolves to the mode the host GRANTED; on an error or a 2.5s silence it
     resolves to the mode we are still in. */
  function requestDisplayMode(mode) {
    var before = currentDisplayMode();
    return Promise.race([
      request("ui/request-display-mode", { mode: mode }),
      new Promise(function (resolve) { setTimeout(function () { resolve(undefined); }, 2500); }),
    ]).then(function (res) {
      var granted = res && res.mode ? String(res.mode) : before;
      hostContext.displayMode = granted;
      document.documentElement.setAttribute("data-display-mode", granted);
      reportSize();
      return granted;
    });
  }
"""

# Telling the model what the user did, and speaking as the user. ui/message's
# content is a ContentBlock[] (ext-apps spec.types.ts), never a bare block.
MODEL_CONTEXT_JS = r"""  /* Fire-and-forget: request() resolves undefined on method-not-found. */
  function updateModelContext(text, structured) {
    var params = { content: [{ type: "text", text: String(text) }] };
    if (structured) params.structuredContent = structured;
    request("ui/update-model-context", params);
  }
  function sendMessage(text) {
    request("ui/message", { role: "user", content: [{ type: "text", text: String(text) }] });
  }
"""

# The in-place "Full breakdown ▾" fallback when fullscreen is unavailable:
# content is built on first open only.
DISCLOSURE_JS = r"""  var disclosureSeq = 0;
  function disclosure(parent, text, buildFn) {
    var btn = el("button", "disclosure");
    btn.type = "button";
    btn.setAttribute("aria-expanded", "false");
    btn.appendChild(el("span", null, text));
    var chev = el("span", "chev", "▾");
    chev.setAttribute("aria-hidden", "true");
    btn.appendChild(chev);
    var body = el("div", "disclosure-body");
    body.id = "disclosure-" + (++disclosureSeq);
    body.hidden = true;
    btn.setAttribute("aria-controls", body.id);
    var built = false;
    btn.addEventListener("click", function () {
      var open = btn.getAttribute("aria-expanded") !== "true";
      if (open && !built) { built = true; buildFn(body); }
      body.hidden = !open;
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      reportSize();
    });
    parent.appendChild(btn);
    parent.appendChild(body);
    return { button: btn, body: body };
  }
"""

# The one muted failure line: "Couldn't load: trailer (Steam), studio (IGDB)".
NOTICE_JS = r"""  function notice(parent, items) {
    var text;
    if (typeof items === "string") {
      text = items;
    } else {
      var seen = {};
      var parts = list(items).map(function (it) {
        if (!it) return null;
        if (typeof it === "string") return it;
        // No `what`: the source alone if there is one, else nothing to say.
        if (!it.what) return it.source ? String(it.source) : null;
        return it.what + (it.source ? " (" + it.source + ")" : "");
      }).filter(function (p) {
        if (!p || seen[p]) return false;
        seen[p] = true;
        return true;
      });
      if (!parts.length) return null;
      text = "Couldn't load: " + parts.join(", ");
    }
    var node = el("div", "notice", text);
    node.setAttribute("role", "status");
    parent.appendChild(node);
    return node;
  }
"""

# All component JS in splice order (one line per widget, not seven).
COMPONENTS_JS = (
    LABELS_JS
    + NUMBERS_JS
    + SCORE_CHIP_JS
    + MATCH_BAR_JS
    + SKELETON_JS
    + DISPLAY_MODE_JS
    + MODEL_CONTEXT_JS
    + DISCLOSURE_JS
    + NOTICE_JS
)

# Name-seeded hue for the gradient plate used when there is no art.
COVER_HUE_JS = r"""  function coverHue(name) {
    var h = 0;
    for (var i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) % 360;
    return h;
  }
"""

# Cover art with the gradient-plate fallback on a missing/broken image.
COVER_NODE_JS = r"""  function coverNode(game) {
    var wrap = el("div", "cover-wrap");
    var hue = coverHue(game.name || "?");
    var fallback = el("div", "cover-fallback", game.name || "?");
    fallback.style.background =
      "linear-gradient(160deg, hsl(" + hue + ",45%,38%), hsl(" + ((hue + 40) % 360) + ",50%,22%))";
    if (game.cover_url) {
      var img = document.createElement("img");
      img.alt = game.name ? "Cover art for " + game.name : "";
      img.loading = "lazy";
      img.onerror = function () { img.remove(); wrap.appendChild(fallback); };
      img.src = game.cover_url;
      wrap.appendChild(img);
    } else {
      wrap.appendChild(fallback);
    }
    return wrap;
  }
"""

# ---- Fullscreen and the screenshot carousel ---------------------------------
# Best-effort fullscreen: no button where the API is absent, and the
# button removes itself when the host denies the request.
FULLSCREEN_BUTTON_JS = r"""  function fullscreenButton(target) {
    if (!document.fullscreenEnabled && !document.webkitFullscreenEnabled) return null;
    var req = target.requestFullscreen || target.webkitRequestFullscreen;
    if (!req) return null;
    var btn = el("button", "fs-btn", "⛶");
    btn.setAttribute("aria-label", "Full screen");
    btn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      ev.preventDefault();
      try {
        var pending = req.call(target);
        if (pending && pending.catch) {
          pending.catch(function () { btn.remove(); });
        }
      } catch (e) {
        btn.remove();
      }
    });
    return btn;
  }
"""

# One carousel arrow.
NAV_BUTTON_JS = r"""  function navButton(cls, glyph, ariaText, onClick) {
    var btn = el("button", "car-nav " + cls, glyph);
    btn.setAttribute("aria-label", ariaText);
    btn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      onClick();
    });
    return btn;
  }
"""

# The lightbox stage: image, arrows, counter, wrapping ``show(i)`` and the
# pointer drag/swipe. Spliced INSIDE each widget's own ``openCarousel``,
# which owns the overlay lifecycle (a stack in apps.py, one slot in
# apps_eval.py) — it closes over ``shots``, ``index``, ``panel`` and
# ``gameName`` there.
CAROUSEL_STAGE_JS = r"""    var stage = el("div", "car-stage");
    var img = document.createElement("img");
    img.className = "car-img";
    stage.appendChild(img);
    var counter = el("div", "car-count", "");
    if (shots.length > 1) {
      stage.appendChild(navButton("car-prev", "‹", "Previous screenshot",
        function () { show(index - 1); }));
      stage.appendChild(navButton("car-next", "›", "Next screenshot",
        function () { show(index + 1); }));
      stage.appendChild(counter);
    }
    panel.appendChild(stage);
    var fs = fullscreenButton(stage);
    if (fs) panel.appendChild(fs);

    function show(i) {
      index = ((i % shots.length) + shots.length) % shots.length;   // wraps
      var shot = shots[index];
      img.src = shot.full || shot.thumb;
      img.alt = (gameName ? gameName + " " : "") + "screenshot " + (index + 1);
      counter.textContent = (index + 1) + " / " + shots.length;
    }
    show(index);

    // Drag/swipe. Pointer events cover mouse and touch in one path; a drag
    // shorter than the threshold is a tap and does nothing.
    var startX = null;
    stage.addEventListener("pointerdown", function (ev) { startX = ev.clientX; });
    stage.addEventListener("pointercancel", function () { startX = null; });
    stage.addEventListener("pointerup", function (ev) {
      if (startX === null) return;
      var dx = ev.clientX - startX;
      startX = null;
      if (Math.abs(dx) > 40) show(index + (dx < 0 ? 1 : -1));
    });
"""

# ---- Trailer hero + the media panel (one viewer, one thumb strip) -----------
# The trailer stage: play badge, link pill, poster, the Steam mp4 with its
# per-<source> error fallback, and the click-to-load youtube-nocookie embed.
HERO_MEDIA_JS = r"""  function playBadge(ariaText) {
    var btn = el("button", "play-badge");
    btn.setAttribute("aria-label", ariaText);
    var glyph = el("span", null, "▶");
    glyph.setAttribute("aria-hidden", "true");
    btn.appendChild(glyph);
    return btn;
  }
  function linkPill(hero, text, url) {
    var pill = el("button", "hero-pill", text);
    pill.addEventListener("click", function (ev) {
      ev.stopPropagation();
      openLink(url);
    });
    hero.appendChild(pill);
    return pill;
  }
  function posterNode(url, alt) {
    var img = document.createElement("img");
    img.className = "hero-media";
    img.alt = alt || "";
    img.src = url;
    return img;
  }
  function mp4Hero(hero, trailer) {
    var urls = [trailer.url, trailer.hq_url].filter(Boolean);
    var video = document.createElement("video");
    video.className = "hero-media";
    video.controls = true;
    video.preload = "none";     // never autoplay: zero bytes until the user asks
    video.playsInline = true;
    if (trailer.poster) video.poster = trailer.poster;
    video.setAttribute("aria-label", trailer.name || "Trailer");
    urls.forEach(function (u) {
      var src = document.createElement("source");
      src.src = u;
      src.type = "video/mp4";
      video.appendChild(src);
    });
    /* A <video> with <source> children never fires `error` on itself — the
       failures land on the <source> elements and error events don't bubble,
       so catch them on the way down and give up once every rendition failed.
       This is the fallback for Valve dropping the undocumented legacy mp4
       URLs, and equally for a host that strips media-src from the CSP. */
    var failures = 0;
    var done = false;
    video.addEventListener("error", function () {
      failures += 1;
      if (done || failures < urls.length) return;
      done = true;
      posterFallback(hero, trailer);
    }, true);
    hero.appendChild(video);
  }
  function posterFallback(hero, trailer) {
    hero.textContent = "";
    if (trailer.poster) {
      hero.appendChild(posterNode(trailer.poster, trailer.name || "Trailer thumbnail"));
    } else {
      hero.appendChild(el("div", "hero-missing", "Trailer unavailable here"));
    }
    var url = trailer.hq_url || trailer.url;
    var badge = playBadge("Open the trailer" + (trailer.name ? ": " + trailer.name : ""));
    badge.addEventListener("click", function () { openLink(url); });
    hero.appendChild(badge);
  }
  function youtubeHero(hero, trailer) {
    if (trailer.poster) {
      hero.appendChild(posterNode(trailer.poster, trailer.name || "Trailer thumbnail"));
    } else {
      hero.appendChild(el("div", "hero-missing", trailer.name || "Trailer"));
    }
    var watchUrl = "https://www.youtube.com/watch?v=" + trailer.video_id;
    var badge = playBadge("Play trailer" + (trailer.name ? ": " + trailer.name : ""));
    badge.addEventListener("click", function () {
      // Lazy by design: nothing is fetched from YouTube until this click.
      var frame = document.createElement("iframe");
      frame.className = "hero-media";
      frame.src = "https://www.youtube-nocookie.com/embed/" + encodeURIComponent(trailer.video_id);
      frame.setAttribute("allowfullscreen", "");
      frame.setAttribute("title", trailer.name || "Trailer");
      hero.textContent = "";
      hero.appendChild(frame);
      // A CSP-blocked nested frame is not detectable from JS, so keep the
      // link-out route visible even after the embed is swapped in.
      linkPill(hero, "Watch on YouTube ↗", watchUrl);
      reportSize();
    });
    hero.appendChild(badge);
    linkPill(hero, "Watch on YouTube ↗", watchUrl);
  }
"""

# One viewer plus one thumb strip, trailer first — trailer selection,
# thumb building, and the in-place stage swap.
MEDIA_PANEL_JS = r"""  function trailerEntry(media) {
    var trailer = media.trailer;
    if (!trailer) return null;
    if (trailer.kind === "mp4" && trailer.url) return { kind: "mp4", trailer: trailer };
    if (trailer.kind === "youtube" && trailer.video_id) {
      return { kind: "youtube", trailer: trailer };
    }
    return null;                                    // trailer of an unknown kind
  }
  function shotLabel(gameName, i) {
    return (gameName ? gameName + " " : "") + "screenshot " + (i + 1);
  }
  function showEntry(viewer, entry, shots, gameName) {
    viewer.textContent = "";
    if (entry.kind === "mp4") {
      mp4Hero(viewer, entry.trailer);
      // Native controls carry the browser's own fullscreen button; this is the
      // extra affordance for hosts that surface neither.
      var video = viewer.querySelector("video");
      if (video) {
        var videoFs = fullscreenButton(video);
        if (videoFs) viewer.appendChild(videoFs);
      }
      return;
    }
    if (entry.kind === "youtube") {
      youtubeHero(viewer, entry.trailer);           // the embed handles its own
      return;
    }
    var shotText = shotLabel(gameName, entry.index);
    var btn = el("button", "shot-btn");
    btn.setAttribute("aria-label", "Enlarge " + shotText);
    var img = document.createElement("img");
    img.className = "hero-media";
    img.alt = "";                                   // the button carries the label
    img.src = entry.shot.full || entry.shot.thumb;
    btn.appendChild(img);
    btn.addEventListener("click", function () {
      openCarousel(shots, entry.index, gameName, btn);
    });
    viewer.appendChild(btn);
    var fs = fullscreenButton(img);
    if (fs) viewer.appendChild(fs);
  }
  function thumbNode(entry, gameName) {
    var btn = el("button", "thumb");
    if (entry.kind === "shot") {
      btn.setAttribute("aria-label", "Show " + shotLabel(gameName, entry.index));
      var img = document.createElement("img");
      img.alt = "";
      img.loading = "lazy";
      img.src = entry.shot.thumb || entry.shot.full;
      btn.appendChild(img);
      return btn;
    }
    btn.setAttribute("aria-label", "Show the trailer");
    if (entry.trailer.poster) {
      var poster = document.createElement("img");
      poster.alt = "";
      poster.loading = "lazy";
      poster.src = entry.trailer.poster;
      btn.appendChild(poster);
    } else {
      btn.appendChild(el("span", "thumb-text", "Trailer"));
    }
    btn.appendChild(el("span", "thumb-play", "▶"));
    return btn;
  }
  function mediaNode(parent, media, gameName) {
    var shots = list(media.screenshots).filter(function (s) {
      return s && (s.thumb || s.full);
    });
    var entries = [];
    var trailer = trailerEntry(media);
    if (trailer) entries.push(trailer);
    shots.forEach(function (shot, i) {
      entries.push({ kind: "shot", shot: shot, index: i });
    });
    if (!entries.length) return;

    var box = section(parent, "Media");
    var viewer = el("div", "hero viewer");
    box.appendChild(viewer);

    // `screenshots_truncated` is deliberately NOT surfaced: the extra images
    // are not in the payload, so the old "+N more" chip advertised something
    // nothing could open.
    if (entries.length > 1) {
      var strip = el("div", "strip thumbs");
      var thumbs = entries.map(function (entry) { return thumbNode(entry, gameName); });
      thumbs.forEach(function (btn, i) {
        btn.setAttribute("aria-pressed", "false");
        btn.addEventListener("click", function () { select(i); });
        strip.appendChild(btn);
      });
      box.appendChild(strip);
      var select = function (i) {
        thumbs.forEach(function (btn, j) {
          btn.classList.toggle("sel", i === j);
          btn.setAttribute("aria-pressed", i === j ? "true" : "false");
        });
        showEntry(viewer, entries[i], shots, gameName);
        reportSize();
      };
      select(0);                                    // trailer first when there is one
      return;
    }
    showEntry(viewer, entries[0], shots, gameName);
  }
"""

# ---- Ownership stickers, similar games, studio pedigree ---------------------
# His rating, his hours (or "Unplayed") and, failing both, "Owned" — the chip
# row for a related game (lineage comparisons). Zero hours on an owned game is
# authoritative NOT-played; null hours is unknown and says nothing.
OWNERSHIP_TAGS_JS = r"""  function ownershipTags(item) {
    var hours = num(item.playtime_hours);
    var unplayed = !!item.unplayed || (!!item.owned && hours === 0);
    var chips = [youChip(item.my_rating), playedChip(hours, unplayed)];
    if (item.owned && !chips[0] && !chips[1]) chips.push(scoreChip({ label: "Owned" }));
    return chipRow(chips);
  }
"""

# The owned games most like this one (tools/game_media.py's similar_in_library)
# as a strip of mini covers. Each card is cover, name, year and ONE chip row:
# every item is owned (the pool IS the library), so the row carries his rating
# ("You 9/10"), then his hours ("Played 132h") or, unrated and unplayed,
# "Unplayed" — at most two.
SIMILAR_NODE_JS = r"""  function similarTags(item) {
    var you = youChip(item.my_rating);
    var unplayed = !you && !!item.unplayed;
    return chipRow([you, playedChip(item.playtime_hours, unplayed)]);
  }
  function similarNode(parent, similar) {
    var items = list(similar.items).filter(function (i) { return i && i.name; });
    if (!items.length) return;
    var box = section(parent, "Similar in your library");
    var strip = el("div", "strip");
    items.forEach(function (item) {
      var card = el("div", "sim");
      card.appendChild(coverNode(item));
      var body = el("div", "sim-body");
      body.appendChild(el("div", "sim-name", item.name || "?"));
      if (item.release_year) body.appendChild(el("div", "sim-year", String(item.release_year)));
      var why = list(item.shared_tags).filter(Boolean);
      if (why.length) card.title = "Shares: " + why.join(", ");
      var tags = similarTags(item);
      if (tags) body.appendChild(tags);
      card.appendChild(body);
      strip.appendChild(card);
    });
    // No "+N more" chip: the extras are not in the payload, and there is
    // nothing to click through to.
    box.appendChild(strip);
    var total = num(similar.count);
    // The count is "owned games clearing the shared-tag bar", so it is a
    // denominator the row can honestly claim — every one of them is his.
    var note = (similar.truncated && total != null && total > items.length)
      ? "The " + items.length + " of your " + plural(total, "game") + " most like this one"
      : "Your " + plural(items.length, "game") + " most like this one";
    var unplayed = items.filter(function (i) { return i.unplayed; }).length;
    if (unplayed) note += " · " + unplayed + " unplayed";
    box.appendChild(el("div", "note", note));
  }
"""

# "From the studio": the headline, the per-poster badge and the strip with its
# track-record footer (``plural`` lives in NUMBERS_JS).
PEDIGREE_JS = r"""  function pedigreeHeadline(ped) {
    var dev = ped.developer || {};
    var names = list(ped.developer_names).filter(Boolean);
    var parts = [];
    if (names.length) parts.push(names.join(" & "));
    else if (dev.name) parts.push(dev.name);
    var founded = num(dev.founded_year);
    if (founded != null) parts.push("est. " + founded);
    var size = num(ped.catalog_size);
    if (size) parts.push(plural(size, "game", ped.catalog_truncated));
    return parts.join(" · ");
  }
  /* ONE score per poster: his own rating ("You 8/10") outranks the critic
     score ("Critics 84"), which only stands in when he hasn't rated it. An
     owned game he never rated still says "Owned". */
  function pedigreeBadges(item) {
    var chips = [];
    var rating = num(item.my_rating);
    var critic = num(item.critic_score);
    if (item.owned && rating != null) {
      chips.push(youChip(rating));
    } else if (critic != null && critic >= 0) {
      chips.push(criticsChip(critic));
    }
    if (item.owned && rating == null) chips.push(scoreChip({ label: "Owned" }));
    return chipRow(chips);
  }
  function pedigreeNode(parent, ped) {
    if (!ped) return;
    var headline = pedigreeHeadline(ped);
    var items = list(ped.previous_games).filter(function (i) { return i && i.name; });
    if (!headline && !items.length) return;
    var box = section(parent, "From the studio");
    if (headline) box.appendChild(el("div", "ped-head", headline));
    // The publisher is a line of text, never a poster row: a publisher's back
    // catalogue is a distribution list, not a body of work.
    if (ped.publisher_name) {
      box.appendChild(el("div", "ped-pub", "published by " + ped.publisher_name));
    }
    if (!items.length) return;
    var strip = el("div", "strip ped-strip");
    items.forEach(function (item) {
      var card = el("div", "sim");
      card.appendChild(coverNode(item));
      var body = el("div", "sim-body");
      body.appendChild(el("div", "sim-name", item.name || "?"));
      if (item.release_year) body.appendChild(el("div", "sim-year", String(item.release_year)));
      var badges = pedigreeBadges(item);
      if (badges) body.appendChild(badges);
      card.appendChild(body);
      strip.appendChild(card);
    });
    box.appendChild(strip);
    var record = ped.library_track_record;
    if (record) {
      var avg = num(record.avg_my_rating);
      // The track record covers only the annotated (shown) games; when the
      // catalogue runs deeper, "last N" keeps the claim honest.
      var span = ped.previous_truncated
        ? "their last " + plural(items.length, "game")
        : "their " + plural(items.length, "previous game");
      box.appendChild(el("div", "note",
        "You've played " + (num(record.played_count) || 0) + " of "
        + span + (avg != null ? " — avg " + avg + "/10." : ".")));
    }
  }
"""

# ---- Size reporting + teardown ----------------------------------------------
# Debounced, change-only ui/notifications/size-changed reporting; teardown()
# stops every timer and the observer when the host tears the view down.
SIZING_JS = r"""  /* ---------- sizing ---------- */
  var sizeTimer = null;
  var lastSize = "";
  function reportSize() {
    if (tornDown || window.__PREVIEW_DATA__) return;
    clearTimeout(sizeTimer);
    sizeTimer = setTimeout(function () {
      sizeTimer = null;
      if (tornDown) return;
      // Only notify on real changes: some hosts (Android app) get confused
      // by a stream of identical/oscillating size notifications.
      var w = Math.ceil(document.documentElement.scrollWidth);
      var h = Math.ceil(document.documentElement.scrollHeight);
      var key = w + "x" + h;
      if (key === lastSize) return;
      lastSize = key;
      notify("ui/notifications/size-changed", { width: w, height: h });
    }, 120);
  }
  var resizeObserver = null;
  if (window.ResizeObserver) {
    resizeObserver = new ResizeObserver(reportSize);
    resizeObserver.observe(document.body);
  }
  /* After teardown the view is gone: reportSize() and host-context-changed
     become no-ops and no timer survives. */
  function teardown() {
    tornDown = true;
    clearTimeout(sizeTimer);
    sizeTimer = null;
    clearTimeout(hintTimer);
    if (resizeObserver) resizeObserver.disconnect();
  }
"""

# Startup: the preview globals, the skeleton, and the ui/initialize handshake
# (declaring both display modes; appInfo per the ext-apps SDK schema, with
# clientInfo kept as the legacy alias the published spec example used).
INIT_JS = r"""  /* ---------- startup ---------- */
  function startWidget(appName) {
    document.documentElement.setAttribute("data-display-mode", "inline");
    if (window.__PREVIEW_HOST_CONTEXT__) applyHostContext(window.__PREVIEW_HOST_CONTEXT__);
    if (window.__PREVIEW_DATA__) {
      render(window.__PREVIEW_DATA__);
      return;
    }
    showSkeleton();
    request("ui/initialize", {
      protocolVersion: "2026-01-26",
      appCapabilities: { availableDisplayModes: ["inline", "fullscreen"] },
      appInfo: { name: appName, version: "1.0" },
      clientInfo: { name: appName, version: "1.0" },
    }).then(function (res) {
      hostCaps = (res && res.hostCapabilities) || {};
      applyHostContext(res && res.hostContext);
      notify("ui/notifications/initialized");
    });
  }
"""
