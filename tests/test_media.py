"""data/media.py — on-demand trailer/screenshot/similar-games/pedigree fetching.

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


def _epoch(year: int) -> int:
    return int(datetime(year, 1, 1, tzinfo=UTC).timestamp())


def _involved(
    *,
    name: str = "Team Cherry",
    company_id: int = 6455,
    developer: bool = True,
    publisher: bool = False,
    porting: bool = False,
    supporting: bool = False,
    start_date: int | None = 1341100800,  # 2012-07-01
    country: int | None = 36,
) -> dict:
    return {
        "company": {
            "id": company_id,
            "name": name,
            "start_date": start_date,
            "country": country,
        },
        "developer": developer,
        "publisher": publisher,
        "porting": porting,
        "supporting": supporting,
    }


def _pedigree_game(
    *,
    involved: list[dict],
    hypes: int | None = None,
    first_release_date: int | None = 1487894400,  # 2017-02-24
) -> list[dict]:
    return [
        {
            "id": 1520,
            "name": "Hollow Knight",
            "summary": "Explore a ruined kingdom.",
            "screenshots": [{"image_id": "sc1"}],
            "videos": [],
            "similar_games": [],
            "involved_companies": involved,
            "hypes": hypes,
            "first_release_date": first_release_date,
        }
    ]


def _catalog(count: int, *, first_year: int = 2016, start_id: int = 200) -> list[dict]:
    """``count`` developed games, one per year going backwards from ``first_year``."""
    return [
        {
            "id": start_id + index,
            "name": f"Earlier {index}",
            "cover": {"image_id": f"cat{index}"},
            "first_release_date": _epoch(first_year - index),
            "aggregated_rating": 80.4 + index,
        }
        for index in range(count)
    ]


def _dispatching_post(game_payload: list[dict], catalog_rows: list[dict]):
    """One ``_post_igdb_games`` mock serving both queries the IGDB path makes."""
    calls: dict[str, list[str]] = {"game": [], "catalog": []}

    async def post(query: str, headers: dict[str, str]) -> list[dict]:
        if "involved_companies.company =" in query:
            calls["catalog"].append(query)
            return catalog_rows
        calls["game"].append(query)
        return game_payload

    return AsyncMock(side_effect=post), calls


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
        cached = json.loads(await get_meta(media._cache_key("steam", 99)))
        self.assertIsNone(cached["payload"])

    async def test_malformed_appdetails_payload_raises_on_the_failure_aware_path(self):
        # HTTP-successful JSON with a non-dict top level is a FAILURE, not
        # "Steam has no data" — swallowed, the media cache would remember it
        # as a 24h miss. Enrichment callers keep the legacy swallow.
        from gamelib_mcp.data.steam_store import fetch_store_appdetails

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        client = _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))
        with self.assertRaises(ValueError):
            await fetch_store_appdetails(367520, client, raise_on_failure=True)
        self.assertIsNone(await fetch_store_appdetails(367520, client))

    async def test_head_transport_failure_keeps_the_trailer(self):
        # The HEAD gate exists to catch Valve DROPPING the legacy renditions
        # (a definitive non-2xx), not to demand a healthy CDN this instant: a
        # timeout must not bake a trailer-less payload into the 7-day cache.
        # The widget's own <video> error fallback covers a truly dead URL.
        def factory(*_args, **_kwargs):
            def handler(request: httpx.Request) -> httpx.Response:
                raise httpx.ConnectTimeout("cdn hiccup")

            return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))

        with (
            patch.object(
                media, "fetch_store_appdetails", AsyncMock(return_value=_appdetails())
            ),
            patch("gamelib_mcp.data.media.httpx.AsyncClient", factory),
        ):
            result = await media.get_game_media(steam_appid=367520)

        assert result is not None
        self.assertEqual(result["media"]["trailer"]["kind"], "mp4")
        self.assertEqual(result["errors"], [])

    async def test_igdb_failure_does_not_block_steam_media(self):
        # The two sides run concurrently and independently: an unhealthy IGDB
        # must not stop the PREFERRED Steam source from answering (nor stay
        # silent about its own failure).
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
                AsyncMock(side_effect=IGDBRequestFailure("igdb is down")),
            ),
        ):
            result = await media.get_game_media(steam_appid=367520, igdb_id=1520)

        assert result is not None
        self.assertEqual(result["media"]["source"], "steam")
        self.assertEqual(result["errors"], ["igdb: fetch failed"])

    async def test_steam_request_failure_is_not_cached_as_a_miss(self):
        # An outage must stay distinguishable from "Steam has no data": a miss
        # is remembered for 24h, so caching a transient failure would strip
        # media from every card of this game for the rest of the day.
        failing = AsyncMock(side_effect=httpx.ConnectError("steam is down"))
        with patch.object(media, "fetch_store_appdetails", failing) as store:
            first = await media.get_game_media(steam_appid=77)
            second = await media.get_game_media(steam_appid=77)

        # The outage is REPORTED, not disguised as a media-less game: the
        # payload comes back empty-handed but says why.
        for result in (first, second):
            assert result is not None
            self.assertIsNone(result["media"])
            self.assertEqual(result["errors"], ["steam: fetch failed"])
        # Retried on the second call (nothing cached), and no miss written.
        self.assertEqual(store.await_count, 2)
        self.assertIsNone(await get_meta(media._cache_key("steam", 77)))
        # The failure path asked for the failure-aware fetch.
        self.assertTrue(store.await_args.kwargs["raise_on_failure"])

    async def test_steam_request_failure_serves_the_stale_payload(self):
        stale = {"media": {"source": "steam", "trailer": None, "screenshots": [],
                           "screenshot_count": 0, "screenshots_truncated": False,
                           "short_description": "old but true"},
                 "similar_raw": None, "similar_count": None, "igdb_id": None,
                 "pedigree_raw": None}
        await set_meta(
            media._cache_key("steam", 88),
            json.dumps({
                "fetched_at": (datetime.now(UTC) - timedelta(days=30)).isoformat(),
                "payload": stale,
            }),
        )
        failing = AsyncMock(side_effect=httpx.ConnectError("steam is down"))
        with patch.object(media, "fetch_store_appdetails", failing):
            result = await media.get_game_media(steam_appid=88)

        assert result is not None
        self.assertEqual(result["media"]["short_description"], "old but true")
        # Stale-served is a successful answer, not a reported failure.
        self.assertEqual(result["errors"], [])

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


class PedigreeTests(ToolDBTestCase):
    """Who made this, and what they shipped before it (issue #159).

    The IGDB path now makes two queries — the game, then one bounded page of
    the developer's own catalogue — so every test here serves them through one
    mock that dispatches on the query text.
    """

    def _patched(self, post):
        return (
            patch.dict(os.environ, _IGDB_ENV, clear=False),
            patch("gamelib_mcp.data.media._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.media._post_igdb_games", post),
        )

    async def _fetch(self, game_payload, catalog_rows, **kwargs):
        post, calls = _dispatching_post(game_payload, catalog_rows)
        env, token, posted = self._patched(post)
        with env, token, posted:
            result = await media.get_game_media(igdb_id=1520, **kwargs)
        return result, calls

    async def test_a_failed_catalog_fetch_is_reported_and_never_cached(self):
        # The game query succeeded but the catalogue query failed: the strip
        # renders header-only THIS call, the error is reported, and the
        # incomplete payload must not freeze into the 7-day game cache —
        # the next call retries the catalogue.
        calls: dict[str, int] = {"game": 0, "catalog": 0}

        async def post(query: str, headers: dict[str, str]) -> list[dict]:
            if "involved_companies.company =" in query:
                calls["catalog"] += 1
                raise IGDBRequestFailure("catalog outage")
            calls["game"] += 1
            return _pedigree_game(involved=[_involved()])

        env, token, posted = self._patched(AsyncMock(side_effect=post))
        with env, token, posted:
            first = await media.get_game_media(igdb_id=1520)
            second = await media.get_game_media(igdb_id=1520)

        for result in (first, second):
            assert result is not None
            self.assertNotIn("partial", result)
            self.assertEqual(result["errors"], ["igdb: company catalog fetch failed"])
            pedigree = result["pedigree_raw"]
            self.assertEqual(pedigree["developer"]["name"], "Team Cherry")
            self.assertEqual(pedigree["previous_games"], [])
            self.assertFalse(pedigree["big_catalog"])
        # Neither the game payload nor a catalog miss was cached: both queries
        # ran again on the second call.
        self.assertEqual(calls, {"game": 2, "catalog": 2})
        self.assertIsNone(await get_meta(media._cache_key("igdb", 1520)))

    async def test_contractors_never_become_the_studio(self):
        # A developer row ALSO flagged porting or supporting is a contractor on
        # this one title, not the body of work the strip is about.
        result, _ = await self._fetch(
            _pedigree_game(
                involved=[
                    _involved(name="Port House", company_id=99, porting=True),
                    _involved(name="Helper Studio", company_id=98, supporting=True),
                    _involved(name="Team Cherry", company_id=6455),
                    _involved(name="Co Dev", company_id=6456, start_date=None),
                    _involved(
                        name="Big Publisher", company_id=42, developer=False, publisher=True
                    ),
                ],
                hypes=137,
            ),
            _catalog(3),
        )

        assert result is not None
        pedigree = result["pedigree_raw"]
        self.assertEqual(
            pedigree["developer"],
            {
                "name": "Team Cherry",
                "igdb_company_id": 6455,
                "founded_year": 2012,
                "country": 36,
            },
        )
        self.assertEqual(pedigree["developer_names"], ["Team Cherry", "Co Dev"])
        self.assertEqual(pedigree["publisher_name"], "Big Publisher")
        # Carried for completeness, never rendered by either widget.
        self.assertEqual(pedigree["hypes"], 137)

    async def test_no_qualifying_developer_means_no_pedigree_at_all(self):
        result, calls = await self._fetch(
            _pedigree_game(
                involved=[
                    _involved(name="Port House", company_id=99, porting=True),
                    _involved(
                        name="Publisher Only", company_id=42, developer=False, publisher=True
                    ),
                ]
            ),
            _catalog(3),
        )

        assert result is not None
        self.assertIsNone(result["pedigree_raw"])
        # …and no catalogue is fetched for a studio that was never identified.
        self.assertEqual(calls["catalog"], [])
        self.assertEqual(result["media"]["source"], "igdb")

    async def test_the_catalog_query_is_one_bounded_games_query(self):
        _, calls = await self._fetch(
            _pedigree_game(involved=[_involved()]), _catalog(3)
        )

        (query,) = calls["catalog"]
        self.assertIn("involved_companies.company = 6455", query)
        self.assertIn("involved_companies.developer = true", query)
        self.assertIn("sort first_release_date desc", query)
        # The bound is the whole point: company.developed is unbounded, and an
        # EA would drag five hundred entries through every card.
        self.assertIn(f"limit {media.COMPANY_CATALOG_LIMIT};", query)
        self.assertIn("aggregated_rating", query)

    async def test_only_earlier_releases_count_and_the_row_is_capped(self):
        catalog = [
            # The candidate itself, a dateless row, and a LATER release all
            # drop out; the rest are the track record.
            {"id": 1520, "name": "Hollow Knight", "first_release_date": _epoch(2017)},
            {"id": 777, "name": "Undated", "first_release_date": None},
            {
                "id": 778,
                "name": "Silksong",
                "first_release_date": _epoch(2025),
                "aggregated_rating": 95.0,
            },
            *_catalog(8, first_year=2016),
        ]
        result, _ = await self._fetch(_pedigree_game(involved=[_involved()]), catalog)

        assert result is not None
        pedigree = result["pedigree_raw"]
        self.assertEqual(pedigree["previous_count"], 8)
        self.assertEqual(len(pedigree["previous_games"]), media.PREVIOUS_GAMES_CAP)
        self.assertTrue(pedigree["previous_truncated"])
        names = [entry["name"] for entry in pedigree["previous_games"]]
        self.assertEqual(names, [f"Earlier {i}" for i in range(6)])  # newest first
        self.assertEqual(
            pedigree["previous_games"][0],
            {
                "igdb_id": 200,
                "name": "Earlier 0",
                "release_year": 2016,
                "cover_image_id": "cat0",
                "critic_score": 80,
            },
        )
        self.assertEqual(pedigree["catalog_size"], 11)
        self.assertFalse(pedigree["catalog_truncated"])
        self.assertFalse(pedigree["big_catalog"])

    async def test_a_candidate_with_no_release_date_falls_back_to_now(self):
        # An announced game has nothing to compare against, so "before now" is
        # the same question asked loosely — never "everything they ever made".
        catalog = [
            *_catalog(2, first_year=2016),
            {"id": 900, "name": "Not Out Yet", "first_release_date": _epoch(2099)},
        ]
        result, _ = await self._fetch(
            _pedigree_game(involved=[_involved()], first_release_date=None), catalog
        )

        assert result is not None
        names = [e["name"] for e in result["pedigree_raw"]["previous_games"]]
        self.assertEqual(names, ["Earlier 0", "Earlier 1"])

    async def test_the_big_studio_damper_leaves_the_header_line_alone(self):
        result, _ = await self._fetch(
            _pedigree_game(involved=[_involved(name="Ubisoft Montreal")]),
            _catalog(media.BIG_CATALOG_THRESHOLD + 1),
        )

        assert result is not None
        pedigree = result["pedigree_raw"]
        self.assertTrue(pedigree["big_catalog"])
        self.assertEqual(pedigree["previous_games"], [])
        self.assertEqual(pedigree["previous_count"], 0)
        self.assertFalse(pedigree["previous_truncated"])
        # The studio facts still stand — only the poster row is dropped.
        self.assertEqual(pedigree["catalog_size"], media.BIG_CATALOG_THRESHOLD + 1)
        self.assertEqual(pedigree["developer"]["name"], "Ubisoft Montreal")

    async def test_a_studio_at_the_threshold_still_gets_its_posters(self):
        result, _ = await self._fetch(
            _pedigree_game(involved=[_involved()]),
            _catalog(media.BIG_CATALOG_THRESHOLD, first_year=2016),
        )

        assert result is not None
        self.assertFalse(result["pedigree_raw"]["big_catalog"])
        self.assertEqual(
            len(result["pedigree_raw"]["previous_games"]), media.PREVIOUS_GAMES_CAP
        )

    async def test_a_full_page_reports_the_catalog_as_truncated(self):
        result, _ = await self._fetch(
            _pedigree_game(involved=[_involved()]),
            _catalog(media.COMPANY_CATALOG_LIMIT),
        )

        assert result is not None
        self.assertTrue(result["pedigree_raw"]["catalog_truncated"])

    async def test_the_company_catalog_is_cached_across_games_and_expires(self):
        post, calls = _dispatching_post(
            _pedigree_game(involved=[_involved()]), _catalog(3)
        )
        env, token, posted = self._patched(post)
        with env, token, posted:
            # Two different games, one studio: the catalogue is fetched once.
            await media.get_game_media(igdb_id=1520)
            await media.get_game_media(igdb_id=1521)

        self.assertEqual(len(calls["game"]), 2)
        self.assertEqual(len(calls["catalog"]), 1)
        self.assertIsNotNone(await get_meta("game_media_company:6455"))

        # A 30-day-old entry is refetched; a fresh one would not be.
        await set_meta(
            "game_media_company:6455",
            json.dumps(
                {
                    "fetched_at": (datetime.now(UTC) - timedelta(days=31)).isoformat(),
                    "payload": [],
                }
            ),
        )
        post, calls = _dispatching_post(
            _pedigree_game(involved=[_involved()]), _catalog(3)
        )
        env, token, posted = self._patched(post)
        with env, token, posted:
            await media.get_game_media(igdb_id=1522)
        self.assertEqual(len(calls["catalog"]), 1)

    async def test_a_catalog_failure_costs_the_posters_not_the_pedigree(self):
        async def post(query: str, headers: dict[str, str]) -> list[dict]:
            if "involved_companies.company =" in query:
                raise RuntimeError("igdb down")
            return _pedigree_game(involved=[_involved()])

        env, token, posted = self._patched(AsyncMock(side_effect=post))
        with env, token, posted:
            result = await media.get_game_media(igdb_id=1520)

        assert result is not None
        pedigree = result["pedigree_raw"]
        self.assertEqual(pedigree["developer"]["name"], "Team Cherry")
        self.assertEqual(pedigree["previous_games"], [])
        self.assertEqual(pedigree["catalog_size"], 0)

    async def test_a_steam_result_borrows_the_pedigree_like_similar_games(self):
        # Pedigree is IGDB-only, and Steam appids are the commonest candidate —
        # without the borrow they would never get a studio strip at all.
        factory, _ = _head_transport()
        post, _ = _dispatching_post(
            _pedigree_game(involved=[_involved()]), _catalog(2)
        )
        env, token, posted = self._patched(post)
        with (
            patch.object(
                media, "fetch_store_appdetails", AsyncMock(return_value=_appdetails())
            ),
            patch("gamelib_mcp.data.media.httpx.AsyncClient", factory),
            env,
            token,
            posted,
        ):
            result = await media.get_game_media(steam_appid=367520, igdb_id=1520)

        assert result is not None
        self.assertEqual(result["media"]["source"], "steam")
        self.assertEqual(result["pedigree_raw"]["developer"]["name"], "Team Cherry")

    async def test_a_pre_pedigree_cache_entry_is_not_served(self):
        # The cached payload has no schema, so the ONLY way a widened shape
        # refetches instead of rendering half a card for a week is a key the
        # old entries are not the answer to.
        await set_meta(
            "game_media:igdb:1520",
            json.dumps(
                {
                    "fetched_at": datetime.now(UTC).isoformat(),
                    "payload": {
                        "media": {"source": "igdb"},
                        "similar_raw": None,
                        "similar_count": None,
                        "igdb_id": 1520,
                    },
                }
            ),
        )
        result, calls = await self._fetch(
            _pedigree_game(involved=[_involved()]), _catalog(2)
        )

        assert result is not None
        self.assertEqual(len(calls["game"]), 1)
        self.assertIsNotNone(result["pedigree_raw"])
        self.assertIn(media.MEDIA_CACHE_VERSION, media._cache_key("igdb", 1520))


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
            first = await media.get_game_media(name="Transient")
            second = await media.get_game_media(name="Transient")

        # Reported, not disguised as "this game has no media".
        for result in (first, second):
            assert result is not None
            self.assertIsNone(result["media"])
            self.assertEqual(result["errors"], ["igdb: name resolution failed"])
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
            "pedigree_raw": None,
            "igdb_id": None,
        }

    async def test_a_fresh_cache_entry_skips_the_fetch(self):
        await self._seed_cache(
            media._cache_key("steam", 5), self._payload("cached"), age=timedelta(days=1)
        )
        store = AsyncMock(return_value=_appdetails())
        with patch.object(media, "fetch_store_appdetails", store):
            result = await media.get_game_media(steam_appid=5)

        assert result is not None
        self.assertEqual(result["media"]["short_description"], "cached")
        store.assert_not_awaited()

    async def test_an_expired_entry_is_refetched(self):
        await self._seed_cache(
            media._cache_key("steam", 5), self._payload("stale"), age=timedelta(days=8)
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
            media._cache_key("steam", 5), self._payload("stale"), age=timedelta(days=30)
        )
        with patch.object(
            media, "fetch_store_appdetails", AsyncMock(side_effect=RuntimeError("down"))
        ):
            result = await media.get_game_media(steam_appid=5)

        assert result is not None
        self.assertEqual(result["media"]["short_description"], "stale")
        # The stale copy is served, never overwritten by the failure.
        cached = json.loads(await get_meta(media._cache_key("steam", 5)))
        self.assertEqual(cached["payload"]["media"]["short_description"], "stale")

    async def test_a_source_that_suddenly_returns_nothing_serves_the_stale_payload(self):
        await self._seed_cache(
            media._cache_key("steam", 5), self._payload("stale"), age=timedelta(days=30)
        )
        with patch.object(media, "fetch_store_appdetails", AsyncMock(return_value=None)):
            result = await media.get_game_media(steam_appid=5)

        assert result is not None
        self.assertEqual(result["media"]["short_description"], "stale")

    async def test_an_expired_miss_is_retried(self):
        await self._seed_cache(media._cache_key("steam", 5), None, age=timedelta(hours=25))
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
