"""Same-day collapse coordinator pins — the rTMS umbrella (Step 4 §3).

Events sharing ``(gcal_collapse_key, date)`` project to ONE GCal entry spanning
earliest-start → latest-end, titled with the auto-summary
``"<key> — N sessions (HH:MM–HH:MM)"``. The first/existing-id member is the
PRIMARY (owns the single ``gcal_event_id``); siblings carry the key with no
own id. :func:`alfred.integrations.gcal_sync.sync_collapse_group` is an
idempotent full recompute (the no-double-create guarantee).

Coverage (the operator-greenlit pin list): 3-member→one entry, add-4th→patch,
none-excluded, secondary-delete→recompute, primary-delete→promote,
last-delete→teardown, adopt-manual-umbrella, idempotent-twice,
different-date/key→separate groups, no-match→noop, cancelled-member-excluded,
+ the CLI batch path.

Unconditional pins (no importorskip — GCal client is a fake; no HTTP/SDK).
"""
from __future__ import annotations

import json
from pathlib import Path

import frontmatter
import structlog

from alfred.integrations.gcal_config import GCalConfig
from alfred.integrations.gcal_sync import resolve_collapse_key, sync_collapse_group


# ---------------------------------------------------------------------------
# Fakes + helpers
# ---------------------------------------------------------------------------


class _FakeGCal:
    """Records create/update/delete calls; create returns a fresh id."""

    def __init__(self, *, update_returns_none: bool = False) -> None:
        self.created: list[tuple[str, dict]] = []
        self.updated: list[tuple[str, dict]] = []
        self.deleted: list[str] = []
        self._n = 0
        self._update_none = update_returns_none

    def create_event(self, calendar_id, **kw):
        self._n += 1
        eid = f"evt-{self._n}"
        self.created.append((eid, kw))
        return eid

    def update_event(self, calendar_id, event_id, **kw):
        self.updated.append((event_id, kw))
        return None if self._update_none else {"id": event_id}

    def delete_event(self, calendar_id, event_id):
        self.deleted.append(event_id)
        return True


def _cfg():
    return GCalConfig(
        enabled=True, alfred_calendar_id="cal@g.com",
        alfred_calendar_label="alfred",
    )


_OFFSET = "-03:00"


def _seed(
    tmp: Path, name: str, *, date_str: str, start_hm: str | None = None,
    end_hm: str | None = None, key: str = "rTMS",
    gcal_event_id: str | None = None, gcal_sync: str | None = None,
    status: str | None = None,
) -> Path:
    event_dir = tmp / "event"
    event_dir.mkdir(exist_ok=True)
    fm: dict = {"type": "event", "name": name, "date": date_str}
    if key is not None:
        fm["gcal_collapse_key"] = key
    if start_hm is not None:
        fm["start"] = f"{date_str}T{start_hm}:00{_OFFSET}"
    if end_hm is not None:
        fm["end"] = f"{date_str}T{end_hm}:00{_OFFSET}"
    if gcal_event_id is not None:
        fm["gcal_event_id"] = gcal_event_id
    if gcal_sync is not None:
        fm["gcal_sync"] = gcal_sync
    if status is not None:
        fm["status"] = status
    path = event_dir / f"{name}.md"
    path.write_text(
        frontmatter.dumps(frontmatter.Post("body\n", **fm)) + "\n",
        encoding="utf-8",
    )
    return path


def _meta(path: Path) -> dict:
    return frontmatter.load(str(path)).metadata


def _run(tmp, client, *, key="rTMS", date_str="2026-07-06", orphan=""):
    return sync_collapse_group(
        client=client, config=_cfg(), vault_path=tmp,
        collapse_key=key, group_date=date_str, intended_on=True,
        orphan_event_id=orphan,
    )


D = "2026-07-06"


# ---------------------------------------------------------------------------
# 1. Three members → ONE entry, span min→max, auto-summary title
# ---------------------------------------------------------------------------


def test_three_members_one_create_span_and_title(tmp_path):
    _seed(tmp_path, "A", date_str=D, start_hm="08:30", end_hm="09:00")
    _seed(tmp_path, "B", date_str=D, start_hm="09:30", end_hm="10:00")
    _seed(tmp_path, "C", date_str=D, start_hm="10:30", end_hm="11:00")
    client = _FakeGCal()

    out = _run(tmp_path, client)

    assert out["action"] == "created"
    assert len(client.created) == 1
    assert out["member_count"] == 3
    assert out["title"] == "rTMS — 3 sessions (08:30–11:00)"
    primary_id = out["primary_event_id"]
    # Primary = earliest-start member (A) owns the id; B/C are secondaries.
    assert _meta(tmp_path / "event" / "A.md")["gcal_event_id"] == primary_id
    assert "gcal_event_id" not in _meta(tmp_path / "event" / "B.md")
    assert "gcal_event_id" not in _meta(tmp_path / "event" / "C.md")


