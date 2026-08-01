"""#27 slice 1 — the classify-time ``email_urgent`` feed emit (contract pins).

The classifier (inside curator) upserts ONE ``email_urgent`` feed item per record
that lands high — LLM verdict OR sender-override — the moment it's classified.
These pins hold the emit's contract independent of the LLM: a fake ``llm_caller``
returns a canned verdict, so no real Anthropic call happens.

Load-bearing pins (mutation-verified):
  * NO RECONCILE — a classify-time emit must NEVER act the sync producer's
    ``email_tier`` open items (the two-emitter fight the distinct kind prevents).
  * belt — a raising emit must NOT block classification/structuring.
  * fail-safe — ``feed_handle=None`` (unwired caller) emits nothing even on high.

Plus: evidence shape (incl. gmail_url constant-prefix + no-id→blank), high_source
machine-readable both values, stable-key identity, disabled-skip ILB log, and the
``urgent_emitted`` log-emission pin (a dropped log line must redden the test).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import frontmatter
import pytest
import structlog

from alfred.email_classifier.classifier import classify_record
from alfred.email_classifier.config import EmailClassifierConfig
from alfred.email_classifier.vault_helpers import reset_contacts_cache
from alfred.feed import FeedEmitHandle, FeedItem, FeedStore
from alfred.feed.model import STATE_ACTED, STATE_OPEN, make_id

# The Gmail deep-link constant prefix — pinned here so the evidence-shape test
# reddens if gmail_filing's URL scheme drifts (the FE renders it as a plain
# external anchor and depends on this exact prefix).
_GMAIL_PREFIX = "https://mail.google.com/mail/u/0/#search/rfc822msgid:"


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    reset_contacts_cache()
    yield
    reset_contacts_cache()


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    for sub in ("person", "note"):
        (v / sub).mkdir(parents=True)
    return v


@pytest.fixture
def config() -> EmailClassifierConfig:
    cfg = EmailClassifierConfig(enabled=True)
    cfg.anthropic.api_key = "DUMMY_ANTHROPIC_TEST_KEY"
    return cfg


def _handle(tmp_path: Path, *, enabled: bool = True, instance: str = "Salem") -> FeedEmitHandle:
    return FeedEmitHandle(
        store_path=str(tmp_path / "feed.jsonl"), instance=instance, enabled=enabled,
    )


def _store(handle: FeedEmitHandle) -> FeedStore:
    return FeedStore(handle.store_path)


def _seed_note(
    vault: Path, name: str, *, body: str = "Hi Andrew — please review by EOD.\n",
    message_id: str | None = None,
) -> str:
    rel = f"note/{name}.md"
    fm: dict[str, Any] = {"type": "note", "name": name, "created": "2026-08-01"}
    if message_id is not None:
        fm["email_message_id"] = message_id
    (vault / "note" / f"{name}.md").write_text(
        frontmatter.dumps(frontmatter.Post(body, **fm)) + "\n", encoding="utf-8",
    )
    return rel


def _seed_flagged_person(vault: Path, name: str, *, email: str) -> None:
    fm = {
        "type": "person", "name": name, "created": "2026-08-01",
        "email": email, "high_priority_sender": True,
    }
    (vault / "person" / f"{name}.md").write_text(
        frontmatter.dumps(frontmatter.Post(f"# {name}\n", **fm)) + "\n", encoding="utf-8",
    )


def _fake_llm(priority: str, *, reasoning: str = "reason", action_hint: str | None = None):
    payload = json.dumps({"priority": priority, "action_hint": action_hint, "reasoning": reasoning})

    def _caller(system: str, user: str, cfg: EmailClassifierConfig) -> str:
        return payload

    return _caller


_HIGH_INBOX = "**From:** jamie@example.com\n**Subject:** Friday\n\nCan you confirm 3pm?\n"


# ---------------------------------------------------------------------------
# high verdict → emit; high_source machine-readable (both values)
# ---------------------------------------------------------------------------


def test_high_llm_verdict_emits_urgent_with_llm_source(vault: Path, config, tmp_path) -> None:
    """LLM picks high on its own → one email_urgent item, high_source='llm'."""
    handle = _handle(tmp_path)
    rel = _seed_note(vault, "Urgent one")

    result = classify_record(
        vault_path=vault, note_rel_path=rel, inbox_content=_HIGH_INBOX,
        config=config, llm_caller=_fake_llm("high"), feed_handle=handle,
    )

    assert result.priority == "high"
    items = _store(handle).load()
    fid = make_id("email_urgent", rel)
    assert fid in items
    item = items[fid]
    assert item.kind == "email_urgent"
    assert item.state == STATE_OPEN
    assert item.mode == "decide" and item.attention == "needs_you"
    assert item.instance == "Salem"
    assert item.evidence["high_source"] == "llm"


def test_high_override_emits_urgent_with_override_source(vault: Path, config, tmp_path) -> None:
    """Sender-override forces high (LLM said medium) → high_source='override'.

    Slice 3 gates the push on override-highs, so this value is load-bearing."""
    handle = _handle(tmp_path)
    _seed_flagged_person(vault, "Paul Chudnovsky", email="pchudnovsky@coxandpalmer.com")
    inbox = (
        "From: Chudnovsky, Paul (Halifax) <pchudnovsky@coxandpalmer.com>\n"
        "**Subject:** Re: contract\n\nSee redlines.\n"
    )
    rel = _seed_note(vault, "Contract review")

    result = classify_record(
        vault_path=vault, note_rel_path=rel, inbox_content=inbox,
        config=config, llm_caller=_fake_llm("medium"), feed_handle=handle,
    )

    assert result.priority == "high" and result.override_applied is True
    item = _store(handle).load()[make_id("email_urgent", rel)]
    assert item.evidence["high_source"] == "override"


def test_non_high_does_not_emit(vault: Path, config, tmp_path) -> None:
    """A medium verdict → NO email_urgent item (the interrupt is high-only)."""
    handle = _handle(tmp_path)
    rel = _seed_note(vault, "Just medium")

    classify_record(
        vault_path=vault, note_rel_path=rel, inbox_content=_HIGH_INBOX,
        config=config, llm_caller=_fake_llm("medium"), feed_handle=handle,
    )

    assert _store(handle).load() == {}


# ---------------------------------------------------------------------------
# fail-safe — handle absent / disabled
# ---------------------------------------------------------------------------


def test_handle_absent_does_not_emit(vault: Path, config, tmp_path) -> None:
    """feed_handle=None (unwired caller, e.g. backfill) → no emit even on high.

    The store file must not even be created."""
    rel = _seed_note(vault, "High no handle")
    store_path = tmp_path / "feed.jsonl"

    result = classify_record(
        vault_path=vault, note_rel_path=rel, inbox_content=_HIGH_INBOX,
        config=config, llm_caller=_fake_llm("high"), feed_handle=None,
    )

    assert result.priority == "high"
    assert not store_path.exists()


def test_handle_disabled_does_not_emit_and_logs_skip(vault: Path, config, tmp_path) -> None:
    """feed.enabled=False → explicit skip log (ILB), no emit."""
    handle = _handle(tmp_path, enabled=False)
    rel = _seed_note(vault, "High disabled")

    with structlog.testing.capture_logs() as captured:
        classify_record(
            vault_path=vault, note_rel_path=rel, inbox_content=_HIGH_INBOX,
            config=config, llm_caller=_fake_llm("high"), feed_handle=handle,
        )

    assert _store(handle).load() == {}
    skips = [c for c in captured if c.get("event") == "email_classifier.urgent_emit_skipped_disabled"]
    assert len(skips) == 1 and skips[0]["path"] == rel


# ---------------------------------------------------------------------------
# evidence shape — incl. gmail_url constant-prefix + no-id→blank
# ---------------------------------------------------------------------------


def test_evidence_shape_full(vault: Path, config, tmp_path) -> None:
    """All evidence keys present; gmail_url carries the constant prefix + the
    URL-encoded message id when the record has one."""
    handle = _handle(tmp_path)
    rel = _seed_note(vault, "With mid", message_id="<abc123@mail.example.com>")

    classify_record(
        vault_path=vault, note_rel_path=rel, inbox_content=_HIGH_INBOX,
        config=config, llm_caller=_fake_llm("high", reasoning="named + deadline"),
        feed_handle=handle,
    )

    ev = _store(handle).load()[make_id("email_urgent", rel)].evidence
    assert set(ev) == {
        "sender", "subject", "body", "truncated", "message_id", "gmail_url",
        "record_path", "classifier_priority", "classifier_reason", "high_source",
    }
    assert ev["sender"] == "jamie@example.com"
    assert ev["subject"] == "With mid"
    assert ev["record_path"] == rel
    assert ev["classifier_priority"] == "high"
    assert ev["classifier_reason"] == "named + deadline"
    assert ev["message_id"] == "<abc123@mail.example.com>"
    assert ev["gmail_url"].startswith(_GMAIL_PREFIX)
    # Angle brackets are stripped + the id is URL-encoded (the '@' becomes %40).
    assert "abc123%40mail.example.com" in ev["gmail_url"]
    assert isinstance(ev["truncated"], bool)


def test_gmail_url_blank_when_no_message_id(vault: Path, config, tmp_path) -> None:
    """No email_message_id in frontmatter → gmail_url is blank (FE renders no link)."""
    handle = _handle(tmp_path)
    rel = _seed_note(vault, "No mid")  # no message_id seeded

    classify_record(
        vault_path=vault, note_rel_path=rel, inbox_content=_HIGH_INBOX,
        config=config, llm_caller=_fake_llm("high"), feed_handle=handle,
    )

    ev = _store(handle).load()[make_id("email_urgent", rel)].evidence
    assert ev["message_id"] == ""
    assert ev["gmail_url"] == ""


# ---------------------------------------------------------------------------
# stable-key identity + title (#28 no-sender rule reused)
# ---------------------------------------------------------------------------


def test_stable_key_is_record_path(vault: Path, config, tmp_path) -> None:
    handle = _handle(tmp_path)
    rel = _seed_note(vault, "Keyed")

    classify_record(
        vault_path=vault, note_rel_path=rel, inbox_content=_HIGH_INBOX,
        config=config, llm_caller=_fake_llm("high"), feed_handle=handle,
    )

    assert make_id("email_urgent", rel) in _store(handle).load()


def test_title_includes_sender_when_present(vault: Path, config, tmp_path) -> None:
    handle = _handle(tmp_path)
    rel = _seed_note(vault, "Has sender")

    classify_record(
        vault_path=vault, note_rel_path=rel, inbox_content=_HIGH_INBOX,
        config=config, llm_caller=_fake_llm("high"), feed_handle=handle,
    )

    title = _store(handle).load()[make_id("email_urgent", rel)].title
    assert title == "Urgent email: jamie@example.com — Has sender"


def test_title_drops_sender_segment_when_absent(vault: Path, config, tmp_path) -> None:
    """No parseable From-line → the '(unknown) — ' segment is dropped (#28 rule)."""
    handle = _handle(tmp_path)
    rel = _seed_note(vault, "No sender line")
    # Account+Subject shape (is_email_inbox true) but no From-line → sender="".
    inbox = "Account: work\n**Subject:** heads up\n\nbody text\n"

    classify_record(
        vault_path=vault, note_rel_path=rel, inbox_content=inbox,
        config=config, llm_caller=_fake_llm("high"), feed_handle=handle,
    )

    title = _store(handle).load()[make_id("email_urgent", rel)].title
    assert title == "Urgent email: No sender line"
    assert "—" not in title


# ---------------------------------------------------------------------------
# NO-RECONCILE pin — sync's email_tier items survive a classify-time emit
# ---------------------------------------------------------------------------


def test_no_reconcile_sync_email_tier_survives(vault: Path, config, tmp_path) -> None:
    """A classify-time email_urgent emit is a PER-ITEM UPSERT: it must NOT act
    the sync producer's open email_tier items. If this emit ever reconciled
    (mass-acting the other emitter's open set), those items would flip to acted
    and this pin reddens."""
    handle = _handle(tmp_path)
    store = _store(handle)
    # Seed two open sync-owned email_tier items (as the daily_sync producer would).
    tier_ids = []
    for i in range(2):
        it = FeedItem.create(
            kind="email_tier", stable_key=f"note/Tier{i}.md", instance="Salem",
            title=f"Email tier: t{i}", evidence={"record_path": f"note/Tier{i}.md"},
            source_ref={"producer": "daily_sync"},
        )
        store.upsert(it)
        tier_ids.append(it.id)

    rel = _seed_note(vault, "Urgent alongside tier")
    classify_record(
        vault_path=vault, note_rel_path=rel, inbox_content=_HIGH_INBOX,
        config=config, llm_caller=_fake_llm("high"), feed_handle=handle,
    )

    folded = store.load()
    # The new urgent item exists...
    assert make_id("email_urgent", rel) in folded
    # ...and EVERY pre-existing email_tier item is STILL open (untouched).
    for tid in tier_ids:
        assert folded[tid].state == STATE_OPEN, f"{tid} was acted — emit reconciled!"


# ---------------------------------------------------------------------------
# belt — a raising emit never blocks classification/structuring
# ---------------------------------------------------------------------------


def test_belt_emit_failure_does_not_block_classification(
    vault: Path, config, tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FeedStore.upsert raises → classify_record STILL returns the high result
    AND the priority frontmatter is persisted; the failure is logged (ILB),
    never propagated."""
    handle = _handle(tmp_path)
    rel = _seed_note(vault, "Belt test")

    def _boom(self, item):  # noqa: ANN001
        raise RuntimeError("disk full")

    monkeypatch.setattr("alfred.feed.store.FeedStore.upsert", _boom)

    with structlog.testing.capture_logs() as captured:
        result = classify_record(
            vault_path=vault, note_rel_path=rel, inbox_content=_HIGH_INBOX,
            config=config, llm_caller=_fake_llm("high"), feed_handle=handle,
        )

    # Classification completed + persisted despite the emit fault.
    assert result.priority == "high"
    post = frontmatter.load(str(vault / rel))
    assert post.metadata["priority"] == "high"
    # The fault is surfaced (not silent).
    fails = [c for c in captured if c.get("event") == "email_classifier.urgent_emit_failed"]
    assert len(fails) == 1
    assert fails[0]["path"] == rel and fails[0]["error_type"] == "RuntimeError"


# ---------------------------------------------------------------------------
# log-emission pin (discipline #9) — urgent_emitted drives the code path
# ---------------------------------------------------------------------------


def test_urgent_emitted_log_fires_with_fields(vault: Path, config, tmp_path) -> None:
    """The success path emits ``email_classifier.urgent_emitted`` with path,
    feed_id, high_source, instance — a dropped line or field-rename reddens this."""
    handle = _handle(tmp_path)
    rel = _seed_note(vault, "Logged")

    with structlog.testing.capture_logs() as captured:
        classify_record(
            vault_path=vault, note_rel_path=rel, inbox_content=_HIGH_INBOX,
            config=config, llm_caller=_fake_llm("high"), feed_handle=handle,
        )

    matches = [c for c in captured if c.get("event") == "email_classifier.urgent_emitted"]
    assert len(matches) == 1
    m = matches[0]
    assert m["path"] == rel
    assert m["feed_id"] == make_id("email_urgent", rel)
    assert m["high_source"] == "llm"
    assert m["instance"] == "Salem"


# ---------------------------------------------------------------------------
# plumb — classify_records_for_inbox threads the handle
# ---------------------------------------------------------------------------


def test_batch_entrypoint_threads_handle(vault: Path, config, tmp_path) -> None:
    """The batch entry point passes feed_handle through to classify_record, so a
    high record emits; without the handle it does not."""
    from alfred.email_classifier.classifier import classify_records_for_inbox

    handle = _handle(tmp_path)
    rel = _seed_note(vault, "Batch high")

    classify_records_for_inbox(
        vault, _HIGH_INBOX, [rel], config,
        llm_caller=_fake_llm("high"), feed_handle=handle,
    )
    assert make_id("email_urgent", rel) in _store(handle).load()


def test_batch_entrypoint_no_handle_no_emit(vault: Path, config, tmp_path) -> None:
    from alfred.email_classifier.classifier import classify_records_for_inbox

    rel = _seed_note(vault, "Batch high nohandle")
    store_path = tmp_path / "feed.jsonl"

    classify_records_for_inbox(
        vault, _HIGH_INBOX, [rel], config, llm_caller=_fake_llm("high"),
    )
    assert not store_path.exists()
