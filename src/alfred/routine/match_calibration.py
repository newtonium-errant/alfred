"""Self-correcting ``routine_done`` matcher — Phase 1 capture sink.

The fuzzy completion matcher (``_matches_item`` / ``_match_confidence`` in
``routine.cli``) makes a JUDGMENT: does this free-text completion match this
routine item? Per the platform self-correcting-by-design standard
(``feedback_self_correcting_design_standard``), a judgment path must learn from
its mistakes: **capture the correction signal → feed it back → human-approve**.

This module is the **capture** half (Phase 1). When the vault-wide fuzzy match
succeeds with a LOW confidence (below the configured threshold), ``cmd_done``
appends one :class:`PendingMatch` row here — a pending-review queue the Daily
Sync ``routine_match`` section reads each morning and presents for operator
confirm/reject (Phase 2 closes the loop into the learned glossary).

Guardrail (load-bearing): the match path writes ONLY to this PENDING sink. The
learned glossary (the corpus the matcher consults) is mutated ONLY by an
operator reply through the Daily Sync ``reply_dispatch`` — never by a match.
Capturing a pending row is NOT a behavior change: nothing reads this file except
the read-only Daily Sync surface.

Append-only JSONL, per-instance (routine is Salem-only), schema-tolerant load
(the ``from_dict`` known-field filter — the load() contract) so a row written
by a newer/older tool version never crashes the reader.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

# Default capture sink + threshold. Per-instance ``.salem.jsonl`` mirrors the
# existing calibration corpora (``email_calibration.salem.jsonl`` etc.);
# routine + the Daily Sync channel are both Salem-scoped. Operators override
# via the ``routine.match_calibration`` config block. T=0.5 is the Phase 1
# starting floor (GREENLIT Q1) — observability refines it from real traffic.
DEFAULT_PENDING_PATH = "./data/routine_match_pending.salem.jsonl"
DEFAULT_CONFIDENCE_THRESHOLD = 0.5
# Phase 3 (no-match / alias path) min-plausibility floor. When a completion
# matches NOTHING, the closest candidate is only surfaced as a "did you mean…"
# alias suggestion if its ``_match_confidence`` clears this floor — below it the
# suggestion would be noise (a wildly-unrelated item), so we emit the ILB
# "nothing close" signal instead. Tunable via ``routine.match_calibration``.
DEFAULT_NO_MATCH_FLOOR = 0.3
# The learned glossary (Phase 2) — operator-approved confirm/reject/alias rows
# the matcher consults. Mutated ONLY by an operator reply through the Daily Sync
# reply_dispatch; never by a match. Per-instance, mirrors the pending sink.
DEFAULT_CORPUS_PATH = "./data/routine_match_corpus.salem.jsonl"
# Review-surface bounds (see :func:`filter_pending_for_review`). The pending
# sink is append-only and never pruned, so without these a captured row is
# re-surfaced every single morning forever. ``MAX_AGE_DAYS`` retires a row the
# operator has demonstrably chosen not to act on; ``MAX_ITEMS`` caps one day's
# review list so a capture burst can't wall the feed. Both tunable per-instance
# via the ``routine.match_calibration`` config block.
DEFAULT_PENDING_MAX_AGE_DAYS = 21
DEFAULT_PENDING_MAX_ITEMS = 10


@dataclass
class PendingMatch:
    """One low-confidence ``routine_done`` fuzzy match awaiting operator review.

    Captured at match time (``cmd_done`` success branch) when
    ``confidence < threshold``. Read-only until the operator confirms/rejects it
    in the Daily Sync surface (Phase 2).
    """

    query: str  # the operator's free-text completion phrase
    matched_to: str  # the routine item the matcher chose (low_conf) OR the
    # closest "did you mean" candidate (no_match — Phase 3)
    record: str  # the routine record name the item lives on
    confidence: float  # the _match_confidence score at capture time
    completion_date: str = ""  # the date the completion was logged for
    captured_at: str = ""  # ISO timestamp of capture
    # Phase 3: capture KIND. ``"low_conf"`` = a below-threshold match the
    # matcher MADE (confirm/reject → glossary). ``"no_match"`` = nothing
    # matched and ``matched_to`` is the closest candidate suggestion
    # (confirm → alias, reject → suppress). Default ``"low_conf"`` keeps
    # Phase-1/2 rows (written without the field) loading unchanged.
    kind: str = "low_conf"

    @classmethod
    def from_dict(cls, data: dict) -> "PendingMatch":
        """Schema-tolerant construct — filter to known fields (load contract).

        A row written by a different tool version with extra/missing fields
        loads without crashing; unknown keys are dropped, absent keys take
        dataclass defaults. ``query``/``matched_to``/``record`` are required
        (no default) — a row missing them is malformed and skipped by
        :func:`load_pending`.
        """
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def append_pending(path: str | Path, entry: PendingMatch) -> None:
    """Append one pending-match row to the capture JSONL (mkdir parent).

    One write per low-confidence match. The routine CLI is invoked
    per-completion (talker subprocess or operator CLI), so there are no
    concurrent writers to this file within a single completion.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(entry), ensure_ascii=False)
    with p.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_pending(path: str | Path) -> list[PendingMatch]:
    """Load all pending-match rows (schema-tolerant; empty list if absent).

    Malformed rows (bad JSON, or missing a required field) are skipped with a
    warning rather than crashing the reader — the Daily Sync surface must
    degrade gracefully on a partially-corrupt capture file.
    """
    p = Path(path)
    if not p.exists():
        return []
    out: list[PendingMatch] = []
    for raw_line in p.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if not isinstance(data, dict):
                raise ValueError("row is not a JSON object")
            out.append(PendingMatch.from_dict(data))
        except (ValueError, TypeError) as exc:
            log.warning(
                "routine.match_calibration.skip_bad_pending_row",
                path=str(p), error=str(exc),
            )
    return out


