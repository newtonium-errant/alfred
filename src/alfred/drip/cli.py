"""``alfred drip`` subcommand handlers (#44b).

Operator-facing commands:

* ``run`` — drain one budgeted increment per enabled campaign. This is the
  ignition the #44 machinery was missing; it is what a scheduler invokes.
* ``status`` — render the same lines the ops brief shows, on demand, without
  running anything. Deliberately the SAME renderer as the brief so the two
  surfaces can never disagree about a campaign's progress.
* ``repair-verify`` — re-run the CURRENT verifier over items already recorded
  ``done`` and demote the ones it no longer believes (#60). Deliberately
  operator-invoked; see :func:`cmd_repair_verify` for why it must not be
  automatic.

Budgets come from config on every path. That is the D1 gap this slice closes,
and it is why no call here passes a literal: a number typed at a call site is a
number the operator cannot change.
"""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

import structlog

from .brief_line import render_campaign_line
from .config import DripConfig
from .runner import run_increment
from .repair import repair_false_dones
from .run_lock import run_lock
from .state import campaign_state_path, load_state, save_state
from .wiring import DripConfigError, build_campaign, build_progress

log = structlog.get_logger(__name__)


def _select(config: DripConfig, campaign_name: str | None) -> dict:
    """Campaigns to act on, or a loud error naming what IS configured."""
    enabled = config.enabled_campaigns()
    if campaign_name is None:
        return enabled
    if campaign_name not in config.campaigns:
        raise DripConfigError(
            f"unknown campaign {campaign_name!r} — configured: "
            f"{sorted(config.campaigns) or '(none)'}"
        )
    if campaign_name not in enabled:
        raise DripConfigError(
            f"campaign {campaign_name!r} is configured but not enabled — set "
            f"drip.campaigns.{campaign_name}.enabled: true"
        )
    return {campaign_name: config.campaigns[campaign_name]}


def _select_configured(config: DripConfig, campaign_name: str | None) -> dict:
    """Like :func:`_select` but WITHOUT the enabled gate.

    Used by ``repair-verify``, and the difference is deliberate rather than
    sloppy: state repair is wanted precisely when a campaign is PARKED. #60's
    containment disabled the link001 timer the moment the false-dones were
    found, and if the operator also flips ``enabled: false`` then an
    enabled-only selector would refuse the one command that makes the campaign
    safe to switch back on. ``status`` already reports disabled campaigns for
    the same reason — "not draining" and "not there" are different facts.
    """
    if campaign_name is None:
        return dict(config.campaigns)
    if campaign_name not in config.campaigns:
        raise DripConfigError(
            f"unknown campaign {campaign_name!r} — configured: "
            f"{sorted(config.campaigns) or '(none)'}"
        )
    return {campaign_name: config.campaigns[campaign_name]}


def cmd_run(
    config: DripConfig,
    *,
    campaign_name: str | None = None,
    apply: bool = True,
    today: date | None = None,
) -> int:
    """Drain one increment per selected campaign. Returns a process exit code.

    ``today`` is passed EXPLICITLY into the runner on every call. Its parameter
    is annotated ``date = None``, and omitting it raises ``AttributeError`` on
    the first item that records spend — verified against the runner, not
    assumed. A caller that lets it default works fine against an empty cursor
    and dies once the campaign actually costs something, which is the worst
    available time to find out.
    """
    day = today or date.today()
    selected = _select(config, campaign_name)

    if not selected:
        # ILB: an instance with drip off, or with every campaign disabled, is a
        # legitimate steady state — and it must not look like a crashed run.
        print("Drip: no enabled campaigns — ran, nothing to drain.")
        log.info(
            "drip.run.no_enabled_campaigns",
            configured=sorted(config.campaigns),
            detail="ran, nothing to drain — no campaign is enabled",
        )
        return 0

    # A dry run reads and reports; it claims nothing and spends nothing, so it
    # takes NO lock. Locking it would mean an operator asking "what would run?"
    # gets refused for the hour a real run is in progress — the one moment the
    # question is most worth asking.
    if not apply:
        return _drain(selected, config, day=day, apply=False)

    # Everything below this point can spend money and claim items, so exactly
    # one process per instance may be here at a time. See ``run_lock`` for the
    # measurement: without this, a kicked run overlapping the hourly timer
    # processes every claimed item TWICE and both runs report success.
    #
    # This also moves the blank-instance refusal earlier than it used to fire
    # (it came from ``campaign_state_path`` inside the loop). Same
    # ``DripConfigError``, same CLI boundary that #66 gave it — just raised
    # before any campaign is built rather than after.
    with run_lock(config.data_dir, config.instance) as acquired:
        if not acquired:
            # ILB, and NOT an error: the work this run wanted done is already
            # being done by the run holding the lock. Exit 0 so a kicked run
            # colliding with the timer is not reported as a failure.
            print(
                "Drip: another run is already in progress on this instance — "
                "standing down (its work covers this one)."
            )
            return 0
        return _drain(selected, config, day=day, apply=True)


