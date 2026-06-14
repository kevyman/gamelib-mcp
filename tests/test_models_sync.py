"""Schema tests for the sync ack + status response models."""

import unittest

from gamelib_mcp.tools.models import RefreshLibraryResponse, SyncStatusResponse


class SyncModelTests(unittest.TestCase):
    def test_refresh_ack_shape(self):
        m = RefreshLibraryResponse(
            status="started", platforms=["steam", "gog"], already_running=False
        )
        self.assertEqual(m.status, "started")
        self.assertEqual(m.platforms, ["steam", "gog"])
        self.assertFalse(m.already_running)

    def test_sync_status_shape(self):
        m = SyncStatusResponse(
            status="in_progress",
            started_at="2026-06-14T12:00:00+00:00",
            finished_at=None,
            platforms={"steam": {"state": "done", "last_success_at": None, "error": None}},
        )
        self.assertEqual(m.status, "in_progress")
        self.assertEqual(m.platforms["steam"]["state"], "done")
