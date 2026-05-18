"""Async batch structuring pass for wk2b capture sessions.

When a capture session ends (`/end`), the bot layer kicks off
:func:`process_capture_session` as a detached task. The coroutine:

    1. Calls Sonnet with a tool_use-enforced schema producing a
       :class:`StructuredSummary` over the raw transcript.
    2. Renders the summary as a ``## Structured Summary`` markdown
       block wrapped in ``<!-- ALFRED:DYNAMIC -->`` markers.
    3. Reads the session record, injects the summary ABOVE the raw
       ``# Transcript`` body, and sets ``capture_structured: true``
       in frontmatter.
    4. Sends a Telegram follow-up message with the short-id + /extract
       and /brief hints.

On Sonnet failure the session record still gets updated: the markdown
block contains a human-readable error, frontmatter flips to
``capture_structured: failed``, and the follow-up Telegram message
surfaces the failure.

The module is deliberately **bot-agnostic for the rendering + write
path** (pure functions) and carries a single ``process_capture_session``
orchestrator that owns the Telegram side-effects. Tests mock the SDK at
the ``client.messages.create`` layer (matching the talker's existing
fake pattern in ``tests/telegram/conftest.py``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from alfred.vault import ops

from ._anthropic_compat import messages_create_kwargs
from .utils import get_logger

log = get_logger(__name__)


# --- Data types -----------------------------------------------------------


@dataclass(frozen=True)
class StructuredSummary:
    """The structured output of the capture batch pass.

    Fields match the tool_use schema enforced on Sonnet. All are lists
    of strings — empty lists are legal (a capture with no explicit
    decisions, for example).
    """

    topics: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    action_items: list[str] = field(default_factory=list)
    key_insights: list[str] = field(default_factory=list)
    raw_contradictions: list[str] = field(default_factory=list)


# --- Dynamic-block markers -----------------------------------------------

# Shared with any future dynamic-block consumer (distiller strip,
# vault-reviewer read). Keep them as a pair constant so the writer and
# any downstream reader match byte-for-byte — marker drift would silently
# produce duplicate blocks or unreadable records.
SUMMARY_MARKER_START: Final[str] = "<!-- ALFRED:DYNAMIC -->"
SUMMARY_MARKER_END: Final[str] = "<!-- END ALFRED:DYNAMIC -->"


# --- Prompt + schema -----------------------------------------------------

_BATCH_SYSTEM_PROMPT = """\
You process closed voice/text capture sessions. A capture session is a \
monologue — the user dumped thoughts without interruption. Your job is \
to structure what they said into six buckets using the \
``emit_structured_summary`` tool.

Be conservative. If a bucket is empty, return an empty list for it — \
don't invent content to fill a category. Never editorialize or add \
commentary; every item must be grounded in something the user \
actually said.

Bucket definitions:
- topics: the main subjects the user talked about (max 8 short phrases).
- decisions: explicit choices the user made out loud ("I'm going to X").
- open_questions: things the user flagged as uncertain or needing more \
thought.
- action_items: concrete things the user said they'd do (or asked to be \
reminded to do).
- key_insights: high-confidence insights only — novel framings or \
realizations. Leave empty if the session was mostly venting or \
exploratory.
- raw_contradictions: moments where the user contradicted themselves \
earlier in the same session. Quote both sides briefly.

Entity discrimination — default to NEW, not SAME. When this transcript \
references a known entity (person, building, org, project, location), \
treat it as a NEW reference unless the transcript explicitly \
identifies it as the SAME as a prior known entity. Names overlapping \
with recently-discussed records are not the same record; same context \
(a clinic move, the same partner) does not imply same entity. If a \
reference is ambiguous, leave it abstract in structured output and \
surface the ambiguity in ``open_questions`` rather than collapsing it \
onto the most-recently-discussed record.

Worked examples:

GOOD — explicit SAME signal: user says "calling Wayne Fowler again \
about the Greenwood building" — link to existing Wayne Fowler / \
Greenwood entities; both named explicitly.

GOOD — explicit NEW signal: user says "looking at a new commercial \
property in New Minas, 8736 Commercial St, landlord Hussein Rafih" — \
treat Hussein Rafih and 8736 Commercial St as NEW entities. Do NOT \
link to Wayne Fowler / Greenwood despite a shared business context \
running through prior sessions.