def _drain(
    selected: dict, config: DripConfig, *, day: date, apply: bool,
) -> int:
    """Run one increment for each selected campaign. Returns an exit code.

    Split out of :func:`cmd_run` so the lock can wrap the whole drain without
    the body being written twice — one copy under the lock and one without is
    exactly how the two would drift.
    """
    exit_code = 0
    for name, ccfg in sorted(selected.items()):
        try:
            campaign = build_campaign(name, ccfg, config)
        except DripConfigError as exc:
            # Loud, and does not abort the other campaigns: one misconfigured
            # campaign should not stop a healthy one from draining.
            print(f"Drip: campaign {name!r} could not start — {exc}")
            log.warning("drip.run.campaign_unbuildable", campaign=name, error=str(exc))
            exit_code = 1
            continue

        state_path = campaign_state_path(config.data_dir, config.instance, name)
        state = load_state(state_path, name)

        result = run_increment(
            campaign,
            state,
            state_path=state_path,
            max_items_per_run=ccfg.max_items_per_run,
            max_items_per_week=ccfg.max_items_per_week,
            max_attempts=ccfg.max_attempts,
            max_failures_per_run=ccfg.max_failures_per_run,
            max_awaiting_runs=ccfg.max_awaiting_runs,
            today=day,
            run_id=uuid.uuid4().hex[:8],
            apply=apply,
        )

        mode = "" if apply else " [dry run]"
        print(
            f"Drip {name}{mode}: attempted {result.attempted}, done "
            f"{result.done}, failed {result.failed}, dispatched "
            f"{result.dispatched}, blocked {result.blocked} — "
            f"{result.remaining}/{result.total} left ({result.stop_reason})"
        )
        for err in result.errors[:5]:
            print(f"  ! {err}")

    return exit_code


def cmd_status(config: DripConfig, *, today: date | None = None) -> int:
    """Render every CONFIGURED campaign's line — including disabled ones.

    Disabled campaigns are shown deliberately. "Not in the output" and "not
    draining" are different facts, and a campaign someone turned off two weeks
    ago is exactly the thing an operator needs to see when they wonder why a
    backlog is not moving.
    """
    day = today or date.today()

    if not config.campaigns:
        print("Drip: no campaigns configured.")
        log.info(
            "drip.status.no_campaigns",
            detail="ran, nothing to report — no drip campaigns configured",
        )
        return 0

    for name, ccfg in sorted(config.campaigns.items()):
        try:
            campaign = build_campaign(name, ccfg, config)
        except DripConfigError as exc:
            print(f"**{name}:** ⚠ misconfigured — {exc}")
            log.warning("drip.status.campaign_unbuildable", campaign=name, error=str(exc))
            continue
        state_path = campaign_state_path(config.data_dir, config.instance, name)
        state = load_state(state_path, name)
        progress = build_progress(name, campaign, state, ccfg, today=day)
        print(render_campaign_line(progress))
    return 0


