"""Inter-project message bus (V1) — unit + integration tests (B0–B3).

Covers: mint stability, record round-trip + validate, state schema
tolerance, routing (correct-inbox placement, dedup re-drop skip,
unknown-to → undeliverable, bad-kind/bad-parse → malformed, mint-if-absent,
torn-placement idempotent re-place, ILB tick at zero), drain (move to read/
+ stamp read_at, count_unread excludes read/), the brief section ILB, the
`alfred msg send` spool drop, and the optional operator ping gating.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import structlog

from alfred.msgbus import router as router_mod
from alfred.msgbus.config import MessageBusConfig, load_message_bus_config
from alfred.msgbus.inbox import (
    count_unread,
    drain_inbox,
    list_inbox,
    read_message,
)
from alfred.msgbus.inbox_section import render_inbound_messages_section
from alfred.msgbus.record import (
    MESSAGE_KINDS,
    MessageRecord,
    message_filename,
    parse_message_file,
    validate_record,
    write_message_file,
)
from alfred.msgbus.registry import ProjectEntry, ProjectRegistry, load_registry
from alfred.msgbus.router import (
    malformed_counts_by_project,
    mint_message_id,
    run_route_once,
    scan_spool,
)
from alfred.msgbus.state import MessageBusEntry, MessageBusState


FIXED_CREATED = "2026-06-30T12:00:00+00:00"


def _log_events(captured, event):
    return [c for c in captured if c.get("event") == event]


def _config(tmp_path: Path, **overrides) -> MessageBusConfig:
    spool = tmp_path / "spool"
    spool.mkdir(exist_ok=True)
    projects = [
        ProjectEntry("alfred", str(tmp_path / "alfred" / ".msgbus" / "inbox")),
        ProjectEntry("aftermath-lab", str(tmp_path / "lab" / ".msgbus" / "inbox")),
    ]
    kwargs = dict(
        enabled=True,
        self_project="aftermath-lab",
        spool_path=str(spool),
        state_path=str(tmp_path / "message_bus_state.json"),
        projects=projects,
    )
    kwargs.update(overrides)
    return MessageBusConfig(**kwargs)


def _record(**overrides) -> MessageRecord:
    fields = dict(
        from_project="alfred",
        to_project="aftermath-lab",
        kind="handover",
        correlation_id="cnv-1",
        created=FIXED_CREATED,
        subject="hello",
        body="the body",
    )
    fields.update(overrides)
    return MessageRecord(**fields)


def _drop(spool: Path, record: MessageRecord) -> Path:
    """Write a record into the spool; returns the spool path."""
    if not record.id:
        # tests that exercise mint-if-absent leave id empty; give the file
        # a unique name so multiple no-id drops don't collide.
        name = f"{record.created}-{record.from_project}-{record.subject}.md".replace(
            ":", ""
        ).replace(" ", "_")
        path = spool / name
    else:
        path = spool / message_filename(record)
    write_message_file(path, record)
    return path


# ---------------------------------------------------------------------------
# B0 — mint / record / registry / state
# ---------------------------------------------------------------------------


def test_mint_message_id_stable_and_format():
    a = mint_message_id("alfred", "aftermath-lab", FIXED_CREATED, "hello", "body")
    b = mint_message_id("alfred", "aftermath-lab", FIXED_CREATED, "hello", "body")
    assert a == b                              # deterministic (incl body)
    assert a.startswith("msg-20260630-")       # date from created
    assert len(a.split("-")[-1]) == 8          # sha8
    # any input change → different id
    assert mint_message_id("alfred", "aftermath-lab", FIXED_CREATED, "x", "body") != a


def test_mint_message_id_body_distinguishes():
    """Re-pin (QA-B): distinct body → distinct id (no silent fake-re-drop
    loss); identical content incl body → identical id (a true re-drop)."""
    base = ("alfred", "lab", FIXED_CREATED, "subj")
    assert mint_message_id(*base, "body-A") != mint_message_id(*base, "body-B")
    assert mint_message_id(*base, "same") == mint_message_id(*base, "same")


def test_mint_message_id_date_fallback_to_today():
    from datetime import date
    mid = mint_message_id("a", "b", "no-date-here", "s")
    assert mid.startswith(f"msg-{date.today().strftime('%Y%m%d')}-")


def test_record_roundtrip(tmp_path):
    rec = _record(id="msg-1", reply_to="msg-0", precedence="P")
    path = tmp_path / "m.md"
    write_message_file(path, rec)
    loaded = parse_message_file(path)
    assert loaded.id == "msg-1"
    assert loaded.from_project == "alfred"
    assert loaded.to_project == "aftermath-lab"
    assert loaded.kind == "handover"
    assert loaded.correlation_id == "cnv-1"
    assert loaded.subject == "hello"
    assert loaded.reply_to == "msg-0"
    assert loaded.precedence == "P"
    assert loaded.body.strip() == "the body"


def test_validate_rejects_missing_field():
    errors = validate_record(_record(id="x", subject=""))
    assert any("subject" in e for e in errors)


def test_validate_rejects_bad_kind():
    errors = validate_record(_record(id="x", kind="bogus"))
    assert any("invalid kind" in e for e in errors)


def test_validate_clean_record_passes():
    assert validate_record(_record(id="x")) == []


def test_validate_unknown_destination_with_registry():
    registry = ProjectRegistry([ProjectEntry("alfred", "/x")])
    errors = validate_record(_record(id="x", to_project="nope"), registry)
    assert any("unknown destination" in e for e in errors)


def test_message_kinds_frozen():
    assert MESSAGE_KINDS == frozenset({"handover", "request", "fyi", "reply"})


def test_load_registry_and_lookup():
    reg = load_registry([
        {"name": "alfred", "inbox_path": "/a/inbox"},
        {"name": "lab", "inbox_path": "/b/inbox"},
        {"name": "", "inbox_path": "/skip"},          # missing name → skipped
        {"inbox_path": "/skip2"},                      # no name → skipped
    ])
    assert reg.names() == ["alfred", "lab"]
    assert reg.inbox_for("alfred") == Path("/a/inbox")
    assert reg.read_dir_for("lab") == Path("/b/inbox/read")
    assert reg.get("missing") is None


def test_state_schema_tolerance():
    entry = MessageBusEntry.from_dict(
        {"id": "msg-1", "to_project": "lab", "future_field": "x"},
    )
    assert entry.id == "msg-1"
    assert entry.to_project == "lab"


def test_state_atomic_roundtrip(tmp_path):
    p = tmp_path / "s.json"
    state = MessageBusState(path=p)
    state.entries["msg-1"] = MessageBusEntry(id="msg-1", to_project="lab")
    state.save()
    reloaded = MessageBusState.load(p)
    assert reloaded.entries["msg-1"].to_project == "lab"


def test_state_corrupt_starts_empty(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{ not json")
    assert MessageBusState.load(p).entries == {}


def test_config_inert_default():
    cfg = load_message_bus_config({})
    assert cfg.enabled is False
    assert cfg.projects == []


def test_config_loads_block(tmp_path):
    raw = {"message_bus": {
        "enabled": True,
        "self_project": "lab",
        "interval_minutes": 7,
        "spool_path": "/x/spool",
        "state": {"path": "/x/state.json"},
        "notify": {"telegram": True},
        "projects": [{"name": "alfred", "inbox_path": "/a/inbox"}],
    }}
    cfg = load_message_bus_config(raw)
    assert cfg.enabled is True
    assert cfg.self_project == "lab"
    assert cfg.interval_minutes == 7
    assert cfg.spool_path == "/x/spool"
    assert cfg.state_path == "/x/state.json"
    assert cfg.notify_telegram is True
    assert cfg.registry().names() == ["alfred"]


# ---------------------------------------------------------------------------
# B2 — routing
# ---------------------------------------------------------------------------


async def test_route_places_into_correct_inbox(tmp_path):
    cfg = _config(tmp_path)
    spool = Path(cfg.spool_path)
    rec = _record(id="msg-aaa")
    _drop(spool, rec)

    with structlog.testing.capture_logs() as captured:
        result = await run_route_once(cfg, {})

    assert result["routed"] == 1
    assert result["by_destination"] == {"aftermath-lab": 1}
    dest_inbox = cfg.registry().inbox_for("aftermath-lab")
    placed = list(dest_inbox.glob("*.md"))
    assert len(placed) == 1
    placed_rec = parse_message_file(placed[0])
    assert placed_rec.id == "msg-aaa"
    assert placed_rec.routed_by == "kalle"
    assert placed_rec.routed_at  # stamped
    # spool file archived to routed/, none left pending
    assert list(spool.glob("*.md")) == []
    assert len(list((spool / "routed").glob("*.md"))) == 1
    # state recorded; ILB tick fired
    state = MessageBusState.load(cfg.state_path)
    assert "msg-aaa" in state.entries
    ticks = _log_events(captured, "msgbus.route.tick")
    assert len(ticks) == 1
    assert ticks[0]["routed"] == 1


async def test_route_redrop_of_routed_id_skipped(tmp_path):
    cfg = _config(tmp_path)
    spool = Path(cfg.spool_path)
    rec = _record(id="msg-dup")
    _drop(spool, rec)
    await run_route_once(cfg, {})

    # re-drop the SAME id → second tick skips it (no duplicate placement)
    _drop(spool, rec)
    result = await run_route_once(cfg, {})
    assert result["routed"] == 0
    assert result["skipped_dup"] == 1
    dest_inbox = cfg.registry().inbox_for("aftermath-lab")
    assert len(list(dest_inbox.glob("*.md"))) == 1   # still exactly one


async def test_route_unknown_destination_undeliverable(tmp_path):
    cfg = _config(tmp_path)
    spool = Path(cfg.spool_path)
    _drop(spool, _record(id="msg-x", to_project="ghost"))
    result = await run_route_once(cfg, {})
    assert result["routed"] == 0
    assert result["undeliverable"] == 1
    assert len(list((spool / "undeliverable").glob("*.md"))) == 1


async def test_route_unknown_kind_tolerated_as_fyi(tmp_path):
    # TOLERANT+TAG (task #9): an unknown ``kind`` is enum DRIFT between projects, not a
    # broken message — accepted as ``fyi`` + tagged with the original kind, NOT binned.
    # (The 2026-07-16 incident: rrts sent kind=propose and it was silently quarantined.)
    cfg = _config(tmp_path)
    spool = Path(cfg.spool_path)
    _drop(spool, _record(id="msg-x", kind="bogus"))
    with structlog.testing.capture_logs() as captured:
        result = await run_route_once(cfg, {})
    assert result["malformed"] == 0 and result["kind_tolerated"] == 1
    assert result["routed"] == 1
    tol = _log_events(captured, "msgbus.route.unknown_kind_tolerated")
    assert len(tol) == 1 and tol[0]["original_kind"] == "bogus"
    assert len(list((spool / "malformed").glob("*.md"))) == 0
    # delivered to the receiver inbox as fyi, with the original kind tagged.
    placed = parse_message_file(
        list(cfg.registry().inbox_for("aftermath-lab").glob("*.md"))[0])
    assert placed.kind == "fyi" and placed.original_kind == "bogus"


async def test_route_bad_parse_malformed(tmp_path):
    cfg = _config(tmp_path)
    spool = Path(cfg.spool_path)
    # A file that frontmatter can read but is missing every required field
    # (an empty body, no frontmatter) → structural malformed.
    (spool / "garbage.md").write_text("just text, no frontmatter\n")
    result = await run_route_once(cfg, {})
    assert result["malformed"] == 1
    assert len(list((spool / "malformed").glob("*.md"))) == 1


async def test_route_mint_if_absent(tmp_path):
    cfg = _config(tmp_path)
    spool = Path(cfg.spool_path)
    _drop(spool, _record(id=""))   # NO id — router mints
    result = await run_route_once(cfg, {})
    assert result["routed"] == 1
    dest_inbox = cfg.registry().inbox_for("aftermath-lab")
    placed = parse_message_file(list(dest_inbox.glob("*.md"))[0])
    assert placed.id.startswith("msg-")


async def test_torn_placement_idempotent_replace(tmp_path):
    """Placed but state-save lost (torn): re-routing the same id re-places
    to the SAME id-keyed filename — overwrites, never duplicates."""
    cfg = _config(tmp_path)
    spool = Path(cfg.spool_path)
    rec = _record(id="msg-torn")
    _drop(spool, rec)
    await run_route_once(cfg, {})
    dest_inbox = cfg.registry().inbox_for("aftermath-lab")
    assert len(list(dest_inbox.glob("*.md"))) == 1

    # Simulate torn state: drop state (id no longer known) + re-drop the
    # same record into the spool. Re-route must NOT create a 2nd inbox file.
    Path(cfg.state_path).unlink()
    _drop(spool, rec)
    result = await run_route_once(cfg, {})
    assert result["routed"] == 1
    assert len(list(dest_inbox.glob("*.md"))) == 1   # overwritten, not doubled


async def test_route_ilb_tick_at_zero_work(tmp_path):
    cfg = _config(tmp_path)
    with structlog.testing.capture_logs() as captured:
        result = await run_route_once(cfg, {})
    assert result["scanned"] == 0
    ticks = _log_events(captured, "msgbus.route.tick")
    assert len(ticks) == 1
    for field in ("scanned", "routed", "skipped_dup", "malformed",
                  "undeliverable", "failed"):
        assert ticks[0][field] == 0


async def test_route_per_item_isolation(tmp_path):
    """One malformed file never blocks a valid one in the same tick."""
    cfg = _config(tmp_path)
    spool = Path(cfg.spool_path)
    (spool / "00-garbage.md").write_text("no frontmatter\n")
    _drop(spool, _record(id="msg-good"))
    result = await run_route_once(cfg, {})
    assert result["routed"] == 1
    assert result["malformed"] == 1


def test_scan_spool_no_dir_logs(tmp_path):
    cfg = _config(tmp_path, spool_path=str(tmp_path / "nope"))
    state = MessageBusState(path=tmp_path / "s.json")
    with structlog.testing.capture_logs() as captured:
        res = scan_spool(cfg.spool_path, cfg.registry(), state)
    assert res.scanned == 0
    assert len(_log_events(captured, "msgbus.route.no_spool_dir")) == 1


# ---------------------------------------------------------------------------
# QA regression pins — A (post-mint dedup), B (body→id), C (read-dir + drain
# unlink rollback), E (scan-failure ILB), F (un-movable quarantine)
# ---------------------------------------------------------------------------


async def test_A_idless_redrop_after_drain_not_duplicated(tmp_path):
    """QA-A: an id-less message routed then drained, then re-dropped (same
    content → same minted id) must NOT be re-delivered. Pre-fix the minted
    id bypassed scan_spool's pre-mint gate → duplicate in the inbox."""
    cfg = _config(tmp_path)
    spool = Path(cfg.spool_path)
    _drop(spool, _record(id=""))            # id-less
    await run_route_once(cfg, {})
    inbox = cfg.registry().inbox_for("aftermath-lab")
    drain_inbox(inbox, mark_read=True)      # consumer reads it
    assert count_unread(inbox) == 0

    _drop(spool, _record(id=""))            # SAME content re-dropped
    result = await run_route_once(cfg, {})
    assert result["routed"] == 0            # post-mint dedup caught it
    assert result["skipped_dup"] == 1
    assert count_unread(inbox) == 0         # NOT re-delivered


