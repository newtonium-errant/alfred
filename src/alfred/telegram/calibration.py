"""Calibration block I/O — read and write the user profile's ALFRED:CALIBRATION block.

Wk3 commits 2 + 7. Calibration is Alfred's running model of the primary
user, stored as a marker-wrapped body block on the user's `person` record:

    <!-- ALFRED:CALIBRATION -->
    ## Communication Style
    - bulleted attribution _source: session/X_
    ## Workflow Preferences
    - ...
    <!-- END ALFRED:CALIBRATION -->

The read side (``read_calibration``) is invoked at session open by the
bot; the write side (``propose_updates`` + ``apply_proposals``) runs at
session close. Keeping both ends in one module is deliberate — they
share the marker strings and block regex, and drift between them would
silently produce duplicate blocks or unreadable records.

The distiller strips this block before extracting learnings (wk3 commit 4
adds the pattern to :mod:`alfred.distiller.parser`). That's the whole
reason this lives inside fenced markers: the distiller must never
re-learn Alfred's own self-notes back into vault learnings, or the
extraction pipeline would become a feedback loop.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from .utils import get_logger

log = get_logger(__name__)


# Marker strings. Kept as module constants so commit 7's writer uses the
# exact same pair — a typo-level drift would silently produce a duplicate
# block on every session close.
CALIBRATION_MARKER_START: Final[str] = "<!-- ALFRED:CALIBRATION -->"
CALIBRATION_MARKER_END: Final[str] = "<!-- END ALFRED:CALIBRATION -->"


# DOTALL because the block routinely spans dozens of lines. Non-greedy so
# two adjacent blocks in the unlikely future don't merge into one match.
CALIBRATION_RE: Final[re.Pattern[str]] = re.compile(
    rf"{re.escape(CALIBRATION_MARKER_START)}(.*?){re.escape(CALIBRATION_MARKER_END)}",
    re.DOTALL,
)


def read_calibration(vault_path: Path, user_rel_path: str) -> str | None:
    """Read the calibration block from the user's person record.

    Args:
        vault_path: Vault root.
        user_rel_path: Vault-relative path to the user record (e.g.
            ``person/Andrew Newton``, with or without the ``.md`` suffix).

    Returns:
        The inner text of the calibration block (stripped), or ``None`` if:
            - the file doesn't exist,
            - the file has no calibration markers,
            - the block is present but empty.

    Never raises. Bot startup must not crash because a user's profile
    record is missing or malformed — the fallback is simply "no
    calibration context", which is what wk2 already shipped with.
    """
    if not user_rel_path:
        return None

    # Normalise: allow callers to pass either ``person/Andrew Newton`` or
    # ``person/Andrew Newton.md``. Keeps the call site simple regardless
    # of where the path came from (config stores stems, the router emits
    # wikilink-friendly paths).
    rel = user_rel_path.strip()
    if not rel.endswith(".md"):
        rel = f"{rel}.md"

    file_path = vault_path / rel
    if not file_path.exists():
        log.info(
            "talker.calibration.missing_user_record",
            user_rel_path=user_rel_path,
        )
        return None

    try:
        raw = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning(
            "talker.calibration.read_failed",
            user_rel_path=user_rel_path,
            error=str(exc),
        )
        return None

    match = CALIBRATION_RE.search(raw)
    if match is None:
        log.info(
            "talker.calibration.no_block",
            user_rel_path=user_rel_path,
        )
        return None

    inner = match.group(1).strip()
    if not inner:
        return None
    return inner


# --- Write path (wk3 commit 7) --------------------------------------------

# Confirmation-dial mapping (team-lead decision on open question #1 —
# default is 4 during the validation phase).
#   5: inline surface DURING the session (wk4+ — not implemented yet)
#   4: surface at /end in the close reply, require user confirmation
#   3: auto-append with "[needs confirmation]" marker
#   2: silent write with marker
#   1: silent write, no marker
#   0: never write
DEFAULT_CONFIRMATION_DIAL: Final[int] = 4


# Subsection headings recognised inside the calibration block. Used by
# the apply step to merge proposals into the right bucket. Unknown
# subsections are appended to a catch-all "Notes" heading rather than
# dropped, because silently losing a proposal is worse than adding a
# heading the human will probably relabel themselves.
KNOWN_SUBSECTIONS: Final[tuple[str, ...]] = (
    "Communication Style",
    "Workflow Preferences",
    "Current Priorities",
    "What Alfred Is Still Unsure About",
    "Model Preferences (learned)",
)


@dataclass(frozen=True)
class Proposal:
    """One proposed addition to the calibration block.

    Attributes:
        subsection: Which subsection heading this bullet belongs under.
            Should match one of :data:`KNOWN_SUBSECTIONS`; unknown values
            are honoured but appended under "Notes".
        bullet: The content. Rendered as a Markdown list item (``- …``).
            Should be a single sentence; multi-sentence is fine but
            unusual.
        confidence: Sonnet's own confidence 0-1. Values below 0.6 get the
            ``[needs confirmation]`` marker unless dial < 2.
        source_session_rel: Vault-relative path of the session record
            this proposal came from. Rendered as the italic ``_source:
            session/X_`` attribution at the end of the bullet.
    """

    subsection: str
    bullet: str
    confidence: float = 0.7
    source_session_rel: str = ""


_PROPOSE_PROMPT = """\
You are reading a closed voice-session transcript with a user named Andrew. \
You already have a calibration block that summarises what Alfred knows about \
Andrew. Your job is to propose additions or refinements to that block based \
on what happened in this session. Be conservative — only propose items \
that are genuinely new or materially refine existing text.

