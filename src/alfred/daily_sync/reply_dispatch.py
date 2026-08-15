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

from alfred.routine.match_calibration import (
    CORPUS_ALIAS,
    CORPUS_CONFIRM,
    CORPUS_ONEOFF,
    CORPUS_REJECT,
    KIND_NO_MATCH,
    MatchCorpusEntry,
    append_corpus,
    query_key,
)
from alfred.vault.attribution import (
    confirm_marker,
    parse_audit_entries,
    reject_marker,
)
from alfred.vault.paths import VaultContainmentError, resolve_in_vault

from .assembler import (
    PENDING_ONLY_OK_TOKENS,
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


def _pending_only_verb_refusal(
    correction: ReplyCorrection, kind: str, accepts: str,
) -> str | None:
    """Refuse a pending-queue verb aimed at a ``kind`` that has no such action.

    Returns the operator-facing error, or ``None`` when the verb is fine here.

    WHY THIS IS AT DISPATCH AND NOT IN THE PARSER (#34). ``parse_reply`` sees
    only the reply text — it cannot know that item 3 is a routine match rather
    than a pending item — so every OK-verb collapses to ``ok=True``. The kind is
    known HERE, and ``consumed_token`` survives the collapse, so here is the
    only place the scoping can be honest.

    The leak this closes is small in code and not small in effect: ``3 noted``
    on a routine-match item was indistinguishable from ``3 confirm`` by the time
    it arrived, so it wrote a corpus row teaching the matcher a verdict the
    operator never gave — on an input most likely to be a typo or a
    mis-numbered line. Erroring is the honest outcome: the operator retypes,
    and nothing is learned from a slip.

    Refusals are LOGGED with a reason, per the house rule that a refusal for
    the right cause and one for an unrelated cause must not look alike.
    """
    token = (correction.consumed_token or "").strip().lower()
    if token not in PENDING_ONLY_OK_TOKENS:
        return None
    log.warning(
        f"daily_sync.{kind}.correction_refused",
        item_number=correction.item_number,
        reason="pending_only_verb",
        consumed_token=token,
    )
    return (
        f"item {correction.item_number}: `{token}` resolves a pending item — "
        f"a {kind.replace('_', ' ')} takes {accepts}"
    )


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


def _last_batch_demotion_items(config: DailySyncConfig) -> list[dict[str, Any]]:
    """Return the demotion-proposal items the daemon stashed at fire time.

    Each item carries ``item_number``, ``proposal_id``, ``kind``,
    ``demotion_contests``, ``window_days``, ``threshold``. Empty list on every
    fire that raised no proposal, which is the steady state — the trigger's own
    log line is what says the evaluation happened.
    """
    state = load_state(config.state.path)
    batch = state.get("last_batch") or {}
    items = batch.get("demotion_items") or []
    return [i for i in items if isinstance(i, dict)]


def _last_batch_capture_close_items(
    config: DailySyncConfig,
) -> list[dict[str, Any]]:
    """Return the capture-close items the daemon stashed at fire time.

    Each item carries ``item_number``, ``proposal_id``, ``task_path``,
    ``task_text``, ``evidence_path``, ``evidence_name``, ``score``,
    ``match_source``. Empty list on every fire that raised no proposal, which is
    the steady state — the section's own scan + trigger log lines are what say
    the evaluation happened.

    The evidence fields are read from HERE rather than recomputed against the
    vault, so a confirm applies the question the operator was actually asked. A
    match that moved between the card and the reply would be a different
    question wearing the same item number.
    """
    state = load_state(config.state.path)
    batch = state.get("last_batch") or {}
    items = batch.get("capture_close_items") or []
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


def _last_batch_routine_match_items(config: DailySyncConfig) -> list[dict[str, Any]]:
    """Return the routine-match review items the daemon stashed at fire time.

    Each item carries ``item_number``, ``query``, ``matched_to``,
    ``record``, ``confidence`` (+ ``completion_date`` / ``captured_at``).
    Empty list when the most recent fire had no low-confidence routine
    matches. The reply dispatcher routes a ``confirm`` / ``reject`` verb
    against whichever items list claims the item_number; for routine-match
    items the verdict appends a row to the learned-glossary corpus.
    """
    state = load_state(config.state.path)
    batch = state.get("last_batch") or {}
    items = batch.get("routine_match_items") or []
    return [i for i in items if isinstance(i, dict)]


def _batch_item_numbers(config: DailySyncConfig) -> set[int]:
    """Union of ``item_number`` across every item list in the latest batch.

    Used by the smart-route mistyped-calibration detector (G1) to confirm
    a leading digit refers to a REAL item before nudging — an incidental
    leading digit in ordinary prose (``"2 things I wanted to say"``) whose
    number happens to be in range is NOT enough on its own.
    """
    numbers: set[int] = set()
    for getter in (
        _last_batch_items,
        _last_batch_attribution_items,
        _last_batch_proposal_items,
        _last_batch_demotion_items,
        _last_batch_capture_close_items,
        _last_batch_pending_items,
        _last_batch_routine_match_items,
    ):
        for item in getter(config):
            raw = item.get("item_number")
            if isinstance(raw, bool):
                continue  # bool is an int subclass — never a valid item number
            if isinstance(raw, int):
                numbers.add(raw)
            elif isinstance(raw, str) and raw.isdigit():
                numbers.add(int(raw))
    return numbers


def _batch_type_flags(config: DailySyncConfig) -> dict[str, bool]:
    """Which item types the latest batch carried — drives the calibration hint.

    Mirrors the ``has_*`` arguments :func:`_compose_calibration_hint` takes,
    so the G1 nudge advertises exactly the verbs that apply to THIS batch.
    """
    return {
        "has_email": bool(_last_batch_items(config)),
        "has_attribution": bool(_last_batch_attribution_items(config)),
        "has_proposal": bool(_last_batch_proposal_items(config)),
        "has_demotion": bool(_last_batch_demotion_items(config)),
        "has_capture_close": bool(_last_batch_capture_close_items(config)),
        "has_pending": bool(_last_batch_pending_items(config)),
        "has_routine_match": bool(_last_batch_routine_match_items(config)),
    }


def _applicable_calibration_verbs(flags: dict[str, bool]) -> set[str]:
    """Advertised calibration verbs for the batch's item types.

    Kept in step with what :func:`_compose_calibration_hint` suggests — the
    G1 near-miss detector only nudges when an unrecognized token is a typo
    of a verb the operator was actually told to use for THIS batch, so a
    typo of an inapplicable verb won't false-fire.
    """
    verbs: set[str] = set()
    if (
        flags["has_attribution"] or flags["has_proposal"]
        or flags["has_routine_match"] or flags["has_demotion"]
        or flags["has_capture_close"]
    ):
        verbs.update({"confirm", "reject"})
    if flags["has_pending"]:
        verbs.update({"noted", "show"})
    if flags["has_email"]:
        verbs.update({"same", "ditto", "high", "medium", "low", "spam", "up", "down", "keep"})
    return verbs


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


# G1 (2026-07-30) — mistyped-calibration detector helpers.
#
# A free-standing message routed as a calibration reply but yielding zero
# corrections is EITHER a mistyped calibration verb ("3 confrim") OR ordinary
# prose with an incidental leading digit ("2 things I wanted to say"). The
# first deserves a nudge (ILB — silence is the friction the operator flagged);
# the second must fall through to normal conversation untouched. These helpers
# separate the two with high confidence.

# A leading item number followed by a short remainder. The remainder is
# captured so the detector can size + near-miss it. "item N: <err>" formatted
# unparsed entries (from chain/dup resolution) start with "item" and don't
# match — exactly the entries we want to skip.
_LEADING_ITEM_RE = re.compile(r"^\s*(\d+)\s+(.+)$")


def _osa_distance(a: str, b: str) -> int:
    """Optimal string alignment (restricted Damerau-Levenshtein) distance.

    Insert / delete / substitute AND adjacent transposition each cost 1.
    Transposition matters: the most common calibration typos are letter
    swaps (``"confrim"`` for ``"confirm"``), which plain Levenshtein scores
    as 2 but a human reads as one slip.
    """
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev2: list[int] = []
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(
                cur[j - 1] + 1,      # insertion
                prev[j] + 1,         # deletion
                prev[j - 1] + cost,  # substitution
            )
            if (
                i > 1
                and j > 1
                and a[i - 1] == b[j - 2]
                and a[i - 2] == b[j - 1]
            ):
                cur[j] = min(cur[j], prev2[j - 2] + 1)  # adjacent transposition
        prev2, prev = prev, cur
    return prev[lb]


def _is_calibration_verb_typo(word: str, verbs: set[str]) -> bool:
    """True when ``word`` is a near-miss (likely typo) of an applicable verb.

    ONE edit, and only for words of 4+ characters. Both bounds were looser
    (2 edits for verbs >=5 chars; a 3-char floor) and both admitted ordinary
    English into the nudge path, where a valid leading item number is enough
    to hijack a normal chat reply into a "Tip:" line:

        3-char floor   : "sam"/"spa" -> spam, "lot"/"cup"/"sup" -> low/up
        2-edit budget  : "reset" -> reject, "ditch"/"dirty" -> ditto,
                         "median" -> medium, "nope" -> noted

    Every real typo the feature exists for is ONE edit — a transposition
    ("confrim" -> confirm, "dwon" -> down, "shwo" -> show) or a single
    slip. The second edit bought false positives and no true positives.

    RESIDUAL, stated rather than hidden: words that are genuinely one edit
    from a short verb still match — "slow"/"stow"/"shot" -> show,
    "town"/"dawn" -> down, "keen" -> keep, "notes" -> noted. They are
    indistinguishable from a real typo without a dictionary, and tightening
    further would kill "dwon" -> down, which IS the reported incident this
    nudge was built for. The cost of a residual false positive is one extra
    Tip line on a chat reply; the cost of missing a real typo is the silent
    fall-through G1 exists to close.
    """
    w = word.lower().strip(".,!?:;\"'")
    if len(w) < 4:
        return False
    for verb in verbs:
        if abs(len(w) - len(verb)) > 2:
            continue
        if _osa_distance(w, verb) <= 1:
            return True
    return False


def _detect_mistyped_calibration(reply_text: str, config: DailySyncConfig) -> bool:
    """Distinguish a mistyped calibration reply from ordinary prose.

    Returns True only for the HIGH-CONFIDENCE case (all conditions required):
      * the reply parses to ZERO corrections and is not all-ok (nothing the
        parser recognized), AND
      * some unparsed fragment is ``<n> <token...>`` where ``n`` is a REAL
        item in the latest batch, AND
      * that fragment's remainder is 1-2 words (a verb-shaped token, not
        prose), AND
      * at least one of those words is a near-miss of a verb advertised for
        THIS batch's item types.

    Everything weaker returns False → the caller falls through to normal
    conversation. Never hijacks real chat: long prose fails the 1-2 word
    gate, an out-of-range digit fails the membership gate, and a short
    non-verb ("2 dogs") fails the near-miss gate.
    """
    parsed = parse_reply(reply_text)
    if parsed.all_ok or parsed.corrections:
        return False
    item_numbers = _batch_item_numbers(config)
    if not item_numbers:
        return False
    verbs = _applicable_calibration_verbs(_batch_type_flags(config))
    if not verbs:
        return False
    for fragment in parsed.unparsed:
        m = _LEADING_ITEM_RE.match(fragment)
        if not m:
            continue
        if int(m.group(1)) not in item_numbers:
            continue
        rest_words = m.group(2).split()
        if not 1 <= len(rest_words) <= 2:
            continue
        if any(_is_calibration_verb_typo(w, verbs) for w in rest_words):
            return True
    return False


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
        # No TELEGRAM thread to route to. Don't flip the flag; nothing to flip.
        #
        # This used to read "no batch persisted", which was true while the
        # batch write was itself gated on ``message_ids``. Since 2026-08-15 it
        # is not: a batch persists whenever the fire had items, so a batch can
        # exist with NO ids — a web-only instance (Salem: ``send_batch`` skips
        # with ``telegram_send_skipped``), or a push that failed. The empty
        # answer is still correct and for the sharper reason: there is no
        # Telegram thread a reply could have arrived on. Those fires are acted
        # through the feed/deck instead.
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
    # lands (the calibration window stays OPEN either way below).
    if not result.get("all_ok") and not result.get("confirmed_count"):
        _revert_batch_replied(config)

        # G1 (2026-07-30, ILB) — a free-standing *mistyped* calibration
        # reply ("3 confrim") used to fall through to SILENCE here. When
        # this is a high-confidence mistyped calibration (a real item
        # number + a near-miss of a verb advertised for THIS batch),
        # nudge with the item-type-aware hint instead of going dark. The
        # dispatcher's own body already echoes the raw fragment but omits
        # the hint on this path, so we append it. Weaker shapes (ordinary
        # prose with an incidental leading digit) still fall through
        # untouched — never hijack real chat.
        if _detect_mistyped_calibration(reply_text, config):
            hint = _compose_calibration_hint(**_batch_type_flags(config))
            base_message = result.get("message") or "Calibration: couldn't parse that."
            result["message"] = f"{base_message}{hint}" if hint else base_message
            log.info(
                "daily_sync.smart_route.mistyped_calibration_nudge",
                unparsed=len(result.get("unparsed", [])),
            )
            return result

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


def _routine_match_corpus_path(
    config: DailySyncConfig | None = None,
) -> str | None:
    """Return the learned-glossary corpus path from the routine config.

    The corpus the routine matcher consults lives at
    ``routine.match_calibration.corpus_path``. The dispatcher MUST write
    operator verdicts to that SAME file (the matcher reads it), so we
    resolve it from the routine config rather than duplicating the path
    into the daily_sync config (which would risk an operator-override
    drift: matcher reads the override, dispatcher writes the default).

    Threads ``config.config_path`` so a per-instance daily_sync daemon
    reads ITS OWN config file. ``config=None`` / ``config.config_path is
    None`` fall back to ``"config.yaml"`` for backward compat with test
    fixtures that don't thread the path. Returns ``None`` when the config
    can't be resolved — the dispatcher then buckets a routine-match
    confirm/reject as an execution failure (corpus not writable) rather
    than silently dropping the verdict. Mirrors
    :func:`_canonical_proposals_queue_path`.
    """
    config_path = "config.yaml"
    if config is not None and config.config_path:
        config_path = config.config_path
    try:
        import yaml as _yaml

        from alfred.routine.config import load_from_unified as _load_routine

        with open(config_path, "r", encoding="utf-8") as f:
            raw = _yaml.safe_load(f) or {}
        routine_config = _load_routine(raw)
    except Exception as exc:  # noqa: BLE001
        log.info(
            "daily_sync.routine_match.config_unavailable",
            error=str(exc),
        )
        return None
    return routine_config.match_calibration.corpus_path or None


def _now_iso() -> str:
    """Wall-clock ISO-8601 UTC. Wrapped so tests can monkeypatch."""
    return datetime.now(timezone.utc).isoformat()


def _norm_item_text(value: str) -> str:
    """Normalised form used ONLY to look a chosen correction target up against
    the vault's real routine items (#13).

    Collapses whitespace and casefolds, so a target that came back from a card
    rendered minutes ago still resolves after a trivial edit to the record. The
    corpus row is always written with the VAULT's canonical spelling, never this
    normalised form — glossary pairs key on ``item_text`` verbatim, so writing
    the normalised string would create a pair the matcher never looks up.
    """
    return " ".join((value or "").split()).casefold()


def _resolve_routine_match_correction(
    correction: ReplyCorrection,
    item: dict[str, Any],
    corpus_path: str,
    *,
    vault_path: Path | None = None,
) -> tuple[str | None, bool]:
    """Apply one routine-match confirm/reject (self-correcting matcher).

    Returns ``(error_str_or_None, did_write)``. Routes by the item's KIND:

    ``low_conf`` (Phase 2b — a below-threshold match the matcher MADE):
      * confirm → :data:`CORPUS_CONFIRM` (promote the phrasing to a fast-path
        TRUE the matcher's next vault-wide scan honours).
      * reject → :data:`CORPUS_REJECT` (short-circuit the recurring
        false-positive to FALSE for that ``(query_key, item)`` pair).

    ``no_match`` (Phase 3 — nothing matched; ``matched_to`` is the closest
    "did you mean…" candidate):
      * confirm → :data:`CORPUS_ALIAS` (the phrasing now MATCHES that item —
        the false-NEGATIVE is closed; load_glossary also adds the pair to the
        confirmed set so the matcher's existing verdict consult promotes it).
      * reject → :data:`CORPUS_REJECT` (recorded so the capture path doesn't
        re-ask this suggestion).

    #13 REJECT-WITH-CORRECTION enriches the reject side. A bare reject still
    means "not that item" and nothing more; a reject may additionally carry:

      * ``correction.correction_target`` — the routine item the completion
        ACTUALLY meant. Writes :data:`CORPUS_REJECT` for the proposed pair AND
        :data:`CORPUS_ALIAS` for ``(query_key, chosen item)``, so the NO both
        suppresses the wrong answer and teaches the right one.
      * ``correction.one_off`` — "nothing; that completion was a one-off".
        Writes :data:`CORPUS_REJECT` plus :data:`CORPUS_ONEOFF`, which
        suppresses the PHRASE rather than just the pair.

    NO FREE-TEXT REACHES THE CORPUS. A ``correction_target`` is validated
    against the vault's live active-routine items and the row is written with
    the vault's canonical spelling; an unrecognised target is REFUSED WHOLE —
    nothing is appended, not even the reject. A half-landed correction would be
    the worst outcome available: the card would leave the deck (rejected) while
    the answer the operator supplied was dropped, so the loop would look closed
    and teach nothing.

    "Refused whole" describes the VALIDATION path only, which is where the
    guarantee is needed and where every refusal below lands: those all return
    before any row is appended. It is NOT a claim about the write path. A
    correction can emit two rows (reject + alias, or reject + one-off) and they
    are appended in a loop, so an I/O failure on the second leaves the first on
    disk — a genuinely partial write. Untreated on purpose: the exposure is a
    disk error mid-append, the failure is logged as
    ``corpus_write_failed`` with the row type, and the reject-then-alias order
    means the surviving row is the conservative one (the pair is suppressed;
    the replacement was simply not learned). Making it atomic would mean a
    temp-file rewrite of an append-only corpus, which costs more than the
    failure mode it removes. Every refusal logs
    ``daily_sync.routine_match.correction_refused`` with a ``reason``, because a
    silent-but-correct refusal and a refusal for an unrelated cause (missing
    metadata, wrong verb) are otherwise indistinguishable in the log.

    Modifier / tier verbs make no sense on a routine-match item (they only
    apply to email calibration) — bucketed as an "only accept" unparsed
    string so the dispatcher shows the verb-mismatch hint.

    GUARDRAIL (no-silent-mutation): this is the ONLY path that writes the
    corpus. The match/capture path (``routine.cli.cmd_done``) writes ONLY
    the pending sink, never the corpus. The corpus is operator-reply-only.
    """
    refusal = _pending_only_verb_refusal(
        correction, "routine_match",
        "`confirm`/`keep`/`yes` or `reject`/`delete`/`no`",
    )
    if refusal:
        return refusal, False

    if not (correction.ok or correction.reject):
        return (
            f"item {correction.item_number}: routine matches only "
            f"accept `confirm`/`keep`/`yes` or `reject`/`delete`/`no`",
            False,
        )

    target = (correction.correction_target or "").strip()

    # #13 shape guards, BEFORE any read or write. A correction is an enriched
    # NO; every other combination is contradictory and gets refused rather than
    # silently reinterpreted.
    if target and correction.one_off:
        log.warning(
            "daily_sync.routine_match.correction_refused",
            item_number=correction.item_number,
            reason="target_and_one_off",
            target=target,
        )
        return (
            f"item {correction.item_number}: pick either the item it meant "
            f"or 'one-off', not both",
            False,
        )
    if (target or correction.one_off) and not correction.reject:
        log.warning(
            "daily_sync.routine_match.correction_refused",
            item_number=correction.item_number,
            reason="correction_without_reject",
            target=target,
            one_off=correction.one_off,
        )
        return (
            f"item {correction.item_number}: a correction only applies to a "
            f"reject — confirm means the match was already right",
            False,
        )

    query = str(item.get("query") or "")
    matched_to = str(item.get("matched_to") or "")
    record = str(item.get("record") or "")
    kind = str(item.get("kind") or "low_conf")
    try:
        confidence = float(item.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    if not query or not matched_to:
        return (
            f"item {correction.item_number} routine-match metadata missing",
            False,
        )

    qkey = query_key(query)

    # Resolve the chosen target against the vault BEFORE writing anything, so a
    # refused correction leaves the corpus (and its parent directory) untouched
    # and the card stays on the deck for a retry.
    chosen_text = ""
    chosen_record = ""
    if target:
        if vault_path is None:
            log.warning(
                "daily_sync.routine_match.correction_refused",
                item_number=correction.item_number,
                reason="no_vault_path",
                target=target,
                query=query,
            )
            return (
                f"item {correction.item_number}: vault not configured — "
                f"can't check what you picked against the real routine items",
                False,
            )
        try:
            # Lazy — routine.cli is heavy and imports match_calibration; keeping
            # it function-level mirrors _routine_match_corpus_path's imports and
            # avoids a module-level cycle.
            from alfred.routine.cli import _iter_active_routine_items

            candidates = _iter_active_routine_items(Path(vault_path))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "daily_sync.routine_match.correction_refused",
                item_number=correction.item_number,
                reason="candidates_unavailable",
                target=target,
                error=str(exc),
            )
            return (
                f"item {correction.item_number}: couldn't read the routine "
                f"items to check what you picked",
                False,
            )

        by_norm: dict[str, list[Any]] = {}
        for cand in candidates:
            by_norm.setdefault(_norm_item_text(cand.item_text), []).append(cand)
        hits = by_norm.get(_norm_item_text(target)) or []
        if not hits:
            # THE corpus-poisoning guard. Anything that isn't a live routine
            # item — a typo, a stale card, a hand-crafted request — dies here.
            log.warning(
                "daily_sync.routine_match.correction_refused",
                item_number=correction.item_number,
                reason="target_not_a_routine_item",
                target=target,
                query=query,
                candidates_seen=len(candidates),
            )
            return (
                f"item {correction.item_number}: “{target}” isn't an item on "
                f"any active routine — pick one from the list",
                False,
            )
        # Prefer a hit on the record the suggestion came from when two routines
        # carry the same item text. ``record`` on the row is provenance only —
        # the glossary keys on (query_key, item_text) — but provenance that
        # points at the wrong record is still a lie in the log.
        chosen = next((c for c in hits if c.record_name == record), hits[0])
        chosen_text = chosen.item_text
        chosen_record = chosen.record_name
        if _norm_item_text(chosen_text) == _norm_item_text(matched_to):
            log.warning(
                "daily_sync.routine_match.correction_refused",
                item_number=correction.item_number,
                reason="target_is_the_proposal",
                target=target,
                matched_to=matched_to,
            )
            return (
                f"item {correction.item_number}: that's the item we already "
                f"suggested — confirm it instead of rejecting it",
                False,
            )

    # Pick the corpus row type by (kind, verb). A no_match confirm is an
    # ALIAS (closes a false-negative); everything else is the Phase-2b
    # confirm/reject pair.
    if correction.reject:
        entry_type = CORPUS_REJECT
    elif kind == KIND_NO_MATCH:
        entry_type = CORPUS_ALIAS
    else:
        entry_type = CORPUS_CONFIRM

    action_at = _now_iso()
    rows = [
        MatchCorpusEntry(
            type=entry_type,
            query_key=qkey,
            item_text=matched_to,
            record=record,
            confidence_at_capture=confidence,
            action_at=action_at,
        ),
    ]
    # #13 — the enriched verdict rides as a SECOND row alongside the reject,
    # never instead of it. Two rows, one claim each: a reader that doesn't know
    # the newer type still honours the suppression.
    if chosen_text:
        rows.append(MatchCorpusEntry(
            type=CORPUS_ALIAS,
            query_key=qkey,
            item_text=chosen_text,
            record=chosen_record,
            confidence_at_capture=confidence,
            action_at=action_at,
            note="corrected from a rejected suggestion",
        ))
    elif correction.one_off:
        rows.append(MatchCorpusEntry(
            type=CORPUS_ONEOFF,
            query_key=qkey,
            # Provenance: what we had proposed when the operator said the
            # phrase means nothing. Not a claim about the item.
            item_text=matched_to,
            record=record,
            confidence_at_capture=confidence,
            action_at=action_at,
            note="operator marked the phrase a one-off",
        ))

    for row in rows:
        try:
            append_corpus(corpus_path, row)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "daily_sync.routine_match.corpus_write_failed",
                query=query,
                matched_to=matched_to,
                row_type=row.type,
                error=str(exc),
            )
            return (f"item {correction.item_number}: corpus write failed", False)

    if chosen_text:
        _verdict = "corrected"
    elif correction.one_off:
        _verdict = "one_off"
    elif correction.reject:
        _verdict = "reject"
    elif kind == KIND_NO_MATCH:
        _verdict = "alias"
    else:
        _verdict = "confirm"
    log.info(
        "daily_sync.routine_match.verdict_recorded",
        item_number=correction.item_number,
        verdict=_verdict,
        kind=kind,
        query=query,
        matched_to=matched_to,
        # The RESOLVED path, not the configured one. Without it, "the reject
        # didn't write" and "the reject wrote somewhere I wasn't looking" are
        # indistinguishable from the log — a distinction that cost a full
        # diagnosis cycle on 2026-08-03.
        corpus_path=str(corpus_path),
        query_key=qkey,
        # #13 — the taught answer, and how many rows the verdict actually laid
        # down. "corrected but wrote one row" is a bug shape worth grepping for.
        corrected_to=chosen_text,
        rows_written=len(rows),
    )
    return (None, True)


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

    refusal = _pending_only_verb_refusal(
        correction, "attribution",
        "`confirm`/`keep`/`yes` or `reject`/`delete`/`no`",
    )
    if refusal:
        return refusal, False

    if not (correction.ok or correction.reject):
        # Modifiers / tiers don't apply to attribution items — they
        # only make sense for email calibration. Bucket as unparsed.
        return (
            f"item {correction.item_number}: attribution items only "
            f"accept `confirm`/`keep`/`yes` or `reject`/`delete`/`no`",
            False,
        )

    # Arc #18 M6 containment gate. ``record_path`` reaches here from the
    # persisted ``last_batch`` — written by ``attribution_section`` from a
    # vault walk, so it is trusted-ish, but it round-trips through a JSON state
    # file that nothing re-validates on load. Gate it like any other
    # caller-composed path, and gate it BEFORE the ``.exists()`` probe so a
    # traversal target is never even stat-ed.
    try:
        file_path = resolve_in_vault(
            vault_path, record_path,
            writer="daily_sync.attribution.resolve_correction",
        )
    except VaultContainmentError:
        log.warning(
            "daily_sync.attribution.path_escape_denied",
            record_path=record_path[:200],
            marker_id=marker_id,
        )
        return (
            f"item {correction.item_number}: {record_path} is not a path "
            f"inside the vault",
            False,
        )
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


