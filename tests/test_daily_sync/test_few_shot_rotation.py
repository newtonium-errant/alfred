"""Tests that the email classifier picks up corpus → few-shot rotation.

Covers:
- Empty corpus path → no few-shot block in the prompt.
- Corpus path set with one entry → few-shot block appears.
- Few-shot block contains the operator-corrected tier per entry.
- count=0 disables the block even if the path is set.
- A corrupt corpus file doesn't crash prompt construction.
"""

from __future__ import annotations

from pathlib import Path

from alfred.daily_sync.corpus import CorpusEntry, append_correction
from alfred.email_classifier.classifier import _build_system_prompt
from alfred.email_classifier.config import EmailClassifierConfig


def _enabled_config() -> EmailClassifierConfig:
    cfg = EmailClassifierConfig(enabled=True)
    cfg.anthropic.api_key = "DUMMY_ANTHROPIC_TEST_KEY"
    return cfg


def test_no_corpus_path_no_few_shot_block():
    cfg = _enabled_config()
    prompt = _build_system_prompt(cfg)
    assert "Recent calibration corrections" not in prompt


def test_zero_count_disables_block(tmp_path: Path):
    corpus = tmp_path / "corpus.jsonl"
    append_correction(corpus, CorpusEntry(
        record_path="note/A.md",
        classifier_priority="medium",
        classifier_action_hint=None,
        classifier_reason="reason",
        andrew_priority="low",
        sender="alice@example.com",
        subject="Test",
    ))
    cfg = _enabled_config()
    cfg.calibration_corpus_path = str(corpus)
    cfg.calibration_few_shot_count = 0
    prompt = _build_system_prompt(cfg)
    assert "Recent calibration corrections" not in prompt


def test_few_shot_block_appears_with_corpus(tmp_path: Path):
    corpus = tmp_path / "corpus.jsonl"
    append_correction(corpus, CorpusEntry(
        record_path="note/A.md",
        classifier_priority="medium",
        classifier_action_hint=None,
        classifier_reason="originally medium",
        andrew_priority="low",
        andrew_reason="newsletter, route to digest",
        sender="newsletters@example.com",
        subject="Weekly digest",
        snippet="Top stories of the week",
    ))
    cfg = _enabled_config()
    cfg.calibration_corpus_path = str(corpus)
    cfg.calibration_few_shot_count = 5
    prompt = _build_system_prompt(cfg)
    assert "Recent calibration corrections" in prompt
    assert "newsletters@example.com" in prompt
    assert "Weekly digest" in prompt
    assert "operator says: low" in prompt
    assert "classifier said: medium" in prompt
    assert "newsletter, route to digest" in prompt
    assert "Treat these as authoritative" in prompt


def test_few_shot_block_orders_newest_first(tmp_path: Path):
    corpus = tmp_path / "corpus.jsonl"
    for i in range(3):
        append_correction(corpus, CorpusEntry(
            record_path=f"note/{i}.md",
            classifier_priority="low",
            classifier_action_hint=None,
            classifier_reason="x",
            andrew_priority="low",
            sender=f"sender{i}@example.com",
            subject=f"Subject {i}",
        ))
    cfg = _enabled_config()
    cfg.calibration_corpus_path = str(corpus)
    cfg.calibration_few_shot_count = 3
    prompt = _build_system_prompt(cfg)
    # Newest should appear before oldest in the rendered block
    pos2 = prompt.index("sender2@example.com")
    pos0 = prompt.index("sender0@example.com")
    assert pos2 < pos0


def test_corrupt_corpus_does_not_crash(tmp_path: Path):
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text("not valid json at all\n", encoding="utf-8")
    cfg = _enabled_config()
    cfg.calibration_corpus_path = str(corpus)
    cfg.calibration_few_shot_count = 5
    # Should not raise
    prompt = _build_system_prompt(cfg)
    # The block falls back to absent when no entries parse
    assert "Recent calibration corrections" not in prompt
