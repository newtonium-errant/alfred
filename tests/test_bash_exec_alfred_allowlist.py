"""Allowlist gates for the ``alfred`` first-token additions.

The KAL-LE bash_exec gate admits a curated set of read + write
``alfred`` surfaces (reviews, digest, vault read, transport
propose-person). Anything outside that set must reject so a daemon
lifecycle command (``alfred up``, ``alfred down``) or a destructive
vault mutation (``alfred vault delete``) cannot escape the gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alfred.telegram import bash_exec


pytestmark = [pytest.mark.asyncio]


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    repo = home / "aftermath-alfred"
    repo.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return {"home": home, "repo": repo, "audit": tmp_path / "bash_exec.jsonl"}


@pytest.mark.parametrize("cmd", [
    "alfred reviews list --project alfred",
    "alfred reviews write --project alfred --topic t --body b",
    "alfred reviews read --project alfred --file 2026-04-25-x.md",
    "alfred reviews mark-addressed --project alfred --file 2026-04-25-x.md",
    "alfred digest write",
    "alfred digest preview",
    "alfred digest preview --window-days 14",
    "alfred transport propose-person salem 'Some Name'",
    "alfred vault read note/x.md",
])
async def test_alfred_subcommand_allowed_set_admits(sandbox, cmd):
    """Each ratified KAL-LE surface clears the allowlist gate.

    The CLI dispatch may still fail (no config / missing file / exec
    not found) — what we assert here is that the gate itself does not
    reject. The post-gate failure modes ``command_not_found`` and a
    non-zero exit are both fine; what must NOT show up is any
    ``*_not_allowlisted`` reason.
    """
    result = await bash_exec.execute(
        command=cmd,
        cwd=str(sandbox["repo"]),
        audit_path=str(sandbox["audit"]),
    )
    reason = result["reason"]
    assert "not_allowlisted" not in reason, (
        f"{cmd!r} should clear the gate; got reason={reason!r}"
    )
    assert not reason.startswith("alfred_") or "_requires_subcommand" not in reason, (
        f"{cmd!r}: gate rejected with {reason!r} but should have admitted"
    )


@pytest.mark.parametrize("cmd", [
    "alfred up",
    "alfred down",
    "alfred status",
    "alfred quickstart",
    "alfred curator",
    "alfred janitor scan",
    "alfred distiller run",
    "alfred surveyor watch",
    "alfred brief generate",
    "alfred talker watch",
    "alfred mail fetch",
    "alfred check",
    "alfred bit run-now",
])
async def test_alfred_top_subcommand_outside_allowlist_rejects(sandbox, cmd):
    result = await bash_exec.execute(
        command=cmd,
        cwd=str(sandbox["repo"]),
        audit_path=str(sandbox["audit"]),
    )
    assert result["exit_code"] == -1, cmd
    assert result["reason"].startswith("alfred_subcommand_not_allowlisted:"), (
        f"{cmd!r} should be subcommand-rejected; got {result['reason']!r}"
    )


@pytest.mark.parametrize("cmd,expect_prefix", [
    ("alfred", "alfred_requires_subcommand"),
    ("alfred reviews", "alfred_reviews_requires_subcommand"),
    ("alfred reviews delete --project alfred", "alfred_reviews_subcommand_not_allowlisted:"),
    ("alfred vault delete note/x.md", "alfred_vault_subcommand_not_allowlisted:"),
    ("alfred vault edit note/x.md", "alfred_vault_subcommand_not_allowlisted:"),
    ("alfred vault create note/x.md", "alfred_vault_subcommand_not_allowlisted:"),
    ("alfred transport rotate", "alfred_transport_subcommand_not_allowlisted:"),
    ("alfred transport status", "alfred_transport_subcommand_not_allowlisted:"),
    ("alfred digest delete", "alfred_digest_subcommand_not_allowlisted:"),
    ("alfred reviews open --project alfred", "alfred_reviews_subcommand_not_allowlisted:"),
])
async def test_alfred_inner_gate_rejects(sandbox, cmd, expect_prefix):
    result = await bash_exec.execute(
        command=cmd,
        cwd=str(sandbox["repo"]),
        audit_path=str(sandbox["audit"]),
    )
    assert result["exit_code"] == -1, cmd
    assert result["reason"].startswith(expect_prefix), (
        f"{cmd!r}: expected reason starts with {expect_prefix!r}, got {result['reason']!r}"
    )


async def test_alfred_token_was_not_allowlisted_before(sandbox):
    """Sanity: ``alfred`` is now in the first-token allowlist.

    Catches accidental removal of the addition. If this test fails
    with ``token_not_allowlisted:alfred``, someone reverted the
    bash_exec addition and broke KAL-LE's reviews/digest path.
    """
    result = await bash_exec.execute(
        command="alfred reviews list --project alfred",
        cwd=str(sandbox["repo"]),
        audit_path=str(sandbox["audit"]),
    )
    assert not result["reason"].startswith("token_not_allowlisted:")
