"""Regression-pin tests for the SKILL capability-audit detector.

Per ``feedback_regression_pin_unconditional.md``: these MUST run
unconditionally — no module-level ``pytest.importorskip``. The audit
is pure-Python detection logic over bundled SKILL.md files and the
talker tool registry, both of which ship with the base install.

Coverage shape:
    * synthetic clean fixture (all registered tools mentioned) — assert
      ``is_clean`` and that the explicit "0 findings" summary line is
      emitted per ``feedback_intentionally_left_blank.md``.
    * synthetic gap fixture (one registered tool NOT mentioned) —
      assert the missing tool is surfaced AND that the rendered output
      carries the ``MISSING ADVERTISEMENT:`` line for it.
    * skill_missing case — SKILL.md path doesn't exist; audit returns
      ``skill_missing=True`` and treats every registered tool as
      unadvertised.
    * BIT probe status mapping — clean → OK, gap → WARN, missing
      SKILL → FAIL. Locks the operator-facing severity contract.

Reference incident: SKILL-capability awareness gaps (Apr 28 + May 2,
2026) where a new tool wired in code wasn't advertised to the agent
for ~3 days. This detector closes that loop at ship time / BIT
cycle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from alfred.health.types import Status
from alfred.telegram import skill_audit
from alfred.telegram.health import _check_skill_capability_audit


def _write_synthetic_skill(
    skills_root: Path,
    bundle: str,
    body: str,
) -> Path:
    """Drop a SKILL.md at ``skills_root/<bundle>/SKILL.md`` and return path."""
    bundle_dir = skills_root / bundle
    bundle_dir.mkdir(parents=True, exist_ok=True)
    skill_path = bundle_dir / "SKILL.md"
    skill_path.write_text(body, encoding="utf-8")
    return skill_path


def _patch_skills_dir(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Redirect ``get_skills_dir`` so ``audit_skill`` reads our synthetic SKILLs.

    Patch on the ``skill_audit`` module (where it's imported) not the
    canonical ``alfred._data`` — the function reference is captured at
    import time.
    """
    monkeypatch.setattr(skill_audit, "get_skills_dir", lambda: root)


def _patch_registered_tools(
    monkeypatch: pytest.MonkeyPatch,
    names: list[str],
) -> None:
    """Stub the per-tool-set registry to a known synthetic list.

    Lets the test author control which tool names the audit sees
    without coupling to whatever ``conversation.tools_for_set``
    happens to return today. The audit's job is "tool-name X
    appears in SKILL body Y/N" — that's what's under test, not the
    registry composition.
    """
    monkeypatch.setattr(
        skill_audit,
        "_registered_tool_names",
        lambda tool_set, gcal_enabled, recall_ask_enabled=False: list(names),
    )


def _base_raw_config(tool_set: str = "talker", instance_name: str = "Salem") -> dict[str, Any]:
    """Minimal unified-config dict that passes the talker config loader.

    Only the fields the audit reads matter — ``telegram.instance``
    (for name/tool_set/skill_bundle) and ``telegram.bot_token`` /
    ``allowed_users`` are tolerated as empty. The config loader needs
    ``instance.name`` (required field, no default per the
    no-Alfred-default rule).
    """
    skill_bundle = {
        "talker": "vault-talker",
        "kalle": "vault-kalle",
        "hypatia": "vault-hypatia",
    }.get(tool_set, "vault-talker")
    return {
        "vault": {"path": "/tmp/fake-vault"},
        "telegram": {
            "bot_token": "dummy",
            "allowed_users": [1],
            "anthropic": {"api_key": "DUMMY", "model": "claude-haiku-4-5"},
            "stt": {"api_key": "DUMMY", "provider": "groq"},
            "instance": {
                "name": instance_name,
                "canonical": instance_name.upper(),
                "skill_bundle": skill_bundle,
                "tool_set": tool_set,
            },
        },
    }


# --- _resolve_recall_ask_enabled: direct pins ---------------------------------
#
# The audit's raw-dict resolver had NO direct coverage — only its consumer
# (``_registered_tool_names``, pinned below) was tested, so the fail-closed
# contract rested on an untested ``except Exception`` swallow. That matters
# beyond tidiness: this is the resolver the STAY-C fence flows through on
# the capability-audit path, so a silent True would report ``recall_peers``
# as advertised on a categorically-fenced instance. Mirrors the three pins
# its twin ``conversation._resolve_recall_ask_enabled_for_run_turn`` already
# carries (tests/telegram/test_recall_peers_tool.py).


