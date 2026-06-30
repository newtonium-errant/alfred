"""Brief section — inbound inter-project messages (mirror peer_digests).

Pure render function for the operator's morning brief: counts unread per
registered project and lists subject lines, grouped by project. ILB: emits
"No inbound project messages." when every expected project is empty, so an
idle bus is distinguishable from a missing section. Counts via
:func:`inbox.count_unread` (the SAME helper the router ping uses, so pull
and push can't diverge).
"""

from __future__ import annotations

from .inbox import count_unread, list_inbox
from .registry import ProjectRegistry

_HEADER = "## Inbound Project Messages"
_EMPTY_LINE = "No inbound project messages."


def render_inbound_messages_section(
    registry: ProjectRegistry,
    *,
    expected_projects: list[str] | None = None,
) -> str:
    """Render the inbound-messages brief section body markdown.

    Args:
        registry: the project registry (resolves each name → inbox dir).
        expected_projects: projects to report on. When ``None``/empty, all
            registered projects are used. When the resulting set is empty
            (no projects at all) the section is omitted (returns "").

    Returns:
        Markdown. Empty string ONLY when there are no projects to report
        on at all (section disabled). Otherwise ALWAYS non-empty — the ILB
        "No inbound project messages." line when every inbox is empty.
    """
    expected = list(expected_projects or registry.names())
    if not expected:
        return ""

    per_project: list[tuple[str, int, list[str]]] = []
    total_unread = 0
    for name in expected:
        inbox = registry.inbox_for(name)
        if inbox is None:
            continue
        n = count_unread(inbox)
        total_unread += n
        subjects: list[str] = []
        if n:
            subjects = [
                (rec.subject or "(no subject)") for rec in list_inbox(inbox)
            ]
        per_project.append((name, n, subjects))

    parts: list[str] = [_HEADER]
    if total_unread == 0:
        # Intentionally-left-blank — operator sees the section ran.
        parts.append(_EMPTY_LINE)
        return "\n".join(parts)

    for name, n, subjects in per_project:
        if not n:
            continue
        parts.append(f"### {name} ({n})")
        for subject in subjects:
            parts.append(f"- {subject}")
    return "\n".join(parts)


__all__ = ["render_inbound_messages_section"]
