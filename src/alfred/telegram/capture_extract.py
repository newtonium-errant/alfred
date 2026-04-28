"""``/extract <short-id>`` — opt-in note extraction from a capture session.

Flow when invoked:

    1. Resolve short-id → session record path via the ``closed_sessions``
       state entries (or active if the session is still open — rare).
    2. Load the session record; if it has no ``## Structured Summary``
       section yet, run the batch structuring pass first (implicit chain).
    3. Ask Sonnet to extract up to N standalone ``note`` records via a
       ``create_note`` tool, one call per proposed note. Each carries a
       ``confidence_tier`` (``high``/``medium``) and a short source
       quote.
    4. Write each note via ``vault_create`` with ``created_by_capture:
       true``, ``source_session: [[session/...]]`` frontmatter. Session
       record gets its ``derived_notes`` list populated.
    5. Idempotent: if the session already has a populated
       ``derived_notes`` list, refuse and return the existing list.

Module is side-effect-heavy by design — it OWNS writing notes and
updating the session record. The `/brief` command and the batch pass
share no code here; their vault-write paths live in their own modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import frontmatter

from alfred.vault import ops

from . import capture_batch
from ._anthropic_compat import messages_create_kwargs
from .state import StateManager
from .utils import get_logger

log = get_logger(__name__)


# --- Constants -----------------------------------------------------------

# Cap: max number of notes to create from one capture session. 8 was
# ratified on 2026-04-19 — distiller downstream dedup can collapse
# duplicates if the cap is hit, so 8 is a safety rail rather than a
# quality guarantee.
DEFAULT_MAX_NOTES: Final[int] = 8


# --- Tool schema ---------------------------------------------------------

_EXTRACT_TOOL = {
    "name": "create_note",
    "description": (
        "Create one standalone note record from the capture session. "
        "Call this tool up to 8 times, once per note you want to emit. "
        "Each note should be self-contained — a single idea or insight "
        "that survives outside the session's original conversational "
        "context."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Short, descriptive title (becomes filename stem). "
                    "Title Case. Must be findable by search later."
                ),
            },
            "body": {
                "type": "string",
                "description": "Markdown body. 1-3 short paragraphs.",
            },
            "confidence_tier": {
                "type": "string",
                "enum": ["high", "medium"],
                "description": (
                    "High: unambiguous, the user explicitly flagged this "
                    "as important. Medium: reasonable extraction, distiller "
                    "downstream may prune."
                ),
            },
            "source_quote": {
                "type": "string",
                "description": (
                    "A short (<200 char) verbatim quote from the transcript "
                    "that grounds this note. Rendered as a blockquote."
                ),
            },
        },
        "required": ["name", "body", "confidence_tier", "source_quote"],
    },
}


_EXTRACT_SYSTEM_PROMPT = """\
You extract standalone notes from a closed capture session. A capture \
session is a monologue — the user dumped thoughts without interruption. \
The structured summary has already been produced (topics, decisions, \
open questions, action items, key insights, raw contradictions). \
Your job: pick 1-8 ideas from the session that deserve their own \
searchable note and emit each via the ``create_note`` tool.

Rules:
- Quality over quantity. Fewer good notes > filling the 8 slots.
- Each note must be self-contained — it will be read months later \
without the session context.
- The raw transcript + structured summary are your source. Do not \
invent content. Every note must be traceable to a specific transcript \
passage.
- Title Case names, descriptive enough to surface in search. \
"Note" is a bad name. "Insight on Q2 driver retention" is a good name.
- Bodies: 1-3 short paragraphs. Include a blockquote with the source \
quote the tool is asking for.
- Confidence tier: ``high`` means the user explicitly flagged this or \
returned to it multiple times; ``medium`` means you (the model) judged \
it worth extracting but the user didn't dwell.
- Stop when you're out of high-signal ideas, even if you've emitted \
fewer than 8 notes.
"""


# --- Helpers -------------------------------------------------------------


@dataclass(frozen=True)
class ExtractResult:
    """Return shape for :func:`extract_notes_from_capture`."""

    created_paths: list[str]
    skipped_reason: str = ""  # "already_extracted", "no_session", etc.


def _find_session_by_short_id(
    state: StateManager,
    short_id: str,
) -> str | None:
    """Return the vault-relative path of the session whose id starts with ``short_id``.

    Searches closed_sessions first (most common — `/extract` fires after
    `/end`), then active_sessions as a fallback. Returns None if no
    match.
    """
    if not short_id:
        return None
    # Closed first (the common case).
    for entry in reversed(state.state.get("closed_sessions", []) or []):
        session_id = entry.get("session_id", "") or ""
        if session_id.startswith(short_id):
            return entry.get("record_path") or None
    # Active fallback.
    for raw in (state.state.get("active_sessions", {}) or {}).values():
        sid = raw.get("session_id", "") or ""
        if sid.startswith(short_id):
            # Active sessions don't have a record path yet; caller should
            # tell the user to /end first.
            return None
    return None


def _load_session_record(
    vault_path: Path, session_rel_path: str,
) -> frontmatter.Post | None:
    """Load the session record post; return None if missing."""
    file_path = vault_path / session_rel_path
    if not file_path.exists():
        return None
    try:
        return frontmatter.load(file_path)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.extract.session_read_failed",
            session_rel_path=session_rel_path,
            error=str(exc),
        )
        return None


def _derived_notes_from_post(post: frontmatter.Post) -> list[str]:
    """Return the existing ``derived_notes`` list from frontmatter, or []."""
    raw = post.get("derived_notes")
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    return []


def _extract_transcript_from_post(post: frontmatter.Post) -> str:
    """Pull the raw transcript text from the session body.

    The session body is laid out as:
        <optional ALFRED:DYNAMIC block>
        # Transcript
        ...

    We want everything AFTER ``# Transcript`` (the structured summary
    block goes into the system prompt separately) so the LLM sees only
    the user's own words when deciding which notes to extract.
    """
    body = post.content
    idx = body.find("# Transcript")
    if idx == -1:
        return body
    return body[idx:].strip()


def _extract_summary_from_post(post: frontmatter.Post) -> str:
    """Pull the ``## Structured Summary`` block from the session body."""
    body = post.content
    start = body.find(capture_batch.SUMMARY_MARKER_START)
    if start == -1:
        return ""
    end = body.find(capture_batch.SUMMARY_MARKER_END, start)
    if end == -1:
        return body[start:]
    return body[start : end + len(capture_batch.SUMMARY_MARKER_END)]


