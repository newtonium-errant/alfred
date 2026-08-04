"""Autofix is wired into run_sweep — and only mutates when allowed.

BACKGROUND. ``autofix.py`` was orphaned by the 2026-05-25 backend-
abstraction-collapse: zero call sites, never executed, but still carrying
6 passing unit tests. Then the 2026-06-25 agent-routing filter began
HIDING the codes autofix was supposed to handle from the LLM agent, on
the stated premise that they were "handled deterministically by the
scanner + autofix". With autofix dead that premise was false, and
FM001-004 / LINK002 had NO remediation path at all — detected every
sweep, fixed by nobody. That is the mechanism behind the brief's
"9 sweeps, 0 files fixed".

Wiring it makes the filter's premise true. The risk that arrives with it
is that autofix WRITES to the vault, so the gating is now safety-critical:
a ``scan`` (structural_only) must remain read-only. That is the first
test below, and it is the one that matters most.

The 6 pre-existing autofix unit tests become LIVE pins the moment this
lands. They were green while the code never ran — the condition under
which green means least — so this suite drives autofix THROUGH run_sweep
rather than trusting them.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from textwrap import dedent

import pytest
import structlog

from alfred.janitor.config import (
    JanitorConfig,
    StateConfig,
    SweepConfig,
    VaultConfig,
)
from alfred.janitor.issues import AUTOFIX_FIXABLE_CODES, IssueCode
from alfred.janitor.state import JanitorState


def _build_config(vault: Path, state_dir: Path) -> JanitorConfig:
    return JanitorConfig(
        vault=VaultConfig(
            path=str(vault),
            ignore_dirs=[".obsidian", "_templates", "_bases"],
            ignore_files=[".gitkeep"],
        ),
        sweep=SweepConfig(),
        state=StateConfig(path=str(state_dir / "janitor_state.json")),
    )


def _write_record(vault: Path, rel: str, frontmatter: str, body: str = "") -> None:
    full = vault / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")


def _vault_fingerprint(vault: Path) -> dict[str, str]:
    """Content hash of every file — detects ANY mutation, not just size."""
    return {
        str(p.relative_to(vault)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(vault.rglob("*.md"))
    }


def _fm001_vault(tmp_path: Path):
    """A record missing ``created`` — an FM001, which autofix repairs."""
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_record(
        vault, "person/No Created.md",
        dedent(
            """\
            type: person
            name: No Created
            status: active
            tags: []
            """
        ).rstrip(),
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config = _build_config(vault, state_dir)
    state = JanitorState(config.state.path, config.state.max_sweep_history)
    skills_dir = tmp_path / "skills"
    (skills_dir / "vault-janitor").mkdir(parents=True)
    (skills_dir / "vault-janitor" / "SKILL.md").write_text("# t\n", encoding="utf-8")
    return config, state, skills_dir, vault


# --------------------------------------------------------------------
# SAFETY — a read-only sweep must stay read-only
# --------------------------------------------------------------------

def test_structural_only_sweep_mutates_nothing(tmp_path: Path) -> None:
    """THE SAFETY PIN. ``structural_only`` must not write to the vault.

    Autofix mutates records. Wiring it into run_sweep means a gating
    mistake turns ``alfred janitor scan`` — which an operator reasonably
    expects to be a read — into a silent bulk rewrite. Fingerprints every
    file's CONTENT before and after, so any edit is caught, not just a
    created/deleted file.
    """
    from alfred.janitor import daemon as daemon_mod

    config, state, skills_dir, vault = _fm001_vault(tmp_path)
    before = _vault_fingerprint(vault)

    result = asyncio.run(
        daemon_mod.run_sweep(config, state, skills_dir, structural_only=True)
    )

    assert _vault_fingerprint(vault) == before, (
        "structural_only sweep mutated the vault — autofix gating is wrong"
    )
    # And it genuinely had something it COULD have fixed, so the pin is
    # not passing vacuously on an empty issue set.
    assert any(i.code in AUTOFIX_FIXABLE_CODES for i in result.issues)


def test_structural_only_wins_even_when_fix_mode_is_true(tmp_path: Path) -> None:
    """ISOLATES the ``structural_only`` half of the gate.

    The test above leaves ``fix_mode`` at its default False, so BOTH
    gates are shut and it would still pass against a build whose autofix
    branch read only ``if fix_mode:``. This one sets fix_mode=True and
    structural_only=True together, so ``not structural_only`` is the ONLY
    thing standing between the sweep and a vault write. Without it the
    combination that a caller might plausibly pass — "fix things, but
    just scan for now" — silently rewrites records.
    """
    from alfred.janitor import daemon as daemon_mod

    config, state, skills_dir, vault = _fm001_vault(tmp_path)
    before = _vault_fingerprint(vault)

    asyncio.run(
        daemon_mod.run_sweep(
            config, state, skills_dir, structural_only=True, fix_mode=True,
        )
    )
    assert _vault_fingerprint(vault) == before, (
        "structural_only did not override fix_mode — autofix wrote to the "
        "vault during a scan-only sweep"
    )


def test_fix_mode_false_mutates_nothing(tmp_path: Path) -> None:
    """``fix_mode=False`` is the other half of the gate."""
    from alfred.janitor import daemon as daemon_mod

    config, state, skills_dir, vault = _fm001_vault(tmp_path)
    before = _vault_fingerprint(vault)

    asyncio.run(
        daemon_mod.run_sweep(
            config, state, skills_dir, structural_only=False, fix_mode=False,
        )
    )
    assert _vault_fingerprint(vault) == before


# --------------------------------------------------------------------
# The wiring actually runs (this is what was dead)
# --------------------------------------------------------------------

def test_fix_mode_autofix_repairs_the_record(tmp_path: Path, monkeypatch) -> None:
    """END-TO-END: fix_mode drives autofix through the production entry.

    The pin class that proves the module is WIRED, not merely present —
    per-layer unit tests on autofix passed for months while it never ran.
    The agent backend is stubbed out so this isolates the autofix phase.
    """
    from alfred.janitor import daemon as daemon_mod

    config, state, skills_dir, vault = _fm001_vault(tmp_path)
    before = _vault_fingerprint(vault)

    # Neutralise the agent phase — we are pinning autofix only.
    monkeypatch.setattr(daemon_mod, "_load_skill", lambda d: "")

    result = asyncio.run(
        daemon_mod.run_sweep(
            config, state, skills_dir, structural_only=False, fix_mode=True,
        )
    )

    assert _vault_fingerprint(vault) != before, (
        "fix_mode sweep did NOT mutate — autofix is still not wired"
    )
    assert result.files_fixed >= 1
    # The repair is the real one: ``created`` now present.
    text = (vault / "person/No Created.md").read_text(encoding="utf-8")
    assert "created:" in text


def test_autofix_receives_only_autofix_fixable_codes(
    tmp_path: Path, monkeypatch,
) -> None:
    """Autofix must be handed ONLY its own codes.

    Passing it LINK001/DIR001/STUB001 would have it walk issues it
    cannot repair and, for the flag-only codes, write janitor_notes as a
    side effect of a phase that is supposed to be scoped to repairs.
    """
    from alfred.janitor import daemon as daemon_mod

    config, state, skills_dir, _vault = _fm001_vault(tmp_path)
    monkeypatch.setattr(daemon_mod, "_load_skill", lambda d: "")

    seen: list[IssueCode] = []
    real = daemon_mod.autofix_issues

    def _spy(issues, vault_path, session_path):
        seen.extend(i.code for i in issues)
        return real(issues, vault_path, session_path)

    monkeypatch.setattr(daemon_mod, "autofix_issues", _spy)
    asyncio.run(
        daemon_mod.run_sweep(
            config, state, skills_dir, structural_only=False, fix_mode=True,
        )
    )

    assert seen, "autofix was never invoked"
    assert all(c in AUTOFIX_FIXABLE_CODES for c in seen), (
        f"autofix received non-fixable codes: "
        f"{sorted({c.value for c in seen} - {c.value for c in AUTOFIX_FIXABLE_CODES})}"
    )


# --------------------------------------------------------------------
# ILB signals
# --------------------------------------------------------------------

def test_autofix_complete_signal_has_its_fields(
    tmp_path: Path, monkeypatch,
) -> None:
    """Pins the event AND its counters — a dropped field breaks the
    operator's grep as surely as a dropped event."""
    from alfred.janitor import daemon as daemon_mod

    config, state, skills_dir, _vault = _fm001_vault(tmp_path)
    monkeypatch.setattr(daemon_mod, "_load_skill", lambda d: "")

    with structlog.testing.capture_logs() as captured:
        asyncio.run(
            daemon_mod.run_sweep(
                config, state, skills_dir, structural_only=False, fix_mode=True,
            )
        )

    matches = [c for c in captured if c.get("event") == "sweep.autofix_complete"]
    assert len(matches) == 1
    ev = matches[0]
    assert ev["files_fixed"] >= 1
    assert ev["targets"] >= 1
    assert "files_flagged" in ev
    assert "files_skipped" in ev