async def test_A_idless_two_identical_in_one_tick_dedup(tmp_path):
    """QA-A: two id-less identical messages in ONE tick mint the same id;
    only one is placed (the 2nd is post-mint-deduped, not a silent
    overwrite-loss)."""
    cfg = _config(tmp_path)
    spool = Path(cfg.spool_path)
    # two separate spool files, identical content → identical minted id
    _drop(spool, _record(id="", subject="dup"))
    rec2 = _record(id="", subject="dup")
    (spool / "second-copy.md").write_text(
        (spool / list(spool.glob("*.md"))[0].name).read_text()
    )
    result = await run_route_once(cfg, {})
    inbox = cfg.registry().inbox_for("aftermath-lab")
    assert result["routed"] == 1            # exactly one placed
    assert result["skipped_dup"] == 1       # the identical twin deduped
    assert len(list(inbox.glob("*.md"))) == 1


async def test_B_distinct_body_routes_both(tmp_path):
    """QA-B: two messages identical EXCEPT body get distinct ids → both
    route (no silent fake-re-drop loss). Pre-fix (no body in hash) the 2nd
    collided + was lost."""
    cfg = _config(tmp_path)
    spool = Path(cfg.spool_path)
    from alfred.msgbus.router import mint_message_id
    for body in ("alpha", "beta"):
        rec = _record(body=body)
        rec.id = mint_message_id(
            rec.from_project, rec.to_project, rec.created, rec.subject, body,
        )
        _drop(spool, rec)
    result = await run_route_once(cfg, {})
    inbox = cfg.registry().inbox_for("aftermath-lab")
    assert result["routed"] == 2            # BOTH distinct messages delivered
    assert len(list(inbox.glob("*.md"))) == 2


