"""The batch drip campaign (#83) — wiring, work-list, work, verify.

This file is deliberately end-to-end THROUGH THE PRODUCTION ENTRY POINT
(``build_campaign``) rather than by constructing ``BatchImageCampaign``
directly. Building the object by hand would pass whether or not the
config layer can actually produce one, which is the standing trap: a
feature whose tests thread it and whose production path never does is
green everywhere and dead in the field.

The model is faked. Nothing here makes a network call — the suite's
egress guard would catch it if it did — but the fake is shaped like the
real SDK response (``.content`` of blocks with ``.type``/``.text``) so
the extraction path under test is the production one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from alfred.batch.ledger import append_row, build_row, load_rows
from alfred.batch.manifest import BatchImage, BatchManifest, save_manifest
from alfred.batch.paths import images_dir, ledger_path, manifest_path
from alfred.batch.seal import BatchSealedError
from alfred.drip.campaigns import BatchImageCampaign
from alfred.drip.config import CampaignConfig, DripConfig, DripConfigError
from alfred.drip.wiring import build_campaign
from alfred.vault.ops import vault_create, vault_read

_BATCH = "batch-20260811-aaaa"
_INSTRUCTION = "Reconcile each scan against the July statement."


# --------------------------------------------------------------------------
# Fake SDK
# --------------------------------------------------------------------------


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Response:
    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]


class FakeMessages:
    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._replies:
            raise AssertionError("model called more times than expected")
        return _Response(self._replies.pop(0))


class FakeClient:
    def __init__(self, replies: list[str]) -> None:
        self.messages = FakeMessages(replies)


class QuotaBlockedMessages:
    """Raises with text the runner's ``_is_quota_block`` classifies."""

    def create(self, **kwargs):
        raise RuntimeError(
            "Error: Claude AI usage limit reached. Your limit will reset at "
            "9pm."
        )


class QuotaBlockedClient:
    def __init__(self) -> None:
        self.messages = QuotaBlockedMessages()


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    (v / "note").mkdir(parents=True)
    return v


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _drip(data_dir: Path, vault: Path, instance: str = "Salem") -> DripConfig:
    return DripConfig(
        enabled=True,
        vault_path=str(vault),
        data_dir=str(data_dir),
        instance=instance,
    )


def _cfg(**over) -> CampaignConfig:
    base = {"kind": "batch_image", "enabled": True, "api_key": "DUMMY_BATCH_TEST_KEY"}
    base.update(over)
    return CampaignConfig(**base)


def _seed_batch(
    data_dir: Path,
    vault: Path,
    *,
    n: int = 2,
    instance: str = "Salem",
    batch_status: str = "open",
) -> BatchManifest:
    """Create a carried record + manifest + image files on disk."""
    result = vault_create(
        vault,
        "note",
        f"Batch {_BATCH}",
        set_fields={
            "batch_id": _BATCH,
            "batch_status": batch_status,
            "batch_items_total": n,
        },
        body="placeholder",
        scope="vera_batch",
    )
    imgs = images_dir(data_dir, instance, _BATCH)
    imgs.mkdir(parents=True, exist_ok=True)
    images = []
    for i in range(n):
        fn = f"scan-{i}.jpg"
        (imgs / fn).write_bytes(b"\xff\xd8\xff" + bytes([i]) * 32)
        images.append(BatchImage(
            item_id=f"hash{i}", filename=fn, sha256=f"hash{i}",
            bytes=35, media_type="image/jpeg",
        ))
    m = BatchManifest(
        batch_id=_BATCH,
        instruction=_INSTRUCTION,
        created_at="2026-08-11T10:00:00+00:00",
        instance=instance,
        record_path=result["path"],
        images=images,
    )
    save_manifest(manifest_path(data_dir, instance, _BATCH), m)
    return m


# --------------------------------------------------------------------------
# Wiring — the production entry point
# --------------------------------------------------------------------------