def test_resolve_recall_ask_enabled_true_when_ask_peers_configured() -> None:
    """A populated ``transport.recall.ask.peers`` surfaces the tool."""
    raw = _base_raw_config()
    raw["transport"] = {
        "recall": {"ask": {"peers": {"kal-le": {"types": ["note"]}}}},
    }
    assert skill_audit._resolve_recall_ask_enabled(raw) is True


def test_resolve_recall_ask_enabled_false_when_ask_peers_empty() -> None:
    """Fail-closed: no ask edges means the tool is never advertised."""
    raw = _base_raw_config()
    raw["transport"] = {"recall": {"ask": {"peers": {}}}}
    assert skill_audit._resolve_recall_ask_enabled(raw) is False

    no_block = _base_raw_config()
    assert skill_audit._resolve_recall_ask_enabled(no_block) is False


def test_resolve_recall_ask_enabled_false_on_stayc_fence_raise() -> None:
    """The STAY-C fence raises at load; the resolver must swallow to False.

    A fenced instance surfaces NO recall tool, so the audit must not
    report ``recall_peers`` as advertised for it. Pins the fail-closed
    swallow that the ``except Exception`` branch implements.
    """
    raw = _base_raw_config()
    raw["transport"] = {
        "recall": {"ask": {"peers": {"stay-c": {"types": ["note"]}}}},
    }
    # Sanity: this config really does raise in the transport loader.
    from alfred.transport.config import (
        RecallConfigError, load_from_unified as load_transport,
    )
    with pytest.raises(RecallConfigError):
        load_transport(raw)

    assert skill_audit._resolve_recall_ask_enabled(raw) is False


def test_registered_tool_names_threads_recall_ask_enabled() -> None:
    """#16 item 15: recall_ask_enabled must reach tools_for_set, or recall_peers is
    INVISIBLE to the capability audit (can't confirm it's advertised). Uses the REAL
    _registered_tool_names (not the patched stub)."""
    with_recall = skill_audit._registered_tool_names(
        "talker", gcal_enabled=False, recall_ask_enabled=True
    )
    without_recall = skill_audit._registered_tool_names(
        "talker", gcal_enabled=False, recall_ask_enabled=False
    )
    assert "recall_peers" in with_recall
    assert "recall_peers" not in without_recall


# --- audit_skill: clean / gap / skill_missing ---------------------------------


def test_audit_clean_when_all_tools_advertised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When SKILL.md mentions every registered tool, audit reports clean.

    Validates the "intentionally left blank" contract: the explicit
    summary line MUST be emitted on clean runs so idle is
    distinguishable from broken.
    """
    skills_root = tmp_path / "skills"
    _write_synthetic_skill(
        skills_root,
        "vault-talker",
        # Body advertises both tools; backticks aren't required per
        # the literal-substring spec, mix-and-match to exercise both.
        "You have access to `vault_search` to search the vault and "
        "vault_read to read a record by path.\n",
    )
    _patch_skills_dir(monkeypatch, skills_root)
    _patch_registered_tools(monkeypatch, ["vault_search", "vault_read"])

    raw = _base_raw_config(tool_set="talker", instance_name="Salem")
    result = skill_audit.audit_skill(raw)

    assert result.is_clean
    assert result.missing_advertisements == []
    assert sorted(result.advertised) == ["vault_read", "vault_search"]
    assert result.instance_name == "Salem"
    assert result.tool_set == "talker"
    assert result.skill_bundle == "vault-talker"
    assert not result.skill_missing
    # The explicit "ran, nothing to do" signal — per
    # feedback_intentionally_left_blank.md.
    assert "0 findings" in result.summary_line
    assert "instance=Salem" in result.summary_line
    assert "tools=2" in result.summary_line


def test_audit_gap_flags_missing_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registered tool absent from SKILL body → flagged as missing."""
    skills_root = tmp_path / "skills"
    _write_synthetic_skill(
        skills_root,
        "vault-hypatia",
        # vault_search is advertised; gcal_list_events is NOT — this
        # mirrors the Hypatia gap-of-record (post-Phase-A+ GCal ship).
        "You can use vault_search to find records.\n",
    )
    _patch_skills_dir(monkeypatch, skills_root)
    _patch_registered_tools(
        monkeypatch,
        ["vault_search", "gcal_list_events"],
    )

    raw = _base_raw_config(tool_set="hypatia", instance_name="Hypatia")
    result = skill_audit.audit_skill(raw)

    assert not result.is_clean
    assert result.missing_advertisements == ["gcal_list_events"]
    assert result.advertised == ["vault_search"]
    assert "1 findings" in result.summary_line

    rendered = skill_audit.render_audit(result)
    assert "MISSING ADVERTISEMENT" in rendered
    assert "gcal_list_events" in rendered
    assert "vault-hypatia" in rendered
    assert "tool_set='hypatia'" in rendered
    # The summary line must come first per the output contract.
    first_line = rendered.split("\n", 1)[0]
    assert "0 findings" not in first_line
    assert "1 findings" in first_line


