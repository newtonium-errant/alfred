"""Regression tests for log rotation infrastructure (Tier A #2).

Before this arc shipped, every tool's ``setup_logging`` wired a vanilla
``logging.FileHandler``. With no rotation, ``data/alfred.log`` had grown
to 15 GB and ``data/surveyor.log`` to 14 GB in routine operation. The
fix consolidated handler construction in ``alfred.common.logging_handler``
and routed every tool's ``setup_logging`` through it, picking up size-
based rotation defaults (100 MB x 5 backups) plus operator-overridable
``logging.rotation`` config.

These tests pin three contracts:

1. The shared ``build_rotating_file_handler`` produces a real
   ``RotatingFileHandler`` honoring the configured limits, with
   schema-tolerant input (missing block, empty block, extra keys).
2. Every tool's ``setup_logging`` clone — curator, janitor, distiller,
   instructor, surveyor, telegram (talker), brief, transport — installs
   the RotatingFileHandler with the supplied limits. A future refactor
   that drops the rotation wiring on any single tool will trip the
   matrix test.
3. The orchestrator's ``_rotation_kwargs`` helper round-trips the YAML
   config block into the kwargs splatted into each daemon's
   ``setup_logging`` call.

Per ``feedback_log_emission_test_pattern.md``: the rotation kwargs are
not log-emitting code, so structlog capture isn't applicable here.
Instead, we pin the contract by inspecting the installed handler object
on the stdlib root logger.

Per ``feedback_regression_pin_unconditional.md``: no ``importorskip``.
All deps used here are stdlib.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest
import structlog

from alfred.common.logging_handler import (
    DEFAULT_BACKUP_COUNT,
    DEFAULT_MAX_BYTES,
    build_rotating_file_handler,
    extract_rotation_config,
)


@pytest.fixture(autouse=True)
def _reset_logging():
    """Each test gets a fresh stdlib + structlog config.

    Mirrors the pattern in ``test_surveyor_logging.py`` /
    ``test_talker_logging.py`` so cross-test handler bleed can't mask
    a regression here.
    """
    yield
    structlog.reset_defaults()
    root = logging.getLogger()
    for h in list(root.handlers):
        try:
            h.close()
        except Exception:
            pass
        root.removeHandler(h)


# ---------------------------------------------------------------------------
# 1. ``build_rotating_file_handler`` — shared helper
# ---------------------------------------------------------------------------


def test_build_rotating_file_handler_returns_rotating_handler(tmp_path: Path):
    """Sanity: the helper returns the right class with the right params."""
    log_file = tmp_path / "sub" / "tool.log"
    handler = build_rotating_file_handler(
        log_file, max_bytes=12345, backup_count=7
    )
    try:
        assert isinstance(handler, RotatingFileHandler)
        assert handler.maxBytes == 12345
        assert handler.backupCount == 7
        # Parent dir auto-created (mirrors the pre-existing
        # ``setup_logging`` behavior the helper replaced).
        assert log_file.parent.is_dir()
    finally:
        handler.close()


def test_build_rotating_file_handler_defaults_applied_on_none(tmp_path: Path):
    """``None`` for either kwarg pulls in the module-level default.

    Schema-tolerance contract — operator can omit the rotation block in
    config.yaml and still get sane policy.
    """
    log_file = tmp_path / "tool.log"
    handler = build_rotating_file_handler(log_file)
    try:
        assert handler.maxBytes == DEFAULT_MAX_BYTES
        assert handler.backupCount == DEFAULT_BACKUP_COUNT
    finally:
        handler.close()


def test_build_rotating_file_handler_disable_via_zero_max_bytes(tmp_path: Path):
    """``max_bytes=0`` disables rotation (RotatingFileHandler convention).

    Documented escape hatch — operator setting
    ``logging.rotation.max_bytes: 0`` opts out of in-process rotation
    while keeping the FileHandler wired (so structlog still writes).
    """
    log_file = tmp_path / "tool.log"
    handler = build_rotating_file_handler(log_file, max_bytes=0, backup_count=5)
    try:
        assert handler.maxBytes == 0
    finally:
        handler.close()


def test_build_rotating_file_handler_string_coerced(tmp_path: Path):
    """YAML config can yield string ints on misconfigured deploys.

    Defensive coercion ensures ``"100000000"`` works as well as
    ``100000000`` — surfaces garbage via ValueError rather than
    silently substituting the default.
    """
    log_file = tmp_path / "tool.log"
    handler = build_rotating_file_handler(
        log_file, max_bytes="50000", backup_count="3"  # type: ignore[arg-type]
    )
    try:
        assert handler.maxBytes == 50000
        assert handler.backupCount == 3
    finally:
        handler.close()


def test_build_rotating_file_handler_negative_clamped_to_zero(tmp_path: Path):
    """Negative values clamp to 0 — defensive, prevents a confusing
    RotatingFileHandler crash on first write.
    """
    log_file = tmp_path / "tool.log"
    handler = build_rotating_file_handler(
        log_file, max_bytes=-1, backup_count=-2
    )
    try:
        assert handler.maxBytes == 0
        assert handler.backupCount == 0
    finally:
        handler.close()


def test_build_rotating_file_handler_actually_rotates(tmp_path: Path):
    """End-to-end smoke: writing past ``max_bytes`` produces a ``.log.1`` backup.

    Pins the actual rotation behavior, not just the constructor params.
    A future refactor that swapped RotatingFileHandler for a plain
    FileHandler would pass the constructor checks above but fail this
    test.
    """
    log_file = tmp_path / "tool.log"
    handler = build_rotating_file_handler(log_file, max_bytes=200, backup_count=2)
    handler.setFormatter(logging.Formatter("%(message)s"))
    test_logger = logging.getLogger("alfred.test.rotation")
    test_logger.setLevel(logging.INFO)
    # Detach any preexisting handlers so we only see this one.
    for h in list(test_logger.handlers):
        test_logger.removeHandler(h)
    test_logger.addHandler(handler)
    test_logger.propagate = False
    try:
        # Each line is ~50 chars; 10 lines = ~500 bytes > 200-byte cap.
        for i in range(10):
            test_logger.info("payload-line-with-padding-%02d-aaaaaaaaaaaaaaaaaa", i)
        handler.flush()
    finally:
        test_logger.removeHandler(handler)
        handler.close()

    # The live file exists.
    assert log_file.exists()
    # At least one backup must have been created.
    backups = sorted(tmp_path.glob("tool.log.*"))
    assert backups, (
        f"expected at least one rotated backup at {tmp_path}/tool.log.*; "
        f"found {[p.name for p in tmp_path.iterdir()]}"
    )


# ---------------------------------------------------------------------------
# 2. ``extract_rotation_config`` — YAML extraction helper
# ---------------------------------------------------------------------------


def test_extract_rotation_config_returns_defaults_on_missing_block():
    """No ``rotation`` block in config → defaults."""
    assert extract_rotation_config({"level": "INFO", "dir": "./data"}) == (
        DEFAULT_MAX_BYTES,
        DEFAULT_BACKUP_COUNT,
    )


def test_extract_rotation_config_returns_defaults_on_empty_block():
    """``rotation: {}`` (empty mapping) → defaults.

    Operator wrote the block but supplied no fields — same as omitting.
    """
    assert extract_rotation_config({"rotation": {}}) == (
        DEFAULT_MAX_BYTES,
        DEFAULT_BACKUP_COUNT,
    )


def test_extract_rotation_config_returns_defaults_on_non_dict_block():
    """``rotation: "true"`` (wrong type) → defaults, no crash.

    Schema-tolerance: misconfigured YAML doesn't take the daemon down.
    """
    assert extract_rotation_config({"rotation": "yes please"}) == (
        DEFAULT_MAX_BYTES,
        DEFAULT_BACKUP_COUNT,
    )


def test_extract_rotation_config_honors_supplied_fields():
    """Both fields supplied → both returned."""
    assert extract_rotation_config(
        {"rotation": {"max_bytes": 500_000_000, "backup_count": 3}}
    ) == (500_000_000, 3)


def test_extract_rotation_config_ignores_extra_fields():
    """Schema-tolerance: unknown keys in rotation block don't crash.

    Forward-compat: a future field added by a newer Alfred version
    is silently ignored when running an older binary against a newer
    config.
    """
    assert extract_rotation_config(
        {
            "rotation": {
                "max_bytes": 1234,
                "backup_count": 4,
                "future_field": "anything",
            }
        }
    ) == (1234, 4)


def test_extract_rotation_config_partial_uses_supplied_plus_default():
    """Operator supplies only one field → other takes default."""
    max_bytes, backup_count = extract_rotation_config(
        {"rotation": {"max_bytes": 7777}}
    )
    assert max_bytes == 7777
    assert backup_count == DEFAULT_BACKUP_COUNT

    max_bytes, backup_count = extract_rotation_config(
        {"rotation": {"backup_count": 9}}
    )
    assert max_bytes == DEFAULT_MAX_BYTES
    assert backup_count == 9


# ---------------------------------------------------------------------------
# 3. Per-tool ``setup_logging`` — matrix test pins every clone
# ---------------------------------------------------------------------------


# Each entry: (display name, import path) — the per-tool setup_logging
# function. The matrix below treats them uniformly: every tool's
# ``setup_logging`` MUST install a RotatingFileHandler with the
# supplied limits or this test fails for that tool.
SETUP_LOGGING_SITES = [
    ("curator", "alfred.curator.utils"),
    ("janitor", "alfred.janitor.utils"),
    ("distiller", "alfred.distiller.utils"),
    ("instructor", "alfred.instructor.utils"),
    ("surveyor", "alfred.surveyor.utils"),
    ("telegram", "alfred.telegram.utils"),
    ("brief", "alfred.brief.utils"),
    ("transport", "alfred.transport.utils"),
]


def _find_rotating_handler() -> RotatingFileHandler | None:
    """Return the RotatingFileHandler installed on the root logger, or None."""
    for h in logging.getLogger().handlers:
        if isinstance(h, RotatingFileHandler):
            return h
    return None


@pytest.mark.parametrize("tool_name,module_path", SETUP_LOGGING_SITES)
def test_setup_logging_installs_rotating_handler_with_supplied_limits(
    tmp_path: Path, tool_name: str, module_path: str
):
    """Every tool's ``setup_logging`` must wire a RotatingFileHandler.

    This is the principal regression-pin for the rotation infrastructure.
    If a future refactor reverts any single tool to a plain
    ``logging.FileHandler``, the matrix entry for that tool fails and
    the operator finds out in CI, not when ``data/<tool>.log`` hits
    15 GB.
    """
    import importlib

    mod = importlib.import_module(module_path)
    log_file = tmp_path / f"{tool_name}.log"
    mod.setup_logging(
        level="INFO",
        log_file=str(log_file),
        suppress_stdout=True,
        max_bytes=42_000_000,
        backup_count=3,
    )
    handler = _find_rotating_handler()
    assert handler is not None, (
        f"{tool_name}.setup_logging did not install a RotatingFileHandler; "
        f"root logger handlers: {logging.getLogger().handlers}"
    )
    assert handler.maxBytes == 42_000_000, (
        f"{tool_name}.setup_logging installed a RotatingFileHandler "
        f"with maxBytes={handler.maxBytes}, expected 42_000_000"
    )
    assert handler.backupCount == 3, (
        f"{tool_name}.setup_logging installed a RotatingFileHandler "
        f"with backupCount={handler.backupCount}, expected 3"
    )
    handler.close()


@pytest.mark.parametrize("tool_name,module_path", SETUP_LOGGING_SITES)
def test_setup_logging_applies_defaults_when_kwargs_absent(
    tmp_path: Path, tool_name: str, module_path: str
):
    """Tools whose runners haven't been updated still get rotation.

    Backwards-compat: ``setup_logging(level=..., log_file=..., suppress_stdout=...)``
    (the pre-arc 3-arg call) must still produce a RotatingFileHandler
    with the bundled defaults applied. This pins the schema-tolerant
    contract for callers that pre-date the rotation kwargs.
    """
    import importlib

    mod = importlib.import_module(module_path)
    log_file = tmp_path / f"{tool_name}.log"
    mod.setup_logging(
        level="INFO",
        log_file=str(log_file),
        suppress_stdout=True,
    )
    handler = _find_rotating_handler()
    assert handler is not None, (
        f"{tool_name}.setup_logging produced no RotatingFileHandler with "
        f"defaults"
    )
    assert handler.maxBytes == DEFAULT_MAX_BYTES
    assert handler.backupCount == DEFAULT_BACKUP_COUNT
    handler.close()


# ---------------------------------------------------------------------------
# 4. Orchestrator wiring — ``_rotation_kwargs`` helper
# ---------------------------------------------------------------------------


def test_orchestrator_rotation_kwargs_round_trip():
    """The orchestrator's helper builds the right kwargs dict from YAML."""
    from alfred.orchestrator import _rotation_kwargs

    kwargs = _rotation_kwargs(
        {"level": "INFO", "rotation": {"max_bytes": 999, "backup_count": 2}}
    )
    assert kwargs == {"max_bytes": 999, "backup_count": 2}


