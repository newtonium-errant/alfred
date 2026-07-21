"""s.50 retention SCHEDULE artifact contract pins (task #13 slice 13c — design §4/§10/§11).

Contract-first. The schedule is a versioned, fail-closed-validated JSON document whose sha is pinned
into the clinical chain; these pins bind: validation (missing/extra class, bad window/date/minor
rule), publish atomicity + the exact-bytes sha, fail-closed load, class-window lookup incl.
never-pruned classes, the operator-confirmed v1 numbers, and the bundled-example drift pin.
"""
from __future__ import annotations

import json

import pytest

from alfred._data import get_retention_schedule_example
from alfred.scribe.schedule import (
    SCHEDULE_CLASSES, ScheduleError, canonical_schedule_bytes, class_window_days,
    default_schedule_v1, load_schedule, publish_schedule, schedule_sha256, validate_schedule,
)


def _valid() -> dict:
    return default_schedule_v1()


# ============================ validation ============================


def test_default_v1_validates():
    assert validate_schedule(_valid()) is not None


def test_validate_rejects_non_dict():
    with pytest.raises(ScheduleError):
        validate_schedule(["not", "a", "dict"])


def test_validate_rejects_missing_class():
    data = _valid()
    del data["classes"]["clinical_note"]
    with pytest.raises(ScheduleError):
        validate_schedule(data)


def test_validate_rejects_extra_class():
    data = _valid()
    data["classes"]["sneaky_new_class"] = {"window_days": 1, "basis": "drift"}
    with pytest.raises(ScheduleError):
        validate_schedule(data)


@pytest.mark.parametrize("bad_window", [-1, "3650", 3.5, True])
def test_validate_rejects_bad_window(bad_window):
    data = _valid()
    data["classes"]["clinical_note"]["window_days"] = bad_window
    with pytest.raises(ScheduleError):
        validate_schedule(data)


def test_validate_accepts_null_window_never_pruned():
    data = _valid()
    data["classes"]["clinical_note"]["window_days"] = None   # never-pruned is valid
    assert validate_schedule(data) is not None


def test_validate_rejects_absent_window_days_key():
    # C1: an ABSENT window_days key (a typo like 'window_day') is an incomplete spec, NOT the
    # never-pruned sentinel — it must fail validation (only explicit null means never-pruned), else a
    # fat-fingered publish silently disables 10-yr PHI surfacing.
    data = _valid()
    del data["classes"]["encounter_audio_sealed"]["window_days"]
    data["classes"]["encounter_audio_sealed"]["window_day"] = 3650   # typo'd key
    with pytest.raises(ScheduleError):
        validate_schedule(data)


@pytest.mark.parametrize("bad_version", ["", "  ", None, 5])
def test_validate_rejects_bad_version(bad_version):
    data = _valid()
    data["schedule_version"] = bad_version
    with pytest.raises(ScheduleError):
        validate_schedule(data)


@pytest.mark.parametrize("bad_date", ["", "not-a-date", "2026-13-40", None])
def test_validate_rejects_bad_effective_date(bad_date):
    data = _valid()
    data["effective_date"] = bad_date
    with pytest.raises(ScheduleError):
        validate_schedule(data)


def test_validate_rejects_bad_minor_rule():
    data = _valid()
    data["minor_rule"]["majority_age"] = "nineteen"
    with pytest.raises(ScheduleError):
        validate_schedule(data)


# ============================ publish (atomicity + sha pin) ============================


def test_publish_writes_canonical_bytes_and_pins_exact_sha(tmp_path):
    dest = tmp_path / "seal" / "retention_schedule.json"
    data = _valid()
    pinned = publish_schedule(dest, data)
    assert dest.is_file()
    # the returned sha is over EXACTLY the on-disk bytes (design §10 — the sha pins the exact bytes)
    on_disk = dest.read_bytes()
    assert on_disk == canonical_schedule_bytes(data)
    from alfred.evstore import sha256_hex
    assert pinned["schedule_sha256"] == sha256_hex(on_disk) == schedule_sha256(data)
    assert pinned["schedule_version"] == "v1"
    assert pinned["effective_date"] == "2026-07-19"
    # atomic: no .tmp residue
    assert not dest.with_name(dest.name + ".tmp").exists()


def test_publish_refuses_malformed_writes_nothing(tmp_path):
    dest = tmp_path / "seal" / "retention_schedule.json"
    data = _valid()
    del data["classes"]["consent_events"]              # malformed → REFUSE
    with pytest.raises(ScheduleError):
        publish_schedule(dest, data)
    assert not dest.exists()                            # nothing published (fail-closed)


# ============================ fail-closed load ============================


def test_load_absent_is_none(tmp_path):
    assert load_schedule(tmp_path / "nope.json") is None


def test_load_malformed_json_is_none(tmp_path):
    p = tmp_path / "sched.json"
    p.write_text('{"schedule_version": "v1", ', encoding="utf-8")   # torn JSON
    assert load_schedule(p) is None


def test_load_structurally_invalid_is_none(tmp_path):
    p = tmp_path / "sched.json"
    data = _valid()
    del data["classes"]["clinical_note"]                            # valid JSON, invalid schedule
    p.write_text(json.dumps(data), encoding="utf-8")
    assert load_schedule(p) is None                                 # fail-closed (never raises)


def test_load_valid_round_trips(tmp_path):
    dest = tmp_path / "sched.json"
    publish_schedule(dest, _valid())
    loaded = load_schedule(dest)
    assert loaded is not None and loaded["schedule_version"] == "v1"


# ============================ class-window lookup ============================


def test_class_window_days_clinical_is_ten_years():
    sched = _valid()
    assert class_window_days(sched, "encounter_audio_sealed") == 3650
    assert class_window_days(sched, "clinical_note") == 3650
    assert class_window_days(sched, "transcript_ledger") == 3650


def test_class_window_days_diarize_is_180():
    assert class_window_days(_valid(), "diarize_stats") == 180


def test_class_window_days_bug_reports_is_one_year():
    assert class_window_days(_valid(), "bug_reports") == 365


@pytest.mark.parametrize("never_pruned", [
    "consent_events", "audit_access_log", "retention_events", "voice_presets"])
def test_class_window_days_never_pruned_is_none(never_pruned):
    assert class_window_days(_valid(), never_pruned) is None


def test_class_window_days_unknown_class_is_none():
    assert class_window_days(_valid(), "not_a_class") is None


# ============================ operator-confirmed content + drift pin ============================


def test_v1_has_exactly_the_nine_classes():
    assert set(_valid()["classes"]) == set(SCHEDULE_CLASSES)
    assert len(SCHEDULE_CLASSES) == 9


def test_v1_minor_rule_is_age19_plus_10(tmp_path):
    # design §11: retain to age-of-majority (NS=19) + 10yr, whichever is longer than the adult window.
    assert _valid()["minor_rule"] == {"majority_age": 19, "post_majority_years": 10}


def test_v1_audit_log_floor_is_one_year():
    # NS Reg s.11(3) ≥ 1yr floor on the audit-access-log class.
    assert _valid()["classes"]["audit_access_log"]["floor_days"] == 365


def test_bundled_example_matches_default_v1_no_drift():
    # the shipped example the operator copies must be the canonical bytes of default_schedule_v1 — a
    # drift between the code source-of-truth and the bundled file trips here.
    example = get_retention_schedule_example()
    assert example.is_file()
    assert example.read_bytes() == canonical_schedule_bytes(default_schedule_v1())
    # and it round-trips through the fail-closed loader
    assert load_schedule(example) is not None
