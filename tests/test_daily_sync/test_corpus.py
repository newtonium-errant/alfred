"""Tests for the calibration corpus (append-only JSONL).

Covers:
- append_correction creates the file and parent dir.
- iter_corrections yields entries in append order.
- iter_corrections tolerates corrupt lines.
- recent_corrections diversifies by tier.
- recent_corrections returns deterministic order (oldest first).
- recent_corrections respects limit on a small + large corpus.
"""

from __future__ import annotations

import json
from pathlib import Path

from alfred.daily_sync.corpus import (
    CorpusEntry,
    append_correction,
    iter_corrections,
    recent_corrections,
)


def _entry(
    *, path: str, c_pri: str, a_pri: str, ts: str = "2026-04-22T00:00:00+00:00"
) -> CorpusEntry:
    return CorpusEntry(
        record_path=path,
        classifier_priority=c_pri,
        classifier_action_hint=None,
        classifier_reason="reason",
        andrew_priority=a_pri,
        andrew_reason="",
        timestamp=ts,
    )


def test_append_creates_file_and_dir(tmp_path: Path):
    target = tmp_path / "subdir" / "corpus.jsonl"
    e = _entry(path="note/A.md", c_pri="medium", a_pri="low")
    append_correction(target, e)
    assert target.exists()
    assert target.parent.is_dir()
    line = target.read_text(encoding="utf-8").strip().splitlines()[0]
    decoded = json.loads(line)
    assert decoded["record_path"] == "note/A.md"
    assert decoded["andrew_priority"] == "low"


def test_iter_returns_append_order(tmp_path: Path):
    target = tmp_path / "corpus.jsonl"
    append_correction(target, _entry(path="note/A.md", c_pri="medium", a_pri="low"))
    append_correction(target, _entry(path="note/B.md", c_pri="high", a_pri="high"))
    append_correction(target, _entry(path="note/C.md", c_pri="low", a_pri="spam"))
    paths = [e.record_path for e in iter_corrections(target)]
    assert paths == ["note/A.md", "note/B.md", "note/C.md"]


def test_iter_skips_corrupt_lines(tmp_path: Path):
    target = tmp_path / "corpus.jsonl"
    append_correction(target, _entry(path="note/A.md", c_pri="medium", a_pri="low"))
    # Inject a corrupt line + a valid one
    with target.open("a", encoding="utf-8") as f:
        f.write("not valid json\n")
        f.write(json.dumps({"record_path": "note/B.md", "classifier_priority": "high", "andrew_priority": "high"}) + "\n")
    paths = [e.record_path for e in iter_corrections(target)]
    assert paths == ["note/A.md", "note/B.md"]


def test_iter_missing_file_returns_empty(tmp_path: Path):
    target = tmp_path / "no_such.jsonl"
    assert list(iter_corrections(target)) == []


def test_recent_corrections_limit(tmp_path: Path):
    """``limit`` caps the returned correction count.

    Contract change 2026-05-31: ``recent_corrections`` returns ONLY
    actual corrections (``andrew_priority != classifier_priority``);
    confirmations are filtered out. Fixture below uses 20 actual
    corrections (``medium→low``); under the new contract all 20
    qualify and limit caps the result to 5."""
    target = tmp_path / "corpus.jsonl"
    for i in range(20):
        # medium → low is an actual correction (priorities differ).
        append_correction(target, _entry(path=f"note/{i}.md", c_pri="medium", a_pri="low"))
    out = recent_corrections(target, limit=5)
    # Returns oldest-first, but only 5 entries
    assert len(out) == 5


def test_recent_corrections_diversifies_by_tier(tmp_path: Path):
    """Diversification keeps each correction tier represented.

    Contract change 2026-05-31: confirmations filtered out, so the
    diversification operates on the corrections-only set. Fixture
    below uses 10 ``medium→low`` corrections (one tier) + one each
    of ``→high``, ``→low``, ``→spam`` to verify the rare tiers
    aren't crowded out by the noisy ``→low`` cluster."""
    target = tmp_path / "corpus.jsonl"
    # 10 medium→low corrections (noisy tier — would dominate without
    # diversification), then 1 high, 1 medium, 1 spam at the end.
    # Each row is c_pri != a_pri so all qualify as corrections.
    for i in range(10):
        append_correction(target, _entry(path=f"note/l{i}.md", c_pri="medium", a_pri="low"))
    append_correction(target, _entry(path="note/h.md", c_pri="medium", a_pri="high"))
    append_correction(target, _entry(path="note/m.md", c_pri="low", a_pri="medium"))
    append_correction(target, _entry(path="note/s.md", c_pri="medium", a_pri="spam"))
    out = recent_corrections(target, limit=4, diversify_by_tier=True)
    tiers = {e.andrew_priority for e in out}
    # All four ``andrew_priority`` tiers represented thanks to
    # diversification — including "low" because the rare-tier first
    # pass takes the newest "low" correction before falling back to
    # newest-first fill.
    assert tiers == {"high", "low", "spam", "medium"}


