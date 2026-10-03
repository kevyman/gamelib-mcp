"""Tests for the MCP Apps game-cards widget and cover-art plumbing.

Also home of the shared design-system tests (the token layer, type scale,
bridge protocol and shared components in apps_shared.py): each one asserts
against BOTH widgets, since both splice the same blocks.
"""

import hashlib
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path
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
        # (b) the Binder chip: the tier is the 1px border and the mono value,
        # never a fill — the ground is the card surface. The tier classes only
        # re-point the tier tokens, which are the host's success / warning /
        # danger tokens.
        chip = apps_shared.CHIP_CSS.split("  .chip {", 1)[1].split("}", 1)[0]
        self.assertIn("border: 1px solid var(--gl-tier);", chip)
        self.assertIn("background: var(--gl-surface);", chip)
        value = apps_shared.CHIP_CSS.split("  .chip b {", 1)[1].split("}", 1)[0]
        self.assertIn("color: var(--gl-tier-text);", value)
        self.assertIn("font-family: var(--gl-mono);", value)
        for tier in ("good", "ok", "bad"):
            self.assertIn(f".tier-{tier} {{ --gl-tier: var(--gl-{tier}-edge); --gl-tier-text: var(--gl-{tier});",
                          apps_shared.TOKENS_CSS)
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
        start = apps_shared.SCORE_CHIP_JS.index("function steamChip(desc, url, opts)")
        body = apps_shared.SCORE_CHIP_JS[start:]
        self.assertIn("if (!desc) return null;", body)
        self.assertIn('label: "Steam", value: phrase, tier: steamTier(desc),', body)
        # Both cards render Steam through it, phrase + meter: the detail card
        # with the store link, the grid card with its brand heard, not drawn.
        self.assertIn("steamChip(game.steam_review_desc,\n", apps.GAME_CARDS_HTML)
        self.assertIn("var steam = steamChip(game.steam_review_desc, null);", apps.GAME_CARDS_HTML)
        self.assertNotIn("{ meter: false }", widget_js(apps.GAME_CARDS_HTML).replace(apps_shared.SCORE_CHIP_JS, ""))
        self.assertNotIn("gridSteamChip", apps.GAME_CARDS_HTML)
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
        # The nested-content type is a quiet lozenge on the plate's sub line
        # (no tier color), on the grid card and the detail card alike.
        self.assertIn('return typeLabel ? el("span", "loz type-loz", typeLabel) : null;', apps.GAME_CARDS_HTML)
        self.assertEqual(apps.GAME_CARDS_HTML.count("var type = typeLoz(game);"), 2)
        self.assertIn('el("div", "parent-sub", "⤷ " + pName)', apps.GAME_CARDS_HTML)

    def test_detail_card_renders_content_badge_and_parent_subtitle(self) -> None:
        panel = js_function(apps.GAME_CARDS_HTML, "function identityPanel(game, media)")
        self.assertIn("if (type) sub.appendChild(type);", panel)
        self.assertIn('el("div", "parent-sub", "part of " + pName)', panel)
        self.assertNotIn("content-badge", apps.GAME_CARDS_HTML)

    def test_detail_cover_plate_drops_the_duplicate_title(self) -> None:
        # The plate's title sits right under the art on the detail card, so
        # lettering the name onto the art stand-in reads as a doubled title. The
        # gradient stays; grid cards keep their lettering.
        self.assertIn(
            ".dt-card .cover-fallback { color: transparent; text-shadow: none; }",
            apps.GAME_CARDS_HTML,
        )

    def test_detail_card_has_an_empty_state_with_the_skip_reasons(self) -> None:
        # A never-enriched row (an assessment-minted candidate) fills nothing
        # but the title; say so through the shared notice, and relay
        # get_game_detail's `enrichment` {provider: reason} map in plain words
        # — one sentence per notice, no em dash, never raw ids (B7; executed
        # in DetailCardBehaviourTests).
        for marker in (
            'var emptyText = "No details fetched yet";',
            "var reasons = enrichmentReasons(game.enrichment);",
            'if (reasons.length) emptyText += ": " + midSentence(enrichmentSentence(reasons));',
            "noticeText = enrichmentSentence(reasons);",
            "noticeText = emptyText;",
            # bookkeeping reads last: just before the actions row
            "if (noticeText) flow.insertBefore(notice(flow, noticeText), actions);",
            'var ENRICH_SOURCES = { steam_store: "Steam", protondb: "ProtonDB", igdb: "IGDB" };',
            'no_steam_appid: "No Steam page for this game",',
            'no_match: "No {src} match for this game",',
            'var ENRICH_MISSING = { steam_store: "Steam reviews", protondb: "ProtonDB", igdb: "IGDB details" };',
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
        # Under the big card: the media reel (one viewer + one thumb strip,
        # the shared block) and the IN YOUR LIBRARY strip of mini cards when
        # get_game_detail(media=True) supplies them.
        for marker in (
            'var media = game.media || {};',      # detailCard reads the block
            'stack.appendChild(panel.frame);',
            'mediaNode(flow, media, game.name)',
            'similarStrip(flow, game.similar);',
            'relatedBlock(flow, actions, game);',
            'section(parent, "Media")',
            'var viewer = el("div", "hero viewer")',
            'el("div", "strip thumbs")',
            'btn.classList.toggle("sel", i === j)',
            'select(0);',                         # trailer first when there is one
        ):
            self.assertIn(marker, apps.GAME_CARDS_HTML)
        strip = js_function(apps.GAME_CARDS_HTML, "function similarStrip(parent, similar)")
        for marker in (
            '.filter(function (i) { return i && i.name; }).slice(0, 8);',   # bounded: <=8 minis
            'var sec = eyebrowSection(parent, "In your library");',
            'var strip = el("div", "strip ministrip");',
            'tier: rating != null ? ratingTier(rating) : "none",',
            # the shared mini format (B3/B4): pips, "50h" "played"/status,
            # the year — separate spans, never "9/10 · 50h"
            'lines: miniLines({ rating: rating, hours: item.playtime_hours, status: item.completion_status,',
            "unplayed: item.unplayed, year: item.release_year, platform: item.platform }),",
            'title: why.length ? "Shares: " + why.join(", ") : null,',
        ):
            self.assertIn(marker, strip)

    def test_similar_cards_carry_no_truncated_tag_line(self) -> None:
        # Spec §1.5, then the Binder: a similar game is a mini card. The 10px
        # one-line "why" (shared tags, ellipsized) is gone; the shared tags
        # survive only as the mini's tooltip. The pre-Binder poster cards
        # (similarNode / pedigreeNode, the .sim CSS, the You/Played/Critics
        # sticker chips) were spliced in but never called, so they are gone.
        self.assertIn('title: why.length ? "Shares: " + why.join(", ") : null,', apps.GAME_CARDS_HTML)
        for name, html in WIDGETS:
            with self.subTest(widget=name):
                self.assertNotIn("sim-why", html)
                for gone in ("function similarNode(", "function similarTags(", "function pedigreeNode(",
                             "function pedigreeBadges(", "function ownershipTags(", "function youChip(",
                             "function playedChip(", "function criticsChip(", "  .sim {", ".ped-pub",
                             ".ped-strip"):
                    self.assertNotIn(gone, html)
        for gone in ("SIMILAR_CSS", "SIMILAR_NODE_JS", "OWNERSHIP_TAGS_JS"):
            self.assertFalse(hasattr(apps_shared, gone), gone)
        for name, html in WIDGETS:
            with self.subTest(widget=name):
                self.assertNotIn('"tag rated"', html)
                self.assertNotIn('"tag owned"', html)
                self.assertNotIn('"tag unplayed"', html)

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
        # FROM THE STUDIO: the eyebrow and a strip of minis of what the studio
        # shipped before; the studio, publisher and track record moved into
        # the full breakdown.
        strip = js_function(apps.GAME_CARDS_HTML, "function studioStrip(parent, ped)")
        for marker in (
            "var items = list(ped.previous_games).filter(function (i) { return i && i.name; }).slice(0, 8);",
            'var sec = eyebrowSection(parent, "From the studio");',
            "lines: miniLines({ rating: rating, hours: item.owned ? item.playtime_hours : null,",
        ):
            self.assertIn(marker, strip)
        self.assertIn("studioStrip(flow, game.pedigree);", apps.GAME_CARDS_HTML)
        breakdown = js_function(apps.GAME_CARDS_HTML, "function breakdownNode(game)")
        for marker in (
            'box.appendChild(abilityNode("Studio", facts.join(", ")));',
            'if (ped.publisher_name) box.appendChild(abilityNode("Publisher", ped.publisher_name));',
            '"You\'ve played " + (num(record.played_count) || 0) + " of "',
            '" — avg " + avg + "/10."',
        ):
            self.assertIn(marker, breakdown)

    def test_pedigree_badge_prefers_his_rating_over_the_critic_score(self) -> None:
        # A studio mini reads the shared mini format (B3): his rating as pips
        # when he owns and rated it (and the mini's tier), his hours, "not
        # owned" for one he doesn't have, the year. The critic-score stand-in
        # is gone with the other per-strip line formats.
        strip = js_function(apps.GAME_CARDS_HTML, "function studioStrip(parent, ped)")
        self.assertIn("var rating = item.owned ? num(item.my_rating) : null;", strip)
        self.assertIn('tier: rating != null ? ratingTier(rating) : "none",', strip)
        self.assertIn("owned: !!item.owned, year: item.release_year, platform: item.platform }),", strip)
        self.assertNotIn("critic", strip)

    def test_the_damper_branch_renders_the_header_line_alone(self) -> None:
        # previous_games is empty under the big-studio damper: the eyebrow and
        # a notice saying why (or that nothing earlier resolved), no strip.
        strip = js_function(apps.GAME_CARDS_HTML, "function studioStrip(parent, ped)")
        empty = strip[strip.index("if (!items.length) {"):strip.index('var strip = el("div", "strip ministrip");')]
        for marker in (
            "if (ped.big_catalog && size) {",
            'notice(sec, "No earlier games picked: " + studio + " has "',
            '+ plural(size, "game", ped.catalog_truncated) + " on IGDB");',
            'notice(sec, [{ what: "from the studio", source: "IGDB: no earlier games resolved" }]);',
            "return;",
        ):
            self.assertIn(marker, empty)

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
            # the dialog chrome (role, ✕, trap, keys) is the shared one
            'var chrome = lightboxPanel("lightbox-panel carousel", gameName, closeLightbox);',
            "var onKey = lightboxKeys(panel, function (delta) { show(index + delta); }, closeLightbox);",
            'document.addEventListener("keydown", onKey, true);',
            "if (ev.target === overlay) closeLightbox();",
            "closer.focus({ preventScroll: true });",
            "current.trigger.focus({ preventScroll: true });",
        ):
            self.assertIn(marker, apps.GAME_CARDS_HTML)
        for marker in (
            'navButton("car-prev", "‹"',
            'navButton("car-next", "›"',
            'counter.textContent = (index + 1) + " / " + shots.length;',
            'else if (ev.key === "ArrowLeft") { ev.preventDefault(); step(-1); }',
            'stage.addEventListener("pointerup"',
        ):
            self.assertIn(marker, apps.GAME_CARDS_HTML)

    def test_rank_badge_is_a_quiet_hash_number(self) -> None:
        # The rank is the plate's card number, "No. 1" — mono cap, muted, the
        # global position (offset + index), and only when the payload carries
        # an offset. No CSS counter, no tilt.
        card = js_function(apps.GAME_CARDS_HTML, "function gridCard(game, ctx)")
        self.assertIn('if (typeof c.offset === "number" && typeof c.index === "number") {', card)
        self.assertIn('sub.appendChild(el("span", "card-no", "No. " + (c.offset + c.index + 1)));', card)
        grid = js_function(apps.GAME_CARDS_HTML, "function renderGrid(data)")
        self.assertIn('offset: typeof data.offset === "number" ? data.offset : null', grid)
        css = widget_css(apps.GAME_CARDS_HTML)
        self.assertNotIn("counter(rank)", css)
        self.assertNotIn("№", apps.GAME_CARDS_HTML)
        self.assertIn("font-family: var(--gl-mono);", apps_shared.PLATE_CSS.split(".card-no {", 1)[1].split("}", 1)[0])
        # B1: one row — "No. N" never wraps alone; the others ellipsize first
        self.assertIn(".gc-card .card-no { margin-left: auto; flex-shrink: 0; overflow: visible; }", css)
        self.assertIn(".gc-card .plate .sub { gap: 4px 6px; flex-wrap: nowrap; }", css)

    def test_platform_ids_and_hours_render_through_the_shared_helpers(self) -> None:
        for marker in (
            'label("platform", game.suggested_platform)',
            'label("platform_short", p.platform)',
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
    """The Binder grid (spec 2026-10-04 §1.3, §2 Phase 2A): set line, card
    frame / badge / ribbon / plate / chips / tags, footer, card tap."""

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
        # The set line: "<b>8 of 2090</b>" then one lozenge per facet — the
        # sort, each vibe, "unplayed", "≤ 10h" — never a middot string.
        head = js_function(self.HTML, "function headerParts(data)")
        for marker in (
            '? shown + " of " + total',                          # "8 of 2090"
            ': plural(shown, "game");',
            "facets.push(SORT_LABELS[args.sort_by] || SORT_LABELS.match);",
            "list(args.vibes).filter(Boolean).forEach(function (v) { facets.push(String(v)); });",
            'if (args.unplayed_only !== false) facets.push("unplayed");',
            'if (maxHours != null) facets.push("≤ " + maxHours + "h");',
            'if (minScore != null) facets.push("critics ≥ " + minScore);',
            'facets.push("ProtonDB " + String(args.protondb_min_tier).toLowerCase() + "+");',
        ):
            self.assertIn(marker, head)
        self.assertIn('var SORT_LABELS = { match: "taste match", critic: "critic score", value: "value" };',
                      self.HTML)
        # get_game_detail's arguments never produce discover filters.
        self.assertIn("if (args && !isDetailArgs(args)) {", head)
        line = js_function(self.HTML, "function headerLine(data)")
        self.assertIn('head.appendChild(el("b", null, parts.count));', line)
        self.assertIn('parts.facets.forEach(function (facet) { head.appendChild(el("span", "loz", facet)); });',
                      line)
        css = widget_css(self.HTML)
        self.assertIn("  .grid-head > .loz { text-transform: none; letter-spacing: 0;", css)
        # Tool input arriving after the result redraws the grid (a hook, not
        # a reassigned shared function).
        self.assertIn("hooks.afterToolInput = function () {", self.HTML)
        self.assertNotIn("handleToolInput = function", self.HTML)
        self.assertIn('else if (view === "grid" && gridData) render(gridData);', self.HTML)

    def test_match_bar_leads_the_card_body(self) -> None:
        # The card body, in order: grain → art (cover, badge, ribbon) → plate
        # (title-s, sub spans) → one chip row → the matched tags. The match is
        # the badge now; there is no match bar on a card.
        card = js_function(self.HTML, "function gridCard(game, ctx)")
        self.assertNotIn("matchBar(", card)
        order = [
            "var card = frameNode(",
            "art.appendChild(coverNode(game));",
            "art.appendChild(badge);",
            'art.appendChild(ribbonNode(b.text, "good", null, "s art"));',
            "card.appendChild(art);",
            'plate.appendChild(el("div", "plate-title-s", game.name));',
            "if (sub.childNodes.length) plate.appendChild(sub);",
            "card.appendChild(plate);",
            "card.appendChild(row);",
            'card.appendChild(el("div", "gc-nochip", "No critic scores"));',
            "card.appendChild(tags);",
        ]
        positions = [card.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))
        frame = js_function(self.HTML, "function frameNode(tag, cls, tier)")
        self.assertIn("var grain = grainNode();", frame)
        self.assertIn("if (grain) frame.appendChild(grain);", frame)
        # Matched tags: at most three gap-separated spans with a 6px diamond
        # bullet drawn by CSS — never a " · " join, never pills.
        self.assertIn("var why = matchedTagNames(game).slice(0, 3);", card)
        self.assertIn('why.forEach(function (t) { tags.appendChild(el("span", null, t)); });', card)
        css = widget_css(self.HTML)
        bullet = css.split("  .gc-tags > span::before {", 1)[1].split("}", 1)[0]
        # B2: a 6px diamond in the muted text color (not fainter)
        self.assertIn("background: var(--gl-muted);", bullet)
        for decl in ("width: 6px;", "height: 6px;", "transform: rotate(45deg);"):
            self.assertIn(decl, bullet)
        self.assertNotIn('"pill"', self.HTML)
        self.assertNotIn(".pill {", css)

    def test_cover_carries_only_rank_metacritic_and_type_chip(self) -> None:
        # The art carries the badge (top left) and, only for TOP MATCH or
        # CRITICS' PICK, the slim ribbon on its bottom edge — nothing else.
        card = js_function(self.HTML, "function gridCard(game, ctx)")
        self.assertEqual(card.count("art.appendChild("), 3)
        self.assertIn('badge.classList.add("badge-on-art");', card)
        bits = js_function(self.HTML, "function cardBits(game, ctx)")
        # the badge: the match when sorted by match, else the critic score
        self.assertIn('if (pct != null && sort === "match") {', bits)
        self.assertIn('bits.push({ part: "badge", value: pct, suffix: "%", tag: "Match",', bits)
        self.assertIn('tier: pct >= TOP_MATCH ? "good" : pct >= 50 ? "ok" : "none", text: pct + "% match" });',
                      bits)
        self.assertIn('var name = mc != null ? "Metacritic" : "OpenCritic";', bits)
        self.assertIn("tier: criticTier(game)", bits)
        # the ribbon: TOP MATCH on the first card of an offset-0 match page
        # at >=70, else CRITICS' PICK at Metacritic >=85, else none
        self.assertIn("var CRITICS_PICK = 85;", self.HTML)
        self.assertIn("var TOP_MATCH = 70;", self.HTML)
        self.assertIn('if (pct != null && pct >= TOP_MATCH && sort === "match" && c.index === 0 '
                      '&& c.offset === 0) {', bits)
        self.assertIn('bits.push({ part: "ribbon", text: "Top match" });', bits)
        self.assertIn('} else if (mc != null && mc >= CRITICS_PICK) {', bits)
        self.assertIn('bits.push({ part: "ribbon", text: "Critics\' pick" });', bits)
        # the frame is the badge's tier
        self.assertIn('frameNode(tappable ? "button" : "div", "frame-s gc-card", badgeBit ? badgeBit.tier : "none");',
                      card)
        self.assertNotIn("type-chip", self.HTML)
        self.assertNotIn('cls: "corner"', self.HTML)

    def test_grid_steam_chip_is_phrase_only_on_a_line_of_its_own(self) -> None:
        # One chip row of at most two: Metacritic / OpenCritic drawn as MC /
        # OC (the full name in the title and the aria-label) — skipping the
        # one the badge already shows — or the Steam phrase with its meter,
        # its brand an .sr-only label. "No critic scores" when there is none.
        bits = js_function(self.HTML, "function cardBits(game, ctx)")
        for marker in (
            'if (mc != null && badgeSource !== "mc") {',
            'chips.push({ part: "chip", label: "MC", value: mc, tier: mcTier(mc), title: "Metacritic",',
            'text: "Metacritic " + mc });',
            'chips.push({ part: "chip", label: "OC", value: oc, tier: ocTier(oc, game.opencritic_tier),',
            'steam.classList.add("gc-steam");',
            'if (steamLbl) steamLbl.classList.add("sr-only");',
            "return bits.concat(chips.slice(0, 2));",
        ):
            self.assertIn(marker, bits)
        card = js_function(self.HTML, "function gridCard(game, ctx)")
        self.assertIn("var chip = scoreChip({ label: b.label, value: b.value, tier: b.tier, title: b.title });",
                      card)
        self.assertIn('chip.setAttribute("aria-label", b.text);', card)
        self.assertIn('} else if (!badgeBit || badgeBit.tag === "Match") {', card)
        css = widget_css(self.HTML)
        self.assertIn(".gc-card .chip.gc-steam b { order: -1; }", css)
        sr = css.split("  .sr-only {", 1)[1].split("}", 1)[0]
        for decl in ("position: absolute;", "width: 1px;", "clip-path: inset(50%);"):
            self.assertIn(decl, sr)

    def test_grid_hours_say_what_they_are(self) -> None:
        # The plate shows the figure ("2.3h", "~4h"); its title and the
        # card's label say which it is.
        bits = js_function(self.HTML, "function cardBits(game, ctx)")
        self.assertIn('if (played) bits.push({ part: "hours", value: played, title: "Your playtime", '
                      'text: played + " played" });', bits)
        self.assertIn('else if (hltb) bits.push({ part: "hours", value: hltb, title: "HowLongToBeat, main story", '
                      'text: hltb + " to beat" });', bits)
        card = js_function(self.HTML, "function gridCard(game, ctx)")
        self.assertIn("span.title = b.title;", card)
        self.assertIn('partsOf("platform").forEach(function (b) { sub.appendChild(el("span", "loz", b.short)); });',
                      card)

    def test_the_card_is_named_by_its_title_and_what_it_shows(self) -> None:
        card = js_function(self.HTML, "function gridCard(game, ctx)")
        self.assertIn("var bits = cardBits(game, c);", card)
        self.assertIn('card.setAttribute("aria-label", cardLabel(game, bits));', card)
        self.assertNotIn('"aria-labelledby"', self.HTML)        # aria-label must win
        # the card IS the tap target: a <button class="frame frame-s …">, so
        # Enter and Space come from the element, not a key handler
        self.assertIn('card.type = "button";', card)
        self.assertIn('card.addEventListener("click", function () { selectGame(game); });', card)
        self.assertNotIn('card.setAttribute("role", "button");', card)
        self.assertNotIn("Show details for", self.HTML)

    def test_show_next_button_only_when_has_more(self) -> None:
        more = js_function(self.HTML, "function showNextButton(data)")
        for marker in (
            "if (!data.has_more || !shown) return null;",
            "var limit = num(args.limit) || shown;",
            'var idle = "Show next " + count;',
            'el("button", "btn act-more", idle)',
            'sendMessage("Show the next " + count + " recommendations (offset " + next + ")");',
            'more.textContent = "Asked the chat for the next " + count + "…";',
            # a lost message stays retryable: the button comes back after 8s
            "more.disabled = false;\n        more.textContent = idle;\n      }, 8000);",
        ):
            self.assertIn(marker, more)
        actions = js_function(self.HTML, "function gridActions(data)")
        for marker in (
            "var more = showNextButton(data);",
            "if (canFullscreen() && data.results.length) {",
            'el("button", "btn primary act-expand", "Open full screen")',   # the primary pill
            'requestDisplayMode("fullscreen");',
        ):
            self.assertIn(marker, actions)
        # At most two actions: the secondary "Show next" and the primary.
        self.assertEqual(actions.count('el("button"') + more.count('el("button"'), 2)
        css = widget_css(self.HTML)
        self.assertIn('html[data-display-mode="fullscreen"] .act-expand { display: none; }', css)
        self.assertIn("justify-content: flex-end;", css)
        self.assertIn(".actions > .btn, .actions > .disclosure, .grid-head > .btn, .topbar > .btn "
                      "{ white-space: nowrap; }", css)
        self.assertNotIn(".actions { flex-direction: column; }", css)

    def test_fullscreen_grid_widens_and_sticks_the_header(self) -> None:
        # Two columns on a phone, three from 560px, four in fullscreen; the
        # set line sticks and carries "Show next N" on its right.
        css = widget_css(self.HTML)
        self.assertIn(".grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }", css)
        wide = css.split("  @media (min-width: 560px) {\n    .grid {", 1)[1].split("\n  }", 1)[0]
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr));", wide)
        self.assertIn('html[data-display-mode="fullscreen"] .grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }',
                      wide)
        start = css.index('html[data-display-mode="fullscreen"] .grid-head, .topbar {')
        rule = css[start:css.index("}", start)]
        self.assertIn("position: sticky;", rule)
        self.assertIn("background: var(--gl-surface);", rule)
        self.assertIn(".grid-head > .btn { margin-left: auto; }", css)
        grid = js_function(self.HTML, "function renderGrid(data)")
        self.assertIn('if (gridMode === "fullscreen") {\n      var more = showNextButton(data);\n'
                      "      if (more) head.appendChild(more);", grid)
        # a display-mode change lays the grid out again
        self.assertIn('else if (view === "grid" && gridData && currentDisplayMode() !== gridMode) render(gridData);',
                      js_function(self.HTML, "function hostContextChanged(ctx)"))

    def test_cards_are_dealt_in_once_per_payload(self) -> None:
        # M2: the set line fades in first, then each card is dealt 40ms apart
        # (dealIn caps the stagger); a redraw of the same PAGE — keyed on its
        # offset and game ids, not the payload object — keeps them still
        # (executed in GridReplayBehaviourTests); a skeleton resolves into the
        # view (M5). No other motion is local.
        grid = js_function(self.HTML, "function renderGrid(data)")
        self.assertIn("var key = gridKey(data);", grid)
        self.assertIn("var fresh = dealtKey !== key;", grid)
        self.assertIn("if (fresh) fadeIn(head);", grid)
        self.assertIn("if (fresh) dealIn(card, i);", grid)
        self.assertNotIn("dealtData", self.HTML)
        render = js_function(self.HTML, "function render(data)")
        self.assertIn("gcResolve(skel, node);", render)
        resolve = js_function(self.HTML, "function gcResolve(skel, node)")
        self.assertIn("resolveSkeleton(skel, function () { return node; });", resolve)
        self.assertIn('node.classList.remove("deal");', resolve)
        local = widget_css(self.HTML)
        for name, block in shared_blocks():
            local = local.replace(block, "")
        self.assertNotIn("animation", local)
        self.assertNotIn("@keyframes", local)
        # the one local transition is the lightbox's own (out of scope)
        self.assertEqual(re.findall(r"transition:[^;]*", local), ["transition: transform 0.19s ease"])

    def test_render_code_joins_nothing_with_middots(self) -> None:
        # Separate spans with a gap, never " · " strings: the module's own
        # code carries no middot at all (an aria-label would be the only
        # allowed place, and none needs one).
        source = (Path(apps.__file__)).read_text()
        self.assertNotIn("·", source)

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
        # 8s, and the tap stays pending so a late grant can still drill in
        self.assertIn("pendingSelection = selection;", tap)
        self.assertIn('requestDisplayMode("fullscreen", 8000).then(function (mode) {', tap)
        self.assertIn("if (pendingSelection !== selection) return;", tap)
        self.assertIn('if (mode === "fullscreen") openDrill(game, selection.before);', tap)
        self.assertIn('else sendMessage("Show me " + game.name);', tap)

    def test_the_drill_in_shows_a_skeleton_then_the_live_detail(self) -> None:
        drill = js_function(self.HTML, "function openDrill(game, before)")
        for marker in (
            # the top bar: a secondary pill with a chevron glyph
            'var back = el("button", "btn");',
            'var glyph = iconNode("0 0 20 20", BACK_GLYPH);',
            'back.appendChild(el("span", null, "Back to results"));',
            'back.addEventListener("click", backToResults);',
            'holder.appendChild(skeleton("detail"));',
            'callTool("get_game_detail", { game_id: game.game_id, media: true }, 30000)',
            "if (seq !== drillSeq) return;",
            # the skeleton resolves into the card (M5)
            "gcResolve(skel, detailCard(data));",
            # the failure names its cause, then says what is on screen
            'if (res === TIMED_OUT) { fill(game, "The library didn\'t answer in 30s."); return; }',
            # the drill-in's call is get_game_detail, not the grid's own tool
            'if (res && res.isError) { fill(game, toolErrorText(res, "get_game_detail")); return; }',
            'else if (res === undefined) fill(game, "The host didn\'t run the lookup.");',
            'if (failure) notice(holder, failure + " Showing what the list had.");',
        ):
            self.assertIn(marker, drill)
        self.assertNotIn("←", drill)
        self.assertIn('var BACK_GLYPH = [["path", { d: "m12.5 4.5-5.5 5.5 5.5 5.5" }]];', self.HTML)
        # the shared toolErrorText (TOOL_RESULT_JS), not a local copy
        self.assertEqual(self.HTML.count("function toolErrorText("), 1)
        self.assertIn("function toolErrorText(result, name)", apps_shared.TOOL_RESULT_JS)
        call = js_function(self.HTML, "function callTool(name, args, timeoutMs)")
        self.assertIn('request("tools/call", { name: name, arguments: args }, ms + 1000)', call)
        self.assertIn("resolve(TIMED_OUT); }, ms);", call)
        back = js_function(self.HTML, "function backToResults()")
        self.assertIn("if (gridData) render(gridData);", back)
        self.assertIn('if (restore !== "fullscreen") requestDisplayMode("inline");', back)
        self.assertIn('var cards = root.querySelectorAll(".gc-card");', back)
        # The host's own close button ends the drill-in too.
        self.assertIn(
            'if (view === "drill" && currentDisplayMode() !== "fullscreen" && gridData) render(gridData);',
            js_function(self.HTML, "function hostContextChanged(ctx)"),
        )


