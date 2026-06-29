"""``alfred routine`` subcommand handlers.

Phase 1 commands:

  - ``alfred routine done [<record>] <item>`` — append a completion date
    to ``completion_log[item]`` on the routine record. Single source of
    truth for date-append semantics. The ``<record>`` argument is
    OPTIONAL since Phase 2B B1 (2026-05-30): when omitted, the CLI does
    a vault-wide fuzzy match across all active routine records.
  - ``alfred routine run-now`` — force-build today's daily aggregator
    note. Useful for ad-hoc operator runs + testing.
  - ``alfred routine status`` — print last run + schedule summary.

The ``done`` verb mutates the ``completion_log`` frontmatter field on
``routine/<record>.md``. The mutation is append-only and idempotent:
calling ``done`` twice with the same item on the same day yields one
log entry (no duplicate dates within a single day).

Salem-only enforcement: every command checks
``config.instance_name == REQUIRED_INSTANCE`` and raises a clear
ScopeError on mismatch. The aggregator daemon's start-guard handles
the same check separately; the CLI guard exists so an operator
invoking ``alfred routine done`` on a non-Salem instance gets a
visible refusal rather than silently mutating the wrong vault.

Phase 2B B1 additions (2026-05-30):

  - ``--completed-at YYYY-MM-DD`` flag on ``done`` for back-dating.
    Validated ≤ today (no future completion). Default: today (in
    ``config.schedule.timezone``).
  - Structured JSON canary discriminator on ``--json`` output:
    ``kind`` ∈ {success, unknown_record, unknown_item, ambiguous_item,
    idempotent_noop, future_date_rejected}. The structured shape
    exists so the talker subprocess wrapper can return canary results
    to the LLM without parsing free-text error messages. Pre-B1 the
    JSON shape was ``{ok: True | False, error: ...}``; the new
    ``kind`` field augments that; the ``ok`` field stays for backwards
    compat with any existing scripted consumers.
  - Vault-wide fuzzy item match: when ``<record>`` is omitted (or
    explicitly empty), scan every active routine's items for
    substring + stem-tolerant matches on ``item.text``. 0 matches →
    ``unknown_item``; 1 → use it; 2+ → ``ambiguous_item`` (returns
    the candidate list so the talker can ask back).
"""

from __future__ import annotations

import contextlib
import functools
import re
import sys
from dataclasses import dataclass
from datetime import date as date_type, datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import frontmatter  # type: ignore[import-untyped]
import structlog
import yaml

from alfred.vault.scope import ScopeError

from .aggregator import run_aggregator_once
from .config import REQUIRED_INSTANCE, RoutineConfig
from .state import StateManager

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# JSON-stdout contract guard (arc-followup 1, 2026-06-28)
# ---------------------------------------------------------------------------
#
# The routine CLI's ``--json`` output (the canary the talker subprocess +
# tests ``json.loads``) shares ``sys.stdout`` with structlog. structlog's
# *unconfigured* default sink is ``sys.stdout`` (``PrintLoggerFactory``), and
# any config a prior caller/test left on the global ``structlog`` can also
# point at stdout — so a diagnostic firing mid-handler interleaves a rendered
# log line with the JSON and breaks the parse. This bit:
#
#   * ``routine.cli.completion_log_not_dict`` / ``skipping_bad_log_entry`` /
#     ``bad_timezone`` — ungated edge-path logs in ``cmd_done``;
#   * the nested ``routine.aggregator.*`` logs fired by ``run-now``;
#   * the original ``routine_done.matched`` (Step 5 band-aided it with a
#     per-log ``if not wants_json`` gate — now subsumed by this guard).
#
# In production the ``cmd_routine`` dispatcher already suppresses stdout
# (``_setup_logging_from_config(suppress_stdout=wants_json)``); this guard
# closes the DIRECT-call path (tests + any future importer of ``cmd_*``) so
# stdout can never be polluted on ANY routine JSON CLI path — robust against
# whatever global structlog state a prior test left behind, because it
# OVERRIDES the config for the handler's duration then RESTORES it.
#
# Diagnostics stay VISIBLE on stderr (per ``feedback_intentionally_left_blank``)
# rather than being dropped. A no-op when ``wants_json`` is False — the human
# path renders wherever logging is configured, and ``structlog.testing.
# capture_logs`` (the log-emission tests, all ``wants_json=False``) still works
# because the guard never touches that path.


@contextlib.contextmanager
def _json_stdout_guard(wants_json: bool):
    """Route EVERY structlog logger to ``sys.stderr`` for the duration when
    ``wants_json`` — so the routine CLI's stdout stays a pure JSON contract —
    then restore the prior global config. No-op when ``wants_json`` is False."""
    if not wants_json:
        yield
        return
    prev = structlog.get_config() if structlog.is_configured() else None
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )
    try:
        yield
    finally:
        if prev is not None:
            structlog.configure(**prev)
        else:
            structlog.reset_defaults()


def _json_stdout_safe(fn: Callable) -> Callable:
    """Decorator: wrap a routine CLI handler so its ``wants_json`` path can
    never leak a log line onto stdout (see ``_json_stdout_guard``). All routine
    handlers take ``wants_json`` keyword-only, so reading it from kwargs is
    exact."""

    @functools.wraps(fn)
    def _wrapper(*args: Any, **kwargs: Any) -> Any:
        with _json_stdout_guard(bool(kwargs.get("wants_json", False))):
            return fn(*args, **kwargs)

    return _wrapper


# Module-load-order contract: keep the DONE_KIND_* + ITEM_KIND_*
# constant blocks (below) ABOVE the bottom-of-file
# ``from .cli_items import ...`` line. ``cli_items.py`` imports
# these constants from us at deferred-import time; defining them
# AFTER the bottom-of-file import would crash on partial-module-load
# (the import statement runs first, cli_items.py tries to import
# the constants, they don't exist yet → ImportError). Same applies
# to ``_check_salem_only``, ``_emit_canary``, ``_fuzzy_match_vault_wide``,
# ``_ItemCandidate``, ``_matches_item``, ``_routine_path`` — all of
# which ``cli_items.py`` lazy-imports from this module. Moving any
# of these definitions below the bottom-of-file import block would
# silently break the deferred-circular-import design.


# ---------------------------------------------------------------------------
# Phase 2B B1 (2026-05-30) — Conversational completion canary kinds
# ---------------------------------------------------------------------------
#
# Cross-agent contract — the structured JSON discriminator the talker
# subprocess wrapper consumes to decide what to say back to the user.
# String-typed for forward-compat with the talker's JSON parsing path
# (the SKILL recognises these literal values verbatim).
#
# Rename any of these = update SKILL.md's "Marking routines done"
# section in lockstep + the talker subprocess dispatcher.
DONE_KIND_SUCCESS = "success"
DONE_KIND_UNKNOWN_RECORD = "unknown_record"
DONE_KIND_UNKNOWN_ITEM = "unknown_item"
DONE_KIND_AMBIGUOUS_ITEM = "ambiguous_item"
DONE_KIND_IDEMPOTENT_NOOP = "idempotent_noop"
DONE_KIND_FUTURE_DATE_REJECTED = "future_date_rejected"
# Dispatcher-only canary kinds — emitted by the talker subprocess
# wrapper in :mod:`alfred.telegram.conversation` when the subprocess
# itself fails (the CLI can't produce these because they describe
# states OUTSIDE the CLI's runtime). Still belong in the canary
# contract because the talker routes on the same ``kind`` discriminator
# regardless of which layer produced the value.
DONE_KIND_TIMEOUT = "timeout"
DONE_KIND_SUBPROCESS_ERROR = "subprocess_error"


