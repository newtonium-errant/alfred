"""Tests for the sovereign loopback PWA ingest backend (#49 Slice A).

Layers:
  * config — INERT default, coercion.
  * barrier-e (sovereign boundary) — 0.0.0.0 / :: / missing-token / egress-field
    refused at load; loopback passes; inert no-ops. (exit-79 class.)
  * pins — the route's audio-ext set == pipeline's; the sub-tree allowlist ==
    the dataclass fields (no drift).
  * routes — chunk-naming contract (B1), atomic writes, server-validated seq
    (contract #3), R6 label rejection, synthetic gate (refuse-before-disk),
    token fail-closed, Host-pin (R3), no-CORS, caps (N3), close (B3), NON-PHI
    status (R2), PHI-in-error/log negative.
  * pipeline hardening — W2 per-chunk + per-subdir isolation (mutation-bind).
  * e2e — route → sweep → accumulate → transcript → draft (fake STT
    unconditional; REAL whisper decode gated on the [scribe] extra + a staged
    model, per feedback_regression_pin_unconditional — CORE pins stay unskipped).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import socket
import wave
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import pytest
import structlog

import alfred.distiller.backends.ollama as ollama_mod
from alfred.scribe import (
    ScribeState,
    compute_encounter_id,
    ledger_path,
    load_ledger,
    run_sweep,
)
from alfred.scribe.config import (
    INGEST_WEB_ALLOWED_KEYS,
    ScribeConfig,
    ScribeIngestWebConfig,
    ScribeLlmConfig,
    ScribeSttConfig,
    load_from_unified,
)
from alfred.scribe import ingest_web as iw
from alfred.scribe.ingest_web import IngestWebServer
from alfred.scribe.pipeline import _AUDIO_EXTENSIONS
from alfred.sovereign.boundary import SovereignBoundaryError, validate_sovereign_boundary

_SALT = "DUMMY_SCRIBE_TEST_SALT"
_TOKEN = "secret-ingest-token-xyz"
_LABEL = "enc-1720000000000-0123456789abcdef"     # the machine-token shape (R6)
_LABEL2 = "enc-1720000000001-fedcba9876543210"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _config(tmp_path, *, enabled=True, token=_TOKEN, host="127.0.0.1", port=None, mode="synthetic", **web_over):
    return ScribeConfig(
        mode=mode,
        input_dir=str(tmp_path / "inbox"),
        stt=ScribeSttConfig(provider="fake"),
        llm=ScribeLlmConfig(base_url="http://127.0.0.1:11434", model="qwen2.5:14b"),
        ingest_web=ScribeIngestWebConfig(
            enabled=enabled, host=host, port=port or _free_port(), token=token, **web_over,
        ),
        encounter_salt=_SALT,
    )


@asynccontextmanager
async def _serve(config):
    server = IngestWebServer(config)
    await server.start()
    try:
        yield f"http://127.0.0.1:{config.ingest_web.port}"
    finally:
        await server.stop()


def _auth(token=_TOKEN, host=None):
    h = {}
    if token is not None:
        h["Authorization"] = f"Bearer {token}"
    if host is not None:
        h["Host"] = host
    return h


async def _post_chunk(sess, base, *, label=_LABEL, seq=1, ext="webm", synthetic="true",
                      body=b"AUDIOBYTES", close=None, token=_TOKEN, host=None):
    params = {"label": label, "seq": str(seq), "ext": ext, "synthetic": synthetic}
    if close is not None:
        params["close"] = close
    async with sess.post(base + iw.INGEST_CHUNK_ROUTE, params=params, data=body,
                         headers=_auth(token, host)) as r:
        payload = await r.json() if r.content_type == "application/json" else await r.text()
        return r.status, payload, r.headers


# ---------------------------------------------------------------------------
# config + pins
# ---------------------------------------------------------------------------

def test_ingest_web_config_defaults_inert():
    c = ScribeIngestWebConfig()
    assert c.enabled is False                 # INERT by default
    assert c.host == "127.0.0.1"
    assert c.token == ""


def test_config_absent_block_is_inert():
    cfg = load_from_unified({"scribe": {"encounter_salt": _SALT, "stt": {"provider": "fake"}}})
    assert cfg.ingest_web.enabled is False    # no ingest_web block → inert


def test_config_coercion_string_enabled_and_int_port():
    cfg = load_from_unified({"scribe": {"encounter_salt": _SALT, "ingest_web": {
        "enabled": "true", "port": "9999", "token": "t"}}})
    assert cfg.ingest_web.enabled is True and cfg.ingest_web.port == 9999


def test_route_ext_set_matches_pipeline():
    # The route must only accept exts the sweep discovers (else a chunk is written
    # but never folded). Pin against pipeline._AUDIO_EXTENSIONS.
    assert {"." + e for e in iw.ALLOWED_AUDIO_EXTS} == _AUDIO_EXTENSIONS


def test_subtree_allowlist_matches_dataclass_fields():
    # Barrier-e's sub-tree allowlist must track the dataclass exactly (no drift:
    # a new field must be allowlisted, or barrier-e falsely refuses it).
    assert INGEST_WEB_ALLOWED_KEYS == set(ScribeIngestWebConfig.__dataclass_fields__)


def test_mp4_not_accepted():
    assert "mp4" not in iw.ALLOWED_AUDIO_EXTS  # Safari path deliberately excluded (contract #2)


# ---------------------------------------------------------------------------
# barrier-e (sovereign boundary — exit-79 class)
# ---------------------------------------------------------------------------

def _sov_raw(**ingest):
    return {
        "sovereign": {"enabled": True},
        "scribe": {
            "stt": {"provider": "fake"},
            "llm": {"base_url": "http://127.0.0.1:11434"},
            "ingest_web": ingest,
        },
    }


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.0.5", "example.com"])
def test_barrier_e_non_loopback_host_refused(host):
    with pytest.raises(SovereignBoundaryError) as exc:
        validate_sovereign_boundary(_sov_raw(enabled=True, host=host, token="t"), env={})
    assert exc.value.reason == "barrier_e"


def test_barrier_e_missing_token_refused():
    with pytest.raises(SovereignBoundaryError) as exc:
        validate_sovereign_boundary(_sov_raw(enabled=True, host="127.0.0.1", token=""), env={})
    assert exc.value.reason == "barrier_e"


def test_barrier_e_egress_field_refused():
    # An unexpected egress-shaped field in the sub-tree → refused (allowlist-closed).
    with pytest.raises(SovereignBoundaryError) as exc:
        validate_sovereign_boundary(
            _sov_raw(enabled=True, host="127.0.0.1", token="t",
                     forward_url="https://api.evil.com/collect"), env={})
    assert exc.value.reason == "barrier_e"


def test_barrier_e_loopback_passes():
    # localhost / 127.0.0.1 / ::1 all pass with a token present.
    for host in ("127.0.0.1", "localhost", "::1"):
        validate_sovereign_boundary(_sov_raw(enabled=True, host=host, token="t"), env={})


def test_barrier_e_noop_when_inert():
    # enabled:false → barrier-e is a no-op even with a 0.0.0.0 host (no server binds).
    validate_sovereign_boundary(_sov_raw(enabled=False, host="0.0.0.0"), env={})


def test_barrier_e_enabled_coercion_aligned_with_typed_load():
    # "does the barrier validate" == "does the server bind": a quoted enabled:
    # "false" is INERT in BOTH (no false-positive barrier-e breach), and a quoted
    # "true" ARMS both. Shared coerce_ingest_web_enabled guarantees the alignment.
    validate_sovereign_boundary(_sov_raw(enabled="false", host="0.0.0.0"), env={})  # inert → no-op
    cfg = load_from_unified({"scribe": {"encounter_salt": _SALT, "ingest_web": {
        "enabled": "false", "host": "0.0.0.0"}}})
    assert cfg.ingest_web.enabled is False           # typed loader agrees → inert
    # quoted "true" arms barrier-e → a 0.0.0.0 host is then refused.
    with pytest.raises(SovereignBoundaryError) as exc:
        validate_sovereign_boundary(_sov_raw(enabled="true", host="0.0.0.0", token="t"), env={})
    assert exc.value.reason == "barrier_e"


# ---------------------------------------------------------------------------
# Audit FIX 1 — barrier-e env-placeholder seam (was fail-OPEN: barrier read the
# RAW ${VAR}, the server binds from the SUBSTITUTED value)
# ---------------------------------------------------------------------------

def test_barrier_e_enabled_env_placeholder_arms_and_validates(monkeypatch):
    # `enabled: "${SCRIBE_WEB}"` with SCRIBE_WEB=true + host 0.0.0.0. Pre-fix the
    # barrier coerced the LITERAL "${SCRIBE_WEB}" → False → returned inert while the
    # typed config substituted → True → the daemon BOUND to 0.0.0.0 (fail-OPEN).
    # Now the barrier substitutes the sub-tree → enabled resolves True → the 0.0.0.0
    # host is refused. MUTATION-BIND: revert barrier-e to read raw → this passes
    # (fail-open) → RED against this assertion.
    monkeypatch.setenv("SCRIBE_WEB", "true")
    with pytest.raises(SovereignBoundaryError) as exc:
        validate_sovereign_boundary(
            _sov_raw(enabled="${SCRIBE_WEB}", host="0.0.0.0", token="realtok"), env={})
    assert exc.value.reason == "barrier_e"
    # and the barrier now AGREES with the typed loader (both resolve enabled True).
    cfg = load_from_unified({"scribe": {"encounter_salt": _SALT, "ingest_web": {
        "enabled": "${SCRIBE_WEB}", "host": "127.0.0.1", "token": "x"}}})
    assert cfg.ingest_web.enabled is True


def test_barrier_e_enabled_env_placeholder_unset_is_inert(monkeypatch):
    # Unset ${SCRIBE_WEB} → the literal stays → coerce False in BOTH the barrier and
    # the server → inert (no bind, no validation). Consistent, not fail-open.
    monkeypatch.delenv("SCRIBE_WEB", raising=False)
    validate_sovereign_boundary(_sov_raw(enabled="${SCRIBE_WEB}", host="0.0.0.0"), env={})  # no raise
    cfg = load_from_unified({"scribe": {"encounter_salt": _SALT, "ingest_web": {
        "enabled": "${SCRIBE_WEB}", "host": "0.0.0.0"}}})
    assert cfg.ingest_web.enabled is False


def test_barrier_e_token_env_placeholder_unset_refused(monkeypatch):
    # `token: "${SCRIBE_INGEST_TOKEN}"` with the var UNSET → the literal placeholder
    # would become the live source-visible bearer. barrier-e must REFUSE (fail-loud
    # on a missing secret). MUTATION-BIND: remove the placeholder check → the
    # non-empty literal passes → RED.
    monkeypatch.delenv("SCRIBE_INGEST_TOKEN", raising=False)
    with pytest.raises(SovereignBoundaryError) as exc:
        validate_sovereign_boundary(
            _sov_raw(enabled=True, host="127.0.0.1", token="${SCRIBE_INGEST_TOKEN}"), env={})
    assert exc.value.reason == "barrier_e"


def test_barrier_e_resolved_env_placeholders_pass(monkeypatch):
    # All three fields via placeholders that RESOLVE to loopback/real values → pass.
    monkeypatch.setenv("SCRIBE_WEB", "true")
    monkeypatch.setenv("SCRIBE_HOST", "127.0.0.1")
    monkeypatch.setenv("SCRIBE_INGEST_TOKEN", "a-real-secret")
    validate_sovereign_boundary(
        _sov_raw(enabled="${SCRIBE_WEB}", host="${SCRIBE_HOST}", token="${SCRIBE_INGEST_TOKEN}"),
        env={})  # no raise


def test_barrier_c_cloud_key_placeholder_still_detected_gap_d(monkeypatch):
    # Gap-D REGRESSION PIN (end-to-end): the barrier-e substitution must NOT disarm
    # barrier-c's RAW-config ${CLOUD_KEY} scan. A ${ANTHROPIC_API_KEY} placeholder
    # anywhere in the (un-substituted) config still trips barrier-c.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    raw = _sov_raw(enabled=True, host="127.0.0.1", token="realtok")
    raw["logging"] = {"some_field": "${ANTHROPIC_API_KEY}"}
    with pytest.raises(SovereignBoundaryError) as exc:
        validate_sovereign_boundary(raw, env={})
    assert exc.value.reason == "barrier_c"


def test_barrier_e_does_not_mutate_raw_config_gap_d_direct(monkeypatch):
    # Gap-D DIRECT PIN (the non-mutation property itself, NOT barrier ordering):
    # after a SUCCESSFUL validate_sovereign_boundary on an ingest_web with ${VAR}
    # placeholders, the RAW config's host/token are STILL the literal placeholders —
    # barrier-e substituted a fresh COPY (substitute_env_in_value), never `raw`. So
    # barrier-c's RAW ${CLOUD_KEY} scan is provably unaffected regardless of barrier
    # ordering. (env resolves them to loopback/real so the barrier PASSES + returns.)
    monkeypatch.setenv("SCRIBE_WEB_HOST", "127.0.0.1")
    monkeypatch.setenv("SCRIBE_WEB_TOKEN", "a-real-secret")
    raw = _sov_raw(enabled=True, host="${SCRIBE_WEB_HOST}", token="${SCRIBE_WEB_TOKEN}")
    validate_sovereign_boundary(raw, env={})   # PASSES (host resolves loopback, token real)
    # RAW is UNMUTATED — still the literal placeholders (the barrier substituted a copy).
    assert raw["scribe"]["ingest_web"]["host"] == "${SCRIBE_WEB_HOST}"
    assert raw["scribe"]["ingest_web"]["token"] == "${SCRIBE_WEB_TOKEN}"


# ---------------------------------------------------------------------------
# routes — the chunk-naming contract + atomicity (B1, contract #4/#6/#7)
# ---------------------------------------------------------------------------

def test_valid_chunk_written_with_contract_names(tmp_path):
    cfg = _config(tmp_path)

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            status, payload, _ = await _post_chunk(s, base, seq=1)
            assert status == 200
            assert payload["encounter_id"] == compute_encounter_id(_LABEL, salt=_SALT)
            assert payload["seq"] == 1

    asyncio.run(_go())
    enc_dir = Path(cfg.input_dir) / _LABEL
    assert (enc_dir / "chunk_1.webm").is_file()                 # B1 audio name
    meta = json.loads((enc_dir / "chunk_1.meta.json").read_text())  # B1 sidecar name
    assert meta == {"synthetic": True, "seq": 1}               # LITERAL boolean (contract #6)
    # atomic: no .tmp residue.
    assert not list(enc_dir.glob("*.tmp"))


def test_seq_must_be_monotonic_gapfree(tmp_path):
    cfg = _config(tmp_path)

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            # first chunk must be seq 1 — seq 2 first is refused.
            st, _, _ = await _post_chunk(s, base, seq=2)
            assert st == 409
            st, _, _ = await _post_chunk(s, base, seq=1)
            assert st == 200
            # replay seq 1 (expected is now 2) → refused.
            st, _, _ = await _post_chunk(s, base, seq=1)
            assert st == 409
            # a gap (seq 3 when expected 2) → refused.
            st, _, _ = await _post_chunk(s, base, seq=3)
            assert st == 409
            # the contiguous next → accepted.
            st, _, _ = await _post_chunk(s, base, seq=2)
            assert st == 200

    asyncio.run(_go())


# ---------------------------------------------------------------------------
# R6 label + synthetic gate + token + Host-pin + CORS
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "John Doe 1980-01-01", "MRN-4432211", "../../etc/passwd", "enc-123-abc",
    "enc-1720000000000-XYZ", ".hidden", "",
])
def test_label_non_token_shape_rejected(tmp_path, bad):
    cfg = _config(tmp_path)

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            st, payload, _ = await _post_chunk(s, base, label=bad, seq=1)
            return st, payload

    st, payload = asyncio.run(_go())
    assert st == 400 and payload["error"] == "invalid_label"    # R6
    # no dir created for a bad label (nothing written).
    assert not (Path(cfg.input_dir) / bad).exists()


@pytest.mark.parametrize("suffix", ["\n", "\r\n", "\x00"])
def test_label_trailing_newline_rejected(tmp_path, suffix):
    # WARN-1 (R6 strictness): Python's `$` matches BEFORE a trailing \n, so
    # re.match would ACCEPT ``enc-…-…\n`` (a distinct dir name that splits an
    # encounter). fullmatch requires the WHOLE string → the newline variant is
    # refused. Build the URL by hand so the raw %0A reaches request.query intact.
    import urllib.parse
    cfg = _config(tmp_path)
    label = _LABEL + suffix

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            qs = urllib.parse.urlencode({"label": label, "seq": "1", "ext": "webm", "synthetic": "true"})
            async with s.post(f"{base}{iw.INGEST_CHUNK_ROUTE}?{qs}", data=b"x", headers=_auth()) as r:
                return r.status, await r.json()

    st, payload = asyncio.run(_go())
    assert st == 400 and payload["error"] == "invalid_label"
    # nothing written — no enc- dir for the newline-suffixed label.
    inbox = Path(cfg.input_dir)
    assert not inbox.exists() or not list(inbox.iterdir())


def test_synthetic_gate_refuses_before_disk(tmp_path):
    cfg = _config(tmp_path)

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            st, payload, _ = await _post_chunk(s, base, seq=1, synthetic="false")
            return st, payload

    st, payload = asyncio.run(_go())
    assert st == 403 and payload["error"] == "synthetic_required"
    # refuse-BEFORE-disk: nothing written.
    assert not (Path(cfg.input_dir) / _LABEL).exists()


def test_token_fail_closed(tmp_path):
    cfg = _config(tmp_path)

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            wrong, _, _ = await _post_chunk(s, base, seq=1, token="WRONG")
            missing, _, _ = await _post_chunk(s, base, seq=1, token=None)
            return wrong, missing

    wrong, missing = asyncio.run(_go())
    assert wrong == 401 and missing == 401
    assert not (Path(cfg.input_dir) / _LABEL).exists()          # fail-closed: no write


def test_host_pin_rejects_wrong_host(tmp_path):
    cfg = _config(tmp_path)

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            # a DNS-rebind request carries an attacker domain as Host.
            st, payload, _ = await _post_chunk(
                s, base, seq=1, host=f"evil.example.com:{cfg.ingest_web.port}")
            return st, payload

    st, payload = asyncio.run(_go())
    assert st == 421 and payload["error"] == "wrong_host"       # R3


def test_no_cors_headers_anywhere(tmp_path):
    cfg = _config(tmp_path)

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            _, _, headers = await _post_chunk(s, base, seq=1)
            return headers

    headers = asyncio.run(_go())
    assert not any(k.lower().startswith("access-control-") for k in headers)  # R3.2


def test_unsupported_ext_rejected(tmp_path):
    cfg = _config(tmp_path)

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            st, payload, _ = await _post_chunk(s, base, seq=1, ext="mp4")
            return st, payload

    st, payload = asyncio.run(_go())
    assert st == 400 and payload["error"] == "unsupported_ext"


def test_silent_rejects_now_logged_ilb(tmp_path):
    # #10 (ILB): the previously-SILENT validation rejects now emit a PHI-safe
    # scribe.ingest_web.rejected log (opaque reason only), matching the sibling
    # paths — so a client erroring every request is distinguishable from no traffic.
    cfg = _config(tmp_path)

    async def _go():
        with structlog.testing.capture_logs() as caps:
            async with _serve(cfg) as base, aiohttp.ClientSession() as s:
                await _post_chunk(s, base, seq=0)                      # invalid_seq (< 1)
                await _post_chunk(s, base, seq="abc")                  # invalid_seq (unparseable)
                await _post_chunk(s, base, seq=1, ext="mp4")           # unsupported_ext
                await _post_chunk(s, base, seq=1, body=b"")            # empty_chunk
                async with s.post(base + iw.CLOSE_ROUTE,              # invalid_label (close)
                                 params={"label": "not-a-token"}, headers=_auth()):
                    pass
                async with s.get(base + iw.STATUS_ROUTE,             # invalid_label (status)
                                params={"label": "not-a-token"}, headers=_auth()):
                    pass
        return caps

    caps = asyncio.run(_go())
    rejects = [c for c in caps if c.get("event") == "scribe.ingest_web.rejected"]
    reasons = {c.get("reason") for c in rejects}
    assert {"invalid_seq", "unsupported_ext", "empty_chunk", "invalid_label"} <= reasons
    # PHI-safe: reject logs carry ONLY opaque codes — never the raw label / audio.
    for c in rejects:
        assert "label" not in c                                # no raw label field
        assert all("not-a-token" not in str(v) for v in c.values())  # bad label never echoed


# ---------------------------------------------------------------------------
# close (B3) + status (R2/N4) + caps (N3)
# ---------------------------------------------------------------------------

def test_close_route_writes_sentinel(tmp_path):
    cfg = _config(tmp_path)

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            await _post_chunk(s, base, seq=1)
            async with s.post(base + iw.CLOSE_ROUTE, params={"label": _LABEL},
                             headers=_auth()) as r:
                close_status = r.status
            # unknown encounter → 404
            async with s.post(base + iw.CLOSE_ROUTE, params={"label": _LABEL2},
                             headers=_auth()) as r2:
                unknown_status = r2.status
            return close_status, unknown_status

    close_status, unknown_status = asyncio.run(_go())
    assert close_status == 200 and unknown_status == 404
    assert (Path(cfg.input_dir) / _LABEL / "_CLOSED").is_file()  # B3


def test_close_flag_on_final_chunk(tmp_path):
    cfg = _config(tmp_path)

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            await _post_chunk(s, base, seq=1, close="true")

    asyncio.run(_go())
    assert (Path(cfg.input_dir) / _LABEL / "_CLOSED").is_file()


def test_status_non_phi_only(tmp_path):
    cfg = _config(tmp_path)

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            await _post_chunk(s, base, seq=1)
            await _post_chunk(s, base, seq=2, close="true")
            async with s.get(base + iw.STATUS_ROUTE, params={"label": _LABEL},
                            headers=_auth()) as r:
                return r.status, await r.json()

    st, body = asyncio.run(_go())
    assert st == 200
    # NON-PHI keys ONLY — no transcript/draft/segment/body field may appear (R2).
    assert set(body) == {"encounter_id", "chunks", "max_seq", "closed", "state"}
    assert body["chunks"] == 2 and body["max_seq"] == 2 and body["closed"] is True
    assert body["state"] == "closed"
    assert body["encounter_id"] == compute_encounter_id(_LABEL, salt=_SALT)


def test_caps_chunk_count(tmp_path):
    cfg = _config(tmp_path, max_chunks_per_encounter=1)

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            with structlog.testing.capture_logs() as caps:
                st1, _, _ = await _post_chunk(s, base, seq=1)
                st2, payload, _ = await _post_chunk(s, base, seq=2)
            return st1, st2, payload, caps

    st1, st2, payload, caps = asyncio.run(_go())
    assert st1 == 200 and st2 == 413 and payload["error"] == "chunk_cap"
    assert any(c.get("event") == "scribe.ingest_web.cap_hit" and c.get("cap") == "chunks" for c in caps)


def test_caps_encounter_bytes(tmp_path):
    cfg = _config(tmp_path, max_encounter_bytes=5)

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            with structlog.testing.capture_logs() as caps:
                st, payload, _ = await _post_chunk(s, base, seq=1, body=b"0123456789")
            return st, payload, caps

    st, payload, caps = asyncio.run(_go())
    assert st == 413 and payload["error"] == "encounter_cap"
    assert any(c.get("event") == "scribe.ingest_web.cap_hit" and c.get("cap") == "encounter_bytes" for c in caps)


def test_caps_chunk_bytes_client_max_size(tmp_path):
    cfg = _config(tmp_path, max_chunk_bytes=8)

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            st, payload, _ = await _post_chunk(s, base, seq=1, body=b"way-too-many-bytes")
            return st, payload

    st, payload = asyncio.run(_go())
    assert st == 413 and payload["error"] == "chunk_too_large"


# ---------------------------------------------------------------------------
# PHI-in-error/log negative
# ---------------------------------------------------------------------------

def test_phi_never_in_error_body_or_logs(tmp_path):
    # A PHI-ish label must never echo back in the error body nor appear in any log.
    cfg = _config(tmp_path)
    phi = "Jane-Patient-DOB-1970"

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            with structlog.testing.capture_logs() as caps:
                async with s.post(base + iw.INGEST_CHUNK_ROUTE,
                                 params={"label": phi, "seq": "1", "ext": "webm", "synthetic": "true"},
                                 data=b"x", headers=_auth()) as r:
                    body = await r.text()
            return body, caps

    body, caps = asyncio.run(_go())
    assert phi not in body                                       # opaque error, no label echo
    assert all(phi not in json.dumps(c, default=str) for c in caps)  # no PHI in logs


# ---------------------------------------------------------------------------
# daemon wiring — INERT default pin + enabled-starts
# ---------------------------------------------------------------------------

def test_daemon_inert_when_disabled(tmp_path):
    from alfred.scribe.daemon import _maybe_start_ingest_server
    cfg = _config(tmp_path, enabled=False)

    async def _go():
        with structlog.testing.capture_logs() as caps:
            server = await _maybe_start_ingest_server(cfg)
        return server, caps

    server, caps = asyncio.run(_go())
    assert server is None                                        # NO server bound (inert)
    ev = [c for c in caps if c.get("event") == "scribe.ingest_web.up"]
    assert len(ev) == 1 and ev[0]["enabled"] is False           # ILB: inert distinguishable from broken


def test_daemon_starts_server_when_enabled(tmp_path):
    from alfred.scribe.daemon import _maybe_start_ingest_server
    cfg = _config(tmp_path, enabled=True)

    async def _go():
        with structlog.testing.capture_logs() as caps:
            server = await _maybe_start_ingest_server(cfg)
        try:
            up = [c for c in caps if c.get("event") == "scribe.ingest_web.up"]
            assert len(up) == 1 and up[0]["enabled"] is True and up[0]["port"] == cfg.ingest_web.port
        finally:
            await server.stop()

    asyncio.run(_go())


# ---------------------------------------------------------------------------
# e2e — route → sweep → accumulate → transcript → draft (FAKE STT, unconditional)
# ---------------------------------------------------------------------------

def _fake_ollama_returning(canned):
    async def _fake(prompt, system=None, model="", endpoint="", **kw):
        return (canned, {"stop_reason": "stop", "prompt_eval_count": 500})
    return _fake


_CANNED = json.dumps({
    "subjective": [{"claim": "Chest pain for 2 days", "source_spans": ["S1"]}],
    "objective": [], "assessment": [], "plan": [],
    "assessment_reasoning_stated": False,
})


def test_route_to_sweep_to_draft_fake_stt(tmp_path, monkeypatch):
    # The route writes exactly what the sweep consumes: a chunk POSTed via the
    # ingest face is discovered, folded, and drafted by the UNCHANGED pipeline.
    # (Fake STT — the naming/settle/gate/W1 mechanics; the REAL whisper decode is
    # the gated test below.)
    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools", _fake_ollama_returning(_CANNED))
    cfg = _config(tmp_path)
    vault = tmp_path / "vault"

    async def _ingest():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            st, _, _ = await _post_chunk(s, base, seq=1, close="true")
            assert st == 200

    asyncio.run(_ingest())
    enc_dir = Path(cfg.input_dir) / _LABEL
    # fake STT reads the sibling .txt (stands in for a decode of the audio bytes).
    (enc_dir / "chunk_1.txt").write_text("Patient reports chest pain for 2 days.\n", encoding="utf-8")

    state = ScribeState(str(tmp_path / "state.json"))
    counts = asyncio.run(run_sweep(cfg, state, vault))

    assert counts["chunks_folded"] == 1
    eid = compute_encounter_id(_LABEL, salt=_SALT)
    ledger = load_ledger(ledger_path(enc_dir, eid))
    assert ledger is not None and ledger.segments        # transcript accumulated
    assert ledger.closed is True                         # _CLOSED finalized it
    # a clinical_note ai_draft landed in the vault.
    drafts = list((vault / "clinical_note").glob("*.md")) if (vault / "clinical_note").is_dir() else []
    assert drafts, "expected a clinical_note ai_draft"


def test_w2_undecodable_chunk_isolated_sweep_survives(tmp_path, monkeypatch):
    # W2 mutation-bind: encounter A has a settled chunk that FAILS to decode (fake
    # STT raises — no .txt sidecar); encounter B is healthy. A sorts BEFORE B.
    # With per-chunk + per-subdir isolation: A is held+isolated (decode_failed),
    # the sweep SURVIVES, and B is still folded + drafted.
    #   MUTATION-BIND (verified manually): remove the try/except around
    #   stt_mod.transcribe in accumulate_encounter (and the per-subdir guard in
    #   run_sweep) → A's STTError propagates → the sweep dies → B never processed.
    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools", _fake_ollama_returning(_CANNED))
    cfg = _config(tmp_path)
    vault = tmp_path / "vault"
    a = "enc-1720000000000-000000000000000a"      # sorts before b
    b = "enc-1720000000000-000000000000000b"

    async def _ingest():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            assert (await _post_chunk(s, base, label=a, seq=1, close="true"))[0] == 200
            assert (await _post_chunk(s, base, label=b, seq=1, close="true"))[0] == 200

    asyncio.run(_ingest())
    # B gets a .txt (decodes); A does NOT (fake STT raises → undecodable chunk).
    (Path(cfg.input_dir) / b / "chunk_1.txt").write_text("Cough for three days.\n", encoding="utf-8")

    state = ScribeState(str(tmp_path / "state.json"))
    with structlog.testing.capture_logs() as caps:
        counts = asyncio.run(run_sweep(cfg, state, vault))

    # A isolated (its chunk decode-failed, NOT folded); B folded despite A failing first.
    assert any(c.get("event") == "scribe.accumulator.chunk_decode_failed" for c in caps)
    assert counts["encounters"] == 2
    assert counts["chunks_folded"] == 1                  # only B folded
    eid_b = compute_encounter_id(b, salt=_SALT)
    assert load_ledger(ledger_path(Path(cfg.input_dir) / b, eid_b)).segments  # B processed


def test_w2_per_subdir_isolation_accumulate_raises(tmp_path, monkeypatch):
    # W2 per-SUBDIR isolation — its UNIQUE value (distinct from per-chunk): a raise
    # from accumulate_encounter ITSELF (a corrupt ledger, an OSError in
    # _discover_chunks, a checkpoint error) — NOT a per-chunk STTError, which the
    # inner guard already catches. Force accumulate_encounter to RAISE for A → the
    # sweep must SURVIVE, emit the fail-isolated signal, and STILL process B.
    #   MUTATION-BIND (verified manually): remove the per-subdir try/except in
    #   run_sweep → A's raise propagates out of run_sweep → the whole sweep dies →
    #   B never processed → RED. Reverted clean.
    import alfred.scribe.pipeline as pipeline_mod
    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools", _fake_ollama_returning(_CANNED))
    cfg = _config(tmp_path)
    vault = tmp_path / "vault"
    a = "enc-1720000000000-000000000000000a"      # sorts before b
    b = "enc-1720000000000-000000000000000b"

    async def _ingest():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            assert (await _post_chunk(s, base, label=a, seq=1, close="true"))[0] == 200
            assert (await _post_chunk(s, base, label=b, seq=1, close="true"))[0] == 200

    asyncio.run(_ingest())
    (Path(cfg.input_dir) / b / "chunk_1.txt").write_text("Sore throat two days.\n", encoding="utf-8")

    real_accum = pipeline_mod.accumulate_encounter

    def _raising_accum(enc_dir, **kw):
        if enc_dir.name == a:
            raise RuntimeError("corrupt ledger")   # a raise from accumulate ITSELF
        return real_accum(enc_dir, **kw)

    monkeypatch.setattr(pipeline_mod, "accumulate_encounter", _raising_accum)

    state = ScribeState(str(tmp_path / "state.json"))
    with structlog.testing.capture_logs() as caps:
        counts = asyncio.run(run_sweep(cfg, state, vault))

    # A raised → isolated (fail-isolated signal), the sweep SURVIVED, B still folded.
    assert any(c.get("event") == "scribe.pipeline.encounter_error" for c in caps)
    assert counts["failed"] >= 1                          # A isolated as failed
    assert counts["chunks_folded"] == 1                  # B folded despite A raising first
    eid_b = compute_encounter_id(b, salt=_SALT)
    assert load_ledger(ledger_path(Path(cfg.input_dir) / b, eid_b)).segments  # B processed


# ---------------------------------------------------------------------------
# REAL-DECODE e2e (HARD) — gated on the [scribe] extra + a staged model
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    importlib.util.find_spec("faster_whisper") is None,
    reason="real-decode e2e needs the [scribe] extra (faster-whisper) + a staged model",
)
def test_real_decode_e2e_through_route(tmp_path, monkeypatch):
    # HARD acceptance: drive a REAL whisper decode of a self-contained WAV written
    # via the route → sweep → accumulate → transcript. Fake STT hides B1/B2/W1/W2;
    # this proves the route's on-disk chunk is decodable by the actual local model
    # on the real pipeline. (Asserting specific transcript TEXT needs a recorded
    # speech fixture — deferred; here we prove the decode PATH completes + a ledger
    # is written under the real decoder.)
    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools", _fake_ollama_returning(_CANNED))
    cfg = _config(tmp_path)
    cfg.stt.provider = "faster-whisper"
    vault = tmp_path / "vault"

    # a valid, self-contained 1s mono 16k WAV (B2 — a complete decodable file).
    wav_bytes_path = tmp_path / "sample.wav"
    with wave.open(str(wav_bytes_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    wav_bytes = wav_bytes_path.read_bytes()

    async def _ingest():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            st, _, _ = await _post_chunk(s, base, seq=1, ext="wav", body=wav_bytes, close="true")
            assert st == 200

    asyncio.run(_ingest())
    state = ScribeState(str(tmp_path / "state.json"))
    try:
        asyncio.run(run_sweep(cfg, state, vault))   # real WhisperModel decode (needs a staged model)
    except Exception as e:  # pragma: no cover — no staged model in this env
        pytest.skip(f"no staged whisper model for a real decode: {type(e).__name__}")
    enc_dir = Path(cfg.input_dir) / _LABEL
    eid = compute_encounter_id(_LABEL, salt=_SALT)
    assert load_ledger(ledger_path(enc_dir, eid)) is not None   # decode path completed, ledger written
