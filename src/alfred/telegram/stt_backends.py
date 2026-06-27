"""STT fallback chain — async backend protocol + Groq/Deepgram backends + router.

M1 of the STT fallback-chain spec (algernon-stt-fallback-spec-2026-06-27, v2).
The talker's voice path was single-vendor (one Groq Whisper call); this wires
a backup engine behind it so a single STT failure never silently drops a
voice note — seamless to the user, logged for the operator.

Scope — M1 ONLY:
  * async ``SttBackend`` protocol + ``SttResult`` (§3) — NO cross-engine
    confidence float; ``tier`` drives behaviour.
  * ``GroqWhisperBackend`` (verbose_json → no_speech_prob + avg_logprob) +
    the empty-contract migration (return empty ``SttResult``, never raise).
  * ``DeepgramBackend`` (httpx async; punctuate+smart_format for output-shape
    parity §7).
  * ``transcribe_with_fallback`` router (§3) applying the §4 matrix incl. the
    three empty branches.

DEFERRED (TODO, do NOT build here):
  * M2 — circuit-breaker (per-backend state under the bot's asyncio.Lock,
    half-open single-probe, ``never_skip`` backstop, Retry-After clamp).
    Without it, on a Groq 429 every voice note tries Groq-then-Deepgram —
    acceptable for M1 (the chain still serves; just no zero-cost skip).
  * M3 — morning rollup + self-correcting surfacing.
  * M4 — local-Whisper ``never_skip`` backstop + the degraded-tier marker.
    ``never_skip`` is parsed + carried through now (cheap), but no backend
    sets it in M1 and the router has no backstop-special-casing yet.

Vocab (spec must-fix 2): the previously-hardcoded ``_STT_VOCABULARY_PROMPT``
is now config-driven ``vocab_terms`` (telegram/config.py), passed to every
backend so each biases the same domain terms in its own way (Whisper
``prompt=``, Deepgram ``keywords``). Without it, a fallback transcribes
domain terms worse than primary, breaking the "comparable = seamless" claim.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import httpx

from .utils import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Result + error taxonomy (§3 / §4)
# ---------------------------------------------------------------------------


@dataclass
class SttResult:
    """One backend's transcription outcome (§3).

    A backend MUST return an ``SttResult`` (possibly with empty ``text``) for
    genuine silence — it MUST NOT raise for empty. It raises a classified
    :class:`SttError` ONLY for true failures (network / 429 / auth / 400 /
    decode). This is the load-bearing empty-contract (§4): the router needs
    to distinguish "the engine heard nothing" (an empty result) from "the
    engine failed" (a raise) so it doesn't re-spend on the backup for silence.

    ``has_speech_signal`` drives the §4 empty branch:
      * ``True``  — the engine detected speech but produced ~nothing (decode
        miss) → the router TRY_NEXTs (a different decoder may succeed).
      * ``False`` — genuine silence → the router SERVEs the empty (no
        re-spend; a 2-sec "uh" stays empty everywhere + Groq bills a 10s
        minimum per call).
      * ``None``  — the engine exposes no speech-detection signal.

    ``confidence_raw`` is an OPAQUE per-engine signal, NEVER compared across
    engines (Whisper ``avg_logprob`` is a negative log-scale; Deepgram is a
    0-1 probability). ``confidence_kind`` labels which it is. The
    seamless-vs-flagged decision (§7) is driven by ``tier``, never by
    comparing confidence.
    """

    text: str
    backend_id: str
    tier: str
    has_speech_signal: bool | None = None
    confidence_raw: float | None = None
    confidence_kind: str | None = None  # "logprob" | "probability" | None
    latency_ms: int = 0
    cost: float | None = None


@dataclass
class NoTranscript:
    """Returned by the router when every backend failed or served empty at
    chain-end — the caller (bot) asks the user to type.

    ``reason`` distinguishes the §6 cases for observability:
      * ``"all_failed"`` — every backend raised.
      * ``"degraded"`` — chain-end empty (no transcript, but not an error).
    (``all_circuits_open`` arrives with the M2 circuit-breaker.)
    """

    reason: str = "all_failed"


# --- error taxonomy (§4 classes) -------------------------------------------

# Canonical failure classes (§4) — carried on SttError.error_class and into
# the stt.transcribed log's primary_failure.class. Stable strings; the
# morning rollup (M3) buckets on them.
STT_ERR_NETWORK = "network"          # network / asyncio-timeout / 5xx
STT_ERR_RATE_LIMIT = "rate_limit"    # 429, or 4xx body says quota/credit
STT_ERR_AUTH = "auth"                # 401/403 body says key/auth invalid
STT_ERR_BAD_REQUEST = "bad_request"  # 400/413/415/decode — per-input
STT_ERR_UNKNOWN = "unknown"


class SttError(Exception):
    """A classified, true STT failure (NOT an empty result — see SttResult).

    ``error_class`` is one of the ``STT_ERR_*`` constants (§4); ``detail`` is
    a short grep-able head (HTTP status + body excerpt, or exception class).
    The router's §4 matrix reads ``error_class`` to decide circuit-trip
    (M2) / alert (M2) / next-backend. In M1 every class → next-backend.
    """

    def __init__(
        self, error_class: str, detail: str, *, backend_id: str = "",
    ) -> None:
        super().__init__(f"[{error_class}] {detail}")
        self.error_class = error_class
        self.detail = detail
        self.backend_id = backend_id


def _classify_http_status(status: int, body_tail: str) -> str:
    """Map an HTTP status + body excerpt to an STT_ERR_* class (§4).

    The body distinguishes the ambiguous 4xx: 429 (+ 402/403 *credit*) is
    rate-limit; 401/403 *key* is auth; 400/413/415 is bad-request. 5xx is
    network-class. Body-sniffing is deliberately conservative — a 403 that
    doesn't clearly say "credit/quota" is treated as auth (won't self-heal,
    surfaced once) rather than silently retried forever.
    """
    body_lc = (body_tail or "").lower()
    if status >= 500:
        return STT_ERR_NETWORK
    if status == 429:
        return STT_ERR_RATE_LIMIT
    if status in (402, 403) and any(
        kw in body_lc for kw in ("quota", "credit", "insufficient", "billing")
    ):
        return STT_ERR_RATE_LIMIT
    if status in (401, 403):
        return STT_ERR_AUTH
    if status in (400, 413, 415):
        return STT_ERR_BAD_REQUEST
    return STT_ERR_UNKNOWN


# ---------------------------------------------------------------------------
# Backend protocol (§3)
# ---------------------------------------------------------------------------


@runtime_checkable
class SttBackend(Protocol):
    """Async STT backend interface (§3).

    ``transcribe`` MUST return an :class:`SttResult` (possibly empty text) for
    genuine silence and raise a classified :class:`SttError` ONLY for true
    failures. ``vocab`` is the domain-term biasing list each backend maps to
    its engine's mechanism (Whisper ``prompt=``, Deepgram ``keywords``).
    """

    backend_id: str
    tier: str          # "primary" | "comparable" | "degraded"
    never_skip: bool   # M4 backstop flag — carried now, used by M2 router
    timeout_s: float

    async def transcribe(
        self, audio: bytes, mime: str, vocab: list[str],
    ) -> SttResult:
        ...


# ---------------------------------------------------------------------------
# GroqWhisperBackend (§3) — the existing primary, refactored into the protocol
# ---------------------------------------------------------------------------


_GROQ_ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"


@dataclass
class GroqWhisperBackend:
    """Groq Whisper (``whisper-large-v3``) via the OpenAI-compatible endpoint.

    ``response_format=verbose_json`` (spec §9) is required to surface
    ``no_speech_prob`` (→ ``has_speech_signal``) and the segment
    ``avg_logprob`` (→ ``confidence_raw``, kind ``"logprob"``). The plain
    ``json`` format returns only ``text``.

    Empty-contract migration (M1 prerequisite, §12): an empty/silent result
    RETURNS an empty ``SttResult`` (with ``has_speech_signal`` derived from
    ``no_speech_prob``), it does NOT raise — so the router routes empty via
    the §4 empty rows, not the failure path. Only real HTTP/network/decode
    errors raise a classified :class:`SttError`.
    """

    api_key: str
    model: str = "whisper-large-v3"
    tier: str = "comparable"
    timeout_s: float = 10.0
    language: str = "en"
    response_format: str = "verbose_json"
    never_skip: bool = False
    backend_id: str = "groq-whisper"
    # "empty" threshold (§9 min_transcript_chars) — measured after trimming
    # whitespace + lone punctuation. Set by the router from config.
    min_transcript_chars: int = 3

    async def transcribe(
        self, audio: bytes, mime: str, vocab: list[str],
    ) -> SttResult:
        if not self.api_key:
            raise SttError(
                STT_ERR_AUTH, "Groq api_key is empty",
                backend_id=self.backend_id,
            )

        filename = "voice.ogg" if mime.endswith("ogg") else "voice.bin"
        files = {"file": (filename, audio, mime)}
        data: dict[str, str] = {
            "model": self.model,
            "response_format": self.response_format,
        }
        if self.language:
            data["language"] = self.language
        prompt = _vocab_to_whisper_prompt(vocab)
        if prompt:
            data["prompt"] = prompt
        headers = {"Authorization": f"Bearer {self.api_key}"}

        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.post(
                    _GROQ_ENDPOINT, headers=headers, files=files, data=data,
                )
        except httpx.HTTPError as exc:
            raise SttError(
                STT_ERR_NETWORK, f"HTTP error: {exc}",
                backend_id=self.backend_id,
            ) from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        if resp.status_code >= 400:
            body_tail = (resp.text or "")[:300]
            raise SttError(
                _classify_http_status(resp.status_code, body_tail),
                f"HTTP {resp.status_code}: {body_tail or '(no body)'}",
                backend_id=self.backend_id,
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            raise SttError(
                STT_ERR_BAD_REQUEST, f"non-JSON response: {exc}",
                backend_id=self.backend_id,
            ) from exc

        text = _normalize_transcript(payload.get("text") or "")
        no_speech_prob, avg_logprob = _groq_verbose_signals(payload)
        # has_speech_signal: speech was detected when no_speech_prob is LOW.
        # 0.5 is Whisper's conventional no-speech threshold. None when the
        # signal is absent (plain json format / older response).
        has_speech_signal: bool | None
        if no_speech_prob is None:
            has_speech_signal = None
        else:
            has_speech_signal = no_speech_prob < 0.5

        is_empty = len(text) < self.min_transcript_chars
        return SttResult(
            text="" if is_empty else text,
            backend_id=self.backend_id,
            tier=self.tier,
            has_speech_signal=has_speech_signal,
            confidence_raw=avg_logprob,
            confidence_kind="logprob" if avg_logprob is not None else None,
            latency_ms=latency_ms,
            cost=None,
        )


def _groq_verbose_signals(
    payload: dict,
) -> tuple[float | None, float | None]:
    """Extract (no_speech_prob, avg_logprob) from a Groq verbose_json payload.

    verbose_json carries a ``segments`` list, each with ``no_speech_prob`` +
    ``avg_logprob``. We aggregate: max ``no_speech_prob`` (most-silent
    segment dominates the silence call) is conservative for "did it hear
    speech"; mean ``avg_logprob`` is the opaque confidence provenance.
    Returns (None, None) when segments are absent (plain json shape).
    """
    segments = payload.get("segments")
    if not isinstance(segments, list) or not segments:
        return None, None
    no_speech: list[float] = []
    logprobs: list[float] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        nsp = seg.get("no_speech_prob")
        if isinstance(nsp, (int, float)):
            no_speech.append(float(nsp))
        alp = seg.get("avg_logprob")
        if isinstance(alp, (int, float)):
            logprobs.append(float(alp))
    nsp_agg = max(no_speech) if no_speech else None
    alp_agg = (sum(logprobs) / len(logprobs)) if logprobs else None
    return nsp_agg, alp_agg


# ---------------------------------------------------------------------------
# DeepgramBackend (§3 / §7) — httpx async (no SDK import-time dep)
# ---------------------------------------------------------------------------


_DEEPGRAM_ENDPOINT = "https://api.deepgram.com/v1/listen"


@dataclass
class DeepgramBackend:
    """Deepgram pre-recorded transcription via httpx (async).

    httpx-direct (not the SDK) mirrors the established ElevenLabs convention
    (pyproject voice extra: "implementation uses httpx directly … pinning the
    official SDK … for users who want to swap"); keeps the runtime free of an
    import-time SDK dep so the regression pins run unconditionally.

    Output-shape parity (§7, must-fix): ``punctuate=true`` + ``smart_format=
    true`` are ALWAYS set — Whisper returns punctuated/cased text by default
    and an unconfigured Deepgram returns lowercase/unpunctuated text that
    breaks the talker's command parsing + ``routine_done``-style matchers.
    "Comparable" means comparable output SHAPE, not just WER.

    ``vocab`` → Deepgram ``keywords`` (one repeated query param per term),
    biasing the model toward domain proper nouns.
    """

    api_key: str
    model: str = "nova-2"
    tier: str = "comparable"
    timeout_s: float = 10.0
    language: str = "en"
    punctuate: bool = True
    smart_format: bool = True
    never_skip: bool = False
    backend_id: str = "deepgram"
    min_transcript_chars: int = 3

    async def transcribe(
        self, audio: bytes, mime: str, vocab: list[str],
    ) -> SttResult:
        if not self.api_key:
            raise SttError(
                STT_ERR_AUTH, "Deepgram api_key is empty",
                backend_id=self.backend_id,
            )

        # Output-shape parity (§7): these are NOT optional — see class doc.
        params: list[tuple[str, str]] = [
            ("model", self.model),
            ("punctuate", "true" if self.punctuate else "false"),
            ("smart_format", "true" if self.smart_format else "false"),
        ]
        if self.language:
            params.append(("language", self.language))
        # Domain-term biasing — Deepgram repeats the ``keywords`` param.
        for term in vocab:
            t = (term or "").strip()
            if t:
                params.append(("keywords", t))

        headers = {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": mime or "audio/ogg",
        }

        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.post(
                    _DEEPGRAM_ENDPOINT,
                    headers=headers, params=params, content=audio,
                )
        except httpx.HTTPError as exc:
            raise SttError(
                STT_ERR_NETWORK, f"HTTP error: {exc}",
                backend_id=self.backend_id,
            ) from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        if resp.status_code >= 400:
            body_tail = (resp.text or "")[:300]
            raise SttError(
                _classify_http_status(resp.status_code, body_tail),
                f"HTTP {resp.status_code}: {body_tail or '(no body)'}",
                backend_id=self.backend_id,
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            raise SttError(
                STT_ERR_BAD_REQUEST, f"non-JSON response: {exc}",
                backend_id=self.backend_id,
            ) from exc

        transcript, confidence = _deepgram_first_alternative(payload)
        text = _normalize_transcript(transcript)
        # Deepgram has no explicit no_speech flag on the pre-recorded route;
        # derive speech-detected from "did it return any words/transcript."
        # A non-empty transcript ⇒ speech detected; empty ⇒ no speech signal.
        has_speech_signal = bool(text)
        is_empty = len(text) < self.min_transcript_chars
        return SttResult(
            text="" if is_empty else text,
            backend_id=self.backend_id,
            tier=self.tier,
            has_speech_signal=has_speech_signal if not is_empty else False,
            confidence_raw=confidence,
            confidence_kind="probability" if confidence is not None else None,
            latency_ms=latency_ms,
            cost=None,
        )


def _deepgram_first_alternative(
    payload: dict,
) -> tuple[str, float | None]:
    """Extract (transcript, confidence) from a Deepgram listen response.

    Shape: ``results.channels[0].alternatives[0].{transcript,confidence}``.
    Defensive at each hop — a missing key yields ("", None) rather than a
    KeyError (which would mis-classify a malformed-but-200 body as a crash).
    """
    try:
        alt = (
            payload["results"]["channels"][0]["alternatives"][0]
        )
    except (KeyError, IndexError, TypeError):
        return "", None
    if not isinstance(alt, dict):
        return "", None
    transcript = str(alt.get("transcript") or "")
    conf = alt.get("confidence")
    confidence = float(conf) if isinstance(conf, (int, float)) else None
    return transcript, confidence


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


# Whisper silence-hallucinations + lone punctuation that "empty" trimming
# must treat as garbled/suspect, not genuine content (§4).
_SUSPECT_GARBAGE = {".", "..", "...", "you", "thank you.", "thank you", "-"}


def _normalize_transcript(text: str) -> str:
    """Trim whitespace + collapse lone-punctuation / known silence
    hallucinations to empty, so the "empty" check (§4) is measured against
    real content. Returns the trimmed text, or "" when it's only
    whitespace/punctuation/a known Whisper silence artefact.
    """
    s = (text or "").strip()
    if not s:
        return ""
    if s.lower() in _SUSPECT_GARBAGE:
        return ""
    # Lone punctuation (e.g. "." or "...") → empty.
    if all(not c.isalnum() for c in s):
        return ""
    return s


def _vocab_to_whisper_prompt(vocab: list[str]) -> str:
    """Join vocab terms into a Whisper ``prompt=`` biasing string."""
    terms = [t.strip() for t in vocab if t and t.strip()]
    return ", ".join(terms)


# ---------------------------------------------------------------------------
# Router (§3 / §4) — async, global deadline, empty-policy matrix
# ---------------------------------------------------------------------------

# §4 empty-policy decisions.
_SERVE = "serve"
_TRY_NEXT = "try_next"
_DEGRADE = "degrade"


def _empty_policy(
    result: SttResult, *, is_last: bool, ambiguous_tries_left: int,
) -> str:
    """Decide what to do with an EMPTY result per the §4 empty rows.

    Non-empty results never reach here (the router serves them directly).

      * empty at chain-end (``is_last``) → DEGRADE (never serve an empty
        string from the last link; ask the user to type).
      * empty WITH speech detected (decode miss) → TRY_NEXT while an extra
        attempt is budgeted (``ambiguous_tries_left`` > 0), else DEGRADE at
        chain-end / SERVE is not chosen for speech-but-empty.
      * empty as genuine silence (``has_speech_signal`` False/None) from a
        non-terminal backend → SERVE (do not re-spend on the backup).
    """
    if is_last:
        return _DEGRADE
    if result.has_speech_signal is True:
        # Decode miss — a different engine may succeed. Bounded.
        return _TRY_NEXT if ambiguous_tries_left > 0 else _DEGRADE
    # has_speech_signal False or None → genuine silence → serve the empty.
    return _SERVE


async def transcribe_with_fallback(
    audio: bytes,
    mime: str,
    chain: list[SttBackend],
    vocab: list[str],
    total_budget_s: float,
) -> SttResult | NoTranscript:
    """Route ``audio`` through ``chain`` (primary → backups), §3 + §4.

    Runs sequentially on the bot's event loop; each ``await`` yields. Returns
    the first served :class:`SttResult`, or :class:`NoTranscript` when every
    backend failed / the chain degraded at its end. The bot turns
    ``NoTranscript`` into a "mind typing it?" reply.

    M1 has NO circuit-breaker (M2) and no ``never_skip`` backstop special-
    casing (M4) — the loop simply tries each backend in order under a global
    ``total_budget_s`` deadline + per-backend ``timeout_s``. The §4 matrix
    decides next-vs-serve-vs-degrade. Both M1 backends are tier="comparable"
    so a served fallback is seamless (no user marker — M4 adds the degraded
    marker).

    Per ``feedback_intentionally_left_blank``: every outcome logs a
    ``stt.transcribed`` (served) or ``stt.exhausted`` (degraded/failed)
    event with the per-call fields (§6).
    """
    if not chain:
        log.warning("stt.exhausted", reason="empty_chain")
        return NoTranscript(reason="all_failed")

    deadline = time.monotonic() + total_budget_s
    last = chain[-1]
    primary_failure: dict[str, str] | None = None
    # §4: the speech-but-empty case gets ONE extra attempt across the chain.
    ambiguous_tries_left = 1

    for backend in chain:
        is_last = backend is last
        if time.monotonic() >= deadline and not is_last:
            # Global budget blown → skip to the backstop / graceful-degrade
            # rather than stacking more timeouts (§3/§10).
            log.info(
                "stt.budget_exceeded",
                backend_skipped=backend.backend_id,
                detail="total_budget_s elapsed; skipping to chain end",
            )
            continue

        try:
            result = await backend.transcribe(audio, mime, vocab)
        except SttError as exc:
            # §4: every class → next backend in M1 (circuit-trip + alert are
            # M2). Capture the first (primary) failure for the served-log.
            if primary_failure is None:
                primary_failure = {
                    "class": exc.error_class, "detail": exc.detail[:200],
                }
            log.warning(
                "stt.backend_failed",
                backend_id=backend.backend_id,
                error_class=exc.error_class,
                detail=exc.detail[:200],
                is_last=is_last,
            )
            continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — unknown → treat as failure
            if primary_failure is None:
                primary_failure = {
                    "class": STT_ERR_UNKNOWN, "detail": str(exc)[:200],
                }
            log.warning(
                "stt.backend_failed",
                backend_id=backend.backend_id,
                error_class=STT_ERR_UNKNOWN,
                detail=str(exc)[:200],
                is_last=is_last,
            )
            continue

        if result.text:
            # Non-empty → serve. fell_back iff not the first link.
            fell_back = backend is not chain[0]
            _log_served(result, fell_back=fell_back,
                        primary_failure=primary_failure)
            return result

        # Empty result → §4 empty matrix.
        decision = _empty_policy(
            result, is_last=is_last,
            ambiguous_tries_left=ambiguous_tries_left,
        )
        if decision == _SERVE:
            fell_back = backend is not chain[0]
            _log_served(result, fell_back=fell_back,
                        primary_failure=primary_failure)
            return result
        if decision == _TRY_NEXT:
            ambiguous_tries_left -= 1
            log.info(
                "stt.empty_try_next",
                backend_id=backend.backend_id,
                detail="empty WITH speech detected (decode miss) → next engine",
            )
            continue
        # _DEGRADE → stop, graceful-degrade.
        break

    # Chain exhausted (all failed, or degraded at the end).
    reason = "all_failed" if primary_failure is not None else "degraded"
    log.info(
        "stt.exhausted",
        reason=reason,
        primary_failure=primary_failure,
        chain_len=len(chain),
        detail=(
            "every backend failed" if reason == "all_failed"
            else "chain degraded to empty at its end"
        ),
    )
    return NoTranscript(reason=reason)


def build_chain(stt_config: object) -> list[SttBackend]:
    """Construct the ordered list of backend instances from STTConfig.

    Reads ``stt_config.effective_chain()`` (the explicit ``chain`` or the
    legacy single-Groq synthesis) + the chain-level ``min_transcript_chars``,
    and maps each ``SttBackendConfig`` to its backend class. An unknown
    ``backend`` id is skipped with a warn (a typo'd backend must fail loud,
    not silently shrink the chain to nothing — but it also must not crash the
    voice path). M1 knows ``groq-whisper`` + ``deepgram``; ``local-whisper``
    arrives in M4.
    """
    min_chars = int(getattr(stt_config, "min_transcript_chars", 3) or 3)
    chain: list[SttBackend] = []
    for bc in stt_config.effective_chain():
        backend_id = (getattr(bc, "backend", "") or "").lower()
        if backend_id in ("groq-whisper", "groq"):
            chain.append(GroqWhisperBackend(
                api_key=bc.api_key,
                model=bc.model or "whisper-large-v3",
                tier=bc.tier,
                timeout_s=float(bc.timeout_s),
                language=bc.language,
                response_format=getattr(bc, "response_format", "verbose_json"),
                never_skip=bool(getattr(bc, "never_skip", False)),
                min_transcript_chars=min_chars,
            ))
        elif backend_id == "deepgram":
            chain.append(DeepgramBackend(
                api_key=bc.api_key,
                model=bc.model or "nova-2",
                tier=bc.tier,
                timeout_s=float(bc.timeout_s),
                language=bc.language,
                punctuate=bool(getattr(bc, "punctuate", True)),
                smart_format=bool(getattr(bc, "smart_format", True)),
                never_skip=bool(getattr(bc, "never_skip", False)),
                min_transcript_chars=min_chars,
            ))
        else:
            # M4 TODO: local-whisper backstop. Unknown id → skip + warn.
            log.warning(
                "stt.unknown_backend",
                backend=backend_id,
                detail=(
                    "unknown STT backend id in chain — skipped. M1 supports "
                    "groq-whisper + deepgram; local-whisper lands in M4."
                ),
            )
    return chain


def _log_served(
    result: SttResult, *, fell_back: bool,
    primary_failure: dict[str, str] | None,
) -> None:
    """Emit the §6 per-call ``stt.transcribed`` structured event."""
    log.info(
        "stt.transcribed",
        backend_used=result.backend_id,
        fell_back=fell_back,
        primary_failure=primary_failure,
        latency_ms=result.latency_ms,
        confidence_kind=result.confidence_kind,
        confidence_raw=result.confidence_raw,
        tier=result.tier,
        chars=len(result.text),
        # M2 TODO (circuit-breaker): add circuit_state={groq:..,deepgram:..}
        # per §6 once per-backend state exists.
    )


__all__ = [
    "STT_ERR_AUTH",
    "STT_ERR_BAD_REQUEST",
    "STT_ERR_NETWORK",
    "STT_ERR_RATE_LIMIT",
    "STT_ERR_UNKNOWN",
    "DeepgramBackend",
    "GroqWhisperBackend",
    "NoTranscript",
    "SttBackend",
    "SttError",
    "SttResult",
    "build_chain",
    "transcribe_with_fallback",
]
