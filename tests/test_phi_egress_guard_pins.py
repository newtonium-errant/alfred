"""Premise pins for the PHI-egress guards in the shipped example configs.

WHY THESE EXIST, AND WHY THEY LOOK UNUSUAL
==========================================

The guards this file defends are DEFAULT VALUES IN COMMENTED EXAMPLE YAML —
``web.voice.stt.shadow_capture.enabled: false`` at two sites in
``config.yaml.example``, and the ``- "Hypatia"`` entry in the ``vocab_terms``
example block. Every one of them is inside a ``#``-commented block.

That makes them invisible to the obvious instrument. ``yaml.safe_load`` of
``config.yaml.example`` discards comments wholesale, so DELETING a commented
guard leaves the loaded document byte-identical. Measured on this tree
(2026-08-21): deleting the ``- "Hypatia"`` line, deleting the
``enabled: false`` line, and flipping it to ``true`` each left a whole-file
``yaml.safe_load`` fingerprint IDENTICAL. A single-layer prover certifies
"nothing changed" for exactly the mutation class that matters here.

So these pins de-comment and PARSE the block, then assert on the resulting
Python object. Two consequences worth stating:

*   They bind to the PAYLOAD STRUCTURE, never to the literal text. The string
    ``- "Hypatia"`` occurs TWICE in ``config.yaml.example`` — once as the real
    entry in the ``vocab_terms`` list, and once inside the prose block "WHY THE
    SETTINGS NAMING HYPATIA STAY" that explains why the entry stays. A
    text-shaped check (``'- "Hypatia"' in example``) has a decoy and would pass
    with the real entry deleted.
    ``test_the_hypatia_pin_binds_to_structure_not_the_literal`` is the control
    that proves this pin does not.
    (Deliberately no line numbers: a coordinate into a file this lane also
    ANNOTATES expires on contact. The first draft of this docstring cited
    :1107, and the annotations in this same commit moved the entry to :1117 —
    false before it was ever read. Name the block, not the line.)

*   They keep working if an operator UNCOMMENTS the block: ``_payload`` strips
    at most one comment marker and returns live lines unchanged.

There is deliberately NO proximity pin ("the reason must appear within N lines
of the guard"). It was measured and disqualified: it scores red on the
corrected tree because its own top hits are the correction text. A pin that
fires on its own subject is not a pin.

WHAT THE GUARDS ARE, so a future reader does not sweep them
-----------------------------------------------------------
All clinical work is STAY-C's; Hypatia is the scholar/scribe instance and is
NOT clinical. The guards naming Hypatia are defence-in-depth after a REAL
2026-07-08 PHI-egress incident (a patient-PHI clinic conversation transited
Hypatia's cloud STT), not a claim about Hypatia's workload. They target REAL
CROSSOVER — clinical WORK routed to a non-clinical instance, or bulk/systematic
PHI reaching a cloud surface. They are NOT zero-tolerance: an incidental
non-work mention is an accepted residual at this stage of development. Full
note: "WHY THE SETTINGS NAMING HYPATIA STAY" in ``config.yaml.example``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "config.yaml.example"
CONFTEST = Path(__file__).resolve().parent / "conftest.py"

_COMMENT = re.compile(r"^([ \t]*)#[ ]?(.*)$")


def _payload(line: str) -> str:
    """Line content with at most ONE leading comment marker removed.

    Commented lines yield the text after ``# ``, preserving indentation
    RELATIVE to the marker; live lines yield themselves. A block therefore
    parses identically whether it ships commented out or an operator has
    uncommented it.
    """
    m = _COMMENT.match(line)
    return m.group(2) if m else line


def _indent(s: str) -> int:
    return len(s) - len(s.lstrip())


def parse_example_blocks(key: str, text: str | None = None) -> list[dict]:
    """Every ``<key>:`` block in the example, de-commented and YAML-parsed.

    An anchor is a line whose payload is EXACTLY ``<key>:`` — nothing after the
    colon. That is the structural bind: prose mentioning ``shadow_capture:
    enabled: false`` inside a sentence is not an anchor, because the payload
    carries trailing text.
    """
    src = EXAMPLE.read_text(encoding="utf-8") if text is None else text
    lines = src.splitlines()
    found: list[dict] = []
    for i, line in enumerate(lines):
        payload = _payload(line)
        if payload.strip() != f"{key}:":
            continue
        anchor = _indent(payload)
        block = [payload[anchor:]]
        for nxt in lines[i + 1:]:
            q = _payload(nxt)
            if not q.strip():
                block.append("")
                continue
            if _indent(q) <= anchor:
                break
            block.append(q[anchor:])
        found.append(yaml.safe_load("\n".join(block)))
    return found


# ---------------------------------------------------------------------------
# The guards themselves
# ---------------------------------------------------------------------------


def test_shadow_capture_ships_disabled_at_every_example_site():
    """Enabling shadow_capture egresses raw mic PCM to api.groq.com.

    Both example sites are ``web.voice.stt.shadow_capture`` — the session-mode
    block and the relay-mode block. The count is pinned as well as the values:
    a THIRD site appearing un-audited should fail loudly rather than ship
    un-pinned (extra noise beats a missed egress default, the same direction
    the health-status denylist takes).
    """
    blocks = parse_example_blocks("shadow_capture")
    assert len(blocks) == 2, (
        f"expected exactly 2 shadow_capture example sites, found {len(blocks)} "
        "— a new site needs its own audit before this pin is widened"
    )
    for block in blocks:
        assert block["shadow_capture"]["enabled"] is False


def test_hypatia_survives_in_the_vocab_terms_example():
    """``Hypatia`` is the instance's own NAME — a proper noun the STT must
    transcribe. It is NOT a PHI-egress guard, and it is NOT clinical vocab.

    It shares a file with the guards, so a sweep aimed at "de-clinicalising"
    Hypatia mentions can take it out by collateral. It is pinned here so that
    deletion is a red test rather than a silent transcription regression.
    """
    blocks = parse_example_blocks("vocab_terms")
    assert len(blocks) == 1, f"expected 1 vocab_terms list block, found {len(blocks)}"
    terms = blocks[0]["vocab_terms"]

    # Positive control, same test: the block must actually be a populated list
    # of the OTHER seeded terms. Without this, "Hypatia in terms" would pass
    # identically against a parser that returned some unrelated structure.
    assert isinstance(terms, list) and len(terms) > 1
    assert "VAC" in terms and "QuickBooks" in terms

    assert "Hypatia" in terms


def test_the_hypatia_pin_binds_to_structure_not_the_literal():
    """CONTROL for the pin above — proves the decoy cannot satisfy it.

    ``- "Hypatia"`` occurs twice in the file: the real list entry, and the
    prose explaining why the entry stays. Delete the real entry and a
    text-shaped check still passes on the prose. The structural pin must not.
    """
    src = EXAMPLE.read_text(encoding="utf-8")
    lines = src.splitlines(keepends=True)

    entry_lines = [i for i, ln in enumerate(lines) if _payload(ln).strip() == '- "Hypatia"']
    assert len(entry_lines) == 1, "expected exactly one real list entry"
    mutated = "".join(lines[: entry_lines[0]] + lines[entry_lines[0] + 1:])

    # The decoy survives the deletion — this is what makes a text pin vacuous.
    assert '- "Hypatia"' in mutated

    # The structural pin does not.
    terms = parse_example_blocks("vocab_terms", text=mutated)[0]["vocab_terms"]
    assert "Hypatia" not in terms
    assert "VAC" in terms  # the rest of the block is intact — a targeted mutation


# ---------------------------------------------------------------------------
# Premise pin — the claim the annotations rest on
# ---------------------------------------------------------------------------


def test_clinical_sections_are_unreachable_by_a_sovereign_config():
    """The annotations at the three no-instance-named "A CLINIC instance…"
    sites assert that STAY-C cannot carry those blocks AT ALL, because the
    sections are absent from the fail-closed allowlist.

    That is a claim about ``sovereign/boundary.py``, not about the example
    file. Pinned here so widening the allowlist reddens this test instead of
    silently turning three shipped comments into misinformation.

    Note the two mechanisms differ and the comments say so: ``telegram`` and
    ``brief_digest_push`` are ALSO catalogued in ``EGRESS_CONFIG_SECTIONS``;
    ``email_classifier`` is denied by the allowlist alone.
    """
    from alfred.sovereign.boundary import (
        EGRESS_CONFIG_SECTIONS,
        SOVEREIGN_ALLOWED_SECTIONS,
    )

    for section in ("telegram", "email_classifier", "brief_digest_push"):
        assert section not in SOVEREIGN_ALLOWED_SECTIONS

    # Positive control: the allowlist is populated and does admit the sections
    # a sovereign instance genuinely needs. Without this, the assertions above
    # pass identically against an empty/renamed frozenset.
    assert {"scribe", "vault", "sovereign"} <= SOVEREIGN_ALLOWED_SECTIONS

    assert "telegram" in EGRESS_CONFIG_SECTIONS
    assert "brief_digest_push" in EGRESS_CONFIG_SECTIONS
    # ...and the one the catalog does NOT carry, so the comment that says so
    # stays honest.
    assert "email_classifier" not in EGRESS_CONFIG_SECTIONS


# ---------------------------------------------------------------------------
# Deletion pin — ephemeral_config
# ---------------------------------------------------------------------------


def _conftest_fixture_names() -> set[str]:
    """Fixture names defined in tests/conftest.py, read from the AST.

    AST rather than grep: a name in a docstring or a comment is not a fixture,
    and this pin is about what pytest would actually register.
    """
    tree = ast.parse(CONFTEST.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            label = getattr(target, "attr", None) or getattr(target, "id", None)
            if label == "fixture":
                names.add(node.name)
    return names


def test_ephemeral_config_fixture_stays_deleted():
    """``ephemeral_config`` was removed 2026-08-21 — dead, and worse than dead.

    It ``yaml.safe_load``-ed ``config.yaml.example`` and was requested by
    nothing (one occurrence tree-wide: its own definition). The harm was not
    the dead code but that it READ as example-config coverage while being
    structurally blind to every guard in that file — they are all commented,
    and ``safe_load`` discards comments. A careful lane took it for coverage
    and shipped a wrong commit body on the strength of it.

    If a future test genuinely needs the example config, use
    ``parse_example_blocks`` above (comment-aware) or read the text directly —
    do not reintroduce a whole-file ``safe_load`` fixture, which cannot see
    what this module exists to protect.
    """
    fixtures = _conftest_fixture_names()

    # Positive control on the LIVE path: the same AST lookup finds a fixture
    # that IS defined and IS requested (904 call sites). Without this, the
    # assertion below would pass identically if the lookup were broken and
    # returned an empty set.
    assert "tmp_vault" in fixtures

    assert "ephemeral_config" not in fixtures
