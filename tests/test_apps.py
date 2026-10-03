"""Tests for the MCP Apps game-cards widget and cover-art plumbing.

Also home of the shared design-system tests (the token layer, type scale,
bridge protocol and shared components in apps_shared.py): each one asserts
against BOTH widgets, since both splice the same blocks.
"""

import hashlib
import re
import unittest
from unittest.mock import AsyncMock, patch

from fastmcp import Client, FastMCP

from gamelib_mcp import apps, apps_eval, apps_shared
from gamelib_mcp.data import igdb
from gamelib_mcp.tools.common import cover_url


def shared_blocks() -> list[tuple[str, str]]:
    """The (name, text) pairs both widgets splice into their HTML."""
    return [
        (name, value)
        for name, value in sorted(vars(apps_shared).items())
        if name.isupper() and not name.startswith("_") and isinstance(value, str)
    ]


WIDGETS = (("game-cards", apps.GAME_CARDS_HTML), ("eval-card", apps_eval.EVAL_CARD_HTML))


def widget_css(html: str) -> str:
    """The document's own stylesheet."""
    return html.split("<style>", 1)[1].split("</style>", 1)[0]


def widget_js(html: str) -> str:
    """The document's own script."""
    return html.split("<script>", 1)[1].split("</script>", 1)[0]


class CoverUrlTests(unittest.TestCase):
    def test_igdb_slug_preferred(self) -> None:
        self.assertEqual(
            cover_url("co1wyy", 1145360),
            "https://images.igdb.com/igdb/image/upload/t_cover_big/co1wyy.jpg",
        )

    def test_steam_capsule_fallback(self) -> None:
        self.assertEqual(
            cover_url(None, 1145360),
            "https://cdn.cloudflare.steamstatic.com/steam/apps/1145360/library_600x900.jpg",
        )

    def test_no_sources(self) -> None:
        self.assertIsNone(cover_url(None, None))


class IGDBCoverParseTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_game_requests_and_parses_cover(self) -> None:
        async def fake_post(query: str, headers: dict[str, str]) -> list[dict]:
            self.assertIn("cover.image_id", query)
            return [
                {
                    "id": 113112,
                    "name": "Hades",
                    "category": igdb.CATEGORY_MAIN_GAME,
                    "cover": {"id": 89530, "image_id": "co39vc"},
                }
            ]

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "client"}, clear=True),
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb._post_igdb_games", new=fake_post),
        ):
            results = await igdb.search_game("Hades")

        self.assertEqual(results[0].cover_image_id, "co39vc")

    async def test_missing_or_malformed_cover_is_none(self) -> None:
        for cover in (None, 89530):  # absent, or a bare id with no expansion
            item = {"id": 1, "name": "X", "category": igdb.CATEGORY_MAIN_GAME}
            if cover is not None:
                item["cover"] = cover
            self.assertIsNone(igdb._parse_igdb_item(item).cover_image_id)


class GameCardsResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_resource_registered_and_serves_widget(self) -> None:
        mcp = FastMCP("test")
        apps.register_apps(mcp)
        async with Client(mcp) as client:
            resources = await client.list_resources()
            uris = [str(r.uri) for r in resources]
            self.assertIn(apps.GAME_CARDS_URI, uris)
            content = await client.read_resource(apps.GAME_CARDS_URI)
            html = content[0].text

        # The hand-rolled bridge must speak the MCP Apps handshake and the
        # result notification, and handle preview injection for local review.
        for marker in (
            "ui/initialize",
            "appInfo",              # required by the ext-apps SDK schema
            "ui/notifications/initialized",
            "ui/notifications/tool-result",
            "ui/notifications/size-changed",
            "tools/call",           # a card tap drills into get_game_detail
            "get_game_detail",
            "ui/open-link",         # rating pills link out via the host
            "__PREVIEW_DATA__",
        ):
            self.assertIn(marker, html)

    def test_score_chips_encode_quality_tier_not_brand(self) -> None:
        # Replaces the old brand-palette pin (spec 2026-10-03 §1.3): color is
        # the quality tier only, the source is the label text.
        # (a) none of the brand hexes survive, in either widget;
        for name, html in WIDGETS:
            lowered = html.lower()
            for hex_color in (
                "#fc430a", "#9e00b4", "#4aa1ce", "#80b06a",  # OpenCritic tiers
                "#66c0f4", "#b9a074", "#c85e2d",             # Steam summary text
                "#6c3;", "#fc3;", "#f00;",                   # Metacritic metascore
            ):
                with self.subTest(widget=name, color=hex_color):
                    self.assertNotIn(hex_color, lowered)
        # (b) the three tier classes map to the --gl-good/ok/bad tokens, which
        # are the host's success/warning/danger tokens;
        for tier in ("good", "ok", "bad"):
            self.assertIn(
                f".chip.tier-{tier} {{ background: var(--gl-{tier}-bg); "
                f"border-color: var(--gl-{tier}-edge); color: var(--gl-{tier}); }}",
                apps_shared.CHIP_CSS,
            )
        for token, host in (("good", "success"), ("ok", "warning"), ("bad", "danger")):
            self.assertIn(f"--gl-{token}: var(--color-text-{host},", apps_shared.TOKENS_CSS)
        # (c) every chip has a text label: scoreChip always writes the label
        # first, and every call site in both widgets passes one.
        self.assertIn('chip.appendChild(el("span", "lbl", opts.label));', apps_shared.SCORE_CHIP_JS)
        for name, html in WIDGETS:
            js = widget_js(html)
            calls = len(re.findall(r"scoreChip\(\{", js))
            labelled = len(re.findall(r"scoreChip\(\{\s*label: ", js))
            with self.subTest(widget=name):
                self.assertGreater(calls, 0)
                self.assertEqual(calls, labelled)

    def test_steam_chip_never_shows_the_meter_without_the_phrase(self) -> None:
        start = apps_shared.SCORE_CHIP_JS.index("function steamChip(desc, url)")
        body = apps_shared.SCORE_CHIP_JS[start:]
        self.assertIn("if (!desc) return null;", body)
        self.assertIn('label: "Steam", value: phrase, tier: steamTier(desc),', body)
        # The detail card renders Steam through it (phrase + meter); the grid
        # renders the phrase without the meter (GridModeTests pins that rule).
        self.assertIn("steamChip(game.steam_review_desc,", apps.GAME_CARDS_HTML)
        self.assertIn("gridSteamChip(game.steam_review_desc)", apps.GAME_CARDS_HTML)
        self.assertNotIn("steamBadge", apps.GAME_CARDS_HTML)

    def test_csp_allows_exactly_the_cover_and_media_hosts(self) -> None:
        # Covers + the media hosts a get_game_detail(media=True) card needs:
        # Steam serves screenshots and movie posters from shared.*, the mp4
        # renditions from cdn.*, and IGDB trailers are YouTube thumbnails.
        self.assertEqual(
            apps._GAME_CARDS_CSP.resource_domains,
            [
                "https://images.igdb.com",
                "https://cdn.cloudflare.steamstatic.com",
                "https://cdn.akamai.steamstatic.com",
                "https://shared.akamai.steamstatic.com",
                "https://shared.cloudflare.steamstatic.com",
                "https://i.ytimg.com",
                "https://assets.claude.ai",   # the host's own font files
            ],
        )

    async def test_both_resources_prefer_no_host_border(self) -> None:
        # The widgets paint their own panels on a transparent page; a host
        # frame around them would double every border.
        mcp = FastMCP("test")
        apps.register_apps(mcp)
        apps_eval.register_eval_app(mcp)
        async with Client(mcp) as client:
            resources = {str(r.uri): r for r in await client.list_resources()}
        for uri in (apps.GAME_CARDS_URI, apps_eval.EVAL_CARD_URI):
            with self.subTest(uri=uri):
                self.assertIs(resources[uri].meta["ui"]["prefersBorder"], False)

    def test_csp_frames_only_the_privacy_mode_youtube_host(self) -> None:
        # frame_domains feeds frame-src; the trailer embed is the only nested
        # frame the widget ever creates, and only after a click.
        self.assertEqual(
            apps._GAME_CARDS_CSP.frame_domains,
            ["https://www.youtube-nocookie.com"],
        )


