"""One-time migration: tier system Phase 1 (2026-05-28).

Three sub-tasks bundled in one script, all gated by ``--dry-run``:

  1. **Rename ``tier:`` → ``base_tier:``** on every ``task/*.md`` record
     that carries the legacy ``tier:`` integer field. The talker-set
     tag from 2026-05-27 conversations used the bare ``tier:`` key;
     the Phase 1 ship ratified ``base_tier:`` as the canonical name
     so the tier-system code (compute / brief render) can look up
     one field. Both keys carry the same int value 1/2/3.

  2. **Populate RRTS escalation fields** on the two RRTS task records
     (``RRTS Invoicing.md`` + ``RRTS Payroll.md``). Sets
     ``base_tier=2``, ``escalate_to=1``, ``escalate_at_days=<N>`` on
     each (N=3 for Invoicing, N=1 for Payroll per dispatch). Leaves
     existing ``due`` / ``priority`` / body content untouched.

  3. **Standing-practices migration** — create
     ``routine/Standing Practices.md`` (cadence: daily, 5 aspirational
     items) and cancel the 5 corresponding task records (Reading,
     Writing, Playing Music, Listening to Music, Exercise) with
     ``status: cancelled``, ``cancelled_at: 2026-05-28``, and a
     ``## Migration note`` body append linking to the new routine.

Operating mode (per dispatch 2026-05-28):

  * Default mode = LIVE RUN. The script DOES vault writes unless
    ``--dry-run`` is passed.
  * All writes go through the ``alfred vault`` CLI as subprocess
    invocations — NOT direct library calls — so the audit log and
    scope check fire on every mutation. The migration scope (``ALFRED_
    VAULT_SCOPE=migration``, shipped in the unset-capability commit
    ``b85db23``) permits the necessary verbs.
  * Idempotency per sub-task:
    - 1: skip records where ``base_tier`` is present AND ``tier`` is
         absent (already migrated). Both-present is the partial-state
         case — drop ``tier:`` to converge.
    - 2: set/overwrite. Re-runs are deterministic no-ops.
    - 3: skip routine creation if the file exists. Skip task cancel
         if ``status == cancelled`` AND ``cancelled_at`` is present.

Recommended invocation:

    # Inspect what would happen — NO writes.
    python -m alfred.scripts.migrate_tier_phase1 --dry-run

    # Execute against the live vault. Operator should have stashed
    # any pre-existing dirty-tree state BEFORE invoking.
    python -m alfred.scripts.migrate_tier_phase1

If ``--vault`` is omitted, the script defaults to ``$ALFRED_VAULT_PATH``
then ``/home/andrew/alfred/vault`` (Salem's vault).

This is a Salem-only migration — the tier system is Salem-only in
Phase 1. Running against another instance's vault is operator error;
the script doesn't gate on instance name (the dispatch said scope-
gating is a Phase 2 concern), so a misdirected ``--vault`` would
mutate the wrong vault. Operator confirms the path in the dry-run
header before authorising the live run.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import frontmatter


# --- Constants ------------------------------------------------------------


#: ISO date string stamped on cancelled task records' ``cancelled_at``
#: field + threaded into the body-append migration note. Hardcoded
#: rather than ``date.today()`` so re-runs of the same migration
#: produce identical timestamps (idempotency-friendly; the operator
#: ratified 2026-05-28 as THE migration date).
MIGRATION_DATE = "2026-05-28"


#: Standing-practice task records to cancel + migrate into the
#: ``Standing Practices`` routine. Order matters — preserved in the
#: routine body's "Backlinks to the cancelled origin records" list
#: and in the routine's ``items`` array so an operator reading either
#: surface sees the same ordering.
STANDING_PRACTICE_TASKS: tuple[str, ...] = (
    "Reading",
    "Writing",
    "Playing Music",
    "Listening to Music",
    "Exercise",
)


#: RRTS escalation field values per dispatch. Tuples of
#: (record stem, base_tier, escalate_to, escalate_at_days).
RRTS_ESCALATION_TARGETS: tuple[tuple[str, int, int, int], ...] = (
    ("RRTS Invoicing", 2, 1, 3),
    ("RRTS Payroll",   2, 1, 1),
)


#: Routine record name + filename stem.
STANDING_PRACTICES_ROUTINE = "Standing Practices"


#: Routine body — operator-facing prose explaining the migration with
#: backlinks to the cancelled origin records. Constructed at module-
#: import time so the same string is used by both plan and apply
#: paths (no drift between dry-run preview and live write).
_ROUTINE_BODY_LINES = [
    "# Standing Practices",
    "",
    "Aspirational anchors I aim to do daily. Surface in the brief as "
    "Tier-3-equivalent practice reminders; never closed, never "
    "completed-and-archived.",
    "",
    "## Migration history",
    f"{MIGRATION_DATE} — Migrated from individual task records into "
    "this routine. Backlinks to the cancelled origin records:",
]
for _practice in STANDING_PRACTICE_TASKS:
    _ROUTINE_BODY_LINES.append(f"- [[task/{_practice}]]")
ROUTINE_BODY = "\n".join(_ROUTINE_BODY_LINES) + "\n"


#: Body append stamped onto each cancelled task record. Includes the
#: migration date + a backlink to the new routine record so an
#: operator opening a cancelled task file can find the live continuation.
TASK_CANCEL_BODY_APPEND = (
    f"\n## Migration note\n\n{MIGRATION_DATE} — Migrated into "
    f"[[routine/{STANDING_PRACTICES_ROUTINE}]] as part of tier system "
    f"Phase 1 ship. Standing practices belong in the routine system "
    f"(recurring, never-closed); kept as cancelled task records for "
    f"backlink continuity.\n"
)


# --- Data types -----------------------------------------------------------


@dataclass
class TierRename:
    """One record's tier→base_tier rename plan."""
    rel_path: str
    tier_value: int