def test_build_campaign_produces_a_batch_campaign(data_dir, vault) -> None:
    c = build_campaign("batch_image", _cfg(), _drip(data_dir, vault))
    assert isinstance(c, BatchImageCampaign)
    assert c.name == "batch_image"
    assert c.instance == "Salem"
    assert Path(c.data_dir) == data_dir


def test_build_campaign_threads_the_cost_levers(data_dir, vault) -> None:
    """Model and max_tokens must reach the object, not just the config."""
    c = build_campaign(
        "batch_image",
        _cfg(model="claude-sonnet-5", max_tokens=1234),
        _drip(data_dir, vault),
    )
    assert c.model == "claude-sonnet-5"
    assert c.max_tokens == 1234


def test_build_campaign_requires_an_api_key(data_dir, vault) -> None:
    """Fail at config time, not on scan 1 of 30."""
    with pytest.raises(DripConfigError, match="api_key"):
        build_campaign("batch_image", _cfg(api_key=""), _drip(data_dir, vault))


def test_build_campaign_requires_an_instance(data_dir, vault) -> None:
    with pytest.raises(DripConfigError, match="instance"):
        build_campaign(
            "batch_image", _cfg(), _drip(data_dir, vault, instance=""),
        )


def test_unknown_kind_message_lists_batch_image(data_dir, vault) -> None:
    """The 'expected one of' literal is hand-maintained — pin it."""
    with pytest.raises(DripConfigError, match="batch_image"):
        build_campaign(
            "nope", _cfg(kind="nope"), _drip(data_dir, vault),
        )


# --------------------------------------------------------------------------
# Item ids
# --------------------------------------------------------------------------


def test_item_id_round_trip() -> None:
    item = BatchImageCampaign.build_item("b1", "deadbeef")
    assert BatchImageCampaign.parse_item(item) == ("b1", "deadbeef")


@pytest.mark.parametrize("bad", ["", "nosep", "::x", "b::"])
def test_malformed_item_id_raises(bad: str) -> None:
    with pytest.raises(ValueError, match="malformed batch item id"):
        BatchImageCampaign.parse_item(bad)


# --------------------------------------------------------------------------
# worklist
# --------------------------------------------------------------------------


def test_worklist_empty_before_any_batch(data_dir, vault) -> None:
    c = build_campaign("batch_image", _cfg(), _drip(data_dir, vault))
    assert c.worklist() == []


def test_worklist_no_batch_dir_emits_ilb(data_dir, vault) -> None:
    """Intentionally-left-blank: pre-first-use must not look broken."""
    c = build_campaign("batch_image", _cfg(), _drip(data_dir, vault))
    with structlog.testing.capture_logs() as captured:
        c.worklist()
    events = [x for x in captured
              if x.get("event") == "drip.batch_image.no_batch_dir"]
    assert len(events) == 1
    assert "nothing to drain" in events[0]["detail"]


def test_worklist_lists_every_item_of_an_open_batch(data_dir, vault) -> None:
    _seed_batch(data_dir, vault, n=3)
    c = build_campaign("batch_image", _cfg(), _drip(data_dir, vault))
    assert c.worklist() == [
        f"{_BATCH}::hash0", f"{_BATCH}::hash1", f"{_BATCH}::hash2",
    ]


def test_worklist_drops_a_fully_drained_batch(data_dir, vault) -> None:
    """A finished batch stops being re-walked every run."""
    _seed_batch(data_dir, vault, n=2)
    lp = ledger_path(data_dir, "Salem", _BATCH)
    for i in range(2):
        append_row(lp, build_row(
            item_id=f"hash{i}", filename=f"scan-{i}.jpg",
            outcome="ok", result="x",
        ))
    c = build_campaign("batch_image", _cfg(), _drip(data_dir, vault))
    assert c.worklist() == []