# ---------------------------------------------------------------------------
# 2. Add a 4th member → PATCH the primary, no new create
# ---------------------------------------------------------------------------


def test_add_member_patches_not_recreates(tmp_path):
    _seed(tmp_path, "A", date_str=D, start_hm="08:30", end_hm="09:00",
          gcal_event_id="evt-existing")
    _seed(tmp_path, "B", date_str=D, start_hm="09:30", end_hm="10:00")
    _seed(tmp_path, "C", date_str=D, start_hm="10:30", end_hm="11:00")
    _seed(tmp_path, "D", date_str=D, start_hm="11:30", end_hm="14:45")
    client = _FakeGCal()

    out = _run(tmp_path, client)

    assert out["action"] == "patched"
    assert len(client.created) == 0
    assert client.updated and client.updated[0][0] == "evt-existing"
    assert out["member_count"] == 4
    assert out["title"] == "rTMS — 4 sessions (08:30–14:45)"
    assert out["primary_event_id"] == "evt-existing"


# ---------------------------------------------------------------------------
# 3. gcal_sync:none member excluded from the group
# ---------------------------------------------------------------------------


def test_sync_none_member_excluded(tmp_path):
    _seed(tmp_path, "A", date_str=D, start_hm="08:30", end_hm="09:00")
    _seed(tmp_path, "B", date_str=D, start_hm="09:30", end_hm="10:00")
    _seed(tmp_path, "C", date_str=D, start_hm="10:30", end_hm="11:00",
          gcal_sync="none")
    client = _FakeGCal()

    out = _run(tmp_path, client)

    assert out["member_count"] == 2  # C excluded
    assert out["title"] == "rTMS — 2 sessions (08:30–10:00)"


# ---------------------------------------------------------------------------
# 4. Secondary delete → recompute span (patch)
# ---------------------------------------------------------------------------


def test_secondary_delete_recomputes_span(tmp_path):
    _seed(tmp_path, "A", date_str=D, start_hm="08:30", end_hm="09:00",
          gcal_event_id="evt-1")
    _seed(tmp_path, "B", date_str=D, start_hm="09:30", end_hm="10:00")
    _seed(tmp_path, "C", date_str=D, start_hm="10:30", end_hm="11:00")
    # Simulate the delete of secondary C (file gone before the hook fires).
    (tmp_path / "event" / "C.md").unlink()
    client = _FakeGCal()

    out = _run(tmp_path, client)

    assert out["action"] == "patched"
    assert out["member_count"] == 2
    assert out["title"] == "rTMS — 2 sessions (08:30–10:00)"
    assert len(client.created) == 0


# ---------------------------------------------------------------------------
# 5. Primary delete → PROMOTE the orphaned id onto a survivor
# ---------------------------------------------------------------------------


def test_primary_delete_promotes_orphan_onto_survivor(tmp_path):
    # Primary A is already deleted (file gone); B/C survive (no own id).
    _seed(tmp_path, "B", date_str=D, start_hm="09:30", end_hm="10:00")
    _seed(tmp_path, "C", date_str=D, start_hm="10:30", end_hm="11:00")
    client = _FakeGCal()

    out = _run(tmp_path, client, orphan="evt-old")

    assert out["action"] == "patched"  # adopt the orphan (no new create)
    assert len(client.created) == 0
    assert out["primary_event_id"] == "evt-old"
    # The orphan id is promoted onto the earliest survivor (B).
    assert _meta(tmp_path / "event" / "B.md")["gcal_event_id"] == "evt-old"
    assert "gcal_event_id" not in _meta(tmp_path / "event" / "C.md")
    assert client.updated and client.updated[0][0] == "evt-old"


# ---------------------------------------------------------------------------
# 6. Last-member delete → tear down the entry
# ---------------------------------------------------------------------------


def test_last_member_delete_tears_down_entry(tmp_path):
    # No members remain; the deleted primary's orphan id must be removed.
    (tmp_path / "event").mkdir()
    client = _FakeGCal()

    out = _run(tmp_path, client, orphan="evt-old")

    assert out["action"] == "deleted"
    assert out["member_count"] == 0
    assert client.deleted == ["evt-old"]


# ---------------------------------------------------------------------------
# 7. Adopt a manually-created umbrella as primary (no second create)
# ---------------------------------------------------------------------------