def cmd_repair_verify(
    config: DripConfig,
    *,
    campaign_name: str | None = None,
    apply: bool = False,
) -> int:
    """Re-verify every ``done`` item and demote the ones that did not land.

    #60's repair half. A campaign's ``done`` rows are only as trustworthy as the
    verifier that wrote them, and link001's verifier was satisfiable by the very
    defect it existed to catch: a YAML-wrapped link failed the exact-string match
    in ``work()`` (nothing edited) and failed it here too, which the removal
    branch reads as success. 4 of the first live increment's 12 items were
    recorded ``done`` over an untouched record and then permanently skipped, the
    work-list being frozen. Fixing the verifier does not un-record them — only
    re-asking the question does.

    **Dry run by DEFAULT**, matching ``build-worklist`` and inverting ``run``.
    The deploy moment for this is a single deliberate invocation, so previewing
    which items it intends to reopen costs one command and makes the write an
    informed one.

    **Never automatic, and never wired into ``run``.** A verifier that
    re-litigates its own history on every scheduled tick is a campaign that can
    oscillate: one bad verify turns settled work back into pending, the next run
    re-edits records that were already correct. Repair is a response to a KNOWN
    verifier bug, which is a thing a human knows and a timer does not.

    Three properties the demotion holds:

    * **Item-keyed** — each item's own ``verify(item_id)`` decides its own fate.
    * **Idempotent** — a second pass sees the demoted rows as ``pending`` rather
      than ``done``, so they are out of scope; it demotes nothing and says so.
    * **Silent on nothing** is impossible — every campaign prints a sentence on
      every run. Zero demotions renders as "ran, 0 demoted"; zero ``done`` rows
      to examine renders as its own distinct line, because "checked them and
      they were all fine" and "there was nothing to check" are different facts.
      The ``drip.repair_verify.summary`` event fires on both paths, so one grep
      covers every outcome.

    A verify that RAISES does not demote. Failing to prove an item landed is not
    proof it didn't (the #40 over-reach, same lesson): an unreadable record would
    otherwise reopen work that was genuinely finished. Those are counted and
    logged separately so the number is never mistaken for a clean bill.

    The audit itself lives in :func:`alfred.drip.repair.repair_false_dones`,
    where it is a pure function of a campaign and a state object and the rules
    above can be pinned without a config, a work-list file or a state file. This
    handler is the operator-facing shell: select, load, delegate, save, print.

    **``--apply`` takes the run lock** (#89). The demotion is a
    read-modify-write on the SAME per-campaign state file ``run_increment``
    writes, and it was happening outside the lock the hourly timer holds. The
    interleaving loses data silently in either direction: repair loads state,
    the timer's run claims items and saves, repair saves its now-stale copy and
    the claims vanish — or the reverse, and the demotions do. Both runs report
    success, which is the same signature the lock was introduced for
    (``run_lock``'s docstring: two concurrent runs produced 12 work() calls over
    a 6-item list and both reported a clean done=6).
    """
    selected = _select_configured(config, campaign_name)

    if not selected:
        # ILB: no configured campaigns is a legitimate state, not a crash.
        print("Drip repair-verify: no campaigns configured — ran, nothing to check.")
        log.info(
            "drip.repair_verify.no_campaigns",
            detail="ran, nothing to check — no drip campaigns configured",
        )
        return 0

    # A dry run reads and reports; it writes no state, so it takes NO lock —
    # the same reasoning ``cmd_run`` applies to its own preview. Refusing to
    # tell an operator what repair WOULD do because a run is in progress
    # would deny the answer at the moment it is most worth having.
    if not apply:
        return _repair_all(selected, config, apply=False)

    with run_lock(config.data_dir, config.instance) as acquired:
        if not acquired:
            # ILB, and NOT an error — but a DIFFERENT outcome from ``run``'s
            # stand-down, and it must not borrow that wording. A skipped run
            # is covered by the run holding the lock; a skipped REPAIR is not
            # covered by anything, because ``run`` never re-verifies done
            # rows. So this says plainly that the repair did not happen and
            # has to be re-issued.
            print(
                "Drip repair-verify: a drip run is in progress on this "
                "instance — standing down WITHOUT repairing. Nothing was "
                "demoted; re-run this command once the run finishes."
            )
            log.warning(
                "drip.repair_verify.lock_busy",
                instance=config.instance,
                detail="repair-verify --apply could not take the run lock; no "
                       "state was read or written. Unlike a skipped run, this "
                       "work is NOT covered by the lock holder — re-issue it.",
            )
            return 0
        return _repair_all(selected, config, apply=True)