NODE = shutil.which("node")

# The card-tap state machine with everything around it stubbed: requests are
# promises the probe resolves by hand, in the order the host would answer.
_TAP_SHIM = r"""
var messages = [], drilled = [], requests = [];
var view = "grid", gridData = { results: [] }, lastSelectedId = null, mode = "inline";
var gridMode = "inline", renders = 0;
function render() { renders += 1; }
function updateModelContext() {}
function sendMessage(text) { messages.push(text); }
function openDrill(game, before) { drilled.push([game.name, before]); view = "drill"; }
function currentDisplayMode() { return mode; }
function canFullscreen() { return true; }
function requestDisplayMode(m, timeoutMs) {
  return new Promise(function (resolve) { requests.push({ mode: m, timeoutMs: timeoutMs, resolve: resolve }); });
}
function tick() { return new Promise(function (r) { setTimeout(r, 0); }); }
"""

_TAP_PROBE = r"""
(async function () {
  var out = {};
  // Late grant: the host flips to fullscreen by context change, then the
  // request times out (resolving to the mode we are in by then).
  selectGame({ game_id: 1, name: "Hades II" });
  out.timeout = requests[0].timeoutMs;
  mode = "fullscreen";
  hostContextChanged({ displayMode: "fullscreen" });
  requests[0].resolve("fullscreen");
  await tick();
  out.lateGrant = { drilled: drilled.slice(), messages: messages.slice() };

  // Silence for 8s: the chat fallback, once.
  drilled = []; messages = []; mode = "inline"; view = "grid";
  selectGame({ game_id: 2, name: "Dead Cells" });
  requests[1].resolve("inline");
  await tick();
  out.timedOut = { drilled: drilled.slice(), messages: messages.slice() };

  // A grant in the answer, then the matching context change: one drill.
  drilled = []; messages = [];
  selectGame({ game_id: 3, name: "Noita" });
  requests[2].resolve("fullscreen");
  await tick();
  mode = "fullscreen";
  hostContextChanged({ displayMode: "fullscreen" });
  out.granted = { drilled: drilled.slice(), messages: messages.slice() };

  // No tap pending: a grid whose display mode changed is laid out again
  // (once), and a context change that keeps the mode redraws nothing.
  view = "grid"; mode = "fullscreen"; gridMode = "inline"; renders = 0;
  hostContextChanged({ displayMode: "fullscreen" });
  gridMode = "fullscreen";
  hostContextChanged({ theme: "dark" });
  out.relayout = renders;
  console.log(JSON.stringify(out));
})();
"""