def test_no_autofix_targets_signal_when_nothing_to_fix(
    tmp_path: Path, monkeypatch,
) -> None:
    """ILB: an all-agent-actionable sweep says 'ran, nothing to autofix'
    rather than leaving the phase silent (silence reads as broken)."""
    from alfred.janitor import daemon as daemon_mod

    vault = tmp_path / "vault"
    vault.mkdir()
    # Complete record whose only defect is a broken wikilink (LINK001) —
    # agent-actionable, nothing for autofix.
    _write_record(
        vault, "person/Complete.md",
        dedent(
            """\
            type: person
            name: Complete
            status: active
            created: 2026-01-01
            tags: []
            related:
            - '[[person/Does Not Exist]]'
            """
        ).rstrip(),
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config = _build_config(vault, state_dir)
    state = JanitorState(config.state.path, config.state.max_sweep_history)
    skills_dir = tmp_path / "skills"
    (skills_dir / "vault-janitor").mkdir(parents=True)
    (skills_dir / "vault-janitor" / "SKILL.md").write_text("# t\n", encoding="utf-8")
    monkeypatch.setattr(daemon_mod, "_load_skill", lambda d: "")

    with structlog.testing.capture_logs() as captured:
        result = asyncio.run(
            daemon_mod.run_sweep(
                config, state, skills_dir, structural_only=False, fix_mode=True,
            )
        )

    assert not any(i.code in AUTOFIX_FIXABLE_CODES for i in result.issues)
    matches = [c for c in captured if c.get("event") == "sweep.no_autofix_targets"]
    assert len(matches) == 1
    assert matches[0]["issues_found"] == result.issues_found