def test_audit_skill_missing_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SKILL.md absent → worst case: every registered tool is unadvertised."""
    skills_root = tmp_path / "skills"
    # Deliberately don't write any SKILL.md.
    _patch_skills_dir(monkeypatch, skills_root)
    _patch_registered_tools(monkeypatch, ["vault_search", "vault_read"])

    raw = _base_raw_config(tool_set="talker", instance_name="Salem")
    result = skill_audit.audit_skill(raw)

    assert result.skill_missing
    assert not result.is_clean
    assert sorted(result.missing_advertisements) == ["vault_read", "vault_search"]
    assert result.advertised == []

    rendered = skill_audit.render_audit(result)
    assert "SKILL MISSING" in rendered
    assert "vault-talker" in rendered


def test_audit_skip_when_telegram_section_absent() -> None:
    """No ``telegram`` block → audit returns ``instance_missing=True``.

    Caller (CLI / BIT probe) surfaces as a SKIP-style message rather
    than a finding. Locks the contract that the audit is non-fatal
    when there's no instance to audit.
    """
    result = skill_audit.audit_skill({})
    assert result.instance_missing
    assert result.is_clean  # SKIP counts as clean for exit-code purposes
    assert "no telegram section" in result.instance_missing_reason.lower()
    assert "skipped" in result.summary_line.lower()


def test_audit_skip_when_instance_name_missing() -> None:
    """``telegram.instance`` block without ``name`` → instance_missing.

    Mirrors the per-instance-defaults rule: we won't silently default
    to "Alfred". The audit reports SKIP rather than crashing.
    """
    raw = {
        "telegram": {
            "bot_token": "DUMMY",
            "anthropic": {"api_key": "DUMMY"},
            # No instance block — config loader's TypeError is caught.
        },
    }
    result = skill_audit.audit_skill(raw)
    assert result.instance_missing
    assert result.is_clean
    assert "incomplete" in result.instance_missing_reason


def test_bit_probe_skip_when_telegram_section_absent() -> None:
    """No telegram section → BIT probe returns SKIP (not WARN/FAIL).

    Critical for the rollup: a SKIP cascades cleanly through
    ``Status.worst`` alongside other section-absent SKIPs. A WARN
    here would cause the talker rollup to WARN on an instance with
    no talker configured — false alarm.
    """
    check = _check_skill_capability_audit({})
    assert check.status == Status.SKIP
    assert "no telegram section" in check.detail.lower()


def test_audit_kalle_tool_set_loads_kalle_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``instance.tool_set='kalle'`` → audit reads from ``vault-kalle/SKILL.md``.

    Locks the multi-instance routing — a regression that defaulted
    every audit to ``vault-talker`` would silently report KAL-LE as
    "clean" against Salem's SKILL.
    """
    skills_root = tmp_path / "skills"
    _write_synthetic_skill(
        skills_root,
        "vault-kalle",
        "You have `bash_exec` and `vault_create` available.\n",
    )
    _patch_skills_dir(monkeypatch, skills_root)
    _patch_registered_tools(monkeypatch, ["bash_exec", "vault_create"])

    raw = _base_raw_config(tool_set="kalle", instance_name="KAL-LE")
    result = skill_audit.audit_skill(raw)

    assert result.is_clean
    assert result.skill_bundle == "vault-kalle"
    assert result.skill_path.parent.name == "vault-kalle"


# --- BIT probe status mapping ----------------------------------------------


