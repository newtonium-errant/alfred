#!/usr/bin/env python3
"""PreToolUse hook: block direct edits to src/alfred/ from the main conversation.

Per CLAUDE.md and ``feedback_team_lead_direct_commits.md``:
- Code changes under ``src/alfred/`` MUST go through a builder agent in a
  worktree, then through code-reviewer pass and fast-forward.
- Documentation, configs, and process files are exceptions — team lead
  edits them directly.

Detection mechanism: builders operate in worktrees at
``.claude/worktrees/agent-*/`` so their file_path arguments are prefixed
with the worktree path. The team lead operates on the main repo at
``/home/andrew/alfred/src/alfred/``. Path prefix distinguishes which.

Allowed paths under src/alfred/:
- ``.claude/worktrees/<anything>/src/alfred/...`` — builder agent in worktree

Blocked paths under src/alfred/:
- ``/home/andrew/alfred/src/alfred/...`` (or relative ``src/alfred/...``)
  when not in a worktree — team lead direct edit

Triggers on Edit, Write, MultiEdit, NotebookEdit. Other tools pass through.
"""
import json
import sys

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# The main repo path we're protecting. Edits to this prefix that aren't
# inside a worktree subtree are team-lead-direct violations.
MAIN_SRC_PREFIX = "/home/andrew/alfred/src/alfred/"

# Worktree subtree marker. Any path containing this is a builder edit
# (builders work in worktrees at .claude/worktrees/agent-<id>/).
WORKTREE_MARKER = "/.claude/worktrees/"


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return  # malformed input → fail open

    if payload.get("tool_name") not in EDIT_TOOLS:
        return

    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not file_path:
        return

    # Builders edit through worktrees. If the path is inside a worktree,
    # allow regardless of where in the tree.
    if WORKTREE_MARKER in file_path:
        return

    # Team-lead edits to src/alfred/ on the main repo are blocked.
    if not file_path.startswith(MAIN_SRC_PREFIX) and not file_path.startswith(
        "src/alfred/"
    ):
        return

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "Team lead must not edit src/alfred/ directly. Code "
                    "changes route through a builder agent in a worktree, "
                    "then through code-reviewer pass and fast-forward to "
                    "master. Per CLAUDE.md and "
                    "feedback_team_lead_direct_commits.md.\n\n"
                    f"Blocked path: {file_path}\n\n"
                    "To proceed: spawn a builder via the Agent tool with "
                    'isolation: "worktree" and route the change through it.\n\n'
                    "Soft exceptions (allowed direct): CLAUDE.md, "
                    ".claude/agents/*.md, memory/*.md, .gitignore, "
                    "config.yaml/config.<instance>.yaml, vault/* session "
                    "notes, .claude/hooks/*.py (this hook is one)."
                ),
            }
        },
        sys.stdout,
    )


if __name__ == "__main__":
    main()