@unittest.skipUnless(NODE, "node is not installed")
class CardTapBehaviourTests(unittest.TestCase):
    """B2, executed: never the chat fallback while fullscreen may be granted."""

    @classmethod
    def setUpClass(cls) -> None:
        html = apps.GAME_CARDS_HTML
        start = html.index("  var pendingSelection = null;")
        machine = html[start:html.index("  function openDrill(game, before) {", start)]
        assert NODE is not None
        proc = subprocess.run([NODE, "-e", _TAP_SHIM + machine + _TAP_PROBE],
                              capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode != 0:
            raise AssertionError(proc.stderr)
        cls.out = json.loads(proc.stdout)

    def test_the_request_waits_eight_seconds(self) -> None:
        self.assertEqual(self.out["timeout"], 8000)

    def test_a_late_grant_drills_in_and_sends_no_message(self) -> None:
        self.assertEqual(self.out["lateGrant"], {"drilled": [["Hades II", "inline"]], "messages": []})

    def test_silence_falls_back_to_the_chat_once(self) -> None:
        self.assertEqual(self.out["timedOut"], {"drilled": [], "messages": ["Show me Dead Cells"]})

    def test_a_granted_answer_drills_in_once(self) -> None:
        self.assertEqual(self.out["granted"], {"drilled": [["Noita", "inline"]], "messages": []})

    def test_a_display_mode_change_lays_the_grid_out_again(self) -> None:
        self.assertEqual(self.out["relayout"], 1)


# The card's accessible name is executed against the real gridCard in
# tests/test_apps_shared.py::CardLabelBehaviourTests (item 14: the label is
# built from the same bits the card renders).


class DetailModeTests(unittest.TestCase):
    """The Binder detail card (spec 2026-10-04 §1.3, §2 Phase 2A): the big
    frame, then the ground — description, review, awards, tags, reel, strips,
    actions."""

    HTML = apps.GAME_CARDS_HTML

    def test_identity_panel_leads_the_stack(self) -> None:
        # the card, then the ground: what it is like, the reel, the strips,
        # the actions (Full breakdown + the store link)
        card = js_function(self.HTML, "function detailCard(game)")
        order = ["var panel = identityPanel(game, media);", "stack.appendChild(panel.frame);",
                 "dealIn(panel.frame, 0);", "var flow = groundNode(game, media);",
                 "mediaNode(flow, media, game.name)", "similarStrip(flow, game.similar);",
                 "studioStrip(flow, game.pedigree);", "relatedBlock(flow, actions, game);",
                 "var store = storeLink(game);", "stack.appendChild(flow);"]
        positions = [card.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))
        ground = js_function(self.HTML, "function groundNode(game, media)")
        order = ['var desc = el("p", "desc", description);', "flow.appendChild(flavorNode(String(review), true));",
                 'abil.appendChild(abilityNode("Award", text));', "flow.appendChild(tagLine(tags));"]
        positions = [ground.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))

    def test_identity_panel_cover_widths(self) -> None:
        # The frame body: art (cover at full card width, badge, art-edge
        # ribbon) → plate → stats. From 560px the card is 300px with the
        # ground beside it.
        panel = js_function(self.HTML, "function identityPanel(game, media)")
        order = ['var frame = frameNode("article", "dt-card", detailTier(game));',
                 "art.appendChild(coverNode(game));", "if (badge) art.appendChild(badge);",
                 "if (ribbon) art.appendChild(ribbon);", "frame.appendChild(art);",
                 'row.appendChild(el("h2", "plate-title", game.name));',
                 'row.appendChild(el("span", "card-no", "No. " + game.game_id));',
                 "frame.appendChild(plate);", "if (stats) frame.appendChild(stats);"]
        positions = [panel.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))
        css = widget_css(self.HTML)
        wide = css.split("  @media (min-width: 560px) {\n    .detail-stack {", 1)[1].split("}", 1)[0]
        self.assertIn("grid-template-columns: 300px minmax(0, 1fr);", wide)

    def test_frame_tier_badge_and_ribbon_rules(self) -> None:
        tier = js_function(self.HTML, "function detailTier(game)")
        self.assertIn("return mine != null ? ratingTier(mine) : criticTier(game);", tier)
        critic = js_function(self.HTML, "function criticTier(game)")
        self.assertIn("if (realScore(game.metacritic_score)) return mcTier(game.metacritic_score);", critic)
        self.assertIn("if (realScore(game.opencritic_score)) return ocTier(game.opencritic_score, "
                      "game.opencritic_tier);", critic)
        self.assertIn('return "none";', critic)
        badge = js_function(self.HTML, "function detailBadge(game)")
        self.assertIn('badgeNode({ value: mine, suffix: "/10", tag: "Your rating", tier: ratingTier(mine) });', badge)
        self.assertIn('tag: "Metacritic",', badge)
        ribbon = js_function(self.HTML, "function statusRibbon(game)")
        for marker in (
            'if (status) return ribbonNode(label("status", game.completion_status), status[1], note, "art");',
            'if (hours === 0 && game.owned) return ribbonNode("Unplayed", "none", null, "art");',
            "return null;",
        ):
            self.assertIn(marker, ribbon)

    def test_time_to_beat_and_protondb_chips(self) -> None:
        # The chip row (chipRow keeps three): Metacritic, OpenCritic, Steam
        # phrase + meter, ProtonDB. Time to beat is the LENGTH stat row.
        chips = js_function(self.HTML, "function detailChips(game)")
        self.assertIn("tier: protonTier(game.protondb_tier),", chips)
        self.assertIn("return chipRow(chips);", chips)
        self.assertNotIn('label: "HLTB"', self.HTML)
        self.assertNotIn('label: "Time to beat"', self.HTML)

    def test_stat_rows(self) -> None:
        stats = js_function(self.HTML, "function detailStats(game)")
        order = [
            'stats.appendChild(statRow({ label: "Played", value: hoursLabel(hours), key: true }));',
            'if (last) stats.appendChild(statRow({ label: "Last", value: last }));',
            'if (length) stats.appendChild(statRow({ label: "Length", note: "main story", value: length }));',
            "if (paid) stats.appendChild(paid);",
            "if (chips) stats.appendChild(chips);",
            'var rating = statRow({ label: "Rating", value: pipsNode(mine, 10, tier) });',
        ]
        positions = [stats.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("if (hours != null && hours > 0) {", stats)
        paid = js_function(self.HTML, "function paidRow(game)")
        self.assertIn('value: amount === 0 ? "Free" : money(amount, row.price_currency),', paid)
        self.assertIn('label("purchase_source", row.purchase_source)', paid)

    def test_your_rating_is_a_tiered_chip(self) -> None:
        # His rating is the badge and the ten RATING pips, both in its tier —
        # no "Your rating" chip any more.
        stats = js_function(self.HTML, "function detailStats(game)")
        self.assertIn('rating.classList.add("tier-" + tier);', stats)
        self.assertNotIn('label: "Your rating"', self.HTML)
        self.assertNotIn("mine >= 7", self.HTML)
        self.assertNotIn("My rating: ", self.HTML)

    def test_description_clamps_to_three_lines_with_a_more_toggle(self) -> None:
        css = widget_css(self.HTML)
        start = css.index("  .desc {")
        self.assertIn("-webkit-line-clamp: 3;", css[start:css.index("}", start)])
        start = css.index("  .more-toggle {")
        self.assertIn("min-height: 44px;", css[start:css.index("}", start)])
        ground = js_function(self.HTML, "function groundNode(game, media)")
        for marker in (
            'var more = el("button", "more-toggle", "More");',
            'more.textContent = open ? "Less" : "More";',
            'more.setAttribute("aria-expanded", open ? "true" : "false");',
            "if (desc.scrollHeight > desc.clientHeight + 1) {",
        ):
            self.assertIn(marker, ground)

    def test_tags_are_one_muted_line_of_at_most_eight(self) -> None:
        ground = js_function(self.HTML, "function groundNode(game, media)")
        self.assertIn("var tags = list(game.tags).filter(Boolean).slice(0, 8);", ground)
        line = js_function(self.HTML, "function tagLine(tags, cls)")
        self.assertIn('tags.forEach(function (t) { line.appendChild(el("span", null, t)); });', line)

    def test_award_lines_come_from_the_game_awards_features_only(self) -> None:
        self.assertIn("var AWARD_RE = /^the game awards - (.+) - (winner|nominee)$/i;", self.HTML)
        lines = js_function(self.HTML, "function awardLines(features)")
        self.assertIn('lines.push("The Game Awards — " + listed + ", winner");', lines)
        self.assertIn('if (nominated.length) lines.push("Nominee for " + names(nominated).join(", "));', lines)
        self.assertIn(".slice(0, 3)", lines)

    def test_similar_and_studio_sit_behind_fullscreen_or_a_disclosure(self) -> None:
        # The strips are inline now; what sits behind "Full breakdown" is the
        # rest — studio, publisher, track record, the three lengths, genres.
        block = js_function(self.HTML, "function relatedBlock(flow, actions, game)")
        for marker in (
            "var node = breakdownNode(game);",
            "if (!node) return;",
            'if (currentDisplayMode() === "fullscreen") {',
            # the shared control (apps_shared.DISCLOSURE_JS), no local copy
            'fullscreenOrDisclosure(actions, "Full breakdown", build);',
        ):
            self.assertIn(marker, block)
        self.assertNotIn("requestDisplayMode", block)
        self.assertNotIn("chev-out", self.HTML)
        css = widget_css(self.HTML)
        self.assertIn("  .dt-actions > .disclosure {", css)
        self.assertIn("  .dt-actions > .disclosure-body { flex-basis: 100%; order: 3; margin-top: 4px; }", css)
        store = js_function(self.HTML, "function storeLink(game)")
        self.assertIn('var url = "https://store.steampowered.com/app/" + appid + "/";', store)
        self.assertIn("openLink(url);", store)

    def test_carousel_rows_snap_and_peek(self) -> None:
        css = widget_css(self.HTML)
        start = css.index(".strip:not(.thumbs) {")
        rule = css[start:css.index("}", start)]
        for decl in (
            "scroll-snap-type: x mandatory;",
            "overscroll-behavior-x: contain;",
            "scroll-padding-left: calc(4px + var(--gl-safe-left, 0px));",
            "scroll-padding-right: calc(4px + var(--gl-safe-right, 0px));",
        ):
            self.assertIn(decl, rule)
        # the minis ride on the shared .strip.ministrip (MINI_CSS)
        self.assertIn('el("div", "strip ministrip")', self.HTML)
        self.assertNotIn(".sim { width: 120px;", css)




_SAMPLES = Path(__file__).resolve().parent.parent / "scripts" / "preview_samples"


def _sample(name: str) -> dict:
    payload = json.loads((_SAMPLES / name).read_text())
    payload.pop("_note", None)
    return payload


def run_cards(probe: str) -> dict:
    """test_apps_shared.run_widget for the game cards, from a file: the probes
    here carry whole saved payloads, past the OS limit on one argv string."""
    import tempfile

    from test_apps_shared import MINI_DOM

    tail = '  startWidget("gamelib-game-cards");\n})();'
    script = apps.GAME_CARDS_HTML.split("<script>\n", 1)[1].split("</script>", 1)[0]
    assert script.count(tail) == 1
    script = script.replace(tail, tail.split("\n")[0] + "\n" + probe + "\n})();")
    assert NODE is not None
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "probe.js"
        path.write_text(MINI_DOM + script)
        proc = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=60, check=False)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout)