def _resolve_demotion_correction(
    correction: ReplyCorrection,
    item: dict[str, Any],
    config: DailySyncConfig,
) -> tuple[str | None, bool]:
    """Apply one demotion-proposal confirm/reject. ``(error_or_None, did_write)``.

    CONFIRM writes the persisted tier override AND marks the proposal accepted,
    in that order. The order is load-bearing for the same reason the contest
    dispatcher's is: the override is the thing the operator asked for, and a
    queue marked accepted over a failed override write would leave him told
    "back under review" by a card that is still glance-tier tomorrow. Marking
    the queue is the bookkeeping; if only one of the two can land, it must be
    the override.

    REJECT marks the proposal rejected and writes nothing else. The rejection's
    ``resolved_at`` IS the cooldown clock — see
    :func:`~.demotion_proposals.cooldown_until` — so a reject that failed to
    record its timestamp would re-ask tomorrow off the same evidence, which is
    the specific failure the cooldown exists to prevent. Hence the write is
    checked and a failure is surfaced rather than swallowed.
    """
    from alfred.feed.model import ATTENTION_NEEDS_YOU, MODE_DECIDE

    from .demotion_proposals import (
        STATE_ACCEPTED,
        STATE_REJECTED,
        resolve_proposal,
    )
    from .tier_override import TierOverride, set_override

    proposal_id = str(item.get("proposal_id") or "")
    kind = str(item.get("kind") or "")
    if not proposal_id or not kind:
        return (
            f"item {correction.item_number} demotion proposal metadata missing",
            False,
        )

    refusal = _pending_only_verb_refusal(
        correction, "demotion",
        "`confirm`/`keep`/`yes` or `reject`/`delete`/`no`",
    )
    if refusal:
        return refusal, False

    if not (correction.ok or correction.reject):
        return (
            f"item {correction.item_number}: the attribution-tier proposal only "
            f"accepts `confirm`/`keep`/`yes` or `reject`/`delete`/`no`",
            False,
        )

    attribution = getattr(config, "attribution", None)
    if attribution is None:
        return (f"item {correction.item_number}: attribution not configured", False)
    queue_path = attribution.resolved_demotion_queue_path()
    now_iso = datetime.now(timezone.utc).isoformat()

    if correction.reject:
        try:
            flipped = resolve_proposal(
                queue_path, proposal_id, STATE_REJECTED, resolved_at=now_iso,
            )
        except OSError as exc:
            log.warning(
                "daily_sync.demotion.state_write_failed",
                proposal_id=proposal_id, action="reject", error=str(exc),
            )
            return (f"item {correction.item_number}: queue write failed", False)
        if not flipped:
            log.info(
                "daily_sync.demotion.reject_noop", proposal_id=proposal_id,
                detail="already resolved or no longer in the queue",
            )
            return None, False
        log.info(
            "daily_sync.demotion.rejected", proposal_id=proposal_id, kind=kind,
            cooldown_days=item.get("window_days"),
        )
        return None, True

    # Confirm — the override FIRST, then the bookkeeping.
    try:
        set_override(
            attribution.resolved_tier_override_path(),
            TierOverride(
                kind=kind,
                mode=MODE_DECIDE,
                attention=ATTENTION_NEEDS_YOU,
                approved_at=now_iso,
                reason=(
                    f"{item.get('demotion_contests', '?')} wrong auto-confirms "
                    f"in {item.get('window_days', '?')} days"
                ),
                proposal_id=proposal_id,
            ),
        )
    except OSError as exc:
        log.warning(
            "daily_sync.demotion.override_write_failed",
            proposal_id=proposal_id, kind=kind, error=str(exc),
        )
        return (
            f"item {correction.item_number}: couldn't record the tier change",
            False,
        )

    try:
        resolve_proposal(
            queue_path, proposal_id, STATE_ACCEPTED, resolved_at=now_iso,
        )
    except OSError as exc:
        # The override LANDED, which is what the operator asked for, so this is
        # reported as applied. The cost of the unmarked queue row is one
        # suppressed re-proposal, and the trigger suppresses on
        # ``override_in_force`` anyway — which is why that check exists as well
        # as the pending-one-at-a-time check, rather than instead of it.
        log.warning(
            "daily_sync.demotion.state_write_failed",
            proposal_id=proposal_id, action="confirm", error=str(exc),
            detail="the tier override landed; only the queue row is unmarked",
        )
    log.info(
        "daily_sync.demotion.approved", proposal_id=proposal_id, kind=kind,
        demotion_contests=item.get("demotion_contests"),
    )
    return None, True