# ---------------------------------------------------------------------------
# Phase 2B B3 (2026-05-30) — Conversational item-CRUD canary kinds
# ---------------------------------------------------------------------------
#
# Cross-agent contract — the structured JSON discriminator the talker
# subprocess wrapper consumes to decide what to say back to the user
# after an add/remove/edit on a routine record's items list.
#
# Three success kinds (one per action) so the SKILL can phrase the
# operator-facing confirmation correctly ("Added X to <routine>" vs
# "Removed X from <routine>" vs "Updated X on <routine>"). Six
# failure / refusal kinds (mostly mirroring B1's DONE_KIND_* shape):
# unknown_record, unknown_item, ambiguous_item, plus three B3-specific:
# cadence_conflict (mutual-exclusion violation without explicit clear
# flag), duplicate_item (add with text matching existing), invalid_field
# (operator-supplied value fails type/range validation).
#
# Rename any of these = update SKILL.md's "Adjusting routines" section
# in lockstep + the talker subprocess dispatcher's lazy import.
# Set-difference lockstep pin lives in
# ``tests/telegram/test_conversation_routine_item.py``.
ITEM_KIND_ADDED = "added"
ITEM_KIND_REMOVED = "removed"
ITEM_KIND_EDITED = "edited"
ITEM_KIND_UNKNOWN_RECORD = "unknown_record"
ITEM_KIND_UNKNOWN_ITEM = "unknown_item"
ITEM_KIND_AMBIGUOUS_ITEM = "ambiguous_item"
ITEM_KIND_CADENCE_CONFLICT = "cadence_conflict"
ITEM_KIND_DUPLICATE_ITEM = "duplicate_item"
ITEM_KIND_INVALID_FIELD = "invalid_field"
# Un-log (surgical single-date removal — the inverse of ``done``).
# ``unlogged`` = a date was removed; ``not_logged`` = the date wasn't present
# (idempotent no-op, exit 0 — DISTINCT from done's ``idempotent_noop`` so the
# talker can voice "you hadn't logged that, nothing to remove" vs "already
# logged"). Per the intentionally-left-blank principle the no-op is explicit.
ITEM_KIND_UNLOGGED = "unlogged"
ITEM_KIND_NOT_LOGGED = "not_logged"


def _check_salem_only(config: RoutineConfig) -> None:
    """Raise ScopeError unless the active instance is Salem.

    Salem-only contract surfaces at three layers:
      - schema (routine in canonical scope only)
      - scope rules (HYPATIA / KALLE create allowlists exclude routine)
      - daemon-start guard + this CLI guard (instance-level refusal)

    The two-layer scope.create check would fail anyway on a non-Salem
    config, but the routine record-mutation path bypasses scope (the
    CLI rewrites the frontmatter directly via frontmatter.dumps rather
    than going through vault_edit). Hence the explicit gate here.
    """
    if config.instance_name != REQUIRED_INSTANCE:
        raise ScopeError(
            f"alfred routine is Salem-only in Phase 1. Detected "
            f"instance: {config.instance_name!r} (required: "
            f"{REQUIRED_INSTANCE!r}). Per the Phase 1 ratified "
            f"contract, only the Salem instance maintains routine "
            f"records — Hypatia and KAL-LE have no canonical surface "
            f"for them. Phase 2 may relax this; today, refuse."
        )


def _routine_path(vault_path: Path, record: str) -> Path:
    """Resolve a routine name to its on-disk path.

    Accepts either the bare record name (``"For Self Health"``) or a
    relative path (``"routine/For Self Health"``). Returns the
    absolute path; raises ``FileNotFoundError`` when the file is
    missing.
    """
    routine_dir = vault_path / "routine"
    if record.endswith(".md"):
        record = record[:-3]
    if record.startswith("routine/"):
        record = record[len("routine/"):]
    candidate = routine_dir / f"{record}.md"
    if not candidate.exists():
        raise FileNotFoundError(
            f"Routine record not found: {candidate} "
            f"(looking under {routine_dir})"
        )
    return candidate