def test_adopt_manual_umbrella_as_primary(tmp_path):
    # 3 granular members (no id) + the manual umbrella (has gcal_event_id,
    # spans the day). Coordinator elects the umbrella (it has the id) →
    # ADOPTS it; no duplicate create.
    _seed(tmp_path, "A", date_str=D, start_hm="08:30", end_hm="09:00")
    _seed(tmp_path, "B", date_str=D, start_hm="09:30", end_hm="10:00")
    _seed(tmp_path, "C", date_str=D, start_hm="10:30", end_hm="11:00")
    _seed(tmp_path, "Umbrella", date_str=D, start_hm="08:30", end_hm="14:45",
          gcal_event_id="manual-1")
    client = _FakeGCal()

    out = _run(tmp_path, client)

    assert out["action"] == "patched"
    assert len(client.created) == 0
    assert out["primary_event_id"] == "manual-1"
    assert client.updated[0][0] == "manual-1"
    # Umbrella keeps the id; granular members stay secondary.
    assert _meta(tmp_path / "event" / "Umbrella.md")["gcal_event_id"] == "manual-1"
    for n in ("A", "B", "C"):
        assert "gcal_event_id" not in _meta(tmp_path / "event" / f"{n}.md")


# ---------------------------------------------------------------------------
# 8. Idempotent — a second pass creates no second entry, AND (NOTE-A) skips the
#    redundant PATCH when nothing changed.
# ---------------------------------------------------------------------------


def test_idempotent_recompute(tmp_path):
    _seed(tmp_path, "A", date_str=D, start_hm="08:30", end_hm="09:00")
    _seed(tmp_path, "B", date_str=D, start_hm="09:30", end_hm="10:00")
    client = _FakeGCal()

    out1 = _run(tmp_path, client)
    out2 = _run(tmp_path, client)

    assert out1["action"] == "created"
    # NOTE-A skip-unchanged: the second identical recompute no longer fires a
    # redundant PATCH — span+title match the stored gcal_collapse_synced
    # signature, so the coordinator short-circuits to a noop.
    assert out2["action"] == "noop"
    assert out2["noop"] == "collapse_unchanged"
    assert client.updated == []           # NO redundant update_event call
    assert len(client.created) == 1       # exactly one entry ever created
    assert out1["primary_event_id"] == out2["primary_event_id"]
    assert out2["member_count"] == 2


# ---------------------------------------------------------------------------
# 9. Different date / different key → separate groups
# ---------------------------------------------------------------------------


def test_different_date_and_key_are_separate_groups(tmp_path):
    _seed(tmp_path, "A", date_str=D, start_hm="08:30", end_hm="09:00")
    _seed(tmp_path, "B", date_str=D, start_hm="09:30", end_hm="10:00")
    # Same key, different DATE.
    _seed(tmp_path, "C", date_str="2026-07-07", start_hm="08:30", end_hm="09:00")
    # Different KEY, same date.
    _seed(tmp_path, "P", date_str=D, start_hm="13:00", end_hm="14:00", key="physio")
    client = _FakeGCal()

    out = _run(tmp_path, client)  # rTMS / 2026-07-06
    assert out["member_count"] == 2  # only A, B

    out_phys = _run(tmp_path, client, key="physio")
    assert out_phys["member_count"] == 1  # only P


# ---------------------------------------------------------------------------
# 10. No matching members → clean noop (no spurious create/delete)
# ---------------------------------------------------------------------------


def test_no_match_is_noop(tmp_path):
    (tmp_path / "event").mkdir()
    _seed(tmp_path, "A", date_str=D, start_hm="08:30", end_hm="09:00", key="rTMS")
    client = _FakeGCal()

    out = _run(tmp_path, client, key="does-not-exist")

    assert out == {"noop": "no_eligible_members"}
    assert client.created == [] and client.deleted == []


# ---------------------------------------------------------------------------
# 11. Cancelled member excluded from the span
# ---------------------------------------------------------------------------


def test_cancelled_member_excluded(tmp_path):
    _seed(tmp_path, "A", date_str=D, start_hm="08:30", end_hm="09:00")
    _seed(tmp_path, "B", date_str=D, start_hm="09:30", end_hm="10:00")
    _seed(tmp_path, "C", date_str=D, start_hm="10:30", end_hm="11:00",
          status="cancelled")
    client = _FakeGCal()

    out = _run(tmp_path, client)

    assert out["member_count"] == 2  # C cancelled → excluded
    assert out["title"] == "rTMS — 2 sessions (08:30–10:00)"


# ---------------------------------------------------------------------------
# 12. Disabled config → skip (no calls)
# ---------------------------------------------------------------------------