# Readers for the rendered DOM (run under test_apps_shared.MINI_DOM).
_DOM_READERS = r"""
  function q(n, s) { return n.querySelector(s); }
  function txt(n) { return n ? n.textContent : null; }
  function kids(n) { return n ? n.children : []; }
  function cls(n) { return n.getAttribute("class") || n.className; }
  function card(c) {
    var badge = q(c, ".badge"), ribbon = q(c, ".ribbon");
    return {
      tag: c.tagName, cls: c.className, label: c.getAttribute("aria-label"),
      deal: c.classList.contains("deal"), i: c.style["--i"] === undefined ? null : c.style["--i"],
      kids: kids(c).map(cls),
      badge: badge ? [txt(q(badge, ".badge-num")), txt(q(badge, ".badge-suffix")), txt(q(badge, ".badge-tag")),
                      badge.className] : null,
      ribbon: ribbon ? [txt(ribbon), ribbon.className] : null,
      title: txt(q(c, ".plate-title-s")),
      sub: kids(q(c, ".sub")).map(function (s) { return [s.className, s.textContent, s.title || null]; }),
      chips: c.querySelectorAll(".chip").map(function (ch) {
        return [txt(q(ch, ".lbl")), txt(q(ch, "b")), ch.getAttribute("aria-label"), ch.className];
      }),
      nochip: txt(q(c, ".gc-nochip")),
      tags: kids(q(c, ".gc-tags")).map(txt),
    };
  }
"""

_GRID_PROBE = _DOM_READERS + r"""
  (function () {
    applyHostContext({ displayMode: MODE, availableDisplayModes: ["inline", "fullscreen"] });
    lastToolInput = INPUT;
    render(PAYLOAD);
    var page = root.firstElementChild;
    var head = q(page, ".grid-head");
    var actions = q(page, ".grid-actions");
    var out = {
      page: page.className,
      head: kids(head).map(function (n) { return [n.tagName, n.className, n.textContent]; }),
      cards: page.querySelectorAll(".gc-card").map(card),
      actions: actions ? kids(actions).map(function (n) { return [n.className, n.textContent]; }) : null,
      text: page.textContent,
    };
    out.headFades = q(page, ".grid-head").classList.contains("fade-in");
    out.firstDeals = root.querySelectorAll(".deal").length;
    // a redraw of the same payload (Back, a late tool input) deals nothing
    render(PAYLOAD);
    out.redrawDeals = root.querySelectorAll(".deal").length;
    // nor does the host re-delivering the same page as a new object
    render(JSON.parse(JSON.stringify(PAYLOAD)));
    out.copyDeals = root.querySelectorAll(".deal").length;
    out.copyFades = root.querySelectorAll(".fade-in").length;
    // the next page (a new offset) is a fresh deal
    var next = JSON.parse(JSON.stringify(PAYLOAD));
    next.offset = (typeof next.offset === "number" ? next.offset : 0) + next.results.length;
    render(next);
    out.nextDeals = root.querySelectorAll(".deal").length;
    console.log(JSON.stringify(out));
  })();
"""

_RESOLVE_PROBE = r"""
  (async function () {
    var out = {};
    answer("ui/initialize", { hostCapabilities: {}, hostContext: {} });
    await tick();
    host({ jsonrpc: "2.0", method: "ui/notifications/tool-input", params: { arguments: { sort_by: "match", limit: 8 } } });
    out.skeleton = root.children.map(function (n) { return n.className; });
    host({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: { structuredContent: PAYLOAD } });
    out.resolving = root.children.map(function (n) { return n.className; });
    out.cardsDealt = root.querySelectorAll(".gc-card").filter(function (c) { return c.classList.contains("deal"); }).length;
    out.dealAt = root.firstElementChild.style["--deal-at"] || null;
    flushTimers();
    out.resolved = root.children.map(function (n) { return n.className; });
    console.log(JSON.stringify(out));
  })();
"""


