"""Tests for the skill:// MCP resources (ADR 0006 decision 4) and their
tool twin, get_skill (decision 4b)."""

import contextlib
import importlib.util
import json
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import yaml  # transitively pinned via the MCP SDK stack; used to prove stub validity
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError

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
        self.assertIn("skill://bundle-evaluation/SKILL.md", uris)
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
        self.assertIn("bundle-evaluation", by_name)

        backlog_entry = by_name["backlog-triage"]
        self.assertRegex(backlog_entry["version"], r"^\d+\.\d+\.\d+$")
        self.assertIn("play next", backlog_entry["description"])
        self.assertIn("skill://backlog-triage/SKILL.md", backlog_entry["files"])

        quality_entry = by_name["game-quality"]
        self.assertRegex(quality_entry["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(quality_entry["files"], ["skill://game-quality/SKILL.md"])

        bundle_entry = by_name["bundle-evaluation"]
        self.assertRegex(bundle_entry["version"], r"^\d+\.\d+\.\d+$")
        self.assertIn("bundle", bundle_entry["description"].lower())
        self.assertEqual(bundle_entry["files"], ["skill://bundle-evaluation/SKILL.md"])

    async def test_index_resource_reports_json_mime_type(self) -> None:
        mcp = FastMCP("test")
        skill_resources.register_skill_resources(mcp)

        async with Client(mcp) as client:
            resources = await client.list_resources()

        index_resource = next(r for r in resources if str(r.uri) == "skill://index.json")
        self.assertEqual(index_resource.mimeType, "application/json")

    async def test_edited_file_is_served_fresh_without_reregistration(self) -> None:
        # Content is read lazily at request time, not captured at registration.
        with (
            tempfile_skill_layout() as skills_dir,
            patch.object(skill_resources, "SKILLS_DIR", skills_dir),
        ):
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

    async def test_skill_added_after_startup_reaches_the_resource_surface_too(self) -> None:
        # Enumeration is as fresh as content: the index rebuilds from disk
        # per read, and the skill://{skill}/{path*} wildcard template serves
        # files whose concrete URIs weren't registered at startup — so the
        # resources never lag behind what get_skill's per-call scan reports.
        with (
            tempfile_skill_layout() as skills_dir,
            patch.object(skill_resources, "SKILLS_DIR", skills_dir),
        ):
            mcp = FastMCP("test")
            skill_resources.register_skill_resources(mcp)

            late_dir = skills_dir / "late-skill"
            late_dir.mkdir()
            (late_dir / "SKILL.md").write_text(
                '---\nname: late-skill\ndescription: Arrived late.\nversion: "0.1.0"\n---\n\nLate body.\n',
                encoding="utf-8",
            )

            async with Client(mcp) as client:
                index = json.loads(
                    (await client.read_resource("skill://index.json"))[0].text
                )
                self.assertIn("late-skill", {entry["name"] for entry in index})

                content = await client.read_resource("skill://late-skill/SKILL.md")
                self.assertIn("Late body.", content[0].text)

                # The whitelist guard holds on the template path as well.
                with self.assertRaises(McpError):
                    await client.read_resource("skill://late-skill/no-such-file.md")


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
            self.assertIn("bundle-evaluation", names)

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
        with (
            tempfile_empty_dir() as empty_dir,
            patch.object(skill_resources, "SKILLS_DIR", empty_dir),
        ):
            mcp = FastMCP("test")
            with self.assertLogs(skill_resources.logger, level="WARNING") as log_ctx:
                skill_resources.register_skill_resources(mcp)

            self.assertTrue(any("No skill files found" in msg for msg in log_ctx.output))

            async with Client(mcp) as client:
                resources = await client.list_resources()

        self.assertEqual(resources, [])


class ResolveSkillsDirTests(unittest.TestCase):
    def test_packaged_copy_wins_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "site-packages" / "gamelib_mcp"
            (pkg / "skills").mkdir(parents=True)
            self.assertEqual(skill_resources._resolve_skills_dir(pkg), pkg / "skills")

    def test_repo_sibling_used_without_packaged_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "repo" / "gamelib_mcp"
            pkg.mkdir(parents=True)
            self.assertEqual(
                skill_resources._resolve_skills_dir(pkg), pkg.parent / "skills"
            )


class SkillPackagingDriftTests(unittest.TestCase):
    """Deployed artifacts must carry skills/ — without these, Docker and
    wheel installs take the fail-soft path and serve zero skill:// resources
    while the server instructions still advertise skill://index.json."""

    REPO_ROOT = Path(__file__).resolve().parent.parent

    def test_wheel_force_includes_skills(self) -> None:
        pyproject = (self.REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("[tool.hatch.build.targets.wheel.force-include]", pyproject)
        self.assertIn('"skills" = "gamelib_mcp/skills"', pyproject)

    def test_docker_image_copies_skills(self) -> None:
        dockerfile = (self.REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY skills/ skills/", dockerfile)


class SkillToolReferenceDriftTests(unittest.IsolatedAsyncioTestCase):
    """Every tool call written in a SKILL.md must exist on the wire surface.

    ADR 0006's motivating failure: the installed skills kept referencing
    get_taste_profile, get_backlog_stats, get_spending_stats,
    get_series_breakdown, get_wishlist_deals and search_games_batch for
    months after ADR 0004 consolidated them into get_stats(report=...),
    get_wishlist and search_games. Nothing failed loudly — clients just
    guessed at the replacement on every run. Now that the skill text is
    canonical here, that drift class is a red test instead of a review item.
    """

    # snake_case identifier immediately followed by an open paren.
    CALL_RE = re.compile(r"\b([a-z][a-z0-9_]{3,})\(")
    # A backticked identifier with no call syntax — `get_game_detail`. Skills
    # name tools this way constantly, and a call-syntax-only scan misses them
    # entirely, so a rename would leave this suite green and drift right back.
    # Matched on the tool surface's verb prefixes so that response fields and
    # columns (`bundle_name`, `price_paid`, `last_seen_in_source`) — which are
    # backticked just as often — are not mistaken for tools.
    BACKTICK_RE = re.compile(
        r"`((?:get|set|add|split|merge|delete|update|check|search|discover|import"
        r"|rate|query|manage|create)_[a-z0-9_]+)`"
    )
    # `foo=` that is not part of `==`, `!=`, `<=`, `>=`.
    KWARG_RE = re.compile(r"(?<![=!<>])\b([a-z][a-z0-9_]*)\s*=(?!=)")
    STRING_KWARG_RE = re.compile(r'\b([a-z][a-z0-9_]*)\s*=\s*"([^"]*)"')

    # Tool names that were consolidated away by ADR 0004. A skill naming one
    # of these is drift even in prose, where the call-form scan can't see it.
    RETIRED_TOOL_NAMES = (
        "get_taste_profile",
        "get_backlog_stats",
        "get_spending_stats",
        "get_series_breakdown",
        "get_wishlist_deals",
        "search_games_batch",
    )

    async def _tool_schemas(self) -> dict[str, dict]:
        from gamelib_mcp.main import mcp

        async with Client(mcp) as client:
            tools = await client.list_tools()
        return {tool.name: (tool.inputSchema or {}) for tool in tools}

    def _skill_texts(self) -> dict[str, str]:
        return {
            path.parent.name: path.read_text(encoding="utf-8")
            for path in sorted(skill_resources.SKILLS_DIR.glob("*/SKILL.md"))
        }

    @classmethod
    def _calls(cls, text: str) -> list[tuple[str, str]]:
        """Yield (name, argument-span) for every `name(...)` in the text.

        Scans forward with paren matching so multi-line call examples and
        nested brackets (`tags=["a", "b"]`) come back whole.
        """
        calls = []
        for match in cls.CALL_RE.finditer(text):
            depth, index = 0, match.end() - 1
            while index < len(text):
                if text[index] == "(":
                    depth += 1
                elif text[index] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                index += 1
            calls.append((match.group(1), text[match.end() : index]))
        return calls

    async def test_referenced_tools_are_all_registered(self) -> None:
        schemas = await self._tool_schemas()
        offenders: dict[str, set[str]] = {}

        for skill_name, text in self._skill_texts().items():
            referenced = {name for name, _ in self._calls(text)}
            # A snake_case call form in a skill is a tool reference; keep the
            # underscore-free ones out so ordinary prose like "score(n)" or a
            # formula never registers as drift. Crucially this filter does NOT
            # consult the registered set, so a RETIRED name still gets caught.
            candidates = {name for name in referenced if "_" in name or name in schemas}
            unknown = candidates - schemas.keys()
            if unknown:
                offenders[skill_name] = unknown

        self.assertEqual(
            offenders,
            {},
            f"SKILL.md references tools that are not registered: {offenders}",
        )

    async def test_standalone_backticked_tool_names_are_registered(self) -> None:
        schemas = await self._tool_schemas()
        offenders: dict[str, set[str]] = {}

        for skill_name, text in self._skill_texts().items():
            named = set(self.BACKTICK_RE.findall(text))
            unknown = named - schemas.keys()
            if unknown:
                offenders[skill_name] = unknown

        self.assertEqual(
            offenders,
            {},
            f"SKILL.md names tools (without call syntax) that are not registered: {offenders}",
        )

    async def test_referenced_tool_parameters_exist_on_their_schemas(self) -> None:
        schemas = await self._tool_schemas()
        offenders: dict[str, set[str]] = {}

        for skill_name, text in self._skill_texts().items():
            for tool_name, args in self._calls(text):
                schema = schemas.get(tool_name)
                if schema is None:
                    continue  # covered by the registration test above
                allowed = set(schema.get("properties", {}))
                used = set(self.KWARG_RE.findall(args))
                unknown = used - allowed
                if unknown:
                    offenders[f"{skill_name}:{tool_name}"] = unknown

        self.assertEqual(
            offenders,
            {},
            f"SKILL.md passes parameters no tool accepts: {offenders}",
        )

    async def test_referenced_enum_values_are_valid(self) -> None:
        schemas = await self._tool_schemas()
        offenders: dict[str, str] = {}

        for skill_name, text in self._skill_texts().items():
            for tool_name, args in self._calls(text):
                schema = schemas.get(tool_name)
                if schema is None:
                    continue
                properties = schema.get("properties", {})
                for param, value in self.STRING_KWARG_RE.findall(args):
                    choices = properties.get(param, {}).get("enum")
                    # Skip placeholder values in illustrative call examples.
                    if not choices or "..." in value:
                        continue
                    if value not in choices:
                        offenders[f"{skill_name}:{tool_name}({param})"] = value

        self.assertEqual(
            offenders,
            {},
            f"SKILL.md uses values outside a tool's enum: {offenders}",
        )

    def test_no_skill_names_a_retired_tool(self) -> None:
        offenders: dict[str, list[str]] = {}
        for skill_name, text in self._skill_texts().items():
            named = [old for old in self.RETIRED_TOOL_NAMES if old in text]
            if named:
                offenders[skill_name] = named

        self.assertEqual(
            offenders,
            {},
            f"SKILL.md names pre-ADR-0004 tools that no longer exist: {offenders}",
        )


class GetSkillToolTests(unittest.IsolatedAsyncioTestCase):
    """get_skill (ADR 0006 decision 4b): the resources' tool twin, for hosts
    whose model cannot call resources/read (claude.ai custom connectors)."""

    async def test_index_mode_matches_resource_index(self) -> None:
        from gamelib_mcp import main

        response = await main.get_skill()
        by_name = {entry.name: entry for entry in response.skills}

        async with Client(main.mcp) as client:
            content = await client.read_resource("skill://index.json")
        resource_index = {entry["name"]: entry for entry in json.loads(content[0].text)}

        self.assertEqual(set(by_name), set(resource_index))
        for name, entry in by_name.items():
            resource_entry = resource_index[name]
            self.assertEqual(entry.description, resource_entry["description"])
            self.assertEqual(entry.version, resource_entry["version"])
            # The tool lists relative paths; the resource lists the same
            # files as skill:// URIs.
            self.assertEqual(
                [f"skill://{name}/{rel}" for rel in entry.files],
                resource_entry["files"],
            )
        self.assertIsNone(response.note)

    async def test_file_mode_returns_exact_disk_bytes(self) -> None:
        from gamelib_mcp import main

        response = await main.get_skill(skill="game-quality")
        on_disk = (
            skill_resources.SKILLS_DIR / "game-quality" / "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertEqual(response.content, on_disk)
        self.assertEqual(response.skill, "game-quality")
        self.assertEqual(response.path, "SKILL.md")
        self.assertEqual(response.version, "3.2.0")

    async def test_wire_call_returns_structured_content(self) -> None:
        from gamelib_mcp import main

        async with Client(main.mcp) as client:
            result = await client.call_tool("get_skill", {"skill": "backlog-triage"})
        self.assertIn("name: backlog-triage", result.data.content)

    async def test_skill_added_after_startup_is_served_without_restart(self) -> None:
        # The tool re-scans the skills directory per call, unlike the
        # resources (enumerated once at registration).
        from gamelib_mcp import main

        with (
            tempfile_skill_layout() as skills_dir,
            patch.object(skill_resources, "SKILLS_DIR", skills_dir),
        ):
            names = {entry.name for entry in (await main.get_skill()).skills}
            self.assertEqual(names, {"demo-skill"})

            late_dir = skills_dir / "late-skill"
            late_dir.mkdir()
            (late_dir / "SKILL.md").write_text(
                '---\nname: late-skill\ndescription: Arrived late.\nversion: "0.1.0"\n---\n\nBody.\n',
                encoding="utf-8",
            )
            names = {entry.name for entry in (await main.get_skill()).skills}
            self.assertEqual(names, {"demo-skill", "late-skill"})
            self.assertIn("Body.", (await main.get_skill(skill="late-skill")).content)

    async def test_unknown_skill_is_a_tool_error_naming_available(self) -> None:
        from gamelib_mcp import main

        with self.assertRaises(ToolError) as ctx:
            await main.get_skill(skill="no-such-skill")
        self.assertIn("game-quality", str(ctx.exception))

    async def test_unknown_path_is_a_tool_error_naming_files(self) -> None:
        from gamelib_mcp import main

        with self.assertRaises(ToolError) as ctx:
            await main.get_skill(skill="game-quality", path="scripts/craft_score.py")
        self.assertIn("SKILL.md", str(ctx.exception))

    async def test_traversal_path_is_rejected(self) -> None:
        # skills/README.md exists on disk one level above the skill dir; a
        # free-form path must not be able to reach it (or anything else
        # outside the scanned file list).
        from gamelib_mcp import main

        for attempt in ("../README.md", "/etc/hostname", "..\\README.md"):
            with self.subTest(path=attempt), self.assertRaises(ToolError):
                await main.get_skill(skill="game-quality", path=attempt)

    async def test_path_without_skill_is_a_tool_error(self) -> None:
        from gamelib_mcp import main

        with self.assertRaises(ToolError):
            await main.get_skill(path="README.md")

    async def test_missing_skills_dir_returns_empty_index_with_note(self) -> None:
        from gamelib_mcp import main

        missing = Path("/nonexistent/definitely-not-here/skills")
        with patch.object(skill_resources, "SKILLS_DIR", missing):
            response = await main.get_skill()
            self.assertEqual(response.skills, [])
            self.assertIn("missing", response.note)

            with self.assertRaises(ToolError) as ctx:
                await main.get_skill(skill="game-quality")
        self.assertIn("none", str(ctx.exception))

    async def test_instructions_point_at_both_surfaces(self) -> None:
        from gamelib_mcp.main import mcp

        self.assertIn("get_skill", mcp.instructions)
        self.assertIn("skill://index.json", mcp.instructions)


class PackageSkillsScriptTests(unittest.TestCase):
    """scripts/package_skills.py — the trigger-stub build for tools-only
    hosts (claude.ai Skills zip upload, ~/.claude/skills copies)."""

    @classmethod
    def setUpClass(cls) -> None:
        script = Path(__file__).resolve().parent.parent / "scripts" / "package_skills.py"
        spec = importlib.util.spec_from_file_location("package_skills", script)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    @staticmethod
    def _strict_yaml_frontmatter(stub: str) -> dict:
        """Parse a stub's frontmatter with a real YAML parser.

        Deliberately NOT _parse_frontmatter: the upload target may parse
        strictly, and the descriptions contain ': ' and quotes, so anything
        short of valid YAML is a rejected zip (Codex P1 on PR #142).
        """
        block = stub.split("---\n")[1]
        return yaml.safe_load(block)

    def test_stub_preserves_frontmatter_and_points_at_get_skill(self) -> None:
        with (
            tempfile_skill_layout() as skills_dir,
            patch.object(skill_resources, "SKILLS_DIR", skills_dir),
            tempfile.TemporaryDirectory() as out,
        ):
            zips = self.module.package_skills(Path(out))

            stub = (Path(out) / "demo-skill" / "SKILL.md").read_text(encoding="utf-8")
            fields = self._strict_yaml_frontmatter(stub)
            self.assertEqual(fields["name"], "demo-skill")
            self.assertEqual(fields["description"], "A demo skill.")
            self.assertEqual(fields["version"], "1.0.0")
            # The body is a pointer, not the methodology.
            self.assertIn('get_skill(skill="demo-skill")', stub)
            self.assertNotIn("Body.", stub)

            self.assertEqual([z.name for z in zips], ["demo-skill.zip"])
            with zipfile.ZipFile(zips[0]) as zf:
                self.assertEqual(zf.namelist(), ["demo-skill/SKILL.md"])
                self.assertEqual(zf.read("demo-skill/SKILL.md").decode("utf-8"), stub)

    def test_real_skills_package_as_valid_yaml_with_canonical_descriptions(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            self.module.package_skills(Path(out))
            for skill_dir in sorted(skill_resources.SKILLS_DIR.glob("*/")):
                if not (skill_dir / "SKILL.md").is_file():
                    continue
                canonical = skill_resources._parse_frontmatter(
                    (skill_dir / "SKILL.md").read_text(encoding="utf-8")
                )
                stub_path = Path(out) / canonical["name"] / "SKILL.md"
                self.assertTrue(stub_path.is_file(), stub_path)
                stub = self._strict_yaml_frontmatter(
                    stub_path.read_text(encoding="utf-8")
                )
                # The description is the trigger surface — it must survive
                # stub generation verbatim (through a STRICT parser: the real
                # descriptions embed ': ' and double quotes) or
                # auto-triggering degrades.
                self.assertEqual(stub["description"], canonical["description"])
                self.assertEqual(stub["version"], canonical["version"])


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
