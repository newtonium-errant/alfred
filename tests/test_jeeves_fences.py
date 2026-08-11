"""Structural pins for the Jeeves fences (task #81, stage 1).

Every assertion here checks a property of the SOURCE or of a type, not of a
behaviour, because these are the fences that a well-intentioned refactor
erases without any test going red. The design says "copy the scribe's
posture, not its code" and "non-cued audio dies with the ring"; these turn
both into something that can fail.
"""

from __future__ import annotations

import ast
import re
from dataclasses import fields
from pathlib import Path

import pytest

from alfred import jeeves
from alfred.jeeves import miss_store as miss_store_mod
from alfred.jeeves import ring as ring_mod
from alfred.jeeves import telemetry as telemetry_mod

JEEVES_DIR = Path(jeeves.__file__).parent


def _module_paths() -> list[Path]:
    return sorted(p for p in JEEVES_DIR.glob("*.py"))


def _imported_modules(path: Path) -> set[str]:
    """Every module name imported by ``path``, including inside functions.

    An AST walk rather than a text grep on purpose: a lazy import inside a
    function body is exactly how a forbidden dependency sneaks back in, and
    it is invisible to a naive scan of the import block at the top.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_jeeves_package_never_imports_the_clinical_scribe():
    """FENCE 5. Two ambient scribes on one codepath is how personal audio
    ends up in consent territory. The gate copies the scribe's POSTURE; it
    must share none of its code, at any import depth."""
    offenders: dict[str, set[str]] = {}
    for path in _module_paths():
        bad = {m for m in _imported_modules(path) if m.startswith("alfred.scribe")}
        if bad:
            offenders[path.name] = bad
    assert offenders == {}, (
        "alfred.jeeves must not import alfred.scribe (fence 5 — the clinical "
        f"scribe is a separate codepath by design). Offenders: {offenders}"
    )


def test_ring_module_has_no_filesystem_surface():
    """FENCE 2, structurally. A RingBuffer cannot write itself anywhere:
    the module imports no filesystem machinery, so 'non-cued audio is never
    persisted' is a property of the code rather than a promise about it."""
    source = (JEEVES_DIR / "ring.py").read_text(encoding="utf-8")
    imported = _imported_modules(JEEVES_DIR / "ring.py")

    assert not {m for m in imported if m.split(".")[0] in {"os", "pathlib", "shutil", "tempfile"}}, (
        f"ring.py must import no filesystem modules; got {sorted(imported)}"
    )
    for forbidden in ("open(", "Path(", "mkdir", "write_bytes", "write_text"):
        assert forbidden not in source, (
            f"ring.py contains {forbidden!r} — the ring must have no way to "
            "put audio on disk (fence 2)"
        )


def test_ring_buffer_exposes_no_path_or_persistence_method():
    """The same fence from the API side: no public method takes or returns a
    path, and none is named like a save."""
    names = [n for n in dir(ring_mod.RingBuffer) if not n.startswith("_")]
    for name in names:
        assert not any(
            token in name.lower()
            for token in ("save", "persist", "dump", "write", "flush", "path", "file")
        ), f"RingBuffer.{name} looks like a persistence surface (fence 2)"


def test_telemetry_row_carries_no_transcript_field():
    """FENCE 4. The telemetry row is content-free BY CONSTRUCTION — there is
    no field a transcript could be assigned to."""
    field_names = {f.name for f in fields(telemetry_mod.TelemetryRow)}
    for content_shaped in (
        "text", "transcript", "body", "content", "words", "utterance",
        "audio", "audio_b64", "speech",
    ):
        assert content_shaped not in field_names, (
            f"TelemetryRow.{content_shaped} would carry content into the one "
            "Jeeves artefact that is deliberately RETAINED (fence 4)"
        )


def test_the_miss_index_carries_no_transcript_field_either():
    """FENCE 4, EXTENDED TO THE SECOND RETAINED ARTEFACT (#98, ruling 1).

    Ruling 1 added a durable artefact beside the telemetry file: an index of
    retained miss-report windows. An index is exactly the shape that
    acquires a "just the transcript, for convenience" field six months
    later, at which point the package has a second, quieter copy of what was
    said — with the audio sitting next to it. Same pin, same reason.
    """
    field_names = {f.name for f in fields(miss_store_mod.MissArtifact)}
    for content_shaped in (
        "text", "transcript", "body", "content", "words", "utterance",
        "audio", "audio_b64", "speech", "matched_phrase",
    ):
        assert content_shaped not in field_names, (
            f"MissArtifact.{content_shaped} would carry content into the index "
            "that sits beside RETAINED garage audio (fence 4)"
        )


