"""#63a — attribution-confirmation re-tier: FYI demotion, 24h auto-confirm, contest door.

Contract tests written from the operator ruling BEFORE the implementation
(contract-first). The ruling, in four parts:

  1. DEMOTE attribution-confirmation cards from needs-you/deck review to
     FYI/glance tier.
  2. AUTO-CONFIRM after 24h unless contested.
  3. The contest door stays open — tap-to-contest from the FYI card reverts the
     item to needs-you AND records the contest (it is the correction signal per
     the platform's self-correcting standard; it must not be dropped).
  4. The audit trail must DISTINGUISH how an entry was confirmed: operator-explicit
     vs timeout auto-confirm vs deploy-time backfill — three distinct
     ``confirmed_via`` values, deliberately NOT merged.

On the backfill distinction (ratified, and pinned here because merging the values
is irreversible): ``timeout_24h`` means "had its 24 hours under current rules,
nobody objected"; ``backfill`` means "swept in at deploy, never offered under
these rules". They carry different contest-weighing confidence, and counting
backfill as timeout would inflate the attribution-quality signal.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import frontmatter
import pytest
import structlog

from alfred.daily_sync import attribution_section as asec
from alfred.daily_sync.action_router import (
    CONTEST_ACTION,
    FEED_ACTIONS,
    STATUS_CONTESTED,
    act,
)
from alfred.daily_sync.config import AttributionConfig, DailySyncConfig
from alfred.daily_sync.confidence import load_state, save_state
from alfred.daily_sync.feed_producer import _FAMILIES, build_feed_items
from alfred.feed import FeedStore
from alfred.feed.model import (
    ATTENTION_FYI,
    ATTENTION_NEEDS_YOU,
    KIND_DEFAULTS,
    MODE_DECIDE,
    MODE_FYI,
    STATE_OPEN,
    make_id,
)
from alfred.vault.attribution import (
    CONFIRMED_VIA_BACKFILL,
    CONFIRMED_VIA_OPERATOR,
    CONFIRMED_VIA_TIMEOUT,
    AuditEntry,
    append_audit_entry,
    confirm_marker,
    contest_marker,
    parse_audit_entries,
)

# The sweep event — ONE grep-able name, asserted by tests on both the zero path
# and the work path so a rename can't silently strand the operator's grep.
SWEEP_EVENT = "daily_sync.attribution.auto_confirm_sweep"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _ds_config(tmp_path: Path) -> DailySyncConfig:
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(tmp_path / "email_corpus.jsonl")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.attribution = AttributionConfig(
        enabled=True,
        batch_size=5,
        scan_paths=[],
        corpus_path=str(tmp_path / "attribution_corpus.jsonl"),
    )
    return cfg


def _make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "note").mkdir(parents=True)
    return vault


def _seed_record(
    vault: Path,
    rel_path: str,
    *,
    marker_id: str,
    date: str,
    confirmed: bool = False,
    contested: bool = False,
) -> None:
    """Write a vault record carrying one attribution_audit entry."""
    fm: dict[str, Any] = {
        "type": "note",
        "name": rel_path.removesuffix(".md").rsplit("/", 1)[-1],
    }
    append_audit_entry(fm, AuditEntry(
        marker_id=marker_id,
        agent="salem",
        date=date,
        section_title="Test Section",
        reason="talker conversation turn",
        confirmed_by_andrew=confirmed,
        confirmed_at="2026-08-01T00:00:00+00:00" if confirmed else None,
        contested=contested,
        contested_at="2026-08-01T00:00:00+00:00" if contested else None,
    ))
    body = (
        f'<!-- BEGIN_INFERRED marker_id="{marker_id}" -->\n'
        f"wrapped content body\n"
        f'<!-- END_INFERRED marker_id="{marker_id}" -->'
    )
    path = vault / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        frontmatter.dumps(frontmatter.Post(body, **fm)) + "\n", encoding="utf-8",
    )


def _entries(vault: Path, rel_path: str) -> list[AuditEntry]:
    post = frontmatter.load(str(vault / rel_path))
    return parse_audit_entries(post.metadata or {})


def _rows(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)


def _attribution_item(
    marker_id: str, *, record_path: str, contested: bool = False,
) -> dict[str, Any]:
    return {
        "item_number": 1,
        "record_path": record_path,
        "marker_id": marker_id,
        "agent": "salem",
        "date": "2026-08-09T18:44:00+00:00",
        "section_title": "Test Section",
        "reason": "talker conversation turn",
        "content_preview": "Wrapped content preview text.",
        "contested": contested,
    }


def _publish(store: FeedStore, item: dict[str, Any]) -> str:
    fis = build_feed_items("attribution", [item], "salem")
    assert len(fis) == 1
    store.upsert(fis[0])
    return fis[0].id


def _seed_batch(cfg: DailySyncConfig, items: list[dict[str, Any]]) -> None:
    save_state(cfg.state.path, {
        "last_batch": {
            "date": "2026-08-10", "message_ids": [100], "attribution_items": items,
        },
    })


# ---------------------------------------------------------------------------
# Ruling part 1 — the FYI demotion
# ---------------------------------------------------------------------------


def test_attribution_kind_default_is_fyi_tier() -> None:
    """Ruling 1: attribution is a glance kind, not a needs-you decision."""
    assert KIND_DEFAULTS["attribution"] == (MODE_FYI, ATTENTION_FYI)


def test_fyi_attribution_is_excluded_by_the_deck_mode_query() -> None:
    """The deck fetches ``mode=decide`` server-side (routes_feed filters
    ``it.mode != q_mode``), so an FYI-tier attribution item is never dealt as a
    card. This is the mechanism the demotion actually rides on — pinned rather
    than reasoned about."""
    item = build_feed_items("attribution", [_attribution_item("m1", record_path="note/A.md")], "salem")[0]
    assert item.mode == MODE_FYI
    assert item.mode != MODE_DECIDE  # the deck's query value


def test_contested_attribution_is_produced_as_needs_you() -> None:
    """Ruling 3: contest REVERTS the item to needs-you. The producer re-derives
    that from the entry's own contested flag, so the revert survives the next
    sync instead of being undone by a reconcile."""
    contested = _attribution_item("m2", record_path="note/B.md", contested=True)
    item = build_feed_items("attribution", [contested], "salem")[0]
    assert item.mode == MODE_DECIDE
    assert item.attention == ATTENTION_NEEDS_YOU


# ---------------------------------------------------------------------------
# Ruling part 4 — three DISTINCT confirmed_via values, schema-tolerant
# ---------------------------------------------------------------------------


def test_the_three_confirmed_via_values_are_distinct() -> None:
    """Merging any two is irreversible; this pin is the tripwire."""
    values = {CONFIRMED_VIA_OPERATOR, CONFIRMED_VIA_TIMEOUT, CONFIRMED_VIA_BACKFILL}
    assert len(values) == 3
    assert CONFIRMED_VIA_TIMEOUT != CONFIRMED_VIA_BACKFILL


def test_audit_entry_round_trips_confirmed_via_and_contested() -> None:
    entry = AuditEntry(
        marker_id="m", agent="salem", date="2026-08-10T00:00:00+00:00",
        section_title="S", reason="r",
        confirmed_by_andrew=True, confirmed_at="2026-08-10T01:00:00+00:00",
        confirmed_via=CONFIRMED_VIA_TIMEOUT, contested=True,
        contested_at="2026-08-10T02:00:00+00:00",
    )
    back = AuditEntry.from_dict(entry.to_dict())
    assert back.confirmed_via == CONFIRMED_VIA_TIMEOUT
    assert back.contested is True
    assert back.contested_at == "2026-08-10T02:00:00+00:00"


def test_from_dict_tolerates_records_predating_the_new_fields() -> None:
    """BACKWARD tolerance: a record written before #63a has no confirmed_via /
    contested keys at all. It must load, not raise."""
    old = {
        "marker_id": "m", "agent": "salem", "date": "2026-04-23T17:42:00+00:00",
        "section_title": "S", "reason": "r",
        "confirmed_by_andrew": False, "confirmed_at": None,
    }
    entry = AuditEntry.from_dict(old)
    assert entry.confirmed_via is None
    assert entry.contested is False
    assert entry.contested_at is None


def test_from_dict_tolerates_unknown_future_keys() -> None:
    """FORWARD tolerance: a record written by a NEWER build round-trips through
    an older loader without crashing."""
    raw = {
        "marker_id": "m", "agent": "salem", "date": "2026-08-10T00:00:00+00:00",
        "section_title": "S", "reason": "r",
        "confirmed_via": CONFIRMED_VIA_BACKFILL,
        "some_field_from_the_future": {"nested": 1},
    }
    entry = AuditEntry.from_dict(raw)
    assert entry.confirmed_via == CONFIRMED_VIA_BACKFILL


def test_operator_confirm_stamps_via_operator() -> None:
    """An explicit operator confirm is distinguishable in the audit trail from
    both machine paths."""
    fm: dict[str, Any] = {}
    append_audit_entry(fm, AuditEntry(
        marker_id="m", agent="salem", date="2026-08-10T00:00:00+00:00",
        section_title="S", reason="r",
    ))
    confirm_marker(fm, "m")
    entry = parse_audit_entries(fm)[0]
    assert entry.confirmed_by_andrew is True
    assert entry.confirmed_via == CONFIRMED_VIA_OPERATOR


def test_contest_marker_sets_contested_without_confirming() -> None:
    fm: dict[str, Any] = {}
    append_audit_entry(fm, AuditEntry(
        marker_id="m", agent="salem", date="2026-08-10T00:00:00+00:00",
        section_title="S", reason="r",
    ))
    changed = contest_marker(fm, "m")
    entry = parse_audit_entries(fm)[0]
    assert changed is True
    assert entry.contested is True
    assert entry.contested_at is not None
    # Contest is NOT a confirm — it reopens the question.
    assert entry.confirmed_by_andrew is False
    assert entry.confirmed_via is None


# ---------------------------------------------------------------------------
# Ruling part 2 — the 24h auto-confirm sweep
# ---------------------------------------------------------------------------


def test_untouched_entry_older_than_24h_auto_confirms_as_timeout(tmp_path: Path) -> None:
    """The core of ruling 2. Entry postdates the policy start, so it HAD its 24
    hours under current rules and nobody objected → timeout_24h."""
    cfg = _ds_config(tmp_path)
    vault = _make_vault(tmp_path)
    # Policy already started two days ago (not a first sweep).
    save_state(cfg.state.path, {
        "attribution_policy_start_at": _iso(NOW - timedelta(days=2)),
    })
    _seed_record(vault, "note/A.md", marker_id="m1", date=_iso(NOW - timedelta(hours=30)))

    result = asec.auto_confirm_sweep(vault, cfg, now=NOW)

    entry = _entries(vault, "note/A.md")[0]
    assert entry.confirmed_by_andrew is True
    assert entry.confirmed_via == CONFIRMED_VIA_TIMEOUT
    assert result.auto_confirmed == 1
    assert result.timed_out == 1
    assert result.backfilled == 0


def test_entry_predating_the_policy_start_confirms_as_backfill(tmp_path: Path) -> None:
    """The RATIFIED distinction. This entry was never offered under the new
    rules — it was swept in at deploy. It must NOT be recorded as if it had sat
    unopposed for its 24 hours."""
    cfg = _ds_config(tmp_path)
    vault = _make_vault(tmp_path)
    _seed_record(vault, "note/Old.md", marker_id="m2", date=_iso(NOW - timedelta(days=40)))

    result = asec.auto_confirm_sweep(vault, cfg, now=NOW)

    entry = _entries(vault, "note/Old.md")[0]
    assert entry.confirmed_by_andrew is True
    assert entry.confirmed_via == CONFIRMED_VIA_BACKFILL
    assert result.backfilled == 1
    assert result.timed_out == 0


def test_first_sweep_persists_a_stable_policy_start(tmp_path: Path) -> None:
    """The policy start is stamped ONCE and never moves — otherwise every run
    would re-classify fresh entries as backfill forever."""
    cfg = _ds_config(tmp_path)
    vault = _make_vault(tmp_path)
    asec.auto_confirm_sweep(vault, cfg, now=NOW)
    first = load_state(cfg.state.path).get("attribution_policy_start_at")
    assert first

    asec.auto_confirm_sweep(vault, cfg, now=NOW + timedelta(days=5))
    assert load_state(cfg.state.path).get("attribution_policy_start_at") == first


def test_an_entry_created_after_the_policy_start_ages_out_as_timeout_not_backfill(
    tmp_path: Path,
) -> None:
    """The two labels must not collapse across runs. Run 1 establishes the
    policy start; an entry created AFTER it, swept on a later run, is timeout —
    it genuinely had its window."""
    cfg = _ds_config(tmp_path)
    vault = _make_vault(tmp_path)
    asec.auto_confirm_sweep(vault, cfg, now=NOW)  # establishes policy start

    _seed_record(vault, "note/New.md", marker_id="m3", date=_iso(NOW + timedelta(hours=1)))
    result = asec.auto_confirm_sweep(vault, cfg, now=NOW + timedelta(hours=30))

    entry = _entries(vault, "note/New.md")[0]
    assert entry.confirmed_via == CONFIRMED_VIA_TIMEOUT
    assert result.timed_out == 1
    assert result.backfilled == 0


def test_entry_younger_than_24h_is_left_alone(tmp_path: Path) -> None:
    cfg = _ds_config(tmp_path)
    vault = _make_vault(tmp_path)
    save_state(cfg.state.path, {
        "attribution_policy_start_at": _iso(NOW - timedelta(days=2)),
    })
    _seed_record(vault, "note/Fresh.md", marker_id="m4", date=_iso(NOW - timedelta(hours=3)))

    result = asec.auto_confirm_sweep(vault, cfg, now=NOW)

    assert _entries(vault, "note/Fresh.md")[0].confirmed_by_andrew is False
    assert result.auto_confirmed == 0
    assert result.audited == 1


def test_contested_entry_is_preserved_not_auto_confirmed(tmp_path: Path) -> None:
    """Ruling 2's "unless contested". A contested entry is the operator saying
    the machine got it wrong — auto-confirming it would silently overrule him."""
    cfg = _ds_config(tmp_path)
    vault = _make_vault(tmp_path)
    _seed_record(
        vault, "note/C.md", marker_id="m5",
        date=_iso(NOW - timedelta(days=10)), contested=True,
    )

    result = asec.auto_confirm_sweep(vault, cfg, now=NOW)

    entry = _entries(vault, "note/C.md")[0]
    assert entry.confirmed_by_andrew is False
    assert entry.confirmed_via is None
    assert entry.contested is True
    assert result.contested_preserved == 1
    assert result.auto_confirmed == 0


def test_already_confirmed_entry_is_not_restamped(tmp_path: Path) -> None:
    """Idempotence: a re-run must not overwrite an operator confirm with a
    machine one — that would erase the strongest signal in the trail."""
    cfg = _ds_config(tmp_path)
    vault = _make_vault(tmp_path)
    _seed_record(
        vault, "note/Done.md", marker_id="m6",
        date=_iso(NOW - timedelta(days=10)), confirmed=True,
    )
    before = (vault / "note/Done.md").read_text()

    result = asec.auto_confirm_sweep(vault, cfg, now=NOW)

    assert (vault / "note/Done.md").read_text() == before
    assert result.auto_confirmed == 0


def test_entry_with_an_unparseable_date_is_preserved_not_confirmed(tmp_path: Path) -> None:
    """Fail SAFE: an entry whose age can't be computed has not demonstrably had
    its 24 hours, so it stays for the operator rather than being swept."""
    cfg = _ds_config(tmp_path)
    vault = _make_vault(tmp_path)
    _seed_record(vault, "note/Bad.md", marker_id="m7", date="not-a-date")

    result = asec.auto_confirm_sweep(vault, cfg, now=NOW)

    assert _entries(vault, "note/Bad.md")[0].confirmed_by_andrew is False
    assert result.undated_preserved == 1
    assert result.auto_confirmed == 0


def test_sweep_writes_a_corpus_row_naming_how_it_confirmed(tmp_path: Path) -> None:
    """The audit trail is the deliverable, not a side effect — the corpus row
    must carry the via so the trail is readable without re-walking the vault."""
    cfg = _ds_config(tmp_path)
    vault = _make_vault(tmp_path)
    _seed_record(vault, "note/A.md", marker_id="m8", date=_iso(NOW - timedelta(days=40)))

    asec.auto_confirm_sweep(vault, cfg, now=NOW)

    rows = _rows(cfg.attribution.corpus_path)
    assert len(rows) == 1
    assert rows[0]["confirmed_via"] == CONFIRMED_VIA_BACKFILL
    assert rows[0]["marker_id"] == "m8"
    # An auto-confirm must NEVER claim the operator acted.
    assert rows[0]["andrew_action"] != "confirm"


# --- ILB: the sweep reports every run, including the zero path ---------------


def test_sweep_emits_its_counts_on_the_zero_path(tmp_path: Path) -> None:
    """Intentionally-left-blank: an empty vault must still say "ran, nothing to
    do" — silence is indistinguishable from a sweep that never fired."""
    cfg = _ds_config(tmp_path)
    vault = _make_vault(tmp_path)

    with structlog.testing.capture_logs() as captured:
        result = asec.auto_confirm_sweep(vault, cfg, now=NOW)

    matches = [c for c in captured if c.get("event") == SWEEP_EVENT]
    assert len(matches) == 1
    assert matches[0]["audited"] == 0
    assert matches[0]["auto_confirmed"] == 0
    assert matches[0]["contested_preserved"] == 0
    assert result.audited == 0