def _repair_all(selected: dict, config: DripConfig, *, apply: bool) -> int:
    """Re-verify each selected campaign. Returns an exit code.

    Split out of :func:`cmd_repair_verify` so the lock can wrap the whole pass
    without the body existing twice — one copy under the lock and one without
    is exactly how the two drift. Mirrors ``cmd_run`` / :func:`_drain`.
    """
    mode = "" if apply else " [dry run]"

    for name, ccfg in sorted(selected.items()):
        try:
            campaign = build_campaign(name, ccfg, config)
        except DripConfigError as exc:
            # Loud, and does not abort the other campaigns — same posture as run.
            print(f"Drip repair-verify: campaign {name!r} could not start — {exc}")
            log.warning(
                "drip.repair_verify.campaign_unbuildable",
                campaign=name, error=str(exc),
            )
            continue

        state_path = campaign_state_path(config.data_dir, config.instance, name)
        state = load_state(state_path, name)

        result = repair_false_dones(campaign, state, apply=apply)

        for item_id, error in result.unverifiable_items:
            print(f"  ? {item_id} — verify raised: {error}")
        for item_id in result.demoted_items:
            print(f"  ! {item_id} — recorded done, not verifiable → pending")

        if apply and result.demoted:
            save_state(state_path, state)

        if not result.audited:
            # ILB: "no done rows to check" and "checked them and they were all
            # fine" are different facts and must not render identically.
            print(
                f"Drip repair-verify {name}{mode}: ran, no done items to "
                f"re-verify."
            )
            continue

        errors_note = (
            f", {result.unverifiable} unverifiable" if result.unverifiable else ""
        )
        print(
            f"Drip repair-verify {name}{mode}: ran, {result.demoted} demoted, "
            f"{result.confirmed} confirmed{errors_note} "
            f"(of {result.audited} done)."
        )
        if not apply and result.demoted:
            print(
                f"  [dry run] nothing was written — re-run with --apply to "
                f"reopen {result.demoted} item(s)."
            )

    return 0


def cmd_build_worklist(
    janitor_config,
    *,
    out_path: str,
    apply: bool = False,
) -> int:
    """Build the FROZEN link001 work-list (#50). Dry-run unless ``apply``.

    Dry-run is the DEFAULT here, the opposite of ``drip run``, and deliberately
    so: writing this file commits a set of irreversible edits to a branch
    decision that must never be regenerated. Previewing the counts before
    freezing them is the cheap half of that.

    Fails LOUD on a missing vault and on a zero-item result. An empty work-list
    and an absent one are indistinguishable downstream — ``load_worklist_file``
    already refuses a missing file for that reason — so writing an empty one
    would manufacture exactly the "you are finished" signal the campaign must
    never receive by accident.
    """
    from alfred.janitor.state import JanitorState

    from .worklist import build_link001_worklist, render_worklist

    state = JanitorState(
        janitor_config.state.path, janitor_config.state.max_sweep_history,
    )
    build = build_link001_worklist(janitor_config, state)

    if build.total == 0:
        # ERROR, never a quiet empty file.
        print(
            "Drip build-worklist: scanned the vault and found NO LINK001 to "
            "freeze. Refusing to write an empty work-list — an empty list and "
            "a missing one are indistinguishable to the campaign, and this one "
            "would read as 'the backlog is drained'."
        )
        log.warning(
            "drip.worklist.empty",
            campaign="link001_repair",
            detail="ran, found nothing to freeze — no file written",
        )
        return 1

    print(
        f"Drip build-worklist link001: {build.total} item(s) — "
        f"{build.annotate} annotate, {build.remove} remove — "
        f"across {build.files} record(s)"
    )
    if build.skipped:
        # Never silent about refusals: a reference the builder declined to
        # freeze is work the campaign will never do, so it is named here.
        print(f"  skipped {len(build.skipped)}:")
        for rel_path, target, reason in build.skipped[:10]:
            print(f"    {rel_path} :: {target or '(unparsed)'} — {reason}")

    if not apply:
        print(
            f"  [dry run] would write {out_path} — re-run with --apply to "
            "freeze this list."
        )
        for item in build.items[:5]:
            print(f"    {item}")
        if build.total > 5:
            print(f"    … and {build.total - 5} more")
        return 0

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        render_worklist(build, source=f"alfred drip build-worklist link001"),
        encoding="utf-8",
    )
    print(f"  wrote {out}")
    log.info(
        "drip.worklist.written",
        campaign="link001_repair", path=str(out), items=build.total,
        annotate=build.annotate, remove=build.remove,
    )
    return 0


__all__ = [
    "cmd_build_worklist",
    "cmd_repair_verify",
    "cmd_run",
    "cmd_status",
]
