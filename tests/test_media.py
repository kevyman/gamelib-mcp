"""data/media.py — on-demand trailer/screenshot/similar-games fetching.

No real HTTP: the Steam path mocks ``fetch_store_appdetails`` and serves the
trailer HEAD check through an httpx.MockTransport, and the IGDB path mocks
``_post_igdb_games`` (the same seam test_igdb.py patches, so the request gate
and its retries stay out of these tests). The meta-KV cache is real, which is
what the caching tests are about, so they run on the shared temp-DB harness.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx
from conftest import ToolDBTestCase

from gamelib_mcp.data import media
from gamelib_mcp.data.db import get_meta, set_meta
from gamelib_mcp.data.igdb import IGDBGame, IGDBRequestFailure

_IGDB_ENV = {"TWITCH_CLIENT_ID": "client", "TWITCH_CLIENT_SECRET": "secret"}


def _screenshots(count: int) -> list[dict]:
    return [
        {
            "id": index,
            "path_thumbnail": f"https://shared.akamai.steamstatic.com/{index}_thumb.jpg",
            "path_full": f"https://shared.akamai.steamstatic.com/{index}_full.jpg",
        }
        for index in range(count)
    ]


def _appdetails(*, screenshots: int = 2, movies: list | None = None) -> dict:
    return {
        "short_description": "A tiny bug with a nail.",
        "screenshots": _screenshots(screenshots),
        "movies": movies
        if movies is not None
        else [
            {
                "id": 256658589,
                "name": "Release Trailer",
                "thumbnail": "https://shared.akamai.steamstatic.com/poster.jpg",
                "highlight": True,
            }
        ],
    }


# Bound before any patching: the factory below replaces httpx.AsyncClient in
# the media module's namespace, which IS the httpx module — building the mock
# client through the patched name would recurse.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _head_transport(status_code: int = 200):
    """An httpx client factory whose every request answers with ``status_code``."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status_code)

    def factory(*_args, **_kwargs):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))

    return factory, seen


def _igdb_payload(*, videos: list | None = None, similar: int = 0) -> list[dict]:
    return [
        {
            "id": 1520,
            "name": "Hollow Knight",
            "summary": "Explore a ruined kingdom.",
            "screenshots": [{"image_id": "sc1"}, {"image_id": "sc2"}],
            "videos": videos
            if videos is not None
            else [
                {"video_id": "aaa", "name": "Gameplay"},
                {"video_id": "bbb", "name": "Launch Trailer"},
            ],
            "similar_games": [
                {
                    "id": 100 + index,
                    "name": f"Similar {index}",
                    "first_release_date": 1451606400,  # 2016-01-01
                    "cover": {"image_id": f"cover{index}"},
                }
                for index in range(similar)
            ],
        }
    ]


