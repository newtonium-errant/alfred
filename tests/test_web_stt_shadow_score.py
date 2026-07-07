"""Tests for the minimal on-box A/B preference scorer core (the pure
select / blind-pair / preference-write / rewrite / summarize functions). The
interactive ``main`` is a thin stdin wrapper and is not driven here."""

from __future__ import annotations

import json
import random
from pathlib import Path

from alfred.web.stt_shadow_score import (
    blind_pair,
    load_corpus,
    record_preference,
    select_for_scoring,
    summarize,
    write_corpus,
)


def _rec(af, div, noisy, groq="g text", dg="d text", pref=None):
    r = {
        "audio_file": af,
        "divergence": div,
        "groq": {"text": groq},
        "deepgram": {"text": dg},
        "noise": {"noisy": noisy},
    }
    if pref is not None:
        r["operator_preference"] = pref
    return r


def test_load_corpus_skips_blank_and_malformed(tmp_path: Path) -> None:
    p = tmp_path / "corpus.jsonl"
    p.write_text(
        json.dumps(_rec("a.wav", 0.3, True)) + "\n\n"
        + "{not json}\n"
        + json.dumps(_rec("b.wav", 0.1, False)) + "\n"
    )
    recs = load_corpus(p)
    assert [r["audio_file"] for r in recs] == ["a.wav", "b.wav"]


def test_load_corpus_missing_file(tmp_path: Path) -> None:
    assert load_corpus(tmp_path / "nope.jsonl") == []


def test_select_filters_divergent_noisy_unscored() -> None:
    recs = [
        _rec("keep.wav", 0.4, True),                       # divergent + noisy
        _rec("quiet.wav", 0.4, False),                     # not noisy → drop
        _rec("same.wav", 0.0, True),                       # not divergent → drop
        _rec("done.wav", 0.4, True, pref="groq"),          # already scored → drop
    ]
    out = select_for_scoring(recs, min_divergence=0.1)
    assert [r["audio_file"] for r in out] == ["keep.wav"]


def test_select_all_noise_flag_includes_quiet() -> None:
    recs = [_rec("quiet.wav", 0.4, False)]
    assert select_for_scoring(recs, min_divergence=0.1, require_noisy=False)
    assert not select_for_scoring(recs, min_divergence=0.1, require_noisy=True)


def test_select_excludes_records_with_no_text() -> None:
    recs = [_rec("empty.wav", 0.4, True, groq="", dg="")]
    assert select_for_scoring(recs, min_divergence=0.1) == []


def test_blind_pair_randomizes_sides_but_maps_back() -> None:
    rec = _rec("x.wav", 0.5, True, groq="GROQ", dg="DEEP")
    # seed that puts groq on A
    a, b, sides = blind_pair(rec, random.Random(0))
    assert {a, b} == {"GROQ", "DEEP"}
    # sides maps each shown letter back to its true vendor
    assert set(sides.values()) == {"groq", "deepgram"}
    assert sides["A"] == ("groq" if a == "GROQ" else "deepgram")
    assert sides["B"] == ("groq" if b == "GROQ" else "deepgram")


def test_record_preference_resolves_letter_to_vendor() -> None:
    sides = {"A": "groq", "B": "deepgram"}
    assert record_preference({}, "A", sides)["operator_preference"] == "groq"
    assert record_preference({}, "b", sides)["operator_preference"] == "deepgram"
    assert record_preference({}, "tie", sides)["operator_preference"] == "tie"
    assert record_preference({}, "skip", sides)["operator_preference"] == "skip"
    assert record_preference({}, "", sides)["operator_preference"] == "skip"


def test_write_corpus_roundtrips(tmp_path: Path) -> None:
    p = tmp_path / "corpus.jsonl"
    recs = [_rec("a.wav", 0.3, True, pref="groq"), _rec("b.wav", 0.2, False)]
    write_corpus(p, recs)
    back = load_corpus(p)
    assert back[0]["operator_preference"] == "groq"
    assert len(back) == 2


def test_summarize_counts_noisy_decided() -> None:
    recs = [
        _rec("a.wav", 0.4, True, pref="groq"),
        _rec("b.wav", 0.4, True, pref="groq"),
        _rec("c.wav", 0.4, True, pref="deepgram"),
        _rec("d.wav", 0.4, True, pref="tie"),
        _rec("e.wav", 0.4, False, pref="groq"),   # scored but NOT noisy
        _rec("f.wav", 0.4, True),                  # noisy, unscored
    ]
    s = summarize(recs)
    assert s["scored"] == 5
    assert s["noisy_scored"] == 4
    assert s["tally"]["groq"] == 2 and s["tally"]["deepgram"] == 1
    assert s["tally"]["tie"] == 1
    assert s["groq_pct_of_decided"] == round(100 * 2 / 3, 1)
