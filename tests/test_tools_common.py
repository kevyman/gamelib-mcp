"""Tests for gamelib_mcp.tools.common helpers."""

import unittest

from gamelib_mcp.tools.common import describe_failure

STEAM_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"


class DescribeFailureTests(unittest.TestCase):
    def test_strips_query_string_from_status_error(self):
        exc = RuntimeError(
            f"Client error '401 Unauthorized' for url '{STEAM_URL}?key=SECRETKEY123&steamid=1'"
        )
        text = describe_failure(exc)
        self.assertNotIn("SECRETKEY123", text)
        self.assertNotIn("steamid", text)
        self.assertIn("401 Unauthorized", text)
        self.assertIn(f"{STEAM_URL}'", text)

    def test_plain_message_untouched(self):
        self.assertEqual(describe_failure(RuntimeError("boom")), "boom")

    def test_empty_message_falls_back_to_type_name(self):
        self.assertEqual(describe_failure(ValueError()), "ValueError")

    def test_strips_every_url_in_message(self):
        exc = RuntimeError(
            "first https://a.example.com/x?token=AAA#frag then http://b.example.com/y?k=BBB done"
        )
        text = describe_failure(exc)
        self.assertNotIn("AAA", text)
        self.assertNotIn("BBB", text)
        self.assertNotIn("frag", text)
        self.assertEqual(text, "first https://a.example.com/x then http://b.example.com/y done")