def _today_iso(tz_name: str) -> str:
    """Return today's ISO date string in the configured timezone.

    Read from ``config.schedule.timezone`` so the date matches the
    aggregator's daily fire boundary — relevant near midnight when the
    OS clock might be in UTC but the operator's day boundary is Halifax.
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        # Fall back to date.today() if the tz string is bad — surface a
        # warning, don't crash. The operator's normal config has a valid
        # tz; this path only fires on a typo.
        log.warning("routine.cli.bad_timezone", tz=tz_name)
        return date_type.today().isoformat()
    return datetime.now(tz).date().isoformat()


# ---------------------------------------------------------------------------
# Phase 2B B1 — fuzzy item match across the vault's active routines
# ---------------------------------------------------------------------------


@dataclass
class _ItemCandidate:
    """One (record_name, item_text, path) tuple surfaced by the fuzzy
    match.

    ``record_name`` is the OPERATOR-FACING display name — taken from
    the record's frontmatter ``name`` if present, else the file stem.
    What the brief renders / the talker echoes back to the operator.

    ``item_text`` is the verbatim item ``text`` field from the routine
    record's ``items`` list.

    ``path`` is the resolved on-disk path. Captured here at scan time
    (rather than re-resolved later via ``_routine_path(record_name)``)
    because ``record_name`` may NOT match the file stem when the
    operator has set a frontmatter ``name:`` that differs from the
    filename. The pre-fix shape recomputed
    ``_routine_path(vault_path, chosen.record_name)`` in the
    vault-wide-fuzzy success branch, which crashed with an uncaught
    ``FileNotFoundError`` whenever an active routine had
    ``name: <X>`` with X ≠ file-stem. The reviewer flagged the bug
    2026-05-30; the fix is to carry the already-resolved path here
    rather than re-derive it.
    """
    record_name: str
    item_text: str
    path: Path


#: Stop-words filtered out of fuzzy-match token sets. Operator
#: phrasing like "I walked the dog" carries function words that don't
#: contribute to matching ("I", "the"). Keep this list small — every
#: addition reduces the chance of a legitimate signal making it
#: through. Match by exact-token-equality (post-stemming).
_FUZZY_STOPWORDS: frozenset[str] = frozenset({
    "i", "the", "a", "an", "to", "my", "for", "on", "in", "at",
    "and", "or", "but", "of",
})


#: Vowel set used for the conservative ``-ed`` / ``-ing`` restore-``-e``
#: heuristic. Excludes ``y`` deliberately — Porter-stemmer-style: ``y``
#: at word-end behaves as a vowel for English morphology ("played" →
#: "play", NOT "playe"), so the restore-``-e`` rule treats trailing
#: ``y`` as already-vowelic and skips restoration.
_VOWELS: frozenset[str] = frozenset("aeiou")


def _fuzzy_stem(value: str) -> str:
    """Normalise a string for stem-tolerant matching.

    Lowercases, strips punctuation, collapses whitespace, stems EVERY
    word against a small English suffix list (``-ing``, ``-ed``,
    ``-s``), and joins. Three rules:

      * **``-s``** — strip only when the char before ``-s`` is NEITHER
        ``e`` (``exercise`` / ``pause`` / ``tense`` end in ``-se``,
        not plural ``-s``) NOR another ``s`` (``class`` / ``glass``
        end in ``-ss``, not plural ``-s``). This preserves the
        bug-fix from the first B1 ship: ``exercise`` stayed
        ``exercis`` because the original ``-s`` rule was too greedy.
      * **``-ed``** — strip the suffix, then if the resulting stem
        ends in ``<vowel><consonant-not-y>`` (a CVC ending in a
        non-``y`` consonant), restore the silent ``-e``: ``exercised``
        → strip → ``exercis`` → restore → ``exercise``; ``walked`` →
        strip → ``walk`` (ends in ``lk`` consonant-consonant) → no
        restore → ``walk``. The non-``y`` exclusion handles
        ``played`` → ``play`` (ends in ``y``, treated as vowel for
        this check) → no restore.
      * **``-ing``** — same restore-``-e`` heuristic as ``-ed``:
        ``noting`` → strip → ``not`` → restore → ``note``; ``walking``
        → strip → ``walk`` → no restore → ``walk``.

    Per-suffix length floor: ``len(word) > len(suffix) + 1`` (so
    ``red``, length 3, doesn't get stripped to ``r`` because
    ``3 > 2 + 1`` is False).

    Stop-words are NOT removed here — caller (``_matches_item``)
    handles stop-word filtering when token-set-comparing because the
    raw stemmed form is also useful for substring fallback checks.

    Pure function — used both by the per-item match check and by the
    fuzzy candidate scan. Tests pin the behavior.
    """
    cleaned = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    if not cleaned:
        return ""
    parts = cleaned.split()
    out: list[str] = []
    for word in parts:
        out.append(_stem_word(word))
    return " ".join(out)


def _stem_word(word: str) -> str:
    """Stem one whitespace-stripped lowercased word.

    Implements the three rules documented on :func:`_fuzzy_stem`. Pure
    function; tests pin the per-word behavior independently of the
    multi-word stem pipeline.
    """
    # ``-ing`` (check first because "-ed" / "-s" don't apply to words
    # ending in "-ing": "walking" doesn't end in "ed" or "s").
    if len(word) > 4 and word.endswith("ing"):
        stem = word[:-3]
        return _maybe_restore_silent_e(stem)
    # ``-ed`` (check before ``-s`` because "-ed" ends in "d" not "s",
    # so the order is structurally independent; we still pick ``-ed``
    # first because operator past-tense phrasing is more common than
    # plural-form phrasing for routine items).
    if len(word) > 3 and word.endswith("ed"):
        stem = word[:-2]
        return _maybe_restore_silent_e(stem)
    # ``-s`` — conservative strip per the docstring above.
    if len(word) > 2 and word.endswith("s"):
        before_s = word[-2]
        # ``-se`` (exercise, pause, tense) and ``-ss`` (class, glass)
        # are not plurals — leave the word alone.
        if before_s not in ("e", "s"):
            return word[:-1]
    return word


def _maybe_restore_silent_e(stem: str) -> str:
    """If ``stem`` ends in ``<vowel><consonant-not-y>``, append ``-e``.

    The CVC-ending-in-non-y-consonant pattern catches the
    silent-``e`` words ("exercise", "note", "like", "live") whose
    past-tense / present-participle strip the silent ``-e`` along
    with the suffix. Restoring it normalises ``exercised`` /
    ``exercising`` back to ``exercise`` for matching purposes.

    Edge cases:
      * Stem ends in ``y`` (post-strip): ``play`` from ``played`` —
        ``y`` is vowel-like → no restore. Correct — ``play`` is the
        base verb.
      * Stem ends in vowel-vowel (rare): no restore (not a CVC).
      * Stem is too short (< 2 chars): no restore. The pattern needs
        two chars to check.
    """
    if len(stem) < 2:
        return stem
    last = stem[-1]
    second_last = stem[-2]
    # CVC-with-non-y-consonant pattern: position -2 is a vowel,
    # position -1 is a consonant that isn't ``y``.
    if (
        second_last in _VOWELS
        and last not in _VOWELS
        and last != "y"
    ):
        return stem + "e"
    return stem


# Min stem length for the check-2 stem-substring fallback (Step 5
# structural fix, 2026-06-06 Tilray→Meds). A stem shorter than this (e.g.
# "med" from "Meds", 3 chars) substrings into unrelated tokens
# ("medical", "medium", "comedian") far too eagerly, so the substring
# fallback is skipped below that floor. Legitimate short/exact matches are
# caught by check 1 (original-text substring) or check 3 (token
# containment), so this floor only suppresses the false-positive class.
_MIN_STEM_LEN = 4


def _matches_item(query: str, item_text: str, glossary=None) -> bool:
    """True if ``query`` matches ``item_text`` per the fuzzy rules.

    ``glossary`` (optional, the Phase-2 self-correcting learned glossary —
    ``match_calibration.Glossary``) is consulted FIRST: an operator-confirmed
    REJECT for this ``(query_key, item)`` pair short-circuits to False (kills a
    recurring false-positive); a confirmed CONFIRM (or alias) short-circuits to
    True (promotes an idiosyncratic phrasing the operator approved). Default
    ``None`` ⟹ the matcher behaves EXACTLY as before (empty glossary == today;
    the existing 2-arg call-sites are byte-identical). GUARDRAIL: consulting is
    READ-ONLY — the glossary is mutated only by an operator reply.

    Three checks (any pass → match):
      1. Case-insensitive substring on the raw text (the strict
         "operator typed a substring of the canonical text" case —
         e.g. "Walk dog" in the brief is rendered exactly that way).
      2. Stem-normalised substring (the operator's phrasing differs in
         verb conjugation: "exercised" stems to "exercise" via
         strip ``-ed`` + restore silent ``-e``; "Exercise" stays
         "exercise" (the ``-s`` rule preserves ``-se`` endings) —
         substring matches).
      3. Token-set overlap: stop-words filtered, then every
         non-stop-word token in EITHER the query's or the item's
         stem-normalised form must appear in the other. So
         "I walked the dog" → tokens {walk, dog} (after stop-word
         filter), "Walk dog" → tokens {walk, dog}, equal sets →
         match. "I walked" → {walk} ⊂ {walk, dog} → match (single
         word matches a multi-word item if it's a non-stop content
         word).

    The three-check ladder is intentionally generous on the operator's
    side. False-positives (matching the wrong item) surface to the
    ``ambiguous_item`` canary — the operator gets asked back rather
    than silently wronged.
    """
    if not query or not item_text:
        return False
    # Phase-2 learned-glossary consult (READ-ONLY) — an operator-approved
    # verdict for this pair wins over the fuzzy ladder. Empty/None glossary
    # falls straight through to the unchanged fuzzy checks below.
    if glossary is not None and not glossary.is_empty():
        from . import match_calibration as _mc
        verdict = glossary.verdict(_mc.query_key(query), item_text)
        if verdict == "reject":
            return False
        if verdict == "confirm":
            return True
    if query.casefold() in item_text.casefold():
        return True
    qstem = _fuzzy_stem(query)
    istem = _fuzzy_stem(item_text)
    if not qstem or not istem:
        return False
    # Check 2: stem-substring containment — gated by TWO structural
    # guards (Step 5 structural fix, 2026-06-06 Tilray→Meds). The bare
    # substring check matched too aggressively: ``_fuzzy_stem("Meds")
    # == "med"`` (3 chars), and ``"med" in "tilray medical registration
    # renewal"`` fired here, returning True with effectively zero shared
    # content tokens.
    #   (A) min-stem-length floor — skip the fallback when EITHER stem
    #       is below ``_MIN_STEM_LEN`` (catches the 3-char "med").
    #   (B) confidence gate — require non-zero token-overlap confidence
    #       (``_match_confidence > 0``); zero overlap is not a match.
    # BOTH must pass = defense-in-depth (Tilray→Meds fails both). The
    # self-correcting matcher LOOP is a separate deferred piece; this is
    # the structural close only. Legitimate short/exact matches are
    # caught by check 1 (original-text substring) above or check 3
    # (token containment) below — this only suppresses the
    # substring-only zero-overlap false-positive class.
    if (
        (qstem in istem or istem in qstem)
        and len(qstem) >= _MIN_STEM_LEN
        and len(istem) >= _MIN_STEM_LEN
        and _match_confidence(query, item_text) > 0.0
    ):
        return True
    # Token-set overlap with stop-word filter.
    q_tokens = {
        t for t in qstem.split() if t and t not in _FUZZY_STOPWORDS
    }
    i_tokens = {
        t for t in istem.split() if t and t not in _FUZZY_STOPWORDS
    }
    if not q_tokens or not i_tokens:
        return False
    # Single-direction containment (either set is subset of the
    # other) → match. Mutual non-empty overlap is the looser shape
    # but produces too many false positives ("walk the cat" matching
    # "Walk dog" via shared "walk").
    return q_tokens <= i_tokens or i_tokens <= q_tokens


def _match_confidence(query: str, item_text: str) -> float:
    """Return a 0.0–1.0 confidence score for a query→item match.

    P4 / Surface (b) — 2026-06-07 instrumentation helper. The
    operative ``_matches_item`` returns ``bool`` for back-compat with
    30+ test sites; this is a separate helper called from the success
    branch of :func:`cmd_done` so we can log a per-match confidence
    score without changing the matcher's return signature.

    Scoring shape — Jaccard-like ratio over stemmed + stopword-
    filtered token sets:

      * Token-set intersection / max(|q|, |i|)
      * 0.0 when no shared non-stopword content tokens
      * 1.0 when token sets are identical (after stem + stopword
        filter)
      * In between when the sets overlap partially

    Worked example (2026-06-06 Tilray→Meds canonical false positive):
      * query = "Tilray Medical Registration Renewal"
      * item_text = "Meds"
      * qstem tokens = {tilray, medical, registration, renewal}
      * istem tokens = {med}
      * Intersection = {} (med ≠ medical post-stem)
      * Confidence = 0.0

    Worked example (genuine match):
      * query = "I walked the dog yesterday"
      * item_text = "Walk dog"
      * qstem tokens = {walk, dog, yesterday} (stopword "I", "the"
        filtered)
      * istem tokens = {walk, dog}
      * Intersection = {walk, dog}
      * Confidence = 2 / max(3, 2) = 0.667

    Worked example (exact match):
      * query = "Walk dog"
      * item_text = "Walk dog"
      * Confidence = 1.0

    The scoring is intentionally simple — Jaccard over stemmed token
    sets is a single-pass O(n) computation, no dependencies beyond
    the existing stem helper. A future tightening pass can use the
    confidence threshold to gate the check-2 substring fallback (see
    the TODO P4-followup comment in ``_matches_item`` at line ~400);
    that's deferred per the 2026-06-07 brief — instrument first,
    measure across actual Salem traffic, then tune.

    Returns:
        Float in [0.0, 1.0]. NaN-free; empty-input cases return 0.0.
    """
    qstem = _fuzzy_stem(query)
    istem = _fuzzy_stem(item_text)
    if not qstem or not istem:
        return 0.0
    q_tokens = {
        t for t in qstem.split() if t and t not in _FUZZY_STOPWORDS
    }
    i_tokens = {
        t for t in istem.split() if t and t not in _FUZZY_STOPWORDS
    }
    if not q_tokens or not i_tokens:
        return 0.0
    intersection = q_tokens & i_tokens
    if not intersection:
        return 0.0
    return len(intersection) / max(len(q_tokens), len(i_tokens))


def _iter_active_routine_items(vault_path: Path) -> list[_ItemCandidate]:
    """Walk ``vault/routine/*.md`` and yield every (record, item) pair
    from ``status: active`` routines.

    Defensive: parse failures and malformed shapes silently skip
    (mirrors the aggregator's tolerance pattern). Empty list returned
    when ``routine/`` doesn't exist (fresh vault). Per
    ``feedback_intentionally_left_blank`` the empty case is the
    caller's problem to render — this helper just returns the data.
    """
    routine_dir = vault_path / "routine"
    if not routine_dir.is_dir():
        return []
    out: list[_ItemCandidate] = []
    for path in sorted(routine_dir.glob("*.md")):
        try:
            post = frontmatter.load(str(path))
        except Exception:  # noqa: BLE001
            continue
        fm = dict(post.metadata or {})
        if str(fm.get("status") or "active").lower() == "archived":
            continue
        record_name = str(fm.get("name") or path.stem)
        raw_items = fm.get("items") or []
        if not isinstance(raw_items, list):
            continue
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            text = str(raw.get("text") or "").strip()
            if not text:
                continue
            out.append(_ItemCandidate(
                record_name=record_name,
                item_text=text,
                path=path,
            ))
    return out


def _fuzzy_match_vault_wide(
    vault_path: Path, item_query: str, glossary=None,
) -> tuple[list[_ItemCandidate], list[_ItemCandidate]]:
    """Vault-wide fuzzy match for an item.

    ``glossary`` (optional Phase-2 learned glossary) is threaded into
    :func:`_matches_item` so operator-approved confirm/reject verdicts steer
    the match. Default ``None`` ⟹ unchanged behaviour.

    Returns ``(matches, all_candidates)``:
      * ``matches`` — the subset that matches ``item_query`` per
        :func:`_matches_item`. May be empty (no match), 1 (use it), or
        2+ (ambiguous, caller asks back).
      * ``all_candidates`` — the full active-routine item inventory.
        Surfaced alongside matches so the canary JSON output can show
        the operator what was available (helpful when ``matches`` is
        empty — they see they typed something not in the vault).
    """
    all_candidates = _iter_active_routine_items(vault_path)
    matches = [
        c for c in all_candidates
        if _matches_item(item_query, c.item_text, glossary)
    ]
    return matches, all_candidates


def _capture_no_match(
    config: "RoutineConfig",
    query: str,
    all_candidates: list[_ItemCandidate],
    completion_date: str,
    glossary,
) -> None:
    """Self-correcting matcher — Phase 3 no-match / alias CAPTURE.

    A vault-wide completion that matched NOTHING is the false-NEGATIVE signal
    (the mirror of the low-confidence false-positive Phase 1 captures). We
    compute the CLOSEST active-routine item by ``_match_confidence`` and surface
    it as a "did you mean…" alias suggestion for operator review — confirm →
    alias (the phrasing now matches), reject → suppressed.

    Guardrails:
      * MIN-PLAUSIBILITY FLOOR — only suggest when the closest score clears
        ``no_match_floor``; below it the suggestion is noise, so we emit the ILB
        ``no_match_nothing_close`` signal instead (idle distinguishable from
        broken). Empty candidate list → same ILB signal.
      * No re-asking a rejected suggestion — if the operator already rejected
        this ``(query_key, candidate)`` pair, skip (recorded, not re-asked).
      * Writes ONLY the pending sink (``no_match`` kind) — never the corpus
        (operator-reply-only). Best-effort: the caller wraps this so a capture
        failure never breaks the completion's canary.

    A confirmed alias makes the matcher MATCH the phrasing next time, so it
    never reaches this branch again (no confirm-suppression check needed here).
    """
    from . import match_calibration as _mc

    if not all_candidates:
        log.info(
            "routine.match_calibration.no_match_nothing_close",
            query=query,
            reason="no_active_items",
        )
        return

    best: _ItemCandidate | None = None
    best_score = 0.0
    for c in all_candidates:
        score = _match_confidence(query, c.item_text)
        if score > best_score:
            best_score = score
            best = c

    floor = config.match_calibration.no_match_floor
    if best is None or best_score < floor:
        log.info(
            "routine.match_calibration.no_match_nothing_close",
            query=query,
            best_score=round(best_score, 3),
            floor=floor,
        )
        return

    if not glossary.is_empty() and (
        glossary.verdict(_mc.query_key(query), best.item_text) == "reject"
    ):
        log.info(
            "routine.match_calibration.no_match_already_rejected",
            query=query,
            candidate=best.item_text,
        )
        return

    from datetime import datetime, timezone

    _mc.append_pending(
        config.match_calibration.pending_path,
        _mc.PendingMatch(
            query=query,
            matched_to=best.item_text,
            record=best.record_name,
            confidence=best_score,
            completion_date=completion_date,
            captured_at=datetime.now(timezone.utc).isoformat(),
            kind=_mc.KIND_NO_MATCH,
        ),
    )
    log.info(
        "routine.match_calibration.no_match_captured",
        query=query,
        candidate=best.item_text,
        record=best.record_name,
        score=round(best_score, 3),
    )


def _validate_completed_at(
    completed_at: str | None,
    tz_name: str,
    *,
    today_override: str | None = None,
) -> tuple[str, str | None]:
    """Resolve + validate the completed-at date.

    Returns ``(iso, error)``: ``iso`` is the resolved YYYY-MM-DD
    string (today when input was None), ``error`` is a human-readable
    rejection message (e.g. "completed_at 2027-01-01 is in the
    future" or "completed_at 'foo' is not a valid ISO date") or None
    on success.

    ``today_override`` (when supplied) takes precedence over the
    timezone-derived today for the future-date check + the
    default-when-empty value. Used by test fixtures that need to
    freeze the today-anchor while still exercising the validation
    logic. Production callers pass None → ``_today_iso(tz_name)``
    is used.

    Future-dating is rejected per dispatch. Operator clamping
    behavior: today's date in the configured timezone is the upper
    bound (inclusive). A completed_at exactly equal to today is
    allowed.
    """
    iso_today = today_override or _today_iso(tz_name)
    if completed_at is None or not str(completed_at).strip():
        return iso_today, None
    try:
        parsed = date_type.fromisoformat(str(completed_at).strip()[:10])
    except ValueError:
        return iso_today, (
            f"completed_at {completed_at!r} is not a valid ISO date "
            f"(expected YYYY-MM-DD)"
        )
    today = date_type.fromisoformat(iso_today)
    if parsed > today:
        return iso_today, (
            f"completed_at {parsed.isoformat()} is in the future "
            f"(today is {iso_today}); rejecting"
        )
    return parsed.isoformat(), None


@_json_stdout_safe
def cmd_done(
    config: RoutineConfig,
    record_name: str,
    item_text: str,
    *,
    wants_json: bool = False,
    today_override: str | None = None,
    completed_at: str | None = None,
) -> int:
    """Append a completion date to ``completion_log[item_text]`` on the
    record.

    ``record_name`` may be empty/whitespace to trigger vault-wide fuzzy
    match against all active routines' items.

    ``completed_at`` is an optional YYYY-MM-DD string (Phase 2B B1).
    None / empty → today. Future dates → rejected with
    ``DONE_KIND_FUTURE_DATE_REJECTED``.

    Returns exit code (0 on success or idempotent_noop, 1 on every
    other canary). Idempotent — re-runs with the same (record, item,
    date) are no-ops at the data layer (no duplicate date appended).

    On ``wants_json``: emits a structured payload with a ``kind``
    discriminator (one of the ``DONE_KIND_*`` constants) so the talker
    subprocess wrapper can route on it without parsing free-text
    error messages.
    """
    _check_salem_only(config)
    vault_path = Path(config.vault_path)

    # ---- Resolve completed_at + validate not-future ------------------
    # ``today_override`` is the legacy test-only knob (a single ISO
    # date string treated as "today"); ``completed_at`` is the new
    # operator-facing back-date flag. The validator helper handles
    # both — today_override (when supplied) wins as the today-anchor
    # for the future-date check, completed_at is the explicit
    # back-date input. NOTE-2 cleanup 2026-05-30 — the inline
    # validation logic that used to live here was duplicating the
    # helper; refactored to a single call site.
    iso, date_error = _validate_completed_at(
        completed_at,
        config.schedule.timezone,
        today_override=today_override,
    )
    if date_error is not None:
        return _emit_canary(
            wants_json=wants_json,
            kind=DONE_KIND_FUTURE_DATE_REJECTED,
            exit_code=1,
            message=date_error,
            payload={
                "completed_at_input": completed_at,
                "today": iso,
            },
        )

    # ---- Resolve record (strict-by-name OR vault-wide fuzzy) ---------
    # Two paths: (a) operator supplied ``record_name`` → strict lookup,
    # fall through to fuzzy on THAT record's items only; (b) operator
    # omitted ``record_name`` → vault-wide fuzzy across all active
    # routines.
    resolved_path: Path | None = None
    resolved_record: str = ""
    if record_name and record_name.strip():
        try:
            resolved_path = _routine_path(vault_path, record_name)
            resolved_record = record_name
        except FileNotFoundError:
            return _emit_canary(
                wants_json=wants_json,
                kind=DONE_KIND_UNKNOWN_RECORD,
                exit_code=1,
                message=(
                    f"Routine record {record_name!r} not found under "
                    f"{vault_path / 'routine'}"
                ),
                payload={"record_name_input": record_name},
            )
    else:
        # Vault-wide fuzzy: find the record by item text. Load the Phase-2
        # learned glossary ONCE here (not per _matches_item call — the scan
        # invokes the matcher per item) and thread it through. Empty/absent
        # glossary ⟹ unchanged matcher behaviour.
        from . import match_calibration as _mc
        _glossary = _mc.load_glossary(config.match_calibration.corpus_path)
        matches, all_candidates = _fuzzy_match_vault_wide(
            vault_path, item_text, _glossary,
        )
        if not matches:
            # Phase 3 — no-match / alias CAPTURE (best-effort, additive). The
            # closest plausible candidate is surfaced as a "did you mean…"
            # suggestion for operator review; a capture failure must NEVER
            # change the unknown_item canary the operator sees.
            try:
                _capture_no_match(
                    config, item_text, all_candidates, iso, _glossary,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "routine.match_calibration.no_match_capture_failed",
                    error=str(exc),
                    query=item_text,
                )
            return _emit_canary(
                wants_json=wants_json,
                kind=DONE_KIND_UNKNOWN_ITEM,
                exit_code=1,
                message=(
                    f"No active routine item matches {item_text!r}. "
                    f"Available items: "
                    f"{', '.join(c.item_text for c in all_candidates[:20])}"
                    f"{' (showing first 20)' if len(all_candidates) > 20 else ''}"
                ),
                payload={
                    "item_text_input": item_text,
                    "available_count": len(all_candidates),
                    "available_items": [
                        {"record": c.record_name, "item": c.item_text}
                        for c in all_candidates
                    ],
                },
            )
        if len(matches) > 1:
            return _emit_canary(
                wants_json=wants_json,
                kind=DONE_KIND_AMBIGUOUS_ITEM,
                exit_code=1,
                message=(
                    f"{item_text!r} matches {len(matches)} routine items. "
                    f"Ask back with the candidate list."
                ),
                payload={
                    "item_text_input": item_text,
                    "candidates": [
                        {"record": c.record_name, "item": c.item_text}
                        for c in matches
                    ],
                },
            )
        # Exactly one match — use it. Carry ``chosen.path`` directly
        # rather than re-resolving via ``_routine_path(record_name)``:
        # the latter does file-stem lookup, which crashes with
        # FileNotFoundError when the routine carries a frontmatter
        # ``name:`` different from the file stem. WARN-1 fix
        # 2026-05-30 — see ``_ItemCandidate.path`` docstring.
        chosen = matches[0]
        # P4 / Surface (b) — 2026-06-07: emit a per-match confidence
        # log so future false-positive analysis has data without
        # re-instrumenting the matcher. Operator-grep on
        # ``confidence=0.0`` surfaces every check-2 substring-only
        # match (the 2026-06-06 Tilray→Meds failure mode); higher
        # values surface progressively better matches. The log fires
        # ONLY on the single-match success path (here) — ambiguous
        # and no-match canaries already carry their own
        # operator-visible diagnostics.
        #
        # Per ``feedback_log_emission_test_pattern.md``: the log
        # shape is pinned by
        # ``test_routine_done_matched_log_emission_fires_on_success``
        # in ``tests/routine/test_routine_done_confidence.py``.
        confidence = _match_confidence(item_text, chosen.item_text)
        # The Step-5 ``if not wants_json`` gate on this matched-log (the
        # long-standing ``test_vault_wide_fuzzy_*`` JSONDecodeError) is now
        # SUBSUMED by the ``@_json_stdout_safe`` handler guard: on the json
        # path structlog is routed to stderr for the handler's duration, so
        # this line stays VISIBLE (per ``feedback_intentionally_left_blank``)
        # without polluting the stdout JSON contract. The confidence emission
        # test pins the shape on the ``wants_json=False`` path.
        log.info(
            "routine_done.matched",
            query=item_text,
            matched_to=chosen.item_text,
            record=chosen.record_name,
            confidence=confidence,
        )
        # Self-correcting matcher — Phase 1 CAPTURE. A LOW-confidence fuzzy
        # match (below the configured threshold) is captured to the pending
        # sink for operator review in the Daily Sync ``routine_match`` section.
        # GUARDRAIL: this writes ONLY to the pending sink — never the learned
        # glossary (which is mutated only by an operator reply). Best-effort:
        # a capture failure must NEVER break the completion (capture is
        # observability, the completion is the operator's actual intent).
        # Captured with the ORIGINAL query (``item_text`` here, before the
        # canonicalise reassignment below).
        #
        # Phase 2b loop-closer: skip re-capture when the operator has ALREADY
        # ruled on this (query_key, item) pair — a confirm verdict in the
        # glossary promoted this match, so re-surfacing it for review every
        # completion would be review-queue noise. (A reject verdict can't reach
        # the success path — it would have excluded the item from ``matches`` —
        # so the only reachable verdict here is confirm.)
        _already_ruled = (
            not _glossary.is_empty()
            and _glossary.verdict(_mc.query_key(item_text), chosen.item_text)
            is not None
        )
        if confidence < config.match_calibration.threshold and not _already_ruled:
            try:
                from datetime import datetime, timezone

                from . import match_calibration as _mc
                _mc.append_pending(
                    config.match_calibration.pending_path,
                    _mc.PendingMatch(
                        query=item_text,
                        matched_to=chosen.item_text,
                        record=chosen.record_name,
                        confidence=confidence,
                        completion_date=iso,
                        captured_at=datetime.now(timezone.utc).isoformat(),
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "routine.match_calibration.capture_failed",
                    error=str(exc),
                    query=item_text,
                    matched_to=chosen.item_text,
                )
        resolved_record = chosen.record_name
        item_text = chosen.item_text  # canonicalise to verbatim text
        resolved_path = chosen.path

    assert resolved_path is not None  # narrowing
    path = resolved_path

    # ---- Load record + completion_log --------------------------------
    post = frontmatter.load(str(path))
    fm = dict(post.metadata or {})

    completion_log_raw = fm.get("completion_log") or {}
    if not isinstance(completion_log_raw, dict):
        # Operator hand-edit dropped the dict — restore.
        log.warning(
            "routine.cli.completion_log_not_dict",
            path=str(path),
            type=type(completion_log_raw).__name__,
        )
        completion_log_raw = {}
    completion_log: dict[str, list[str]] = {}
    for key, val in completion_log_raw.items():
        # Normalise: each value should be a list of ISO date strings.
        # Tolerate scalar-as-single-list and YAML-native date objects.
        if isinstance(val, list):
            normalised: list[str] = []
            for v in val:
                if isinstance(v, date_type):
                    normalised.append(v.isoformat())
                elif isinstance(v, str):
                    normalised.append(v)
                else:
                    log.debug(
                        "routine.cli.skipping_bad_log_entry",
                        key=str(key), value=repr(v),
                    )
            completion_log[str(key)] = normalised
        elif isinstance(val, (str, date_type)):
            completion_log[str(key)] = [
                val.isoformat() if isinstance(val, date_type) else val
            ]
        else:
            completion_log[str(key)] = []

    # ---- Verify item exists on this specific record ------------------
    # When record_name was supplied explicitly, the strict + fuzzy
    # cascade applies: strict text-equality first, then fuzzy on the
    # record's items. When record_name was empty (vault-wide fuzzy
    # already canonicalised item_text above), this is just a sanity
    # pass — item_text WILL be in known_texts by construction.
    raw_items = fm.get("items") or []
    if not isinstance(raw_items, list):
        raw_items = []
    known_items: list[_ItemCandidate] = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        t = str((it or {}).get("text") or "").strip()
        if t:
            known_items.append(_ItemCandidate(
                record_name=resolved_record,
                item_text=t,
                path=path,
            ))
    known_texts = {c.item_text for c in known_items}
    if item_text not in known_texts:
        # Fall through to fuzzy match on THIS record's items.
        on_record_matches = [
            c for c in known_items if _matches_item(item_text, c.item_text)
        ]
        if not on_record_matches:
            return _emit_canary(
                wants_json=wants_json,
                kind=DONE_KIND_UNKNOWN_ITEM,
                exit_code=1,
                message=(
                    f"Item {item_text!r} not found on routine "
                    f"{resolved_record!r}. Known items: "
                    f"{sorted(known_texts) if known_texts else '(none)'}"
                ),
                payload={
                    "item_text_input": item_text,
                    "record": resolved_record,
                    "known_items": sorted(known_texts),
                },
            )
        if len(on_record_matches) > 1:
            return _emit_canary(
                wants_json=wants_json,
                kind=DONE_KIND_AMBIGUOUS_ITEM,
                exit_code=1,
                message=(
                    f"{item_text!r} matches {len(on_record_matches)} "
                    f"items on {resolved_record!r}. Ask back."
                ),
                payload={
                    "item_text_input": item_text,
                    "record": resolved_record,
                    "candidates": [
                        {"record": c.record_name, "item": c.item_text}
                        for c in on_record_matches
                    ],
                },
            )
        # Exactly one fuzzy match on this record — canonicalise.
        item_text = on_record_matches[0].item_text

    # ---- Idempotent append -------------------------------------------
    existing = completion_log.get(item_text, [])
    if iso in existing:
        new_list = existing
        appended = False
    else:
        new_list = existing + [iso]
        appended = True

    if not appended:
        # Idempotent no-op: skip the write entirely (no point
        # round-tripping the same content). Fire BOTH log events
        # ONLY in plain-text mode:
        #   * ``routine.cli.done`` with ``appended=False`` — the
        #     pre-B1 contract, pinned by regression test
        #     ``test_done_emits_log_event``. Preserved verbatim for
        #     plain-text invocations so the observability surface
        #     stays backwards-compatible.
        #   * ``routine.cli.done.idempotent_noop`` — the B1 addition,
        #     finer-grained signal that the canary path took the
        #     idempotent branch.
        #
        # **Why suppress on ``wants_json``**: structlog's default sink
        # writes log events to stdout in CLI process context, which
        # interleaves the rendered log line with the JSON canary
        # output. The talker subprocess wrapper expects single-line
        # parseable JSON on stdout (per the reversed-LINE-scan
        # pattern in :func:`alfred.telegram.conversation.
        # _dispatch_routine_done`); the structlog line breaks that
        # contract. The canary JSON IS the structured event for
        # JSON-mode invocations — the ``kind`` field carries the same
        # ``success`` / ``idempotent_noop`` signal the log events
        # convey. Tests that need to pin the structlog emission use
        # ``wants_json=False`` paths.
        if not wants_json:
            log.info(
                "routine.cli.done",
                record=resolved_record,
                item=item_text,
                date=iso,
                appended=False,
                path=str(path.relative_to(vault_path)),
            )
            log.info(
                "routine.cli.done.idempotent_noop",
                record=resolved_record,
                item=item_text,
                date=iso,
                path=str(path.relative_to(vault_path)),
            )
        return _emit_canary(
            wants_json=wants_json,
            kind=DONE_KIND_IDEMPOTENT_NOOP,
            exit_code=0,
            message=(
                f"Already logged: {resolved_record} / {item_text} @ {iso}"
            ),
            payload={
                "record": resolved_record,
                "item": item_text,
                "date": iso,
                "path": str(path.relative_to(vault_path)),
                "appended": False,
            },
        )

    completion_log[item_text] = new_list
    fm["completion_log"] = completion_log

    # Round-trip: frontmatter.dumps re-emits the file with the mutated
    # metadata. We bypass ``vault_edit`` here because routine completion
    # logging is a structured frontmatter mutation that doesn't fit the
    # set_fields shape (per-key value-list append) and the Salem-only
    # guard above is the operative gate.
    new_post = frontmatter.Post(post.content, **fm)
    # frontmatter.dumps uses ``yaml.safe_dump`` internally, which sorts
    # keys by default. We want to preserve the operator's original key
    # order — emit the frontmatter ourselves with sort_keys=False.
    fm_yaml = yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False)
    out = f"---\n{fm_yaml}---\n\n{new_post.content}\n"
    path.write_text(out, encoding="utf-8")

    # Suppress the legacy ``routine.cli.done`` structlog event in
    # JSON mode — structlog's stdout sink would interleave with the
    # JSON canary and break wrapper / test parseability. The canary
    # IS the structured event in JSON mode. See ``_emit_canary``
    # docstring + the idempotent-branch comment above for the full
    # rationale. Plain-text mode keeps emitting the log line per
    # the regression-pin ``test_done_emits_log_event``.
    if not wants_json:
        log.info(
            "routine.cli.done",
            record=resolved_record,
            item=item_text,
            date=iso,
            appended=appended,
            path=str(path.relative_to(vault_path)),
        )
    return _emit_canary(
        wants_json=wants_json,
        kind=DONE_KIND_SUCCESS,
        exit_code=0,
        message=f"Logged: {resolved_record} / {item_text} @ {iso}",
        payload={
            "record": resolved_record,
            "item": item_text,
            "date": iso,
            "path": str(path.relative_to(vault_path)),
            "appended": True,
        },
    )


def _emit_canary(
    *,
    wants_json: bool,
    kind: str,
    exit_code: int,
    message: str,
    payload: dict[str, Any],
) -> int:
    """Emit either JSON (canary discriminator) or plain text + return.

    JSON shape carries ``ok`` (back-compat with pre-B1 consumers),
    ``kind`` (the new B1 discriminator constant), ``error`` (when
    exit_code != 0 OR kind == idempotent_noop, the human-readable
    message), plus a payload-flat union of the canary-specific
    fields. The ``ok`` field is True for success AND
    idempotent_noop — both are non-error states from the caller's
    POV.

    Pre-B1 the JSON shape was ``{ok, record, item, date, appended,
    path}`` OR ``{ok: False, error}``. New shape is the union: every
    field that COULD be useful is present, the canary tells the
    caller which fields apply.

    **Single-line JSON contract.** The JSON payload is emitted as
    ``json.dumps(body)`` with NO ``indent`` argument — produces a
    single line. Two reasons (both surfaced by failing tests on the
    first B1 ship):

      1. The talker subprocess wrapper in
         :mod:`alfred.telegram.conversation` does a reversed-LINE scan
         (mirroring the ``migrate_tier_phase1.py`` structlog-pollution
         defense pattern). A pretty-printed multi-line payload puts
         ``{`` and ``}`` on separate lines; neither line parses as
         standalone JSON; the wrapper falls through to the
         "no parseable canary" failure path.
      2. Test fixtures use ``json.loads(capsys.readouterr().out)`` to
         pin the canary contract. Multi-line stdout (especially when
         the legacy ``routine.cli.done`` structlog event interleaves
         with the canary) makes that parse fail unpredictably.

    The trade-off: single-line JSON is less human-readable when an
    operator runs ``alfred routine done X Y --json`` and reads the
    output directly. The plain-text mode (when ``wants_json=False``)
    is the human-readable surface; ``--json`` is the
    machine-readable / wrapper surface and prioritises
    parseability.
    """
    ok = exit_code == 0
    if wants_json:
        import json
        body: dict[str, Any] = {"ok": ok, "kind": kind}
        # ``not_logged`` (un-log no-op) is the exit-0 analog of
        # ``idempotent_noop`` — surface its human-readable message in JSON too
        # (the ILB "you hadn't logged that" signal the talker voices). Both are
        # non-error exit-0 states, so the message lands under ``message``.
        if not ok or kind in (DONE_KIND_IDEMPOTENT_NOOP, ITEM_KIND_NOT_LOGGED):
            body["error" if not ok else "message"] = message
        body.update(payload)
        # Single-line JSON — see docstring for the rationale (subprocess
        # wrapper line-scan + test fixture parseability).
        print(json.dumps(body))
    else:
        # Plain-text: success / idempotent_noop go to stdout; error
        # canaries go to stderr.
        stream = sys.stderr if not ok else sys.stdout
        prefix = (
            "" if ok else f"[{kind}] "
        )
        print(f"{prefix}{message}", file=stream)
    return exit_code


@_json_stdout_safe
def cmd_run_now(
    config: RoutineConfig,
    *,
    wants_json: bool = False,
    today_override: str | None = None,
) -> int:
    """Force-build today's daily aggregator note. Useful for ad-hoc runs."""
    _check_salem_only(config)
    state_mgr = StateManager(config.state.path)
    state_mgr.load()
    if today_override:
        today = date_type.fromisoformat(today_override)
    else:
        today = datetime.now(ZoneInfo(config.schedule.timezone)).date()
    rel_path = run_aggregator_once(config, today, state_mgr)
    if wants_json:
        import json
        print(json.dumps({
            "ok": True,
            "date": today.isoformat(),
            "path": rel_path,
        }, indent=2))
    else:
        print(f"Aggregator wrote: {rel_path}")
    return 0


@_json_stdout_safe
def cmd_status(config: RoutineConfig, *, wants_json: bool = False) -> int:
    """Print last run + schedule summary."""
    _check_salem_only(config)
    state_mgr = StateManager(config.state.path)
    state_mgr.load()
    latest = state_mgr.state.latest()
    payload: dict[str, Any] = {
        "schedule": {
            "time": config.schedule.time,
            "timezone": config.schedule.timezone,
        },
        "vault_path": config.vault_path,
        "instance_name": config.instance_name,
        "latest": latest.to_dict() if latest else None,
        "run_count": len(state_mgr.state.runs),
    }
    if wants_json:
        import json
        print(json.dumps(payload, indent=2))
        return 0

    print("=" * 60)
    print("ALFRED ROUTINE STATUS")
    print("=" * 60)
    print(f"Schedule:      {config.schedule.time} {config.schedule.timezone}")
    print(f"Instance:      {config.instance_name}")
    print(f"Vault:         {config.vault_path}")
    if latest:
        print(f"Last run:      {latest.generated_at}")
        print(f"  date:        {latest.date}")
        print(f"  path:        {latest.vault_path}")
        print(f"  routines:    {latest.routines_contributing}")
        print(f"  items:       {latest.item_count}")
        print(f"  critical:    {latest.critical_pending}")
    else:
        # Per intentionally-left-blank: emit visible "no run yet" rather
        # than silence.
        print("Last run:      never")
    print(f"Runs recorded: {len(state_mgr.state.runs)}")
    return 0


__all__ = [
    # Public command handlers (consumed by alfred.cli's cmd_routine).
    "cmd_done",
    "cmd_run_now",
    "cmd_status",
    # Phase 2B B3 — re-export of the item-CRUD handlers from
    # cli_items.py (deferred import at the bottom of this module
    # to avoid circular-import deadlock with cli_items.py importing
    # B1 helpers + canary constants from here). Same source-of-truth
    # pattern as cmd_done above — the routine subsystem's CLI surface
    # is now bigger than fits one module, but the import-path stays
    # unified.
    "cmd_item_add",
    "cmd_item_remove",
    "cmd_item_edit",
    "cmd_undone",
    # Phase 2B B1 cross-agent contract — canary kind discriminator
    # constants. Talker subprocess wrapper imports these so the
    # raw-string literals don't drift between layers. SKILL.md's
    # "Marking routines done" section quotes the string values
    # verbatim; rename here = update SKILL.md + the talker
    # dispatcher import in lockstep.
    "DONE_KIND_SUCCESS",
    "DONE_KIND_UNKNOWN_RECORD",
    "DONE_KIND_UNKNOWN_ITEM",
    "DONE_KIND_AMBIGUOUS_ITEM",
    "DONE_KIND_IDEMPOTENT_NOOP",
    "DONE_KIND_FUTURE_DATE_REJECTED",
    "DONE_KIND_TIMEOUT",
    "DONE_KIND_SUBPROCESS_ERROR",
    # Phase 2B B3 — item-CRUD canary kinds. Same lockstep pattern
    # as DONE_KIND_*; SKILL.md's "Adjusting routines" section quotes
    # the string values verbatim; rename here = update SKILL.md +
    # the talker dispatcher import in lockstep.
    "ITEM_KIND_ADDED",
    "ITEM_KIND_REMOVED",
    "ITEM_KIND_EDITED",
    "ITEM_KIND_UNKNOWN_RECORD",
    "ITEM_KIND_UNKNOWN_ITEM",
    "ITEM_KIND_AMBIGUOUS_ITEM",
    "ITEM_KIND_CADENCE_CONFLICT",
    "ITEM_KIND_DUPLICATE_ITEM",
    "ITEM_KIND_INVALID_FIELD",
]


# Phase 2B B3 — deferred import of the item-CRUD handlers from
# cli_items.py. Placed at the BOTTOM of the module so cli_items.py's
# import of the canary constants + helpers from this module
# (`from .cli import ITEM_KIND_*, _check_salem_only, ...`) resolves
# against the already-defined symbols above. Circular import works
# because Python's import machinery resolves the partially-loaded
# `cli` module when `cli_items` imports from it — every symbol
# cli_items.py needs is defined above this line.
from .cli_items import (  # noqa: E402, F401
    cmd_item_add,
    cmd_item_edit,
    cmd_item_remove,
    cmd_undone,
)