Current calibration block:
---
{current_calibration}
---

Session transcript (last {transcript_tail_turns} turns):
---
{transcript_excerpt}
---

Session type: {session_type}

Respond with ONLY a JSON array of proposals. No prose, no markdown fences. \
Each element is an object with keys:
  "subsection": one of {subsection_list} (pick the most appropriate).
  "bullet": a single-sentence addition (no leading dash).
  "confidence": 0.0–1.0 (your confidence that this belongs in the profile).

Return an empty array [] if nothing in this session merits a calibration update.

Example:
[
  {{"subsection": "Workflow Preferences",
    "bullet": "Prefers to batch morning planning into a single /end reply rather than mid-session interruptions.",
    "confidence": 0.75}}
]
"""


async def propose_updates(
    client: Any,
    transcript_text: str,
    current_calibration: str | None,
    session_type: str,
    source_session_rel: str,
    model: str = "claude-sonnet-4-6",
    transcript_tail_turns: int = 20,
) -> list[Proposal]:
    """Ask Sonnet for calibration updates; return a list of Proposals.

    Never raises. Network error / bad JSON / empty response → empty list.
    The caller applies the graceful-degradation contract: no proposals
    means the session closes exactly like wk2 (no calibration mutation).

    ``transcript_text`` should already be trimmed to the last few turns
    (20 is the wk3 default) so we don't pay Sonnet for the entire
    session on every close.

    ``source_session_rel`` is the vault-relative path (or name) of the
    session record being closed — stamped onto every produced Proposal
    so ``apply_proposals`` can render the italic attribution without a
    separate lookup.
    """
    prompt = _PROPOSE_PROMPT.format(
        current_calibration=current_calibration or "(empty)",
        transcript_tail_turns=transcript_tail_turns,
        transcript_excerpt=transcript_text or "(empty)",
        session_type=session_type,
        subsection_list=", ".join(f'"{s}"' for s in KNOWN_SUBSECTIONS),
    )

    try:
        response = await client.messages.create(
            model=model,
            max_tokens=1024,
            temperature=0.2,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("talker.calibration.propose_api_error", error=str(exc))
        return []

    raw = _extract_text(response)
    if not raw:
        return []

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        log.warning(
            "talker.calibration.propose_parse_failed",
            raw_head=raw[:200],
        )
        return []

    if not isinstance(parsed, list):
        log.warning(
            "talker.calibration.propose_bad_shape",
            shape=type(parsed).__name__,
        )
        return []

    proposals: list[Proposal] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        subsection = item.get("subsection") or ""
        bullet = item.get("bullet") or ""
        if not bullet.strip():
            continue
        confidence_raw = item.get("confidence", 0.7)
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError):
            confidence = 0.7
        proposals.append(Proposal(
            subsection=str(subsection),
            bullet=str(bullet).strip(),
            confidence=max(0.0, min(1.0, confidence)),
            source_session_rel=source_session_rel,
        ))
    return proposals


def apply_proposals(
    vault_path: Path,
    user_rel_path: str,
    proposals: list[Proposal],
    session_record_path: str,
    confirmation_dial: int = DEFAULT_CONFIRMATION_DIAL,
) -> dict[str, Any]:
    """Apply the proposals to the user's calibration block.

    Returns a dict summarising what happened:
        {"written": bool, "applied": list[Proposal], "skipped": list[Proposal],
         "reason": str}

    Dial behaviour:
        - 0: nothing is written (returned immediately).
        - 1: silent write, no confirmation marker.
        - 2: silent write with ``[needs confirmation]`` on any confidence < 0.6.
        - 3: same as 2 but applies the marker to EVERY proposal regardless of
          confidence — the "default-to-skeptical" dial.
        - 4: write everything with confidence >= 0.6 silently; items below
          that get the marker. The caller is expected to surface the
          proposals inline in the /end reply (bot layer handles that).
        - 5: reserved for "inline during session" in wk4; for now it behaves
          like 4 so the dial isn't broken if someone flips to it.

    Never raises. Filesystem / regex / vault_edit errors are logged and
    returned as ``written=False``.
    """
    summary: dict[str, Any] = {
        "written": False,
        "applied": [],
        "skipped": [],
        "reason": "",
    }

    if confirmation_dial <= 0:
        summary["reason"] = "dial_zero"
        return summary

    if not proposals:
        summary["reason"] = "no_proposals"
        return summary

    # Normalise user_rel_path to include ``.md`` — same contract as the
    # reader.
    rel = user_rel_path.strip()
    if not rel.endswith(".md"):
        rel = f"{rel}.md"

    # Render bullets with per-dial marker logic.
    rendered_by_subsection: dict[str, list[str]] = {}
    for proposal in proposals:
        needs_marker = _needs_marker(proposal, confirmation_dial)
        if needs_marker is None:
            summary["skipped"].append(proposal)
            continue
        bullet_text = _render_bullet(proposal, needs_marker, session_record_path)
        sub = proposal.subsection if proposal.subsection in KNOWN_SUBSECTIONS else "Notes"
        rendered_by_subsection.setdefault(sub, []).append(bullet_text)
        summary["applied"].append(proposal)

    if not rendered_by_subsection:
        summary["reason"] = "all_skipped"
        return summary

    # Build a body_rewriter closure and invoke vault_edit. Errors logged
    # and swallowed so close_session never fails on calibration mischief.
    from alfred.vault import ops  # local import to avoid cycle at module load

    # Calibration audit gap (c4): every bullet under
    # ``rendered_by_subsection`` is, by definition, agent-inferred prose
    # synthesised by Sonnet from the session transcript. Wrap each
    # subsection's bullets in a BEGIN_INFERRED/END_INFERRED marker pair
    # and append one ``attribution_audit`` entry per subsection to the
    # person record's frontmatter. The Daily Sync confirm/reject flow
    # then surfaces each subsection's bundle to Andrew.
    #
    # Per-bullet markers were considered and rejected: each subsection
    # already has a clear semantic boundary (## Heading), and one
    # confirm/reject decision per subsection matches Andrew's mental
    # model better than a per-bullet barrage.
    from alfred.vault import attribution

    audit_entries: list[attribution.AuditEntry] = []
    wrapped_by_subsection: dict[str, list[str]] = {}
    for sub, bullets in rendered_by_subsection.items():
        joined = "\n".join(bullets)
        # source_session_rel is per-proposal but they're all from the
        # same session for this apply call; use the first non-empty.
        sample_source = ""
        for prop in summary["applied"]:
            if prop.subsection == sub or (
                prop.subsection not in KNOWN_SUBSECTIONS and sub == "Notes"
            ):
                if prop.source_session_rel:
                    sample_source = prop.source_session_rel
                    break
        reason_src = sample_source or session_record_path or "(unknown session)"
        wrapped_block, entry = attribution.with_inferred_marker(
            joined,
            section_title=f"Calibration — {sub}",
            agent="salem",
            reason=f"calibration update (source={reason_src})",
        )
        wrapped_by_subsection[sub] = [wrapped_block]
        audit_entries.append(entry)

    # Read existing frontmatter so we can merge our new audit entries
    # into any list that already exists (prior calibration runs).
    existing_audit: list = []
    try:
        existing = ops.vault_read(vault_path, rel)
        existing_fm = existing.get("frontmatter") or {}
        if isinstance(existing_fm.get("attribution_audit"), list):
            existing_audit = list(existing_fm["attribution_audit"])
    except Exception as exc:  # noqa: BLE001 — read failure shouldn't block writes
        log.info(
            "talker.calibration.audit_read_failed",
            error=str(exc),
        )

    merged_fm: dict = {"attribution_audit": existing_audit}
    for entry in audit_entries:
        attribution.append_audit_entry(merged_fm, entry)

    def _rewriter(body: str) -> str:
        return _insert_into_block(body, wrapped_by_subsection)

    try:
        ops.vault_edit(
            vault_path,
            rel,
            set_fields={"attribution_audit": merged_fm["attribution_audit"]},
            body_rewriter=_rewriter,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.calibration.apply_failed",
            user_rel_path=user_rel_path,
            error=str(exc),
        )
        summary["reason"] = f"vault_edit_failed: {exc}"
        return summary

    summary["written"] = True
    summary["reason"] = "ok"
    log.info(
        "talker.calibration.applied",
        user_rel_path=user_rel_path,
        applied=len(summary["applied"]),
        skipped=len(summary["skipped"]),
        dial=confirmation_dial,
    )
    return summary


def _needs_marker(proposal: Proposal, dial: int) -> bool | None:
    """Return True/False/None where None = skip this proposal.

    Skip only when the proposal + dial combination dictates it
    (currently nothing skips; kept in the contract so a future dial can
    introduce confidence-gate skipping without refactoring the caller).
    """
    if dial <= 1:
        return False  # silent write, no marker
    if dial == 2:
        return proposal.confidence < 0.6
    if dial == 3:
        return True  # marker on every bullet
    # Dial 4 and 5: marker only on low-confidence items. Dial 5's "inline
    # during session" surface is a bot-layer thing; at write-time dial 5
    # behaves like dial 4.
    return proposal.confidence < 0.6


def _render_bullet(
    proposal: Proposal,
    needs_marker: bool,
    session_record_path: str,
) -> str:
    """Render one proposal as a Markdown list item."""
    parts = [f"- {proposal.bullet.rstrip()}"]
    if needs_marker:
        parts.append("[needs confirmation]")
    source_ref = proposal.source_session_rel or session_record_path
    if source_ref:
        # Italic attribution per team-lead decision on open question #2.
        # Strip ``.md`` for readability — the Obsidian link resolver
        # still targets the right file.
        source_display = source_ref
        if source_display.endswith(".md"):
            source_display = source_display[:-3]
        parts.append(f"_source: {source_display}_")
    return " ".join(parts)


_SUBSECTION_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _insert_into_block(
    body: str,
    rendered_by_subsection: dict[str, list[str]],
) -> str:
    """Insert new bullets into the calibration block's subsections.

    If the block is missing, returns the body unchanged (caller treats
    this as a no-op; the migration should have created the block).

    For each subsection:
        - If a heading exists → append bullets at the end of that
          subsection (before the next ``## `` or before the end marker).
        - If no heading exists → append a fresh ``## <sub>`` section at
          the end of the block with the bullets.
    """
    match = CALIBRATION_RE.search(body)
    if match is None:
        log.warning("talker.calibration.no_block_for_apply")
        return body

    inner = match.group(1)
    # Determine spans of each subsection so we can append cleanly.
    # Strategy: for each requested subsection, either find its heading
    # inside ``inner`` and insert new bullets just before the next
    # heading (or end of block), or append a fresh heading at the end.

    updated_inner = inner
    for sub, bullets in rendered_by_subsection.items():
        joined = "\n".join(bullets)
        updated_inner = _append_to_subsection(updated_inner, sub, joined)

    # Reassemble the body with the updated inner block.
    new_block = (
        CALIBRATION_MARKER_START
        + updated_inner
        + CALIBRATION_MARKER_END
    )
    return body[: match.start()] + new_block + body[match.end():]


def _append_to_subsection(inner: str, sub: str, bullets: str) -> str:
    """Append ``bullets`` to the given subsection within ``inner``."""
    heading_re = re.compile(rf"^##\s+{re.escape(sub)}\s*$", re.MULTILINE)
    heading_match = heading_re.search(inner)
    if heading_match is None:
        # Append a fresh heading at the end.
        trailer = inner.rstrip()
        addition = f"\n\n## {sub}\n\n{bullets}\n"
        return trailer + addition + "\n"

    # Find the next ``## `` heading AFTER this one (or end of string).
    next_heading = _SUBSECTION_HEADING_RE.search(inner, heading_match.end())
    end_of_section = next_heading.start() if next_heading else len(inner)

    section_body = inner[heading_match.end():end_of_section]
    # Strip trailing blank lines inside the section, re-add a single one,
    # then the new bullets, then the blank line that originally preceded
    # the next heading. Keeping the section body terminator as ``\n\n``
    # before the next heading matches the rest of the block's spacing.
    trimmed = section_body.rstrip() + "\n"
    updated_section = trimmed + bullets + "\n"
    if next_heading is not None:
        updated_section += "\n"
    return (
        inner[:heading_match.end()]
        + updated_section
        + inner[end_of_section:]
    )


def _extract_text(response: Any) -> str:
    """Pull concatenated text from an Anthropic response (shared with router.py)."""
    content = getattr(response, "content", None) or []
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", "")
            if text:
                parts.append(text)
    return "\n".join(parts).strip()
