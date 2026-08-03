"""Arc #18 M0 — unit pins for the shared vault path-containment gate.

Two halves, both load-bearing:

  * **Escape pins** — every vector the 2026-08-03 audit identified must be
    refused, with the ``vault.containment.escape_denied`` signal emitted.
  * **Legitimate-name pins** — the shapes MEASURED in the real vault (8,614
    records) must all be ACCEPTED unchanged. These are the regression guard
    against someone "hardening" the gate into a character blocklist, which
    would reject real vault content: 5 real records have ``/`` in the name,
    233 stems contain ``.``, 3,209 records sit at nested paths.

The sibling-prefix case is the single most important pin in the file: it is
the case the CURRENT ``vault/ops.py::_resolve_vault_path`` fails (raw
``str.startswith`` compare). A pin set that cannot distinguish the new gate
from the old one has not earned the ship.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import structlog

from alfred.vault.paths import VaultContainmentError, resolve_in_vault

WRITER = "test.writer"


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """A vault root with the on-disk shape the escape pins need around it.

    The sibling directory is REAL (``<tmp>/vault-evil``) so the sibling-prefix
    pin exercises a genuinely reachable location, not a hypothetical one.
    """
    root = tmp_path / "vault"
    (root / "routine").mkdir(parents=True)
    (root / "task").mkdir(parents=True)
    (tmp_path / "vault-evil").mkdir()
    return root


def _denials(cap: list[dict]) -> list[dict]:
    return [c for c in cap if c.get("event") == "vault.containment.escape_denied"]


# ---------------------------------------------------------------------------
# Escape pins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rel_path", "label"),
    [
        ("../../etc/passwd", "classic_traversal"),
        ("routine/../../../../etc/cron.d/x.md", "deep_traversal_audit_shape"),
        ("..", "bare_dotdot"),
        ("routine/../../outside/x.md", "embedded_rejoin"),
        ("./../../outside.md", "leading_dot_slash"),
    ],
)
def test_traversal_is_refused(vault: Path, rel_path: str, label: str) -> None:
    """``..``-bearing paths that land outside the vault are refused."""
    with structlog.testing.capture_logs() as cap:
        with pytest.raises(VaultContainmentError):
            resolve_in_vault(vault, rel_path, writer=WRITER)
    denials = _denials(cap)
    assert len(denials) == 1, label
    assert denials[0]["writer"] == WRITER
    assert denials[0]["rel_path"] == rel_path
    assert denials[0]["reason"] == "outside_vault_root"


def test_sibling_prefix_escape_is_refused(vault: Path) -> None:
    """THE pin that distinguishes this gate from the one it replaces.

    ``<tmp>/vault`` + ``../vault-evil/pwned.md`` resolves to
    ``<tmp>/vault-evil/pwned.md``. That string STARTS WITH ``<tmp>/vault``, so
    the incumbent ``vault/ops.py::_resolve_vault_path`` prefix compare ADMITS
    it. Component-wise ``is_relative_to`` correctly rejects it.

    If this test ever goes green against a ``startswith`` implementation, the
    gate has silently regressed to the defect this arc exists to fix.
    """
    rel = "../vault-evil/pwned.md"

    # Demonstrate the incumbent guard's answer, so the pin documents WHY it
    # exists rather than merely asserting an outcome.
    resolved = (vault / rel).resolve()
    assert str(resolved).startswith(str(vault.resolve())), (
        "precondition: this case must be one the prefix compare admits"
    )
    assert not resolved.is_relative_to(vault.resolve())

    with structlog.testing.capture_logs() as cap:
        with pytest.raises(VaultContainmentError) as exc:
            resolve_in_vault(vault, rel, writer=WRITER)
    assert exc.value.reason == "outside_vault_root"
    assert len(_denials(cap)) == 1


def test_absolute_path_override_is_refused(vault: Path) -> None:
    """pathlib silently discards the root on an absolute join
    (``Path('/vault') / '/etc/passwd'`` -> ``/etc/passwd``); the gate refuses
    rather than honouring it."""
    assert vault / "/etc/passwd" == Path("/etc/passwd")  # the footgun itself
    with structlog.testing.capture_logs() as cap:
        with pytest.raises(VaultContainmentError) as exc:
            resolve_in_vault(vault, "/etc/passwd", writer=WRITER)
    assert exc.value.reason == "outside_vault_root"
    assert len(_denials(cap)) == 1


def test_symlink_escape_is_refused(vault: Path, tmp_path: Path) -> None:
    """A symlink INSIDE the vault pointing OUT is refused.

    This vector carries no ``..`` — the composed path looks like an ordinary
    in-vault relative path — which is precisely why ``os.path.normpath``-only
    guards miss it and why ``.resolve()`` is load-bearing. It is the residual
    ``vault/scope.py::_delete_target_class`` names as "the boarded
    resolve-against-vault arc".
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("secret", encoding="utf-8")
    (vault / "innocent").symlink_to(outside, target_is_directory=True)

    rel = "innocent/secret.md"
    assert ".." not in rel  # the whole point: nothing to string-match on

    with structlog.testing.capture_logs() as cap:
        with pytest.raises(VaultContainmentError) as exc:
            resolve_in_vault(vault, rel, writer=WRITER)
    assert exc.value.reason == "outside_vault_root"
    assert len(_denials(cap)) == 1


