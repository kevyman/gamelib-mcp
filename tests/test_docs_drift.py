"""Keeps the user-facing docs honest about the tool surface.

The README's tool table and the .env examples went a month out of date after
the assessment tools shipped (audit 2026-09-01, item 8): five tools missing
from the README, four names in .env.example that no tool has carried since
ADR 0004's consolidation. Two directions are checked here — every tool-shaped
name a doc mentions is a registered tool, and every registered tool is
mentioned in the README — plus one hygiene rule (no links to a maintainer's
local absolute path). Historical documents under docs/ are deliberately out of
scope: plans and specs describe the tools as they were designed, not as they
are.
"""

import re
import unittest
from pathlib import Path

from gamelib_mcp import main

REPO = Path(__file__).resolve().parent.parent

# Docs a new deployer or contributor reads. Anything here must name only tools
# that exist today.
USER_FACING_DOCS = ("README.md", ".env.example", ".env.local.example", "LOCAL_DOCKER.md", "deploy.md")

# A "tool-shaped" identifier: one of the verb prefixes the tool surface uses,
# an underscore, then more snake_case. Bare `sync` is matched separately via
# backticks. Table/column names (game_platforms, tag_affinity, ...) start with
# nouns and never match.
_TOOL_VERBS = (
    "get", "set", "sync", "add", "update", "delete", "merge", "split", "import",
    "create", "record", "manage", "rate", "check", "discover", "search", "query", "void",
)
_TOOL_SHAPED = re.compile(r"\b((?:" + "|".join(_TOOL_VERBS) + r")_[a-z0-9_]+)\b")
_BACKTICK_SPAN = re.compile(r"`([^`\n]+)`")
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*")
_LOCAL_ABSOLUTE_LINK = re.compile(r"\]\((/home/|/Users/|[A-Za-z]:\\)")

# Tool-shaped names a doc may legitimately use that are not tools. Empty today;
# add a name here with a comment rather than loosening the regex.
_ALLOWED_NON_TOOLS: frozenset[str] = frozenset()


class DocsToolReferenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tools = {tool.name for tool in await main.mcp.list_tools()}

    def _read(self, name: str) -> str:
        return (REPO / name).read_text(encoding="utf-8")

    async def test_docs_name_only_registered_tools(self) -> None:
        stale: dict[str, list[str]] = {}
        for doc in USER_FACING_DOCS:
            mentioned = set(_TOOL_SHAPED.findall(self._read(doc)))
            unknown = sorted(mentioned - self.tools - _ALLOWED_NON_TOOLS)
            if unknown:
                stale[doc] = unknown
        self.assertEqual(
            stale, {},
            f"docs name tools that are not registered: {stale} — the tool was "
            "renamed or merged (see ADR 0004); update the doc, or add a "
            "justified entry to _ALLOWED_NON_TOOLS",
        )

    async def test_readme_mentions_every_tool(self) -> None:
        readme = self._read("README.md")
        in_backticks: set[str] = set()
        for span in _BACKTICK_SPAN.findall(readme):
            in_backticks.update(_IDENTIFIER.findall(span))
        missing = sorted(self.tools - in_backticks)
        self.assertEqual(
            missing, [],
            f"registered tools absent from README.md: {missing} — add a row to "
            "the MCP Tools table",
        )
        declared = re.search(r"^(\d+) tools\.", readme, re.MULTILINE)
        self.assertIsNotNone(declared, "README's tool table header should state the count")
        assert declared is not None
        self.assertEqual(
            int(declared.group(1)), len(self.tools),
            "README's stated tool count drifted from the registered surface",
        )

    def test_no_links_to_local_absolute_paths(self) -> None:
        offenders = [
            doc for doc in USER_FACING_DOCS
            if _LOCAL_ABSOLUTE_LINK.search(self._read(doc))
        ]
        self.assertEqual(
            offenders, [],
            "markdown links must be repo-relative, not a maintainer's local path",
        )