def test_the_miss_index_allowlist_matches_its_row_exactly():
    """DRIFT PIN, the telemetry writer's, for the index writer."""
    assert miss_store_mod.ALLOWED_FIELDS == {
        f.name for f in fields(miss_store_mod.MissArtifact)
    }


def test_only_ONE_module_can_put_bytes_on_disk():
    """FENCE 2 MADE STRUCTURAL FOR RULING 1's CARVE-OUT.

    ``tests/test_jeeves_miss_retention.py`` proves BEHAVIOURALLY that only
    the miss path retains audio. This proves it about the SOURCE: exactly
    one module in the package can open a file for binary writing at all, so
    a future capture path cannot quietly acquire the ability without this
    going red. The behavioural pin can only see the code paths a test
    happens to drive; this one sees the whole package.
    """
    offenders: dict[str, list[str]] = {}
    for path in _module_paths():
        source = path.read_text(encoding="utf-8")
        hits = [
            token for token in ('"wb"', "'wb'", "write_bytes", '"ab"', "'ab'")
            if token in source
        ]
        if hits:
            offenders[path.name] = hits
    assert offenders == {"miss_store.py": ['"wb"']}, (
        "exactly one module may write binary data to disk — the retained "
        f"miss-report window (#98 ruling 1). Got: {offenders}"
    )


def test_a_generated_artefact_id_fits_the_telemetry_string_ceiling():
    """The id rides in a telemetry row so morning review can join a row to
    an artefact. That only stays safe while it is SHORT and fixed-shape —
    a path in the same slot would be neither."""
    for _ in range(20):
        generated = miss_store_mod.new_artifact_id()
        assert miss_store_mod.ARTIFACT_ID_RE.match(generated), generated
        assert len(generated) < telemetry_mod.MAX_STRING_CHARS


def test_the_retention_telemetry_fields_cannot_carry_a_window():
    """The two fields ruling 1 added to the row are a BOOLEAN and a
    fixed-shape id. Neither can hold what was said or what was heard, which
    is the property that let fence 4 survive the carve-out."""
    annotations = {f.name: f.type for f in fields(telemetry_mod.TelemetryRow)}
    assert annotations["miss_audio_retained"] == "bool"
    assert "miss_audio_id" in annotations
    assert "miss_audio_path" not in annotations, (
        "a PATH in a telemetry row is the one string here that is long and "
        "environment-specific — paths live in the artefact index"
    )


def test_telemetry_allowlist_matches_the_row_exactly():
    """DRIFT PIN. The write-time allowlist and the dataclass must be the same
    set: a field added to one and not the other is either a silent drop or a
    field that bypasses validation."""
    assert telemetry_mod.ALLOWED_FIELDS == {
        f.name for f in fields(telemetry_mod.TelemetryRow)
    }


def test_no_module_hardcodes_an_instance_name():
    """PROMOTION-READY (ruling 7). Jeeves rides a peer for v1 and may become
    its own instance later, so no module may name one. The route target is
    config; there is no instance name in the package.

    WORD-BOUNDARY matched, not substring: 'vera' is inside 'several' and
    'kalle' inside nothing useful — a substring scan here fires on ordinary
    prose and gets disabled within a week, which is worse than not having it.
    """
    pattern = re.compile(
        r"\b(salem|kal[-_]?le|hypatia|vera)\b", re.IGNORECASE,
    )
    offenders: dict[str, list[str]] = {}
    for path in _module_paths():
        hits = sorted(set(pattern.findall(path.read_text(encoding="utf-8"))))
        if hits:
            offenders[path.name] = hits
    assert offenders == {}, (
        f"instance names are config, never code (ruling 7). Offenders: {offenders}"
    )


@pytest.mark.parametrize("module_name", [
    "audio", "config", "cues", "gate", "marklog", "miss_store", "ring",
    "service", "stt", "suspend", "telemetry", "transport_sink", "wake",
    "window",
])
def test_every_declared_module_exists(module_name: str):
    """The package ``__all__`` is a claim about what is here; keep it true."""
    assert module_name in jeeves.__all__
    assert (JEEVES_DIR / f"{module_name}.py").exists()
