"""Matching an open capture-born task against evidence it was fulfilled (#64).

THE CASE THIS ANSWERS. The operator said in a voice session "I'm going to
attach some screenshots of workout plans"; the capture pipeline correctly
filed ``attach-screenshots-of-workout-plans`` as an open task. The screenshots
then arrived, the plan was ingested, Salem began coaching FROM it the next
day — and the task sat ``todo`` until he found it during manual cleanup. The
promise and its fulfilment were both in the vault; nothing ever compared them.

WHY THE MATCH MUST BE FUZZY. That task's text never string-matches the records
that fulfilled it. "attach screenshots of workout plans" against a record
named for the actual plan shares few tokens and no phrase. A matcher that
demanded exactness would answer "no evidence" on the very case that motivated
the feature, which is the shape of heuristic this codebase rules against.

MIRRORED, NOT IMPORTED. ``routine.match_calibration`` is the house
self-correcting matcher and this follows its shape deliberately —
pending → corpus → glossary → operator verdict, with the same
threshold/floor split. It is NOT imported: its key space is completion-phrase
↔ routine-item, this one is task-text ↔ record-name, and forcing one module
to serve both is how a shared matcher grows per-caller branches until neither
caller can read it.

HOW THIS MATCHER GETS BETTER FROM BEING WRONG (the standard's actual test).
Three parts, all present from the start rather than bolted on:

1. **Capture the signal.** Every proposal the operator answers writes a corpus
   row — a confirm is a POSITIVE pair, a reject a NEGATIVE one. Below-threshold
   near-misses are written to the pending store instead, so the evidence for
   "the threshold is too high" accumulates without spending a card.
2. **Feed it back.** :class:`Glossary` turns answered pairs into future
   behaviour: a rejected pair is never proposed again, and a confirmed pair
   teaches the phrase→record association that raw similarity could not see.
   The workout case is exactly one glossary entry — "workout plans" means the
   records he actually files plans under — supplied once by a human and reused
   forever.
3. **Propose, never self-apply.** Threshold movement is a PROPOSAL built from
   corpus statistics, not an automatic adjustment. A matcher that retunes
   itself silently drifts, and the drift is invisible precisely because the
   thing that would notice is the thing that moved.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: Score at or above which a match is strong enough to spend a card on.
#: Mirrors ``routine.match_calibration.DEFAULT_CONFIDENCE_THRESHOLD``; the two
#: are independent numbers over different key spaces that happen to start at
#: the same value, NOT one constant shared by reference.
DEFAULT_CONFIDENCE_THRESHOLD = 0.5

#: Below this, a pair is not evidence of anything and is dropped entirely.
#: Between the floor and the threshold a pair is a NEAR MISS: recorded to the
#: pending store for review, never surfaced as a card. That band is where the
#: evidence for moving the threshold comes from — without it the only data
#: would be about matches already good enough, which cannot tell you the bar
#: is too high.
DEFAULT_NO_MATCH_FLOOR = 0.3

#: Verdicts an operator answer can carry.
VERDICT_CONFIRMED = "confirmed"
VERDICT_REJECTED = "rejected"

_STOPWORDS = frozenset({
    "a", "an", "the", "to", "of", "for", "and", "or", "in", "on", "at", "by",
    "with", "from", "into", "some", "my", "our", "his", "her", "their", "its",
    "i", "im", "ill", "going", "gonna", "will", "want", "need", "should",
    "this", "that", "these", "those", "it", "is", "are", "be", "been",
    # Contraction FRAGMENTS. Stripping punctuation turns "I'm" into "i m" and
    # "I'll" into "i ll", so the leftover particle survives as a token and
    # pollutes the key — measured: "I'm going to attach…" keyed with a stray
    # "m". These are never meaningful on their own.
    "m", "s", "ve", "re", "ll", "t", "d",
})

_PUNCT_RE = re.compile(r"[^\w\s]+")
_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    if not text:
        return ""
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", text.lower())).strip()


def _stem(token: str) -> str:
    """Crude plural/possessive fold — enough to make ``plans`` and ``plan``
    the same token.

    Deliberately not a real stemmer: the alternative is a dependency or a
    hand-rolled Porter, and this only has to survive the operator's phrasing
    drift between a spoken promise and a filed record name. Measured need:
    without it, "workout plans" and "Workout Plan" keyed differently and the
    motivating case could not match.
    """
    t = token
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 3 and t.endswith("es") and not t.endswith(("ses", "zes")):
        return t[:-2]
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


def _tokens(text: str) -> set[str]:
    """Meaningful, stemmed token set — the unit both the key and the score use."""
    return {
        _stem(t) for t in normalize(text).split()
        if t and t not in _STOPWORDS
    }


def query_key(text: str) -> str:
    """Canonical key for a task's text — the unit a glossary verdict keys on.

    Sorted, de-duplicated, stop-word-filtered, stemmed token set. So "I'm
    going to attach some screenshots of workout plans" and "attach workout
    plan screenshots" collapse to the same key, and a verdict the operator
    gave on one phrasing generalises to the other. Without this the glossary
    would learn a verdict per sentence and effectively never fire twice.
    """
    return " ".join(sorted(_tokens(text)))


#: Damping applied when the whole case for a match is ONE shared token.
#:
#: A record named "Plan" trivially covers any task mentioning plans; at full
#: coverage that scores 1.0 and spends a card on a coincidence. The value is
#: chosen so a perfect single-token overlap lands BETWEEN the floor and the
#: threshold — 1.0 x 0.45 = 0.45, above the 0.3 floor and below the 0.5 bar.
#: So the pair is recorded as a near miss (evidence, reviewable, and one
#: glossary confirm away from scoring 1.0 forever) and never becomes a card.
#:
#: MEASURED, not assumed: at 0.6 the "Plan" case scored 0.600 and WOULD have
#: fired, contradicting the paragraph above it. Keep this strictly below
#: ``DEFAULT_CONFIDENCE_THRESHOLD`` if either number moves.
_SINGLE_TOKEN_DAMP = 0.45


def _similarity(a: str, b: str) -> float:
    """Order-insensitive similarity in 0..1.

    Two components, maxed rather than averaged:

    * ``SequenceMatcher`` over the normalized strings — catches shared
      phrasing and word order.
    * **COVERAGE** of the shorter token set (the Szymkiewicz-Simpson overlap
      coefficient), not Jaccard.

    The coverage choice is the load-bearing one, and it was MEASURED. A record
    name is typically a short subset of a spoken promise: "I'm going to attach
    some screenshots of workout plans" against a record "Louka Workout Plan"
    shares {workout, plan} out of a 5-token union — Jaccard 0.40, which is
    BELOW the 0.5 threshold. The feature would not have fired on the case that
    motivated it. Coverage asks the right question ("is the record's name
    accounted for by the promise?") and scores the same pair 2/3 = 0.67.

    Jaccard punishes the promise for being wordy, which is what promises are.

    THE FALSE-POSITIVE GUARD. Coverage alone would score a one-token record
    name at 1.0 against any task mentioning that word. So full coverage credit
    requires at least TWO shared meaningful tokens; a single shared token is
    damped (:data:`_SINGLE_TOKEN_DAMP`) to land between the floor and the
    threshold. A coincidence becomes evidence, not a question — and one
    glossary confirm promotes a legitimate one-token name to 1.0 forever.
    """
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    seq = difflib.SequenceMatcher(None, na, nb).ratio()

    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return seq
    shared = ta & tb
    if len(shared) >= 2:
        coverage = len(shared) / min(len(ta), len(tb))
        return max(seq, coverage)
    if shared:
        return max(seq, (len(shared) / min(len(ta), len(tb))) * _SINGLE_TOKEN_DAMP)
    return seq


# ---------------------------------------------------------------------------
# The learned corpus + glossary
# ---------------------------------------------------------------------------


@dataclass
class MatchCorpusEntry:
    """One answered pair — the correction signal, appended forever.

    ``verdict`` is the operator's, never the machine's. ``score`` is what the
    matcher thought at the time, kept so a later threshold proposal can ask
    "what score were the ones he rejected?" — which is the whole question.
    """

    ts: str
    task_key: str
    task_text: str
    evidence_name: str
    verdict: str
    score: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MatchCorpusEntry | None":
        if not isinstance(data, dict):
            return None
        known = {f.name for f in fields(cls)}
        try:
            row = cls(**{k: v for k, v in data.items() if k in known})
        except TypeError:
            return None
        if not row.task_key or row.verdict not in (
            VERDICT_CONFIRMED, VERDICT_REJECTED,
        ):
            return None
        return row


def append_corpus(path: str | Path, entry: MatchCorpusEntry) -> None:
    """Append one answered pair. Creates the parent directory when missing."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")


