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
class MailConfig:
    accounts: list[MailAccount] = field(default_factory=list)
    poll_interval: int = 300  # seconds
    state_path: str = "./data/mail_state.json"
    inbox_dir: str = "inbox"
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
        ))
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
    )
