"""Tests for the scrape-config descriptor layer and its validation gate.

Covers: the data-only config vocabulary (host allowlist, selector/regex
compilation, unknown keys, bounds), the versioned DB override lifecycle
(insert → supersede → rollback chain, fail-open loading), and
validate_candidate_config's guard against wrong-but-plausible selectors
(fixture replay + mocked live trials).
"""

import json
import unittest
from unittest.mock import AsyncMock, patch

from conftest import ToolDBTestCase, seed_game
from gamelib_mcp.data import scrape_config as sc
from gamelib_mcp.data import scrape_validate as sv


class ConfigVocabularyTests(unittest.TestCase):
    def test_defaults_round_trip(self):
        for provider in sc.SCRAPE_PROVIDERS:
            defaults = sc.default_config(provider)
            as_dict = sc.config_to_dict(defaults)
            self.assertEqual(sc.config_from_dict(provider, as_dict), defaults)

    def test_partial_override_merges_over_defaults(self):
        config = sc.config_from_dict("backloggd", {"review_card_selector": ".review-tile"})
        self.assertEqual(config.review_card_selector, ".review-tile")
        # Everything else keeps its default.
        self.assertEqual(config.title_selector, ".game-name h3")
        self.assertEqual(config.pagination_cap, 100)

    def test_unknown_key_rejected(self):
        problems = sc.validate_config_dict("backloggd", {"shell_command": "rm -rf /"})
        self.assertTrue(any("unknown config key" in p for p in problems))

    def test_host_change_rejected(self):
        problems = sc.validate_config_dict(
            "backloggd", {"url_template": "https://evil.example.com/u/{user}/reviews"}
        )
        self.assertTrue(any("allowlist" in p for p in problems))

    def test_http_scheme_rejected(self):
        problems = sc.validate_config_dict(
            "backloggd", {"url_template": "http://backloggd.com/u/{user}/reviews"}
        )
        self.assertTrue(any("https" in p for p in problems))

    def test_unexpected_placeholder_rejected(self):
        problems = sc.validate_config_dict(
            "backloggd", {"url_template": "https://backloggd.com/{secret}/reviews"}
        )
        self.assertTrue(any("unexpected placeholders" in p for p in problems))

    def test_missing_required_placeholder_rejected(self):
        # A page template without {page} would re-fetch page 1 until the
        # pagination cap; validation must reject it up front.
        problems = sc.validate_config_dict(
            "backloggd", {"page_url_template": "https://backloggd.com/u/{user}/reviews"}
        )
        self.assertTrue(any("missing required placeholders" in p and "page" in p for p in problems))

        problems = sc.validate_config_dict(
            "steam_reviews",
            {"page_url_template": "https://steamcommunity.com/id/{user}/recommended/"},
        )
        self.assertTrue(any("missing required placeholders" in p for p in problems))

        problems = sc.validate_config_dict(
            "metacritic", {"game_url_template": "https://www.metacritic.com/game/fixed/"}
        )
        self.assertTrue(any("missing required placeholders" in p and "slug" in p for p in problems))

    def test_invalid_selector_rejected(self):
        problems = sc.validate_config_dict("steam_reviews", {"review_box_selector": "[[["})
        self.assertTrue(any("invalid CSS selector" in p for p in problems))

    def test_invalid_regex_rejected(self):
        problems = sc.validate_config_dict("steam_reviews", {"appid_regex": "("})
        self.assertTrue(any("invalid regex" in p for p in problems))

    def test_regex_without_capture_group_rejected(self):
        problems = sc.validate_config_dict("steam_reviews", {"appid_regex": r"/recommended/\d+/"})
        self.assertTrue(any("capture group" in p for p in problems))

    def test_int_bounds_enforced(self):
        problems = sc.validate_config_dict("backloggd", {"fuzzy_cutoff": 5})
        self.assertTrue(any("between" in p for p in problems))

    def test_slug_map_values_validated(self):
        problems = sc.validate_config_dict(
            "metacritic", {"platform_query_values": {"ps5": "PlayStation 5!"}}
        )
        self.assertTrue(any("lowercase slug" in p for p in problems))

    def test_unknown_provider_rejected(self):
        with self.assertRaises(sc.ScrapeConfigError):
            sc.config_from_dict("nosuch", {})


