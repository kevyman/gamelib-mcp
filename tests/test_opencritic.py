import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

import httpx

from gamelib_mcp.data import opencritic


class OpenCriticMatcherTests(unittest.TestCase):
    def test_normalize_match_title_folds_accents_and_ampersands(self) -> None:
        self.assertEqual(
            opencritic._normalize_match_title("Pokémon Mystery & Dungeon"),
            opencritic._normalize_match_title("Pokemon Mystery and Dungeon"),
        )

    def test_normalize_match_title_removes_punctuation_only_noise(self) -> None:
        self.assertEqual(
            opencritic._normalize_match_title("Ghost Recon: Breakpoint"),
            opencritic._normalize_match_title("Ghost Recon Breakpoint"),
        )

    def test_extract_edition_tokens_finds_distinguishing_tokens(self) -> None:
        self.assertEqual(
            opencritic._extract_edition_tokens("Resident Evil 4 Remake"),
            {"remake"},
        )

    def test_choose_match_prefers_exact_edition_match(self) -> None:
        candidates = [
            {"title": "Resident Evil 4", "url": "/game/1/re4", "opencritic_id": 1},
            {"title": "Resident Evil 4 Remake", "url": "/game/2/re4-remake", "opencritic_id": 2},
        ]
        match = opencritic._choose_match("Resident Evil 4 Remake", candidates)
        self.assertEqual(match["opencritic_id"], 2)

    def test_choose_match_prefers_exact_base_title_over_variant(self) -> None:
        candidates = [
            {"title": "Persona 3", "url": "/game/1/persona-3", "opencritic_id": 1},
            {"title": "Persona 3 Portable", "url": "/game/2/persona-3-portable", "opencritic_id": 2},
        ]
        match = opencritic._choose_match("Persona 3", candidates)
        self.assertEqual(match["opencritic_id"], 1)

    def test_choose_match_returns_none_for_ambiguous_candidates(self) -> None:
        candidates = [
            {"title": "Persona 3 Portable", "url": "/game/1/persona-3-portable", "opencritic_id": 1},
            {"title": "Persona 3 Reload", "url": "/game/2/persona-3-reload", "opencritic_id": 2},
        ]
        self.assertIsNone(opencritic._choose_match("Persona 3", candidates))


