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


def _last_batch_pending_items(config: DailySyncConfig) -> list[dict[str, Any]]:
    """Return the pending-items entries the daemon stashed at fire time.

    Each item carries ``item_number``, ``id`` (queue uuid),
    ``category``, ``created_by_instance``, ``session_id``,
    ``context``, ``resolution_options`` (list of ``{id, label}``).
    Empty list when the most recent fire had no pending items. The
    reply dispatcher routes ``noted`` / ``show me`` verbs against
    whichever items list claims the item_number.
    """
    state = load_state(config.state.path)
    batch = state.get("last_batch") or {}
    items = batch.get("pending_items") or []
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
#
# Task #55 (2026-06-01) — kept in lockstep with ``_ALL_OK_PATTERNS``
# in ``assembler.py``. If you widen one, widen the other.
_SMART_ROUTE_ALL_OK_RE = re.compile(
    r"^(?:"
    r"✅|✔|👍|"
    r"ok|okay|"
    r"all good|all ok|all clear|"
    r"looks good|good to go|"
    r"approved|approve all|approve|"
    r"confirm all|all confirm|confirmed|"
    r"lgtm|"
    r"yes|y"
    r")\s*[.!]?\s*$",
    re.IGNORECASE,
)


# Range token — ``1-5 confirm`` / ``items 3-7 reject`` /
# ``4 through 9 high``. Task #55 (2026-06-01) — a single range token
# is enough to flag a calibration reply, otherwise the two-numbered-
# reference gate below would reject ``"1-5 confirm"`` (only one
# leading-digit token, even though it semantically spans five items).
#
# **Anchored** at start-of-string with ``^\s*`` (paired with
# :meth:`re.Pattern.match` below, not :meth:`re.Pattern.search`). An
# unanchored match would false-positive on prose like
# ``"chapters 1-5 keep reading on the bus"`` / ``"sections 3-7 delete
# the old draft"`` / ``"pages 4-9 down at the bookstore"`` — the
# leading word doesn't prevent the range substring from matching,
# and the smart-route guard (zero corrections + zero all_ok) doesn't
# save these because the per-fragment parser DOES produce
# corrections (for the wrong items). The siblings
# ``_SMART_ROUTE_NUMBERED_LIST_RE`` and ``_SMART_ROUTE_ALL_OK_RE``
# follow the same anchored-match discipline.
#
# Verb alternation is intentionally narrower than the per-fragment
# parser's combined set: ``critical`` / ``tracked`` /
# ``aspirational`` / ``approve`` are NOT recognised here because the
# per-fragment parser doesn't accept them either (the smart-route
# guard would revert the routing on zero output, but bouncing the
# message through the parser only to revert is wasteful and
# misleading in logs).
_SMART_ROUTE_RANGE_RE = re.compile(
    r"^\s*(?:items?\s+)?\d+\s*(?:[-–—]|\s+through\s+)\s*\d+\s+"
    r"(?:high|medium|low|spam|"
    r"confirm|reject|keep|delete|remove|"
    r"yes|no|ok|up|down)\b",
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
    r"ok|okay|good|approved|"
    # Pending Items Queue Phase 1 verbs.
    r"noted|show|"
    # Stage 1 2026-05-15 — duplicate verb for "5 duplicate" /
    # "5 duplicate of 4" shapes.
    r"duplicate)\b",
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
    # Task #55 (2026-06-01) — single range token ("1-5 confirm") is
    # enough on its own; the two-numbered-reference gate below would
    # otherwise reject it for having only one leading-digit token.
    # Anchored match (NOT search) so prose like ``"chapters 1-5 keep
    # reading"`` doesn't false-positive on the embedded range
    # substring.
    if _SMART_ROUTE_RANGE_RE.match(cleaned):
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
    instance_name: str = "salem",
    raw_config: dict[str, Any] | None = None,
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
        instance_name=instance_name,
        raw_config=raw_config,
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


def _canonical_proposals_queue_path(
    config: DailySyncConfig | None = None,
) -> str | None:
    """Return the canonical-proposals queue path from the transport config.

    The queue lives in ``transport.canonical.proposals_path``. Returns
    ``None`` when the transport config can't be resolved — the
    dispatcher treats a missing path as "proposals feature not wired
    up" and buckets confirm/reject on a proposal item into unparsed.

    Threads ``config.config_path`` through to ``load_config(path)`` so
    a per-instance daily_sync daemon (Hypatia, KAL-LE) reads ITS OWN
    config file instead of silently defaulting to Salem's
    ``config.yaml``. ``config=None`` and ``config.config_path is None``
    both fall back to ``"config.yaml"`` for backward compat with
    existing test fixtures that didn't thread the path. Mirrors
    commit 420364b's pattern.
    """
    config_path = "config.yaml"
    if config is not None and config.config_path:
        config_path = config.config_path
    try:
        from alfred.transport.config import load_config
        transport_config = load_config(config_path)
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
    from alfred.vault.ops import VaultError
    try:
        result = vault_create(
            vault_path=vault_path,
            record_type=record_type,
            name=name,
            set_fields=proposed_fields or None,
            scope=instance_scope,
        )
    except VaultError as exc:
        # File-already-exists is the merge-trigger for Stage 1 person
        # proposals (2026-05-15). Andrew's design: when Hypatia / KAL-LE
        # propose a person canonical record that ALREADY exists in
        # Salem's vault, merge proposed fields into the existing record
        # (fill-empty conservative) rather than fail the proposal.
        # Other VaultErrors (scope-deny, near-match refusal, etc.) and
        # non-person record types fall through to the original
        # ``couldn't create`` error path.
        err_str = str(exc)
        if (
            record_type == "person"
            and "already exists" in err_str.lower()
        ):
            return _merge_person_proposal(
                correction=correction,
                correlation_id=correlation_id,
                name=name,
                proposed_fields=proposed_fields,
                vault_path=vault_path,
                proposals_queue_path=proposals_queue_path,
                instance_scope=instance_scope,
            )
        log.warning(
            "daily_sync.proposals.create_failed",
            correlation_id=correlation_id,
            record_type=record_type,
            name=name,
            error=err_str,
            error_type=exc.__class__.__name__,
        )
        return (
            f"item {correction.item_number}: couldn't create "
            f"{record_type}/{name}: {exc}",
            False,
        )
    except Exception as exc:  # noqa: BLE001
        # Non-VaultError exceptions surface verbatim — defensive
        # against unexpected backend failures.
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


# ---------------------------------------------------------------------------
# Person merge-on-conflict (Stage 1, 2026-05-15)
# ---------------------------------------------------------------------------
#
# When a peer (Hypatia / KAL-LE) proposes a canonical person record that
# already exists in Salem's vault, we want a fill-empty merge rather
# than an "execution failure" bucket. Stage 1 supports ``person`` only;
# Stage 2 will generalize + surface conflicts as next-batch daily-sync
# items. Andrew's framing 2026-05-15: aliases are important — all
# variants resolve to the same record, the receiving instance picks up
# what the proposer offered without clobbering Salem's existing data.


# Path of the auditable merge log inside Salem's vault. Append-only;
# new merges are appended as H2 sections. Used by Salem's vault_read
# when Andrew asks about a recent merge.
_PERSON_MERGE_LOG_REL_PATH = "process/Person Merge Log.md"


def _merge_person_proposal(
    *,
    correction: ReplyCorrection,
    correlation_id: str,
    name: str,
    proposed_fields: dict[str, Any],
    vault_path: Path,
    proposals_queue_path: str,
    instance_scope: str,
) -> tuple[str | None, bool]:
    """Merge a person proposal into an existing record (Stage 1, 2026-05-15).

    Called from :func:`_resolve_proposal_correction` when
    ``vault_create`` raised a ``File already exists`` :class:`VaultError`
    AND ``record_type == "person"``. The merge is conservative
    (fill-empty only) and alias-aware:

      * Direct match: try ``person/{name}.md`` first.
      * Alias fallback: if direct miss, scan ``person/*.md`` and match
        ``name`` against each record's ``aliases`` frontmatter list.
        First match wins; 2+ matches return an error (operator
        disambiguates).
      * No match: defensive error — the file-exists VaultError implied
        SOMETHING exists, so 0 matches is "weird state."

    Field policy:
      * Existing field is None / empty / missing → SET from proposal.
      * Existing equals proposal → no-op.
      * Existing differs from proposal (both non-empty) → SKIP, append
        to ``conflict_fields``. Stage 2 surfaces these as next-batch
        items. Stage 1 logs them and writes them to the merge log
        for operator visibility.

    Alias addition: if ``name`` differs from existing record's ``name``
    AND isn't already in ``aliases``, append it to ``aliases``.

    On success: emit ``daily_sync.proposals.merged_into_existing`` log
    event, append a section to the merge log file, mark the proposal
    ``accepted`` with ``accepted_via="merge"``.

    Returns ``(error_str_or_None, did_write)``.
    """
    from alfred.transport.canonical_proposals import (
        STATE_ACCEPTED,
        update_proposal_state,
    )
    from alfred.vault.ops import VaultError, vault_edit, vault_read

    # 1. Locate the existing record — direct first, then alias scan.
    existing_path: str | None = None
    existing_fm: dict[str, Any] = {}

    direct_rel = f"person/{name}.md"
    try:
        record = vault_read(vault_path, direct_rel)
        existing_path = direct_rel
        existing_fm = dict(record.get("frontmatter") or {})
    except VaultError:
        # Direct miss — fall through to alias scan.
        existing_path = None

    if existing_path is None:
        matches: list[tuple[str, dict[str, Any]]] = []
        person_dir = vault_path / "person"
        if person_dir.exists():
            for fp in sorted(person_dir.glob("*.md")):
                rel = f"person/{fp.name}"
                try:
                    rec = vault_read(vault_path, rel)
                except VaultError:
                    continue
                fm = dict(rec.get("frontmatter") or {})
                aliases = fm.get("aliases") or []
                if not isinstance(aliases, list):
                    continue
                # Case-insensitive alias match; Salem's curator stores
                # aliases as the operator typed them, but match permits
                # casing drift.
                if any(
                    isinstance(a, str) and a.strip().lower() == name.strip().lower()
                    for a in aliases
                ):
                    matches.append((rel, fm))
        if len(matches) == 0:
            log.warning(
                "daily_sync.proposals.merge_lookup_failed",
                correlation_id=correlation_id,
                proposal_name=name,
                reason="no_direct_or_alias_match",
            )
            return (
                f"item {correction.item_number}: file-exists VaultError "
                f"but couldn't locate existing record by name or alias",
                False,
            )
        if len(matches) > 1:
            paths_list = ", ".join(p for p, _ in matches)
            log.warning(
                "daily_sync.proposals.merge_lookup_ambiguous",
                correlation_id=correlation_id,
                proposal_name=name,
                paths=[p for p, _ in matches],
            )
            return (
                f"item {correction.item_number}: alias '{name}' matches "
                f"multiple existing records: {paths_list}",
                False,
            )
        existing_path, existing_fm = matches[0]

    # 2. Conservative fill-empty merge — walk proposed_fields, classify.
    filled_fields: list[str] = []
    conflict_fields: list[tuple[str, Any, Any]] = []
    merge_set: dict[str, Any] = {}

    for field_name, proposed_value in (proposed_fields or {}).items():
        existing_value = existing_fm.get(field_name)
        if existing_value is None or existing_value == "" or (
            isinstance(existing_value, list) and not existing_value
        ):
            merge_set[field_name] = proposed_value
            filled_fields.append(field_name)
        elif existing_value == proposed_value:
            # No-op — proposal contributes nothing new.
            continue
        else:
            conflict_fields.append((field_name, existing_value, proposed_value))

    # 3. Alias addition — if the proposal's name differs from the
    # existing record's ``name`` AND isn't already aliased.
    #
    # Case-insensitive uniqueness on BOTH sides of the comparison.
    # Earlier ship had a case-drift bug: ``existing_aliases=["ben"]`` +
    # proposal ``name="Ben"`` would slip the case-sensitive
    # ``name not in existing_aliases`` check and produce a duplicate
    # ``aliases=["ben", "Ben"]`` after merge. The lookup loop above
    # already matches case-insensitively (line ~886); the addition
    # check now mirrors that semantic. The lookup path stays untouched.
    existing_name = str(existing_fm.get("name") or "").strip()
    existing_aliases_raw = existing_fm.get("aliases") or []
    if not isinstance(existing_aliases_raw, list):
        existing_aliases_raw = []
    existing_aliases = [str(a) for a in existing_aliases_raw if isinstance(a, str)]
    existing_aliases_lower = {a.strip().lower() for a in existing_aliases}
    aliases_added: list[str] = []
    name_lower = name.strip().lower() if name else ""
    if (
        name
        and name_lower != existing_name.strip().lower()
        and name_lower not in existing_aliases_lower
    ):
        # Preserve any pending alias merge from filled_fields above
        # (if proposed_fields itself supplied aliases, we'd union).
        new_aliases = list(existing_aliases)
        new_aliases_lower = set(existing_aliases_lower)
        if "aliases" in merge_set:
            # Merge proposed aliases first, then append the proposal name.
            proposed_aliases = merge_set["aliases"] or []
            if isinstance(proposed_aliases, list):
                for a in proposed_aliases:
                    sa = str(a)
                    sa_lower = sa.strip().lower()
                    if sa and sa_lower not in new_aliases_lower:
                        new_aliases.append(sa)
                        new_aliases_lower.add(sa_lower)
        new_aliases.append(name)
        new_aliases_lower.add(name_lower)
        merge_set["aliases"] = new_aliases
        aliases_added.append(name)
        if "aliases" not in filled_fields:
            filled_fields.append("aliases")

    # 4. Apply via vault_edit when there's anything to write.
    if merge_set:
        try:
            vault_edit(
                vault_path=vault_path,
                rel_path=existing_path,
                set_fields=merge_set,
                scope=instance_scope,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "daily_sync.proposals.merge_edit_failed",
                correlation_id=correlation_id,
                proposal_name=name,
                existing_path=existing_path,
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
            return (
                f"item {correction.item_number}: merge into "
                f"{existing_path} failed: {exc}",
                False,
            )

    # 5. Emit audit log.
    log.info(
        "daily_sync.proposals.merged_into_existing",
        correlation_id=correlation_id,
        proposal_name=name,
        existing_path=existing_path,
        filled_fields=list(filled_fields),
        conflict_fields=[
            (fname, fexisting, fproposed)
            for (fname, fexisting, fproposed) in conflict_fields
        ],
        aliases_added=list(aliases_added),
    )

    # 6. Append a section to the merge log file. Best-effort: errors
    # here are observability leaks, not data loss — the merge already
    # landed on the existing record.
    try:
        _append_person_merge_log_entry(
            vault_path=vault_path,
            correlation_id=correlation_id,
            proposal_name=name,
            existing_path=existing_path,
            filled_fields=filled_fields,
            conflict_fields=conflict_fields,
            aliases_added=aliases_added,
        )
    except Exception as exc:  # noqa: BLE001 — log-file write must not crash dispatch
        log.warning(
            "daily_sync.proposals.merge_log_write_failed",
            correlation_id=correlation_id,
            existing_path=existing_path,
            error=str(exc),
            error_type=exc.__class__.__name__,
        )

    # 7. Flip queue state to ``accepted`` with ``accepted_via="merge"``.
    try:
        update_proposal_state(
            proposals_queue_path,
            correlation_id,
            STATE_ACCEPTED,
            accepted_via="merge",
        )
    except OSError as exc:
        log.warning(
            "daily_sync.proposals.state_write_failed",
            correlation_id=correlation_id,
            action="merge",
            error=str(exc),
        )
        # Idempotency: even if the queue write fails the merge already
        # landed on the existing record. Next Daily Sync will re-surface
        # the proposal; the alias / fill-empty path is idempotent so a
        # re-confirm produces no-op or another merge-log entry.

    return (None, True)


# Mode tag for the structlog event when a merge had no diffs to apply
# (everything proposed already matched the existing record). Kept as
# a separate constant so dashboards / grep workflows can pin it.
_MERGE_NOOP_EVENT = "daily_sync.proposals.merge_noop"


def _append_person_merge_log_entry(
    *,
    vault_path: Path,
    correlation_id: str,
    proposal_name: str,
    existing_path: str,
    filled_fields: list[str],
    conflict_fields: list[tuple[str, Any, Any]],
    aliases_added: list[str],
) -> None:
    """Append a merge-log section to ``vault/process/Person Merge Log.md``.

    Creates the file with valid frontmatter (``type: process``) when
    absent, so it's a queryable vault record. Each merge appends an
    H2 section with timestamp + summary fields; Salem's ``vault_read``
    on the file gives the operator a readable history.

    Race-conscious: we open + read + append + atomic-rename via tmp
    file. The dispatcher is invoked from the bot's per-chat-serialized
    handler, so concurrent merges in practice never happen — but the
    pattern matches Salem's other append-only vault writers.
    """
    file_path = vault_path / _PERSON_MERGE_LOG_REL_PATH

    timestamp = _now_iso()
    section_lines: list[str] = []
    section_lines.append("")
    section_lines.append(f"## {timestamp} — {proposal_name}")
    section_lines.append(f"- Proposal correlation: `{correlation_id}`")
    section_lines.append(f"- Existing record: `{existing_path}`")
    if filled_fields:
        section_lines.append(
            "- Fields filled (empty → proposal): "
            + ", ".join(filled_fields)
        )
    else:
        section_lines.append("- Fields filled (empty → proposal): (none)")
    if conflict_fields:
        section_lines.append(
            "- Fields kept (existing non-empty differed from proposal): "
            + ", ".join(fname for (fname, _e, _p) in conflict_fields)
        )
    else:
        section_lines.append(
            "- Fields kept (existing non-empty differed from proposal): (none)"
        )
    if aliases_added:
        section_lines.append("- Aliases added: " + ", ".join(aliases_added))
    else:
        section_lines.append("- Aliases added: (none)")

    new_section = "\n".join(section_lines) + "\n"

    # Bootstrap file with valid process-record frontmatter on first
    # merge. Reuses the canonical scaffold template fields so the file
    # is a queryable vault record (type=process) rather than a free-
    # form markdown blob.
    file_path.parent.mkdir(parents=True, exist_ok=True)
    if not file_path.exists():
        bootstrap_fm = {
            "type": "process",
            "status": "active",
            "name": "Person Merge Log",
            "description": (
                "Append-only audit log of person canonical proposals "
                "merged into existing records (Stage 1, 2026-05-15)."
            ),
            "frequency": "as-needed",
            "tags": [],
            "related": [],
            "created": timestamp.split("T", 1)[0],
        }
        bootstrap_body = (
            "# Person Merge Log\n\n"
            "Each entry below corresponds to one canonical proposal "
            "merged into an existing person record. Stage 1 (2026-05-15) "
            "covers the person record type only.\n"
        )
        post = frontmatter.Post(bootstrap_body, **bootstrap_fm)
        file_path.write_text(
            frontmatter.dumps(post) + "\n",
            encoding="utf-8",
        )

    # Atomic append: read existing content, concat new section, write
    # via tmp + rename so a crash mid-write doesn't leave a torn file.
    existing_text = file_path.read_text(encoding="utf-8")
    if not existing_text.endswith("\n"):
        existing_text += "\n"
    new_text = existing_text + new_section
    tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
    tmp_path.write_text(new_text, encoding="utf-8")
    tmp_path.replace(file_path)


def _resolution_id_from_correction(
    correction: ReplyCorrection,
    item: dict[str, Any],
) -> str | None:
    """Map Andrew's terse reply token to a resolution_option id.

    Inputs::

        correction.consumed_token = "noted" | "show" | "ok" | ...
        item["resolution_options"]  = [{"id": "noted", "label": ...}, ...]

    Logic:

      * ``noted`` matches any option whose ``id`` is exactly
        ``"noted"`` OR whose label starts with the word "noted".
      * ``show`` (the leading verb of ``"show me"``) matches any
        option whose ``id`` starts with ``"show"`` (covers
        ``show_me``, ``show_text``, etc.) OR whose label starts with
        the word "show".
      * ``ok`` / ``yes`` / ``confirm`` map to the first option whose
        ``id`` is ``"noted"`` (legacy default for "no action needed").
        This covers the all-ok shortcut path.

    Returns ``None`` when no option matches — the dispatcher buckets
    that as unparsed.
    """
    options = item.get("resolution_options") or []
    if not isinstance(options, list):
        return None
    token = (correction.consumed_token or "").strip().lower()
    if not token and correction.ok:
        # Synthetic all-ok path or untokenized confirm.
        token = "noted"

    def _option_id(o: dict[str, Any]) -> str:
        return str(o.get("id") or "").strip().lower()

    def _option_label(o: dict[str, Any]) -> str:
        return str(o.get("label") or "").strip().lower()

    # Direct id match wins.
    for o in options:
        if isinstance(o, dict) and _option_id(o) == token:
            return _option_id(o)

    # ``show`` prefix.
    if token == "show":
        for o in options:
            if isinstance(o, dict) and _option_id(o).startswith("show"):
                return _option_id(o)
            if isinstance(o, dict) and _option_label(o).startswith("show"):
                return _option_id(o)

    # ``noted`` / generic ok → first option with id "noted".
    if token in {"noted", "ok", "okay", "yes", "y", "confirm", "confirmed", "keep"}:
        for o in options:
            if isinstance(o, dict) and _option_id(o) == "noted":
                return _option_id(o)
        # Fallback: first option id (better than failing — ``"noted"``
        # by Daily Sync convention is always option 0 of an
        # outbound_failure entry).
        if options and isinstance(options[0], dict):
            return _option_id(options[0])

    return None


def _resolve_pending_item_correction(
    correction: ReplyCorrection,
    item: dict[str, Any],
    *,
    self_instance: str,
    raw_config: dict[str, Any] | None = None,
) -> tuple[str | None, bool, str]:
    """Apply one pending-item resolution.

    Returns ``(error_str_or_None, did_resolve, applied_summary)``.

    Routing logic:

      * If ``item.created_by_instance`` is the running instance (or
        an alias like ``"talker"`` / ``"alfred"``), resolve locally
        via :func:`alfred.pending_items.executor.resolve_local_item`.
      * Otherwise dispatch via the
        :func:`pending_items_resolve` peer call to the originating
        instance.

    The peer dispatch is async — we run it in a fresh event loop
    when the dispatcher is called from a sync context (the bot's
    ``handle_daily_sync_reply`` path is sync wrt the parser). For
    Phase 1 we use ``asyncio.run`` inside a thread when an outer loop
    is already running; in tests the dispatcher is exercised directly
    and we fall through to sync-friendly code paths.

    ``raw_config`` is the pre-loaded unified config dict (passed
    through from the bot's ``handle_message`` callback). When
    provided, the local + peer helpers use it directly and skip
    the per-call ``open("config.yaml")`` round-trip — important on
    a hot path that fires for every Daily Sync reply. When ``None``,
    helpers fall back to opening ``config.yaml`` from the current
    working directory (legacy / test-friendly path).

    ``self_instance`` MUST be a non-empty instance identity. The
    bot wiring already plumbs ``agent_slug_for(talker_config)``
    through; an empty value here means a config-load failure
    silently routed Hypatia / KAL-LE items as if they were Salem.
    Raises :class:`ValueError` rather than silently fall back.
    """
    if not (self_instance or "").strip():
        # Per `feedback_hardcoding_and_alfred_naming.md`: silent
        # fallback to "salem" hides single-instance assumptions on
        # multi-instance installs. Caller must plumb a real value.
        raise ValueError(
            "self_instance must be a non-empty instance identity; "
            "got empty/None"
        )

    item_id = str(item.get("id") or "")
    created_by = str(item.get("created_by_instance") or "").strip().lower()
    if not item_id:
        return (
            f"item {correction.item_number}: pending item id missing",
            False,
            "",
        )

    resolution_id = _resolution_id_from_correction(correction, item)
    if resolution_id is None:
        return (
            f"item {correction.item_number}: pending items only "
            f"accept `noted` or `show me`",
            False,
            "",
        )

    # Normalize the running instance identity. The Salem alias-set
    # (``salem`` / ``alfred`` / ``talker``) is intentional — Salem-
    # originated items can carry any of those legacy created_by
    # labels. Other instances (Hypatia, KAL-LE) match strictly.
    self_normalized = self_instance.strip().lower()
    is_local = (
        created_by in {self_normalized, "salem"}
        if self_normalized in {"salem", "alfred", "talker"}
        else created_by == self_normalized
    )

    try:
        if is_local:
            applied_summary = _resolve_pending_item_locally(
                item_id=item_id,
                resolution_id=resolution_id,
                raw_config=raw_config,
            )
        else:
            applied_summary = _resolve_pending_item_via_peer(
                item_id=item_id,
                resolution_id=resolution_id,
                peer_name=created_by,
                self_instance=self_normalized,
                raw_config=raw_config,
            )
    except _PendingItemResolveFailure as exc:
        return (
            f"item {correction.item_number}: {exc}",
            False,
            "",
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        log.warning(
            "daily_sync.pending_items.resolve_unexpected",
            item_id=item_id,
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        return (
            f"item {correction.item_number}: unexpected error: {exc}",
            False,
            "",
        )

    return (None, True, applied_summary)


class _PendingItemResolveFailure(Exception):
    """Internal — surfaced as a per-item error string."""


def _load_raw_config_lazy(raw_config: dict[str, Any] | None) -> dict[str, Any]:
    """Return ``raw_config`` if provided, else open ``config.yaml`` once.

    Hot-path helper. The bot now plumbs ``raw_config`` through from
    its ``bot_data`` (loaded once at startup) so the dispatcher
    doesn't re-read the config file per Telegram reply. The fallback
    open-from-cwd path is preserved for legacy / direct test callers
    that exercise the dispatcher without the bot wiring.
    """
    if raw_config is not None:
        return raw_config
    try:
        import yaml as _yaml
        with open("config.yaml", "r", encoding="utf-8") as f:
            return _yaml.safe_load(f) or {}
    except OSError as exc:
        raise _PendingItemResolveFailure(
            f"config.yaml not readable: {exc}"
        ) from exc


def _resolve_pending_item_locally(
    *,
    item_id: str,
    resolution_id: str,
    raw_config: dict[str, Any] | None = None,
) -> str:
    """Resolve an item against the local queue. Sync wrapper."""
    from alfred.pending_items.config import (
        load_from_unified as load_pending,
    )
    from alfred.pending_items.executor import resolve_local_item

    raw = _load_raw_config_lazy(raw_config)

    pi_config = load_pending(raw)
    if not pi_config.enabled:
        raise _PendingItemResolveFailure(
            "pending_items not enabled on this instance"
        )

    vault_path_str = (raw.get("vault") or {}).get("path", "./vault")
    telegram_users = (raw.get("telegram") or {}).get("allowed_users") or []
    user_id = 0
    if telegram_users:
        try:
            user_id = int(telegram_users[0])
        except (TypeError, ValueError):
            user_id = 0

    coro = resolve_local_item(
        queue_path=pi_config.queue_path,
        item_id=item_id,
        resolution_id=resolution_id,
        vault_path=Path(vault_path_str),
        user_id=user_id,
    )
    result = _run_coro_sync(coro)
    if not result.get("ok"):
        raise _PendingItemResolveFailure(
            result.get("summary") or result.get("error") or "resolve failed"
        )
    return str(result.get("summary") or "resolved")


def _resolve_pending_item_via_peer(
    *,
    item_id: str,
    resolution_id: str,
    peer_name: str,
    self_instance: str,
    raw_config: dict[str, Any] | None = None,
) -> str:
    """Dispatch resolution to the originating peer.

    ``self_instance`` is the running instance's normalized identity
    (already validated non-empty by the calling correction handler).
    It feeds the ``self_name`` field on the peer call so the audit
    trail records the actual sender rather than a default. Per
    ``feedback_hardcoding_and_alfred_naming.md`` (2026-04-26 sweep) +
    the 2026-05-21 transport/client.py sibling-default sweep, the
    transport helper now requires this kwarg explicitly.
    """
    from alfred.transport.client import peer_resolve_pending_item
    from alfred.transport.config import load_from_unified as load_transport
    from alfred.transport.exceptions import TransportError

    raw = _load_raw_config_lazy(raw_config)

    transport_config = load_transport(raw)
    coro = peer_resolve_pending_item(
        peer_name,
        item_id=item_id,
        resolution=resolution_id,
        self_name=self_instance,
        config=transport_config,
    )
    try:
        response = _run_coro_sync(coro)
    except TransportError as exc:
        raise _PendingItemResolveFailure(
            f"peer dispatch failed: {exc}"
        ) from exc
    if not isinstance(response, dict):
        raise _PendingItemResolveFailure("peer returned non-dict response")
    if not response.get("executed"):
        raise _PendingItemResolveFailure(
            response.get("summary") or response.get("error") or "peer rejected"
        )
    return f"{response.get('summary') or 'resolved'} (via {peer_name})"


def _run_coro_sync(coro: Any) -> dict[str, Any]:
    """Run an awaitable from a sync caller, regardless of event-loop context.

    The Daily Sync reply dispatcher is invoked from the bot's sync
    handler (PTB's ``handle_message`` callback) — there's already a
    running event loop. ``asyncio.run`` would refuse. We use
    ``asyncio.new_event_loop`` + ``loop.run_until_complete`` inside a
    short-lived thread to avoid blocking the bot's loop.

    Phase 2 will refactor the dispatcher to be natively async; for
    now this scaffolding keeps the smart-routing path unchanged.
    """
    import asyncio as _asyncio
    import concurrent.futures

    try:
        running = _asyncio.get_running_loop()
    except RuntimeError:
        running = None

    if running is None:
        # Sync caller — run directly.
        return _asyncio.run(coro)

    # We're inside an event loop already (bot's). Run the coroutine
    # in a separate thread with its own loop.
    def _runner():
        loop = _asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(_runner)
        # 10s stopgap; Phase 2 native-async refactor planned. Slow /
        # down peers shouldn't be able to freeze the bot's event loop
        # for half a minute every Daily Sync reply.
        return future.result(timeout=10.0)


def _format_pending_item_applied_line(
    item: dict[str, Any],
    *,
    resolution_id: str,
    summary: str,
) -> str:
    """One-liner describing a pending-item resolution.

    Format::

        "Item N: [hypatia] outbound_failure → noted"
        "Item N: [salem]   outbound_failure → show_me — delivered..."
    """
    item_number = item.get("item_number") or "?"
    instance = str(item.get("created_by_instance") or "?").lower()
    category = str(item.get("category") or "pending_item")
    tail = f" — {summary}" if summary and resolution_id != "noted" else ""
    return f"Item {item_number}: [{instance}] {category} → {resolution_id}{tail}"


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


# Verb-mismatch markers — substrings the resolvers (and the dispatch
# loop's pre-resolver gates) emit when Andrew's verb doesn't match
# what the item type accepts. Used by :func:`_is_verb_mismatch_error`
# to distinguish "the parser understood, but the verb doesn't apply"
# (which deserves the calibration hint) from "the parser understood
# AND routed correctly, but the executor failed" (which deserves the
# verbatim error string — typically a scope-deny or
# vault-path-not-configured message that Andrew needs to see so he
# can react, e.g. "this proposal needs Salem to confirm, not me").
#
# 2026-05-10 incident: Andrew's "1 confirm" on KAL-LE's Daily Sync hit
# the proposal-confirm path, dispatched correctly, then ``vault_create``
# raised ``ScopeError`` (KAL-LE isn't the canonical owner for person
# records). The error string was perfectly informative ("Scope 'kalle'
# may not create local 'person' records — those are Salem's canonical
# authority") but the dispatcher buried it under "didn't understand
# item 1" + an email-section hint. This discriminator + the new
# ``execution_errors`` bucket fix the surfacing.
_VERB_MISMATCH_MARKERS = (
    "only accept",       # "attribution items only accept ..." / "canonical proposals only accept ..." / "pending items only accept ..."
    "only meaningful",   # "`reject` is only meaningful for attribution items"
    "not meaningful",    # "`reject` not meaningful — use `noted`"
)


def _is_verb_mismatch_error(err: str) -> bool:
    """Return True when ``err`` is a verb/shape-mismatch (deserves hint).

    Verb-mismatch errors mean Andrew's verb didn't fit this item type
    (e.g. ``reject`` on an email item, ``high`` on a pending item).
    They surface to Andrew via the "didn't understand item N" message
    with a hint about which verbs DO apply to this batch's items.

    Execution failures (``not in last batch``, ``vault_path not
    provided``, scope-deny from vault_create, peer dispatch failed)
    have richer error strings the operator needs to see verbatim.
    Those return ``False`` here and route to ``execution_errors``.
    """
    return any(marker in err for marker in _VERB_MISMATCH_MARKERS)


def _compose_calibration_hint(
    *,
    has_email: bool,
    has_attribution: bool,
    has_proposal: bool,
    has_pending: bool,
) -> str:
    """Build the "Tip: ..." hint based on which item types are in the batch.

    The 2026-05-10 KAL-LE incident surfaced the gap: the hint was
    hardcoded "Same / Ditto / Same as #N" — email-calibration verbs —
    but KAL-LE's batch had zero email items. The hint told Andrew to
    use verbs that wouldn't have parsed against any item in the batch.

    Hint composition (per item-type presence):

      * Email only → preserve the historical Salem hint
        ("Same / Ditto / Same as #N" — the chaining shortcut for
        contiguous identical-priority items).
      * Attribution / proposal items → ``N confirm`` / ``N reject``
        (matches what attribution_section.py:357 and
        canonical_proposals_section.py:186 advertise in the batch
        message body).
      * Pending items → ``N noted`` / ``N show me``.
      * Mixed → list the applicable verbs.

    Empty batch (none flagged) → empty hint (no actionable verbs to
    suggest). Falls through cleanly without a stray "Tip:" prefix.
    """
    verbs: list[str] = []
    if has_attribution or has_proposal:
        verbs.append("'N confirm' / 'N reject'")
    if has_pending:
        verbs.append("'N noted' / 'N show me'")
    if has_email:
        verbs.append("'Same' / 'Ditto' / 'Same as #N'")

    if not verbs:
        return ""
    if len(verbs) == 1:
        return f" (Tip: {verbs[0]} are supported for list items.)"
    return f" (Tip: {' or '.join(verbs)}.)"


def _format_count_with_cluster_expansion(
    *, corrections_count: int, written_count: int
) -> str:
    """Render the count phrase, surfacing cluster fan-out when it occurred.

    When ``corrections_count == written_count`` (no cluster fan-out, or
    only non-email items where 1 item == 1 row), returns the simple
    ``"N item(s)"`` form. When ``written_count > corrections_count``
    (email cluster fan-out wrote more corpus rows than operator-visible
    items), surfaces both numbers + the sibling count so the operator
    sees that their N corrections produced M rows — preventing the
    "I sent 5 corrections but Alfred said 6" miscount friction.

    2026-05-18 — operator-friction surface: morning calibration on 5
    emails produced a "6 corrections" confirmation because item 1's
    ViewPoint listing cluster had 2 siblings. The framing was misleading;
    the underlying fan-out (feb052c) was correct.
    """
    if written_count <= corrections_count:
        # Defensive ``<=``: should never be strictly less, but if it
        # ever is (e.g. a future per-item resolver that produces zero
        # rows but still counts as a correction), fall back to the
        # simpler form rather than emitting a nonsense parenthetical.
        return f"{corrections_count} item(s)"
    siblings = written_count - corrections_count
    return (
        f"{corrections_count} item(s) "
        f"({written_count} corpus rows, including {siblings} cluster sibling(s))"
    )


def _build_confirmation_body(
    *,
    parsed_all_ok: bool,
    applied_lines: list[str],
    written_count: int,
    corrections_count: int,
    unparsed_item_numbers: list[int],
    raw_errors: list[str],
    execution_errors: list[str] | None = None,
    hint: str = "",
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

    2026-05-10 — split parse-shape failures (``unparsed_item_numbers``)
    from execution failures (``execution_errors``). Execution errors
    have informative strings of their own — surface them verbatim
    instead of burying them under the canned "didn't understand" hint.
    ``hint`` is item-type-aware (built by ``_compose_calibration_hint``);
    callers that don't pass one get the empty default.

    2026-05-18 — ``corrections_count`` is N (operator-visible items
    resolved) while ``written_count`` is M (corpus rows written).
    When email cluster fan-out makes M > N, the message surfaces the
    sibling count parenthetically so the operator's reply count matches
    the confirmation. See ``_format_count_with_cluster_expansion``.
    """
    count_phrase = _format_count_with_cluster_expansion(
        corrections_count=corrections_count, written_count=written_count
    )
    # all-ok shortcut stays terse — Andrew already knows what he confirmed.
    if parsed_all_ok:
        if written_count == 0 and not execution_errors:
            return "Calibration: nothing to apply."
        if written_count == 0:
            # All_ok shortcut where every item hit an execution failure
            # (e.g. ✅ on a Daily Sync where vault_path isn't wired). The
            # confirmation summary becomes the error list.
            lines = ["Calibration: confirmed, but none could be applied:"]
            for err in (execution_errors or [])[:5]:
                lines.append(f"  - {err}")
            remaining = max(0, len(execution_errors or []) - 5)
            if remaining > 0:
                lines.append(f"  ... and {remaining} more.")
            return "\n".join(lines)
        head = f"Calibration: confirmed all {count_phrase}."
        if not execution_errors:
            return head
        lines = [head, "Some items couldn't be applied:"]
        for err in execution_errors[:5]:
            lines.append(f"  - {err}")
        remaining = max(0, len(execution_errors) - 5)
        if remaining > 0:
            lines.append(f"  ... and {remaining} more.")
        return "\n".join(lines)

    lines: list[str] = []
    if applied_lines:
        # 2026-05-18 — count phrase replaces bare ``{written_count} correction(s)``
        # so cluster fan-out (M > N) doesn't make the operator miscount.
        if written_count > corrections_count:
            siblings = written_count - corrections_count
            lines.append(
                f"Calibration: applied {corrections_count} correction(s) "
                f"({written_count} corpus rows, including "
                f"{siblings} cluster sibling(s))."
            )
        else:
            lines.append(f"Calibration: applied {corrections_count} correction(s).")
        # Cap at 5 so the reply bubble doesn't get unwieldy on mobile.
        for line in applied_lines[:5]:
            lines.append(f"  {line}")
        remaining = len(applied_lines) - 5
        if remaining > 0:
            lines.append(f"  ... and {remaining} more.")

    if execution_errors:
        # Execution-failure errors carry their own informative string
        # (scope-deny, vault_path not provided, peer dispatch failed,
        # etc.). Surface verbatim so Andrew can react.
        prefix = "Couldn't apply" if lines else "Calibration: couldn't apply"
        lines.append(f"{prefix}:")
        for err in execution_errors[:5]:
            lines.append(f"  - {err}")
        remaining = max(0, len(execution_errors) - 5)
        if remaining > 0:
            lines.append(f"  ... and {remaining} more.")

    if unparsed_item_numbers:
        nums_sorted = sorted(set(unparsed_item_numbers))
        if len(nums_sorted) == 1:
            which = f"item {nums_sorted[0]}"
        else:
            which = "items " + ", ".join(str(n) for n in nums_sorted)
        if lines:
            lines.append(f"Didn't understand {which} — could you restate?{hint}")
        else:
            lines.append(f"Calibration: didn't understand {which} — could you restate?{hint}")
    elif raw_errors and not applied_lines and not execution_errors:
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

    # ``via="duplicate-of-M"`` (Stage 1, 2026-05-15) — when the parser
    # resolved a ``duplicate`` chain, the correction inherits item M's
    # tier/modifier/ok flags. But ``ok=True`` on the inherited
    # correction would resolve against item N's classifier_priority,
    # not item M's — and the operator's intent is "treat item N the
    # same way as item M", which means andrew_priority must equal
    # whatever item M would have produced. We special-case the
    # resolution: look up the source item by number and use ITS
    # classifier_priority as the basis when applying ok/modifier.
    # Explicit new_tier corrections still win as-is (unconditional).
    source_classifier_priority: str | None = None
    if correction.via and correction.via.startswith("duplicate-of-"):
        try:
            source_num = int(correction.via.split("-")[-1])
        except (ValueError, IndexError):
            source_num = -1
        source_item = items_by_num.get(source_num) if source_num > 0 else None
        if source_item is not None:
            source_classifier_priority = str(
                source_item.get("classifier_priority", "")
            ).lower()

    # Resolve the new tier:
    #   - explicit tier wins if set
    #   - else apply modifier ("down"/"up") to classifier_priority
    #     (or to the duplicate-source's classifier_priority when via=duplicate)
    #   - else "ok" — andrew confirms classifier output (source's, for duplicates)
    if correction.new_tier is not None:
        andrew_priority = correction.new_tier
    elif correction.modifier:
        basis = (
            source_classifier_priority
            if source_classifier_priority is not None
            else classifier_priority
        )
        andrew_priority = apply_modifier(basis, correction.modifier)
    elif correction.ok:
        andrew_priority = (
            source_classifier_priority
            if source_classifier_priority is not None
            else classifier_priority
        )
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
            via=correction.via,
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
    instance_name: str = "salem",
    raw_config: dict[str, Any] | None = None,
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

    ``instance_name`` (Phase 1 Pending Items) is the running
    instance's identity (``"salem"``, ``"hypatia"``, ``"kalle"``).
    Used by :func:`_resolve_pending_item_correction` to decide
    whether to resolve locally or dispatch to a peer via
    ``pending_items_resolve``. Default ``"salem"`` matches the
    primary aggregator instance — peer instances should pass their
    own name.

    ``raw_config`` is the pre-loaded unified config dict. When
    supplied (the production bot wiring does this), the pending-item
    helpers skip per-call ``open("config.yaml")`` round-trips. When
    ``None``, helpers fall back to opening the file from cwd —
    legacy / test-friendly path.

    On a match, the result dict carries:
      - ``confirmed_count``: int — how many entries were written
        (sum across email + attribution + proposal + pending)
      - ``unparsed``: list[str] — MIXED bucket of fragments the
        dispatcher couldn't materialize: parse-shape failures AND
        execution failures (scope-deny, vault_path missing, etc.).
        Kept dual-purpose for backward compatibility with existing
        programmatic consumers.
      - ``execution_errors``: list[str] — SUBSET of ``unparsed``
        carrying only the execution-failure strings (2026-05-16
        addition). Always present, always a list, possibly empty —
        consumers can branch without ``KeyError`` defense.
      - ``message``: str — confirmation text to reply with
      - ``all_ok``: bool
      - ``email_count``: int — email rows written
      - ``attribution_count``: int — attribution actions applied
      - ``proposal_count``: int — canonical-proposal actions applied
      - ``pending_count``: int — pending-item resolutions executed
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
    pending_items = _last_batch_pending_items(config)
    pending_by_num = {
        int(i.get("item_number", 0)): i for i in pending_items
    }

    parsed: ReplyParseResult = parse_reply(reply_text)

    email_written = 0  # corpus rows written (M — fans out across cluster siblings)
    email_items_corrected = 0  # email ITEMS resolved (N — one per applied_lines line)
    attribution_written = 0
    proposal_written = 0  # propose-person c2
    pending_written = 0  # Pending Items Queue Phase 1
    applied_lines: list[str] = []  # c3 — one per-item summary line per accepted correction
    errors: list[str] = list(parsed.unparsed)
    unparsed_item_numbers: list[int] = []  # c3 — numeric IDs of items that hit a verb/shape mismatch
    execution_errors: list[str] = []  # 2026-05-10 — informative strings from resolver execution failures
    corpus_path = _attribution_corpus_path(config)
    proposals_queue_path = (
        _canonical_proposals_queue_path(config) if proposal_items else None
    )

    def _bucket_resolver_error(item_number: int, err: str) -> None:
        """Route a resolver's error string to the right user-facing bucket.

        Verb-mismatch errors (the resolver / pre-resolver gate refused
        because the verb doesn't fit this item type) land in
        ``unparsed_item_numbers`` so the user-facing message shows the
        item-type-aware "Tip: ..." hint. Execution failures (scope-deny,
        vault_create exception, peer dispatch failed, queue-path
        unconfigured, etc.) land in ``execution_errors`` so the
        informative error string is surfaced verbatim. See
        ``_is_verb_mismatch_error`` for the discriminator details.
        """
        errors.append(err)
        if _is_verb_mismatch_error(err):
            unparsed_item_numbers.append(item_number)
        else:
            execution_errors.append(err)

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
                email_items_corrected += 1
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
                    try:
                        item_num = int(item.get("item_number", 0))
                    except (TypeError, ValueError):
                        item_num = 0
                    _bucket_resolver_error(
                        item_num,
                        f"item {item.get('item_number')}: vault_path not provided",
                    )
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
                        _bucket_resolver_error(synthetic.item_number, err)
                    elif did_write:
                        attribution_written += 1
                        applied_lines.append(
                            _format_attribution_applied_line(item, action="confirm")
                        )
        if proposal_items:
            if vault_path is None or proposals_queue_path is None:
                for item in proposal_items:
                    try:
                        item_num = int(item.get("item_number", 0))
                    except (TypeError, ValueError):
                        item_num = 0
                    _bucket_resolver_error(
                        item_num,
                        f"item {item.get('item_number')}: "
                        f"{'vault_path' if vault_path is None else 'proposals queue'}"
                        f" not configured",
                    )
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
                        _bucket_resolver_error(synthetic.item_number, err)
                    elif did_write:
                        proposal_written += 1
                        applied_lines.append(
                            _format_proposal_applied_line(item, action="confirm")
                        )
        # Pending Items Queue Phase 1 — all_ok shortcut maps to the
        # ``noted`` resolution on every pending item. ``show me``
        # never fires from a pure-ack token; Andrew only triggers
        # delivery via an explicit per-item reply.
        if pending_items:
            for item in pending_items:
                synthetic = ReplyCorrection(
                    item_number=int(item.get("item_number", 0)),
                    ok=True,
                    consumed_token="noted",
                )
                err, did_resolve, summary = _resolve_pending_item_correction(
                    synthetic, item,
                    self_instance=instance_name,
                    raw_config=raw_config,
                )
                if err is not None:
                    _bucket_resolver_error(synthetic.item_number, err)
                elif did_resolve:
                    pending_written += 1
                    applied_lines.append(
                        _format_pending_item_applied_line(
                            item, resolution_id="noted", summary=summary,
                        )
                    )

    else:
        for correction in parsed.corrections:
            email_item = email_by_num.get(correction.item_number)
            attribution_item = attribution_by_num.get(correction.item_number)
            proposal_item = proposal_by_num.get(correction.item_number)
            pending_item = pending_by_num.get(correction.item_number)

            if email_item is not None:
                # Reject verb makes no sense on an email item.
                if correction.reject:
                    _bucket_resolver_error(
                        correction.item_number,
                        f"item {correction.item_number}: `reject` is "
                        f"only meaningful for attribution items",
                    )
                    continue
                entries, err = _resolve_correction(correction, email_by_num)
                if err is not None:
                    _bucket_resolver_error(correction.item_number, err)
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
                    email_items_corrected += 1
                    applied_lines.append(
                        _format_email_applied_line(
                            email_item,
                            entries[0].andrew_priority,
                            cluster_size=cluster_size,
                        )
                    )
            elif attribution_item is not None:
                if vault_path is None:
                    _bucket_resolver_error(
                        correction.item_number,
                        f"item {correction.item_number}: vault_path not provided",
                    )
                    continue
                err, did_write = _resolve_attribution_correction(
                    correction, attribution_item, vault_path, corpus_path,
                )
                if err is not None:
                    _bucket_resolver_error(correction.item_number, err)
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
                    _bucket_resolver_error(
                        correction.item_number,
                        f"item {correction.item_number}: "
                        f"{'vault_path' if vault_path is None else 'proposals queue'}"
                        f" not configured",
                    )
                    continue
                err, did_write = _resolve_proposal_correction(
                    correction, proposal_item, vault_path, proposals_queue_path,
                    instance_scope=instance_scope,
                )
                if err is not None:
                    _bucket_resolver_error(correction.item_number, err)
                    continue
                if did_write:
                    proposal_written += 1
                    applied_lines.append(
                        _format_proposal_applied_line(
                            proposal_item,
                            action="reject" if correction.reject else "confirm",
                        )
                    )
            elif pending_item is not None:
                # Pending Items Queue Phase 1 — ``noted`` / ``show me``.
                # Reject verbs make no sense here (use ``noted`` for
                # "no action needed"). Tier / modifier likewise.
                if correction.reject:
                    _bucket_resolver_error(
                        correction.item_number,
                        f"item {correction.item_number}: "
                        f"`reject` not meaningful — use `noted` to "
                        f"close without action",
                    )
                    continue
                err, did_resolve, summary = _resolve_pending_item_correction(
                    correction, pending_item,
                    self_instance=instance_name,
                    raw_config=raw_config,
                )
                if err is not None:
                    _bucket_resolver_error(correction.item_number, err)
                    continue
                if did_resolve:
                    pending_written += 1
                    resolution_id = _resolution_id_from_correction(
                        correction, pending_item,
                    ) or "noted"
                    applied_lines.append(
                        _format_pending_item_applied_line(
                            pending_item,
                            resolution_id=resolution_id,
                            summary=summary,
                        )
                    )
            else:
                # No matching item in any of the four batch maps. This
                # is parse-stage "wrong number" — the user typed an item
                # number that wasn't in the batch. Belongs to
                # ``unparsed_item_numbers`` (gets the calibration hint),
                # NOT execution_errors. The error string lacks one of
                # the verb-mismatch markers so we route explicitly here.
                errors.append(
                    f"item {correction.item_number} not in last batch"
                )
                unparsed_item_numbers.append(correction.item_number)

    written_count = (
        email_written + attribution_written + proposal_written + pending_written
    )
    # 2026-05-18 — N (items corrected) vs M (corpus rows written). When
    # an email correction lands on a c5 cluster of size K > 1, the corpus
    # fan-out writes K rows for ONE operator-visible item. ``corrections_count``
    # tracks the operator-visible total (N); ``written_count`` tracks the
    # corpus-row total (M). _build_confirmation_body renders both when
    # they diverge so the operator's count of emails-replied-to matches
    # what the confirmation message says.
    corrections_count = (
        email_items_corrected
        + attribution_written
        + proposal_written
        + pending_written
    )

    # c3 — user-facing body. Per-item summary lines go in (capped at 5
    # so the Telegram reply stays readable on mobile), followed by a
    # human-readable parse-failure sentence with an item-type-aware
    # hint (2026-05-10 — see ``_compose_calibration_hint``). Execution
    # errors are surfaced verbatim instead of being buried under
    # "didn't understand".
    hint = _compose_calibration_hint(
        has_email=bool(email_items),
        has_attribution=bool(attribution_items),
        has_proposal=bool(proposal_items),
        has_pending=bool(pending_items),
    )
    body = _build_confirmation_body(
        parsed_all_ok=parsed.all_ok,
        applied_lines=applied_lines,
        written_count=written_count,
        corrections_count=corrections_count,
        unparsed_item_numbers=unparsed_item_numbers,
        raw_errors=errors,
        execution_errors=execution_errors,
        hint=hint,
    )

    log.info(
        "daily_sync.reply_processed",
        parent_message_id=parent_message_id,
        all_ok=parsed.all_ok,
        email_written=email_written,
        email_items_corrected=email_items_corrected,
        attribution_written=attribution_written,
        proposal_written=proposal_written,
        pending_written=pending_written,
        corrections_count=corrections_count,
        written_count=written_count,
        unparsed=len(errors),
        execution_failures=len(execution_errors),
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
        # 2026-05-18 — ``corrections_count`` exposes the operator-visible
        # item total (N) alongside ``confirmed_count`` (M = corpus rows
        # written). N <= M whenever email cluster fan-out occurs.
        # Programmatic consumers (n8n hooks, dashboards) can use whichever
        # framing they need without re-counting via ``email_count`` deltas.
        "corrections_count": corrections_count,
        "attribution_count": attribution_written,
        "proposal_count": proposal_written,
        "pending_count": pending_written,
        "unparsed": errors,
        # 2026-05-16 — NOTE-1 closeout. ``unparsed`` is a mixed bucket
        # (both parse-shape failures and execution failures) for
        # backward compatibility with existing programmatic consumers
        # (n8n hooks, dashboards). ``execution_errors`` is the
        # additive SIBLING field carrying ONLY the execution-failure
        # subset that ``_bucket_resolver_error`` routed via the
        # ``_is_verb_mismatch_error`` discriminator's execution-error
        # branch (e.g., scope-deny strings from ``vault_create``,
        # ``vault_path`` missing, peer-dispatch failures). Always a
        # list (possibly empty), never missing — consumers can
        # branch on ``result["execution_errors"]`` without
        # ``KeyError`` defensive code.
        "execution_errors": list(execution_errors),
        "message": body,
        "all_ok": parsed.all_ok,
    }
