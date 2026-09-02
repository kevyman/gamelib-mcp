import logging
import os
import unittest
from unittest.mock import patch

from gamelib_mcp.env import load_project_dotenv


def test_load_project_dotenv_skips_fifo_without_opening(tmp_path):
    dotenv_path = tmp_path / ".env"
    os.mkfifo(dotenv_path)

    with patch("gamelib_mcp.env.load_dotenv") as mock_load_dotenv:
        loaded = load_project_dotenv(dotenv_path)

    assert loaded is False
    mock_load_dotenv.assert_not_called()


class LogLevelFromEnvTests(unittest.TestCase):
    def test_defaults_to_info(self):
        from gamelib_mcp import main

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOG_LEVEL", None)
            self.assertEqual(main._log_level_from_env(), logging.INFO)

    def test_parses_named_level_case_insensitively(self):
        from gamelib_mcp import main

        with patch.dict(os.environ, {"LOG_LEVEL": " debug "}):
            self.assertEqual(main._log_level_from_env(), logging.DEBUG)

    def test_unknown_value_falls_back_to_info(self):
        from gamelib_mcp import main

        with patch.dict(os.environ, {"LOG_LEVEL": "verbose"}):
            self.assertEqual(main._log_level_from_env(), logging.INFO)
