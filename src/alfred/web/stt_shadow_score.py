"""Minimal on-box blind A/B preference scorer for the web STT shadow corpus.

DEVIATION (flagged): the scope's preferred home for ``preference`` mode is the
existing ``stt_replay.py`` harness, but that harness lives OUTSIDE the alfred
repo (``aftermath-honeydew-review/teams/honeydew-review/stt_replay.py``), so
extending it can't ride this one alfred commit. Per the scope's explicit
fallback ("if not readily extendable, build a minimal on-box A/B scorer in
alfred and flag that deviation") this is that minimal scorer. It reads the same
``corpus.jsonl`` the shadow writes, so the external harness's ``divergences`` /
``score`` modes still consume the corpus unchanged; this only fills the
``operator_preference`` gap.

The scoring cut (scope §1): iterate the DIVERGENT + NOISY records, show both
transcripts BLIND (randomized which side is Groq), the operator points at the
``.wav`` and picks a side; the winning VENDOR is written back into the record.
Cutting on the noisy subset is load-bearing — if Groq only wins when quiet,
Deepgram is already fine.

The record-selection / blind-pairing / preference-write core is pure and unit-
tested; :func:`main` is a thin stdin/stdout wrapper around it.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any


def load_corpus(corpus_jsonl: Path) -> list[dict]:
    """Read ``corpus.jsonl`` → list of records (skips blank / malformed lines)."""
    records: list[dict] = []
    if not corpus_jsonl.exists():
        return records
    for line in corpus_jsonl.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def select_for_scoring(
    records: list[dict],
    *,
    min_divergence: float = 0.0,
    require_noisy: bool = True,
    only_unscored: bool = True,
) -> list[dict]:
    """The scoring subset: divergent (``divergence >= min_divergence``), noisy
    (``noise.noisy``, when ``require_noisy``), and not-yet-scored (no
    ``operator_preference``, when ``only_unscored``). Records missing a Groq or
    Deepgram text are excluded (nothing to compare)."""
    out: list[dict] = []
    for r in records:
        if only_unscored and r.get("operator_preference"):
            continue
        if float(r.get("divergence", 0.0) or 0.0) < min_divergence:
            continue
        if require_noisy and not (r.get("noise") or {}).get("noisy", False):
            continue
        groq_text = (r.get("groq") or {}).get("text", "")
        dg_text = (r.get("deepgram") or {}).get("text", "")
        if not (groq_text or dg_text):
            continue
        out.append(r)
    return out


def blind_pair(record: dict, rng: random.Random) -> tuple[str, str, dict[str, str]]:
    """Return ``(option_a_text, option_b_text, side_to_vendor)`` with the Groq/
    Deepgram sides randomly assigned to A/B so the operator can't tell which is
    which. ``side_to_vendor`` maps ``"A"``/``"B"`` back to ``"groq"``/``"deepgram"``."""
    groq_text = (record.get("groq") or {}).get("text", "")
    dg_text = (record.get("deepgram") or {}).get("text", "")
    if rng.random() < 0.5:
        return groq_text, dg_text, {"A": "groq", "B": "deepgram"}
    return dg_text, groq_text, {"A": "deepgram", "B": "groq"}


def record_preference(
    record: dict, choice: str, side_to_vendor: dict[str, str],
) -> dict:
    """Write the operator's blind pick into ``record`` (mutates + returns it).

    ``choice`` is ``"A"``/``"B"`` (resolved to the vendor via
    ``side_to_vendor``), or ``"tie"`` / ``"skip"`` recorded literally."""
    c = (choice or "").strip().lower()
    if c in ("a", "b"):
        record["operator_preference"] = side_to_vendor[c.upper()]
    elif c == "tie":
        record["operator_preference"] = "tie"
    else:
        record["operator_preference"] = "skip"
    return record


def write_corpus(corpus_jsonl: Path, records: list[dict]) -> None:
    """Rewrite ``corpus.jsonl`` from ``records`` (atomic .tmp → rename)."""
    tmp = corpus_jsonl.with_suffix(corpus_jsonl.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fout:
        for r in records:
            fout.write(json.dumps(r) + "\n")
    tmp.replace(corpus_jsonl)


def summarize(records: list[dict]) -> dict[str, Any]:
    """Tally scored preferences over the noisy subset for the §6 read-back."""
    scored = [r for r in records if r.get("operator_preference")]
    noisy_scored = [r for r in scored if (r.get("noise") or {}).get("noisy")]
    tally = {"groq": 0, "deepgram": 0, "tie": 0, "skip": 0}
    for r in noisy_scored:
        tally[r["operator_preference"]] = tally.get(r["operator_preference"], 0) + 1
    decided = tally["groq"] + tally["deepgram"]
    return {
        "total_records": len(records),
        "scored": len(scored),
        "noisy_scored": len(noisy_scored),
        "tally": tally,
        "groq_pct_of_decided": round(100 * tally["groq"] / decided, 1) if decided else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus_dir", help="dir containing corpus.jsonl + .wav files")
    parser.add_argument("--min-divergence", type=float, default=0.1)
    parser.add_argument("--all-noise", action="store_true",
                        help="score every divergent clip, not just the noisy subset")
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args(argv)

    corpus_dir = Path(args.corpus_dir)
    corpus_jsonl = corpus_dir / "corpus.jsonl"
    records = load_corpus(corpus_jsonl)
    todo = select_for_scoring(
        records, min_divergence=args.min_divergence,
        require_noisy=not args.all_noise,
    )
    if not todo:
        # Intentionally-left-blank: nothing to score is a real, distinct state.
        print("No unscored divergent"
              + ("+noisy" if not args.all_noise else "")
              + f" records in {corpus_jsonl} (of {len(records)} total). Nothing to do.")
        return 0

    rng = random.Random(args.seed)
    print(f"Scoring {len(todo)} record(s). For each: play the .wav, pick A / B / "
          "tie / skip (q to stop + save).\n")
    for i, record in enumerate(todo, 1):
        a_text, b_text, side_to_vendor = blind_pair(record, rng)
        print(f"[{i}/{len(todo)}] wav: {corpus_dir / record.get('audio_file', '?')}")
        noise = record.get("noise") or {}
        print(f"   divergence={record.get('divergence')} "
              f"noise_floor_ema={noise.get('noise_floor_ema')} noisy={noise.get('noisy')}")
        print(f"   A: {a_text!r}")
        print(f"   B: {b_text!r}")
        choice = input("   pick [A/B/tie/skip/q]: ").strip().lower()
        if choice == "q":
            break
        record_preference(record, choice, side_to_vendor)
        print(f"   -> recorded: {record['operator_preference']}\n")

    write_corpus(corpus_jsonl, records)
    summary = summarize(records)
    print("\nSaved. Summary (noisy subset):")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