class OpenCriticParserTests(unittest.TestCase):
    def test_parse_discovery_candidates_extracts_opencritic_api_results(self) -> None:
        payload = """
        [
          {"id": 7966, "name": "Remnant: From the Ashes", "dist": 0, "relation": "game"},
          {"id": 10038, "name": "Remnant From The Ashes - Subject 2923", "dist": 0.36, "relation": "game"},
          {"id": 1, "name": "OpenCritic", "dist": 0.0, "relation": "company"}
        ]
        """
        self.assertEqual(
            opencritic._parse_discovery_candidates(payload),
            [
                {
                    "title": "Remnant: From the Ashes",
                    "url": "https://opencritic.com/game/7966/remnant-from-the-ashes",
                    "opencritic_id": 7966,
                },
                {
                    "title": "Remnant From The Ashes - Subject 2923",
                    "url": "https://opencritic.com/game/10038/remnant-from-the-ashes-subject-2923",
                    "opencritic_id": 10038,
                },
            ],
        )

    def test_parse_discovery_candidates_extracts_duckduckgo_redirect_targets(self) -> None:
        html = """
        <html><body>
          <a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fopencritic.com%2Fgame%2F9755%2Fborderlands-2&amp;rut=abc">
            Borderlands 2 Reviews - OpenCritic
          </a>
        </body></html>
        """
        self.assertEqual(
            opencritic._parse_discovery_candidates(html),
            [
                {
                    "title": "Borderlands 2 Reviews - OpenCritic",
                    "url": "https://opencritic.com/game/9755/borderlands-2",
                    "opencritic_id": 9755,
                }
            ],
        )

    def test_candidate_to_export_url_normalizes_relative_urls(self) -> None:
        self.assertEqual(
            opencritic._candidate_to_export_url({"url": "/game/120/portal-2"}),
            "https://opencritic.com/game/120/portal-2/export",
        )

    def test_parse_export_page_extracts_required_fields(self) -> None:
        html = '''
        <script id="__NEXT_DATA__" type="application/json"></script>
        <script>
        window.__STATE__ = {"id":120,"name":"Portal 2","topCriticScore":95,
        "tier":"Mighty","percentRecommended":98,"numReviews":69,
        "url":"https://opencritic.com/game/120/portal-2"};
        </script>
        '''
        record = opencritic._parse_opencritic_record(html, "https://opencritic.com/game/120/portal-2/export")
        self.assertEqual(record["opencritic_id"], 120)
        self.assertEqual(record["opencritic_url"], "https://opencritic.com/game/120/portal-2")
        self.assertEqual(record["opencritic_score"], 95)
        self.assertEqual(record["opencritic_tier"], "Mighty")
        self.assertEqual(record["opencritic_percent_rec"], 98.0)
        self.assertEqual(record["opencritic_num_reviews"], 69)

    def test_parse_export_page_extracts_server_app_state_payload(self) -> None:
        html = """
        <script id="serverApp-state" type="application/json">
        {&q;game/9755&q;:{&q;percentRecommended&q;:85.71428571428571,
        &q;numReviews&q;:23,&q;topCriticScore&q;:85.9,&q;tier&q;:&q;Mighty&q;,
        &q;name&q;:&q;Borderlands 2&q;}}
        </script>
        """
        record = opencritic._parse_opencritic_record(html, "https://opencritic.com/game/9755/borderlands-2/export")
        self.assertEqual(record["opencritic_id"], 9755)
        self.assertEqual(record["opencritic_url"], "https://opencritic.com/game/9755/borderlands-2")
        self.assertEqual(record["opencritic_score"], 86)
        self.assertEqual(record["opencritic_tier"], "Mighty")
        self.assertEqual(record["opencritic_percent_rec"], 85.71428571428571)
        self.assertEqual(record["opencritic_num_reviews"], 23)

    def test_parse_opencritic_record_returns_none_when_required_fields_missing(self) -> None:
        self.assertIsNone(opencritic._parse_opencritic_record("<html></html>", "https://opencritic.com/game/1/test/export"))


class OpenCriticDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_discover_from_opencritic_uses_api_meta_search_with_resolved_bearer(self) -> None:
        response = Mock(
            status_code=200,
            text='[{"id":120,"name":"Portal 2","dist":0,"relation":"game"}]',
        )
        response.raise_for_status = Mock(return_value=None)

        client = AsyncMock()
        client.get.return_value = response
        client.__aenter__.return_value = client
        client.__aexit__.return_value = False

        with (
            patch("gamelib_mcp.data.opencritic.httpx.AsyncClient", return_value=client),
            patch("gamelib_mcp.data.opencritic._get_opencritic_api_bearer", AsyncMock(return_value="Bearer test-token")),
        ):
            candidates = await opencritic._discover_from_opencritic("Portal 2")

        self.assertEqual(
            candidates,
            [{"title": "Portal 2", "url": "https://opencritic.com/game/120/portal-2", "opencritic_id": 120}],
        )
        client.get.assert_awaited_once_with(
            "https://api.opencritic.com/api/meta/search",
            params={"criteria": "Portal 2"},
            headers={
                **opencritic._HEADERS,
                "Accept": "application/json, text/plain, */*",
                "Authorization": "Bearer test-token",
                "Origin": "https://opencritic.com",
                "Referer": "https://opencritic.com/search?q=Portal+2",
            },
        )

    async def test_discover_from_opencritic_logs_auth_retry_details(self) -> None:
        auth_error = Mock(status_code=400, text='{"message":"API key is required."}')
        auth_error.raise_for_status = Mock(return_value=None)
        success = Mock(status_code=200, text='[{"id":120,"name":"Portal 2","dist":0,"relation":"game"}]')
        success.raise_for_status = Mock(return_value=None)

        client = AsyncMock()
        client.get.side_effect = [auth_error, success]
        client.__aenter__.return_value = client
        client.__aexit__.return_value = False

        with (
            patch("gamelib_mcp.data.opencritic.httpx.AsyncClient", return_value=client),
            patch("gamelib_mcp.data.opencritic._get_opencritic_api_bearer", AsyncMock(side_effect=["Bearer stale", "Bearer fresh"])),
            self.assertLogs("gamelib_mcp.data.opencritic", level="INFO") as logs,
        ):
            await opencritic._discover_from_opencritic("Portal 2")

        self.assertTrue(any("API key is required" in line for line in logs.output))
        self.assertTrue(any("refreshing bearer" in line.lower() for line in logs.output))

    async def test_discover_candidates_returns_primary_results_without_fallback(self) -> None:
        primary = [{"title": "Portal 2", "url": "https://opencritic.com/game/120/portal-2", "opencritic_id": 120}]
        with (
            patch("gamelib_mcp.data.opencritic._discover_from_opencritic", AsyncMock(return_value=primary)),
            patch("gamelib_mcp.data.opencritic._discover_from_search_fallback", AsyncMock()) as fallback,
        ):
            candidates = await opencritic.discover_candidates("Portal 2")

        self.assertEqual(candidates, primary)
        fallback.assert_not_awaited()

    async def test_discover_candidates_uses_search_fallback_when_primary_is_empty(self) -> None:
        with (
            patch("gamelib_mcp.data.opencritic._discover_from_opencritic", AsyncMock(return_value=[])),
            patch(
                "gamelib_mcp.data.opencritic._discover_from_search_fallback",
                AsyncMock(return_value=[{"title": "Portal 2", "url": "https://opencritic.com/game/120/portal-2", "opencritic_id": 120}]),
            ),
        ):
            candidates = await opencritic.discover_candidates("Portal 2")

        self.assertEqual(candidates[0]["opencritic_id"], 120)


