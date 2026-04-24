#!/usr/bin/env python3
"""Static contract check: curator SKILL.md <-> curator scope alignment.

Parses every ``alfred vault <sub>`` invocation the curator SKILL shows
the agent and asserts each would pass ``check_scope("curator", ...)``.
Exits non-zero on mismatch with a "SKILL instructs X but curator scope
forbids it" message.

Why: curator is the only fully-agentic tool left (distiller/janitor have
migrated most issue codes to deterministic Python). When scope.py is
tightened, the SKILL may still reference a now-denied path. Root
CLAUDE.md documents the Q3 2026-04-19 precedent (dead STUB001 step
alive 24h after a janitor body-write denial). This script closes that
gap for curator.

No pytest — see feedback_pytest_wsl_hang.md. Run:
    python scripts/smoke_curator_scope.py
"""
from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_PATH = (
    REPO_ROOT / "src" / "alfred" / "_bundled"
    / "skills" / "vault-curator" / "SKILL.md"
)
sys.path.insert(0, str(REPO_ROOT / "src"))

from alfred.vault.scope import ScopeError, check_scope  # noqa: E402

OPERATION_BY_SUBCOMMAND = {
    "read": "read", "search": "search", "list": "list", "context": "context",
    "create": "create", "edit": "edit", "move": "move", "delete": "delete",
}

# Matches a bare `alfred vault <sub> ...` line, possibly after `$ ` or
# `cat ... |`. Rejects prose mentions inside backticks by anchoring to
# the start of a line (after optional shell-noise).
COMMAND_START = re.compile(
    r"^(?:\s*|\s*\$\s+|.*\|\s+)alfred\s+vault\s+(?P<sub>[a-z]+)(?P<rest>.*)$"
)


def extract_commands(skill_text: str) -> list[tuple[int, str, str]]:
    """Return [(line_number, subcommand, rest), ...].

    Joins trailing-backslash line continuations so multi-line shell
    examples parse as one invocation.
    """
    hits: list[tuple[int, str, str]] = []
    raw = skill_text.splitlines()
    i = 0
    while i < len(raw):
        line = raw[i]
        while line.rstrip().endswith("\\") and i + 1 < len(raw):
            line = line.rstrip()[:-1] + " " + raw[i + 1]
            i += 1
        m = COMMAND_START.match(line)
        if m:
            hits.append((i + 1, m.group("sub"), m.group("rest").strip()))
        i += 1
    return hits


def parse_flags(rest: str) -> dict:
    """Extract scope-relevant signals from a command tail.

    Returns positional args, --set/--append field names (for the
    field_allowlist / edit check), and whether a body write flag is
    present (for the body_write gate on create/edit).
    """
    try:
        tokens = shlex.split(rest, posix=True)
    except ValueError:
        tokens = rest.split()

    positional: list[str] = []
    set_fields: list[str] = []
    append_fields: list[str] = []
    body_write = False
    idx = 0
    while idx < len(tokens):
        t = tokens[idx]
        if t == "--set" and idx + 1 < len(tokens):
            set_fields.append(tokens[idx + 1].split("=", 1)[0])
            idx += 2
            continue
        if t.startswith("--set="):
            set_fields.append(t[len("--set="):].split("=", 1)[0])
        elif t == "--append" and idx + 1 < len(tokens):
            append_fields.append(tokens[idx + 1].split("=", 1)[0])
            idx += 2
            continue
        elif t.startswith("--append="):
            append_fields.append(t[len("--append="):].split("=", 1)[0])
        elif t == "--body-stdin" or t.startswith("--body-append"):
            body_write = True
            if t == "--body-append" and idx + 1 < len(tokens):
                idx += 2
                continue
        elif not t.startswith("--"):
            positional.append(t)
        idx += 1
    return {
        "positional": positional,
        "set_fields": set_fields,
        "append_fields": append_fields,
        "body_write": body_write,
    }


def check_one(sub: str, parsed: dict) -> tuple[bool, str]:
    operation = OPERATION_BY_SUBCOMMAND.get(sub)
    if operation is None:
        return False, f"unknown subcommand '{sub}' — not in vault CLI"

    kwargs: dict = {}
    if operation == "create":
        # Pass record_type + body_write so future narrowing (e.g. a
        # curator_types_only rule) is exercised by this smoke.
        if parsed["positional"]:
            kwargs["record_type"] = parsed["positional"][0]
        kwargs["body_write"] = parsed["body_write"]
    elif operation == "edit":
        kwargs["fields"] = parsed["set_fields"] + parsed["append_fields"]
        kwargs["body_write"] = parsed["body_write"]
    elif operation in ("move", "delete"):
        if parsed["positional"]:
            kwargs["rel_path"] = parsed["positional"][0]

    try:
        check_scope("curator", operation, **kwargs)
    except ScopeError as e:
        return False, str(e)
    return True, ""


def main() -> int:
    if not SKILL_PATH.exists():
        print(f"FAIL: curator SKILL not found at {SKILL_PATH}")
        return 1

    commands = extract_commands(SKILL_PATH.read_text(encoding="utf-8"))
    if not commands:
        print(f"FAIL: no 'alfred vault' invocations found in {SKILL_PATH}")
        return 1

    pass_count = 0
    violations: list[tuple[int, str, str, str]] = []
    for line_no, sub, rest in commands:
        ok, msg = check_one(sub, parse_flags(rest))
        if ok:
            pass_count += 1
        else:
            violations.append((line_no, sub, rest, msg))

    print(f"parsed {len(commands)} 'alfred vault' invocations from {SKILL_PATH.name}")
    print(f"curator scope accepts: {pass_count}")
    print(f"curator scope rejects: {len(violations)}")

    if violations:
        print()
        print("--- violations ---")
        for line_no, sub, rest, msg in violations:
            print(
                f"  L{line_no}: SKILL instructs 'alfred vault {sub} "
                f"{rest[:80]}' but curator scope forbids it: {msg}"
            )
        print()
        print(
            "Action: either the SKILL drifted past a scope narrowing "
            "(remove the now-forbidden step) or the scope tightened "
            "too far (relax SCOPE_RULES['curator']). See root CLAUDE.md "
            "'Scope/schema-narrowing commits trigger a SKILL audit'."
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
