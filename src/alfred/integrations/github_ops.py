"""GitHub ops — KAL-LE-only GitHub access for the ticket pipeline.

THE PRIVILEGE BOUNDARY (ratified VERA→KAL-LE→GitHub pipeline design,
2026-06-11). Exactly ONE instance — KAL-LE — holds a GitHub credential.
The fine-grained PAT (Issues RW + Pull requests READ + Metadata R on
the single configured repo) is referenced ONLY from KAL-LE's config
(``github:`` section in ``config.kalle.yaml`` →
``${ALGERNON_KALLE_GITHUB_PAT}``). The host ``.env`` is shared across
instances, so the env var itself is host-visible; the boundary is
therefore enforced by two code-level facts working together:

1. **Config reference** — only KAL-LE's config file carries a
   ``github:`` section, so only KAL-LE's loaders ever resolve the PAT.
2. **The instance gate** — :func:`build_github_client` raises
   :class:`GitHubOpsWrongInstance` when the calling instance's name
   doesn't match ``github.instance``, so even a misconfigured copy of
   the section into another instance's YAML fails loud at client
   build time, never silently grants access.

VERA never talks to GitHub: it pushes tickets to KAL-LE over the peer
protocol and receives issue link-backs. GitHub Actions owns PR
creation under its own app identity; merging is the operator via
branch protection. This module's job is the narrow KAL-LE slice:
create/search issues at intake, read issues/PRs for the digest's
effectiveness loop.

Scope-first (CLAUDE.md): :data:`GITHUB_OPS` — the op × caller-context
allowlist matrix — is the principal artifact of this module. Every
HTTP call consults the shared gate :func:`_check_github_op` first;
implementation flows from the matrix, not the other way around.

Transport: httpx (no ``gh`` shell-out, no new deps), modeled on the
``alfred.brief.watches`` idioms — 10s timeout, explicit User-Agent,
module-level request function for test monkeypatching.

Audit: every op (allowed, denied, errored) appends one JSONL row via
:func:`append_github_audit` (modeled on
``alfred.transport.canonical_audit.append_audit`` semantics — parent
dir create, single append write, never raises).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import structlog

from alfred._env import substitute_env_in_value

log = structlog.get_logger(__name__)


GITHUB_API_BASE = "https://api.github.com"

# Short — pipeline callers own retry/containment (c3); a slow GitHub
# day is an audited error row, not a hung daemon.
_TIMEOUT_SECONDS = 10.0

_USER_AGENT = "algernon-github-ops"

DEFAULT_AUDIT_LOG_PATH = "./data/github_ops_audit.jsonl"


# ---------------------------------------------------------------------------
# THE MATRIX — op × caller-context allowlist (the principal artifact)
# ---------------------------------------------------------------------------
#
# Caller contexts:
#   "ticket_intake" — KAL-LE's deterministic ticket-intake handler
#                     (c3): records a VERA-pushed ticket, files the
#                     GitHub issue, recovers from dedupe gaps.
#   "digest"        — KAL-LE's digest/effectiveness loop (c5): reads
#                     issue + linked-PR state to derive dispositions
#                     (e.g. merged_after_rework) and close the loop.
#
# DENY-ROWS BY DESIGN (absent op == denied; the gate has no default-
# allow path):
#   * issue_comment / issue_close — reserved future ops, operator-
#     gated. The MVP pipeline never mutates an issue after creation;
#     widening requires touching this matrix AND its contract-pin test
#     in the same commit.
#   * ALL pr writes (pr_create / pr_merge / pr_comment / pr_review /
#     pr_close / ...) — PERMANENTLY denied. GitHub Actions owns PRs
#     under its own app identity; merge authority is the operator via
#     branch protection. KAL-LE only ever READS PR state.
#   * repo contents / workflows / settings — not even in the PAT's
#     permission set (fine-grained PAT: Issues RW, Pull requests READ,
#     Metadata R). Listed here so nobody "helpfully" adds them later
#     without noticing the credential can't do it anyway.
#
# op -> frozenset of allowed caller contexts. Absent op == denied.
GITHUB_OPS: dict[str, frozenset[str]] = {
    "issue_create":    frozenset({"ticket_intake"}),
    "issue_label_add": frozenset({"ticket_intake"}),   # create-time labels only at MVP
    "issue_search":    frozenset({"ticket_intake"}),   # marker-based dedupe recovery
    "issue_get":       frozenset({"digest"}),          # linked-PR/outcome check
    "issue_timeline":  frozenset({"digest"}),
    "pr_get":          frozenset({"digest"}),          # effectiveness-loop capture (ratified amendment)
    "pr_reviews":      frozenset({"digest"}),          # disposition derivation (merged_after_rework)
}


# ---------------------------------------------------------------------------
# Issue body marker — the dedupe join key
# ---------------------------------------------------------------------------

# c3 composes the full HTML-comment marker into issue bodies; the
# marker-based dedupe search (``issue_search_marker``) queries on the
# INNER text (GitHub's search index strips HTML comment delimiters).
ISSUE_MARKER_TEMPLATE = "<!-- algernon-ticket: {ticket_uid} -->"


def issue_marker(ticket_uid: str) -> str:
    """Render the issue-body marker for one ticket UID."""
    return ISSUE_MARKER_TEMPLATE.format(ticket_uid=ticket_uid)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GitHubOpsError(Exception):
    """Base for all github_ops policy errors."""


class GitHubOpsDenied(GitHubOpsError):
    """The (op, caller) pair is not allowed by :data:`GITHUB_OPS`."""


class GitHubOpsNotConfigured(GitHubOpsError):
    """The ``github:`` config is absent or incomplete.

    Raised by :func:`build_github_client` — callers performing WRITE
    ops must let this propagate (never silently degrade); read-side
    callers may catch it and render an explicit "not configured" line.
    """


class GitHubOpsWrongInstance(GitHubOpsError):
    """A non-KAL-LE instance tried to build the GitHub client.

    The code-gate half of the single-credential privilege boundary —
    see the module docstring.
    """


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def append_github_audit(
    audit_log_path: str | Path,
    *,
    op: str,
    repo: str,
    caller: str,
    outcome: str,
    ticket_uid: str = "",
    issue_number: int | None = None,
    http_status: int | None = None,
    from_peer: str = "",
    correlation_id: str = "",
    error: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one audit entry to the github_ops JSONL log.

    Modeled EXACTLY on ``transport.canonical_audit.append_audit``
    semantics:
      - Creates the parent directory if missing.
      - Single ``open(..., "a")`` write per call.
      - NEVER raises — disk errors log-and-continue (audit failures
        must not interrupt the GitHub call or its containment).

    ``outcome`` vocabulary: ``created | exists | adopted | denied |
    error | ok``.

    ``extra`` is the hook for effectiveness-loop fields (c5:
    pr_number, pr_state, disposition, latency_days). Merged FIRST so
    the core identity fields below overwrite any conflicting key —
    ``extra`` can add fields but can never corrupt op/repo/caller.
    """
    if not audit_log_path:
        # Audit explicitly disabled — skip. Prod configs always set this.
        return
    path = Path(audit_log_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning(
            "github_ops.audit_write_failed",
            path=str(path),
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        return

    entry: dict[str, Any] = {}
    if isinstance(extra, dict):
        entry.update(extra)
    entry.update({
        "ts": datetime.now(timezone.utc).isoformat(),
        "op": op,
        "repo": repo,
        "caller": caller,
        "outcome": outcome,
        "ticket_uid": ticket_uid,
        "issue_number": issue_number,
        "http_status": http_status,
        "from_peer": from_peer,
        "correlation_id": correlation_id,
        "error": error,
    })
    line = json.dumps(entry, default=str) + "\n"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError as exc:
        log.warning(
            "github_ops.audit_write_failed",
            path=str(path),
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        return


def read_github_audit(audit_log_path: str | Path) -> list[dict[str, Any]]:
    """Read the audit log into a list of dicts (tests + CLI inspection)."""
    path = Path(audit_log_path)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                out.append(json.loads(stripped))
            except json.JSONDecodeError:
                continue
    return out


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def _check_github_op(op: str, caller: str) -> None:
    """Shared gate — consulted before EVERY HTTP call.

    Same shape as ``vault.scope``'s ``_check_body_mutation_allowed``:
    one chokepoint consulting the matrix; absent op == denied, wrong
    caller == denied. Raises :class:`GitHubOpsDenied`; the client
    wrapper audits the denial row before re-raising.
    """
    allowed = GITHUB_OPS.get(op)
    if allowed is None:
        raise GitHubOpsDenied(
            f"GitHub op '{op}' is not in the ops matrix — denied by design"
        )
    if caller not in allowed:
        raise GitHubOpsDenied(
            f"GitHub op '{op}' is not allowed for caller '{caller}' "
            f"(allowed callers: {sorted(allowed)})"
        )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class GitHubOpsConfig:
    """Typed view of the ``github:`` config section (KAL-LE-only)."""

    repo: str = ""
    pat: str = ""
    instance: str = ""
    labels: list[str] = field(default_factory=lambda: ["auto-fix"])
    # ticket_type / priority value -> GitHub label name. c3 consults
    # this at issue-create time; unmapped values get no extra label.
    label_map: dict[str, str] = field(default_factory=dict)
    audit_log_path: str = DEFAULT_AUDIT_LOG_PATH


def load_github_config(raw: dict[str, Any]) -> GitHubOpsConfig | None:
    """Build :class:`GitHubOpsConfig` from the unified config dict.

    Returns ``None`` when the ``github:`` section is absent — callers
    that get ``None`` must NOT silently degrade for write ops (use
    :func:`build_github_client`, which fails loud).

    Env substitution: the top-level ``_load_unified_config`` does NOT
    substitute ``${VAR}`` placeholders (verified 2026-06-11 — each
    tool's ``load_from_unified`` substitutes its own section, e.g.
    ``transport/config.py``), so this loader substitutes locally via
    the canonical ``alfred._env`` helper. An unset env var leaves the
    literal ``${VARNAME}`` in place, which
    :func:`build_github_client`'s fail-loud guard detects.
    """
    section = raw.get("github")
    if not isinstance(section, dict):
        return None
    section = substitute_env_in_value(section)

    labels_raw = section.get("labels")
    if isinstance(labels_raw, list):
        labels = [str(item) for item in labels_raw]
    else:
        labels = ["auto-fix"]

    label_map_raw = section.get("label_map") or {}
    label_map: dict[str, str] = {}
    if isinstance(label_map_raw, dict):
        label_map = {str(k): str(v) for k, v in label_map_raw.items()}

    return GitHubOpsConfig(
        repo=str(section.get("repo", "") or ""),
        pat=str(section.get("pat", "") or ""),
        instance=str(section.get("instance", "") or ""),
        labels=labels,
        label_map=label_map,
        audit_log_path=str(
            section.get("audit_log_path", DEFAULT_AUDIT_LOG_PATH)
            or DEFAULT_AUDIT_LOG_PATH
        ),
    )


def build_github_client(
    raw: dict[str, Any],
    instance_name: str,
) -> "GitHubOpsClient":
    """Fail-loud factory for the GitHub ops client.

    Raises (never silently skips):
      * :class:`GitHubOpsNotConfigured` — ``github:`` section absent,
        ``pat`` empty or still carrying an unsubstituted ``${VAR}``
        placeholder, ``repo`` empty, or ``instance`` empty.
      * :class:`GitHubOpsWrongInstance` — ``instance_name`` doesn't
        match ``github.instance`` (the code-gate half of the
        privilege boundary; see module docstring).

    Both failure classes append an ``outcome="denied"`` audit row with
    the reason before raising.
    """
    config = load_github_config(raw)
    if config is None:
        reason = "github: config section absent"
        append_github_audit(
            DEFAULT_AUDIT_LOG_PATH,
            op="build_client",
            repo="",
            caller=instance_name,
            outcome="denied",
            error=reason,
        )
        raise GitHubOpsNotConfigured(
            f"GitHub ops not configured: {reason} — add a github: section "
            "(repo, pat, instance) to this instance's config"
        )

    missing: list[str] = []
    if not config.repo:
        missing.append("repo (empty)")
    if not config.pat:
        missing.append("pat (empty)")
    elif "${" in config.pat:
        missing.append(
            f"pat (unsubstituted placeholder {config.pat!r} — env var not set)"
        )
    if not config.instance:
        missing.append("instance (empty)")
    if missing:
        reason = f"github config incomplete: {'; '.join(missing)}"
        append_github_audit(
            config.audit_log_path,
            op="build_client",
            repo=config.repo,
            caller=instance_name,
            outcome="denied",
            error=reason,
        )
        raise GitHubOpsNotConfigured(f"GitHub ops not configured: {reason}")

    if instance_name != config.instance:
        reason = (
            f"GitHub ops are configured for instance {config.instance!r} "
            f"but the caller is {instance_name!r}"
        )
        append_github_audit(
            config.audit_log_path,
            op="build_client",
            repo=config.repo,
            caller=instance_name,
            outcome="denied",
            error=reason,
        )
        raise GitHubOpsWrongInstance(reason)

    return GitHubOpsClient(config)


# ---------------------------------------------------------------------------
# HTTP — module-level request function (test monkeypatch point, the
# watches-module convention)
# ---------------------------------------------------------------------------


async def _github_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        return await client.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json_body,
        )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class GitHubOpsClient:
    """Thin, gated, audited httpx wrapper over the GitHub REST API.

    Build via :func:`build_github_client` (the fail-loud factory) —
    constructing directly skips the not-configured / wrong-instance
    guards and is for tests only.
    """

    def __init__(self, config: GitHubOpsConfig) -> None:
        self._config = config

    @property
    def config(self) -> GitHubOpsConfig:
        return self._config

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._config.pat}",
            "User-Agent": _USER_AGENT,
        }

    def _gate(
        self,
        op: str,
        caller: str,
        *,
        ticket_uid: str = "",
        correlation_id: str = "",
    ) -> None:
        """Consult the matrix; audit + warn + raise on violation."""
        try:
            _check_github_op(op, caller)
        except GitHubOpsDenied as exc:
            append_github_audit(
                self._config.audit_log_path,
                op=op,
                repo=self._config.repo,
                caller=caller,
                outcome="denied",
                ticket_uid=ticket_uid,
                correlation_id=correlation_id,
                error=str(exc),
            )
            log.warning(
                "github_ops.denied",
                op=op,
                caller=caller,
                repo=self._config.repo,
            )
            raise

    async def _request_audited(
        self,
        *,
        op: str,
        caller: str,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        ticket_uid: str = "",
        correlation_id: str = "",
    ) -> httpx.Response:
        """Gate → HTTP → audit-on-error. Success auditing is the
        per-method caller's job (outcomes differ: created/ok/...)."""
        self._gate(
            op, caller, ticket_uid=ticket_uid, correlation_id=correlation_id,
        )
        try:
            resp = await _github_request(
                method,
                url,
                headers=self._headers(),
                params=params,
                json_body=json_body,
            )
        except httpx.HTTPError as exc:
            # Transport-level failure (timeout, DNS, ...) — no status.
            append_github_audit(
                self._config.audit_log_path,
                op=op,
                repo=self._config.repo,
                caller=caller,
                outcome="error",
                ticket_uid=ticket_uid,
                correlation_id=correlation_id,
                error=f"{exc.__class__.__name__}: {exc}",
            )
            raise
        if resp.status_code >= 400:
            append_github_audit(
                self._config.audit_log_path,
                op=op,
                repo=self._config.repo,
                caller=caller,
                outcome="error",
                http_status=resp.status_code,
                ticket_uid=ticket_uid,
                correlation_id=correlation_id,
                error=resp.text[:500],
            )
            # Propagates httpx.HTTPStatusError — c3 owns containment.
            resp.raise_for_status()
        return resp

    # --- intake ops ---------------------------------------------------------

    async def issue_create(
        self,
        *,
        title: str,
        body: str,
        labels: list[str],
        ticket_uid: str,
        caller: str,
        correlation_id: str = "",
    ) -> dict[str, Any]:
        """POST /repos/{repo}/issues — returns ``{number, html_url}``."""
        resp = await self._request_audited(
            op="issue_create",
            caller=caller,
            method="POST",
            url=f"{GITHUB_API_BASE}/repos/{self._config.repo}/issues",
            json_body={
                "title": title,
                "body": body,
                "labels": list(labels),
            },
            ticket_uid=ticket_uid,
            correlation_id=correlation_id,
        )
        data = resp.json()
        number = data.get("number")
        append_github_audit(
            self._config.audit_log_path,
            op="issue_create",
            repo=self._config.repo,
            caller=caller,
            outcome="created",
            ticket_uid=ticket_uid,
            issue_number=number,
            http_status=resp.status_code,
            correlation_id=correlation_id,
        )
        return {"number": number, "html_url": data.get("html_url", "")}

    async def issue_search_marker(
        self,
        *,
        ticket_uid: str,
        caller: str,
        correlation_id: str = "",
    ) -> dict[str, Any] | None:
        """Marker-based dedupe recovery — GET /search/issues.

        Searches issue bodies for the INNER marker text (GitHub's
        search index strips the HTML comment delimiters that
        :func:`issue_marker` wraps around it). Returns the first hit
        as ``{number, html_url, state}`` or ``None`` on no match.
        """
        query = (
            f'repo:{self._config.repo} in:body '
            f'"algernon-ticket: {ticket_uid}"'
        )
        resp = await self._request_audited(
            op="issue_search",
            caller=caller,
            method="GET",
            url=f"{GITHUB_API_BASE}/search/issues",
            params={"q": query},
            ticket_uid=ticket_uid,
            correlation_id=correlation_id,
        )
        data = resp.json()
        items = data.get("items") if isinstance(data, dict) else None
        items = items if isinstance(items, list) else []
        first = items[0] if items and isinstance(items[0], dict) else None
        append_github_audit(
            self._config.audit_log_path,
            op="issue_search",
            repo=self._config.repo,
            caller=caller,
            outcome="ok",
            ticket_uid=ticket_uid,
            issue_number=first.get("number") if first else None,
            http_status=resp.status_code,
            correlation_id=correlation_id,
            extra={"match_count": len(items)},
        )
        if first is None:
            return None
        return {
            "number": first.get("number"),
            "html_url": first.get("html_url", ""),
            "state": first.get("state", ""),
        }

    # --- digest (read-only) ops ----------------------------------------------

    async def _get_json(
        self,
        *,
        op: str,
        caller: str,
        url: str,
        correlation_id: str = "",
        issue_number: int | None = None,
    ) -> Any:
        """Thin audited GET returning parsed JSON."""
        resp = await self._request_audited(
            op=op,
            caller=caller,
            method="GET",
            url=url,
            correlation_id=correlation_id,
        )
        append_github_audit(
            self._config.audit_log_path,
            op=op,
            repo=self._config.repo,
            caller=caller,
            outcome="ok",
            issue_number=issue_number,
            http_status=resp.status_code,
            correlation_id=correlation_id,
        )
        return resp.json()

    async def issue_get(
        self,
        *,
        number: int,
        caller: str,
        correlation_id: str = "",
    ) -> Any:
        """GET /repos/{repo}/issues/{number}."""
        return await self._get_json(
            op="issue_get",
            caller=caller,
            url=f"{GITHUB_API_BASE}/repos/{self._config.repo}/issues/{number}",
            correlation_id=correlation_id,
            issue_number=number,
        )

    async def issue_timeline(
        self,
        *,
        number: int,
        caller: str,
        correlation_id: str = "",
    ) -> Any:
        """GET /repos/{repo}/issues/{number}/timeline.

        The timeline API is GA in REST v3 — the old
        ``mockingbird-preview`` Accept header is long retired; the
        standard ``application/vnd.github+json`` suffices.
        """
        return await self._get_json(
            op="issue_timeline",
            caller=caller,
            url=(
                f"{GITHUB_API_BASE}/repos/{self._config.repo}"
                f"/issues/{number}/timeline"
            ),
            correlation_id=correlation_id,
            issue_number=number,
        )

    async def pr_get(
        self,
        *,
        number: int,
        caller: str,
        correlation_id: str = "",
    ) -> Any:
        """GET /repos/{repo}/pulls/{number} — read-only (see matrix)."""
        return await self._get_json(
            op="pr_get",
            caller=caller,
            url=f"{GITHUB_API_BASE}/repos/{self._config.repo}/pulls/{number}",
            correlation_id=correlation_id,
        )

    async def pr_reviews(
        self,
        *,
        number: int,
        caller: str,
        correlation_id: str = "",
    ) -> Any:
        """GET /repos/{repo}/pulls/{number}/reviews — read-only."""
        return await self._get_json(
            op="pr_reviews",
            caller=caller,
            url=(
                f"{GITHUB_API_BASE}/repos/{self._config.repo}"
                f"/pulls/{number}/reviews"
            ),
            correlation_id=correlation_id,
        )


__all__ = [
    "DEFAULT_AUDIT_LOG_PATH",
    "GITHUB_OPS",
    "GitHubOpsClient",
    "GitHubOpsConfig",
    "GitHubOpsDenied",
    "GitHubOpsError",
    "GitHubOpsNotConfigured",
    "GitHubOpsWrongInstance",
    "ISSUE_MARKER_TEMPLATE",
    "append_github_audit",
    "build_github_client",
    "issue_marker",
    "load_github_config",
    "read_github_audit",
]
