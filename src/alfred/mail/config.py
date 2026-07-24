"""Mail fetcher configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class MailAccount:
    name: str
    email: str
    imap_host: str
    imap_port: int = 993
    password: str = ""
    folders: list[str] = field(default_factory=lambda: ["INBOX"])
    mark_read: bool = True
    # #7 7a — this account is pulled by the NATIVE IMAP fetch loop (the daemon path), vs delivered by
    # the n8n webhook. Default False: an existing account (e.g. live.ca, which arrives via the Outlook
    # webhook) is NOT double-fetched. The Gmail rehome account sets ``fetch: true``. Gated ABOVE by the
    # global ``mail.fetch.enabled`` INERT switch — this flag only SELECTS which accounts the loop pulls
    # once the loop is turned on.
    fetch: bool = False

    def resolved_password(self) -> str:
        """Resolve ${VAR} references in password."""
        pw = self.password
        if pw.startswith("${") and pw.endswith("}"):
            var = pw[2:-1]
            return os.environ.get(var, "")
        return pw


@dataclass
class IdleTickConfig:
    """Mail idle-tick heartbeat — "intentionally left blank" liveness signal.

    A periodic ``mail.idle_tick`` log event so observers can distinguish
    *idle / healthy* from *broken*. Without it, a stretch with no inbound
    webhooks (or fetched emails) is indistinguishable from a hung daemon.

    Counter semantic: one webhook received OR one email fetched = one
    event. The webhook path is the live one in production (n8n forwards
    Outlook → tunnel → here); the IMAP fetcher counts too if it's the
    user's chosen path.

    Defaults are deliberately on — see ``src/alfred/common/heartbeat.py``
    for the cadence rationale.
    """

    enabled: bool = True
    interval_seconds: int = 60


@dataclass
class MailFetchConfig:
    """#7 7a — the native IMAP fetch LOOP gate (the rehome's run path).

    INERT by default (``enabled: False``): the mail daemon runs ONLY the webhook receiver, exactly as
    today — the fetch loop does not run and NEVER opens an IMAP connection. Setting ``enabled: true``
    (the operator-gated flip, 7b) starts a background fetch thread ALONGSIDE the webhook that
    periodically pulls the ``fetch: true`` accounts (Gmail) into the same inbox. The webhook is never
    evicted. ``poll_interval`` (seconds) governs the loop; falls back to ``MailConfig.poll_interval``."""

    enabled: bool = False
    poll_interval: int | None = None   # None ⇒ use MailConfig.poll_interval
    # #7 7b — the parity-proof shadow fetch (``alfred mail fetch --shadow``) writes READ-ONLY captured
    # records here, DELIBERATELY OUTSIDE the vault inbox so the curator never ingests them. Gitignored.
    # Not the daemon loop's concern (the loop always writes to the real inbox); this only scopes the
    # box-run parity harness. Default is under ``data/`` alongside the other non-vault runtime artifacts.
    shadow_dir: str = "./data/mail_shadow"


@dataclass
class MailConfig:
    accounts: list[MailAccount] = field(default_factory=list)
    poll_interval: int = 300  # seconds
    state_path: str = "./data/mail_state.json"
    inbox_dir: str = "inbox"
    # #7 7a — the native IMAP fetch-loop gate (INERT by default).
    fetch: MailFetchConfig = field(default_factory=MailFetchConfig)

    def fetch_poll_interval(self) -> int:
        """The fetch loop's cadence — its own override, else the top-level poll_interval."""
        return self.fetch.poll_interval if self.fetch.poll_interval else self.poll_interval

    def fetch_accounts(self) -> list[MailAccount]:
        """The accounts the native fetch loop pulls (``fetch: true``) — the webhook-delivered accounts
        (fetch: false) are excluded so they are never double-fetched."""
        return [a for a in self.accounts if a.fetch]
    # Idle-tick heartbeat — see :class:`IdleTickConfig`. Defaulted-on
    # via the dataclass default_factory; absent block in YAML keeps
    # ``enabled=True`` / ``interval_seconds=60``.
    idle_tick: IdleTickConfig = field(default_factory=IdleTickConfig)


def load_from_unified(raw: dict) -> MailConfig:
    """Build MailConfig from the unified config dict."""
    section = raw.get("mail", {})
    accounts = []
    for acc in section.get("accounts", []):
        accounts.append(MailAccount(
            name=acc.get("name", ""),
            email=acc.get("email", ""),
            imap_host=acc.get("imap_host", ""),
            imap_port=acc.get("imap_port", 993),
            password=acc.get("password", ""),
            folders=acc.get("folders", ["INBOX"]),
            mark_read=acc.get("mark_read", True),
            fetch=bool(acc.get("fetch", False)),
        ))
    # #7 7a — the native fetch-loop gate (INERT by default: absent block ⇒ enabled=False).
    fetch_raw = section.get("fetch") or {}
    fetch_cfg = MailFetchConfig(
        enabled=bool(fetch_raw.get("enabled", False)),
        poll_interval=fetch_raw.get("poll_interval"),
        shadow_dir=fetch_raw.get("shadow_dir", "./data/mail_shadow"),
    )
    # Idle-tick — defaulted-on; partial dict merges over dataclass default.
    idle_raw = section.get("idle_tick") or {}
    idle_tick = IdleTickConfig(
        enabled=bool(idle_raw.get("enabled", True)),
        interval_seconds=int(idle_raw.get("interval_seconds", 60)),
    )
    return MailConfig(
        accounts=accounts,
        poll_interval=section.get("poll_interval", 300),
        state_path=section.get("state", {}).get("path", "./data/mail_state.json"),
        inbox_dir=section.get("inbox_dir", "inbox"),
        idle_tick=idle_tick,
        fetch=fetch_cfg,
    )