class SteamMediaTests(ToolDBTestCase):
    async def test_screenshots_trailer_and_description(self):
        factory, seen = _head_transport()
        with (
            patch.object(
                media, "fetch_store_appdetails", AsyncMock(return_value=_appdetails())
            ) as store,
            patch("gamelib_mcp.data.media.httpx.AsyncClient", factory),
        ):
            result = await media.get_game_media(steam_appid=367520)

        assert result is not None
        # The media filter groups, not the enrichment ones — the 7-day store
        # cache predates them, which is why this is fetched on demand.
        self.assertEqual(
            store.await_args.kwargs["filters"], "screenshots,movies,short_description"
        )
        block = result["media"]
        self.assertEqual(block["source"], "steam")
        self.assertEqual(block["short_description"], "A tiny bug with a nail.")
        self.assertEqual(
            block["screenshots"][0],
            {
                "thumb": "https://shared.akamai.steamstatic.com/0_thumb.jpg",
                "full": "https://shared.akamai.steamstatic.com/0_full.jpg",
            },
        )
        self.assertEqual(
            block["trailer"],
            {
                "kind": "mp4",
                "url": (
                    "https://cdn.cloudflare.steamstatic.com/steam/apps/"
                    "256658589/movie480.mp4"
                ),
                "hq_url": (
                    "https://cdn.cloudflare.steamstatic.com/steam/apps/"
                    "256658589/movie_max.mp4"
                ),
                "poster": "https://shared.akamai.steamstatic.com/poster.jpg",
                "name": "Release Trailer",
            },
        )
        # Exactly one HEAD, against the 480 rendition.
        self.assertEqual([r.method for r in seen], ["HEAD"])
        self.assertTrue(str(seen[0].url).endswith("movie480.mp4"))
        self.assertIsNone(result["similar_raw"])

    async def test_trailer_head_404_drops_only_the_trailer(self):
        # The constructed mp4 is undocumented legacy surface; when Valve drops
        # it the card must lose the hero, not the screenshots.
        factory, _ = _head_transport(404)
        with (
            patch.object(
                media, "fetch_store_appdetails", AsyncMock(return_value=_appdetails())
            ),
            patch("gamelib_mcp.data.media.httpx.AsyncClient", factory),
        ):
            result = await media.get_game_media(steam_appid=367520)

        assert result is not None
        self.assertIsNone(result["media"]["trailer"])
        self.assertEqual(len(result["media"]["screenshots"]), 2)

    async def test_screenshots_are_capped_with_the_true_total(self):
        factory, _ = _head_transport()
        with (
            patch.object(
                media,
                "fetch_store_appdetails",
                AsyncMock(return_value=_appdetails(screenshots=11)),
            ),
            patch("gamelib_mcp.data.media.httpx.AsyncClient", factory),
        ):
            result = await media.get_game_media(steam_appid=367520)

        assert result is not None
        block = result["media"]
        self.assertEqual(len(block["screenshots"]), media.SCREENSHOT_CAP)
        self.assertEqual(block["screenshot_count"], 11)
        self.assertTrue(block["screenshots_truncated"])

    async def test_highlighted_movie_wins_over_the_first(self):
        factory, _ = _head_transport()
        movies = [
            {"id": 1, "name": "Teaser", "thumbnail": "t1"},
            {"id": 2, "name": "Highlight", "thumbnail": "t2", "highlight": True},
        ]
        with (
            patch.object(
                media,
                "fetch_store_appdetails",
                AsyncMock(return_value=_appdetails(movies=movies)),
            ),
            patch("gamelib_mcp.data.media.httpx.AsyncClient", factory),
        ):
            result = await media.get_game_media(steam_appid=367520)

        assert result is not None
        self.assertIn("/2/movie480.mp4", result["media"]["trailer"]["url"])

    async def test_no_appdetails_data_is_a_cached_miss(self):
        with patch.object(
            media, "fetch_store_appdetails", AsyncMock(return_value=None)
        ) as store:
            self.assertIsNone(await media.get_game_media(steam_appid=99))
            self.assertIsNone(await media.get_game_media(steam_appid=99))

        self.assertEqual(store.await_count, 1)
        cached = json.loads(await get_meta("game_media:steam:99"))
        self.assertIsNone(cached["payload"])

    async def test_steam_media_borrows_similar_games_from_igdb(self):
        # Similar games exist only on IGDB. A Steam-sourced result still
        # reaches over for them when the game's IGDB record is reachable —
        # otherwise the most common candidates (Steam appids) would never get
        # a similar row. The MEDIA block itself stays whole-source Steam.
        factory, _ = _head_transport()
        with (
            patch.object(
                media, "fetch_store_appdetails", AsyncMock(return_value=_appdetails())
            ),
            patch("gamelib_mcp.data.media.httpx.AsyncClient", factory),
            patch.dict(os.environ, _IGDB_ENV, clear=False),
            patch("gamelib_mcp.data.media._get_token", AsyncMock(return_value="token")),
            patch(
                "gamelib_mcp.data.media._post_igdb_games",
                AsyncMock(return_value=_igdb_payload(similar=3)),
            ),
        ):
            result = await media.get_game_media(steam_appid=367520, igdb_id=1520)

        assert result is not None
        self.assertEqual(result["media"]["source"], "steam")
        self.assertEqual(result["media"]["trailer"]["kind"], "mp4")
        self.assertEqual(len(result["similar_raw"]), 3)
        self.assertEqual(result["similar_count"], 3)
        self.assertEqual(result["igdb_id"], 1520)


