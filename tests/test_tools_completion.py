"""Characterization tests for gamelib_mcp.tools.completion (suggest_completion_status)."""

from datetime import UTC, datetime

from conftest import ToolDBTestCase, add_platform, make_steam_game, seed_game

from gamelib_mcp.tools import completion
from gamelib_mcp.tools.models import CompletionSuggestionsResponse
from gamelib_mcp.tools.platforms import update_game


def _epoch(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp())


class SuggestCompletionStatusTests(ToolDBTestCase):
    async def test_over_hltb_playtime_suggests_completed(self):
        await make_steam_game(
            "Hades", 1, playtime_minutes=62 * 60, hltb_main=38.0
        )
        result = await completion.suggest_completion_status()
        self.assertEqual(result["count"], 1)
        entry = result["suggestions"][0]
        self.assertEqual(entry["name"], "Hades")
        self.assertEqual(entry["suggested_status"], "completed")
        self.assertIn("62h", entry["reason"])
        self.assertIn("38h", entry["reason"])
        self.assertEqual(entry["hltb_main"], 38.0)
        self.assertEqual(entry["playtime_hours"], 62.0)

    async def test_already_classified_games_are_skipped(self):
        gid = await make_steam_game(
            "Hades", 1, playtime_minutes=62 * 60, hltb_main=38.0
        )
        await update_game(game_id=gid, completion_status="completed")
        result = await completion.suggest_completion_status()
        self.assertEqual(result["count"], 0)

    async def test_dormant_underplayed_game_suggests_abandoned(self):
        old_epoch = _epoch(2020, 1, 1)  # far more than 365 days before "now"
        await make_steam_game(
            "Starfield",
            1,
            playtime_minutes=5 * 60,
            hltb_main=40.0,
            rtime_last_played=old_epoch,
        )
        result = await completion.suggest_completion_status()
        self.assertEqual(result["count"], 1)
        entry = result["suggestions"][0]
        self.assertEqual(entry["name"], "Starfield")
        self.assertEqual(entry["suggested_status"], "abandoned")
        self.assertIn("2020-01-01", entry["reason"])
        self.assertEqual(entry["last_played"], "2020-01-01")

    async def test_recently_played_underplayed_game_is_not_suggested(self):
        recent_epoch = int(datetime.now(UTC).timestamp())
        await make_steam_game(
            "Still Playing",
            1,
            playtime_minutes=5 * 60,
            hltb_main=40.0,
            rtime_last_played=recent_epoch,
        )
        result = await completion.suggest_completion_status()
        self.assertEqual(result["count"], 0)

    async def test_no_hltb_or_last_played_produces_no_suggestion(self):
        gid = await seed_game("No Data")
        await add_platform(gid, "gog", playtime_minutes=600)  # no hltb_main set
        result = await completion.suggest_completion_status()
        self.assertEqual(result["count"], 0)

    async def test_zero_playtime_produces_no_suggestion(self):
        await make_steam_game("Untouched", 1, playtime_minutes=0, hltb_main=10.0)
        result = await completion.suggest_completion_status()
        self.assertEqual(result["count"], 0)

    async def test_limit_is_respected(self):
        for i in range(5):
            await make_steam_game(
                f"Completed {i}", 100 + i, playtime_minutes=100 * 60, hltb_main=10.0
            )
        result = await completion.suggest_completion_status(limit=2)
        self.assertEqual(result["count"], 2)
        self.assertEqual(len(result["suggestions"]), 2)

    async def test_farmed_games_excluded(self):
        await make_steam_game(
            "Farmed", 1, playtime_minutes=100 * 60, hltb_main=10.0, is_farmed=1
        )
        result = await completion.suggest_completion_status()
        self.assertEqual(result["count"], 0)

    async def test_huge_playtime_no_hltb_suggests_evergreen_not_completed(self):
        # Rocket-League-shaped: an endless multiplayer game with no
        # HowLongToBeat main-story entry at all, but hundreds of hours played.
        await make_steam_game(
            "Rocket League", 1, playtime_minutes=400 * 60, hltb_main=None
        )
        result = await completion.suggest_completion_status()
        self.assertEqual(result["count"], 1)
        entry = result["suggestions"][0]
        self.assertEqual(entry["name"], "Rocket League")
        self.assertEqual(entry["suggested_status"], "evergreen")
        self.assertNotEqual(entry["suggested_status"], "completed")
        self.assertIn("400h", entry["reason"])
        self.assertIsNone(entry["hltb_main"])
        # The wire schema must accept a null hltb_main on this branch — the
        # tool is annotated with CompletionSuggestionsResponse, so a non-null
        # model field would make the MCP layer reject exactly this result.
        validated = CompletionSuggestionsResponse(**result)
        self.assertIsNone(validated.suggestions[0].hltb_main)

    async def test_moderate_playtime_no_hltb_produces_no_suggestion(self):
        # Not enough playtime to distinguish "endless game" from "barely
        # tried it" when there's no HLTB signal to lean on.
        gid = await seed_game("Mystery Game")
        await add_platform(gid, "steam", playtime_minutes=10 * 60)
        result = await completion.suggest_completion_status()
        self.assertEqual(result["count"], 0)

    async def test_playtime_far_over_hltb_suggests_evergreen_not_completed(self):
        # Tabletop-Simulator-shaped: HLTB main exists but playtime dwarfs it.
        await make_steam_game(
            "Tabletop Simulator", 1, playtime_minutes=150 * 60, hltb_main=8.0
        )
        result = await completion.suggest_completion_status()
        self.assertEqual(result["count"], 1)
        entry = result["suggestions"][0]
        self.assertEqual(entry["suggested_status"], "evergreen")
        self.assertIn("150h", entry["reason"])
        self.assertIn("8h", entry["reason"])

    async def test_evergreen_games_are_never_suggested(self):
        gid = await make_steam_game(
            "Rocket League", 1, playtime_minutes=400 * 60, hltb_main=None
        )
        await update_game(game_id=gid, completion_status="evergreen")
        result = await completion.suggest_completion_status()
        self.assertEqual(result["count"], 0)

    async def test_non_primary_library_item_excluded(self):
        parent_id = await seed_game("Base Game")
        await add_platform(parent_id, "steam", playtime_minutes=100 * 60)
        dlc_id = await seed_game(
            "Base Game: DLC",
            hltb_main=10.0,
            content_type="dlc",
            parent_game_id=parent_id,
            is_primary_library_item=0,
        )
        await add_platform(dlc_id, "steam", playtime_minutes=100 * 60)
        result = await completion.suggest_completion_status()
        names = [entry["name"] for entry in result["suggestions"]]
        self.assertNotIn("Base Game: DLC", names)
