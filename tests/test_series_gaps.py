"""Tests for gamelib_mcp.data.series_gaps: meta-KV-cached IGDB series members."""

from datetime import UTC
from unittest.mock import AsyncMock, patch

from conftest import ToolDBTestCase

from gamelib_mcp.data import series_gaps
from gamelib_mcp.data.igdb import IGDBRequestFailure, SeriesMember


def _member(igdb_id: int = 1, name: str = "Pikmin") -> SeriesMember:
    return SeriesMember(
        igdb_id=igdb_id,
        name=name,
        first_release_date="2002-01-01",
        game_type=0,
        platforms=[130],
    )


# No TWITCH_CLIENT_ID/SECRET is set in this test environment, so
# fetch_version_parent_aliases short-circuits to {} without any network call —
# tests that only care about the member list/cache mechanics don't need to
# mock it separately.
class GetSeriesMembersCachedTests(ToolDBTestCase):
    async def test_fresh_fetch_writes_meta_key(self) -> None:
        fetch_mock = AsyncMock(return_value=[_member()])
        with patch("gamelib_mcp.data.series_gaps.fetch_series_members", fetch_mock):
            result = await series_gaps.get_series_members_cached("collection", 555)

        self.assertEqual(result.members, [_member()])
        self.assertEqual(result.aliases, {})
        fetch_mock.assert_awaited_once_with("collection", 555)

        from gamelib_mcp.data.db import get_meta

        raw = await get_meta("series_members_v3:collection:555")
        self.assertIsNotNone(raw)

    async def test_second_call_within_ttl_does_not_refetch(self) -> None:
        fetch_mock = AsyncMock(return_value=[_member()])
        with patch("gamelib_mcp.data.series_gaps.fetch_series_members", fetch_mock):
            await series_gaps.get_series_members_cached("collection", 555)
            await series_gaps.get_series_members_cached("collection", 555)

        fetch_mock.assert_awaited_once()

    async def test_refresh_bypasses_cache(self) -> None:
        fetch_mock = AsyncMock(return_value=[_member()])
        with patch("gamelib_mcp.data.series_gaps.fetch_series_members", fetch_mock):
            await series_gaps.get_series_members_cached("collection", 555)
            await series_gaps.get_series_members_cached("collection", 555, refresh=True)

        self.assertEqual(fetch_mock.await_count, 2)

    async def test_refresh_also_refetches_aliases(self) -> None:
        fetch_mock = AsyncMock(return_value=[_member()])
        alias_mock = AsyncMock(return_value={})
        with (
            patch("gamelib_mcp.data.series_gaps.fetch_series_members", fetch_mock),
            patch("gamelib_mcp.data.series_gaps.fetch_version_parent_aliases", alias_mock),
        ):
            await series_gaps.get_series_members_cached("collection", 555)
            await series_gaps.get_series_members_cached("collection", 555, refresh=True)

        self.assertEqual(alias_mock.await_count, 2)

    async def test_fetch_failure_with_stale_cache_serves_stale_data(self) -> None:
        fetch_mock = AsyncMock(return_value=[_member()])
        with patch("gamelib_mcp.data.series_gaps.fetch_series_members", fetch_mock):
            await series_gaps.get_series_members_cached("collection", 555)

        failing_mock = AsyncMock(side_effect=IGDBRequestFailure("boom"))
        with patch("gamelib_mcp.data.series_gaps.fetch_series_members", failing_mock):
            result = await series_gaps.get_series_members_cached(
                "collection", 555, refresh=True
            )

        self.assertEqual(result.members, [_member()])

    async def test_fetch_failure_with_no_cache_raises(self) -> None:
        failing_mock = AsyncMock(side_effect=IGDBRequestFailure("boom"))
        with (
            patch("gamelib_mcp.data.series_gaps.fetch_series_members", failing_mock),
            self.assertRaises(IGDBRequestFailure),
        ):
            await series_gaps.get_series_members_cached("collection", 999)

    async def test_malformed_cache_entry_treated_as_absent(self) -> None:
        from gamelib_mcp.data.db import set_meta

        await set_meta("series_members_v3:collection:1", "not json")
        fetch_mock = AsyncMock(return_value=[_member(igdb_id=2)])
        with patch("gamelib_mcp.data.series_gaps.fetch_series_members", fetch_mock):
            result = await series_gaps.get_series_members_cached("collection", 1)

        self.assertEqual(result.members, [_member(igdb_id=2)])
        fetch_mock.assert_awaited_once()

    async def test_old_format_cache_without_aliases_triggers_refetch(self) -> None:
        # Simulates a cache entry written before aliases existed (either the
        # pre-fix code, or — since the key was also namespaced — a stray write
        # under the new key missing the "aliases" field). _parse_cache must
        # treat it as absent rather than silently serving alias-less data.
        import json
        from datetime import datetime

        from gamelib_mcp.data.db import get_meta, set_meta

        old_payload = json.dumps(
            {
                "fetched_at": datetime.now(UTC).isoformat(),
                "members": [
                    {
                        "igdb_id": 1,
                        "name": "Pikmin",
                        "first_release_date": "2002-01-01",
                        "game_type": 0,
                        "platforms": [130],
                    }
                ],
                # deliberately no "aliases" key
            }
        )
        await set_meta("series_members_v3:collection:1", old_payload)

        fetch_mock = AsyncMock(return_value=[_member(igdb_id=2, name="Pikmin 2")])
        with patch("gamelib_mcp.data.series_gaps.fetch_series_members", fetch_mock):
            result = await series_gaps.get_series_members_cached("collection", 1)

        fetch_mock.assert_awaited_once()
        self.assertEqual(result.members, [_member(igdb_id=2, name="Pikmin 2")])
        self.assertEqual(result.aliases, {})

        # And the cache was rewritten in the new (with-aliases) format.
        raw = await get_meta("series_members_v3:collection:1")
        assert raw is not None
        self.assertIn("aliases", json.loads(raw))