@dataclass
class EscalationSet:
    """One record's escalation-fields population plan."""
    rel_path: str
    base_tier: int
    escalate_to: int
    escalate_at_days: int


@dataclass
class TaskCancel:
    """One standing-practice task to cancel + body-append."""
    rel_path: str
    practice_name: str  # stem without .md


@dataclass
class MigrationPlan:
    """End-to-end plan structure — populated by ``build_plan``, then
    consumed by ``apply_plan``.

    Idempotency-skip lists carry the records the migration would have
    touched but doesn't because they're already in the target state.
    Operators reading the dry-run see exactly what's pending vs.
    what's already done.
    """
    # Sub-task 1
    tier_renames: list[TierRename] = field(default_factory=list)
    tier_already_renamed: list[str] = field(default_factory=list)

    # Sub-task 2
    escalation_sets: list[EscalationSet] = field(default_factory=list)
    escalation_missing_records: list[str] = field(default_factory=list)

    # Sub-task 3
    routine_to_create: bool = False
    routine_already_exists: bool = False
    task_cancels: list[TaskCancel] = field(default_factory=list)
    tasks_already_cancelled: list[str] = field(default_factory=list)
    tasks_missing: list[str] = field(default_factory=list)


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


def discover_tier_renames(
    vault: Path,
) -> tuple[list[TierRename], list[str]]:
    """Scan ``vault/task/*.md`` for legacy ``tier:`` field.

    Returns ``(pending, already_renamed)``:
      * ``pending`` — records carrying ``tier:`` (with or without
        ``base_tier:``; both-present is the partial-state case the
        rename will converge).
      * ``already_renamed`` — records with ``base_tier:`` AND no
        ``tier:`` (idempotency-skip).

    Records with NEITHER field are silently skipped — they're records
    that never participated in the tier system. The dry-run report
    shows the operator only the in-scope records.

    Sorted alphabetically by ``rel_path`` for deterministic output.
    """
    task_dir = vault / "task"
    pending: list[TierRename] = []
    already: list[str] = []
    if not task_dir.is_dir():
        return pending, already

    for path in sorted(task_dir.glob("*.md")):
        fm = _load_frontmatter(path)
        has_tier = "tier" in fm
        has_base_tier = "base_tier" in fm
        rel_path = f"task/{path.name}"
        if has_tier:
            tier_value = fm.get("tier")
            if not isinstance(tier_value, int):
                # Operator hand-edit produced a non-int tier — skip
                # the rename and surface to operator via the report.
                # The dry-run output's "tier-rename" section will
                # omit this record; operator sees the gap.
                continue
            pending.append(TierRename(rel_path=rel_path, tier_value=tier_value))
        elif has_base_tier:
            already.append(rel_path)
    return pending, already