class ContentTypeBadgeTests(unittest.TestCase):
    """The DLC/expansion/edition badge + "part of <base game>" subtitle.

    apps.py has no headless-DOM test harness (see GameCardsResourceTests
    above), so these follow the same source-presence style: they assert the
    label map and render call-sites exist with the right shape rather than
    executing the JS.
    """

    def test_nested_content_types_have_human_labels(self) -> None:
        # Nested types (data/content.py::NESTED_CONTENT_TYPES) each map to a
        # short human label used for both the grid chip and the detail badge.
        for key, label in (
            ("dlc", "DLC"),
            ("expansion", "Expansion"),
            ("bundle", "Bundle"),
            ("edition", "Edition"),
            ("unknown_addon", "Add-on"),
        ):
            self.assertIn(f'{key}: "{label}"', apps.GAME_CARDS_HTML)

    def test_primary_content_types_are_not_in_the_label_map(self) -> None:
        # Primary types (data/content.py::PRIMARY_CONTENT_TYPES) must render
        # no badge at all — contentTypeLabel() falls back to null for any key
        # not in CONTENT_TYPE_LABELS, so the map must never mention them.
        start = apps.GAME_CARDS_HTML.index("var CONTENT_TYPE_LABELS")
        end = apps.GAME_CARDS_HTML.index("};", start)
        label_map_src = apps.GAME_CARDS_HTML[start:end]
        for primary_type in (
            "base_game",
            "standalone_expansion",
            "remake",
            "remaster",
            "expanded_game",
            "port",
        ):
            self.assertNotIn(primary_type, label_map_src)

    def test_grid_card_renders_type_chip_and_parent_subtitle(self) -> None:
        self.assertIn('el("span", "type-chip", typeLabel)', apps.GAME_CARDS_HTML)
        self.assertIn('el("div", "parent-sub", "⤷ " + pName)', apps.GAME_CARDS_HTML)

    def test_detail_card_renders_content_badge_and_parent_subtitle(self) -> None:
        self.assertIn(
            'el("span", "chip content-badge", typeLabel)', apps.GAME_CARDS_HTML
        )
        self.assertIn('el("div", "sub parent-sub", "part of " + pName)', apps.GAME_CARDS_HTML)

    def test_detail_cover_plate_drops_the_duplicate_title(self) -> None:
        # The h1 sits right beside the plate on the detail card, so stamping
        # the name onto the art stand-in reads as a doubled title. The gradient
        # stays; grid cards (no heading beside the cover) keep their lettering.
        self.assertIn(
            ".detail .cover-fallback { color: transparent; text-shadow: none; }",
            apps.GAME_CARDS_HTML,
        )

    def test_detail_card_has_an_empty_state_with_the_skip_reasons(self) -> None:
        # A never-enriched row (an assessment-minted candidate) fills nothing
        # but the title; say so through the shared notice, and relay
        # get_game_detail's `enrichment` {provider: reason} map in words
        # ("IGDB: no match", "Steam: no app id"), never as raw ids.
        for marker in (
            'var emptyText = "No details fetched yet";',
            "var reasons = enrichmentReasons(game.enrichment);",
            'return source + ": " + reason;',
            'emptyText += " — " + reasons.join(" · ");',
            "notice(body, emptyText);",
            'var ENRICH_SOURCES = { steam_store: "Steam", protondb: "ProtonDB", igdb: "IGDB" };',
            'no_steam_appid: "no app id",',
            'no_match: "no match",',
        ):
            self.assertIn(marker, apps.GAME_CARDS_HTML)
        self.assertNotIn("empty-state", apps.GAME_CARDS_HTML)
        self.assertNotIn('parts.push(k + ": " + why[k]);', apps.GAME_CARDS_HTML)

    def test_parent_name_supports_both_grid_and_detail_shapes(self) -> None:
        # Grid/search rows carry a flat parent_name; get_game_detail carries
        # parent: {game_id, name} on nested rows. parentName() must read both.
        self.assertIn("game.parent && game.parent.name", apps.GAME_CARDS_HTML)
        self.assertIn("game.parent_name", apps.GAME_CARDS_HTML)

    def test_badge_and_subtitle_text_use_textcontent_not_innerhtml(self) -> None:
        # The widget's only escaping mechanism is el()'s use of textContent
        # (never innerHTML with payload data) — verify the new badge/subtitle
        # strings go through that same helper rather than string concatenation
        # into markup.
        self.assertNotIn("innerHTML", apps.GAME_CARDS_HTML)

    def test_media_sections_render_from_the_detail_payload(self) -> None:
        # The detail card grows a media panel (one viewer + one thumb strip)
        # and a "similar in your library" row when get_game_detail(media=True)
        # supplies them. Source-presence style, like the badge tests above —
        # there is no headless-DOM harness.
        for marker in (
            'var media = game.media || {};',      # detailCard reads the block
            'stack.appendChild(identityPanel(game, media));',
            'mediaNode(stack, media, game.name)',
            'relatedBlock(stack, game);',
            'section(parent, "Media")',
            'var viewer = el("div", "hero viewer")',
            'el("div", "strip thumbs")',
            'btn.classList.toggle("sel", i === j)',
            'select(0);',                         # trailer first when there is one
            'if (similar) similarNode(parent, game.similar);',
            'el("div", "sim-name", item.name || "?")',
            'section(parent, "Similar in your library")',
            '"Your " + plural(items.length, "game") + " most like this one"',
            '"The " + items.length + " of your " + plural(total, "game") + " most like this one"',
            'note += " · " + unplayed + " unplayed"',
        ):
            self.assertIn(marker, apps.GAME_CARDS_HTML)

    def test_similar_cards_carry_no_truncated_tag_line(self) -> None:
        # Spec §1.5: a mini card is cover, name, year and ONE chip row. The
        # 10px one-line "why" (shared tags, ellipsized) is gone; the shared
        # tags survive only as the card's tooltip.
        for name, html in WIDGETS:
            with self.subTest(widget=name):
                self.assertNotIn("sim-why", html)
                self.assertIn('card.title = "Shares: " + why.join(", ");', html)
        start = apps_shared.SIMILAR_NODE_JS.index("function similarTags(item)")
        end = apps_shared.SIMILAR_NODE_JS.index("function similarNode(", start)
        tags = apps_shared.SIMILAR_NODE_JS[start:end]
        # rating-or-unplayed, then hours: at most two stickers.
        self.assertEqual(tags.count("tags.appendChild("), 3)
        self.assertIn("else if (item.unplayed)", tags)

    def test_the_dead_more_chip_is_gone_from_the_media_block(self) -> None:
        # Ported from the evaluation card: "+N more" was unclickable, because
        # the extra images are not in the payload. Neither the flag nor the
        # count is read any more.
        self.assertNotIn("media.screenshots_truncated", apps.GAME_CARDS_HTML)
        self.assertNotIn("media.screenshot_count", apps.GAME_CARDS_HTML)

    def test_fullscreen_is_attempted_but_never_faked(self) -> None:
        # A sandboxed host iframe without allow="fullscreen" reports
        # fullscreenEnabled false (no button at all), and a denied request
        # removes the button rather than leaving an inert control behind.
        self.assertIn(
            "if (!document.fullscreenEnabled && !document.webkitFullscreenEnabled) return null;",
            apps.GAME_CARDS_HTML,
        )
        self.assertIn("pending.catch(function () { btn.remove(); });", apps.GAME_CARDS_HTML)

    def test_counts_are_pluralized(self) -> None:
        # Hand-ported with the evaluation card's fix: "1 games" shipped once.
        self.assertIn(
            'return n + (truncated ? "+" : "") + " " + word '
            '+ (n === 1 && !truncated ? "" : "s");',
            apps.GAME_CARDS_HTML,
        )
        self.assertIn('plural(size, "game", ped.catalog_truncated)', apps.GAME_CARDS_HTML)

    def test_pedigree_strip_renders_from_the_detail_payload(self) -> None:
        # "From the studio": header line, optional publisher line, the poster
        # row, and the track-record footer. Source-presence style, like the
        # media markers above.
        for marker in (
            "if (studio) pedigreeNode(parent, game.pedigree);",
            'section(parent, "From the studio")',
            'el("div", "ped-head", headline)',
            'el("div", "ped-pub", "published by " + ped.publisher_name)',
            '"You\'ve played " + (num(record.played_count) || 0) + " of "',
            '" — avg " + avg + "/10."',
        ):
            self.assertIn(marker, apps.GAME_CARDS_HTML)

    def test_pedigree_badge_prefers_his_rating_over_the_critic_score(self) -> None:
        # One badge per poster: his own rating wins the slot, the critic score
        # only stands in when he hasn't rated it, and an owned-but-unrated game
        # still gets its ownership sticker.
        start = apps.GAME_CARDS_HTML.index("function pedigreeBadges(item)")
        end = apps.GAME_CARDS_HTML.index("function pedigreeNode(", start)
        badges = apps.GAME_CARDS_HTML[start:end]
        self.assertIn('if (item.owned && rating != null) {', badges)
        self.assertIn('el("span", "tag rated", rating + "/10")', badges)
        self.assertIn('} else if (critic != null && critic >= 0) {', badges)
        self.assertIn('if (item.owned && rating == null) tags.appendChild', badges)

    def test_the_damper_branch_renders_the_header_line_alone(self) -> None:
        # previous_games is empty under the big-studio damper: the header (and
        # the publisher line) render, and the function returns before the strip.
        start = apps.GAME_CARDS_HTML.index("function pedigreeNode(parent, ped)")
        end = apps.GAME_CARDS_HTML.index('var strip = el("div", "strip ped-strip")', start)
        head = apps.GAME_CARDS_HTML[start:end]
        self.assertIn("if (!items.length) return;", head)

    def test_hype_counts_are_never_rendered(self) -> None:
        # `hypes` rides in the payload for completeness; this card does not
        # argue from popularity, so no renderer may read it.
        self.assertNotIn("hypes", apps.GAME_CARDS_HTML)

    def test_the_trailer_leads_the_reel_and_never_autoplays(self) -> None:
        # The trailer is thumb one when there is one (screenshots alone still
        # make a viewer), and the mp4 fetches zero bytes until the viewer hits
        # play.
        self.assertIn(
            'if (trailer.kind === "mp4" && trailer.url) return { kind: "mp4", trailer: trailer };',
            apps.GAME_CARDS_HTML,
        )
        self.assertIn("if (trailer) entries.push(trailer);", apps.GAME_CARDS_HTML)
        self.assertIn('video.preload = "none";', apps.GAME_CARDS_HTML)
        # …and the YouTube branch loads nothing until the click either.
        self.assertIn(
            'frame.src = "https://www.youtube-nocookie.com/embed/"',
            apps.GAME_CARDS_HTML,
        )

    def test_screenshot_lightbox_is_fixed_closable_and_keyboard_reachable(self) -> None:
        # The detail card's own media viewer survives the overlay removal: it
        # is a media viewer, not navigation. One slot, position: fixed over
        # the iframe, closed by the ✕, Escape or a backdrop click, with Tab
        # kept inside it and focus handed back to the stage that opened it.
        self.assertIn(
            "openCarousel(shots, entry.index, gameName, btn)", apps.GAME_CARDS_HTML
        )
        css = widget_css(apps.GAME_CARDS_HTML)
        self.assertIn(".overlay.lightbox { position: fixed; inset: 0; }", css)
        for marker in (
            'var overlay = el("div", "overlay lightbox");',
            'panel.setAttribute("role", "dialog");',
            'panel.setAttribute("aria-modal", "true");',
            'closer.addEventListener("click", closeLightbox);',
            "if (ev.target === overlay) closeLightbox();",
            'if (ev.key === "Escape") { ev.preventDefault(); closeLightbox(); }',
            'else if (ev.key === "Tab") keepFocusInside(ev, panel);',
            "closer.focus({ preventScroll: true });",
            "current.trigger.focus({ preventScroll: true });",
        ):
            self.assertIn(marker, apps.GAME_CARDS_HTML)
        for marker in (
            'navButton("car-prev", "‹"',
            'navButton("car-next", "›"',
            'counter.textContent = (index + 1) + " / " + shots.length;',
            'if (ev.key === "ArrowLeft") show(index - 1);',
            'stage.addEventListener("pointerup"',
        ):
            self.assertIn(marker, apps.GAME_CARDS_HTML)

    def test_rank_badge_is_a_quiet_hash_number(self) -> None:
        # "#1", not "№ 01": 12px caption weight, muted on the surface, no tilt.
        css = widget_css(apps.GAME_CARDS_HTML)
        self.assertIn('content: "#" counter(rank);', css)
        self.assertNotIn("№", apps.GAME_CARDS_HTML)
        start = css.index(".grid.ranked .cover-wrap::before {")
        rule = css[start:css.index("}", start)]
        for decl in (
            "font-size: var(--gl-cap);",
            "font-weight: var(--gl-strong);",
            "background: var(--gl-surface);",
            "color: var(--gl-muted);",
        ):
            self.assertIn(decl, rule)
        self.assertNotIn("rotate", rule)

    def test_platform_ids_and_hours_render_through_the_shared_helpers(self) -> None:
        for marker in (
            'label("platform", game.suggested_platform)',
            'label("platform", p.platform)',
            "hoursLabel(game.hltb_main, true)",
            'label("tier", game.protondb_tier)',
        ):
            self.assertIn(marker, apps.GAME_CARDS_HTML)
        # No local copies left to drift from apps_shared.NUMBERS_JS.
        self.assertEqual(apps.GAME_CARDS_HTML.count("function hoursLabel("), 1)
        self.assertNotIn('game.playtime_hours + "h played"', apps.GAME_CARDS_HTML)

    def test_drill_in_detail_call_requests_media(self) -> None:
        self.assertIn(
            # 30s, not callTool's 15s default: a cold click-through runs full
            # enrichment plus the media lookup's own 8s budget server-side.
            'callTool("get_game_detail", { game_id: game.game_id, media: true }, 30000)',
            apps.GAME_CARDS_HTML,
        )

    def test_widget_stays_dependency_free(self) -> None:
        # No CDN, no external script, and still no innerHTML anywhere — the
        # media sections build every node through el()/createElement.
        self.assertNotIn("<script src", apps.GAME_CARDS_HTML)
        self.assertNotIn("innerHTML", apps.GAME_CARDS_HTML)

    def test_uri_is_content_hashed_and_reflects_current_html(self) -> None:
        expected = (
            "ui://gamelib/game-cards-"
            + hashlib.sha1(apps.GAME_CARDS_HTML.encode()).hexdigest()[:8]
            + ".html"
        )
        self.assertEqual(apps.GAME_CARDS_URI, expected)


