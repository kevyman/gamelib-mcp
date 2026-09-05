"""gamelib_mcp.deal_alerts: triggers, debounce, delivery, and its never-fail rule.

Patches ``get_wishlist_deals`` and ``httpx.AsyncClient`` as imported INTO
``gamelib_mcp.deal_alerts`` (the repo's patching convention), so the pricing
path and the network are both out of scope here — what is under test is which
deals speak, which ones stay quiet, and when the debounce stamp is written.
"""

import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

from conftest import ToolDBTestCase, seed_game

from gamelib_mcp import deal_alerts
from gamelib_mcp.data import db as db_module

_WEBHOOK = "https://example.invalid/webhook"
_DISCORD_WEBHOOK = "https://discord.com/api/webhooks/123/test-token"


def _deal(game_id: int, name: str, **overrides) -> dict:
    deal = {
        "game_id": game_id,
        "name": name,
        "platform": "steam",
        "shop": "Steam",
        "price": 19.99,
        "regular_price": 39.99,
        "cut_pct": 50,
        "currency": "EUR",
        "deal_url": "https://store/1",
        "history_low": 12.49,
        "at_history_low": False,
        "alternatives": [],
    }
    deal.update(overrides)
    return deal


def _client(status_codes: list[int]) -> Mock:
    """An httpx.AsyncClient stand-in answering with the given status codes."""
    client = AsyncMock()
    client.post.side_effect = [Mock(status_code=code) for code in status_codes]
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    return client