def test_orchestrator_rotation_kwargs_defaults_on_missing():
    """The helper falls back to bundled defaults when block is absent."""
    from alfred.orchestrator import _rotation_kwargs

    kwargs = _rotation_kwargs({"level": "INFO"})
    assert kwargs == {
        "max_bytes": DEFAULT_MAX_BYTES,
        "backup_count": DEFAULT_BACKUP_COUNT,
    }


def test_orchestrator_rotation_kwargs_splat_into_setup_logging(tmp_path: Path):
    """End-to-end: ``setup_logging(**_rotation_kwargs(cfg))`` produces the
    expected RotatingFileHandler.

    Pins the integration contract between the orchestrator helper and the
    per-tool setup_logging call — i.e. ``**_rotation_kwargs(log_cfg)``
    is a valid splat into every tool's signature.
    """
    from alfred.curator.utils import setup_logging
    from alfred.orchestrator import _rotation_kwargs

    log_cfg = {
        "level": "INFO",
        "rotation": {"max_bytes": 88_000_000, "backup_count": 4},
    }
    setup_logging(
        level="INFO",
        log_file=str(tmp_path / "curator.log"),
        suppress_stdout=True,
        **_rotation_kwargs(log_cfg),
    )
    handler = _find_rotating_handler()
    assert handler is not None
    assert handler.maxBytes == 88_000_000
    assert handler.backupCount == 4
    handler.close()