def test_sweep_emits_full_counts_on_the_work_path(tmp_path: Path) -> None:
    cfg = _ds_config(tmp_path)
    vault = _make_vault(tmp_path)
    save_state(cfg.state.path, {
        "attribution_policy_start_at": _iso(NOW - timedelta(days=30)),
    })
    _seed_record(vault, "note/A.md", marker_id="a", date=_iso(NOW - timedelta(days=2)))
    _seed_record(vault, "note/B.md", marker_id="b", date=_iso(NOW - timedelta(hours=2)))
    _seed_record(
        vault, "note/C.md", marker_id="c",
        date=_iso(NOW - timedelta(days=2)), contested=True,
    )

    with structlog.testing.capture_logs() as captured:
        result = asec.auto_confirm_sweep(vault, cfg, now=NOW)

    matches = [c for c in captured if c.get("event") == SWEEP_EVENT]
    assert len(matches) == 1
    ev = matches[0]
    assert ev["audited"] == 3
    assert ev["auto_confirmed"] == 1
    assert ev["contested_preserved"] == 1
    assert ev["timed_out"] == 1
    assert ev["backfilled"] == 0
    assert result.auto_confirmed == 1


def test_sweep_respects_configured_scan_paths(tmp_path: Path) -> None:
    """Instance scoping is config-driven (no instance literals): a record
    outside scan_paths is not swept."""
    cfg = _ds_config(tmp_path)
    cfg.attribution.scan_paths = ["note"]
    vault = _make_vault(tmp_path)
    (vault / "person").mkdir(parents=True, exist_ok=True)
    _seed_record(vault, "note/In.md", marker_id="in", date=_iso(NOW - timedelta(days=40)))
    _seed_record(vault, "person/Out.md", marker_id="out", date=_iso(NOW - timedelta(days=40)))

    result = asec.auto_confirm_sweep(vault, cfg, now=NOW)

    assert _entries(vault, "note/In.md")[0].confirmed_by_andrew is True
    assert _entries(vault, "person/Out.md")[0].confirmed_by_andrew is False
    assert result.audited == 1


