"""#7 7c-i — EmailFilingConfig loading: absent → disabled, opt-in parse, env substitution, defaults."""

from __future__ import annotations

from alfred.email_filing.config import EmailFilingConfig, load_from_unified


def test_absent_block_is_disabled():
    cfg = load_from_unified({})
    assert isinstance(cfg, EmailFilingConfig)
    assert cfg.enabled is False


def test_defaults():
    cfg = EmailFilingConfig()
    assert cfg.enabled is False
    assert cfg.fallback_enabled is True
    assert cfg.anthropic.model == "claude-sonnet-4-6"
    assert cfg.calibration_corpus_path == ""
    assert cfg.calibration_few_shot_count == 10
    assert cfg.rules_additions_path == "./data/email_filing_rules.json"


def test_enabled_block_parses():
    cfg = load_from_unified({
        "email_filing": {
            "enabled": True,
            "fallback_enabled": False,
            "anthropic": {"model": "claude-sonnet-4-6", "max_tokens": 128},
            "calibration_corpus_path": "./data/corpus.jsonl",
            "calibration_few_shot_count": 5,
            "rules_additions_path": "./data/rules.json",
        }
    })
    assert cfg.enabled is True
    assert cfg.fallback_enabled is False
    assert cfg.anthropic.max_tokens == 128
    assert cfg.calibration_corpus_path == "./data/corpus.jsonl"
    assert cfg.calibration_few_shot_count == 5
    assert cfg.rules_additions_path == "./data/rules.json"


def test_env_substitution(monkeypatch):
    monkeypatch.setenv("FILING_TEST_KEY", "resolved-secret")
    cfg = load_from_unified({
        "email_filing": {"enabled": True, "anthropic": {"api_key": "${FILING_TEST_KEY}"}}
    })
    assert cfg.anthropic.api_key == "resolved-secret"


def test_unknown_keys_ignored_forward_compat():
    cfg = load_from_unified({"email_filing": {"enabled": True, "future_field": "x"}})
    assert cfg.enabled is True