def test_symlinked_vault_root_still_contains(tmp_path: Path) -> None:
    """When the VAULT ROOT itself is reached through a symlink, an in-vault
    path must still be accepted — ``vault_root`` is resolved too, so both sides
    of the comparison are realpaths. Pins that the root-side ``.resolve()``
    isn't decorative."""
    real = tmp_path / "real-vault"
    (real / "routine").mkdir(parents=True)
    (real / "routine" / "Foo.md").write_text("x", encoding="utf-8")
    link = tmp_path / "linked-vault"
    link.symlink_to(real, target_is_directory=True)

    out = resolve_in_vault(link, "routine/Foo.md", writer=WRITER)
    assert out == (real / "routine" / "Foo.md").resolve()


@pytest.mark.parametrize("rel_path", ["", "   ", "\t"])
def test_empty_path_is_refused(vault: Path, rel_path: str) -> None:
    """An empty path joins to the vault ROOT — technically inside, never a
    valid record target. Fail-closed with a distinct reason so a caller bug
    surfaces instead of being absorbed."""
    with structlog.testing.capture_logs() as cap:
        with pytest.raises(VaultContainmentError) as exc:
            resolve_in_vault(vault, rel_path, writer=WRITER)
    assert exc.value.reason == "empty_path"
    assert len(_denials(cap)) == 1


def test_embedded_nul_is_refused(vault: Path) -> None:
    """An embedded NUL raises ValueError inside pathlib; the gate converts it
    to a refusal rather than letting it crash the writer."""
    with structlog.testing.capture_logs() as cap:
        with pytest.raises(VaultContainmentError) as exc:
            resolve_in_vault(vault, "routine/\x00evil.md", writer=WRITER)
    assert exc.value.reason.startswith("unresolvable:")
    assert len(_denials(cap)) == 1


def test_writer_is_required_keyword(vault: Path) -> None:
    """``writer`` must be a REQUIRED keyword — per builder.md's optional-gate
    rule, a defaulted gate parameter is a standing trap (tests thread it,
    production doesn't, every pin stays green). Pinned so nobody 'helpfully'
    gives it a default."""
    with pytest.raises(TypeError):
        resolve_in_vault(vault, "routine/Foo.md")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Legitimate-name pins — measured shapes from the real vault (audit section 6)
# ---------------------------------------------------------------------------


#: Real record names from the production vault. The first five genuinely
#: contain ``/`` (they created stray nested directories on disk); a slash-
#: rejecting blocklist would refuse all five.
REAL_SLASH_NAMES = [
    "decision/Preference Matcher Index Materializes to data/operator_preferences.json on Every Write.md",
    "decision/Route Email Truncation Fix Upstream to n8n/Graph Rather Than Downstream Alfred Compensation.md",
    "decision/Alfred Fork Basis Is ssdavidai/alfred Through Commit 9d27ad9.md",
    "decision/Verify FedEx Duty/Tax Charge on Official Site Before Paying Rather Than Emailed Link.md",
    "decision/Capture Note Extraction Is Opt-In via /extract Not Automatic on /end.md",
]

#: Dot-bearing stems (233 in the real vault) — a "reject dots" rule breaks these.
REAL_DOT_NAMES = [
    "contradiction/Scam Email Record Title Asserts 5500 Funds While Body Shows 55.00.md",
    "decision/Schedule Primitive Uses zoneinfo.ZoneInfo for DST-Aware Wall-Clock Math.md",
    "note/Tim Denning Reminder Record Asserts live.ca Routing in Context.md",
]

