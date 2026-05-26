"""``alfred routine`` subcommand handlers.

Phase 1 commands:

  - ``alfred routine done <record> <item>`` — append today's date to
    ``completion_log[item]`` on the routine record. Single source of
    truth for date-append semantics; Phase 2 will surface this through
    Telegram.
  - ``alfred routine run-now`` — force-build today's daily aggregator
    note. Useful for ad-hoc operator runs + testing.
  - ``alfred routine status`` — print last run + schedule summary.

The ``done`` verb mutates the ``completion_log`` frontmatter field on
``routine/<record>.md``. The mutation is append-only and idempotent:
calling ``done`` twice with the same item on the same day yields one
log entry (no duplicate dates within a single day).

Salem-only enforcement: every command checks
``config.instance_name == REQUIRED_INSTANCE`` and raises a clear
ScopeError on mismatch. The aggregator daemon's start-guard handles
the same check separately; the CLI guard exists so an operator
invoking ``alfred routine done`` on a non-Salem instance gets a
visible refusal rather than silently mutating the wrong vault.
"""

from __future__ import annotations

import sys
from datetime import date as date_type, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import frontmatter  # type: ignore[import-untyped]
import structlog
import yaml

from alfred.vault.scope import ScopeError

from .aggregator import run_aggregator_once
from .config import REQUIRED_INSTANCE, RoutineConfig
from .state import StateManager

log = structlog.get_logger(__name__)


def _check_salem_only(config: RoutineConfig) -> None:
    """Raise ScopeError unless the active instance is Salem.

    Salem-only contract surfaces at three layers:
      - schema (routine in canonical scope only)
      - scope rules (HYPATIA / KALLE create allowlists exclude routine)
      - daemon-start guard + this CLI guard (instance-level refusal)

    The two-layer scope.create check would fail anyway on a non-Salem
    config, but the routine record-mutation path bypasses scope (the
    CLI rewrites the frontmatter directly via frontmatter.dumps rather
    than going through vault_edit). Hence the explicit gate here.
    """
    if config.instance_name != REQUIRED_INSTANCE:
        raise ScopeError(
            f"alfred routine is Salem-only in Phase 1. Detected "
            f"instance: {config.instance_name!r} (required: "
            f"{REQUIRED_INSTANCE!r}). Per the Phase 1 ratified "
            f"contract, only the Salem instance maintains routine "
            f"records — Hypatia and KAL-LE have no canonical surface "
            f"for them. Phase 2 may relax this; today, refuse."
        )


def _routine_path(vault_path: Path, record: str) -> Path:
    """Resolve a routine name to its on-disk path.

    Accepts either the bare record name (``"For Self Health"``) or a
    relative path (``"routine/For Self Health"``). Returns the
    absolute path; raises ``FileNotFoundError`` when the file is
    missing.
    """
    routine_dir = vault_path / "routine"
    if record.endswith(".md"):
        record = record[:-3]
    if record.startswith("routine/"):
        record = record[len("routine/"):]
    candidate = routine_dir / f"{record}.md"
    if not candidate.exists():
        raise FileNotFoundError(
            f"Routine record not found: {candidate} "
            f"(looking under {routine_dir})"
        )
    return candidate


