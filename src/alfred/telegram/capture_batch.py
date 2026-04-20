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

    response = await client.messages.create(
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
    )

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


def render_summary_markdown(summary: StructuredSummary) -> str:
    """Render a :class:`StructuredSummary` as ``## Structured Summary`` markdown.

    Wrapped in ``<!-- ALFRED:DYNAMIC -->`` markers so the distiller can
    strip it before running learning extraction (matches the existing
    calibration-block protocol). Empty buckets still render their
    heading with "(none)" — preserves the schema shape so a downstream
    parser can rely on every heading being present, and makes "this
    session had no decisions" visually distinguishable from "we
    forgot to extract decisions".
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
) -> None:
    """Inject the summary block + flip ``capture_structured`` frontmatter.

    ``structured_flag`` is one of ``"true"`` / ``"failed"`` — stored as
    a YAML string (not a bool) to leave room for future states like
    ``"partial"`` without a schema migration. The session record MUST
    already exist (written by session.close_session before this is
    called).
    """
    def _rewrite(body: str) -> str:
        return _insert_summary_above_transcript(body, summary_markdown)

    ops.vault_edit(
        vault_path,
        session_rel_path,
        set_fields={"capture_structured": structured_flag},
        body_rewriter=_rewrite,
    )


# --- Orchestrator --------------------------------------------------------


async def process_capture_session(
    client: Any,
    vault_path: Path,
    session_rel_path: str,
    transcript: list[dict[str, Any]],
    model: str,
    send_follow_up: Any | None = None,
    short_id: str = "",
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
    """
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

    summary_md = render_summary_markdown(summary)
    try:
        await write_summary_to_session_record(
            vault_path, session_rel_path, summary_md, "true",
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
]
