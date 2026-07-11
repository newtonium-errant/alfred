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
    _entry("generalized anxiety disorder", "generalised anxiety disorder", "gad"),
    _entry("panic disorder"),
    _entry("post-traumatic stress disorder", "posttraumatic stress disorder", "ptsd"),
    _entry("bipolar disorder", "bipolar affective disorder"),
    _entry("attention deficit hyperactivity disorder", "adhd"),
    _entry("obsessive-compulsive disorder", "obsessive compulsive disorder", "ocd"),
    # --- endocrine / metabolic ---
    _entry("type 2 diabetes", "type 2 diabetes mellitus", "type ii diabetes", "t2dm"),
    _entry("type 1 diabetes", "type 1 diabetes mellitus", "t1dm"),
    _entry("hypothyroidism"),
    _entry("hyperthyroidism"),
    _entry("hypertension", "htn"),
    _entry("hyperlipidemia", "hyperlipidaemia", "dyslipidemia", "dyslipidaemia"),
    _entry("gout"),
    _entry("obesity"),
    # --- cardio / respiratory ---
    _entry("coronary artery disease", "cad"),
    _entry("congestive heart failure", "heart failure", "chf"),
    _entry("atrial fibrillation", "afib"),
    _entry("myocardial infarction"),                 # abbrev MI EXCLUDED (ambiguous)
    _entry("chronic obstructive pulmonary disease", "copd"),
    _entry("asthma"),
    _entry("obstructive sleep apnea", "obstructive sleep apnoea", "sleep apnea", "sleep apnoea", "osa"),
    _entry("pneumonia"),
    # --- gastrointestinal ---
    _entry("gastroesophageal reflux disease", "gastro-oesophageal reflux disease", "acid reflux", "gerd"),
    _entry("peptic ulcer disease", "pud"),
    _entry("irritable bowel syndrome", "ibs"),
    _entry("celiac disease", "coeliac disease"),
    # --- genitourinary / renal ---
    _entry("urinary tract infection", "uti"),
    _entry("chronic kidney disease", "ckd"),
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
    _entry("anemia", "anaemia"),
)


def normalize_text(text: str) -> str:
    """Lowercase + collapse whitespace so a multi-word phrase form matches across
    varied spacing (case-insensitive by construction — forms are lowercased)."""
    return re.sub(r"\s+", " ", (text or "").lower())


def _form_present(form: str, normalized_text: str) -> bool:
    """Word-boundary PHRASE presence (NEVER substring). ``form`` is already
    lowercased; ``normalized_text`` is ``normalize_text`` output."""
    return re.search(r"\b" + re.escape(form) + r"\b", normalized_text) is not None


def entry_present(entry: DiagnosisEntry, text: str) -> bool:
    """True iff ANY surface form of ``entry`` is present (word-boundary) in ``text``."""
    norm = normalize_text(text)
    return any(_form_present(f, norm) for f in entry.forms)


def diagnoses_named_in(text: str) -> list[DiagnosisEntry]:
    """The lexicon entries NAMED (any form, word-boundary) in ``text``. Order
    follows :data:`DIAGNOSIS_LEXICON` (deterministic)."""
    norm = normalize_text(text)
    return [e for e in DIAGNOSIS_LEXICON if any(_form_present(f, norm) for f in e.forms)]
