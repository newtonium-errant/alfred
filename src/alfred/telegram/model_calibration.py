"""Model-selection calibration scaffold.

Wk3 commit 8. The calibration block on the user's person record has a
"Model Preferences (learned)" subsection. This module is the two-way
bridge between that subsection and the session-open model selection:

- **Read path**: :func:`parse_model_preferences` reads the subsection's
  bullets, returning a dict keyed by session type → ``ModelPref``. The
  bot's session opener consults this before threading the router's
  model choice so Andrew-learned defaults override type defaults.

- **Update path**: :func:`propose_default_flip` inspects the recent
  closed-session history and, when the same session type has been
  escalated mid-session 3 times in the last 5 sessions of that type,
  returns a :class:`calibration.Proposal` suggesting we flip the type's
  default model. The caller pipes the proposal into commit 7's
  :func:`calibration.apply_proposals` — meaning the model-default
  suggestion rides the same dial + marker machinery as any other
  calibration update, surfaced inline at the next ``/end``.

The threshold (``MODEL_CAL_THRESHOLD``) and window (``MODEL_CAL_WINDOW``)
are tuned to avoid recommending flips on one-off escalations while
reacting quickly enough that a real pattern doesn't take weeks to surface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from .calibration import Proposal
from .state import StateManager
from .utils import get_logger

log = get_logger(__name__)


MODEL_CAL_THRESHOLD: Final[int] = 3
MODEL_CAL_WINDOW: Final[int] = 5


# Canonical model IDs — mirror bot._OPUS_MODEL / _SONNET_MODEL. Kept here
# as well so parse_model_preferences can recognise them without a
# circular import against bot.py.
_OPUS_MODEL: Final[str] = "claude-opus-4-7"
_SONNET_MODEL: Final[str] = "claude-sonnet-4-6"


@dataclass(frozen=True)
class ModelPref:
    """One parsed "Model Preferences (learned)" bullet.

    Attributes:
        session_type: One of the canonical types (note/task/journal/article/brainstorm).
        model: Anthropic model id to use as the opening model for this type.
        raw: The original bullet text — preserved so logs can show the
            exact line Andrew wrote (and so we can eventually warn if
            the parsed `model` drifts from whatever alias is current).
    """

    session_type: str
    model: str
    raw: str


# Bullet line shape: ``- <session_type>: <model_id>`` with optional
# italic attribution (_source: …_) or [needs confirmation] suffix.
# Kept permissive because this is user-editable text; if the parse fails
# we just log and move on rather than raising.
_MODEL_PREF_LINE_RE = re.compile(
    r"^\s*-\s*"
    r"(?P<session_type>note|task|journal|article|brainstorm)"
    r"\s*[:\u2192-]\s*"  # colon, arrow, or dash
    r"(?P<model>claude-[a-z0-9\-]+)",
    re.IGNORECASE | re.MULTILINE,
)


def parse_model_preferences(calibration_str: str | None) -> dict[str, ModelPref]:
    """Parse the "Model Preferences (learned)" subsection.

    Returns a dict keyed by session type (lower-cased) → ``ModelPref``.

    Tolerates missing subsection, empty subsection, malformed bullets —
    anything unparseable is skipped and logged. The read path falls back
    to session-type defaults for any type not present in the result, so
    an empty dict is a valid "no calibration-based override" answer.
    """
    if not calibration_str:
        return {}

    # Find the Model Preferences subsection in the calibration body.
    # Match ``## Model Preferences (learned)`` heading up to the next
    # ``## `` heading or end of string.
    section_re = re.compile(
        r"##\s*Model Preferences[^\n]*\n(.*?)(?=\n##\s|\Z)",
        re.DOTALL,
    )
    section_match = section_re.search(calibration_str)
    if section_match is None:
        return {}

    section_body = section_match.group(1)
    prefs: dict[str, ModelPref] = {}
    for line in section_body.splitlines():
        m = _MODEL_PREF_LINE_RE.match(line)
        if not m:
            continue
        session_type = m.group("session_type").lower()
        model = m.group("model").lower()
        if session_type in prefs:
            log.info(
                "talker.model_cal.duplicate_type_in_block",
                session_type=session_type,
                prior=prefs[session_type].raw,
                new=line,
            )
            # Last-write-wins — matches how the apply_proposals writer
            # produces updates (append, so the newer line is lower).
        prefs[session_type] = ModelPref(
            session_type=session_type,
            model=model,
            raw=line.strip(),
        )
    return prefs


def propose_default_flip(
    session_type: str,
    state_mgr: StateManager,
) -> Proposal | None:
    """Return a Proposal if the recent history warrants flipping the default.

    Looks at the most recent :data:`MODEL_CAL_WINDOW` closed sessions of
    the same type. If at least :data:`MODEL_CAL_THRESHOLD` of them were
    escalated mid-session (``model != opening_model``), emits a Proposal
    suggesting the calibration block record the escalated-to model as
    the default for this type.

    The caller pipes the return value into
    :func:`alfred.telegram.calibration.apply_proposals`, same path as any
    other calibration update — meaning it rides the confirmation dial.
    """
    closed = state_mgr.state.get("closed_sessions", []) or []
    # Filter to this type, most-recent first, cap at the window.
    same_type = [
        e for e in reversed(closed)
        if (e.get("session_type") or "note") == session_type
    ][:MODEL_CAL_WINDOW]

    if len(same_type) < MODEL_CAL_THRESHOLD:
        # Not enough history yet.
        return None

    escalated = [e for e in same_type if _was_escalated(e)]
    if len(escalated) < MODEL_CAL_THRESHOLD:
        return None

    # Target model: most-common "to" model among escalated sessions.
    # Ties break to the first encountered (which is the most recent).
    counts: dict[str, int] = {}
    for entry in escalated:
        target = entry.get("model") or entry.get("closing_model")
        if not target:
            continue
        counts[target] = counts.get(target, 0) + 1
    if not counts:
        return None
    target_model = max(counts.items(), key=lambda kv: kv[1])[0]

    bullet = (
        f"{session_type}: {target_model} "
        f"(escalated in {len(escalated)} of the last {len(same_type)} "
        f"{session_type} sessions)"
    )
    return Proposal(
        subsection="Model Preferences (learned)",
        bullet=bullet,
        confidence=0.8,
    )


def _was_escalated(closed_entry: dict) -> bool:
    """True if a closed-session entry had a mid-session model change.

    Session-type entries written pre-commit 8 don't carry ``opening_model``,
    so the field is looked up defensively. Without both fields we can't
    tell whether an escalation happened, so those entries are counted as
    non-escalated (conservative — we'd rather miss a flip suggestion
    than invent one on missing data).
    """
    opening = closed_entry.get("opening_model")
    final = closed_entry.get("model") or closed_entry.get("closing_model")
    if not opening or not final:
        return False
    return opening != final
