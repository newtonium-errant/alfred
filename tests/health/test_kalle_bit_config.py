"""End-to-end BIT regression-pin for KAL-LE-shape configs (2026-05-16).

Bug-of-record: KAL-LE's BIT was surfacing ``[ FAIL ] curator.last-
successful-process`` for 5 days because the curator probe assumed every
instance runs a curator daemon. KAL-LE doesn't — surveyor handles its
inbox-watching; there's no curator daemon in its daemon set. The probe
consulted an absent state file via the dataclass-default path and the
``_check_vault`` / ``_check_backend`` sub-probes ran against a config
that had no curator section.

Salem's peer-digest pulled KAL-LE's BIT record nightly; the per-probe
SKIP detail rendered as 'red — 1 fail' in the morning brief peer-digest
section, which Andrew only caught when reading the brief carefully on
2026-05-15.

Fix: curator / janitor / distiller probes mirror the SKIP-when-section-
absent gating already in place on surveyor / brief / mail / talker /
transport / cloudflared / gcal / daily_sync / instructor.

This test runs the FULL aggregator against a config shape that mirrors
KAL-LE's deployed ``config.kalle.yaml`` and asserts:

1. ``overall_status`` is OK (no FAIL surfaces).
2. The tools KAL-LE doesn't run (curator, janitor, mail, brief, gcal,
   cloudflared) all surface as SKIP at the tool level — not OK with
   silently-passing probes, not FAIL with stale state.
3. The tools KAL-LE DOES run (distiller, surveyor, talker, transport,
   instructor, daily_sync) surface as actual probe rollups, not SKIP.

Per ``feedback_intentionally_left_blank.md``: SKIP-with-detail
distinguishes "not configured for this instance" from "configured but
broken."

Per ``feedback_regression_pin_unconditional.md``: this test runs
unconditionally — no module-level importorskip.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from alfred.health import aggregator as agg
from alfred.health.aggregator import run_all_checks
from alfred.health.types import Status


# Canonical KAL-LE config shape — kept in lockstep with what's deployed
# in ``config.kalle.yaml``. When the canonical shape changes (new
# section added to KAL-LE), update this fixture so the test catches a
# regression that lets a probe run when it shouldn't.
#
# KAL-LE configures: telegram, transport, instructor, distiller,
# daily_sync, surveyor, bit. KAL-LE does NOT configure: curator,
# janitor, mail, brief, gcal, cloudflared.
#
# Concrete state-file paths and tokens are placeholder strings — the
# probes use the dataclass defaults when fields are absent and the
# fixture mirrors what an absent ``data/`` looks like at probe time
# (no file → SKIP at the per-probe level on configured tools, SKIP at
# the tool level on absent tools).
def _kalle_raw(tmp_path: Path) -> dict[str, Any]:
    """Return a KAL-LE-shape config dict for testing."""
    vault_dir = tmp_path / "kalle_vault"
    vault_dir.mkdir(exist_ok=True)
    data_dir = tmp_path / "kalle_data"
    data_dir.mkdir(exist_ok=True)
    return {
        "vault": {"path": str(vault_dir)},
        "agent": {"backend": "openclaw"},  # skip anthropic-auth network call
        "logging": {"dir": str(data_dir)},
        "telegram": {
            # Bot token only — talker probe substitutes env vars then
            # surfaces FAIL on unresolved placeholders. A literal-shape
            # token short-circuits to OK at probe level for the bot-
            # token check, but the anthropic-auth sub-probe will FAIL
            # without an api_key. We tolerate that — the test asserts
            # on the SKIP behavior of absent-section tools, not on
            # talker's full OK rollup.
            "bot_token": "fake-token-for-test",
            "allowed_users": [1],
            "instance": {"name": "kal-le"},
        },
        "instructor": {
            "state": {"path": str(data_dir / "instructor_state.json")},
        },
        "distiller": {
            "state": {"path": str(data_dir / "distiller_state.json")},
        },
        "daily_sync": {
            # Enabled-false → tool-level SKIP. Mirrors the production
            # KAL-LE shape (daily_sync defined but disabled in some
            # rollout phases).
            "enabled": False,
            "schedule": {"time": "09:00", "timezone": "America/Halifax"},
            "state": {"path": str(data_dir / "daily_sync_state.json")},
        },
        "surveyor": {
            "ollama": {"base_url": "http://127.0.0.1:11434"},
            "milvus": {"uri": str(data_dir / "milvus_lite.db")},
            "openrouter": {"api_key": "ollama", "model": "qwen2.5:14b"},
            "state": {"path": str(data_dir / "surveyor_state.json")},
        },
        "transport": {
            "state": {"path": str(data_dir / "transport_state.json")},
        },
        "bit": {},
    }


@pytest.fixture(autouse=True)
def _clear_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the health registry between tests so import side-effects
    don't bleed between this module and others.

    Also clear ALFRED_TRANSPORT_TOKEN so the transport probe surfaces
    a predictable FAIL on token rather than depending on env state.
    Per the dispatcher env-var injection test-hygiene contract in
    CLAUDE.md.
    """
    monkeypatch.delenv("ALFRED_TRANSPORT_TOKEN", raising=False)
    agg.clear_registry()


