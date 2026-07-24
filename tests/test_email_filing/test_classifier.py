"""#7 7c-i — the filing pass: rule→email_category write, LLM fallback, orthogonality, never-auto-mutate.

Pins (UNCONDITIONAL):
  * A rule match writes an ADDITIVE ``email_category`` frontmatter field (rule path, no LLM call).
  * The LLM fallback fires ONLY on no-rule-match, is constrained to the closed category set, and a
    non-category / invalid response writes nothing (ILB ``email_filing.no_category``).
  * ORTHOGONALITY: the filing write touches ONLY ``email_category`` — never priority/action_hint/
    priority_reasoning; a faulting filing pass never raises (fault-isolated from the priority axis).
  * NEVER-AUTO-MUTATE: categorization does not auto-flip the ``confidence.filing`` gate.
  * Log-emission pins (capture_logs) for the categorized + no_category events.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter
import pytest
import structlog

from alfred.email_filing import EmailFilingConfig, classify_filing_for_inbox

# A matching (rule) sender/subject and a non-matching one.
_RULE_INBOX = "# Your receipt\n\n**From:** billing@digitalocean.com\n**To:** me@x.com\n\n---\n\nReceipt body."
_NOMATCH_INBOX = "# Lunch?\n\n**From:** friend@gmail.com\n**To:** me@x.com\n\n---\n\nWant lunch tomorrow?"


@dataclass
class _FakeLLM:
    response: str = ""
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, system: str, user: str, config: EmailFilingConfig) -> str:
        self.calls.append({"system": system, "user": user})
        return self.response


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    for sub in ("note", "inbox"):
        (vault / sub).mkdir()
    return vault


def _seed_note(vault: Path, name: str, **extra_fm) -> str:
    fm = {"type": "note", "name": name, "description": "d", "created": "2026-07-23", "tags": [], "related": []}
    fm.update(extra_fm)
    post = frontmatter.Post(f"# {name}\n", **fm)
    (vault / "note" / f"{name}.md").write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return f"note/{name}.md"


def _cfg(**kw) -> EmailFilingConfig:
    c = EmailFilingConfig(enabled=True, **kw)
    c.anthropic.api_key = "DUMMY_ANTHROPIC_TEST_KEY"
    return c


def _read_fm(vault: Path, rel: str) -> dict:
    return frontmatter.load(str(vault / rel)).metadata


# ===========================================================================
# Rule path: additive email_category write, no LLM call
# ===========================================================================

def test_rule_match_writes_email_category(tmp_path):
    vault = _vault(tmp_path)
    rel = _seed_note(vault, "n1")
    llm = _FakeLLM(response='{"category": "Finance/Personal"}')  # should NOT be called on a rule match
    result = classify_filing_for_inbox(vault, _RULE_INBOX, [rel], _cfg(), llm_caller=llm)
    assert result.category == "Business/Receipts"
    assert result.source == "rule"
    assert result.written == [rel]
    assert _read_fm(vault, rel)["email_category"] == "Business/Receipts"
    assert llm.calls == []  # rule match short-circuits the LLM


def test_all_notes_from_one_email_get_the_same_category(tmp_path):
    vault = _vault(tmp_path)
    rels = [_seed_note(vault, "a"), _seed_note(vault, "b")]
    result = classify_filing_for_inbox(vault, _RULE_INBOX, rels, _cfg())
    assert set(result.written) == set(rels)
    for rel in rels:
        assert _read_fm(vault, rel)["email_category"] == "Business/Receipts"


# ===========================================================================
# LLM fallback: fires ONLY on no-rule-match; constrained to the closed set
# ===========================================================================

def test_llm_fallback_fires_on_no_rule_match(tmp_path):
    vault = _vault(tmp_path)
    rel = _seed_note(vault, "n2")
    llm = _FakeLLM(response='{"category": "Finance/Personal"}')
    result = classify_filing_for_inbox(vault, _NOMATCH_INBOX, [rel], _cfg(), llm_caller=llm)
    assert result.source == "llm"
    assert result.category == "Finance/Personal"
    assert len(llm.calls) == 1
    assert _read_fm(vault, rel)["email_category"] == "Finance/Personal"


def test_llm_fallback_invalid_label_writes_nothing(tmp_path):
    vault = _vault(tmp_path)
    rel = _seed_note(vault, "n3")
    llm = _FakeLLM(response='{"category": "Made/UpCategory"}')  # outside the closed set
    result = classify_filing_for_inbox(vault, _NOMATCH_INBOX, [rel], _cfg(), llm_caller=llm)
    assert result.category is None and result.source == "none"
    assert "email_category" not in _read_fm(vault, rel)


def test_llm_fallback_none_writes_nothing(tmp_path):
    vault = _vault(tmp_path)
    rel = _seed_note(vault, "n4")
    llm = _FakeLLM(response='{"category": "none"}')
    result = classify_filing_for_inbox(vault, _NOMATCH_INBOX, [rel], _cfg(), llm_caller=llm)
    assert result.category is None
    assert "email_category" not in _read_fm(vault, rel)


def test_fallback_disabled_skips_llm_entirely(tmp_path):
    vault = _vault(tmp_path)
    rel = _seed_note(vault, "n5")
    llm = _FakeLLM(response='{"category": "Finance/Personal"}')
    result = classify_filing_for_inbox(vault, _NOMATCH_INBOX, [rel], _cfg(fallback_enabled=False), llm_caller=llm)
    assert result.category is None and llm.calls == []


# ===========================================================================
# Gates: disabled / non-email / no-notes are no-ops
# ===========================================================================

def test_disabled_config_is_noop(tmp_path):
    vault = _vault(tmp_path)
    rel = _seed_note(vault, "n6")
    llm = _FakeLLM(response='{"category": "Finance/Personal"}')
    result = classify_filing_for_inbox(vault, _RULE_INBOX, [rel], EmailFilingConfig(enabled=False), llm_caller=llm)
    assert result.category is None and result.written == [] and llm.calls == []
    assert "email_category" not in _read_fm(vault, rel)


def test_non_email_inbox_is_noop(tmp_path):
    vault = _vault(tmp_path)
    rel = _seed_note(vault, "n7")
    result = classify_filing_for_inbox(vault, "# A voice memo\n\nno headers here", [rel], _cfg())
    assert result.category is None and result.written == []


def test_no_note_paths_is_noop(tmp_path):
    vault = _vault(tmp_path)
    result = classify_filing_for_inbox(vault, _RULE_INBOX, ["person/x.md"], _cfg())
    assert result.category is None and result.written == []


# ===========================================================================
# ORTHOGONALITY: filing writes ONLY email_category; a fault never raises
# ===========================================================================

def test_filing_write_does_not_perturb_priority_fields(tmp_path):
    vault = _vault(tmp_path)
    # A note the priority classifier already wrote to.
    rel = _seed_note(vault, "n8", priority="high", action_hint="calendar", priority_reasoning="was urgent")
    classify_filing_for_inbox(vault, _RULE_INBOX, [rel], _cfg())
    fm = _read_fm(vault, rel)
    # email_category added...
    assert fm["email_category"] == "Business/Receipts"
    # ...and the priority axis is byte-untouched.
    assert fm["priority"] == "high"
    assert fm["action_hint"] == "calendar"
    assert fm["priority_reasoning"] == "was urgent"


def test_filing_fault_is_isolated_never_raises(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    rel = _seed_note(vault, "n9")
    from alfred.vault.ops import VaultError

    def _boom(*a, **k):
        raise VaultError("simulated write failure")
    monkeypatch.setattr("alfred.email_filing.classifier.vault_edit", _boom)
    # MUST NOT raise — a filing write fault is caught per-note; the priority pass (already done) is safe.
    result = classify_filing_for_inbox(vault, _RULE_INBOX, [rel], _cfg())
    assert result.written == []  # nothing persisted, but no exception propagated


# ===========================================================================
# NEVER-AUTO-MUTATE: categorization does not flip confidence.filing
# ===========================================================================

def test_categorization_does_not_auto_flip_confidence_filing(tmp_path):
    from alfred.daily_sync.config import ConfidenceConfig
    from alfred.daily_sync.confidence import list_confidence, set_confidence

    vault = _vault(tmp_path)
    rel = _seed_note(vault, "n10")
    seed = ConfidenceConfig()
    state = tmp_path / "ds_state.json"
    set_confidence(state, "filing", False, seed=seed)  # establish the gate as OFF
    before = state.read_bytes()

    classify_filing_for_inbox(vault, _RULE_INBOX, [rel], _cfg())  # rule match + write

    # The gate stays OFF and the state file is byte-unchanged — the filing pass never touches it.
    assert list_confidence(state, seed)["filing"] is False
    assert state.read_bytes() == before


# ===========================================================================
# Log-emission pins (discipline #9)
# ===========================================================================

def test_categorized_log_emitted(tmp_path):
    vault = _vault(tmp_path)
    rel = _seed_note(vault, "n11")
    with structlog.testing.capture_logs() as cap:
        classify_filing_for_inbox(vault, _RULE_INBOX, [rel], _cfg())
    ev = [c for c in cap if c.get("event") == "email_filing.categorized"]
    assert len(ev) == 1
    assert ev[0]["category"] == "Business/Receipts"
    assert ev[0]["source"] == "rule"
    assert ev[0]["notes"] == 1


def test_no_category_ilb_log_emitted(tmp_path):
    vault = _vault(tmp_path)
    rel = _seed_note(vault, "n12")
    with structlog.testing.capture_logs() as cap:
        classify_filing_for_inbox(vault, _NOMATCH_INBOX, [rel], _cfg(fallback_enabled=False))
    ev = [c for c in cap if c.get("event") == "email_filing.no_category"]
    assert len(ev) == 1
    assert ev[0]["fallback_enabled"] is False