async def test_C_crash_window_redelivery_blocked_by_read_dir(tmp_path):
    """QA-C: a place→state-save crash + a destination drain, with state then
    lost, must NOT re-deliver an already-read message. The consumer-side
    read/ dedup catches it even when state can't."""
    cfg = _config(tmp_path)
    spool = Path(cfg.spool_path)
    rec = _record(id="msg-crash")
    _drop(spool, rec)
    await run_route_once(cfg, {})
    inbox = cfg.registry().inbox_for("aftermath-lab")
    drain_inbox(inbox, mark_read=True)      # consumer reads → moved to read/
    assert count_unread(inbox) == 0

    # Simulate the crash-window: state is LOST (so the pre/post-mint state
    # gates can't help), and the same message is re-dropped.
    Path(cfg.state_path).unlink()
    _drop(spool, rec)
    result = await run_route_once(cfg, {})
    assert result["routed"] == 0            # read-dir dedup blocked it
    assert count_unread(inbox) == 0         # NOT re-delivered
    assert len(list((inbox / "read").glob("*.md"))) == 1


def test_C_drain_unlink_failure_rolls_back_read_copy(tmp_path, monkeypatch):
    """QA-C: if the post-write unlink fails, the message must NOT end up in
    BOTH inbox/ and read/ — the read/ copy is rolled back so it stays only
    in inbox/ (re-drainable), not counted as drained."""
    import alfred.msgbus.inbox as inbox_mod

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    write_message_file(inbox / message_filename(_record(id="m1")), _record(id="m1"))

    real_unlink = Path.unlink

    def _boom_unlink(self, *a, **k):
        if self.parent == inbox and self.suffix == ".md":
            raise OSError("cannot unlink")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", _boom_unlink)
    drained = inbox_mod.drain_inbox(inbox, mark_read=True)
    assert drained == []                       # not counted as drained
    # message remains ONLY in inbox/ (read/ copy rolled back)
    assert count_unread(inbox) == 1
    assert not list((inbox / "read").glob("*.md"))


