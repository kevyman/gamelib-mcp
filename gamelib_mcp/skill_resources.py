"""MCP resources + tool backend serving the canonical gaming-skill text.

ADR 0006 (docs/adr/0006-skills-stay-client-side-server-absorbs-mechanics.md)
keeps the gaming skills' triggering and judgment layer client-side — no host
auto-triggers a server-distributed skill today — but makes this repo their
canonical home and, per its decision 4, serves that text as plain MCP
resources in the SEP-2640 ("Skills Extension", draft) URI shape:
``skill://<skill-name>/<relative-path>`` per file, plus a discovery index at
``skill://index.json``. These are ordinary resources on the stable
2025-11-25 protocol revision — no new methods, no ADR 0005 conflict — so any
connected client can read/@-mention them on demand even without the skill
installed locally.

Decision 4b (2026-08-07 addendum): claude.ai's connector surface never hands
the model ``resources/read`` — resources are user-attachable only — so the
same bytes are also reachable through the ``get_skill`` tool (registered in
``main.py``, backed by :func:`skill_index_payload` / :func:`read_skill_file`
here). Both surfaces share :func:`_scan_skills`, so they cannot drift. The
tool is the bridge for tools-only hosts, in the shape the Skills Over MCP
working group records for exactly this gap; it retires when a registered
client speaks the ratified extension.

Skills live under ``skills/<skill-name>/`` at the repo root (a sibling of
this package; wheel builds force-include a copy at ``gamelib_mcp/skills``
and the Docker image COPYs the directory, so deployed artifacts serve the
same resources — see ``_resolve_skills_dir``), each with a ``SKILL.md``
carrying YAML frontmatter (``name``,
``description``, ``version``) plus any supporting files a skill chooses to
ship. Every file under a skill directory that has a ``SKILL.md`` gets its own
resource; ``skills/README.md`` itself (install instructions, not a skill) is
not a skill directory and is skipped.

Content is read from disk lazily, at request time, not captured into a
closure at registration time — an edited skill file is served fresh without
a server restart. Enumeration is fresh too, on both surfaces: the tool and
``skill://index.json`` rebuild from a disk scan per request, and a wildcard
resource template (``skill://{skill}/{path*}``) serves any currently scanned
file, so a skill added after startup is readable everywhere even though its
concrete per-file resource URIs (the SEP-2640 enumerable shape) are only
registered at startup.

Fails soft: no ``skills/`` directory, or a directory with no valid skill
subfolders, logs a warning and registers nothing (including no
``skill://index.json``) rather than crashing startup.
"""

from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastmcp.exceptions import ToolError

logger = logging.getLogger(__name__)

def _resolve_skills_dir(package_dir: Path | None = None) -> Path:
    """Find the skills/ directory across the deployment shapes.

    A wheel install carries skills/ INSIDE the package (the pyproject
    force-include maps it to gamelib_mcp/skills), so that copy wins when
    present; source checkouts, editable installs, and the Docker image keep
    it at the repo root as a sibling of the package. Returns the sibling
    path even when neither exists so the fail-soft warning names a real
    location.
    """
    if package_dir is None:
        package_dir = Path(__file__).resolve().parent
    packaged = package_dir / "skills"
    return packaged if packaged.is_dir() else package_dir.parent / "skills"


SKILLS_DIR = _resolve_skills_dir()


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Parse the flat ``key: value`` YAML frontmatter block SKILL.md files use.

    Deliberately minimal (no PyYAML dependency for three scalar fields):
    handles a leading ``---`` delimiter, single-line ``key: value`` pairs up
    to the closing ``---``, and strips a matching pair of surrounding quotes
    from the value (e.g. ``version: "2.0.0"``). Malformed or missing
    frontmatter yields an empty dict rather than raising.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}

    fields: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            fields[key] = value
    return fields