def test_worklist_drained_batch_emits_ilb(data_dir, vault) -> None:
    _seed_batch(data_dir, vault, n=1)
    append_row(ledger_path(data_dir, "Salem", _BATCH), build_row(
        item_id="hash0", filename="scan-0.jpg", outcome="ok", result="x",
    ))
    c = build_campaign("batch_image", _cfg(), _drip(data_dir, vault))
    with structlog.testing.capture_logs() as captured:
        c.worklist()
    events = [x for x in captured
              if x.get("event") == "drip.batch_image.nothing_pending"]
    assert len(events) == 1
    assert events[0]["drained"] == 1


def test_worklist_keeps_a_partially_drained_batch(data_dir, vault) -> None:
    _seed_batch(data_dir, vault, n=2)
    append_row(ledger_path(data_dir, "Salem", _BATCH), build_row(
        item_id="hash0", filename="scan-0.jpg", outcome="ok", result="x",
    ))
    c = build_campaign("batch_image", _cfg(), _drip(data_dir, vault))
    # Both items stay admitted: admission is BATCH-level so it is not the
    # negation of verify() (see the campaign docstring).
    assert c.worklist() == [f"{_BATCH}::hash0", f"{_BATCH}::hash1"]


# --------------------------------------------------------------------------
# work — the full slice
# --------------------------------------------------------------------------


def _run_one(campaign, item, client) -> None:
    """Drive ``work`` with an injected fake client."""
    import alfred.drip.campaigns as mod

    orig = mod.BatchImageCampaign.work

    def patched(self, item_id: str) -> None:
        from alfred.batch.paths import images_dir as _imgs
        from alfred.batch.worker import process_one

        batch_id, content_hash = self.parse_item(item_id)
        process_one(
            client=client,
            manifest=self._manifest_for(batch_id),
            ledger_file=self._ledger_for(batch_id),
            images_root=_imgs(self.data_dir, self.instance, batch_id),
            vault_path=self.vault_path,
            item_id=content_hash,
            model=self.model,
            max_tokens=self.max_tokens,
            # Read off the campaign, never a literal: this helper mirrors
            # ``work``, and a hardcoded value here would let the real
            # threading break while every test using it stayed green.
            # ``test_work_threads_the_carried_context_bound`` pins the real one.
            carried_context_max_chars=self.carried_context_max_chars,
        )

    mod.BatchImageCampaign.work = patched
    try:
        campaign.work(item)
    finally:
        mod.BatchImageCampaign.work = orig


def test_work_records_a_row_and_renders_the_record(data_dir, vault) -> None:
    m = _seed_batch(data_dir, vault, n=2)
    c = build_campaign("batch_image", _cfg(), _drip(data_dir, vault))
    _run_one(c, f"{_BATCH}::hash0", FakeClient(["Total: $412.55"]))

    rows = load_rows(ledger_path(data_dir, "Salem", _BATCH))
    assert [r["item_id"] for r in rows] == ["hash0"]
    assert rows[0]["result"] == "Total: $412.55"

    record = vault_read(vault, m.record_path)
    assert "Total: $412.55" in record["body"]
    assert "### scan-0.jpg" in record["body"]
    assert record["frontmatter"]["batch_items_done"] == 1
    assert record["frontmatter"]["batch_items_total"] == 2


def test_work_is_idempotent_and_does_not_call_the_model(data_dir, vault) -> None:
    """A replayed image is free — the whole point of content addressing."""
    _seed_batch(data_dir, vault, n=1)
    c = build_campaign("batch_image", _cfg(), _drip(data_dir, vault))
    _run_one(c, f"{_BATCH}::hash0", FakeClient(["first"]))

    second = FakeClient([])  # any call raises
    _run_one(c, f"{_BATCH}::hash0", second)
    assert second.messages.calls == []
    assert len(load_rows(ledger_path(data_dir, "Salem", _BATCH))) == 1


