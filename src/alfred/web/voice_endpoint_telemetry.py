"""Adaptive-endpointing telemetry — features-only, collect-only, privacy-critical.

Increment 1 emits ONE durable event per endpoint decision into the UNIFIED
voice-calibration corpus (``data/voice_calibration/events.jsonl`` — the SAME
corpus V3.1 barge uses, discriminated by ``event_family``). It APPLIES NOTHING
(no learning in Increment 1); it is passive evidence for the Increment-2 go/no-go.

PRIVACY (the one net-new consideration, scope §5): the sink records tail
FEATURES ONLY — lexical-CATEGORY booleans, never the raw tail text (the
no-transcript-in-logs contract, ``voice_stt.py:50`` / ``barge_in.py:11``). This
is enforced STRUCTURALLY here by :data:`_ALLOWED_FIELDS`: any field not on the
allowlist is DROPPED before write, so even a careless caller cannot leak text.

Isolation mirrors ``voice_stt_shadow``: :meth:`emit` is fire-and-forget (never
blocks/raises into the turn), the task is retained in the module-level
:data:`_ENDPOINT_TASKS` (GC-safe), and the body sits under a top-level catch-all.
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
_ENDPOINT_TASKS: set[asyncio.Task] = set()

# The ONLY per-event fields that may be written. Feature-CATEGORY booleans +
# scalars + the decision — NEVER a raw-text field. Anything else is dropped.
_ALLOWED_FIELDS = frozenset({
    "trailing_is_conjunction",
    "trailing_is_filler",
    "trailing_is_dangling",
    "ends_with_terminal_punct",
    "n_tokens",
    "decision",
    "signal_category",
    "hold_ms_applied",
    "resumed_within_hold",
    "ms_trailing_silence_at_fire",
    "trigger",
})


class VoiceEndpointTelemetry:
    """One per voice session. ``emit`` is the fire-and-forget hook the STT
    worker calls at commit time; it returns immediately."""

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
        module-level ``_ENDPOINT_TASKS`` set (GC-safe)."""
        try:
            safe = {k: v for k, v in fields.items() if k in _ALLOWED_FIELDS}
            task = asyncio.ensure_future(self._emit(safe))
            _ENDPOINT_TASKS.add(task)
            task.add_done_callback(_ENDPOINT_TASKS.discard)
        except Exception:  # noqa: BLE001 — scheduling must never touch the turn
            log.warning("web.voice.stt.endpoint_telemetry_schedule_failed",
                        voice_session_id=self._vid)

    async def _emit(self, safe_fields: dict[str, Any]) -> None:
        try:
            record = {
                "event_family": "endpoint",
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
                "web.voice.stt.endpoint_telemetry_failed",
                voice_session_id=self._vid,
                error=str(exc)[:300], error_type=type(exc).__name__,
            )


__all__ = ["VoiceEndpointTelemetry"]