def js_function(html: str, signature: str) -> str:
    """The source of one widget function, from its signature to the next one."""
    start = html.index(signature)
    end = html.find("\n  function ", start + len(signature))
    return html[start:end if end > 0 else len(html)]


class GridModeTests(unittest.TestCase):
    """Spec 2026-10-03 §2.1.1–§2.1.4: header line, card body, footer, card tap."""

    HTML = apps.GAME_CARDS_HTML

    def test_the_overlay_drill_in_is_gone(self) -> None:
        # A card tap hands back to the conversation; no in-widget overlay.
        for gone in (
            "function openOverlay(",
            "function openDetail(",
            "overlay-panel",
            "var overlays = []",
            "__PREVIEW_OPEN_INDEX__",
            "loading-note",
            '"pill match"',
        ):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, self.HTML)

    def test_header_line_reads_the_tool_input(self) -> None:
        head = js_function(self.HTML, "function headerParts(data)")
        for marker in (
            'var SORT_LABELS = { match: "taste match", critic: "critic score", value: "value" };',
            '? shown + " of " + plural(total, "game")',          # "12 of 143 games"
            ': plural(shown, "game");',
            'parts.push("sorted by " + (SORT_LABELS[args.sort_by] || SORT_LABELS.match));',
            'parts.push(vibes.join(" + "));',
            'if (args.unplayed_only !== false) parts.push("unplayed only");',
            'parts.push("≤ " + maxHours + "h");',
            'parts.push("critics ≥ " + minScore);',
        ):
            self.assertIn(marker, self.HTML if marker.startswith("var SORT") else head)
        # get_game_detail's arguments never produce discover filters.
        self.assertIn("if (args && !isDetailArgs(args)) {", head)
        # The count is the emphasised part; the rest is the muted caption.
        self.assertIn('head.appendChild(el("b", null, parts.count));', self.HTML)
        css = widget_css(self.HTML)
        self.assertIn(".grid-head b { color: var(--gl-text); font-weight: var(--gl-strong); }", css)
        # Tool input arriving after the result redraws the grid.
        self.assertIn("handleToolInput = function (params) {", self.HTML)
        self.assertIn("if (gotResult && view === \"grid\" && gridData) render(gridData);", self.HTML)

    def test_match_bar_leads_the_card_body(self) -> None:
        card = js_function(self.HTML, "function gridCard(game)")
        self.assertIn("body.appendChild(matchBar(game.match_percent));", card)
        self.assertNotIn('"% match"', card)
        order = [
            'body.appendChild(el("div", "title", game.name));',
            "body.appendChild(matchBar(game.match_percent));",
            'body.appendChild(el("div", "meta", metaBits.join(" · ")));',
            "body.appendChild(scores);",
            'body.appendChild(el("div", "tagline", why.join(" · ")));',
        ]
        positions = [card.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))
        # Matched tags are one muted text line, at most three, no pills.
        self.assertIn("var why = matchedTagNames(game).slice(0, 3);", card)
        self.assertNotIn('"pill"', self.HTML)
        self.assertNotIn(".pill {", widget_css(self.HTML))

    def test_cover_carries_only_rank_metacritic_and_type_chip(self) -> None:
        card = js_function(self.HTML, "function gridCard(game)")
        self.assertEqual(card.count("cover.appendChild("), 2)
        self.assertIn('cls: "corner",', card)
        self.assertIn('if (typeLabel) cover.appendChild(el("span", "type-chip", typeLabel));', card)
        self.assertIn(".cover-wrap .type-chip { position: absolute; left: 6px; bottom: 6px;",
                      widget_css(self.HTML))

    def test_grid_steam_chip_is_phrase_only_on_a_line_of_its_own(self) -> None:
        chip = js_function(self.HTML, "function gridSteamChip(desc)")
        self.assertIn('label: "Steam", value: phrase, tier: steamTier(desc),', chip)
        self.assertNotIn("meter", chip)                     # the meter stays on the detail card
        self.assertIn('cls: words >= 2 ? "steam-line" : "",', chip)
        css = widget_css(self.HTML)
        self.assertIn(".card .chip.steam-line { flex-basis: 100%; display: block; }", css)

    def test_show_next_button_only_when_has_more(self) -> None:
        actions = js_function(self.HTML, "function gridActions(data)")
        for marker in (
            "if (data.has_more && shown) {",
            "var limit = num(args.limit) || shown;",
            'el("button", "btn act-more", "Show next " + count)',
            'sendMessage("Show the next " + count + " recommendations (offset " + next + ")");',
            "if (canFullscreen()) {",
            'el("button", "btn act-expand", "Expand")',
            'requestDisplayMode("fullscreen");',
        ):
            self.assertIn(marker, actions)
        # At most two actions: exactly these two buttons are ever built.
        self.assertEqual(actions.count('el("button"'), 2)
        css = widget_css(self.HTML)
        self.assertIn('html[data-display-mode="fullscreen"] .act-expand { display: none; }', css)
        self.assertIn("justify-content: flex-end;", css)
        self.assertIn("@media (max-width: 419px) {\n    .actions { flex-direction: column; }\n"
                      "    .actions .btn { width: 100%; }", css)

    def test_fullscreen_grid_widens_and_sticks_the_header(self) -> None:
        css = widget_css(self.HTML)
        self.assertIn(
            'html[data-display-mode="fullscreen"] .grid { grid-template-columns: '
            "repeat(auto-fill, minmax(160px, 1fr)); }",
            css,
        )
        start = css.index('html[data-display-mode="fullscreen"] .grid-head, .topbar {')
        rule = css[start:css.index("}", start)]
        self.assertIn("position: sticky;", rule)
        self.assertIn("background: var(--gl-surface);", rule)

    def test_card_tap_updates_model_context_then_fullscreen_or_chat(self) -> None:
        tap = js_function(self.HTML, "function selectGame(game)")
        self.assertIn(
            '"User selected " + game.name + " (game_id " + game.game_id + ") from the recommendations",',
            tap,
        )
        self.assertIn("{ game_id: game.game_id, name: game.name });", tap)
        # The model hears about it before anything else happens.
        self.assertLess(tap.index("updateModelContext("), tap.index("canFullscreen()"))
        self.assertIn('if (!canFullscreen()) {\n      sendMessage("Show me " + game.name);', tap)
        self.assertIn('requestDisplayMode("fullscreen").then(function (mode) {', tap)
        self.assertIn('if (mode === "fullscreen") openDrill(game, before);', tap)
        self.assertIn('else sendMessage("Show me " + game.name);', tap)

    def test_the_drill_in_shows_a_skeleton_then_the_live_detail(self) -> None:
        drill = js_function(self.HTML, "function openDrill(game, before)")
        for marker in (
            'el("button", "btn", "← Back to results")',
            'back.addEventListener("click", backToResults);',
            'el("div", "topbar-title", game.name)',
            'holder.appendChild(skeleton("detail"));',
            'callTool("get_game_detail", { game_id: game.game_id, media: true }, 30000)',
            "if (seq !== drillSeq) return;",
            "else fill(game, true);",
        ):
            self.assertIn(marker, drill)
        back = js_function(self.HTML, "function backToResults()")
        self.assertIn("if (gridData) render(gridData);", back)
        self.assertIn('if (restore !== "fullscreen") requestDisplayMode("inline");', back)
        # The host's own close button ends the drill-in too.
        self.assertIn(
            'if (view === "drill" && currentDisplayMode() !== "fullscreen" && gridData) render(gridData);',
            self.HTML,
        )


