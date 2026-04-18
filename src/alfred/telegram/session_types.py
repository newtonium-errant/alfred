"""Session type defaults for the opening-cue router.

Each session type carries a bundle of defaults: which Anthropic model to use,
whether the type supports continuation, and how aggressively to push back on
the user mid-conversation (a calibration hint surfaced later in Stage 2a).

This module is pure data + a lookup helper. The router (``router.py``) calls
:func:`defaults_for` after classifying the opening cue; the bot applies those
defaults when opening the session. Values here match the table in the voice
design doc (``vault/session/Voice Chat and Calibration Design 2026-04-15.md``,
"Session types" section).

Opus model id: ``claude-opus-4-7`` is the wk2 default (per team-lead decision
on plan open question #1). If that alias is rejected at runtime, the caller
should log and fall back to ``claude-opus-4-5`` — we do NOT second-guess the
id here because backfilling a defensive fallback belongs at the call site
(only it has the error context to decide whether to retry).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


# --- Model constants ------------------------------------------------------

# The router itself runs on Sonnet — cheap + fast classification. Pinned
# constant rather than config for wk2 (plan open question #7). Promote to
# ``config.yaml`` if we need to swap models without a code change.
ROUTER_MODEL: Final[str] = "claude-sonnet-4-6"

# Session-type defaults. These names are what the router emits; the keys
# here must stay in sync with the JSON schema in ``router.py``.
_SONNET: Final[str] = "claude-sonnet-4-6"
_OPUS: Final[str] = "claude-opus-4-7"


@dataclass(frozen=True)
class SessionTypeDefaults:
    """Defaults for one session type.

    Attributes:
        session_type: Canonical name used in frontmatter and state.
        model: Anthropic model id to start the session on.
        supports_continuation: Whether a "continue previous" cue should look
            for a prior session record of this type and seed transcript.
        pushback_frequency: Rough hint for how often the assistant should
            offer opinions / challenge assumptions (``low|medium|high``).
            Not wired into the turn loop yet — recorded here so wk3+ can
            consume it without another schema change.
    """

    session_type: str
    model: str
    supports_continuation: bool
    pushback_frequency: str


_DEFAULTS_TABLE: Final[dict[str, SessionTypeDefaults]] = {
    "note": SessionTypeDefaults(
        session_type="note",
        model=_SONNET,
        supports_continuation=False,
        pushback_frequency="low",
    ),
    "task": SessionTypeDefaults(
        session_type="task",
        model=_SONNET,
        supports_continuation=False,
        pushback_frequency="none",
    ),
    "journal": SessionTypeDefaults(
        session_type="journal",
        model=_SONNET,
        supports_continuation=True,
        pushback_frequency="high",
    ),
    "article": SessionTypeDefaults(
        session_type="article",
        model=_OPUS,
        supports_continuation=True,
        pushback_frequency="medium",
    ),
    "brainstorm": SessionTypeDefaults(
        session_type="brainstorm",
        model=_SONNET,
        supports_continuation=True,
        pushback_frequency="medium",
    ),
}


# Default fallback when the router errors or returns garbage. ``note`` is
# the safest bet: cheap model, no continuation, low push-back — indistinguishable
# from the wk1 behaviour where every session was effectively a "note".
_FALLBACK_TYPE: Final[str] = "note"


def defaults_for(session_type: str | None) -> SessionTypeDefaults:
    """Return defaults for ``session_type``, falling back to ``note``.

    An unknown or missing type is treated as ``note`` — identical to the wk1
    default, which means any router failure degrades gracefully.
    """
    if not session_type:
        return _DEFAULTS_TABLE[_FALLBACK_TYPE]
    return _DEFAULTS_TABLE.get(session_type, _DEFAULTS_TABLE[_FALLBACK_TYPE])


def known_types() -> list[str]:
    """Return the sorted list of canonical session-type names."""
    return sorted(_DEFAULTS_TABLE.keys())
