import unittest
from unittest.mock import patch

from gamelib_mcp.tools.integrations import get_integration_status


class IntegrationToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_integration_status_returns_filtered_platforms(self) -> None:
        fake_payload = {
            "epic": {"platform": "epic", "overall_status": "ready"},
            "gog": {"platform": "gog", "overall_status": "unconfigured"},
        }

        with patch(
            "gamelib_mcp.tools.integrations.inspect_all_integrations_dict",
            return_value=fake_payload,
        ):
            result = await get_integration_status(["epic"])

        self.assertEqual(
            result,
            {"epic": {"platform": "epic", "overall_status": "ready"}},
        )

    async def test_get_integration_status_treats_empty_platform_filter_as_empty_result(self) -> None:
        fake_payload = {
            "epic": {"platform": "epic", "overall_status": "ready"},
            "gog": {"platform": "gog", "overall_status": "unconfigured"},
        }

        with patch(
            "gamelib_mcp.tools.integrations.inspect_all_integrations_dict",
            return_value=fake_payload,
        ):
            result = await get_integration_status([])

        self.assertEqual(result, {})

    async def test_get_integration_status_returns_compact_payload_when_not_verbose(self) -> None:
        fake_payload = {
            "epic": {
                "platform": "epic",
                "overall_status": "ready",
                "summary": "Legendary credentials and metadata cache are present.",
                "active_backend": "legendary-cache",
                "checks": [{"name": "legendary_user_json", "status": "pass"}],
            }
        }

        with patch(
            "gamelib_mcp.tools.integrations.inspect_all_integrations_dict",
            return_value=fake_payload,
        ):
            result = await get_integration_status(verbose=False)

        self.assertEqual(
            result,
            {
                "epic": {
                    "overall_status": "ready",
                    "summary": "Legendary credentials and metadata cache are present.",
                    "active_backend": "legendary-cache",
                }
            },
        )