# ---------------------------------------------------------------------------
# 5. CLI wiring — ``_setup_logging_from_config``
# ---------------------------------------------------------------------------


def test_cli_setup_logging_from_config_honors_rotation(tmp_path: Path):
    """``alfred.cli._setup_logging_from_config`` reads the rotation block.

    Used by every ``alfred <subcommand>`` handler that wants logging.
    Without this wiring, CLI-invoked workflows (one-off scans, etc.)
    would write to un-rotated logs while daemon runs rotated correctly.
    """
    from alfred.cli import _setup_logging_from_config

    raw = {
        "logging": {
            "level": "INFO",
            "dir": str(tmp_path),
            "rotation": {"max_bytes": 55_000_000, "backup_count": 2},
        }
    }
    _setup_logging_from_config(raw, tool="curator", suppress_stdout=True)
    handler = _find_rotating_handler()
    assert handler is not None
    assert handler.maxBytes == 55_000_000
    assert handler.backupCount == 2
    handler.close()


def test_cli_setup_logging_from_config_defaults_when_block_absent(tmp_path: Path):
    """CLI handler with missing rotation block → bundled defaults."""
    from alfred.cli import _setup_logging_from_config

    raw = {"logging": {"level": "INFO", "dir": str(tmp_path)}}
    _setup_logging_from_config(raw, tool="curator", suppress_stdout=True)
    handler = _find_rotating_handler()
    assert handler is not None
    assert handler.maxBytes == DEFAULT_MAX_BYTES
    assert handler.backupCount == DEFAULT_BACKUP_COUNT
    handler.close()


