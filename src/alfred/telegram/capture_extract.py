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
            # Phase 2 deliverable #2 (2026-05-17): anchor preservation.
            # When the operator dictated a positional anchor near the
            # claim ("p. 23 says..." / "at the 15-minute mark..." /
            # "in paragraph 3..."), emit the normalised anchor here.
            # Empty string when no anchor was dictated near this claim.
            #
            # Normalisation conventions (per the locked plan's
            # "Auto-maintenance behaviors" → item 4):
            #   * Book              → ``p.23``  (page; arabic numerals
            #                                    for body, roman for
            #                                    front matter — preserve
            #                                    exactly what operator
            #                                    said)
            #   * Article / Substack → ``¶3``    (paragraph mark + number;
            #                                    or ``§<n>`` for sections)
            #   * Podcast / video    → ``0:15:30`` (HH:MM:SS or MM:SS)
            #   * Lecture            → ``slide 12`` / ``min 23``
            #   * Conversation       → ``""`` (typically no anchor — leave
            #                                    empty)
            #
            # The anchor is preserved BOTH as ``source_anchor:`` frontmatter
            # on the spawned zettel AND as an inline body annotation at
            # the start of the body (e.g., ``(p.23)``). The frontmatter
            # is the queryable surface; the inline annotation is
            # human-readable in the rendered note.
            "source_anchor": {
                "type": "string",
                "description": (
                    "Optional positional anchor (page / timestamp / "
                    "paragraph / slide) from the source material near "
                    "this claim. Examples: ``p.23`` (book), ``¶3`` "
                    "(article), ``0:15:30`` (video/podcast), "
                    "``slide 12`` (lecture). Empty string when no "
                    "anchor was dictated near this claim. The anchor "
                    "is preserved on the spawned zettel as both "
                    "``source_anchor:`` frontmatter and an inline "
                    "``(<anchor>)`` body annotation."
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

ANCHOR PRESERVATION (Phase 2, 2026-05-17):

When the operator dictated a positional anchor near a claim — a page \
number, timestamp, paragraph mark, slide number — preserve it in the \
``source_anchor`` field. The anchor lets the operator (and Hypatia in \
future re-engagements) jump back to the exact source location later.

Anchor formats by source type:
- Book              → ``p.23`` (preserve arabic vs roman numerals as \
operator said them)
- Article / Substack → ``¶3`` (paragraph) or ``§2`` (section)
- Podcast / video    → ``0:15:30`` (HH:MM:SS) or ``15:30`` (MM:SS)
- Lecture            → ``slide 12`` or ``min 23``
- Conversation       → typically no anchor; leave ``source_anchor`` empty

Worked examples:

Transcript snippet 1: "Marcus on page 23 talks about how the dichotomy \
of control is foundational..."
  → Extract a note about "Dichotomy of Control as Foundation" with \
``source_anchor: "p.23"``. The body should mention the claim in \
operator's voice; do NOT inline the (p.23) annotation in the body \
text — the wrapping code adds it automatically.

Transcript snippet 2: "Around the fifteen-minute mark Hadot makes a \
really striking point about spiritual exercises..."
  → ``source_anchor: "0:15:00"`` (normalize "fifteen minutes" to \
``0:15:00``; if operator said "fifteen-thirty" or "15 minutes and 30 \
seconds" use ``0:15:30``).

Transcript snippet 3: "In paragraph three of the Substack post the \
author argues..."
  → ``source_anchor: "¶3"``.

Transcript snippet 4: "I was just rambling about my own thoughts on \
stoicism, no specific source location..."
  → ``source_anchor: ""`` (empty — operator wasn't anchoring to a \
specific source location).

When in doubt, leave ``source_anchor`` empty. False anchors are worse \
than missing anchors.
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


def _wikilink_from_fm(value: Any) -> str:
    """Coerce a frontmatter ``source`` / ``author`` field to a wikilink.

    Tolerates the three legal shapes:
        * Wikilink string ``"[[source/Meditations]]"`` → passed through.
        * Bare wikilink-target ``"source/Meditations"`` → wrapped.
        * Free-text (e.g. ``"Carlo Atendido"`` on legacy records) →
          ignored; returns ``""`` because a free-text author isn't
          a record reference and would create a broken wikilink.

    Empty / None → ``""``.
    """
    if not value:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if text.startswith("[[") and text.endswith("]]"):
        return text
    # Plausible record path: contains a slash AND looks like
    # ``<type>/<name>``. Bare strings without slashes are legacy free-
    # text (e.g. ``author: Carlo Atendido``) — leave them alone.
    if "/" in text and not text.startswith("http"):
        return f"[[{text}]]"
    return ""


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
    body: str,
    source_quote: str,
    source_session_rel: str,
    *,
    source_anchor: str = "",
) -> str:
    """Compose the final note body with a source-quote blockquote + attribution.

    Phase 2 deliverable #2 (2026-05-17): when ``source_anchor`` is
    non-empty, prepend an inline ``(<anchor>)`` annotation at the start
    of the body. This gives the operator a human-readable anchor right
    in the note's first line; the queryable ``source_anchor:``
    frontmatter is set separately by the caller.

    Examples of rendered output:
        source_anchor="p.23" → "(p.23) Marcus returns to the dichotomy
                                of control as foundational..."
        source_anchor=""     → "Marcus returns to the dichotomy of
                                control as foundational..."
    """
    body_clean = body.strip()
    quote_clean = source_quote.strip()
    anchor_clean = (source_anchor or "").strip()
    attribution = f"_Source: [[{source_session_rel}]]_"

    # Prepend the inline anchor annotation when present. Operator-facing
    # surface: anchor sits at the start of the body text, immediately
    # before the substantive content. The LLM is instructed NOT to
    # include the inline annotation in its body output (the system
    # prompt's ANCHOR PRESERVATION section says "do NOT inline the
    # (p.23) annotation in the body text — the wrapping code adds it
    # automatically").
    if anchor_clean:
        body_clean = f"({anchor_clean}) {body_clean}"

    parts = [body_clean]
    if quote_clean:
        parts.append("")
        parts.append(f"> {quote_clean}")
    parts.append("")
    parts.append(attribution)
    return "\n".join(parts) + "\n"


# --- Main extraction entry point -----------------------------------------


#: Operator override values for the capture-extract target. Set via
#: the ``/end-zettel`` / ``/end-note`` slash-command variants; null when
#: the operator uses plain ``/end`` (default discriminator runs).
#:
#: ``zettel`` forces zettel/ regardless of source-anchor state.
#: ``note`` forces note/ regardless of source-anchor state.
#: Anything else (including None / "" / unknown values) is treated
#: as "no override — use default discriminator".
_OPERATOR_OVERRIDE_VALUES: frozenset[str] = frozenset({"zettel", "note"})


def _resolve_extract_target_type(
    anchor_scope: str,
    *,
    source_anchored: bool,
    operator_override: str | None = None,
) -> str:
    """Return the vault_create record type for extracted records.

    Phase 1.x three-tier discriminator (2026-05-16 ratified rework after
    Andrew's "not all Hypatia notes are zettels" correction):

      * Salem (anchor_scope != "hypatia") → always ``note/``.
        Scope-gated: Salem doesn't carry the ``zettel`` create-allowlist
        entry, so even if a future bug surfaced an override on Salem,
        ops.vault_create would refuse. Returning ``note`` here is the
        contract-honest path.

      * Hypatia + operator override = "note" → forced ``note/``.
        Operator's explicit ``/end-note`` overrides any anchor state.

      * Hypatia + operator override = "zettel" → forced ``zettel/``.
        Operator's explicit ``/end-zettel`` overrides any anchor state.

      * Hypatia + no override → default discriminator:
          - source_anchored=True (session has ``source:`` or ``author:``
            wikilink in frontmatter) → ``zettel/``
          - source_anchored=False → ``note/``

    The memo branch (≤1 user message → ``memo/``) lives in
    ``capture_batch.process_capture_session`` and runs BEFORE the
    extractor — this discriminator only applies on the multi-message
    path. See the three-tier table in the brief:

    | Trigger                                  | Type      |
    |------------------------------------------|-----------|
    | ≤1 user message at /end                  | memo/     |
    | multi-msg + source-anchored OR /end-zettel | zettel/   |
    | multi-msg + (no anchor AND no /end-zettel) | note/    |
    """
    if anchor_scope != "hypatia":
        return "note"
    if operator_override in _OPERATOR_OVERRIDE_VALUES:
        return operator_override  # type: ignore[return-value]
    return "zettel" if source_anchored else "note"


#: Backwards-compat passthrough — preserved for any caller that hasn't
#: migrated to ``_resolve_extract_target_type`` yet. Routes through the
#: new discriminator with source_anchored=True (the prior shape's
#: behaviour for Hypatia) so the result matches the previous all-zettel
#: dispatch. New callers should use the discriminator directly.
def _extract_target_type(anchor_scope: str) -> str:
    """DEPRECATED: pre-Phase-1.x dispatch. Use
    :func:`_resolve_extract_target_type` instead.

    Returns ``zettel`` for Hypatia, ``note`` for everyone else —
    matching the original Phase 1 behaviour. Kept as a one-line
    passthrough so any external import doesn't break; production
    extraction calls go through ``_resolve_extract_target_type``
    via ``extract_notes_from_capture``.
    """
    return _resolve_extract_target_type(
        anchor_scope, source_anchored=True, operator_override=None,
    )


async def extract_notes_from_capture(
    client: Any,
    state: StateManager,
    vault_path: Path,
    short_id: str,
    model: str,
    max_notes: int = DEFAULT_MAX_NOTES,
    *,
    agent_slug: str = "salem",
    anchor_scope: str = "",
    operator_override: str | None = None,
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

    ``anchor_scope`` (Phase 1 Zettelkasten cutover, 2026-05-16) — when
    ``"hypatia"``, the three-tier discriminator in
    :func:`_resolve_extract_target_type` runs to pick the target type:
    ``zettel/`` for source-anchored sessions, ``note/`` for unanchored
    sessions, with operator-override support via ``/end-zettel`` /
    ``/end-note``. Salem (anchor_scope="" or anything non-Hypatia)
    always produces ``note/`` records (legacy operational behaviour).

    ``operator_override`` (Phase 1.x, 2026-05-16) — when non-None,
    bypasses the source-anchored default discriminator. Values:
    ``"zettel"`` (forces zettel/ even if no anchor), ``"note"`` (forces
    note/ even if anchored), ``None`` (use the source-anchored default).
    Caller reads this from the session record's
    ``capture_extract_target_override:`` frontmatter field (set by
    the ``/end-zettel`` / ``/end-note`` slash command variants at
    session close). Salem scope ignores the override — see
    :func:`_resolve_extract_target_type` for the full contract.

    ``source_anchored`` is inferred from the session record's
    frontmatter — True when either ``source:`` or ``author:`` carries
    a wikilink. Computed inline below; not a separate kwarg.

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

    # Capture-source-anchor (2026-05-16): read source/author wikilinks
    # off the session frontmatter so derived notes can carry them in
    # ``related``. The capture-batch orchestrator wrote these at
    # session-close time when the opening turn matched
    # ``I'm reading X by Y``.
    source_wikilink = _wikilink_from_fm(post.get("source"))
    author_wikilink = _wikilink_from_fm(post.get("author"))
    source_anchored = bool(source_wikilink) or bool(author_wikilink)

    # Phase 1.x discriminator support (2026-05-16): the session record's
    # ``capture_extract_target_override`` frontmatter field carries the
    # operator's ``/end-zettel`` / ``/end-note`` choice (or is absent if
    # the operator used plain ``/end``). The kwarg ``operator_override``
    # takes precedence when explicitly passed (test paths, future
    # programmatic re-extraction); otherwise we read from frontmatter
    # so an ``/extract`` invocation minutes/hours after the original
    # ``/end-zettel`` still honours the override.
    if operator_override is None:
        fm_override = str(
            post.get("capture_extract_target_override") or ""
        ).strip().lower()
        if fm_override in _OPERATOR_OVERRIDE_VALUES:
            operator_override = fm_override

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

    # Phase 1.x three-tier discriminator (2026-05-16): the target type
    # depends on (anchor_scope, source_anchored, operator_override).
    # Resolved once outside the loop so every record from this
    # extraction lands at the same type.
    #
    # Salem always note/. Hypatia: operator_override wins; otherwise
    # source-anchored → zettel, unanchored → note. The memo branch
    # (≤1 user message) lives in capture_batch and runs before this
    # extractor is invoked; this discriminator only fires on the
    # multi-message path.
    target_type = _resolve_extract_target_type(
        anchor_scope,
        source_anchored=source_anchored,
        operator_override=operator_override,
    )

    created_paths: list[str] = []
    created_titles: list[tuple[str, str]] = []  # (rel_path, original_title)
    for note in notes:
        name = str(note.get("name") or "").strip()
        body = str(note.get("body") or "").strip()
        confidence_tier = str(note.get("confidence_tier") or "medium").strip()
        source_quote = str(note.get("source_quote") or "").strip()
        # Phase 2 deliverable #2 (2026-05-17): anchor preservation.
        # ``source_anchor`` is optional in the tool schema; absent /
        # empty string means the operator didn't dictate a positional
        # reference near this claim. When present, it lands BOTH on
        # the zettel's frontmatter (queryable) AND inline in the body
        # (human-readable in the rendered note).
        source_anchor = str(note.get("source_anchor") or "").strip()

        if not name or not body:
            continue

        full_body = _note_body(
            body, source_quote, session_rel,
            source_anchor=source_anchor,
        )
        # Compose ``related`` with source + author wikilinks. Peer cross-
        # links are appended below once all notes in this session are
        # created (we need every peer's vault path first).
        related: list[str] = []
        if source_wikilink:
            related.append(source_wikilink)
        if author_wikilink:
            related.append(author_wikilink)

        set_fields: dict[str, Any] = {
            "created_by_capture": True,
            "source_session": f"[[{session_rel}]]",
            "confidence_tier": confidence_tier,
        }
        if related:
            set_fields["related"] = related
        # Phase 2: persist anchor as queryable frontmatter when present.
        # Empty anchors are omitted (no field at all) — Phase 1 lesson:
        # silent absence is fine when the absence is meaningful (no
        # anchor was dictated), but writing ``source_anchor: ""``
        # everywhere pollutes the frontmatter surface.
        if source_anchor:
            set_fields["source_anchor"] = source_anchor

        try:
            # ``scope`` kwarg routes the create through the per-instance
            # create-allowlist gate. Empty string ``""`` (Salem default)
            # passes through ``check_scope(None, ...)`` semantics — no
            # scope check fires, matching legacy behaviour. Hypatia
            # scope gates against ``HYPATIA_CREATE_TYPES`` which admits
            # ``zettel``.
            result = ops.vault_create(
                vault_path,
                target_type,
                name,
                set_fields=set_fields,
                body=full_body,
                scope=(anchor_scope or None),
            )
            created_paths.append(result["path"])
            created_titles.append((result["path"], name))
        except ops.VaultError as exc:
            log.info(
                "talker.extract.vault_create_failed",
                session_rel_path=session_rel,
                name=name,
                target_type=target_type,
                error=str(exc),
            )
            continue

    # Phase 2 deliverable #5 (2026-05-17): Permanent Notes spawned
    # auto-append. For each zettel just created with a source/-anchored
    # parent, idempotently append ``- [[zettel/<Title>]]`` to that
    # source's ``## Permanent Notes spawned`` body section.
    #
    # Gated by:
    #   * target_type == "zettel" (note/ records don't accrue to the
    #     Permanent Notes spawned list — that section's semantics are
    #     specifically zettel-only per the locked plan's "Permanent
    #     Notes spawned maintenance" rule).
    #   * source_wikilink non-empty (no source-anchor → no destination
    #     to append to).
    #
    # Failure-isolated: each per-zettel append wraps in try/except —
    # a missing source record or vault_edit failure on one zettel
    # logs + continues without aborting the extraction.
    if target_type == "zettel" and source_wikilink and created_paths:
        try:
            from . import capture_source_anchor as _csa
            # Strip wikilink brackets to get the source rel_path.
            for zettel_rel in created_paths:
                # Each zettel's wikilink for the source's Permanent
                # Notes spawned list — drop the .md suffix.
                zettel_no_md = (
                    zettel_rel[:-3] if zettel_rel.endswith(".md") else zettel_rel
                )
                zettel_wikilink = f"[[{zettel_no_md}]]"
                try:
                    # ``anchor_scope`` is guaranteed ``"hypatia"`` here:
                    # ``target_type == "zettel"`` only resolves when the
                    # discriminator at ``_resolve_extract_target_type``
                    # took the Hypatia branch, which requires
                    # ``anchor_scope == "hypatia"`` (Salem path returns
                    # ``"note"`` and never reaches this hook).
                    _csa.append_permanent_note_spawned(
                        vault_path=vault_path,
                        source_rel_path=source_wikilink,
                        zettel_wikilink=zettel_wikilink,
                        scope=anchor_scope,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.info(
                        "talker.extract.perm_notes_append_failed",
                        zettel_rel=zettel_rel,
                        source_wikilink=source_wikilink,
                        error=str(exc),
                    )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "talker.extract.perm_notes_append_unhandled",
                session_rel_path=session_rel,
                error=str(exc),
            )

    # Within-session cross-link pass. Computed AFTER all notes are
    # created so every peer has a real vault path to wikilink against.
    # Each note's ``related`` gets the peer wikilinks merged in
    # (preserving the source/author entries that vault_create already
    # wrote). When there are no qualifying peers, no edit fires —
    # matches the conservative-by-default heuristic.
    if len(created_titles) >= 2:
        try:
            # Local import to keep the capture_extract import surface tight.
            from . import capture_source_anchor as _csa

            cross_links = _csa.compute_peer_cross_links(created_titles)
            if cross_links:
                log.info(
                    "talker.extract.cross_links_computed",
                    session_rel_path=session_rel,
                    links=len(cross_links),
                )
                # Source/author wikilinks computed earlier; merge into
                # each note's related list with the peer links.
                anchor_links = [
                    link for link in (source_wikilink, author_wikilink) if link
                ]
                for rel_path, peer_links in cross_links.items():
                    merged = list(anchor_links) + peer_links
                    try:
                        ops.vault_edit(
                            vault_path,
                            rel_path,
                            set_fields={"related": merged},
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.info(
                            "talker.extract.cross_link_edit_failed",
                            note_path=rel_path,
                            error=str(exc),
                        )
            else:
                log.info(
                    "talker.extract.cross_links_none",
                    session_rel_path=session_rel,
                    notes=len(created_titles),
                )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "talker.extract.cross_links_failed",
                session_rel_path=session_rel,
                error=str(exc),
            )

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
        target_type=target_type,
        anchor_scope=anchor_scope,
        source_anchored=source_anchored,
        author_anchored=bool(author_wikilink),
        operator_override=operator_override or "",
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


# ---------------------------------------------------------------------------
# Mid-session recap (queue #10, 2026-05-18)
# ---------------------------------------------------------------------------
#
# Operator's ``/recap`` command on an OPEN capture session.
# Read-only summary; no record creation, no state mutation.
#
# Two modes:
#   * ``brief`` (default) — cheap call via
#     :func:`capture_batch.run_brief_recap_structuring` returning a 2-bucket
#     BriefRecap (topics + key_insights).
#   * ``verbose`` — same call as end-of-session structuring via
#     :func:`capture_batch.run_batch_structuring` returning a 6-bucket
#     StructuredSummary. No re-encounter scan (mid-session; the scan is a
#     post-close operation that consults the vault for prior records
#     anchored to the same source).
#
# Both modes render via :func:`capture_batch.render_recap_markdown` —
# plain markdown without the ``<!-- ALFRED:DYNAMIC -->`` markers since
# the output is a Telegram chat reply, not a vault-embedded summary.
#
# Failure-isolation: any LLM error returns a human-readable error
# string rather than raising. The caller (the bot's ``/recap``
# handler) renders the string directly to the operator.


RECAP_MODE_BRIEF: Final[str] = "brief"
RECAP_MODE_VERBOSE: Final[str] = "verbose"
_RECAP_VALID_MODES: frozenset[str] = frozenset(
    {RECAP_MODE_BRIEF, RECAP_MODE_VERBOSE}
)


def _empty_recap_markdown(mode: str) -> str:
    """Render the no-transcript-yet placeholder. Per
    ``feedback_intentionally_left_blank.md``: explicit "(nothing
    surfaced yet)" rather than silent empty output. Operator who fires
    ``/recap`` before they've said anything sees a clear signal.
    """
    label = "Recap (brief)" if mode == RECAP_MODE_BRIEF else "Recap (verbose)"
    return (
        f"## {label}\n\n"
        f"(no captures yet — say something and re-run /recap)\n"
    )


async def summarize_capture_session_so_far(
    client: Any,
    transcript: list[dict[str, Any]],
    model: str,
    *,
    mode: str = RECAP_MODE_BRIEF,
) -> str:
    """Produce a mid-session recap markdown string for an open capture
    session. Read-only — no vault writes, no state mutation.

    Args:
      client: Anthropic SDK client (or fake conforming to the
        ``client.messages.create`` shape).
      transcript: in-progress capture transcript — list of turns from
        the active session's ``transcript`` field.
      model: anthropic model identifier. Brief mode is cheap; verbose
        mode runs the full 6-bucket extraction.
      mode: ``"brief"`` (default) or ``"verbose"``. Any other value
        raises ``ValueError`` — the caller is expected to validate
        the operator's argument before invoking.

    Returns:
      Markdown-formatted recap string ready for Telegram reply. Empty
      transcript yields an explicit ``(no captures yet…)`` placeholder
      per the "intentionally left blank" discipline.

    Failure mode:
      LLM call failure (network, parse error, missing tool_use block)
      returns a human-readable error string. NEVER raises. The caller
      doesn't need a try/except wrapper — the operator sees an
      error message in chat, not a broken bot.
    """
    if mode not in _RECAP_VALID_MODES:
        raise ValueError(
            f"summarize_capture_session_so_far: mode must be 'brief' or "
            f"'verbose', got {mode!r}"
        )

    # Empty-transcript early exit — no point calling the LLM for an
    # empty session. Both modes use the same "(no captures yet)"
    # placeholder shape.
    if not transcript or all(
        not _turn_has_text(t) for t in transcript
    ):
        return _empty_recap_markdown(mode)

    # Local import keeps this module's surface tight + matches the
    # pattern already used elsewhere for capture_batch imports.
    from . import capture_batch as _cb

    try:
        if mode == RECAP_MODE_BRIEF:
            summary = await _cb.run_brief_recap_structuring(
                client, transcript, model,
            )
            return _cb.render_recap_markdown(summary, mode="brief")
        # verbose
        summary = await _cb.run_batch_structuring(
            client, transcript, model,
        )
        return _cb.render_recap_markdown(summary, mode="verbose")
    except Exception as exc:  # noqa: BLE001
        # Operator-facing error string — never crash the chat handler.
        log.warning(
            "talker.capture.recap_failed",
            mode=mode,
            error=str(exc),
        )
        return (
            f"## Recap ({mode})\n\n"
            f"_Recap failed: {exc}_\n\n"
            f"Try again or /end the session for a full summary.\n"
        )


def _turn_has_text(turn: dict[str, Any]) -> bool:
    """True when ``turn`` is a user role with non-empty text content.

    Mirrors the filter used by
    :func:`capture_batch._count_user_turns`: same role + same content-
    shape tolerance. Pulled out as a helper so the empty-transcript
    early-exit in ``summarize_capture_session_so_far`` checks the
    same predicate.
    """
    if turn.get("role") != "user":
        return False
    content = turn.get("content", "")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                btext = (block.get("text") or "").strip()
                if btext:
                    return True
        return False
    return bool(str(content).strip())


__all__ = [
    "DEFAULT_MAX_NOTES",
    "ExtractResult",
    "extract_notes_from_capture",
    # Phase 1 (legacy) per-scope dispatch — kept as a passthrough; new
    # callers should use ``_resolve_extract_target_type`` directly.
    "_extract_target_type",
    # Phase 1.x three-tier discriminator (2026-05-16).
    "_resolve_extract_target_type",
    "_OPERATOR_OVERRIDE_VALUES",
    # Mid-session /recap (queue #10, 2026-05-18).
    "summarize_capture_session_so_far",
    "RECAP_MODE_BRIEF",
    "RECAP_MODE_VERBOSE",
]
