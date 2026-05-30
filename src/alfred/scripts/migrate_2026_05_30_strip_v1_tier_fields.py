"""One-shot migration: strip V1 tier fields (2026-05-30).

The Tier-V2 redesign superseded the V1 ``base_tier`` + ``escalate_to`` +
``escalate_at_days`` field semantics. V2 stores curation in
``vault/daily/<date>.md``'s ``tier_curation`` frontmatter; legacy V1
fields on task records are dead-but-harmless.

Routine Phase 2A Ship E additionally cancelled the 3 records that had
been the design-driver for V1 escalation (RRTS Invoicing, RRTS Payroll,
Pay Clinic Rental to Hussein Rafih) — superseded by routine records.
No operator-memory hedge remains; all V1 tier fields across all
surfaced records are inert.

Operator (Andrew) ratified 2026-05-30: strip all V1 tier fields.

## Scope

Strip ``base_tier``, ``escalate_to``, ``escalate_at_days`` from every
``task/*.md`` record that carries them. Per-record idempotency-skip
when none of the three fields are present (record was either never
in the V1 tier system, or a prior run of this script already cleaned
it).

## Apply shape

One ``alfred vault edit task/<file>.md`` call per record carrying any
of the three fields. ``--unset`` flag emitted for EACH field actually
present on that record (omitted for fields absent — the CLI's unset
on a missing field is a no-op but ships a spurious mutation_log
entry; cleaner to skip). The unset-capability dual-emission contract
(per ``migrate_tier_phase1.py``'s ``_apply_tier_rename`` docstring)
means the audit log records one row per ``--unset`` flag.

## Operating mode (per dispatch 2026-05-30)

  * Default mode = LIVE RUN. The script DOES vault writes unless
    ``--dry-run`` is passed.
  * All writes go through the ``alfred vault`` CLI as subprocess
    invocations — NOT direct library calls — so the audit log and
    scope check fire on every mutation. The migration scope
    (``ALFRED_VAULT_SCOPE=migration``) permits the necessary verbs.
  * Idempotency: skip records where NONE of the 3 fields are present
    (already cleaned, or never in scope). The script's plan-build
    surfaces both buckets (``records_pending`` + ``records_already_
    clean``) so the operator sees the convergence state.

Recommended invocation:

    # Inspect what would happen — NO writes.
    python -m alfred.scripts.migrate_2026_05_30_strip_v1_tier_fields --dry-run

    # Execute against the live vault.
    python -m alfred.scripts.migrate_2026_05_30_strip_v1_tier_fields

If ``--vault`` is omitted, the script defaults to ``$ALFRED_VAULT_PATH``
then ``/home/andrew/alfred/vault`` (Salem's vault).

This is a Salem-only migration — the V1 tier system was Salem-only.
Running against another instance's vault is operator error; the
script doesn't gate on instance name. Operator confirms the ``--vault``
path in the dry-run header before authorising the live run.
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

import frontmatter


# --- Constants ------------------------------------------------------------


#: The three V1 tier fields the migration strips. Order is preserved in
#: the dry-run output's per-record line so the operator sees the same
#: ordering on every record.
V1_TIER_FIELDS: tuple[str, ...] = (
    "base_tier",
    "escalate_to",
    "escalate_at_days",
)


# --- Data types -----------------------------------------------------------


@dataclass
class StripRecord:
    """One record's V1-tier-strip plan.

    ``fields_present`` carries the subset of ``V1_TIER_FIELDS`` actually
    present on this record. Order matches ``V1_TIER_FIELDS`` so the
    dry-run report shows a deterministic ordering per record. The
    apply path emits one ``--unset`` flag per entry in this list.
    """
    rel_path: str
    fields_present: list[str]


@dataclass
class StripPlan:
    """End-to-end strip plan structure — populated by ``build_plan``,
    consumed by ``apply_plan``.

    ``records_pending`` carries records with at least one V1 tier field
    present; ``records_already_clean`` carries records with NONE of the
    three fields (idempotency-skip — already cleaned, or never in
    scope). The two buckets together surface the convergence state
    to the operator reading the dry-run.

    Note: records that lack ALL three fields are reported as "already
    clean" rather than silently skipped. Per
    ``feedback_intentionally_left_blank.md``: silent skip leaves no
    ground-truth count; the explicit bucket gives the operator a
    sanity-check that the script saw the records and decided to skip.
    Records that were NEVER in scope (e.g. tasks created post-V2
    that never had a base_tier) and records that were ALREADY cleaned
    by a prior run look identical from the strip's perspective — both
    have zero V1 fields. The bucket name "already clean" is
    intentionally agnostic between those two cases.
    """
    records_pending: list[StripRecord] = field(default_factory=list)
    records_already_clean: list[str] = field(default_factory=list)


# --- Plan discovery -------------------------------------------------------


def _load_frontmatter(path: Path) -> dict:
    """Parse a record file and return its frontmatter dict.

    Returns ``{}`` on parse failure — keeps the plan-build path robust
    against partially-broken records. The mutation path doesn't reach
    a broken record because it's flagged ``already_clean`` (no fields
    visible to the discovery loop).
    """
    try:
        post = frontmatter.load(path)
    except Exception:  # noqa: BLE001
        return {}
    return dict(post.metadata or {})


def discover_strip_records(
    vault: Path,
) -> tuple[list[StripRecord], list[str]]:
    """Scan ``vault/task/*.md`` for V1 tier fields.

    Returns ``(pending, already_clean)``:
      * ``pending`` — records carrying at least one of
        ``base_tier`` / ``escalate_to`` / ``escalate_at_days``. The
        ``fields_present`` list on each StripRecord carries the
        subset actually present, in ``V1_TIER_FIELDS`` order.
      * ``already_clean`` — task records where NONE of the three
        fields are present. Reported (not silently skipped) so the
        operator sees the full task-count vs. pending-count delta in
        the dry-run report.

    Sorted alphabetically by ``rel_path`` for deterministic output.

    Empty buckets returned cleanly if ``task/`` directory is absent
    (per ``feedback_intentionally_left_blank.md``: callers render the
    empty bucket with the unconditional "nothing pending" sentinel
    rather than handling a raise).
    """
    task_dir = vault / "task"
    pending: list[StripRecord] = []
    already_clean: list[str] = []
    if not task_dir.is_dir():
        return pending, already_clean

    for path in sorted(task_dir.glob("*.md")):
        fm = _load_frontmatter(path)
        rel_path = f"task/{path.name}"
        fields_present = [f for f in V1_TIER_FIELDS if f in fm]
        if fields_present:
            pending.append(StripRecord(
                rel_path=rel_path,
                fields_present=fields_present,
            ))
        else:
            already_clean.append(rel_path)
    return pending, already_clean


def build_plan(vault: Path) -> StripPlan:
    """Discover the strip plan. No vault writes."""
    plan = StripPlan()
    plan.records_pending, plan.records_already_clean = discover_strip_records(vault)
    return plan


# --- Plan rendering -------------------------------------------------------


def print_plan(plan: StripPlan, vault: Path, *, dry_run: bool) -> None:
    """Emit the human-readable migration report.

    Per ``feedback_intentionally_left_blank.md`` the section header
    fires unconditionally + an explicit "nothing to do" sentinel when
    the pending bucket is empty. The operator reading the dry-run
    output should NEVER see a silently-empty section.
    """
    mode = "DRY-RUN — no changes will be written" if dry_run else "LIVE RUN — writes WILL happen"
    print("V1 Tier-Field Strip Migration Plan")
    print(f"  Vault: {vault}")
    print(f"  Mode:  {mode}")
    print()

    print("--- Strip V1 tier fields (base_tier / escalate_to / escalate_at_days) ---")
    if not plan.records_pending:
        print("  (no records pending strip)")
    else:
        for entry in plan.records_pending:
            fields_str = ", ".join(entry.fields_present)
            print(f"  {entry.rel_path}  (unset: {fields_str})")
        print(f"  TOTAL: {len(plan.records_pending)} record(s) to strip")
    if plan.records_already_clean:
        print(
            f"  (idempotency-skip: "
            f"{len(plan.records_already_clean)} record(s) already clean — "
            f"none of the 3 V1 fields present)"
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

    Module-path note (carries through from ``migrate_tier_phase1.py``):
    we shell to ``python -m alfred`` (canonical __main__.py dispatch)
    — NOT ``python -m alfred.cli``. The latter has no __main__ guard,
    silently no-ops, exits 0, produces empty stdout. The empty-stdout
    canary below catches that failure shape if a refactor flips back.

    Parsing contract (mirrored from ``migrate_tier_phase1.py``):
      * Empty stdout → ``empty stdout`` canary RuntimeError.
      * Non-empty stdout, no parseable JSON → ``not parseable as JSON``
        canary RuntimeError (distinct surface — operator can grep
        which path failed).
      * Reversed-line scan returns the LAST JSON-parseable line
        (structlog-pollution defense — info lines may interleave above
        the structured payload).
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

    Mirrors ``migrate_tier_phase1.py``'s env shape exactly: fresh
    ``ALFRED_VAULT_SESSION`` UUID per call so concurrent / interleaved
    migration runs don't collide on the session file; ``ALFRED_VAULT_
    SCOPE=migration`` so the unset capability is permitted;
    ``ALFRED_VAULT_AUDIT_LOG`` plumbed directly (we're bypassing the
    top-level ``cmd_vault`` dispatcher).
    """
    data_dir = vault.parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    session_id = uuid.uuid4().hex
    session_file = data_dir / f"v1-tier-strip-session-{session_id}.jsonl"
    return {
        "ALFRED_VAULT_PATH": str(vault),
        "ALFRED_VAULT_SCOPE": "migration",
        "ALFRED_VAULT_SESSION": str(session_file),
        "ALFRED_VAULT_AUDIT_LOG": str(data_dir / "vault_audit.log"),
    }


# --- Apply ----------------------------------------------------------------


def _apply_strip(
    entry: StripRecord, env: dict[str, str],
) -> None:
    """Execute one record's V1-tier-strip via subprocess.

    One ``alfred vault edit`` call with ``--unset <field>`` repeated
    for each field actually present on the record. The CLI's unset
    capability is gated under the migration scope's ``edit: True``;
    the audit log records ONE row per ``--unset`` flag per the
    unset-capability dual-emission contract.

    Fields absent from the record are NOT included in the call —
    unsetting a missing field is a CLI no-op but still ships a
    spurious mutation_log row. Skipping absent fields keeps the
    audit log clean.
    """
    flag_args: list[str] = []
    for fld in entry.fields_present:
        flag_args.extend(["--unset", fld])
    _alfred_vault_cmd(
        "edit", entry.rel_path,
        *flag_args,
        env=env,
    )


def apply_plan(
    plan: StripPlan, vault: Path,
) -> dict[str, int]:
    """Execute the plan against the live vault.

    Returns a counter dict:
      * ``records_stripped`` — count of records where the unset(s)
        applied successfully.
      * ``fields_unset`` — total ``--unset`` flags emitted across all
        records. Equals the sum of ``len(entry.fields_present)`` for
        every entry in ``records_pending``; surfaces the audit-log
        row count the operator should see post-run.

    Mid-stream failure: if any subprocess invocation raises
    ``RuntimeError`` (the failure shape ``_alfred_vault_cmd`` produces
    on non-zero CLI exit or empty/unparseable stdout), prints a
    structured ``PARTIAL MIGRATION`` sentinel naming the count of
    records already written + a recovery pointer, then re-raises so
    ``main`` can surface a non-zero exit. Per
    ``feedback_intentionally_left_blank.md``: a mid-stream failure
    without operator-facing signal would leave the operator staring
    at a Python traceback with no idea what shipped vs. what didn't.

    Idempotency holds across the partial state: the skip-already-clean
    logic in ``build_plan`` means a re-run skips records that landed
    successfully and retries the failed one.
    """
    env = _build_subprocess_env(vault)
    counters = {
        "records_stripped": 0,
        "fields_unset":     0,
    }

    try:
        for entry in plan.records_pending:
            fields_str = ", ".join(entry.fields_present)
            print(f"  stripping V1 tier fields: {entry.rel_path}  (unset: {fields_str})")
            _apply_strip(entry, env)
            counters["records_stripped"] += 1
            counters["fields_unset"] += len(entry.fields_present)
    except RuntimeError:
        # Partial-state sentinel — operator-facing AND grep-able.
        total_records = counters["records_stripped"]
        total_fields = counters["fields_unset"]
        print(
            f"\n--- PARTIAL MIGRATION — {total_records} record(s) "
            f"stripped ({total_fields} field(s) unset) before failure ---"
        )
        print(
            "--- Recovery: re-run the script; idempotency will skip "
            "already-clean records and retry the failed one. ---"
        )
        raise

    return counters


# --- CLI -------------------------------------------------------------------


def _default_vault_path() -> Path:
    """Resolve the default Salem vault path.

    Order: ``$ALFRED_VAULT_PATH`` env var > ``/home/andrew/alfred/vault``
    fallback. The fallback is hardcoded to Andrew's known Salem path
    (this script ships ONCE for the V1 strip; not a generic tool).
    """
    env_path = os.environ.get("ALFRED_VAULT_PATH")
    if env_path:
        return Path(env_path).expanduser()
    return Path("/home/andrew/alfred/vault")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "V1 tier-field strip migration (2026-05-30). Removes "
            "base_tier / escalate_to / escalate_at_days from every "
            "task/*.md record that carries them. Default mode is LIVE "
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
    try:
        counters = apply_plan(plan, vault)
    except RuntimeError as exc:
        # ``apply_plan`` already printed PARTIAL MIGRATION + recovery
        # pointer before re-raising. Emit a tail line with the
        # underlying cause (summary string from ``_alfred_vault_cmd``
        # carries exit code + stderr/stdout excerpt per builder.md
        # "Subprocess Failure Logging") to stderr.
        print(f"--- Failure cause: {exc}", file=sys.stderr)
        return 1
    print()
    print("Migration complete:")
    print(f"  records stripped:    {counters['records_stripped']}")
    print(f"  fields unset:        {counters['fields_unset']}")
    print(
        "Summary: "
        f"records_stripped={counters['records_stripped']} "
        f"fields_unset={counters['fields_unset']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
