"""#26 Phase 3 — PROPOSE + APPROVE + RETENTION + TIMELY-RELOAD contract tests.

Covers the 1a⋈1b join, the `negation-candidates` list + audited `negation-glossary
approve/reject` CLI, the PHI-free evstore decision audit, the destroy-with-encounter
row-prune + its completeness gate (the false-proof probe), the 90d age-cap, the
mtime-gated reload, and the count-only brief relay (the no-PHI-crossing probe).

Grounded @ ab15165 (Phase 1+2 merged). The detector (grounding.py) is UNCHANGED.
"""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from alfred.brief.config import StaycNegationRelayConfig
from alfred.brief.stayc_relay import render_stayc_negation_relay_section
from alfred.cli import build_parser, cmd_scribe
from alfred.scribe import negation_suppression as ns
from alfred.scribe.events import ScribeEvents
from alfred.scribe.grounding import _CITE_NEGATION_RE, _negated_concepts
from alfred.scribe.retention_sweep import RetentionSweep, RetentionSweepSummary
from alfred.sovereign.boundary import CLOUD_KEY_ENV_VARS

_SALT = "DUMMY_SCRIBE_TEST_SALT"
_CITE = "Your sugars haven't come down the way I'd hoped on the metformin."
_CLAIM = "Blood sugars not adequately controlled on metformin"


@pytest.fixture(autouse=True)
def _scrub_cloud_env(monkeypatch):
    for k in CLOUD_KEY_ENV_VARS:
        monkeypatch.delenv(k, raising=False)


def _empa_pair():
    claim = _negated_concepts(_CLAIM, _CITE_NEGATION_RE)[0]
    cite = _negated_concepts(_CITE, _CITE_NEGATION_RE)[0]
    return claim, cite


def _store(*pairs):
    return ns.NegationSuppression(pairs=tuple((frozenset(a), frozenset(b)) for a, b in pairs))


def _cand_dir(tmp_path):
    return tmp_path / "scribe"           # <input_dir parent>/scribe when input_dir = tmp/inbox


def _seed(tmp_path, *, source_id="enc-empa", claim=None, cite=None, kept=True,
          ts="2026-07-01T00:00:00+00:00", section="objective", claim_index=0):
    """Seed a matched 1a candidate + 1b attest row for one claim. Defaults = the empagliflozin sets."""
    cl, ci = _empa_pair()
    claim = claim if claim is not None else cl
    cite = cite if cite is not None else ci
    d = _cand_dir(tmp_path)
    ns._append_row(ns._candidates_file(d), {
        "kind": "candidate", "ts": ts, "source_id": source_id, "section": section,
        "claim_index": claim_index, "reason": "negation_mismatch",
        "claim_concepts": [sorted(claim)], "cite_concepts": [sorted(cite)], "disposition": "pending"})
    ns._append_row(ns._attest_outcomes_file(d), {
        "kind": "attest_outcome", "ts": ts, "source_id": source_id, "section": section,
        "claim_index": claim_index, "reason": "negation_mismatch", "kept": kept})
    return ns.candidate_id(source_id, section, claim_index)