async def test_E_scan_failure_still_emits_tick(tmp_path, monkeypatch):
    """QA-E: a scan-level failure still emits the ILB tick (scan_error)."""
    cfg = _config(tmp_path)

    def _boom(*a, **k):
        raise OSError("spool unreadable")

    monkeypatch.setattr(router_mod, "scan_spool", _boom)
    with structlog.testing.capture_logs() as captured:
        result = await run_route_once(cfg, {})
    assert result["scan_error"] is True
    assert result["scanned"] == 0
    ticks = _log_events(captured, "msgbus.route.tick")
    assert len(ticks) == 1
    assert ticks[0]["scan_error"] is True


async def test_F_unmovable_quarantine_not_recounted(tmp_path, monkeypatch):
    """QA-F: a quarantine file that can't be MOVED is renamed out of the
    glob (.qfail) so it isn't re-parsed + re-counted every tick."""
    cfg = _config(tmp_path)
    spool = Path(cfg.spool_path)
    (spool / "garbage.md").write_text("no frontmatter\n")

    def _boom_move(*a, **k):
        raise OSError("cannot move")

    monkeypatch.setattr(router_mod.shutil, "move", _boom_move)
    r1 = await run_route_once(cfg, {})
    assert r1["malformed"] == 1
    assert list(spool.glob("*.qfail"))          # renamed out of the glob
    assert not list(spool.glob("*.md"))         # no .md left to re-count
    # second tick: the .qfail file is invisible → NOT re-counted
    r2 = await run_route_once(cfg, {})
    assert r2["malformed"] == 0