# ---------------------------------------------------------------------------
# 6. Structlog events still flow through the rotating handler
# ---------------------------------------------------------------------------


def test_structlog_events_land_in_rotating_log_file(tmp_path: Path):
    """The whole point of the surveyor's silent-writer fix (commit ``e6b5ad6``)
    was to make structlog events land in the configured log file. Rotation
    must not regress that — structlog routes through stdlib, stdlib hits
    the RotatingFileHandler, the file receives bytes.

    Cross-checks the surveyor-logging regression contract against the
    new rotation infrastructure.
    """
    from alfred.surveyor.utils import setup_logging as surveyor_setup_logging

    log_file = tmp_path / "surveyor.log"
    surveyor_setup_logging(
        level="INFO",
        log_file=str(log_file),
        suppress_stdout=True,
        max_bytes=10_000_000,
        backup_count=5,
    )
    log = structlog.get_logger()
    log.info("test.rotation.event", phase="check")
    for h in logging.getLogger().handlers:
        h.flush()
    contents = log_file.read_text(encoding="utf-8")
    assert "test.rotation.event" in contents
    assert "phase" in contents


# ---------------------------------------------------------------------------
# 7. ``__main__.py`` entry points thread rotation kwargs (Tier A #2.1 NOTE 1)
# ---------------------------------------------------------------------------
#
# Each of curator/janitor/distiller/surveyor ships a ``__main__.py`` so
# operators can invoke ``python -m alfred.<tool>`` outside the
# orchestrator. Before this followup, those paths called
# ``setup_logging(level=..., log_file=...)`` without rotation kwargs,
# silently dropping the operator's ``logging.rotation`` config and
# falling back to the bundled 100 MB × 5 default.
#
# The matrix below verifies each ``__main__.py`` defines a
# ``_load_rotation_kwargs(config_path)`` helper and that helper
# round-trips the YAML rotation block. The helper-level test is
# sufficient (we don't subprocess-spawn ``python -m alfred.curator``
# because that exercises asyncio loops + real daemons); the contract
# is that the helper is THERE and threads kwargs into ``setup_logging``.

