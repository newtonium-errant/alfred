"""OpenClaw backend — invokes OpenClaw CLI with workspace access to the vault."""

from __future__ import annotations

import asyncio
import json as _json
import os
import shutil
import uuid
from pathlib import Path

from ..config import OpenClawBackendConfig
from ..utils import get_logger
from . import BackendResult, BaseBackend, build_prompt

log = get_logger(__name__)


def _clear_agent_sessions(agent_id: str) -> None:
    """Remove all session files for an agent so each invocation starts fresh.

    OpenClaw ties each agent to a single session file.  Concurrent or
    back-to-back invocations will deadlock on the session lock unless we
    wipe the session state between runs.
    """
    sessions_dir = Path.home() / ".openclaw" / "agents" / agent_id / "sessions"
    if not sessions_dir.exists():
        return
    for f in sessions_dir.iterdir():
        try:
            f.unlink(missing_ok=True)
        except Exception:
            pass


def sync_workspace_claude_md(agent_id: str, vault_path: str) -> None:
    """Copy the vault's CLAUDE.md into the agent's workspace so OpenClaw
    injects it into the system prompt (vault architecture / ontology reference).
    """
    workspace = Path.home() / ".openclaw" / "agents" / agent_id / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    src = Path(vault_path) / "CLAUDE.md"
    dst = workspace / "CLAUDE.md"
    if src.exists():
        shutil.copy2(src, dst)


class OpenClawBackend(BaseBackend):
    def __init__(self, config: OpenClawBackendConfig, env_overrides: dict[str, str] | None = None) -> None:
        self.config = config
        self.env_overrides = env_overrides or {}

    async def process(
        self,
        inbox_content: str,
        skill_text: str,
        vault_context: str,
        inbox_filename: str,
        vault_path: str,
    ) -> BackendResult:
        prompt = build_prompt(inbox_content, skill_text, vault_context, inbox_filename, vault_path)

        # Use a unique session ID per invocation for full isolation
        session_id = f"curator-{uuid.uuid4().hex[:12]}"

        cmd = [self.config.command, "agent", *self.config.args,
               "--agent", self.config.agent_id,
               "--session-id", session_id,
               "--message", prompt, "--local", "--json"]

        log.info(
            "openclaw.dispatching",
            command=self.config.command,
            agent_id=self.config.agent_id,
            session_id=session_id,
            timeout=self.config.timeout,
        )

        # Clear previous session state to avoid lock contention.
        _clear_agent_sessions(self.config.agent_id)

        # Ensure workspace has latest vault CLAUDE.md for ontology context.
        sync_workspace_claude_md(self.config.agent_id, vault_path)

        try:
            env = {**os.environ, **self.env_overrides}
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.config.timeout,
            )
        except asyncio.TimeoutError:
            log.error("openclaw.timeout", timeout=self.config.timeout)
            return BackendResult(success=False, summary="ERROR: timeout")
        except FileNotFoundError:
            log.error("openclaw.command_not_found", command=self.config.command)
            return BackendResult(
                success=False,
                summary=f"ERROR: command not found: {self.config.command}",
            )

        raw = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            log.warning(
                "openclaw.nonzero_exit", code=proc.returncode, stderr=err[:500]
            )
            return BackendResult(
                success=False,
                summary=f"Exit code {proc.returncode}: {err[:500]}",
            )

        log.info("openclaw.completed", summary_length=len(raw))
        return BackendResult(
            success=True,
            summary=raw.strip(),
        )