def _resolve_capture_close_correction(
    correction: ReplyCorrection,
    item: dict[str, Any],
    config: DailySyncConfig,
    vault_path: Path | None,
    *,
    instance_scope: str = "talker",
) -> tuple[str | None, bool]:
    """Apply one capture-close confirm/reject. ``(error_or_None, did_write)``.

    THE ORDER IS THE DESIGN, and it differs per verb.

    CONFIRM: close the task, THEN write the corpus row, THEN mark the queue.
    The close is what he asked for; everything after it is bookkeeping and
    learning. If the vault write fails nothing else happens and he is told —
    reporting "closed" over a task still sitting ``todo`` is the precise lie
    this feature exists to stop telling. If the corpus append fails afterwards
    the close still stands: the cost is one lost training pair, not a wrong
    state, so it is logged and the resolve continues. If the queue flip fails
    the close still stands too, and the section's already-resolved suppression
    catches the orphaned pending row at the next render.

    REJECT: write the NEGATIVE corpus row, THEN mark the queue rejected. Nothing
    touches the vault — a rejection means the task stays open, which is what it
    already is. Both writes are load-bearing in different ways: the corpus row
    excludes this pair from ever being proposed again, and the queue flip's
    ``resolved_at`` IS the per-task cooldown clock. A reject that failed to
    record its timestamp would re-ask tomorrow off the same evidence, so that
    write is checked and its failure surfaced rather than swallowed.
    """
    from alfred.vault.ops import VaultError, vault_edit

    from .capture_close_match import (
        MatchCorpusEntry,
        VERDICT_CONFIRMED,
        VERDICT_REJECTED,
        append_corpus,
        now_iso,
        query_key,
    )
    from .capture_close_proposals import (
        STATE_ACCEPTED,
        STATE_REJECTED,
        resolve_proposal,
    )

    proposal_id = str(item.get("proposal_id") or "")
    task_path = str(item.get("task_path") or "")
    if not proposal_id or not task_path:
        return (
            f"item {correction.item_number} capture-close metadata missing",
            False,
        )

    refusal = _pending_only_verb_refusal(
        correction, "capture_close",
        "`confirm`/`keep`/`yes` or `reject`/`delete`/`no`",
    )
    if refusal:
        return refusal, False

    if not (correction.ok or correction.reject):
        return (
            f"item {correction.item_number}: the captured-task proposal only "
            f"accepts `confirm`/`keep`/`yes` or `reject`/`delete`/`no`",
            False,
        )

    cc = getattr(config, "capture_close", None)
    if cc is None or not (getattr(cc, "queue_path", "") or "").strip():
        return (
            f"item {correction.item_number}: capture-close queue not configured",
            False,
        )

    task_text = str(item.get("task_text") or "")
    evidence_name = str(item.get("evidence_name") or "")
    corpus_path = (getattr(cc, "corpus_path", "") or "").strip()
    ts = now_iso()

    def _learn(verdict: str) -> None:
        """Append the answered pair. Never fatal — see the docstring."""
        if not corpus_path:
            log.warning(
                "daily_sync.capture_close.corpus_unconfigured",
                proposal_id=proposal_id, verdict=verdict,
                detail="the operator's verdict could not be learned from — the "
                       "same pair may be proposed again",
            )
            return
        try:
            append_corpus(corpus_path, MatchCorpusEntry(
                ts=ts,
                task_key=query_key(task_text),
                task_text=task_text,
                evidence_name=evidence_name,
                verdict=verdict,
                score=float(item.get("score") or 0.0),
            ))
        except OSError as exc:
            log.warning(
                "daily_sync.capture_close.corpus_write_failed",
                proposal_id=proposal_id, verdict=verdict, error=str(exc),
                detail="the verdict was applied but not learned from",
            )

    if correction.reject:
        _learn(VERDICT_REJECTED)
        try:
            flipped = resolve_proposal(
                cc.queue_path, proposal_id, STATE_REJECTED, resolved_at=ts,
            )
        except OSError as exc:
            log.warning(
                "daily_sync.capture_close.state_write_failed",
                proposal_id=proposal_id, action="reject", error=str(exc),
            )
            return (f"item {correction.item_number}: queue write failed", False)
        if not flipped:
            log.info(
                "daily_sync.capture_close.reject_noop", proposal_id=proposal_id,
                detail="already resolved or no longer in the queue",
            )
            return None, False
        log.info(
            "daily_sync.capture_close.rejected", proposal_id=proposal_id,
            task_path=task_path, evidence_path=item.get("evidence_path"),
            cooldown_days=getattr(cc, "window_days", None),
        )
        return None, True

    # Confirm — the close FIRST.
    if vault_path is None:
        return (
            f"item {correction.item_number}: vault_path not provided",
            False,
        )
    try:
        vault_edit(
            vault_path, task_path,
            set_fields={"status": "done"},
            scope=instance_scope,
        )
    except (VaultError, OSError) as exc:
        log.warning(
            "daily_sync.capture_close.close_failed",
            proposal_id=proposal_id, task_path=task_path,
            error_type=exc.__class__.__name__, error=str(exc),
        )
        return (
            f"item {correction.item_number}: couldn't close {task_path} "
            f"({exc})",
            False,
        )

    _learn(VERDICT_CONFIRMED)

    try:
        resolve_proposal(
            cc.queue_path, proposal_id, STATE_ACCEPTED, resolved_at=ts,
        )
    except OSError as exc:
        # The task IS closed, which is what he asked for, so this reports as
        # applied. The orphaned pending row is caught by the section's
        # already-resolved suppression at the next render — which is why that
        # suppression exists as well as the one-per-task check, not instead.
        log.warning(
            "daily_sync.capture_close.state_write_failed",
            proposal_id=proposal_id, action="confirm", error=str(exc),
            detail="the task was closed; only the queue row is unmarked",
        )
    log.info(
        "daily_sync.capture_close.approved", proposal_id=proposal_id,
        task_path=task_path, evidence_path=item.get("evidence_path"),
        score=item.get("score"), match_source=item.get("match_source"),
    )
    return None, True


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

    refusal = _pending_only_verb_refusal(
        correction, "proposal",
        "`confirm`/`keep`/`yes` or `reject`/`delete`/`no`",
    )
    if refusal:
        return refusal, False

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


