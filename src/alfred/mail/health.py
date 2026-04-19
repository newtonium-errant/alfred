"""Mail health check — static only.

Per plan Part 11: the mail check is **static only**.  We do not probe
IMAP auth because the IMAP flow burns connections and can trigger
provider-side rate limits, and because an incorrect password here
won't show up as a health-check-time warning anyway (the runtime
fetcher logs rate-limit errors when they happen).

Probes:
  * mail section present — if absent, SKIP
  * accounts configured — if empty, WARN (the user has a section but
    no accounts)
  * per-account required fields present (name, email, imap_host)
  * inbox dir exists under vault
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alfred.health.aggregator import register_check
from alfred.health.types import CheckResult, Status, ToolHealth


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


async def health_check(raw: dict[str, Any], mode: str = "quick") -> ToolHealth:
    """Run mail health checks (static only)."""
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

    status = Status.worst([r.status for r in results])
    return ToolHealth(tool="mail", status=status, results=results)


register_check("mail", health_check)