_ENRICH_PROBE = r"""
  (function () {
    function say(why) { return enrichmentSentence(enrichmentReasons(why)); }
    console.log(JSON.stringify({
      steam: say({ steam_store: "no_steam_appid", protondb: "no_steam_appid" }),
      mixed: say({ steam_store: "no_steam_platform_row", protondb: "no_steam_platform_row", igdb: "unconfigured" }),
      one: say({ protondb: "no_steam_appid" }),
      igdb: say({ igdb: "no_match" }),
      odd: say({ hltb: "rate_limited" }),
    }));
  })();
"""


@unittest.skipUnless(NODE, "node is not installed")
class EnrichmentNoticeTests(unittest.TestCase):
    """B7, executed: the skipped-provider map as one plain sentence."""

    @classmethod
    def setUpClass(cls) -> None:
        from test_apps_shared import run_widget

        cls.out = run_widget("game-cards", _ENRICH_PROBE)

    def test_reasons_read_as_one_plain_sentence(self) -> None:
        self.assertEqual(self.out["steam"],
                         "No Steam page for this game, so Steam reviews and ProtonDB are unavailable")
        self.assertEqual(self.out["mixed"],
                         "Not owned on Steam, so Steam reviews and ProtonDB are unavailable; IGDB is not configured")
        self.assertEqual(self.out["one"], "No Steam page for this game, so ProtonDB is unavailable")
        self.assertEqual(self.out["igdb"], "No IGDB match for this game")
        self.assertEqual(self.out["odd"], "HowLongToBeat: rate limited")
        for text in self.out.values():
            self.assertNotIn("\u2014", text)


@unittest.skipUnless(NODE, "node is not installed")
class GridCardBehaviourTests(unittest.TestCase):
    """The grid, executed on the saved discover_games payload: the set line,
    each card's frame tier / badge / ribbon / plate / chips / tags, the deal."""

    @classmethod
    def setUpClass(cls) -> None:
        payload = _sample("discover_taste_match.json")

        def run(mode: str, tool_input: dict, data: dict) -> dict:
            return run_cards("  var MODE = " + json.dumps(mode) + ";\n  var INPUT = "
                              + json.dumps(tool_input) + ";\n  var PAYLOAD = " + json.dumps(data) + ";\n"
                              + _GRID_PROBE)

        cls.match = run("inline", {"sort_by": "match", "limit": 8, "vibes": ["cozy"], "max_hltb_hours": 10},
                        payload)
        cls.critic = run("inline", {"sort_by": "critic", "limit": 8}, payload)
        cls.full = run("fullscreen", {"sort_by": "match", "limit": 8}, payload)
        hades = {"game_id": 1, "name": "Hades II", "match_percent": 100, "hltb_main": 26.5,
                 "suggested_platform": "steam", "playtime_hours": 2.3, "metacritic_score": 93,
                 "opencritic_score": 91, "steam_review_desc": "Overwhelmingly Positive",
                 "matched_tags": ["roguelike"]}
        steam_only = {"game_id": 2, "name": "Noita", "match_percent": 55, "steam_review_desc": "Very Positive",
                      "metacritic_score": -1}
        cls.extra = run("inline", {"sort_by": "match"},
                        {"results": [hades, steam_only], "offset": 20, "total_matches": 22, "has_more": False})
        cls.resolve = run_cards("  var PAYLOAD = " + json.dumps(payload) + ";\n" + _RESOLVE_PROBE)

    def test_the_set_line_is_the_count_and_facet_lozenges(self) -> None:
        self.assertEqual(self.match["head"], [
            ["B", "", "8 of 2090"], ["SPAN", "loz", "taste match"], ["SPAN", "loz", "cozy"],
            ["SPAN", "loz", "unplayed"], ["SPAN", "loz", "≤ 10h"],
        ])
        self.assertNotIn("·", self.match["text"])

    def test_frame_tier_follows_the_match_or_the_critic_sort(self) -> None:
        tiers = [c["cls"].replace(" deal", "") for c in self.match["cards"]]
        self.assertEqual(tiers, ["frame frame-s gc-card tier-good", "frame frame-s gc-card tier-ok",
                                 "frame frame-s gc-card tier-ok", "frame frame-s gc-card tier-ok",
                                 "frame frame-s gc-card tier-ok", "frame frame-s gc-card tier-none",
                                 "frame frame-s gc-card tier-none", "frame frame-s gc-card tier-none"])
        self.assertTrue(all(c["tag"] == "BUTTON" for c in self.match["cards"]))
        # sorted by critics: the Metacritic (OpenCritic fallback) tier; none without one
        critic = [c["cls"].replace(" deal", "").rsplit(" ", 1)[1] for c in self.critic["cards"]]
        self.assertEqual(critic, ["tier-good", "tier-none", "tier-ok", "tier-ok", "tier-ok", "tier-none",
                                  "tier-ok", "tier-good"])

    def test_the_badge_is_the_match_or_the_critic_score(self) -> None:
        cards = self.match["cards"]
        self.assertEqual(cards[0]["badge"], ["73", "%", "Match", "badge tier-good badge-on-art"])
        self.assertEqual(cards[7]["badge"], ["47", "%", "Match", "badge tier-none badge-on-art"])
        critic = self.critic["cards"]
        self.assertEqual(critic[0]["badge"], ["82", None, "Metacritic", "badge tier-good badge-on-art"])
        self.assertIsNone(critic[1]["badge"])                     # no critic score at all

    def test_ribbons_only_for_top_match_and_critics_pick(self) -> None:
        ribbons = [c["ribbon"] for c in self.match["cards"]]
        self.assertEqual(ribbons[0], ["Top match", "ribbon ribbon-s ribbon-art tier-good"])
        self.assertEqual(ribbons[7], ["Critics' pick", "ribbon ribbon-s ribbon-art tier-good"])   # MC 88
        self.assertEqual(ribbons[1:7], [None] * 6)
        # not the first card of the first page: no TOP MATCH; MC 93 >= 85 still picks
        self.assertEqual(self.extra["cards"][0]["ribbon"][0], "Critics' pick")
        self.assertIsNone(self.critic["cards"][0]["ribbon"])     # sorted by critics: MC 82 < 85

    def test_the_plate_sub_line_is_separate_spans(self) -> None:
        cards = self.match["cards"]
        self.assertEqual(cards[0]["title"], "The Spirit and the Mouse")
        self.assertEqual(cards[0]["sub"], [["", "~4.2h", "HowLongToBeat, main story"],
                                           ["loz", "Epic", None], ["card-no", "No. 1", None]])
        self.assertEqual(cards[6]["sub"], [["loz", "Switch 2", None], ["card-no", "No. 7", None]])
        # played hours win over the estimate; numbering is global (offset 20)
        self.assertEqual(self.extra["cards"][0]["sub"], [["", "2.3h", "Your playtime"], ["loz", "Steam", None],
                                                         ["card-no", "No. 21", None]])

    def test_one_chip_row_of_at_most_two_abbreviated_chips(self) -> None:
        cards = self.match["cards"]
        self.assertEqual([c[:3] for c in cards[0]["chips"]],
                         [["MC", "82", "Metacritic 82"], ["OC", "75", "OpenCritic 75"]])
        self.assertEqual(cards[2]["chips"][1][:2], ["Steam", "Very positive"])
        self.assertIn("gc-steam", cards[2]["chips"][1][3])
        self.assertEqual(cards[1]["chips"], [])
        self.assertEqual(cards[1]["nochip"], "No critic scores")
        self.assertTrue(all(len(c["chips"]) <= 2 for c in self.match["cards"] + self.extra["cards"]))
        # three scores, two chips: the Steam phrase is the one left out
        self.assertEqual([c[0] for c in self.extra["cards"][0]["chips"]], ["MC", "OC"])
        # sorted by critics the badge shows Metacritic, so the row skips it
        self.assertEqual([c[0] for c in self.critic["cards"][0]["chips"]], ["OC"])

    def test_matched_tags_are_separate_words(self) -> None:
        self.assertEqual(self.match["cards"][0]["tags"], ["emotional", "narrative", "exploration"])

    def test_the_label_is_what_the_card_shows_with_full_names(self) -> None:
        self.assertEqual(
            self.extra["cards"][0]["label"],
            "Hades II: 100% match, Critics' pick, 2.3h played, Steam, Metacritic 93, OpenCritic 91")
        self.assertEqual(self.extra["cards"][1]["label"], "Noita: 55% match, Steam Very positive")
        self.assertEqual(self.match["cards"][0]["label"],
                         "The Spirit and the Mouse: 73% match, Top match, ~4.2h to beat, Epic Games, "
                         "Metacritic 82, OpenCritic 75")

    def test_cards_deal_in_with_a_capped_stagger_once(self) -> None:
        cards = self.match["cards"]
        self.assertTrue(all(c["deal"] for c in cards))
        self.assertEqual([c["i"] for c in cards], ["0", "1", "2", "3", "4", "5", "5", "5"])
        self.assertEqual(self.match["redrawDeals"], 0)
        # A1: keyed on offset + game ids, not identity — rendering the same
        # page twice is ONE deal; a new page deals again. M2: the set line
        # fades in first, only on a fresh page.
        self.assertEqual(self.match["firstDeals"], len(cards))
        self.assertEqual(self.match["copyDeals"], 0)
        self.assertEqual(self.match["copyFades"], 0)
        self.assertTrue(self.match["headFades"])
        self.assertEqual(self.match["nextDeals"], len(cards))
        # the grain is every frame's first child
        self.assertTrue(all(c["kids"][0] == "grain" for c in cards))

    def test_footer_inline_set_line_in_fullscreen(self) -> None:
        self.assertEqual(self.match["actions"], [["btn act-more", "Show next 8"],
                                                 ["btn primary act-expand", "Open full screen"]])
        self.assertIsNone(self.full["actions"])
        self.assertEqual(self.full["head"][-1], ["BUTTON", "btn act-more", "Show next 8"])

    def test_the_startup_skeleton_resolves_into_the_grid(self) -> None:
        out = self.resolve
        self.assertEqual(out["skeleton"], ["skel skel-grid"])
        self.assertEqual(out["resolving"], ["grid-page", "skel skel-grid leaving"])
        self.assertEqual(out["cardsDealt"], 8)
        self.assertEqual(out["dealAt"], "60ms")
        self.assertEqual(out["resolved"], ["grid-page"])


