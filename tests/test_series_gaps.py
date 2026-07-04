"""Tests for gamelib_mcp.data.series_gaps: meta-KV-cached IGDB series members."""

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


class GetSeriesMembersCachedTests(ToolDBTestCase):
    async def test_fresh_fetch_writes_meta_key(self) -> None:
        fetch_mock = AsyncMock(return_value=[_member()])
        with patch("gamelib_mcp.data.series_gaps.fetch_series_members", fetch_mock):
            members = await series_gaps.get_series_members_cached("collection", 555)

        self.assertEqual(members, [_member()])
        fetch_mock.assert_awaited_once_with("collection", 555)

        from gamelib_mcp.data.db import get_meta

        raw = await get_meta("series_members:collection:555")
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

    async def test_fetch_failure_with_stale_cache_serves_stale_data(self) -> None:
        fetch_mock = AsyncMock(return_value=[_member()])
        with patch("gamelib_mcp.data.series_gaps.fetch_series_members", fetch_mock):
            await series_gaps.get_series_members_cached("collection", 555)

        failing_mock = AsyncMock(side_effect=IGDBRequestFailure("boom"))
        with patch("gamelib_mcp.data.series_gaps.fetch_series_members", failing_mock):
            members = await series_gaps.get_series_members_cached(
                "collection", 555, refresh=True
            )

        self.assertEqual(members, [_member()])

    async def test_fetch_failure_with_no_cache_raises(self) -> None:
        failing_mock = AsyncMock(side_effect=IGDBRequestFailure("boom"))
        with patch("gamelib_mcp.data.series_gaps.fetch_series_members", failing_mock):
            with self.assertRaises(IGDBRequestFailure):
                await series_gaps.get_series_members_cached("collection", 999)

    async def test_malformed_cache_entry_treated_as_absent(self) -> None:
        from gamelib_mcp.data.db import set_meta

        await set_meta("series_members:collection:1", "not json")
        fetch_mock = AsyncMock(return_value=[_member(igdb_id=2)])
        with patch("gamelib_mcp.data.series_gaps.fetch_series_members", fetch_mock):
            members = await series_gaps.get_series_members_cached("collection", 1)

        self.assertEqual(members, [_member(igdb_id=2)])
        fetch_mock.assert_awaited_once()