class DetailModeTests(unittest.TestCase):
    """Spec 2026-10-03 §2.1.5–§2.1.6: identity panel, disclosure, carousel rows."""

    HTML = apps.GAME_CARDS_HTML

    def test_identity_panel_leads_the_stack(self) -> None:
        card = js_function(self.HTML, "function detailCard(game)")
        order = ["identityPanel(game, media)", "mediaNode(stack, media, game.name)",
                 "relatedBlock(stack, game)"]
        positions = [card.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))

    def test_identity_panel_cover_widths(self) -> None:
        css = widget_css(self.HTML)
        self.assertIn("grid-template-columns: 120px minmax(0, 1fr);", css)
        self.assertIn("@media (max-width: 559px) {\n    .detail {\n"
                      "      grid-template-columns: 84px minmax(0, 1fr);", css)

    def test_your_rating_is_a_tiered_chip(self) -> None:
        panel = js_function(self.HTML, "function identityPanel(game, media)")
        self.assertIn('label: "Your rating", value: mine + "/10",', panel)
        self.assertIn('tier: mine >= 7 ? "good" : mine >= 5 ? "ok" : "bad",', panel)
        self.assertNotIn("My rating: ", self.HTML)

    def test_description_clamps_to_three_lines_with_a_more_toggle(self) -> None:
        css = widget_css(self.HTML)
        start = css.index("  .desc {")
        self.assertIn("-webkit-line-clamp: 3;", css[start:css.index("}", start)])
        panel = js_function(self.HTML, "function identityPanel(game, media)")
        for marker in (
            'var more = el("button", "more-toggle", "More");',
            'more.textContent = open ? "Less" : "More";',
            'more.setAttribute("aria-expanded", open ? "true" : "false");',
            "if (desc.scrollHeight > desc.clientHeight + 1) {",
        ):
            self.assertIn(marker, panel)

    def test_tags_are_one_muted_line_of_at_most_eight(self) -> None:
        panel = js_function(self.HTML, "function identityPanel(game, media)")
        self.assertIn("var tags = (game.tags || []).filter(Boolean).slice(0, 8);", panel)
        self.assertIn('body.appendChild(el("div", "tagline", tags.join(" · ")));', panel)

    def test_similar_and_studio_sit_behind_fullscreen_or_a_disclosure(self) -> None:
        block = js_function(self.HTML, "function relatedBlock(stack, game)")
        for marker in (
            '[similar ? "Similar games you own" : null, studio ? "From the studio" : null]',
            'if (currentDisplayMode() === "fullscreen") { build(stack); return; }',
            "if (!canFullscreen()) { disclosure(stack, text, build); return; }",
            'requestDisplayMode("fullscreen").then(function (mode) {',
            'if (mode === "fullscreen") build(wrap);',
            "else disclosure(wrap, text, build).button.click();",
        ):
            self.assertIn(marker, block)

    def test_carousel_rows_snap_and_peek(self) -> None:
        css = widget_css(self.HTML)
        start = css.index(".strip:not(.thumbs) {")
        rule = css[start:css.index("}", start)]
        for decl in (
            "scroll-snap-type: x mandatory;",
            "overscroll-behavior-x: contain;",
            "scroll-padding-left: calc(2px + var(--gl-safe-left, 0px));",
            "scroll-padding-right: calc(2px + var(--gl-safe-right, 0px));",
        ):
            self.assertIn(decl, rule)
        self.assertIn(".sim { width: 120px; scroll-snap-align: start; }", css)


