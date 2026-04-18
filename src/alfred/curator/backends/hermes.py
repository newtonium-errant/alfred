"""Hermes backend — sends the curator prompt over HTTP to a persistent
Background Hermes agent, no OpenClaw CLI subprocess required.

The Hermes agent exposes an HTTP webui API:

    POST /api/chat/start   -> {stream_id: "..."}
    GET  /api/chat/stream  -> SSE stream of tokens (newline-delimited
                               ``data: {...}`` events terminated by
                               ``event: done``)

Sessions are keyed ``vault-{scope}`` (e.g. ``vault-curator``) so the agent
accumulates skills across invocations, unlike the OpenClaw CLI where
``_clear_agent_sessions`` resets state between stages.

Ref upstream 8e2673c. Selected via ``agent.backend: "hermes"`` in
``config.yaml``. When not selected, the module is still importable but
otherwise unused.

**Not yet validated in production.** Shipped as a code path so a future
switch to ``backend: "hermes"`` is a one-line config change rather than
a code task. Smoke-tested via the backend factory and config round-trip;
live HTTP validation deferred until the user chooses to use it.
"""

from __future__ import annotations

import json as _json
import os

import httpx

from ..config import HermesBackendConfig
from ..utils import get_logger
from . import BackendResult, BaseBackend, build_prompt

log = get_logger(__name__)

# Default URL for the background Hermes agent.
DEFAULT_HERMES_URL = "http://hermes-bg:8787"


class HermesBackend(BaseBackend):
    """HTTP backend that dispatches to a persistent Hermes agent."""

    def __init__(
        self,
        config: HermesBackendConfig,
        vault_path: str = "",
        scope: str = "curator",
    ) -> None:
        self.config = config
        self.vault_path = vault_path
        self.scope = scope
        # Explicit config wins; otherwise env var; otherwise the docker
        # service name default. Matches upstream precedence.
        self.base_url = (
            config.url or os.environ.get("HERMES_BG_URL", DEFAULT_HERMES_URL)
        )
        self.timeout = config.timeout

    async def process(
        self,
        inbox_content: str,
        skill_text: str,
        vault_context: str,
        inbox_filename: str,
        vault_path: str,
    ) -> BackendResult:
        prompt = build_prompt(
            inbox_content,
            skill_text,
            vault_context,
            inbox_filename,
            vault_path,
        )

        log.info(
            "hermes.dispatching",
            url=self.base_url,
            prompt_len=len(prompt),
            scope=self.scope,
        )

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                # Start a chat session with the background Hermes
                start_resp = await client.post(
                    f"{self.base_url}/api/chat/start",
                    json={
                        "message": prompt,
                        "session_id": f"vault-{self.scope}",
                    },
                )
                start_resp.raise_for_status()
                start_data = start_resp.json()
                stream_id = start_data.get("stream_id")

                if not stream_id:
                    log.error(
                        "hermes.no_stream_id",
                        start_data=start_data,
                        scope=self.scope,
                    )
                    return BackendResult(
                        success=False,
                        summary=f"Hermes returned no stream_id: {start_data}",
                    )

                # Poll for the response via SSE
                response_text = ""
                async with client.stream(
                    "GET",
                    f"{self.base_url}/api/chat/stream",
                    params={"stream_id": stream_id},
                    timeout=self.timeout,
                ) as stream:
                    async for line in stream.aiter_lines():
                        if line.startswith("data: "):
                            data_str = line[6:]
                            try:
                                data = _json.loads(data_str)
                            except _json.JSONDecodeError:
                                continue
                            if "token" in data:
                                response_text += data["token"]
                            elif "text" in data:
                                response_text += data["text"]
                        elif line.startswith("event: done"):
                            break

                log.info(
                    "hermes.completed",
                    response_len=len(response_text),
                    scope=self.scope,
                )

                # Hermes is expected to drive vault writes via the
                # ``alfred vault`` CLI, the same contract as the Claude
                # and OpenClaw backends. The daemon's mutation log reader
                # collects files_changed — we only report the summary here.
                return BackendResult(
                    success=True,
                    summary=response_text.strip() or "No response",
                )

        except httpx.TimeoutException:
            log.error(
                "hermes.timeout",
                timeout=self.timeout,
                scope=self.scope,
            )
            return BackendResult(
                success=False,
                summary=f"Hermes timed out after {self.timeout}s",
            )
        except httpx.HTTPStatusError as e:
            log.error(
                "hermes.http_error",
                status=e.response.status_code,
                body=e.response.text[:500],
                scope=self.scope,
            )
            return BackendResult(
                success=False,
                summary=f"Hermes HTTP {e.response.status_code}: {e.response.text[:200]}",
            )
        except Exception as e:
            log.error("hermes.error", error=str(e), scope=self.scope)
            return BackendResult(
                success=False,
                summary=f"Hermes error: {e}",
            )