def _format_demotion_applied_line(
    item: dict[str, Any],
    *,
    action: str,
) -> str:
    """One-liner describing a demotion-proposal confirm/reject.

    The CONFIRM line names the escape hatch. The operator has just changed a
    standing tier with two words, and the sentence that tells him it worked is
    the only natural place to tell him how to undo it — a reversibility that
    lives only in a module docstring is one he does not have.
    """
    item_number = item.get("item_number") or "?"
    kind = str(item.get("kind") or "attribution").strip()
    if action == "reject":
        window = item.get("window_days") or "?"
        return (
            f"Item {item_number}: left {kind} cards as they are — "
            f"I won't ask again for {window} days"
        )
    return (
        f"Item {item_number}: {kind} cards are back under review "
        f"(undo with `alfred tier-override clear {kind}`)"
    )


def _format_capture_close_applied_line(
    item: dict[str, Any],
    *,
    action: str,
) -> str:
    """One-liner describing a capture-close confirm/reject.

    The CONFIRM line names the task it closed, not just the item number. He is
    answering several cards in one reply and the numbers are not memorable; the
    sentence that tells him it worked is the only place he can check that the
    thing that closed is the thing he meant to close.
    """
    item_number = item.get("item_number") or "?"
    task_text = str(item.get("task_text") or item.get("task_path") or "the task")
    if action == "reject":
        return (
            f"Item {item_number}: left \"{task_text}\" open — that wasn't the "
            f"evidence, and I won't offer that pairing again"
        )
    return f"Item {item_number}: closed \"{task_text}\" as done"


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


