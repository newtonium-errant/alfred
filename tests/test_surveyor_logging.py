"""Regression tests for surveyor logging + audit-log contracts.

Background: surveyor's ``setup_logging`` previously wired structlog through
``PrintLoggerFactory``, which wrote events directly to stdout. In daemon
mode the orchestrator redirects stdout to ``/dev/null`` (see
``alfred.orchestrator._silence_stdio``), so every ``writer.tags_updated`` /
``writer.tags_unchanged`` / ``daemon.*`` event was silently dropped — only
``httpx`` debug lines (which use stdlib logging) ever reached
``data/surveyor.log``. The fix routes structlog through stdlib logging so
the configured ``FileHandler`` actually receives the events.

A second, independent gap: ``VaultWriter`` never invoked the unified
audit-log helper, so ``data/vault_audit.log`` had zero surveyor entries
even when writes succeeded. The fix wires ``audit_log_path`` through the
writer and emits a ``modify`` JSONL line per persisted write.

These tests pin both contracts so a future refactor can't regress us back
into the silent-writer state.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import structlog

from alfred.surveyor.utils import setup_logging
from alfred.surveyor.writer import VaultWriter
from alfred.surveyor.state import PipelineState


@pytest.fixture(autouse=True)
def _reset_logging():
    """Each test gets a fresh stdlib + structlog config.

    ``setup_logging`` calls ``logging.basicConfig(force=True)`` which
    replaces handlers, but structlog itself is process-global with
    ``cache_logger_on_first_use=True``. The reset here is defensive — if a
    later test in the suite touches structlog config, prior tests' loggers
    won't leak captured handlers into this module.
    """
    yield
    # Reset structlog and stdlib logging so the next test starts clean.
    structlog.reset_defaults()
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)


def test_setup_logging_routes_structlog_events_to_log_file(tmp_path: Path):
    """The headline regression: structlog events must land in the log file.

    This is the exact contract the silent-writer bug violated. If this test
    starts failing, surveyor is back to writing tags invisibly.
    """
    log_file = tmp_path / "surveyor.log"
    setup_logging(level="INFO", log_file=str(log_file), suppress_stdout=True)

    log = structlog.get_logger()
    log.info("writer.tags_updated", path="person/Alice.md", tags=["a", "b"])
    log.info("writer.tags_unchanged", path="person/Bob.md", tag_count=3)

    # Force any buffered handlers to disk before reading.
    for h in logging.getLogger().handlers:
        h.flush()

    contents = log_file.read_text(encoding="utf-8")
    assert "writer.tags_updated" in contents, (
        f"writer.tags_updated event missing from log file. Contents:\n{contents}"
    )
    assert "writer.tags_unchanged" in contents
    assert "person/Alice.md" in contents
    assert "person/Bob.md" in contents


def test_writer_tags_updated_event_emitted_on_real_write(tmp_path: Path):
    """End-to-end: VaultWriter.write_alfred_tags must emit a structlog event
    that lands in the configured log file.

    This wires the actual writer code path through the actual logging
    config — covers a regression where someone refactors writer.py and
    accidentally drops the ``log.info`` call, or where setup_logging stops
    routing through stdlib (the original bug).
    """
    vault = tmp_path / "vault"
    (vault / "person").mkdir(parents=True)
    target = vault / "person" / "Alice.md"
    target.write_text(
        "---\ntype: person\nname: Alice\n---\n\nbody\n",
        encoding="utf-8",
    )

    log_file = tmp_path / "surveyor.log"
    setup_logging(level="INFO", log_file=str(log_file), suppress_stdout=True)

    state = PipelineState(tmp_path / "state.json")
    writer = VaultWriter(vault, state)
    writer.write_alfred_tags("person/Alice.md", ["alpha", "beta"])

    for h in logging.getLogger().handlers:
        h.flush()

    contents = log_file.read_text(encoding="utf-8")
    assert "writer.tags_updated" in contents
    assert "person/Alice.md" in contents


def test_writer_tags_unchanged_event_emitted_when_skipping(tmp_path: Path):
    """The skip-if-equal guard added in 7c1a452 must be observable.

    The whole point of that commit's ``writer.tags_unchanged`` event was to
    prove the dedup branch was firing instead of needlessly rewriting the
    file. If this test fails, the surveyor is back to a state where you
    can't tell whether a no-op skip happened or the writer never ran.
    """
    vault = tmp_path / "vault"
    (vault / "person").mkdir(parents=True)
    target = vault / "person" / "Alice.md"
    target.write_text(
        "---\ntype: person\nname: Alice\nalfred_tags:\n- alpha\n- beta\n---\n\nbody\n",
        encoding="utf-8",
    )

    log_file = tmp_path / "surveyor.log"
    setup_logging(level="INFO", log_file=str(log_file), suppress_stdout=True)

    state = PipelineState(tmp_path / "state.json")
    writer = VaultWriter(vault, state)
    # Re-propose the same tags in different order — should hit the
    # normalized-equal short-circuit and emit writer.tags_unchanged.
    writer.write_alfred_tags("person/Alice.md", ["beta", "alpha"])

    for h in logging.getLogger().handlers:
        h.flush()

    contents = log_file.read_text(encoding="utf-8")
    assert "writer.tags_unchanged" in contents
    assert "writer.tags_updated" not in contents


def test_writer_appends_to_audit_log_on_tag_write(tmp_path: Path):
    """Surveyor writes must show up in data/vault_audit.log.

    Curator/janitor/distiller all append to the unified audit log via
    ``alfred.vault.mutation_log.append_to_audit_log`` so drift
    investigations can attribute every vault mutation to a tool.
    Surveyor was the only writer that didn't, which made it impossible
    to tell who touched a file. This test pins the new behaviour: a
    successful tag write produces exactly one JSONL line tagged
    ``"tool": "surveyor"`` with ``"op": "modify"``.
    """
    vault = tmp_path / "vault"
    (vault / "person").mkdir(parents=True)
    target = vault / "person" / "Alice.md"
    target.write_text(
        "---\ntype: person\nname: Alice\n---\n\nbody\n",
        encoding="utf-8",
    )

    audit_log = tmp_path / "vault_audit.log"
    state = PipelineState(tmp_path / "state.json")
    writer = VaultWriter(vault, state, audit_log_path=audit_log)
    writer.write_alfred_tags("person/Alice.md", ["alpha", "beta"])

    assert audit_log.exists(), "vault_audit.log was not created"
    lines = [line for line in audit_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly 1 audit line, got {len(lines)}: {lines}"

    entry = json.loads(lines[0])
    assert entry["tool"] == "surveyor"
    assert entry["op"] == "modify"
    assert entry["path"] == "person/Alice.md"
    assert entry["detail"] == "alfred_tags"


def test_writer_appends_to_audit_log_on_relationship_write(tmp_path: Path):
    """Same contract for write_relationships — also lands in the audit log
    with a distinct ``detail`` so surveyor's two write types are
    distinguishable downstream.
    """
    vault = tmp_path / "vault"
    (vault / "person").mkdir(parents=True)
    target = vault / "person" / "Alice.md"
    target.write_text(
        "---\ntype: person\nname: Alice\n---\n\nbody\n",
        encoding="utf-8",
    )

    audit_log = tmp_path / "vault_audit.log"
    state = PipelineState(tmp_path / "state.json")
    writer = VaultWriter(vault, state, audit_log_path=audit_log)
    writer.write_relationships(
        "person/Alice.md",
        [{"target": "person/Bob", "type": "knows", "confidence": 0.8}],
    )

    assert audit_log.exists()
    lines = [line for line in audit_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["tool"] == "surveyor"
    assert entry["op"] == "modify"
    assert entry["detail"] == "relationships"


def test_writer_audit_log_skipped_when_tags_unchanged(tmp_path: Path):
    """The skip-if-equal short-circuit must NOT emit an audit-log line.

    The audit log is supposed to mirror real file mutations, not every
    labeling attempt. If a no-op skip wrote to the audit log, drift
    investigations would see phantom modifications and double-count the
    surveyor's churn.
    """
    vault = tmp_path / "vault"
    (vault / "person").mkdir(parents=True)
    target = vault / "person" / "Alice.md"
    target.write_text(
        "---\ntype: person\nname: Alice\nalfred_tags:\n- alpha\n- beta\n---\n\nbody\n",
        encoding="utf-8",
    )

    audit_log = tmp_path / "vault_audit.log"
    state = PipelineState(tmp_path / "state.json")
    writer = VaultWriter(vault, state, audit_log_path=audit_log)
    writer.write_alfred_tags("person/Alice.md", ["beta", "alpha"])

    # No file mutation happened, so no audit-log line should exist.
    assert not audit_log.exists() or audit_log.read_text(encoding="utf-8").strip() == ""


def test_writer_without_audit_log_path_still_writes(tmp_path: Path):
    """Backwards compat: a writer constructed without ``audit_log_path``
    must still successfully mutate the file — audit logging is opt-in.
    """
    vault = tmp_path / "vault"
    (vault / "person").mkdir(parents=True)
    target = vault / "person" / "Alice.md"
    target.write_text(
        "---\ntype: person\nname: Alice\n---\n\nbody\n",
        encoding="utf-8",
    )

    state = PipelineState(tmp_path / "state.json")
    writer = VaultWriter(vault, state)  # no audit_log_path
    writer.write_alfred_tags("person/Alice.md", ["x", "y"])

    import frontmatter
    after = frontmatter.loads(target.read_text(encoding="utf-8"))
    assert after.metadata["alfred_tags"] == ["x", "y"]
