"""The Jeeves capture gate — fail-closed, and JEEVES'S OWN (task #81).

The STAY-C sovereign scribe has a gate of exactly this shape
(``alfred.scribe.ingest.guard_ingest``): a mode flag that IS the legal line,
a fail-closed default, and one place where the mode decides whether input
may be processed. **This module copies that POSTURE and shares none of its
code**, which is a deliberate design constraint rather than an accident of
convenience.

The reason is the whole fence-5 argument. That scribe carries PHI under
attestation, retention and anti-spoliation rules. Jeeves is a garage
microphone in a personal/RRTS space. Two ambient scribes on one codepath is
precisely how personal audio ends up somewhere it needs consent to be — one
refactor that "unifies the gates", one mode enum that gains a third value,
and the separation that was structural becomes a code review nobody runs.
So: no import, no shared base class, no shared constant.
``alfred.jeeves`` imports nothing from ``alfred.scribe``, and
``tests/test_jeeves_fences.py`` walks the package source to prove it.

The line itself: ``synthetic`` (the fail-closed default) processes ONLY
captures carrying ``synthetic: true`` provenance. ``live`` — reachable only
by writing exactly ``mode: live`` in config — lets real garage audio
through. Absent, None, unknown and malformed all resolve to synthetic, so a
typo cannot arm a microphone.

TWO GATES LIVE HERE, and they are separate functions on purpose.
:func:`guard_capture` is the MODE line above, and it runs on both sides of
the peer link (the device, and the receiving instance's capture route).
:func:`guard_not_suspended` is the COMPANY TOGGLE (#98, ruling 3) and runs
on the DEVICE ONLY — suspension is a property of a microphone, and the
receiving instance does not have one. Its docstring argues that at length,
because folding the two together is the obvious-looking refactor and it
would make a peer refuse every capture the device successfully sent.
"""

from __future__ import annotations

from typing import Any

import structlog

from . import suspend
from .config import JEEVES_MODE_LIVE, JEEVES_MODE_SYNTHETIC, JeevesConfig

log = structlog.get_logger(__name__)

#: Refusal reasons. Greppable, and distinct from the accept reasons so an
#: operator reading a log can count either without reading the other.
REFUSED_MISSING_SYNTHETIC = "missing_synthetic_provenance"
REFUSED_SUSPENDED = "capture_suspended"
ACCEPTED_LIVE_MODE = "live_mode"
ACCEPTED_SYNTHETIC_PROVENANCE = "synthetic_provenance_present"
ACCEPTED_NOT_SUSPENDED = "not_suspended"


class JeevesCaptureRefused(Exception):
    """Raised when the mode gate refuses a capture (fail-closed).

    ``reason`` is a greppable id, ``mode`` the resolved mode, ``source_id``
    names the refused capture for triage. Carries NO transcript and no audio
    — an exception message is one of the places content leaks most easily,
    because it ends up in tracebacks, logs and error reports at once.
    """

    def __init__(
        self, reason: str, detail: str, mode: str, source_id: str = "",
    ) -> None:
        self.reason = reason
        self.detail = detail
        self.mode = mode
        self.source_id = source_id
        super().__init__(f"jeeves capture refused [{reason}]: {detail}")


class JeevesCaptureSuspended(JeevesCaptureRefused):
    """Raised when the COMPANY TOGGLE refuses a capture (#98, ruling 3).

    A subclass so a caller that only cares "was this capture refused" needs
    no change, while one that has to tell the operator WHY can catch this
    specifically — "you asked me to stop listening" and "this instance is in
    synthetic mode" are the same refusal and completely different sentences.

    Carries the :class:`~alfred.jeeves.suspend.SuspendStatus` that refused
    it, so the vehicle UI can render the reason without re-reading the store
    (and without the two disagreeing across the read).
    """

    def __init__(
        self,
        reason: str,
        detail: str,
        mode: str,
        source_id: str = "",
        *,
        status: suspend.SuspendStatus | None = None,
    ) -> None:
        super().__init__(reason, detail, mode, source_id)
        self.status = status


def _provenance_is_synthetic(provenance: Any) -> bool:
    """STRICT synthetic test — fail-closed.

    Only a dict carrying the literal boolean ``synthetic: True`` counts. A
    missing tag, the STRING ``"true"``, ``1``, ``None``, a non-dict — all
    False, all refused in synthetic mode. Accepting truthy values would mean
    a YAML quirk or a JSON round-trip could arm the live path.
    """
    return isinstance(provenance, dict) and provenance.get("synthetic") is True