class PreviewScriptTests(unittest.TestCase):
    def test_preview_simulates_fullscreen_instead_of_opening_an_overlay(self) -> None:
        from pathlib import Path

        source = (Path(__file__).resolve().parent.parent / "scripts" / "preview_game_cards.py").read_text()
        self.assertNotIn("--open", source)
        self.assertNotIn("__PREVIEW_OPEN_INDEX__", source)
        self.assertIn('"--display", choices=["inline", "fullscreen"]', source)
        self.assertIn('context["displayMode"] = "fullscreen"', source)
        self.assertIn('context["availableDisplayModes"] = ["inline", "fullscreen"]', source)
        self.assertIn("if (window.__PREVIEW_TOOL_INPUT__) lastToolInput = window.__PREVIEW_TOOL_INPUT__;",
                      apps.GAME_CARDS_HTML)


class DesignSystemTests(unittest.TestCase):
    """Spec 2026-10-03 §1.1–§1.2: host theming, type scale, touch, focus."""

    def test_documents_declare_both_schemes_and_paint_no_page(self) -> None:
        for name, html in WIDGETS:
            with self.subTest(widget=name):
                self.assertIn('<meta name="color-scheme" content="light dark">', html)
                self.assertIn("html, body { background: transparent; }", widget_css(html))

    def test_tokens_resolve_to_host_variables_with_claude_fallbacks(self) -> None:
        for marker in (
            "--gl-text: var(--color-text-primary, light-dark(#141413, #FAF9F5));",
            "--gl-surface: var(--color-background-primary, light-dark(#FFFFFF, #30302E));",
            "--gl-inset: var(--color-background-secondary, light-dark(#F5F4ED, #262624));",
            "--gl-r-md: var(--border-radius-md, 8px);",
            "--gl-bw: var(--border-width-regular, 0.5px);",
            "--gl-font: var(--font-sans, system-ui,",
            # theme without variables still flips the light-dark() fallbacks
            ':root[data-theme="dark"] { color-scheme: dark; }',
        ):
            self.assertIn(marker, apps_shared.TOKENS_CSS)

    def test_no_hex_color_outside_the_token_layer(self) -> None:
        # Every structural color is a --gl-* token; the only hexes left are
        # the fallbacks in TOKENS_CSS itself (the stamp and the cover plates
        # are built from tokens and hsl()).
        for name, html in WIDGETS:
            css = widget_css(html).replace(apps_shared.TOKENS_CSS, "")
            js = widget_js(html)
            with self.subTest(widget=name):
                self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b", css), [])
                self.assertEqual(re.findall(r"[\"']#[0-9a-fA-F]{3,8}[\"']", js), [])

    def test_three_sizes_two_weights_nothing_below_12px(self) -> None:
        sizes = {"var(--gl-cap)", "var(--gl-body)", "var(--gl-h)", "var(--gl-title)"}
        weights = {"var(--gl-regular)", "var(--gl-strong)"}
        for name, html in WIDGETS:
            css = widget_css(html)
            with self.subTest(widget=name):
                self.assertTrue(set(re.findall(r"font-size:\s*([^;}]+)", css)) <= sizes)
                self.assertTrue(set(re.findall(r"font-weight:\s*([^;}]+)", css)) <= weights)
                # the shorthand would smuggle a size past the check above
                self.assertEqual(re.findall(r"(?<![-\w])font:(?!\s*inherit)", css), [])
        for token, px in (("cap", 12), ("body", 14), ("h", 16), ("title", 20)):
            self.assertRegex(
                apps_shared.TOKENS_CSS,
                rf"--gl-{token}: max\(12px, var\(--font-[a-z-]+-size, {px}px\)\);",
            )

    def test_focus_rings_hit_areas_and_reduced_motion(self) -> None:
        self.assertIn(
            ":focus-visible { outline: 2px solid var(--gl-border-strong); outline-offset: 2px; }",
            apps_shared.A11Y_CSS,
        )
        self.assertIn("inset: -4px;", apps_shared.A11Y_CSS)    # 32px on pointer devices
        self.assertIn("inset: -6px;", apps_shared.A11Y_CSS)    # 44px on touch
        self.assertIn("@media (prefers-reduced-motion: reduce)", apps_shared.A11Y_CSS)
        self.assertIn("html.touch .btn, html.touch .disclosure { min-height: 44px; }",
                      apps_shared.CONTROLS_CSS)
        self.assertIn("min-height: 40px;", apps_shared.CONTROLS_CSS)
        # the skeleton only pulses when motion is allowed
        self.assertIn("@media (prefers-reduced-motion: no-preference)", apps_shared.SKELETON_CSS)

    def test_nothing_tilts_but_the_verdict_stamp(self) -> None:
        # The toybox stickers are gone. The stamp keeps its -3deg; the only
        # other rotate() is the disclosure chevron flipping when open.
        for name, html in WIDGETS:
            with self.subTest(widget=name):
                found = sorted(re.findall(r"rotate\([^)]*\)", widget_css(html)))
                allowed = ["rotate(180deg)", "rotate(-3deg)"]
                self.assertTrue(set(found) <= set(allowed), found)
        self.assertIn("transform: rotate(-3deg);", widget_css(apps_eval.EVAL_CARD_HTML))

    def test_numbers_sit_in_tabular_figures(self) -> None:
        for block in (apps_shared.CHIP_CSS, apps_shared.TAG_CSS, apps_shared.PANEL_CSS):
            self.assertIn("font-variant-numeric: tabular-nums;", block)