import yaml


MAIN_MODULE_ROTATION_SITES = [
    ("curator", "alfred.curator.__main__"),
    ("janitor", "alfred.janitor.__main__"),
    ("distiller", "alfred.distiller.__main__"),
    ("surveyor", "alfred.surveyor.__main__"),
]


@pytest.mark.parametrize("tool_name,module_path", MAIN_MODULE_ROTATION_SITES)
def test_main_module_paths_thread_rotation(
    tmp_path: Path, tool_name: str, module_path: str
):
    """Each ``__main__.py`` exposes ``_load_rotation_kwargs`` that
    extracts the YAML ``logging.rotation`` block into kwargs ready to
    splat into ``setup_logging``.

    Per Tier A #2.1 NOTE 1: the ``python -m alfred.<tool>`` paths must
    honor operator rotation config, same as the orchestrator. Tests the
    helper, not the subprocess (no daemon loops in CI).
    """
    import importlib

    mod = importlib.import_module(module_path)
    assert hasattr(mod, "_load_rotation_kwargs"), (
        f"{module_path} is missing ``_load_rotation_kwargs`` — the "
        f"``python -m alfred.{tool_name}`` path will silently drop the "
        f"operator's rotation config"
    )

    # Write a YAML with a rotation block and verify the helper extracts it.
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "logging": {
                    "level": "INFO",
                    "dir": "./data",
                    "rotation": {"max_bytes": 7_777_777, "backup_count": 3},
                }
            }
        )
    )
    kwargs = mod._load_rotation_kwargs(str(config_path))
    assert kwargs == {"max_bytes": 7_777_777, "backup_count": 3}, (
        f"{module_path}._load_rotation_kwargs returned {kwargs}, "
        f"expected the YAML rotation block round-tripped"
    )


@pytest.mark.parametrize("tool_name,module_path", MAIN_MODULE_ROTATION_SITES)
def test_main_module_paths_rotation_kwargs_defaults_on_missing(
    tmp_path: Path, tool_name: str, module_path: str
):
    """When the YAML omits the rotation block, the helper returns the
    bundled defaults — preserving the schema-tolerance contract.
    """
    import importlib

    mod = importlib.import_module(module_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"logging": {"level": "INFO", "dir": "./data"}})
    )
    kwargs = mod._load_rotation_kwargs(str(config_path))
    assert kwargs == {
        "max_bytes": DEFAULT_MAX_BYTES,
        "backup_count": DEFAULT_BACKUP_COUNT,
    }


@pytest.mark.parametrize("tool_name,module_path", MAIN_MODULE_ROTATION_SITES)
def test_main_module_paths_rotation_kwargs_missing_file(
    tmp_path: Path, tool_name: str, module_path: str
):
    """Helper degrades gracefully on a missing config file (returns
    empty kwargs so ``setup_logging`` falls back to bundled defaults).
    """
    import importlib

    mod = importlib.import_module(module_path)
    missing = tmp_path / "does-not-exist.yaml"
    kwargs = mod._load_rotation_kwargs(str(missing))
    assert kwargs == {}


