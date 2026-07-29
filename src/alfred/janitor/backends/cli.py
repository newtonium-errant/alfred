"""Claude Code backend — runs claude -p with vault access via alfred vault CLI."""

from __future__ import annotations

import asyncio

from alfred.health.agent_failure import build_failure_summary, classify_agent_failure
from alfred.subprocess_env import claude_subprocess_env

from ..config import ClaudeBackendConfig
from ..utils import get_logger
from . import BackendResult, BaseBackend, build_sweep_prompt

log = get_logger(__name__)


class ClaudeBackend(BaseBackend):
    def __init__(self, config: ClaudeBackendConfig, env_overrides: dict[str, str] | None = None) -> None:
        self.config = config
        self.env_overrides = env_overrides or {}

    async def process(
        self,
        skill_text: str,
        issue_report: str,
        affected_records: str,
        vault_path: str,
        open_triage_block: str = "",
    ) -> BackendResult:
        prompt = build_sweep_prompt(
            skill_text,
            issue_report,
            affected_records,
            vault_path,
            open_triage_block=open_triage_block,
        )

        cmd = [self.config.command, *self.config.args]

        # Restrict to Bash-only (agent uses alfred vault commands)
        if self.config.allowed_tools:
            cmd.extend(["--allowedTools", ",".join(self.config.allowed_tools)])

        # Prompt via stdin to avoid ARG_MAX limits on large inputs
        cmd.append("-p")
        cmd.append("-")

        # Build environment with vault env vars. ANTHROPIC_API_KEY et al.
        # are stripped so `claude -p` uses OAuth/Max-plan auth instead of
        # API-credit billing — see alfred.subprocess_env for details.
        env = claude_subprocess_env(overrides=self.env_overrides)

        log.info(
            "claude.dispatching",
            command=self.config.command,
            vault=vault_path,
            timeout=self.config.timeout,
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8")),
                timeout=self.config.timeout,
            )
        except asyncio.TimeoutError:
            log.error("claude.timeout", timeout=self.config.timeout)
            return BackendResult(success=False, summary="ERROR: timeout", kind="other")
        except FileNotFoundError:
            log.error("claude.command_not_found", command=self.config.command)
            return BackendResult(
                success=False,
                summary=f"ERROR: command not found: {self.config.command}",
                kind="other",
            )

        raw = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            # Shared classify + summary (see 2026-07-29 weekly-limit incident):
            # quota / auth banners land on stdout, not stderr.
            kind = classify_agent_failure(raw, err)
            summary = build_failure_summary(proc.returncode, raw, err)
            log.warning(
                "claude.nonzero_exit",
                code=proc.returncode,
                kind=kind,
                stderr=err[:500],
                stdout_tail=raw[-2000:] if raw else "",
                summary=summary,
            )
            return BackendResult(
                success=False,
                summary=summary,
                kind=kind,
            )

        log.info("claude.completed", summary_length=len(raw))
        return BackendResult(success=True, summary=raw.strip())
