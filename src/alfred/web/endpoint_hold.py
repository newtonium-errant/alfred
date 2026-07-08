"""Adaptive turn-end (endpointing) — the pure content-aware HOLD/COMMIT gate.

Increment 1 of the adaptive-endpointing arc (docs/adaptive_endpointing_scope.md).
Keeps Deepgram's 300ms endpoint as the snappy floor; re-interprets each
``EVENT_UTTERANCE_END`` as a CANDIDATE and runs a pure :func:`classify_tail` on
the accumulated finals-buffer tail. Complete thought → COMMIT in the same tick
(zero added latency, structural). Mid-thought (trailing conjunction / filler /
dangling function word) → the worker arms a bounded concurrent HOLD.

TWO-AGENT SEAM: the prompt-tuner owns the SET CONTENTS of the four module-level
frozensets (:data:`CONJUNCTIONS`, :data:`FILLERS_SINGLE`, :data:`FILLERS_MULTI`,
:data:`DANGLING`) and the decision-RULE wording, delivered as a frozen contract
(``docs/endpoint_signal_contract.md``). This module owns the STRUCTURE:
:class:`TailResult`'s shape, the tokenization, and the rule PLUMBING (the two
prompt-tuner-owned rules — terminal-punct vetoes a weak hold; missing-punct
alone never holds, only escalates a firing signal — are implemented here and
reconciled against the contract's final wording). The frozensets below are the
FINAL QA-cleared calibration (contract §2), swapped in from the conservative
starter set.

Privacy: :func:`classify_tail` is the SINGLE text-touching path — it returns the
decision AND the feature booleans telemetry needs, so raw tail text is inspected
once and never leaves this call (never logged — ``voice_stt.py:50`` contract).
"""

from __future__ import annotations

from dataclasses import dataclass

# Decision constants (the classify_tail verdict).
COMMIT = "commit"
HOLD = "hold"

_TERMINAL_PUNCT = frozenset({".", "?", "!"})
# Trailing closing quotes/brackets stripped from the RAW string BEFORE the
# terminal-punct veto (contract §3.1 rule 2 — so `he said "go."` still vetoes).
_CLOSING_WRAP = "\"')]}»”’"
# Token EDGE strip (contract §3.1 rule 4): leading/trailing punctuation removed,
# INTERNAL apostrophes/hyphens preserved ("don't"→"don't", "twenty-one" intact).
_PUNCT_EDGES = ".,!?;:\"'()[]{}—–-"

# ---------------------------------------------------------------------------
# LEXICON — the FROZEN, QA-cleared ship values from endpoint_signal_contract.md
# §2 (prompt-tuner owns the CONTENTS; this is the swap-in of the reviewed
# contract, no word moves). Conservative by design: every included word is one
# a sentence ENDING on is almost always still going; when in doubt, EXCLUDED.
# Reconcile any future change against the contract §2 — do not edit ad hoc.
# ---------------------------------------------------------------------------

CONJUNCTIONS = frozenset({
    "and", "so", "but", "or", "because", "if", "when", "although", "unless",
    "until",
})

FILLERS_SINGLE = frozenset({
    "um", "umm", "uh", "uhh", "er", "hmm",
})

# Two-token trailing fillers (matched on the last two whitespace tokens).
FILLERS_MULTI = frozenset({
    "let me", "i mean",
})

# Articles / possessive-determiners / the two safest prepositions an utterance
# essentially never ends on. `his`/`her`/`its` excluded (double as pronouns);
# phrasal-particle prepositions (for/with/at/by/in/on…) excluded (§2.3).
DANGLING = frozenset({
    "the", "a", "an", "my", "your", "our", "their", "to", "of",
})


@dataclass(frozen=True)
class TailResult:
    """Output of :func:`classify_tail`. ``decision`` is the primary verdict
    (``COMMIT``/``HOLD``); ``features`` is the per-category boolean dict the
    telemetry sink records (features-only — NEVER the raw tail text)."""

    decision: str
    signal_category: str | None   # "conjunction"|"filler"|"dangling"|None
    features: dict


@dataclass(frozen=True)
class EndpointHoldSettings:
    """Mount-normalized adaptive-endpointing settings (mirrors
    ``barge_in.BargeSettings``). Built by :func:`normalize_endpoint_hold_settings`
    from the config dataclass; passed to the worker + :func:`classify_tail`."""

    enabled: bool = False
    base_extend_ms: int = 500
    max_total_hold_ms: int = 1500
    hold_on_conjunction: bool = True
    hold_on_filler: bool = True
    hold_on_dangling: bool = True


def _trailing_token(text: str) -> str:
    """The last word, lowercased, with edge punctuation stripped but INTERNAL
    apostrophes kept ("don't" stays "don't", "so," → "so"). Deliberately NOT
    ``barge_in.normalize_text`` (which splits "don't" → "don t" and would
    mis-read the trailing token)."""
    words = (text or "").strip().split()
    if not words:
        return ""
    return words[-1].lower().strip(_PUNCT_EDGES)