class OverrideLifecycleTests(ToolDBTestCase):
    async def test_load_returns_defaults_when_table_empty(self):
        config = await sc.load_scrape_config("backloggd")
        self.assertEqual(config, sc.default_config("backloggd"))

    async def test_active_override_is_merged_over_defaults(self):
        await sc.insert_scrape_config_version(
            "backloggd",
            {"review_card_selector": ".review-tile"},
            status="active",
            source="ai_heal",
            note="site redesign 2026-07",
        )
        config = await sc.load_scrape_config("backloggd")
        self.assertEqual(config.review_card_selector, ".review-tile")
        self.assertEqual(config.title_selector, ".game-name h3")

    async def test_new_active_supersedes_previous(self):
        v1 = await sc.insert_scrape_config_version(
            "backloggd", {"pagination_cap": 50}, status="active", source="manual"
        )
        v2 = await sc.insert_scrape_config_version(
            "backloggd", {"pagination_cap": 75}, status="active", source="manual"
        )
        self.assertEqual((v1, v2), (1, 2))
        rows = {row["version"]: row["status"] for row in await sc.list_scrape_config_rows("backloggd")}
        self.assertEqual(rows, {1: "superseded", 2: "active"})
        config = await sc.load_scrape_config("backloggd")
        self.assertEqual(config.pagination_cap, 75)

    async def test_rollback_restores_previous_then_defaults(self):
        await sc.insert_scrape_config_version(
            "backloggd", {"pagination_cap": 50}, status="active", source="manual"
        )
        await sc.insert_scrape_config_version(
            "backloggd", {"pagination_cap": 75}, status="active", source="manual"
        )

        restored = await sc.rollback_scrape_config_db("backloggd")
        self.assertEqual(restored, 1)
        self.assertEqual((await sc.load_scrape_config("backloggd")).pagination_cap, 50)

        restored = await sc.rollback_scrape_config_db("backloggd")
        self.assertIsNone(restored)
        self.assertEqual((await sc.load_scrape_config("backloggd")).pagination_cap, 100)

        # Rolling back with nothing active is a harmless no-op.
        self.assertIsNone(await sc.rollback_scrape_config_db("backloggd"))

    async def test_pending_version_needs_activation(self):
        version = await sc.insert_scrape_config_version(
            "backloggd", {"pagination_cap": 42}, status="pending", source="ai_heal"
        )
        self.assertEqual((await sc.load_scrape_config("backloggd")).pagination_cap, 100)

        await sc.activate_scrape_config_version("backloggd", version)
        self.assertEqual((await sc.load_scrape_config("backloggd")).pagination_cap, 42)

        with self.assertRaises(sc.ScrapeConfigError):
            await sc.activate_scrape_config_version("backloggd", version)  # no longer pending

    async def test_malformed_active_row_fails_open_to_defaults(self):
        from gamelib_mcp.data.db import get_db

        async with get_db() as db:
            await db.execute(
                """INSERT INTO scrape_config
                       (provider, version, config_json, status, source, created_at)
                   VALUES ('backloggd', 1, ?, 'active', 'manual', '2026-07-01T00:00:00+00:00')""",
                (json.dumps({"url_template": "https://evil.example.com/{user}"}),),
            )
            await db.commit()

        config = await sc.load_scrape_config("backloggd")
        self.assertEqual(config, sc.default_config("backloggd"))


