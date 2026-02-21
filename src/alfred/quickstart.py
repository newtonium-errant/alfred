"""Interactive quickstart wizard — scaffolds vault + writes config."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml


def _get_scaffold_dir() -> Path:
    """Return path to bundled scaffold/."""
    from alfred._data import get_scaffold_dir
    return get_scaffold_dir()


ENTITY_DIRS = [
    "person", "project", "org", "location", "process",
    "inbox", "inbox/processed",
    "account", "asset", "conversation", "note",
    "decision", "assumption", "constraint", "contradiction", "synthesis",
    "event", "task", "session", "run", "input", "thread", "view",
]


def _prompt(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{question}{suffix}: ").strip()
    return answer or default


def _prompt_choice(question: str, choices: list[str], default: int = 1) -> int:
    print(f"\n{question}")
    for i, choice in enumerate(choices, 1):
        marker = " *" if i == default else ""
        print(f"  [{i}] {choice}{marker}")
    while True:
        raw = input(f"Choice [{default}]: ").strip()
        if not raw:
            return default
        try:
            val = int(raw)
            if 1 <= val <= len(choices):
                return val
        except ValueError:
            pass
        print(f"Please enter 1-{len(choices)}")


def _check_command(cmd: str) -> bool:
    """Check if a command is on PATH."""
    return shutil.which(cmd) is not None


def _check_ollama() -> bool:
    """Check if Ollama is reachable."""
    try:
        import httpx
        resp = httpx.get("http://localhost:11434/api/tags", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def run_quickstart() -> None:
    print("=" * 50)
    print("  Alfred Quickstart")
    print("=" * 50)

    # 1. Vault path
    vault_path = _prompt("\nWhere should your vault live?", "./vault")
    vault_path = os.path.abspath(vault_path)

    # 2. Scaffold
    scaffold_dir = _get_scaffold_dir()
    if not scaffold_dir.exists():
        print(f"\nWarning: scaffold directory not found at {scaffold_dir}")
        print("Skipping scaffold copy.")
        do_scaffold = False
    else:
        do_scaffold = _prompt("\nCopy vault scaffold (templates, bases, config)?", "y").lower() in ("y", "yes")

    # 3. Agent backend
    backend_idx = _prompt_choice(
        "Which agent backend?",
        ["Claude Code (requires `claude` on PATH)", "Zo Computer (HTTP API)", "OpenClaw (requires `openclaw` on PATH)"],
        default=1,
    )
    backend_map = {1: "claude", 2: "zo", 3: "openclaw"}
    backend = backend_map[backend_idx]

    # 4. Validate backend
    zo_api_key = ""
    if backend == "claude":
        if _check_command("claude"):
            print("  [OK] `claude` found on PATH")
        else:
            print("  [!!] `claude` not found on PATH -- install Claude Code first")

    elif backend == "zo":
        zo_api_key = _prompt("  Enter your ZO_API_KEY")
        if not zo_api_key:
            print("  [!!] No API key provided -- you'll need to set ZO_API_KEY in .env")

    elif backend == "openclaw":
        if _check_command("openclaw"):
            print("  [OK] `openclaw` found on PATH")
        else:
            print("  [!!] `openclaw` not found on PATH")

    # 5. Surveyor
    enable_surveyor = _prompt("\nEnable surveyor? (requires Ollama + OpenRouter) [y/N]", "n").lower() in ("y", "yes")
    openrouter_api_key = ""

    if enable_surveyor:
        # Install surveyor dependencies
        print("\n  Installing surveyor dependencies...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "alfred-vault[all]"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print("  [OK] Surveyor dependencies installed")
        else:
            print("  [!!] Failed to install surveyor dependencies:")
            print(f"       {result.stderr.strip()[:200]}")
            print("       Run manually: pip install alfred-vault[all]")

        if _check_ollama():
            print("  [OK] Ollama is running")
        else:
            print("  [!!] Ollama not reachable at localhost:11434")
            print("       Install Ollama and run: ollama pull nomic-embed-text")

        openrouter_api_key = _prompt("  Enter your OPENROUTER_API_KEY")
        if not openrouter_api_key:
            print("  [!!] No API key -- you'll need to set OPENROUTER_API_KEY in .env")

    # 6. Write files
    print(f"\n--- Setting up ---")

    # Create vault directory and entity dirs
    vault = Path(vault_path)
    vault.mkdir(parents=True, exist_ok=True)
    for d in ENTITY_DIRS:
        (vault / d).mkdir(parents=True, exist_ok=True)
    print(f"  Created vault directory: {vault_path}")

    # Copy scaffold — copies the entire scaffold tree (dirs, templates, bases, config)
    if do_scaffold and scaffold_dir.exists():
        for item in scaffold_dir.iterdir():
            src = item
            dst = vault / item.name
            if src.is_dir():
                if dst.exists():
                    # Merge: copy contents without wiping existing files
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)

        print(f"  Copied scaffold (entity dirs, templates, bases, .obsidian config)")

    # Build config.yaml
    config = {
        "vault": {
            "path": vault_path,
            "ignore_dirs": ["_templates", "_bases", "_docs", ".obsidian", "view"],
            "ignore_files": [".gitkeep"],
        },
        "agent": {
            "backend": backend,
        },
        "logging": {
            "level": "INFO",
            "dir": "./data",
        },
        "curator": {
            "inbox_dir": "inbox",
            "processed_dir": "inbox/processed",
            "watcher": {"poll_interval": 5, "debounce_seconds": 10},
            "state": {"path": "./data/curator_state.json"},
        },
        "janitor": {
            "sweep": {
                "interval_seconds": 3600,
                "deep_sweep_interval_hours": 24,
                "structural_only": False,
                "stub_body_threshold_chars": 50,
                "orphan_exempt_dirs": ["dashboard", "view"],
                "max_files_per_agent_call": 30,
                "fix_log_in_vault": True,
            },
            "state": {"path": "./data/janitor_state.json", "max_sweep_history": 20},
        },
        "distiller": {
            "extraction": {
                "interval_seconds": 3600,
                "deep_interval_hours": 24,
                "candidate_threshold": 0.3,
                "max_sources_per_batch": 20,
                "source_types": ["conversation", "session", "note", "task", "project"],
                "learn_types": ["assumption", "decision", "constraint", "contradiction", "synthesis"],
            },
            "state": {"path": "./data/distiller_state.json", "max_run_history": 20},
        },
    }

    if enable_surveyor:
        config["surveyor"] = {
            "watcher": {"debounce_seconds": 30},
            "ollama": {
                "base_url": "http://localhost:11434",
                "model": "nomic-embed-text",
                "embedding_dims": 768,
            },
            "milvus": {
                "uri": "./data/milvus_lite.db",
                "collection_name": "vault_embeddings",
            },
            "clustering": {
                "hdbscan": {"min_cluster_size": 3, "min_samples": 2},
                "leiden": {"resolution": 1.0},
            },
            "openrouter": {
                "api_key": "${OPENROUTER_API_KEY}",
                "base_url": "https://openrouter.ai/api/v1",
                "model": "x-ai/grok-4.1-fast",
                "temperature": 0.3,
            },
            "labeler": {
                "max_files_per_cluster_context": 20,
                "body_preview_chars": 200,
                "min_cluster_size_to_label": 2,
            },
            "state": {"path": "./data/surveyor_state.json"},
        }

    with open("config.yaml", "w", encoding="utf-8") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    print(f"  Wrote config.yaml")

    # Write .env
    env_lines = ["# Alfred environment variables"]
    if zo_api_key:
        env_lines.append(f"ZO_API_KEY={zo_api_key}")
    if openrouter_api_key:
        env_lines.append(f"OPENROUTER_API_KEY={openrouter_api_key}")
    env_lines.append("")

    with open(".env", "w", encoding="utf-8") as f:
        f.write("\n".join(env_lines))
    print(f"  Wrote .env")

    # Create data dir
    Path("data").mkdir(exist_ok=True)
    print(f"  Created data/ directory")

    # Summary
    print(f"\n{'=' * 50}")
    print(f"  Setup complete!")
    print(f"{'=' * 50}")
    print(f"\n  Vault:   {vault_path}")
    print(f"  Backend: {backend}")
    print(f"  Surveyor: {'enabled' if enable_surveyor else 'disabled'}")
    print(f"\n  Next steps:")
    print(f"    1. Open {vault_path} as an Obsidian vault")
    print(f"    2. Run `alfred up` to start all daemons")
    print(f"    3. Or run individual tools: `alfred janitor scan`")
    if not enable_surveyor:
        print(f"    4. To enable surveyor later: pip install alfred-vault[all]")

    # Auto-launch prompt
    start_now = _prompt("\nStart daemons now?", "n").lower() in ("y", "yes")
    if start_now:
        from alfred.daemon import spawn_daemon
        log_dir = config.get("logging", {}).get("dir", "./data")
        log_file = f"{log_dir}/alfred.log"
        pid = spawn_daemon(config_path="config.yaml", only=None, log_file=log_file)
        print(f"\n  Alfred started (pid {pid}). Logs: {log_file}")
        print("  Stop with: alfred down")