def _format_routine_match_applied_line(
    item: dict[str, Any],
    *,
    action: str,
) -> str:
    """Return a one-liner describing a routine-match confirm/reject.

    Format (by kind + verb)::

        low_conf confirm: "Item N: \"walk doggo\" → \"Walk dog\" — confirmed (learned)"
        low_conf reject:  "Item N: \"walk doggo\" → \"Walk dog\" — rejected (won't match)"
        no_match confirm: "Item N: \"feed birds\" → \"Feed cat\" — aliased (now matches)"
        no_match reject:  "Item N: \"feed birds\" → \"Feed cat\" — rejected (won't suggest)"
    """
    item_number = item.get("item_number") or "?"
    query = str(item.get("query") or "").strip() or "(unknown)"
    matched_to = str(item.get("matched_to") or "").strip() or "(unknown)"
    is_no_match = str(item.get("kind") or "low_conf") == KIND_NO_MATCH
    if action == "reject":
        tail = "rejected (won't suggest)" if is_no_match else "rejected (won't match)"
    elif is_no_match:
        tail = "aliased (now matches)"
    else:
        tail = "confirmed (learned)"
    return f"Item {item_number}: \"{query}\" → \"{matched_to}\" — {tail}"


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
    has_routine_match: bool = False,
    has_demotion: bool = False,
    has_capture_close: bool = False,
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
      * Attribution / proposal / demotion / capture-close items → ``N confirm`` /
        ``N reject`` (matches what attribution_section.py:357,
        canonical_proposals_section.py:186 and demotion_section's
        ``render_batch`` advertise in the batch message body).
      * Pending items → ``N noted`` / ``N show me``.
      * Mixed → list the applicable verbs.

    Empty batch (none flagged) → empty hint (no actionable verbs to
    suggest). Falls through cleanly without a stray "Tip:" prefix.
    """
    verbs: list[str] = []
    if (
        has_attribution or has_proposal or has_routine_match or has_demotion
        or has_capture_close
    ):
        verbs.append("'N confirm' / 'N reject'")
    if has_pending:
        verbs.append("'N noted' / 'N show me'")
    if has_email:
        # Name the TIER verbs, not just the copy-previous shorthand. The
        # accepted set (``_applicable_calibration_verbs``) is
        # {same, ditto, high, medium, low, spam, up, down, keep} and the
        # near-miss detector fires on all of them — so the reported incident
        # was a typo of "down" answered by a tip that only mentioned "Same"
        # and "Ditto". Telling the operator to use a verb we did not accept a
        # near-miss of is the asymmetry; this closes it for the email lane.
        verbs.append(
            "'N high' / 'N medium' / 'N low' / 'N spam' / "
            "'Same' / 'Ditto' / 'Same as #N'"
        )

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
    unparsed_fragments: list[str],
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

    ``unparsed_fragments`` is parser-orphaned text — input that never
    reached an item number. It renders WHENEVER it is non-empty (#38):
    it used to be a last-resort fallback shown only when nothing else
    had happened, which meant one successful sibling silently swallowed
    the operator's words. Callers must pass ONLY the orphans (the
    parser's own ``unparsed``), never the accumulated error list, which
    also holds a string per bucketed item and would double-report.

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

    if unparsed_fragments:
        # Text the PARSER could not attach to any item number.
        #
        # Rendered UNCONDITIONALLY (#38). This was previously gated on
        # ``not applied_lines and not execution_errors`` — so a SUCCESSFUL
        # SIBLING silently ate it: "5 confirm / 6 correct ..." wrote item 5's
        # corpus row, discarded fragment 6, and replied as if everything
        # landed. The fragment survived only in the programmatic ``unparsed``
        # field, which Telegram never renders. Neither a sibling applying nor a
        # sibling failing to execute says anything about whether THIS text
        # parsed, so neither may suppress it.
        #
        # An independent ``if`` rather than an ``elif`` on
        # ``unparsed_item_numbers`` is safe because the two are disjoint by
        # construction: anything the resolver bucketed to an item number is
        # reported there, and only parser-orphaned text reaches this list. That
        # disjointness is why the caller passes ``parsed.unparsed`` and NOT the
        # accumulated error list — the latter is a superset that also holds a
        # string for every bucketed item, so un-gating it would echo each of
        # those twice.
        prefix = "Couldn't parse" if lines else "Calibration: couldn't parse"
        lines.append(f"{prefix}: {', '.join(unparsed_fragments[:3])}.")
        remaining = len(unparsed_fragments) - 3
        if remaining > 0:
            # The cap used to truncate in silence — the same silent-drop shape
            # one level down.
            lines.append(f"  ... and {remaining} more.")

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

    # Same kind-scoping as the attribution/proposal/routine_match resolvers
    # (#34). On an email item ``noted`` would collapse to "the classifier's
    # priority was right" and write a corpus row on what is almost certainly a
    # mis-aimed pending verb.
    refusal = _pending_only_verb_refusal(
        correction, "email", "a tier, a modifier, or `confirm`/`keep`",
    )
    if refusal:
        return None, refusal

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
      - ``demotion_count``: int — attribution-tier proposals answered
      - ``capture_close_count``: int — capture-born tasks closed as fulfilled
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
    demotion_items = _last_batch_demotion_items(config)
    demotion_by_num = {
        int(i.get("item_number", 0)): i for i in demotion_items
    }
    capture_close_items = _last_batch_capture_close_items(config)
    capture_close_by_num = {
        int(i.get("item_number", 0)): i for i in capture_close_items
    }
    pending_items = _last_batch_pending_items(config)
    pending_by_num = {
        int(i.get("item_number", 0)): i for i in pending_items
    }
    routine_match_items = _last_batch_routine_match_items(config)
    routine_match_by_num = {
        int(i.get("item_number", 0)): i for i in routine_match_items
    }

    parsed: ReplyParseResult = parse_reply(reply_text)

    email_written = 0  # corpus rows written (M — fans out across cluster siblings)
    email_items_corrected = 0  # email ITEMS resolved (N — one per applied_lines line)
    attribution_written = 0
    proposal_written = 0  # propose-person c2
    demotion_written = 0  # #72 — attribution-tier demotion proposals
    capture_close_written = 0  # #64 — capture-born tasks closed as fulfilled
    pending_written = 0  # Pending Items Queue Phase 1
    routine_match_written = 0  # self-correcting matcher Phase 2b — glossary verdicts
    applied_lines: list[str] = []  # c3 — one per-item summary line per accepted correction
    errors: list[str] = list(parsed.unparsed)
    unparsed_item_numbers: list[int] = []  # c3 — numeric IDs of items that hit a verb/shape mismatch
    execution_errors: list[str] = []  # 2026-05-10 — informative strings from resolver execution failures
    corpus_path = _attribution_corpus_path(config)
    proposals_queue_path = (
        _canonical_proposals_queue_path(config) if proposal_items else None
    )
    routine_match_corpus_path = (
        _routine_match_corpus_path(config) if routine_match_items else None
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
        # #72 — the all_ok shortcut deliberately does NOT touch a demotion
        # proposal. A bare "ok" acknowledging a batch of email items must never
        # be read as approving a standing change to a feed tier: the whole point
        # of routing this through propose-then-approve is that the operator says
        # yes TO THIS QUESTION, by its number. An unanswered proposal simply
        # stays pending and is re-rendered tomorrow.
        if demotion_items:
            log.info(
                "daily_sync.demotion.all_ok_skipped",
                count=len(demotion_items),
                detail="a bare ack does not approve a tier change — reply "
                       "`N confirm` to the numbered item",
            )
        # #64 — the all_ok shortcut deliberately does NOT close a captured task,
        # for the same reason it does not approve a tier change, and one more.
        # The evidence here is a FUZZY MATCH: a bare "ok" acknowledging a batch
        # of email items must never be read as agreeing that a promise was kept.
        # A wrong close silently deletes work he still intended to do, and he
        # would find it the way he found the stale task — by accident, later.
        # An unanswered proposal simply stays pending and is re-rendered.
        if capture_close_items:
            log.info(
                "daily_sync.capture_close.all_ok_skipped",
                count=len(capture_close_items),
                detail="a bare ack does not close a captured task — reply "
                       "`N confirm` to the numbered item",
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
        # Self-correcting matcher Phase 2b — ✅ confirms every low-confidence
        # routine match in one shot (each was a CORRECT fuzzy match, so the
        # operator-approved verdict promotes all of them to the glossary).
        # Reject is never an all_ok action.
        if routine_match_items:
            if routine_match_corpus_path is None:
                for item in routine_match_items:
                    try:
                        item_num = int(item.get("item_number", 0))
                    except (TypeError, ValueError):
                        item_num = 0
                    _bucket_resolver_error(
                        item_num,
                        f"item {item.get('item_number')}: routine-match "
                        f"corpus not configured",
                    )
            else:
                for item in routine_match_items:
                    synthetic = ReplyCorrection(
                        item_number=int(item.get("item_number", 0)),
                        ok=True,
                    )
                    err, did_write = _resolve_routine_match_correction(
                        synthetic, item, routine_match_corpus_path,
                        vault_path=vault_path,
                    )
                    if err is not None:
                        _bucket_resolver_error(synthetic.item_number, err)
                    elif did_write:
                        routine_match_written += 1
                        applied_lines.append(
                            _format_routine_match_applied_line(
                                item, action="confirm",
                            )
                        )

    else:
        for correction in parsed.corrections:
            email_item = email_by_num.get(correction.item_number)
            attribution_item = attribution_by_num.get(correction.item_number)
            proposal_item = proposal_by_num.get(correction.item_number)
            demotion_item = demotion_by_num.get(correction.item_number)
            capture_close_item = capture_close_by_num.get(correction.item_number)
            pending_item = pending_by_num.get(correction.item_number)
            routine_match_item = routine_match_by_num.get(correction.item_number)

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
            elif demotion_item is not None:
                err, did_write = _resolve_demotion_correction(
                    correction, demotion_item, config,
                )
                if err is not None:
                    _bucket_resolver_error(correction.item_number, err)
                    continue
                if did_write:
                    demotion_written += 1
                    applied_lines.append(
                        _format_demotion_applied_line(
                            demotion_item,
                            action="reject" if correction.reject else "confirm",
                        )
                    )
            elif capture_close_item is not None:
                # #64 — confirm closes the capture-born task and writes a
                # POSITIVE corpus row; reject writes a NEGATIVE one and starts
                # the per-task cooldown. Both verdicts feed the glossary, which
                # is what makes answering a card change future behaviour rather
                # than just this one card.
                err, did_write = _resolve_capture_close_correction(
                    correction, capture_close_item, config, vault_path,
                    instance_scope=instance_scope,
                )
                if err is not None:
                    _bucket_resolver_error(correction.item_number, err)
                    continue
                if did_write:
                    capture_close_written += 1
                    applied_lines.append(
                        _format_capture_close_applied_line(
                            capture_close_item,
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
            elif routine_match_item is not None:
                # Self-correcting matcher Phase 2b — confirm/reject a
                # low-confidence routine match. Confirm promotes the
                # phrasing in the glossary; reject suppresses the
                # recurring false-positive. Both write the corpus (the
                # ONLY corpus-write path — capture writes pending only).
                if routine_match_corpus_path is None:
                    _bucket_resolver_error(
                        correction.item_number,
                        f"item {correction.item_number}: routine-match "
                        f"corpus not configured",
                    )
                    continue
                err, did_write = _resolve_routine_match_correction(
                    correction, routine_match_item, routine_match_corpus_path,
                    vault_path=vault_path,
                )
                if err is not None:
                    _bucket_resolver_error(correction.item_number, err)
                    continue
                if did_write:
                    routine_match_written += 1
                    applied_lines.append(
                        _format_routine_match_applied_line(
                            routine_match_item,
                            action="reject" if correction.reject else "confirm",
                        )
                    )
            else:
                # No matching item in any of the batch maps. This is
                # parse-stage "wrong number" — the user typed an item
                # number that wasn't in the batch. Belongs to
                # ``unparsed_item_numbers`` (gets the calibration hint),
                # NOT execution_errors. The error string lacks one of
                # the verb-mismatch markers so we route explicitly here.
                errors.append(
                    f"item {correction.item_number} not in last batch"
                )
                unparsed_item_numbers.append(correction.item_number)

    written_count = (
        email_written + attribution_written + proposal_written
        + pending_written + routine_match_written + demotion_written
        + capture_close_written
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
        + routine_match_written
        + demotion_written
        + capture_close_written
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
        has_routine_match=bool(routine_match_items),
        has_demotion=bool(demotion_items),
        has_capture_close=bool(capture_close_items),
    )
    body = _build_confirmation_body(
        parsed_all_ok=parsed.all_ok,
        applied_lines=applied_lines,
        written_count=written_count,
        corrections_count=corrections_count,
        unparsed_item_numbers=unparsed_item_numbers,
        # PARSER-ORPHANED text only — deliberately NOT the accumulated ``errors``
        # list. ``errors`` starts as a copy of this and then also collects a
        # string for every item bucketed into ``unparsed_item_numbers`` /
        # ``execution_errors``, so passing it would report those items twice
        # (#38). ``parsed.unparsed`` is untouched by that accumulation.
        unparsed_fragments=list(parsed.unparsed),
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
        routine_match_written=routine_match_written,
        demotion_written=demotion_written,
        capture_close_written=capture_close_written,
        corrections_count=corrections_count,
        written_count=written_count,
        unparsed=len(errors),
        # #38 — the parser-orphaned subset, counted separately. ``unparsed`` is
        # a mixed bucket (kept as-is for existing consumers); this is the slice
        # that now always reaches the operator's reply.
        unparsed_fragments=len(parsed.unparsed),
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
        "demotion_count": demotion_written,
        "capture_close_count": capture_close_written,
        "pending_count": pending_written,
        "routine_match_count": routine_match_written,
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