def _today_iso(tz_name: str) -> str:
    """Return today's ISO date string in the configured timezone.

    Read from ``config.schedule.timezone`` so the date matches the
    aggregator's daily fire boundary — relevant near midnight when the
    OS clock might be in UTC but the operator's day boundary is Halifax.
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        # Fall back to date.today() if the tz string is bad — surface a
        # warning, don't crash. The operator's normal config has a valid
        # tz; this path only fires on a typo.
        log.warning("routine.cli.bad_timezone", tz=tz_name)
        return date_type.today().isoformat()
    return datetime.now(tz).date().isoformat()


def cmd_done(
    config: RoutineConfig,
    record_name: str,
    item_text: str,
    *,
    wants_json: bool = False,
    today_override: str | None = None,
) -> int:
    """Append today's date to ``completion_log[item_text]`` on the record.

    Returns exit code (0 on success, 1 on failure). Idempotent — re-runs
    with the same (record, item, date) are no-ops at the data layer.
    """
    _check_salem_only(config)
    vault_path = Path(config.vault_path)
    path = _routine_path(vault_path, record_name)

    iso = today_override or _today_iso(config.schedule.timezone)
    post = frontmatter.load(str(path))
    fm = dict(post.metadata or {})

    completion_log_raw = fm.get("completion_log") or {}
    if not isinstance(completion_log_raw, dict):
        # Operator hand-edit dropped the dict — restore.
        log.warning(
            "routine.cli.completion_log_not_dict",
            path=str(path),
            type=type(completion_log_raw).__name__,
        )
        completion_log_raw = {}
    completion_log: dict[str, list[str]] = {}
    for key, val in completion_log_raw.items():
        # Normalise: each value should be a list of ISO date strings.
        # Tolerate scalar-as-single-list and YAML-native date objects.
        if isinstance(val, list):
            normalised: list[str] = []
            for v in val:
                if isinstance(v, date_type):
                    normalised.append(v.isoformat())
                elif isinstance(v, str):
                    normalised.append(v)
                else:
                    log.debug(
                        "routine.cli.skipping_bad_log_entry",
                        key=str(key), value=repr(v),
                    )
            completion_log[str(key)] = normalised
        elif isinstance(val, (str, date_type)):
            completion_log[str(key)] = [
                val.isoformat() if isinstance(val, date_type) else val
            ]
        else:
            completion_log[str(key)] = []

    existing = completion_log.get(item_text, [])

    # Validate the item actually exists on this routine — strict path
    # so the operator catches typos early. (Compare against item.text
    # values in ``items``.)
    raw_items = fm.get("items") or []
    if not isinstance(raw_items, list):
        raw_items = []
    known_texts = {
        str((it or {}).get("text") or "").strip()
        for it in raw_items
        if isinstance(it, dict)
    }
    if item_text not in known_texts:
        message = (
            f"Item {item_text!r} not found in routine {record_name!r}. "
            f"Known items: {sorted(known_texts) if known_texts else '(none)'}"
        )
        if wants_json:
            import json
            print(json.dumps({"ok": False, "error": message}, indent=2))
        else:
            print(message, file=sys.stderr)
        return 1

    # Idempotent: only append when today's date isn't already in the list.
    if iso in existing:
        new_list = existing
        appended = False
    else:
        new_list = existing + [iso]
        appended = True

    completion_log[item_text] = new_list
    fm["completion_log"] = completion_log

    # Round-trip: frontmatter.dumps re-emits the file with the mutated
    # metadata. We bypass ``vault_edit`` here because routine completion
    # logging is a structured frontmatter mutation that doesn't fit the
    # set_fields shape (per-key value-list append) and the Salem-only
    # guard above is the operative gate.
    new_post = frontmatter.Post(post.content, **fm)
    # frontmatter.dumps uses ``yaml.safe_dump`` internally, which sorts
    # keys by default. We want to preserve the operator's original key
    # order — emit the frontmatter ourselves with sort_keys=False.
    fm_yaml = yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False)
    out = f"---\n{fm_yaml}---\n\n{new_post.content}\n"
    path.write_text(out, encoding="utf-8")

    log.info(
        "routine.cli.done",
        record=record_name,
        item=item_text,
        date=iso,
        appended=appended,
        path=str(path.relative_to(vault_path)),
    )
    if wants_json:
        import json
        print(json.dumps({
            "ok": True,
            "record": record_name,
            "item": item_text,
            "date": iso,
            "appended": appended,
            "path": str(path.relative_to(vault_path)),
        }, indent=2))
    elif appended:
        print(f"Logged: {record_name} / {item_text} @ {iso}")
    else:
        print(f"Already logged today: {record_name} / {item_text} @ {iso}")
    return 0


def cmd_run_now(
    config: RoutineConfig,
    *,
    wants_json: bool = False,
    today_override: str | None = None,
) -> int:
    """Force-build today's daily aggregator note. Useful for ad-hoc runs."""
    _check_salem_only(config)
    state_mgr = StateManager(config.state.path)
    state_mgr.load()
    if today_override:
        today = date_type.fromisoformat(today_override)
    else:
        today = datetime.now(ZoneInfo(config.schedule.timezone)).date()
    rel_path = run_aggregator_once(config, today, state_mgr)
    if wants_json:
        import json
        print(json.dumps({
            "ok": True,
            "date": today.isoformat(),
            "path": rel_path,
        }, indent=2))
    else:
        print(f"Aggregator wrote: {rel_path}")
    return 0


def cmd_status(config: RoutineConfig, *, wants_json: bool = False) -> int:
    """Print last run + schedule summary."""
    _check_salem_only(config)
    state_mgr = StateManager(config.state.path)
    state_mgr.load()
    latest = state_mgr.state.latest()
    payload: dict[str, Any] = {
        "schedule": {
            "time": config.schedule.time,
            "timezone": config.schedule.timezone,
        },
        "vault_path": config.vault_path,
        "instance_name": config.instance_name,
        "latest": latest.to_dict() if latest else None,
        "run_count": len(state_mgr.state.runs),
    }
    if wants_json:
        import json
        print(json.dumps(payload, indent=2))
        return 0

    print("=" * 60)
    print("ALFRED ROUTINE STATUS")
    print("=" * 60)
    print(f"Schedule:      {config.schedule.time} {config.schedule.timezone}")
    print(f"Instance:      {config.instance_name}")
    print(f"Vault:         {config.vault_path}")
    if latest:
        print(f"Last run:      {latest.generated_at}")
        print(f"  date:        {latest.date}")
        print(f"  path:        {latest.vault_path}")
        print(f"  routines:    {latest.routines_contributing}")
        print(f"  items:       {latest.item_count}")
        print(f"  critical:    {latest.critical_pending}")
    else:
        # Per intentionally-left-blank: emit visible "no run yet" rather
        # than silence.
        print("Last run:      never")
    print(f"Runs recorded: {len(state_mgr.state.runs)}")
    return 0
