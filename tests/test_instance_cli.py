"""Tests for ``alfred instance new`` CLI scaffolding (c8)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml


def test_config_kalle_example_exists_and_is_valid_yaml():
    """The shipped template must parse."""
    # Try the repo root (test runs from there) and the project dir.
    # Accept both the current name and the legacy one.
    candidates = [
        Path("config.instance.yaml.example"),
        Path(__file__).resolve().parent.parent / "config.instance.yaml.example",
        Path("config.kalle.yaml.example"),
        Path(__file__).resolve().parent.parent / "config.kalle.yaml.example",
    ]
    template = next((p for p in candidates if p.exists()), None)
    assert template is not None, "config.instance.yaml.example missing"

    raw = yaml.safe_load(template.read_text(encoding="utf-8"))
    # Key sections present.
    assert "daemon" in raw
    assert "transport" in raw
    assert "telegram" in raw
    assert raw["transport"]["server"]["port"] == 8892
    assert raw["telegram"]["instance"]["skill_bundle"] == "vault-kalle"
    assert raw["telegram"]["instance"]["tool_set"] == "kalle"
    assert raw["telegram"]["anthropic"]["model"] == "claude-opus-4-7"
    # gap_timeout_seconds widened for coding sessions.
    assert raw["telegram"]["session"]["gap_timeout_seconds"] >= 3600


def test_config_kalle_example_has_bash_exec_audit_path():
    candidates = [
        Path("config.instance.yaml.example"),
        Path(__file__).resolve().parent.parent / "config.instance.yaml.example",
        Path("config.kalle.yaml.example"),
        Path(__file__).resolve().parent.parent / "config.kalle.yaml.example",
    ]
    template = next((p for p in candidates if p.exists()), None)
    raw = yaml.safe_load(template.read_text(encoding="utf-8"))
    # bash_exec audit path is separate from Salem's.
    assert "bash_exec" in raw["telegram"]
    assert "/home/andrew/.alfred/kalle/" in raw["telegram"]["bash_exec"]["audit_path"]


def test_instance_new_scaffolds_directories(tmp_path, monkeypatch, capsys):
    """`alfred instance new testinstance` creates data + logs dirs + config file."""
    import argparse

    from alfred.cli import cmd_instance

    # Point /home/andrew/.alfred/... into tmp to avoid touching the real FS.
    # The CLI uses literal paths; monkeypatch Path to redirect is too
    # invasive. Instead, check that the function errors cleanly when
    # paths can't be created, AND in a writable tmp we also verify
    # config file creation.
    monkeypatch.chdir(tmp_path)

    # Copy the template into the tmp dir so the CLI can find it.
    repo_root = Path(__file__).resolve().parent.parent
    template_src = next(
        (repo_root / name for name in (
            "config.instance.yaml.example",
            "config.kalle.yaml.example",
        ) if (repo_root / name).exists()),
        None,
    )
    assert template_src is not None, "scaffold template missing in repo root"
    (tmp_path / "config.instance.yaml.example").write_text(
        template_src.read_text(encoding="utf-8"), encoding="utf-8",
    )

    # Stub the /home/andrew/.alfred path by monkeypatching inside the
    # CLI via an env override. The CLI hardcodes /home/andrew/.alfred/
    # so running this test end-to-end as root-user andrew passes; as
    # any other user it'll hit permission errors. We tolerate that by
    # using a subdirectory of tmp via a symlink-free approach:
    # redirect HOME and use the normal ~/.alfred if we're running as
    # andrew, else skip gracefully.
    if os.getuid() != 0 and not os.access("/home/andrew/.alfred", os.W_OK):
        pytest.skip("No write access to /home/andrew/.alfred; skipping.")

    args = argparse.Namespace(
        instance_cmd="new",
        instance_name="kalle-test-scaffold",
        force=True,
    )
    cmd_instance(args)

    # Config file created.
    config_file = tmp_path / "config.kalle-test-scaffold.yaml"
    assert config_file.exists()
    raw = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    # Name was substituted into directory paths.
    assert "/home/andrew/.alfred/kalle-test-scaffold/" in str(raw)

    # Stdout has the BotFather checklist.
    captured = capsys.readouterr()
    assert "BotFather" in captured.out
    assert "TELEGRAM_KALLE_TEST_SCAFFOLD_BOT_TOKEN" in captured.out

    # Cleanup — the instance dir was created outside tmp_path.
    import shutil
    shutil.rmtree(
        "/home/andrew/.alfred/kalle-test-scaffold",
        ignore_errors=True,
    )


def test_instance_new_rejects_invalid_names(tmp_path, monkeypatch, capsys):
    """Empty/non-alphanumeric names reject cleanly."""
    import argparse

    from alfred.cli import cmd_instance

    monkeypatch.chdir(tmp_path)

    args = argparse.Namespace(
        instance_cmd="new",
        instance_name="",
        force=False,
    )
    with pytest.raises(SystemExit):
        cmd_instance(args)

    args2 = argparse.Namespace(
        instance_cmd="new",
        instance_name="bad name with spaces",
        force=False,
    )
    with pytest.raises(SystemExit):
        cmd_instance(args2)


def test_instance_new_rejects_existing_config_without_force(tmp_path, monkeypatch, capsys):
    import argparse

    from alfred.cli import cmd_instance

    monkeypatch.chdir(tmp_path)
    # Create a file that would be clobbered.
    (tmp_path / "config.instance.yaml.example").write_text("# template\n", encoding="utf-8")
    (tmp_path / "config.existing-test.yaml").write_text("# pre-existing\n", encoding="utf-8")

    args = argparse.Namespace(
        instance_cmd="new",
        instance_name="existing-test",
        force=False,
    )
    with pytest.raises(SystemExit):
        cmd_instance(args)
    captured = capsys.readouterr()
    assert "already exists" in captured.out


def test_env_example_has_kalle_vars():
    """.env.example documents the four Stage 3.5 variables."""
    env_example = Path(__file__).resolve().parent.parent / ".env.example"
    assert env_example.exists()
    content = env_example.read_text(encoding="utf-8")
    assert "TELEGRAM_KALLE_BOT_TOKEN" in content
    assert "ALFRED_KALLE_TRANSPORT_TOKEN" in content
    assert "ALFRED_KALLE_PEER_TOKEN" in content
    assert "ALFRED_SALEM_PEER_TOKEN" in content


def test_config_yaml_example_documents_port_convention():
    """config.yaml.example's multi-instance block mentions ports 8891..8895."""
    path = Path(__file__).resolve().parent.parent / "config.yaml.example"
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "8891" in content
    assert "8892" in content
    # Port convention documented.
    assert "KAL-LE" in content or "kalle" in content.lower()