#: Legitimately nested paths (3,209 at depth 3, 343 at depth 4).
REAL_NESTED_PATHS = [
    "inbox/processed/email-live-20260618-170406-Shop-Talk-TONIGHT-5pm-PST.md",
    "quarantine/spam/2026-05/Substack Notes Bootcamp Invitation — Marketing Email.md",
]

#: Punctuation/unicode drawn from real record titles.
REAL_PUNCT_NAMES = [
    "decision/Legacy *_interval_hours Fields Kept as Backward-Compat Fallbacks.md",
    "decision/Cross-Cutting Telemetry Registered as Application-Level TypeHandler at group=-1.md",
    "decision/KAL-LE Scope Denies Move and Delete — Curation Is Additive.md",
    "decision/S.A.L.E.M. Backronym Resolves to Steward of Andrew's Life Engagements and Memory.md",
    "contradiction/AppleCare+ Monthly Agreement 278414491820 Shows Two-Month Receipt Gap.md",
    "contradiction/RBC Sub-Threshold Balance Persists Through $250 Manual Deposit.md",
]


@pytest.mark.parametrize(
    "rel_path",
    REAL_SLASH_NAMES + REAL_DOT_NAMES + REAL_NESTED_PATHS + REAL_PUNCT_NAMES,
)
def test_real_vault_names_are_accepted(vault: Path, rel_path: str) -> None:
    """Every measured real-vault shape resolves inside and is ACCEPTED, with
    NO denial logged. This is the blocklist-regression guard."""
    with structlog.testing.capture_logs() as cap:
        out = resolve_in_vault(vault, rel_path, writer=WRITER)
    assert out.is_relative_to(vault.resolve())
    assert _denials(cap) == []


def test_benign_internal_dotdot_is_accepted(vault: Path) -> None:
    """``routine/../routine/Foo.md`` contains ``..`` but lands INSIDE the
    vault. It must be accepted — proof the gate tests destination, not
    spelling. (A ``".." in rel_path`` blocklist would fail this.)"""
    with structlog.testing.capture_logs() as cap:
        out = resolve_in_vault(vault, "routine/../routine/Foo.md", writer=WRITER)
    assert out == (vault / "routine" / "Foo.md").resolve()
    assert _denials(cap) == []


def test_returns_resolved_normalised_path(vault: Path) -> None:
    """The gate returns the RESOLVED path, so callers write exactly what was
    verified. Two spellings of the same record collapse to one path — which is
    what makes their ``file_rmw_lock`` sidecars agree."""
    a = resolve_in_vault(vault, "routine/Foo.md", writer=WRITER)
    b = resolve_in_vault(vault, "routine/../routine/Foo.md", writer=WRITER)
    assert a == b
    assert ".." not in str(a)
    assert a.is_absolute()


def test_accepts_path_and_str_alike(vault: Path) -> None:
    """Callers hold either; both must work identically."""
    assert resolve_in_vault(vault, "routine/Foo.md", writer=WRITER) == \
        resolve_in_vault(vault, Path("routine/Foo.md"), writer=WRITER)


def test_nonexistent_path_is_accepted(vault: Path) -> None:
    """Containment is about LOCATION, not existence — ``promote`` legitimately
    creates absent records. Existence checks stay the callers' business."""
    out = resolve_in_vault(vault, "routine/Does Not Exist Yet.md", writer=WRITER)
    assert out == (vault / "routine" / "Does Not Exist Yet.md").resolve()
    assert not out.exists()


def test_deeply_nested_new_path_is_accepted(vault: Path) -> None:
    """A not-yet-created nested path still resolves (non-strict) and is
    accepted — ``.resolve()`` must not require the parents to exist."""
    out = resolve_in_vault(
        vault, "quarantine/spam/2027-01/New Thing.md", writer=WRITER,
    )
    assert out.is_relative_to(vault.resolve())


def test_error_carries_structured_detail(vault: Path) -> None:
    """The exception exposes the same fields as the log line, so a caller that
    maps it onto its own vocabulary can still surface specifics."""
    with pytest.raises(VaultContainmentError) as exc:
        resolve_in_vault(vault, "../../etc/passwd", writer="routine.completion.done")
    err = exc.value
    assert err.writer == "routine.completion.done"
    assert err.rel_path == "../../etc/passwd"
    assert err.vault_root == str(vault.resolve())
    assert err.resolved == str(Path(os.path.dirname(os.path.dirname(str(vault.resolve())))) / "etc" / "passwd")
    assert "escapes the vault root" in str(err)