class ValidationGateTests(ToolDBTestCase):
    async def test_defaults_pass_with_fixture_only(self):
        # No live trial possible (env unset) → the fixture replay is the gate.
        with patch.dict("os.environ", {"BACKLOGGD_USER": ""}):
            report = await sv.validate_candidate_config("backloggd", {})
        self.assertTrue(report["valid"])
        statuses = {c["name"]: c["status"] for c in report["checks"]}
        self.assertEqual(statuses["fixture_replay"], "pass")
        self.assertEqual(statuses["live_trial"], "skipped")

    async def test_structurally_invalid_config_rejected_without_fetching(self):
        report = await sv.validate_candidate_config(
            "backloggd", {"url_template": "https://evil.example.com/{user}"}
        )
        self.assertFalse(report["valid"])
        self.assertEqual(report["checks"][0]["name"], "schema")
        self.assertEqual(report["checks"][0]["status"], "fail")

    async def test_wrong_but_plausible_selector_rejected_by_fixture(self):
        # `.review-body .card-text` for the title selector parses *something*
        # structurally valid but semantically wrong; with no live trial the
        # fixture replay must catch it.
        with patch.dict("os.environ", {"BACKLOGGD_USER": ""}):
            report = await sv.validate_candidate_config(
                "backloggd",
                {
                    "title_selector": ".review-body .card-text",
                    "title_container_class": "review-card",
                    "title_inner_selector": ".card-text",
                },
            )
        self.assertFalse(report["valid"])
        statuses = {c["name"]: c["status"] for c in report["checks"]}
        self.assertEqual(statuses["fixture_replay"], "fail")

    async def test_live_title_overlap_rejects_wrong_element(self):
        # Live page parses, but the extracted "titles" don't look like the
        # user's library at all → the overlap sanity check fails the config.
        await seed_game("Hades")
        await seed_game("Celeste")

        live_rows = [
            {"title": "Sign in", "score": 5.0, "text": ""},
            {"title": "Popular this week", "score": 4.0, "text": ""},
        ]
        fixture_html = (sv.FIXTURES_DIR / "backloggd_reviews.html").read_text(encoding="utf-8")
        with (
            patch.dict("os.environ", {"BACKLOGGD_USER": "someone"}),
            patch.object(sv, "_fetch_text", AsyncMock(return_value=fixture_html)),
            patch("gamelib_mcp.data.backloggd._parse_page", return_value=live_rows),
        ):
            report = await sv.validate_candidate_config("backloggd", {})

        self.assertFalse(report["valid"])
        statuses = {c["name"]: c["status"] for c in report["checks"]}
        self.assertEqual(statuses["title_overlap"], "fail")

    async def test_live_pass_with_stale_fixture_warns_but_validates(self):
        # Site changed: fixture replay fails, but the live trial passes and
        # titles overlap the library → the heal is accepted with a warning.
        await seed_game("Hades")
        await seed_game("Celeste")

        live_rows = [
            {"title": "Hades", "score": 4.5, "text": ""},
            {"title": "Celeste", "score": 5.0, "text": ""},
        ]
        with (
            patch.dict("os.environ", {"BACKLOGGD_USER": "someone"}),
            patch.object(sv, "_fetch_text", AsyncMock(return_value="<html></html>")),
            patch("gamelib_mcp.data.backloggd._parse_page", side_effect=[[], live_rows]),
        ):
            # First _parse_page call is the fixture replay (returns []), the
            # second is the live trial.
            report = await sv.validate_candidate_config("backloggd", {})

        self.assertTrue(report["valid"])
        statuses = {c["name"]: c["status"] for c in report["checks"]}
        self.assertEqual(statuses["fixture_replay"], "warn")
        self.assertEqual(statuses["live_trial"], "pass")
        self.assertEqual(statuses["title_overlap"], "pass")

    async def test_dekudeals_defaults_pass_with_fixture_only(self):
        # No live trial possible (env unset) → the fixture replay is the gate,
        # covering both the JSON wishlist-item parse and the HTML price parse.
        with patch.dict("os.environ", {"DEKUDEALS_WISHLIST_URL": ""}):
            report = await sv.validate_candidate_config("dekudeals", {})
        self.assertTrue(report["valid"])
        statuses = {c["name"]: c["status"] for c in report["checks"]}
        self.assertEqual(statuses["fixture_replay"], "pass")
        self.assertEqual(statuses["live_trial"], "skipped")

    async def test_dekudeals_wrong_price_selector_rejected_by_fixture(self):
        # Pointing price_selector at the struck-through regular-price element
        # parses *something* structurally valid but wrong (and misses items
        # with no discount entirely); with no live trial the fixture replay
        # over dekudeals_wishlist_page.html must catch it.
        with patch.dict("os.environ", {"DEKUDEALS_WISHLIST_URL": ""}):
            report = await sv.validate_candidate_config(
                "dekudeals", {"price_selector": ".text-tight s"}
            )
        self.assertFalse(report["valid"])
        statuses = {c["name"]: c["status"] for c in report["checks"]}
        self.assertEqual(statuses["fixture_replay"], "fail")

    async def test_metacritic_live_trial_compares_against_stored_score(self):
        game_id = await seed_game("Example Game")
        from gamelib_mcp.data.db import get_db, upsert_game_platform_enrichment

        async with get_db() as db:
            cursor = await db.execute(
                "INSERT INTO game_platforms (game_id, platform, owned) VALUES (?, 'ps5', 1)",
                (game_id,),
            )
            gp_id = cursor.lastrowid
            await db.commit()
        await upsert_game_platform_enrichment(gp_id, metacritic_score=90)

        page = (sv.FIXTURES_DIR / "metacritic_game.html").read_text(encoding="utf-8")
        with patch.object(sv, "_fetch_text", AsyncMock(return_value=page)):
            report = await sv.validate_candidate_config("metacritic", {})
        # Fixture page scores 88, stored 90 → within tolerance → pass.
        self.assertTrue(report["valid"])

        async with get_db() as db:
            await db.execute(
                "UPDATE game_platform_enrichment SET metacritic_score = 20 WHERE game_platform_id = ?",
                (gp_id,),
            )
            await db.commit()
        with patch.object(sv, "_fetch_text", AsyncMock(return_value=page)):
            report = await sv.validate_candidate_config("metacritic", {})
        # 88 vs stored 20 → outside tolerance → the config is rejected.
        self.assertFalse(report["valid"])