def _note_body(
    body: str, source_quote: str, source_session_rel: str,
) -> str:
    """Compose the final note body with a source-quote blockquote + attribution."""
    body_clean = body.strip()
    quote_clean = source_quote.strip()
    attribution = f"_Source: [[{source_session_rel}]]_"

    parts = [body_clean]
    if quote_clean:
        parts.append("")
        parts.append(f"> {quote_clean}")
    parts.append("")
    parts.append(attribution)
    return "\n".join(parts) + "\n"


# --- Main extraction entry point -----------------------------------------


async def extract_notes_from_capture(
    client: Any,
    state: StateManager,
    vault_path: Path,
    short_id: str,
    model: str,
    max_notes: int = DEFAULT_MAX_NOTES,
    *,
    agent_slug: str = "salem",
) -> ExtractResult:
    """Extract up to ``max_notes`` standalone notes from a capture session.

    Idempotent: if the session record's ``derived_notes`` frontmatter
    already has entries, returns the existing list with
    ``skipped_reason="already_extracted"`` — caller renders the "delete
    first to re-run" message.

    ``agent_slug`` is the running instance's slug — forwarded to the
    implicit-chain :func:`capture_batch.write_summary_to_session_record`
    call so the attribution-audit entry carries the right agent. Default
    ``"salem"`` preserves legacy behaviour for tests that skip the plumb.

    Returns an :class:`ExtractResult`. Never raises; failure modes
    degrade to empty ``created_paths`` + a populated ``skipped_reason``.
    """
    session_rel = _find_session_by_short_id(state, short_id)
    if session_rel is None:
        log.info(
            "talker.extract.session_not_found",
            short_id=short_id,
        )
        return ExtractResult(created_paths=[], skipped_reason="no_session")

    post = _load_session_record(vault_path, session_rel)
    if post is None:
        return ExtractResult(created_paths=[], skipped_reason="no_record")

    existing = _derived_notes_from_post(post)
    if existing:
        log.info(
            "talker.extract.idempotent_skip",
            session_rel_path=session_rel,
            existing_count=len(existing),
        )
        return ExtractResult(
            created_paths=list(existing),
            skipped_reason="already_extracted",
        )

    # Implicit chain: if no structured summary is present, run the batch
    # pass first so the LLM extraction call has something to work with.
    summary_block = _extract_summary_from_post(post)
    if not summary_block:
        # Reconstruct a synthetic transcript from the body and run the
        # batch pass. We don't have the JSON transcript here — only the
        # rendered body — but the body is close enough for Sonnet to
        # structure.
        try:
            transcript = _synthetic_transcript_from_body(post.content)
            summary = await capture_batch.run_batch_structuring(
                client, transcript, model,
            )
            summary_md = capture_batch.render_summary_markdown(summary)
            await capture_batch.write_summary_to_session_record(
                vault_path, session_rel, summary_md, "true",
                agent_slug=agent_slug,
            )
            # Refresh the post so the summary is visible below.
            refreshed = _load_session_record(vault_path, session_rel)
            if refreshed is not None:
                post = refreshed
                summary_block = _extract_summary_from_post(post)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "talker.extract.implicit_structure_failed",
                session_rel_path=session_rel,
                error=str(exc),
            )
            # Keep going with whatever summary_block we have (possibly empty)
            # — Sonnet can still extract notes from the transcript alone.

    transcript_text = _extract_transcript_from_post(post)

    try:
        notes = await _call_extract_llm(
            client=client,
            model=model,
            transcript_text=transcript_text,
            summary_block=summary_block,
            max_notes=max_notes,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.extract.llm_failed",
            session_rel_path=session_rel,
            error=str(exc),
        )
        return ExtractResult(created_paths=[], skipped_reason=f"llm_error: {exc}")

    if not notes:
        log.info(
            "talker.extract.no_notes",
            session_rel_path=session_rel,
        )
        return ExtractResult(created_paths=[], skipped_reason="no_notes_emitted")

    # Cap defensively — the LLM should have obeyed, but trim anyway.
    notes = notes[:max_notes]

    created_paths: list[str] = []
    for note in notes:
        name = str(note.get("name") or "").strip()
        body = str(note.get("body") or "").strip()
        confidence_tier = str(note.get("confidence_tier") or "medium").strip()
        source_quote = str(note.get("source_quote") or "").strip()

        if not name or not body:
            continue

        full_body = _note_body(body, source_quote, session_rel)
        try:
            result = ops.vault_create(
                vault_path,
                "note",
                name,
                set_fields={
                    "created_by_capture": True,
                    "source_session": f"[[{session_rel}]]",
                    "confidence_tier": confidence_tier,
                },
                body=full_body,
            )
            created_paths.append(result["path"])
        except ops.VaultError as exc:
            log.info(
                "talker.extract.vault_create_failed",
                session_rel_path=session_rel,
                name=name,
                error=str(exc),
            )
            continue

    if created_paths:
        try:
            ops.vault_edit(
                vault_path,
                session_rel,
                set_fields={
                    "derived_notes": [f"[[{p}]]" for p in created_paths],
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "talker.extract.session_update_failed",
                session_rel_path=session_rel,
                error=str(exc),
            )

    log.info(
        "talker.extract.done",
        session_rel_path=session_rel,
        created=len(created_paths),
    )
    return ExtractResult(created_paths=created_paths)


def _synthetic_transcript_from_body(body: str) -> list[dict[str, Any]]:
    """Reconstruct a minimal transcript list from the session body text.

    Used only when implicit structuring has to run because the session
    record is missing the ``ALFRED:DYNAMIC`` summary block. We scan for
    ``**Andrew**`` lines and treat each as a user turn. Good enough for
    a follow-up structuring call — Sonnet doesn't need per-turn
    timestamps to produce a useful summary.
    """
    turns: list[dict[str, Any]] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("**Andrew**"):
            continue
        # Strip the speaker/timestamp prefix; the content is after the
        # first colon that follows the closing ``):``.
        idx = stripped.find("): ")
        if idx == -1:
            continue
        text = stripped[idx + 3 :].strip()
        if text:
            turns.append({"role": "user", "content": text})
    return turns


async def _call_extract_llm(
    client: Any,
    model: str,
    transcript_text: str,
    summary_block: str,
    max_notes: int,
) -> list[dict[str, Any]]:
    """Invoke Sonnet with the extraction prompt; return parsed note dicts.

    The tool_choice uses ``type: "auto"`` (not pinned to the tool) so
    the model can emit fewer tool calls than ``max_notes`` — or zero,
    if the session genuinely doesn't warrant a note. The loop below
    collects every ``create_note`` tool_use block from the response
    content and returns them.
    """
    user_content = (
        f"Session transcript:\n---\n{transcript_text or '(empty)'}\n---\n\n"
        f"Structured summary (pre-computed):\n---\n"
        f"{summary_block or '(none)'}\n---\n\n"
        f"Emit up to {max_notes} notes via the create_note tool. "
        "Fewer is fine. Zero is fine if nothing in this session warrants "
        "a standalone note."
    )

    response = await client.messages.create(**messages_create_kwargs(
        model=model,
        max_tokens=4096,
        temperature=0.3,
        system=[
            {
                "type": "text",
                "text": _EXTRACT_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_content}],
        tools=[_EXTRACT_TOOL],
        tool_choice={"type": "auto"},
    ))

    content = getattr(response, "content", None) or []
    notes: list[dict[str, Any]] = []
    for block in content:
        btype = getattr(block, "type", None) or (
            block.get("type") if isinstance(block, dict) else None
        )
        if btype != "tool_use":
            continue
        bname = getattr(block, "name", "") or (
            block.get("name") if isinstance(block, dict) else ""
        )
        if bname != "create_note":
            continue
        inp = getattr(block, "input", None) or (
            block.get("input") if isinstance(block, dict) else None
        )
        if isinstance(inp, dict):
            notes.append(inp)
    return notes


__all__ = [
    "DEFAULT_MAX_NOTES",
    "ExtractResult",
    "extract_notes_from_capture",
]