def test_disabled_attribution_block_sweeps_nothing(tmp_path: Path) -> None:
    cfg = _ds_config(tmp_path)
    cfg.attribution.enabled = False
    vault = _make_vault(tmp_path)
    _seed_record(vault, "note/A.md", marker_id="m", date=_iso(NOW - timedelta(days=40)))

    result = asec.auto_confirm_sweep(vault, cfg, now=NOW)

    assert _entries(vault, "note/A.md")[0].confirmed_by_andrew is False
    assert result.audited == 0


def test_auto_confirmed_entries_drop_out_of_the_review_batch(tmp_path: Path) -> None:
    """End-to-end point of the whole feature: after the sweep, the operator's
    batch no longer carries the entries that aged out."""
    cfg = _ds_config(tmp_path)
    vault = _make_vault(tmp_path)
    _seed_record(vault, "note/Old.md", marker_id="old", date=_iso(NOW - timedelta(days=40)))
    _seed_record(vault, "note/New.md", marker_id="new", date=_iso(NOW - timedelta(hours=2)))

    assert len(asec.build_batch(vault, cfg)) == 2
    asec.auto_confirm_sweep(vault, cfg, now=NOW)
    remaining = asec.build_batch(vault, cfg)

    assert [i.marker_id for i in remaining] == ["new"]