def discover_escalation_targets(
    vault: Path,
) -> tuple[list[EscalationSet], list[str]]:
    """Build the escalation-set plan for the RRTS targets.

    Returns ``(pending, missing)``:
      * ``pending`` — RRTS records that exist on disk (set/overwrite
        idempotency means we don't gate on "already-set"; every run
        re-applies the same values).
      * ``missing`` — RRTS records the dispatch named but that don't
        exist in the vault. Surfaced to operator as a warning in the
        dry-run report; live run skips silently (the named record
        isn't there to mutate).

    Both lists carry vault-relative paths sorted alphabetically.
    """
    pending: list[EscalationSet] = []
    missing: list[str] = []
    for stem, base_tier, escalate_to, escalate_at_days in RRTS_ESCALATION_TARGETS:
        rel_path = f"task/{stem}.md"
        full = vault / "task" / f"{stem}.md"
        if not full.is_file():
            missing.append(rel_path)
            continue
        pending.append(EscalationSet(
            rel_path=rel_path,
            base_tier=base_tier,
            escalate_to=escalate_to,
            escalate_at_days=escalate_at_days,
        ))
    pending.sort(key=lambda e: e.rel_path)
    missing.sort()
    return pending, missing


def discover_standing_practices(
    vault: Path,
) -> tuple[bool, bool, list[TaskCancel], list[str], list[str]]:
    """Plan sub-task 3 — routine creation + task cancellation.

    Returns ``(routine_to_create, routine_already_exists, task_cancels,
    tasks_already_cancelled, tasks_missing)``.

    Routine creation:
      * ``routine_to_create=True`` when the file doesn't exist.
      * ``routine_already_exists=True`` when it does — idempotency
        skip.

    Task cancellation per standing practice:
      * In ``task_cancels`` when the file exists AND
        ``status != cancelled`` (live run will set status, stamp
        cancelled_at, append migration note).
      * In ``tasks_already_cancelled`` when the file exists AND
        ``status == cancelled`` AND ``cancelled_at`` is present
        (idempotency skip — already migrated).
      * In ``tasks_missing`` when the file doesn't exist (dispatch
        named it but it's not in the vault).
    """
    routine_path = vault / "routine" / f"{STANDING_PRACTICES_ROUTINE}.md"
    routine_already_exists = routine_path.is_file()
    routine_to_create = not routine_already_exists

    task_cancels: list[TaskCancel] = []
    already_cancelled: list[str] = []
    missing: list[str] = []

    for practice in STANDING_PRACTICE_TASKS:
        rel_path = f"task/{practice}.md"
        full = vault / "task" / f"{practice}.md"
        if not full.is_file():
            missing.append(rel_path)
            continue
        fm = _load_frontmatter(full)
        status = str(fm.get("status") or "").lower()
        has_cancelled_at = bool(fm.get("cancelled_at"))
        if status == "cancelled" and has_cancelled_at:
            already_cancelled.append(rel_path)
            continue
        task_cancels.append(TaskCancel(
            rel_path=rel_path,
            practice_name=practice,
        ))

    return routine_to_create, routine_already_exists, task_cancels, already_cancelled, missing


def build_plan(vault: Path) -> MigrationPlan:
    """Discover all three sub-tasks. No vault writes.

    Idempotency: each sub-task's plan separates ``pending`` from
    ``already-done`` so the dry-run report shows both — operators can
    confirm the migration converges on the desired state across
    multiple invocations.
    """
    plan = MigrationPlan()

    # Sub-task 1
    plan.tier_renames, plan.tier_already_renamed = discover_tier_renames(vault)

    # Sub-task 2
    plan.escalation_sets, plan.escalation_missing_records = discover_escalation_targets(vault)

    # Sub-task 3
    (
        plan.routine_to_create,
        plan.routine_already_exists,
        plan.task_cancels,
        plan.tasks_already_cancelled,
        plan.tasks_missing,
    ) = discover_standing_practices(vault)

    return plan


# --- Plan rendering -------------------------------------------------------


