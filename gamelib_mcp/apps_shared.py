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
widgets genuinely disagree on — the grid's cover plates, each lightbox's placement,
the evaluation card's verdict stamp — that stays local to its widget.
``tests/test_apps_eval.py::WidgetDriftTests`` fails if a block of any size
worth sharing reappears in both files instead.

Design system (docs/specs/2026-10-03-widget-ux-redesign.md §1, restyled by
"The Binder", docs/specs/2026-10-04-binder-design-language.md): every color,
radius, shadow and font is a ``--gl-*`` custom property defined ONCE in
``TOKENS_CSS`` as ``var(<host token>, <fallback>)`` (or a plain value when it
is theme-invariant). The host's ``hostContext.styles.variables`` (Claude's
theme tokens) win when present; the ``light-dark()`` fallbacks keep ChatGPT,
Goose and the offline preview rendering, and follow ``hostContext.theme`` (via
``color-scheme``) or, with no host at all, ``prefers-color-scheme``. Widget
CSS references ``--gl-*`` names only. Type is four sizes (20 / 16 / 14 / 12px)
in three weights (400 / 600 / 800); nothing renders below 12px. The Binder
components (the card ``.frame``, its art, badge, plate, stats, pips, ribbon,
traits, minis) and their motion live in ``BINDER_CSS`` / ``BINDER_JS``.
"""

import json
import re

from .data.purchases import PURCHASE_SOURCES
from .platforms_registry import PLATFORMS

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

# ownership.purchase_source is the closed PURCHASE_SOURCES vocabulary
# (data/purchases/__init__.py). The card reads it as "paid €12.00 via <label>", so the labels
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
# this block, the cover plate (its gradient is generated per name in
# coverNode; its ink is the --gl-plate-* tokens below) and, until Phase 2B of
# the Binder spec retires it, the verdict stamp rule (apps_eval.py, built from
# tokens). tests/test_apps.py::DesignSystemTests pins that.
_TOKENS_LAYER_CSS = r"""  :root {
    color-scheme: light dark;
    --gl-text: var(--color-text-primary, light-dark(#141413, #FAF9F5));
    --gl-text-2: var(--color-text-secondary, light-dark(#3D3D3A, #C2C0B6));
    /* Muted text clears 4.5:1 on both surfaces it sits on: light #6B6A64 is
       4.9:1 on the inset and 5.4:1 on white; dark #9C9A92 is 5.4:1 on the
       inset and 4.7:1 on the surface. */
    --gl-muted: var(--color-text-tertiary, light-dark(#6B6A64, #9C9A92));
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
    /* The tier edge: the 2px badge ring, the chip and mini-art border. */
    --gl-good-edge: var(--color-border-success, light-dark(#437426, #7AB948));
    --gl-ok-edge: var(--color-border-warning, light-dark(#B8891F, #D1A041));
    --gl-bad-edge: var(--color-border-danger, light-dark(#A73D39, #EE8884));
    --gl-inverse-bg: var(--color-background-inverse, light-dark(#141413, #FAF9F5));
    --gl-inverse-text: var(--color-text-inverse, light-dark(#FFFFFF, #141413));
    /* The Binder (docs/specs/assets/binder/gl.css). The keyline is the 1px
       inner line on art; the rarity stops are the three hues of each tier's
       brushed-metal card border, at the light-token lightness under light. */
    --gl-keyline: light-dark(rgba(20, 20, 19, 0.12), rgba(250, 249, 245, 0.14));
    --gl-rarity-good-1: light-dark(#265B19, #437426);
    --gl-rarity-good-2: light-dark(#437426, #7AB948);
    --gl-rarity-good-3: light-dark(#7AB948, #A8D98A);
    --gl-rarity-ok-1: light-dark(#805C1F, #8A6A24);
    --gl-rarity-ok-2: light-dark(#B8891F, #D1A041);
    --gl-rarity-ok-3: light-dark(#D1A041, #F0D08A);
    --gl-rarity-bad-1: light-dark(#7F2C28, #602A28);
    --gl-rarity-bad-2: light-dark(#A73D39, #EE8884);
    --gl-rarity-bad-3: light-dark(#EE8884, #A73D39);
    --gl-rarity-good: conic-gradient(from 210deg, var(--gl-rarity-good-1), var(--gl-rarity-good-2) 9%, var(--gl-rarity-good-3) 21%, var(--gl-rarity-good-1) 32%, var(--gl-rarity-good-2) 44%, var(--gl-rarity-good-3) 57%, var(--gl-rarity-good-1) 68%, var(--gl-rarity-good-2) 80%, var(--gl-rarity-good-3) 91%, var(--gl-rarity-good-1));
    --gl-rarity-ok: conic-gradient(from 210deg, var(--gl-rarity-ok-1), var(--gl-rarity-ok-2) 9%, var(--gl-rarity-ok-3) 21%, var(--gl-rarity-ok-1) 32%, var(--gl-rarity-ok-2) 44%, var(--gl-rarity-ok-3) 57%, var(--gl-rarity-ok-1) 68%, var(--gl-rarity-ok-2) 80%, var(--gl-rarity-ok-3) 91%, var(--gl-rarity-ok-1));
    --gl-rarity-bad: conic-gradient(from 210deg, var(--gl-rarity-bad-1), var(--gl-rarity-bad-2) 9%, var(--gl-rarity-bad-3) 21%, var(--gl-rarity-bad-1) 32%, var(--gl-rarity-bad-2) 44%, var(--gl-rarity-bad-3) 57%, var(--gl-rarity-bad-1) 68%, var(--gl-rarity-bad-2) 80%, var(--gl-rarity-bad-3) 91%, var(--gl-rarity-bad-1));
    /* Theme-invariant Binder inks: the deep plate (badge tag, ribbon ink),
       the ribbon fills and their bevel, the static specular line and the
       hover sheen on art, and the play chip's ring (it sits on art, so it
       never follows the theme either). */
    --gl-deep: #141413;
    --gl-deep-ink: #FAF9F5;
    --gl-ribbon-ink: #141413;
    --gl-ribbon-good: #7AB948;
    --gl-ribbon-ok: #D1A041;
    --gl-ribbon-bad: #EE8884;
    --gl-ribbon-none: #C2C0B6;
    --gl-ribbon-hi: rgba(255, 255, 255, 0.35);
    --gl-ribbon-lo: rgba(0, 0, 0, 0.22);
    --gl-specular: rgba(255, 255, 255, 0.12);
    --gl-sheen: rgba(255, 255, 255, 0.16);
    --gl-play-ring: rgba(20, 20, 19, 0.35);
    /* What every component reads; a .tier-* class (below) re-points them. */
    --gl-tier: var(--gl-border-strong);
    --gl-tier-text: var(--gl-text-2);
    --gl-tier-fill: var(--gl-ribbon-none);
    --gl-rarity: none;
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
    /* The Binder card: 14px radius / 6px frame, the grid card 12px / 4px. */
    --gl-r-card: 14px;
    --gl-r-card-s: 12px;
    --gl-frame: 6px;
    --gl-bw: var(--border-width-regular, 0.5px);
    --gl-shadow: var(--shadow-sm, 0 1px 3px 0 rgba(0, 0, 0, 0.1), 0 1px 2px -1px rgba(0, 0, 0, 0.1));
    --gl-shadow-md: var(--shadow-md, 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -2px rgba(0, 0, 0, 0.1));
    --gl-font: var(--font-sans, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif);
    /* Numerals (stat values, badge numbers, prices) and flavor text only. */
    --gl-mono: var(--font-mono, ui-monospace, Menlo, Consolas, monospace);
    --gl-serif: ui-serif, Georgia, "Times New Roman", serif;
    /* Exactly four sizes (20 / 16 / 14 / 12px: title, h, body, cap). Clamped:
       a host token below 12px never shrinks the type under the floor
       ("nothing renders below 12px"). */
    --gl-title: max(12px, var(--font-heading-lg-size, 20px));
    --gl-h: max(12px, var(--font-heading-md-size, 16px));
    --gl-body: max(12px, var(--font-text-sm-size, 14px));
    --gl-cap: max(12px, var(--font-text-xs-size, 12px));
    --gl-title-lh: var(--font-heading-lg-line-height, 1.25);
    --gl-h-lh: var(--font-heading-md-line-height, 1.4);
    --gl-body-lh: var(--font-text-sm-line-height, 1.4);
    --gl-cap-lh: var(--font-text-xs-line-height, 1.4);
    /* Exactly three weights; mono numerals use the same three. */
    --gl-regular: var(--font-weight-normal, 400);
    --gl-strong: var(--font-weight-semibold, 600);
    --gl-heavy: 800;
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

# ``--gl-x: var(--host, light-dark(L, D));`` or, with no host token,
# ``--gl-x: light-dark(L, D);`` (the Binder's keyline and rarity stops).
_LIGHT_DARK_TOKEN = re.compile(
    r"^    (--gl-[a-z0-9-]+): "
    r"(?:var\((--[a-z0-9-]+), light-dark\((.+)\)\)|light-dark\((.+)\));$",
    re.MULTILINE,
)


def _split_pair(args: str) -> tuple[str, str]:
    """``"rgba(1, 2, 3, 0.1), #FFF"`` → the two top-level arguments."""
    depth = 0
    for i, ch in enumerate(args):
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            return args[:i].strip(), args[i + 1:].strip()
    raise ValueError(f"light-dark() needs two arguments: {args!r}")


def _plain_color_fallback(layer: str) -> str:
    """The color tokens again as plain values, for WebViews without light-dark().

    Generated from the layer above, so the two can never drift: every
    ``--gl-*: var(<host token>, light-dark(L, D))`` becomes ``var(<host
    token>, L)`` by default and ``var(<host token>, D)`` under a dark
    ``prefers-color-scheme`` (unless the host forced light) or a host dark
    theme. Host variables still win either way. A token with no host variable
    (``--gl-*: light-dark(L, D)``) becomes the plain ``L`` / ``D``.
    """
    tokens = [
        (name, host, *_split_pair(hosted or bare))
        for name, host, hosted, bare in _LIGHT_DARK_TOKEN.findall(layer)
    ]

    def value(host: str, color: str) -> str:
        return f"var({host}, {color})" if host else color

    def block(indent: str, pick: int) -> str:
        return "".join(
            f"{indent}{name}: {value(host, (light, dark)[pick])};\n"
            for name, host, light, dark in tokens
        )

    return (
        "  /* Older WebViews without light-dark(): the same color tokens as plain\n"
        "     values (generated from the layer above) — light by default, dark\n"
        "     under a dark prefers-color-scheme or a host dark theme. */\n"
        "  @supports not (color: light-dark(red, blue)) {\n"
        "    :root {\n" + block("      ", 0) + "    }\n"
        "    @media (prefers-color-scheme: dark) {\n"
        "      :root:not([data-theme=\"light\"]) {\n" + block("        ", 1) + "      }\n"
        "    }\n"
        "    :root[data-theme=\"dark\"] {\n" + block("      ", 1) + "    }\n"
        "  }\n"
    )


# Theme-dependent values that are not colors (light-dark() takes colors only),
# chosen the way the plain fallback above chooses: light by default, dark under
# a dark prefers-color-scheme (unless the host forced light) or a host dark
# theme.
_THEMED_VALUES_CSS = r"""  :root { --gl-grain-opacity: 0.035; }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) { --gl-grain-opacity: 0.06; }
  }
  :root[data-theme="dark"] { --gl-grain-opacity: 0.06; }
"""

# The four tier classes. They work on ANY component: each re-points the tier
# tokens every Binder component reads — --gl-tier (edge, ring, chip border),
# --gl-tier-text (value, pip), --gl-tier-fill (ribbon) and --gl-rarity (the
# card border). Color encodes the quality tier only, never the brand.
_TIERS_CSS = r"""  .tier-good { --gl-tier: var(--gl-good-edge); --gl-tier-text: var(--gl-good); --gl-tier-fill: var(--gl-ribbon-good); --gl-rarity: var(--gl-rarity-good); }
  .tier-ok { --gl-tier: var(--gl-ok-edge); --gl-tier-text: var(--gl-ok); --gl-tier-fill: var(--gl-ribbon-ok); --gl-rarity: var(--gl-rarity-ok); }
  .tier-bad { --gl-tier: var(--gl-bad-edge); --gl-tier-text: var(--gl-bad); --gl-tier-fill: var(--gl-ribbon-bad); --gl-rarity: var(--gl-rarity-bad); }
  .tier-none { --gl-tier: var(--gl-border-strong); --gl-tier-text: var(--gl-text-2); --gl-tier-fill: var(--gl-ribbon-none); --gl-rarity: none; }
"""

TOKENS_CSS = (
    _TOKENS_LAYER_CSS
    + _plain_color_fallback(_TOKENS_LAYER_CSS)
    + _THEMED_VALUES_CSS
    + _TIERS_CSS
)

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
# Hit-area rule: every small interactive element carries an invisible ::after
# that extends its tap target past its visual box — >=32px on pointer devices,
# >=44px on html.touch — and the extension NEVER reaches a sibling's own box.
# A chip row's gap is therefore at least twice the extension on that axis
# (.chips: 8px rows / 6px columns against -4px / -3px; on touch 12px / 8px
# against -6px / -4px). Link chips are at least 24px tall (CHIP_CSS), so
# 24 + 2x4 = 32 on a pointer; on touch the chips grow to a 32px visual height
# so 32 + 2x6 = 44 is met by the chip's own height plus the extension, not by
# overlapping the next row. The host element must be a containing block
# (positioned, or transformed like the stamp); .btn and .disclosure are made
# relative in CONTROLS_CSS, a tappable .frame in FRAME_CSS and a mini card's
# link or button in MINI_CSS. The old grid cards and media thumbs carry no
# extension: their overflow: hidden would clip it, and both are far past 44px
# already. The Binder's card and pill take the ring 3px out (gl.css) in the
# same text color — the reference sheet's 0.4-alpha border-strong ring is the
# contrast failure described above, so its color is not ported; the thumbs
# and minis keep 2px, the reach their strip's padding leaves unclipped.
A11Y_CSS = r"""  :focus-visible { outline: 2px solid var(--gl-text); outline-offset: 2px; }
  :focus:not(:focus-visible) { outline: none; }
  .frame:focus-visible, .btn:focus-visible, a.art:focus-visible { outline-offset: 3px; }
  a.chip::after, .btn::after, .disclosure::after, .fs-btn::after,
  .car-nav::after, .overlay-close::after, .hero-pill::after,
  .stamp[role="button"]::after, button.stamp::after,
  .frame[role="button"]::after, button.frame::after, .mini a::after, .mini button::after {
    content: "";
    position: absolute;
    inset: -4px;
  }
  .chips a.chip::after { inset: -4px -3px; }
  html.touch .chip, html.touch .hero-pill { min-height: 32px; }
  html.touch a.chip::after, html.touch .btn::after, html.touch .disclosure::after,
  html.touch .fs-btn::after, html.touch .car-nav::after,
  html.touch .overlay-close::after, html.touch .hero-pill::after,
  html.touch .stamp[role="button"]::after, html.touch button.stamp::after,
  html.touch .frame[role="button"]::after, html.touch button.frame::after,
  html.touch .mini a::after, html.touch .mini button::after {
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
  /* The eyebrow over a block or strip ("IN YOUR LIBRARY"): the label style
     (12px / 600, uppercase, tracked) in the tertiary text color. */
  .section-title {
    display: block;
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    font-weight: var(--gl-strong);
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--gl-muted);
    margin-bottom: 10px;
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
# The ONE chip for every score. Color encodes quality tier only — on the 1px
# border and the value, never a fill (the chip's ground is the card surface,
# which also keeps a chip readable where it sits on cover art) — and the
# brand is the label text. A
# figure ("83", "25h", "€19.99") sits in the mono numerals; a phrase ("Very
# positive", "Strong") is a ``b.word`` in the label face (scoreChip decides).
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
    min-height: 24px;
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    font-weight: var(--gl-regular);
    padding: 2px 7px;
    border-radius: var(--gl-r-xs);
    border: 1px solid var(--gl-tier);
    background: var(--gl-surface);
    color: var(--gl-text-2);
    font-variant-numeric: tabular-nums;
    text-decoration: none;
  }
  .chip .lbl { color: var(--gl-text-2); white-space: nowrap; }
  .chip b {
    font-family: var(--gl-mono);
    font-size: var(--gl-h);
    font-weight: var(--gl-regular);
    line-height: 1;
    color: var(--gl-tier-text);
  }
  .chip b.word {
    font-family: var(--gl-font);
    font-size: var(--gl-cap);
    font-weight: var(--gl-strong);
    line-height: var(--gl-cap-lh);
  }
  .chip .aux { color: var(--gl-muted); white-space: nowrap; }
  /* TODO(Binder Phase 2A): tests/test_apps.py::GameCardsResourceTests still
     pins these three fill rules verbatim; the Binder chip has no tier fill, so
     the rule right after them resets it. Drop all four when that pin is
     rewritten to the border/value rule. */
  .chip.tier-good { background: var(--gl-good-bg); border-color: var(--gl-good-edge); color: var(--gl-good); }
  .chip.tier-ok { background: var(--gl-ok-bg); border-color: var(--gl-ok-edge); color: var(--gl-ok); }
  .chip.tier-bad { background: var(--gl-bad-bg); border-color: var(--gl-bad-edge); color: var(--gl-bad); }
  .chip.tier-good, .chip.tier-ok, .chip.tier-bad { background: var(--gl-surface); border-color: var(--gl-tier); color: var(--gl-text-2); }
  .chip .meter {
    position: relative;
    width: 28px;
    height: 4px;
    background: var(--gl-border);
    overflow: hidden;
    flex: none;
  }
  .chip .meter-fill { display: block; height: 100%; background: var(--gl-tier-text); }
  .chip .ext { color: var(--gl-muted); }
  /* 24px + the -4px extension = a 32px target on a pointer (A11Y_CSS). */
  a.chip { cursor: pointer; min-height: 24px; }
  a.chip:hover { background: var(--gl-inset); }
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
# card, detail card). Each card and panel is a common (tier-none) Binder frame
# — a 1px hairline, the surface-colored frame and a 2px inner keyline — holding
# inset blocks; the 1.6s pulse (MOTION_CSS's skel-pulse) only runs when the
# viewer allows motion.
SKELETON_CSS = r"""  .skel { display: flex; flex-direction: column; gap: 12px; max-width: 760px; }
  .skel-grid { max-width: none; gap: 10px; }
  .skel-eval { margin: 0 auto; width: 100%; }
  .skel-detail, .skel-neutral { max-width: 720px; }
  .sk-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(142px, 1fr)); gap: 12px; }
  .sk-card, .sk-panel {
    background: var(--gl-surface);
    border: var(--gl-frame) solid var(--gl-surface);
    border-radius: var(--gl-r-card);
    box-shadow: 0 0 0 1px var(--gl-border), inset 0 0 0 2px var(--gl-border);
    overflow: hidden;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .sk-card { border-width: 4px; border-radius: var(--gl-r-card-s); padding: 2px; }
  .sk-body { padding: 8px 10px 10px; display: flex; flex-direction: column; gap: 8px; }
  .sk-panel { padding: 12px; }
  .sk-row { display: flex; gap: 14px; align-items: flex-start; }
  .sk-col { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 8px; padding-top: 4px; }
  .sk { background: var(--gl-inset); border-radius: var(--gl-r-xs); }
  .sk-cover { aspect-ratio: 2 / 3; border-radius: var(--gl-r-sm) var(--gl-r-sm) 0 0; }
  .sk-thumb { flex: 0 0 84px; aspect-ratio: 2 / 3; }
  .skel-detail .sk-thumb { flex-basis: 120px; }
  .sk-head { height: 12px; width: 60%; max-width: 340px; }
  .sk-line { height: 12px; }
  .sk-line.short { width: 55%; }
  .sk-line.wide { height: 16px; width: 70%; }
  .sk-bar { height: 4px; border-radius: var(--gl-r-full); }
  .sk-chips { display: flex; gap: 6px; flex-wrap: wrap; }
  .sk-chip { width: 76px; height: 24px; }
  .sk-card .sk-chip { width: 64px; }
  .sk-stamp { flex: 0 0 96px; height: 40px; }
  .sk-facts { margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--gl-border); }
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
    .sk { animation: skel-pulse 1600ms ease-in-out infinite; }
  }
"""

# Buttons (Binder pills: 40px on a pointer, 44px on touch; the primary in the
# inverse fill, the secondary a 1px border-strong outline; an optional leading
# 20px SVG glyph), the disclosure toggle and the muted failure notice with
# its 16px outlined "!" (built in NOTICE_JS).
CONTROLS_CSS = r"""  .btn, .disclosure {
    position: relative;
    min-height: 40px;
    padding: 0 18px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    border-radius: var(--gl-r-full);
    border: 1px solid var(--gl-border-strong);
    background: transparent;
    color: var(--gl-text);
    font-size: var(--gl-h);
    font-weight: var(--gl-strong);
    text-decoration: none;
    cursor: pointer;
  }
  .btn > svg, .disclosure > svg {
    width: 20px;
    height: 20px;
    flex: none;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.7;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .btn.primary { background: var(--gl-inverse-bg); color: var(--gl-inverse-text); border-color: transparent; }
  html.touch .btn, html.touch .disclosure { min-height: 44px; }
  .disclosure { width: 100%; }
  /* A full-width toggle whose label can run long ("Similar games you own ·
     From the studio"): body size keeps it on one line at 360px. */
  .disclosure { font-size: var(--gl-body); }
  .disclosure .chev { transition: transform 0.15s ease; }
  .disclosure[aria-expanded="true"] .chev { transform: rotate(180deg); }
  .disclosure-body { display: flex; flex-direction: column; gap: 12px; margin-top: 12px; }
  .disclosure-body[hidden] { display: none; }
  .notice {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    color: var(--gl-muted);
    padding: 4px 0;
  }
  .notice > svg {
    width: 16px;
    height: 16px;
    flex: none;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.5;
    stroke-linecap: round;
  }
"""

# All component CSS in splice order (one line per widget, not five).
COMPONENTS_CSS = CHIP_CSS + MATCH_BAR_CSS + SKELETON_CSS + CONTROLS_CSS

# ---- Media CSS: hero stage, strips, thumbs ----------------------------------
# The 16:9 trailer/screenshot stage (the Binder reel: 8px radius, the 1px
# keyline drawn over the media), its poster, the play button — a stage-wide
# target whose visible part is the 44px round chip bottom-right, so it never
# covers the title lettering — and the link pill (bottom-left, clear of it).
HERO_CSS = r"""  .hero {
    position: relative;
    border-radius: var(--gl-r-md);
    overflow: hidden;
    background: var(--gl-stage);
    aspect-ratio: 16 / 9;
  }
  .hero::before {
    content: "";
    position: absolute;
    inset: 0;
    z-index: 4;
    border-radius: inherit;
    box-shadow: inset 0 0 0 1px var(--gl-keyline);
    pointer-events: none;
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
    display: block;
    background: var(--gl-stage-veil);
    border: 0;
    padding: 0;
    cursor: pointer;
    -webkit-tap-highlight-color: transparent;
  }
  .play-badge span {
    position: absolute;
    right: 10px;
    bottom: 10px;
    width: 44px;
    height: 44px;
    border-radius: var(--gl-r-full);
    background: var(--gl-deep-ink);
    color: var(--gl-deep);
    box-shadow: 0 0 0 1px var(--gl-play-ring);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: var(--gl-body);
    line-height: 1;
    padding-left: 3px;
    transition: transform 0.12s ease;
  }
  .play-badge:hover span, .play-badge:focus-visible span { transform: scale(1.06); }
  .play-badge:focus-visible span { outline: 2px solid var(--gl-deep-ink); outline-offset: 2px; }
  .hero-pill {
    position: absolute;
    left: 10px;
    bottom: 10px;
    z-index: 2;
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    font-weight: var(--gl-strong);
    padding: 4px 10px;
    border-radius: var(--gl-r-full);
    border: 1px solid var(--gl-border);
    background: var(--gl-surface);
    color: var(--gl-text);
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
    text-align: center;
    padding: 0 12px;
  }
"""

# Sideways-scrolling strip shared by thumbs, similar games and pedigree.
# The 4px padding on top and both sides is the focus ring's reach (2px ring +
# 2px offset): a scroll container clips at its padding box, so anything less
# shaves the ring off a focused thumb or card. The matching negative inline
# margin keeps the items flush with the panel's content edge — the ring draws
# into the panel's own padding.
STRIP_CSS = r"""  .strip {
    display: flex;
    gap: 10px;
    overflow-x: auto;
    margin-inline: -4px;
    padding: 4px 4px 6px;
    scrollbar-width: thin;
    scroll-snap-type: x proximity;
    scroll-padding-inline: calc(4px + var(--gl-safe-left)) calc(4px + var(--gl-safe-right));
    overscroll-behavior-x: contain;
  }
  .strip > * { scroll-snap-align: start; }
  /* Trailing spacer: the last item can scroll fully into view (clear of the
     safe area) instead of ending flush against a clipped edge. */
  .strip::after { content: ""; flex: 0 0 max(4px, var(--gl-safe-right)); }
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

# The fullscreen button and the thumb strip's thumbnails (4px radius, the 1px
# keyline over the image; the selected thumb carries a 2px inverse outline).
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
  .thumbs { margin-top: 8px; }
  .thumb {
    position: relative;
    flex: none;
    padding: 0;
    border: 0;
    border-radius: var(--gl-r-xs);
    overflow: hidden;
    background: var(--gl-inset);
    cursor: pointer;
    -webkit-tap-highlight-color: transparent;
  }
  .thumb::before {
    content: "";
    position: absolute;
    inset: 0;
    z-index: 1;
    border-radius: inherit;
    box-shadow: inset 0 0 0 1px var(--gl-keyline);
    pointer-events: none;
  }
  .thumb img { display: block; width: 116px; height: 66px; object-fit: cover; }
  /* On the keyline layer, not the outline: the focus ring keeps the outline. */
  .thumb.sel::before { box-shadow: inset 0 0 0 2px var(--gl-inverse-bg); }
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
# hover, display mode). Opens the IIFE both widgets live inside, and declares
# the hooks a widget assigns instead of reassigning a shared function.
BRIDGE_JS = r"""(function () {
  "use strict";

  /* ---------- widget hooks ---------- */
  /* The shared blocks call these at fixed points; a widget ASSIGNS the ones
     it needs (hooks.afterHostContext = …) rather than wrapping a shared
     function, so every shared caller — the router, the observer, a timer —
     reaches the widget's logic. */
  var hooks = {
    afterToolInput: function () {},             // (args) after lastToolInput is stored
    afterToolResult: function () {},            // (data) after render(data)
    afterHostContext: function () {},           // (ctx) after a host context is applied
    shouldReportSize: function () { return true; },
  };

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
      case "ping":                                               // liveness check
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
  var safeInsets = { top: 0, right: 0, bottom: 0, left: 0 };
  var deviceCaps = {};   // merged across partial deviceCapabilities updates
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
      /* Merged like the rest of the context: a partial update (one changed
         token) must not wipe the theme. A variable goes only when the host
         explicitly sets it to null or "", and the --gl-* fallback then shows. */
      Object.keys(vars).forEach(function (name) {
        if (name.indexOf("--") !== 0) return;
        var value = vars[name];
        if (value === null || value === "") docEl.style.removeProperty(name);
        else if (value !== undefined) docEl.style.setProperty(name, String(value));
      });
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
    /* Merged per side, like the rest: only a side sent as a number changes;
       an absent side keeps the inset it had. */
    var insets = ctx.safeAreaInsets;
    if (insets && typeof insets === "object") {
      [["top", "Top"], ["right", "Right"], ["bottom", "Bottom"], ["left", "Left"]].forEach(function (side) {
        var sent = insets[side[0]];
        if (typeof sent === "number" && isFinite(sent)) safeInsets[side[0]] = Math.max(0, sent);
        var extra = safeInsets[side[0]];
        document.body.style["padding" + side[1]] = (BASE_GUTTER + extra) + "px";
        if (side[0] === "left" || side[0] === "right") {
          docEl.style.setProperty("--gl-safe-" + side[0], extra + "px");
        }
      });
      hostContext.safeAreaInsets = {
        top: safeInsets.top, right: safeInsets.right, bottom: safeInsets.bottom, left: safeInsets.left,
      };
    }
    var device = ctx.deviceCapabilities;
    if (device && typeof device === "object") {
      // host-context-changed may carry only the changed key ({hover: false}):
      // merge into the remembered capabilities, then apply the merged view,
      // so an omitted `touch` never shrinks the 44px hit areas back down.
      if (typeof device.touch === "boolean") deviceCaps.touch = device.touch;
      if (typeof device.hover === "boolean") deviceCaps.hover = device.hover;
      hostContext.deviceCapabilities = { touch: deviceCaps.touch, hover: deviceCaps.hover };
      if (typeof deviceCaps.touch === "boolean") docEl.classList.toggle("touch", deviceCaps.touch);
      if (typeof deviceCaps.hover === "boolean") docEl.classList.toggle("no-hover", deviceCaps.hover === false);
    } else if (!hostContext.deviceCapabilities) {
      applyInputFallback();
    }
    if (ctx.displayMode) docEl.setAttribute("data-display-mode", String(ctx.displayMode));
    hooks.afterHostContext(ctx);
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

# structuredContent-or-text result unwrapping, the error text of an isError
# result, plus the tool-input and tool-cancelled handlers the bridge routes to.
TOOL_RESULT_JS = r"""  var lastToolInput = null;
  var lastToolMeta = null;
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
  /* The tool this view belongs to: the host's toolInfo when it sent one,
     else a name riding on the tool-input's _meta; null when neither says. */
  function toolName() {
    var info = hostContext.toolInfo;
    var name = info && info.tool && info.tool.name;
    if (!name && lastToolMeta) name = lastToolMeta.toolName || lastToolMeta.name;
    return typeof name === "string" && name ? name : null;
  }
  /* A tool error names its source and its own cause — "get_game_detail
     failed: Game not found." (or "The tool failed: …" when no name is known)
     — at most 160 chars, ending as a sentence so a follow-up ("Showing what
     the list had.") can come after it. `name` overrides the view's own tool
     (the drill-in's app-initiated call is a different tool). */
  function toolErrorText(result, name) {
    var text = (list(result && result.content).find(function (c) {
      return c && c.type === "text";
    }) || {}).text;
    var source = name || toolName();
    text = (source ? source + " failed: " : "The tool failed: ")
      + String(text || "it reported an error").replace(/\s+/g, " ").trim();
    if (text.length > 160) text = text.slice(0, 159).trim() + "…";
    return /[.!?…]$/.test(text) ? text : text + ".";
  }
  function handleToolResult(result) {
    gotResult = true;
    var data = result && result.isError ? null : resultData(result);
    if (data) {
      render(data);
      hooks.afterToolResult(data);
      return;
    }
    // Never leave the skeleton pulsing forever over a result we can't show.
    if (!rootShowsContent()) root.textContent = "";
    notice(root, result && result.isError ? toolErrorText(result) : "Couldn't read the result");
    reportSize();
  }
  /* The arguments arrive before the result: keep them (the grid's header
     line is built from them). The widget's afterToolInput hook swaps the
     neutral startup skeleton for its own shape. */
  function handleToolInput(params) {
    lastToolInput = (params && params.arguments) || {};
    var meta = params && params._meta;
    lastToolMeta = meta && typeof meta === "object" ? meta : null;
    hooks.afterToolInput(lastToolInput);
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
# ``root``/``el``/``section``/``list``/``num`` — the whole DOM helper vocabulary.
DOM_HELPERS_JS = r"""  var root = document.getElementById("root");

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }
  /* A panel with an eyebrow title, appended to parent. */
  function section(parent, title) {
    var box = el("section", "panel");
    if (title) box.appendChild(el("div", "section-title", title));
    parent.appendChild(box);
    return box;
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
  /* "114k reviews": a count always carries its noun. The unit follows the
     ROUNDED value, so a count never reads "1000k" or "10.0k": 999,600 → 1M,
     9,950 → 10k, 1,049 → 1k, 999 → 999. */
  function compactCount(v, word) {
    var n = num(v);
    if (n == null) return null;
    var r = Math.round(n);
    var k = Math.round(r / 1000);                   // whole thousands
    var k1 = Math.round(r / 100) / 10;              // thousands, one decimal
    var text = r >= 1000000 || k >= 1000 ? (Math.round(r / 100000) / 10) + "M"
      : r >= 10000 || k1 >= 10 ? k + "k"
      : r >= 1000 ? k1 + "k"
      : String(r);
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
  /* A figure ("83", "25h", "~4.1h/wk", "€19.99", "9/10") sits in the mono
     numerals; a value with a space or no digit at all is a phrase. */
  function isFigure(text) {
    var t = String(text);
    return /\d/.test(t) && !/\s/.test(t);
  }
  /* {label, value, tier, title, url, meter (0-100), aux, cls} → one chip.
     Color is the quality tier only; the brand is the label text. A phrase
     value is a b.word (the label face), a figure a plain b (mono). */
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
      var value = String(opts.value);
      chip.appendChild(el("b", isFigure(value) ? null : "word", value));
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
  /* The phrase always rides with the meter — the meter alone says nothing.
     opts.meter === false (a 150px grid card): label and phrase only, a phrase
     of two or more words on a line of its own (.steam-line, styled by the
     grid), with a real space after the label as its break point. */
  function steamChip(desc, url, opts) {
    if (!desc) return null;
    var compact = !!opts && opts.meter === false;
    var step = steamStep(desc);
    var phrase = String(desc).toLowerCase();
    phrase = phrase.charAt(0).toUpperCase() + phrase.slice(1);
    var words = phrase.split(/\s+/).filter(Boolean).length;
    var chip = scoreChip({
      label: "Steam", value: phrase, tier: steamTier(desc),
      meter: compact || step == null ? null : Math.round((step / 9) * 100),
      title: step == null ? "Steam reviews" : "Steam reviews: " + step + " of 9 on Steam's scale",
      url: url, cls: compact && words >= 2 ? "steam-line" : "",
    });
    if (compact) chip.insertBefore(document.createTextNode(" "), chip.querySelector("b"));
    return chip;
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
     and the media stage. neutral = one panel (cover, three lines, a chip
     row): the startup shape, before the tool input says which tool ran. */
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
    if (kind === "neutral") {
      var box = el("div", "sk-panel");
      var line = el("div", "sk-row");
      line.appendChild(sk("sk-thumb"));
      var text = el("div", "sk-col");
      lines(text, 3);
      chips(text, 3);
      line.appendChild(text);
      box.appendChild(line);
      wrap.appendChild(box);
      return wrap;
    }
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
  /* Resolves to the mode the host GRANTED; on an error or a silence of
     timeoutMs (default 2.5s) it resolves to the mode we are in by then — a
     host-context-changed that arrived meanwhile is the truth, not the mode
     we started from. */
  function requestDisplayMode(mode, timeoutMs) {
    return Promise.race([
      request("ui/request-display-mode", { mode: mode }),
      new Promise(function (resolve) {
        setTimeout(function () { resolve(undefined); }, timeoutMs || 2500);
      }),
    ]).then(function (res) {
      var granted = res && res.mode ? String(res.mode) : currentDisplayMode();
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

# The in-place "Full breakdown ▾" disclosure (content built on first open
# only), and the ONE "open it big" control both widgets use on top of it:
# fullscreen where the host offers it, the disclosure in place where it
# doesn't or refuses.
DISCLOSURE_JS = r"""  var disclosureSeq = 0;
  /* intercept(), when given, runs on a click that would OPEN the body; a
     true return means it took the click (setOpen opens it later, or not). */
  function disclosure(parent, text, buildFn, intercept) {
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
    function setOpen(open) {
      if (open && !built) { built = true; buildFn(body); }
      body.hidden = !open;
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      reportSize();
    }
    btn.addEventListener("click", function () {
      var open = btn.getAttribute("aria-expanded") !== "true";
      if (open && intercept && intercept()) return;
      setOpen(open);
    });
    parent.appendChild(btn);
    parent.appendChild(body);
    return { button: btn, body: body, setOpen: setOpen };
  }
  /* The button that opens a block "big": on a host offering fullscreen (and
     not in it already) a click asks for fullscreen — ⤢ says so — and on a
     grant hands over to onFullscreen (the evaluation card builds its
     breakdown under the card) or, without one, opens the block in place.
     No fullscreen on offer, or a refusal, opens the disclosure in place, and
     after a refusal every later click stays in place. */
  function fullscreenOrDisclosure(parent, text, build, onFullscreen) {
    var inPlace = false;
    var asking = false;
    var d = disclosure(parent, text, build, function () {
      if (inPlace || !canFullscreen() || currentDisplayMode() === "fullscreen") return false;
      if (asking) return true;
      asking = true;
      requestDisplayMode("fullscreen").then(function (mode) {
        asking = false;
        if (mode === "fullscreen" && onFullscreen) { onFullscreen(); return; }
        if (mode !== "fullscreen") inPlace = true;
        chev.textContent = "▾";
        d.setOpen(true);
      });
      return true;
    });
    var chev = d.button.querySelector(".chev");
    if (canFullscreen() && currentDisplayMode() !== "fullscreen") chev.textContent = "⤢";
    return d;
  }
"""

# The one muted failure line: "Couldn't load: trailer (Steam), studio (IGDB)",
# led by its 16px outlined "!". It carries the SVG builder every Binder icon
# uses (svgEl / iconNode): here, because NOTICE_JS is the first shared block
# that draws one and some test shims splice it alone.
NOTICE_JS = r"""  /* ---------- SVG (icons, the card grain) ---------- */
  /* One SVG element: attributes set verbatim, kids appended (nulls skipped).
     Null where the document cannot make SVG at all, so every caller degrades
     to its text alone. Strings only ever reach attributes — never markup. */
  var SVG_NS = "http://www.w3.org/2000/svg";
  function svgEl(tag, attrs, kids) {
    if (!document.createElementNS) return null;
    var node = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, String(attrs[k])); });
    list(kids).forEach(function (kid) { if (kid) node.appendChild(kid); });
    return node;
  }
  /* A decorative icon: [[tag, attrs], ...] shapes in a viewBox, hidden from
     assistive tech (the text beside it says what it means). */
  function iconNode(viewBox, shapes) {
    return svgEl("svg", { viewBox: viewBox, "aria-hidden": "true", focusable: "false" },
      list(shapes).map(function (s) { return svgEl(s[0], s[1]); }));
  }
  var NOTICE_ICON = [["circle", { cx: 8, cy: 8, r: 6.5 }], ["path", { d: "M8 4.8v3.8M8 11.1v.1" }]];
  function notice(parent, items) {
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
    var node = el("div", "notice");
    node.setAttribute("role", "status");
    var icon = iconNode("0 0 16 16", NOTICE_ICON);
    if (icon) node.appendChild(icon);
    node.appendChild(el("span", null, text));
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

# Cover art with the gradient-plate fallback on a missing/broken image;
# ``coverPlate`` is the plate alone (the evaluation card's anchor covers).
COVER_NODE_JS = r"""  function coverPlate(name, cls, text) {
    var hue = coverHue(name || "?");
    var plate = el("div", cls, text);
    plate.style.background =
      "linear-gradient(160deg, hsl(" + hue + ",45%,38%), hsl(" + ((hue + 40) % 360) + ",50%,22%))";
    return plate;
  }
  function coverNode(game) {
    var wrap = el("div", "cover-wrap");
    var fallback = coverPlate(game.name, "cover-fallback", game.name || "?");
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

# ---- The Binder: card components + motion -----------------------------------
# docs/specs/2026-10-04-binder-design-language.md §1.2, ported from
# docs/specs/assets/binder/gl.css (repo class names: unprefixed, like .chip).
# Every game is a collectible card: a ``.frame`` (the rarity border encodes
# the one tier the card is about), its ``.grain``, the ``.art`` window, the
# ``.badge``, the title ``.plate``, the ``.stats`` block, ``.pips``, the
# ``.ribbon``, ``.trait`` rows, ``.ability`` run-ins, ``.flavor`` prose and
# ``.mini`` cards for strips. Each part is a public constant; BINDER_CSS /
# BINDER_JS splice them all, in this order, into both widgets.

# The card frame: a 6px (grid: 4px) conic rarity border via the padding-box /
# border-box background trick; a common card (tier-none, or no tier class) is
# a 1px hairline plus a 2px inner keyline in the same footprint. A tappable
# card is a <button class="frame …"> (or role=button). The grain is an inline
# SVG (grainNode), never a background image — the CSP forbids data: URIs.
FRAME_CSS = r"""  .frame {
    position: relative;
    display: flex;
    flex-direction: column;
    border: var(--gl-frame) solid transparent;
    border-radius: var(--gl-r-card);
    color: var(--gl-text);
    background: linear-gradient(var(--gl-surface), var(--gl-surface)) padding-box, var(--gl-rarity) border-box;
  }
  .frame-s { --gl-frame: 4px; border-radius: var(--gl-r-card-s); }
  .frame:not(.tier-good):not(.tier-ok):not(.tier-bad) {
    background: var(--gl-surface);
    border-color: var(--gl-surface);
    box-shadow: 0 0 0 1px var(--gl-border), inset 0 0 0 2px var(--gl-border);
  }
  button.frame, a.frame { width: 100%; padding: 0; text-align: left; text-decoration: none; cursor: pointer; }
  .frame[role="button"] { cursor: pointer; }
  .grain {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    border-radius: calc(var(--gl-r-card) - var(--gl-frame));
    overflow: hidden;
    opacity: var(--gl-grain-opacity);
    mix-blend-mode: overlay;
    pointer-events: none;
  }
  .frame-s > .grain { border-radius: calc(var(--gl-r-card-s) - var(--gl-frame)); }
"""

# The art window: 2:3 cover (or 16:9 with .art-hero), the 1px keyline drawn
# over the image, one static 12% specular line. coverNode() drops straight in
# (its wrap fills the window), so a missing cover is the name-seeded plate.
ART_CSS = r"""  .art {
    position: relative;
    display: block;
    aspect-ratio: 2 / 3;
    overflow: hidden;
    border-radius: var(--gl-r-md);
    background: var(--gl-inset);
  }
  .art-hero { aspect-ratio: 16 / 9; }
  .frame > .art { border-radius: calc(var(--gl-r-card) - var(--gl-frame)) calc(var(--gl-r-card) - var(--gl-frame)) 0 0; }
  .frame-s > .art { border-radius: calc(var(--gl-r-card-s) - var(--gl-frame)) calc(var(--gl-r-card-s) - var(--gl-frame)) 0 0; }
  .art > img { display: block; width: 100%; height: 100%; object-fit: cover; }
  .art > .cover-wrap { position: absolute; inset: 0; aspect-ratio: auto; }
  .art::before {
    content: "";
    position: absolute;
    inset: 0;
    z-index: 1;
    border-radius: inherit;
    box-shadow: inset 0 0 0 1px var(--gl-keyline);
    pointer-events: none;
  }
  .art::after {
    content: "";
    position: absolute;
    inset: 0 -24px;
    z-index: 1;
    pointer-events: none;
    background: linear-gradient(235deg, transparent calc(30% - 0.5px), var(--gl-specular) calc(30% - 0.5px) calc(30% + 0.5px), transparent calc(30% + 0.5px));
  }
"""

# The overall badge: a 52px inverse disc, the mono number (an optional "/10"
# suffix), the 2px tier ring, and the tag on the deep plate beneath it.
# .badge-on-art pins it top-left over the art; .badge-low drops it to the
# bottom-left for art whose lettering sits at the top.
BADGE_CSS = r"""  .badge {
    position: relative;
    z-index: 2;
    width: 52px;
    height: 52px;
    flex: none;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: var(--gl-r-full);
    background: var(--gl-inverse-bg);
    color: var(--gl-inverse-text);
    box-shadow: 0 0 0 2px var(--gl-tier);
    font-family: var(--gl-mono);
    font-size: var(--gl-title);
    font-weight: var(--gl-heavy);
    line-height: 1;
    font-variant-numeric: tabular-nums;
  }
  .badge-on-art { position: absolute; top: 10px; left: 10px; }
  .badge-on-art .badge-tag { left: -4px; transform: none; }
  .badge-low { top: auto; bottom: 22px; }
  .badge-suffix {
    align-self: center;
    margin-left: 1px;
    padding-top: 4px;
    font-size: var(--gl-cap);
    font-weight: var(--gl-regular);
  }
  .badge-tag {
    position: absolute;
    top: calc(100% - 6px);
    left: 50%;
    transform: translateX(-50%);
    white-space: nowrap;
    padding: 0 6px;
    border-radius: var(--gl-r-xs);
    background: var(--gl-deep);
    color: var(--gl-deep-ink);
    font-family: var(--gl-font);
    font-size: var(--gl-cap);
    font-weight: var(--gl-strong);
    line-height: 18px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
"""

# The title plate under the art: the title left, the card number right
# ("No. 046" — the real game or assessment id, never invented), then the .sub
# line of separate gap-separated spans (studio, year, the platform .loz) —
# never middots or pipes. .sub is scoped to the plate: both widgets still
# carry their own .sub line until Phase 2 rebuilds them on the plate. .caps is
# the label style (12px / 600, uppercase, tracked) every run-in uses.
PLATE_CSS = r"""  .plate { position: relative; padding: 10px 12px 8px; border-bottom: 1px solid var(--gl-border); }
  .plate-row { display: flex; align-items: baseline; justify-content: space-between; gap: 6px; }
  .plate-row > :first-child { min-width: 0; margin: 0; }
  .plate-title {
    font-size: var(--gl-title);
    font-weight: var(--gl-heavy);
    line-height: var(--gl-title-lh);
    letter-spacing: -0.025em;
    overflow-wrap: anywhere;
  }
  .plate-title-s {
    font-size: var(--gl-h);
    font-weight: var(--gl-heavy);
    line-height: var(--gl-title-lh);
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    overflow: hidden;
  }
  .card-no {
    flex: none;
    font-family: var(--gl-mono);
    font-size: var(--gl-cap);
    color: var(--gl-muted);
    white-space: nowrap;
  }
  .plate .sub {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px 10px;
    margin-top: 4px;
    font-size: var(--gl-cap);
    line-height: 20px;
    color: var(--gl-muted);
  }
  .loz {
    display: inline-flex;
    align-items: center;
    height: 20px;
    padding: 0 6px;
    border: 1px solid var(--gl-border);
    border-radius: var(--gl-r-xs);
    background: var(--gl-inset);
    color: var(--gl-text-2);
    font-size: var(--gl-cap);
    font-weight: var(--gl-strong);
    line-height: 1;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    white-space: nowrap;
  }
  .caps {
    font-size: var(--gl-cap);
    font-weight: var(--gl-strong);
    line-height: var(--gl-cap-lh);
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
"""

# The stat block: label (with an optional note after it, "last 30d"), the
# dotted leader, the mono value; .is-key marks the strongest value. At most
# six rows on a card.
STATS_CSS = r"""  .stats { display: flex; flex-direction: column; margin: 0; padding: 6px 12px 0; }
  .stat { display: flex; align-items: baseline; min-height: 24px; line-height: 24px; }
  .stat-label {
    font-size: var(--gl-cap);
    font-weight: var(--gl-strong);
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--gl-muted);
    white-space: nowrap;
  }
  .stat-note {
    margin-left: 6px;
    font-size: var(--gl-cap);
    font-weight: var(--gl-regular);
    letter-spacing: 0;
    text-transform: none;
    color: var(--gl-muted);
    white-space: nowrap;
  }
  .stat-lead {
    flex: 1;
    min-width: 12px;
    margin: 0 8px;
    border-bottom: 1px dotted var(--gl-muted);
    opacity: 0.7;
    align-self: flex-end;
    transform: translateY(-7px);
  }
  .stat-val {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-family: var(--gl-mono);
    font-size: var(--gl-h);
    font-weight: var(--gl-regular);
    color: var(--gl-text-2);
    white-space: nowrap;
  }
  .stat.is-key .stat-label { color: var(--gl-text-2); }
  .stat.is-key .stat-val { color: var(--gl-text); font-weight: var(--gl-heavy); }
  .stat-val .chip, .stat-val .pips-word { font-family: var(--gl-font); }
"""

# Pips: 8px diamonds (a 45deg square, 4px between the tips), lit in the tier's
# text color, unlit in the hairline; .half is lit on its left.
PIPS_CSS = r"""  .pips { display: inline-flex; align-items: center; gap: 7px; height: 12px; padding: 0 2px; }
  .pips > span { width: 8px; height: 8px; flex: none; transform: rotate(45deg); background: var(--gl-border); }
  .pips > .on { background: var(--gl-tier-text); }
  .pips > .half { background: linear-gradient(45deg, var(--gl-tier-text) 50%, var(--gl-border) 50%); }
  .pips-word { font-size: var(--gl-body); font-weight: var(--gl-heavy); color: var(--gl-tier-text); }
"""

# The verdict / status band — the stamp's successor: 40px (slim 24px), the
# tier fill, ink always --gl-ribbon-ink, notched ends. .ribbon-straddle
# overhangs its card 20px each side (eval); .ribbon-art sits flush on the
# art's bottom edge (grid, detail). A second span is the mono note.
RIBBON_CSS = r"""  .ribbon {
    position: relative;
    z-index: 3;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 12px;
    height: 40px;
    padding: 0 18px;
    background: var(--gl-tier-fill);
    color: var(--gl-ribbon-ink);
    font-size: var(--gl-body);
    font-weight: var(--gl-heavy);
    letter-spacing: 0.1em;
    text-transform: uppercase;
    white-space: nowrap;
    clip-path: polygon(0 0, 100% 0, calc(100% - 10px) 50%, 100% 100%, 0 100%, 10px 50%);
    box-shadow: inset 0 1px 0 var(--gl-ribbon-hi), inset 0 -1px 0 var(--gl-ribbon-lo);
  }
  .ribbon.has-note { justify-content: space-between; gap: 10px; letter-spacing: 0.08em; }
  .ribbon-note {
    font-family: var(--gl-mono);
    font-size: var(--gl-cap);
    font-weight: var(--gl-heavy);
    letter-spacing: 0;
    text-transform: none;
  }
  .ribbon-s {
    height: 24px;
    padding: 0 16px;
    font-size: var(--gl-cap);
    letter-spacing: 0.08em;
    clip-path: polygon(0 0, 100% 0, calc(100% - 7px) 50%, 100% 100%, 0 100%, 7px 50%);
  }
  .ribbon-straddle {
    position: absolute;
    left: calc(-20px - var(--gl-frame));
    right: calc(-20px - var(--gl-frame));
    bottom: calc(-20px - var(--gl-frame));
  }
  .ribbon-art { position: absolute; left: 0; right: 0; bottom: 0; }
"""

# For-you / weakness rows: the 20px icon (a four-point spark, good; the shield
# outline, bad) and the text; .traits-head is the label in the tier color.
TRAIT_CSS = r"""  .traits { display: flex; flex-direction: column; }
  .traits-head {
    padding-bottom: 6px;
    font-size: var(--gl-cap);
    font-weight: var(--gl-strong);
    line-height: var(--gl-cap-lh);
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--gl-tier-text);
  }
  .trait {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    padding: 10px 0;
    border-top: 1px solid var(--gl-border);
    font-size: var(--gl-body);
    line-height: 20px;
    color: var(--gl-text);
  }
  .trait:last-child { border-bottom: 1px solid var(--gl-border); }
  .trait > svg {
    flex: none;
    width: 20px;
    height: 20px;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.6;
    stroke-linejoin: round;
  }
  .trait-plus > svg { color: var(--gl-good); }
  .trait-minus > svg { color: var(--gl-bad); }
"""

# The run-in: the kind as a label (STUDIO, PEOPLE, MOMENT, ANTICIPATION,
# AWARD), then body text on the same line.
ABILITY_CSS = r"""  .ability { margin: 0; font-size: var(--gl-body); line-height: 1.5; color: var(--gl-text-2); }
  .ability > b {
    margin-right: 6px;
    color: var(--gl-text);
    font-size: var(--gl-cap);
    font-weight: var(--gl-strong);
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
"""

# Flavor text: italic serif in the secondary color (craft note, description);
# .flavor-quote hangs a quotation mark for his own review.
FLAVOR_CSS = r"""  .flavor {
    margin: 0;
    font-family: var(--gl-serif);
    font-style: italic;
    font-size: var(--gl-body);
    line-height: 1.5;
    color: var(--gl-text-2);
  }
  .flavor-quote { position: relative; padding-left: 22px; }
  .flavor-quote::before {
    content: "\201C";
    position: absolute;
    left: 0;
    top: -2px;
    font-family: var(--gl-serif);
    font-style: normal;
    font-size: var(--gl-title);
    line-height: 1;
    color: var(--gl-muted);
  }
"""

# Mini cards for strips (similar, studio, anchors, lineage): 48x64 art in a
# 2px tier border, the name (2-line clamp), up to two cap lines of separate
# spans (a figure in mono). A tappable mini is one transparent button over the
# whole card, so the content stays plain flow. .ministrip rides on .strip
# (STRIP_CSS: padding, snap, the trailing spacer) with the Binder's 12px gap
# and peeking cards.
MINI_CSS = r"""  .mini { position: relative; display: flex; align-items: flex-start; gap: 10px; min-height: 64px; color: var(--gl-text); }
  .mini-art {
    flex: none;
    width: 48px;
    height: 64px;
    border: 2px solid var(--gl-tier);
    border-radius: var(--gl-r-sm);
    overflow: hidden;
    background: var(--gl-inset);
  }
  .mini-art > .cover-wrap { aspect-ratio: auto; width: 100%; height: 100%; }
  .mini-art .cover-fallback { color: transparent; text-shadow: none; }
  .mini-body { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .mini-name {
    font-size: var(--gl-body);
    font-weight: var(--gl-strong);
    line-height: 1.35;
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    overflow: hidden;
  }
  .mini-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0 8px;
    font-size: var(--gl-cap);
    line-height: var(--gl-cap-lh);
    color: var(--gl-muted);
  }
  .mini-meta .v { font-family: var(--gl-mono); color: var(--gl-text-2); }
  .mini a, .mini button {
    position: absolute;
    inset: 0;
    z-index: 1;
    padding: 0;
    border: 0;
    border-radius: var(--gl-r-sm);
    background: transparent;
    cursor: pointer;
    -webkit-tap-highlight-color: transparent;
  }
  .strip.ministrip { gap: 12px; }
  .ministrip > * { flex: 0 0 min(232px, 74%); }
"""

# Motion (docs/specs/assets/binder/MOTION.md, durations table). Compositor
# only (transform, opacity, clip-path, background-position). Every movement
# sits in the no-preference block; outside it are the opacity-only fallbacks
# a viewer who asked for reduced motion still gets — A11Y_CSS's wildcard stops
# every other animation, so the two crossfades re-assert themselves there.
#   M1 deal-in (card 300ms cubic-bezier(.2,0,0,1): fade, rise 12px, -1deg to
#      0; ribbon stamp +100/200 overshoot; badge pop +140/180; pips from +160,
#      36ms apart; leaders +80/200, 36ms stagger) — at rest by 500ms. M2 grid
#      cards (.frame-s) deal the body only, 40ms apart, capped at the 6th.
#   M3 press: scale .985, 90ms in / 160ms out; the specular shifts 12px; the
#      primary button darkens 6%.
#   M4 hover tilt + one 700ms sheen sweep, fine pointers only; the only
#      will-change.
#   M5 skeleton pulse 1600ms; leave 120ms ease-out; the result from +60ms
#      (resolveSkeleton). M6 the straddling ribbon settles 2px (+340/140).
#   M7 meters fill 300ms from the left, +120ms.
MOTION_CSS = r"""  .deal { animation: gl-fade-in 300ms ease-out backwards; animation-delay: var(--deal-at, 0ms); }
  .leaving { animation: gl-fade-out 120ms ease-out forwards; pointer-events: none; }
  @keyframes gl-fade-in { from { opacity: 0; } }
  @keyframes gl-fade-out { to { opacity: 0; } }
  button.frame:active, a.frame:active, .frame[role="button"]:active, .btn:active { opacity: 0.8; }
  .btn.primary { isolation: isolate; }
  .btn.primary::before {
    content: "";
    position: absolute;
    inset: 0;
    z-index: -1;
    border-radius: inherit;
    background: var(--gl-deep);
    opacity: 0;
  }
  .btn.primary:active::before { opacity: 0.06; }
  @media (prefers-reduced-motion: reduce) {
    .deal { animation: gl-fade-in 300ms ease-out backwards !important; }
    .leaving { animation: gl-fade-out 240ms ease-out forwards !important; }
  }
  @media (prefers-reduced-motion: no-preference) {
    .deal {
      animation: deal 300ms cubic-bezier(0.2, 0, 0, 1) backwards;
      animation-delay: calc(var(--deal-at, 0ms) + var(--i, 0) * 40ms);
    }
    .deal:not(.frame-s) .ribbon {
      transform-origin: 0 50%;
      animation: stamp 200ms cubic-bezier(0.34, 1.4, 0.64, 1) backwards;
      animation-delay: calc(var(--deal-at, 0ms) + 100ms);
    }
    .deal:not(.frame-s) .ribbon-straddle {
      animation: stamp 200ms cubic-bezier(0.34, 1.4, 0.64, 1) backwards, settle 140ms ease-out;
      animation-delay: calc(var(--deal-at, 0ms) + 100ms), calc(var(--deal-at, 0ms) + 340ms);
    }
    .deal:not(.frame-s) .badge {
      animation: pop 180ms ease-out backwards;
      animation-delay: calc(var(--deal-at, 0ms) + 140ms);
    }
    .deal:not(.frame-s) .pips > .on, .deal:not(.frame-s) .pips > .half {
      animation: pip 160ms ease-out backwards;
      animation-delay: calc(var(--deal-at, 0ms) + 160ms + var(--j, 0) * 36ms);
    }
    .deal:not(.frame-s) .stat-lead {
      animation: leader 200ms ease-out backwards;
      animation-delay: calc(var(--deal-at, 0ms) + 80ms + var(--j, 0) * 36ms);
    }
    .deal .meter-fill, .deal .match .fill {
      transform-origin: 0 50%;
      animation: fill 300ms ease-out backwards;
      animation-delay: calc(var(--deal-at, 0ms) + 120ms);
    }
    .stats > :nth-child(2), .pips > :nth-child(2) { --j: 1; }
    .stats > :nth-child(3), .pips > :nth-child(3) { --j: 2; }
    .stats > :nth-child(4), .pips > :nth-child(4) { --j: 3; }
    .stats > :nth-child(5), .pips > :nth-child(5) { --j: 4; }
    .stats > :nth-child(n+6), .pips > :nth-child(n+6) { --j: 5; }
    button.frame, a.frame, .frame[role="button"], .btn {
      transition: transform 160ms ease-out, opacity 160ms ease-out;
    }
    button.frame:active, a.frame:active, .frame[role="button"]:active, .btn:active {
      transform: scale(0.985);
      opacity: 1;
      transition-duration: 90ms;
    }
    .btn.primary::before { transition: opacity 160ms ease-out; }
    .btn.primary:active::before { transition-duration: 90ms; }
    button.frame .art::after, a.frame .art::after, .frame[role="button"] .art::after {
      transition: transform 160ms ease-out;
    }
    button.frame:active .art::after, a.frame:active .art::after, .frame[role="button"]:active .art::after {
      transform: translateX(-12px);
      transition-duration: 90ms;
    }
    @media (hover: hover) and (pointer: fine) {
      button.frame:hover, a.frame:hover, .frame[role="button"]:hover {
        transform: perspective(900px) rotateY(-5deg) rotateX(3deg);
        will-change: transform;
      }
      button.frame:hover:active, a.frame:hover:active, .frame[role="button"]:hover:active {
        transform: perspective(900px) rotateY(-5deg) rotateX(3deg) scale(0.985);
      }
      button.frame .art::after, a.frame .art::after, .frame[role="button"] .art::after {
        background:
          linear-gradient(235deg, transparent 42%, var(--gl-sheen) 50%, transparent 58%) no-repeat,
          linear-gradient(235deg, transparent calc(30% - 0.5px), var(--gl-specular) calc(30% - 0.5px) calc(30% + 0.5px), transparent calc(30% + 0.5px));
        background-size: 300% 100%, 100% 100%;
        background-position: 0% 0, 0 0;
      }
      button.frame:hover .art::after, a.frame:hover .art::after, .frame[role="button"]:hover .art::after {
        animation: sheen 700ms ease-out 1;
      }
    }
    @keyframes deal { from { opacity: 0; transform: translateY(12px) rotate(-1deg); } }
    @keyframes stamp { from { opacity: 0; transform: scaleX(0.6); } }
    @keyframes settle { from { transform: translateY(-2px); } to { transform: none; } }
    @keyframes pop { from { opacity: 0; transform: scale(0.6); } }
    @keyframes pip { from { opacity: 0; transform: rotate(45deg) scale(0.6); } }
    @keyframes leader { from { clip-path: inset(0 100% 0 0); } to { clip-path: inset(0); } }
    @keyframes fill { from { transform: scaleX(0); } }
    @keyframes skel-pulse { 50% { opacity: 0.55; } }
    @keyframes sheen { from { background-position: 0% 0, 0 0; } to { background-position: 100% 0, 0 0; } }
  }
"""

# All Binder CSS in splice order (one line per widget).
BINDER_CSS = (
    FRAME_CSS
    + ART_CSS
    + BADGE_CSS
    + PLATE_CSS
    + STATS_CSS
    + PIPS_CSS
    + RIBBON_CSS
    + TRAIT_CSS
    + ABILITY_CSS
    + FLAVOR_CSS
    + MINI_CSS
    + MOTION_CSS
)

# The Binder builders. Every node comes from el() (strings via textContent) or
# svgEl() (document.createElementNS; NOTICE_JS) — never markup.
GRAIN_JS = r"""  /* ---------- the Binder: builders ---------- */
  /* The card grain — gl.css's inline SVG, the FIRST child of every .frame.
     Identical filter ids across cards are harmless: it is the same filter. */
  function grainNode() {
    return svgEl("svg", { "class": "grain", "aria-hidden": "true" }, [
      svgEl("filter", { id: "gl-grain-f" }, [
        svgEl("feTurbulence", { type: "fractalNoise", baseFrequency: ".9", numOctaves: "2", stitchTiles: "stitch" }),
        svgEl("feColorMatrix", { type: "saturate", values: "0" }),
      ]),
      svgEl("rect", { width: "100%", height: "100%", filter: "url(#gl-grain-f)" }),
    ]);
  }
"""

BADGE_JS = r"""  /* {value, suffix, tag, tier} → the badge: the number, an optional suffix
     ("/10"), the tag beneath ("OpenCritic", "Match", "Your rating"; CSS
     uppercases it) and the ring in the tier color. No value, no badge. */
  function badgeNode(opts) {
    var o = opts || {};
    if (o.value === undefined || o.value === null || o.value === "") return null;
    var badge = el("div", "badge tier-" + (o.tier || "none"));
    badge.appendChild(el("span", "badge-num", String(o.value)));
    if (o.suffix) badge.appendChild(el("span", "badge-suffix", String(o.suffix)));
    if (o.tag) badge.appendChild(el("span", "badge-tag", String(o.tag)));
    return badge;
  }
"""

STATS_JS = r"""  /* {label, note, value, key} → one stat row: the label (its note after it,
     "last 30d"), the dotted leader, the value — text, or a node (a chip,
     pips). key marks the strongest value on the card. */
  function statRow(opts) {
    var o = opts || {};
    var row = el("div", "stat" + (o.key ? " is-key" : ""));
    var name = el("span", "stat-label", o.label);
    if (o.note) name.appendChild(el("span", "stat-note", String(o.note)));
    row.appendChild(name);
    var lead = el("span", "stat-lead");
    lead.setAttribute("aria-hidden", "true");
    row.appendChild(lead);
    var value = el("span", "stat-val");
    if (o.value && typeof o.value === "object" && o.value.nodeType) value.appendChild(o.value);
    else if (o.value !== undefined && o.value !== null) value.textContent = String(o.value);
    row.appendChild(value);
    return row;
  }
"""

PIPS_JS = r"""  /* `lit` of `of` diamonds in the tier's text color. lit rounds to the
     nearest half; a half pip is lit on its left. Read as "8.5 of 10". */
  function pipsNode(lit, of, tier) {
    var total = Math.max(0, Math.round(num(of) || 0));
    var n = Math.max(0, Math.min(total, Math.round((num(lit) || 0) * 2) / 2));
    var node = el("span", "pips tier-" + (tier || "none"));
    node.setAttribute("role", "img");
    node.setAttribute("aria-label", n + " of " + total);
    for (var i = 0; i < total; i++) {
      node.appendChild(el("span", i + 1 <= n ? "on" : i + 0.5 === n ? "half" : null));
    }
    return node;
  }
"""

RIBBON_JS = r"""  /* The verdict / status band: the text (CSS uppercases it), an optional
     mono note on the right ("wait for ~€40", "82h"), the tier fill. variant:
     "" (40px), "s" (24px), "straddle" (eval: overhangs its card), "art"
     (flush on the art's bottom edge) — or several, space-separated ("s art"). */
  var RIBBON_VARIANTS = ["s", "straddle", "art"];
  function ribbonNode(text, tier, note, variant) {
    var cls = "ribbon";
    String(variant || "").split(/\s+/).forEach(function (v) {
      if (RIBBON_VARIANTS.indexOf(v) >= 0) cls += " ribbon-" + v;
    });
    cls += " tier-" + (tier || "none");
    if (note) cls += " has-note";
    var node = el("div", cls);
    node.appendChild(el("span", "ribbon-text", text));
    if (note) node.appendChild(el("span", "ribbon-note", String(note)));
    return node;
  }
"""

TRAIT_JS = r"""  /* One for-you ("plus": the four-point spark) or weakness ("minus": the
     shield outline) row: the 20px icon, then the text. */
  var TRAIT_ICONS = {
    plus: "M10 2.5c.6 4.3 3.2 6.9 7.5 7.5-4.3.6-6.9 3.2-7.5 7.5-.6-4.3-3.2-6.9-7.5-7.5 4.3-.6 6.9-3.2 7.5-7.5Z",
    minus: "M10 2.2 16.5 4.7v5c0 4.2-2.8 7.1-6.5 8.6-3.7-1.5-6.5-4.4-6.5-8.6v-5L10 2.2Z",
  };
  function traitNode(kind, text) {
    var k = kind === "plus" ? "plus" : "minus";
    var node = el("div", "trait trait-" + k);
    var icon = iconNode("0 0 20 20", [["path", { d: TRAIT_ICONS[k] }]]);
    if (icon) node.appendChild(icon);
    node.appendChild(el("span", null, text));
    return node;
  }
"""

ABILITY_JS = r"""  /* A run-in: the kind label (STUDIO, PEOPLE, MOMENT, ANTICIPATION,
     AWARD), then the text on the same line. */
  function abilityNode(kindLabel, text) {
    var node = el("p", "ability");
    node.appendChild(el("b", null, kindLabel));
    node.appendChild(el("span", null, text));
    return node;
  }
"""

FLAVOR_JS = r"""  /* Italic serif prose (the craft note, a description); quote = his own
     review, with the hanging quotation mark. */
  function flavorNode(text, quote) {
    return el("p", "flavor" + (quote ? " flavor-quote" : ""), text);
  }
"""

MINI_JS = r"""  /* {name, cover_url, lines, tier, onClick} → a mini card: the 48x64 art
     (coverNode, so a missing or broken cover is the name-seeded plate) in a
     2px tier border, the name, at most two cap lines. A line is a list of
     parts, each text (a figure in mono) or a node (pips), gap-separated —
     never joined with middots. onClick makes the whole card one button,
     named by the game. */
  function miniCard(opts) {
    var o = opts || {};
    var card = el("div", "mini tier-" + (o.tier || "none"));
    var art = el("div", "mini-art");
    art.appendChild(coverNode({ name: o.name, cover_url: o.cover_url }));
    card.appendChild(art);
    var body = el("div", "mini-body");
    body.appendChild(el("div", "mini-name", o.name || "?"));
    list(o.lines).slice(0, 2).forEach(function (line) {
      var meta = el("div", "mini-meta");
      (Array.isArray(line) ? line : [line]).forEach(function (part) {
        if (part === null || part === undefined || part === "") return;
        if (typeof part === "object" && part.nodeType) { meta.appendChild(part); return; }
        var text = String(part);
        meta.appendChild(el("span", isFigure(text) ? "v" : null, text));
      });
      if (meta.childNodes.length) body.appendChild(meta);
    });
    card.appendChild(body);
    if (typeof o.onClick === "function") {
      var hit = el("button", "mini-hit");
      hit.type = "button";
      hit.setAttribute("aria-label", o.name || "Game");
      hit.addEventListener("click", function (ev) { o.onClick(ev); });
      card.appendChild(hit);
    }
    return card;
  }
"""

MOTION_JS = r"""  /* ---------- motion (MOTION.md; the CSS is MOTION_CSS) ---------- */
  /* M1/M2: deal a card in. index staggers siblings 40ms apart, capped at the
     6th (index 5) — later cards share its delay. */
  var DEAL_STAGGER_CAP = 5;
  function dealIn(node, index) {
    if (!node) return node;
    var i = Math.max(0, Math.min(DEAL_STAGGER_CAP, Math.floor(num(index) || 0)));
    node.style.setProperty("--i", String(i));
    node.classList.add("deal");
    return node;
  }
  /* M5: the skeleton resolves into the result. build() makes the node. With
     the skeleton on screen the node takes its place at once (the content is
     there for every reader immediately) while the skeleton, lifted out of the
     flow over its old box, fades out — 120ms, a 240ms crossfade under reduced
     motion — and the node deals in from +60ms (no offset under reduced
     motion): the overlap that reads as one object resolving. Without a
     skeleton the node is only dealt in and comes back detached for the
     caller to place. */
  var SKELETON_LEAVE_MS = 120;
  var SKELETON_LEAVE_REDUCED_MS = 240;
  var RESOLVE_OFFSET_MS = 60;
  function resolveSkeleton(skel, build) {
    var node = build();
    if (!node) return node;
    var parent = skel && skel.parentNode;
    if (parent) {
      var reduced = mediaQueryMatches("(prefers-reduced-motion: reduce)");
      var top = skel.offsetTop, left = skel.offsetLeft, width = skel.offsetWidth;
      parent.insertBefore(node, skel);
      skel.style.position = "absolute";
      skel.style.top = top + "px";
      skel.style.left = left + "px";
      skel.style.width = width + "px";
      skel.style.margin = "0";
      skel.setAttribute("aria-hidden", "true");
      skel.classList.add("leaving");
      if (!reduced) node.style.setProperty("--deal-at", RESOLVE_OFFSET_MS + "ms");
      setTimeout(function () {
        if (skel.parentNode) skel.parentNode.removeChild(skel);
        reportSize();
      }, reduced ? SKELETON_LEAVE_REDUCED_MS : SKELETON_LEAVE_MS);
    }
    return dealIn(node, 0);
  }
"""

# All Binder JS in splice order (one line per widget).
BINDER_JS = (
    GRAIN_JS
    + BADGE_JS
    + STATS_JS
    + PIPS_JS
    + RIBBON_JS
    + TRAIT_JS
    + ABILITY_JS
    + FLAVOR_JS
    + MINI_JS
    + MOTION_JS
)

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
    btn.type = "button";
    btn.setAttribute("aria-label", ariaText);
    btn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      onClick();
    });
    return btn;
  }
"""

# The screenshot lightbox's dialog chrome — the panel's dialog semantics, the
# ✕, the focus trap and the key routing — ONE implementation both widgets'
# ``openCarousel`` call (each keeps its own overlay slot and placement).
LIGHTBOX_CHROME_JS = r"""  /* Everything in the dialog Tab can reach, in document order. */
  var FOCUSABLE = 'button, [href], [tabindex]:not([tabindex="-1"])';
  function focusables(panel) {
    return Array.prototype.filter.call(panel.querySelectorAll(FOCUSABLE), function (n) {
      return !n.disabled && !n.hidden;
    });
  }
  /* Tab and Shift+Tab cycle through every focusable in the dialog, wrapping
     at both ends; from anywhere else (the panel itself, or outside) Tab
     lands on the first and Shift+Tab on the last. */
  function keepFocusInside(ev, panel) {
    var items = focusables(panel);
    if (!items.length) { ev.preventDefault(); panel.focus(); return; }
    var first = items[0];
    var last = items[items.length - 1];
    var inside = items.indexOf(document.activeElement) >= 0;
    if (ev.shiftKey && (!inside || document.activeElement === first)) {
      ev.preventDefault();
      last.focus();
    } else if (!ev.shiftKey && (!inside || document.activeElement === last)) {
      ev.preventDefault();
      first.focus();
    }
  }
  /* A document focusin listener for while the dialog is open: focus that
     lands outside it (a click on the page behind, a host-sent focus) is
     pulled back to the dialog's first focusable. */
  function focusGuard(panel) {
    return function (ev) {
      if (!ev.target || panel === ev.target || panel.contains(ev.target)) return;
      var first = focusables(panel)[0] || panel;
      first.focus({ preventScroll: true });
    };
  }
  /* A modal dialog panel holding its ✕ (a type=button, labelled). */
  function lightboxPanel(cls, gameName, onClose) {
    var panel = el("div", cls);
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    panel.setAttribute("aria-label", (gameName ? gameName + " " : "") + "screenshots");
    panel.tabIndex = -1;
    var closer = el("button", "overlay-close", "✕");
    closer.type = "button";
    closer.setAttribute("aria-label", "Close screenshots");
    closer.addEventListener("click", onClose);
    panel.appendChild(closer);
    return { panel: panel, closer: closer };
  }
  /* Escape closes, the arrow keys step through the set, Tab stays inside.
     Installed on document in the capture phase while the dialog is open. */
  function lightboxKeys(panel, step, onClose) {
    return function (ev) {
      if (ev.key === "Escape") { ev.preventDefault(); onClose(); }
      else if (ev.key === "ArrowLeft") { ev.preventDefault(); step(-1); }
      else if (ev.key === "ArrowRight") { ev.preventDefault(); step(1); }
      else if (ev.key === "Tab") keepFocusInside(ev, panel);
    };
  }
"""

# The lightbox stage: image, arrows, counter, wrapping ``show(i)`` and the
# pointer drag/swipe. Spliced INSIDE each widget's own ``openCarousel``,
# which owns the overlay lifecycle (one slot in each widget, placed its own
# way) — it closes over ``shots``, ``index``, ``panel`` and
# ``gameName`` there.
CAROUSEL_STAGE_JS = r"""    var stage = el("div", "car-stage");
    var img = document.createElement("img");
    img.className = "car-img";
    // A failed full-size image hides itself behind the neutral line instead
    // of painting a broken glyph and its alt text over the stage.
    var missing = el("div", "hero-missing", "Screenshot unavailable");
    missing.style.display = "none";
    img.onerror = function () {
      img.style.display = "none";
      missing.style.display = "";
    };
    stage.appendChild(img);
    stage.appendChild(missing);
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
      img.style.display = "";
      missing.style.display = "none";
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
  /* A poster that fails to load gives way to the neutral stage line —
     never a broken-image glyph with its alt text painted over the stage. */
  function posterNode(url, alt, missingText) {
    var img = document.createElement("img");
    img.className = "hero-media";
    img.alt = alt || "";
    img.onerror = function () {
      var missing = el("div", "hero-missing below-badge", missingText || "Trailer");
      if (img.parentNode) img.parentNode.replaceChild(missing, img);
    };
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
      hero.appendChild(posterNode(trailer.poster, trailer.name || "Trailer thumbnail",
        "Trailer unavailable here"));
    } else {
      hero.appendChild(el("div", "hero-missing below-badge", "Trailer unavailable here"));
    }
    var url = trailer.hq_url || trailer.url;
    var badge = playBadge("Open the trailer" + (trailer.name ? ": " + trailer.name : ""));
    badge.addEventListener("click", function () { openLink(url); });
    hero.appendChild(badge);
  }
  function youtubeHero(hero, trailer) {
    if (trailer.poster) {
      hero.appendChild(posterNode(trailer.poster, trailer.name || "Trailer thumbnail",
        trailer.name || "Trailer"));
    } else {
      hero.appendChild(el("div", "hero-missing below-badge", trailer.name || "Trailer"));
    }
    // Encoded once: the embed and the watch link carry the same id.
    var videoId = encodeURIComponent(trailer.video_id);
    var watchUrl = "https://www.youtube.com/watch?v=" + videoId;
    var badge = playBadge("Play trailer" + (trailer.name ? ": " + trailer.name : ""));
    badge.addEventListener("click", function () {
      // Lazy by design: nothing is fetched from YouTube until this click.
      var frame = document.createElement("iframe");
      frame.className = "hero-media";
      frame.src = "https://www.youtube-nocookie.com/embed/" + videoId;
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
    var fs = null;
    img.onerror = function () {
      var missing = el("div", "hero-missing", "Screenshot unavailable");
      if (img.parentNode) img.parentNode.replaceChild(missing, img);
      if (fs) fs.remove();                          // nothing left to enlarge
    };
    img.src = entry.shot.full || entry.shot.thumb;
    btn.appendChild(img);
    btn.addEventListener("click", function () {
      openCarousel(shots, entry.index, gameName, btn);
    });
    viewer.appendChild(btn);
    fs = fullscreenButton(img);
    if (fs) viewer.appendChild(fs);
  }
  /* A thumb image that fails shows the strip's neutral text tile instead
     (which carries its own ▶, so the overlay glyph goes with the image). */
  function thumbImage(btn, url, text) {
    var img = document.createElement("img");
    img.alt = "";
    img.loading = "lazy";
    img.onerror = function () {
      if (!img.parentNode) return;
      img.parentNode.replaceChild(el("span", "thumb-text", text), img);
      Array.prototype.slice.call(btn.childNodes).forEach(function (n) {
        if (n.className === "thumb-play") n.remove();
      });
    };
    img.src = url;
    btn.appendChild(img);
  }
  function thumbNode(entry, gameName) {
    var btn = el("button", "thumb");
    btn.type = "button";
    if (entry.kind === "shot") {
      btn.setAttribute("aria-label", "Show " + shotLabel(gameName, entry.index));
      thumbImage(btn, entry.shot.thumb || entry.shot.full, "Screenshot " + (entry.index + 1));
      return btn;
    }
    btn.setAttribute("aria-label", "Show the trailer");
    if (entry.trailer.poster) {
      thumbImage(btn, entry.trailer.poster, "▶ Trailer");
      btn.appendChild(el("span", "thumb-play", "▶"));
    } else {
      btn.appendChild(el("span", "thumb-text", "▶ Trailer"));
    }
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
    if (tornDown || window.__PREVIEW_DATA__ || !hooks.shouldReportSize()) return;
    clearTimeout(sizeTimer);
    sizeTimer = setTimeout(function () {
      sizeTimer = null;
      if (tornDown || !hooks.shouldReportSize()) return;
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
  /* The first skeleton is the widget's skeletonKind() with no tool input yet:
     the neutral panel for the game cards (which tool ran is still unknown),
     the evaluation card's own shape (it serves one tool). initialized goes
     out only after a real ui/initialize answer; on an error or a timeout the
     widget stays quiet and still renders whatever tool-result arrives. */
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
      if (!res || typeof res !== "object") return;
      hostCaps = res.hostCapabilities || {};
      applyHostContext(res.hostContext);
      notify("ui/notifications/initialized");
    });
  }
"""