# ---------------------------------------------------------------------------
# Ruling part 3 — the contest door
# ---------------------------------------------------------------------------


def test_contest_is_in_the_capability_ceiling() -> None:
    assert CONTEST_ACTION in FEED_ACTIONS["attribution"]


def test_the_web_contest_verb_matches_the_backend_ceiling() -> None:
    """CROSS-SURFACE DRIFT PIN, same shape as the snooze-ladder pin in
    tests/tier/test_board_snooze.py.

    The PWA hardcodes its own copy of the action_id (TypeScript can't import a
    Python constant), so the two can drift in silence — and neither side can
    notice alone. Rename it on one side only and the operator gets a button that
    400s with `invalid_action`, while every test on both sides stays green: the
    web tests compare the constant against itself, and the backend tests never
    look at the browser.

    Parsed out of the TS source rather than restated here, so this doesn't just
    become a third copy that drifts too.

    Mutation: change CONTEST_ACTION on either side alone → this fails and names
    the mismatch.
    """
    import re

    ts = (
        Path(__file__).resolve().parents[2]
        / "web" / "lib" / "algernon" / "feedConstants.ts"
    )
    assert ts.exists(), f"the web constants moved — update this pin: {ts}"
    match = re.search(
        r"CONTEST_ACTION\s*=\s*'([^']+)'", ts.read_text(encoding="utf-8"),
    )
    assert match, "CONTEST_ACTION not found in feedConstants.ts"
    web_verb = match.group(1)

    assert web_verb == CONTEST_ACTION, (
        f"contest verb drift — web POSTs {web_verb!r}, "
        f"backend ceiling admits {CONTEST_ACTION!r}"
    )
    assert web_verb in FEED_ACTIONS["attribution"], (
        f"the web's {web_verb!r} is not in the attribution capability ceiling"
    )