# ---------------------------------------------------------------------------
# Operator notification (optional, gated)
# ---------------------------------------------------------------------------


async def test_notify_fires_when_enabled(tmp_path, monkeypatch):
    cfg = _config(tmp_path, notify_telegram=True)
    _drop(Path(cfg.spool_path), _record(id="msg-n"))
    sent = []
    monkeypatch.setattr(
        router_mod, "_telegram_send",
        lambda token, chat_id, text: sent.append((token, chat_id, text)),
    )
    raw = {"telegram": {"bot_token": "DUMMY_TG_TOKEN", "allowed_users": [123]}}
    await run_route_once(cfg, raw)
    assert len(sent) == 1
    assert "aftermath-lab" in sent[0][2]


async def test_notify_silent_when_disabled(tmp_path, monkeypatch):
    cfg = _config(tmp_path, notify_telegram=False)   # gate off
    _drop(Path(cfg.spool_path), _record(id="msg-n"))
    sent = []
    monkeypatch.setattr(
        router_mod, "_telegram_send",
        lambda *a, **k: sent.append(a),
    )
    await run_route_once(cfg, {"telegram": {"bot_token": "t", "allowed_users": [1]}})
    assert sent == []


async def test_H_notify_failure_does_not_log_token(tmp_path, monkeypatch):
    """QA-H: a notify failure logs error_type ONLY — never str(exc), which
    on an httpx error can embed the /bot<token>/ URL."""
    cfg = _config(tmp_path, notify_telegram=True)
    _drop(Path(cfg.spool_path), _record(id="msg-n"))

    def _boom(token, chat_id, text):
        raise RuntimeError(f"connect to https://api.telegram.org/bot{token}/sendMessage failed")

    monkeypatch.setattr(router_mod, "_telegram_send", _boom)
    raw = {"telegram": {"bot_token": "SECRET_TG_TOKEN", "allowed_users": [123]}}
    with structlog.testing.capture_logs() as captured:
        await run_route_once(cfg, raw)
    fails = _log_events(captured, "msgbus.route.notify_failed")
    assert len(fails) == 1
    assert "error" not in fails[0]                # str(exc) NOT logged
    assert fails[0]["error_type"] == "RuntimeError"
    assert "SECRET_TG_TOKEN" not in json.dumps(fails[0])


# ---------------------------------------------------------------------------
# B3 — drain + section
# ---------------------------------------------------------------------------


async def test_drain_moves_to_read_and_stamps(tmp_path):
    cfg = _config(tmp_path)
    _drop(Path(cfg.spool_path), _record(id="msg-d"))
    await run_route_once(cfg, {})
    inbox = cfg.registry().inbox_for("aftermath-lab")
    assert count_unread(inbox) == 1

    drained = drain_inbox(inbox, mark_read=True)
    assert len(drained) == 1
    assert drained[0].read_at  # stamped
    assert count_unread(inbox) == 0
    # the message now lives in read/
    assert len(list((inbox / "read").glob("*.md"))) == 1


