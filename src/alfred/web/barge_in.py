"""V3 barge-in — the pure suppression pipeline (no I/O, no optional deps).

:func:`evaluate_barge` decides whether a candidate transcript — a partial (Stage
A) or a final (Stage B) that arrived WHILE the assistant is speaking — should
INTERRUPT playback (barge) or be SUPPRESSED. Evaluation order (contract §1.4):

    too_early → interrupt-phrase bypass → backchannel → min_words/min_chars → echo

Everything here is a pure function of its arguments (the driver owns the state:
elapsed_ms since speaking start, the per-turn spoken buffer, and all telemetry).
No transcript text is ever logged by callers — this module only returns a
decision + a match score.

Design notes pinned in-code:

* **too_early beats interrupt-phrase.** A "stop" inside the first ``too_early_ms``
  suppresses at partial time (barge-guard against clipped self-audio); recovery
  rides the final path ~0.5-1 s later (§1.4).
* **echo_score is halving-resistant** (§1.5): ``max(longest_contiguous_token_run,
  token overlap coefficient)`` over token sets, so a single mid-phrase ASR
  substitution / dropped word / split word keeps the score high (the mandated
  garbled-echo cases all clear the threshold) while scattered common words stay
  low. The candidate is truncated to its last ~50 tokens before scoring (CPU
  bound; a longer candidate with sub-threshold containment fails anyway).
* Matching is plain normalized full-string / token comparison — **NO regex**.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import BargeInConfig

# Candidate token cap before echo scoring (§1.5 CPU bound).
_ECHO_CANDIDATE_TOKENS = 50
# List-cap hardening (contract §1.3 / sec-W1).
_MAX_LIST_ENTRIES = 64
_MAX_ENTRY_CHARS = 48


# Frozen defaults (config-extensible). All already normalized.
BACKCHANNEL_DEFAULTS: frozenset[str] = frozenset({
    "yeah", "yes", "yep", "yup", "uh huh", "uhhuh", "mhm", "mm", "mmhmm",
    "ok", "okay", "right", "sure", "got it", "i see", "huh", "hmm", "cool",
    "nice", "wow", "oh", "ah", "aha", "gotcha", "true", "exactly", "totally",
})

INTERRUPT_PHRASE_DEFAULTS: frozenset[str] = frozenset({
    "stop", "wait", "hold on", "no wait", "wait stop", "stop stop",
    "okay stop", "ok stop", "no no", "hang on", "hold up",
})


# --- normalization ---------------------------------------------------------


def normalize_text(text: str) -> str:
    """Lowercase, strip punctuation to spaces, collapse whitespace. Pure."""
    out = []
    for ch in (text or "").lower():
        out.append(ch if ch.isalnum() or ch.isspace() else " ")
    return " ".join("".join(out).split())


def _tokens(text: str) -> list[str]:
    return normalize_text(text).split()


def _longest_contiguous_run(cand: list[str], spoken: list[str]) -> int:
    """Longest run of consecutive ``cand`` tokens appearing contiguously in
    ``spoken`` (token-level longest-common-substring). O(n*m), inputs are
    small (candidate ≤ 50 tokens)."""
    if not cand or not spoken:
        return 0
    prev = [0] * (len(spoken) + 1)
    best = 0
    for i in range(1, len(cand) + 1):
        cur = [0] * (len(spoken) + 1)
        ci = cand[i - 1]
        for j in range(1, len(spoken) + 1):
            if ci == spoken[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def echo_score(candidate: str, spoken: str) -> float:
    """How strongly ``candidate`` echoes the ``spoken`` buffer, in [0, 1].

    ``max(longest_contiguous_token_run / len(candidate), overlap_coefficient)``
    where the overlap coefficient is ``|cand ∩ spoken| / min(|cand|, |spoken|)``
    over token SETS, gated by a ≥2-token intersection (a lone shared common word
    can't score). The set-overlap direction makes the score robust to token-
    count changes (splits/merges) — a single garble can't halve it (§1.5)."""
    cand = _tokens(candidate)[-_ECHO_CANDIDATE_TOKENS:]
    spok = _tokens(spoken)
    if not cand or not spok:
        return 0.0
    run_len = _longest_contiguous_run(cand, spok)
    # A lone-token run is weak evidence (and gets gated out of coverage by the
    # ≥2 rule below) — require ≥2 contiguous tokens for the run to count.
    run_ratio = (run_len / len(cand)) if run_len >= 2 else 0.0
    cset, sset = set(cand), set(spok)
    inter = cset & sset
    coverage = (len(inter) / min(len(cset), len(sset))) if len(inter) >= 2 else 0.0
    return max(run_ratio, coverage)


# --- decision --------------------------------------------------------------


@dataclass(frozen=True)
class BargeDecision:
    """Result of :func:`evaluate_barge`. ``reason`` is ``""`` on a barge, else
    ``too_early`` | ``backchannel`` | ``too_short`` | ``echo``. ``score`` is the
    echo score (only meaningful for ``reason == "echo"``)."""

    barge: bool
    reason: str = ""
    score: float = 0.0


@dataclass(frozen=True)
class BargeSettings:
    """Mount-normalized, driver-threaded barge settings (contract §1.3 — a
    ctor-passed object, NOT getattr chains). ``interrupt_phrases`` /
    ``backchannel_phrases`` are the resolved frozensets (defaults + the instance
    name + config extras, all normalized)."""

    enabled: bool = False
    too_early_ms: int = 700
    min_words: int = 2
    min_chars: int = 6
    echo_threshold: float = 0.8
    echo_grace_s: float = 2.0
    interrupt_phrases: frozenset[str] = field(default_factory=lambda: INTERRUPT_PHRASE_DEFAULTS)
    backchannel_phrases: frozenset[str] = field(default_factory=lambda: BACKCHANNEL_DEFAULTS)


def evaluate_barge(
    text: str, *, elapsed_ms: float, spoken: str, settings: BargeSettings,
) -> BargeDecision:
    """Run the suppression pipeline (§1.4). Pure. ``elapsed_ms`` = time since the
    speaking window started; ``spoken`` = the per-turn fed-text buffer."""
    norm = normalize_text(text)
    if not norm:
        return BargeDecision(False, "too_short")
    # 1. too_early — beats everything, including interrupt phrases.
    if elapsed_ms < settings.too_early_ms:
        return BargeDecision(False, "too_early")
    # 2. interrupt-phrase bypass — full-string normalized match → barge now.
    if norm in settings.interrupt_phrases:
        return BargeDecision(True)
    # 3. backchannel — full-string normalized match → suppress.
    if norm in settings.backchannel_phrases:
        return BargeDecision(False, "backchannel")
    # 4. min_words / min_chars floor.
    if len(norm.split()) < settings.min_words or len(norm) < settings.min_chars:
        return BargeDecision(False, "too_short")
    # 5. echo gate.
    score = echo_score(text, spoken)
    if score >= settings.echo_threshold:
        return BargeDecision(False, "echo", score)
    return BargeDecision(True)


# --- mount-time normalization ----------------------------------------------


def _normalize_list(raw: list, warnings: list[str], label: str) -> list[str]:
    """Normalize + cap a config phrase list (≤64 entries, ≤48 chars each);
    over-cap / over-long entries are DROPPED with a config_clamped warning."""
    out: list[str] = []
    dropped_long = 0
    for entry in raw:
        if not isinstance(entry, str):
            continue
        if len(entry) > _MAX_ENTRY_CHARS:
            dropped_long += 1
            continue
        norm = normalize_text(entry)
        if norm:
            out.append(norm)
    if dropped_long:
        warnings.append(f"{label}: dropped {dropped_long} entr(ies) over "
                        f"{_MAX_ENTRY_CHARS} chars")
    if len(out) > _MAX_LIST_ENTRIES:
        warnings.append(f"{label}: capped {len(out)} entries to {_MAX_LIST_ENTRIES}")
        out = out[:_MAX_LIST_ENTRIES]
    return out


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def normalize_barge_settings(
    cfg: "BargeInConfig", *, instance_name: str = "",
) -> tuple[BargeSettings, list[str]]:
    """Build the driver's :class:`BargeSettings` from config. Pure. Clamps every
    numeric field, caps + normalizes the phrase lists (§1.3 / sec-W1), and folds
    the lowercased instance name into the interrupt phrases (§1.4 — "salem!" is
    the most natural interrupt). Returns ``(settings, warnings)``; the mount code
    logs each warning as ``web.voice.barge.config_clamped``."""
    warnings: list[str] = []
    too_early = _clamp(int(cfg.too_early_ms), 0, 5000)
    if too_early != cfg.too_early_ms:
        warnings.append(f"too_early_ms {cfg.too_early_ms} clamped to {too_early}")
    min_words = _clamp(int(cfg.min_words), 1, 20)
    min_chars = _clamp(int(cfg.min_chars), 1, 200)
    threshold = _clamp(float(cfg.echo_threshold), 0.0, 1.0)
    if threshold != cfg.echo_threshold:
        warnings.append(f"echo_threshold {cfg.echo_threshold} clamped to {threshold}")
    grace = _clamp(float(cfg.echo_grace_s), 0.0, 30.0)

    interrupt_extra = _normalize_list(list(cfg.interrupt_extra), warnings, "interrupt_extra")
    backchannel_extra = _normalize_list(list(cfg.backchannel_extra), warnings, "backchannel_extra")

    interrupt = set(INTERRUPT_PHRASE_DEFAULTS) | set(interrupt_extra)
    name = normalize_text(instance_name)
    if name:
        interrupt.add(name)
    backchannel = set(BACKCHANNEL_DEFAULTS) | set(backchannel_extra)

    settings = BargeSettings(
        enabled=bool(cfg.enabled),
        too_early_ms=too_early,
        min_words=min_words,
        min_chars=min_chars,
        echo_threshold=threshold,
        echo_grace_s=grace,
        interrupt_phrases=frozenset(interrupt),
        backchannel_phrases=frozenset(backchannel),
    )
    return settings, warnings