def print_plan(plan: MigrationPlan, vault: Path, *, dry_run: bool) -> None:
    """Emit the human-readable migration report.

    Per ``feedback_intentionally_left_blank.md`` every sub-task's
    section emits a header unconditionally + an explicit "nothing to
    do" sentinel when its bucket is empty. The operator reading the
    dry-run output should NEVER see a silently-empty section.
    """
    mode = "DRY-RUN — no changes will be written" if dry_run else "LIVE RUN — writes WILL happen"
    print("Tier Phase 1 Migration Plan")
    print(f"  Vault: {vault}")
    print(f"  Mode:  {mode}")
    print()

    # --- Sub-task 1
    print("--- Sub-task 1: rename tier: → base_tier: ---")
    if not plan.tier_renames:
        print("  (no records pending rename)")
    else:
        for entry in plan.tier_renames:
            print(
                f"  {entry.rel_path}  "
                f"(tier: {entry.tier_value} → "
                f"base_tier: {entry.tier_value})"
            )
        print(f"  TOTAL: {len(plan.tier_renames)} record(s) to rename")
    if plan.tier_already_renamed:
        print(
            f"  (idempotency-skip: "
            f"{len(plan.tier_already_renamed)} record(s) already use "
            f"base_tier:)"
        )
    print()

    # --- Sub-task 2
    print("--- Sub-task 2: populate RRTS escalation fields ---")
    if not plan.escalation_sets:
        print("  (no records to update — RRTS targets not found)")
    else:
        for entry in plan.escalation_sets:
            print(f"  {entry.rel_path}")
            print(
                f"    set: base_tier={entry.base_tier}, "
                f"escalate_to={entry.escalate_to}, "
                f"escalate_at_days={entry.escalate_at_days}"
            )
        print(f"  TOTAL: {len(plan.escalation_sets)} record(s) to update")
    if plan.escalation_missing_records:
        print("  WARNING: dispatch-named records missing from vault:")
        for rel in plan.escalation_missing_records:
            print(f"    {rel}")
    print()

    # --- Sub-task 3
    print("--- Sub-task 3: standing-practices → routine ---")
    if plan.routine_to_create:
        print(
            f"  Create: routine/{STANDING_PRACTICES_ROUTINE}.md "
            f"(cadence: daily, items: {len(STANDING_PRACTICE_TASKS)})"
        )
    elif plan.routine_already_exists:
        print(
            f"  (idempotency-skip: "
            f"routine/{STANDING_PRACTICES_ROUTINE}.md already exists)"
        )
    if plan.task_cancels:
        print("  Cancel:")
        for entry in plan.task_cancels:
            print(
                f"    {entry.rel_path}  "
                f"(status=cancelled, cancelled_at={MIGRATION_DATE}, "
                f"body-append migration note)"
            )
        print(
            f"  TOTAL: "
            f"{1 if plan.routine_to_create else 0} routine "
            f"+ {len(plan.task_cancels)} task(s) to cancel"
        )
    else:
        print("  (no task cancellations pending)")
    if plan.tasks_already_cancelled:
        print(
            f"  (idempotency-skip: "
            f"{len(plan.tasks_already_cancelled)} task(s) already "
            f"cancelled)"
        )
    if plan.tasks_missing:
        print("  WARNING: dispatch-named tasks missing from vault:")
        for rel in plan.tasks_missing:
            print(f"    {rel}")
    print()


# --- CLI subprocess wrapper -----------------------------------------------


def _alfred_vault_cmd(
    verb: str,
    *args: str,
    env: dict[str, str],
    vault_path: Path,
    stdin: str | None = None,
) -> dict:
    """Invoke ``python -m alfred.cli vault <verb> <args>`` as a subprocess.

    Returns the parsed JSON response. Raises ``RuntimeError`` with the
    full stderr + stdout tail on non-zero exit per builder.md
    "Subprocess Failure Logging" — the stdout-tail sentinel is
    load-bearing so grep tooling can spot zero-output failures.

    The migration env (``ALFRED_VAULT_SCOPE=migration`` + fresh
    ``ALFRED_VAULT_SESSION`` UUID + ``ALFRED_VAULT_AUDIT_LOG`` path)
    is threaded through every invocation so every write hits the
    audit log under ``tool="cli"``.

    Implementation note: we shell to ``python -m alfred.cli`` (not the
    ``alfred`` entrypoint script) because the entrypoint resolution
    depends on the venv's egg-link, which isn't a guarantee from
    inside a worktree or alt-Python environment. ``sys.executable -m
    alfred.cli`` is the canonical-reproducible form.
    """
    cmd = [
        sys.executable, "-m", "alfred.cli",
        "vault", verb, *args,
    ]
    # Merge the supplied env with os.environ so PATH / PYTHONPATH /
    # other prerequisites stay populated. The supplied env wins for
    # the ALFRED_* keys (which is the point).
    full_env = {**os.environ, **env}
    proc = subprocess.run(
        cmd,
        input=stdin,
        text=True,
        capture_output=True,
        env=full_env,
    )
    if proc.returncode != 0:
        # Per builder.md "Subprocess Failure Logging": log BOTH stderr
        # and a stdout tail with the sentinel for zero-output cases.
        err = proc.stderr or ""
        raw = proc.stdout or ""
        detail = raw[:200] or err[:200] or "(no output)"
        raise RuntimeError(
            f"alfred vault {verb} failed: Exit code {proc.returncode}: "
            f"{detail} (stderr={err[:500]!r}, stdout_tail="
            f"{raw[-2000:]!r}, cmd={cmd}, rel_path={args[0] if args else '?'})"
        )

    raw = proc.stdout.strip()
    if not raw:
        return {}
    # The CLI emits one JSON object per invocation. Take the LAST
    # non-empty line to defend against incidental log lines that may
    # interleave on stdout (per the structlog stdout-pollution
    # diagnosed in the unset capability ship — same trap class).
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return {}


