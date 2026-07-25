"""Mail health check — static config validation + account-delivery liveness.

Static probes (no IMAP connection — the runtime fetcher logs rate-limit
errors when they happen, and burning a probe connection risks provider
rate limits):
  * mail section present — if absent, SKIP
  * accounts configured — if empty, WARN (the user has a section but
    no accounts)
  * per-account required fields present (name, email, imap_host)
  * inbox dir exists under vault

Account-delivery liveness (#31 — Monitor A): per configured account, how
long since the last email ARRIVED. Sourced from the arrival artifact — the
raw ``email-{account}-*.md`` file the fetcher/webhook write to the inbox and
the curator moves (filename + mtime retained) to ``inbox/processed/``. This
is deliberately INDEPENDENT of whether the curator managed to STRUCTURE the
record (that is Monitor B's job): a raw email sitting unstructured in
``processed/`` still counts as "delivered." Would have caught the Gmail
intake going silently dead for ~2 months (newest record May 22, discovered
only during the cutover). Per ``feedback_intentionally_left_blank.md``:
account silence must be distinguishable from a healthy quiet account.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from alfred.health.aggregator import register_check
from alfred.health.types import CheckResult, Status, ToolHealth


# Default delivery-liveness thresholds (days). Surfaced as module constants
# so threshold-tuning is a 1-line change, mirroring curator/health.py's
# ``_CURATOR_STALE_*`` precedent. Generous by design (the false-alarm guard):
# a legitimately-quiet account should not cry wolf. Per-account + global
# overrides come from ``mail.liveness`` in config (see ``_check_liveness``).
_MAIL_LIVENESS_WARN_DAYS = 3
_MAIL_LIVENESS_FAIL_DAYS = 7

# Where the curator moves processed arrival files (its config default). Read
# from ``curator.processed_dir`` at runtime; this is the fallback.
_DEFAULT_PROCESSED_DIR = "inbox/processed"


def _check_accounts(accounts: list) -> list[CheckResult]:
    """Static validation of each configured mail account.

    Missing name/email/imap_host are FAIL — those are required to
    even attempt a connection, and misconfigured accounts would
    error in a subtle way at runtime otherwise.
    """
    out: list[CheckResult] = []
    if not accounts:
        out.append(CheckResult(
            name="mail-accounts",
            status=Status.WARN,
            detail="mail section present but no accounts configured",
        ))
        return out

    for idx, acc in enumerate(accounts):
        missing: list[str] = []
        for field in ("name", "email", "imap_host"):
            if not acc.get(field):
                missing.append(field)
        label = acc.get("name") or acc.get("email") or f"account[{idx}]"
        if missing:
            out.append(CheckResult(
                name=f"account:{label}",
                status=Status.FAIL,
                detail=f"missing required fields: {', '.join(missing)}",
            ))
        else:
            out.append(CheckResult(
                name=f"account:{label}",
                status=Status.OK,
                detail=f"{acc.get('email')} on {acc.get('imap_host')}",
                data={"name": label, "email": acc.get("email")},
            ))
    return out


def _check_inbox(raw: dict[str, Any], mail: dict) -> CheckResult:
    vault_path_str = (raw.get("vault", {}) or {}).get("path", "") or ""
    if not vault_path_str:
        return CheckResult(
            name="inbox-dir",
            status=Status.FAIL,
            detail="vault.path not set",
        )
    inbox_rel = mail.get("inbox_dir", "inbox")
    full = Path(vault_path_str) / inbox_rel
    if not full.exists():
        return CheckResult(
            name="inbox-dir",
            status=Status.WARN,
            detail=f"inbox missing: {full}",
        )
    return CheckResult(
        name="inbox-dir",
        status=Status.OK,
        detail=str(full),
        data={"path": str(full)},
    )


def _newest_arrival_mtime(
    vault: Path, inbox_dir: str, processed_dir: str, account_name: str
) -> float | None:
    """Newest mtime of an ``email-{account}-*.md`` arrival file across the
    inbox and the curator's processed dir. ``None`` if the account has no
    arrival records anywhere.

    Uses a manual prefix/suffix filter rather than ``glob`` so an account
    name containing a glob-special char (``[``, ``*``, ``?``) can't corrupt
    the match. mtime (not filename-timestamp parsing) is the age key — it is
    set at write time, preserved by ``shutil.move`` into ``processed/``, and
    is format-agnostic across the fetcher + webhook filename shapes.
    """
    prefix = f"email-{account_name}-"
    newest: float | None = None
    for rel in (inbox_dir, processed_dir):
        d = vault / rel
        if not d.is_dir():
            continue
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for f in entries:
            if not (f.name.startswith(prefix) and f.name.endswith(".md")):
                continue
            try:
                m = f.stat().st_mtime
            except OSError:
                continue
            if newest is None or m > newest:
                newest = m
    return newest


def _check_account_liveness(
    vault: Path,
    inbox_dir: str,
    processed_dir: str,
    name: str,
    warn_days: int,
    fail_days: int,
) -> CheckResult:
    """One account's delivery-liveness CheckResult.

    SKIP when the account has never delivered (fresh account / nothing
    arrived yet — distinct from "went dark"). Otherwise OK/WARN/FAIL on the
    age of the newest arrival, WARN-before-FAIL. Per ILB: a quiet-but-fresh
    account is not a failure; a previously-live account gone silent is.
    """
    newest = _newest_arrival_mtime(vault, inbox_dir, processed_dir, name)
    if newest is None:
        # No baseline to measure staleness against. A configured account with
        # no delivered records yet is healthy-quiet, not a failure — return OK
        # (not SKIP, which ``Status.worst`` ranks above OK and would drag the
        # whole mail tool to SKIP on every fresh/quiet install). The WARN/FAIL
        # escalation below only engages once an account HAS delivered, which is
        # exactly the gmail-went-silent case. The "configured but never
        # delivered" edge is unassessable without an expected-cadence baseline
        # (the Phase-2 learned-cadence follow-up).
        return CheckResult(
            name=f"delivery:{name}",
            status=Status.OK,
            detail="no delivered records yet (fresh/quiet account — nothing to measure)",
            data={"account": name},
        )
    age_days = (time.time() - newest) / 86400.0
    payload: dict[str, Any] = {
        "account": name,
        "age_days": round(age_days, 2),
        "warn_days": warn_days,
        "fail_days": fail_days,
    }
    if age_days > fail_days:
        return CheckResult(
            name=f"delivery:{name}",
            status=Status.FAIL,
            detail=f"no delivery in {round(age_days, 1)}d (>{fail_days}d) — intake may be dead",
            data=payload,
        )
    if age_days > warn_days:
        return CheckResult(
            name=f"delivery:{name}",
            status=Status.WARN,
            detail=f"no delivery in {round(age_days, 1)}d (>{warn_days}d) — quiet",
            data=payload,
        )
    return CheckResult(
        name=f"delivery:{name}",
        status=Status.OK,
        detail=f"last delivery {round(age_days, 1)}d ago",
        data=payload,
    )


def _check_liveness(raw: dict[str, Any], mail: dict) -> list[CheckResult]:
    """Per-account delivery-liveness CheckResults (Monitor A).

    Config lives under ``mail.liveness`` and is read from the raw dict (the
    health aggregator hands each check the unified config, not the built
    dataclass — same convention as curator/health.py):

        mail:
          liveness:
            enabled: true      # default true
            warn_days: 3       # global default
            fail_days: 7
            accounts:
              gmail: {warn_days: 2, fail_days: 5}   # per-account override

    Returns a single SKIP line when explicitly disabled (ILB: intentionally
    off is distinguishable from missing). Empty list when there is nothing to
    check (no vault path / no accounts — those are surfaced by the static
    probes already).
    """
    liveness = mail.get("liveness") or {}
    if not liveness.get("enabled", True):
        # Observable (ILB) but non-dragging: OK-with-detail rather than SKIP,
        # which ``Status.worst`` would ratchet the whole mail tool down to.
        return [
            CheckResult(
                name="delivery-liveness",
                status=Status.OK,
                detail="delivery-liveness monitoring disabled (mail.liveness.enabled=false)",
            )
        ]

    vault_path_str = (raw.get("vault", {}) or {}).get("path", "") or ""
    accounts = mail.get("accounts", []) or []
    if not vault_path_str or not accounts:
        return []

    global_warn = int(liveness.get("warn_days", _MAIL_LIVENESS_WARN_DAYS))
    global_fail = int(liveness.get("fail_days", _MAIL_LIVENESS_FAIL_DAYS))
    per_account = liveness.get("accounts", {}) or {}
    inbox_dir = mail.get("inbox_dir", "inbox")
    processed_dir = (raw.get("curator", {}) or {}).get(
        "processed_dir", _DEFAULT_PROCESSED_DIR
    )
    vault = Path(vault_path_str)

    out: list[CheckResult] = []
    for acc in accounts:
        name = acc.get("name")
        if not name:
            # Malformed account already FAILed by _check_accounts; skip here.
            continue
        override = per_account.get(name, {}) or {}
        warn_days = int(override.get("warn_days", global_warn))
        fail_days = int(override.get("fail_days", global_fail))
        out.append(
            _check_account_liveness(
                vault, inbox_dir, processed_dir, name, warn_days, fail_days
            )
        )
    return out


async def health_check(raw: dict[str, Any], mode: str = "quick") -> ToolHealth:
    """Run mail health checks: static config validation + delivery liveness."""
    mail = raw.get("mail")
    if mail is None:
        return ToolHealth(
            tool="mail",
            status=Status.SKIP,
            detail="no mail section in config",
        )

    results: list[CheckResult] = []
    results.extend(_check_accounts(mail.get("accounts", []) or []))
    results.append(_check_inbox(raw, mail))
    results.extend(_check_liveness(raw, mail))

    status = Status.worst([r.status for r in results])
    return ToolHealth(tool="mail", status=status, results=results)


register_check("mail", health_check)
