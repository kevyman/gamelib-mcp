#!/usr/bin/env python3
"""Inject the live model's posture into every prompt.

Registered in .claude/settings.json for two events:

- SessionStart: the payload here is the only place Claude Code (sometimes)
  reports the session's model, so cache it per-session for later prompts.
- UserPromptSubmit: resolve the live model and return the matching
  ``## <key>`` section of rules/model-postures.md as additionalContext.

UserPromptSubmit payloads carry no model field, so resolution is layered:
last assistant message in the transcript (freshest once the session has
turns — it tracks a mid-session /model switch, which the SessionStart cache
never would), then the SessionStart cache (covers the first prompts of a
session), then $ANTHROPIC_MODEL, then the orchestrator default.

Subagents never see UserPromptSubmit; their postures are baked into
.claude/agents/*.md instead. Stdlib only — runs under any python3, no venv.

Fail-open by design: any error exits 0 with no output. A broken hook must
never block or slow the prompt.
"""

import json
import os
import re
import sys
import tempfile
from pathlib import Path

# Ordered: first substring match on the model ID wins. "mythos" shares the
# fable posture (same underlying model).
POSTURE_KEYS = [
    ("fable", "fable"),
    ("mythos", "fable"),
    ("opus", "opus"),
    ("sonnet", "sonnet"),
    ("haiku", "haiku"),
]

# The main session is the orchestrator, so an undetectable model gets the
# orchestrator posture rather than nothing.
DEFAULT_KEY = "fable"


def project_dir() -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env)
    # .claude/hooks/inject_model_posture.py -> repo root
    return Path(__file__).resolve().parent.parent.parent


def cache_file(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)
    return Path(tempfile.gettempdir()) / "claude-model-posture" / f"{safe}.txt"


def payload_model(payload: dict) -> str:
    for field in ("model", "model_id", "modelId"):
        value = payload.get(field)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):  # e.g. {"id": ..., "display_name": ...}
            inner = value.get("id") or value.get("display_name")
            if isinstance(inner, str) and inner:
                return inner
    return ""


def model_from_transcript(transcript_path: str) -> str:
    """Model of the last assistant message in the session transcript (JSONL)."""
    try:
        lines = Path(transcript_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        if '"model"' not in line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = entry.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            model = message.get("model")
            if isinstance(model, str):
                return model
    return ""


def model_from_cache(session_id: str) -> str:
    try:
        return cache_file(session_id).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def detect_model(payload: dict) -> str:
    model = payload_model(payload)
    if model:
        return model
    transcript = payload.get("transcript_path")
    if isinstance(transcript, str) and transcript:
        model = model_from_transcript(transcript)
        if model:
            return model
    session_id = payload.get("session_id")
    if isinstance(session_id, str) and session_id:
        model = model_from_cache(session_id)
        if model:
            return model
    return os.environ.get("ANTHROPIC_MODEL", "")


def posture_key(model: str) -> str:
    lowered = model.lower()
    for needle, key in POSTURE_KEYS:
        if needle in lowered:
            return key
    return DEFAULT_KEY


def posture_section(key: str) -> str:
    path = project_dir() / "rules" / "model-postures.md"
    text = path.read_text(encoding="utf-8")
    match = re.search(
        rf"^## {re.escape(key)}\n(.*?)(?=^## |\Z)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def handle_session_start(payload: dict) -> None:
    model = payload_model(payload)
    session_id = payload.get("session_id")
    if not model or not isinstance(session_id, str) or not session_id:
        return
    target = cache_file(session_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(model, encoding="utf-8")


def handle_prompt_submit(payload: dict) -> None:
    section = posture_section(posture_key(detect_model(payload)))
    if not section:
        return
    context = (
        '<model-posture source="rules/model-postures.md">\n'
        f"{section}\n"
        "</model-posture>"
    )
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        },
        sys.stdout,
    )


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    if payload.get("hook_event_name") == "SessionStart":
        handle_session_start(payload)
    else:
        handle_prompt_submit(payload)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - fail open, never block the prompt
        pass
    sys.exit(0)