class ScrapeAdminToolTests(ToolDBTestCase):
    async def test_get_scrape_config_reports_defaults(self):
        from gamelib_mcp.tools import scrape_admin

        result = await scrape_admin.get_scrape_config("backloggd")
        self.assertTrue(result["on_defaults"])
        self.assertIsNone(result["active_override"])
        self.assertEqual(result["effective_config"], result["defaults"])
        self.assertEqual(result["history"], [])

    async def test_unknown_provider_raises_tool_error(self):
        from fastmcp.exceptions import ToolError

        from gamelib_mcp.tools import scrape_admin

        with self.assertRaises(ToolError):
            await scrape_admin.get_scrape_config("opencritic")

    async def test_propose_rejects_invalid_and_persists_nothing(self):
        from gamelib_mcp.tools import scrape_admin

        result = await scrape_admin.propose_scrape_config(
            "backloggd", {"url_template": "https://evil.example.com/{user}"}
        )
        self.assertFalse(result["applied"])
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(await sc.list_scrape_config_rows("backloggd"), [])

    async def test_propose_auto_applies_validated_config(self):
        from gamelib_mcp.tools import scrape_admin

        with patch.dict("os.environ", {"BACKLOGGD_USER": "", "SCRAPE_HEAL_REQUIRE_APPROVAL": ""}):
            result = await scrape_admin.propose_scrape_config(
                "backloggd", {"pagination_cap": 50}, note="tune paging"
            )
        self.assertTrue(result["applied"])
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["version"], 1)
        self.assertEqual((await sc.load_scrape_config("backloggd")).pagination_cap, 50)

        stored = (await sc.list_scrape_config_rows("backloggd"))[0]
        self.assertEqual(stored["source"], "ai_heal")
        self.assertEqual(stored["note"], "tune paging")
        self.assertTrue(json.loads(stored["validation_report"])["valid"])

    async def test_require_approval_lands_pending_then_approve(self):
        from gamelib_mcp.tools import scrape_admin

        with patch.dict(
            "os.environ", {"BACKLOGGD_USER": "", "SCRAPE_HEAL_REQUIRE_APPROVAL": "1"}
        ):
            result = await scrape_admin.propose_scrape_config("backloggd", {"pagination_cap": 50})
        self.assertFalse(result["applied"])
        self.assertEqual(result["status"], "pending")
        self.assertEqual((await sc.load_scrape_config("backloggd")).pagination_cap, 100)

        approved = await scrape_admin.approve_scrape_config("backloggd", result["version"])
        self.assertEqual(approved["effective_config"]["pagination_cap"], 50)

    async def test_rollback_tool_returns_to_defaults(self):
        from gamelib_mcp.tools import scrape_admin

        with patch.dict("os.environ", {"BACKLOGGD_USER": "", "SCRAPE_HEAL_REQUIRE_APPROVAL": ""}):
            await scrape_admin.propose_scrape_config("backloggd", {"pagination_cap": 50})

        result = await scrape_admin.rollback_scrape_config("backloggd")
        self.assertTrue(result["on_defaults"])
        self.assertIsNone(result["restored_version"])
        self.assertEqual(result["effective_config"]["pagination_cap"], 100)

    async def test_status_payload_reports_drift(self):
        from gamelib_mcp.tools import scrape_admin

        payload = await scrape_admin.scrape_config_status_payload()
        self.assertEqual(payload["active_backend"], "defaults")
        self.assertTrue(all(info["on_defaults"] for info in payload["providers"].values()))

        with patch.dict("os.environ", {"BACKLOGGD_USER": "", "SCRAPE_HEAL_REQUIRE_APPROVAL": ""}):
            await scrape_admin.propose_scrape_config("backloggd", {"pagination_cap": 50})
        payload = await scrape_admin.scrape_config_status_payload()
        self.assertEqual(payload["active_backend"], "scrape_config")
        self.assertIn("backloggd", payload["summary"])
        self.assertFalse(payload["providers"]["backloggd"]["on_defaults"])