def test_recent_corrections_no_diversification(tmp_path: Path):
    """``diversify_by_tier=False`` returns the most-recent N
    corrections in append order (oldest-first).

    Contract change 2026-05-31: confirmations filtered out. Fixture
    below uses 5 ``low→spam`` corrections (each is c_pri != a_pri)."""
    target = tmp_path / "corpus.jsonl"
    for i in range(5):
        # low → spam: actual correction, all 5 qualify.
        append_correction(target, _entry(path=f"note/{i}.md", c_pri="low", a_pri="spam"))
    out = recent_corrections(target, limit=3, diversify_by_tier=False)
    # Newest 3 oldest-first
    paths = [e.record_path for e in out]
    assert paths == ["note/2.md", "note/3.md", "note/4.md"]


def test_recent_corrections_empty_corpus(tmp_path: Path):
    target = tmp_path / "no_such.jsonl"
    assert recent_corrections(target, limit=5) == []


def test_recent_corrections_zero_limit(tmp_path: Path):
    target = tmp_path / "corpus.jsonl"
    append_correction(target, _entry(path="note/A.md", c_pri="low", a_pri="low"))
    assert recent_corrections(target, limit=0) == []


# ---------------------------------------------------------------------------
# Filter-to-actual-corrections regression pins (2026-05-31)
#
# Bug fixed 2026-05-31: pre-fix ``recent_corrections`` returned the
# most-recent N ENTRIES of any kind, including confirmations
# (operator-priority == classifier-priority). The classifier prompt
# rendered these as few-shot examples like ``low→low / spam→spam``
# that taught the classifier nothing. Verified against the live
# Salem corpus: 106 entries, 46 corrections — pre-fix few-shot
# window often contained 0/10 actual corrections.
#
# Tests below pin the corrections-only contract.


def test_recent_corrections_filters_out_confirmations(tmp_path: Path):
    """Pre-fix this test would have FAILED — confirmations leaked
    into the returned list.

    Fixture: 5 confirmations (``low→low``) + 3 corrections (mixed
    tiers). Asserts the returned list contains ONLY the 3
    corrections, NONE of the confirmations.
    """
    target = tmp_path / "corpus.jsonl"
    # 5 confirmations interleaved with 3 corrections (chronological
    # order: c1, conf1, c2, conf2, c3, conf3, conf4, conf5).
    append_correction(target, _entry(path="note/c1.md", c_pri="medium", a_pri="low"))
    append_correction(target, _entry(path="note/conf1.md", c_pri="low", a_pri="low"))
    append_correction(target, _entry(path="note/c2.md", c_pri="medium", a_pri="high"))
    append_correction(target, _entry(path="note/conf2.md", c_pri="low", a_pri="low"))
    append_correction(target, _entry(path="note/c3.md", c_pri="medium", a_pri="spam"))
    append_correction(target, _entry(path="note/conf3.md", c_pri="spam", a_pri="spam"))
    append_correction(target, _entry(path="note/conf4.md", c_pri="medium", a_pri="medium"))
    append_correction(target, _entry(path="note/conf5.md", c_pri="high", a_pri="high"))

    out = recent_corrections(target, limit=10)
    paths = {e.record_path for e in out}
    # Only the three c* entries qualify; all five conf* must be
    # filtered out.
    assert paths == {"note/c1.md", "note/c2.md", "note/c3.md"}
    # Sanity: every returned entry IS a correction.
    assert all(e.is_correction() for e in out)


