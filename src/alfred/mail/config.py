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
class MailConfig:
    accounts: list[MailAccount] = field(default_factory=list)
    poll_interval: int = 300  # seconds
    state_path: str = "./data/mail_state.json"
    inbox_dir: str = "inbox"


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
    return MailConfig(
        accounts=accounts,
        poll_interval=section.get("poll_interval", 300),
        state_path=section.get("state", {}).get("path", "./data/mail_state.json"),
        inbox_dir=section.get("inbox_dir", "inbox"),
    )
