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
# The learned glossary (Phase 2) — operator-approved confirm/reject/alias rows
# the matcher consults. Mutated ONLY by an operator reply through the Daily Sync
# reply_dispatch; never by a match. Per-instance, mirrors the pending sink.
DEFAULT_CORPUS_PATH = "./data/routine_match_corpus.salem.jsonl"


@dataclass
class PendingMatch:
    """One low-confidence ``routine_done`` fuzzy match awaiting operator review.

    Captured at match time (``cmd_done`` success branch) when
    ``confidence < threshold``. Read-only until the operator confirms/rejects it
    in the Daily Sync surface (Phase 2).
    """

    query: str  # the operator's free-text completion phrase
    matched_to: str  # the routine item text the matcher chose
    record: str  # the routine record name the item lives on
    confidence: float  # the _match_confidence score at capture time
    completion_date: str = ""  # the date the completion was logged for
    captured_at: str = ""  # ISO timestamp of capture

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

    Built by :func:`load_glossary`. Last-write-wins per ``(query_key,
    item_text)`` pair, so a later operator action overrides an earlier one
    (e.g. confirm after a mistaken reject). ``aliases`` maps a query_key to the
    item it was confirmed to alias (Phase 3) — also last-write-wins.
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
        elif entry.type == CORPUS_ALIAS:
            # An alias is also a promotion (the phrasing should now match the
            # aliased item) — record both the alias map and the confirmed pair.
            aliases[entry.query_key] = entry.item_text
            rejected.discard(pair)
            confirmed.add(pair)
    return Glossary(confirmed=confirmed, rejected=rejected, aliases=aliases)


__all__ = [
    "DEFAULT_PENDING_PATH",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DEFAULT_CORPUS_PATH",
    "CORPUS_CONFIRM",
    "CORPUS_REJECT",
    "CORPUS_ALIAS",
    "Glossary",
    "MatchCorpusEntry",
    "PendingMatch",
    "append_corpus",
    "append_pending",
    "load_glossary",
    "load_pending",
    "query_key",
]
