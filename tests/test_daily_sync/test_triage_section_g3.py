"""G3 (2026-07-30) — triage section must not claim unwired interactivity.

The module docstring used to say the operator "confirms/declines each dedup
proposal" — but there is NO triage branch in ``reply_dispatch`` (per-item
routing is deferred to the interface arc's Decide card). These pins lock the
truthful state: the render advertises no reply verbs, and the docstring no
longer describes the section as interactive.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

from pathlib import Path

from alfred.daily_sync import triage_section
from alfred.daily_sync.triage_section import render_batch


def _record(vault: Path, name: str) -> tuple[Path, dict, str]:
    return (vault / "task" / f"{name}.md", {"type": "task", "alfred_triage": True}, name)


def test_triage_render_advertises_no_unwired_verbs(tmp_path: Path) -> None:
    """The rendered section lists items only — no confirm/decline/reject verbs.

    Those verbs are not wired for triage; advertising them would recreate the
    operator-facing lie. If a future Decide-card ship wires routing, update
    this pin in lockstep.
    """
    records = [_record(tmp_path, "Triage - Alpha dedup"), _record(tmp_path, "Triage - Beta dedup")]
    rendered, summaries = render_batch(records, tmp_path, start_index=1)

    lowered = rendered.lower()
    for verb in ("confirm", "decline", "reject"):
        assert verb not in lowered, f"triage render must not advertise '{verb}'"
    # Sanity: it still renders the items it was given.
    assert "Triage - Alpha dedup" in rendered
    assert len(summaries) == 2


def test_triage_empty_state_sentinel_has_no_verbs(tmp_path: Path) -> None:
    rendered, summaries = render_batch([], tmp_path, start_index=1)
    assert "no triage items today" in rendered
    assert summaries == []
    lowered = rendered.lower()
    assert "confirm" not in lowered and "decline" not in lowered


def test_docstring_does_not_claim_interactive_confirm_decline() -> None:
    """Comment-lies regression pin: the docstring must not describe per-item
    confirm/decline as a working feature."""
    doc = triage_section.__doc__ or ""
    assert "confirms/declines each dedup proposal" not in doc
    # A truth-marker must be present so a future reword can't silently drop it.
    lowered = doc.lower()
    assert "not wired" in lowered or "surfaced-for-review" in lowered
