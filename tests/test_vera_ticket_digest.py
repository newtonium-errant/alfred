"""Tests for the VERA ticket digest assembler + the assembler-source seam.

VERA P2 (2026-06-09). Covers:
  * ``assemble_ticket_digest`` — open/in_progress selected, terminal
    statuses excluded, newest-first ordering, plain-text format.
  * Empty-set intentionally-left-blank digest ("No open tickets").
  * The ``brief_digest_push.source`` config selector parse.
  * ``_assemble_for_source`` branch selection (tickets vs git_activity),
    with the git_activity default preserved byte-identically for KAL-LE.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import frontmatter
import pytest

from alfred.brief.kalle_brief_daemon import (
    BriefDigestPushConfig,
    _assemble_for_source,
    load_brief_digest_push_config,
)
from alfred.brief.vera_ticket_digest import (
    OPEN_TICKET_STATUSES,
    assemble_ticket_digest,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_vera_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vera-vault"
    (vault / "ticket").mkdir(parents=True)
    return vault


def _seed_ticket(
    vault: Path,
    filename: str,
    *,
    title: str,
    ticket_type: str = "bug",
    priority: str = "medium",
    status: str = "open",
    created: str = "2026-06-09",
) -> None:
    """Write a ticket record to ``<vault>/ticket/<filename>.md``."""
    post = frontmatter.Post(
        f"# {title}\n\nClaude Code brief body here.\n",
        type="ticket",
        title=title,
        ticket_type=ticket_type,
        reporter="Ben",
        area="checkout",
        priority=priority,
        status=status,
        created=created,
    )
    (vault / "ticket" / f"{filename}.md").write_text(
        frontmatter.dumps(post) + "\n", encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# OPEN_TICKET_STATUSES contract
# ---------------------------------------------------------------------------


def test_open_ticket_statuses_pinned():
    """Open = open + in_progress; terminal states excluded."""
    assert OPEN_TICKET_STATUSES == frozenset({"open", "in_progress"})
    for terminal in ("resolved", "closed", "wont_fix"):
        assert terminal not in OPEN_TICKET_STATUSES


# ---------------------------------------------------------------------------
# assemble_ticket_digest — selection
# ---------------------------------------------------------------------------


def test_open_and_in_progress_selected(tmp_path: Path):
    vault = _make_vera_vault(tmp_path)
    _seed_ticket(vault, "t-open", title="Open one", status="open")
    _seed_ticket(vault, "t-inprog", title="In progress one", status="in_progress")

    digest = assemble_ticket_digest(today=date(2026, 6, 9), vault_path=vault)

    assert "Open one" in digest
    assert "In progress one" in digest
    assert "2 open tickets" in digest


def test_terminal_statuses_excluded(tmp_path: Path):
    """resolved / closed / wont_fix tickets do NOT surface."""
    vault = _make_vera_vault(tmp_path)
    _seed_ticket(vault, "t-open", title="Still open", status="open")
    _seed_ticket(vault, "t-resolved", title="Resolved one", status="resolved")
    _seed_ticket(vault, "t-closed", title="Closed one", status="closed")
    _seed_ticket(vault, "t-wontfix", title="Wont fix one", status="wont_fix")

    digest = assemble_ticket_digest(today=date(2026, 6, 9), vault_path=vault)

    assert "Still open" in digest
    assert "Resolved one" not in digest
    assert "Closed one" not in digest
    assert "Wont fix one" not in digest
    assert "1 open ticket" in digest  # singular


def test_newest_first_ordering(tmp_path: Path):
    """Tickets render newest-first by created date."""
    vault = _make_vera_vault(tmp_path)
    _seed_ticket(vault, "t-old", title="Oldest", created="2026-06-01")
    _seed_ticket(vault, "t-mid", title="Middle", created="2026-06-05")
    _seed_ticket(vault, "t-new", title="Newest", created="2026-06-09")

    digest = assemble_ticket_digest(today=date(2026, 6, 9), vault_path=vault)

    pos_new = digest.index("Newest")
    pos_mid = digest.index("Middle")
    pos_old = digest.index("Oldest")
    assert pos_new < pos_mid < pos_old


def test_digest_line_format(tmp_path: Path):
    """Each line: type · title · priority: <p> · <status> (plain text)."""
    vault = _make_vera_vault(tmp_path)
    _seed_ticket(
        vault, "t1",
        title="Login button broken on checkout",
        ticket_type="bug",
        priority="high",
        status="open",
    )
    digest = assemble_ticket_digest(today=date(2026, 6, 9), vault_path=vault)

    assert (
        "- bug · Login button broken on checkout · priority: high · open"
        in digest
    )
    # Plain text — no emoji.
    assert "🐛" not in digest
    assert "✨" not in digest


def test_digest_header_carries_count_and_date(tmp_path: Path):
    vault = _make_vera_vault(tmp_path)
    _seed_ticket(vault, "t1", title="A", status="open")
    _seed_ticket(vault, "t2", title="B", status="in_progress")
    digest = assemble_ticket_digest(today=date(2026, 6, 9), vault_path=vault)
    assert digest.startswith("2 open tickets (as of 2026-06-09):")


# ---------------------------------------------------------------------------
# assemble_ticket_digest — empty-set intentionally-left-blank
# ---------------------------------------------------------------------------


def test_empty_set_renders_ilb_digest(tmp_path: Path):
    """No open tickets → explicit 'No open tickets' body (never empty)."""
    vault = _make_vera_vault(tmp_path)
    # Only a terminal-status ticket exists → nothing open.
    _seed_ticket(vault, "t-closed", title="Done", status="closed")

    digest = assemble_ticket_digest(today=date(2026, 6, 9), vault_path=vault)

    assert digest == "No open tickets (as of 2026-06-09)."
    assert digest != ""  # ILB invariant — never an empty string


def test_no_ticket_dir_renders_ilb_digest(tmp_path: Path):
    """Absent ticket/ dir → ILB digest, no crash."""
    vault = tmp_path / "empty-vault"
    vault.mkdir()
    digest = assemble_ticket_digest(today=date(2026, 6, 9), vault_path=vault)
    assert digest == "No open tickets (as of 2026-06-09)."


def test_malformed_record_skipped_not_fatal(tmp_path: Path):
    """A malformed ticket file is skipped; the digest still renders."""
    vault = _make_vera_vault(tmp_path)
    _seed_ticket(vault, "t-good", title="Good ticket", status="open")
    # Write a non-frontmatter / broken file into ticket/.
    (vault / "ticket" / "broken.md").write_text(
        "---\nthis: : is not: valid yaml: [\n---\nbody\n",
        encoding="utf-8",
    )
    digest = assemble_ticket_digest(today=date(2026, 6, 9), vault_path=vault)
    # The good ticket still surfaces.
    assert "Good ticket" in digest


def test_non_ticket_type_record_ignored(tmp_path: Path):
    """A non-ticket record in ticket/ (wrong type) is ignored."""
    vault = _make_vera_vault(tmp_path)
    _seed_ticket(vault, "t-real", title="Real ticket", status="open")
    note = frontmatter.Post("# Note\n", type="note", title="Stray note", status="open")
    (vault / "ticket" / "stray.md").write_text(
        frontmatter.dumps(note) + "\n", encoding="utf-8",
    )
    digest = assemble_ticket_digest(today=date(2026, 6, 9), vault_path=vault)
    assert "Real ticket" in digest
    assert "Stray note" not in digest
    assert "1 open ticket" in digest


def test_missing_priority_renders_unset(tmp_path: Path):
    """A ticket without a priority field renders 'priority: unset'."""
    vault = _make_vera_vault(tmp_path)
    post = frontmatter.Post(
        "# T\n",
        type="ticket", title="No priority", ticket_type="bug",
        reporter="Ben", area="x", status="open", created="2026-06-09",
    )  # no priority key
    (vault / "ticket" / "np.md").write_text(
        frontmatter.dumps(post) + "\n", encoding="utf-8",
    )
    digest = assemble_ticket_digest(today=date(2026, 6, 9), vault_path=vault)
    assert "priority: unset" in digest


# ---------------------------------------------------------------------------
# Config — source selector parse
# ---------------------------------------------------------------------------


def test_load_config_source_defaults_git_activity():
    """Absent source → git_activity (KAL-LE back-compat)."""
    raw: dict[str, Any] = {
        "logging": {"dir": "/tmp/d"},
        "brief_digest_push": {"enabled": True, "self_name": "kal-le"},
    }
    cfg = load_brief_digest_push_config(raw)
    assert cfg.source == "git_activity"


def test_load_config_source_tickets(tmp_path: Path):
    raw: dict[str, Any] = {
        "logging": {"dir": "/tmp/d"},
        "vault": {"path": str(tmp_path / "vera-vault")},
        "brief_digest_push": {
            "enabled": True,
            "self_name": "vera",
            "source": "tickets",
        },
    }
    cfg = load_brief_digest_push_config(raw)
    assert cfg.source == "tickets"
    # vault_path falls back to the unified vault.path when omitted from
    # the brief_digest_push block.
    assert cfg.vault_path == str(tmp_path / "vera-vault")


def test_load_config_explicit_vault_path_override(tmp_path: Path):
    custom = tmp_path / "custom-ticket-vault"
    raw: dict[str, Any] = {
        "logging": {"dir": "/tmp/d"},
        "vault": {"path": str(tmp_path / "ignored")},
        "brief_digest_push": {
            "enabled": True,
            "self_name": "vera",
            "source": "tickets",
            "vault_path": str(custom),
        },
    }
    cfg = load_brief_digest_push_config(raw)
    assert cfg.vault_path == str(custom)


# ---------------------------------------------------------------------------
# _assemble_for_source — branch selection + KAL-LE byte-identity
# ---------------------------------------------------------------------------


def test_assemble_for_source_tickets(tmp_path: Path):
    vault = _make_vera_vault(tmp_path)
    _seed_ticket(vault, "t1", title="Routing test", status="open")
    config = BriefDigestPushConfig(
        enabled=True, self_name="vera", source="tickets",
        vault_path=str(vault),
    )
    out = _assemble_for_source(config, date(2026, 6, 9))
    assert "Routing test" in out
    assert "1 open ticket" in out


def test_assemble_for_source_git_activity_calls_kalle_assembler(
    monkeypatch: pytest.MonkeyPatch,
):
    """Default source routes to the KAL-LE assembler with the same args.

    Byte-identity guard: ``_assemble_for_source`` with the default
    git_activity source must call ``assemble_digest`` (KAL-LE's) — NOT
    the ticket assembler — preserving KAL-LE's behaviour exactly.
    """
    captured: dict[str, Any] = {}

    def _fake_assemble_digest(**kw: Any) -> str:
        captured.update(kw)
        return "KALLE DIGEST OUTPUT"

    import alfred.brief.kalle_brief_daemon as daemon_mod
    monkeypatch.setattr(daemon_mod, "assemble_digest", _fake_assemble_digest)

    config = BriefDigestPushConfig(
        enabled=True, self_name="kal-le", source="git_activity",
        repo_paths=["/repo/a"], data_dir="/data",
    )
    out = _assemble_for_source(config, date(2026, 4, 23))

    assert out == "KALLE DIGEST OUTPUT"
    # The KAL-LE assembler received its expected kwargs.
    assert captured["today"] == date(2026, 4, 23)
    assert captured["data_dir"] == Path("/data")
    assert captured["repo_paths"] == [Path("/repo/a")]


def test_assemble_for_source_unknown_falls_back_to_git_activity(
    monkeypatch: pytest.MonkeyPatch,
):
    """An unknown source value degrades to git_activity (logged, no crash)."""
    def _fake_assemble_digest(**_kw: Any) -> str:
        return "KALLE FALLBACK"

    import alfred.brief.kalle_brief_daemon as daemon_mod
    monkeypatch.setattr(daemon_mod, "assemble_digest", _fake_assemble_digest)

    config = BriefDigestPushConfig(
        enabled=True, self_name="x", source="bogus_source",
        repo_paths=[], data_dir="/data",
    )
    out = _assemble_for_source(config, date(2026, 4, 23))
    assert out == "KALLE FALLBACK"