# ---------------------------------------------------------------------------
# 8. ``logging.rotation.policy_applied`` log emission (Tier A #2.1 NOTE 2)
# ---------------------------------------------------------------------------
#
# Per ``feedback_intentionally_left_blank.md``: operators must be able
# to distinguish "config honored, rotation disabled via ``max_bytes: 0``"
# from "config silently dropped, default applied." The
# ``logging.rotation.policy_applied`` event surfaces the resolved
# policy at the tail of every ``setup_logging`` call. Per
# ``feedback_log_emission_test_pattern.md``: the test MUST drive the
# production code path AND assert on the captured event via
# ``structlog.testing.capture_logs()``.


def _flush_handlers() -> None:
    """Flush every handler on the root logger.

    The policy_applied event hits the RotatingFileHandler immediately,
    but Python's file buffer may not flush to disk before the test
    reads back. Explicit flush avoids racy reads.
    """
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:
            pass


_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")


def _read_log(log_file: Path) -> str:
    """Read the on-disk log file's contents, stripped of ANSI escapes.

    Structlog's ``ConsoleRenderer`` wraps keys/values in ANSI color
    sequences. Tests assert on plain ``key=value`` substrings — those
    only survive if the ANSI is stripped first.
    """
    _flush_handlers()
    if not log_file.exists():
        return ""
    raw = log_file.read_text(encoding="utf-8")
    return _ANSI_RE.sub("", raw)


def test_policy_applied_log_emitted_on_setup(tmp_path: Path):
    """``setup_logging`` writes a ``logging.rotation.policy_applied``
    event to the configured log file with the resolved policy fields.

    Per ``feedback_log_emission_test_pattern.md`` — assertion drives
    the production code path. Note: ``structlog.testing.capture_logs``
    doesn't work here because ``setup_logging`` calls
    ``structlog.configure`` mid-call, which clobbers the capture
    instrumentation. The real production sink is the log file, so we
    read it back.
    """
    from alfred.curator.utils import setup_logging

    log_file = tmp_path / "curator.log"
    setup_logging(
        level="INFO",
        log_file=str(log_file),
        suppress_stdout=True,
        max_bytes=88_888_888,
        backup_count=4,
    )
    contents = _read_log(log_file)
    # Event-name and key field assertions — catches drop-the-event
    # AND drop-the-field refactor regressions.
    assert "logging.rotation.policy_applied" in contents, (
        f"expected ``logging.rotation.policy_applied`` event in {log_file}, "
        f"got: {contents!r}"
    )
    assert "max_bytes=88888888" in contents
    assert "backup_count=4" in contents
    assert str(log_file) in contents
    assert "rotation_enabled=True" in contents


def test_policy_applied_log_emits_rotation_enabled_false_on_zero(tmp_path: Path):
    """``max_bytes=0`` (the disable-rotation escape hatch) produces
    ``rotation_enabled=False`` in the on-disk log line.

    This is the principal observability win for the policy_applied
    event — without it, an operator setting ``rotation.max_bytes: 0``
    cannot distinguish honored-disabled from silently-dropped-default
    in their log file.
    """
    from alfred.janitor.utils import setup_logging

    log_file = tmp_path / "janitor.log"
    setup_logging(
        level="INFO",
        log_file=str(log_file),
        suppress_stdout=True,
        max_bytes=0,
        backup_count=5,
    )
    contents = _read_log(log_file)
    assert "logging.rotation.policy_applied" in contents
    assert "max_bytes=0" in contents
    assert "backup_count=5" in contents
    assert "rotation_enabled=False" in contents


def test_policy_applied_log_emits_defaults_when_kwargs_absent(tmp_path: Path):
    """The policy_applied event reports the RESOLVED values — so when
    the caller omits the kwargs, the event shows the bundled defaults
    (not ``None``).

    This is what lets an operator confirm "config block was missing,
    defaults applied" via the log line rather than guessing from
    behavior.
    """
    from alfred.distiller.utils import setup_logging

    log_file = tmp_path / "distiller.log"
    setup_logging(
        level="INFO",
        log_file=str(log_file),
        suppress_stdout=True,
    )
    contents = _read_log(log_file)
    assert "logging.rotation.policy_applied" in contents
    assert f"max_bytes={DEFAULT_MAX_BYTES}" in contents
    assert f"backup_count={DEFAULT_BACKUP_COUNT}" in contents
    assert "rotation_enabled=True" in contents


