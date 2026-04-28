"""Resolve a Telegram reply against the persisted Daily Sync batch.

The talker bot calls :func:`handle_daily_sync_reply` from
``handle_message`` BEFORE its inline-command check / session pipeline.
When the reply matches the persisted Daily Sync message_ids, the
parser walks Andrew's terse reply, resolves modifiers ("down"/"up")
against the batch's per-item classifier tier, writes one
:class:`CorpusEntry` per touched item, and returns a confirmation
message to send back. Returns ``None`` when the reply is NOT a Daily
Sync reply — caller falls through to the normal pipeline.

Phase 2 extends the dispatch with attribution-item routing. The
state-file ``last_batch`` now carries TWO item lists:

  * ``items`` — email calibration items (existing — untouched).
  * ``attribution_items`` — attribution-audit items (new in c3).

Each correction parsed from Andrew's reply is routed by its
``item_number`` against whichever list claims it. Email items follow
the existing classifier-priority resolution path; attribution items
flow through ``confirm_marker`` / ``reject_marker`` from
``alfred.vault.attribution`` and append to a separate corpus file.

Single source of truth: this module is the only place that converts
Andrew's reply into corpus rows. Slash-command-driven calibration
(``/calibrate`` re-fire) routes through here too once a fresh batch
arrives and Andrew replies to it.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter
import structlog

from alfred.vault.attribution import (
    confirm_marker,
    parse_audit_entries,
    reject_marker,
)

from .assembler import (
    ReplyCorrection,
    ReplyParseResult,
    apply_modifier,
    parse_reply,
)
from .attribution_corpus import AttributionCorpusEntry, append_entry as append_attribution_entry
from .config import DailySyncConfig
from .confidence import load_state, save_state
from .corpus import CorpusEntry, append_correction

log = structlog.get_logger(__name__)


def _last_batch_message_ids(config: DailySyncConfig) -> set[int]:
    """Return the set of Telegram message_ids the bot last pushed."""
    state = load_state(config.state.path)
    batch = state.get("last_batch") or {}
    ids = batch.get("message_ids") or []
    return {int(x) for x in ids if isinstance(x, (int, str)) and str(x).isdigit()}


def _last_batch_items(config: DailySyncConfig) -> list[dict[str, Any]]:
    """Return the per-item dicts the daemon stashed at fire time."""
    state = load_state(config.state.path)
    batch = state.get("last_batch") or {}
    items = batch.get("items") or []
    return [i for i in items if isinstance(i, dict)]


def _last_batch_attribution_items(config: DailySyncConfig) -> list[dict[str, Any]]:
    """Return the per-attribution-item dicts the daemon stashed at fire time.

    Empty list when the most recent fire didn't include any attribution
    items (e.g. the empty-state "No attribution items pending review."
    case). The reply parser treats item_numbers not present here as
    email items, falling back to the existing classifier-priority path.
    """
    state = load_state(config.state.path)
    batch = state.get("last_batch") or {}
    items = batch.get("attribution_items") or []
    return [i for i in items if isinstance(i, dict)]


def _last_batch_proposal_items(config: DailySyncConfig) -> list[dict[str, Any]]:
    """Return the canonical-proposals items the daemon stashed at fire time.

    Each item carries ``item_number``, ``correlation_id``,
    ``proposer``, ``record_type``, ``name``, ``proposed_fields``,
    ``source``. Empty list when the most recent fire had no pending
    proposals. The reply dispatcher routes a confirm/reject verb
    against whichever items list claims the item_number.
    """
    state = load_state(config.state.path)
    batch = state.get("last_batch") or {}
    items = batch.get("proposal_items") or []
    return [i for i in items if isinstance(i, dict)]


def reply_targets_daily_sync(
    config: DailySyncConfig,
    parent_message_id: int,
) -> bool:
    """Return True iff ``parent_message_id`` matches the persisted batch."""
    return parent_message_id in _last_batch_message_ids(config)


# ---------------------------------------------------------------------------
# Option B — smart-routing reply parser (Phase 2)
# ---------------------------------------------------------------------------
#
# Andrew's UX expectation (2026-04-23 voice session):
#
#   "If my first message after receiving the calibration data looks like
#    a calibration response, including partial responses, treat it as
#    such. If I need to add more detail later I will use the reply to
#    message function, like I would in a human conversation."
#
# Implementation:
#   1. The state-file ``last_batch`` carries a new ``replied: bool``
#      field (default false). The reply dispatcher flips it to true on
#      ANY successful Daily Sync reply (smart-routed or reply-to-message).
#   2. ``maybe_smart_route_reply`` is called early in the bot's message
#      handler — BEFORE the normal conversation pipeline. When the
#      message text matches the calibration heuristic AND the latest
#      Daily Sync hasn't been replied to yet, the dispatcher routes it
#      through the existing reply flow.
#   3. False-positive guard: if the parser returns zero corrections AND
#      zero all_ok, the smart-routing was wrong — revert the flag and
#      let the caller fall through to normal conversation.
#
# Once ``replied=true`` for a batch, subsequent messages route through
# normal conversation. Andrew uses Telegram's reply-to-message for
# follow-up clarifications, which still hits the existing
# ``reply_targets_daily_sync`` path with explicit override semantics.

# Calibration-shape heuristic patterns. Order matters — the parser
# returns the FIRST matching shape so a pure ``✅`` short-circuits
# without running the more expensive numbered-list regex.
#
# Why not just always defer to the parser? The parser will
# enthusiastically bucket "1. tomorrow we should..." as item 1 with
# unparsed token "tomorrow", which is a noisy false positive. The
# heuristic is a cheap pre-filter: it only routes to the parser when
# the message has the SHAPE of a calibration response, not just a
# coincidental leading digit.

# Whole-message ack tokens — the existing parser also recognises these
# (via ``_ALL_OK_PATTERNS``). Duplicated here so the smart-routing
# decision doesn't depend on importing parser internals.
_SMART_ROUTE_ALL_OK_RE = re.compile(
    r"^(?:✅|✔|👍|ok|okay|all good|all ok|looks good|approved)\s*[.!]?\s*$",
    re.IGNORECASE,
)

# Numbered-list bullet at the start of the message: ``1.``, ``1)``,
# ``1 ``. Lenient on whitespace + bullet style. The single-digit form
# is intentional — Daily Sync items are 1-indexed and rarely exceed 30,
# but ``\d+`` rather than ``\d{1,2}`` keeps the regex simple.
_SMART_ROUTE_NUMBERED_LIST_RE = re.compile(r"^\s*\d+\s*[.\):]?\s+\S")

# Multi-numbered references: ``1 down, 2 spam`` / ``1: high; 6 confirm``.
# We require TWO matches so a single coincidental "1 hour later"
# doesn't false-positive. The token alternation matches the email tier
# verbs + attribution verbs the parser recognises.
_SMART_ROUTE_NUM_REF_RE = re.compile(
    r"\b\d+\s*[.,:\-]?\s*"
    r"(high|medium|med|low|spam|up|down|"
    r"confirm|keep|yes|reject|delete|remove|no|"
    r"ok|okay|good|approved)\b",
    re.IGNORECASE,
)


def looks_like_calibration_reply(text: str) -> bool:
    """Return True when ``text`` has the shape of a Daily Sync reply.

    Three matching shapes (any one is sufficient):

    1. A whole-message ack token (``✅``, ``ok``, ``all good``, etc.).
    2. A leading numbered-list bullet (``1. ...`` / ``1) ...`` /
       ``1 ...``) — caller still verifies the parser actually
       extracts a correction (false-positive guard).
    3. Two or more "<number> <verb>" tokens in the text — the
       multi-item shorthand Andrew uses for batched corrections
       (``1 down, 2 spam``).

    The function is deliberately conservative on the third shape (two
    matches required) so prose like "1 hour later, 2 questions came
    up" doesn't smart-route. The caller's false-positive guard
    (parser returns zero corrections) catches the residual misses.
    """
    if not text:
        return False
    stripped = text.strip()
    if not stripped:
        return False
    # Strip a leading bullet so " - ✅" still matches the all-ok pattern.
    cleaned = re.sub(r"^[-*•]\s+", "", stripped)
    if _SMART_ROUTE_ALL_OK_RE.match(cleaned):
        return True
    if _SMART_ROUTE_NUMBERED_LIST_RE.match(cleaned):
        return True
    matches = _SMART_ROUTE_NUM_REF_RE.findall(cleaned)
    if len(matches) >= 2:
        return True
    return False


def is_latest_batch_replied(config: DailySyncConfig) -> bool:
    """Return True when the latest persisted batch already saw a reply.

    Looks up ``last_batch.replied`` from the state file. A missing
    key (older state, no batch ever pushed) returns False — the
    smart-routing guard treats "no batch" as "nothing to route to".
    """
    state = load_state(config.state.path)
    batch = state.get("last_batch") or {}
    return bool(batch.get("replied", False))


def mark_batch_replied(config: DailySyncConfig) -> None:
    """Flip ``last_batch.replied`` to True in the state file.

    No-op when no batch is persisted. Tolerant of malformed state —
    we read+rewrite via the existing ``load_state`` / ``save_state``
    helpers so the rest of the state file is preserved verbatim.
    """
    state = load_state(config.state.path)
    batch = state.get("last_batch")
    if not isinstance(batch, dict):
        return
    if batch.get("replied") is True:
        return  # idempotent
    batch["replied"] = True
    state["last_batch"] = batch
    try:
        save_state(config.state.path, state)
    except OSError as exc:
        log.warning(
            "daily_sync.smart_route.flag_write_failed",
            error=str(exc),
        )


def _revert_batch_replied(config: DailySyncConfig) -> None:
    """Roll back ``last_batch.replied`` after a false-positive smart-route.

    Used when ``maybe_smart_route_reply`` optimistically flips the
    flag but the parser produces zero actionable output — we don't
    want to lock Andrew out of the legitimate calibration window
    because of a mis-classified message.
    """
    state = load_state(config.state.path)
    batch = state.get("last_batch")
    if not isinstance(batch, dict):
        return
    if batch.get("replied") is not True:
        return
    batch["replied"] = False
    state["last_batch"] = batch
    try:
        save_state(config.state.path, state)
    except OSError as exc:
        log.warning(
            "daily_sync.smart_route.flag_revert_failed",
            error=str(exc),
        )


def maybe_smart_route_reply(
    config: DailySyncConfig,
    reply_text: str,
    *,
    vault_path: Path | None = None,
    instance_scope: str = "talker",
) -> dict[str, Any] | None:
    """Try to handle ``reply_text`` as a Daily Sync reply WITHOUT a
    reply-to-message context. Returns the same shape as
    :func:`handle_daily_sync_reply` on success, or ``None`` to fall
    through to normal conversation.

    Routing rules (Andrew's spec, 2026-04-23):
      1. If a Daily Sync batch is persisted AND it has not yet been
         replied to AND ``reply_text`` matches the calibration shape:
         route through the dispatcher with the latest batch's first
         ``message_id`` as the synthetic parent.
      2. If the parser produces zero corrections AND zero ``all_ok``,
         treat the route as a false positive — revert the ``replied``
         flag and return ``None`` so the caller falls through.
      3. Once ``replied=true``, subsequent messages always fall
         through. Andrew uses reply-to-message for follow-ups.

    ``instance_scope`` mirrors :func:`handle_daily_sync_reply` — the
    running instance's scope name forwarded so canonical-record
    proposal-confirms create under the right scope.

    The caller (bot) checks ``reply_targets_daily_sync`` first. This
    function is the second-line dispatch for messages that DON'T have
    a Telegram reply context — the "first-message-after-Daily-Sync
    looks like a calibration response" UX.
    """
    if not reply_text or not reply_text.strip():
        return None

    if is_latest_batch_replied(config):
        return None

    if not looks_like_calibration_reply(reply_text):
        return None

    message_ids = sorted(_last_batch_message_ids(config))
    if not message_ids:
        # No batch persisted — nothing to route to. Don't flip the
        # flag; nothing to flip.
        return None

    # Optimistically flip the flag BEFORE running the dispatcher so a
    # crash mid-dispatch doesn't leave the next legitimate
    # smart-routed message stranded behind a "not yet replied" gate.
    # The false-positive guard below reverts the flag if needed.
    mark_batch_replied(config)

    # Use the lowest message_id as the synthetic parent — the
    # dispatcher only checks set membership so any of the persisted
    # IDs works.
    synthetic_parent = message_ids[0]
    result = handle_daily_sync_reply(
        config,
        parent_message_id=synthetic_parent,
        reply_text=reply_text,
        vault_path=vault_path,
        instance_scope=instance_scope,
    )

    if result is None:
        # Defensive: shouldn't happen because we just confirmed
        # message_ids exist, but the dispatcher could conceivably
        # return None on a torn-state read. Revert the flag.
        _revert_batch_replied(config)
        return None

    # False-positive guard: zero confirmed AND not all_ok means the
    # parser couldn't extract a calibration action from this message.
    # Revert the flag so the next legitimate calibration reply still
    # lands.
    if not result.get("all_ok") and not result.get("confirmed_count"):
        _revert_batch_replied(config)
        log.info(
            "daily_sync.smart_route.false_positive_revert",
            unparsed=len(result.get("unparsed", [])),
        )
        return None

    log.info(
        "daily_sync.smart_route.applied",
        parent_message_id=synthetic_parent,
        confirmed=result.get("confirmed_count", 0),
        all_ok=result.get("all_ok", False),
    )
    return result


def _attribution_corpus_path(config: DailySyncConfig) -> str:
    """Return the attribution corpus path, falling back to the default.

    Tolerant of older configs that pre-date the ``attribution`` block.
    """
    block = getattr(config, "attribution", None)
    if block is None:
        return "./data/attribution_audit_corpus.jsonl"
    return getattr(block, "corpus_path", "./data/attribution_audit_corpus.jsonl")


def _canonical_proposals_queue_path() -> str | None:
    """Return the canonical-proposals queue path from the transport config.

    The queue lives in ``transport.canonical.proposals_path``. Returns
    ``None`` when the transport config can't be resolved — the
    dispatcher treats a missing path as "proposals feature not wired
    up" and buckets confirm/reject on a proposal item into unparsed.
    """
    try:
        from alfred.transport.config import load_config
        transport_config = load_config()
    except Exception as exc:  # noqa: BLE001
        log.info(
            "daily_sync.proposals.transport_config_unavailable",
            error=str(exc),
        )
        return None
    path = transport_config.canonical.proposals_path
    return path or None


def _now_iso() -> str:
    """Wall-clock ISO-8601 UTC. Wrapped so tests can monkeypatch."""
    return datetime.now(timezone.utc).isoformat()


def _resolve_attribution_correction(
    correction: ReplyCorrection,
    item: dict[str, Any],
    vault_path: Path,
    corpus_path: str,
) -> tuple[str | None, bool]:
    """Apply one attribution-item correction.

    Returns ``(error_str_or_None, did_write_corpus)``. The bool is True
    only when the call materially changed the record AND wrote a corpus
    row — the no-op-idempotent path returns ``(None, False)`` so the
    dispatcher can show "0 applied" instead of double-counting.

    On confirm: read the record, apply ``confirm_marker``, write back,
    append a confirm row to the attribution corpus. On reject: read,
    apply ``reject_marker``, write back, append a reject row preserving
    the rejected content. Idempotent — if the marker is already
    confirmed (or already absent) we log + skip without re-writing.

    Unknown verbs (modifier/tier on an attribution item) become an
    "unparsed" string the caller buckets so Andrew sees the bot's
    "couldn't parse" reply for that item.
    """
    marker_id = str(item.get("marker_id") or "")
    record_path = str(item.get("record_path") or "")
    agent = str(item.get("agent") or "")
    section_title = str(item.get("section_title") or "")
    marker_date = str(item.get("date") or "")

    if not marker_id or not record_path:
        return (f"item {correction.item_number} attribution metadata missing", False)

    if not (correction.ok or correction.reject):
        # Modifiers / tiers don't apply to attribution items — they
        # only make sense for email calibration. Bucket as unparsed.
        return (
            f"item {correction.item_number}: attribution items only "
            f"accept `confirm`/`keep`/`yes` or `reject`/`delete`/`no`",
            False,
        )

    file_path = vault_path / record_path
    if not file_path.exists():
        log.warning(
            "daily_sync.attribution.record_missing",
            record_path=record_path,
            marker_id=marker_id,
        )
        return (
            f"item {correction.item_number}: record {record_path} no longer exists",
            False,
        )

    try:
        post = frontmatter.load(str(file_path))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "daily_sync.attribution.read_failed",
            record_path=record_path,
            error=str(exc),
        )
        return (f"item {correction.item_number}: couldn't read {record_path}", False)

    fm = post.metadata or {}
    body = post.content or ""

    # Idempotency: confirm-then-confirm is a no-op; reject-then-reject
    # likewise. Look up the entry in frontmatter once; if it isn't
    # present (or already in the right state), short-circuit with
    # ``(None, False)`` — no error, but also no new write.
    entries = parse_audit_entries(fm)
    target = next((e for e in entries if e.marker_id == marker_id), None)

    if correction.ok:
        if target is None:
            log.info(
                "daily_sync.attribution.confirm.already_resolved",
                marker_id=marker_id,
                record_path=record_path,
            )
            return (None, False)
        if target.confirmed_by_andrew:
            log.info(
                "daily_sync.attribution.confirm.idempotent_noop",
                marker_id=marker_id,
                record_path=record_path,
            )
            return (None, False)
        confirm_marker(fm, marker_id, by="andrew")
        post.metadata = fm
        try:
            file_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
        except OSError as exc:
            log.warning(
                "daily_sync.attribution.write_failed",
                record_path=record_path,
                error=str(exc),
            )
            return (f"item {correction.item_number}: write failed", False)
        try:
            append_attribution_entry(
                corpus_path,
                AttributionCorpusEntry(
                    type="attribution_confirm",
                    marker_id=marker_id,
                    record_path=record_path,
                    agent=agent,
                    section_title=section_title,
                    marker_date=marker_date,
                    andrew_action="confirm",
                    action_at=_now_iso(),
                    andrew_note=correction.note,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "daily_sync.attribution.corpus_write_failed",
                record_path=record_path,
                marker_id=marker_id,
                error=str(exc),
            )
        return (None, True)

    # reject path
    if target is None:
        log.info(
            "daily_sync.attribution.reject.already_resolved",
            marker_id=marker_id,
            record_path=record_path,
        )
        return (None, False)
    # Preserve the rejected section content in the corpus before we
    # strip it from the body — load-bearing for the audit trail.
    preview = str(item.get("content_preview") or "")
    new_body, new_fm = reject_marker(body, fm, marker_id)
    post.metadata = new_fm
    post.content = new_body
    try:
        file_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning(
            "daily_sync.attribution.write_failed",
            record_path=record_path,
            error=str(exc),
        )
        return (f"item {correction.item_number}: write failed", False)
    try:
        append_attribution_entry(
            corpus_path,
            AttributionCorpusEntry(
                type="attribution_reject",
                marker_id=marker_id,
                record_path=record_path,
                agent=agent,
                section_title=section_title,
                marker_date=marker_date,
                andrew_action="reject",
                action_at=_now_iso(),
                andrew_note=correction.note,
                original_section_content=preview,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "daily_sync.attribution.corpus_write_failed",
            record_path=record_path,
            marker_id=marker_id,
            error=str(exc),
        )
    return (None, True)


def _resolve_proposal_correction(
    correction: ReplyCorrection,
    item: dict[str, Any],
    vault_path: Path,
    proposals_queue_path: str,
    *,
    instance_scope: str = "talker",
) -> tuple[str | None, bool]:
    """Apply one canonical-proposal confirm/reject.

    Returns ``(error_str_or_None, did_write)``.

    On confirm: calls :func:`vault_create` with the running instance's
    ``scope`` (read from ``config.instance.tool_set`` and threaded in
    by :func:`handle_daily_sync_reply`) and the proposer's
    ``proposed_fields`` to create the canonical record, then marks the
    proposal ``accepted`` in the queue JSONL. Default ``"talker"``
    preserves Salem's behaviour for legacy callers.

    On reject: marks the proposal ``rejected`` and does NOT create
    any record.

    Unknown verbs (modifier/tier on a proposal item) become an
    ``unparsed`` string so Andrew sees the "couldn't parse" hint for
    that item.
    """
    from alfred.transport.canonical_proposals import (
        STATE_ACCEPTED,
        STATE_REJECTED,
        update_proposal_state,
    )
    from alfred.vault.ops import vault_create

    correlation_id = str(item.get("correlation_id") or "")
    record_type = str(item.get("record_type") or "")
    name = str(item.get("name") or "")
    proposed_fields = dict(item.get("proposed_fields") or {})

    if not correlation_id or not record_type or not name:
        return (
            f"item {correction.item_number} proposal metadata missing",
            False,
        )

    if not (correction.ok or correction.reject):
        return (
            f"item {correction.item_number}: canonical proposals only "
            f"accept `confirm`/`keep`/`yes` or `reject`/`delete`/`no`",
            False,
        )

    if correction.reject:
        try:
            flipped = update_proposal_state(
                proposals_queue_path, correlation_id, STATE_REJECTED,
            )
        except OSError as exc:
            log.warning(
                "daily_sync.proposals.state_write_failed",
                correlation_id=correlation_id,
                action="reject",
                error=str(exc),
            )
            return (f"item {correction.item_number}: queue write failed", False)
        if not flipped:
            # Already rejected / missing / nonexistent — idempotent no-op.
            log.info(
                "daily_sync.proposals.reject.no_op",
                correlation_id=correlation_id,
            )
            return (None, False)
        log.info(
            "daily_sync.proposals.rejected",
            correlation_id=correlation_id,
            record_type=record_type,
            name=name,
        )
        return (None, True)

    # confirm path — create the canonical record under the running
    # instance's scope (Salem → "talker", KAL-LE → "kalle", Hypatia →
    # "hypatia"; validated by SCOPE_RULES). Read from
    # ``config.instance.tool_set`` and threaded in by the caller; default
    # "talker" preserves legacy behaviour for callers that skip the plumb.
    try:
        result = vault_create(
            vault_path=vault_path,
            record_type=record_type,
            name=name,
            set_fields=proposed_fields or None,
            scope=instance_scope,
        )
    except Exception as exc:  # noqa: BLE001
        # Most common failure: record already exists on disk (race).
        # Surface the reason so Andrew sees what happened; either way
        # the proposal should not block the queue indefinitely, so flip
        # it to rejected with a note in the log.
        log.warning(
            "daily_sync.proposals.create_failed",
            correlation_id=correlation_id,
            record_type=record_type,
            name=name,
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        return (
            f"item {correction.item_number}: couldn't create "
            f"{record_type}/{name}: {exc}",
            False,
        )

    try:
        update_proposal_state(
            proposals_queue_path, correlation_id, STATE_ACCEPTED,
        )
    except OSError as exc:
        log.warning(
            "daily_sync.proposals.state_write_failed",
            correlation_id=correlation_id,
            action="confirm",
            error=str(exc),
        )
        # The record exists now; the queue-file mark is stale. This is
        # observability leakage not data loss — Andrew will see the
        # same proposal again next Daily Sync and can reject it, and
        # ``update_proposal_state`` is idempotent so the next try lands.

    log.info(
        "daily_sync.proposals.accepted",
        correlation_id=correlation_id,
        record_type=record_type,
        name=name,
        vault_path=result.get("path") if isinstance(result, dict) else None,
    )
    return (None, True)


def _format_proposal_applied_line(
    item: dict[str, Any],
    *,
    action: str,
) -> str:
    """Return a one-liner describing a proposal confirm/reject.

    Format::

        "Item N: created person/Elena Brighton (from KAL-LE)"
        "Item N: rejected proposal for person/Arthur Mbeki (from KAL-LE)"
    """
    item_number = item.get("item_number") or "?"
    proposer = str(item.get("proposer") or "").strip() or "(unknown proposer)"
    record_type = str(item.get("record_type") or "record").strip()
    name = str(item.get("name") or "(unknown)").strip()
    if action == "reject":
        return (
            f"Item {item_number}: rejected proposal for "
            f"{record_type}/{name} (from {proposer})"
        )
    return (
        f"Item {item_number}: created {record_type}/{name} "
        f"(from {proposer})"
    )


def _format_email_applied_line(
    item: dict[str, Any],
    andrew_priority: str,
    *,
    cluster_size: int = 1,
) -> str:
    """Return a one-liner describing what the applier did for an email item.

    Format (singleton)::

        "Item N: {sender} — 'Subject' -> {TIER}"

    Format (cluster of size > 1, c5)::

        "Item N: {sender} — 'Subject' -> {TIER} (applied to {K} records)"

    c3 — Andrew asked for the feedback loop to be visible: replace the
    opaque ``"applied N correction(s)"`` with a per-item summary of
    what was learned. We don't invent a rule that doesn't exist — the
    calibration corpus is still append-only and the classifier rotates
    the tail as few-shot examples. The echo reports the ACTION
    (tier assignment) and SCOPE (single item or K-record cluster) the
    applier actually performed.
    """
    item_number = item.get("item_number") or "?"
    sender = str(item.get("sender") or "").strip() or "(unknown)"
    subject = str(item.get("subject") or "").strip() or "(no subject)"
    tier = str(andrew_priority or "").upper() or "?"
    suffix = ""
    if cluster_size > 1:
        suffix = f" (applied to {cluster_size} records)"
    return f"Item {item_number}: {sender} — \"{subject}\" -> {tier}{suffix}"


def _format_attribution_applied_line(
    item: dict[str, Any],
    *,
    action: str,
) -> str:
    """Return a one-liner describing an attribution confirm/reject.

    Format::

        "Item N: {agent} marker in {record_path} — confirmed"
        "Item N: {agent} marker in {record_path} — rejected"
    """
    item_number = item.get("item_number") or "?"
    agent = str(item.get("agent") or "").strip() or "(unknown agent)"
    record_path = str(item.get("record_path") or "").strip() or "(unknown record)"
    verb = "rejected" if action == "reject" else "confirmed"
    return f"Item {item_number}: {agent} marker in {record_path} — {verb}"


def _build_confirmation_body(
    *,
    parsed_all_ok: bool,
    applied_lines: list[str],
    written_count: int,
    unparsed_item_numbers: list[int],
    raw_errors: list[str],
) -> str:
    """Compose the user-facing confirmation reply.

    c3 restructures this block:

      * When items were applied, emit a per-item summary (up to 5 lines
        so the Telegram bubble stays readable on mobile).
      * When items failed to parse, render a user-facing list of item
        numbers with a hint about the "Same" chaining shortcut — no
        raw-token dump.
      * Pure-ack (``✅``) keeps its short one-liner form.

    Fallback (written_count == 0 AND no unparsed numbers but raw errors
    exist) prints the raw error because the parser produced fragments
    that don't map to item numbers — the operator still needs to see
    them.
    """
    # all-ok shortcut stays terse — Andrew already knows what he confirmed.
    if parsed_all_ok:
        if written_count == 0:
            return "Calibration: nothing to apply."
        # Prefer the short form; attribution/email split is internal detail.
        return f"Calibration: confirmed all {written_count} item(s)."

    lines: list[str] = []
    if applied_lines:
        lines.append(f"Calibration: applied {written_count} correction(s).")
        # Cap at 5 so the reply bubble doesn't get unwieldy on mobile.
        for line in applied_lines[:5]:
            lines.append(f"  {line}")
        remaining = len(applied_lines) - 5
        if remaining > 0:
            lines.append(f"  ... and {remaining} more.")

    if unparsed_item_numbers:
        nums_sorted = sorted(set(unparsed_item_numbers))
        if len(nums_sorted) == 1:
            which = f"item {nums_sorted[0]}"
        else:
            which = "items " + ", ".join(str(n) for n in nums_sorted)
        hint = (
            " (Tip: 'Same' / 'Ditto' / 'Same as #N' are supported "
            "for list items.)"
        )
        if lines:
            lines.append(f"Didn't understand {which} — could you restate?{hint}")
        else:
            lines.append(f"Calibration: didn't understand {which} — could you restate?{hint}")
    elif raw_errors and not applied_lines:
        # Edge case: parser-level failures that never got a bucketed
        # item number (e.g. the pre-c1 regression of orphan fragments).
        # Render them as-is so the operator can see the raw input. This
        # path should be very rare post-c1.
        lines.append(f"Calibration: couldn't parse: {', '.join(raw_errors[:3])}.")

    if not lines:
        return "Calibration: nothing to apply."

    return "\n".join(lines)


def _item_record_paths(item: dict[str, Any]) -> list[str]:
    """Return every vault record path the item covers.

    c5 — email items may represent a CLUSTER of N near-identical
    records (``cluster_record_paths`` populated). In that case a
    correction fans out to every member path. Legacy / singleton
    items return a single-element list containing ``record_path``.
    Empty / malformed items return ``[]``.
    """
    cluster = item.get("cluster_record_paths")
    if isinstance(cluster, list) and cluster:
        # Preserve the stored order and de-duplicate while keeping
        # the primary (index 0) first.
        seen: set[str] = set()
        ordered: list[str] = []
        for path in cluster:
            sp = str(path or "").strip()
            if sp and sp not in seen:
                seen.add(sp)
                ordered.append(sp)
        if ordered:
            return ordered
    primary = str(item.get("record_path") or "").strip()
    return [primary] if primary else []


def _resolve_correction(
    correction: ReplyCorrection,
    items_by_num: dict[int, dict[str, Any]],
) -> tuple[list[CorpusEntry] | None, str | None]:
    """Convert one :class:`ReplyCorrection` into a list of :class:`CorpusEntry`.

    Returns ``(entries, error)`` — exactly one is non-None. The list
    contains ONE entry per underlying record (always 1 for singleton
    items; N for a c5 cluster). Errors are short human-readable
    strings the caller can echo back to Andrew so he knows which
    fragments couldn't be applied.
    """
    item = items_by_num.get(correction.item_number)
    if item is None:
        return None, f"item {correction.item_number} not in last batch"

    classifier_priority = str(item.get("classifier_priority", "")).lower()
    classifier_action_hint = item.get("classifier_action_hint")
    classifier_reason = str(item.get("classifier_reason") or "")

    # Resolve the new tier:
    #   - explicit tier wins if set
    #   - else apply modifier ("down"/"up") to classifier_priority
    #   - else "ok" — andrew confirms classifier output
    if correction.new_tier is not None:
        andrew_priority = correction.new_tier
    elif correction.modifier:
        andrew_priority = apply_modifier(classifier_priority, correction.modifier)
    elif correction.ok:
        andrew_priority = classifier_priority
    else:
        # Should be unreachable — _parse_fragment requires at least one of
        # tier/modifier/ok to be set. Defensive return so a future regex
        # bug doesn't crash the dispatcher.
        return None, f"item {correction.item_number} had no actionable token"

    record_paths = _item_record_paths(item)
    if not record_paths:
        return None, f"item {correction.item_number} has no record path"

    timestamp = datetime.now(timezone.utc).isoformat()
    entries = [
        CorpusEntry(
            record_path=path,
            classifier_priority=classifier_priority,
            classifier_action_hint=(
                classifier_action_hint
                if isinstance(classifier_action_hint, (str, type(None)))
                else str(classifier_action_hint)
            ),
            classifier_reason=classifier_reason,
            andrew_priority=andrew_priority,
            andrew_action_hint=None,  # c2 doesn't yet expose action-hint corrections
            andrew_reason=correction.note,
            timestamp=timestamp,
            sender=str(item.get("sender") or ""),
            subject=str(item.get("subject") or ""),
            snippet=str(item.get("snippet") or ""),
        )
        for path in record_paths
    ]
    return entries, None


def handle_daily_sync_reply(
    config: DailySyncConfig,
    parent_message_id: int,
    reply_text: str,
    *,
    vault_path: Path | None = None,
    instance_scope: str = "talker",
) -> dict[str, Any] | None:
    """Process a Daily Sync reply. Returns a result dict or ``None``.

    Returns ``None`` when the reply isn't aimed at the persisted Daily
    Sync batch — the caller (talker bot) treats ``None`` as "fall
    through to normal pipeline".

    Item-level routing (Phase 2): the parser produces generic
    ``ReplyCorrection`` instances. The dispatcher looks up each
    correction's ``item_number`` in the email items map first; if
    absent, it tries the attribution items map; if still absent, the
    correction is bucketed as unparsed. ``vault_path`` is required for
    attribution items (so the dispatcher can read + write the affected
    record); it's a kwarg so existing email-only tests continue to
    pass without supplying it.

    ``instance_scope`` is the running instance's scope name (mirror of
    ``config.instance.tool_set``: ``"talker"`` for Salem, ``"kalle"``
    for KAL-LE, ``"hypatia"`` for Hypatia). Forwarded to
    :func:`_resolve_proposal_correction` so canonical-record creates on
    proposal-confirm pass through the right scope's allowlist. Default
    ``"talker"`` preserves Salem's behaviour for legacy callers / tests
    that skip the plumb.

    On a match, the result dict carries:
      - ``confirmed_count``: int — how many entries were written
        (sum across email + attribution)
      - ``unparsed``: list[str] — fragments the parser couldn't resolve
      - ``message``: str — confirmation text to reply with
      - ``all_ok``: bool
      - ``email_count``: int — email rows written
      - ``attribution_count``: int — attribution actions applied
    """
    if not reply_targets_daily_sync(config, parent_message_id):
        return None

    email_items = _last_batch_items(config)
    email_by_num = {int(i.get("item_number", 0)): i for i in email_items}
    attribution_items = _last_batch_attribution_items(config)
    attribution_by_num = {
        int(i.get("item_number", 0)): i for i in attribution_items
    }
    proposal_items = _last_batch_proposal_items(config)
    proposal_by_num = {
        int(i.get("item_number", 0)): i for i in proposal_items
    }

    parsed: ReplyParseResult = parse_reply(reply_text)

    email_written = 0
    attribution_written = 0
    proposal_written = 0  # propose-person c2
    applied_lines: list[str] = []  # c3 — one per-item summary line per accepted correction
    errors: list[str] = list(parsed.unparsed)
    unparsed_item_numbers: list[int] = []  # c3 — numeric IDs of items that couldn't parse
    corpus_path = _attribution_corpus_path(config)
    proposals_queue_path = _canonical_proposals_queue_path() if proposal_items else None

    # all_ok shortcut: write an email corpus row per email item (fanned
    # out across cluster members — c5) AND confirm every attribution
    # item. "✅" means "everything in the entire Daily Sync is good" —
    # both lists.
    if parsed.all_ok:
        for item in email_items:
            classifier_priority = str(item.get("classifier_priority", "")).lower()
            timestamp = datetime.now(timezone.utc).isoformat()
            record_paths = _item_record_paths(item)
            if not record_paths:
                continue
            rows_written_this_item = 0
            for path in record_paths:
                entry = CorpusEntry(
                    record_path=path,
                    classifier_priority=classifier_priority,
                    classifier_action_hint=item.get("classifier_action_hint"),
                    classifier_reason=str(item.get("classifier_reason") or ""),
                    andrew_priority=classifier_priority,
                    andrew_action_hint=None,
                    andrew_reason="",
                    timestamp=timestamp,
                    sender=str(item.get("sender") or ""),
                    subject=str(item.get("subject") or ""),
                    snippet=str(item.get("snippet") or ""),
                )
                try:
                    append_correction(config.corpus.path, entry)
                    rows_written_this_item += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "daily_sync.corpus_write_failed",
                        record_path=entry.record_path,
                        error=str(exc),
                    )
            if rows_written_this_item > 0:
                email_written += rows_written_this_item
                applied_lines.append(
                    _format_email_applied_line(
                        item,
                        classifier_priority,
                        cluster_size=rows_written_this_item,
                    )
                )
        if attribution_items:
            if vault_path is None:
                # Can't apply attribution confirms without a vault path.
                # Log + record an error for each attribution item so
                # the operator sees the gap rather than a silent no-op.
                for item in attribution_items:
                    errors.append(
                        f"item {item.get('item_number')}: vault_path not provided"
                    )
                    try:
                        unparsed_item_numbers.append(int(item.get("item_number", 0)))
                    except (TypeError, ValueError):
                        pass
            else:
                for item in attribution_items:
                    synthetic = ReplyCorrection(
                        item_number=int(item.get("item_number", 0)),
                        ok=True,
                    )
                    err, did_write = _resolve_attribution_correction(
                        synthetic, item, vault_path, corpus_path,
                    )
                    if err is not None:
                        errors.append(err)
                        unparsed_item_numbers.append(synthetic.item_number)
                    elif did_write:
                        attribution_written += 1
                        applied_lines.append(
                            _format_attribution_applied_line(item, action="confirm")
                        )
        if proposal_items:
            if vault_path is None or proposals_queue_path is None:
                for item in proposal_items:
                    errors.append(
                        f"item {item.get('item_number')}: "
                        f"{'vault_path' if vault_path is None else 'proposals queue'}"
                        f" not configured"
                    )
                    try:
                        unparsed_item_numbers.append(int(item.get("item_number", 0)))
                    except (TypeError, ValueError):
                        pass
            else:
                for item in proposal_items:
                    synthetic = ReplyCorrection(
                        item_number=int(item.get("item_number", 0)),
                        ok=True,
                    )
                    err, did_write = _resolve_proposal_correction(
                        synthetic, item, vault_path, proposals_queue_path,
                        instance_scope=instance_scope,
                    )
                    if err is not None:
                        errors.append(err)
                        unparsed_item_numbers.append(synthetic.item_number)
                    elif did_write:
                        proposal_written += 1
                        applied_lines.append(
                            _format_proposal_applied_line(item, action="confirm")
                        )

    else:
        for correction in parsed.corrections:
            email_item = email_by_num.get(correction.item_number)
            attribution_item = attribution_by_num.get(correction.item_number)
            proposal_item = proposal_by_num.get(correction.item_number)

            if email_item is not None:
                # Reject verb makes no sense on an email item.
                if correction.reject:
                    errors.append(
                        f"item {correction.item_number}: `reject` is "
                        f"only meaningful for attribution items"
                    )
                    unparsed_item_numbers.append(correction.item_number)
                    continue
                entries, err = _resolve_correction(correction, email_by_num)
                if err is not None:
                    errors.append(err)
                    unparsed_item_numbers.append(correction.item_number)
                    continue
                assert entries is not None and entries
                # c5 — fan-out: one corpus row per cluster member.
                # Cluster-aware summary line replaces the prior per-
                # record line so Andrew sees "(4 records)" rather than
                # four identical lines.
                cluster_size = len(entries)
                rows_written_this_item = 0
                for entry in entries:
                    try:
                        append_correction(config.corpus.path, entry)
                        rows_written_this_item += 1
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "daily_sync.corpus_write_failed",
                            record_path=entry.record_path,
                            error=str(exc),
                        )
                if rows_written_this_item > 0:
                    email_written += rows_written_this_item
                    applied_lines.append(
                        _format_email_applied_line(
                            email_item,
                            entries[0].andrew_priority,
                            cluster_size=cluster_size,
                        )
                    )
            elif attribution_item is not None:
                if vault_path is None:
                    errors.append(
                        f"item {correction.item_number}: vault_path not provided"
                    )
                    unparsed_item_numbers.append(correction.item_number)
                    continue
                err, did_write = _resolve_attribution_correction(
                    correction, attribution_item, vault_path, corpus_path,
                )
                if err is not None:
                    errors.append(err)
                    unparsed_item_numbers.append(correction.item_number)
                    continue
                if did_write:
                    attribution_written += 1
                    applied_lines.append(
                        _format_attribution_applied_line(
                            attribution_item,
                            action="reject" if correction.reject else "confirm",
                        )
                    )
            elif proposal_item is not None:
                if vault_path is None or proposals_queue_path is None:
                    errors.append(
                        f"item {correction.item_number}: "
                        f"{'vault_path' if vault_path is None else 'proposals queue'}"
                        f" not configured"
                    )
                    unparsed_item_numbers.append(correction.item_number)
                    continue
                err, did_write = _resolve_proposal_correction(
                    correction, proposal_item, vault_path, proposals_queue_path,
                    instance_scope=instance_scope,
                )
                if err is not None:
                    errors.append(err)
                    unparsed_item_numbers.append(correction.item_number)
                    continue
                if did_write:
                    proposal_written += 1
                    applied_lines.append(
                        _format_proposal_applied_line(
                            proposal_item,
                            action="reject" if correction.reject else "confirm",
                        )
                    )
            else:
                errors.append(
                    f"item {correction.item_number} not in last batch"
                )
                unparsed_item_numbers.append(correction.item_number)

    written_count = email_written + attribution_written + proposal_written

    # c3 — user-facing body. Per-item summary lines go in (capped at 5
    # so the Telegram reply stays readable on mobile), followed by a
    # human-readable parse-failure sentence that mentions the "Same"
    # chaining shortcut instead of dumping raw fragments.
    body = _build_confirmation_body(
        parsed_all_ok=parsed.all_ok,
        applied_lines=applied_lines,
        written_count=written_count,
        unparsed_item_numbers=unparsed_item_numbers,
        raw_errors=errors,
    )

    log.info(
        "daily_sync.reply_processed",
        parent_message_id=parent_message_id,
        all_ok=parsed.all_ok,
        email_written=email_written,
        attribution_written=attribution_written,
        proposal_written=proposal_written,
        unparsed=len(errors),
    )

    # Mark the batch as replied so subsequent messages route through
    # normal conversation (Andrew's UX expectation: reply-to-message
    # for follow-up clarifications, not chained smart-routes).
    # We only flip the flag when something material happened (all_ok
    # or at least one correction landed) — a pure-noise reply-to-
    # message that produced zero corrections shouldn't lock out the
    # smart-routing window for a real calibration reply later.
    if parsed.all_ok or written_count > 0:
        try:
            mark_batch_replied(config)
        except Exception as exc:  # noqa: BLE001 — flag-write failure must not crash the dispatcher
            log.warning(
                "daily_sync.reply_processed.flag_write_failed",
                error=str(exc),
            )

    return {
        "confirmed_count": written_count,
        "email_count": email_written,
        "attribution_count": attribution_written,
        "proposal_count": proposal_written,
        "unparsed": errors,
        "message": body,
        "all_ok": parsed.all_ok,
    }