def test_bit_probe_ok_on_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clean audit → probe returns OK with explicit detail."""
    skills_root = tmp_path / "skills"
    _write_synthetic_skill(
        skills_root,
        "vault-talker",
        "Tools: vault_search, vault_read.\n",
    )
    _patch_skills_dir(monkeypatch, skills_root)
    _patch_registered_tools(monkeypatch, ["vault_search", "vault_read"])

    raw = _base_raw_config(tool_set="talker", instance_name="Salem")
    check = _check_skill_capability_audit(raw)

    assert check.status == Status.OK
    assert check.name == "skill-capability-audit"
    assert "2 tools advertised" in check.detail
    assert check.data["registered_count"] == 2
    assert check.data["advertised_count"] == 2


def test_bit_probe_warn_on_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit with missing advertisement → probe returns WARN.

    WARN (not FAIL) because the daemon is functional; the SKILL is
    just out-of-sync. The probe detail names the missing tools so
    BIT records are grep-able for the gap.
    """
    skills_root = tmp_path / "skills"
    _write_synthetic_skill(
        skills_root,
        "vault-hypatia",
        "Use vault_search.\n",
    )
    _patch_skills_dir(monkeypatch, skills_root)
    _patch_registered_tools(
        monkeypatch,
        ["vault_search", "gcal_list_events"],
    )

    raw = _base_raw_config(tool_set="hypatia", instance_name="Hypatia")
    check = _check_skill_capability_audit(raw)

    assert check.status == Status.WARN
    assert "gcal_list_events" in check.detail
    assert check.data["missing_count"] == 1
    assert check.data["missing_tools"] == ["gcal_list_events"]
    assert check.data["tool_set"] == "hypatia"