_DETAIL_PROBE = _DOM_READERS + r"""
  (function () {
    function detail(game) {
      root.textContent = "";
      render(game);
      var stack = root.firstElementChild;
      var frame = stack.children[0];
      var flow = stack.children[1];
      var badge = q(frame, ".badge"), ribbon = q(frame, ".ribbon"), pips = q(frame, ".pips");
      var stats = q(frame, ".stats");
      return {
        frame: [frame.tagName, frame.className, frame.classList.contains("deal")],
        frameKids: kids(frame).map(cls),
        artKids: kids(q(frame, ".art")).map(cls),
        badge: badge ? [txt(q(badge, ".badge-num")), txt(q(badge, ".badge-suffix")), txt(q(badge, ".badge-tag")),
                        badge.className] : null,
        ribbon: ribbon ? [txt(q(ribbon, ".ribbon-text")), txt(q(ribbon, ".ribbon-note")), ribbon.className] : null,
        plate: [txt(q(frame, ".plate-title")), txt(q(frame, ".card-no"))],
        sub: kids(q(frame, ".sub")).map(function (s) { return [s.className, s.textContent]; }),
        stats: kids(stats).map(function (r) {
          if (!r.classList.contains("stat")) return ["chips"].concat(r.children.map(function (c) { return txt(q(c, ".lbl")); }));
          var note = txt(q(r, ".stat-note"));
          var name = txt(q(r, ".stat-label"));
          return [note ? name.slice(0, name.length - note.length) : name, note, txt(q(r, ".stat-val")),
                  r.classList.contains("is-key")];
        }),
        pips: pips ? [pips.children.filter(function (p) { return p.className === "on"; }).length,
                      pips.children.filter(function (p) { return p.className === "half"; }).length,
                      pips.children.length, pips.className] : null,
        flow: kids(flow).map(cls),
        awards: flow.querySelectorAll(".ability").map(txt),
        flavor: txt(q(flow, ".flavor")),
        tags: kids(q(flow, ".tagline")).map(txt),
        eyebrows: flow.querySelectorAll(".section-title").map(txt),
        minis: flow.querySelectorAll(".mini").map(function (m) {
          return [m.className, txt(q(m, ".mini-name"))].concat(m.querySelectorAll(".mini-meta").map(function (l) {
            return l.children.map(txt);
          }));
        }),
        notices: flow.querySelectorAll(".notice").map(txt),
        actions: kids(q(flow, ".dt-actions")).map(function (n) { return [n.className, n.textContent]; }),
        text: stack.textContent,
      };
    }
    console.log(JSON.stringify({ ghost: detail(GHOST), unrated: detail(UNRATED), unknown: detail(UNKNOWN),
                                 bare: detail({ game_id: 9, name: "Mystery", enrichment: { igdb: "no_match" } }) }));
  })();
"""

_DRILL_PROBE = r"""
  (async function () {
    var out = {};
    applyHostContext({ displayMode: "fullscreen", availableDisplayModes: ["inline", "fullscreen"] });
    openDrill({ game_id: 2333, name: "Ghost of Tsushima" }, "inline");
    var bar = root.children[0], holder = root.children[1];
    out.bar = [bar.className, bar.children.map(function (n) { return n.className; }),
               bar.children[0].children.map(function (n) { return n.tagName; }), bar.children[0].textContent];
    out.before = holder.children.map(function (n) { return n.className; });
    answer("tools/call", { structuredContent: GHOST });
    await tick(); await tick();
    out.during = holder.children.map(function (n) { return n.className; });
    out.frame = holder.children[0].children[0].className;
    flushTimers();
    out.after = holder.children.map(function (n) { return n.className; });
    console.log(JSON.stringify(out));
  })();
"""


@unittest.skipUnless(NODE, "node is not installed")
class DetailCardBehaviourTests(unittest.TestCase):
    """The detail card, executed on the saved get_game_detail payload."""

    @classmethod
    def setUpClass(cls) -> None:
        ghost = _sample("detail_ghost_of_tsushima.json")
        unrated = {"game_id": 7, "name": "Before I Forget", "metacritic_score": 88, "playtime_hours": 0,
                   "owned": True, "platforms": [{"platform": "steam", "owned": True, "price_paid": 4.99,
                                                 "price_currency": "EUR", "purchase_source": "humble"}],
                   "features": ["the game awards - best debut indie game - winner",
                                "the game awards - games for impact - winner",
                                "golden joystick awards - best indie game - winner"]}
        unknown = {"game_id": 8, "name": "Abandoned Thing", "completion_status": "abandoned",
                   "my_rating": {"normalized_score": 4.5}, "playtime_hours": None}
        probe = ("  var GHOST = " + json.dumps(ghost) + ";\n  var UNRATED = " + json.dumps(unrated)
                 + ";\n  var UNKNOWN = " + json.dumps(unknown) + ";\n")
        cls.out = run_cards(probe + _DETAIL_PROBE)
        cls.drill = run_cards("  var GHOST = " + json.dumps(ghost) + ";\n" + _DRILL_PROBE)

    def test_the_big_frame_is_tiered_by_his_rating_and_dealt_in(self) -> None:
        ghost = self.out["ghost"]
        self.assertEqual(ghost["frame"], ["ARTICLE", "frame dt-card tier-good deal", True])
        self.assertEqual(ghost["frameKids"], ["grain", "art", "plate", "stats"])
        self.assertEqual(ghost["artKids"], ["cover-wrap", "badge tier-good badge-on-art",
                                            "ribbon ribbon-art tier-good has-note"])
        self.assertEqual(ghost["badge"], ["8", "/10", "Your rating", "badge tier-good badge-on-art"])
        # unrated: the critic tier and the Metacritic badge
        self.assertEqual(self.out["unrated"]["frame"][1], "frame dt-card tier-good deal")
        self.assertEqual(self.out["unrated"]["badge"][:3], ["88", None, "Metacritic"])
        # rated 4.5: bad
        self.assertEqual(self.out["unknown"]["frame"][1], "frame dt-card tier-bad deal")
        self.assertEqual(self.out["bare"]["frame"][1], "frame dt-card tier-none deal")

    def test_the_status_ribbon(self) -> None:
        self.assertEqual(self.out["ghost"]["ribbon"], ["Completed", "82h", "ribbon ribbon-art tier-good has-note"])
        self.assertEqual(self.out["unrated"]["ribbon"], ["Unplayed", None, "ribbon ribbon-art tier-none"])
        self.assertEqual(self.out["unknown"]["ribbon"], ["Abandoned", None, "ribbon ribbon-art tier-bad"])
        self.assertIsNone(self.out["bare"]["ribbon"])              # unknown state: nothing

    def test_the_plate(self) -> None:
        ghost = self.out["ghost"]
        self.assertEqual(ghost["plate"], ["Ghost of Tsushima", "No. 2333"])
        self.assertEqual(ghost["sub"], [["", "Sucker Punch Productions"], ["", "2020"], ["loz", "PS5"]])

    def test_the_stat_rows_and_pips(self) -> None:
        self.assertEqual(self.out["ghost"]["stats"], [
            ["Played", None, "82h", True],
            ["Last", None, "Sep 2022", False],
            ["Length", "main story", "~25h", False],
            ["Paid", "free giveaway", "Free", False],
            ["chips", "Metacritic", "OpenCritic"],
            ["Rating", None, "", False],
        ])
        self.assertEqual(self.out["ghost"]["pips"], [8, 0, 10, "pips tier-good"])
        self.assertEqual(self.out["unknown"]["pips"], [4, 1, 10, "pips tier-bad"])
        self.assertEqual(self.out["unrated"]["stats"],
                         [["Paid", "Humble", "€4.99", False], ["chips", "Metacritic"]])
        self.assertIsNone(self.out["unrated"]["pips"])

    def test_the_ground_in_order(self) -> None:
        ghost = self.out["ghost"]
        # the enrichment notice is bookkeeping: after FROM THE STUDIO, just
        # before the actions row, never in the middle of the copy
        self.assertEqual(ghost["flow"], ["desc", "more-toggle", "flavor flavor-quote", "dt-abil", "tagline",
                                         "panel reel", "eyebrow-sec", "eyebrow-sec", "notice", "actions dt-actions"])
        self.assertTrue(ghost["flavor"].startswith("Absolutely beautiful and well made."))
        self.assertEqual(len(ghost["tags"]), 8)
        self.assertEqual(ghost["eyebrows"], ["Media", "In your library", "From the studio"])

    def test_award_lines(self) -> None:
        self.assertEqual(self.out["ghost"]["awards"], [
            "AwardThe Game Awards — Best Art Direction, winner",
            "AwardNominee for Game of the Year, Best Game Direction, Best Narrative",
        ])
        # The Game Awards only; two wins read as a list
        self.assertEqual(self.out["unrated"]["awards"],
                         ["AwardThe Game Awards — Best Debut Indie Game and Games for Impact, winner"])
        self.assertEqual(self.out["bare"]["awards"], [])

    def test_the_library_and_studio_strips(self) -> None:
        minis = self.out["ghost"]["minis"]
        # B3/B4: the shared mini format — pips when rated (a node, no text),
        # then hours + status ("played" for hours with no status, "unplayed"
        # when the payload says so), then the year
        self.assertEqual(minis[0], ["mini tier-good", "Marvel's Spider-Man", [""], ["50h", "played"], ["2018"]])
        self.assertEqual(minis[2], ["mini tier-none", "Marvel's Spider-Man 2", ["25h", "played"], ["2023"]])
        self.assertEqual(minis[4][2:], [["unplayed"], ["2023"]])
        self.assertEqual(self.out["ghost"]["notices"], [
            "No earlier games picked: Sucker Punch Productions has 30+ games on IGDB",
            "No Steam page for this game, so Steam reviews and ProtonDB are unavailable",
        ])

    def test_actions_and_the_empty_state(self) -> None:
        self.assertEqual(self.out["ghost"]["actions"][0][1], "Full breakdown▾")
        self.assertEqual(self.out["bare"]["notices"], ["No details fetched yet: no IGDB match for this game"])
        for key in ("ghost", "unrated", "unknown", "bare"):
            with self.subTest(card=key):
                self.assertNotIn("·", self.out[key]["text"])

    def test_the_drill_in_resolves_its_skeleton(self) -> None:
        drill = self.drill
        self.assertEqual(drill["bar"], ["topbar", ["btn"], ["svg", "SPAN"], "Back to results"])
        self.assertEqual(drill["before"], ["skel skel-detail"])
        self.assertEqual(drill["during"], ["detail-stack", "skel skel-detail leaving"])
        self.assertEqual(drill["frame"], "frame dt-card tier-good deal")
        self.assertEqual(drill["after"], ["detail-stack"])


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

    def _render_from_json(self, sample: str, *extra: str) -> tuple[dict, str]:
        import sys
        import tempfile
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        payload_path = root / "scripts" / "preview_samples" / sample
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "preview.html"
            # No DATABASE_URL and no tool code: --from-json must not touch a DB.
            subprocess.run(
                [sys.executable, str(root / "scripts" / "preview_game_cards.py"),
                 "--from-json", str(payload_path), "-o", str(out), *extra],
                check=True, capture_output=True, text=True, timeout=60,
                env={"PATH": "/usr/bin:/bin", "DATABASE_URL": str(Path(tmp) / "absent" / "no.db")},
            )
            html = out.read_text()
        return json.loads(payload_path.read_text()), html

    def test_from_json_renders_a_saved_grid_payload(self) -> None:
        payload, html = self._render_from_json("discover_taste_match.json", "--theme", "dark")
        self.assertIn('<div id="root"></div>', html)
        self.assertIn("window.__PREVIEW_DATA__ = ", html)
        self.assertIn(json.dumps(payload["results"][0]["name"]), html)
        self.assertIn("window.__PREVIEW_HOST_CONTEXT__ = ", html)
        self.assertIn('window.__PREVIEW_TOOL_INPUT__ = {"sort_by": "match", "limit": 8};', html)
        self.assertNotIn('"_note"', html)

    def test_from_json_renders_a_saved_detail_payload(self) -> None:
        payload, html = self._render_from_json(
            "detail_ghost_of_tsushima.json", "--display", "fullscreen",
            "--tool-input", '{"game_id": 2333, "media": true}',
        )
        self.assertIn('<div id="root"></div>', html)
        self.assertIn(json.dumps(payload["name"]), html)
        self.assertIn('"displayMode": "fullscreen"', html)
        self.assertIn('window.__PREVIEW_TOOL_INPUT__ = {"game_id": 2333, "media": true};', html)


