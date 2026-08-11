"""Jeeves — the garage ambient scribe (task #81, design ratified 2026-08-10).

A HOST-AGNOSTIC capture service. It listens continuously and LOCALLY, and
sends **only cued windows** to cloud STT. Everything else about the design
follows from that one split, so it is stated first and defended by structure
rather than by comment:

    mic ─► [local, continuous]                 ─► nothing leaves
            ring buffer (RAM, N minutes)
            wake-word model on the live stream
                  │ cue fires
                  ▼
            window extraction (ring lookback + short lookahead)
                  │
                  ▼ [network, CUED ONLY]
            cloud STT (the existing GroqWhisperBackend)
                  │
                  ▼ [local]
            cue classification: MARK-DOWN vs ROUTE
              ┌───┴────┐
              ▼        ▼
          local log   peer intake route (vault record)

THE FENCES (ratification 2026-08-10; none of these is negotiable in code):

1. **Local cue detection.** Wake-word detection runs on-device
   (:mod:`.wake`). No continuous cloud audio, ever. The only bytes that
   reach a network are the ones :mod:`.window` cut out of the ring after a
   cue fired.
2. **Non-cued audio dies with the ring.** :class:`.ring.RingBuffer` is
   RAM-only and has NO persistence surface — no save, no path, no file
   handle. Eviction is the wrap, and there is no code path that retrieves
   an evicted chunk. (Pinned in ``tests/test_jeeves_ring.py``.)
3. **Cued captures are routinely MULTI-SPEAKER.** The garage is a
   workout/lounge; a second household voice is the room's NORMAL condition
   (trial verdict 2026-08-11). Nothing here assumes a single speaker, and
   nothing tries to separate them.
4. **Telemetry carries NO content.** :mod:`.telemetry` is structurally
   content-free — the row dataclass has no text field and the writer
   refuses any key outside a closed allowlist. It answers "did the cue
   fire" and "how far back did we reach", never "what was said".
5. **Personal / RRTS only, NEVER clinical.** This package copies the
   STAY-C scribe's fail-closed POSTURE (:mod:`.gate`) and shares NO code
   with it. ``alfred.jeeves`` imports nothing from ``alfred.scribe`` — that
   is a structural pin, not an intention (``tests/test_jeeves_fences.py``).
   Two ambient scribes on one codepath is exactly how personal audio ends
   up somewhere it needs consent to be.
6. **Notes-only v1.** The ``jeeves`` vault scope creates ``{note, source}``
   and nothing else; no task minting (a device that mints tasks from
   ambient speech in a noisy room mints junk tasks).

PROMOTION-READY (ruling 7). Jeeves rides a peer instance for v1 but is a
plausible instance of its own later, so it keeps its own config block, its
own scope, and its own peer token, and couples to no instance-specific
module. The only outward dependency is the STT vocabulary seam
(``telegram.stt_vocab_learning.effective_vocab_terms``), which is CONSUMED,
never copied — see :mod:`.stt`.

WHAT IS NOT HERE. Microphone access. The audio-capture layer is a thin
adapter (:class:`.audio.AudioSource`); the real mic implementation is
host/deployment territory (stage 3). Stage 1 ships the service driving
file- and memory-backed sources, which is also what the tests use.
"""

from __future__ import annotations

__all__ = [
    "audio",
    "config",
    "cues",
    "gate",
    "marklog",
    "ring",
    "service",
    "stt",
    "telemetry",
    "transport_sink",
    "wake",
    "window",
]
