import unittest

from gamelib_mcp.tools.integrations import get_integration_status


class IntegrationToolsTests(unittest.TestCase):
    def test_get_integration_status_returns_filtered_platforms(self) -> None:
        payload = {
            "epic": {"platform": "epic", "overall_status": "ready"},
            "gog": {"platform": "gog", "overall_status": "unconfigured"},
        }
        result = get_integration_status(payload, ["epic"])
        self.assertEqual(result, {"epic": {"platform": "epic", "overall_status": "ready"}})

    def test_get_integration_status_treats_empty_platform_filter_as_empty_result(self) -> None:
        payload = {
            "epic": {"platform": "epic", "overall_status": "ready"},
            "gog": {"platform": "gog", "overall_status": "unconfigured"},
        }
        result = get_integration_status(payload, [])
        self.assertEqual(result, {})

    def test_get_integration_status_returns_compact_payload_when_not_verbose(self) -> None:
        payload = {
            "epic": {
                "platform": "epic",
                "overall_status": "ready",
                "summary": "Legendary credentials and metadata cache are present.",
                "active_backend": "legendary-cache",
                "checks": [{"name": "legendary_user_json", "status": "pass"}],
            }
        }
        result = get_integration_status(payload, verbose=False)
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