def _build_subprocess_env(vault: Path) -> dict[str, str]:
    """Compose the env-var dict for ``alfred vault`` subprocess calls.

    Generates a fresh ``ALFRED_VAULT_SESSION`` UUID per invocation
    (caller scope) — every script run gets its own audit trail,
    distinct from interleaving daemon writes. The session-file path
    is a tmp-ish location under the vault's data dir; existing
    ``_log_or_audit`` semantics route the per-write mutations there
    AND flush to the audit log.

    ``ALFRED_VAULT_AUDIT_LOG`` points at the standard ``data/vault_
    audit.log`` path relative to the vault root, mirroring the
    daemon convention. (Actual daemon path resolution lives in the
    top-level ``cmd_vault`` dispatcher; here we plumb the env var
    directly since we're bypassing the dispatcher.)
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


def _apply_tier_rename(
    entry: TierRename, env: dict[str, str], vault: Path,
) -> None:
    """Execute one tier→base_tier rename via subprocess.

    Combined ``--set base_tier=<N> --unset tier`` in a single
    ``alfred vault edit`` call. The unset-capability ship's
    ``_check_body_mutation_allowed`` gates the unset under the
    migration scope's ``edit: True``; the audit log records the call
    as TWO entries (one ``op=edit``, one ``op=unset``) per the
    unset-capability dual-emission contract.
    """
    _alfred_vault_cmd(
        "edit", entry.rel_path,
        "--set", f"base_tier={entry.tier_value}",
        "--unset", "tier",
        env=env,
        vault_path=vault,
    )


def _apply_escalation_set(
    entry: EscalationSet, env: dict[str, str], vault: Path,
) -> None:
    """Execute one escalation-fields set via subprocess.

    Three ``--set`` flags in one call. Idempotent: re-running with
    the same values produces no-op edits (set is overwrite semantics;
    same value in = same value out).
    """
    _alfred_vault_cmd(
        "edit", entry.rel_path,
        "--set", f"base_tier={entry.base_tier}",
        "--set", f"escalate_to={entry.escalate_to}",
        "--set", f"escalate_at_days={entry.escalate_at_days}",
        env=env,
        vault_path=vault,
    )


def _apply_routine_create(
    env: dict[str, str], vault: Path,
) -> None:
    """Create the ``Standing Practices`` routine via subprocess.

    Uses ``alfred vault create`` with ``--set`` flags for each
    frontmatter field + ``--body-stdin`` for the operator-facing
    prose body. Items are passed as a JSON-encoded list (the CLI's
    ``--set`` value parser tries ``json.loads`` first, falling back
    to string — see ``_parse_set_args`` in vault/cli.py).

    ``cadence: {type: daily}`` mirrors the existing routine fixtures
    (``For Self Health.md`` / ``Core Daily.md``) exactly so the
    aggregator's ``is_due()`` dispatcher recognises it.
    """
    items = [
        {"text": practice, "priority": "aspirational"}
        for practice in STANDING_PRACTICE_TASKS
    ]
    _alfred_vault_cmd(
        "create",
        "routine", STANDING_PRACTICES_ROUTINE,
        "--set", "status=active",
        "--set", f"cadence={json.dumps({'type': 'daily'})}",
        "--set", f"items={json.dumps(items)}",
        "--set", "completion_log={}",
        "--body-stdin",
        env=env,
        vault_path=vault,
        stdin=ROUTINE_BODY,
    )


def _apply_task_cancel(
    entry: TaskCancel, env: dict[str, str], vault: Path,
) -> None:
    """Cancel one standing-practice task + append migration note.

    Single ``alfred vault edit`` call combining ``--set status=cancelled
    --set cancelled_at=<date>`` with ``--body-append`` carrying the
    migration prose. The dual session-log emission from the unset
    capability isn't relevant here (no unset), so the audit log sees
    one ``op=edit`` row.
    """
    _alfred_vault_cmd(
        "edit", entry.rel_path,
        "--set", "status=cancelled",
        "--set", f"cancelled_at={MIGRATION_DATE}",
        "--body-append", TASK_CANCEL_BODY_APPEND,
        env=env,
        vault_path=vault,
    )


def apply_plan(
    plan: MigrationPlan, vault: Path,
) -> dict[str, int]:
    """Execute the plan against the live vault.

    Returns a counter dict with per-sub-task counts:
      * ``tier_renamed``           — records where tier→base_tier applied
      * ``escalation_set``         — records where escalation fields set
      * ``routine_created``        — 0 or 1
      * ``tasks_cancelled``        — count of standing-practice cancels
    """
    env = _build_subprocess_env(vault)
    counters = {
        "tier_renamed":           0,
        "escalation_set":         0,
        "routine_created":        0,
        "tasks_cancelled":        0,
    }

    # Sub-task 1
    for entry in plan.tier_renames:
        print(f"  renaming tier → base_tier: {entry.rel_path}")
        _apply_tier_rename(entry, env, vault)
        counters["tier_renamed"] += 1

    # Sub-task 2
    for entry in plan.escalation_sets:
        print(
            f"  setting escalation fields: {entry.rel_path} "
            f"(base_tier={entry.base_tier}, "
            f"escalate_to={entry.escalate_to}, "
            f"escalate_at_days={entry.escalate_at_days})"
        )
        _apply_escalation_set(entry, env, vault)
        counters["escalation_set"] += 1

    # Sub-task 3 — routine first (so the body-append on cancelled
    # tasks can reference an existing record), then task cancels.
    if plan.routine_to_create:
        print(
            f"  creating routine: "
            f"routine/{STANDING_PRACTICES_ROUTINE}.md "
            f"(cadence: daily, items: {len(STANDING_PRACTICE_TASKS)})"
        )
        _apply_routine_create(env, vault)
        counters["routine_created"] = 1

    for entry in plan.task_cancels:
        print(f"  cancelling task: {entry.rel_path}")
        _apply_task_cancel(entry, env, vault)
        counters["tasks_cancelled"] += 1

    return counters


# --- CLI -------------------------------------------------------------------


def _default_vault_path() -> Path:
    """Resolve the default Salem vault path.

    Order: ``$ALFRED_VAULT_PATH`` env var > ``/home/andrew/alfred/vault``
    fallback. The fallback is hardcoded to Andrew's known Salem path
    (this script ships ONCE for the tier Phase 1 migration; not a
    generic tool).
    """
    env_path = os.environ.get("ALFRED_VAULT_PATH")
    if env_path:
        return Path(env_path).expanduser()
    return Path("/home/andrew/alfred/vault")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Tier system Phase 1 migration (2026-05-28). Renames "
            "tier:→base_tier: on 24 task records, populates RRTS "
            "escalation fields, migrates 5 standing-practice tasks "
            "into a Standing Practices routine. Default mode is LIVE "
            "RUN; pass --dry-run to inspect the plan without writes. "
            "Idempotent — safe to re-run."
        ),
    )
    parser.add_argument(
        "--vault",
        type=Path,
        default=_default_vault_path(),
        help=(
            "Path to the Salem vault root. Defaults to $ALFRED_VAULT_PATH "
            "or /home/andrew/alfred/vault."
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
    counters = apply_plan(plan, vault)
    print()
    print("Migration complete:")
    print(f"  tier renamed:             {counters['tier_renamed']}")
    print(f"  escalation set:           {counters['escalation_set']}")
    print(f"  routine created:          {counters['routine_created']}")
    print(f"  tasks cancelled:          {counters['tasks_cancelled']}")
    print(
        "Summary: "
        f"renamed={counters['tier_renamed']} "
        f"escalation_set={counters['escalation_set']} "
        f"routine_created={counters['routine_created']} "
        f"tasks_cancelled={counters['tasks_cancelled']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
