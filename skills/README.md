# Gaming skills

This directory is the **canonical home** of gamelib-mcp's client-side gaming
skills (`game-quality`, `backlog-triage`, and any future ones such as the
planned bundle-evaluation skill), per
[ADR 0006](../docs/adr/0006-skills-stay-client-side-server-absorbs-mechanics.md).

Skills stay client-side — MCP has no mechanism today that both auto-triggers
on conversation content *and* ships bundled scripts — but they version with
this repo so their tool references never drift from the actual wire surface
(`main.py`'s `@mcp.tool` registrations). Any installed copy, including
`~/.claude/skills` and claude.ai Skills uploads, is a **downstream copy** of
what's here, not the source of truth.

## Installing a skill

Copy or symlink the skill folder into your client's skills directory, e.g.:

```bash
cp -r skills/game-quality ~/.claude/skills/
cp -r skills/backlog-triage ~/.claude/skills/
# or, to track repo updates automatically:
ln -s "$(pwd)/skills/game-quality" ~/.claude/skills/game-quality
ln -s "$(pwd)/skills/backlog-triage" ~/.claude/skills/backlog-triage
```

Each `SKILL.md` carries a `version` in its frontmatter; bump it when the
methodology or tool references change.
