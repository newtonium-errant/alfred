"""V3.1 barge-in telemetry — features-only, collect-only, privacy-critical.

Emits ONE durable event per barge DECISION into the UNIFIED voice-calibration
corpus (``data/voice_calibration/events.jsonl`` — the SAME corpus the
endpoint-hold sink writes, discriminated by ``event_family``). It APPLIES
NOTHING (no learning here); it is passive evidence for the V3.1 barge
calibration go/no-go. The barge decisions are ALSO logged to talker.log by
``VoiceTurnDriver._log_barge`` / ``_barge_outcome`` (§1.9 / §1.9b, ephemeral +
ANSI-coloured); this sink is the durable, parseable, feature-bearing twin.

PRIVACY (load-bearing, scope §5): the sink records lexical-CATEGORY FEATURES
ONLY (derived booleans / scalars / the decision), NEVER the raw OR normalized
transcript text (the no-transcript-in-logs contract, ``voice_stt.py:50`` /
``barge_in.py:11``). This is enforced STRUCTURALLY here by
:data:`_BARGE_ALLOWED_FIELDS`: any field not on the allowlist is DROPPED before
write, so even a careless caller that hands ``emit`` a ``text`` / ``norm`` /
``transcript`` field cannot leak the utterance — the whole reason the corpus
carries derived booleans, not the words.

Isolation mirrors ``voice_endpoint_telemetry``: :meth:`emit` is fire-and-forget
(never blocks/raises into the turn), the task is retained in the module-level
:data:`_BARGE_TASKS` (GC-safe), and the body sits under a top-level catch-all.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .utils import get_logger

log = get_logger(__name__)

# GC-safe (a bare create_task is only weak-ref'd — project_stt_test_series).
_BARGE_TASKS: set[asyncio.Task] = set()

# The ONLY per-event fields that may be written. Derived lexical-CATEGORY
# booleans + scalars + the decision + the config that produced it — NEVER a
# raw/normalized-text field. Anything else is dropped. (ids / ms / echo_score
# are already contract-blessed — ``_log_barge`` logs them today.)
_BARGE_ALLOWED_FIELDS = frozenset({
    "decision",                   # "barge" | "suppress"
    "reason",                     # too_early|backchannel|too_short|echo|
                                  # interrupt_phrase|confirmed|late_echo
    "ms_into_speaking",           # int — elapsed since playback start
    "echo_score",                 # float, rounded
    "word_count",                 # int — normalized token count
    "char_count",                 # int — normalized char count
    "starts_with_backchannel",    # bool — norm's FIRST token ∈ backchannel set
    "is_backchannel_exact",       # bool — full norm ∈ backchannel set
    "matched_interrupt_phrase",   # bool — full norm ∈ interrupt set
    "cfg_too_early_ms",           # int — self-describing config snapshot
    "cfg_echo_threshold",         # float — self-describing config snapshot
    "outcome",                    # completed|cancelled|empty|"" (false-barge label)
    "utterance_id",               # str
    "turn_id",                    # str — join key (confirmed ↔ outcome)
})


class VoiceBargeTelemetry:
    """One per voice session. ``emit`` is the fire-and-forget hook the turn
    driver calls at each barge decision seam; it returns immediately."""

    def __init__(
        self,
        *,
        corpus_dir: str,
        web_user: str,
        voice_session_id: str,
        instance_name: str = "",
    ) -> None:
        self._dir = Path(corpus_dir)
        self._web_user = web_user
        self._vid = voice_session_id
        self._instance = instance_name

    def emit(self, fields: dict[str, Any]) -> None:
        """Fire-and-forget. NEVER blocks or raises into the caller (the live
        turn). Non-allowlisted keys are dropped (privacy). The task lives in the
        module-level ``_BARGE_TASKS`` set (GC-safe)."""
        try:
            safe = {k: v for k, v in fields.items() if k in _BARGE_ALLOWED_FIELDS}
            task = asyncio.ensure_future(self._emit(safe))
            _BARGE_TASKS.add(task)
            task.add_done_callback(_BARGE_TASKS.discard)
        except Exception:  # noqa: BLE001 — scheduling must never touch the turn
            log.warning("web.voice.barge.telemetry_schedule_failed",
                        voice_session_id=self._vid)

    async def _emit(self, safe_fields: dict[str, Any]) -> None:
        try:
            record = {
                "event_family": "barge",
                "ts": datetime.now(timezone.utc).isoformat(),
                "web_user": self._web_user,
                "voice_session_id": self._vid,
                "instance": self._instance,
                **safe_fields,
            }
            self._dir.mkdir(parents=True, exist_ok=True)
            with (self._dir / "events.jsonl").open("a", encoding="utf-8") as fout:
                fout.write(json.dumps(record) + "\n")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — isolation backstop
            log.warning(
                "web.voice.barge.telemetry_failed",
                voice_session_id=self._vid,
                error=str(exc)[:300], error_type=type(exc).__name__,
            )


__all__ = ["VoiceBargeTelemetry"]
