"""Tests for the skill:// MCP resources (ADR 0006 decision 4)."""

import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastmcp import Client, FastMCP

from gamelib_mcp import skill_resources


class SkillResourcesRegisteredTests(unittest.IsolatedAsyncioTestCase):
    """Registers against the real repo skills/ directory."""

    async def test_resources_list_includes_skill_and_index_uris(self) -> None:
        mcp = FastMCP("test")
        skill_resources.register_skill_resources(mcp)

        async with Client(mcp) as client:
            resources = await client.list_resources()
            uris = {str(r.uri) for r in resources}

        self.assertIn("skill://index.json", uris)
        self.assertIn("skill://backlog-triage/SKILL.md", uris)
        self.assertIn("skill://game-quality/SKILL.md", uris)
        # ADR 0006 stage 4: the craft/fit scripts moved server-side
        # (get_assessment_context) and skills/game-quality/scripts/ was
        # removed — game-quality is SKILL.md-only now, like backlog-triage.
        self.assertFalse(any("scripts/" in uri for uri in uris))
        # skills/README.md is install docs, not a skill directory — must not
        # be registered under any skill:// URI.
        self.assertFalse(any("README" in uri for uri in uris))

    async def test_read_skill_md_returns_real_content(self) -> None:
        mcp = FastMCP("test")
        skill_resources.register_skill_resources(mcp)

        async with Client(mcp) as client:
            content = await client.read_resource("skill://backlog-triage/SKILL.md")

        text = content[0].text
        on_disk = (
            skill_resources.SKILLS_DIR / "backlog-triage" / "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertEqual(text, on_disk)
        self.assertIn("name: backlog-triage", text)

    async def test_game_quality_is_skill_md_only(self) -> None:
        # ADR 0006 stage 4: the craft/fit scripts moved server-side
        # (get_assessment_context) and skills/game-quality/scripts/ was
        # removed, so game-quality no longer ships any supporting files.
        mcp = FastMCP("test")
        skill_resources.register_skill_resources(mcp)

        async with Client(mcp) as client:
            resources = await client.list_resources()
            uris = {str(r.uri) for r in resources}

        quality_uris = {u for u in uris if u.startswith("skill://game-quality/")}
        self.assertEqual(quality_uris, {"skill://game-quality/SKILL.md"})

    async def test_index_json_lists_each_skill_with_frontmatter_fields(self) -> None:
        mcp = FastMCP("test")
        skill_resources.register_skill_resources(mcp)

        async with Client(mcp) as client:
            content = await client.read_resource("skill://index.json")

        payload = json.loads(content[0].text)
        by_name = {entry["name"]: entry for entry in payload}

        self.assertIn("backlog-triage", by_name)
        self.assertIn("game-quality", by_name)

        backlog_entry = by_name["backlog-triage"]
        self.assertEqual(backlog_entry["version"], "2.1.0")
        self.assertIn("play next", backlog_entry["description"])
        self.assertIn("skill://backlog-triage/SKILL.md", backlog_entry["files"])

        quality_entry = by_name["game-quality"]
        self.assertEqual(quality_entry["version"], "2.1.0")
        self.assertEqual(quality_entry["files"], ["skill://game-quality/SKILL.md"])

    async def test_index_resource_reports_json_mime_type(self) -> None:
        mcp = FastMCP("test")
        skill_resources.register_skill_resources(mcp)

        async with Client(mcp) as client:
            resources = await client.list_resources()

        index_resource = next(r for r in resources if str(r.uri) == "skill://index.json")
        self.assertEqual(index_resource.mimeType, "application/json")

    async def test_edited_file_is_served_fresh_without_reregistration(self) -> None:
        # Content is read lazily at request time, not captured at registration.
        with tempfile_skill_layout() as skills_dir:
            with patch.object(skill_resources, "SKILLS_DIR", skills_dir):
                mcp = FastMCP("test")
                skill_resources.register_skill_resources(mcp)

                skill_md = skills_dir / "demo-skill" / "SKILL.md"
                skill_md.write_text(
                    skill_md.read_text(encoding="utf-8") + "\nEdited after registration.\n",
                    encoding="utf-8",
                )

                async with Client(mcp) as client:
                    content = await client.read_resource("skill://demo-skill/SKILL.md")

        self.assertIn("Edited after registration.", content[0].text)


class SkillResourcesRealAppTests(unittest.IsolatedAsyncioTestCase):
    """Sanity check against the actual server app, not a bare FastMCP()."""

    async def test_real_app_serves_skill_index(self) -> None:
        from gamelib_mcp.main import mcp

        async with Client(mcp) as client:
            resources = await client.list_resources()
            uris = {str(r.uri) for r in resources}
            self.assertIn("skill://index.json", uris)

            content = await client.read_resource("skill://index.json")
            payload = json.loads(content[0].text)
            names = {entry["name"] for entry in payload}
            self.assertIn("backlog-triage", names)
            self.assertIn("game-quality", names)

        self.assertIn("skill://index.json", mcp.instructions)


class SkillResourcesMissingDirTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_skills_dir_registers_nothing_and_logs_warning(self) -> None:
        missing = Path("/nonexistent/definitely-not-here/skills")
        self.assertFalse(missing.exists())

        with patch.object(skill_resources, "SKILLS_DIR", missing):
            mcp = FastMCP("test")
            with self.assertLogs(skill_resources.logger, level="WARNING") as log_ctx:
                skill_resources.register_skill_resources(mcp)

            self.assertTrue(any("skills/ directory not found" in msg for msg in log_ctx.output))

            async with Client(mcp) as client:
                resources = await client.list_resources()

        self.assertEqual(resources, [])

    async def test_skills_dir_with_no_valid_skill_folders_registers_nothing(self) -> None:
        with tempfile_empty_dir() as empty_dir:
            with patch.object(skill_resources, "SKILLS_DIR", empty_dir):
                mcp = FastMCP("test")
                with self.assertLogs(skill_resources.logger, level="WARNING") as log_ctx:
                    skill_resources.register_skill_resources(mcp)

                self.assertTrue(any("No skill files found" in msg for msg in log_ctx.output))

                async with Client(mcp) as client:
                    resources = await client.list_resources()

        self.assertEqual(resources, [])


class ParseFrontmatterTests(unittest.TestCase):
    def test_parses_name_description_version(self) -> None:
        text = (
            "---\n"
            "name: demo-skill\n"
            'description: Uses "quotes" and stays on one line.\n'
            'version: "1.2.3"\n'
            "---\n\n# Body\n"
        )
        fields = skill_resources._parse_frontmatter(text)
        self.assertEqual(fields["name"], "demo-skill")
        self.assertEqual(fields["description"], 'Uses "quotes" and stays on one line.')
        self.assertEqual(fields["version"], "1.2.3")

    def test_missing_frontmatter_returns_empty(self) -> None:
        self.assertEqual(skill_resources._parse_frontmatter("# Just a heading\n"), {})

    def test_unterminated_frontmatter_returns_empty(self) -> None:
        self.assertEqual(skill_resources._parse_frontmatter("---\nname: x\n"), {})


@contextlib.contextmanager
def tempfile_skill_layout():
    with tempfile.TemporaryDirectory() as tmp:
        skills_dir = Path(tmp)
        skill_dir = skills_dir / "demo-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: demo-skill\ndescription: A demo skill.\nversion: \"1.0.0\"\n---\n\nBody.\n",
            encoding="utf-8",
        )
        yield skills_dir


@contextlib.contextmanager
def tempfile_empty_dir():
    with tempfile.TemporaryDirectory() as tmp:
        # An empty skills/ dir (or one with only non-skill files, like
        # README.md) has no SKILL.md anywhere, so nothing registers.
        (Path(tmp) / "README.md").write_text("Install docs.\n", encoding="utf-8")
        yield Path(tmp)


if __name__ == "__main__":
    unittest.main()