def _write_config(tmp_path):
    cfg = {
        "vault": {"path": str(tmp_path / "vault")},
        "logging": {"dir": str(tmp_path / "data")},
        "scribe": {
            "mode": "clinical", "encounter_salt": _SALT,
            "input_dir": str(tmp_path / "inbox"),          # → candidates_dir = tmp/scribe
            "stt": {"provider": "fake"}, "clinicians": ["np_jamie"],
            "events": {"dir": str(tmp_path / "data" / "events")},
        },
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def _run(argv):
    """Drive cmd_scribe, capturing (exit_code, stdout)."""
    args = build_parser().parse_args(argv)
    buf = io.StringIO()
    code = 0
    with redirect_stdout(buf):
        try:
            cmd_scribe(args)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return code, buf.getvalue()


def _json(out):
    """Extract the CLI's json.dumps(indent=2) block from stdout — the event store logs (timestamped
    structlog console lines) precede it, so isolate the object starting at the first bare ``{`` line."""
    lines = out.splitlines()
    for i, ln in enumerate(lines):
        if ln == "{":
            return json.loads("\n".join(lines[i:]))
    raise AssertionError(f"no indent=2 JSON object in CLI output:\n{out}")


def _events(tmp_path):
    raw = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    log_dir = Path(raw["logging"]["dir"])
    return ScribeEvents.from_config(raw, log_dir, legacy_audit_path=log_dir / "clinical_attest_audit.jsonl")


# ===========================================================================
# candidate_id + JOIN (1a⋈1b)
# ===========================================================================

def test_candidate_id_deterministic_phi_free():
    a = ns.candidate_id("enc-x", "objective", 0)
    assert a == ns.candidate_id("enc-x", "objective", 0)         # deterministic
    assert a.startswith("npc-") and "enc-x" not in a            # opaque, no source_id leak


def test_join_review_ready_kept_true_only(tmp_path):
    _seed(tmp_path, source_id="enc-a", kept=True)
    _seed(tmp_path, source_id="enc-b", kept=False)
    rr = ns.join_review_ready(_cand_dir(tmp_path))
    ids = {c.source_id for c in rr}
    assert ids == {"enc-a"}                                      # kept=False → not review-ready


def test_join_requires_attest_outcome(tmp_path):
    # a candidate row with NO attest outcome → not review-ready (never signed)
    d = _cand_dir(tmp_path)
    cl, ci = _empa_pair()
    ns._append_row(ns._candidates_file(d), {
        "kind": "candidate", "ts": "2026-07-01T00:00:00+00:00", "source_id": "enc-noattest",
        "section": "objective", "claim_index": 0, "claim_concepts": [sorted(cl)],
        "cite_concepts": [sorted(ci)], "disposition": "pending"})
    assert ns.join_review_ready(d) == []


def test_join_dedupes_checkpoint_duplicates(tmp_path):
    _seed(tmp_path, source_id="enc-a", ts="2026-07-01T00:00:00+00:00")
    _seed(tmp_path, source_id="enc-a", ts="2026-07-02T00:00:00+00:00")   # a later checkpoint dup
    rr = ns.join_review_ready(_cand_dir(tmp_path))
    assert len(rr) == 1                                          # deduped by (source_id, section, idx)


def test_join_excludes_decided(tmp_path):
    cid = _seed(tmp_path, source_id="enc-a")
    assert len(ns.join_review_ready(_cand_dir(tmp_path))) == 1
    assert ns.join_review_ready(_cand_dir(tmp_path), {cid}) == []   # decided → excluded
    assert ns.count_pending(_cand_dir(tmp_path), {cid}) == 0


# ===========================================================================
# PRUNE — destroy-with-encounter + age-cap + completeness
# ===========================================================================

def test_prune_by_source_removes_both_sinks_others_survive(tmp_path):
    _seed(tmp_path, source_id="enc-x")
    _seed(tmp_path, source_id="enc-y")
    removed = ns.prune_candidates_for_source(_cand_dir(tmp_path), "enc-x")
    assert removed == 2                                          # 1 candidate + 1 attest row
    assert ns.count_rows_for_source(_cand_dir(tmp_path), "enc-x") == 0   # completeness verifier == 0
    assert ns.count_rows_for_source(_cand_dir(tmp_path), "enc-y") == 2   # unrelated encounter survives


def test_age_cap_drops_old_keeps_recent(tmp_path):
    _seed(tmp_path, source_id="enc-old", ts="2026-01-01T00:00:00+00:00")
    _seed(tmp_path, source_id="enc-new", ts="2026-07-20T00:00:00+00:00")
    removed = ns.prune_candidates_by_age(_cand_dir(tmp_path), "2026-06-01T00:00:00+00:00")
    assert removed == 2                                          # only enc-old's two rows
    assert ns.count_rows_for_source(_cand_dir(tmp_path), "enc-new") == 2


def test_age_cap_drops_undateable_row_phi_bearing_posture(tmp_path):
    # PHI-BEARING sink → an un-dateable row is DROPPED (fail-safe toward not-retaining-PHI), the
    # OPPOSITE of the PHI-free diarize sink which preserves undateable rows.
    d = _cand_dir(tmp_path)
    ns._append_row(ns._candidates_file(d), {"kind": "candidate", "source_id": "enc-nots",
                                            "section": "s", "claim_index": 0, "claim_concepts": [["a"]],
                                            "cite_concepts": [["b"]]})   # NO ts
    removed = ns.prune_candidates_by_age(d, "2026-06-01T00:00:00+00:00")
    assert removed == 1 and ns.count_rows_for_source(d, "enc-nots") == 0


# ===========================================================================
# GLOSSARY write + reload
# ===========================================================================

def test_append_approved_pair_writes_0600_bumps_revision(tmp_path):
    gp = tmp_path / "g.json"
    cl, ci = _empa_pair()
    rev = ns.append_approved_pair(gp, candidate_id="npc-1", claim_concept=list(cl),
                                  cite_concept=list(ci), approved_by="andrew",
                                  approved_at="2026-07-23T00:00:00+00:00", dropped_count=0)
    assert rev == 1 and (os.stat(gp).st_mode & 0o777) == 0o600
    data = json.loads(gp.read_text())
    assert data["version"] == 1 and data["revision"] == 1
    entry = data["pairs"][0]
    assert entry["id"] == "npc-1"                                # provenance = candidate_id (NOT source_id)
    assert set(entry["claim_concept"]) == set(cl) and set(entry["cite_concept"]) == set(ci)
    # the appended pair feeds the detector
    assert ns.load_suppression(gp).suppresses(set(cl), [set(ci)]) is True


def test_append_approved_pair_idempotent(tmp_path):
    gp = tmp_path / "g.json"
    ns.append_approved_pair(gp, candidate_id="npc-1", claim_concept=["a"], cite_concept=["b"],
                            approved_by="x", approved_at="t")
    rev2 = ns.append_approved_pair(gp, candidate_id="npc-1", claim_concept=["a"], cite_concept=["b"],
                                   approved_by="x", approved_at="t")
    data = json.loads(gp.read_text())
    assert rev2 == 1 and len(data["pairs"]) == 1                 # double-approve → no duplicate pair


def test_maybe_reload_mtime_gated(tmp_path):
    gp = tmp_path / "g.json"
    store0 = ns.load_suppression(gp)                             # absent → empty
    mt0 = ns.glossary_mtime(gp)
    s1, mt1 = ns.maybe_reload_suppression(store0, gp, mt0)
    assert s1 is store0 and mt1 == mt0                           # unchanged (still absent) → same object
    ns.append_approved_pair(gp, candidate_id="npc-1", claim_concept=["a"], cite_concept=["b"],
                            approved_by="x", approved_at="t")
    import structlog
    with structlog.testing.capture_logs() as cap:
        s2, mt2 = ns.maybe_reload_suppression(s1, gp, mt1)
    assert mt2 != mt1 and len(s2.pairs) == 1                     # file appeared → reloaded
    # log-emission pin (discipline #9): the operator-facing 'reloaded' signal fires with the pair count
    hits = [e for e in cap if e.get("event") == "scribe.negation_suppression.reloaded"]
    assert len(hits) == 1 and hits[0]["pairs"] == 1


# ===========================================================================
# evstore decision audit (PHI-free)
# ===========================================================================

def test_negation_events_phi_free_and_decided_ids(tmp_path):
    _write_config(tmp_path)
    ev = _events(tmp_path)
    ev.negation_approved(candidate_id="npc-1", operator="andrew", glossary_version=3, dropped_count=1)
    ev.negation_rejected(candidate_id="npc-2", operator="andrew")
    assert ev.negation_decided_ids() == {"npc-1", "npc-2"}
    row = ev.query("clinical", family="learning", kind="negation.approved")[0]
    # PHI-FREE: subject is the candidate HASH; payload is only scalars — no concept-set / token
    assert row["subject_id"] == "npc-1" and row["actor"] == "andrew"
    assert row["payload"] == {"glossary_version": 3, "dropped_count": 1}


# ===========================================================================
# CLI — negation-candidates list
# ===========================================================================

def test_cli_list_empty_ilb(tmp_path):
    _write_config(tmp_path)
    code, out = _run(["--config", str(tmp_path / "config.yaml"), "scribe", "negation-candidates"])
    assert code == 0 and "No negation-paraphrase candidates awaiting review." in out   # ILB


def test_cli_list_shows_concept_sets_not_raw_text(tmp_path):
    _write_config(tmp_path)
    cid = _seed(tmp_path, source_id="enc-empa")
    code, out = _run(["--config", str(tmp_path / "config.yaml"), "scribe", "negation-candidates"])
    assert code == 0 and cid in out
    assert "metformin" in out and "controlled" in out           # concept tokens rendered
    assert _CLAIM not in out and "come down the way" not in out  # NEVER the raw sentence


def test_cli_list_json(tmp_path):
    _write_config(tmp_path)
    cid = _seed(tmp_path, source_id="enc-empa")
    code, out = _run(["--config", str(tmp_path / "config.yaml"), "scribe", "negation-candidates", "--json"])
    data = _json(out)
    assert data["pending"] == 1 and data["candidates"][0]["candidate_id"] == cid


# ===========================================================================
# CLI — approve / reject (audited)
# ===========================================================================

def _approve(tmp_path, cid, *extra):
    return _run(["--config", str(tmp_path / "config.yaml"), "scribe", "negation-glossary",
                 "approve", cid, "--operator", "andrew", *extra])


def test_cli_approve_writes_glossary_audit_and_suppresses(tmp_path):
    _write_config(tmp_path)
    cid = _seed(tmp_path, source_id="enc-empa")
    code, out = _approve(tmp_path, cid)
    assert code == 0 and _json(out)["approved"] == cid
    # glossary now suppresses the empagliflozin pair
    cl, ci = _empa_pair()
    store = ns.load_suppression(_cand_dir(tmp_path) / ns.NEGATION_GLOSSARY_NAME)
    assert store.suppresses(set(cl), [set(ci)]) is True
    # a durable PHI-free negation.approved landed
    assert cid in _events(tmp_path).negation_decided_ids()
    # and the candidate leaves the review list (decided)
    _, out2 = _run(["--config", str(tmp_path / "config.yaml"), "scribe", "negation-candidates"])
    assert "No negation-paraphrase candidates" in out2


def test_cli_approve_already_decided_is_noop(tmp_path):
    _write_config(tmp_path)
    cid = _seed(tmp_path, source_id="enc-empa")
    assert _approve(tmp_path, cid)[0] == 0
    code, out = _approve(tmp_path, cid)
    assert code == 1 and "already decided" in out


def test_cli_approve_not_review_ready_errors(tmp_path):
    _write_config(tmp_path)
    code, out = _approve(tmp_path, "npc-doesnotexist")
    assert code == 1 and "not review-ready" in out


def test_cli_approve_multi_concept_requires_pair(tmp_path):
    # a candidate with 2 claim concepts + 1 cite → approve MUST refuse without --pair (never cross-product)
    _write_config(tmp_path)
    d = _cand_dir(tmp_path)
    ns._append_row(ns._candidates_file(d), {
        "kind": "candidate", "ts": "2026-07-01T00:00:00+00:00", "source_id": "enc-multi",
        "section": "objective", "claim_index": 0, "claim_concepts": [["a", "b"], ["c", "d"]],
        "cite_concepts": [["e", "f"]], "disposition": "pending"})
    ns._append_row(ns._attest_outcomes_file(d), {
        "kind": "attest_outcome", "ts": "2026-07-01T00:00:00+00:00", "source_id": "enc-multi",
        "section": "objective", "claim_index": 0, "reason": "negation_mismatch", "kept": True})
    cid = ns.candidate_id("enc-multi", "objective", 0)
    code, out = _approve(tmp_path, cid)
    assert code == 1 and "--pair" in out                        # refused, enumerated
    # with --pair it approves the selected concept only
    code2, out2 = _approve(tmp_path, cid, "--pair", "1:0")
    assert code2 == 0
    store = ns.load_suppression(_cand_dir(tmp_path) / ns.NEGATION_GLOSSARY_NAME)
    assert store.suppresses({"c", "d"}, [{"e", "f"}]) is True   # the picked pair
    assert store.suppresses({"a", "b"}, [{"e", "f"}]) is False  # the un-picked one never stored


def test_cli_approve_drop_removes_tokens_and_counts(tmp_path):
    _write_config(tmp_path)
    cid = _seed(tmp_path, source_id="enc-empa")
    code, out = _approve(tmp_path, cid, "--drop", "metformin")
    assert code == 0
    res = _json(out)
    assert res["dropped_count"] == 2 and "metformin" not in res["claim_concept"]  # dropped from both sides
    # the audit carries the COUNT, never the token string
    row = _events(tmp_path).query("clinical", family="learning", kind="negation.approved")[0]
    assert row["payload"]["dropped_count"] == 2
    assert "metformin" not in json.dumps(row)                   # no token string anywhere in the event


def test_approve_crash_between_glossary_write_and_audit_self_heals(tmp_path, monkeypatch):
    # LOAD-BEARING (heavy-gate probe): approve order is glossary-write THEN audit. The self-heal
    # claim holds ONLY because `decided` is derived from the AUDIT EVENT CHAIN, never from the glossary
    # pairs. Simulate a crash AFTER the glossary write but BEFORE the audit → the pair is in the
    # glossary but there is NO negation.approved event → the candidate must STILL surface (NOT a
    # permanent, unrecorded detector mutation) → a re-run completes the audit idempotently. If `decided`
    # were glossary-derived, the candidate would be marked decided with no chain record — a med-legal BLOCK.
    _write_config(tmp_path)
    cid = _seed(tmp_path, source_id="enc-empa")
    calls = {"n": 0}
    real = ScribeEvents.negation_approved

    def _flaky(self, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash after glossary write, before audit")
        return real(self, **kw)
    monkeypatch.setattr(ScribeEvents, "negation_approved", _flaky)

    gp = _cand_dir(tmp_path) / ns.NEGATION_GLOSSARY_NAME
    # 1st approve: glossary written, audit crashes → fail-loud, exit 1
    code1, out1 = _approve(tmp_path, cid)
    assert code1 == 1 and "audit FAILED" in out1
    assert len(json.loads(gp.read_text())["pairs"]) == 1              # the pair IS on disk
    decided = _events(tmp_path).negation_decided_ids()
    assert cid not in decided                                         # but NOT decided (event-derived)
    # → it STILL surfaces for review (no silent unrecorded mutation)
    assert cid in {c.candidate_id for c in ns.join_review_ready(_cand_dir(tmp_path), decided)}
    # 2nd approve (re-run): audit succeeds, glossary append is idempotent → now decided, no dup pair
    code2, out2 = _approve(tmp_path, cid)
    assert code2 == 0
    assert len(json.loads(gp.read_text())["pairs"]) == 1             # STILL exactly one pair
    assert cid in _events(tmp_path).negation_decided_ids()            # NOW recorded in the chain


def test_cli_reject_audits_and_removes_from_list(tmp_path):
    _write_config(tmp_path)
    cid = _seed(tmp_path, source_id="enc-empa")
    code, out = _run(["--config", str(tmp_path / "config.yaml"), "scribe", "negation-glossary",
                      "reject", cid, "--operator", "andrew"])
    assert code == 0 and _json(out)["rejected"] == cid
    assert cid in _events(tmp_path).negation_decided_ids()
    # no glossary pair written on reject
    assert not (_cand_dir(tmp_path) / ns.NEGATION_GLOSSARY_NAME).exists()
    _, out2 = _run(["--config", str(tmp_path / "config.yaml"), "scribe", "negation-candidates"])
    assert "No negation-paraphrase candidates" in out2


# ===========================================================================
# DESTROY integration — probe (i): destroyed can NEVER stand with a surviving row
# ===========================================================================

def _destroy_argv(tmp_path, enc, *extra):
    return ["--config", str(tmp_path / "config.yaml"), "scribe", "retention", "destroy", enc,
            "--reason", "patient_request", "--ticket", "T-1", *extra]


def test_destroy_dry_run_enumerates_candidate_rows(tmp_path):
    _write_config(tmp_path)
    _seed(tmp_path, source_id="enc-empa")
    code, out = _run(_destroy_argv(tmp_path, "enc-empa", "--dry-run"))
    assert _json(out)["negation_candidate_rows"] == 2      # the hook sees the derived-PHI rows


def test_destroy_prunes_rows_and_emits_destroyed(tmp_path, monkeypatch):
    # POSITIVE path: with the backup purge satisfied, a real destroy PRUNES the candidate rows and
    # emits retention.destroyed.
    from alfred.scribe import backup as backup_mod
    monkeypatch.setattr(backup_mod, "purge_encounter",
                        lambda *a, **k: backup_mod.PurgeResult(complete=True, encounter_id="enc-empa"))
    _write_config(tmp_path)
    _seed(tmp_path, source_id="enc-empa")
    code, out = _run(_destroy_argv(tmp_path, "enc-empa", "--yes"))
    assert ns.count_rows_for_source(_cand_dir(tmp_path), "enc-empa") == 0    # rows gone
    assert _events(tmp_path).retention_destroyed_row("enc-empa") is not None  # destroyed emitted


def test_destroy_blocks_destroyed_when_a_candidate_row_survives(tmp_path, monkeypatch):
    # PROBE (i) — the false-proof class: if the candidate prune leaves a derived-PHI row, retention.destroyed
    # MUST NOT be emitted (a destruction leaving PHI is NOT 'destroyed'). Isolate the gate: satisfy the
    # backup purge, then NO-OP the candidate prune so a row survives → destroyed must be blocked.
    from alfred.scribe import backup as backup_mod
    monkeypatch.setattr(backup_mod, "purge_encounter",
                        lambda *a, **k: backup_mod.PurgeResult(complete=True, encounter_id="enc-empa"))
    monkeypatch.setattr(ns, "prune_candidates_for_source", lambda *a, **k: 0)   # prune fails to remove
    _write_config(tmp_path)
    _seed(tmp_path, source_id="enc-empa")
    code, out = _run(_destroy_argv(tmp_path, "enc-empa", "--yes"))
    assert code == 1 and "INCOMPLETE" in out
    assert "negation candidate spool" in out                    # the surviving row is named as the blocker
    assert _events(tmp_path).retention_destroyed_row("enc-empa") is None   # NO false proof-of-destruction


# ===========================================================================
# RETENTION SWEEP — age-cap + PHI-free count spool (probe ii: no concept crosses)
# ===========================================================================

def _sweep(tmp_path, *, review_spool):
    from alfred.scribe.config import load_from_unified
    raw = {"scribe": {"mode": "clinical", "encounter_salt": _SALT,
                      "input_dir": str(tmp_path / "inbox"),
                      "events": {"dir": str(tmp_path / "ev")},
                      "retention": {"review_spool_path": str(review_spool)}}}
    cfg = load_from_unified(raw)
    ev = ScribeEvents.from_config(raw, log_dir=str(tmp_path / "logs"),
                                  legacy_audit_path=tmp_path / "logs" / "a.jsonl")
    return RetentionSweep(cfg, ev)


def test_sweep_age_caps_old_candidates(tmp_path):
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    _seed(tmp_path, source_id="enc-old", ts=old)
    _seed(tmp_path, source_id="enc-new", ts=datetime.now(timezone.utc).isoformat())
    sweep = _sweep(tmp_path, review_spool=tmp_path / "review.spool")
    dropped = sweep._prune_negation_candidates(datetime.now(timezone.utc))
    assert dropped == 2                                         # enc-old's rows (200d > 90d cap)
    assert ns.count_rows_for_source(_cand_dir(tmp_path), "enc-new") == 2


def test_sweep_count_spool_is_phi_free(tmp_path):
    # PROBE (ii) — the count relay carries the COUNT only, never a concept-set / token.
    _seed(tmp_path, source_id="enc-empa")                       # concept token "metformin" in the spool
    review_spool = tmp_path / "review.spool"
    sweep = _sweep(tmp_path, review_spool=review_spool)
    summary = RetentionSweepSummary()
    sweep._write_negation_review_spool(datetime.now(timezone.utc), summary)
    neg_spool = review_spool.with_name(ns.NEGATION_REVIEW_SPOOL_NAME)
    text = neg_spool.read_text(encoding="utf-8")
    assert "pending: 1" in text and "generated_at:" in text
    assert "metformin" not in text and "controlled" not in text  # NO concept token crosses the relay
    assert summary.pending_negation_candidates == 1


def test_sweep_count_spool_gated_on_review_spool_path(tmp_path):
    _seed(tmp_path, source_id="enc-empa")
    from alfred.scribe.config import load_from_unified
    raw = {"scribe": {"mode": "clinical", "encounter_salt": _SALT,
                      "input_dir": str(tmp_path / "inbox"), "events": {"dir": str(tmp_path / "ev")}}}
    cfg = load_from_unified(raw)
    ev = ScribeEvents.from_config(raw, log_dir=str(tmp_path / "logs"),
                                  legacy_audit_path=tmp_path / "logs" / "a.jsonl")
    RetentionSweep(cfg, ev)._write_negation_review_spool(datetime.now(timezone.utc),
                                                         RetentionSweepSummary())
    assert not (_cand_dir(tmp_path) / ns.NEGATION_REVIEW_SPOOL_NAME).exists()   # unconfigured → no spool


# ===========================================================================
# BRIEF relay section — count-only, ILB, no PHI
# ===========================================================================

def _neg_cfg(spool, **kw):
    return StaycNegationRelayConfig(enabled=True, spool_path=str(spool), **kw)


def test_brief_relay_disabled_is_silent(tmp_path):
    cfg = StaycNegationRelayConfig(enabled=False, spool_path=str(tmp_path / "s.spool"))
    assert render_stayc_negation_relay_section(cfg, datetime.now(timezone.utc)) == ""


def test_brief_relay_zero_is_explicit_ilb(tmp_path):
    spool = tmp_path / "neg.spool"
    spool.write_text(f"generated_at: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
                     f"pending: 0\n", encoding="utf-8")
    line = render_stayc_negation_relay_section(_neg_cfg(spool), datetime.now(timezone.utc))
    assert "0 paraphrase candidates awaiting review" in line   # ILB — idle ≠ broken, not silent


def test_brief_relay_count_only_never_leaks_concept(tmp_path):
    spool = tmp_path / "neg.spool"
    spool.write_text(f"generated_at: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
                     f"pending: 3\n", encoding="utf-8")
    line = render_stayc_negation_relay_section(_neg_cfg(spool), datetime.now(timezone.utc))
    assert "3 paraphrase candidates awaiting review" in line


def test_brief_relay_absent_spool_is_visible(tmp_path):
    line = render_stayc_negation_relay_section(_neg_cfg(tmp_path / "nope.spool"),
                                               datetime.now(timezone.utc))
    assert "no data" in line and "not found" in line           # dead sweep is VISIBLE, not silent


def test_brief_relay_stale_is_visible(tmp_path):
    spool = tmp_path / "neg.spool"
    old = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
    spool.write_text(f"generated_at: {old}\npending: 2\n", encoding="utf-8")
    line = render_stayc_negation_relay_section(_neg_cfg(spool, staleness_hours=25.0),
                                               datetime.now(timezone.utc))
    assert "stale" in line
