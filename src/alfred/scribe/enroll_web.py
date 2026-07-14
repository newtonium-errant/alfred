"""Voice-enrollment routes + RAM-only custody — scribe P4-5a (Slice B server).

Rides the #49 loopback ``ingest_web`` server (no new server/port/CSP). The
biometric-custody capability: the enroll routes pin the ``enroll_token`` (the
two-token split lives in ``ingest_web._authorize_route``). RAM-ONLY custody —
enrollment bytes live only in this process's ``_SESSIONS`` table; NO tmp / staging
/ disk path exists, and a crash destroys the only copy. Caps + TTL are module
constants (every cap hit logged 429). Everything fails OPEN: no enroll/preset state
can ever block or 4xx an encounter CHUNK.

Build order: fake-embed seam FIRST so this whole surface is CI-testable. The real
pyannote embedding + the PyAV webm/mp4 decode are the on-box path (the
``_prepare_windows`` seam); the fake provider embeds raw window bytes directly.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from aiohttp import web

from alfred.scribe import embed_voice, enroll_learning
from alfred.scribe import enrollment as en
from alfred.scribe.close_manifest import CLOSE_SENTINEL_NAME
from alfred.scribe.config import ScribeConfig
from alfred.scribe.ingest_web import _reject

log = structlog.get_logger(__name__)

# --- route paths (kept in lockstep with ingest_web's token-class sets; pinned) --
ENROLL_START = "/scribe/enroll/start"
ENROLL_CHUNK = "/scribe/enroll/chunk"
ENROLL_FINALIZE = "/scribe/enroll/finalize"
ENROLL_RESULT = "/scribe/enroll/result"
ENROLL_ABANDON = "/scribe/enroll/abandon"
PRESETS_LIST = "/scribe/presets"
PRESETS_RENAME = "/scribe/presets/rename"
PRESETS_DELETE = "/scribe/presets/delete"
ENCOUNTER_PRESET = "/scribe/encounter/preset"

# --- RAM custody caps (module constants; calibrate never touches these) ---------
_MAX_SESSIONS = 2
_MAX_WINDOWS = 8
_MAX_WINDOW_BYTES = 8 * 1024 * 1024          # 8 MiB / window
_MAX_SESSION_BYTES = 32 * 1024 * 1024        # 32 MiB / session
_SESSION_TTL_S = 600                         # 10 min

# --- enrollment gates (hard = degenerate only; the rest advisory until calibrate) --
_MIN_NET_SPEECH_S = 10.0                      # HARD: <10 s net speech → too_short
_TARGET_DURATION_S = 30.0                     # advisory
_ADVISORY_SNR_DB = 10.0                       # advisory
_ADVISORY_SELF_SIM = 0.80                     # advisory
# Fake-seam speech proxy (bytes→seconds) — CI TEST MATH ONLY (fake provider path).
_FAKE_BYTES_PER_SEC = 16000
_FAKE_SNR_DB = 20.0                           # fake fixed (passes the advisory snr)
# ON-BOX PLACEHOLDER (P4-4 dependency): the real (pyannote) path's net_speech_s is a
# byte-size proxy UNTIL VAD net-speech measurement on the decoded PCM lands. It DOES
# feed the 10 s too_short HARD gate on-box, so it is a (placeholder) contract surface —
# NOT the same as the fake-path constant above. Replace with VAD at the on-box #54 smoke.
_ONBOX_NET_SPEECH_PLACEHOLDER_BYTES_PER_SEC = 16000


class DecodeError(Exception):
    """Enrollment audio could not be decoded (the real PyAV path; → decode_failed)."""


@dataclass
class _EnrollSession:
    session_id: str
    user: str
    preset_id: str | None                    # re-record target, else None (new)
    windows: list[bytes] = field(default_factory=list)
    created_at: float = field(default_factory=time.monotonic)
    state: str = "recording"                 # recording | processing | done
    result: dict[str, Any] | None = None
    _task: Any = None                        # the background finalize task

    def total_bytes(self) -> int:
        return sum(len(w) for w in self.windows)

    def clear_bytes(self) -> None:
        self.windows = []                    # drop the biometric bytes (RAM custody)


# THE only custody store — process RAM. No disk, no reaper thread; TTL-swept on access.
_SESSIONS: dict[str, _EnrollSession] = {}


def _sweep_expired() -> None:
    now = time.monotonic()
    expired = [s for s, sess in _SESSIONS.items() if now - sess.created_at > _SESSION_TTL_S]
    for sid in expired:
        sess = _SESSIONS.pop(sid, None)
        if sess is not None:
            sess.clear_bytes()


def _cfg(request: web.Request) -> ScribeConfig:
    return request.app["scribe_config"]


# --- /enroll/start -----------------------------------------------------------

async def handle_enroll_start(request: web.Request) -> web.StreamResponse:
    _sweep_expired()
    config = _cfg(request)
    q = request.query
    user = q.get("user", "")
    preset = q.get("preset")                 # re-record target (optional)
    try:
        en.validate_user_for_enroll(user, config.clinicians)   # fail-CLOSED before recording
    except en.EnrollmentError:
        log.warning("scribe.enroll.rejected", route=ENROLL_START, reason="user_not_clinician")
        enroll_learning.audit(config.diarize.enrollment_dir, "enroll_rejected", user=user,
                              reason="user_not_clinician")
        return _reject("user_not_clinician", 403)
    if preset is not None:
        if not en.PRESET_ID_RE.fullmatch(preset):
            return _reject("invalid_preset", 400)
        # Re-record refuses while the preset is bound to an OPEN encounter (a mid-
        # encounter swap must never re-anchor a live recording).
        if _preset_bound_to_open_encounter(config, preset):
            log.warning("scribe.enroll.rejected", route=ENROLL_START, reason="preset_bound_open_encounter")
            return _reject("preset_bound_open_encounter", 409)
    if sum(1 for s in _SESSIONS.values()) >= _MAX_SESSIONS:
        log.warning("scribe.enroll.cap_hit", cap="sessions", limit=_MAX_SESSIONS)
        return _reject("too_many_sessions", 429)
    session_id = en.mint_session_id()
    _SESSIONS[session_id] = _EnrollSession(session_id=session_id, user=user, preset_id=preset)
    enroll_learning.audit(config.diarize.enrollment_dir, "enroll_started", user=user, preset_id=preset)
    return web.json_response({"session": session_id, "state": "recording"}, status=200)


def _preset_bound_to_open_encounter(config: ScribeConfig, preset_id: str) -> bool:
    input_dir = Path(config.input_dir)
    if not input_dir.is_dir():
        return False
    for enc in input_dir.iterdir():
        if not enc.is_dir():
            continue
        b = en.read_binding(enc)
        if b and b.get("preset_id") == preset_id and not (enc / CLOSE_SENTINEL_NAME).exists():
            return True
    return False


# --- /enroll/chunk -----------------------------------------------------------

async def handle_enroll_chunk(request: web.Request) -> web.StreamResponse:
    _sweep_expired()
    session = _SESSIONS.get(request.query.get("session", ""))
    if session is None or session.state != "recording":
        return _reject("unknown_session", 404)
    body = await request.read()
    if len(body) > _MAX_WINDOW_BYTES:
        log.warning("scribe.enroll.cap_hit", cap="window_bytes", limit=_MAX_WINDOW_BYTES)
        return _reject("window_too_large", 429)
    if len(session.windows) >= _MAX_WINDOWS:
        log.warning("scribe.enroll.cap_hit", cap="windows", limit=_MAX_WINDOWS)
        return _reject("too_many_windows", 429)
    if session.total_bytes() + len(body) > _MAX_SESSION_BYTES:
        log.warning("scribe.enroll.cap_hit", cap="session_bytes", limit=_MAX_SESSION_BYTES)
        return _reject("session_too_large", 429)
    session.windows.append(body)
    return web.json_response({"windows": len(session.windows)}, status=200)


# --- /enroll/finalize (async) + /result --------------------------------------

async def handle_enroll_finalize(request: web.Request) -> web.StreamResponse:
    _sweep_expired()
    config = _cfg(request)
    session = _SESSIONS.get(request.query.get("session", ""))
    if session is None or session.state != "recording":
        return _reject("unknown_session", 404)
    try:
        body = await request.json()
    except (json.JSONDecodeError, Exception):  # noqa: BLE001
        body = {}
    name = str((body or {}).get("name", "")).strip()[: en.NAME_MAX] or "unnamed"
    session.state = "processing"
    # Async: the CPU work (decode + embed) runs OFF the event loop; the client polls
    # /result. The task ALWAYS sets a result + clears bytes (RAM custody), even on error.
    session._task = asyncio.create_task(_run_finalize(config, session, name))
    return web.json_response({"state": "processing"}, status=200)


async def _run_finalize(config: ScribeConfig, session: _EnrollSession, name: str) -> None:
    try:
        result = await asyncio.to_thread(_finalize_sync, config, session, name)
    except Exception as e:  # noqa: BLE001 — a finalize crash must not wedge the session
        result = {"verdict": "engine_error", "stats": {}, "error_class": type(e).__name__}
    finally:
        session.clear_bytes()                # bytes gone success OR failure (RAM custody)
    session.result = result
    session.state = "done"


def _finalize_sync(config: ScribeConfig, session: _EnrollSession, name: str) -> dict[str, Any]:
    """Decode → embed → centroid → gates → atomic write. Returns the verdict dict
    ({verdict, stats, preset_id?}). Runs in a worker thread (to_thread)."""
    try:
        embed_inputs, net_speech_s = _prepare_windows(config, session.windows)
    except DecodeError:
        return {"verdict": "decode_failed", "stats": _degenerate_stats(session)}
    if not embed_inputs or net_speech_s <= 0.0:
        return {"verdict": "no_speech", "stats": _degenerate_stats(session)}
    if net_speech_s < _MIN_NET_SPEECH_S:
        return {"verdict": "too_short", "stats": _sample_stats(session, [], net_speech_s)}
    try:
        vecs = embed_voice.embed_windows(config, embed_inputs)
    except Exception:  # noqa: BLE001 — engine failure is a verdict, never a crash
        return {"verdict": "engine_error", "stats": _degenerate_stats(session)}
    centroid = en.spherical_mean_centroid(vecs)
    stats = _sample_stats(session, vecs, net_speech_s)
    advisory, verdict = _quality(stats)
    preset = _build_preset(config, session, name, centroid, stats, advisory, verdict)
    en.write_preset(config.diarize.enrollment_dir, preset, is_new=(session.preset_id is None))
    enroll_learning.audit(
        config.diarize.enrollment_dir,
        "preset_rerecorded" if session.preset_id else "preset_created",
        preset_id=preset.preset_id, user=preset.user,
        centroid_version=preset.centroid_version, verdict=verdict,
    )
    return {"verdict": verdict, "stats": stats, "preset_id": preset.preset_id}


def _prepare_windows(config: ScribeConfig, windows: list[bytes]) -> tuple[list[bytes], float]:
    """(embed_inputs, net_speech_s). FAKE: pass raw window bytes through, net-speech
    proxied from byte size (deterministic). PYANNOTE: PyAV BytesIO decode (webm+mp4
    via ``_sniff_container``) + VAD net-speech — the on-box path (raises DecodeError
    on an undecodable blob)."""
    provider = (config.diarize.provider or "").strip().lower()
    total = sum(len(w) for w in windows)
    if provider == "fake":
        return windows, total / _FAKE_BYTES_PER_SEC
    # Real path (on-box): each window is a container blob; decode by sniffed type.
    decoded: list[bytes] = []
    for w in windows:
        container = _sniff_container(w)
        if container is None:
            raise DecodeError("unrecognized enrollment container (not webm/mp4)")
        decoded.append(_decode_container(w, container))   # PyAV BytesIO (on-box)
    # net-speech would be VAD-measured on the decoded PCM; ON-BOX PLACEHOLDER proxy here
    # (a distinct constant from the fake-path one — this feeds the real too_short gate).
    return decoded, total / _ONBOX_NET_SPEECH_PLACEHOLDER_BYTES_PER_SEC


def _sniff_container(data: bytes) -> str | None:
    """Container dispatch for the phone PWA (iOS Safari emits mp4/AAC, others
    webm/opus). EBML magic → webm; an ``ftyp`` box at offset 4 → mp4. The
    format-DISPATCH seam is CI-pinned; the actual PyAV decode is on-box."""
    if len(data) >= 4 and data[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    if len(data) >= 8 and data[4:8] == b"ftyp":
        return "mp4"
    return None


def _decode_container(data: bytes, container: str) -> bytes:  # pragma: no cover — on-box PyAV
    """PyAV BytesIO decode → PCM bytes (on-box). Lazy-imports av; raises DecodeError
    on failure. Never reached in torch-free CI (the fake provider skips decode)."""
    try:
        import io
        import av
    except ImportError as e:
        raise DecodeError("PyAV not installed — the [scribe-diarize] on-box extra provides it") from e
    try:
        with av.open(io.BytesIO(data), format=container) as fh:
            pcm = b"".join(
                bytes(frame.planes[0]) for frame in fh.decode(audio=0)
            )
        return pcm
    except Exception as e:  # noqa: BLE001
        raise DecodeError(f"PyAV decode failed: {type(e).__name__}") from e


def _degenerate_stats(session: _EnrollSession) -> dict[str, Any]:
    return {"n_windows": len(session.windows), "duration_s": 0.0,
            "net_speech_s": 0.0, "snr_db_est": 0.0, "spread": 0.0}


def _sample_stats(session: _EnrollSession, vecs: list[list[float]], net_speech_s: float) -> dict[str, Any]:
    """The 5 frozen sample_stats keys, ALWAYS all present (a finalize regression
    can't silently write empty stats — pinned in the finalize tests)."""
    spread = 0.0
    if vecs:
        centroid = en.spherical_mean_centroid(vecs)
        sims = [en.cosine(v, centroid) for v in vecs]
        spread = round(1.0 - (sum(sims) / len(sims)), 4)
    return {
        "n_windows": len(session.windows),
        "duration_s": round(net_speech_s, 2),      # fake: duration == net speech
        "net_speech_s": round(net_speech_s, 2),
        "snr_db_est": _FAKE_SNR_DB,
        "spread": spread,
    }


def _quality(stats: dict[str, Any]) -> tuple[dict[str, bool], str]:
    """Advisory-until-calibrate gates → verdict ok | ok_marginal (the HARD gates
    fired earlier). A marginal preset is persisted with the badge; matching stays
    fail-closed regardless."""
    advisory = {
        "duration_ok": stats["net_speech_s"] >= _TARGET_DURATION_S,
        "snr_ok": stats["snr_db_est"] >= _ADVISORY_SNR_DB,
        "self_sim_ok": (1.0 - stats["spread"]) >= _ADVISORY_SELF_SIM,
    }
    return advisory, ("ok" if all(advisory.values()) else "ok_marginal")


def _build_preset(config, session, name, centroid, stats, advisory, verdict) -> en.Preset:
    now = en._iso_now()
    fp = embed_voice.engine_fingerprint(config)
    if session.preset_id is not None:                # RE-RECORD: same id, bump version
        prior, _ = en.load_preset(
            en.preset_path(config.diarize.enrollment_dir, session.user, session.preset_id))
        version = (prior.centroid_version + 1) if prior else 1
        created = prior.created_at if prior else now
        preset_id = session.preset_id
    else:
        version, created, preset_id = 1, now, en.mint_preset_id()
    return en.Preset(
        preset_id=preset_id, user=session.user, name=name, status=en.STATUS_ACTIVE,
        centroids=[centroid], embedding_dim=len(centroid),
        centroid_digest=en.centroid_digest([centroid]), centroid_version=version,
        centroid_source=en.CENTROID_SOURCE_RECORDED, enrolled_at=now,
        created_at=created, updated_at=now, engine=fp, sample_stats=stats,
        quality={"verdict": verdict, "advisory": advisory}, device_hint={},
    )


async def handle_enroll_result(request: web.Request) -> web.StreamResponse:
    _sweep_expired()
    session = _SESSIONS.get(request.query.get("session", ""))
    if session is None:
        return web.json_response({"state": "unknown_session"}, status=200)
    if session.state != "done":
        return web.json_response({"state": "processing"}, status=200)
    return web.json_response({"state": "done", **(session.result or {})}, status=200)


async def handle_enroll_abandon(request: web.Request) -> web.StreamResponse:
    session = _SESSIONS.pop(request.query.get("session", ""), None)
    if session is not None:
        session.clear_bytes()
    return web.json_response({"state": "abandoned"}, status=200)


# --- presets list / rename / delete ------------------------------------------

async def handle_presets_list(request: web.Request) -> web.StreamResponse:
    config = _cfg(request)
    user = request.query.get("user", "")
    if not en.valid_user(user):
        return _reject("invalid_user", 400)
    fp = embed_voice.engine_fingerprint(config)
    entries = en.list_user_presets(config.diarize.enrollment_dir, user, fp)
    presets = []
    for e in entries:                                # metadata + classification ONLY — no centroid
        p = e.preset
        presets.append({
            "preset_id": p.preset_id if p else e.path.stem,
            "name": (p.name if p else None),
            "status": (p.status if p else None),
            "classification": e.classification,
            "centroid_version": (p.centroid_version if p else None),
            "quality": (p.quality if p else None),
            "device_hint": (p.device_hint if p else None),
            "created_at": (p.created_at if p else None),
            "updated_at": (p.updated_at if p else None),
            "revoked": (p.revoked if p else None),
        })
    # empty-registry vs all-incompatible are DISTINCT explicit states.
    state = "empty" if not presets else (
        "all_incompatible" if all(x["classification"] != en.CLASS_USABLE for x in presets) else "ok")
    return web.json_response({"user": user, "state": state, "presets": presets}, status=200)


async def handle_presets_rename(request: web.Request) -> web.StreamResponse:
    config = _cfg(request)
    q = request.query
    user, preset_id = q.get("user", ""), q.get("preset", "")
    if not (en.valid_user(user) and en.PRESET_ID_RE.fullmatch(preset_id)):
        return _reject("invalid_request", 400)
    path = en.preset_path(config.diarize.enrollment_dir, user, preset_id)
    preset, fail = en.load_preset(path)
    if preset is None:
        return _reject("unknown_preset", 404)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    name = str((body or {}).get("name", "")).strip()[: en.NAME_MAX]
    if not name:
        return _reject("invalid_name", 400)
    d = preset.to_dict()
    d["name"], d["updated_at"] = name, en._iso_now()
    en._atomic_write_json(path, d)
    enroll_learning.audit(config.diarize.enrollment_dir, "preset_renamed", preset_id=preset_id, user=user)
    return web.json_response({"preset_id": preset_id, "name": name}, status=200)


async def handle_presets_delete(request: web.Request) -> web.StreamResponse:
    config = _cfg(request)
    q = request.query
    user, preset_id = q.get("user", ""), q.get("preset", "")
    if not (en.valid_user(user) and en.PRESET_ID_RE.fullmatch(preset_id)):
        return _reject("invalid_request", 400)
    try:
        en.revoke_preset(config.diarize.enrollment_dir, user, preset_id, reason="user_delete")
    except en.EnrollmentError:
        return _reject("unknown_preset", 404)
    enroll_learning.audit(config.diarize.enrollment_dir, "preset_deleted", preset_id=preset_id, user=user)
    return web.json_response({"preset_id": preset_id, "state": "revoked"}, status=200)


# --- POST /scribe/encounter/preset (ingest token; binding) -------------------

async def handle_encounter_preset(request: web.Request) -> web.StreamResponse:
    from alfred.scribe.ingest_web import ENCOUNTER_LABEL_RE
    config = _cfg(request)
    q = request.query
    label, preset_id = q.get("label", ""), q.get("preset", "")
    if not (ENCOUNTER_LABEL_RE.fullmatch(label) and en.PRESET_ID_RE.fullmatch(preset_id)):
        return _reject("invalid_request", 400)
    enc_dir = Path(config.input_dir) / label
    fp = embed_voice.engine_fingerprint(config)
    # find the preset by scanning the user dirs (label carries no user); it must be usable.
    resolved = _find_usable_preset(config, preset_id, fp)
    if resolved is None:
        return _reject("preset_unusable", 409)
    preset, _cls = resolved
    existing = en.read_binding(enc_dir)
    if existing is not None:
        # Locked at first bind. Same (user, preset, digest) = idempotent; different = 409.
        same = (existing.get("preset_id") == preset.preset_id
                and existing.get("centroid_digest") == preset.centroid_digest)
        if same:
            return web.json_response({"preset_id": preset.preset_id, "state": "bound"}, status=200)
        log.warning("scribe.enroll.rejected", route=ENCOUNTER_PRESET, reason="preset_locked")
        return _reject("preset_locked", 409)
    try:
        en.write_binding(enc_dir, preset)
    except en.EnrollmentError:
        return _reject("preset_locked", 409)         # raced to write-once
    enroll_learning.audit(config.diarize.enrollment_dir, "preset_selected",
                          preset_id=preset.preset_id, user=preset.user)
    return web.json_response({"preset_id": preset.preset_id, "state": "bound"}, status=200)


def _find_usable_preset(config: ScribeConfig, preset_id: str, fp: dict[str, Any]):
    enroll_dir = config.diarize.enrollment_dir
    root = Path(enroll_dir) if enroll_dir else None
    if root is None or not root.is_dir():
        return None
    for ud in root.iterdir():
        if not ud.is_dir() or not en.valid_user(ud.name):
            continue
        path = en.preset_path(enroll_dir, ud.name, preset_id)
        if path.is_file():
            preset, fail = en.load_preset(path)
            cls = en.classify(preset, fail, fp)
            return (preset, cls) if (preset is not None and cls == en.CLASS_USABLE) else None
    return None


# --- registration ------------------------------------------------------------

def register_enroll_routes(app: web.Application) -> None:
    """Register the enrollment face on the ingest app. Called by
    ``ingest_web.create_ingest_app`` ONLY when ``enroll_token`` is set (else the
    face is inert — the middleware 404s these paths)."""
    app.router.add_post(ENROLL_START, handle_enroll_start)
    app.router.add_post(ENROLL_CHUNK, handle_enroll_chunk)
    app.router.add_post(ENROLL_FINALIZE, handle_enroll_finalize)
    app.router.add_get(ENROLL_RESULT, handle_enroll_result)
    app.router.add_post(ENROLL_ABANDON, handle_enroll_abandon)
    app.router.add_get(PRESETS_LIST, handle_presets_list)
    app.router.add_post(PRESETS_RENAME, handle_presets_rename)
    app.router.add_post(PRESETS_DELETE, handle_presets_delete)
    app.router.add_post(ENCOUNTER_PRESET, handle_encounter_preset)