class IGDBMediaTests(ToolDBTestCase):
    def _patched_igdb(self, post):
        return (
            patch.dict(os.environ, _IGDB_ENV, clear=False),
            patch("gamelib_mcp.data.media._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.media._post_igdb_games", post),
        )

    async def test_screenshots_youtube_trailer_and_summary(self):
        post = AsyncMock(return_value=_igdb_payload())
        env, token, posted = self._patched_igdb(post)
        with env, token, posted:
            result = await media.get_game_media(igdb_id=1520)

        assert result is not None
        block = result["media"]
        self.assertEqual(block["source"], "igdb")
        self.assertEqual(
            block["screenshots"][0],
            {
                "thumb": (
                    "https://images.igdb.com/igdb/image/upload/t_screenshot_med/sc1.jpg"
                ),
                "full": (
                    "https://images.igdb.com/igdb/image/upload/t_screenshot_big/sc1.jpg"
                ),
            },
        )
        # The video NAMED a trailer wins even though it is not first.
        self.assertEqual(
            block["trailer"],
            {
                "kind": "youtube",
                "video_id": "bbb",
                "poster": "https://i.ytimg.com/vi/bbb/hqdefault.jpg",
                "name": "Launch Trailer",
            },
        )
        self.assertEqual(block["short_description"], "Explore a ruined kingdom.")
        self.assertEqual(result["igdb_id"], 1520)

    async def test_first_video_is_used_when_none_is_named_a_trailer(self):
        post = AsyncMock(
            return_value=_igdb_payload(videos=[{"video_id": "zzz", "name": "Devlog"}])
        )
        env, token, posted = self._patched_igdb(post)
        with env, token, posted:
            result = await media.get_game_media(igdb_id=1520)

        assert result is not None
        self.assertEqual(result["media"]["trailer"]["video_id"], "zzz")

    async def test_similar_games_are_capped_with_the_true_count(self):
        post = AsyncMock(return_value=_igdb_payload(similar=12))
        env, token, posted = self._patched_igdb(post)
        with env, token, posted:
            result = await media.get_game_media(igdb_id=1520)

        assert result is not None
        self.assertEqual(len(result["similar_raw"]), media.SIMILAR_CAP)
        self.assertEqual(result["similar_count"], 12)
        self.assertEqual(
            result["similar_raw"][0],
            {
                "igdb_id": 100,
                "name": "Similar 0",
                "release_year": 2016,
                "cover_image_id": "cover0",
            },
        )

    async def test_unconfigured_igdb_is_a_miss_not_an_error(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TWITCH_CLIENT_ID", None)
            os.environ.pop("TWITCH_CLIENT_SECRET", None)
            self.assertIsNone(await media.get_game_media(igdb_id=1520))


class NameResolutionTests(ToolDBTestCase):
    async def test_exact_name_resolves_and_the_mapping_is_cached(self):
        lookup = AsyncMock(
            return_value=[IGDBGame(igdb_id=1520, name="Hollow Knight", category=0,
                                   first_release_date="2017-02-24")]
        )
        post = AsyncMock(return_value=_igdb_payload())
        with (
            patch.dict(os.environ, _IGDB_ENV, clear=False),
            patch("gamelib_mcp.data.media.fetch_games_by_exact_name", lookup),
            patch("gamelib_mcp.data.media._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.media._post_igdb_games", post),
        ):
            first = await media.get_game_media(name="Hollow Knight")
            second = await media.get_game_media(name="  hollow knight  ")

        assert first is not None and second is not None
        self.assertEqual(first["igdb_id"], 1520)
        # One name lookup for both calls, and the media query is cached too.
        self.assertEqual(lookup.await_count, 1)
        self.assertEqual(post.await_count, 1)
        self.assertIsNotNone(await get_meta("game_media_name:hollow knight"))

    async def test_ambiguous_exact_name_refuses_to_guess(self):
        lookup = AsyncMock(
            return_value=[
                IGDBGame(igdb_id=1, name="The Bridge", category=0, first_release_date=None),
                IGDBGame(igdb_id=2, name="The Bridge", category=0, first_release_date=None),
            ]
        )
        post = AsyncMock()
        with (
            patch.dict(os.environ, _IGDB_ENV, clear=False),
            patch("gamelib_mcp.data.media.fetch_games_by_exact_name", lookup),
            patch("gamelib_mcp.data.media._post_igdb_games", post),
        ):
            self.assertIsNone(await media.get_game_media(name="The Bridge"))
        post.assert_not_awaited()

    async def test_a_miss_is_cached_so_it_is_not_re_queried(self):
        lookup = AsyncMock(return_value=[])
        with (
            patch.dict(os.environ, _IGDB_ENV, clear=False),
            patch("gamelib_mcp.data.media.fetch_games_by_exact_name", lookup),
        ):
            self.assertIsNone(await media.get_game_media(name="Not A Real Game"))
            self.assertIsNone(await media.get_game_media(name="Not A Real Game"))

        self.assertEqual(lookup.await_count, 1)
        cached = json.loads(await get_meta("game_media_name:not a real game"))
        self.assertIsNone(cached["payload"])

    async def test_a_lookup_failure_is_not_cached(self):
        lookup = AsyncMock(side_effect=IGDBRequestFailure("boom"))
        with (
            patch.dict(os.environ, _IGDB_ENV, clear=False),
            patch("gamelib_mcp.data.media.fetch_games_by_exact_name", lookup),
        ):
            self.assertIsNone(await media.get_game_media(name="Transient"))
            self.assertIsNone(await media.get_game_media(name="Transient"))

        # A failure must not become a 24h "this game doesn't exist" backoff.
        self.assertEqual(lookup.await_count, 2)
        self.assertIsNone(await get_meta("game_media_name:transient"))


class MediaCacheTests(ToolDBTestCase):
    async def _seed_cache(self, key: str, payload, *, age: timedelta) -> None:
        await set_meta(
            key,
            json.dumps(
                {
                    "fetched_at": (datetime.now(UTC) - age).isoformat(),
                    "payload": payload,
                }
            ),
        )

    def _payload(self, description: str) -> dict:
        return {
            "media": {
                "source": "steam",
                "trailer": None,
                "screenshots": [],
                "screenshot_count": 0,
                "screenshots_truncated": False,
                "short_description": description,
            },
            "similar_raw": None,
            "similar_count": None,
            "igdb_id": None,
        }

    async def test_a_fresh_cache_entry_skips_the_fetch(self):
        await self._seed_cache(
            "game_media:steam:5", self._payload("cached"), age=timedelta(days=1)
        )
        store = AsyncMock(return_value=_appdetails())
        with patch.object(media, "fetch_store_appdetails", store):
            result = await media.get_game_media(steam_appid=5)

        assert result is not None
        self.assertEqual(result["media"]["short_description"], "cached")
        store.assert_not_awaited()

    async def test_an_expired_entry_is_refetched(self):
        await self._seed_cache(
            "game_media:steam:5", self._payload("stale"), age=timedelta(days=8)
        )
        factory, _ = _head_transport()
        with (
            patch.object(
                media, "fetch_store_appdetails", AsyncMock(return_value=_appdetails())
            ),
            patch("gamelib_mcp.data.media.httpx.AsyncClient", factory),
        ):
            result = await media.get_game_media(steam_appid=5)

        assert result is not None
        self.assertEqual(result["media"]["short_description"], "A tiny bug with a nail.")

    async def test_a_failed_refresh_serves_the_stale_payload(self):
        await self._seed_cache(
            "game_media:steam:5", self._payload("stale"), age=timedelta(days=30)
        )
        with patch.object(
            media, "fetch_store_appdetails", AsyncMock(side_effect=RuntimeError("down"))
        ):
            result = await media.get_game_media(steam_appid=5)

        assert result is not None
        self.assertEqual(result["media"]["short_description"], "stale")
        # The stale copy is served, never overwritten by the failure.
        cached = json.loads(await get_meta("game_media:steam:5"))
        self.assertEqual(cached["payload"]["media"]["short_description"], "stale")

    async def test_a_source_that_suddenly_returns_nothing_serves_the_stale_payload(self):
        await self._seed_cache(
            "game_media:steam:5", self._payload("stale"), age=timedelta(days=30)
        )
        with patch.object(media, "fetch_store_appdetails", AsyncMock(return_value=None)):
            result = await media.get_game_media(steam_appid=5)

        assert result is not None
        self.assertEqual(result["media"]["short_description"], "stale")

    async def test_an_expired_miss_is_retried(self):
        await self._seed_cache("game_media:steam:5", None, age=timedelta(hours=25))
        factory, _ = _head_transport()
        with (
            patch.object(
                media, "fetch_store_appdetails", AsyncMock(return_value=_appdetails())
            ) as store,
            patch("gamelib_mcp.data.media.httpx.AsyncClient", factory),
        ):
            result = await media.get_game_media(steam_appid=5)

        store.assert_awaited_once()
        assert result is not None
        self.assertEqual(result["media"]["screenshot_count"], 2)

    async def test_nothing_resolvable_returns_none(self):
        self.assertIsNone(await media.get_game_media())