class BridgeProtocolTests(unittest.TestCase):
    """Spec §1.1.4–§1.1.5 and §1.4: the hand-rolled bridge's new surface."""

    def test_every_inbound_notification_is_routed(self) -> None:
        for marker in (
            'case "ui/notifications/tool-result": handleToolResult(m.params); return;',
            'case "ui/notifications/tool-input": handleToolInput(m.params); return;',
            'case "ui/notifications/tool-input-partial": return;',
            'case "ui/notifications/tool-cancelled": handleToolCancelled(m.params); return;',
            'case "ui/notifications/host-context-changed": applyHostContext(m.params); return;',
            'case "ui/resource-teardown":',
            "post({ jsonrpc: \"2.0\", id: m.id, result: {} });",
            "error: { code: -32601, message: \"Method not found\" } });",
        ):
            self.assertIn(marker, apps_shared.BRIDGE_JS)
        self.assertIn("lastToolInput = (params && params.arguments) || {};", apps_shared.TOOL_RESULT_JS)
        self.assertIn('notice(root, "Cancelled");', apps_shared.TOOL_RESULT_JS)
        self.assertIn("if (resizeObserver) resizeObserver.disconnect();", apps_shared.SIZING_JS)

    def test_host_context_is_merged_and_applied(self) -> None:
        for marker in (
            "Object.keys(ctx).forEach(function (k) { hostContext[k] = ctx[k]; });",
            "docEl.dataset.theme = ctx.theme;",
            "docEl.style.colorScheme = ctx.theme;",
            "docEl.style.setProperty(name, String(value));",
            'fonts.id = "host-fonts";',
            "fonts.textContent = styles.css.fonts;",
            'document.body.style["padding" + side[1]] = (BASE_GUTTER + extra) + "px";',
            'docEl.classList.toggle("touch", !!device.touch);',
            'docEl.classList.toggle("no-hover", device.hover === false);',
            'docEl.setAttribute("data-display-mode", String(ctx.displayMode));',
        ):
            self.assertIn(marker, apps_shared.BRIDGE_JS)

    def test_initialize_declares_display_modes_and_applies_the_answer(self) -> None:
        for marker in (
            'appCapabilities: { availableDisplayModes: ["inline", "fullscreen"] },',
            "appInfo: { name: appName,",
            "clientInfo: { name: appName,",
            "applyHostContext(res && res.hostContext);",
            "if (window.__PREVIEW_HOST_CONTEXT__) applyHostContext(window.__PREVIEW_HOST_CONTEXT__);",
            "showSkeleton();",
        ):
            self.assertIn(marker, apps_shared.INIT_JS)
        self.assertIn('startWidget("gamelib-game-cards");', apps.GAME_CARDS_HTML)
        self.assertIn('startWidget("gamelib-eval-card");', apps_eval.EVAL_CARD_HTML)

    def test_blocked_link_toast_shows_the_url_without_instructions(self) -> None:
        self.assertIn('"This host blocked the link."', apps_shared.EXTERNAL_LINK_JS)
        self.assertIn('t.appendChild(el("div", "toast-url", url));', apps_shared.EXTERNAL_LINK_JS)
        self.assertIn("user-select: all;", apps_shared.TOAST_CSS)
        for name, html in WIDGETS:
            with self.subTest(widget=name):
                self.assertNotIn("right-click", html)