def test_work_quarantines_an_unreadable_scan(data_dir, vault) -> None:
    m = _seed_batch(data_dir, vault, n=1)
    c = build_campaign("batch_image", _cfg(), _drip(data_dir, vault))
    _run_one(
        c, f"{_BATCH}::hash0",
        FakeClient(["CANNOT_READ: the page is cut off at the fold"]),
    )
    rows = load_rows(ledger_path(data_dir, "Salem", _BATCH))
    assert rows[0]["outcome"] == "quarantined"
    assert "cut off at the fold" in rows[0]["note"]
    body = vault_read(vault, m.record_path)["body"]
    assert "needs a look" in body


def test_work_quarantines_an_empty_reply(data_dir, vault) -> None:
    """'Model returned nothing' must not render as a successful blank."""
    _seed_batch(data_dir, vault, n=1)
    c = build_campaign("batch_image", _cfg(), _drip(data_dir, vault))
    _run_one(c, f"{_BATCH}::hash0", FakeClient(["   "]))
    rows = load_rows(ledger_path(data_dir, "Salem", _BATCH))
    assert rows[0]["outcome"] == "quarantined"
    assert "no text" in rows[0]["note"]


def test_work_sends_the_image_and_instruction(data_dir, vault) -> None:
    _seed_batch(data_dir, vault, n=1)
    c = build_campaign("batch_image", _cfg(), _drip(data_dir, vault))
    client = FakeClient(["ok"])
    _run_one(c, f"{_BATCH}::hash0", client)
    content = client.messages.calls[0]["messages"][0]["content"]
    kinds = [b["type"] for b in content]
    assert "image" in kinds
    assert any(_INSTRUCTION in b.get("text", "") for b in content)


def test_work_carries_prior_results_as_context(data_dir, vault) -> None:
    """D1: cross-image context comes from the carried record."""
    _seed_batch(data_dir, vault, n=2)
    c = build_campaign("batch_image", _cfg(), _drip(data_dir, vault))
    _run_one(c, f"{_BATCH}::hash0", FakeClient(["FIRST RESULT"]))
    client = FakeClient(["second"])
    _run_one(c, f"{_BATCH}::hash1", client)
    content = client.messages.calls[0]["messages"][0]["content"]
    assert any("FIRST RESULT" in b.get("text", "") for b in content)


def test_work_on_a_sealed_record_refuses_without_spending(
    data_dir, vault,
) -> None:
    """The seal guard fires BEFORE the model call — a sealed batch is free."""
    _seed_batch(data_dir, vault, n=1, batch_status="sealed")
    c = build_campaign("batch_image", _cfg(), _drip(data_dir, vault))
    client = FakeClient([])  # any call raises
    with pytest.raises(BatchSealedError, match="SEALED"):
        _run_one(c, f"{_BATCH}::hash0", client)
    assert client.messages.calls == []
    assert load_rows(ledger_path(data_dir, "Salem", _BATCH)) == []