def test_count_unread_excludes_read(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "read").mkdir()
    write_message_file(inbox / message_filename(_record(id="m1")), _record(id="m1"))
    write_message_file(inbox / message_filename(_record(id="m2", subject="s2")),
                       _record(id="m2", subject="s2"))
    write_message_file((inbox / "read") / "old.md", _record(id="m0"))
    assert count_unread(inbox) == 2   # read/ excluded


def test_list_and_read_message(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    write_message_file(inbox / message_filename(_record(id="m1")), _record(id="m1"))
    records = list_inbox(inbox)
    assert [r.id for r in records] == ["m1"]
    assert read_message(inbox, "m1").subject == "hello"
    assert read_message(inbox, "nope") is None


def test_drain_dry_run_does_not_move(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    write_message_file(inbox / message_filename(_record(id="m1")), _record(id="m1"))
    drained = drain_inbox(inbox, mark_read=False)
    assert len(drained) == 1
    assert count_unread(inbox) == 1   # NOT moved


def test_section_ilb_at_zero(tmp_path):
    cfg = _config(tmp_path)
    # create inbox dirs but no messages
    for name in cfg.registry().names():
        cfg.registry().inbox_for(name).mkdir(parents=True, exist_ok=True)
    section = render_inbound_messages_section(
        cfg.registry(), expected_projects=cfg.registry().names(),
    )
    assert "No inbound project messages." in section
    assert section.startswith("## Inbound Project Messages")


def test_section_lists_subjects(tmp_path):
    cfg = _config(tmp_path)
    inbox = cfg.registry().inbox_for("aftermath-lab")
    inbox.mkdir(parents=True, exist_ok=True)
    write_message_file(
        inbox / message_filename(_record(id="m1", subject="Ship the bus")),
        _record(id="m1", subject="Ship the bus"),
    )
    section = render_inbound_messages_section(
        cfg.registry(), expected_projects=cfg.registry().names(),
    )
    assert "aftermath-lab (1)" in section
    assert "- Ship the bus" in section


def test_section_empty_registry_returns_empty():
    section = render_inbound_messages_section(
        ProjectRegistry([]), expected_projects=[],
    )
    assert section == ""


# ---------------------------------------------------------------------------
# B1 — alfred msg send (CLI helper level)
# ---------------------------------------------------------------------------


def test_msg_send_writes_parseable_spool_file(tmp_path):
    from alfred.cli import _msg_send

    cfg = _config(tmp_path)
    args = SimpleNamespace(
        to="aftermath-lab", kind="handover", subject="Handover X",
        from_project="alfred", correlation_id="", reply_to="",
        precedence="R", body_file="", body="some body",
    )
    _msg_send(args, cfg, {})
    spool_files = list(Path(cfg.spool_path).glob("*.md"))
    assert len(spool_files) == 1
    rec = parse_message_file(spool_files[0])
    assert rec.from_project == "alfred"
    assert rec.to_project == "aftermath-lab"
    assert rec.kind == "handover"
    assert rec.subject == "Handover X"
    assert rec.id.startswith("msg-")
    assert rec.correlation_id.startswith("cnv-")   # minted fresh
    assert rec.body.strip() == "some body"


def test_msg_send_reply_echoes_correlation_and_reply_to(tmp_path):
    from alfred.cli import _msg_send

    cfg = _config(tmp_path)
    args = SimpleNamespace(
        to="alfred", kind="reply", subject="Re: X",
        from_project="aftermath-lab", correlation_id="cnv-thread-1",
        reply_to="msg-parent", precedence="R", body_file="", body="ack",
    )
    _msg_send(args, cfg, {})
    rec = parse_message_file(list(Path(cfg.spool_path).glob("*.md"))[0])
    assert rec.correlation_id == "cnv-thread-1"
    assert rec.reply_to == "msg-parent"
    assert rec.kind == "reply"


async def test_send_then_route_end_to_end(tmp_path):
    from alfred.cli import _msg_send

    cfg = _config(tmp_path)
    args = SimpleNamespace(
        to="aftermath-lab", kind="fyi", subject="E2E",
        from_project="alfred", correlation_id="", reply_to="",
        precedence="R", body_file="", body="hi",
    )
    _msg_send(args, cfg, {})
    result = await run_route_once(cfg, {})
    assert result["routed"] == 1
    inbox = cfg.registry().inbox_for("aftermath-lab")
    placed = list_inbox(inbox)
    assert len(placed) == 1
    assert placed[0].subject == "E2E"


def test_G_inbox_no_arg_falls_back_to_self_project(tmp_path, capsys):
    """QA-G: `alfred msg inbox list` (no project) resolves to
    message_bus.self_project — the parser default '' + the _msg_inbox
    fallback honor the documented behavior."""
    from alfred.cli import _msg_inbox

    cfg = _config(tmp_path, self_project="aftermath-lab")
    inbox = cfg.registry().inbox_for("aftermath-lab")
    inbox.mkdir(parents=True, exist_ok=True)
    write_message_file(inbox / message_filename(_record(id="m1")), _record(id="m1"))

    # project="" (the parser default for the no-arg form)
    args = SimpleNamespace(project="", inbox_action="list", message_id="")
    _msg_inbox(args, cfg)   # must NOT sys.exit — resolves to self_project
    out = capsys.readouterr().out
    assert "m1" in out
    assert "unread: 1" in out


def test_G_inbox_parser_default_empty_project():
    """The parser leaves project='' for `inbox list` so the fallback fires."""
    from alfred.cli import build_parser
    ns = build_parser().parse_args(["msg", "inbox", "list"])
    assert ns.project == ""
    assert ns.inbox_action == "list"
    ns2 = build_parser().parse_args(["msg", "inbox", "alfred", "drain"])
    assert ns2.project == "alfred"
    assert ns2.inbox_action == "drain"


# ---------------------------------------------------------------------------
# Orchestrator registration
# ---------------------------------------------------------------------------


class TestOrchestratorRegistration:
    def test_runner_registered(self):
        import alfred.orchestrator as orch
        assert "message_bus" in orch.TOOL_RUNNERS

    def test_in_spawn_priority(self):
        import alfred.orchestrator as orch
        assert "message_bus" in orch.SPAWN_PRIORITY

    def test_two_arg_signature(self):
        import inspect
        import alfred.orchestrator as orch
        params = list(inspect.signature(orch.TOOL_RUNNERS["message_bus"]).parameters)
        assert params == ["raw", "suppress_stdout"]


# ---------------------------------------------------------------------------
# Task #9 — malformed BOUNCE + receiver-side bin signal
# ---------------------------------------------------------------------------

async def test_route_malformed_bounces_to_sender(tmp_path):
    # A structurally-malformed message (here: missing subject) is binned AND a reply-kind
    # BOUNCE lands in the SENDER's inbox — the bin is no longer silent.
    cfg = _config(tmp_path)
    spool = Path(cfg.spool_path)
    _drop(spool, _record(id="msg-y", subject=""))   # missing subject -> structural malformed
    with structlog.testing.capture_logs() as captured:
        result = await run_route_once(cfg, {})
    assert result["malformed"] == 1 and result["bounced"] == 1
    bounced_ev = _log_events(captured, "msgbus.route.bounced")
    assert len(bounced_ev) == 1 and bounced_ev[0]["to"] == "alfred"
    bounces = list(cfg.registry().inbox_for("alfred").glob("*.md"))
    assert len(bounces) == 1
    b = parse_message_file(bounces[0])
    assert b.kind == "reply"
    assert b.subject.startswith("BOUNCED malformed:")
    assert b.to_project == "alfred" and b.reply_to == "msg-y"
    assert "missing subject" in b.body and "malformed/" in b.body


async def test_route_malformed_no_sender_no_bounce(tmp_path):
    # A malformed message with NO ``from`` cannot be bounced (no sender) — binned only.
    cfg = _config(tmp_path)
    spool = Path(cfg.spool_path)
    _drop(spool, _record(id="msg-z", from_project="", subject=""))
    result = await run_route_once(cfg, {})
    assert result["malformed"] == 1 and result["bounced"] == 0
    for name in cfg.registry().names():
        assert len(list(cfg.registry().inbox_for(name).glob("*.md"))) == 0


async def test_route_malformed_unregistered_sender_no_bounce(tmp_path):
    # A malformed message whose ``from`` is not a registered project cannot be bounced
    # (no inbox) — binned only, no crash.
    cfg = _config(tmp_path)
    spool = Path(cfg.spool_path)
    _drop(spool, _record(id="msg-w", from_project="ghost-project", subject=""))
    result = await run_route_once(cfg, {})
    assert result["malformed"] == 1 and result["bounced"] == 0


def test_malformed_counts_by_project(tmp_path):
    cfg = _config(tmp_path)
    spool = Path(cfg.spool_path)
    mdir = spool / "malformed"
    mdir.mkdir()
    write_message_file(mdir / "a1.md", _record(id="m1", to_project="alfred", subject=""))
    write_message_file(mdir / "a2.md", _record(id="m2", to_project="alfred", subject=""))
    write_message_file(mdir / "b1.md", _record(id="m3", to_project="aftermath-lab"))
    (mdir / "junk.md").write_text("no frontmatter, no destination\n")
    counts = malformed_counts_by_project(spool)
    assert counts.get("alfred") == 2
    assert counts.get("aftermath-lab") == 1
    assert counts.get("?") == 1   # unparseable / no-``to`` bucket


def test_malformed_counts_empty_when_no_bin(tmp_path):
    # Intentionally-left-blank: no malformed dir -> empty dict, never raises.
    cfg = _config(tmp_path)
    assert malformed_counts_by_project(cfg.spool_path) == {}


def test_msg_status_surfaces_malformed_bin(tmp_path, capsys):
    # RECEIVER SIGNAL: `msg status` must show the per-project malformed-bin count so a
    # routine drain can't miss a quarantined message addressed to it.
    import argparse
    from alfred.cli import _msg_status
    cfg = _config(tmp_path)
    mdir = Path(cfg.spool_path) / "malformed"
    mdir.mkdir()
    write_message_file(mdir / "a1.md", _record(id="m1", to_project="alfred", subject=""))
    _msg_status(argparse.Namespace(), cfg, False)
    out = capsys.readouterr().out
    assert "alfred:" in out and "1 in malformed bin!" in out


def test_msg_inbox_list_shows_kind_drift_and_bin(tmp_path, capsys):
    # `msg inbox list` shows the kind-drift tag on a tolerated message AND the malformed-bin
    # line for messages addressed to this project.
    import argparse
    from alfred.cli import _msg_inbox
    cfg = _config(tmp_path)
    inbox = cfg.registry().inbox_for("alfred")
    inbox.mkdir(parents=True, exist_ok=True)
    rec = _record(id="m1", to_project="alfred", kind="fyi", original_kind="propose")
    write_message_file(inbox / message_filename(rec), rec)
    mdir = Path(cfg.spool_path) / "malformed"
    mdir.mkdir()
    write_message_file(mdir / "b1.md", _record(id="m2", to_project="alfred", subject=""))
    args = argparse.Namespace(project="alfred", inbox_action="list", json=False)
    _msg_inbox(args, cfg)
    out = capsys.readouterr().out
    assert "kind-drift: propose\u2192fyi" in out
    assert "in the MALFORMED BIN" in out


async def test_route_unknown_kind_plus_structural_break_bins_with_real_kind(tmp_path):
    # Finding A: a message with BOTH an unknown kind AND a structural break (missing subject)
    # is BINNED + BOUNCED reporting the REAL kind — NOT tolerated (kind_tolerated stays 0), and
    # NEVER rewritten to fyi in the bounce or on disk. Tolerance is deferred until AFTER the gates.
    cfg = _config(tmp_path)
    spool = Path(cfg.spool_path)
    _drop(spool, _record(id="msg-c", kind="zorp", subject=""))
    result = await run_route_once(cfg, {})
    assert result["malformed"] == 1 and result["bounced"] == 1
    assert result["kind_tolerated"] == 0 and result["routed"] == 0
    binned = parse_message_file(list((spool / "malformed").glob("*.md"))[0])
    assert binned.kind == "zorp"   # binned file keeps the REAL kind (not rewritten)
    b = parse_message_file(list(cfg.registry().inbox_for("alfred").glob("*.md"))[0])
    assert "original kind: zorp" in b.body and "original kind: fyi" not in b.body
    assert "missing subject" in b.body


def test_msg_status_surfaces_orphan_and_json_bin(tmp_path, capsys):
    # Findings B+C: a malformed file addressed to an UNREGISTERED (non-empty) `to` AND one with
    # no/unknown destination must both surface — on the JSON key AND the text '(!)' backstop.
    import argparse
    import json as _json
    from alfred.cli import _msg_status
    cfg = _config(tmp_path)
    mdir = Path(cfg.spool_path) / "malformed"
    mdir.mkdir()
    write_message_file(mdir / "a1.md", _record(id="m1", to_project="alfredd", subject=""))
    (mdir / "junk.md").write_text("no frontmatter, no destination\n")
    # JSON surface carries both keys (finding C part 1).
    _msg_status(argparse.Namespace(), cfg, True)
    data = _json.loads(capsys.readouterr().out)
    assert data["malformed_bin_by_project"]["alfredd"] == 1
    assert data["malformed_bin_by_project"]["?"] == 1
    # Text surface: the orphan '(!)' lines, including the unregistered `to` (finding B).
    _msg_status(argparse.Namespace(), cfg, False)
    tout = capsys.readouterr().out
    assert "(!)" in tout
    assert "alfredd" in tout                      # unregistered `to` no longer invisible (B)
    assert "no/unknown destination" in tout       # '?' bucket text (C part 2)