class LabelTests(unittest.IsolatedAsyncioTestCase):
    """Spec §1.3 LABELS_JS: no raw identifier ever renders."""

    def test_every_registry_platform_has_an_explicit_label(self) -> None:
        from gamelib_mcp.platforms_registry import PLATFORMS

        names = {spec.name for spec in PLATFORMS}
        self.assertEqual(names, set(apps_shared._PLATFORM_DISPLAY))
        self.assertEqual(
            {name: apps_shared.PLATFORM_LABELS[name] for name in names},
            {
                "steam": "Steam", "epic": "Epic Games", "gog": "GOG",
                "switch2": "Switch 2", "ps5": "PS5", "xbox": "Xbox",
                "itchio": "itch.io", "ea": "EA app", "ubisoft": "Ubisoft Connect",
                "other": "Other",
            },
        )

    async def test_every_verdict_literal_has_an_explicit_label(self) -> None:
        from gamelib_mcp import main
        from gamelib_mcp.tools.assessment import ASSESSMENT_VERDICTS

        tools = {t.name: t for t in await main.mcp.list_tools()}
        verdict = tools["record_assessment"].parameters["properties"]["verdict"]
        enum = next(o["enum"] for o in verdict["anyOf"] if "enum" in o)
        self.assertEqual(set(enum), set(ASSESSMENT_VERDICTS))
        self.assertEqual(
            apps_shared.VERDICT_LABELS,
            {
                "buy_now": "Buy now",
                "wishlist_for_sale": "Wishlist for a sale",
                "try_demo": "Try the demo",
                "skip": "Skip",
                "play_what_you_own": "Play what you own",
            },
        )
        self.assertEqual(set(apps_shared.VERDICT_LABELS), set(enum))

    def test_the_maps_ride_in_both_widgets_with_a_humanizing_fallback(self) -> None:
        self.assertIn('"switch2": "Switch 2"', apps_shared.LABELS_JS)
        self.assertIn('"play_what_you_own": "Play what you own"', apps_shared.LABELS_JS)
        # Unknown values degrade to a title-cased, underscore-stripped form.
        self.assertIn("return humanize(key);", apps_shared.LABELS_JS)
        self.assertIn('replace(/_+/g, " ")', apps_shared.LABELS_JS)
        self.assertEqual(apps_shared._humanize("steam_deck"), "Steam Deck")
        for name, html in WIDGETS:
            with self.subTest(widget=name):
                self.assertIn(apps_shared.LABELS_JS, html)


