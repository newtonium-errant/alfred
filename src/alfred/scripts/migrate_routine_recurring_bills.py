"""One-time migration: routine Phase 2A — Recurring Bills + Admin (2026-05-29).

Two sub-tasks bundled in one script, all gated by ``--dry-run``:

  1. **Cancel 3 named one-shot recurring task records.** Identified by
     exact filename: ``RRTS Invoicing.md``, ``RRTS Payroll.md``,
     ``Pay Clinic Rental to Hussein Rafih.md``. For each: set
     ``status: cancelled``, ``cancelled_at: 2026-05-29``,
     ``migrated_to: '[[routine/Recurring Bills + Admin]]'``, and
     body-append a migration note linking to the new routine record.

  2. **Create ``routine/Recurring Bills + Admin.md``** carrying 4 items
     with ``due_pattern`` + escalation knobs per Ship A's schema:

       - Pay Clinic Rental to Hussein Rafih   monthly day=1,    surface=5, escalate=0
       - Garbage Day                          biweekly Thu,     escalate=1 (no surface; T1-only)
       - RRTS Invoicing                       weekly  day=tue,  surface=1, escalate=0
       - RRTS Payroll                         biweekly Thu,     surface=1, escalate=0

     Each item carries the canonical ``priority`` matching the
     operator's intent (critical for garbage, tracked for the bills).

Operating mode (per ``feedback_migration_script_default_live``):

  * Default mode = LIVE RUN. The script DOES vault writes unless
    ``--dry-run`` is passed.
  * All writes go through the ``alfred vault`` CLI as subprocess
    invocations — NOT direct library calls — so the audit log and
    scope check fire on every mutation. The migration scope
    (``ALFRED_VAULT_SCOPE=migration``) permits ``edit`` (with
    ``allow_body_writes`` for the body-append) and ``create`` for
    ``routine`` records (per ``MIGRATION_CREATE_TYPES``).
  * Idempotency per sub-task:
    - 1: skip records where ``status == cancelled`` AND
         ``migrated_to`` is set (already migrated).
    - 2: skip routine creation if the file exists and carries all 4
         expected items. If the file exists but is missing items,
         the plan flags it for operator review (FAIL-LOUD — don't
         silently extend an operator-edited routine).

Recommended invocation:

    # Inspect what would happen — NO writes.
    python -m alfred.scripts.migrate_routine_recurring_bills --dry-run

    # Execute against the live vault. Operator should have stashed
    # any pre-existing dirty-tree state BEFORE invoking.
    python -m alfred.scripts.migrate_routine_recurring_bills

If ``--vault`` is omitted, the script defaults to ``$ALFRED_VAULT_PATH``
then ``/home/andrew/alfred/vault`` (Salem's vault).

This is a Salem-only migration — the routine Phase 2A surface is
Salem-only. Running against another instance's vault is operator
error; the script doesn't gate on instance name (mirrors the tier
Phase 1 precedent). Operator confirms the path in the dry-run header
before authorising the live run.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter


# --- Constants ------------------------------------------------------------


#: ISO date string stamped on cancelled task records' ``cancelled_at``
#: field + threaded into the body-append migration note. Hardcoded
#: rather than ``date.today()`` so re-runs of the same migration
#: produce identical timestamps (idempotency-friendly; the operator
#: ratified 2026-05-29 as THE migration date).
MIGRATION_DATE = "2026-05-29"


#: Routine record name + filename stem.
ROUTINE_RECORD_NAME = "Recurring Bills + Admin"


#: Tuples of (filename stem) for each task record to cancel.
#: The body-append text references the new routine by name.
TASKS_TO_CANCEL: tuple[str, ...] = (
    "RRTS Invoicing",
    "RRTS Payroll",
    "Pay Clinic Rental to Hussein Rafih",
)


#: The 4 items the new routine will carry. Each tuple is
#: ``(text, priority, due_pattern dict, surface_at_days | None,
#: escalate_at_days)`` — laid out as a constant so the discovery,
#: render, and apply paths all consume the same source-of-truth and
#: the dry-run report matches the live-write content exactly.
ROUTINE_ITEMS: tuple[tuple[str, str, dict[str, Any], int | None, int], ...] = (
    (
        "Pay Clinic Rental to Hussein Rafih",
        "tracked",
        {"type": "monthly", "day": 1},
        5,
        0,
    ),
    (
        "Garbage Day",
        "critical",
        {"type": "biweekly", "day": "thu", "anchor": "2026-05-28"},
        None,  # surface_at_days absent → T1-only window
        1,
    ),
    (
        "RRTS Invoicing",
        "tracked",
        {"type": "weekly", "day": "tue"},
        1,
        0,
    ),
    (
        "RRTS Payroll",
        "tracked",
        {"type": "biweekly", "day": "thu", "anchor": "2026-05-29"},
        1,
        0,
    ),
)


#: Routine body — operator-facing prose explaining the migration with
#: backlinks to the cancelled origin records. Constructed at module-
#: import time so the same string is used by both plan and apply
#: paths (no drift between dry-run preview and live write).
_ROUTINE_BODY_LINES = [
    f"# {ROUTINE_RECORD_NAME}",
    "",
    "Recurring deadline-bearing items routed through the routine "
    "system per Routine Phase 2A (2026-05-29). Auto-surfaced as T1/T2 "
    "candidates in the morning brief via `due_pattern` + escalation "
    "knobs.",
    "",
    "## Migration history",
    f"{MIGRATION_DATE} — Created from three orphan task records:",
    "- [[task/Pay Clinic Rental to Hussein Rafih]] (cancelled)",
    "- [[task/RRTS Invoicing]] (cancelled)",
    "- [[task/RRTS Payroll]] (cancelled)",
    "- Garbage Day added fresh (anchor 2026-05-28).",
]
ROUTINE_BODY = "\n".join(_ROUTINE_BODY_LINES) + "\n"


#: Body append stamped onto each cancelled task record. Includes the
#: migration date + a backlink to the new routine record so an
#: operator opening a cancelled task file can find the live
#: continuation. Per the dispatch's worked example verbatim.
TASK_CANCEL_BODY_APPEND = (
    f"\n## Migration note\n\n{MIGRATION_DATE} — Migrated into "
    f"[[routine/{ROUTINE_RECORD_NAME}]] as part of Routine Phase 2A. "
    f"This was a one-shot recurring-task antipattern; recurring items "
    f"belong in routines with `due_pattern` + escalation, not as task "
    f"records the talker recreates each cycle.\n"
)


#: The frontmatter ``migrated_to`` wikilink value — stamped on each
#: cancelled task record so a future tool walking task records can
#: find the routine record that supersedes it.
MIGRATED_TO_LINK = f"[[routine/{ROUTINE_RECORD_NAME}]]"


# --- Data types -----------------------------------------------------------


@dataclass
class TaskCancel:
    """One task-record cancellation plan."""
    rel_path: str
    task_name: str  # stem without .md


@dataclass
class MigrationPlan:
    """End-to-end plan structure — populated by ``build_plan``, then
    consumed by ``apply_plan``.

    Idempotency-skip lists carry the records the migration would have
    touched but doesn't because they're already in the target state.
    Operators reading the dry-run see exactly what's pending vs.
    what's already done.

    Per ``feedback_plan_discovery_silent_skips``: every ``continue`` in
    the discovery path appends to a named bucket so the dry-run report
    can surface it (e.g., ``tasks_missing`` if any of the 3
    cancellation targets are absent → WARNING block in print_plan).
    """
    # Sub-task 1
    task_cancels: list[TaskCancel] = field(default_factory=list)
    tasks_already_cancelled: list[str] = field(default_factory=list)
    tasks_missing: list[str] = field(default_factory=list)

    # Sub-task 2
    routine_to_create: bool = False
    routine_already_exists: bool = False
    # Set when routine exists but is missing one or more expected items
    # (operator-edited routine). FAIL-LOUD: the live run will refuse to
    # touch it; operator must hand-fix. Each entry: item text the
    # routine is missing.
    routine_missing_items: list[str] = field(default_factory=list)


# --- Plan discovery -------------------------------------------------------


def _load_frontmatter(path: Path) -> dict:
    """Parse a record file and return its frontmatter dict.

    Returns ``{}`` on parse failure — keeps the plan-build path robust
    against partially-broken records. The mutation path doesn't reach
    a broken record because it's flagged ``already_done`` or missing
    by the plan-build gate.
    """
    try:
        post = frontmatter.load(path)
    except Exception:  # noqa: BLE001
        return {}
    return dict(post.metadata or {})


def discover_task_cancels(
    vault: Path,
) -> tuple[list[TaskCancel], list[str], list[str]]:
    """Plan sub-task 1 — task cancellation.

    Returns ``(task_cancels, already_cancelled, missing)``.

    Per task record per ``TASKS_TO_CANCEL``:
      * In ``task_cancels`` when the file exists AND
        (``status != cancelled`` OR ``migrated_to`` absent — partial-
        state convergence). Live run will set status, stamp
        cancelled_at + migrated_to, append migration note.
      * In ``already_cancelled`` when the file exists AND
        ``status == cancelled`` AND ``migrated_to`` is set
        (idempotency skip — already migrated).
      * In ``missing`` when the file doesn't exist (dispatch named it
        but it's not in the vault). Per
        ``feedback_plan_discovery_silent_skips`` this surfaces as a
        WARNING in the dry-run report so the operator sees it BEFORE
        the live run rather than discover it silently post-migration.
    """
    task_cancels: list[TaskCancel] = []
    already: list[str] = []
    missing: list[str] = []

    for task_name in TASKS_TO_CANCEL:
        rel_path = f"task/{task_name}.md"
        full = vault / "task" / f"{task_name}.md"
        if not full.is_file():
            missing.append(rel_path)
            continue
        fm = _load_frontmatter(full)
        status = str(fm.get("status") or "").lower()
        has_migrated_to = bool(fm.get("migrated_to"))
        if status == "cancelled" and has_migrated_to:
            already.append(rel_path)
            continue
        task_cancels.append(TaskCancel(
            rel_path=rel_path,
            task_name=task_name,
        ))
    return task_cancels, already, missing


def discover_routine_create(
    vault: Path,
) -> tuple[bool, bool, list[str]]:
    """Plan sub-task 2 — routine record creation.

    Returns ``(routine_to_create, routine_already_exists,
    routine_missing_items)``.

    Routine creation:
      * ``routine_to_create=True`` when the file doesn't exist.
      * ``routine_already_exists=True`` AND ``routine_missing_items=[]``
        when the file exists AND carries all 4 expected items (full
        idempotency skip).
      * ``routine_already_exists=True`` AND ``routine_missing_items``
        non-empty when the file exists but doesn't carry one or more
        expected items. FAIL-LOUD: the live run refuses to touch the
        record (operator may have edited it intentionally). Surfaced
        in the dry-run report so the operator can hand-fix.

    Item presence check: compares ``item.text`` substring inclusion
    against the expected ``text`` values. Doesn't validate the full
    schema (due_pattern, etc.) — the presence check is the migration's
    convergence signal, not a full schema validator.
    """
    routine_path = vault / "routine" / f"{ROUTINE_RECORD_NAME}.md"
    if not routine_path.is_file():
        return True, False, []

    # Routine exists — verify it carries the 4 expected items.
    fm = _load_frontmatter(routine_path)
    existing_items = fm.get("items") or []
    existing_texts: set[str] = set()
    if isinstance(existing_items, list):
        for item in existing_items:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    existing_texts.add(text)

    missing_items: list[str] = []
    for expected_text, _priority, _due_pattern, _surface, _escalate in ROUTINE_ITEMS:
        if expected_text not in existing_texts:
            missing_items.append(expected_text)

    return False, True, missing_items


def build_plan(vault: Path) -> MigrationPlan:
    """Discover all sub-tasks. No vault writes.

    Idempotency: each sub-task's plan separates ``pending`` from
    ``already-done`` so the dry-run report shows both — operators can
    confirm the migration converges on the desired state across
    multiple invocations.
    """
    plan = MigrationPlan()

    # Sub-task 1
    (
        plan.task_cancels,
        plan.tasks_already_cancelled,
        plan.tasks_missing,
    ) = discover_task_cancels(vault)

    # Sub-task 2
    (
        plan.routine_to_create,
        plan.routine_already_exists,
        plan.routine_missing_items,
    ) = discover_routine_create(vault)

    return plan


# --- Plan rendering -------------------------------------------------------


def print_plan(plan: MigrationPlan, vault: Path, *, dry_run: bool) -> None:
    """Emit the human-readable migration report.

    Per ``feedback_intentionally_left_blank.md`` every sub-task's
    section emits a header unconditionally + an explicit "nothing to
    do" sentinel when its bucket is empty. The operator reading the
    dry-run output should NEVER see a silently-empty section.

    Per ``feedback_plan_discovery_silent_skips.md``: every named bucket
    populated by the discovery path is rendered explicitly — missing
    records → WARNING block; already-cancelled → idempotency-skip count.
    """
    mode = (
        "DRY-RUN — no changes will be written"
        if dry_run
        else "LIVE RUN — writes WILL happen"
    )
    print("Routine Phase 2A — Recurring Bills + Admin Migration Plan")
    print(f"  Vault: {vault}")
    print(f"  Mode:  {mode}")
    print()

    # --- Sub-task 1
    print("--- Sub-task 1: cancel 3 one-shot recurring tasks ---")
    if not plan.task_cancels:
        print("  (no task cancellations pending)")
    else:
        for entry in plan.task_cancels:
            print(
                f"  {entry.rel_path}  "
                f"(status=cancelled, cancelled_at={MIGRATION_DATE}, "
                f"migrated_to={MIGRATED_TO_LINK}, "
                f"body-append migration note)"
            )
        print(f"  TOTAL: {len(plan.task_cancels)} task(s) to cancel")
    if plan.tasks_already_cancelled:
        print(
            f"  (idempotency-skip: "
            f"{len(plan.tasks_already_cancelled)} task(s) already "
            f"cancelled)"
        )
    if plan.tasks_missing:
        # Per feedback_plan_discovery_silent_skips: missing records
        # surface as a WARNING block so the operator sees them BEFORE
        # the live run rather than discover silent skips post-migration.
        print("  WARNING: dispatch-named tasks missing from vault:")
        for rel in plan.tasks_missing:
            print(f"    {rel}")
    print()

    # --- Sub-task 2
    print("--- Sub-task 2: create Recurring Bills + Admin routine ---")
    if plan.routine_to_create:
        print(
            f"  Create: routine/{ROUTINE_RECORD_NAME}.md "
            f"(cadence: daily, items: {len(ROUTINE_ITEMS)})"
        )
        for text, priority, due_pattern, surface, escalate in ROUTINE_ITEMS:
            surface_str = (
                f"surface_at_days={surface}" if surface is not None else "—"
            )
            print(
                f"    - {text}  "
                f"(priority={priority}, due_pattern={due_pattern}, "
                f"{surface_str}, escalate_at_days={escalate})"
            )
    elif plan.routine_already_exists and not plan.routine_missing_items:
        print(
            f"  (idempotency-skip: "
            f"routine/{ROUTINE_RECORD_NAME}.md already exists with all "
            f"{len(ROUTINE_ITEMS)} expected items)"
        )
    elif plan.routine_missing_items:
        # FAIL-LOUD per dispatch — operator-edited routine; live run
        # refuses to touch.
        print(
            f"  WARNING: routine/{ROUTINE_RECORD_NAME}.md exists but is "
            f"missing {len(plan.routine_missing_items)} expected item(s):"
        )
        for text in plan.routine_missing_items:
            print(f"    - {text}")
        print(
            "  Live run will SKIP this sub-task — hand-edit the routine "
            "to add the missing items, then re-run."
        )
    print()

    # --- Idempotent-no-op sentinel per feedback_intentionally_left_blank
    # When NOTHING is pending across all sub-tasks (a clean re-run after
    # successful migration), surface the explicit sentinel so the
    # operator can distinguish "ran, nothing to do" from "broken".
    nothing_pending = (
        not plan.task_cancels
        and not plan.routine_to_create
        and not plan.routine_missing_items
    )
    if nothing_pending:
        print(
            "--- All sub-tasks idempotent-no-op — migration already "
            "complete. ---"
        )
        print()


# --- CLI subprocess wrapper -----------------------------------------------


def _alfred_vault_cmd(
    verb: str,
    *args: str,
    env: dict[str, str],
    stdin: str | None = None,
) -> dict:
    """Invoke ``python -m alfred vault <verb> <args>`` as a subprocess.

    Returns the parsed JSON response. Raises ``RuntimeError`` with the
    full stderr + stdout tail on non-zero exit per builder.md
    "Subprocess Failure Logging" — the stdout-tail sentinel is
    load-bearing so grep tooling can spot zero-output failures.

    The migration env (``ALFRED_VAULT_SCOPE=migration`` + fresh
    ``ALFRED_VAULT_SESSION`` UUID + ``ALFRED_VAULT_AUDIT_LOG`` path)
    is threaded through every invocation so every write hits the
    audit log under ``tool="cli"``.

    Implementation note (CRITICAL — verified 2026-05-28 against the
    live vault when the tier Phase 1 migration first ran zero records):
    we shell to ``python -m alfred`` (the canonical package-as-module
    form, dispatched via ``src/alfred/__main__.py`` which calls
    ``cli.main()``) — NOT ``python -m alfred.cli``. ``alfred.cli`` is
    a plain module with no ``if __name__ == "__main__"`` guard, so
    ``python -m alfred.cli`` imports it (executing top-level code)
    but produces NO output and exits 0 cleanly. That shape masquerades
    as a successful subprocess call and SILENTLY drops every vault
    mutation. The tier Phase 1 ship's first live run hit this trap;
    we don't repeat the mistake.

    ``sys.executable -m alfred`` (NOT the ``alfred`` entrypoint
    script) keeps the invocation reproducible across venvs +
    worktrees regardless of egg-link state.

    Parsing contract:
      * Empty stdout — likely a silent CLI no-op (the
        ``python -m alfred.cli`` bug shape). Raises with the
        ``empty stdout`` canary.
      * Non-empty stdout but no parseable JSON line — likely garbage
        output. Raises with the ``not parseable as JSON`` shape.
      * Non-empty stdout with at least one parseable JSON line on a
        reversed scan — returns that dict.
    """
    cmd = [
        sys.executable, "-m", "alfred",
        "vault", verb, *args,
    ]
    full_env = {**os.environ, **env}
    proc = subprocess.run(
        cmd,
        input=stdin,
        text=True,
        capture_output=True,
        env=full_env,
    )
    if proc.returncode != 0:
        err = proc.stderr or ""
        raw = proc.stdout or ""
        detail = raw[:200] or err[:200] or "(no output)"
        raise RuntimeError(
            f"alfred vault {verb} failed: Exit code {proc.returncode}: "
            f"{detail} (stderr={err[:500]!r}, stdout_tail="
            f"{raw[-2000:]!r}, cmd={cmd}, rel_path={args[0] if args else '?'})"
        )

    raw = proc.stdout.strip() if proc.stdout else ""

    if not raw:
        stderr_repr = repr(proc.stderr[:500]) if proc.stderr else "empty"
        raise RuntimeError(
            f"alfred vault {verb}: Subprocess returned exit-0 with "
            f"empty stdout — likely silent CLI failure (wrong module "
            f"path, CLI no-op, or regression that stopped emitting "
            f"JSON). cmd={cmd!r}, "
            f"rel_path={args[0] if args else '?'}, "
            f"stderr={stderr_repr}"
        )

    # Tail-to-head JSON scan defends against structlog interleaving.
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue

    raise RuntimeError(
        f"alfred vault {verb}: Subprocess output not parseable as "
        f"JSON. stdout_tail={raw[-2000:]!r}, cmd={cmd!r}, "
        f"rel_path={args[0] if args else '?'}"
    )


def _build_subprocess_env(vault: Path) -> dict[str, str]:
    """Compose the env-var dict for ``alfred vault`` subprocess calls.

    Generates a fresh ``ALFRED_VAULT_SESSION`` UUID per invocation
    (caller scope) — every script run gets its own audit trail,
    distinct from interleaving daemon writes.
    """
    data_dir = vault.parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    session_id = uuid.uuid4().hex
    session_file = data_dir / f"migration-session-{session_id}.jsonl"
    return {
        "ALFRED_VAULT_PATH": str(vault),
        "ALFRED_VAULT_SCOPE": "migration",
        "ALFRED_VAULT_SESSION": str(session_file),
        "ALFRED_VAULT_AUDIT_LOG": str(data_dir / "vault_audit.log"),
    }


# --- Apply ----------------------------------------------------------------


def _apply_task_cancel(
    entry: TaskCancel, env: dict[str, str],
) -> None:
    """Cancel one task record + append migration note + stamp
    migrated_to wikilink.

    Single ``alfred vault edit`` call combining
    ``--set status=cancelled``, ``--set cancelled_at=<date>``,
    ``--set migrated_to=<wikilink>``, and ``--body-append`` with the
    migration prose. The migration scope permits all three set ops
    + the body-append per ``allow_body_writes: True``.
    """
    _alfred_vault_cmd(
        "edit", entry.rel_path,
        "--set", "status=cancelled",
        "--set", f"cancelled_at={MIGRATION_DATE}",
        "--set", f"migrated_to={MIGRATED_TO_LINK}",
        "--body-append", TASK_CANCEL_BODY_APPEND,
        env=env,
    )


def _build_routine_items_json() -> str:
    """Construct the ``items`` JSON list for the routine create call.

    Each item carries ``text`` + ``priority`` + ``due_pattern`` +
    (optional) ``surface_at_days`` + ``escalate_at_days``. The
    surface_at_days field is OMITTED for T1-only items (Garbage Day)
    rather than written as ``null`` — keeps the on-disk shape
    minimal AND matches the routine record schema's convention.
    """
    items: list[dict[str, Any]] = []
    for text, priority, due_pattern, surface, escalate in ROUTINE_ITEMS:
        item: dict[str, Any] = {
            "text": text,
            "priority": priority,
            "due_pattern": due_pattern,
            "escalate_at_days": escalate,
        }
        if surface is not None:
            # Insert surface_at_days BEFORE escalate_at_days for human-
            # readable ordering of the YAML dump (the brief/talker
            # operator will read these by-hand at some point).
            item = {
                "text": text,
                "priority": priority,
                "due_pattern": due_pattern,
                "surface_at_days": surface,
                "escalate_at_days": escalate,
            }
        items.append(item)
    return json.dumps(items)


def _apply_routine_create(env: dict[str, str]) -> None:
    """Create the ``Recurring Bills + Admin`` routine via subprocess.

    Uses ``alfred vault create`` with ``--set`` flags for each
    frontmatter field + ``--body-stdin`` for the operator-facing
    prose body. Items are passed as a JSON-encoded list (the CLI's
    ``--set`` value parser tries ``json.loads`` first, falling back
    to string).

    ``completion_log={}`` (empty dict) matches the existing routine
    fixtures (``Core Daily.md``, etc.) and the runtime aggregator
    handles dict. Per schema relaxation 2026-05-28, both ``{}`` and
    ``[]`` are accepted at create time.
    """
    _alfred_vault_cmd(
        "create",
        "routine", ROUTINE_RECORD_NAME,
        "--set", "status=active",
        "--set", f"cadence={json.dumps({'type': 'daily'})}",
        "--set", f"items={_build_routine_items_json()}",
        "--set", f"completion_log={json.dumps({})}",
        "--body-stdin",
        env=env,
        stdin=ROUTINE_BODY,
    )


def apply_plan(
    plan: MigrationPlan, vault: Path,
) -> dict[str, int]:
    """Execute the plan against the live vault.

    Returns a counter dict with per-sub-task counts:
      * ``tasks_cancelled`` — count of task records cancelled
      * ``routine_created`` — 0 or 1

    Mid-stream failure: if any subprocess invocation raises
    ``RuntimeError`` (the failure shape ``_alfred_vault_cmd`` produces
    on non-zero CLI exit), prints a structured partial-migration
    sentinel naming the total records ALREADY written + a recovery
    pointer, then re-raises so ``main`` can surface a non-zero exit.

    Per ``feedback_intentionally_left_blank.md``: a mid-stream failure
    without operator-facing signal would leave the operator staring at
    a Python traceback with no idea what shipped vs. what didn't.
    The sentinel is grep-able as ``PARTIAL MIGRATION``.

    Idempotency holds across the partial state: the skip-already-
    cancelled / skip-routine-exists logic means a re-run skips the
    records that DID land and retries the failed one.

    Order: routine creation FIRST (so the body-append on cancelled
    tasks can reference an existing record), THEN task cancels —
    mirrors the tier Phase 1 precedent for the same reason.
    """
    env = _build_subprocess_env(vault)
    counters = {
        "routine_created":   0,
        "tasks_cancelled":   0,
    }

    try:
        # Sub-task 2 first — routine create so body-append wikilinks
        # resolve.
        if plan.routine_to_create:
            print(
                f"  creating routine: "
                f"routine/{ROUTINE_RECORD_NAME}.md "
                f"(cadence: daily, items: {len(ROUTINE_ITEMS)})"
            )
            _apply_routine_create(env)
            counters["routine_created"] = 1
        elif plan.routine_missing_items:
            # FAIL-LOUD: operator-edited routine; skip + report.
            print(
                f"  SKIP routine/{ROUTINE_RECORD_NAME}.md — exists but "
                f"missing {len(plan.routine_missing_items)} expected "
                f"item(s); hand-edit required."
            )

        # Sub-task 1 — task cancels.
        for entry in plan.task_cancels:
            print(f"  cancelling task: {entry.rel_path}")
            _apply_task_cancel(entry, env)
            counters["tasks_cancelled"] += 1
    except RuntimeError:
        # Partial-state sentinel — operator-facing AND grep-able.
        total_written = sum(counters.values())
        print(
            f"\n--- PARTIAL MIGRATION — {total_written} record(s) "
            f"written before failure ---"
        )
        print(
            "--- Recovery: re-run the script; idempotency will skip "
            "completed records and retry the failed one. ---"
        )
        raise

    return counters


# --- CLI -------------------------------------------------------------------


def _default_vault_path() -> Path:
    """Resolve the default Salem vault path.

    Order: ``$ALFRED_VAULT_PATH`` env var > ``/home/andrew/alfred/vault``
    fallback. The fallback is hardcoded to Andrew's known Salem path
    (this script ships ONCE for the Routine Phase 2A migration; not a
    generic tool).
    """
    env_path = os.environ.get("ALFRED_VAULT_PATH")
    if env_path:
        return Path(env_path).expanduser()
    return Path("/home/andrew/alfred/vault")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Routine Phase 2A migration (2026-05-29). Cancels 3 one-"
            "shot recurring task records (RRTS Invoicing, RRTS Payroll, "
            "Pay Clinic Rental to Hussein Rafih) and creates "
            "routine/Recurring Bills + Admin.md with 4 deadline-bearing "
            "items per Ship A's DuePattern schema. Default mode is LIVE "
            "RUN; pass --dry-run to inspect the plan without writes. "
            "Idempotent — safe to re-run."
        ),
    )
    parser.add_argument(
        "--vault",
        type=Path,
        default=_default_vault_path(),
        help=(
            "Path to the Salem vault root. Defaults to "
            "$ALFRED_VAULT_PATH or /home/andrew/alfred/vault."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Report the plan without touching the vault. Default is "
            "LIVE RUN — the script DOES vault writes unless this "
            "flag is passed."
        ),
    )
    args = parser.parse_args(argv)

    vault: Path = args.vault.expanduser().resolve()
    if not vault.is_dir():
        print(
            f"error: vault path is not a directory: {vault}",
            file=sys.stderr,
        )
        return 2

    plan = build_plan(vault)
    print_plan(plan, vault, dry_run=args.dry_run)

    if args.dry_run:
        print(
            "--- DRY-RUN — no changes written. "
            "Re-run without --dry-run to execute. ---"
        )
        return 0

    print("--- APPLYING ---")
    try:
        counters = apply_plan(plan, vault)
    except RuntimeError as exc:
        # ``apply_plan`` already printed the partial-migration sentinel
        # before re-raising. Emit a tail line with the underlying cause
        # + surface non-zero exit.
        print(f"--- Failure cause: {exc}", file=sys.stderr)
        return 1
    print()
    print("Migration complete:")
    print(f"  routine created:    {counters['routine_created']}")
    print(f"  tasks cancelled:    {counters['tasks_cancelled']}")
    print(
        "Summary: "
        f"routine_created={counters['routine_created']} "
        f"tasks_cancelled={counters['tasks_cancelled']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