def test_contest_records_the_signal_and_reverts_to_needs_you(tmp_path: Path) -> None:
    """Ruling 3, both halves in one act: the contest is RECORDED (the correction
    signal is not dropped on the floor) AND the item goes back to needs-you."""
    cfg = _ds_config(tmp_path)
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    vault = _make_vault(tmp_path)
    _seed_record(vault, "note/A.md", marker_id="mc", date=_iso(NOW - timedelta(hours=2)))
    item = _attribution_item("mc", record_path="note/A.md")
    fid = _publish(store, item)
    _seed_batch(cfg, [item])

    result = act(
        fid, CONTEST_ACTION, feed_store=store, config=cfg, vault_path=vault,
        instance_name="salem", instance_scope="talker",
    )

    assert result.ok and result.status == STATUS_CONTESTED
    # 1. Recorded in the vault record.
    entry = _entries(vault, "note/A.md")[0]
    assert entry.contested is True
    # 2. Recorded in the corpus — the correction signal, durably.
    rows = _rows(cfg.attribution.corpus_path)
    assert len(rows) == 1
    assert rows[0]["andrew_action"] == "contest"
    assert rows[0]["marker_id"] == "mc"
    # 3. Reverted to needs-you, and still OPEN (it awaits a decision).
    stored = store.load()[fid]
    assert stored.mode == MODE_DECIDE
    assert stored.attention == ATTENTION_NEEDS_YOU
    assert stored.state == STATE_OPEN