def iter_corpus(path: str | Path) -> list[MatchCorpusEntry]:
    """Every parseable corpus row, in file order.

    A half-corrupt store degrades to "fewer learned pairs", never to a crash:
    this feeds a daily proposal, and a matcher that cannot start because one
    line is malformed is worse than one that has forgotten a verdict.
    """
    target = Path(path)
    if not target.exists():
        return []
    out: list[MatchCorpusEntry] = []
    skipped = 0
    for raw in target.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            skipped += 1
            continue
        row = MatchCorpusEntry.from_dict(data)
        if row is None:
            skipped += 1
            continue
        out.append(row)
    if skipped:
        log.warning(
            "daily_sync.capture_close.corpus_rows_skipped",
            path=str(target), skipped=skipped,
            detail="unparseable corpus rows ignored — learned match verdicts "
                   "may be missing pairs the operator answered",
        )
    return out


@dataclass
class Glossary:
    """The matcher-facing view of the corpus.

    **LATER-VERDICT-WINS per ``(task_key, evidence_name)`` pair.** Replaying
    the append-only log in order, a later answer overrides an earlier one —
    a confirm after a mistaken reject, or the reverse. :meth:`verdict` checks
    ``rejected`` first so an exclusion wins even if a load bug ever left a pair
    in both sets (belt-and-braces, not the mechanism).
    """

    confirmed: set[tuple[str, str]] = field(default_factory=set)
    rejected: set[tuple[str, str]] = field(default_factory=set)

    def verdict(self, task_key: str, evidence_name: str) -> str | None:
        pair = (task_key, normalize(evidence_name))
        if pair in self.rejected:
            return VERDICT_REJECTED
        if pair in self.confirmed:
            return VERDICT_CONFIRMED
        return None