def test_work_on_another_batchs_record_refuses_and_leaves_it_untouched(
    data_dir, vault,
) -> None:
    """Gate review WARN-1, driven through the REAL vault_edit path.

    Batch A's manifest points at batch B's record — a wiring fault
    (stale record_path, a rename, an id collision). Every scope check
    admits it: the target is a note, it carries a ``batch_id``, and its
    status is ``open``. The per-type allowlist and the scope layer's
    presence check are both satisfied, which is exactly why the identity
    comparison has to exist.

    The assertion that matters is the LAST one. "Did it raise" is weak —
    a refusal for any unrelated reason looks identical. What proves the
    guard is that batch B's record is byte-for-byte what it was: neither
    the body_replace nor the frontmatter counters landed, so both write
    surfaces of the single ``vault_edit`` call were prevented.
    """
    from alfred.batch.manifest import save_manifest

    # Batch B's record — the victim. Open, batch-owned, entirely valid.
    victim = vault_create(
        vault,
        "note",
        "Batch batch-OTHER",
        set_fields={
            "batch_id": "batch-OTHER",
            "batch_status": "open",
            "batch_items_total": 5,
        },
        body="BATCH B RESULTS — must survive untouched",
        scope="vera_batch",
    )
    before = (vault / victim["path"]).read_text(encoding="utf-8")

    # Batch A, mis-wired to point at B's record.
    m = _seed_batch(data_dir, vault, n=1)
    m.record_path = victim["path"]
    save_manifest(manifest_path(data_dir, "Salem", _BATCH), m)

    c = build_campaign("batch_image", _cfg(), _drip(data_dir, vault))
    client = FakeClient([])  # any model call raises
    with pytest.raises(BatchSealedError, match="DIFFERENT"):
        _run_one(c, f"{_BATCH}::hash0", client)

    # Refused before spending.
    assert client.messages.calls == []
    assert load_rows(ledger_path(data_dir, "Salem", _BATCH)) == []
    # And — the point of the test — B's record is untouched.
    assert (vault / victim["path"]).read_text(encoding="utf-8") == before


def test_quota_error_propagates_for_the_runner_to_classify(
    data_dir, vault,
) -> None:
    """BLOCKED is a pause, not a verdict — the worker must not swallow it."""
    from alfred.drip.runner import _is_quota_block

    _seed_batch(data_dir, vault, n=1)
    c = build_campaign("batch_image", _cfg(), _drip(data_dir, vault))
    with pytest.raises(RuntimeError) as ei:
        _run_one(c, f"{_BATCH}::hash0", QuotaBlockedClient())
    blocked, _kind = _is_quota_block(ei.value)
    assert blocked is True
    # No row: a blocked item must be re-attempted, not recorded done.
    assert load_rows(ledger_path(data_dir, "Salem", _BATCH)) == []


# --------------------------------------------------------------------------
# verify — two observations, not one
# --------------------------------------------------------------------------


def test_verify_false_before_processing(data_dir, vault) -> None:
    _seed_batch(data_dir, vault, n=1)
    c = build_campaign("batch_image", _cfg(), _drip(data_dir, vault))
    assert c.verify(f"{_BATCH}::hash0") is False


def test_verify_true_after_processing(data_dir, vault) -> None:
    _seed_batch(data_dir, vault, n=1)
    c = build_campaign("batch_image", _cfg(), _drip(data_dir, vault))
    _run_one(c, f"{_BATCH}::hash0", FakeClient(["done"]))
    assert c.verify(f"{_BATCH}::hash0") is True


def test_verify_false_when_ledger_row_exists_but_render_did_not_land(
    data_dir, vault,
) -> None:
    """THE pin for the not-the-negation property.

    A ledger row alone must NOT satisfy verify. This simulates exactly
    the failure the naive verifier would miss: the result was recorded
    but the vault write never happened, so the operator's record shows
    nothing while the item would be marked DONE.
    """
    _seed_batch(data_dir, vault, n=1)
    append_row(ledger_path(data_dir, "Salem", _BATCH), build_row(
        item_id="hash0", filename="scan-0.jpg", outcome="ok", result="x",
    ))
    c = build_campaign("batch_image", _cfg(), _drip(data_dir, vault))
    assert c.verify(f"{_BATCH}::hash0") is False


def test_verify_false_for_a_missing_batch(data_dir, vault) -> None:
    c = build_campaign("batch_image", _cfg(), _drip(data_dir, vault))
    assert c.verify("no-such-batch::hash0") is False


def test_campaign_protocol_flags(data_dir, vault) -> None:
    c = build_campaign("batch_image", _cfg(), _drip(data_dir, vault))
    # Vision call per scan -> the weekly cap must bind.
    assert c.spends_quota() is True
    # The render lands before work() returns, so an unverified return is a
    # genuine failure rather than a dispatch.
    assert c.verify_is_async() is False
