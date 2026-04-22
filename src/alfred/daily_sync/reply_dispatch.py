"""Resolve a Telegram reply against the persisted Daily Sync batch.

The talker bot calls :func:`handle_daily_sync_reply` from
``handle_message`` BEFORE its inline-command check / session pipeline.
When the reply matches the persisted Daily Sync message_ids, the
parser walks Andrew's terse reply, resolves modifiers ("down"/"up")
against the batch's per-item classifier tier, writes one
:class:`CorpusEntry` per touched item, and returns a confirmation
message to send back. Returns ``None`` when the reply is NOT a Daily
Sync reply — caller falls through to the normal pipeline.

Single source of truth: this module is the only place that converts
Andrew's reply into corpus rows. Slash-command-driven calibration
(``/calibrate`` re-fire) routes through here too once a fresh batch
arrives and Andrew replies to it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog

from .assembler import (
    ReplyCorrection,
    ReplyParseResult,
    apply_modifier,
    parse_reply,
)
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


def reply_targets_daily_sync(
    config: DailySyncConfig,
    parent_message_id: int,
) -> bool:
    """Return True iff ``parent_message_id`` matches the persisted batch."""
    return parent_message_id in _last_batch_message_ids(config)


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
) -> dict[str, Any] | None:
    """Process a Daily Sync reply. Returns a result dict or ``None``.

    Returns ``None`` when the reply isn't aimed at the persisted Daily
    Sync batch — the caller (talker bot) treats ``None`` as "fall
    through to normal pipeline".

    On a match, the result dict carries:
      - ``confirmed_count``: int — how many entries were written
      - ``unparsed``: list[str] — fragments the parser couldn't resolve
      - ``message``: str — confirmation text to reply with
      - ``all_ok``: bool
    """
    if not reply_targets_daily_sync(config, parent_message_id):
        return None

    items = _last_batch_items(config)
    items_by_num = {int(i.get("item_number", 0)): i for i in items}
    parsed: ReplyParseResult = parse_reply(reply_text)

    written_count = 0
    errors: list[str] = list(parsed.unparsed)

    # all_ok shortcut: write a corpus entry per item with andrew_priority ==
    # classifier_priority so the corpus reflects "Andrew confirmed everything
    # in this batch as-classified at <timestamp>".
    if parsed.all_ok:
        for item in items:
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
                written_count += 1
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "daily_sync.corpus_write_failed",
                    record_path=entry.record_path,
                    error=str(exc),
                )

    else:
        for correction in parsed.corrections:
            entry, err = _resolve_correction(correction, items_by_num)
            if err is not None:
                errors.append(err)
                continue
            assert entry is not None
            try:
                append_correction(config.corpus.path, entry)
                written_count += 1
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "daily_sync.corpus_write_failed",
                    record_path=entry.record_path,
                    error=str(exc),
                )

    # Build a terse confirmation reply.
    if parsed.all_ok:
        body = f"Calibration: confirmed all {written_count} item(s)."
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
        written=written_count,
        unparsed=len(errors),
    )

    return {
        "confirmed_count": written_count,
        "unparsed": errors,
        "message": body,
        "all_ok": parsed.all_ok,
    }
