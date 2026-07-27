"""MCP resources serving the canonical text of the client-side gaming skills.

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
a server restart.

Fails soft: no ``skills/`` directory, or a directory with no valid skill
subfolders, logs a warning and registers nothing (including no
``skill://index.json``) rather than crashing startup.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Any

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


def _register_index_resource(mcp: Any, index: list[dict[str, Any]]) -> None:
    def _skill_index() -> list[dict[str, Any]]:
        return index

    mcp.resource(
        "skill://index.json",
        name="skill_index",
        description=(
            "Discovery index of the client-side gaming skills' canonical text "
            "(name, description, version, file URIs per skill)."
        ),
        mime_type="application/json",
    )(_skill_index)


def register_skill_resources(mcp: Any) -> None:
    """Register one ``skill://`` resource per skill file, plus ``skill://index.json``."""
    if not SKILLS_DIR.is_dir():
        logger.warning(
            "skills/ directory not found at %s; no skill:// resources registered", SKILLS_DIR
        )
        return

    index: list[dict[str, Any]] = []

    for skill_dir in sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue

        try:
            frontmatter = _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        except OSError as exc:
            logger.warning("Failed to read %s: %s", skill_md, exc)
            continue

        skill_name = frontmatter.get("name") or skill_dir.name

        try:
            files = sorted(p for p in skill_dir.rglob("*") if p.is_file())
        except OSError as exc:
            logger.warning("Failed to list files under %s: %s", skill_dir, exc)
            continue

        file_uris: list[str] = []
        for file_path in files:
            rel_path = file_path.relative_to(skill_dir).as_posix()
            uri = f"skill://{skill_name}/{rel_path}"
            _register_skill_file_resource(mcp, uri, skill_name, rel_path, file_path)
            file_uris.append(uri)

        if not file_uris:
            continue

        index.append(
            {
                "name": skill_name,
                "description": frontmatter.get("description", ""),
                "version": frontmatter.get("version", ""),
                "files": file_uris,
            }
        )

    if not index:
        logger.warning("No skill files found under %s; no skill:// resources registered", SKILLS_DIR)
        return

    _register_index_resource(mcp, index)
