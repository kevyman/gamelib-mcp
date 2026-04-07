import os
import unittest
from unittest.mock import AsyncMock, patch

from gamelib_mcp.data import opencritic


class OpenCriticTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_game_skips_anonymous_requests_when_api_key_is_missing(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(opencritic.httpx, "AsyncClient") as client_cls,
        ):
            result = await opencritic._search_game("Loop Hero")

        self.assertIsNone(result)
        client_cls.assert_not_called()