class OpenCriticEnrichTests(unittest.IsolatedAsyncioTestCase):
    async def test_enrich_opencritic_writes_scraped_fields_on_success(self) -> None:
        with (
            patch("gamelib_mcp.data.opencritic._load_opencritic_context", AsyncMock(return_value={"release_date": "2026-03-01", "opencritic_cached_at": None})),
            patch("gamelib_mcp.data.opencritic.discover_candidates", AsyncMock(return_value=[{"title": "Portal 2", "url": "https://opencritic.com/game/120/portal-2", "opencritic_id": 120}])),
            patch("gamelib_mcp.data.opencritic._choose_match", return_value={"title": "Portal 2", "url": "https://opencritic.com/game/120/portal-2", "opencritic_id": 120}),
            patch("gamelib_mcp.data.opencritic._fetch_via_client", AsyncMock(return_value={"status": "matched", "fields": {"opencritic_id": 120, "opencritic_url": "https://opencritic.com/game/120/portal-2", "opencritic_score": 95, "opencritic_tier": "Mighty", "opencritic_percent_rec": 98.0, "opencritic_num_reviews": 69}})),
            patch("gamelib_mcp.data.opencritic.upsert_game_platform_enrichment", AsyncMock()) as upsert,
        ):
            result = await opencritic.enrich_opencritic(7, "Portal 2")

        self.assertEqual(result["status"], "matched")
        upsert.assert_awaited_once()
        upsert.assert_awaited_once_with(
            7,
            opencritic_id=120,
            opencritic_url="https://opencritic.com/game/120/portal-2",
            opencritic_score=95,
            opencritic_tier="Mighty",
            opencritic_percent_rec=98.0,
            opencritic_num_reviews=69,
            opencritic_cached_at=result["fields"]["opencritic_cached_at"],
        )
        self.assertIsInstance(result["fields"]["opencritic_cached_at"], str)

    async def test_enrich_opencritic_returns_ambiguous_without_writing_success_fields(self) -> None:
        with (
            patch("gamelib_mcp.data.opencritic._load_opencritic_context", AsyncMock(return_value={"release_date": "2026-03-01", "opencritic_cached_at": None})),
            patch("gamelib_mcp.data.opencritic.discover_candidates", AsyncMock(return_value=[{"title": "Persona 3", "url": "https://opencritic.com/game/1/persona-3", "opencritic_id": 1}])),
            patch("gamelib_mcp.data.opencritic._choose_match", return_value=None),
            patch("gamelib_mcp.data.opencritic.upsert_game_platform_enrichment", AsyncMock()) as upsert,
        ):
            result = await opencritic.enrich_opencritic(7, "Persona 3")

        self.assertEqual(result["status"], "ambiguous")
        upsert.assert_awaited_once_with(
            7,
            opencritic_cached_at=result["cached_at"],
        )
        self.assertTrue(result["cached_at"].startswith("AMBIGUOUS:"))

    async def test_enrich_opencritic_persists_no_match_marker(self) -> None:
        with (
            patch("gamelib_mcp.data.opencritic._load_opencritic_context", AsyncMock(return_value={"release_date": "2026-03-01", "opencritic_cached_at": None})),
            patch("gamelib_mcp.data.opencritic.discover_candidates", AsyncMock(return_value=[])),
            patch("gamelib_mcp.data.opencritic.upsert_game_platform_enrichment", AsyncMock()) as upsert,
        ):
            result = await opencritic.enrich_opencritic(7, "Missing Game")

        self.assertEqual(result["status"], "no_match")
        upsert.assert_awaited_once_with(
            7,
            opencritic_cached_at=result["cached_at"],
        )
        self.assertTrue(result["cached_at"].startswith("NO_MATCH:"))

    async def test_enrich_opencritic_persists_fetch_error_marker(self) -> None:
        with (
            patch("gamelib_mcp.data.opencritic._load_opencritic_context", AsyncMock(return_value={"release_date": "2026-03-01", "opencritic_cached_at": None})),
            patch("gamelib_mcp.data.opencritic.discover_candidates", AsyncMock(return_value=[{"title": "Portal 2", "url": "https://opencritic.com/game/120/portal-2", "opencritic_id": 120}])),
            patch("gamelib_mcp.data.opencritic._choose_match", return_value={"title": "Portal 2", "url": "https://opencritic.com/game/120/portal-2", "opencritic_id": 120}),
            patch("gamelib_mcp.data.opencritic._fetch_via_client", AsyncMock(return_value={"status": "http_error"})),
            patch("gamelib_mcp.data.opencritic.upsert_game_platform_enrichment", AsyncMock()) as upsert,
        ):
            result = await opencritic.enrich_opencritic(7, "Portal 2")

        self.assertEqual(result["status"], "http_error")
        upsert.assert_awaited_once_with(
            7,
            opencritic_cached_at=result["cached_at"],
        )
        self.assertTrue(result["cached_at"].startswith("HTTP_ERROR:"))


