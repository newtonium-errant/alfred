"""Talker session lifecycle — open, append, timeout, close.

A "session" here is a voice/text conversation between the user (via Telegram)
and Alfred via the Anthropic API. State lives in the :class:`StateManager`
(persisted JSON on disk); this module is pure logic.

Session records are written to the vault at close time via the ``talker`` scope.
Timeouts are checked on two axes: a periodic tick (``check_timeouts_with_meta``)
and a one-shot startup sweep (``resolve_on_startup``) that recovers sessions
orphaned across a daemon restart.

The transcript is a list of Anthropic-style message dicts — ``role`` is
``"user"`` or ``"assistant"``, and ``content`` is either a string or a list of
content blocks (for tool_use / tool_result turns). The body renderer compacts
tool blocks into one-line summaries so session records stay human-readable.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ._anthropic_compat import messages_create_kwargs
from .state import StateManager
from .utils import get_logger

log = get_logger(__name__)

# --- Substance-slug derivation tunables -------------------------------------
#
# Gate thresholds for "this session has enough content to be worth a
# substance-derived slug". Sessions below either threshold fall through
# to the opening-text slug — saves a useless LLM call on "are you awake?"
# and other one-shot pings.
_SUBSTANCE_MIN_TURNS = 3
_SUBSTANCE_MIN_CHARS = 150
# Hard cap on transcript text fed to the LLM. Keeps the call cheap and
# reliable even on long sessions; the derivation only needs the gist.
_SUBSTANCE_MAX_TRANSCRIPT_CHARS = 8000


# --- Session dataclass ---


@dataclass
class Session:
    """In-memory view of an active talker session.

    The canonical store is the JSON state file; this dataclass is a typed
    projection for callers that prefer attribute access. ``transcript`` holds
    Anthropic-format message dicts (``role`` + ``content``).

    Wk3 commit 8: ``opening_model`` records the model the session was
    *opened* on (via the router + calibration overrides). ``model`` may
    be flipped mid-session by ``/opus`` / ``/sonnet`` / implicit
    escalation; ``opening_model`` stays fixed. The diff between the two
    at close time is the "session escalated" signal the model-preference
    calibration threshold counts on.
    """

    session_id: str
    chat_id: int
    started_at: datetime
    last_message_at: datetime
    model: str
    transcript: list[dict[str, Any]] = field(default_factory=list)
    vault_ops: list[dict[str, str]] = field(default_factory=list)
    opening_model: str = ""
    # Outbound delivery failures attached to assistant turns. Each entry is a
    # dict with ``turn_index`` (0-based index into ``transcript``),
    # ``timestamp``, ``error``, ``length``, ``chunks_attempted``,
    # ``chunks_sent``, and ``delivered: false``. Populated by the bot's
    # outbound transport when ``sendMessage`` fails after chunking; surfaced
    # in the session-record frontmatter at close time so undelivered text is
    # never silently dropped. Empty by default — the field is omitted from
    # the frontmatter when no failures occurred.
    outbound_failures: list[dict[str, Any]] = field(default_factory=list)
    # Vision (image-message) attachments. Each entry is a dict with
    # ``path`` (vault-relative or absolute string), ``turn_index`` (0-based
    # into ``transcript``), ``timestamp``, ``bytes``, and the Telegram
    # ``file_unique_id`` for cross-reference. Populated by the bot's
    # photo handler when an image is downloaded + saved to inbox/.
    # Surfaced in the session-record frontmatter as ``images: [...]`` so
    # the distiller and any retroactive analysis can pull the saved
    # paths. Empty by default — field omitted from frontmatter when no
    # images attached.
    images: list[dict[str, Any]] = field(default_factory=list)

    # --- serialization ---

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "chat_id": self.chat_id,
            "started_at": self.started_at.isoformat(),
            "last_message_at": self.last_message_at.isoformat(),
            "model": self.model,
            "transcript": self.transcript,
            "vault_ops": self.vault_ops,
            "opening_model": self.opening_model or self.model,
            "outbound_failures": self.outbound_failures,
            "images": self.images,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        return cls(
            session_id=data["session_id"],
            chat_id=int(data["chat_id"]),
            started_at=_parse_iso(data["started_at"]),
            last_message_at=_parse_iso(data["last_message_at"]),
            model=data["model"],
            transcript=list(data.get("transcript") or []),
            vault_ops=list(data.get("vault_ops") or []),
            # Missing opening_model (wk2 records) → use current model as
            # the opening snapshot. Conservative: a rehydrated wk2
            # session was opened on its ``model`` so this is correct.
            opening_model=data.get("opening_model") or data.get("model", ""),
            outbound_failures=list(data.get("outbound_failures") or []),
            # Vision: missing on pre-vision rehydrated sessions. Defaults
            # to empty list so old state files load cleanly.
            images=list(data.get("images") or []),
        )


# --- Helpers ---


def _parse_iso(value: str) -> datetime:
    """Parse an ISO 8601 datetime, tolerating ``Z`` suffixes."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _persist(state: StateManager, session: Session) -> None:
    """Sync the session dataclass back into state and save.

    Preserves any stashed ``_*`` metadata (``_vault_path_root``,
    ``_session_type``, etc.) that the bot layer wrote onto the active
    dict — those fields are orthogonal to the :class:`Session` dataclass
    but the timeout / shutdown close paths depend on them. Without this
    merge, the first ``append_turn`` after ``_open_session_with_stash``
    would wipe them.
    """
    existing = state.get_active(session.chat_id) or {}
    merged = dict(existing)
    merged.update(session.to_dict())
    # Re-apply any stashed ``_*`` keys the dataclass doesn't know about.
    for key, value in existing.items():
        if key.startswith("_"):
            merged[key] = value
    state.set_active(session.chat_id, merged)
    state.save()


def _slug_from_dt(dt: datetime) -> str:
    """Produce ``YYYY-MM-DD HHMM`` slug used in the session record name."""
    return dt.strftime("%Y-%m-%d %H%M")


def _slug_from_date(dt: datetime) -> str:
    """Produce ``YYYY-MM-DD`` slug — used in Hypatia's mode-prefixed names."""
    return dt.strftime("%Y-%m-%d")


# Filename-safe slug: lowercase ASCII alphanumerics + dashes.
_TOPIC_SLUG_KEEP = re.compile(r"[^a-z0-9-]+")


