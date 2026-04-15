#!/usr/bin/env python3
"""PreToolUse hook: block background Agent spawns that look like editing tasks.

Editing agents need foreground so that permission prompts for Edit/Write can
resolve. A background edit agent silently gets denied. This hook enforces the
rule at the harness level.
"""
import json
import re
import sys

EDIT_VERBS = re.compile(
    r"\b(apply|edit|implement|add|change|modify|write|remove|delete|refactor|fix|rename|create)\b",
    re.IGNORECASE,
)

def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return  # malformed input → allow (fail open)

    if payload.get("tool_name") != "Agent":
        return
    tool_input = payload.get("tool_input") or {}
    if not tool_input.get("run_in_background"):
        return
    prompt = tool_input.get("prompt") or ""
    if not EDIT_VERBS.search(prompt):
        return

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "Editing agents must run in foreground. This Agent spawn has "
                    "run_in_background=true and the prompt contains edit-implying "
                    "verbs (apply/edit/implement/add/change/modify/write/remove/"
                    "delete/refactor/fix/rename/create). Background agents cannot "
                    "resolve permission prompts, so Edit/Write will be denied "
                    "silently. Re-spawn without run_in_background."
                ),
            }
        },
        sys.stdout,
    )

if __name__ == "__main__":
    main()