class OpenCriticRefreshPolicyTests(unittest.TestCase):
    def test_recent_release_is_stale_after_seven_days(self) -> None:
        fetched_at = "2026-04-01T00:00:00+00:00"
        release_date = "2026-03-20"
        now = datetime(2026, 4, 10, tzinfo=timezone.utc)
        self.assertFalse(opencritic._is_opencritic_fresh(fetched_at, release_date, now))

    def test_old_release_never_refreshes_after_success(self) -> None:
        fetched_at = "2025-01-01T00:00:00+00:00"
        release_date = "2023-05-01"
        now = datetime(2026, 4, 10, tzinfo=timezone.utc)
        self.assertTrue(opencritic._is_opencritic_fresh(fetched_at, release_date, now))

    def test_invalid_cached_timestamp_is_not_fresh(self) -> None:
        now = datetime(2026, 4, 10, tzinfo=timezone.utc)
        self.assertFalse(opencritic._is_opencritic_fresh("FAILED", "2026-03-20", now))

    def test_naive_cached_timestamp_is_treated_as_utc(self) -> None:
        now = datetime(2026, 4, 5, tzinfo=timezone.utc)
        self.assertTrue(opencritic._is_opencritic_fresh("2026-04-01T00:00:00", "2026-03-20", now))


class OpenCriticFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_opencritic_record_returns_parse_failed_on_empty_html(self) -> None:
        response = Mock(status_code=200, text="<html></html>")
        response.raise_for_status = Mock(return_value=None)
        client = AsyncMock()
        client.get.return_value = response
        with (
            patch("gamelib_mcp.data.opencritic._sleep_with_jitter", AsyncMock()),
            self.assertLogs("gamelib_mcp.data.opencritic", level="INFO") as logs,
        ):
            result = await opencritic._fetch_opencritic_record(client, "https://opencritic.com/game/120/portal-2/export")
        self.assertEqual(result["status"], "parse_failed")
        self.assertTrue(any("parse failed" in line.lower() for line in logs.output))

    async def test_fetch_opencritic_record_returns_matched_fields(self) -> None:
        response = Mock(
            status_code=200,
            text=(
                '<script>window.__STATE__ = {"id":120,"topCriticScore":95,'
                '"tier":"Mighty","percentRecommended":98,"numReviews":69,'
                '"url":"https://opencritic.com/game/120/portal-2"};</script>'
            ),
        )
        response.raise_for_status = Mock(return_value=None)
        client = AsyncMock()
        client.get.return_value = response
        with patch("gamelib_mcp.data.opencritic._sleep_with_jitter", AsyncMock()):
            result = await opencritic._fetch_opencritic_record(client, "https://opencritic.com/game/120/portal-2/export")
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["fields"]["opencritic_id"], 120)

    async def test_fetch_opencritic_record_retries_retryable_http_status(self) -> None:
        retryable = Mock(status_code=503, text="")
        retryable.request = Mock()
        retryable.raise_for_status = Mock(return_value=None)
        success = Mock(
            status_code=200,
            text=(
                '<script>window.__STATE__ = {"id":120,"topCriticScore":95,'
                '"tier":"Mighty","percentRecommended":98,"numReviews":69,'
                '"url":"https://opencritic.com/game/120/portal-2"};</script>'
            ),
        )
        success.raise_for_status = Mock(return_value=None)
        client = AsyncMock()
        client.get.side_effect = [retryable, success]
        with patch("gamelib_mcp.data.opencritic._sleep_with_jitter", AsyncMock()) as sleep:
            result = await opencritic._fetch_opencritic_record(client, "https://opencritic.com/game/120/portal-2/export")
        self.assertEqual(result["status"], "matched")
        self.assertEqual(client.get.await_count, 2)
        self.assertEqual(sleep.await_count, 2)

    async def test_fetch_opencritic_record_returns_http_error_for_non_retryable_status(self) -> None:
        response = Mock(status_code=404, text="")
        response.raise_for_status = Mock(
            side_effect=httpx.HTTPStatusError("not found", request=Mock(), response=response)
        )
        client = AsyncMock()
        client.get.return_value = response
        with patch("gamelib_mcp.data.opencritic._sleep_with_jitter", AsyncMock()) as sleep:
            result = await opencritic._fetch_opencritic_record(client, "https://opencritic.com/game/120/portal-2/export")
        self.assertEqual(result["status"], "http_error")
        self.assertEqual(client.get.await_count, 1)
        sleep.assert_awaited_once()

    async def test_fetch_opencritic_record_retries_request_errors(self) -> None:
        success = Mock(
            status_code=200,
            text=(
                '<script>window.__STATE__ = {"id":120,"topCriticScore":95,'
                '"tier":"Mighty","percentRecommended":98,"numReviews":69,'
                '"url":"https://opencritic.com/game/120/portal-2"};</script>'
            ),
        )
        success.raise_for_status = Mock(return_value=None)
        client = AsyncMock()
        client.get.side_effect = [
            httpx.RequestError("timeout", request=Mock()),
            success,
        ]
        with patch("gamelib_mcp.data.opencritic._sleep_with_jitter", AsyncMock()) as sleep:
            result = await opencritic._fetch_opencritic_record(client, "https://opencritic.com/game/120/portal-2/export")
        self.assertEqual(result["status"], "matched")
        self.assertEqual(client.get.await_count, 2)
        self.assertEqual(sleep.await_count, 2)
