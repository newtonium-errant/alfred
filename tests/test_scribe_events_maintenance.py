"""Daemon event-store maintenance pins (event-store design §4 / §5.3 / §5.5 / §8 rows 8-9).

  store.heartbeat: emitted when >24h since the last, per-family counts, NOT within 24h;
  access.system_reads_summary: once per UTC day (day latch), even at zero;
  post-attest-edit scan: detects a signed-note body edit, emits + latches per (encounter, sha),
    hot-window-bounded per-sweep vs full at boot.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import frontmatter

from alfred.scribe.attest import attest
from alfred.scribe.events import ScribeEvents
from alfred.scribe.events_maintenance import ScribeEventMaintenance
from alfred.vault.ops import vault_create

_SALT = "DUMMY_SCRIBE_TEST_SALT"
_CLINICIANS = {"np_jamie"}


def _events(tmp_path, clock="2026-07-16T12:00:00+00:00"):
    raw = {"scribe": {"mode": "clinical", "encounter_salt": _SALT,
                      "events": {"dir": str(tmp_path / "ev")}}}
    return ScribeEvents.from_config(raw, log_dir=str(tmp_path / "logs"), clock=lambda: clock)


def _attested_note(tmp_path, ev, *, source_id="enc-heartbeat00001", now=None):
    vault = tmp_path / "vault"
    rel = vault_create(
        vault, "clinical_note", f"Enc {source_id}",
        set_fields={"ai_draft": True, "synthetic": True, "status": "ai_draft",
                    "source_id": source_id, "drafted_by": "stayc_scribe",
                    "encounter_completeness": {"protocol": 1, "complete": True}},
        body="## Subjective\nReports chest pain.\n", scope="stayc_clinical")["path"]
    attest(vault, rel, new_status="attested", attester="np_jamie",
           clinician_ids=_CLINICIANS, audit_path=tmp_path / "a.jsonl",
           now=now or datetime(2026, 7, 16, 12, tzinfo=timezone.utc), events=ev)
    return vault, rel


# --- heartbeat --------------------------------------------------------------

def test_heartbeat_emits_with_family_counts(tmp_path):
    ev = _events(tmp_path)
    ev.note_draft_created(subject_id="e", body_sha="a")
    ev.note_ready(subject_id="e", body_sha="b", expected_final_seq=1, folded_through=1)
    ev.encounter_opened(subject_id="e")
    maint = ScribeEventMaintenance(ev)
    maint.heartbeat_if_due(now="2026-07-16T12:00:00+00:00")
    hb = ev.query("clinical", kind="store.heartbeat")
    assert len(hb) == 1
    p = hb[0]["payload"]
    assert p["count_note"] == 2 and p["count_encounter"] == 1
    assert p["count_attestation"] == 0 and p["count_consent"] == 0 and p["count_retention"] == 0


def test_heartbeat_not_emitted_within_24h(tmp_path):
    ev = _events(tmp_path)
    maint = ScribeEventMaintenance(ev)
    maint.heartbeat_if_due(now="2026-07-16T12:00:00+00:00")
    maint.heartbeat_if_due(now="2026-07-16T20:00:00+00:00")  # 8h later — no second heartbeat
    assert len(ev.query("clinical", kind="store.heartbeat")) == 1
    maint.heartbeat_if_due(now="2026-07-17T13:00:00+00:00")  # >24h — second heartbeat
    assert len(ev.query("clinical", kind="store.heartbeat")) == 2


# --- daily suppressed-reads summary -----------------------------------------

def test_suppression_summary_once_per_day_even_at_zero(tmp_path):
    ev = _events(tmp_path)
    maint = ScribeEventMaintenance(ev)
    maint.flush_suppressed_if_new_day(now="2026-07-16T09:00:00+00:00")
    maint.flush_suppressed_if_new_day(now="2026-07-16T18:00:00+00:00")  # same day — no second
    rows = ev.query("access", kind="access.system_reads_summary")
    assert len(rows) == 1 and rows[0]["payload"]["count"] == 0  # ILB — emits even zero
    maint.flush_suppressed_if_new_day(now="2026-07-17T01:00:00+00:00")  # new day
    assert len(ev.query("access", kind="access.system_reads_summary")) == 2


# --- post-attest-edit scan --------------------------------------------------

def _edit_body_out_of_band(vault, rel, extra):
    p = vault / rel
    post = frontmatter.load(str(p))
    post.content = post.content + "\n" + extra
    with open(p, "w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post))


def test_post_attest_edit_detected_and_latched(tmp_path):
    ev = _events(tmp_path)
    vault, rel = _attested_note(tmp_path, ev, source_id="enc-edit00000001")
    maint = ScribeEventMaintenance(ev)
    # clean attested note → no mismatch.
    assert maint.post_attest_edit_scan(vault, full=True) == []
    assert ev.query("clinical", kind="note.post_attest_edit_detected") == []

    # an out-of-band edit of the SIGNED note → detected + emitted.
    _edit_body_out_of_band(vault, rel, "SNEAKY POST-ATTEST EDIT")
    edits = maint.post_attest_edit_scan(vault, full=True)
    assert len(edits) == 1 and edits[0]["subject_id"] == "enc-edit00000001"
    emitted = ev.query("clinical", kind="note.post_attest_edit_detected")
    assert len(emitted) == 1
    assert emitted[0]["payload"]["current_body_sha"] == edits[0]["current_body_sha"]

    # latched — a second scan at the same sha does NOT re-emit.
    assert maint.post_attest_edit_scan(vault, full=True) == []
    assert len(ev.query("clinical", kind="note.post_attest_edit_detected")) == 1


def test_post_attest_scan_hot_window_bounds_per_sweep(tmp_path):
    ev = _events(tmp_path)
    # attested 100 days ago + the note file backdated → OUTSIDE the 30-day hot window.
    old = datetime(2026, 4, 1, 12, tzinfo=timezone.utc)
    vault, rel = _attested_note(tmp_path, ev, source_id="enc-old000000001", now=old)
    _edit_body_out_of_band(vault, rel, "EDIT ON A COLD NOTE")
    old_epoch = old.timestamp()
    os.utime(vault / rel, (old_epoch, old_epoch))  # backdate mtime too

    maint = ScribeEventMaintenance(ev)
    now_iso = "2026-07-16T12:00:00+00:00"
    # per-sweep (bounded) SKIPS a cold encounter (attested-long-ago + old mtime).
    assert maint.post_attest_edit_scan(vault, now=now_iso) == []
    # the full scan (boot / verify --deep) still catches it.
    assert len(maint.post_attest_edit_scan(vault, full=True, now=now_iso)) == 1


def test_post_attest_scan_inactive_facade_noop(tmp_path):
    ev = _events(tmp_path)
    ev._active = False
    maint = ScribeEventMaintenance(ev)
    assert maint.post_attest_edit_scan(tmp_path / "vault", full=True) == []
    assert maint.heartbeat_if_due(now="2026-07-16T12:00:00+00:00") is None
