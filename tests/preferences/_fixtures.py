"""Shared test fixtures for operator-preference V1 tests."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent


def write_preference(
    vault: Path,
    slug: str,
    *,
    name: str,
    shape: str,
    scope: str,
    status: str = "active",
    applies_to_instance: str | None = None,
    cites_canonical: str | None = None,
    matcher: dict | None = None,
    source_quote: str = "test source quote",
    source_session: str = "[[session/conversation-test]]",
    policy_body: str = "Test policy body.",
) -> Path:
    """Write a preference record to ``vault/preference/<slug>.md``.

    Handles all V1 fields including the optional ``cites_canonical``
    + matcher. Returns the path the record was written to.
    """
    pref_dir = vault / "preference"
    pref_dir.mkdir(parents=True, exist_ok=True)

    lines = [
        "---",
        "type: preference",
        f"status: {status}",
        f"name: \"{name}\"",
        f"shape: {shape}",
        f"scope: {scope}",
    ]
    lines.append(
        f"applies_to_instance: {applies_to_instance}"
        if applies_to_instance
        else "applies_to_instance: null"
    )
    lines.append("applies_to_user: null")
    lines.append(
        f"cites_canonical: \"{cites_canonical}\""
        if cites_canonical
        else "cites_canonical: null"
    )
    lines.append(f"source_quote: \"{source_quote}\"")
    lines.append(f"source_session: \"{source_session}\"")
    if matcher is not None:
        lines.append("matcher:")
        for k, v in matcher.items():
            if isinstance(v, dict):
                lines.append(f"  {k}:")
                for kk, vv in v.items():
                    # Single-quote string args to preserve regex escape
                    # sequences like ``\b``. Double-quoted YAML
                    # interprets backslash escapes (e.g. ``\b`` →
                    # backspace char), corrupting the regex on parse.
                    # Single-quotes preserve the literal string; the
                    # only YAML escape inside single-quotes is `''` for
                    # a literal apostrophe — we escape defensively.
                    quoted = str(vv).replace("'", "''")
                    lines.append(f"    {kk}: '{quoted}'")
            else:
                lines.append(f"  {k}: {v}")
    lines.append("created: '2026-05-24'")
    lines.append("tags: []")
    lines.append("---")
    lines.append("")
    lines.append(f"# {name}")
    lines.append("")
    lines.append("## Policy")
    lines.append("")
    lines.append(policy_body)
    lines.append("")

    path = pref_dir / f"{slug}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