# ---------------------------------------------------------------------------
# Phase 2 — the learned glossary (corpus) the matcher consults.
# ---------------------------------------------------------------------------
#
# GUARDRAIL: rows here are written ONLY by an operator reply through the Daily
# Sync ``reply_dispatch`` (confirm/reject/alias) — NEVER by a match. The matcher
# only READS the glossary. An empty glossary ⟹ the matcher behaves exactly as
# before (the consult is purely additive).

# Capture KINDs (PendingMatch.kind / RoutineMatchItem.kind).
KIND_LOW_CONF = "low_conf"  # the matcher made a below-threshold match
KIND_NO_MATCH = "no_match"  # nothing matched; matched_to is the closest candidate

# Corpus row types.
CORPUS_CONFIRM = "match_confirm"  # operator confirmed a low-conf match was right
CORPUS_REJECT = "match_reject"    # operator rejected a match (known-bad pair)
CORPUS_ALIAS = "match_alias"      # operator confirmed a no-match alias (Phase 3)


def query_key(text: str) -> str:
    """Canonical normalised key for a completion phrase.

    casefold + stem + stop-word filter, then sorted token-set joined — so
    "I walked the dog" and "walked the dog" and "dog, walked" all collapse to
    the same key. Used to key glossary lookups so a learned verdict generalises
    across the operator's phrasings of the same completion.

    Lazy-imports the stemmer from ``routine.cli`` (which imports this module) —
    function-level to avoid the import cycle; resolved at call time when both
    modules are loaded.
    """
    from .cli import _FUZZY_STOPWORDS, _fuzzy_stem

    stemmed = _fuzzy_stem(text or "")
    tokens = sorted(
        {t for t in stemmed.split() if t and t not in _FUZZY_STOPWORDS}
    )
    return " ".join(tokens)