def test_a_contested_entry_survives_the_next_sweep(tmp_path: Path) -> None:
    """The contest must actually STICK — the whole door is theatre if the next
    night's sweep confirms it anyway."""
    cfg = _ds_config(tmp_path)
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    vault = _make_vault(tmp_path)
    _seed_record(vault, "note/A.md", marker_id="mc2", date=_iso(NOW - timedelta(days=40)))
    item = _attribution_item("mc2", record_path="note/A.md")
    fid = _publish(store, item)
    _seed_batch(cfg, [item])

    act(fid, CONTEST_ACTION, feed_store=store, config=cfg, vault_path=vault,
        instance_name="salem", instance_scope="talker")
    result = asec.auto_confirm_sweep(vault, cfg, now=NOW)

    assert _entries(vault, "note/A.md")[0].confirmed_by_andrew is False
    assert result.contested_preserved == 1


def test_contest_without_a_vault_path_is_an_honest_error(tmp_path: Path) -> None:
    cfg = _ds_config(tmp_path)
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    item = _attribution_item("mc3", record_path="note/A.md")
    fid = _publish(store, item)
    _seed_batch(cfg, [item])

    result = act(
        fid, CONTEST_ACTION, feed_store=store, config=cfg, vault_path=None,
        instance_name="salem", instance_scope="talker",
    )

    assert not result.ok
    assert _rows(cfg.attribution.corpus_path) == []