def test_disabled_config_skips(tmp_path):
    _seed(tmp_path, "A", date_str=D, start_hm="08:30", end_hm="09:00")
    client = _FakeGCal()
    out = sync_collapse_group(
        client=client, config=GCalConfig(enabled=False),
        vault_path=tmp_path, collapse_key="rTMS", group_date=D,
    )
    assert out == {}
    assert client.created == []


# ---------------------------------------------------------------------------
# 13. resolve_collapse_key + the CLI batch path
# ---------------------------------------------------------------------------


def test_resolve_collapse_key():
    assert resolve_collapse_key({"gcal_collapse_key": "  rTMS "}) == "rTMS"
    assert resolve_collapse_key({"gcal_collapse_key": ""}) == ""
    assert resolve_collapse_key({}) == ""
    assert resolve_collapse_key({"gcal_collapse_key": 5}) == ""


def test_cli_collapse_disabled(tmp_path, capsys):
    from alfred.integrations import gcal_cli

    rc = gcal_cli.cmd_collapse(
        {"vault": {"path": str(tmp_path)}, "gcal": {"enabled": False}},
        collapse_key="rTMS", group_date=D, wants_json=True,
    )
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_cli_collapse_bad_date(tmp_path, capsys):
    from alfred.integrations import gcal_cli

    rc = gcal_cli.cmd_collapse(
        {
            "vault": {"path": str(tmp_path)},
            "gcal": {"enabled": True, "alfred_calendar_id": "cal@g.com"},
        },
        collapse_key="rTMS", group_date="not-a-date", wants_json=True,
    )
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


# ---------------------------------------------------------------------------
# 14. NOTE-A skip-unchanged — an unchanged recompute fires NO redundant PATCH
# ---------------------------------------------------------------------------


def test_unchanged_recompute_skips_redundant_patch(tmp_path):
    """First sync creates + persists the gcal_collapse_synced signature. A
    second recompute with no change short-circuits to a noop — NO update_event.
    This is the redundant-PATCH (non-time-field member edit → hook → recompute)
    that NOTE-A eliminates."""
    _seed(tmp_path, "A", date_str=D, start_hm="08:30", end_hm="09:00")
    _seed(tmp_path, "B", date_str=D, start_hm="09:30", end_hm="10:00")
    client = _FakeGCal()

    out1 = _run(tmp_path, client)
    assert out1["action"] == "created"
    # The signature is persisted on the elected primary for next-pass compare.
    primary_meta = _meta(tmp_path / "event" / "A.md")
    assert primary_meta.get("gcal_collapse_synced")
    assert "|" in primary_meta["gcal_collapse_synced"]

    # Per feedback_log_emission_test_pattern.md: pin the ILB observability
    # event so a future refactor that drops/renames it is caught here.
    with structlog.testing.capture_logs() as captured:
        out2 = _run(tmp_path, client)
    assert out2["action"] == "noop"
    assert out2["noop"] == "collapse_unchanged"
    assert client.updated == []          # the redundant PATCH was skipped
    assert len(client.created) == 1
    assert out2["primary_event_id"] == out1["primary_event_id"]
    matches = [c for c in captured if c.get("event") == "gcal.collapse_unchanged"]
    assert len(matches) == 1, (
        f"expected one gcal.collapse_unchanged; got "
        f"{[c.get('event') for c in captured]!r}"
    )
    assert matches[0]["primary_event_id"] == out1["primary_event_id"]
    assert matches[0]["member_count"] == 2


# ---------------------------------------------------------------------------
# 15. NOTE-A — a GENUINE span change still PATCHes (skip is change-gated)
# ---------------------------------------------------------------------------


def test_changed_span_still_patches(tmp_path):
    """After a first sync, adding a member extends the span + bumps the
    "N sessions" title → the signature differs → the coordinator still fires
    the PATCH (the skip is strictly change-gated)."""
    _seed(tmp_path, "A", date_str=D, start_hm="08:30", end_hm="09:00")
    _seed(tmp_path, "B", date_str=D, start_hm="09:30", end_hm="10:00")
    client = _FakeGCal()

    out1 = _run(tmp_path, client)
    assert out1["action"] == "created"

    # New later member → span end moves to 14:45, "3 sessions" → new signature.
    _seed(tmp_path, "C", date_str=D, start_hm="13:30", end_hm="14:45")
    out2 = _run(tmp_path, client)

    assert out2["action"] == "patched"
    assert client.updated and client.updated[0][0] == out1["primary_event_id"]
    assert out2["member_count"] == 3
    assert out2["title"] == "rTMS — 3 sessions (08:30–14:45)"
    assert len(client.created) == 1      # still no second create
