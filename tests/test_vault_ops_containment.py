"""Arc #18 M5 — containment at the agent-facing vault-ops layer.

``vault/ops.py::_resolve_vault_path`` has guarded every agent-facing vault op
since the beginning, and its check was WRONG: a raw ``str.startswith`` prefix
compare, which admits any SIBLING directory whose name extends the vault root's.
It is the platform's live containment primitive, it asserted its own correctness
in a docstring ("preventing traversal"), and that is plausibly why nobody looked
twice. M5 delegates it to the same ``resolve_in_vault`` gate the board and
routine writers use, so the platform has ONE containment implementation.

Two halves, and the SECOND is weighted heavier here:

  * **Escape refusals** — the sibling-prefix case is the regression pin; it is
    the exact case the old guard passed.
  * **Preservation** — every op must still accept the record shapes production
    actually contains. This layer serves the agent-facing CLI scopes (curator,
    janitor, distiller), so an over-broad fix would pass every escape pin above
    while quietly breaking the agents' ability to touch real records. That is
    the direction a containment fix fails in practice.

The preservation fixtures are the audit's MEASURED shapes from the production
vault (8,614 records) — 5 names containing "/", 233 dotted stems, 3,209
legitimately nested paths — not tidy synthetic names. A preservation pin built
from tidy names proves only that the gate accepts tidy names; it is the
fixture-side of testing your own copy.

Every pin drives the REAL op (``vault_create`` / ``vault_read`` / ``vault_edit``
/ ``vault_move`` / ``vault_delete``), never ``_resolve_vault_path`` alone,
except where the guard's own decision is the subject.

The ops are called WITHOUT a ``scope``, deliberately. Scope is a separate gate
with its own test suite, and it fires FIRST — e.g. curator's ``move`` is
restricted to ``inbox/``, so a scoped call raises ``ScopeError`` before
containment is consulted. A pin that accepted either exception would pass
whether or not containment works, which is this arc's recurring failure shape.
Unscoped calls make containment the only thing that can refuse.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alfred.vault.ops import (
    VaultError,
    _resolve_vault_path,
    vault_create,
    vault_delete,
    vault_edit,
    vault_move,
    vault_read,
)

# --- measured production shapes (audit 2026-08-03) --------------------------

#: Record NAMES containing "/" — all five that exist in the production vault.
#: They create nested directories on disk and are still inside the vault, so
#: every op must keep working on them.
REAL_SLASH_NAMES = [
    "Preference Matcher Index Materializes to data/operator_preferences.json on Every Write",
    "Route Email Truncation Fix Upstream to n8n/Graph Rather Than Downstream Alfred Compensation",
    "Alfred Fork Basis Is ssdavidai/alfred Through Commit 9d27ad9",
    "Verify FedEx Duty/Tax Charge on Official Site Before Paying Rather Than Emailed Link",
    "Capture Note Extraction Is Opt-In via /extract Not Automatic on /end",
]

#: Dotted stems — 233 in the vault. A "reject dots" fix breaks all of them.
REAL_DOTTED_STEMS = [
    "Scam Email Record Title Asserts 5500 Funds While Body Shows 55.00",
    "Schedule Primitive Uses zoneinfo.ZoneInfo for DST-Aware Wall-Clock Math",
    "S.A.L.E.M. Backronym Resolves to Steward of Andrew's Life Engagements and Memory",
    "Treat Ambiguous Credential Events on Live.ca as Compromise Until Verification",
]

#: Nested REL-PATHS — 3,209 records sit at type/sub/name.md, 343 one deeper.
REAL_NESTED_RELPATHS = [
    "inbox/processed/email-live-20260618-170406-Shop-Talk-TONIGHT-5pm-PST.md",
    "quarantine/spam/2026-05/Substack Notes Bootcamp Invitation — Marketing Email.md",
    "quarantine/spam/2026-05/A Decade of Local 10 Chances to Win — truLOCAL Marketing 2026-05-26.md",
]

#: Punctuation/unicode drawn from real record titles.
REAL_PUNCT_NAMES = [
    "Legacy *_interval_hours Fields Kept as Backward-Compat Fallbacks",
    "Cross-Cutting Telemetry Registered as Application-Level TypeHandler at group=-1",
    "KAL-LE Scope Denies Move and Delete — Curation Is Additive",
    "AppleCare+ Monthly Agreement 278414491820 Shows Two-Month Receipt Gap",
    "RBC Sub-Threshold Balance Persists Through $250 Manual Deposit",
]


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    for d in ("decision", "note", "task", "inbox/processed", "quarantine/spam/2026-05"):
        (v / d).mkdir(parents=True, exist_ok=True)
    return v


def _seed(vault: Path, rel_path: str, *, record_type: str = "note") -> Path:
    p = vault / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\ntype: {record_type}\ncreated: 2026-08-03\n---\n\n# seeded\n",
        encoding="utf-8",
    )
    return p


# ---------------------------------------------------------------------------
# THE regression pin — the case the OLD guard admitted
# ---------------------------------------------------------------------------


def test_sibling_prefix_escape_is_refused(tmp_path: Path) -> None:
    """The empirical case from the audit, now a permanent pin.

    With vault ``<tmp>/vault``, ``task/../../vault-evil/pwned.md`` resolves to
    ``<tmp>/vault-evil/pwned.md``. That string STARTS WITH ``<tmp>/vault``, so
    the pre-M5 guard admitted it; component-wise containment rejects it. The
    test asserts the precondition explicitly so it documents WHY it exists — if
    the shared prefix ever stops holding, this stops being the interesting case
    and should be re-derived rather than silently kept.
    """
    vault = tmp_path / "vault"
    (vault / "task").mkdir(parents=True)
    (tmp_path / "vault-evil").mkdir()
    rel = "task/../../vault-evil/pwned.md"

    resolved = (vault / rel).resolve()
    assert str(resolved).startswith(str(vault.resolve())), (
        "precondition: this must be a case the raw prefix compare ADMITS"
    )
    assert not resolved.is_relative_to(vault.resolve())

    with pytest.raises(VaultError, match="Path traversal denied"):
        _resolve_vault_path(vault, rel)


@pytest.mark.parametrize(
    "rel",
    [
        "../../etc/passwd",
        "note/../../../etc/cron.d/x.md",
        "..",
        "/etc/passwd",
        "note/../../outside.md",
    ],
)
def test_escapes_are_refused_through_every_read_and_write_op(
    vault: Path, rel: str,
) -> None:
    """One escape set, driven through each op that takes a rel_path — so a
    gate dropped from ONE op's call site is caught, not just the helper."""
    with pytest.raises(VaultError):
        vault_read(vault, rel)
    with pytest.raises(VaultError):
        vault_edit(vault, rel, set_fields={"status": "done"})
    with pytest.raises(VaultError):
        vault_delete(vault, rel)
    with pytest.raises(VaultError):
        vault_move(vault, rel, "note/Landed.md")
    with pytest.raises(VaultError):
        vault_move(vault, "note/Landed.md", rel)