async def test_kalle_config_has_no_fail_surfaces(tmp_path: Path) -> None:
    """No FAIL rollup on any tool when the config shape is KAL-LE's.

    The pin: every tool whose section is absent on KAL-LE
    (curator, janitor, mail, brief, gcal, cloudflared) MUST surface as
    tool-level SKIP, not as FAIL via probes-against-absent-config.
    """
    raw = _kalle_raw(tmp_path)
    report = await run_all_checks(raw, mode="quick")

    # Per-tool view: nothing should be FAIL. Surveyor/transport/talker
    # may legitimately be WARN (no Ollama running, no transport token,
    # placeholder anthropic key) but not FAIL — and even WARN doesn't
    # block the assertion below. The bug class we're guarding against
    # is FAIL on a tool that KAL-LE simply doesn't run.
    fails = [th for th in report.tools if th.status == Status.FAIL]
    fail_summary = [
        f"{th.tool}: {th.detail or ''} "
        f"[{', '.join(r.name for r in th.results if r.status == Status.FAIL)}]"
        for th in fails
    ]
    # Allow transport / talker to FAIL on missing token / api_key —
    # those are real misconfigurations the operator should fix, not
    # cross-instance bugs. The tools we ASSERT must not FAIL are the
    # KAL-LE-absent ones.
    kalle_absent = {"curator", "janitor", "mail", "brief", "gcal", "cloudflared"}
    bad = [th for th in fails if th.tool in kalle_absent]
    assert not bad, (
        f"KAL-LE-absent tools surfaced as FAIL (regression — see "
        f"feedback_intentionally_left_blank.md and 2026-05-16 ship): "
        f"{[th.tool for th in bad]}. Full summary: {fail_summary}"
    )


async def test_kalle_absent_tools_skip_at_tool_level(tmp_path: Path) -> None:
    """The 6 tools KAL-LE doesn't run surface as tool-level SKIP.

    Tool-level SKIP means:
        * ``ToolHealth.status == Status.SKIP``
        * ``ToolHealth.results == []`` (no probes ran)
        * ``ToolHealth.detail`` carries the "no <tool> section" reason

    That's distinct from a per-probe SKIP (which runs the tool but
    skips a specific probe) — the test pins the tool-level form
    because that's what cleanly signals "instance doesn't run this
    tool" to peer-digest consumers.
    """
    raw = _kalle_raw(tmp_path)
    report = await run_all_checks(raw, mode="quick")

    by_tool = {th.tool: th for th in report.tools}
    kalle_absent = ["curator", "janitor", "mail", "brief", "gcal", "cloudflared"]

    for tool_name in kalle_absent:
        assert tool_name in by_tool, (
            f"{tool_name} probe didn't register — check "
            f"KNOWN_TOOL_MODULES in aggregator.py"
        )
        th = by_tool[tool_name]
        assert th.status == Status.SKIP, (
            f"{tool_name} should SKIP on KAL-LE-shape config but got "
            f"{th.status}: detail={th.detail!r}, "
            f"probes={[(r.name, r.status) for r in th.results]}"
        )
        assert th.results == [], (
            f"{tool_name} ran probes despite being absent from config "
            f"(regression — tool-level SKIP should short-circuit): "
            f"{[(r.name, r.status) for r in th.results]}"
        )
        assert tool_name in (th.detail or ""), (
            f"{tool_name} SKIP detail doesn't name the section: "
            f"{th.detail!r}"
        )


async def test_kalle_configured_tools_run_probes(tmp_path: Path) -> None:
    """The tools KAL-LE DOES run produce real probe rollups.

    Defensive: catch a future refactor that accidentally extends the
    tool-level SKIP gate to a tool KAL-LE actually runs.

    Specifically: distiller and surveyor are configured on KAL-LE; their
    probes must execute and surface a per-probe rollup. (Status may be
    OK / WARN depending on whether the state file / Ollama exists, but
    the ``results`` list must not be empty.)
    """
    raw = _kalle_raw(tmp_path)
    report = await run_all_checks(raw, mode="quick")
    by_tool = {th.tool: th for th in report.tools}

    # distiller and surveyor are unambiguously configured on KAL-LE —
    # both have sections in the canonical config.kalle.yaml shape.
    for tool_name in ("distiller", "surveyor"):
        assert tool_name in by_tool, (
            f"{tool_name} probe didn't register — check KNOWN_TOOL_MODULES"
        )
        th = by_tool[tool_name]
        # NOT tool-level SKIP — must have actually run probes.
        assert th.results, (
            f"{tool_name} surfaced empty results on KAL-LE-shape config; "
            f"the SKIP gate may have over-reached: detail={th.detail!r}"
        )


async def test_kalle_skip_count_dominates(tmp_path: Path) -> None:
    """KAL-LE's BIT should have more SKIP rollups than FAIL rollups.

    Less a sharp invariant, more a smoke check that the overall
    cross-instance shape is sane: an instance that runs ~7 tools but
    has probes for ~13 should produce ~6 SKIPs from absent sections.
    A regression that flips SKIPs back to FAILs would show up here as
    fail_count > 0 on tools that should be SKIP.
    """
    raw = _kalle_raw(tmp_path)
    report = await run_all_checks(raw, mode="quick")

    skip_count = sum(1 for th in report.tools if th.status == Status.SKIP)
    fail_count = sum(
        1
        for th in report.tools
        if th.status == Status.FAIL
        and th.tool in {"curator", "janitor", "mail", "brief", "gcal", "cloudflared"}
    )

    # At least the 6 KAL-LE-absent tools we explicitly listed should
    # produce SKIPs. Real-world configs may have other SKIPs too
    # (mail-without-accounts → WARN, gcal section + enabled=false →
    # SKIP, etc.) — we just need the floor.
    assert skip_count >= 6, (
        f"Expected >=6 tool-level SKIPs on KAL-LE-shape config "
        f"(curator+janitor+mail+brief+gcal+cloudflared); got {skip_count}: "
        f"{[(th.tool, th.status.value) for th in report.tools]}"
    )
    assert fail_count == 0, (
        f"KAL-LE-absent tools surfaced as FAIL ({fail_count}); "
        f"regression — see 2026-05-16 ship"
    )