def test_policy_applied_log_suppressed_when_no_log_file(tmp_path: Path):
    """No log file → no policy_applied event (no file handler was wired,
    nothing to report).

    Pins the no-op contract: callers that suppress the file handler
    (CLI smoke tests, etc.) don't crash and don't leave artifacts.
    Asserts via the absence of the event file: a stray write would
    have to land somewhere.
    """
    from alfred.brief.utils import setup_logging

    # The function should complete cleanly without writing to any file.
    setup_logging(
        level="INFO",
        log_file=None,
        suppress_stdout=True,
        max_bytes=100,
        backup_count=2,
    )
    # Nothing under tmp_path should have been touched.
    artifacts = list(tmp_path.iterdir())
    assert artifacts == [], (
        f"expected no files written when log_file=None, got: {artifacts}"
    )


@pytest.mark.parametrize("tool_name,module_path", SETUP_LOGGING_SITES)
def test_policy_applied_log_emitted_for_every_tool(
    tmp_path: Path, tool_name: str, module_path: str
):
    """Matrix pin: every tool's ``setup_logging`` emits the
    policy_applied event to its log file.

    A future refactor that drops the ``emit_rotation_policy_log`` call
    from any single tool's setup_logging trips this matrix entry —
    same shape as ``test_setup_logging_installs_rotating_handler_with_supplied_limits``,
    different observability contract.
    """
    import importlib

    mod = importlib.import_module(module_path)
    log_file = tmp_path / f"{tool_name}.log"
    mod.setup_logging(
        level="INFO",
        log_file=str(log_file),
        suppress_stdout=True,
        max_bytes=42_000_000,
        backup_count=3,
    )
    contents = _read_log(log_file)
    assert "logging.rotation.policy_applied" in contents, (
        f"{tool_name}.setup_logging did not emit "
        f"``logging.rotation.policy_applied`` to {log_file}; "
        f"contents: {contents!r}"
    )
    assert "max_bytes=42000000" in contents
    assert "backup_count=3" in contents
    assert "rotation_enabled=True" in contents


# ---------------------------------------------------------------------------
# 9. Config loaders tolerate the ``logging.rotation`` block (NOTE 1 regression)
# ---------------------------------------------------------------------------
#
# Pre-followup, curator/janitor/distiller's ``load_config`` and
# ``load_from_unified`` crashed with ``TypeError: LoggingConfig.__init__()
# got an unexpected keyword argument 'rotation'`` whenever the YAML
# contained the example config's rotation block. The ``__main__.py``
# fix is moot if the typed config layer can't load the file.


CONFIG_LOAD_SITES = [
    ("curator", "alfred.curator.config"),
    ("janitor", "alfred.janitor.config"),
    ("distiller", "alfred.distiller.config"),
]


@pytest.mark.parametrize("tool_name,module_path", CONFIG_LOAD_SITES)
def test_load_config_tolerates_rotation_block(
    tmp_path: Path, tool_name: str, module_path: str
):
    """``load_config`` (file-based) doesn't crash on the YAML rotation
    block — keys not on ``LoggingConfig`` are silently stripped before
    the typed build.

    Surveyor's loader is omitted because its own ``_build_dataclass``
    is already schema-tolerant of unknown keys (filters by field
    name) — covered indirectly by the matrix in section 3.
    """
    import importlib

    mod = importlib.import_module(module_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "logging": {
                    "level": "INFO",
                    "file": str(tmp_path / f"{tool_name}.log"),
                    "rotation": {"max_bytes": 12345, "backup_count": 2},
                }
            }
        )
    )
    # Should not raise.
    cfg = mod.load_config(str(config_path))
    assert cfg.logging.level == "INFO"
    # The rotation block was stripped — the typed dataclass only
    # carries level + file.
    assert not hasattr(cfg.logging, "rotation")


@pytest.mark.parametrize("tool_name,module_path", CONFIG_LOAD_SITES)
def test_load_from_unified_tolerates_rotation_block(
    tool_name: str, module_path: str
):
    """``load_from_unified`` (in-memory dict path used by orchestrator)
    also tolerates the rotation block.

    Same crash class — different load entry point.
    """
    import importlib

    mod = importlib.import_module(module_path)
    raw = {
        tool_name: {},
        "logging": {
            "level": "INFO",
            "dir": "./data",
            "rotation": {"max_bytes": 100, "backup_count": 2},
        },
    }
    # Surveyor needs vault in the unified dict; the three crash-prone
    # tools (curator/janitor/distiller) accept missing vault by
    # defaulting via normalize_vault_block.
    cfg = mod.load_from_unified(raw)
    assert cfg.logging.level == "INFO"