def test_bit_probe_fail_when_skill_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing SKILL.md → probe returns FAIL (broken, not stale)."""
    skills_root = tmp_path / "skills"
    # No SKILL written.
    _patch_skills_dir(monkeypatch, skills_root)
    _patch_registered_tools(monkeypatch, ["vault_search"])

    raw = _base_raw_config(tool_set="talker", instance_name="Salem")
    check = _check_skill_capability_audit(raw)

    assert check.status == Status.FAIL
    assert "missing" in check.detail.lower()
    assert check.data["missing_count"] == 1


# --- TypeError attribution: drift is NOT "instance incomplete" -------------
#
# The bug (fixed 2026-08-20): ``audit_skill`` wrapped ``load_talker_config``
# in an ``except TypeError`` documented for the missing-``instance.name``
# case, but it caught EVERY TypeError — including the unknown-keyword kind
# raised by config drift — and reported all of them as ``instance_missing``
# with the reason "telegram.instance config incomplete". The probe then
# returned SKIP, and SKIP is in ``QUIET_HEALTH_STATUSES``, so a talker whose
# daemon could not start produced a health surface that was simultaneously
# SILENT and WRONG about the cause.
#
# These four pins drive BOTH arms. The two arms are near neighbours by
# construction — same exception TYPE out of the same call — which is exactly
# why "did it refuse" is not a sufficient assertion here and each pin asserts
# the REASON.


def _drift_raw_config() -> dict[str, Any]:
    """A config whose ``instance`` block is FINE but which fails to load.

    The unknown-key spelling is SAMPLED, not minted: it is the same literal
    ``tests/telegram/test_stt_shadow_config_compat.py`` uses for its
    ``test_unknown_stt_key_still_raises`` positive control, which is the
    established probe for "``_build`` hands unknown keys to the constructor".

    A synthetic unknown key (rather than a really-retired field) is the
    correct instrument here: the real drift shape — a deployed YAML carrying
    ``telegram.stt.shadow_capture`` after that field is removed — cannot be
    reproduced while the field still exists, and the TypeError this raises is
    the identical shape (``got an unexpected keyword argument``).
    """
    raw = _base_raw_config(tool_set="talker", instance_name="Salem")
    raw["telegram"]["stt"] = {"shadow_capture_retired_probe": {"enabled": True}}
    return raw


def test_config_drift_premise_the_loader_really_raises() -> None:
    """PREMISE PIN for the three pins below: this config raises at load.

    Everything below asserts how a TypeError is ATTRIBUTED. If ``_build``
    ever grows an unknown-key filter, this config would load cleanly and the
    pins below would be asserting attribution of an exception that no longer
    happens. This pin makes that failure legible instead of mysterious.
    """
    from alfred.telegram.config import load_from_unified as load_talker_config

    with pytest.raises(TypeError, match="shadow_capture_retired_probe"):
        load_talker_config(_drift_raw_config())


def test_config_drift_is_not_misattributed_to_instance_missing() -> None:
    """Drift → ``config_error``, and the reason does NOT blame the instance.

    Asserting the WHY, not merely that the audit declined to run: before the
    fix this returned ``instance_missing=True`` with an
    "instance config incomplete" reason, which is indistinguishable from the
    genuine missing-name case if you only check that no audit happened.
    """
    result = skill_audit.audit_skill(_drift_raw_config())

    assert result.config_error is True
    assert result.instance_missing is False
    # The specific false claim the bug emitted. Its ABSENCE is the fix.
    assert "instance config incomplete" not in result.config_error_reason
    # And the reason names the real cause.
    assert "TypeError" in result.config_error_reason
    assert "shadow_capture_retired_probe" in result.config_error_reason
    # A failed load is NOT a clean audit — the CLI exits non-zero on this.
    assert result.is_clean is False


def test_bit_probe_config_drift_is_not_quiet() -> None:
    """Drift → WARN, and WARN is an ATTENTION status by the canonical predicate.

    The load-bearing property is not the literal ``WARN`` — it is that the
    status is NOT in ``QUIET_HEALTH_STATUSES``. Asserted through
    ``is_attention_status`` (the canonical predicate per CLAUDE.md's
    health-status rule) rather than a re-localized ``!= "ok"`` comparison, so
    this pin keeps its meaning if the severity is ever retuned.
    """
    from alfred.brief.health_section import is_attention_status

    check = _check_skill_capability_audit(_drift_raw_config())

    assert check.status == Status.WARN
    assert is_attention_status(check.status.value) is True
    assert "did not run" in check.detail
    assert "instance config incomplete" not in check.detail


def test_bit_probe_missing_instance_name_still_skips_quietly() -> None:
    """POSITIVE CONTROL — the documented case still SKIPs, and stays quiet.

    Without this leg the pins above would read identically against a build
    that had simply made EVERY TypeError loud, which would be a different
    bug: an unconfigured instance is genuinely nothing to audit, and turning
    it into a daily attention card is the noise the SKIP status exists to
    prevent. This is the nearest admissible neighbour of the drift case —
    same exception type, same call site, opposite correct outcome.
    """
    from alfred.brief.health_section import is_attention_status

    raw = _base_raw_config(tool_set="talker", instance_name="Salem")
    # Remove ONLY the name key — the trigger the except-branch documents.
    del raw["telegram"]["instance"]["name"]

    result = skill_audit.audit_skill(raw)
    assert result.instance_missing is True
    assert result.config_error is False

    check = _check_skill_capability_audit(raw)
    assert check.status == Status.SKIP
    assert is_attention_status(check.status.value) is False


# --- Live regression — bundled SKILLs match registered tools ---------------


@pytest.mark.parametrize(
    "tool_set,instance_name",
    [
        ("talker", "Salem"),
        ("kalle", "KAL-LE"),
        # NOTE: Hypatia intentionally NOT pinned here — Hypatia is the
        # known gap-of-record (per task brief), and the operator hasn't
        # yet decided what to advertise. Pinning Hypatia clean would
        # mask the very signal this detector is designed to surface.
        # When operator action closes the Hypatia gaps, add it back to
        # this parametrize list as a lockstep pin.
    ],
)
def test_bundled_skill_audit_runs_against_real_bundles(
    tool_set: str,
    instance_name: str,
) -> None:
    """Smoke test against the real bundled SKILL.md files.

    Verifies the audit pipeline (config load → tool registry lookup →
    SKILL read → classify) executes end-to-end against the production
    bundle. The shape of the result is asserted; the specific
    findings (or lack thereof) are NOT pinned because:

        * Findings shift as the SKILL is updated and as new tools are
          wired — pinning the list here would force every legitimate
          tool addition into a two-line test change.
        * The pin is enforced via the BIT cycle in production, not
          via this test. The test's job is "audit runs to completion
          and returns a structured result."

    Hypatia is excluded — see parametrize NOTE.
    """
    raw = _base_raw_config(tool_set=tool_set, instance_name=instance_name)
    # Use the real bundled SKILLs (no _patch_skills_dir) and real
    # registry (no _patch_registered_tools).
    result = skill_audit.audit_skill(raw)

    # Shape assertions only — content is environment-dependent.
    assert isinstance(result.registered_tools, list)
    assert len(result.registered_tools) > 0, (
        f"tool_set={tool_set!r} surfaced zero tools — registry empty?"
    )
    assert isinstance(result.advertised, list)
    assert isinstance(result.missing_advertisements, list)
    assert (
        len(result.advertised) + len(result.missing_advertisements)
        == len(result.registered_tools)
    )
    # Summary line always non-empty (intentionally-left-blank contract).
    assert result.summary_line.startswith("skill-audit: ")