@dataclass
class MatchCorpusEntry:
    """One operator-approved glossary row (confirm / reject / alias)."""

    type: str  # CORPUS_CONFIRM | CORPUS_REJECT | CORPUS_ALIAS
    query_key: str  # canonical key (see query_key())
    item_text: str  # the routine item the verdict is about
    record: str = ""  # the routine record the item lives on
    confidence_at_capture: float = 0.0
    action_at: str = ""  # ISO timestamp of the operator action
    note: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "MatchCorpusEntry":
        """Schema-tolerant construct (load contract)."""
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def append_corpus(path: str | Path, entry: MatchCorpusEntry) -> None:
    """Append one operator-approved row to the glossary JSONL (mkdir parent).

    Called ONLY from the Daily Sync reply_dispatch (an operator confirm/reject).
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(entry), ensure_ascii=False)
    with p.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


@dataclass
class Glossary:
    """The matcher-facing view of the corpus — consulted in ``_matches_item``.

    Built by :func:`load_glossary`.

    **Conflict rule: LATER-VERDICT-WINS, per ``(query_key, item_text)`` pair.**
    Replaying the append-only log in order, a later operator action overrides
    an earlier one for that pair — confirm after a mistaken reject, or reject
    after a mistaken confirm/alias. :meth:`verdict` additionally checks
    ``rejected`` first, so an explicit exclusion wins even if a load bug ever
    left a pair in both sets (belt-and-braces, not the primary mechanism).

    A reject ALSO retracts an alias pointing at the rejected item — see
    :func:`load_glossary`. An alias is a claim about the phrase ("clean hammer
    means Fully Clean House"); rejecting that exact pair withdraws the claim.
    A reject of a DIFFERENT item leaves the alias standing.
    """

    confirmed: set[tuple[str, str]]   # (query_key, item_text) → fast-path True
    rejected: set[tuple[str, str]]    # (query_key, item_text) → short-circuit False
    aliases: dict[str, str]           # query_key → aliased item_text (Phase 3)

    def verdict(self, qkey: str, item_text: str) -> str | None:
        """Return ``"confirm"`` / ``"reject"`` for a known pair, else ``None``.

        Reject is checked first so an explicit exclusion always wins over a
        stale confirm for the same pair (defense-in-depth; last-write-wins at
        load already resolves conflicts, but ordering here is belt-and-braces).
        """
        pair = (qkey, item_text)
        if pair in self.rejected:
            return "reject"
        if pair in self.confirmed:
            return "confirm"
        return None

    def alias_for(self, qkey: str) -> str | None:
        """Return the item a query_key was confirmed to alias, or None (Phase 3)."""
        return self.aliases.get(qkey)

    def is_empty(self) -> bool:
        return not (self.confirmed or self.rejected or self.aliases)


def load_glossary(path: str | Path) -> Glossary:
    """Load the corpus JSONL into a :class:`Glossary` (empty if absent).

    Last-write-wins per pair: replaying the append-only log in order, a confirm
    clears any prior reject for the same pair and vice-versa. Malformed rows are
    skipped with a warning (graceful degradation)."""
    confirmed: set[tuple[str, str]] = set()
    rejected: set[tuple[str, str]] = set()
    aliases: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return Glossary(confirmed=confirmed, rejected=rejected, aliases=aliases)
    for raw_line in p.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if not isinstance(data, dict):
                raise ValueError("row is not a JSON object")
            entry = MatchCorpusEntry.from_dict(data)
        except (ValueError, TypeError) as exc:
            log.warning(
                "routine.match_calibration.skip_bad_corpus_row",
                path=str(p), error=str(exc),
            )
            continue
        pair = (entry.query_key, entry.item_text)
        if entry.type == CORPUS_CONFIRM:
            rejected.discard(pair)
            confirmed.add(pair)
        elif entry.type == CORPUS_REJECT:
            confirmed.discard(pair)
            rejected.add(pair)
            # Retract an alias pointing at the item just rejected. An alias is
            # a claim about the PHRASE ("clean hammer means Fully Clean
            # House"); rejecting that exact pair withdraws it. Without this,
            # the alias outlives the reject: Salem's real corpus holds a
            # 2026-07-31 alias followed by three rejects of the same pair, and
            # ``alias_for`` still returned the aliased item. ``verdict`` was
            # already correct (reject), so the MATCHER never mis-fired — but a
            # stale alias silently resolves OTHER captures of the same phrase.
            # A reject of a DIFFERENT item leaves the alias standing.
            if aliases.get(entry.query_key) == entry.item_text:
                del aliases[entry.query_key]
        elif entry.type == CORPUS_ALIAS:
            # An alias is also a promotion (the phrasing should now match the
            # aliased item) — record both the alias map and the confirmed pair.
            aliases[entry.query_key] = entry.item_text
            rejected.discard(pair)
            confirmed.add(pair)
    return Glossary(confirmed=confirmed, rejected=rejected, aliases=aliases)


@dataclass
class PendingReviewStats:
    """Why :func:`filter_pending_for_review` dropped what it dropped.

    Carried out to the caller so the review surface can log an explicit
    "captured N, surfaced M, suppressed K because …" line. Silent
    suppression would make "the card finally stopped coming back" and
    "the section broke" look identical (ILB).
    """

    captured: int = 0   # rows in the sink
    resolved: int = 0   # already carry an operator verdict in the corpus
    aged_out: int = 0   # older than max_age_days
    capped: int = 0     # trimmed by max_items
    surfaced: int = 0   # what the operator actually sees

    def suppressed(self) -> int:
        return self.resolved + self.aged_out + self.capped


def _captured_date(entry: PendingMatch) -> date | None:
    """Best-effort capture date for the age filter.

    Prefers ``captured_at`` (ISO timestamp, possibly ``Z``-suffixed), falls
    back to ``completion_date`` (plain ``YYYY-MM-DD``). Returns ``None`` when
    neither parses — the caller then FAILS OPEN and keeps the row rather than
    silently retiring something it couldn't date.
    """
    raw = (entry.captured_at or "").strip()
    if raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except ValueError:
            pass
    raw = (entry.completion_date or "").strip()
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return None


def filter_pending_for_review(
    pending: list[PendingMatch],
    glossary: Glossary,
    *,
    today: date | None = None,
    max_age_days: int = DEFAULT_PENDING_MAX_AGE_DAYS,
    max_items: int = DEFAULT_PENDING_MAX_ITEMS,
) -> tuple[list[PendingMatch], PendingReviewStats]:
    """Narrow the append-only pending sink to what's worth reviewing today.

    THE BUG THIS CLOSES: the sink is append-only and has no prune/resolve API,
    and the surfacing path never consulted the corpus. So an operator reject
    wrote its verdict (which the MATCHER honours — the false positive stops
    recurring as a match) while the REVIEW CARD came back every morning
    forever, and the feed's revival-after-acted resurrected it each time. The
    operator read "reject = suppress" and was right about the matcher and
    wrong about the card. Three drops, in order:

    1. **Resolved** — the corpus already holds a verdict for this row. Either
       a direct ``(query_key, matched_to)`` confirm/reject, OR an alias on the
       query_key alone: once the operator has said "that phrase means X",
       re-asking "did you mean Y?" for the same phrase is the groundhog even
       though the pair never matched.
    2. **Aged out** — captured more than ``max_age_days`` ago. USUALLY that
       means the operator saw it and chose not to answer, and asking again
       daily is nagging rather than calibration. It does NOT mean that in
       every case: a row that sat below the ``max_items`` cut every day never
       surfaced at all and still ages out, unseen. The counts distinguish the
       two only in aggregate (``aged_out`` vs ``capped``), not per row — see
       the caller's ILB log, and the starvation note on the cap below.
       Undateable rows fail OPEN (kept) — never silently retire what we
       couldn't date.
    3. **Capped** — at most ``max_items`` rows, **FIFO: the OLDEST unresolved
       rows win** (a selection rule, not a render order — the kept rows keep
       their original file order). A capture burst can't wall the review
       surface, and nothing is starved out of existence.

       FIFO is the point, not an implementation detail. Selecting the NEWEST
       (the original v1 behaviour) starves the oldest rows, and a
       perpetually-starved row eventually ages out **having never been shown
       once** — the system silently deciding on the operator's behalf, which
       fails the CAPTURE half of the self-correcting standard before feedback
       or approval ever enter it. FIFO instead guarantees every row is offered
       at least once before it can expire; its cost is bounded LATENCY (fresh
       captures queue behind a backlog), never loss. Ruled 2026-08-03 after a
       reviewer drove 15 rows at ``max_items=10`` and five aged out unseen.

    Non-destructive by design: the sink is the capture record and stays
    intact; the corpus stays the single source of truth for verdicts. That
    also means this retroactively releases rows already stuck in the loop —
    no migration, no rewrite of operator history.
    """
    today = today or date.today()
    stats = PendingReviewStats(captured=len(pending))
    kept: list[PendingMatch] = []

    for entry in pending:
        qkey = query_key(entry.query)
        if glossary.verdict(qkey, entry.matched_to) is not None:
            stats.resolved += 1
            continue
        if glossary.alias_for(qkey) is not None:
            stats.resolved += 1
            continue
        captured = _captured_date(entry)
        if captured is not None and (today - captured).days > max_age_days:
            stats.aged_out += 1
            continue
        kept.append(entry)

    if max_items >= 0 and len(kept) > max_items:
        stats.capped = len(kept) - max_items
        # FIFO — keep the OLDEST unresolved rows. ``pending`` is in file order,
        # which is capture order, so a plain head-slice is the queue.
        kept = kept[:max_items]

    stats.surfaced = len(kept)
    return kept, stats


__all__ = [
    "DEFAULT_PENDING_PATH",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DEFAULT_NO_MATCH_FLOOR",
    "DEFAULT_CORPUS_PATH",
    "DEFAULT_PENDING_MAX_AGE_DAYS",
    "DEFAULT_PENDING_MAX_ITEMS",
    "KIND_LOW_CONF",
    "KIND_NO_MATCH",
    "CORPUS_CONFIRM",
    "CORPUS_REJECT",
    "CORPUS_ALIAS",
    "Glossary",
    "MatchCorpusEntry",
    "PendingMatch",
    "PendingReviewStats",
    "append_corpus",
    "append_pending",
    "filter_pending_for_review",
    "load_glossary",
    "load_pending",
    "query_key",
]
