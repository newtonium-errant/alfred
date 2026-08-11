"""The company toggle — one suspended state, two doors (task #98, ruling 3).

The operator's ruling: *"Jeeves, company"* suspends wake-word and capture
until a release phrase, **with visible indication that the suspension took
effect, PLUS a manual button as an equal second path — both set the same
state, both logged, and the state survives a client restart (fail-closed: if
the state is unknown → suspended)."*

TWO DOORS, ONE TRANSITION FUNCTION. The spoken path (:mod:`.cues` classifies
``CUE_SUSPEND`` / ``CUE_RESUME``, :mod:`.service` dispatches it) and the
manual path (a button in the vehicle UI, calling :func:`set_suspended`
directly) do not each have their own idea of what suspension means. They are
the same call with a different ``source``. That is the design constraint the
ruling actually imposes: "spoken-or-manual interchangeable" is only true if
the two doors cannot drift, and two code paths that both write a flag WILL
drift — one of them will forget the log line, or the atomic write, or the
0600.

**The manual door is OUT OF PROCESS.** The button lives in a UI; the capture
loop lives in the service. So the state is a FILE, read fresh on every check
rather than cached — a cached flag would mean pressing the button does
nothing until the device is restarted, which is precisely when the operator
is standing in the room watching for it to take effect. That read is a
sub-millisecond stat+parse on a path the OS has cached, and it happens once
per cue and once per audio chunk while suspended, so the cost is not the
consideration; correctness across two processes is.

FAIL-CLOSED, AND WHAT THAT COSTS. A state that cannot be read resolves to
SUSPENDED — unreadable, unparseable, malformed, and **missing**. The last one
is the deliberate one, because it is the case that carries a cost:

* A missing file after a suspension is the exact harm the ruling names. The
  file was written, something removed it (an Android "clear app data", a
  reinstall, a card swap, a botched deploy), and the device comes back
  LISTENING with the visitor still in the room. There is no signal, and the
  operator has no reason to look.
* A missing file on a device that has never suspended is a first boot. The
  device is deaf until someone releases it once, and it says so loudly — in
  the log, and (Part C) on screen with the reason rendered.

The two are indistinguishable at read time, so the tie goes to the
microphone: one failure is visible and recoverable in five seconds, the other
is invisible and is the failure the toggle exists to prevent. First boot pays
one button press; :data:`REASON_NO_STATE_FILE` is its own reason string so
the UI can say "never initialised" rather than "suspended".

THE ONE EXCEPTION, and why it is not a hole: an EMPTY ``state_path`` means
the store is not configured at all, which resolves to NOT suspended and logs
loudly. Fail-closed applies to a state we tried to read and could not
understand; it cannot apply to a feature with nowhere to keep its state,
because that reading would leave every hand-constructed
:class:`~alfred.jeeves.config.JeevesConfig` permanently suspended with no
file to release it. ``load_from_unified`` always DERIVES a non-empty path
(pinned in ``tests/test_jeeves_config.py``), so no loaded config can be in
this state — only a config built field-by-field in a test can.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from . import telemetry

log = structlog.get_logger(__name__)

#: Which door drove a transition. A CLOSED set — it reaches a telemetry row,
#: where an unbounded string would be a content leak (see :mod:`.telemetry`).
SOURCE_SPOKEN = "spoken"
SOURCE_MANUAL = "manual"
SOURCE_FAIL_CLOSED = "fail_closed"
SUSPEND_SOURCES: frozenset[str] = frozenset(
    {SOURCE_SPOKEN, SOURCE_MANUAL, SOURCE_FAIL_CLOSED}
)

#: Why the current state is what it is. Distinct strings because they have
#: distinct remedies AND distinct renderings: "you said company" and "the
#: state file is corrupt" are the same boolean and very different sentences.
REASON_SPOKEN_SUSPEND = "spoken_suspend"
REASON_SPOKEN_RELEASE = "spoken_release"
REASON_MANUAL_SUSPEND = "manual_suspend"
REASON_MANUAL_RELEASE = "manual_release"
REASON_NO_STATE_FILE = "no_state_file"
REASON_UNREADABLE = "unreadable"
REASON_CORRUPT = "corrupt"
REASON_MALFORMED = "malformed"
REASON_STORE_UNCONFIGURED = "store_unconfigured"

#: The reasons that resolve to SUSPENDED without anyone having asked for it.
#: :attr:`SuspendStatus.fail_closed` reads this, and the UI renders those
#: differently — "suspended because you said so" and "suspended because I
#: cannot tell" must not look the same on a screen.
FAIL_CLOSED_REASONS: frozenset[str] = frozenset({
    REASON_NO_STATE_FILE, REASON_UNREADABLE, REASON_CORRUPT, REASON_MALFORMED,
})

#: Owner-only, same posture as the mark log: this file says whether a
#: microphone in a shared space is listening, on a device that may sit on a
#: shelf. 0600 for the file, 0700 for its directory.
_STATE_MODE = stat.S_IRUSR | stat.S_IWUSR
_STATE_DIR_MODE = stat.S_IRWXU

#: Human sentences for the reasons a UI has to render. Kept beside the
#: reason strings rather than in the UI so the vehicle surface and the log
#: cannot disagree about what a state means.
_DETAIL_BY_REASON: dict[str, str] = {
    REASON_SPOKEN_SUSPEND: "suspended by the spoken company phrase",
    REASON_SPOKEN_RELEASE: "released by the spoken release phrase",
    REASON_MANUAL_SUSPEND: "suspended from the manual control",
    REASON_MANUAL_RELEASE: "released from the manual control",
    REASON_NO_STATE_FILE: (
        "suspended because no suspension state has ever been recorded on this "
        "device. This is either a first boot or a state file that was removed "
        "— indistinguishable from here, so the microphone stays off. Release "
        "it once from the manual control and the state persists from then on."
    ),
    REASON_UNREADABLE: (
        "suspended because the suspension state file could not be READ. Until "
        "it can be, the device cannot know whether it was told to stop "
        "listening, so it does not listen."
    ),
    REASON_CORRUPT: (
        "suspended because the suspension state file is not valid JSON. A "
        "half-written file is the signature of a device that lost power "
        "mid-write; releasing it rewrites the file cleanly."
    ),
    REASON_MALFORMED: (
        "suspended because the suspension state file parsed but carries no "
        "usable 'suspended' boolean. A state that cannot be read as a state "
        "is an unknown state."
    ),
    REASON_STORE_UNCONFIGURED: (
        "the company toggle has no store: jeeves.suspend_state_path is empty, "
        "so a suspension could not survive a restart and nothing is being "
        "enforced. A loaded config always derives this path."
    ),
}


@dataclass(frozen=True)
class SuspendStatus:
    """The read hook's answer — the struct the vehicle UI renders.

    Deliberately more than a boolean. A banner that says only "SUSPENDED"
    cannot tell the operator whether the device heard him or whether it
    fell back to suspended because its state file is corrupt, and those two
    need different actions from him. ``reason`` is the greppable id,
    ``detail`` the sentence, ``source`` which door, ``since`` when.
    """

    suspended: bool
    source: str = ""
    reason: str = ""
    since: str = ""
    detail: str = ""

    @property
    def fail_closed(self) -> bool:
        """True when this state was ASSUMED rather than read."""
        return self.reason in FAIL_CLOSED_REASONS

    def as_dict(self) -> dict[str, Any]:
        """Serialisable form — what the store holds and the UI receives."""
        return {
            "suspended": self.suspended,
            "source": self.source,
            "reason": self.reason,
            "since": self.since,
        }


#: Paths already reported as unconfigured. :func:`read_status` runs once per
#: audio chunk, so an unlatched "no store configured" line would be twelve
#: identical warnings a second — the surveyor's once-per-lifecycle latch, for
#: the same reason. The SUSPENDED and fail-closed lines are NOT latched: they
#: are rare by construction (the service stops feeding the ring), and each one
#: is a real event.
_UNCONFIGURED_WARNED: set[str] = set()


def reset_warning_latches() -> None:
    """Clear the once-per-lifecycle latches. TEST HOOK.

    A latched log is invisible to the second test that drives the same path,
    which makes "does this still warn" untestable without it. Production
    never calls this — a device does not want the warning again.
    """
    _UNCONFIGURED_WARNED.clear()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _status(
    suspended: bool, *, source: str, reason: str, since: str = "",
) -> SuspendStatus:
    return SuspendStatus(
        suspended=suspended, source=source, reason=reason, since=since,
        detail=_DETAIL_BY_REASON.get(reason, ""),
    )


def _fail_closed(reason: str, path: str, **extra: Any) -> SuspendStatus:
    """Resolve to SUSPENDED and say why, loudly.

    Every fail-closed resolution logs. A device that is deaf because its
    state file is corrupt looks exactly like a device that is deaf because
    the operator asked for it, and the whole intentionally-left-blank
    principle is that those must be distinguishable without a debugger.
    """
    log.warning(
        "jeeves.suspend.fail_closed",
        reason=reason,
        path=path,
        suspended=True,
        source=SOURCE_FAIL_CLOSED,
        detail=_DETAIL_BY_REASON.get(reason, ""),
        **extra,
    )
    return _status(True, source=SOURCE_FAIL_CLOSED, reason=reason)


def read_status(state_path: str) -> SuspendStatus:
    """The current suspension state — fail-closed on anything unreadable.

    Reads the file every call on purpose (the manual door is another
    process; see the module docstring). Never raises: a capture device whose
    toggle read throws would take down the loop that is still listening.
    """
    if not state_path:
        # Intentionally-left-blank: a toggle with nowhere to keep its state
        # is a configuration state, not a suspension, and it must be visible
        # — nothing here would survive a restart. Latched, because this runs
        # once per audio chunk.
        if "" not in _UNCONFIGURED_WARNED:
            _UNCONFIGURED_WARNED.add("")
            log.warning(
                "jeeves.suspend.store_unconfigured",
                suspended=False,
                reason=REASON_STORE_UNCONFIGURED,
                detail=_DETAIL_BY_REASON[REASON_STORE_UNCONFIGURED],
            )
        return _status(
            False, source="", reason=REASON_STORE_UNCONFIGURED,
        )

    target = Path(state_path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _fail_closed(REASON_NO_STATE_FILE, state_path)
    except OSError as exc:
        return _fail_closed(
            REASON_UNREADABLE, state_path,
            error_type=type(exc).__name__, error=str(exc)[:200],
        )

    try:
        data = json.loads(raw)
    except ValueError:
        return _fail_closed(REASON_CORRUPT, state_path)
    if not isinstance(data, dict):
        return _fail_closed(REASON_MALFORMED, state_path, shape=type(data).__name__)

    # Schema-tolerance filter, the load() contract from CLAUDE.md: an unknown
    # key written by a newer build is dropped rather than crashing the loader
    # on a rollback. A capture device's toggle must degrade, never fail.
    known = {
        k: v for k, v in data.items() if k in SuspendStatus.__dataclass_fields__
    }
    suspended = known.get("suspended")
    if not isinstance(suspended, bool):
        # STRICT, same posture as the gate's synthetic test: the string
        # "true", 1 and None are all malformed. A truthiness coercion here
        # would let a JSON round-trip decide whether a microphone is live.
        return _fail_closed(
            REASON_MALFORMED, state_path, value_type=type(suspended).__name__,
        )

    source = known.get("source")
    reason = known.get("reason")
    since = known.get("since")
    return _status(
        suspended,
        source=source if isinstance(source, str) else "",
        reason=reason if isinstance(reason, str) else "",
        since=since if isinstance(since, str) else "",
    )


def is_suspended(state_path: str) -> bool:
    """Shorthand over :func:`read_status` for the enforcement points."""
    return read_status(state_path).suspended


def _reason_for(suspended: bool, source: str) -> str:
    if source == SOURCE_SPOKEN:
        return REASON_SPOKEN_SUSPEND if suspended else REASON_SPOKEN_RELEASE
    return REASON_MANUAL_SUSPEND if suspended else REASON_MANUAL_RELEASE


def set_suspended(
    state_path: str,
    suspended: bool,
    *,
    source: str,
    telemetry_path: str = "",
    now: str = "",
) -> SuspendStatus:
    """THE transition. Both doors call this and nothing else writes the state.

    ``source`` must be :data:`SOURCE_SPOKEN` or :data:`SOURCE_MANUAL` — the
    two doors the ruling names. :data:`SOURCE_FAIL_CLOSED` is a READ
    outcome, never a transition anyone performs, so passing it is a
    programming error and is refused loudly rather than written to the file
    (a stored ``fail_closed`` source would make a real suspension
    indistinguishable from an assumed one, forever).

    Writes atomically (tmp → rename) at 0600 under a 0700 directory, and
    logs the transition in BOTH directions — a resume is exactly as
    interesting as a suspend, and a log that only records one of them cannot
    answer "was it listening at 3pm".

    Returns the resulting :class:`SuspendStatus`. On a write failure the
    returned status is FAIL-CLOSED SUSPENDED regardless of what was asked
    for: if the transition did not reach the disk it will not survive a
    restart, and the safe reading of "I could not record that you released
    me" is that the device stays off.
    """
    if source not in (SOURCE_SPOKEN, SOURCE_MANUAL):
        raise ValueError(
            f"jeeves suspend transitions come from a door: {SOURCE_SPOKEN!r} "
            f"or {SOURCE_MANUAL!r}, not {source!r}. "
            f"{SOURCE_FAIL_CLOSED!r} is what a READ resolves to when the "
            f"state cannot be understood; storing it would make an assumed "
            f"suspension indistinguishable from one the operator asked for."
        )

    previous = read_status(state_path)
    reason = _reason_for(suspended, source)
    stamp = now or now_iso()
    status = _status(suspended, source=source, reason=reason, since=stamp)

    if not state_path:
        log.error(
            "jeeves.suspend.no_store",
            suspended=suspended,
            source=source,
            detail="a suspension transition was requested but "
                   "jeeves.suspend_state_path is empty, so it was NOT "
                   "recorded and will not survive a restart. The toggle is "
                   "not being enforced.",
        )
        return _status(
            False, source=source, reason=REASON_STORE_UNCONFIGURED,
        )

    target = Path(state_path)
    tmp = target.with_name(target.name + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(target.parent, _STATE_DIR_MODE)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(status.as_dict(), fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, _STATE_MODE)
        os.replace(tmp, target)
    except OSError as exc:
        log.error(
            "jeeves.suspend.write_failed",
            path=state_path,
            requested_suspended=suspended,
            source=source,
            error_type=type(exc).__name__,
            error=str(exc)[:200],
            detail="the suspension state could not be written, so it would "
                   "not survive a restart. Resolving FAIL-CLOSED (suspended) "
                   "rather than reporting a transition that is not on disk.",
        )
        try:
            tmp.unlink()
        except OSError:
            pass
        return _fail_closed(REASON_UNREADABLE, state_path)

    # ILB, both directions. ``changed`` is what makes a repeated "Jeeves,
    # company" readable as a no-op rather than as a second suspension.
    log.info(
        "jeeves.suspend.transition",
        suspended=suspended,
        source=source,
        reason=reason,
        previous_suspended=previous.suspended,
        previous_reason=previous.reason,
        changed=previous.suspended != suspended,
        path=state_path,
        detail=status.detail,
    )

    if telemetry_path:
        _emit_row(telemetry_path, status)
    return status


def _emit_row(telemetry_path: str, status: SuspendStatus) -> None:
    """One retained row per transition, from WHICHEVER door drove it.

    The row lives here rather than in the service so the two doors produce
    the same evidence: the manual button is a UI process that never touches
    the capture loop, and if only the spoken path emitted telemetry the
    morning rollup would show a device that suspends itself and never
    resumes. Content-free by construction — a boolean, a closed-set source,
    and a closed-set reason.
    """
    row = telemetry.TelemetryRow(
        at=status.since or now_iso(),
        event=(
            telemetry.EVENT_SUSPENDED if status.suspended
            else telemetry.EVENT_RESUMED
        ),
        reason=status.reason,
        toggle_source=status.source,
    )
    try:
        telemetry.append_row(telemetry_path, row)
    except telemetry.TelemetryRefused as exc:
        log.error(
            "jeeves.suspend.telemetry_refused",
            reason=exc.reason, field=exc.field_name, detail=exc.detail[:300],
        )
    except OSError as exc:
        log.error(
            "jeeves.suspend.telemetry_write_failed",
            error_type=type(exc).__name__, detail=str(exc)[:200],
        )


#: Field names of :class:`SuspendStatus` — used by the store's schema
#: tolerance filter above and by the drift pin in the fence suite.
STATUS_FIELDS: frozenset[str] = frozenset(f.name for f in fields(SuspendStatus))
