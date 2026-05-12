"""SKILL capability-audit detector.

Safety net for the standing operator-discipline rule documented in
``CLAUDE.md``:

    Feature-enabling commits trigger a SKILL capability audit in the
    same cycle. When the builder ENABLES a new capability (peer protocol
    wired, GCal write-through shipped, image vision online, new instance
    addressable), the agent-facing instructions may not advertise it —
    the talker will say "I can't do that yet" three days after the
    feature went live.

The audit compares the talker's **runtime tool registry** (the schemas
the model actually sees, selected per-instance via
``telegram.instance.tool_set`` + ``gcal.enabled``) against the
**advertised tool surface** in that instance's bundled ``SKILL.md``.
Tools present in the registry but absent from the SKILL body are
flagged so the operator can decide what to advertise.

Detection is **literal**, not semantic — a tool counts as "advertised"
if its registry ``name`` (e.g. ``vault_search``) appears anywhere in
the SKILL body, code blocks and prose alike. The SKILL body is the
LLM's source of truth; any literal match is sufficient to consider
the capability discoverable.

Used by:
    * ``alfred talker skill-audit`` CLI (operator-runs)
    * ``skill-capability-audit`` BIT probe (surfaces every BIT cycle)

Both surfaces call :func:`audit_skill` so the logic is single-sourced.

Reference incidents (per ``CLAUDE.md`` "Feature-enabling commits"):
    * Apr 28 2026 — "Hypatia is just a session name" three days after
      Hypatia config-layer launch.
    * May 2 2026 — "no calendar integration wired up yet" three days
      after Phase A+ GCal R/W shipped.

Both gaps were SKILL-layer awareness lapses, not code-layer breakage.
This detector would have caught both at ship time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alfred._data import get_skills_dir
from alfred.telegram.config import load_from_unified as load_talker_config


@dataclass
class AuditResult:
    """Outcome of one SKILL capability audit.

    ``missing_advertisements`` is the headline finding: tools the model
    can call that the SKILL doesn't mention. Each entry is the tool's
    registry ``name`` (e.g. ``"gcal_list_events"``).

    ``advertised`` is the inverse — tools both registered AND mentioned.
    ``orphaned_advertisements`` (lower-priority inverse check) is
    tool-like substrings the SKILL mentions but that aren't in the
    registry for this tool_set; currently always empty because the
    "tool-like substring" heuristic is too imprecise to flag without
    false positives (deferred — see module-level NOTE).

    ``skill_path`` is the resolved SKILL.md path actually read.
    ``skill_missing`` is True when that file doesn't exist (treated
    as worst-case: every registered tool is unadvertised).
    """

    instance_name: str
    tool_set: str
    skill_bundle: str
    skill_path: Path
    skill_missing: bool
    # ``instance_missing`` distinguishes "operator hasn't configured a
    # talker instance" (no ``telegram.instance`` block, or block
    # without ``name``) from "instance configured but SKILL drifted."
    # The CLI surfaces a SKIP-style message; the BIT probe emits
    # ``Status.SKIP``. Cannot reuse ``skill_missing`` for this — the
    # two conditions deserve different operator action (configure
    # instance vs update SKILL).
    instance_missing: bool = False
    instance_missing_reason: str = ""
    registered_tools: list[str] = field(default_factory=list)
    advertised: list[str] = field(default_factory=list)
    missing_advertisements: list[str] = field(default_factory=list)
    orphaned_advertisements: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """True when the audit found no missing advertisements.

        Treats ``instance_missing`` as "clean" — there's nothing to
        audit. CLI exit-code uses this; BIT probe uses
        ``instance_missing`` directly to pick SKIP vs OK/WARN.
        """
        if self.instance_missing:
            return True
        return not self.missing_advertisements and not self.skill_missing

    @property
    def summary_line(self) -> str:
        """The single-line "intentionally left blank" success signal.

        Per ``feedback_intentionally_left_blank.md`` — silent success
        is indistinguishable from broken. The audit MUST emit this
        even when there are no findings.
        """
        if self.instance_missing:
            return (
                f"skill-audit: skipped — {self.instance_missing_reason}"
            )
        return (
            f"skill-audit: {len(self.missing_advertisements)} findings "
            f"(instance={self.instance_name}, "
            f"tools={len(self.registered_tools)}, "
            f"advertised={len(self.advertised)})"
        )


def _resolve_gcal_enabled(raw: dict[str, Any]) -> bool:
    """Lazy-resolve ``gcal.enabled`` from the raw unified config.

    Mirrors ``conversation._resolve_gcal_enabled_for_run_turn`` but
    reads off the already-parsed raw dict rather than re-opening the
    file. Failure (missing module, missing section) is non-fatal —
    returns False so ``gcal_list_events`` is omitted from the
    registered tool list, matching the runtime fallback.
    """
    try:
        from alfred.integrations.gcal_config import (
            load_from_unified as load_gcal,
        )
        return bool(load_gcal(raw).enabled)
    except Exception:  # noqa: BLE001
        # Missing integration module, malformed section, etc. — treat
        # as "gcal not enabled" and proceed. Misconfig surfaces via
        # other health probes; the audit is downstream of the wiring.
        return False


def _registered_tool_names(
    tool_set: str,
    gcal_enabled: bool,
) -> list[str]:
    """Return the ordered list of tool names the model would see.

    Lazy import: ``conversation.tools_for_set`` is the source of truth
    for the per-instance tool list. Reusing it (rather than
    re-enumerating the registry tables) means the audit stays in
    lockstep with runtime — if a new tool is wired into ``KALLE_VAULT_TOOLS``
    tomorrow, the audit picks it up automatically.
    """
    from alfred.telegram.conversation import tools_for_set
    tools = tools_for_set(tool_set, gcal_enabled=gcal_enabled)
    # Each tool schema dict has a ``name`` field — the canonical tool
    # name passed to the Anthropic Messages API. That's what gets
    # grepped against the SKILL body.
    names: list[str] = []
    for t in tools:
        n = t.get("name")
        if isinstance(n, str) and n and n not in names:
            names.append(n)
    return names


def _classify(
    skill_text: str,
    registered: list[str],
) -> tuple[list[str], list[str]]:
    """Split ``registered`` into (advertised, missing) by literal grep.

    Case-sensitive substring match — tool names are snake_case
    identifiers that don't collide with prose words. Backtick-wrapping
    is common in the SKILLs but NOT required; an inline code example
    using the tool name counts as advertising it.
    """
    advertised: list[str] = []
    missing: list[str] = []
    for name in registered:
        if name in skill_text:
            advertised.append(name)
        else:
            missing.append(name)
    return advertised, missing


def audit_skill(raw: dict[str, Any]) -> AuditResult:
    """Run the SKILL capability audit for the talker config in ``raw``.

    Args:
        raw: The unified config dict (post env-var substitution is fine
            but not required — only the ``telegram.instance`` /
            ``telegram.tool_set`` / ``gcal.enabled`` paths are read).

    Returns:
        :class:`AuditResult` summarising the gap. Always returns a
        result — never raises on operator-input issues (missing
        sections, malformed config). Genuine programmer errors
        (e.g., ``conversation`` module missing entirely) propagate.

    Special cases:
        * ``telegram`` section absent or ``instance.name`` missing →
          returns ``instance_missing=True`` (nothing to audit; caller
          surfaces as SKIP).
        * Resolved ``SKILL.md`` doesn't exist → ``skill_missing=True``
          and every registered tool ends up in
          ``missing_advertisements`` (worst-case: nothing is
          advertised because there's nothing to advertise from).
    """
    # ``telegram`` section absent → nothing to audit. Most other talker
    # health probes ALSO short-circuit on this so the SKIP cascades
    # cleanly through the rollup.
    if not isinstance(raw, dict) or not raw.get("telegram"):
        return AuditResult(
            instance_name="",
            tool_set="",
            skill_bundle="",
            skill_path=Path(""),
            skill_missing=False,
            instance_missing=True,
            instance_missing_reason="no telegram section in config",
        )
    try:
        config = load_talker_config(raw)
    except TypeError as exc:
        # ``InstanceConfig.name`` is a required field with no default
        # (per ``feedback_hardcoding_and_alfred_naming.md`` — we won't
        # silently default to "Alfred"). A config without
        # ``telegram.instance.name`` raises TypeError at load time;
        # surface as instance_missing rather than blowing up.
        return AuditResult(
            instance_name="",
            tool_set="",
            skill_bundle="",
            skill_path=Path(""),
            skill_missing=False,
            instance_missing=True,
            instance_missing_reason=(
                f"telegram.instance config incomplete ({exc})"
            ),
        )
    instance_name = (
        config.instance.name
        if config.instance and config.instance.name
        else "(unnamed)"
    )
    tool_set = (
        config.instance.tool_set
        if config.instance and config.instance.tool_set
        else "talker"
    )
    skill_bundle = (
        config.instance.skill_bundle
        if config.instance and config.instance.skill_bundle
        else "vault-talker"
    )

    gcal_enabled = _resolve_gcal_enabled(raw)
    registered = _registered_tool_names(tool_set, gcal_enabled=gcal_enabled)

    skill_path = get_skills_dir() / skill_bundle / "SKILL.md"
    if not skill_path.exists():
        # Worst case — every registered tool is unadvertised because
        # there's no SKILL to advertise it.
        return AuditResult(
            instance_name=instance_name,
            tool_set=tool_set,
            skill_bundle=skill_bundle,
            skill_path=skill_path,
            skill_missing=True,
            registered_tools=registered,
            advertised=[],
            missing_advertisements=list(registered),
        )

    skill_text = skill_path.read_text(encoding="utf-8")
    advertised, missing = _classify(skill_text, registered)
    return AuditResult(
        instance_name=instance_name,
        tool_set=tool_set,
        skill_bundle=skill_bundle,
        skill_path=skill_path,
        skill_missing=False,
        registered_tools=registered,
        advertised=advertised,
        missing_advertisements=missing,
        # NOTE: inverse check (tools mentioned in SKILL but not in
        # registry) deferred — see module docstring. A reliable
        # tool-like-substring heuristic against free-form SKILL prose
        # is non-trivial (false positives on doc strings, code
        # samples, narrative paragraphs). Detector-only scope per
        # task brief; defer the inverse until a v2 design with a
        # cleaner signal.
        orphaned_advertisements=[],
    )


def render_audit(result: AuditResult) -> str:
    """Format an audit result as human-readable text.

    Output contract (read by ``alfred talker skill-audit`` and the
    BIT probe's ``detail`` field):

        * Headline ``summary_line`` is always emitted (idle-vs-broken
          per ``feedback_intentionally_left_blank.md``).
        * One ``MISSING ADVERTISEMENT:`` line per missing tool, naming
          tool, tool_set, and SKILL path.
        * If ``skill_missing=True``, an extra line flags the absent
          SKILL bundle.
    """
    lines: list[str] = [result.summary_line]
    if result.instance_missing:
        # Summary line already conveys the SKIP reason — no extra lines.
        return "\n".join(lines)
    if result.skill_missing:
        lines.append(
            f"SKILL MISSING: {result.skill_path} does not exist "
            f"(skill_bundle={result.skill_bundle!r})"
        )
    for name in result.missing_advertisements:
        lines.append(
            f"MISSING ADVERTISEMENT: tool {name!r} is in "
            f"tool_set={result.tool_set!r} but not mentioned in "
            f"skills/{result.skill_bundle}/SKILL.md"
        )
    return "\n".join(lines)