def test_contest_is_rejected_on_a_non_attribution_kind(tmp_path: Path) -> None:
    """Capability ceiling: contest is attribution's verb alone. It must not
    become a universal door onto other families' resolvers."""
    for kind, actions in FEED_ACTIONS.items():
        if kind != "attribution":
            assert CONTEST_ACTION not in actions, f"{kind} must not admit contest"


def test_contested_item_can_still_be_confirmed_by_the_operator(tmp_path: Path) -> None:
    """The contest reopens the question; the operator must be able to close it.
    A contested item that could never be resolved would be a roach motel."""
    cfg = _ds_config(tmp_path)
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    vault = _make_vault(tmp_path)
    _seed_record(
        vault, "note/A.md", marker_id="mc4",
        date=_iso(NOW - timedelta(hours=2)), contested=True,
    )
    item = _attribution_item("mc4", record_path="note/A.md", contested=True)
    fid = _publish(store, item)
    _seed_batch(cfg, [item])

    result = act(
        fid, "confirm", feed_store=store, config=cfg, vault_path=vault,
        instance_name="salem", instance_scope="talker",
    )

    assert result.ok
    entry = _entries(vault, "note/A.md")[0]
    assert entry.confirmed_by_andrew is True
    assert entry.confirmed_via == CONFIRMED_VIA_OPERATOR