def _log_decision(
    *, mode: str, accepted: bool, reason: str, source_id: str,
) -> None:
    """Exactly one decision event per capture, accepted or not.

    Intentionally-left-blank applied to a gate: a refused capture and an idle
    device must not look the same in a log, because one of them means the
    operator is speaking to something that is deliberately ignoring him.
    """
    log.info(
        "jeeves.capture_decision",
        mode=mode,
        accepted=accepted,
        reason=reason,
        source_id=source_id,
    )


def guard_capture(
    config: JeevesConfig,
    *,
    provenance: Any,
    source_id: str = "",
) -> None:
    """Fail-closed mode gate. Emits one decision event; raises on refusal.

    * ``live`` mode → accept.
    * ANY other mode (synthetic, or — defensively — an unrecognised value) →
      accept ONLY if ``provenance`` carries ``synthetic: true``.

    The live branch is gated on the EXACT ``config.mode == "live"``, so a
    config whose mode did not normalize falls through to the
    synthetic-required branch. An unknown mode can never open the live path
    even if :func:`~alfred.jeeves.config._normalize_mode` were bypassed —
    defense in depth, because this gate is what stands between an
    always-listening microphone and the network.
    """
    mode = config.mode

    if mode == JEEVES_MODE_LIVE:
        _log_decision(
            mode=mode, accepted=True, reason=ACCEPTED_LIVE_MODE,
            source_id=source_id,
        )
        return

    if _provenance_is_synthetic(provenance):
        _log_decision(
            mode=JEEVES_MODE_SYNTHETIC, accepted=True,
            reason=ACCEPTED_SYNTHETIC_PROVENANCE, source_id=source_id,
        )
        return

    _log_decision(
        mode=JEEVES_MODE_SYNTHETIC, accepted=False,
        reason=REFUSED_MISSING_SYNTHETIC, source_id=source_id,
    )
    raise JeevesCaptureRefused(
        REFUSED_MISSING_SYNTHETIC,
        "this capture lacks a synthetic:true provenance tag and Jeeves is in "
        "synthetic mode (the fail-closed default). Real garage audio is not "
        "processed until jeeves.mode is deliberately set to 'live' — one "
        "config edit, made once the capture device is actually deployed.",
        JEEVES_MODE_SYNTHETIC,
        source_id,
    )


def guard_not_suspended(
    config: JeevesConfig, *, source_id: str = "",
) -> suspend.SuspendStatus:
    """The COMPANY-TOGGLE gate — a SECOND, DEVICE-ONLY line (#98, ruling 3).

    Deliberately NOT folded into :func:`guard_capture`, and the reason is
    the one thing worth reading in this docstring. ``guard_capture`` has two
    callers: this package's capture service, which runs ON the garage
    device, and ``alfred.transport.routes_jeeves``, which runs on the
    RECEIVING instance and validates an inbound POST. Suspension is a
    property of a microphone. The receiving instance has no microphone, and
    its copy of ``jeeves.suspend_state_path`` names a file on the wrong
    machine — one that has never been written, which is the fail-closed
    case. Putting the check inside the shared gate would therefore make a
    peer refuse every capture the device successfully sent, with a reason
    that is true of neither of them.

    So: same MODULE (this is where the fences live), same posture, separate
    entry point, called only from the device side.

    Raises :class:`JeevesCaptureSuspended` when suspended; returns the
    status otherwise, so a caller that wants to log the resolved state does
    not have to read the store twice.
    """
    status = suspend.read_status(config.suspend_state_path)
    if not status.suspended:
        log.info(
            "jeeves.suspend_decision",
            accepted=True,
            reason=ACCEPTED_NOT_SUSPENDED,
            state_reason=status.reason,
            source_id=source_id,
        )
        return status

    log.info(
        "jeeves.suspend_decision",
        accepted=False,
        reason=REFUSED_SUSPENDED,
        state_reason=status.reason,
        fail_closed=status.fail_closed,
        source_id=source_id,
    )
    raise JeevesCaptureSuspended(
        REFUSED_SUSPENDED,
        "Jeeves is SUSPENDED, so this capture was refused before anything "
        "was extracted, transcribed or sent. "
        + (status.detail or "The company toggle is on.")
        + " Release it with the manual control or the spoken release phrase.",
        config.mode,
        source_id,
        status=status,
    )
