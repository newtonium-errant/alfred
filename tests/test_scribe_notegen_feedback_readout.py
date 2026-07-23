"""#14 slice 14e-i — the note-gen feedback READ surfaces (Part A status + Part B raw diff).

Pins: the PHI-free aggregate (per-section edits, median net_word_delta, FP ranking by kept-rate,
high_modification-by-source_id), the ILB empty-sink readout, the Part-B raw-diff recompute, and — the
LOAD-BEARING one — that the Part-B PHI diff reaches STDOUT ONLY (never a log / audit / mutation / file
path). Report-only (no auto-mutation). Regression pins UNCONDITIONAL.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest
import structlog
import yaml

from alfred.cli import build_parser, cmd_scribe
from alfred.scribe import enroll_learning as el
from alfred.scribe import notegen_feedback as nf
from alfred.sovereign.boundary import CLOUD_KEY_ENV_VARS
from alfred.vault.ops import vault_create


@pytest.fixture(autouse=True)
def _scrub_cloud_env(monkeypatch):
    for k in CLOUD_KEY_ENV_VARS:
        monkeypatch.delenv(k, raising=False)


def _row(source_id, **over):
    r = {
        "kind": "notegen_edit", "source_id": source_id, "template_id": "soap", "template_version": 1,
        "sections": {s: {} for s in ("subjective", "objective", "assessment", "plan")},
        "totals": {"net_word_delta": 0}, "flag_survival": {}, "high_modification": False,
    }
    r.update(over)
    return r


def _seed(enroll, *rows):
    for r in rows:
        el.record_notegen_edit(str(enroll), row=r)


# ===========================================================================
# Part A — read + aggregate
# ===========================================================================

def test_read_rows_filters_kind_and_tolerates_absent(tmp_path):
    enroll = tmp_path / "enroll"
    assert nf.read_notegen_edit_rows(enroll) == []          # absent sink → []
    assert nf.read_notegen_edit_rows("") == []              # dormant → []
    el.record_notegen_edit(str(enroll), row=_row("enc-a"))
    el.record_attest_outcome(str(enroll), source_id="enc-a", user=None, preset_id=None,
                             centroid_version=None, reason="negation_mismatch", kept=True)  # other kind
    rows = nf.read_notegen_edit_rows(enroll)
    assert len(rows) == 1 and rows[0]["kind"] == "notegen_edit"   # only notegen_edit rows


def test_aggregate_empty_is_ilb():
    agg = nf.aggregate_feedback([])
    assert agg["attests"] == 0 and agg["fp_ranking"] == [] and agg["high_modification_source_ids"] == []


def test_aggregate_totals_median_ranking_highmod():
    rows = [
        _row("enc-a", sections={"subjective": {"claims_modified": 1, "claims_kept_verbatim": 1},
                                "objective": {}, "assessment": {}, "plan": {"claims_added": 1}},
             totals={"net_word_delta": -4},
             flag_survival={"negation_mismatch": {"removed": 0, "kept": 3}}, high_modification=True),
        _row("enc-b", sections={s: {} for s in ("subjective", "objective", "assessment", "plan")},
             totals={"net_word_delta": 6},
             flag_survival={"negation_mismatch": {"removed": 1, "kept": 0},
                            "number_mismatch": {"removed": 2, "kept": 0}}, high_modification=False),
    ]
    agg = nf.aggregate_feedback(rows)
    assert agg["attests"] == 2
    assert agg["median_net_word_delta"] == 1.0             # median(-4, 6)
    assert agg["sections"]["subjective"]["claims_modified"] == 1
    assert agg["sections"]["plan"]["claims_added"] == 1
    assert agg["high_modification_source_ids"] == ["enc-a"]
    # FP ranking: negation_mismatch kept 3/4 (0.75) ranks ABOVE number_mismatch 0/2 (0.0)
    assert agg["fp_ranking"][0]["reason"] == "negation_mismatch"
    assert agg["fp_ranking"][0]["kept"] == 3 and agg["fp_ranking"][0]["kept_rate"] == 0.75
    assert agg["fp_ranking"][1]["reason"] == "number_mismatch"


def test_aggregate_is_phi_free():
    # The aggregate carries counts / enums / OPAQUE ids ONLY — no claim/cite text. (The "claims_*" keys
    # are PHI-FREE count fields; the pin checks VALUES: opaque source_ids + reason enums, never a
    # sentence, and the top-level structure is the known closed set.)
    agg = nf.aggregate_feedback([_row("enc-a", high_modification=True,
                                      flag_survival={"negation_mismatch": {"removed": 1, "kept": 0}})])
    assert set(agg) == {"attests", "sections", "median_net_word_delta", "flag_survival",
                        "fp_ranking", "high_modification_source_ids"}
    assert agg["high_modification_source_ids"] == ["enc-a"]         # opaque encounter id only
    assert all(r["reason"] == "negation_mismatch" for r in agg["fp_ranking"])   # reason ENUM only
    assert set(agg["flag_survival"]) == {"negation_mismatch"}       # keys are reason enums
    # every section leaf is an int count (no text)
    for sec in agg["sections"].values():
        assert all(isinstance(v, int) for v in sec.values())


# ===========================================================================
# Part B — recompute_raw_diff
# ===========================================================================

def _make_note(vault, source_id, draft, body):
    vault_create(vault, "clinical_note", "Enc", set_fields={
        "ai_draft": True, "synthetic": True, "status": "ai_draft",
        "source_id": source_id, "drafted_by": "stayc_scribe", "draft_original": draft,
    }, body=body, scope="stayc_clinical")


def test_recompute_diff_produces_unified_diff(tmp_path):
    vault = tmp_path / "vault"
    _make_note(vault, "enc-x", "## Subjective\n- Chest pain for two days\n", "## Subjective\n- Chest pain 2 days\n")
    diff = nf.recompute_raw_diff(vault, "enc-x")
    assert "--- draft_original" in diff and "+++ attested_body" in diff
    assert "-- Chest pain for two days" in diff and "+- Chest pain 2 days" in diff


def test_recompute_diff_no_match_is_none(tmp_path):
    assert nf.recompute_raw_diff(tmp_path / "vault", "enc-nope") is None


def test_recompute_diff_identical_is_empty(tmp_path):
    vault = tmp_path / "vault"
    _make_note(vault, "enc-y", "## Subjective\n- Same\n", "## Subjective\n- Same\n")
    assert nf.recompute_raw_diff(vault, "enc-y") == ""      # draft == attested → no diff


# ===========================================================================
# CLI + the LOAD-BEARING stdout-only PHI containment
# ===========================================================================

def _cfg(tmp_path):
    cfg = {"vault": {"path": str(tmp_path / "vault")}, "logging": {"dir": str(tmp_path / "data")},
           "scribe": {"mode": "clinical", "encounter_salt": "S", "input_dir": str(tmp_path / "inbox"),
                      "diarize": {"provider": "fake", "enrollment_dir": str(tmp_path / "enroll")}}}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return tmp_path / "config.yaml"


def _run(config, *argv):
    ns = build_parser().parse_args(["--config", str(config), "scribe", *argv])
    buf = io.StringIO()
    code = 0
    with redirect_stdout(buf):
        try:
            cmd_scribe(ns)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return code, buf.getvalue()


def test_cli_status_ilb_empty(tmp_path):
    out = _run(_cfg(tmp_path), "notegen-feedback", "status")[1]
    assert "no correction signal yet" in out               # ILB — idle ≠ broken


def test_cli_status_populated_and_json(tmp_path):
    config = _cfg(tmp_path)
    _seed(tmp_path / "enroll", _row("enc-a", high_modification=True,
                                    flag_survival={"negation_mismatch": {"removed": 0, "kept": 2}}))
    out = _run(config, "notegen-feedback", "status")[1]
    assert "1 attest(s)" in out and "negation_mismatch" in out and "enc-a" in out
    js = json.loads(_run(config, "notegen-feedback", "status", "--json")[1])
    assert js["attests"] == 1 and js["high_modification_source_ids"] == ["enc-a"]


def test_cli_diff_prints_and_ilb(tmp_path):
    config = _cfg(tmp_path)
    _make_note(tmp_path / "vault", "enc-x", "## S\n- Chest pain for two days\n", "## S\n- Chest pain 2 days\n")
    out = _run(config, "notegen-feedback", "diff", "enc-x")[1]
    assert "Chest pain for two days" in out and "Chest pain 2 days" in out   # the raw diff on stdout
    assert "no clinical_note found" in _run(config, "notegen-feedback", "diff", "enc-none")[1].lower()  # ILB


def test_diff_phi_reaches_stdout_only_never_a_log(tmp_path):
    # LOAD-BEARING (the arc's one PHI surface): the raw diff CONTENT must reach STDOUT ONLY — never any
    # structlog event, never any file under logging.dir (log / audit / mutation trail). Run the diff CLI
    # under log capture; assert the PHI text is in stdout but NOT in any captured log NOR any data file.
    config = _cfg(tmp_path)
    phi = "Chest pain for two days"
    _make_note(tmp_path / "vault", "enc-x", f"## S\n- {phi}\n", "## S\n- Chest pain 2 days\n")
    with structlog.testing.capture_logs() as cap:
        _, out = _run(config, "notegen-feedback", "diff", "enc-x")
    assert phi in out                                       # stdout HAS it
    assert phi not in json.dumps(cap)                       # NO structlog event carries it
    # NO file under logging.dir (data/) contains the PHI diff content.
    data_dir = tmp_path / "data"
    leaked = [p.name for p in data_dir.rglob("*") if p.is_file() and phi in p.read_text(errors="ignore")] \
        if data_dir.exists() else []
    assert leaked == [], f"PHI diff content leaked into {leaked}"
