"""Tests for the STAY-C box-local bug-report lifecycle (task #4) — capture route + module.

Layers:
  * config — ScribeBugConfig defaults / coercion / dir resolution.
  * module (scribe.bug) — 0640/2750 write, PHI-free auto-context (closed key set), OPAQUE id
    (ts+hex, no summary text), event truncation, list/show/resolve, traversal guard, open cap.
  * route (POST /scribe/bug) — ingest-token gate, enabled toggle (404), body cap (413,
    both Content-Length pre-check and post-read), empty/invalid (400), report_cap (429),
    and PHI-safe logging (summary/detail never logged).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import pytest
import structlog

from alfred.scribe import bug as bug_mod
from alfred.scribe import ingest_web as iw
from alfred.scribe.config import (
    ScribeBugConfig,
    ScribeConfig,
    ScribeIngestWebConfig,
    ScribeLlmConfig,
    ScribeSttConfig,
    load_from_unified,
)
from alfred.scribe.ingest_web import IngestWebServer

_SALT = "DUMMY_SCRIBE_TEST_SALT"
_TOKEN = "tok-" + secrets.token_hex(8)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _config(tmp_path, *, token=_TOKEN, **bug_over):
    return ScribeConfig(
        mode="synthetic",
        input_dir=str(tmp_path / "inbox"),
        stt=ScribeSttConfig(provider="fake"),
        llm=ScribeLlmConfig(base_url="http://127.0.0.1:11434"),
        ingest_web=ScribeIngestWebConfig(enabled=True, host="127.0.0.1", port=_free_port(), token=token),
        bug=ScribeBugConfig(**bug_over),
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


def _auth(token=_TOKEN):
    return {"Authorization": f"Bearer {token}"} if token is not None else {}


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def test_bug_config_defaults():
    c = ScribeBugConfig()
    assert c.enabled is True                        # on by default (rides the ingest server)
    assert c.dir == ""
    assert c.max_body_bytes == 8 * 1024
    assert c.max_per_session == 10
    assert c.max_open_reports == 200


def test_bug_config_coercion_and_absent_block():
    cfg = load_from_unified({"scribe": {"encounter_salt": _SALT, "bug": {
        "enabled": "false", "max_body_bytes": "2048", "dir": "/x/bugs"}}})
    assert cfg.bug.enabled is False and cfg.bug.max_body_bytes == 2048 and cfg.bug.dir == "/x/bugs"
    # absent block → all defaults (enabled True).
    cfg2 = load_from_unified({"scribe": {"encounter_salt": _SALT}})
    assert cfg2.bug.enabled is True
    # a nonsense/zero cap keeps the default (never crashes, never a 0 cap).
    cfg3 = load_from_unified({"scribe": {"encounter_salt": _SALT, "bug": {"max_body_bytes": "nope", "max_open_reports": "0"}}})
    assert cfg3.bug.max_body_bytes == 8 * 1024 and cfg3.bug.max_open_reports == 200


def test_bug_dir_resolution_derives_from_input_dir(tmp_path):
    # dir empty → <input_dir parent>/bugs (per-instance-correct, no single-instance literal).
    cfg = _config(tmp_path)
    assert bug_mod.resolve_bug_dir(cfg) == Path(cfg.input_dir).parent / "bugs"
    # explicit dir wins.
    cfg.bug.dir = str(tmp_path / "custom")
    assert bug_mod.resolve_bug_dir(cfg) == tmp_path / "custom"


# ---------------------------------------------------------------------------
# module — write / posture / triage
# ---------------------------------------------------------------------------

def test_write_report_explicit_modes_and_phi_free_context(tmp_path):
    cfg = _config(tmp_path)
    path, bug_id = bug_mod.write_bug_report(
        cfg, summary="Dead button", detail="clicked create, nothing",
        context={"view": "#/presets", "user": "np_jamie", "clinicians_len": 0,
                 "ua": "UA", "server_state": "empty", "attribution": "unarmed",
                 "secret_field": "SHOULD_BE_DROPPED"},
        events=["click new-preset", "runEnroll !user"])
    # EXPLICIT modes (R3) — NOT umask-dependent. File 0640 (group r, so the shared-group watcher
    # reads bodies in full mode); dir 2750 (setgid + group r-x, NOT group-writable).
    assert oct(os.stat(path).st_mode & 0o777) == "0o640"
    bug_dir = bug_mod.resolve_bug_dir(cfg)
    assert oct(os.stat(bug_dir).st_mode & 0o7777) == "0o2750"     # setgid + group r-x, no group w
    text = path.read_text(encoding="utf-8")
    assert f"id: {bug_id}" in text
    assert "view: #/presets" in text and "user: np_jamie" in text
    assert "runEnroll !user" in text                             # ring buffer attached
    # a key NOT in the closed PHI-free set is DROPPED — the file can only carry the allowlist.
    assert "secret_field" not in text and "SHOULD_BE_DROPPED" not in text


def test_report_id_is_opaque_not_derived_from_summary(tmp_path):
    # R1 — the id must NOT embed the reporter's free-text summary (the old slug leaked it into
    # the locked-mode Telegram ping + the daemon log). It is timestamp + random hex only.
    cfg = _config(tmp_path)
    _, bug_id = bug_mod.write_bug_report(cfg, summary="patient john smith cannot save", detail="d")
    assert "john" not in bug_id.lower() and "smith" not in bug_id.lower() and "patient" not in bug_id.lower()
    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{8}", bug_id), bug_id   # ts + 8 hex


def test_write_report_multiline_summary_cannot_inject_frontmatter(tmp_path):
    # a summary with newlines/colons is single-lined so it can't forge a new frontmatter key.
    cfg = _config(tmp_path)
    path, _ = bug_mod.write_bug_report(cfg, summary="line1\nuser: attacker\nline3", detail="d")
    fm = bug_mod._parse_frontmatter(path.read_text(encoding="utf-8"))
    assert fm.get("user", "") != "attacker"                     # the injected key never lands
    assert "\n" not in fm.get("summary", "")


def test_write_report_truncates_the_event_ring(tmp_path):
    cfg = _config(tmp_path)
    path, _ = bug_mod.write_bug_report(cfg, summary="s", detail="d",
                                       events=[f"event-{i}" for i in range(500)])
    body = path.read_text(encoding="utf-8")
    kept = [ln for ln in body.splitlines() if ln.startswith("- event-")]
    assert len(kept) == bug_mod._MAX_EVENTS                      # bounded — a huge ring can't bloat the file
    assert "- event-499" in body                                # the MOST RECENT events are kept


def test_list_show_resolve_roundtrip(tmp_path):
    cfg = _config(tmp_path)
    _, a = bug_mod.write_bug_report(cfg, summary="first", detail="d")
    _, b = bug_mod.write_bug_report(cfg, summary="second", detail="d")
    rows = bug_mod.list_bugs(cfg)
    assert {r["id"] for r in rows} == {a, b}
    assert all(r["resolved"] is False for r in rows)
    assert bug_mod.read_bug(cfg, a).startswith("---")           # show
    assert bug_mod.resolve_bug(cfg, a) is True                  # resolve → moved
    assert {r["id"] for r in bug_mod.list_bugs(cfg)} == {b}     # a no longer open
    resolved = bug_mod.list_bugs(cfg, include_resolved=True)
    assert any(r["id"] == a and r["resolved"] is True for r in resolved)


def test_resolve_and_read_reject_unknown_and_traversal(tmp_path):
    cfg = _config(tmp_path)
    assert bug_mod.read_bug(cfg, "nope") is None
    assert bug_mod.resolve_bug(cfg, "nope") is False
    for evil in ("../../etc/passwd", "..", "a/b", ".hidden", ""):
        assert bug_mod.read_bug(cfg, evil) is None              # traversal-guarded
        assert bug_mod.resolve_bug(cfg, evil) is False


def test_traversal_guard_blocks_even_when_the_escape_target_exists(tmp_path):
    # R4 — the prior test passed even with BUG_ID_RE DELETED (the escape targets didn't exist,
    # so is_file() saved it). PLANT a real file one level above the bug dir: if the guard were
    # removed, `_report_path`'s `bug_dir / "../escape.md"` would resolve to it. The guard must
    # block "../escape" regardless — read returns None AND resolve returns False (the latter
    # also stops os.replace from moving a file OUT of the bug dir).
    cfg = _config(tmp_path)
    bug_dir = bug_mod.resolve_bug_dir(cfg)
    bug_dir.mkdir(parents=True, exist_ok=True)
    escape = bug_dir.parent / "escape.md"
    escape.write_text("secret outside the bug dir", encoding="utf-8")
    assert (bug_dir / "../escape.md").resolve() == escape.resolve()   # the target really is reachable
    assert bug_mod.read_bug(cfg, "../escape") is None                 # ...but the guard blocks it
    assert bug_mod.resolve_bug(cfg, "../escape") is False
    assert escape.is_file()                                           # never moved/read via the id


def test_open_report_cap_raises(tmp_path):
    cfg = _config(tmp_path, max_open_reports=2)
    bug_mod.write_bug_report(cfg, summary="a", detail="d")
    bug_mod.write_bug_report(cfg, summary="b", detail="d")
    with pytest.raises(bug_mod.BugCapRefused) as exc:
        bug_mod.write_bug_report(cfg, summary="c", detail="d")
    assert exc.value.reason == "report_cap"
    # resolving one frees a slot (the cap counts UNRESOLVED top-level reports).
    open_ids = [r["id"] for r in bug_mod.list_bugs(cfg)]
    bug_mod.resolve_bug(cfg, open_ids[0])
    bug_mod.write_bug_report(cfg, summary="c", detail="d")      # no raise now


# ---------------------------------------------------------------------------
# route — POST /scribe/bug
# ---------------------------------------------------------------------------

async def _post_bug(sess, base, payload, *, token=_TOKEN, raw=None):
    headers = _auth(token)
    if raw is not None:
        async with sess.post(base + iw.BUG_ROUTE, data=raw, headers=headers) as r:
            return r.status, (await r.json() if r.content_type == "application/json" else await r.text())
    async with sess.post(base + iw.BUG_ROUTE, json=payload, headers=headers) as r:
        return r.status, await r.json()


def test_route_requires_ingest_token(tmp_path):
    cfg = _config(tmp_path)

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            no_auth, _ = await _post_bug(s, base, {"summary": "x"}, token=None)
            wrong, _ = await _post_bug(s, base, {"summary": "x"}, token="WRONG")
            ok, body = await _post_bug(s, base, {"summary": "Button dead", "detail": "d"})
            return no_auth, wrong, ok, body

    no_auth, wrong, ok, body = asyncio.run(_go())
    assert no_auth == 401 and wrong == 401                      # ingest-token gated, NOT exempt
    assert ok == 200 and body["bug_id"]
    assert (Path(cfg.input_dir).parent / "bugs" / f"{body['bug_id']}.md").is_file()


def test_route_writes_context_and_events(tmp_path):
    cfg = _config(tmp_path)

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            _, body = await _post_bug(s, base, {
                "summary": "dead button", "detail": "tapped create, nothing happened",
                "context": {"view": "#/presets", "user": "np_jamie", "clinicians_len": 0},
                "events": ["click new-preset", "runEnroll returned at !user"],
            })
            return body

    body = asyncio.run(_go())
    text = (Path(cfg.input_dir).parent / "bugs" / f"{body['bug_id']}.md").read_text()
    assert "view: #/presets" in text and "user: np_jamie" in text
    assert "runEnroll returned at !user" in text


def test_route_inert_when_disabled(tmp_path):
    cfg = _config(tmp_path, enabled=False)

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            return (await _post_bug(s, base, {"summary": "x", "detail": "d"}))[0]

    assert asyncio.run(_go()) == 404                            # bug_inert — route off, not just unauth


def test_route_body_cap_content_length_prong(tmp_path):
    cfg = _config(tmp_path, max_body_bytes=200)

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            # honest oversized POST → refused at the Content-Length pre-check.
            return await _post_bug(s, base, {"summary": "s", "detail": "z" * 500})

    st, body = asyncio.run(_go())
    assert st == 413 and body["error"] == "bug_too_large"


def test_route_body_cap_post_read_prong_chunked(tmp_path):
    # R5 — a Transfer-Encoding: chunked POST carries NO Content-Length, so the pre-check is
    # SKIPPED; only the post-read `len(raw) > max_body_bytes` backstop catches it. Binds that
    # prong (deleting it lets a chunked client write up to client_max_size = 25 MiB per report).
    cfg = _config(tmp_path, max_body_bytes=200)

    async def _chunks():
        for _ in range(10):
            yield b"z" * 100                     # 1000 bytes total, no Content-Length → chunked

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            async with s.post(base + iw.BUG_ROUTE, data=_chunks(), headers=_auth()) as r:
                return r.status, await r.json()

    st, body = asyncio.run(_go())
    assert st == 413 and body["error"] == "bug_too_large"


def test_route_empty_and_invalid_json_and_nonstring(tmp_path):
    cfg = _config(tmp_path)

    async def _go():
        out = {}
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            out["empty"] = await _post_bug(s, base, {"summary": "   ", "detail": ""})
            out["badjson"] = await _post_bug(s, base, None, raw=b"not json at all")
            out["notdict"] = await _post_bug(s, base, None, raw=b"[1,2,3]")
            # R6 — a truthy NON-str summary must be an opaque 400, NOT an aiohttp 500 (the old
            # _slug(non-str) AttributeError). Same for detail.
            out["nonstr_summary"] = await _post_bug(s, base, {"summary": ["a", "list"], "detail": "d"})
            out["nonstr_detail"] = await _post_bug(s, base, {"summary": "s", "detail": {"x": 1}})
        return out

    out = asyncio.run(_go())
    assert out["empty"] == (400, {"error": "empty_report"})     # ILB — empty refused visibly
    assert out["badjson"][0] == 400 and out["badjson"][1]["error"] == "invalid_json"
    assert out["notdict"][0] == 400 and out["notdict"][1]["error"] == "invalid_json"
    assert out["nonstr_summary"] == (400, {"error": "invalid_payload"})
    assert out["nonstr_detail"] == (400, {"error": "invalid_payload"})


def test_route_report_cap_429(tmp_path):
    cfg = _config(tmp_path, max_open_reports=1)

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            first, _ = await _post_bug(s, base, {"summary": "a", "detail": "d"})
            second, body = await _post_bug(s, base, {"summary": "b", "detail": "d"})
            return first, second, body

    first, second, body = asyncio.run(_go())
    assert first == 200 and second == 429 and body["error"] == "report_cap"


def test_route_never_logs_phi_including_derived_forms(tmp_path):
    # R1/R13 — the SUMMARY may carry PHI despite the page caution. It must NEVER appear in a log
    # line, NOR a DERIVED form of it: the old slug-in-id put a lowercased/dashed transform of the
    # summary into the bug_id (logged), so an exact-case assertion passed falsely. Plant PHI in
    # the SUMMARY and assert neither it NOR its lowercase/slug form is in any log (the opaque id
    # makes this true).
    cfg = _config(tmp_path)
    phi = "Jane-Patient-DOB-1970"
    phi_slug = "jane-patient-dob-1970"                              # the old leak form

    async def _go():
        with structlog.testing.capture_logs() as caps:
            async with _serve(cfg) as base, aiohttp.ClientSession() as s:
                await _post_bug(s, base, {"summary": phi, "detail": "more detail"})
        return caps

    caps = asyncio.run(_go())
    assert any(c.get("event") == "scribe.bug.written" for c in caps)   # ran, signalled
    for c in caps:
        dump = json.dumps(c, default=str).lower()
        assert phi.lower() not in dump                                # case-insensitive
        assert phi_slug not in dump                                   # ...and the slug form
