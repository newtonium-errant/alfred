# Eval note fixtures (task #16)

One JSON file per corpus case (`<case_id>.json`) — a committed
`StructuredNote` (the `subjective` / `objective` / `assessment` / `plan` SOAP
shape with `[S#]` `source_spans`, plus `assessment_reasoning_stated`). These are
what the **fixture** note-gen seam (`FixtureNoteGenSeam`) feeds the production
grounding + attribution + render composition, so the scorecard regenerates
LLM-free (no Ollama / torch / network — CI-safe).

## What these represent

**STAY-C reference notes** — faithful extract-not-infer output for each
scripted encounter:

- `t1/t2/t3_*` mirror the **real 2026-07-09 qwen2.5-14b** P0-spike notes
  (`t2_dtc_lbp` is the actual box STT transcript; the qwen note content is ported
  into the current SOAP-claim schema). These anchor the corpus to real model
  behavior.
- The two-party `fab_* / drug_* / mh_* / base_*` fixtures are **authored** to the
  extract-not-infer contract (the model output the on-box `--mode real` run
  captures live).

They are the CLEAN reference: every note grounds cleanly except three legitimate
pertinent-negative review flags (STAY-C's grounding surfacing a negation not
verbatim in the transcript — a review flag, not an inaccuracy). The scoring
*tests* (`tests/test_scribe_eval.py`) exercise the detectors with ADVERSARIAL
notes (injected fabrications / wrong drugs / dropped details) — those live in the
test file, not here, so this fixture set stays the clean baseline.

## Regenerating for real (on-box)

`alfred scribe eval --mode real` scores LIVE qwen output on the box (Ollama +
qwen2.5-14b behind the armed sovereign guard) — that is the authoritative
quality measurement. This fixture set is the repeatable stand-in until that run.

## Numeric-grounding note

Claim numbers must appear (numeric-boundary) in the cited segment or grounding
flags a `number_mismatch`. Fixtures therefore mirror the transcript's numeric
form: digits where the segment has digits (`5 mg`, `40 degrees`), words where it
has words (`six out of ten`). Keep this alignment when editing a fixture or its
case transcript.
