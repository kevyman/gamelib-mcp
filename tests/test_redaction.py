"""Tests for gamelib_mcp.redaction and the httpx log-level pin in main."""

import logging
import unittest

import httpx

from gamelib_mcp.redaction import redact_secrets

STEAM_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"


class RedactSecretsTests(unittest.TestCase):
    def test_key_in_the_middle(self):
        self.assertEqual(
            redact_secrets(f"{STEAM_URL}?format=json&key=SECRET1&steamid=765"),
            f"{STEAM_URL}?format=json&key=***&steamid=765",
        )

    def test_key_first_and_at_the_end(self):
        self.assertEqual(redact_secrets(f"{STEAM_URL}?key=SECRET1"), f"{STEAM_URL}?key=***")
        self.assertEqual(
            redact_secrets("https://api.isthereanydeal.com/games/prices/v3?country=BE&key=ITADKEY"),
            "https://api.isthereanydeal.com/games/prices/v3?country=BE&key=***",
        )

    def test_several_secret_params(self):
        text = (
            "https://x.example/a?api_key=A1&apikey=A2&access_token=A3&token=A4"
            "&npsso=A5&client_secret=A6&page=2"
        )
        redacted = redact_secrets(text)
        for value in ("A1", "A2", "A3", "A4", "A5", "A6"):
            self.assertNotIn(value, redacted)
        self.assertEqual(
            redacted,
            "https://x.example/a?api_key=***&apikey=***&access_token=***&token=***"
            "&npsso=***&client_secret=***&page=2",
        )

    def test_param_names_are_case_insensitive(self):
        self.assertEqual(
            redact_secrets("https://x.example/a?KEY=S1&Api_Key=S2&Token=S3"),
            "https://x.example/a?KEY=***&Api_Key=***&Token=***",
        )

    def test_url_inside_an_httpx_status_message(self):
        request = httpx.Request("GET", f"{STEAM_URL}?key=dummy&steamid=7656")
        response = httpx.Response(401, request=request)
        with self.assertRaises(httpx.HTTPStatusError) as caught:
            response.raise_for_status()
        redacted = redact_secrets(str(caught.exception))
        self.assertNotIn("dummy", redacted)
        self.assertIn(
            f"Client error '401 Unauthorized' for url '{STEAM_URL}?key=***&steamid=7656'",
            redacted,
        )

    def test_value_stops_at_fragment_whitespace_and_quotes(self):
        self.assertEqual(
            redact_secrets('see "https://x.example/a?token=T1#frag" and https://y.example/?key=K2 then'),
            'see "https://x.example/a?token=***#frag" and https://y.example/?key=*** then',
        )

    def test_lookalike_param_names_untouched(self):
        text = "https://x.example/a?monkey=1&steam_key=2&keys=3&tokens=4"
        self.assertEqual(redact_secrets(text), text)

    def test_text_without_secrets_unchanged(self):
        for text in ("", "boom", "GOG sync failed: timed out", f"{STEAM_URL}?steamid=1&format=json"):
            self.assertEqual(redact_secrets(text), text)

    def test_exception_input_is_rendered_and_redacted(self):
        exc = RuntimeError(f"Client error '403 Forbidden' for url '{STEAM_URL}?key=SECRET1'")
        self.assertEqual(
            redact_secrets(exc),
            f"Client error '403 Forbidden' for url '{STEAM_URL}?key=***'",
        )

    def test_other_non_str_input_returned_unchanged(self):
        self.assertIsNone(redact_secrets(None))
        payload = {"key": "SECRET1"}
        self.assertIs(redact_secrets(payload), payload)
        self.assertEqual(redact_secrets(42), 42)


class HttpLoggerLevelTests(unittest.TestCase):
    def test_httpx_and_httpcore_held_at_warning_after_main_import(self):
        from gamelib_mcp import main  # noqa: F401

        self.assertEqual(logging.getLogger("httpx").level, logging.WARNING)
        self.assertEqual(logging.getLogger("httpcore").level, logging.WARNING)
        self.assertFalse(logging.getLogger("httpx").isEnabledFor(logging.INFO))