def _mime_type_for(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "text/plain"


@dataclass(frozen=True)
class _SkillScan:
    """One skill directory as found on disk right now."""

    name: str
    description: str
    version: str
    directory: Path
    files: tuple[str, ...]  # relative POSIX paths, sorted


def _scan_skills() -> list[_SkillScan]:
    """Enumerate valid skill directories (has SKILL.md) under SKILLS_DIR.

    The single source of truth for both the ``skill://`` resources (scanned
    once at registration) and the ``get_skill`` tool (scanned fresh per call,
    so a skill added after startup is discoverable without a restart).
    Unreadable directories are skipped with a warning, mirroring the
    fail-soft registration behavior.
    """
    if not SKILLS_DIR.is_dir():
        return []

    scans: list[_SkillScan] = []
    for skill_dir in sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue

        try:
            frontmatter = _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        except OSError as exc:
            logger.warning("Failed to read %s: %s", skill_md, exc)
            continue

        try:
            files = sorted(p for p in skill_dir.rglob("*") if p.is_file())
        except OSError as exc:
            logger.warning("Failed to list files under %s: %s", skill_dir, exc)
            continue
        if not files:
            continue

        scans.append(
            _SkillScan(
                name=frontmatter.get("name") or skill_dir.name,
                description=frontmatter.get("description", ""),
                version=frontmatter.get("version", ""),
                directory=skill_dir,
                files=tuple(p.relative_to(skill_dir).as_posix() for p in files),
            )
        )
    return scans


def _register_skill_file_resource(
    mcp: Any, uri: str, skill_name: str, rel_path: str, file_path: Path
) -> None:
    """Register one static (parameterless) resource that reads ``file_path`` on demand."""

    def _read_skill_file() -> str:
        return file_path.read_text(encoding="utf-8")

    mcp.resource(
        uri,
        name=f"{skill_name}/{rel_path}",
        description=f"Skill file for '{skill_name}': {rel_path}",
        mime_type=_mime_type_for(file_path),
    )(_read_skill_file)


def _register_index_resource(mcp: Any) -> None:
    def _skill_index() -> list[dict[str, Any]]:
        # Rebuilt from disk per read, mirroring skill_index_payload(), so the
        # index and the get_skill tool always agree on what exists.
        return [
            {
                "name": scan.name,
                "description": scan.description,
                "version": scan.version,
                "files": [f"skill://{scan.name}/{rel}" for rel in scan.files],
            }
            for scan in _scan_skills()
        ]

    mcp.resource(
        "skill://index.json",
        name="skill_index",
        description=(
            "Discovery index of the client-side gaming skills' canonical text "
            "(name, description, version, file URIs per skill)."
        ),
        mime_type="application/json",
    )(_skill_index)


def _register_wildcard_resource(mcp: Any) -> None:
    """Catch-all template so files added after startup are still readable.

    Concrete per-file resources are registered from the startup scan (the
    SEP-2640 enumerable shape) and take precedence on exact URI match; this
    template answers for anything the per-request scan knows that startup
    didn't. Lookup is whitelisted against the scanned file list, same as
    read_skill_file.
    """

    def _read_any_skill_file(skill: str, path: str) -> str:
        scan = next((s for s in _scan_skills() if s.name == skill), None)
        if scan is None or path not in scan.files:
            raise ValueError(f"no such skill file: skill://{skill}/{path}")
        return (scan.directory / path).read_text(encoding="utf-8")

    mcp.resource(
        "skill://{skill}/{path*}",
        name="skill_file",
        description="Any current file of a served gaming skill, looked up on demand.",
        mime_type="text/plain",
    )(_read_any_skill_file)


def register_skill_resources(mcp: Any) -> None:
    """Register one ``skill://`` resource per skill file, plus ``skill://index.json``."""
    if not SKILLS_DIR.is_dir():
        logger.warning(
            "skills/ directory not found at %s; no skill:// resources registered", SKILLS_DIR
        )
        return

    scans = _scan_skills()
    for scan in scans:
        for rel_path in scan.files:
            uri = f"skill://{scan.name}/{rel_path}"
            _register_skill_file_resource(mcp, uri, scan.name, rel_path, scan.directory / rel_path)

    if not scans:
        logger.warning("No skill files found under %s; no skill:// resources registered", SKILLS_DIR)
        return

    _register_index_resource(mcp)
    _register_wildcard_resource(mcp)


# ── get_skill tool backends (decision 4b) ─────────────────────────────────────


def skill_index_payload() -> list[dict[str, Any]]:
    """The ``get_skill()`` index: name, description, version, files per skill.

    ``files`` are the relative paths ``get_skill(skill=..., path=...)``
    accepts — the same files the resources serve as
    ``skill://<name>/<path>``.
    """
    return [
        {
            "name": scan.name,
            "description": scan.description,
            "version": scan.version,
            "files": list(scan.files),
        }
        for scan in _scan_skills()
    ]


def read_skill_file(skill: str, path: str = "SKILL.md") -> dict[str, Any]:
    """Read one skill file for ``get_skill(skill=..., path=...)``.

    Lookup is by the skill's frontmatter name (falling back to its directory
    name), matching how the resources are keyed. ``path`` must be one of the
    scanned relative paths — membership in that list is the whole
    traversal guard, since the scan only ever yields paths inside the skill
    directory (the resources never needed one because their URIs are
    pre-enumerated, but a tool takes free-form input).
    """
    scans = _scan_skills()
    scan = next((s for s in scans if s.name == skill), None)
    if scan is None:
        available = ", ".join(s.name for s in scans) or "none (no skills directory on server)"
        raise ToolError(f"Unknown skill {skill!r}. Available skills: {available}")

    if path not in scan.files:
        raise ToolError(
            f"Skill {skill!r} has no file {path!r}. Available files: {', '.join(scan.files)}"
        )

    return {
        "skill": scan.name,
        "path": path,
        "version": scan.version,
        "content": (scan.directory / path).read_text(encoding="utf-8"),
    }