class HostAllowlistFetchTests(unittest.IsolatedAsyncioTestCase):
    """fetch_allowlisted keeps the ALLOWED_HOSTS frozen host a true network
    boundary — a redirect off the allowlist is blocked before the request is
    ever sent, closing the follow_redirects SSRF gap."""

    class _Resp:
        def __init__(self, *, is_redirect=False, location=None, text=""):
            self.is_redirect = is_redirect
            self.headers = {"location": location} if location else {}
            self.text = text

    async def test_same_host_redirect_is_followed(self):
        requested: list[str] = []

        class _Client:
            async def get(_self, url, follow_redirects=False, **_kw):
                requested.append(url)
                if url == "https://backloggd.com/a":
                    return HostAllowlistFetchTests._Resp(
                        is_redirect=True, location="https://www.backloggd.com/b"
                    )
                return HostAllowlistFetchTests._Resp(text="ok")

        resp = await sc.fetch_allowlisted(
            _Client(), "https://backloggd.com/a", provider="backloggd"
        )
        self.assertEqual(resp.text, "ok")
        # Both hops stayed on the allowlist, so both were fetched.
        self.assertEqual(requested, ["https://backloggd.com/a", "https://www.backloggd.com/b"])

    async def test_offsite_redirect_blocked_before_request(self):
        requested: list[str] = []

        class _Client:
            async def get(_self, url, follow_redirects=False, **_kw):
                requested.append(url)
                return HostAllowlistFetchTests._Resp(
                    is_redirect=True, location="https://evil.example.com/steal"
                )

        with self.assertRaises(sc.DisallowedHostError):
            await sc.fetch_allowlisted(
                _Client(), "https://backloggd.com/a", provider="backloggd"
            )
        # The off-allowlist redirect target was never contacted.
        self.assertEqual(requested, ["https://backloggd.com/a"])

    async def test_initial_offsite_url_never_fetched(self):
        class _Client:
            async def get(_self, *_a, **_kw):
                raise AssertionError("off-allowlist URL must not be fetched")

        with self.assertRaises(sc.DisallowedHostError):
            await sc.fetch_allowlisted(
                _Client(), "https://evil.example.com/", provider="backloggd"
            )

    async def test_redirect_loop_is_capped(self):
        class _Client:
            async def get(_self, url, follow_redirects=False, **_kw):
                # Infinite same-host redirect chain.
                return HostAllowlistFetchTests._Resp(
                    is_redirect=True, location="https://backloggd.com/loop"
                )

        with self.assertRaises(sc.DisallowedHostError):
            await sc.fetch_allowlisted(
                _Client(), "https://backloggd.com/loop", provider="backloggd", max_redirects=3
            )