class SharedComponentTests(unittest.TestCase):
    """Spec §1.3: the components Phase B consumes, pinned through their markers."""

    def test_numbers_helpers(self) -> None:
        js = apps_shared.NUMBERS_JS
        self.assertIn('return (estimate ? "~" : "") + (n >= 10 ? Math.round(n)', js)
        self.assertIn('return word ? text + " " + word + (n === 1 ? "" : "s") : text;', js)
        self.assertIn('var CURRENCY_SIGNS = { EUR: "€", USD: "$", GBP: "£" };', js)
        self.assertIn('return code ? value + " " + code : value;', js)
        for name, html in WIDGETS:
            with self.subTest(widget=name):
                # "114k reviews", never the old "93% · n=114k".
                self.assertEqual(re.findall(r"[\"' ]n=", widget_js(html)), [])

    def test_tier_functions(self) -> None:
        js = apps_shared.SCORE_CHIP_JS
        self.assertIn('function mcTier(n) { return n >= 75 ? "good" : n >= 50 ? "ok" : "bad"; }', js)
        self.assertIn('t = n >= 84 ? "mighty" : n >= 75 ? "strong" : n >= 65 ? "fair" : "weak";', js)
        self.assertIn('return s == null ? "none" : s >= 6 ? "good" : s === 5 ? "ok" : "bad";', js)
        self.assertIn('function craftTier(pct) { return pct >= 75 ? "good" : pct >= 50 ? "ok" : "bad"; }', js)

    def test_match_bar(self) -> None:
        js = apps_shared.MATCH_BAR_JS
        for marker in (
            'wrap.setAttribute("role", "meter");',
            'wrap.setAttribute("aria-valuenow", String(p));',
            'wrap.appendChild(el("b", null, p + "% match"));',
            'fill.style.width = p + "%";',
        ):
            self.assertIn(marker, js)
        self.assertIn(".match .fill { display: block; height: 100%; background: var(--gl-inverse-bg); }",
                      apps_shared.MATCH_BAR_CSS)
        self.assertIn("height: 4px;", apps_shared.MATCH_BAR_CSS)

    def test_skeleton_shapes(self) -> None:
        js = apps_shared.SKELETON_JS
        self.assertIn('if (kind === "grid") {', js)
        self.assertIn("for (var c = 0; c < 4; c++) {", js)          # 4 cards
        self.assertIn('lines(col, kind === "detail" ? 4 : 1);', js)  # detail: 4 lines
        self.assertIn("for (var k = 0; k < 3; k++) chips.appendChild", js)  # eval: 3 chips
        self.assertIn('wrap.setAttribute("aria-busy", "true");', js)
        self.assertIn("function skeletonKind()", apps.GAME_CARDS_HTML)
        self.assertIn('function skeletonKind() { return "eval"; }', apps_eval.EVAL_CARD_HTML)

    def test_display_mode_requests_resolve_to_the_granted_mode(self) -> None:
        js = apps_shared.DISPLAY_MODE_JS
        for marker in (
            'request("ui/request-display-mode", { mode: mode })',
            "setTimeout(function () { resolve(undefined); }, 2500);",
            "var granted = res && res.mode ? String(res.mode) : before;",
            'return Array.isArray(modes) && modes.indexOf("fullscreen") >= 0;',
            'document.documentElement.setAttribute("data-display-mode", granted);',
        ):
            self.assertIn(marker, js)

    def test_model_context_and_message(self) -> None:
        js = apps_shared.MODEL_CONTEXT_JS
        self.assertIn('var params = { content: [{ type: "text", text: String(text) }] };', js)
        self.assertIn("if (structured) params.structuredContent = structured;", js)
        self.assertIn('request("ui/update-model-context", params);', js)
        self.assertIn('request("ui/message", { role: "user", content: { type: "text", text: String(text) } });', js)

    def test_disclosure_builds_once_and_reports_state(self) -> None:
        js = apps_shared.DISCLOSURE_JS
        for marker in (
            'btn.setAttribute("aria-expanded", "false");',
            "if (open && !built) { built = true; buildFn(body); }",
            'btn.setAttribute("aria-expanded", open ? "true" : "false");',
            "reportSize();",
        ):
            self.assertIn(marker, js)
        self.assertIn(".disclosure { width: 100%; }", apps_shared.CONTROLS_CSS)

    def test_notice_names_what_failed_and_where(self) -> None:
        js = apps_shared.NOTICE_JS
        self.assertIn('return it.what + (it.source ? " (" + it.source + ")" : "");', js)
        self.assertIn('text = "Couldn\'t load: " + parts.join(", ");', js)
        for name, html in WIDGETS:
            with self.subTest(widget=name):
                self.assertNotIn("some data unavailable", html)


class SharedBlockTests(unittest.TestCase):
    """apps_shared.py is spliced in, never paraphrased.

    The two widgets serve self-contained HTML, so the only thing keeping their
    common blocks in step is that both files splice the SAME constants. If a
    constant stops appearing verbatim, an edit to apps_shared.py has silently
    stopped reaching this widget.
    """

    def test_every_shared_constant_is_spliced_in_verbatim(self) -> None:
        for name, block in shared_blocks():
            with self.subTest(block=name):
                self.assertIn(block, apps.GAME_CARDS_HTML)

    def test_the_shared_module_carries_the_blocks_worth_sharing(self) -> None:
        # A guard on the guard: an empty apps_shared would make the assertions
        # above pass vacuously.
        names = [name for name, _ in shared_blocks()]
        self.assertGreater(len(names), 20)
        for expected in (
            "BRIDGE_JS", "HERO_MEDIA_JS", "MEDIA_PANEL_JS", "CAROUSEL_STAGE_JS",
            "TOKENS_CSS", "CHIP_CSS", "SKELETON_CSS", "LABELS_JS", "NUMBERS_JS",
            "SCORE_CHIP_JS", "MATCH_BAR_JS", "SKELETON_JS", "DISPLAY_MODE_JS",
            "MODEL_CONTEXT_JS", "DISCLOSURE_JS", "NOTICE_JS",
        ):
            self.assertIn(expected, names)


if __name__ == "__main__":
    unittest.main()