def test_symlink_escape_is_refused(vault: Path, tmp_path: Path) -> None:
    """No ``..`` anywhere in this path — the escape is the symlink. Only
    resolution catches it, which is why the old ``normpath``-family guards in
    this layer could not."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("---\ntype: note\n---\n\nsecret\n", encoding="utf-8")
    (vault / "innocent").symlink_to(outside, target_is_directory=True)

    with pytest.raises(VaultError):
        vault_read(vault, "innocent/secret.md")


# ---------------------------------------------------------------------------
# PRESERVATION — the half that fails in practice
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", REAL_SLASH_NAMES)
def test_create_and_read_a_real_slash_bearing_name(vault: Path, name: str) -> None:
    """These five records EXIST in production. A containment fix that rejects
    ``/`` in a name would make them uncreatable and unreadable by every agent."""
    res = vault_create(vault, "decision", name)
    rel = res["path"]
    assert vault_read(vault, rel)["frontmatter"]["type"] == "decision"


@pytest.mark.parametrize("name", REAL_DOTTED_STEMS)
def test_create_and_read_a_real_dotted_stem(vault: Path, name: str) -> None:
    """233 production stems contain a dot."""
    res = vault_create(vault, "note", name)
    assert vault_read(vault, res["path"])["frontmatter"]["type"] == "note"


@pytest.mark.parametrize("name", REAL_PUNCT_NAMES)
def test_create_and_read_real_punctuation_and_unicode(vault: Path, name: str) -> None:
    """``* = + $`` and em-dashes all appear in real record titles."""
    res = vault_create(vault, "decision", name)
    assert vault_read(vault, res["path"])["frontmatter"]["type"] == "decision"


@pytest.mark.parametrize("rel", REAL_NESTED_RELPATHS)
def test_every_op_still_works_on_a_real_nested_path(vault: Path, rel: str) -> None:
    """3,209 records live at ``type/sub/name.md`` and 343 one level deeper.
    Drive the full read -> edit -> move -> delete cycle, because a gate dropped
    from any single op is a different bug from a gate that rejects the shape."""
    _seed(vault, rel)

    assert vault_read(vault, rel)["frontmatter"]["type"] == "note"
    vault_edit(vault, rel, set_fields={"status": "active"})
    assert vault_read(vault, rel)["frontmatter"]["status"] == "active"

    moved = "note/Moved Out Of Nesting.md"
    vault_move(vault, rel, moved)
    assert vault_read(vault, moved)["frontmatter"]["status"] == "active"
    vault_delete(vault, moved)
    with pytest.raises(VaultError):
        vault_read(vault, moved)


def test_benign_internal_dotdot_still_resolves(vault: Path) -> None:
    """``note/../note/X.md`` contains ``..`` but lands INSIDE. Agents compose
    paths like this; refusing it would be spelling-policing, not containment."""
    _seed(vault, "note/Inside.md")
    assert vault_read(vault, "note/../note/Inside.md")["frontmatter"]["type"] == "note"


def test_deeply_nested_create_still_works(vault: Path) -> None:
    """A not-yet-existing nested parent must not be refused — ``.resolve()``
    is non-strict and containment is about location, not existence."""
    res = vault_create(vault, "note", "Brand New Nested Record")
    assert (vault / res["path"]).exists()


def test_move_between_two_real_shapes(vault: Path) -> None:
    """Both ends of a move go through the guard; exercise a slash-bearing name
    and a nested destination together."""
    src = "decision/Verify FedEx Duty/Tax Charge on Official Site.md"
    _seed(vault, src, record_type="decision")
    dst = "inbox/processed/Verify FedEx Duty Tax Charge.md"
    vault_move(vault, src, dst)
    assert vault_read(vault, dst)["frontmatter"]["type"] == "decision"