def _slug_from_topic(text: str, *, max_words: int = 5) -> str:
    """Derive a filename-safe slug from arbitrary text.

    Used by Hypatia's mode-prefixed session names: takes the first
    ``max_words`` whitespace-delimited tokens of ``text`` (lowercased,
    non-alphanumerics dropped) and joins them with dashes. Empty input
    returns ``"untitled"`` so a session opened without any user text
    still produces a valid filename.
    """
    if not text:
        return "untitled"
    s = text.strip().lower()
    if not s:
        return "untitled"
    # First N whitespace-delimited tokens.
    tokens = s.split()[:max_words]
    joined = "-".join(tokens)
    # Drop everything that isn't a-z/0-9/-, then collapse runs and trim.
    joined = _TOPIC_SLUG_KEEP.sub("", joined)
    joined = re.sub(r"-{2,}", "-", joined).strip("-")
    return joined or "untitled"


def _first_user_text(transcript: list[dict[str, Any]]) -> str:
    """Extract the first user turn's text content, for slug derivation.

    Tolerates both string ``content`` and list-of-blocks ``content`` (the
    Anthropic SDK shape for tool turns). Returns empty string when no
    user turn is present or the first user turn has no text.
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


# --- Substance-slug derivation ------------------------------------------
#
# Phase 2 deferred-enhancement #1: derive a session-record filename slug
# from what the session was ABOUT, not the opening message. The opening
# message is a poor signal — Hypatia sessions in particular often start
# with a salutation ("Are you awake?") and only then get into the actual
# topic (Komal Gupta termination, VAC unit economics, etc.).
#
# The flow is post-close: ``close_session`` writes the record at the
# opening-text slug as today, then the async caller (bot.py / daemon
# shutdown / daemon timeout sweeper) opportunistically calls
# :func:`derive_slug_from_substance_async` and :func:`apply_substance_slug`
# to rename the file in place. Failure is isolated — the close already
# succeeded, the rename is best-effort.


_TRIVIAL_OPENERS = {
    # Common one-shot greetings / pings that shouldn't drive the slug.
    # The substance-extractor drops these from the head of the transcript
    # before measuring length / sending to the LLM. Lowercased + stripped
    # of trailing punctuation for the comparison.
    "hi", "hello", "hey", "yo", "sup",
    "are you there", "are you awake", "you up",
    "good morning", "good afternoon", "good evening",
    "morning", "afternoon", "evening",
    "ping", "test", "testing",
}


def _strip_trivial_opener(text: str) -> str:
    """Drop a trivial greeting from the head of a user turn.

    Comparison is case-insensitive against ``_TRIVIAL_OPENERS`` after
    stripping trailing punctuation. Returns the rest of the string when a
    trivial opener is matched, else the original text. Conservative — we
    only strip the *first line* and only when it matches exactly.
    """
    lines = text.strip().splitlines()
    if not lines:
        return text
    first = lines[0].strip().rstrip("?!.,;:")
    if first.lower() in _TRIVIAL_OPENERS:
        rest = "\n".join(lines[1:]).strip()
        return rest
    return text


def _extract_substance_text(transcript: list[dict[str, Any]]) -> str:
    """Concatenate user-turn substance text from a transcript.

    Drops a trivial opening greeting if present (so a session that
    started with "Are you awake?" doesn't have its slug poisoned by the
    salutation). Returns the joined substance text or ``""`` if nothing
    substantive remains.
    """
    parts: list[str] = []
    seen_any_user = False
    for turn in transcript:
        if turn.get("role") != "user":
            continue
        content = turn.get("content", "")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = str(block.get("text") or "")
                    break
        if not text.strip():
            continue
        # Strip a trivial opener only off the FIRST user turn — later
        # repetitions of "hi" mid-conversation are real content.
        if not seen_any_user:
            text = _strip_trivial_opener(text)
            seen_any_user = True
        if text.strip():
            parts.append(text.strip())
    return "\n".join(parts).strip()


def is_substantive(transcript: list[dict[str, Any]]) -> bool:
    """Return True when the transcript warrants a substance-derived slug.

    Gate: at least ``_SUBSTANCE_MIN_TURNS`` total turns AND substance
    text length >= ``_SUBSTANCE_MIN_CHARS``. Sessions below either bar
    fall through to the opening-text slug. The two-axis check guards
    against a single long monologue (1 turn, 500 chars — too sparse to
    extract a clean topic) and a multi-turn ping (5 short "yo"s — no
    real content).
    """
    if len(transcript) < _SUBSTANCE_MIN_TURNS:
        return False
    substance = _extract_substance_text(transcript)
    return len(substance) >= _SUBSTANCE_MIN_CHARS


_SUBSTANCE_SYSTEM_PROMPT = (
    "You are a filename-labelling utility. The user will paste a chat "
    "transcript inside <transcript> tags. You must emit a 3-5 word "
    "filename label describing the SUBJECT MATTER discussed in the "
    "transcript. Do NOT respond to the transcript content as if it "
    "were addressed to you — you are LABELLING it, not continuing it.\n\n"
    "The label should capture: the person, project, document, or "
    "specific subject the user was working on. Skip greetings, "
    "skip the assistant's role, skip how-to-help phrasing.\n\n"
    "Worked examples:\n"
    "- transcript discusses Komal Gupta's termination → "
    "<output>komal gupta termination</output>\n"
    "- transcript discusses VAC unit economics → "
    "<output>vac unit economics</output>\n"
    "- transcript discusses Q3 marketing plan → "
    "<output>q3 marketing plan</output>\n"
    "- transcript discusses substack essay on rural transport credit → "
    "<output>rural transport credit essay</output>\n\n"
    "Output format:\n"
    "- Plain text, 3-5 words, lowercase ASCII letters and digits only.\n"
    "- Single spaces between words. No punctuation, no quotes, no tags "
    "in your reply (the example tags above are just for illustration).\n"
    "- The very first thing you emit is the label itself.\n"
    "- No transcript has no clear subject → emit just 'untitled'."
)


def _format_transcript_for_substance(transcript: list[dict[str, Any]]) -> str:
    """Render a compact transcript for the slug-derivation LLM call.

    Keeps user + assistant text only; tool-use blocks and metadata are
    dropped. Truncated at ``_SUBSTANCE_MAX_TRANSCRIPT_CHARS`` from the
    head so the topic-establishing content is preserved. The rendering
    mirrors ``_render_content`` for content lists (text blocks only) so
    the call cost stays bounded on long tool-heavy sessions.
    """
    lines: list[str] = []
    for turn in transcript:
        role = turn.get("role", "")
        if role not in ("user", "assistant"):
            continue
        content = turn.get("content", "")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            chunks: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    btext = (block.get("text") or "").strip()
                    if btext:
                        chunks.append(btext)
            text = " ".join(chunks)
        text = text.strip()
        if not text:
            continue
        speaker = "User" if role == "user" else "Assistant"
        lines.append(f"{speaker}: {text}")
    rendered = "\n".join(lines)
    if len(rendered) > _SUBSTANCE_MAX_TRANSCRIPT_CHARS:
        rendered = rendered[:_SUBSTANCE_MAX_TRANSCRIPT_CHARS]
    return rendered


def _normalize_substance_slug(text: str, *, max_words: int = 5) -> str:
    """Clean an LLM-emitted slug into a filename-safe form.

    Applies the same character filter / word-cap as
    :func:`_slug_from_topic` so a malformed model response (extra
    quoting, punctuation, line breaks) can't produce a path that
    fails ``vault_move``. Returns ``""`` when the cleaned slug is empty
    so the caller can fall through to the opening-text path.
    """
    if not text:
        return ""
    # First non-empty line — guard against the model adding an explanation.
    first_line = ""
    for line in text.splitlines():
        if line.strip():
            first_line = line.strip()
            break
    if not first_line:
        return ""
    slug = _slug_from_topic(first_line, max_words=max_words)
    if slug == "untitled":
        return ""
    return slug


async def derive_slug_from_substance_async(
    client: Any,
    model: str,
    transcript: list[dict[str, Any]],
    *,
    max_tokens: int = 60,
) -> str:
    """Call Anthropic to extract a 3-5 word topic slug from ``transcript``.

    Returns the cleaned slug on success, or ``""`` on any failure (LLM
    error, empty/malformed response, gate-fail). Failure is silent —
    the caller treats ``""`` as "fall through to opening-text slug".

    ``client`` is an ``anthropic.AsyncAnthropic`` instance (or any
    object exposing ``messages.create``). ``model`` follows the talker's
    Anthropic config; the slug-derivation call uses the same model
    family so the temperature-quirk shim stays consistent across sites.
    """
    if not is_substantive(transcript):
        return ""
    rendered = _format_transcript_for_substance(transcript)
    if not rendered:
        return ""
    # Wrap the transcript in tags so the model treats it as data to
    # label, not as a conversation to continue. Without this framing,
    # Opus tends to "respond" to the transcript content instead of
    # emitting a label.
    user_msg = (
        "Label the following transcript with a 3-5 word filename slug. "
        "Emit ONLY the label, nothing else.\n\n"
        f"<transcript>\n{rendered}\n</transcript>"
    )
    try:
        response = await client.messages.create(**messages_create_kwargs(
            model=model,
            max_tokens=max_tokens,
            temperature=0.0,
            system=[
                {
                    "type": "text",
                    "text": _SUBSTANCE_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_msg}],
        ))
    except Exception as exc:  # noqa: BLE001 — LLM errors must not break close
        log.warning(
            "talker.session.substance_slug_failed",
            stage="llm_call",
            error=str(exc),
        )
        return ""

    raw = ""
    content = getattr(response, "content", None) or []
    for block in content:
        btype = getattr(block, "type", None) or (
            block.get("type") if isinstance(block, dict) else None
        )
        if btype != "text":
            continue
        bt = getattr(block, "text", None)
        if bt is None and isinstance(block, dict):
            bt = block.get("text")
        if bt:
            raw = str(bt)
            break

    slug = _normalize_substance_slug(raw)
    if not slug:
        log.warning(
            "talker.session.substance_slug_failed",
            stage="parse",
            raw=raw[:120],
        )
    return slug


def apply_substance_slug(
    state: StateManager,
    vault_path_root: str,
    rel_path: str,
    new_slug: str,
    session_id: str,
    *,
    short_id_suffix: str | None = None,
) -> str:
    """Rename a just-closed session record to use ``new_slug``.

    ``rel_path`` is the record path returned by :func:`close_session`
    (e.g. ``session/conversation-2026-04-27-are-you-awake-73fe87fa.md``).
    ``new_slug`` replaces the slug portion between the date and the
    short-id suffix, producing
    ``session/conversation-2026-04-27-<new_slug>-73fe87fa.md``. The
    file's frontmatter ``name`` field is rewritten to match.

    Updates the matching ``closed_sessions`` state entry's
    ``record_path`` so downstream consumers (Daily Sync surfacing,
    distiller backlog) follow the new path.

    Returns the new ``rel_path`` on success. On any failure the
    original path is preserved, a warning is logged, and the original
    ``rel_path`` is returned.
    """
    if not new_slug:
        return rel_path

    try:
        import frontmatter  # local import — heavy + only needed on this branch
    except ImportError:
        log.warning(
            "talker.session.substance_slug_failed",
            stage="frontmatter_import",
            rel_path=rel_path,
        )
        return rel_path

    # Parse the existing rel_path: ``session/<mode>-<date>-<old_slug>-<short>.md``.
    # The mode + date prefix and the short-id suffix are preserved; only
    # the middle slug segment is replaced. Legacy "Voice Session — ..."
    # paths don't follow this shape and are skipped (they don't get the
    # new slug — backward compat trumps consistency).
    name = Path(rel_path).name
    if not name.endswith(".md"):
        return rel_path
    stem = name[:-3]
    parts = stem.split("-")
    # Minimum viable shape: <mode>-YYYY-MM-DD-<at-least-one-slug-token>-<short>.
    # That's at least 6 dash-separated tokens (mode, year, month, day,
    # slug, short) — anything shorter doesn't match the per-instance
    # filename pattern and we leave it alone.
    if len(parts) < 6:
        return rel_path
    mode_token = parts[0]
    date_tokens = parts[1:4]
    short_token = short_id_suffix or parts[-1]
    # Sanity: the date tokens should be all-digit YYYY/MM/DD.
    if not all(p.isdigit() for p in date_tokens):
        return rel_path

    new_stem = f"{mode_token}-{'-'.join(date_tokens)}-{new_slug}-{short_token}"
    new_name = f"{new_stem}.md"
    new_rel_path = f"{Path(rel_path).parent.as_posix()}/{new_name}"

    if new_rel_path == rel_path:
        return rel_path

    src = Path(vault_path_root) / rel_path
    dst = Path(vault_path_root) / new_rel_path
    if not src.exists():
        log.warning(
            "talker.session.substance_slug_failed",
            stage="src_missing",
            rel_path=rel_path,
        )
        return rel_path
    if dst.exists():
        # Collision — extremely unlikely (would require a second session
        # with the same date + short id + derived slug) but be defensive.
        log.warning(
            "talker.session.substance_slug_failed",
            stage="dst_exists",
            rel_path=rel_path,
            new_rel_path=new_rel_path,
        )
        return rel_path

    # Update frontmatter ``name`` so the display name in the record
    # matches the renamed file. We rewrite the source file in place
    # BEFORE rename so the disk is consistent if the rename fails.
    try:
        post = frontmatter.load(str(src))
        existing_name = str(post.metadata.get("name") or "")
        # Only touch the slug portion of the display name. Display name
        # shape: ``<Mode> — <date> <slug>`` per
        # ``_build_session_frontmatter``. Splitting on the last
        # dash-bound separator keeps the prefix intact.
        new_display = ""
        if existing_name:
            sep = " — "
            if sep in existing_name:
                head, tail = existing_name.split(sep, 1)
                # tail = "<date> <old-slug>" — replace everything after
                # the first space (the date) with the new slug.
                tail_parts = tail.split(" ", 1)
                if len(tail_parts) == 2 and tail_parts[0]:
                    new_display = f"{head}{sep}{tail_parts[0]} {new_slug}"
        if new_display:
            post.metadata["name"] = new_display
        post.metadata["substance_slug_derived"] = True
        src.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "talker.session.substance_slug_failed",
            stage="frontmatter_rewrite",
            rel_path=rel_path,
            error=str(exc),
        )
        return rel_path

    # Atomic on the same filesystem (``Path.rename``).
    try:
        src.rename(dst)
    except OSError as exc:
        log.warning(
            "talker.session.substance_slug_failed",
            stage="rename",
            rel_path=rel_path,
            new_rel_path=new_rel_path,
            error=str(exc),
        )
        return rel_path

    # Update the closed_sessions state entry so surfacing tools follow
    # the new path. Match on session_id (uuid) — chat_id is not unique
    # across closes, but session_id is.
    try:
        for entry in state.state.get("closed_sessions", []) or []:
            if entry.get("session_id") == session_id:
                entry["record_path"] = new_rel_path
                # Carry the substance flag through so downstream
                # consumers can tell when a slug was substance-derived
                # without re-parsing the filename.
                entry["substance_slug_derived"] = True
                break
        state.save()
    except Exception:  # noqa: BLE001 — file rename already committed
        log.warning(
            "talker.session.substance_slug_state_update_failed",
            rel_path=rel_path,
            new_rel_path=new_rel_path,
        )

    log.info(
        "talker.session.substance_slug_applied",
        rel_path=rel_path,
        new_rel_path=new_rel_path,
        slug=new_slug,
    )
    return new_rel_path


def _snapshot_for_post_close(active: dict[str, Any]) -> dict[str, Any]:
    """Snapshot the fields the post-close hook needs BEFORE close pops the dict.

    :func:`close_session` pops the active-session dict from state, so the
    three call sites that run a post-close hook (bot ``/end`` handler,
    daemon shutdown sweep, daemon timeout sweeper) must copy out the
    fields the hook reads before invoking close. This helper is the one
    place that contract is encoded — adding a new field to
    :func:`maybe_apply_substance_slug` becomes a one-line change here
    instead of three.

    Returns a dict with ``transcript`` (list copy, never None),
    ``session_id`` (string, may be empty), and ``vault_path_root``
    (string, may be empty — caller resolves the fallback). The caller
    threads these directly into :func:`maybe_apply_substance_slug`.
    """
    return {
        "transcript": list(active.get("transcript") or []),
        "session_id": active.get("session_id", ""),
        "vault_path_root": active.get("_vault_path_root", ""),
    }


async def maybe_apply_substance_slug(
    state: StateManager,
    *,
    enabled: bool,
    client: Any,
    model: str,
    vault_path_root: str,
    rel_path: str,
    transcript: list[dict[str, Any]],
    session_id: str,
) -> str:
    """Optionally derive + apply a substance-derived slug to a closed session.

    Single entry point for callers (bot.py, daemon shutdown sweep,
    daemon timeout sweeper). Does nothing and returns ``rel_path``
    unchanged when:

    - ``enabled`` is False (config knob off).
    - ``client`` is None (e.g. legacy callers without an Anthropic client).
    - ``transcript`` doesn't pass :func:`is_substantive`.
    - The LLM call fails or returns an unparseable slug.
    - The path doesn't match the per-instance filename pattern (legacy
      ``Voice Session — ...`` records pass through unchanged).

    Returns the (possibly new) ``rel_path``. Failure paths are logged
    via :func:`apply_substance_slug` / :func:`derive_slug_from_substance_async`.
    """
    if not enabled or client is None:
        return rel_path
    slug = await derive_slug_from_substance_async(client, model, transcript)
    if not slug:
        return rel_path
    return apply_substance_slug(
        state,
        vault_path_root=vault_path_root,
        rel_path=rel_path,
        new_slug=slug,
        session_id=session_id,
    )


# --- Per-instance mode registry -----------------------------------------
#
# Each instance picks a session "mode" at close time. The mode becomes the
# filename prefix (``<mode>-<date>-<slug>-<id>.md``) and, for Hypatia, also
# lands as ``mode:`` in the frontmatter. The registry below is the single
# source of truth for which prefixes each instance is allowed to emit;
# extending an instance's mode set is a one-line change here.
#
# Order matters: the FIRST entry in each list is the instance's default
# fallback when mode-resolution can't infer anything specific.
INSTANCE_MODE_PREFIXES: dict[str, list[str]] = {
    "talker": ["voice", "conversation", "capture"],   # Salem
    "hypatia": ["conversation", "capture"],
    "kalle": ["coding", "review"],
}


def _has_voice_user_turn(transcript: list[dict[str, Any]]) -> bool:
    """True if any user turn was sent as voice (``_kind="voice"``).

    Salem stamps ``_kind`` on every user turn at append time. A session
    that received at least one voice message — even if the rest were
    typed — is classified as a ``voice`` session. The voice/text counts
    in ``_count_message_kinds`` use the same field, so this stays in
    sync with the telemetry summary.
    """
    for turn in transcript:
        if turn.get("role") != "user":
            continue
        if turn.get("_kind") == "voice":
            return True
    return False


def _kalle_invoked_reviews(transcript: list[dict[str, Any]]) -> bool:
    """True if any ``bash_exec`` tool call ran ``alfred reviews ...``.

    KAL-LE drives the ``alfred reviews`` CLI through the ``bash_exec``
    tool surface. Detection is a substring scan across all tool_use
    blocks: any block named ``bash_exec`` whose ``input.command`` starts
    with the ``alfred reviews`` prefix flips the session into ``review``
    mode. False positives (e.g. a code block discussing the command in
    plain text) are ignored — only structured tool_use blocks count.
    """
    for turn in transcript:
        content = turn.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue
            if block.get("name") != "bash_exec":
                continue
            inp = block.get("input") or {}
            cmd = (inp.get("command") or "").strip().lower()
            # Tolerate leading whitespace and the ``alfred reviews``
            # subcommand variants (``write``, ``list``, ``read``,
            # ``mark-addressed``) — substring match on the command head.
            if cmd.startswith("alfred reviews"):
                return True
    return False


def _resolve_mode_for_instance(
    tool_set: str,
    session: Session,
    session_type: str | None,
) -> str:
    """Pick the session ``mode`` for the given instance + transcript.

    Branches on ``tool_set`` to apply per-instance auto-detection:

    - **Salem** (``"talker"``): ``capture`` if the bot stashed
      ``session_type="capture"`` (the ``/capture`` opener), else
      ``voice`` if any user turn was sent as voice, else
      ``conversation``.
    - **Hypatia** (``"hypatia"``): ``capture`` if ``session_type=="capture"``,
      else ``conversation``. Same shape as wk2 ``_mode_from_session_type``
      to keep existing Hypatia behaviour unchanged.
    - **KAL-LE** (``"kalle"``): ``review`` if any ``bash_exec`` tool
      call ran ``alfred reviews ...``, else ``coding``.
    - Unknown / empty ``tool_set``: return ``""``. The caller's
      filename builder maps this to the wk1 ``Voice Session — ...``
      shape so legacy code paths (callers not threaded with
      ``tool_set``) keep working.

    Ambiguous cases default to the instance's first-listed prefix —
    e.g. a Salem session with no voice turns and no ``/capture`` becomes
    ``conversation`` (not ``voice``); detection has to *prove* the more
    specific mode.
    """
    prefixes = INSTANCE_MODE_PREFIXES.get(tool_set)
    if prefixes is None:
        return ""

    st = (session_type or "").lower()

    if tool_set == "talker":
        if st == "capture":
            return "capture"
        if _has_voice_user_turn(session.transcript):
            return "voice"
        return "conversation"

    if tool_set == "hypatia":
        if st == "capture":
            return "capture"
        return "conversation"

    if tool_set == "kalle":
        if _kalle_invoked_reviews(session.transcript):
            return "review"
        return "coding"

    # Registered tool_set without a dedicated branch — fall back to the
    # first-listed prefix so adding a new instance to the registry
    # always produces a well-formed filename even before its
    # detector is wired.
    return prefixes[0]


def _build_record_name(
    session: Session,
    *,
    tool_set: str,
    mode: str,
) -> str:
    """Pick the session-record filename per the instance's tool_set.

    All instances registered in :data:`INSTANCE_MODE_PREFIXES` use the
    mode-prefixed pattern ``<mode>-<YYYY-MM-DD>-<slug>-<short-id>``
    (per ``vault-hypatia/SKILL.md`` and ``~/library-alexandria/CLAUDE.md``,
    now generalized as the project-wide convention). ``slug`` is derived
    from the first user turn (first 5 words). The short id keeps same-day
    sessions on the same opening cue from colliding on ``vault_create``.

    Unknown / empty ``tool_set`` falls back to the wk1
    ``Voice Session — <date> <time> <short-id>`` filename so legacy
    callers (any code path not yet threaded with ``tool_set``) and
    pre-existing vault records stay readable. Existing legacy session
    files are NEVER renamed — backward compat is load-bearing.
    """
    short_id = session.session_id.split("-")[0]
    if tool_set in INSTANCE_MODE_PREFIXES:
        slug = _slug_from_topic(_first_user_text(session.transcript))
        return f"{mode}-{_slug_from_date(session.started_at)}-{slug}-{short_id}"
    return f"Voice Session — {_slug_from_dt(session.started_at)} {short_id}"


def _mode_from_session_type(session_type: str | None) -> str:
    """Map Salem-side ``session_type`` to Hypatia's ``mode`` field.

    Capture-mode sessions (``session_type="capture"``) become
    ``mode: capture`` with ``processed: false`` so the "Unprocessed
    captures" Bases view can read the queue. Everything else collapses
    to ``conversation``. Retained as a thin shim around the
    Hypatia-specific branch of :func:`_resolve_mode_for_instance` for
    callers that only have a session_type string in hand (no transcript).
    """
    if (session_type or "").lower() == "capture":
        return "capture"
    return "conversation"


# --- Public API ---


def open_session(
    state: StateManager,
    chat_id: int,
    model: str,
) -> Session:
    """Create and persist a new active session for ``chat_id``.

    Overwrites any existing active session for that chat — callers should close
    the prior session first if they need to preserve it.
    """
    now = _now_utc()
    session = Session(
        session_id=str(uuid.uuid4()),
        chat_id=int(chat_id),
        started_at=now,
        last_message_at=now,
        model=model,
        opening_model=model,
    )
    _persist(state, session)
    log.info(
        "talker.session.opened",
        chat_id=chat_id,
        session_id=session.session_id,
        model=model,
    )
    return session


def append_turn(
    state: StateManager,
    session: Session,
    role: str,
    content: str | list[dict[str, Any]],
    kind: str = "text",
) -> None:
    """Append an Anthropic-format turn to the transcript and persist.

    ``content`` follows the SDK's shape: either a plain string (simple text
    turn) or a list of content blocks (tool_use / tool_result turns).

    wk2 commit 5:
    - Always stamp ``_ts`` (ISO 8601) on the turn so ``_build_session_body``
      renders real per-turn timestamps. Wk1 relied on the session start time
      for every turn, which made long sessions look like they happened in
      one minute.
    - ``kind`` (``"text"`` or ``"voice"``) is stamped as ``_kind`` on user
      turns. Assistant / tool turns always carry ``_kind="text"`` — they
      don't have an input modality. The voice/text counters in
      ``_count_message_kinds`` read this field at close time.
    """
    now = _now_utc()
    turn: dict[str, Any] = {
        "role": role,
        "content": content,
        "_ts": now.isoformat(),
    }
    if role == "user":
        turn["_kind"] = kind
    session.transcript.append(turn)
    session.last_message_at = now
    _persist(state, session)


def append_vault_op(
    state: StateManager,
    session: Session,
    op: str,
    path: str,
) -> None:
    """Record a vault mutation onto the session and persist.

    Feeds the ``outputs`` field in the eventual session-record frontmatter.
    """
    session.vault_ops.append({
        "op": op,
        "path": path,
        "ts": _now_utc().isoformat(),
    })
    _persist(state, session)


def append_outbound_failure(
    state: StateManager,
    session: Session,
    *,
    turn_index: int,
    error: str,
    length: int,
    chunks_attempted: int,
    chunks_sent: int,
) -> None:
    """Record an outbound Telegram delivery failure on the session and persist.

    Each call appends one entry to ``session.outbound_failures``. ``turn_index``
    points at the assistant turn in ``session.transcript`` whose text failed to
    deliver — ``len(session.transcript) - 1`` at the moment the bot returns
    from ``run_turn``. ``chunks_attempted`` / ``chunks_sent`` lets a future
    surfacing tool (Daily Sync) tell whether the failure was the first chunk
    or a partial-delivery mid-stream.

    Surfaced in the session-record frontmatter at close time as
    ``outbound_failures``; the field is omitted entirely when this list is
    empty.
    """
    session.outbound_failures.append({
        "turn_index": int(turn_index),
        "timestamp": _now_utc().isoformat(),
        "error": str(error),
        "length": int(length),
        "chunks_attempted": int(chunks_attempted),
        "chunks_sent": int(chunks_sent),
        "delivered": False,
    })
    _persist(state, session)


def append_image(
    state: StateManager,
    session: Session,
    *,
    path: str,
    file_unique_id: str,
    bytes_size: int,
) -> None:
    """Record a saved-image attachment on the session and persist.

    Called by the bot's photo handler after the image has been
    downloaded, saved to ``<vault>/inbox/`` and converted to a content
    block. ``turn_index`` points at the user turn the image arrived
    on — ``len(session.transcript)`` at the moment of call (the user
    turn is appended *next* by ``run_turn`` / ``append_turn``, so the
    index is the would-be position).

    Surfaced in the session-record frontmatter as ``images: [...]`` at
    close time. Field omitted entirely when empty so wk1 / pre-vision
    record consumers see no shape change.
    """
    session.images.append({
        "path": str(path),
        "file_unique_id": str(file_unique_id),
        "bytes": int(bytes_size),
        "turn_index": len(session.transcript),
        "timestamp": _now_utc().isoformat(),
    })
    _persist(state, session)


def resolve_on_startup(
    state: StateManager,
    now: datetime,
    gap_seconds: int,
) -> list[str]:
    """Sweep active sessions at daemon boot; close any that have timed out.

    Returns the list of vault paths written for closed sessions.

    Active sessions that have NOT exceeded the gap are left in place — the
    next user message reuses them.
    """
    # Substance-slug rename intentionally not applied here — runs before
    # the Anthropic client is constructed, so the substance-derivation LLM
    # call can't fire. Sessions orphaned across daemon restart keep their
    # opening-text slug. See project_hypatia_phase2_followups.md Phase 2.x #1.
    closed_paths: list[str] = []
    active = dict(state.state.get("active_sessions", {}))
    for chat_id_str, raw in active.items():
        try:
            last = _parse_iso(raw.get("last_message_at", ""))
        except (ValueError, TypeError):
            log.warning(
                "talker.session.invalid_last_message",
                chat_id=chat_id_str,
            )
            continue
        if (now - last).total_seconds() < gap_seconds:
            continue

        vault_path_root = raw.get("_vault_path_root", "")
        # Caller didn't stash vault path — skip gracefully; daemon will
        # retry via check_timeouts_with_meta once it has a config handle.
        if not vault_path_root:
            log.info(
                "talker.session.timeout_deferred",
                chat_id=chat_id_str,
                reason="no_vault_path_on_restart",
            )
            continue
        try:
            path = close_session(
                state,
                vault_path_root=vault_path_root,
                chat_id=int(chat_id_str),
                reason="timeout_on_restart",
                user_vault_path=raw.get("_user_vault_path"),
                stt_model_used=raw.get("_stt_model_used", ""),
                session_type=raw.get("_session_type", "note"),
                continues_from=raw.get("_continues_from"),
                pushback_level=raw.get("_pushback_level"),
                tool_set=raw.get("_tool_set", ""),
            )
            closed_paths.append(path)
        except Exception as exc:  # noqa: BLE001 — log and continue sweep
            log.warning(
                "talker.session.close_failed",
                chat_id=chat_id_str,
                error=str(exc),
            )
    return closed_paths


def check_timeouts_with_meta(
    state: StateManager,
    now: datetime,
    gap_seconds: int,
) -> list[dict[str, Any]]:
    """Periodic tick: close any sessions that have exceeded the gap.

    Returns one dict per just-closed session with ``chat_id``,
    ``session_id``, ``rel_path``, ``transcript``, and ``vault_path_root``
    — enough for a post-close hook (e.g. Phase 2 substance-slug rename)
    to re-derive paths and rename without re-reading state. Used by the
    daemon's async sweeper which has the Anthropic client in scope.

    Relies on the daemon having stashed vault-path metadata onto each
    active session dict when it was created; sessions without that
    metadata are skipped silently.
    """
    closed_meta: list[dict[str, Any]] = []
    active = dict(state.state.get("active_sessions", {}))
    for chat_id_str, raw in active.items():
        try:
            last = _parse_iso(raw.get("last_message_at", ""))
        except (ValueError, TypeError):
            continue
        if (now - last).total_seconds() < gap_seconds:
            continue
        vault_path_root = raw.get("_vault_path_root", "")
        if not vault_path_root:
            continue
        # Snapshot transcript + session_id BEFORE close_session pops
        # the active dict, so the post-close hook can run substance-slug
        # derivation without re-reading state.
        snap = _snapshot_for_post_close(raw)
        transcript_snap = snap["transcript"]
        session_id_snap = snap["session_id"]
        try:
            path = close_session(
                state,
                vault_path_root=vault_path_root,
                chat_id=int(chat_id_str),
                reason="timeout",
                user_vault_path=raw.get("_user_vault_path"),
                stt_model_used=raw.get("_stt_model_used", ""),
                session_type=raw.get("_session_type", "note"),
                continues_from=raw.get("_continues_from"),
                pushback_level=raw.get("_pushback_level"),
                tool_set=raw.get("_tool_set", ""),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "talker.session.timeout_close_failed",
                chat_id=chat_id_str,
                error=str(exc),
            )
            continue
        closed_meta.append({
            "chat_id": int(chat_id_str),
            "session_id": session_id_snap,
            "rel_path": path,
            "transcript": transcript_snap,
            "vault_path_root": vault_path_root,
        })
    return closed_meta


def close_session(
    state: StateManager,
    vault_path_root: str,
    chat_id: int,
    reason: str,
    user_vault_path: str | None,
    stt_model_used: str,
    session_type: str = "note",
    continues_from: str | None = None,
    pushback_level: int | None = None,
    tool_set: str = "",
) -> str:
    """Close the active session for ``chat_id`` and write a ``session/`` record.

    Removes the session from ``active_sessions``, appends a summary to
    ``closed_sessions``, and returns the vault-relative path of the new record.

    ``session_type`` / ``continues_from`` default to ``"note"`` / ``None`` so
    the timeout / shutdown close paths can fall back to the wk1 behaviour when
    the active dict was written before wk2 (``get("_session_type", "note")``).

    ``tool_set`` selects per-instance session-save shape (filename pattern +
    frontmatter fields). All registered tool_sets emit the mode-prefixed
    ``<mode>-<date>-<slug>-<short-id>`` filename; unknown / empty
    ``tool_set`` falls back to the wk1 ``Voice Session — <date> <time> <id>``
    filename. ``"hypatia"`` additionally writes Hypatia-specific
    ``mode``/``processed``/``extracted_to`` frontmatter fields per
    ``vault-hypatia/SKILL.md``. Default ``""`` preserves the legacy wk1
    behaviour for any caller not yet threading the field.
    """
    # Import here to avoid a circular import at module load (ops pulls in
    # frontmatter + yaml which are heavier).
    from alfred.vault import ops as vault_ops

    active_dict = state.get_active(chat_id)
    if active_dict is None:
        raise ValueError(f"No active session for chat_id={chat_id}")

    session = Session.from_dict(active_dict)
    ended_at = _now_utc()
    # Per-instance mode resolution: registered tool_sets infer mode from
    # transcript + session_type; unknown/empty tool_set returns "" and
    # the wk1 ``Voice Session — ...`` filename is used.
    mode = _resolve_mode_for_instance(tool_set, session, session_type)

    fm = _build_session_frontmatter(
        session,
        ended_at=ended_at,
        reason=reason,
        user_vault_path=user_vault_path,
        stt_model_used=stt_model_used,
        session_type=session_type,
        continues_from=continues_from,
        pushback_level=pushback_level,
        tool_set=tool_set,
        mode=mode,
    )
    body = _build_session_body(session)

    # Unique record name — collisions across multiple same-minute closes would
    # otherwise fail vault_create, so the per-instance helpers append a
    # short session id.
    name = _build_record_name(session, tool_set=tool_set, mode=mode)

    vault_path = Path(vault_path_root)
    result = vault_ops.vault_create(
        vault_path,
        "session",
        name,
        set_fields=fm,
        body=body,
    )
    rel_path = result["path"]

    # State cleanup: pop active, append closed-summary, save once.
    # ``session_type`` / ``continues_from`` land here so the router can look up
    # the most recent article/journal/brainstorm session from state alone in
    # wk2 (plan open question #5 — state-only continuation for wk2; body-parser
    # fallback is a wk3 task).
    state.pop_active(chat_id)
    state.append_closed({
        "session_id": session.session_id,
        "chat_id": session.chat_id,
        "started_at": session.started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "reason": reason,
        "record_path": rel_path,
        "message_count": len(session.transcript),
        "vault_ops": len(session.vault_ops),
        "session_type": session_type,
        "continues_from": continues_from,
        # Wk3 commit 8: record the opening and closing model so
        # model_calibration.propose_default_flip can detect mid-session
        # escalation. ``opening_model`` falls back to current ``model``
        # for wk2 records being written during transition.
        "opening_model": session.opening_model or session.model,
        "closing_model": session.model,
    })
    state.save()

    log.info(
        "talker.session.closed",
        chat_id=chat_id,
        session_id=session.session_id,
        reason=reason,
        record_path=rel_path,
        messages=len(session.transcript),
        vault_ops=len(session.vault_ops),
    )
    return rel_path


# --- Frontmatter + body builders (pure, easy to test) ---


def _build_session_frontmatter(
    session: Session,
    ended_at: datetime,
    reason: str,
    user_vault_path: str | None = None,
    stt_model_used: str = "",
    session_type: str = "note",
    continues_from: str | None = None,
    pushback_level: int | None = None,
    tool_set: str = "",
    mode: str = "conversation",
) -> dict[str, Any]:
    """Produce the ``session/`` record frontmatter.

    Pure function — no side effects, no imports of vault ops. Matches section
    4 of the voice-design doc, with the correction that ``outputs`` is
    populated from ``session.vault_ops`` rather than left empty.

    wk2 additions (plan open question #2):
    - Top-level ``session_type`` — one of ``note|task|journal|article|brainstorm``.
    - Top-level ``continues_from`` — wikilink string (``[[session/...]]``) or
      ``None``. Emitted as YAML null when absent so downstream queries can
      filter on ``continues_from != null``.
    - ``telegram.model`` stays as-is (not renamed to ``model_used``) so wk1
      records and wk2 records share the same telemetry schema.

    Per-instance shape (``tool_set``):
    - All registered instances (``INSTANCE_MODE_PREFIXES``) use the
      mode-prefixed display name ``<Mode> — <date> <slug>``.
    - ``"hypatia"`` additionally adds her ``/extract``-workflow fields
      (``mode`` / ``processed`` / ``extracted_to`` / ``duration_minutes``)
      per ``vault-hypatia/SKILL.md`` and ``~/library-alexandria/CLAUDE.md``.
      Salem and KAL-LE deliberately do NOT gain those fields — they're
      tied to Hypatia's capture queue and would cause Bases-view drift
      on the other vaults.
    - Unknown / empty ``tool_set`` falls back to ``Voice Session —
      <date>`` for backward compat with wk1 records.
    """
    voice_count, text_count = _count_message_kinds(session)
    participants = [f"[[{user_vault_path}]]"] if user_vault_path else []
    # Dedup ``outputs`` while preserving first-seen insertion order. A
    # single conversation may issue multiple ``vault_edit`` calls against
    # the same record (e.g. a long-running task list edited 9 times in
    # one session); the audit history lives on ``session.vault_ops`` /
    # the ``vault_operations`` field, but the user-facing ``outputs``
    # list should surface each touched record once. ``dict.fromkeys``
    # gives ordered-set semantics on Python 3.7+.
    outputs = list(dict.fromkeys(
        f"[[{op['path']}]]" for op in session.vault_ops
    ))

    # Display name mirrors the filename pattern: registered instances
    # (talker, hypatia, kalle) use ``<Mode> — <date> <slug>``; legacy /
    # unknown tool_sets keep ``Voice Session — <date> <time>`` for
    # backward compat with wk1 records.
    if tool_set in INSTANCE_MODE_PREFIXES:
        # ``mode`` may be empty if the caller passed an unregistered
        # tool_set string by mistake — fall back to the instance's
        # first-listed prefix so the display name stays well-formed.
        display_mode = mode or INSTANCE_MODE_PREFIXES[tool_set][0]
        display_name = (
            f"{display_mode.capitalize()} — "
            f"{_slug_from_date(session.started_at)} "
            f"{_slug_from_topic(_first_user_text(session.transcript))}"
        )
    else:
        display_name = f"Voice Session — {_slug_from_dt(session.started_at)}"

    fm: dict[str, Any] = {
        "type": "session",
        "status": "completed",
        "name": display_name,
        "created": session.started_at.date().isoformat(),
        "description": (
            f"Telegram talker session ({len(session.transcript)} turns, "
            f"{len(session.vault_ops)} vault ops, closed via {reason})."
        ),
        "intent": "Capture a voice/text conversation with Alfred and any "
                  "vault actions it produced.",
        "participants": participants,
        "project": [],
        "outputs": outputs,
        "related": [],
        "tags": ["voice", "telegram"],
        "session_type": session_type,
        "continues_from": continues_from,
        "telegram": {
            "chat_id": session.chat_id,
            "session_id": session.session_id,
            "started_at": session.started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "close_reason": reason,
            "model": session.model,
            "stt_model": stt_model_used,
            "message_count": len(session.transcript),
            "voice_messages": voice_count,
            "text_messages": text_count,
            "vault_operations": list(session.vault_ops),
            # wk3 commit 1: record the session's pushback dial so the
            # distiller and vault-reviewer can correlate output style with
            # the directive that produced it. Emitted as None when the
            # session was opened before wk3 so wk2 records stay parseable.
            "pushback_level": pushback_level,
        },
    }

    # Outbound delivery failures (Layer 3 of the talker outbound-transport
    # silent-drop fix). Field is omitted entirely when no failures
    # occurred so existing-shape consumers are unaffected. When present,
    # each entry carries enough context (turn_index, length, error) for a
    # surfacing tool to locate the undelivered text in the transcript.
    if session.outbound_failures:
        fm["outbound_failures"] = list(session.outbound_failures)

    # Vision (image attachments). Field omitted when empty so pre-vision
    # session records / consumers see no shape drift. Each entry carries
    # the saved vault path so the distiller / future tools can locate
    # the file (the base64 payload itself never lands in frontmatter —
    # it would inflate every record by MB and break Obsidian indexing).
    if session.images:
        fm["images"] = list(session.images)

    if tool_set == "hypatia":
        # Per Hypatia SKILL spec + library-alexandria/CLAUDE.md: mode +
        # processed gate the "Unprocessed captures" Bases view; capture
        # sessions queue at ``processed: false`` until Hypatia runs the
        # extraction pass on /extract. Conversation sessions go straight
        # to ``processed: true`` (the structuring pass at close time IS
        # the processing for conversations). ``extracted_to`` is an
        # empty list placeholder; Hypatia populates it via vault_edit
        # set_fields when she creates downstream records.
        # ``duration_minutes`` is rounded — the spec ships round numbers
        # for the Bases view "Stale drafts" / "Unprocessed captures"
        # filters, and ended_at - started_at is the canonical source.
        fm["mode"] = mode
        fm["processed"] = (mode != "capture")
        fm["extracted_to"] = []
        elapsed = (ended_at - session.started_at).total_seconds()
        fm["duration_minutes"] = max(0, round(elapsed / 60))

    return fm


def _count_message_kinds(session: Session) -> tuple[int, int]:
    """Return ``(voice, text)`` counts from the transcript.

    Voice/text distinction is stored per-turn as ``_kind`` metadata on the
    message dict by the bot handler (commit 4). If absent, all turns count as
    text.
    """
    voice = 0
    text = 0
    for turn in session.transcript:
        if turn.get("role") != "user":
            continue
        kind = turn.get("_kind") or "text"
        if kind == "voice":
            voice += 1
        else:
            text += 1
    return voice, text


def _build_session_body(session: Session) -> str:
    """Render the transcript as readable Markdown.

    User turns: ``**Andrew** (HH:MM · voice): …``
    Assistant turns: ``**Alfred** (HH:MM): …``

    Tool-use / tool-result blocks inside a content list render as compact
    one-liners (``[tool_use: vault_search glob=project/*.md]``). This keeps
    the session record skimmable — JSON blobs would be unreadable.
    """
    lines: list[str] = ["# Transcript", ""]
    base_time = session.started_at

    for idx, turn in enumerate(session.transcript):
        role = turn.get("role", "user")
        content = turn.get("content", "")
        kind = turn.get("_kind") or "text"

        # Rough timestamp: if the turn has its own ``_ts``, use it;
        # otherwise fall back to the session start. Real timestamps arrive
        # once the bot handler (commit 4) stamps each turn.
        ts_raw = turn.get("_ts")
        ts = _parse_iso(ts_raw) if isinstance(ts_raw, str) else base_time
        hhmm = ts.strftime("%H:%M")

        if role == "user":
            speaker = "Andrew"
            meta = " · voice" if kind == "voice" else ""
            header = f"**{speaker}** ({hhmm}{meta}):"
        else:
            header = f"**Alfred** ({hhmm}):"

        rendered = _render_content(content)
        if rendered:
            lines.append(f"{header} {rendered}")
        else:
            lines.append(header)
        # Blank line between turns, except after the last one
        if idx < len(session.transcript) - 1:
            lines.append("")

    return "\n".join(lines) + "\n"


def _render_content(content: str | list[dict[str, Any]]) -> str:
    """Render Anthropic-format content into one-liner-friendly text."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return str(content)

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        btype = block.get("type", "")
        if btype == "text":
            text = (block.get("text") or "").strip()
            if text:
                parts.append(text)
        elif btype == "tool_use":
            name = block.get("name", "?")
            inp = block.get("input") or {}
            inp_summary = ", ".join(
                f"{k}={_summarize_value(v)}" for k, v in inp.items()
            )
            parts.append(f"[tool_use: {name} {inp_summary}]".rstrip())
        elif btype == "tool_result":
            tid = block.get("tool_use_id", "")
            err = " error" if block.get("is_error") else ""
            parts.append(f"[tool_result{err}: {tid[:8]}…]")
        elif btype == "image":
            # Vision: render as a compact ``[image]`` marker in the
            # transcript body. The base64 payload would balloon the
            # session record by ~MB per screenshot — useless to a
            # reader and harmful to git diffs / Obsidian indexing. The
            # canonical record of *which* image is on the session's
            # ``images`` field (frontmatter), populated at handle_message
            # time by the bot layer when it persists the file to inbox/.
            parts.append("[image]")
        else:
            parts.append(f"[{btype}]")
    return " ".join(parts)


def _summarize_value(value: Any) -> str:
    """Trim long values to keep the compact-block summary readable."""
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "..."