class DesignSystemTests(unittest.TestCase):
    """Spec 2026-10-03 §1.1–§1.2 as amended by the Binder (2026-10-04 §1.1):
    host theming, the four-size / three-weight type scale, touch, focus."""

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

    def test_the_binder_tokens_are_ported_from_the_reference_sheet(self) -> None:
        # §1.1: every new token, light-dark() where the theme matters, a plain
        # value where it doesn't (docs/specs/assets/binder/gl.css).
        tokens = apps_shared.TOKENS_CSS
        for marker in (
            "--gl-keyline: light-dark(rgba(20, 20, 19, 0.12), rgba(250, 249, 245, 0.14));",
            "--gl-deep: #141413;",
            "--gl-deep-ink: #FAF9F5;",
            "--gl-ribbon-ink: #141413;",
            "--gl-ribbon-good: #7AB948;",
            "--gl-ribbon-ok: #D1A041;",
            "--gl-ribbon-bad: #EE8884;",
            "--gl-ribbon-none: #C2C0B6;",
            "--gl-mono: var(--font-mono, ui-monospace, Menlo, Consolas, monospace);",
            '--gl-serif: ui-serif, Georgia, "Times New Roman", serif;',
            "--gl-title: max(12px, var(--font-heading-lg-size, 20px));",
            "--gl-heavy: 800;",
            # the brushed-metal stops: dark hues, light-token lightness under light
            "--gl-rarity-good-1: light-dark(#265B19, #437426);",
            "--gl-rarity-ok-3: light-dark(#D1A041, #F0D08A);",
            "--gl-rarity-bad-2: light-dark(#A73D39, #EE8884);",
            "--gl-rarity-good: conic-gradient(from 210deg, var(--gl-rarity-good-1), var(--gl-rarity-good-2) 9%,",
            # what every component reads, defaulting to "common"
            "--gl-tier: var(--gl-border-strong);",
            "--gl-tier-text: var(--gl-text-2);",
            "--gl-tier-fill: var(--gl-ribbon-none);",
            "--gl-rarity: none;",
        ):
            self.assertIn(marker, tokens)
        # The grain opacity is a number, which light-dark() cannot carry: it
        # follows the same light / dark selection as the plain fallback.
        self.assertIn(":root { --gl-grain-opacity: 0.035; }", tokens)
        self.assertIn(':root:not([data-theme="light"]) { --gl-grain-opacity: 0.06; }', tokens)
        self.assertIn(':root[data-theme="dark"] { --gl-grain-opacity: 0.06; }', tokens)
        self.assertNotRegex(tokens, r"--gl-grain-opacity: light-dark\(")
        # The tier classes only re-point the four tier tokens.
        for tier, fill in (("good", "good"), ("ok", "ok"), ("bad", "bad")):
            self.assertIn(
                f".tier-{tier} {{ --gl-tier: var(--gl-{tier}-edge); --gl-tier-text: var(--gl-{tier}); "
                f"--gl-tier-fill: var(--gl-ribbon-{fill}); --gl-rarity: var(--gl-rarity-{tier}); }}",
                tokens,
            )
        self.assertIn(
            ".tier-none { --gl-tier: var(--gl-border-strong); --gl-tier-text: var(--gl-text-2); "
            "--gl-tier-fill: var(--gl-ribbon-none); --gl-rarity: none; }",
            tokens,
        )

    def test_no_hex_color_outside_the_token_layer(self) -> None:
        # Raw colors (hex, rgb/rgba, hsl) are allowed in exactly two places:
        # (a) TOKENS_CSS and (b) the cover plate — its name-seeded gradient
        # (coverNode) and its ink text. The plate's ink is built from tokens
        # today, so that half of (b) is an allowance, not a use.
        raw = r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\("
        for name, html in WIDGETS:
            css = widget_css(html).replace(apps_shared.TOKENS_CSS, "")
            css = re.sub(r"[^{}]*\.cover-fallback[^{}]*\{[^{}]*\}", "", css)  # (b) ink
            js = widget_js(html).replace(apps_shared.COVER_NODE_JS, "")       # (b) gradient
            with self.subTest(widget=name):
                self.assertEqual(re.findall(raw, css), [])
                self.assertEqual(re.findall(r"[\"']#[0-9a-fA-F]{3,8}[\"']|rgba?\(|hsla?\(", js), [])
                # the plate's ink is the shared token, in both widgets
                self.assertIn("color: var(--gl-plate-ink);", widget_css(html))
                self.assertIn("text-shadow: 0 1px 3px var(--gl-plate-shadow);", widget_css(html))
        self.assertIn('"linear-gradient(160deg, hsl("', apps_shared.COVER_NODE_JS)
        for token in ("--gl-plate-ink: rgba(255, 255, 255, 0.92);",
                      "--gl-plate-shadow: rgba(0, 0, 0, 0.35);"):
            self.assertIn(token, apps_shared.TOKENS_CSS)

    def test_the_media_stage_is_the_documented_dark_exception(self) -> None:
        tokens = apps_shared.TOKENS_CSS
        self.assertIn("A media stage is dark in both\n       themes by design — it frames video and screenshots", tokens)
        start = tokens.index("Theming exception — the media stage.")
        group = tokens[start:tokens.index("--gl-r-xs:")]
        for token in ("--gl-stage:", "--gl-stage-veil:", "--gl-scrim:", "--gl-on-stage:",
                      "--gl-on-stage-dim:", "--gl-shadow-ink:"):
            self.assertIn(token, group)

    def test_four_sizes_three_weights_nothing_below_12px(self) -> None:
        # Exactly four size tokens exist (cap 12, body 14, h 16, title 20),
        # and nothing else sets a size; three weights (regular 400, strong
        # 600, heavy 800), which the mono numerals share.
        defined = re.findall(r"--gl-([a-z0-9-]+): max\(12px,", apps_shared.TOKENS_CSS)
        self.assertEqual(sorted(defined), ["body", "cap", "h", "title"])
        sizes = {"var(--gl-cap)", "var(--gl-body)", "var(--gl-h)", "var(--gl-title)"}
        weights = {"var(--gl-regular)", "var(--gl-strong)", "var(--gl-heavy)"}
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
        for token, value in (("regular", "var(--font-weight-normal, 400)"),
                             ("strong", "var(--font-weight-semibold, 600)"), ("heavy", "800")):
            self.assertIn(f"--gl-{token}: {value};", apps_shared.TOKENS_CSS)
        # the heavy weight is the Binder's titles, badge numbers and ribbons
        for block, selector in ((apps_shared.PLATE_CSS, ".plate-title"),
                                (apps_shared.BADGE_CSS, ".badge"),
                                (apps_shared.RIBBON_CSS, ".ribbon")):
            rule = block.split(selector + " {", 1)[1].split("}", 1)[0]
            self.assertIn("font-weight: var(--gl-heavy);", rule)

    def test_focus_rings_hit_areas_and_reduced_motion(self) -> None:
        self.assertIn(
            ":focus-visible { outline: 2px solid var(--gl-text); outline-offset: 2px; }",
            apps_shared.A11Y_CSS,
        )
        # gl.css takes the ring 3px out on the card and the pill; the color
        # stays the text color (its 0.4-alpha border-strong misses 3:1).
        self.assertIn(
            ".frame:focus-visible, .btn:focus-visible, a.art:focus-visible { outline-offset: 3px; }",
            apps_shared.A11Y_CSS,
        )
        self.assertNotIn("outline: 2px solid var(--gl-border-strong)", "".join(
            widget_css(html) for _, html in WIDGETS))
        self.assertIn("inset: -4px;", apps_shared.A11Y_CSS)    # 32px on pointer devices
        self.assertIn("inset: -6px;", apps_shared.A11Y_CSS)    # 44px on touch
        self.assertIn("@media (prefers-reduced-motion: reduce)", apps_shared.A11Y_CSS)
        # Binder pills: 40px on a pointer, 44px on touch
        self.assertIn("html.touch .btn, html.touch .disclosure { min-height: 44px; }",
                      apps_shared.CONTROLS_CSS)
        self.assertIn("min-height: 40px;", apps_shared.CONTROLS_CSS)
        self.assertIn("border-radius: var(--gl-r-full);",
                      apps_shared.CONTROLS_CSS.split(".btn, .disclosure {", 1)[1].split("}", 1)[0])
        # the skeleton only pulses when motion is allowed
        skeleton = apps_shared.SKELETON_CSS
        motion = skeleton[skeleton.index("@media (prefers-reduced-motion: no-preference)"):]
        self.assertIn(".sk { animation: skel-pulse 1600ms ease-in-out infinite; }", motion)
        self.assertEqual(skeleton.count("animation"), 1)
        self.assertNotIn("@keyframes", skeleton)               # the pulse is MOTION_CSS's

    def test_rotations_are_the_deal_the_pips_and_the_chevron(self) -> None:
        # The only rotate()s: the deal-in's -1deg, the 45deg pip diamond and
        # the disclosure chevron's 180deg flip. The hover tilt's rotateX/Y
        # live in the fine-pointer hover query only (BinderComponentTests).
        allowed = {"rotate(-1deg)", "rotate(45deg)", "rotate(180deg)"}
        for name, html in WIDGETS:
            css = widget_css(html)
            with self.subTest(widget=name):
                found = set(re.findall(r"rotate\([^)]*\)", css))
                self.assertTrue(found <= allowed, found)
                self.assertEqual(found, allowed)
        self.assertIn("@keyframes deal { from { opacity: 0; transform: translateY(12px) rotate(-1deg); } }",
                      apps_shared.MOTION_CSS)

    def test_numbers_sit_in_tabular_figures(self) -> None:
        for block in (apps_shared.CHIP_CSS, apps_shared.TAG_CSS, apps_shared.PANEL_CSS,
                      apps_shared.BADGE_CSS):
            self.assertIn("font-variant-numeric: tabular-nums;", block)
        # stat values, badge numbers, card numbers and chip figures are mono
        for block in (apps_shared.STATS_CSS, apps_shared.BADGE_CSS, apps_shared.PLATE_CSS,
                      apps_shared.CHIP_CSS, apps_shared.RIBBON_CSS):
            self.assertIn("font-family: var(--gl-mono);", block)


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
        self.assertIn('notice(root, "Cancelled before the result arrived.");', apps_shared.TOOL_RESULT_JS)
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
            'docEl.classList.toggle("touch", deviceCaps.touch);',
            'docEl.classList.toggle("no-hover", deviceCaps.hover === false);',
            'docEl.setAttribute("data-display-mode", String(ctx.displayMode));',
        ):
            self.assertIn(marker, apps_shared.BRIDGE_JS)

    def test_initialize_declares_display_modes_and_applies_the_answer(self) -> None:
        for marker in (
            'appCapabilities: { availableDisplayModes: ["inline", "fullscreen"] },',
            "appInfo: { name: appName,",
            "clientInfo: { name: appName,",
            "applyHostContext(res.hostContext);",
            # initialized only answers a real ui/initialize result
            'if (!res || typeof res !== "object") return;',
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

    def test_every_registry_platform_has_a_short_lozenge_label(self) -> None:
        # Lozenges sit on the plate's one-row sub line beside "No. N", so they
        # carry the short form ("Epic", not "Epic Games"); full names stay for
        # prose and aria-labels. Generated from the same registry walk, so an
        # alias reads as its platform in both maps.
        from gamelib_mcp.platforms_registry import PLATFORMS

        names = {spec.name for spec in PLATFORMS}
        self.assertEqual(names, set(apps_shared._PLATFORM_SHORT_DISPLAY))
        self.assertEqual(
            {name: apps_shared.PLATFORM_SHORT_LABELS[name] for name in names},
            {
                "steam": "Steam", "epic": "Epic", "gog": "GOG",
                "switch2": "Switch 2", "ps5": "PS5", "xbox": "Xbox",
                "itchio": "itch.io", "ea": "EA", "ubisoft": "Ubisoft",
                "other": "Other",
            },
        )
        self.assertEqual(set(apps_shared.PLATFORM_SHORT_LABELS), set(apps_shared.PLATFORM_LABELS))
        for spec in PLATFORMS:
            for alias in spec.aliases:
                with self.subTest(alias=alias):
                    self.assertEqual(apps_shared.PLATFORM_SHORT_LABELS[alias],
                                     apps_shared.PLATFORM_SHORT_LABELS[spec.name])
        self.assertIn("var PLATFORM_SHORT_LABELS = ", apps_shared.LABELS_JS)
        self.assertIn("platform_short: PLATFORM_SHORT_LABELS,", apps_shared.LABELS_JS)

    def test_every_platform_lozenge_uses_the_short_label(self) -> None:
        # grid sub (via the bit's short form), detail plate, eval plate
        self.assertIn('short: label("platform_short", game.suggested_platform) });', apps.GAME_CARDS_HTML)
        self.assertIn('sub.appendChild(el("span", "loz", b.short));', apps.GAME_CARDS_HTML)
        self.assertIn('sub.appendChild(el("span", "loz", label("platform_short", p.platform)));',
                      apps.GAME_CARDS_HTML)
        self.assertIn('sub.appendChild(el("span", "loz", label("platform_short", platform)));',
                      apps_eval.EVAL_CARD_HTML)
        for name, html in WIDGETS:
            with self.subTest(widget=name):
                self.assertNotRegex(html, r'el\("span", "loz", label\("platform",')
                self.assertNotIn('el("span", "loz", b.text)', html)

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

    def test_every_purchase_source_has_an_explicit_label(self) -> None:
        from gamelib_mcp.tools.acquisition import PURCHASE_SOURCES

        self.assertEqual(set(apps_shared._PURCHASE_SOURCE_DISPLAY), set(PURCHASE_SOURCES))
        self.assertEqual(set(apps_shared.PURCHASE_SOURCE_LABELS), set(PURCHASE_SOURCES))
        for source in PURCHASE_SOURCES:
            with self.subTest(source=source):
                self.assertNotEqual(apps_shared.PURCHASE_SOURCE_LABELS[source], source)
        self.assertIn("var PURCHASE_SOURCE_LABELS = ", apps_shared.LABELS_JS)
        self.assertIn("purchase_source: PURCHASE_SOURCE_LABELS,", apps_shared.LABELS_JS)
        self.assertIn('label("purchase_source", own.purchase_source)', apps_eval.EVAL_CARD_HTML)

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
        self.assertIn('function ratingTier(n) { return n >= 7 ? "good" : n >= 5 ? "ok" : "bad"; }', js)
        proton = js_function(js, "function protonTier(tierName)")
        self.assertIn('if (t === "native" || t === "platinum" || t === "gold") return "good";', proton)
        self.assertIn('if (t === "silver") return "ok";', proton)
        self.assertIn('return t === "bronze" || t === "borked" ? "bad" : "none";', proton)

    def test_small_card_chips_are_the_one_score_chip(self) -> None:
        # Spec item 3: similar / studio / lineage / anchors read their numbers
        # through scoreChip — "You 9/10", "Critics 84", "Played 132h",
        # "Unplayed", "Status Completed" — at most three per card.
        # The small cards are minis now (MINI_JS): no "You 9/10" / "Played
        # 132h" / "Critics 84" sticker chips are left; the status words and
        # chipRow remain for the detail card's own chip row.
        js = apps_shared.SCORE_CHIP_JS
        for gone in ('label: "You"', 'label: "Critics"', 'label: "Played"', 'label: "Unplayed"'):
            self.assertNotIn(gone, js)
        for marker in (
            'completed: ["Completed", "good"],',
            'evergreen: ["Evergreen", "good"],',
            'abandoned: ["Abandoned", "bad"],',
            'return s ? scoreChip({ label: "Status", value: s[0], tier: s[1] }) : null;',
            "chips.filter(Boolean).slice(0, 3).forEach(",
        ):
            self.assertIn(marker, js)
        self.assertIn(".tags .chip { padding: 1px 6px;", apps_shared.TAG_CSS)
        self.assertIn("html.touch .tags .chip { min-height: 0; }", apps_shared.TAG_CSS)

    def test_the_chip_is_a_tier_border_and_a_mono_figure(self) -> None:
        # The Binder chip (gl.css §12): 1px border in the tier edge, radius 4,
        # the surface as its ground (no tier fill, and readable on art); a
        # figure ("83", "25h") in the mono numerals in the tier's text color,
        # a phrase ("Very positive") a b.word in the label face.
        css = apps_shared.CHIP_CSS
        chip = css.split("  .chip {\n", 1)[1].split("}", 1)[0]
        for decl in ("border: 1px solid var(--gl-tier);", "border-radius: var(--gl-r-xs);",
                     "background: var(--gl-surface);", "min-height: 24px;"):
            self.assertIn(decl, chip)
        value = css.split("  .chip b {\n", 1)[1].split("}", 1)[0]
        for decl in ("font-family: var(--gl-mono);", "font-size: var(--gl-h);",
                     "color: var(--gl-tier-text);"):
            self.assertIn(decl, value)
        word = css.split("  .chip b.word {\n", 1)[1].split("}", 1)[0]
        self.assertIn("font-family: var(--gl-font);", word)
        self.assertIn("font-weight: var(--gl-strong);", word)
        self.assertIn(".chip .meter-fill { display: block; height: 100%; background: var(--gl-tier-text); }", css)
        # tier color reaches the border and the value only, through the
        # .tier-* custom properties: no per-tier chip rule, no tinted fill
        self.assertNotRegex(css, r"\.chip\.tier-")
        self.assertNotRegex(css, r"var\(--gl-(good|ok|bad)-bg\)")
        js = apps_shared.SCORE_CHIP_JS
        self.assertIn('chip.appendChild(el("b", isFigure(value) ? null : "word", value));', js)
        self.assertIn("return /\\d/.test(t) && !/\\s/.test(t);", js)

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
        # grid: the header line, then 4 cards of cover / title / match bar / chips
        self.assertIn('wrap.appendChild(sk("sk-head"));', js)
        self.assertIn("for (var c = 0; c < 4; c++) {", js)
        start = js.index('if (kind === "grid") {')
        grid = js[start:js.index("return wrap;", start)]
        order = ['sk("sk-cover")', 'sk("sk-line")', 'sk("sk-bar")', "chips(body, 2);"]
        self.assertEqual([grid.index(m) for m in order], sorted(grid.index(m) for m in order))
        # eval: header (cover, title, ribbon), score chips, the facts row, the
        # pitch's two lines; both shapes end on the media stage
        for marker in ('row.appendChild(sk("sk-ribbon"));', 'chips(panel, 3, "sk-facts");',
                       "lines(pitch, 2);", 'row.appendChild(sk("sk-media"));', "mediaBlock(wrap);",
                       # thumbs: the 3x3 grid beside the stage on a wide eval card, else a strip
                       'for (var t = 0; t < (kind === "eval" ? 9 : 6); t++) thumbs.appendChild(sk("sk-shot"));'):
            self.assertIn(marker, js)
        # detail: title + sub line, a chip row, two description lines
        detail = js[js.index("} else {"):js.index("mediaBlock(wrap);")]
        self.assertIn("chips(col, 3);\n      lines(col, 2);", detail)
        self.assertIn('wrap.setAttribute("aria-busy", "true");', js)
        self.assertIn(".sk-media { aspect-ratio: 16 / 9;", apps_shared.SKELETON_CSS)
        self.assertIn(".skel-eval .sk-media-row { display: grid; grid-template-columns: minmax(0, 1fr) 296px; }",
                      apps_shared.SKELETON_CSS)
        # neutral: one panel of cover, three lines and a chip row
        neutral = js[js.index('if (kind === "neutral") {'):js.index('if (kind === "grid") {')]
        self.assertIn('line.appendChild(sk("sk-thumb"));', neutral)
        self.assertIn("lines(text, 3);\n      chips(text, 3);", neutral)
        # Binder look (gl.css §17): every card and panel is a common frame —
        # 1px hairline, the surface-colored frame, a 2px inner keyline —
        # holding inset blocks.
        frame = apps_shared.SKELETON_CSS.split("  .sk-card, .sk-panel {\n", 1)[1].split("}", 1)[0]
        for decl in ("border: var(--gl-frame) solid var(--gl-surface);",
                     "border-radius: var(--gl-r-card);",
                     "box-shadow: 0 0 0 1px var(--gl-border), inset 0 0 0 2px var(--gl-border);"):
            self.assertIn(decl, frame)
        self.assertIn(".sk-card { border-width: 4px; border-radius: var(--gl-r-card-s); padding: 2px; }",
                      apps_shared.SKELETON_CSS)
        self.assertIn(".sk { background: var(--gl-inset); border-radius: var(--gl-r-xs); }",
                      apps_shared.SKELETON_CSS)
        self.assertIn("function skeletonKind()", apps.GAME_CARDS_HTML)
        self.assertIn('if (!lastToolInput) return "neutral";', apps.GAME_CARDS_HTML)
        self.assertIn('function skeletonKind() { return "eval"; }', apps_eval.EVAL_CARD_HTML)

    def test_display_mode_requests_resolve_to_the_granted_mode(self) -> None:
        js = apps_shared.DISPLAY_MODE_JS
        for marker in (
            'request("ui/request-display-mode", { mode: mode })',
            # silence resolves to the mode we are in BY THEN (a context change
            # that arrived meanwhile is the truth)
            "var granted = res && res.mode ? String(res.mode) : currentDisplayMode();",
            "setTimeout(function () { resolve(undefined); }, timeoutMs || 2500);",
            'return Array.isArray(modes) && modes.indexOf("fullscreen") >= 0;',
            'document.documentElement.setAttribute("data-display-mode", granted);',
        ):
            self.assertIn(marker, js)

    def test_model_context_and_message(self) -> None:
        js = apps_shared.MODEL_CONTEXT_JS
        self.assertIn('var params = { content: [{ type: "text", text: String(text) }] };', js)
        self.assertIn("if (structured) params.structuredContent = structured;", js)
        self.assertIn('request("ui/update-model-context", params);', js)
        # ui/message content is a ContentBlock[] (ext-apps spec.types.ts).
        self.assertIn('request("ui/message", { role: "user", content: [{ type: "text", text: String(text) }] });', js)

    def test_disclosure_builds_once_and_reports_state(self) -> None:
        js = apps_shared.DISCLOSURE_JS
        for marker in (
            'btn.setAttribute("aria-expanded", "false");',
            "if (open && !built) { built = true; buildFn(inner); }",
            'btn.setAttribute("aria-expanded", open ? "true" : "false");',
            "reportSize();",
        ):
            self.assertIn(marker, js)
        self.assertIn(".disclosure { width: 100%; }", apps_shared.CONTROLS_CSS)

    def test_notice_names_what_failed_and_where(self) -> None:
        js = apps_shared.NOTICE_JS
        self.assertIn('return it.what + (it.source ? " (" + it.source + ")" : "");', js)
        self.assertIn('text = "Couldn\'t load: " + parts.join(", ");', js)
        # gl.css §18: led by the 16px outlined "!", built with createElementNS
        # (svgEl), the text in its own span.
        self.assertIn('var NOTICE_ICON = [["circle", { cx: 8, cy: 8, r: 6.5 }], '
                      '["path", { d: "M8 4.8v3.8M8 11.1v.1" }]];', js)
        self.assertIn('var icon = iconNode("0 0 16 16", NOTICE_ICON);', js)
        self.assertIn('node.appendChild(el("span", null, text));', js)
        self.assertIn("var node = document.createElementNS(SVG_NS, tag);", js)
        notice = apps_shared.CONTROLS_CSS.split("  .notice {\n", 1)[1].split("}", 1)[0]
        self.assertIn("display: flex;", notice)
        self.assertIn("gap: 6px;", notice)
        self.assertIn("  .notice > svg {\n    width: 16px;\n    height: 16px;", apps_shared.CONTROLS_CSS)
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
