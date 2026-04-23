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
from .confidence import load_state
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


def reply_targets_daily_sync(
    config: DailySyncConfig,
    parent_message_id: int,
) -> bool:
    """Return True iff ``parent_message_id`` matches the persisted batch."""
    return parent_message_id in _last_batch_message_ids(config)


def _attribution_corpus_path(config: DailySyncConfig) -> str:
    """Return the attribution corpus path, falling back to the default.

    Tolerant of older configs that pre-date the ``attribution`` block.
    """
    block = getattr(config, "attribution", None)
    if block is None:
        return "./data/attribution_audit_corpus.jsonl"
    return getattr(block, "corpus_path", "./data/attribution_audit_corpus.jsonl")


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


def _resolve_correction(
    correction: ReplyCorrection,
    items_by_num: dict[int, dict[str, Any]],
) -> tuple[CorpusEntry | None, str | None]:
    """Convert one :class:`ReplyCorrection` into a :class:`CorpusEntry`.

    Returns ``(entry, error)`` — exactly one is non-None. Errors are
    short human-readable strings the caller can echo back to Andrew so
    he knows which fragments couldn't be applied.
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

    entry = CorpusEntry(
        record_path=str(item.get("record_path") or ""),
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
        timestamp=datetime.now(timezone.utc).isoformat(),
        sender=str(item.get("sender") or ""),
        subject=str(item.get("subject") or ""),
        snippet=str(item.get("snippet") or ""),
    )
    return entry, None


def handle_daily_sync_reply(
    config: DailySyncConfig,
    parent_message_id: int,
    reply_text: str,
    *,
    vault_path: Path | None = None,
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

    parsed: ReplyParseResult = parse_reply(reply_text)

    email_written = 0
    attribution_written = 0
    errors: list[str] = list(parsed.unparsed)
    corpus_path = _attribution_corpus_path(config)

    # all_ok shortcut: write an email corpus row per email item AND
    # confirm every attribution item. "✅" means "everything in the
    # entire Daily Sync is good" — both lists.
    if parsed.all_ok:
        for item in email_items:
            classifier_priority = str(item.get("classifier_priority", "")).lower()
            entry = CorpusEntry(
                record_path=str(item.get("record_path") or ""),
                classifier_priority=classifier_priority,
                classifier_action_hint=item.get("classifier_action_hint"),
                classifier_reason=str(item.get("classifier_reason") or ""),
                andrew_priority=classifier_priority,
                andrew_action_hint=None,
                andrew_reason="",
                timestamp=datetime.now(timezone.utc).isoformat(),
                sender=str(item.get("sender") or ""),
                subject=str(item.get("subject") or ""),
                snippet=str(item.get("snippet") or ""),
            )
            try:
                append_correction(config.corpus.path, entry)
                email_written += 1
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "daily_sync.corpus_write_failed",
                    record_path=entry.record_path,
                    error=str(exc),
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
                    elif did_write:
                        attribution_written += 1

    else:
        for correction in parsed.corrections:
            email_item = email_by_num.get(correction.item_number)
            attribution_item = attribution_by_num.get(correction.item_number)

            if email_item is not None:
                # Reject verb makes no sense on an email item.
                if correction.reject:
                    errors.append(
                        f"item {correction.item_number}: `reject` is "
                        f"only meaningful for attribution items"
                    )
                    continue
                entry, err = _resolve_correction(correction, email_by_num)
                if err is not None:
                    errors.append(err)
                    continue
                assert entry is not None
                try:
                    append_correction(config.corpus.path, entry)
                    email_written += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "daily_sync.corpus_write_failed",
                        record_path=entry.record_path,
                        error=str(exc),
                    )
            elif attribution_item is not None:
                if vault_path is None:
                    errors.append(
                        f"item {correction.item_number}: vault_path not provided"
                    )
                    continue
                err, did_write = _resolve_attribution_correction(
                    correction, attribution_item, vault_path, corpus_path,
                )
                if err is not None:
                    errors.append(err)
                    continue
                if did_write:
                    attribution_written += 1
            else:
                errors.append(
                    f"item {correction.item_number} not in last batch"
                )

    written_count = email_written + attribution_written

    # Build a terse confirmation reply.
    if parsed.all_ok:
        body = (
            f"Calibration: confirmed {email_written} email + "
            f"{attribution_written} attribution item(s)."
            if attribution_items
            else f"Calibration: confirmed all {email_written} item(s)."
        )
    elif written_count and errors:
        body = (
            f"Calibration: applied {written_count} correction(s). "
            f"Couldn't parse: {', '.join(errors[:3])}."
        )
    elif written_count:
        body = f"Calibration: applied {written_count} correction(s)."
    elif errors:
        body = f"Calibration: couldn't parse: {', '.join(errors[:3])}."
    else:
        body = "Calibration: nothing to apply."

    log.info(
        "daily_sync.reply_processed",
        parent_message_id=parent_message_id,
        all_ok=parsed.all_ok,
        email_written=email_written,
        attribution_written=attribution_written,
        unparsed=len(errors),
    )

    return {
        "confirmed_count": written_count,
        "email_count": email_written,
        "attribution_count": attribution_written,
        "unparsed": errors,
        "message": body,
        "all_ok": parsed.all_ok,
    }
