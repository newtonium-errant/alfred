"""Curated diagnosis lexicon for the #48 inferred-diagnosis post-check.

STATIC + BUNDLED + SOVEREIGN-SAFE — a frozen, in-repo curation. NO external ICD /
SNOMED fetch (a sovereign scribe has no egress), NO semantic NLP (non-deterministic).
The check is a deterministic word-boundary PHRASE match against this set.

CURATION POLICY (locked by design review):
  * ~30-50 PRIMARY-CARE / DSM-5 TEXTBOOK-INFERRABLE labels — conditions a small
    model would fabricate UNSTATED from a symptom/score/treatment pattern (MDD ←
    low mood + PHQ-9 + an SSRI; GAD ← GAD-7; T2DM ← glucose; HTN ← BP; UTI ←
    dysuria). A label the model would never infer unstated does not earn a slot.
  * FULL LABELS preferred. Abbreviations ONLY where PRIMARY-CARE-UNAMBIGUOUS
    (MDD / GAD / COPD / T2DM / HTN / UTI …). Deliberately EXCLUDED abbreviations
    (expansion-only): MS, MI, RA, PD, AD. The FALSE-CLEAR direction is the
    dangerous one — an inferred ``MS`` (multiple sclerosis) must NOT be cleared by
    a cited ``MS`` (morphine sulfate), so ``MS`` is not a matchable form; only
    the full ``multiple sclerosis`` is. Excluding the abbreviation gives a recall
    gap (an abbreviated inferred dx isn't flagged) — the SAFE direction under the
    precision>recall posture (attest is the backstop; the recall gap is what the
    self-correcting increment grows).
  * SCREENING-TOOL-NAME COLLISION (the same false-CLEAR class, generalised):
    EXCLUDE any abbreviation that is a SUBSTRING of a screening-tool / scale name,
    not only abbrevs that collide with another DIAGNOSIS. ``\\bgad\\b`` matches
    INSIDE the instrument name ``GAD-7`` / ``GAD-2`` (the hyphen is a word
    boundary), so a cited "GAD-7 score is 15" segment would be read as the
    DIAGNOSIS being stated and would spuriously CLEAR an inferred GAD fabrication
    (a high-harm false-NEGATIVE on anxiety, the 2nd-most-common inferred dx). So
    ``gad`` is NOT a matchable form; only the full ``generalized/generalised
    anxiety disorder`` is. Reviewer audit (2026-07-11, CORRECTED audit batch 2):
    GAD-7/GAD-2 is NOT the only tool-name collision — ``ckd`` ⊂ ``CKD-EPI`` (the
    standard eGFR estimating equation, on nearly every renal note) and ``gerd`` ⊂
    ``GERD-Q`` (a validated reflux questionnaire) collide the SAME way, so both
    abbrev forms are now DROPPED (full labels kept). PHQ-9 ∌ ``mdd``; the OCD
    instruments (Y-BOCS / OCI-R) embed no dx abbrev. FURTHER CANDIDATE collisions
    flagged for clinical review before any drop (dropping them has a real recall
    cost — these are also the primary way clinicians WRITE the dx): ``ptsd`` ⊂
    ``PC-PTSD-5``, ``adhd`` ⊂ ``ADHD-RS``, ``ibs`` ⊂ ``IBS-SSS``. The RULE stands:
    EXCLUDE an abbrev embedded in its own screening / scale / equation name as the
    set grows.
  * WORD-BOUNDARY PHRASE match, NEVER substring — ``depression`` never matches
    inside ``major depression``; ``\\bMDD\\b`` never matches inside a longer token.

GROWABLE: this frozen set is the phase-1 start. The #48 self-correcting seam
(attest-diff capture) measures the FP/FN rate for a documented later increment
that grows the set from operator-approved overrides.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class DiagnosisEntry:
    """One diagnosis concept: a canonical label + all matchable surface forms
    (canonical + synonyms + PC-unambiguous abbreviations), all lowercased. A
    claim/segment "names" the diagnosis iff ANY form is present (word-boundary)."""

    canonical: str
    forms: frozenset[str]


def _entry(canonical: str, *synonyms: str) -> DiagnosisEntry:
    forms = {canonical.lower()} | {s.lower() for s in synonyms}
    return DiagnosisEntry(canonical=canonical, forms=frozenset(forms))


# The frozen lexicon. Grouped for review; order is not load-bearing.
DIAGNOSIS_LEXICON: tuple[DiagnosisEntry, ...] = (
    # --- mental health (the #48 core surface — SSRI/score-inferred) ---
    _entry("major depressive disorder", "major depression", "mdd"),
    # "gad" abbrev EXCLUDED — it is a substring of the screening tool GAD-7/GAD-2
    # (the hyphen is a word boundary), so a cited "GAD-7 score" would false-CLEAR
    # an inferred GAD. Full labels only (see the SCREENING-TOOL-NAME COLLISION rule).
    _entry("generalized anxiety disorder", "generalised anxiety disorder"),
    _entry("panic disorder"),
    _entry("post-traumatic stress disorder", "posttraumatic stress disorder", "ptsd"),
    _entry("bipolar disorder", "bipolar affective disorder"),
    _entry("attention deficit hyperactivity disorder", "adhd"),
    _entry("obsessive-compulsive disorder", "obsessive compulsive disorder", "ocd"),
    # --- endocrine / metabolic ---
    # reordered "diabetes type 2" + adjectival "diabetic" (a cited "known diabetic
    # on metformin" states the dx — noun-only lexicon false-FLAGGED the noun claim).
    _entry("type 2 diabetes", "type 2 diabetes mellitus", "type ii diabetes", "t2dm",
           "diabetes type 2", "diabetes mellitus type 2", "diabetic"),
    _entry("type 1 diabetes", "type 1 diabetes mellitus", "t1dm", "diabetes type 1"),
    _entry("hypothyroidism", "hypothyroid"),
    _entry("hyperthyroidism", "hyperthyroid"),
    _entry("hypertension", "htn", "hypertensive"),
    _entry("hyperlipidemia", "hyperlipidaemia", "dyslipidemia", "dyslipidaemia"),
    _entry("gout", "gouty"),
    _entry("obesity", "obese"),
    # --- cardio / respiratory ---
    _entry("coronary artery disease", "cad"),
    _entry("congestive heart failure", "heart failure", "chf"),
    _entry("atrial fibrillation", "afib"),
    _entry("myocardial infarction"),                 # abbrev MI EXCLUDED (ambiguous)
    _entry("chronic obstructive pulmonary disease", "copd"),
    _entry("asthma", "asthmatic"),
    _entry("obstructive sleep apnea", "obstructive sleep apnoea", "sleep apnea", "sleep apnoea", "osa"),
    _entry("pneumonia"),
    # --- gastrointestinal ---
    # "gerd" abbrev DROPPED — it is a substring of the screening tool GERD-Q (the
    # hyphen is a word boundary), the SAME false-CLEAR class as GAD⊂GAD-7. Full
    # labels only (see the SCREENING-TOOL-NAME COLLISION rule).
    _entry("gastroesophageal reflux disease", "gastro-oesophageal reflux disease", "acid reflux"),
    _entry("peptic ulcer disease", "pud"),
    _entry("irritable bowel syndrome", "ibs"),
    _entry("celiac disease", "coeliac disease"),
    # --- genitourinary / renal ---
    _entry("urinary tract infection", "uti"),
    # "ckd" abbrev DROPPED — it is a substring of the eGFR equation CKD-EPI (the
    # hyphen is a word boundary), the SAME false-CLEAR class as GAD⊂GAD-7. Full
    # label only (see the SCREENING-TOOL-NAME COLLISION rule).
    _entry("chronic kidney disease"),
    _entry("benign prostatic hyperplasia", "bph"),
    # --- musculoskeletal / neuro (full labels only — RA/MS/PD/AD abbrevs EXCLUDED) ---
    _entry("osteoarthritis"),
    _entry("osteoporosis"),
    _entry("rheumatoid arthritis"),                  # abbrev RA EXCLUDED (ambiguous)
    _entry("migraine"),
    _entry("multiple sclerosis"),                    # abbrev MS EXCLUDED — false-clear by "MS"=morphine
    _entry("parkinson's disease", "parkinsons disease", "parkinson disease"),  # PD EXCLUDED
    _entry("alzheimer's disease", "alzheimers disease", "alzheimer disease"),  # AD EXCLUDED
    # --- hematologic / other ---
    _entry("iron deficiency anemia", "iron deficiency anaemia", "iron-deficiency anemia"),
    _entry("anemia", "anaemia", "anemic", "anaemic"),
)


def normalize_text(text: str) -> str:
    """Lowercase + fold the spelling variance STT/LLM output actually produces,
    so a phrase form matches its clinical variants (case-insensitive by
    construction — forms are lowercased):

      * U+2019 / U+2018 curly apostrophe → ASCII ``'`` — STT/LLM emit curly
        apostrophes by default, which without the fold NEUTER ``parkinson's`` /
        ``alzheimer's`` (the straight-apostrophe forms never match);
      * hyphen → space — a word boundary EITHER way (the screening-tool-name
        collision reasoning above is unaffected: ``gad`` still matches at the
        ``GAD 7`` boundary exactly as it did at the ``GAD-7`` boundary), so
        ``post-traumatic`` and ``post traumatic`` both match the same form;
      * whitespace collapsed so a multi-word form matches across odd spacing.

    Forms are run through this SAME function at match time (``_form_present`` /
    ``form_spans_in``), so a hyphenated lexicon form and a spaced transcript token
    compare equal. (Word ORDER is NOT normalised — reordered variants like
    ``diabetes type 2`` are covered by explicit reordered forms in the lexicon.)"""
    t = (text or "").lower()
    t = t.replace("’", "'").replace("‘", "'")  # curly → ASCII apostrophe
    t = t.replace("-", " ")                               # hyphen → space (still a boundary)
    return re.sub(r"\s+", " ", t)


def _form_present(form: str, normalized_text: str) -> bool:
    """Word-boundary PHRASE presence (NEVER substring). ``form`` is normalised
    through ``normalize_text`` (matching the already-normalised ``normalized_text``)
    so a hyphenated/curly-apostrophe form and its folded transcript token compare
    equal."""
    nf = normalize_text(form)
    return re.search(r"\b" + re.escape(nf) + r"\b", normalized_text) is not None


def entry_present(entry: DiagnosisEntry, text: str) -> bool:
    """True iff ANY surface form of ``entry`` is present (word-boundary) in ``text``."""
    norm = normalize_text(text)
    return any(_form_present(f, norm) for f in entry.forms)


def form_spans_in(entry: DiagnosisEntry, normalized_text: str) -> list[tuple[int, int]]:
    """All ``(start, end)`` spans in ``normalized_text`` (``normalize_text``
    output) where ANY surface form of ``entry`` occurs (word-boundary).

    Used by the inferred-dx hedge-aware clear-check to LOCATE each label
    occurrence so it can inspect the surrounding negation / hedge / attribution
    context (a label present ONLY inside a hedge must not clear an inferred-dx
    flag). ``entry_present`` answers only "is it present"; this answers "where"."""
    spans: list[tuple[int, int]] = []
    for form in entry.forms:
        nf = normalize_text(form)
        for m in re.finditer(r"\b" + re.escape(nf) + r"\b", normalized_text):
            spans.append((m.start(), m.end()))
    return spans


def diagnoses_named_in(text: str) -> list[DiagnosisEntry]:
    """The lexicon entries NAMED (any form, word-boundary) in ``text``. Order
    follows :data:`DIAGNOSIS_LEXICON` (deterministic)."""
    norm = normalize_text(text)
    return [e for e in DIAGNOSIS_LEXICON if any(_form_present(f, norm) for f in e.forms)]