class DealAlertsConfiguredTests(unittest.TestCase):
    def test_unset_or_blank_is_unconfigured(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEAL_ALERT_WEBHOOK_URL", None)
            self.assertFalse(deal_alerts.is_deal_alerts_configured())
        with patch.dict(os.environ, {"DEAL_ALERT_WEBHOOK_URL": "   "}):
            self.assertFalse(deal_alerts.is_deal_alerts_configured())

    def test_a_url_enables_it(self) -> None:
        with patch.dict(os.environ, {"DEAL_ALERT_WEBHOOK_URL": _WEBHOOK}):
            self.assertTrue(deal_alerts.is_deal_alerts_configured())


class TriggerRulesTests(unittest.TestCase):
    def test_target_reached_wins_over_the_all_time_low(self) -> None:
        deal = _deal(1, "Hades", below_assessed_target=True, at_history_low=True)
        self.assertEqual(
            deal_alerts._trigger_for(deal), ("target:19.99", "reached your target price")
        )

    def test_all_time_low_with_a_discount_triggers(self) -> None:
        deal = _deal(1, "Hades", price=12.49, at_history_low=True, cut_pct=70)
        self.assertEqual(
            deal_alerts._trigger_for(deal), ("low:12.49", "at its all-time low")
        )

    def test_all_time_low_at_full_price_is_quiet(self) -> None:
        # A game that has never been discounted sits at its own all-time low
        # forever; alerting on that is a notification about nothing.
        deal = _deal(1, "Never On Sale", at_history_low=True, cut_pct=0)
        self.assertIsNone(deal_alerts._trigger_for(deal))

    def test_an_ordinary_discount_is_quiet(self) -> None:
        self.assertIsNone(deal_alerts._trigger_for(_deal(1, "Just A Sale")))


class MessageFormatTests(unittest.TestCase):
    def test_switch2_shop_has_a_display_name(self) -> None:
        line = deal_alerts._format_discord_line(
            _deal(1, "Hades", platform="switch2", shop="dekudeals"),
            "reached your target price",
        )
        self.assertIn("Switch 2 · DekuDeals · 🎯 Target reached", line)

    def test_discord_renderer_preserves_complete_rows_and_page_metadata(self) -> None:
        row = "**€3.69** **[Gunbrella](<https://store/1>)**\n-# Steam · All-time low"
        chunk = [(1, "low:3.69", row)]
        self.assertEqual(
            deal_alerts._render_discord(chunk, page=1, pages=1, total=1),
            f"## 🏷️ Wishlist deals\n-# 1 deal\n\n{row}",
        )
        self.assertEqual(
            deal_alerts._render_discord(chunk, page=2, pages=3, total=8),
            f"## 🏷️ Wishlist deals\n-# 8 deals · 2/3\n\n{row}",
        )

    def test_discord_renderer_checks_full_page_size_including_emoji_and_header(self) -> None:
        # Row alone fits; adding the header exceeds 1,900 UTF-16 units.
        # Counting Python characters instead would miss this overflow.
        with self.assertRaisesRegex(ValueError, "Discord deal page exceeds"):
            deal_alerts._render_discord(
                [(1, "low:3.69", "🎮" * 940)], page=1, pages=1, total=1,
            )

    def test_line_names_the_price_platform_reason_expiry_and_url(self) -> None:
        line = deal_alerts._format_line(
            _deal(1, "Hollow Knight", price=7.49, cut_pct=50,
                  deal_ends_at="2026-09-15T17:00:00+00:00"),
            "at its all-time low",
        )
        self.assertEqual(
            line,
            "Hollow Knight — 7.49 EUR on steam (Steam) — -50% — "
            "at its all-time low — ends 2026-09-15 — https://store/1",
        )

    def test_expiry_is_omitted_when_unknown(self) -> None:
        line = deal_alerts._format_line(_deal(1, "Open Ended"), "reached your target price")
        self.assertNotIn("ends", line)

    def test_chunks_stay_under_the_size_limit(self) -> None:
        triggered = [
            (i, f"low:{i}.00", f"Game {i} — " + "x" * 200) for i in range(40)
        ]
        chunks = deal_alerts._chunk(triggered)
        self.assertGreater(len(chunks), 1)
        # Nothing is lost in the packing, and every rendered chunk fits.
        self.assertEqual(
            [item for chunk in chunks for item in chunk], triggered
        )
        for chunk in chunks:
            self.assertLessEqual(len(deal_alerts._render(chunk)), 1_900)


class RunDealAlertsTests(ToolDBTestCase):
    async def _run(
        self, deals: list[dict], *, status_codes=(204,), webhook=_WEBHOOK
    ) -> tuple[dict, Mock]:
        client = _client(list(status_codes))
        with (
            patch.dict(os.environ, {"DEAL_ALERT_WEBHOOK_URL": webhook}),
            patch(
                "gamelib_mcp.deal_alerts.get_wishlist_deals",
                AsyncMock(return_value={"deals": deals}),
            ),
            patch("gamelib_mcp.deal_alerts.httpx.AsyncClient", return_value=client),
        ):
            result = await deal_alerts.run_deal_alerts()
        return result, client

    async def _wishlisted(self, name: str) -> int:
        game_id = await seed_game(name)
        await db_module.upsert_wishlist_entry(game_id, "steam", source="steam")
        return game_id

    async def _alert_state(self, game_id: int) -> dict:
        return (await db_module.load_wishlist_alert_state([game_id])).get(game_id) or {}

    async def test_malformed_webhook_still_checks_deals_and_counts_failed_delivery(self):
        game_id = await self._wishlisted("Hades")
        result, client = await self._run(
            [_deal(game_id, "Hades", below_assessed_target=True)],
            webhook="https://[broken/webhook", status_codes=[400],
        )
        self.assertEqual(result, {
            "configured": True, "checked": 1, "triggered": 1, "sent": 0, "failed": 1,
        })
        body = client.post.call_args.kwargs["json"]
        self.assertEqual(body["content"], body["text"])
        self.assertEqual(await self._alert_state(game_id), {})

    async def test_oversized_discord_page_is_not_sent_or_stamped_and_next_page_sends(self):
        game_ids = [await self._wishlisted(name) for name in ("Hades", "Gunbrella")]
        # Simulate a future formatter change outgrowing the row budget.
        with patch(
            "gamelib_mcp.deal_alerts._format_discord_line",
            side_effect=["🎮" * 940, "**€3.69** **Gunbrella**"],
        ):
            result, client = await self._run(
                [_deal(gid, name, below_assessed_target=True)
                 for gid, name in zip(game_ids, ("Hades", "Gunbrella"), strict=True)],
                webhook=_DISCORD_WEBHOOK, status_codes=[204, 204],
            )
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertNotIn("error", result)
        client.post.assert_awaited_once()
        self.assertIn("Gunbrella", client.post.call_args.kwargs["json"]["content"])
        self.assertEqual(await self._alert_state(game_ids[0]), {})
        self.assertTrue(await self._alert_state(game_ids[1]))

    async def test_discord_digest_has_price_hierarchy_and_no_previews_or_pings(self):
        game_id = await self._wishlisted("Gunbrella")
        result, client = await self._run([
            _deal(game_id, "Gunbrella", price=3.69, cut_pct=75, shop="Humble Store",
                  at_history_low=True, deal_ends_at="2026-09-07T17:00:00Z"),
        ], webhook=_DISCORD_WEBHOOK)

        body = client.post.call_args.kwargs["json"]
        self.assertEqual(body["content"],
            "## 🏷️ Wishlist deals\n-# 1 deal\n\n"
            "**€3.69** `−75%` **[Gunbrella](<https://store/1>)**\n"
            "-# Steam · Humble Store · All-time low · Ends 7 Sep")
        self.assertEqual(body["flags"], 4)
        self.assertEqual(body["allowed_mentions"], {"parse": []})
        self.assertNotIn("embeds", body)
        self.assertNotIn("text", body)
        self.assertEqual(result["sent"], 1)
        self.assertEqual((await self._alert_state(game_id))["last_alert_key"], "low:3.69")

    async def test_discord_pages_preserve_links_and_only_stamp_delivered_games(self):
        deals = [
            _deal(await self._wishlisted(f"Bargain {i}"), f"Bargain {i}",
                  below_assessed_target=True, deal_url=f"https://store/{i}/" + "x" * 500)
            for i in range(8)
        ]
        result, client = await self._run(
            deals, webhook=_DISCORD_WEBHOOK, status_codes=[204, 500] + [204] * 8,
        )
        calls = client.post.call_args_list
        self.assertGreater(len(calls), 1)
        delivered = 0
        all_content = "\n".join(call.kwargs["json"]["content"] for call in calls)
        for i, call in enumerate(calls):
            content = call.kwargs["json"]["content"]
            self.assertLessEqual(len(content.encode("utf-16-le")) // 2, 1900)
            self.assertIn(f"8 deals · {i + 1}/{len(calls)}", content)
            for deal in deals:
                if f"[{deal['name']}]" in content:
                    self.assertIn(f"(<{deal['deal_url']}>)", content)
                    state = await self._alert_state(deal["game_id"])
                    self.assertEqual(bool(state), i != 1)
                    delivered += i != 1
        for deal in deals:
            self.assertEqual(all_content.count(f"[{deal['name']}]"), 1)
        self.assertEqual(result["sent"], delivered)
        self.assertEqual(result["failed"], 8 - delivered)

    async def test_discord_escapes_titles_and_handles_missing_optional_details(self):
        name = "A **bold** [title]\n@everyone"
        game_id = await self._wishlisted(name)
        _, client = await self._run([
            _deal(game_id, name, below_assessed_target=True, deal_url=None,
                  shop="Steam", currency="CAD", cut_pct=0),
        ], webhook=_DISCORD_WEBHOOK)
        content = client.post.call_args.kwargs["json"]["content"]
        self.assertIn(r"A \*\*bold\*\* \[title\] @everyone", content)
        self.assertIn("**19.99 CAD**", content)
        self.assertIn("-# Steam · 🎯 Target reached", content)
        self.assertNotIn("Steam · Steam", content)
        self.assertNotIn("Ends", content)
        self.assertNotIn("None", content)

    async def test_discord_pathological_title_and_url_do_not_break_a_message(self):
        game_id = await self._wishlisted("Long title")
        _, client = await self._run([
            _deal(game_id, "🎮*" * 2000, below_assessed_target=True,
                  deal_url="https://store/" + "x" * 3000),
        ], webhook=_DISCORD_WEBHOOK)
        content = client.post.call_args.kwargs["json"]["content"]
        self.assertLessEqual(len(content.encode("utf-16-le")) // 2, 1900)
        self.assertIn("**€19.99**", content)
        self.assertIn("Target reached", content)
        self.assertNotIn("https://", content)
        self.assertNotIn("[", content)

    async def test_unconfigured_is_a_no_op(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=False),
            patch("gamelib_mcp.deal_alerts.get_wishlist_deals", AsyncMock()) as fetch,
        ):
            os.environ.pop("DEAL_ALERT_WEBHOOK_URL", None)
            result = await deal_alerts.run_deal_alerts()

        fetch.assert_not_awaited()
        self.assertEqual(
            result,
            {"configured": False, "checked": 0, "triggered": 0, "sent": 0, "failed": 0},
        )

    async def test_target_price_alert_is_sent_and_stamped(self) -> None:
        game_id = await self._wishlisted("Hades")
        result, client = await self._run(
            [_deal(game_id, "Hades", below_assessed_target=True)]
        )

        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["triggered"], 1)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["failed"], 0)

        body = client.post.call_args.kwargs["json"]
        # Discord (`content`) and Slack (`text`) carry the same text.
        self.assertEqual(body["content"], body["text"])
        self.assertIn("Hades", body["content"])
        self.assertIn("reached your target price", body["content"])

        self.assertEqual((await self._alert_state(game_id))["last_alert_key"], "target:19.99")

    async def test_history_low_alert_is_sent(self) -> None:
        game_id = await self._wishlisted("Hollow Knight")
        result, client = await self._run(
            [_deal(game_id, "Hollow Knight", price=7.49, at_history_low=True, cut_pct=50)]
        )

        self.assertEqual(result["sent"], 1)
        self.assertIn("all-time low", client.post.call_args.kwargs["json"]["content"])
        self.assertEqual((await self._alert_state(game_id))["last_alert_key"], "low:7.49")

    async def test_neither_trigger_sends_nothing(self) -> None:
        game_id = await self._wishlisted("Just A Sale")
        result, client = await self._run([_deal(game_id, "Just A Sale")], status_codes=[])

        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["triggered"], 0)
        self.assertEqual(result["sent"], 0)
        client.post.assert_not_awaited()

    async def test_the_same_event_is_not_repeated(self) -> None:
        game_id = await self._wishlisted("Hades")
        deal = _deal(game_id, "Hades", below_assessed_target=True)

        first, _ = await self._run([deal])
        self.assertEqual(first["sent"], 1)

        second, client = await self._run([deal], status_codes=[])
        self.assertEqual(second["checked"], 1)
        self.assertEqual(second["triggered"], 0)
        client.post.assert_not_awaited()

    async def test_a_further_price_drop_alerts_again(self) -> None:
        game_id = await self._wishlisted("Hades")
        await self._run([_deal(game_id, "Hades", below_assessed_target=True)])

        cheaper = _deal(game_id, "Hades", price=14.99, below_assessed_target=True)
        result, client = await self._run([cheaper])

        self.assertEqual(result["triggered"], 1)
        self.assertEqual(result["sent"], 1)
        client.post.assert_awaited_once()
        self.assertEqual((await self._alert_state(game_id))["last_alert_key"], "target:14.99")

    async def test_a_failed_post_does_not_stamp(self) -> None:
        game_id = await self._wishlisted("Hades")
        result, _ = await self._run(
            [_deal(game_id, "Hades", below_assessed_target=True)], status_codes=[500]
        )

        self.assertEqual(result["triggered"], 1)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["failed"], 1)
        # Unstamped, so the next run retries: a missed drop is the failure
        # mode this feature exists to prevent.
        self.assertEqual(await self._alert_state(game_id), {})

    async def test_a_raising_client_is_swallowed_and_reported(self) -> None:
        game_id = await self._wishlisted("Hades")
        client = _client([204])
        client.post.side_effect = RuntimeError("connection reset")

        with (
            patch.dict(os.environ, {"DEAL_ALERT_WEBHOOK_URL": _WEBHOOK}),
            patch(
                "gamelib_mcp.deal_alerts.get_wishlist_deals",
                AsyncMock(return_value={
                    "deals": [_deal(game_id, "Hades", below_assessed_target=True)]
                }),
            ),
            patch("gamelib_mcp.deal_alerts.httpx.AsyncClient", return_value=client),
        ):
            result = await deal_alerts.run_deal_alerts()

        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(await self._alert_state(game_id), {})

    async def test_a_pricing_failure_never_raises(self) -> None:
        with (
            patch.dict(os.environ, {"DEAL_ALERT_WEBHOOK_URL": _WEBHOOK}),
            patch(
                "gamelib_mcp.deal_alerts.get_wishlist_deals",
                AsyncMock(side_effect=RuntimeError("ITAD down")),
            ),
        ):
            result = await deal_alerts.run_deal_alerts()

        self.assertTrue(result["configured"])
        self.assertEqual(result["sent"], 0)
        self.assertIn("ITAD down", result["error"])

    async def test_many_alerts_are_chunked_and_every_game_stamped(self) -> None:
        game_ids = []
        deals = []
        for i in range(30):
            game_id = await self._wishlisted(f"Bargain {i} " + "long title " * 12)
            game_ids.append(game_id)
            deals.append(
                _deal(game_id, f"Bargain {i} " + "long title " * 12,
                      price=float(i) + 0.99, below_assessed_target=True)
            )

        result, client = await self._run(deals, status_codes=[204] * 10)

        self.assertEqual(result["triggered"], 30)
        self.assertEqual(result["sent"], 30)
        self.assertGreater(client.post.await_count, 1)
        for call in client.post.await_args_list:
            self.assertLess(len(call.kwargs["json"]["content"]), 1_900)

        state = await db_module.load_wishlist_alert_state(game_ids)
        self.assertEqual(len(state), 30)


if __name__ == "__main__":
    unittest.main()