def load_glossary(path: str | Path) -> Glossary:
    """Replay the corpus into the matcher-facing view."""
    g = Glossary()
    for row in iter_corpus(path):
        pair = (row.task_key, normalize(row.evidence_name))
        if row.verdict == VERDICT_CONFIRMED:
            g.rejected.discard(pair)
            g.confirmed.add(pair)
        else:
            g.confirmed.discard(pair)
            g.rejected.add(pair)
    return g


# ---------------------------------------------------------------------------
# Near misses — the evidence that the bar is wrong
# ---------------------------------------------------------------------------


@dataclass
class PendingMatch:
    """A pair that scored between the floor and the threshold.

    Never surfaced as a card. Recorded so that "the threshold is too high" can
    later be argued from data rather than from a hunch — and so a human
    reviewing the store can supply the glossary entry that would have made the
    pair match outright.
    """

    ts: str
    task_path: str
    task_key: str
    task_text: str
    evidence_path: str
    evidence_name: str
    score: float


def append_pending(path: str | Path, entry: PendingMatch) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")


def load_pending(path: str | Path) -> list[dict[str, Any]]:
    """Every parseable near-miss row. Same degrade-don't-crash posture."""
    target = Path(path)
    if not target.exists():
        return []
    out: list[dict[str, Any]] = []
    for raw in target.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchResult:
    """One task-vs-evidence judgement, with why it came out that way."""

    evidence_path: str
    evidence_name: str
    score: float
    #: ``glossary`` when an operator verdict decided it, else ``similarity``.
    source: str


def best_match(
    task_text: str,
    candidates: "list[tuple[str, str]]",
    *,
    glossary: Glossary | None = None,
) -> MatchResult | None:
    """The strongest fulfilment candidate for one task, or ``None``.

    ``candidates`` is ``(rel_path, display_name)``. A glossary CONFIRMED pair
    scores 1.0 and a REJECTED pair is excluded outright — an operator verdict
    outranks similarity in both directions, which is what makes answering a
    card change future behaviour rather than just this one card.
    """
    key = query_key(task_text)
    best: MatchResult | None = None
    for rel_path, name in candidates:
        if glossary is not None:
            v = glossary.verdict(key, name)
            if v == VERDICT_REJECTED:
                continue
            if v == VERDICT_CONFIRMED:
                return MatchResult(rel_path, name, 1.0, "glossary")
        score = _similarity(task_text, name)
        if best is None or score > best.score:
            best = MatchResult(rel_path, name, score, "similarity")
    return best


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
