# Gaming skills

This directory is the **canonical home** of gamelib-mcp's client-side gaming
skills (`game-quality`, `backlog-triage`, `bundle-evaluation`, and any future
ones), per
[ADR 0006](../docs/adr/0006-skills-stay-client-side-server-absorbs-mechanics.md).

Skills stay client-side — MCP has no mechanism today that both auto-triggers
on conversation content *and* ships bundled scripts — but they version with
this repo so their tool references never drift from the actual wire surface
(`main.py`'s `@mcp.tool` registrations). Any installed copy, including
`~/.claude/skills` and claude.ai Skills uploads, is a **downstream copy** of
what's here, not the source of truth.

The server itself serves this text two ways (identical bytes, both read from
disk per request):

- **MCP resources** — `skill://<name>/<path>` plus a `skill://index.json`
  discovery index (SEP-2640 shape), for clients whose model can read
  resources (Claude Code).
- **The `get_skill` tool** (ADR 0006 decision 4b) — no arguments lists the
  skills; `get_skill(skill="game-quality")` returns the SKILL.md text. This
  is the path for hosts that only surface tools to the model (claude.ai
  custom connectors, chatgpt.com).

## Installing a skill

Prefer installing a **trigger stub** over copying the full text. A stub
carries the canonical frontmatter (so auto-triggering by description still
works) but its body just tells the model to call `get_skill` and follow the
returned methodology — meaning edits under `skills/` are live in the next
conversation with no reinstall, and installed copies can no longer drift
from the repo. Build the stubs:

```bash
python scripts/package_skills.py            # writes dist/skill-stubs/
```

- **claude.ai**: upload `dist/skill-stubs/<name>.zip` under Settings →
  Capabilities (re-upload only when a stub's frontmatter changes).
- **Claude Code**: `cp -r dist/skill-stubs/game-quality ~/.claude/skills/`

Full-text installs still work when a client should keep functioning with the
server unreachable — copy or symlink the canonical folder instead:

```bash
cp -r skills/game-quality ~/.claude/skills/
# or, to track repo updates automatically:
ln -s "$(pwd)/skills/game-quality" ~/.claude/skills/game-quality
```

Each `SKILL.md` carries a `version` in its frontmatter; bump it when the
methodology, triggers, or tool references change. `get_skill`'s index (and
`skill://index.json`) reports that version, so a session holding a stale
installed copy can see the mismatch and prefer the fetched text.