class OverlapFloorTests(ToolDBTestCase):
    """The live-trial overlap gate requires both a majority fraction and an
    absolute matched-row floor, so a mostly-wrong page can't clear a low bar."""

    async def test_minority_overlap_rejected(self):
        # 2 of 5 scraped titles match the library (40%) — under the old 25%
        # threshold this passed; it must now fail on the fraction floor.
        for name in ("Hades", "Celeste"):
            await seed_game(name)

        live_rows = [
            {"title": "Hades", "score": 4.5, "text": ""},
            {"title": "Celeste", "score": 5.0, "text": ""},
            {"title": "Trending Now", "score": 4.0, "text": ""},
            {"title": "Sign in", "score": 3.0, "text": ""},
            {"title": "Newsletter", "score": 2.0, "text": ""},
        ]
        with (
            patch.dict("os.environ", {"BACKLOGGD_USER": "someone"}),
            patch.object(sv, "_fetch_text", AsyncMock(return_value="<html></html>")),
            patch("gamelib_mcp.data.backloggd._parse_page", return_value=live_rows),
        ):
            report = await sv.validate_candidate_config("backloggd", {})

        self.assertFalse(report["valid"])
        statuses = {c["name"]: c["status"] for c in report["checks"]}
        self.assertEqual(statuses["title_overlap"], "fail")

    async def test_absolute_floor_blocks_high_fraction_low_count(self):
        # 2 of 3 scraped rows match (67% — clears the fraction bar) but only 2
        # matched, below the absolute floor of 3. This is the poisoned-minority
        # case: a mostly-real page with one injected row on a small sample must
        # not validate a selector change on fraction alone.
        for name in ("Hades", "Celeste"):
            await seed_game(name)

        live_rows = [
            {"title": "Hades", "score": 4.5, "text": ""},
            {"title": "Celeste", "score": 5.0, "text": ""},
            {"title": "Sponsored: Buy Now", "score": 4.0, "text": ""},
        ]
        with (
            patch.dict("os.environ", {"BACKLOGGD_USER": "someone"}),
            patch.object(sv, "_fetch_text", AsyncMock(return_value="<html></html>")),
            patch("gamelib_mcp.data.backloggd._parse_page", return_value=live_rows),
        ):
            report = await sv.validate_candidate_config("backloggd", {})

        self.assertFalse(report["valid"])
        statuses = {c["name"]: c["status"] for c in report["checks"]}
        self.assertEqual(statuses["title_overlap"], "fail")


if __name__ == "__main__":
    unittest.main()