BAD — over-application of prior context: user says "Jamie's NP \
practice is moving into a commercial space, lease starts May 15" — \
do NOT structure this as "moving into Wayne Fowler / Greenwood \
building" just because that was the most-recently-discussed building. \
Leave the building reference abstract; flag the ambiguity as an open \
question ("Which commercial space — Wayne Fowler / Greenwood, or a \
new property?").
"""


_BATCH_TOOL_SCHEMA: dict[str, Any] = {
    "name": "emit_structured_summary",
    "description": (
        "Emit the structured summary of a capture session. Call exactly "
        "once. Every bucket must be a list of strings; empty lists are "
        "legal."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "topics": {"type": "array", "items": {"type": "string"}},
            "decisions": {"type": "array", "items": {"type": "string"}},
            "open_questions": {"type": "array", "items": {"type": "string"}},
            "action_items": {"type": "array", "items": {"type": "string"}},
            "key_insights": {"type": "array", "items": {"type": "string"}},
            "raw_contradictions": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "topics",
            "decisions",
            "open_questions",
            "action_items",
            "key_insights",
            "raw_contradictions",
        ],
    },
}


# --- Batch call ----------------------------------------------------------


def _flatten_transcript(transcript: list[dict[str, Any]]) -> str:
    """Render the transcript as a plain-text monologue for Sonnet.

    Capture sessions are user-only for the conversational turns (the
    bot stayed silent), so the flattener concatenates user turn content
    with timestamps. Assistant turns shouldn't exist in a capture
    session but we skip them defensively if present.
    """
    lines: list[str] = []
    for turn in transcript:
        role = turn.get("role", "?")
        if role != "user":
            continue  # skip any stray assistant turns
        content = turn.get("content", "")
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            # tool_result and other block lists — ignore in capture mode.
            continue
        else:
            text = str(content)
        if not text:
            continue
        ts = turn.get("_ts", "")
        ts_short = ts[11:16] if isinstance(ts, str) and len(ts) >= 16 else ""
        if ts_short:
            lines.append(f"[{ts_short}] {text}")
        else:
            lines.append(text)
    return "\n".join(lines)


def _first_user_text(transcript: list[dict[str, Any]]) -> str:
    """Return the first user turn's text content, or ``""``.

    Local copy of :func:`session._first_user_text` to avoid the
    cross-import (session.py imports from .state, .config; pulling it
    in here for one helper would broaden the dependency surface). Same
    contract: tolerates both string ``content`` and list-of-blocks
    ``content`` shapes.
    """
    for turn in transcript:
        if turn.get("role") != "user":
            continue
        content = turn.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    return str(block.get("text") or "")
        return ""
    return ""


async def run_batch_structuring(
    client: Any,
    transcript: list[dict[str, Any]],
    model: str,
) -> StructuredSummary:
    """Run one Sonnet call with tool_use to structure the capture transcript.

    Uses prompt caching on the system block (stable per deploy) + a
    fresh user turn carrying the transcript. The model is forced to
    emit exactly one ``emit_structured_summary`` tool_use block —
    ``tool_choice`` pins to the specific tool so we never pay for text.

    Raises ``RuntimeError`` on any upstream failure (missing tool_use
    block, invalid input shape, API error). The caller owns the
    failure-path behaviour (writing a ``capture_structured: failed``
    marker).
    """
    flat = _flatten_transcript(transcript)
    user_content = (
        "Transcript:\n---\n" + (flat or "(empty)") + "\n---\n\n"
        "Emit the structured summary via the emit_structured_summary tool."
    )

    response = await client.messages.create(**messages_create_kwargs(
        model=model,
        max_tokens=2048,
        temperature=0.2,
        system=[
            {
                "type": "text",
                "text": _BATCH_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_content}],
        tools=[_BATCH_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": "emit_structured_summary"},
    ))

    content = getattr(response, "content", None) or []
    tool_input: dict[str, Any] | None = None
    for block in content:
        btype = getattr(block, "type", None) or (
            block.get("type") if isinstance(block, dict) else None
        )
        if btype != "tool_use":
            continue
        raw_input = getattr(block, "input", None) or (
            block.get("input") if isinstance(block, dict) else None
        )
        if isinstance(raw_input, dict):
            tool_input = raw_input
            break

    if tool_input is None:
        raise RuntimeError(
            "capture batch: no tool_use block in response"
        )

    # Defensive coercion — the SDK delivers the schema-validated dict,
    # but we still guard against a provider-side regression by coercing
    # missing keys to empty lists and dropping non-string items.
    def _strlist(key: str) -> list[str]:
        raw = tool_input.get(key)
        if not isinstance(raw, list):
            return []
        return [str(x).strip() for x in raw if x]

    return StructuredSummary(
        topics=_strlist("topics"),
        decisions=_strlist("decisions"),
        open_questions=_strlist("open_questions"),
        action_items=_strlist("action_items"),
        key_insights=_strlist("key_insights"),
        raw_contradictions=_strlist("raw_contradictions"),
    )


# --- Rendering -----------------------------------------------------------


def render_summary_markdown(
    summary: StructuredSummary,
    *,
    re_encounters_body: str = "",
) -> str:
    """Render a :class:`StructuredSummary` as ``## Structured Summary`` markdown.

    Wrapped in ``<!-- ALFRED:DYNAMIC -->`` markers so the distiller can
    strip it before running learning extraction (matches the existing
    calibration-block protocol). Empty buckets still render their
    heading with "(none)" — preserves the schema shape so a downstream
    parser can rely on every heading being present, and makes "this
    session had no decisions" visually distinguishable from "we
    forgot to extract decisions".

    ``re_encounters_body`` (2026-05-16, capture-source-anchor arc) is
    the pre-rendered body of the new ``### Re-encounters`` 7th section
    (excluding the heading itself). When empty, the section emits
    ``(none)`` per ``feedback_intentionally_left_blank.md``. The
    extraction-time helper
    :func:`alfred.telegram.capture_source_anchor.render_re_encounters_section`
    is the canonical producer; the orchestrator passes its output
    here.
    """
    lines: list[str] = [SUMMARY_MARKER_START, "", "## Structured Summary", ""]

    def _section(heading: str, items: list[str]) -> None:
        lines.append(f"### {heading}")
        if not items:
            lines.append("(none)")
        else:
            for item in items:
                lines.append(f"- {item}")
        lines.append("")

    _section("Topics", summary.topics)
    _section("Decisions", summary.decisions)
    _section("Open Questions", summary.open_questions)
    _section("Action Items", summary.action_items)
    _section("Key Insights", summary.key_insights)
    _section("Raw Contradictions", summary.raw_contradictions)

    # Re-encounters (7th section, at end). Pre-rendered body in,
    # heading added here so the section shape matches its siblings.
    lines.append("### Re-encounters")
    body = re_encounters_body.strip() or "(none)"
    lines.append(body)
    lines.append("")

    lines.append(SUMMARY_MARKER_END)
    return "\n".join(lines).rstrip() + "\n"


def render_failure_markdown(error_message: str) -> str:
    """Render a failure marker block when the Sonnet call errors.

    Same marker wrapping as the success path so the distiller stripper
    uniformly removes capture summary blocks regardless of
    success/failure. Human-readable so Andrew can see what went wrong
    in Obsidian without grepping logs.
    """
    return (
        f"{SUMMARY_MARKER_START}\n\n"
        "## Structured Summary\n\n"
        f"Structuring failed: {error_message}\n\n"
        "Use `/extract <short-id>` to retry, or re-run manually.\n\n"
        f"{SUMMARY_MARKER_END}\n"
    )


# --- Vault write ---------------------------------------------------------


# Where the summary block lands in the session record body. The session
# record body starts with ``# Transcript\n\n`` — we insert the summary
# ABOVE that header so the structured view is the first thing the user
# sees in Obsidian. If the body shape ever changes (pre-``# Transcript``
# preamble gets added), the insert point moves to "just after the
# first blank line before ``# Transcript``" rather than here; for now
# the literal split keeps the code trivially correct.
_TRANSCRIPT_HEADER: Final[str] = "# Transcript"


def _insert_summary_above_transcript(body: str, summary_md: str) -> str:
    """Insert the summary block above the ``# Transcript`` heading.

    If the heading is missing (older wk1/2 records or a custom body
    shape), prepend the summary block at the top. Idempotent: if a
    summary block already exists (matching the marker pair), the old
    one is replaced in-place rather than nested.
    """
    # Strip any pre-existing summary block.
    start_idx = body.find(SUMMARY_MARKER_START)
    if start_idx != -1:
        end_idx = body.find(SUMMARY_MARKER_END, start_idx)
        if end_idx != -1:
            end_idx += len(SUMMARY_MARKER_END)
            # Also eat trailing blank line(s) after the marker to avoid
            # ballooning whitespace on repeat runs.
            while end_idx < len(body) and body[end_idx] in "\n":
                end_idx += 1
            body = body[:start_idx] + body[end_idx:]

    if _TRANSCRIPT_HEADER in body:
        head, sep, tail = body.partition(_TRANSCRIPT_HEADER)
        return head + summary_md + "\n" + sep + tail
    return summary_md + "\n" + body


async def write_summary_to_session_record(
    vault_path: Path,
    session_rel_path: str,
    summary_markdown: str,
    structured_flag: str,
    *,
    agent_slug: str = "salem",
    extra_fields: dict[str, Any] | None = None,
) -> None:
    """Inject the summary block + flip ``capture_structured`` frontmatter.

    ``structured_flag`` is one of ``"true"`` / ``"failed"`` — stored as
    a YAML string (not a bool) to leave room for future states like
    ``"partial"`` without a schema migration. The session record MUST
    already exist (written by session.close_session before this is
    called).

    ``agent_slug`` is the running instance's slug (lowercased
    ``config.instance.name``); the attribution-audit entry's ``agent``
    field carries this. Defaults to ``"salem"`` so legacy callers and
    older tests preserve their behaviour. Pass
    :func:`alfred.audit.agent_slug_for(config)` from production call
    sites so Hypatia/KAL-LE captures attribute correctly.

    Calibration audit gap (c4): the entire ``## Structured Summary``
    block is Sonnet-inferred output (every section is a model
    classification of the user's monologue), so we wrap the rendered
    markdown in BEGIN_INFERRED/END_INFERRED markers and append one
    ``attribution_audit`` entry to the session record's frontmatter.
    Failure-marker writes (``structured_flag="failed"``) skip the
    wrapping — there's no inferred prose, just a human-readable error.
    """
    set_fields: dict = {"capture_structured": structured_flag}
    if extra_fields:
        # Merge caller-supplied fields (e.g. ``source`` / ``author`` /
        # ``continues_from`` wikilinks produced by the capture-source-
        # anchor resolver). The caller's keys win — they reflect
        # resolution decisions specific to this session-close.
        set_fields.update(extra_fields)
    summary_to_write = summary_markdown

    if structured_flag == "true":
        # Local import to keep this module's surface tight.
        from alfred.vault import attribution

        wrapped, audit_entry = attribution.with_inferred_marker(
            summary_markdown,
            section_title="Structured Summary",
            agent=agent_slug,
            reason=f"capture batch structuring (session={session_rel_path})",
        )
        summary_to_write = wrapped

        # Merge into existing audit list if any.
        existing_audit: list = []
        try:
            existing = ops.vault_read(vault_path, session_rel_path)
            existing_fm = existing.get("frontmatter") or {}
            if isinstance(existing_fm.get("attribution_audit"), list):
                existing_audit = list(existing_fm["attribution_audit"])
        except Exception as exc:  # noqa: BLE001 — read failure shouldn't block the write
            log.info(
                "talker.capture.audit_read_failed",
                session_rel_path=session_rel_path,
                error=str(exc),
            )

        merged_fm: dict = {"attribution_audit": existing_audit}
        attribution.append_audit_entry(merged_fm, audit_entry)
        set_fields["attribution_audit"] = merged_fm["attribution_audit"]

    def _rewrite(body: str) -> str:
        return _insert_summary_above_transcript(body, summary_to_write)

    ops.vault_edit(
        vault_path,
        session_rel_path,
        set_fields=set_fields,
        body_rewriter=_rewrite,
    )


# --- Memo branch (≤1 user message → memo path, skip batch pipeline) ------
#
# Phase 1 Zettelkasten cutover (2026-05-16). Per
# project_hypatia_zettelkasten_redesign.md "LOCKED IMPLEMENTATION PLAN":
# capture sessions with ≤1 user message at /end (or timeout-close) take
# the memo path instead of running the structured-extraction pipeline.
#
# Memo path is fast + cheap: no Sonnet calls, no structured summary,
# no re-encounter scan. Just a ``memo/<slug>.md`` record carrying the
# raw user text + a wikilink back to the originating session.
#
# Trigger is per-instance via ``anchor_scope`` — only Hypatia carries
# the ``memo`` create-allowlist entry today. Salem captures with ≤1
# user message will FALL THROUGH to the regular batch path (which
# handles low-content sessions by emitting empty buckets — already
# tested behaviour). Adding ``memo`` to Salem's scope is a future
# decision; not made here.


#: Memo branch trigger threshold. Sessions with this many user turns
#: or fewer route to the memo path. The threshold is "≤1" per Andrew's
#: spec — single-thought captures land as memos; multi-message
#: sessions go through extraction.
_MEMO_BRANCH_MAX_USER_TURNS: int = 1


#: Body cap for memo records — guard against the rare case where a
#: single user turn is enormous (e.g. operator pasted a long block of
#: text in one /capture turn). Memo bodies trim at this byte count
#: with a "(truncated)" marker so the file stays manageable in
#: Obsidian's editor; the full text lives in the originating session
#: record's transcript regardless.
_MEMO_BODY_MAX_CHARS: int = 4000


def _count_user_turns(transcript: list[dict[str, Any]]) -> int:
    """Count user turns with non-empty text content.

    Mirrors the filter logic in ``_first_user_text`` / ``_flatten_transcript``
    — only user-role turns with actual text content (string ``content``
    or text-block list) count. Empty-content turns and assistant turns
    are excluded.
    """
    count = 0
    for turn in transcript:
        if turn.get("role") != "user":
            continue
        content = turn.get("content", "")
        text = ""
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    btext = (block.get("text") or "").strip()
                    if btext:
                        text = btext
                        break
        if text:
            count += 1
    return count


def _memo_slug_from_text(text: str, *, max_words: int = 5) -> str:
    """Derive a filename-safe memo slug from the raw user text.

    Local helper (mirrors :func:`session._slug_from_topic`) so this
    module doesn't import session.py — same dependency-isolation
    rationale as ``_first_user_text``. Returns ``"untitled"`` on empty
    input; trimming + casefolding + non-alphanumeric stripping keep
    the slug filename-safe across all filesystems Alfred targets.
    """
    import re as _re

    if not text:
        return "untitled"
    s = text.strip().lower()
    if not s:
        return "untitled"
    tokens = s.split()[:max_words]
    joined = "-".join(tokens)
    # Drop everything not a-z / 0-9 / -, collapse runs, trim.
    joined = _re.sub(r"[^a-z0-9-]", "", joined)
    joined = _re.sub(r"-{2,}", "-", joined).strip("-")
    return joined or "untitled"


def _memo_body_from_text(text: str, *, max_chars: int = _MEMO_BODY_MAX_CHARS) -> str:
    """Render the raw user text as a memo body.

    Pattern matches the memo template (``# Memo`` / ``# Context`` /
    ``# Tags``): the user's text lands under ``# Memo``; ``# Context``
    + ``# Tags`` stay empty (operator fills them retrospectively if
    useful). Truncation at ``max_chars`` adds a ``(truncated)`` marker
    so the operator knows the full text is in the session record.
    """
    body = (text or "").strip()
    truncated = False
    if len(body) > max_chars:
        body = body[:max_chars].rstrip()
        truncated = True

    lines = ["# Memo", "", body]
    if truncated:
        lines += ["", "_(truncated — full text in originating session)_"]
    lines += ["", "# Context", "", "# Tags", ""]
    return "\n".join(lines)


async def _create_memo_record(
    vault_path: Path,
    user_text: str,
    session_rel_path: str,
    *,
    scope: str,
    agent_slug: str,
) -> str | None:
    """Write a ``memo/<slug>.md`` record. Return the rel_path or None on failure.

    Body shape comes from :func:`_memo_body_from_text` (raw text + memo
    template scaffolding). Frontmatter carries:

      * ``type: memo`` (set by ops via the type kwarg)
      * ``name``: the human-readable title (slug-display form)
      * ``session``: wikilink to the originating capture session
      * ``created``: today's date (ops default)

    On vault_create failure (scope-deny, write error, etc.), logs and
    returns ``None`` — the caller falls back to the regular batch path
    so a misconfigured memo trigger doesn't black-hole the session.

    ``agent_slug`` is currently unused at this layer (vault_create
    doesn't carry agent attribution for non-attribution-audit writes);
    accepted as a kwarg so the call site matches the rest of the
    orchestrator surface and a future change adding memo attribution
    has the slug in scope.
    """
    slug = _memo_slug_from_text(user_text)
    # Display name is the first ~8 words capitalised, falling back to
    # the slug if the text is empty.
    display = " ".join((user_text or "").strip().split()[:8])
    if not display:
        display = slug.replace("-", " ").title() or "Untitled Memo"

    body = _memo_body_from_text(user_text)
    set_fields = {
        "session": f"[[{session_rel_path[:-3]}]]" if session_rel_path.endswith(".md")
                   else f"[[{session_rel_path}]]",
    }

    try:
        result = ops.vault_create(
            vault_path,
            "memo",
            slug,
            set_fields=set_fields,
            body=body,
            scope=(scope or None),
        )
    except ops.VaultError as exc:
        log.warning(
            "talker.capture.memo_create_failed",
            session_rel_path=session_rel_path,
            slug=slug,
            scope=scope,
            error=str(exc),
        )
        return None

    # Best-effort: set ``name`` to the human-readable display form so
    # Obsidian's title pane shows something nicer than the slug.
    # Failure here is non-fatal — the record exists; just the display
    # name didn't update.
    try:
        ops.vault_edit(
            vault_path,
            result["path"],
            set_fields={"name": display},
            scope=(scope or None),
        )
    except Exception as exc:  # noqa: BLE001
        log.info(
            "talker.capture.memo_name_update_failed",
            memo_path=result["path"],
            error=str(exc),
        )

    return result["path"]


# --- Orchestrator --------------------------------------------------------


async def process_capture_session(
    client: Any,
    vault_path: Path,
    session_rel_path: str,
    transcript: list[dict[str, Any]],
    model: str,
    send_follow_up: Any | None = None,
    short_id: str = "",
    *,
    agent_slug: str = "salem",
    anchor_scope: str = "",
    extract_target_override: str = "",
) -> None:
    """Top-level orchestrator — run batch pass, write summary, send follow-up.

    Scheduled as a detached ``asyncio.create_task`` from ``bot.on_end``.
    Never raises — failures are logged and surfaced via the
    ``capture_structured: failed`` frontmatter flag + failure markdown
    block. The Telegram follow-up message is a best-effort send.

    ``send_follow_up`` is a callable ``(text: str) -> Awaitable[None]``
    (typically a bound Telegram ``Bot.send_message`` call with chat_id
    pre-bound). Passed in rather than built inside so the orchestrator
    stays testable without a full bot context.

    ``agent_slug`` is the running instance's slug (lowercased
    ``config.instance.name``) — forwarded to
    :func:`write_summary_to_session_record` so the attribution-audit
    entry carries the right agent.

    ``anchor_scope`` (2026-05-16, capture-source-anchor arc) — when
    non-empty, the orchestrator parses the first user turn for
    ``I'm reading X by Y`` / ``This continues from [[X]]`` patterns
    and resolves source/author records via the
    :mod:`capture_source_anchor` module. The scope name is forwarded
    to vault_create as the create-allowlist key. Default ``""``
    preserves legacy behaviour for instances that don't carry the
    ``author`` type (e.g. Salem).

    ``extract_target_override`` (Phase 1.x, 2026-05-16) — operator's
    explicit choice from the ``/end-zettel`` / ``/end-note`` slash-
    command variants. When non-empty (``"zettel"`` or ``"note"``), it's
    written to the session record's
    ``capture_extract_target_override:`` frontmatter field so the
    later ``/extract`` invocation honours it via the three-tier
    discriminator. Default ``""`` → field omitted; the discriminator
    falls back to the source-anchored default.
    """
    # Local import keeps this module's surface tight + avoids the
    # capture_source_anchor module loading frontmatter eagerly during
    # cold-start test imports.
    from . import capture_source_anchor as _csa

    # --- Anchor resolution (best-effort; never blocks the batch pass).
    anchors: _csa.ResolvedAnchors | None = None
    opening_text = ""
    if anchor_scope:
        opening_text = _first_user_text(transcript)
        if opening_text:
            try:
                anchors = _csa.resolve_session_anchors(
                    vault_path, opening_text, scope=anchor_scope,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "talker.capture.anchor_resolve_failed",
                    session_rel_path=session_rel_path,
                    error=str(exc),
                )
                anchors = None
            else:
                log.info(
                    "talker.capture.anchors_resolved",
                    session_rel_path=session_rel_path,
                    source_wikilink=anchors.source_wikilink,
                    author_wikilink=anchors.author_wikilink,
                    continues_from=anchors.continues_from,
                    source_created=anchors.source_created,
                    author_created=anchors.author_created,
                    author_ambiguous=anchors.author_ambiguous,
                )

    # Extra session-frontmatter fields written alongside the summary.
    extra_fields: dict[str, Any] = {}
    if anchors is not None:
        if anchors.source_wikilink:
            extra_fields["source"] = anchors.source_wikilink
        if anchors.author_wikilink:
            extra_fields["author"] = anchors.author_wikilink
        if anchors.continues_from:
            extra_fields["continues_from"] = anchors.continues_from

    # Phase 1.x operator override (2026-05-16). When the operator closed
    # the session with ``/end-zettel`` or ``/end-note`` instead of plain
    # ``/end``, the bot.py snapshot caller threads the choice through to
    # this orchestrator. Persist it to session frontmatter so the
    # later ``/extract`` invocation (run via the deferred
    # capture_extract.extract_notes_from_capture path) honours the
    # operator's choice. Validates to one of the canonical values so
    # garbage values don't pollute the record.
    if extract_target_override in ("zettel", "note"):
        extra_fields["capture_extract_target_override"] = (
            extract_target_override
        )

    # --- Memo branch (Phase 1 Zettelkasten cutover, 2026-05-16).
    # ≤1 user message + Hypatia scope → memo path. Skip the entire
    # batch-structuring pipeline (no Sonnet call, no summary block, no
    # re-encounter scan). The memo lives at ``memo/<slug>.md`` with a
    # wikilink back to the originating session record. Per the brief:
    # "the captured thought, lightly cleaned" — body is the raw user
    # text under the ``# Memo`` heading.
    #
    # Salem captures with ≤1 user message do NOT branch here (Salem's
    # scope doesn't carry the ``memo`` create-allowlist entry); they
    # continue through the batch pipeline and produce a session record
    # with mostly-empty Structured Summary buckets (existing behaviour).
    #
    # Failure-isolated: if memo creation fails (scope deny, vault
    # write error), we fall back to the regular batch path. Operator
    # sees the session record either way; just the memo extraction
    # mode degrades.
    user_turn_count = _count_user_turns(transcript)
    if anchor_scope == "hypatia" and user_turn_count <= _MEMO_BRANCH_MAX_USER_TURNS:
        first_user_text = _first_user_text(transcript)
        log.info(
            "talker.capture.memo_branch_triggered",
            session_rel_path=session_rel_path,
            user_turn_count=user_turn_count,
            anchor_scope=anchor_scope,
            first_user_text_len=len(first_user_text),
        )
        memo_rel = await _create_memo_record(
            vault_path=vault_path,
            user_text=first_user_text,
            session_rel_path=session_rel_path,
            scope=anchor_scope,
            agent_slug=agent_slug,
        )
        if memo_rel is not None:
            # Tag the session record so downstream tooling (distiller,
            # vault-reviewer) knows the structured-extraction step was
            # deliberately skipped. ``capture_structured: memo`` is a
            # third valid value alongside ``"true"`` and ``"failed"``;
            # downstream consumers should treat it as "valid completion
            # via the memo path, no Structured Summary expected".
            session_set_fields: dict[str, Any] = {
                "capture_structured": "memo",
                "memo_record": f"[[{memo_rel[:-3]}]]" if memo_rel.endswith(".md")
                               else f"[[{memo_rel}]]",
            }
            session_set_fields.update(extra_fields)
            try:
                ops.vault_edit(
                    vault_path,
                    session_rel_path,
                    set_fields=session_set_fields,
                    scope=(anchor_scope or None),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "talker.capture.memo_session_update_failed",
                    session_rel_path=session_rel_path,
                    error=str(exc),
                )
            log.info(
                "talker.capture.memo_done",
                session_rel_path=session_rel_path,
                memo_rel=memo_rel,
            )
            if send_follow_up is not None:
                try:
                    await send_follow_up(
                        f"Captured as memo ({short_id}). "
                        f"Saved to: {memo_rel}"
                    )
                except Exception as send_exc:  # noqa: BLE001
                    log.warning(
                        "talker.capture.follow_up_failed",
                        session_rel_path=session_rel_path,
                        error=str(send_exc),
                    )
            return
        else:
            # Memo creation failed — log and fall through to the batch
            # path so the session is still processed (degraded but not
            # black-holed). The memo_create_failed log already fired
            # inside _create_memo_record.
            log.warning(
                "talker.capture.memo_branch_fallback_to_batch",
                session_rel_path=session_rel_path,
                user_turn_count=user_turn_count,
            )

    try:
        summary = await run_batch_structuring(client, transcript, model)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.capture.batch_failed",
            session_rel_path=session_rel_path,
            error=str(exc),
        )
        failure_md = render_failure_markdown(str(exc))
        try:
            await write_summary_to_session_record(
                vault_path, session_rel_path, failure_md, "failed",
                agent_slug=agent_slug,
                extra_fields=extra_fields,
            )
        except Exception as write_exc:  # noqa: BLE001
            log.warning(
                "talker.capture.failure_write_failed",
                session_rel_path=session_rel_path,
                error=str(write_exc),
            )
        if send_follow_up is not None:
            try:
                await send_follow_up(
                    f"Structuring failed ({short_id or 'no short-id'}) — "
                    f"run /extract {short_id} to retry."
                )
            except Exception as send_exc:  # noqa: BLE001
                log.warning(
                    "talker.capture.follow_up_failed",
                    session_rel_path=session_rel_path,
                    error=str(send_exc),
                )
        return

    # Re-encounter scan — recency-capped vault search over source /
    # author / topic terms. Failure is non-fatal; section renders
    # ``(none)`` if the scan errors. Per
    # ``feedback_intentionally_left_blank.md`` the section is always
    # emitted so "no prior records" is distinguishable from "scan
    # skipped".
    re_encounters_body = "(none)"
    if anchor_scope:
        try:
            rows = _csa.find_re_encounters(
                vault_path,
                source_wikilink=(anchors.source_wikilink if anchors else ""),
                author_wikilink=(anchors.author_wikilink if anchors else ""),
                topic_terms=list(summary.topics),
                current_session_rel_path=session_rel_path,
            )
            re_encounters_body = _csa.render_re_encounters_section(rows)
            log.info(
                "talker.capture.re_encounters_scanned",
                session_rel_path=session_rel_path,
                hits=len(rows),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "talker.capture.re_encounters_failed",
                session_rel_path=session_rel_path,
                error=str(exc),
            )

    summary_md = render_summary_markdown(
        summary, re_encounters_body=re_encounters_body,
    )
    try:
        await write_summary_to_session_record(
            vault_path, session_rel_path, summary_md, "true",
            agent_slug=agent_slug,
            extra_fields=extra_fields,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.capture.write_failed",
            session_rel_path=session_rel_path,
            error=str(exc),
        )
        if send_follow_up is not None:
            try:
                await send_follow_up(
                    f"Couldn't write summary for {short_id}. Check the log."
                )
            except Exception as send_exc:  # noqa: BLE001
                log.warning(
                    "talker.capture.follow_up_failed",
                    session_rel_path=session_rel_path,
                    error=str(send_exc),
                )
        return

    # Phase 2 deliverable #4 (2026-05-17): re-encounter source-body
    # append. When the capture session resolved to a PRE-EXISTING source
    # record (source_created=False), append today's observations to
    # that source's ``## Observations During`` section. First-encounter
    # sources (just created by the resolver) skip this — they have no
    # prior body to extend.
    #
    # Failure-isolated: a missing source record, a body without the
    # ``## Observations During`` section, or any other write error logs
    # + returns False without raising. The session record is already
    # written; the re-encounter append is best-effort decoration.
    if (
        anchors is not None
        and anchors.source_wikilink
        and not anchors.source_created
    ):
        try:
            from datetime import date as _date
            # Resolve source rel_path from the wikilink form
            # ``[[source/<Title>]]`` → ``source/<Title>.md``.
            source_rel = anchors.source_wikilink.strip("[]")
            if not source_rel.endswith(".md"):
                source_rel = source_rel + ".md"
            _csa.append_re_encounter_observation(
                vault_path=vault_path,
                source_rel_path=source_rel,
                today_iso=_date.today().isoformat(),
                topics=list(summary.topics),
                key_insights=list(summary.key_insights),
                session_rel_path=session_rel_path,
                scope=(anchor_scope or "hypatia"),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "talker.capture.re_encounter_append_unhandled",
                session_rel_path=session_rel_path,
                error=str(exc),
            )

    log.info(
        "talker.capture.batch_done",
        session_rel_path=session_rel_path,
        topics=len(summary.topics),
        decisions=len(summary.decisions),
        action_items=len(summary.action_items),
    )

    if send_follow_up is not None:
        try:
            await send_follow_up(
                f"Capture structured ({short_id}). "
                f"/extract {short_id} for notes, /brief {short_id} for audio."
            )
        except Exception as send_exc:  # noqa: BLE001
            log.warning(
                "talker.capture.follow_up_failed",
                session_rel_path=session_rel_path,
                error=str(send_exc),
            )


# --- JSON-round-trip helper for tests ------------------------------------

def summary_to_dict(summary: StructuredSummary) -> dict[str, Any]:
    """Return a JSON-serialisable dict — used by tests + future telemetry."""
    return {
        "topics": list(summary.topics),
        "decisions": list(summary.decisions),
        "open_questions": list(summary.open_questions),
        "action_items": list(summary.action_items),
        "key_insights": list(summary.key_insights),
        "raw_contradictions": list(summary.raw_contradictions),
    }


# ---------------------------------------------------------------------------
# Brief recap (mid-session /recap, queue #10, 2026-05-18)
# ---------------------------------------------------------------------------
#
# ``/recap brief`` (default) needs a lighter structured summary than the
# end-of-session 7-bucket extraction. Brief is two buckets only —
# topics + key_insights — so the cost is much lower and the reply is
# fast for an operator who just wants "what threads am I on, what's
# stood out?" mid-capture.
#
# ``/recap verbose`` reuses the full ``run_batch_structuring`` + 6
# StructuredSummary buckets. The handler renders both via
# ``render_recap_markdown`` (no ALFRED:DYNAMIC markers — recap output
# is for Telegram reply, not vault embedding).


@dataclass(frozen=True)
class BriefRecap:
    """Brief recap shape — 2 buckets only.

    Distinct from ``StructuredSummary`` (6 buckets) because brief
    recap intentionally narrows to the two surfaces an operator cares
    about mid-capture: what subjects am I actively touching, and what
    has stood out so far. The other four buckets (decisions, open
    questions, action items, raw contradictions) are end-of-session
    artefacts — mid-session the operator hasn't finished the thought.
    """

    topics: list[str] = field(default_factory=list)
    key_insights: list[str] = field(default_factory=list)


_BRIEF_RECAP_SYSTEM_PROMPT = """\
You produce a brief mid-session recap of an in-progress capture \
session. A capture session is a monologue — the user is dumping \
thoughts without interruption. They've asked ``/recap`` mid-session \
to see what they've touched so far.

Two buckets only, via the ``emit_brief_recap`` tool:

- topics: the main subjects the user is talking about so far \
(2-5 short phrases). Threads they're working on. Leave empty if the \
session is too thin to identify discrete topics.
- key_insights: high-confidence insights that have stood out so far \
(0-5 bullets). Novel framings or realisations they've voiced. Leave \
empty if the session has been venting or exploratory; better empty \
than fabricated.

Be conservative. Never invent or editorialise. Every item must be \
grounded in something the user actually said. Empty lists are legal \
and often correct — the operator can see the bucket is empty and \
draw their own conclusion ("haven't surfaced anything yet").
"""


_BRIEF_RECAP_TOOL_SCHEMA: dict[str, Any] = {
    "name": "emit_brief_recap",
    "description": (
        "Emit a brief mid-session recap. Call exactly once. Each bucket "
        "must be a list of strings; empty lists are legal."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "topics": {"type": "array", "items": {"type": "string"}},
            "key_insights": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["topics", "key_insights"],
    },
}


async def run_brief_recap_structuring(
    client: Any,
    transcript: list[dict[str, Any]],
    model: str,
) -> BriefRecap:
    """Cheaper sibling of ``run_batch_structuring`` — 2 buckets, 1024 max
    tokens, lower temperature for stability.

    Raises ``RuntimeError`` on tool-block absence; failure-isolation is
    the caller's responsibility (the recap handler renders an error
    message rather than crashing the user's chat).
    """
    flat = _flatten_transcript(transcript)
    user_content = (
        "Transcript so far:\n---\n" + (flat or "(empty)") + "\n---\n\n"
        "Emit the brief recap via the emit_brief_recap tool."
    )

    response = await client.messages.create(**messages_create_kwargs(
        model=model,
        max_tokens=1024,
        temperature=0.2,
        system=[
            {
                "type": "text",
                "text": _BRIEF_RECAP_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_content}],
        tools=[_BRIEF_RECAP_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": "emit_brief_recap"},
    ))

    content = getattr(response, "content", None) or []
    tool_input: dict[str, Any] | None = None
    for block in content:
        btype = getattr(block, "type", None) or (
            block.get("type") if isinstance(block, dict) else None
        )
        if btype != "tool_use":
            continue
        raw_input = getattr(block, "input", None) or (
            block.get("input") if isinstance(block, dict) else None
        )
        if isinstance(raw_input, dict):
            tool_input = raw_input
            break

    if tool_input is None:
        raise RuntimeError(
            "brief recap: no tool_use block in response"
        )

    def _strlist(key: str) -> list[str]:
        raw = tool_input.get(key)
        if not isinstance(raw, list):
            return []
        return [str(x).strip() for x in raw if x]

    return BriefRecap(
        topics=_strlist("topics"),
        key_insights=_strlist("key_insights"),
    )


def render_recap_markdown(
    summary: StructuredSummary | BriefRecap,
    *,
    mode: str,
) -> str:
    """Render a recap (brief or verbose) for Telegram reply.

    Distinct from :func:`render_summary_markdown`:
      * No ``<!-- ALFRED:DYNAMIC -->`` marker wrapping. Recap output is
        a chat reply, NOT a vault-embedded summary the distiller needs
        to strip.
      * Brief mode renders 2 buckets (Topics, Key Insights). Verbose
        renders the full 6 buckets — same as the end-of-session
        summary but without the Re-encounters section (recap is
        mid-session; re-encounter scan is post-close).
      * Empty buckets render with ``(none)`` per the "intentionally
        left blank" discipline — operator sees the empty buckets
        explicitly so "nothing surfaced yet" is distinguishable from
        "extraction silently dropped a bucket."

    ``mode`` is exactly ``"brief"`` or ``"verbose"`` — the handler is
    the only caller; it validates the operator's argument before
    invoking.
    """
    lines: list[str] = []
    if mode == "brief":
        if not isinstance(summary, BriefRecap):
            raise ValueError(
                f"render_recap_markdown(mode='brief') expects BriefRecap, "
                f"got {type(summary).__name__}"
            )
        lines.append("## Recap (brief)")
        lines.append("")

        def _section(heading: str, items: list[str]) -> None:
            lines.append(f"### {heading}")
            if not items:
                lines.append("(none)")
            else:
                for item in items:
                    lines.append(f"- {item}")
            lines.append("")

        _section("Topics", summary.topics)
        _section("Key Insights", summary.key_insights)
    elif mode == "verbose":
        if not isinstance(summary, StructuredSummary):
            raise ValueError(
                f"render_recap_markdown(mode='verbose') expects "
                f"StructuredSummary, got {type(summary).__name__}"
            )
        lines.append("## Recap (verbose)")
        lines.append("")

        def _section_full(heading: str, items: list[str]) -> None:
            lines.append(f"### {heading}")
            if not items:
                lines.append("(none)")
            else:
                for item in items:
                    lines.append(f"- {item}")
            lines.append("")

        _section_full("Topics", summary.topics)
        _section_full("Decisions", summary.decisions)
        _section_full("Open Questions", summary.open_questions)
        _section_full("Action Items", summary.action_items)
        _section_full("Key Insights", summary.key_insights)
        _section_full("Raw Contradictions", summary.raw_contradictions)
    else:
        raise ValueError(
            f"render_recap_markdown: mode must be 'brief' or 'verbose', "
            f"got {mode!r}"
        )

    return "\n".join(lines).rstrip() + "\n"


# Re-exports for the bot layer.
__all__ = [
    "StructuredSummary",
    "SUMMARY_MARKER_START",
    "SUMMARY_MARKER_END",
    "run_batch_structuring",
    "render_summary_markdown",
    "render_failure_markdown",
    "write_summary_to_session_record",
    "process_capture_session",
    "summary_to_dict",
    # Memo branch (Phase 1 Zettelkasten cutover, 2026-05-16) — exposed
    # for testability.
    "_count_user_turns",
    "_memo_slug_from_text",
    "_memo_body_from_text",
    "_create_memo_record",
    "_MEMO_BRANCH_MAX_USER_TURNS",
    # Brief recap (queue #10, 2026-05-18).
    "BriefRecap",
    "run_brief_recap_structuring",
    "render_recap_markdown",
]