# ---------------------------------------------------------------------------
# Half-deploy honesty (both orders named in the dispatch)
# ---------------------------------------------------------------------------


def test_sweep_auto_confirms_regardless_of_any_ui_surface(tmp_path: Path) -> None:
    """Half-deploy order A — Python live, web not yet. The sweep is surface-free:
    it walks the vault and writes the vault. Nothing about it depends on a card
    having been rendered, so "sweep with no UI demotion" still matches the
    ruling."""
    cfg = _ds_config(tmp_path)
    vault = _make_vault(tmp_path)
    _seed_record(vault, "note/A.md", marker_id="hd", date=_iso(NOW - timedelta(days=40)))

    result = asec.auto_confirm_sweep(vault, cfg, now=NOW)

    assert result.auto_confirmed == 1
    assert _entries(vault, "note/A.md")[0].confirmed_by_andrew is True


def test_the_section_footer_tells_the_operator_that_silence_confirms(tmp_path: Path) -> None:
    """The reply grammar's copy predates auto-confirm and read as though an
    answer were required. It isn't any more — and the difference matters,
    because under the new policy NOT replying is itself a decision. Copy that
    hides that is the system quietly acting on his behalf while implying it
    won't.
    """
    items = asec.build_batch(_seeded_vault_for_footer(tmp_path), _ds_config(tmp_path))
    rendered = asec.render_batch(items)

    assert "confirms itself after a day" in rendered


def _seeded_vault_for_footer(tmp_path: Path) -> Path:
    vault = _make_vault(tmp_path)
    _seed_record(vault, "note/A.md", marker_id="foot", date=_iso(NOW - timedelta(hours=2)))
    return vault


def test_an_old_web_bundle_can_still_ack_the_demoted_card(tmp_path: Path) -> None:
    """Half-deploy order A — Python live, the OLD web bundle still cached on the
    phone. That bundle has no contest door; its only affordance on an FYI row is
    Ack, which POSTs the universal ``ack``.

    Two things must hold. It must SUCCEED — the universal FYI-ack gate admits it
    precisely because the item is now MODE_FYI, so the demotion is what makes the
    old button keep working. And it must NOT confirm the marker: an ack is "seen",
    not "endorsed", and quietly converting one into the other would manufacture
    exactly the operator endorsement the confirmed_via split exists to keep
    honest. The entry stays unconfirmed and ages out through the sweep instead.
    """
    cfg = _ds_config(tmp_path)
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    vault = _make_vault(tmp_path)
    _seed_record(vault, "note/A.md", marker_id="ack1", date=_iso(NOW - timedelta(hours=2)))
    item = _attribution_item("ack1", record_path="note/A.md")
    fid = _publish(store, item)
    _seed_batch(cfg, [item])

    result = act(
        fid, "ack", feed_store=store, config=cfg, vault_path=vault,
        instance_name="salem", instance_scope="talker",
    )

    assert result.ok
    entry = _entries(vault, "note/A.md")[0]
    assert entry.confirmed_by_andrew is False
    assert entry.confirmed_via is None


def test_an_unswept_fyi_item_simply_sits_unconfirmed(tmp_path: Path) -> None:
    """Half-deploy order B — web live, sweep not yet running. The FYI card
    renders and the entry stays unconfirmed. Harmless: no data is lost and no
    promise is broken."""
    cfg = _ds_config(tmp_path)
    vault = _make_vault(tmp_path)
    _seed_record(vault, "note/A.md", marker_id="hd2", date=_iso(NOW - timedelta(days=40)))

    batch = asec.build_batch(vault, cfg)

    assert len(batch) == 1
    assert _entries(vault, "note/A.md")[0].confirmed_by_andrew is False
