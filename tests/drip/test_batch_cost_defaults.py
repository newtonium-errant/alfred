"""#83 cost defaults — model inheritance and the bounded carried context.

Both are COST levers on the one workload where cost multiplies: a batch pays
per scan, so a default nobody chose is paid thirty or sixty times over. Both are
config-backed, so reversing either is a YAML edit rather than a deploy — which
is the property that makes them safe to default at all.

The two failure modes these pin are different. The model default fails LOUDLY
if wrong (a bill). The carried-context bound fails SILENTLY: an unthreaded
parameter leaves the write side live and the read side dead, every per-layer
test green, and the quadratic cost still being paid in the field. So the bound
gets a wiring pin driven through the real ``work()``, not just a unit test of
the renderer.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from alfred.batch.manifest import BatchImage, BatchManifest
from alfred.batch.render import render_body, render_carried_context
from alfred.drip.config import (
    DEFAULT_BATCH_CARRIED_CONTEXT_MAX_CHARS,
    DEFAULT_BATCH_MODEL_FALLBACK,
    CampaignConfig,
    DripConfig,
    load_from_unified,
)
from alfred.drip.wiring import build_campaign


def _drip(tmp_path: Path, **over) -> DripConfig:
    base = dict(
        enabled=True,
        vault_path=str(tmp_path / "vault"),
        data_dir=str(tmp_path / "data"),
        instance="Salem",
    )
    base.update(over)
    return DripConfig(**base)


def _cfg(**over) -> CampaignConfig:
    base = {
        "kind": "batch_image", "enabled": True,
        "api_key": "DUMMY_ANTHROPIC_TEST_KEY",
    }
    base.update(over)
    return CampaignConfig(**base)


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------


def test_an_unset_model_inherits_the_instances_talker_model(tmp_path: Path) -> None:
    """The operator already chose a model for this instance; use it.

    A code-level default here is thirty per-scan decisions the operator did not
    make, on the one workload where the choice multiplies.
    """
    c = build_campaign(
        "batch_image", _cfg(), _drip(tmp_path, talker_model="claude-sonnet-4-6"),
    )
    assert c.model == "claude-sonnet-4-6"


def test_an_explicit_model_overrides_the_inherited_one(tmp_path: Path) -> None:
    """Escape hatch, both directions — the high-res tier stays one line away."""
    c = build_campaign(
        "batch_image",
        _cfg(model="claude-opus-5"),
        _drip(tmp_path, talker_model="claude-sonnet-4-6"),
    )
    assert c.model == "claude-opus-5"


def test_the_default_is_no_longer_the_high_resolution_tier() -> None:
    """The ruling this commit encodes, pinned as a value rather than prose.

    opus-5 consumes up to 4784 visual tokens per image against the standard
    tier's 1568 — roughly 3x per scan before price. Reinstating it as the
    silent default should be a decision, so it fails here first.
    """
    assert CampaignConfig().model == "", "an unset model must INHERIT, not pin"
    assert "opus" not in DEFAULT_BATCH_MODEL_FALLBACK


def test_with_nothing_configured_the_fallback_is_used_and_said_out_loud(
    tmp_path: Path,
) -> None:
    """ILB: the one path where nobody chose the model must announce itself."""
    with structlog.testing.capture_logs() as captured:
        c = build_campaign("batch_image", _cfg(), _drip(tmp_path, talker_model=""))
    assert c.model == DEFAULT_BATCH_MODEL_FALLBACK
    fallbacks = [
        r for r in captured if r.get("event") == "drip.batch_image.model_fallback"
    ]
    assert len(fallbacks) == 1
    assert fallbacks[0]["model"] == DEFAULT_BATCH_MODEL_FALLBACK


def test_the_talker_model_is_read_from_the_unified_config() -> None:
    """The inheritance source must be the real config key, not an invention."""
    cfg = load_from_unified({
        "telegram": {
            "instance": {"name": "VERA"},
            "anthropic": {"model": "claude-sonnet-4-6"},
        },
        "vault": {"path": "/v"},
        "drip": {"campaigns": {"batch_image": {"enabled": True}}},
    })
    assert cfg.talker_model == "claude-sonnet-4-6"


def test_a_missing_telegram_block_leaves_nothing_to_inherit() -> None:
    """Normal on a non-talker instance — absence, not an error."""
    cfg = load_from_unified({"vault": {"path": "/v"}, "drip": {}})
    assert cfg.talker_model == ""


# ---------------------------------------------------------------------------
# The carried-context bound
# ---------------------------------------------------------------------------


def _manifest(n: int) -> BatchManifest:
    return BatchManifest(
        batch_id="20260811-abcd1234",
        instruction="Read the total.",
        created_at="2026-08-11T10:00:00+00:00",
        instance="Salem",
        record_path="note/Batch.md",
        images=[
            BatchImage(
                item_id=f"hash{i}", filename=f"scan-{i}.jpg",
                sha256=f"hash{i}", bytes=100, media_type="image/jpeg",
            )
            for i in range(n)
        ],
    )


def _rows(n: int, *, result_size: int = 200) -> list[dict]:
    return [
        {
            "item_id": f"hash{i}", "filename": f"scan-{i}.jpg",
            "outcome": "ok", "result": f"R{i}-" + "x" * result_size,
        }
        for i in range(n)
    ]


def test_the_carried_context_stops_growing_with_the_batch() -> None:
    """THE cost pin. Unbounded, this grew with the SQUARE of the batch size.

    Asserts against the BOUND, not against ``render_body``'s length, because
    the defect being prevented is "context tracks the batch" — which a
    comparison to some other rendering would not capture.
    """
    manifest = _manifest(60)
    ctx = render_carried_context(manifest, _rows(50), max_chars=2000)
    assert len(ctx) <= 2000 + 200, "the bound is not holding"
    # And the unbounded body it replaced is genuinely much larger, which is
    # what makes the bound worth having.
    assert len(render_body(manifest, _rows(50))) > 10_000


def test_the_bound_keeps_the_most_recent_results() -> None:
    """What the bound preserves: consistency with the scans just processed.

    The tail is what the context is FOR, so the budget is spent from the newest
    row backwards. Dropping the tail instead would keep the cost and lose the
    purpose.
    """
    ctx = render_carried_context(_manifest(20), _rows(10), max_chars=1200)
    assert "scan-9.jpg" in ctx, "the most recent result was dropped"
    assert "scan-0.jpg" not in ctx, "an old result survived the bound"


def test_results_stay_in_submission_order_within_the_context() -> None:
    """Built newest-first for the budget, but presented oldest-first."""
    ctx = render_carried_context(_manifest(20), _rows(10), max_chars=1200)
    kept = [line for line in ctx.splitlines() if line.startswith("### ")]
    assert kept == sorted(kept, key=lambda s: int(s.split("-")[1].split(".")[0]))


def test_an_omission_is_stated_never_silent() -> None:
    """The model must not reason about totals it cannot see."""
    ctx = render_carried_context(_manifest(20), _rows(10), max_chars=1200)
    assert "omitted for length" in ctx


def test_a_complete_context_claims_no_omission() -> None:
    """The disclosure appears only when something really was dropped."""
    ctx = render_carried_context(_manifest(3), _rows(2), max_chars=100_000)
    assert "omitted" not in ctx
    assert "scan-0.jpg" in ctx and "scan-1.jpg" in ctx


def test_the_first_scan_carries_no_context() -> None:
    """ILB: nothing processed yet is an empty string, not a header about none."""
    assert render_carried_context(_manifest(5), [], max_chars=8000) == ""


def test_a_zero_bound_disables_context_entirely() -> None:
    """A legitimate operator choice — each scan judged on its own."""
    assert render_carried_context(_manifest(5), _rows(3), max_chars=0) == ""


def test_quarantined_rows_are_not_carried_as_examples() -> None:
    """A scan that could not be read has no result to be consistent WITH."""
    rows = [{
        "item_id": "hash0", "filename": "scan-0.jpg",
        "outcome": "quarantined", "result": "",
        "note": "too blurry to read",
    }]
    assert render_carried_context(_manifest(3), rows, max_chars=8000) == ""


# ---------------------------------------------------------------------------
# Wiring — the bound must reach the model call
# ---------------------------------------------------------------------------


def test_build_campaign_threads_the_carried_context_bound(tmp_path: Path) -> None:
    c = build_campaign(
        "batch_image", _cfg(carried_context_max_chars=1234), _drip(tmp_path),
    )
    assert c.carried_context_max_chars == 1234


def test_the_config_loader_reads_the_bound_from_yaml() -> None:
    """A knob the YAML cannot set is not a knob."""
    cfg = load_from_unified({
        "telegram": {"instance": {"name": "VERA"}},
        "vault": {"path": "/v"},
        "drip": {"campaigns": {"batch_image": {
            "enabled": True, "carried_context_max_chars": 555,
        }}},
    })
    assert cfg.campaigns["batch_image"].carried_context_max_chars == 555


def test_an_omitted_bound_gets_the_considered_default() -> None:
    assert (
        CampaignConfig().carried_context_max_chars
        == DEFAULT_BATCH_CARRIED_CONTEXT_MAX_CHARS
    )


def _seed_for_consumption(tmp_path: Path, *, n: int, processed: int) -> tuple:
    """A real batch on disk: vault record, manifest, images, and a LONG ledger.

    Returns ``(manifest, ledger_file, images_root, vault, rows)``. Deliberately
    a real record rather than a stub — ``process_one`` reads it for the seal
    check before the model call, so a stub would never reach the consumption
    site this pin is about.
    """
    from alfred.batch.ledger import append_row, build_row, load_rows
    from alfred.batch.manifest import save_manifest
    from alfred.batch.paths import images_dir, ledger_path, manifest_path
    from alfred.vault.ops import vault_create

    vault = tmp_path / "vault"
    (vault / "note").mkdir(parents=True, exist_ok=True)
    data_dir = tmp_path / "data"
    batch_id = "20260811-abcd1234"

    result = vault_create(
        vault, "note", f"Batch {batch_id}",
        set_fields={
            "batch_id": batch_id, "batch_status": "open",
            "batch_items_total": n,
        },
        body="placeholder", scope="vera_batch",
    )

    imgs = images_dir(data_dir, "Salem", batch_id)
    imgs.mkdir(parents=True, exist_ok=True)
    manifest = _manifest(n)
    manifest.record_path = result["path"]
    for image in manifest.images:
        (imgs / image.filename).write_bytes(b"\xff\xd8\xff" + b"\x01" * 32)
    save_manifest(manifest_path(data_dir, "Salem", batch_id), manifest)

    # A long ledger: every result is big enough that the whole body cannot fit
    # inside the bound, which is what makes the two renderings distinguishable.
    lp = ledger_path(data_dir, "Salem", batch_id)
    for i in range(processed):
        append_row(lp, build_row(
            item_id=f"hash{i}", filename=f"scan-{i}.jpg",
            outcome="ok", result=f"RESULT-{i} " + "y" * 400,
        ))
    return manifest, lp, imgs, vault, load_rows(lp)


class _SpyClient:
    """Captures the user content of the one model call it is asked to make."""

    def __init__(self) -> None:
        self.carried = ""
        outer = self

        class _Messages:
            def create(self, **kwargs):
                blocks = kwargs["messages"][0]["content"]
                texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
                # The carried context is the block that names prior results.
                outer.carried = next(
                    (t for t in texts if "already recorded" in t), "",
                )
                return type("R", (), {"content": [
                    type("B", (), {"type": "text", "text": "ok"})(),
                ]})()

        self.messages = _Messages()


def test_process_one_actually_uses_the_bound_it_is_given(tmp_path: Path) -> None:
    """THE CONSUMPTION pin (gate WARN-1) — the sibling of the threading pin.

    The threading pin below proves the configured value ARRIVES at
    ``process_one``. It cannot prove ``process_one`` USES it, and that gap was
    real: swapping ``render_carried_context`` for ``render_body`` at the call
    site — the bound accepted and then ignored — left 300 tests green. An
    accepted-then-ignored parameter is the exact shape that ships a cost fix
    which is never actually applied in the field.

    So this drives the REAL ``process_one`` against a REAL long ledger and
    asserts on what reached the model:

      1. it is strictly SHORTER than the whole body would have been, and
      2. it is the TAIL rather than everything — the earliest scan's section is
         absent while the latest is present.

    (2) is what makes this a bound rather than a coincidence. "Shorter" alone
    could be satisfied by any truncation; only the head-absent/tail-present
    pair distinguishes "kept the recent results" from "cut the string".
    """
    n, processed, bound = 20, 19, 1500
    manifest, ledger_file, imgs, vault, rows = _seed_for_consumption(
        tmp_path, n=n, processed=processed,
    )
    client = _SpyClient()

    from alfred.batch.worker import process_one

    process_one(
        client=client,
        manifest=manifest,
        ledger_file=ledger_file,
        images_root=imgs,
        vault_path=vault,
        item_id=f"hash{processed}",       # the one not yet in the ledger
        model="claude-sonnet-4-6",
        max_tokens=1024,
        carried_context_max_chars=bound,
    )

    carried = client.carried
    assert carried, "no prior-results context reached the model at all"

    whole_body = render_body(manifest, rows)
    assert len(carried) < len(whole_body), (
        f"process_one ignored its bound: it sent {len(carried)} chars, and the "
        f"unbounded body is {len(whole_body)} — the cost fix is not applied"
    )
    # The bound binds (plus the wrapper prose the worker adds around it).
    assert len(carried) < bound + 500, f"context was {len(carried)} chars"

    # The TAIL survived and the HEAD did not — a real bound, not a truncation.
    assert f"scan-{processed - 1}.jpg" in carried, "the most recent result was dropped"
    assert "scan-0.jpg" not in carried, (
        "the earliest result survived a 1500-char bound over 19 x ~400-char "
        "results — the whole body is being sent"
    )


def test_work_threads_the_carried_context_bound(tmp_path: Path, monkeypatch) -> None:
    """THE wiring pin — driven through the REAL ``work()``.

    A gate parameter that defaults sensibly and is threaded only by tests is a
    standing trap: every per-layer pin passes, production keeps paying the
    unbounded cost, and nothing anywhere reports it. This drives the production
    entry point and asserts the configured value ARRIVES at ``process_one``.
    """
    seen: dict = {}
    import alfred.batch.worker as worker_mod

    monkeypatch.setattr(
        worker_mod, "process_one", lambda **kw: seen.update(kw) or "ok",
    )
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: object())

    campaign = build_campaign(
        "batch_image", _cfg(carried_context_max_chars=4321), _drip(tmp_path),
    )
    # A manifest must exist for work() to reach process_one.
    from alfred.batch.manifest import save_manifest
    from alfred.batch.paths import manifest_path

    save_manifest(
        manifest_path(tmp_path / "data", "Salem", "20260811-abcd1234"),
        _manifest(2),
    )
    campaign.work("20260811-abcd1234::hash0")

    assert seen.get("carried_context_max_chars") == 4321, (
        "work() did not thread the configured bound to process_one — the "
        "read side is dead and the quadratic cost is still being paid"
    )