def classify_tail(
    text: str, last_partial: str, settings: EndpointHoldSettings,
) -> TailResult:
    """Pure content-aware HOLD/COMMIT gate on the buffer tail. Implements the
    FROZEN contract §3 rules literally:

    1. ``stripped = text.rstrip()`` (whitespace only); empty → COMMIT.
    2. TERMINAL-PUNCT VETO on the RAW string BEFORE tokenization: strip trailing
       closing quotes/brackets, then last char ∈ ``.?!`` → COMMIT (overrides all
       lexical signals). Runs on raw because ``normalize_text`` would destroy the
       ``.?!``.
    3. Missing terminal punct ALONE never holds — a binary veto at ship; its
       absence only means "proceed to the lexical check".
    4. Tokenize with a dedicated whitespace-split + edge-strip (NOT
       ``normalize_text``), preserving internal apostrophes/hyphens.
    5-7. Single-token (conjunction → filler → dangling) then two-token filler;
       else COMMIT.

    The three category booleans are computed PRE-TOGGLE (recorded for telemetry
    regardless of the toggles); a ``hold_on_*`` toggle suppresses only the HOLD
    DECISION for that category, never the feature. ``last_partial`` is accepted
    for interface stability (a documented future resume-tightening knob) and does
    not affect the Increment-1 decision."""
    stripped = text.rstrip()

    # Rule 2: raw-string terminal-punct veto (strip trailing closing wraps first).
    veto_str = stripped.rstrip(_CLOSING_WRAP)
    ends_terminal = bool(veto_str) and veto_str[-1] in _TERMINAL_PUNCT

    # Rule 4: dedicated whitespace-split + edge-strip tokenization.
    words = stripped.lower().split()
    last = words[-1].strip(_PUNCT_EDGES) if words else ""
    last_two = (
        f"{words[-2].strip(_PUNCT_EDGES)} {last}" if len(words) >= 2 else "")

    # PRE-TOGGLE category booleans (always recorded for telemetry).
    is_conjunction = last in CONJUNCTIONS
    is_filler = last in FILLERS_SINGLE or last_two in FILLERS_MULTI
    is_dangling = last in DANGLING

    features = {
        "trailing_is_conjunction": is_conjunction,
        "trailing_is_filler": is_filler,
        "trailing_is_dangling": is_dangling,
        "ends_with_terminal_punct": ends_terminal,
        "n_tokens": len(words),
    }

    if ends_terminal:  # rule 2: completeness veto overrides all lexical signals
        return TailResult(COMMIT, None, features)

    # Rules 5-7: single-token (conj → filler → dangling) then two-token filler.
    # signal_category = the FIRST firing + toggle-enabled category (attribution);
    # the decision HOLDs iff a category caused it. The toggle gates the DECISION
    # only — the feature booleans above are already recorded.
    category: str | None = None
    if is_conjunction and settings.hold_on_conjunction:
        category = "conjunction"
    elif is_filler and settings.hold_on_filler:
        category = "filler"
    elif is_dangling and settings.hold_on_dangling:
        category = "dangling"
    return TailResult(HOLD if category else COMMIT, category, features)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def normalize_endpoint_hold_settings(
    cfg: "object",
) -> tuple[EndpointHoldSettings, list[str]]:
    """Build :class:`EndpointHoldSettings` from the config dataclass, clamping
    the numeric fields (mirrors ``barge_in.normalize_barge_settings``). Clamps:
    ``base_extend_ms`` [0,1500], ``max_total_hold_ms`` [base,3000] (so the cap
    can never be below one base extension). Returns ``(settings, warnings)``;
    the mount code logs each warning as ``web.voice.stt.config_clamped``."""
    warnings: list[str] = []
    base = _clamp(int(getattr(cfg, "base_extend_ms", 500)), 0, 1500)
    if base != getattr(cfg, "base_extend_ms", 500):
        warnings.append(
            f"base_extend_ms {getattr(cfg, 'base_extend_ms', 500)} clamped to {base}")
    raw_max = int(getattr(cfg, "max_total_hold_ms", 1500))
    max_total = _clamp(raw_max, base, 3000)
    if max_total != raw_max:
        warnings.append(f"max_total_hold_ms {raw_max} clamped to {max_total}")

    return EndpointHoldSettings(
        enabled=bool(getattr(cfg, "enabled", False)),
        base_extend_ms=base,
        max_total_hold_ms=max_total,
        hold_on_conjunction=bool(getattr(cfg, "hold_on_conjunction", True)),
        hold_on_filler=bool(getattr(cfg, "hold_on_filler", True)),
        hold_on_dangling=bool(getattr(cfg, "hold_on_dangling", True)),
    ), warnings


__all__ = [
    "COMMIT", "HOLD", "TailResult", "EndpointHoldSettings",
    "classify_tail", "normalize_endpoint_hold_settings",
    "CONJUNCTIONS", "FILLERS_SINGLE", "FILLERS_MULTI", "DANGLING",
]
