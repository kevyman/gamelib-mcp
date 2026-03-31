import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from steam_mcp.data import epic


class EpicHelpersTests(unittest.TestCase):
    def test_fetch_epic_library_reads_cached_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            metadata_dir = Path(tmpdir) / "metadata"
            metadata_dir.mkdir(parents=True)
            (metadata_dir / "game.json").write_text(
                json.dumps(
                    {
                        "app_name": "artifact-1",
                        "app_title": "Test Game",
                        "asset_infos": {
                            "Windows": {
                                "asset_id": "artifact-1",
                                "app_name": "artifact-1",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"EPIC_LEGENDARY_PATH": tmpdir}, clear=False):
                games = asyncio.run(epic.fetch_epic_library())

        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["app_title"], "Test Game")

    def test_extract_epic_artifact_id_prefers_asset_id(self) -> None:
        artifact_id = epic._extract_epic_artifact_id(
            {
                "app_name": "launcher-name",
                "asset_infos": {
                    "Windows": {
                        "asset_id": "artifact-123",
                        "app_name": "launcher-name",
                    }
                },
            }
        )

        self.assertEqual(artifact_id, "artifact-123")

    def test_fetch_epic_playtime_maps_artifact_ids(self) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"artifactId": "artifact-1", "totalTime": 123},
            {"artifactId": "artifact-2", "totalTime": "456"},
            {"artifactId": "artifact-3", "totalTime": 3600},
        ]
        mock_response.raise_for_status.return_value = None

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def get(self, *_args, **_kwargs):
                return mock_response

        with (
            patch(
                "steam_mcp.data.epic._get_epic_session",
                AsyncMock(
                    return_value={
                        "account_id": "acct-1",
                        "access_token": "token-1",
                        "refresh_token": "refresh-1",
                    }
                ),
            ),
            patch("steam_mcp.data.epic.httpx.AsyncClient", return_value=_FakeClient()),
        ):
            playtime = asyncio.run(epic.fetch_epic_playtime())

        self.assertEqual(playtime, {"artifact-1": 2, "artifact-2": 7, "artifact-3": 60})