def test_recent_corrections_walks_full_corpus_for_corrections(tmp_path: Path):
    """Regression pin for the window-size starve case.

    Fixture: 5 corrections at the HEAD of the corpus + 100
    confirmations at the TAIL. Pre-fix the function only looked at
    the last ``limit * 4`` entries (40 for limit=10) — the 5
    corrections at the head would be invisible, the function would
    return 0 results despite the corpus having corrections.

    Post-fix (dispatch option (a) — walk the full corpus), all 5
    corrections must be returned regardless of how many
    confirmations follow them in append order.
    """
    target = tmp_path / "corpus.jsonl"
    # 5 corrections at the head.
    for i in range(5):
        append_correction(target, _entry(
            path=f"note/c{i}.md",
            c_pri="medium",
            a_pri="low",
        ))
    # 100 confirmations at the tail (would push corrections out of
    # any fixed-size window proportional to limit).
    for i in range(100):
        append_correction(target, _entry(
            path=f"note/conf{i}.md",
            c_pri="low",
            a_pri="low",
        ))

    out = recent_corrections(target, limit=10)
    # All 5 head corrections returned; none of the tail confirmations.
    paths = {e.record_path for e in out}
    assert paths == {f"note/c{i}.md" for i in range(5)}
    # Sanity: 5 results (limit=10 was the cap but only 5 corrections
    # exist in the fixture corpus).
    assert len(out) == 5


def test_recent_corrections_diversifies_by_tier_within_corrections(
    tmp_path: Path,
):
    """Diversification operates on the corrections-only set —
    rare-tier corrections aren't crowded out by a noisy dominant
    tier.

    Fixture: 10 ``low→spam`` corrections (noisy — would saturate a
    raw newest-first take) + 1 ``medium→high`` correction (rare).
    Asserts that with limit=5 and diversify=True, the rare
    ``medium→high`` correction IS in the returned set.
    """
    target = tmp_path / "corpus.jsonl"
    # 1 rare correction first, then 10 noisy ones at the tail
    # (so a newest-first take WITHOUT diversification would never
    # surface the rare one with limit=5).
    append_correction(target, _entry(
        path="note/rare.md", c_pri="medium", a_pri="high",
    ))
    for i in range(10):
        append_correction(target, _entry(
            path=f"note/noisy{i}.md", c_pri="low", a_pri="spam",
        ))

    out = recent_corrections(target, limit=5, diversify_by_tier=True)
    paths = {e.record_path for e in out}
    # Rare tier surfaces despite being older + being only 1 of 11
    # corrections.
    assert "note/rare.md" in paths, (
        f"Diversification must surface the rare medium→high "
        f"correction; got paths={sorted(paths)!r}"
    )
    # Both tiers represented in the result.
    tiers = {e.andrew_priority for e in out}
    assert "high" in tiers
    assert "spam" in tiers


def test_recent_corrections_chronological_oldest_first(tmp_path: Path):
    """Output order MUST be oldest-first.

    The few-shot prompt reads chronologically (oldest first → newest
    last); reversing this order would scramble the implicit timeline
    operator pattern recognition relies on. The classifier consumer
    in ``email_classifier/classifier.py`` does its OWN
    ``reversed()`` to render newest-first in the prompt, so the
    function's output ordering matters for that pipeline.

    Fixture: 3 corrections with distinct timestamps. Assert the
    returned order matches append-order (oldest-first)."""
    target = tmp_path / "corpus.jsonl"
    append_correction(target, _entry(
        path="note/oldest.md", c_pri="medium", a_pri="low",
        ts="2026-05-29T10:00:00+00:00",
    ))
    append_correction(target, _entry(
        path="note/middle.md", c_pri="medium", a_pri="high",
        ts="2026-05-30T10:00:00+00:00",
    ))
    append_correction(target, _entry(
        path="note/newest.md", c_pri="medium", a_pri="spam",
        ts="2026-05-31T10:00:00+00:00",
    ))

    out = recent_corrections(target, limit=10, diversify_by_tier=False)
    paths = [e.record_path for e in out]
    # Oldest-first order preserved (matches append-order since no
    # diversification reshuffling).
    assert paths == ["note/oldest.md", "note/middle.md", "note/newest.md"]

    # Also verify with diversification on — the three corrections
    # are all different tiers, so all three are picked up in the
    # first-pass tier-diversification loop. The function reverses
    # the chosen list at the end so the output is still oldest-first.
    out_diverse = recent_corrections(target, limit=10, diversify_by_tier=True)
    paths_diverse = [e.record_path for e in out_diverse]
    assert paths_diverse == ["note/oldest.md", "note/middle.md", "note/newest.md"]
