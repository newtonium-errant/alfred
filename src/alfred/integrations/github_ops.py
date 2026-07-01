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
protocol and receives issue link-backs. Merging is ALWAYS the operator
via branch protection — :data:`GITHUB_OPS` carries no ``pr_merge`` key,
permanently and by design.

PR CREATION is forge-split (Phase 1B, 2026-06-30):
  * **GitHub path** — GitHub Actions owns PR creation under its own app
    identity; ``pr_create`` is FORGE-FENCED denied on a github-config
    client (the historic "all PR writes denied" rule still holds for
    GitHub, see :meth:`GitHubOpsClient._forge_fence`).
  * **Forgejo path** — the sovereign Forgejo box has no GitHub Actions,
    so KAL-LE's on-box ``fix_drafter`` daemon authors the draft PR. The
    matrix reverses the historic deny at the thinnest seam: ONE new
    write op (``pr_create``), ONE new caller (``fix_drafter``), gated to
    Forgejo only. The git branch-push itself is git-over-HTTP, NOT a REST
    op — it never enters this matrix.

This module's job is the narrow KAL-LE slice: create/search issues at
intake, read issues/PRs for the digest's effectiveness loop, and (on
Forgejo) scan/open auto-fix PRs for the on-box drafter.

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
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import structlog

from alfred._env import substitute_env_in_value

log = structlog.get_logger(__name__)


# Forge type + base are config-driven (``github.forge_type`` /
# ``github.api_base``). Default is GitHub, so a box still on GitHub
# config is BYTE-IDENTICAL to pre-Forgejo behavior — this module gets
# deployed to master while the live pipeline is still on GitHub. Forgejo
# is an opt-in branch (the RRTS tracker's sovereign target), so GitHub +
# Forgejo coexist and cutover/rollback is a one-line config flip.
GITHUB_API_BASE = "https://api.github.com"

# Forge selectors. ``github`` is the default + the byte-identical-to-
# pre-port path; ``forgejo`` opts into the divergent shapes (per-repo
# issue search, []int64 labels, the digest timeline shape).
FORGE_GITHUB = "github"
FORGE_FORGEJO = "forgejo"
FORGE_TYPES = frozenset({FORGE_GITHUB, FORGE_FORGEJO})

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
#   "fix_drafter"   — KAL-LE's on-box auto-fix drafter daemon (Phase
#                     1B, Forgejo-ONLY): scans open auto-fix issues,
#                     re-verifies the auto-fix label, crash-recovery
#                     dedups against open PRs, opens the draft PR. A
#                     SEPARATE caller from ticket_intake/digest by
#                     least-privilege design — its entire allowance is
#                     {issue_get, issue_list, pr_list, pr_create}.
#
# DENY-ROWS BY DESIGN (absent op == denied; the gate has no default-
# allow path):
#   * issue_comment / issue_close — reserved future ops, operator-
#     gated. The MVP pipeline never mutates an issue after creation;
#     widening requires touching this matrix AND its contract-pin test
#     in the same commit.
#   * pr_merge — PERMANENTLY denied (no key, never added), on BOTH forges.
#     The operator is the single merge authority via branch protection; this
#     mirrors claude-auto-fix's never-merge rule. The most load-bearing deny
#     in the matrix — UNCHANGED by Option B (pinned on both forges).
#   * pr_review / pr_close — operator owns the PR lifecycle after the
#     drafter opens it; the drafter only ever CREATES.
#   * pr_create — a WRITE op allowed to ``fix_drafter``. Under Option B it is
#     NO LONGER forge-fenced: the drafter opens PHI-clean fix PRs onto the
#     APP repo, which may legitimately be GitHub — so pr_create/pr_list
#     FOLLOW THE APP-REPO FORGE (``_FORGEJO_ONLY_OPS`` keeps only
#     ``issue_list``, the sovereign-tracker scan). ``pr_merge`` stays denied.
#   * repo contents / workflows / settings / branch_protection — not in
#     the PAT's permission set; branch creation happens via git push,
#     not REST. Listed so nobody "helpfully" adds them later.
#
# op -> frozenset of allowed caller contexts. Absent op == denied.
GITHUB_OPS: dict[str, frozenset[str]] = {
    "issue_create":    frozenset({"ticket_intake"}),
    "issue_label_add": frozenset({"ticket_intake"}),   # create-time labels only at MVP
    "label_list":      frozenset({"ticket_intake"}),   # Forgejo name→id resolution (inside issue_create)
    "issue_search":    frozenset({"ticket_intake"}),   # marker-based dedupe recovery
    # +fix_drafter: authoritative pre-draft auto-fix-label re-verify
    # (forgejo on-box drafter). NOT forge-fenced — digest still reads
    # issues on github; fix_drafter never runs on a github box (triple-
    # gated) so the widened caller can't reach a github client in prod.
    "issue_get":       frozenset({"digest", "fix_drafter"}),
    "issue_timeline":  frozenset({"digest"}),
    "pr_get":          frozenset({"digest"}),          # effectiveness-loop capture (ratified amendment)
    "pr_reviews":      frozenset({"digest"}),          # disposition derivation (merged_after_rework)
    # --- Forgejo on-box auto-fix drafter (Phase 1B, fix_drafter) ------------
    # FORGE-FENCED to Forgejo (see _forge_fence): a github-config client
    # raises before any HTTP. GitHub keeps its claude-auto-fix.yml
    # GH-Action drafter; the on-box flow is the Forgejo replacement.
    "issue_list":      frozenset({"fix_drafter"}),     # scan open auto-fix issues (the work queue)
    "pr_list":         frozenset({"fix_drafter"}),     # crash-recovery dedup (adopt/resume head.ref)
    "pr_create":       frozenset({"fix_drafter"}),     # the ONE PR write op — forgejo-only; operator still merges
}

# Ops that are FORGEJO-ONLY — a second policy gate OUTSIDE the op×caller
# matrix (the matrix has no forge dimension). :meth:`_forge_fence` raises
# :class:`GitHubOpsDenied` for these on a github-config client BEFORE the
# op×caller gate.
#
# Option B (central bug-intake, ratified 2026-07-01): ``pr_create`` +
# ``pr_list`` were DROPPED from this set. Their original "Forgejo-only"
# rationale — "GitHub Actions owns PR creation there" — evaporated under B:
# the on-box drafter is the SOLE PR author (claude-auto-fix.yml /
# claude-code-action / act_runner are gone), and the drafter now opens
# PHI-CLEAN fix PRs onto the APP repo, which may legitimately be GitHub
# (app code stays PHI-free on GitHub; only the central tracker is sovereign
# Forgejo). So pr_create/pr_list must FOLLOW THE APP-REPO FORGE, not be
# pinned to Forgejo. ``issue_list`` STAYS Forgejo-pinned (the tracker is the
# sovereign sink — the drafter only ever SCANS it, never GitHub). ``pr_merge``
# stays a PERMANENT matrix-deny on BOTH forges (operator is the merge gate).
_FORGEJO_ONLY_OPS: frozenset[str] = frozenset(
    {"issue_list"},
)


# ---------------------------------------------------------------------------
# Issue body marker — the dedupe join key
# ---------------------------------------------------------------------------

# c3 composes the full HTML-comment marker into issue bodies; the
# marker-based dedupe search (``issue_search_marker``) keys on it. On
# GitHub the search index strips HTML-comment delimiters, so the query
# matches the INNER text; on Forgejo the per-repo ``q=`` is only a coarse
# pre-filter and the AUTHORITATIVE gate is a client-side substring match
# of this FULL marker against each candidate issue's body.
ISSUE_MARKER_TEMPLATE = "<!-- algernon-ticket: {ticket_uid} -->"


def issue_marker(ticket_uid: str) -> str:
    """Render the issue-body marker for one ticket UID."""
    return ISSUE_MARKER_TEMPLATE.format(ticket_uid=ticket_uid)


# Project routing marker (Option B, 2026-07-01). The intake stamps this into
# the CENTRAL tracker issue body (deterministic infra provenance — the
# authenticated relay peer → project slug); the on-box drafter parses it to
# pick the APP repo to draft the fix against. The slug is NOT PHI → body-safe
# (mirrors the algernon-ticket dedupe marker's placement).
PROJECT_MARKER_TEMPLATE = "<!-- algernon-project: {slug} -->"
_PROJECT_MARKER_RE = re.compile(r"<!--\s*algernon-project:\s*([A-Za-z0-9._-]+)\s*-->")


def project_marker(slug: str) -> str:
    """Render the project-routing marker for one project slug."""
    return PROJECT_MARKER_TEMPLATE.format(slug=slug)


def parse_project_marker(body: str) -> str:
    """Extract the project slug from an issue body, or "" when absent.
    A slug is a safe filesystem/URL token ([A-Za-z0-9._-])."""
    m = _PROJECT_MARKER_RE.search(str(body or ""))
    return m.group(1) if m else ""


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
    # repr=False: the PAT is a credential — keep it out of repr()-driven
    # surfaces (log lines, tracebacks, debugger dumps of the config).
    pat: str = field(default="", repr=False)
    instance: str = ""
    # Forge selector — config-driven so GitHub + Forgejo coexist. Default
    # ``"github"`` keeps a GitHub-config box byte-identical to pre-port.
    # The divergent ops (issue_search_marker, issue_create labels, the
    # digest PR-discovery) branch on this; headers + read-op URLs are
    # compatible across both forges and DON'T branch.
    forge_type: str = FORGE_GITHUB
    # Forge base URL — config-driven (default GitHub). Forgejo instances
    # set ``github.api_base: https://git.<domain>/api/v1`` in their config.
    api_base: str = GITHUB_API_BASE
    labels: list[str] = field(default_factory=lambda: ["auto-fix"])
    # ticket_type / priority value -> GitHub label(s). c3 consults this
    # at issue-create time; unmapped values get no extra label. Each
    # value may be a single label (``"bug"``) OR a list of labels
    # (``["bug", "auto-fix"]``) — the loader normalizes bare strings to
    # 1-element lists so old single-string configs still parse. The
    # list form is what gates the auto-fix label to BUG tickets only
    # (2026-06-13): ``bug: ["bug", "auto-fix"]`` fires the auto-fix
    # workflow at creation, ``enhancement: ["enhancement"]`` does not.
    label_map: dict[str, list[str]] = field(default_factory=dict)
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

    # label_map values accept str | list[str] — normalize each to a
    # list[str] so callers get one uniform shape. A bare string becomes
    # a 1-element list (back-compat: old ``{bug: "bug"}`` configs still
    # work); a list is element-stringified, dropping empty/blank items.
    label_map_raw = section.get("label_map") or {}
    label_map: dict[str, list[str]] = {}
    if isinstance(label_map_raw, dict):
        for k, v in label_map_raw.items():
            if isinstance(v, (list, tuple)):
                values = [
                    str(item).strip()
                    for item in v
                    if str(item).strip()
                ]
            elif v is None:
                values = []
            else:
                text = str(v).strip()
                values = [text] if text else []
            label_map[str(k)] = values

    # ----- auto-fix invariant (operator-ratified 2026-06-13) -------------
    # `auto-fix` may appear ONLY under label_map["bug"] — nowhere else.
    # A bug ticket maps to ["bug", "auto-fix"] so the claude-auto-fix.yml
    # workflow fires at issue creation; every other ticket_type/priority
    # files a tracked issue WITHOUT auto-fix. This guard CODE-enforces that
    # invariant (was convention-only) at the single load/normalization
    # chokepoint: if `auto-fix` leaks into base `labels` or into any
    # non-`bug` label_map value (config typo, copy-paste, a future edit
    # that forgets the gating), it is STRIPPED and a LOUD warning names the
    # offending location so the misconfig is observable, not silent.
    #
    # If `auto-fix` ever legitimately needs to apply to another
    # ticket_type, THIS is the single place to revisit — do NOT generalize
    # to a configurable auto_fix_types set here (out of scope 2026-06-13);
    # keep the invariant keyed to "bug".
    _AUTO_FIX_LABEL = "auto-fix"
    if _AUTO_FIX_LABEL in labels:
        log.warning(
            "github.config.auto_fix_label_stripped",
            location="base-labels",
            detail=(
                "auto-fix found in base `labels` — invariant allows it only "
                "under label_map['bug']; stripping it from base labels"
            ),
        )
        labels = [lbl for lbl in labels if lbl != _AUTO_FIX_LABEL]
    for key, values in list(label_map.items()):
        if key == "bug":
            continue
        if _AUTO_FIX_LABEL in values:
            log.warning(
                "github.config.auto_fix_label_stripped",
                location=f"label_map[{key!r}]",
                detail=(
                    f"auto-fix found under label_map[{key!r}] — invariant "
                    "allows it only under label_map['bug']; stripping it"
                ),
            )
            label_map[key] = [v for v in values if v != _AUTO_FIX_LABEL]

    # forge_type selects the data-plane shapes. Default + unknown values
    # fall back to ``github`` (the byte-identical-to-pre-port path); an
    # unknown value warns so a config typo (e.g. ``forgejoo``) is
    # observable rather than silently selecting the wrong shapes.
    forge_type = str(
        section.get("forge_type", FORGE_GITHUB) or FORGE_GITHUB
    ).strip().lower()
    if forge_type not in FORGE_TYPES:
        log.warning(
            "github.config.unknown_forge_type",
            forge_type=forge_type,
            detail=(
                f"forge_type must be one of {sorted(FORGE_TYPES)}; "
                "falling back to 'github'"
            ),
        )
        forge_type = FORGE_GITHUB

    return GitHubOpsConfig(
        repo=str(section.get("repo", "") or ""),
        pat=str(section.get("pat", "") or ""),
        instance=str(section.get("instance", "") or ""),
        forge_type=forge_type,
        api_base=str(section.get("api_base", GITHUB_API_BASE) or GITHUB_API_BASE),
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


def build_client_for_repo(
    *,
    repo: str,
    pat: str,
    forge_type: str = FORGE_GITHUB,
    api_base: str = "",
    audit_log_path: str = DEFAULT_AUDIT_LOG_PATH,
    instance: str = "",
) -> "GitHubOpsClient":
    """Build a client for an ARBITRARY (repo, forge) — DELIBERATELY bypassing
    the instance gate (Option B, 2026-07-01).

    The credential-holder identity was already validated on the central
    tracker client (:func:`build_github_client`); the on-box drafter then
    builds a SECOND client per app repo to open PHI-clean fix PRs against
    it. That app repo may be a different forge (GitHub) than the central
    tracker (Forgejo) — the ``_headers`` forge-conditional handles the auth
    dispatch. ``api_base`` defaults to the GitHub base when omitted; a
    forgejo target MUST pass its ``/api/v1`` base. The ``fix_drafter`` caller
    is still gated by :data:`GITHUB_OPS` per op, and ``pr_merge`` stays
    permanently denied — this factory does NOT widen the op matrix."""
    config = GitHubOpsConfig(
        repo=repo,
        pat=pat,
        instance=instance,
        forge_type=(forge_type if forge_type in FORGE_TYPES else FORGE_GITHUB),
        api_base=api_base or GITHUB_API_BASE,
        audit_log_path=audit_log_path or DEFAULT_AUDIT_LOG_PATH,
    )
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
        # Lazy-built {label_name.lower(): id} map for Forgejo's []int64
        # labels field. None = not yet fetched; cached per client lifetime
        # (one labels GET per intake daemon process — labels rarely change).
        self._label_id_cache: dict[str, int] | None = None

    @property
    def config(self) -> GitHubOpsConfig:
        return self._config

    def _headers(self) -> dict[str, str]:
        # Forge-CONDITIONAL so the GitHub live path is byte-identical to
        # pre-port (incl. endpoints like /timeline that historically cared
        # about the versioned Accept). GitHub: the exact pre-port headers
        # (``Bearer`` + ``application/vnd.github+json``). Forgejo: its
        # canonical ``token`` scheme + ``application/json``.
        if self._config.forge_type == FORGE_FORGEJO:
            return {
                "Accept": "application/json",
                "Authorization": f"token {self._config.pat}",
                "User-Agent": _USER_AGENT,
            }
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

    def _forge_fence(
        self,
        op: str,
        caller: str,
        *,
        ticket_uid: str = "",
        correlation_id: str = "",
        issue_number: int | None = None,
    ) -> None:
        """Second policy gate (outside the op×caller matrix): the drafter
        ops in :data:`_FORGEJO_ONLY_OPS` are Forgejo-only.

        Runs BEFORE :meth:`_gate`. On a github-config client it audits an
        ``outcome="denied"`` row, logs ``github_ops.forge_denied`` (op in
        the field, greppable), and raises :class:`GitHubOpsDenied` — never
        touching HTTP. A no-op for non-fenced ops and for Forgejo clients.
        """
        if op not in _FORGEJO_ONLY_OPS:
            return
        if self._config.forge_type == FORGE_FORGEJO:
            return
        reason = (
            f"GitHub op '{op}' is forgejo-only (github path: GitHub Actions "
            f"owns PR creation); forge_type={self._config.forge_type!r}"
        )
        append_github_audit(
            self._config.audit_log_path,
            op=op,
            repo=self._config.repo,
            caller=caller,
            outcome="denied",
            ticket_uid=ticket_uid,
            issue_number=issue_number,
            correlation_id=correlation_id,
            error=reason,
        )
        log.warning(
            "github_ops.forge_denied",
            op=op,
            caller=caller,
            forge_type=self._config.forge_type,
            repo=self._config.repo,
        )
        raise GitHubOpsDenied(reason)

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

    async def _resolve_label_ids(
        self,
        names: list[str],
        *,
        caller: str,
        ticket_uid: str = "",
        correlation_id: str = "",
    ) -> list[int]:
        """Translate label NAME strings to Forgejo integer IDs.

        Forgejo's create-issue ``labels`` field is ``[]int64`` — POSTing
        name strings SILENTLY drops them (the ``auto-fix`` label never
        lands → the whole auto-fix flow goes dark while looking healthy).
        Resolve via ``GET /repos/{owner}/{name}/labels`` once per client
        lifetime (cached), then translate. A name with no matching label
        is a LOUD warn (``github_ops.label_unresolved``) and is dropped
        from the POST — NEVER a silent drop. The config keeps ``label_map``
        as NAME strings (the auto-fix-is-bug-only invariant guard in
        ``load_github_config`` is string-keyed + load-bearing); the
        name→id translation lives here, at create time, not in config.
        """
        if not names:
            return []
        if self._label_id_cache is None:
            owner, _, name = self._config.repo.partition("/")
            data = await self._get_json(
                op="label_list",
                caller=caller,
                url=f"{self._config.api_base}/repos/{owner}/{name}/labels",
                correlation_id=correlation_id,
            )
            cache: dict[str, int] = {}
            if isinstance(data, list):
                for label in data:
                    if not isinstance(label, dict):
                        continue
                    lbl_name = str(label.get("name") or "").strip().lower()
                    lbl_id = label.get("id")
                    if (
                        lbl_name
                        and isinstance(lbl_id, int)
                        and not isinstance(lbl_id, bool)
                    ):
                        cache[lbl_name] = lbl_id
            self._label_id_cache = cache

        resolved: list[int] = []
        for raw_name in names:
            key = str(raw_name).strip().lower()
            label_id = self._label_id_cache.get(key)
            if label_id is None:
                # LOUD — a dropped label is a silent auto-fix loss otherwise.
                log.warning(
                    "github_ops.label_unresolved",
                    label=raw_name,
                    repo=self._config.repo,
                    ticket_uid=ticket_uid,
                    detail=(
                        "label name has no matching forge label id — it will "
                        "NOT be applied; create the label in the repo or fix "
                        "the config label_map (auto-fix loss is silent "
                        "otherwise)"
                    ),
                )
                continue
            resolved.append(label_id)
        return resolved

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
        """POST /repos/{repo}/issues — returns ``{number, html_url}``.

        Forge-aware labels. ``labels`` arrives as NAME strings:
          * GitHub — POSTed unchanged (names; byte-identical to pre-port).
          * Forgejo — its ``labels`` field is ``[]int64``, so the names
            are resolved to integer IDs via :meth:`_resolve_label_ids`
            before the POST. The create op is gated FIRST there (before
            the label_list pre-fetch) so a denied caller never triggers a
            labels round-trip.
        """
        if self._config.forge_type == FORGE_FORGEJO:
            # Gate up front: a denied caller audits ONE ``issue_create``
            # denied row and raises, never reaching the label_list
            # pre-fetch (``_request_audited`` re-gates below — a harmless
            # pass once allowed, no double audit on success).
            self._gate(
                "issue_create", caller,
                ticket_uid=ticket_uid, correlation_id=correlation_id,
            )
            json_labels: list[Any] = await self._resolve_label_ids(
                labels,
                caller=caller,
                ticket_uid=ticket_uid,
                correlation_id=correlation_id,
            )
        else:
            # GitHub: POST label NAME strings unchanged (the gate runs
            # inside ``_request_audited`` exactly as pre-port).
            json_labels = list(labels)
        resp = await self._request_audited(
            op="issue_create",
            caller=caller,
            method="POST",
            url=f"{self._config.api_base}/repos/{self._config.repo}/issues",
            json_body={
                "title": title,
                "body": body,
                "labels": json_labels,
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
        """Marker-based dedupe recovery — forge-aware dispatcher.

        GitHub: global ``/search/issues`` (``{"items": [...]}`` parse,
        mandatory ``is:issue`` query) — pre-port behavior, unchanged.
        Forgejo: per-repo ``/repos/{o}/{n}/issues`` (bare LIST,
        ``state=all``, client-side body-marker match is authoritative).
        Both return the first matching issue as ``{number, html_url,
        state}`` or ``None``.
        """
        if self._config.forge_type == FORGE_FORGEJO:
            return await self._issue_search_marker_forgejo(
                ticket_uid=ticket_uid,
                caller=caller,
                correlation_id=correlation_id,
            )
        return await self._issue_search_marker_github(
            ticket_uid=ticket_uid,
            caller=caller,
            correlation_id=correlation_id,
        )

    async def _issue_search_marker_github(
        self,
        *,
        ticket_uid: str,
        caller: str,
        correlation_id: str = "",
    ) -> dict[str, Any] | None:
        """GitHub marker dedupe — GET /search/issues (pre-port behavior).

        Searches issue bodies for the INNER marker text (GitHub's search
        index strips the HTML comment delimiters that :func:`issue_marker`
        wraps around it).

        The ``is:issue`` qualifier is MANDATORY: since GitHub's 2025
        search change, /search/issues 422s on any query without a type
        qualifier — ``{"message": "Query must include 'is:issue' or
        'is:pull-request'"}``. Observed live 2026-06-11 (60/60 audit
        rows, KAL-LE first ticket-pipeline tick). Do not drop it in a
        refactor.
        """
        query = (
            f'repo:{self._config.repo} is:issue in:body '
            f'"algernon-ticket: {ticket_uid}"'
        )
        resp = await self._request_audited(
            op="issue_search",
            caller=caller,
            method="GET",
            url=f"{self._config.api_base}/search/issues",
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

    async def _issue_search_marker_forgejo(
        self,
        *,
        ticket_uid: str,
        caller: str,
        correlation_id: str = "",
    ) -> dict[str, Any] | None:
        """Forgejo marker dedupe — per-repo issue search.

        Forgejo has no global ``/search/issues`` returning ``{"items":
        [...]}``; it exposes a per-repo ``GET /repos/{owner}/{name}/
        issues`` that returns a **bare LIST**. Three stacked correctness
        requirements (a miss on any one re-mints a DUPLICATE issue):

        1. **Parse the response as a LIST directly** — NOT
           ``data.get("items")`` (which would be ``None`` on a list →
           every ticket re-files a duplicate).
        2. **``state=all``** — Forgejo defaults to ``state=open``; a
           closed / wont_fix ticket must still be found so it isn't
           re-minted.
        3. **Client-side body match is the AUTHORITATIVE gate** — the
           ``q={ticket_uid}`` param is only a coarse keyword pre-filter
           (Forgejo's index may not tokenize the HTML-comment marker
           reliably). The dedupe decision is a substring match of the
           FULL marker (:func:`issue_marker`) against each returned
           ``issue["body"]``.

        Returns the first body-matched issue as ``{number, html_url,
        state}`` or ``None`` on no match.
        """
        marker = issue_marker(ticket_uid)
        owner, _, name = self._config.repo.partition("/")
        resp = await self._request_audited(
            op="issue_search",
            caller=caller,
            method="GET",
            url=f"{self._config.api_base}/repos/{owner}/{name}/issues",
            params={
                "type": "issues",   # exclude PRs from the issue index
                "state": "all",     # a closed/wont_fix ticket must still match
                "q": ticket_uid,    # coarse pre-filter ONLY — see body gate below
                "limit": 50,
            },
            ticket_uid=ticket_uid,
            correlation_id=correlation_id,
        )
        data = resp.json()
        # Forgejo returns a BARE LIST (NOT GitHub's {"items": [...]}).
        issues = data if isinstance(data, list) else []
        # AUTHORITATIVE dedupe gate: the full HTML-comment marker must be
        # present in the issue body. ``q=`` is best-effort; this is the
        # real join key.
        matches = [
            issue
            for issue in issues
            if isinstance(issue, dict) and marker in str(issue.get("body") or "")
        ]
        first = matches[0] if matches else None
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
            # match_count = authoritative body matches; prefilter_count =
            # what the coarse q= returned (diagnoses an over-aggressive
            # keyword index that hides a marker-bearing issue).
            extra={
                "match_count": len(matches),
                "prefilter_count": len(issues),
            },
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
            url=(
                f"{self._config.api_base}/repos/{self._config.repo}"
                f"/issues/{number}"
            ),
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

        Returns the issue's timeline comments. On Forgejo each entry
        carries a ``type`` (cross-ref values ``pull_ref|issue_ref|
        comment_ref|commit_ref``) and a ``ref_issue`` — the digest's
        :func:`alfred.brief.kalle_digest._first_cross_referenced_pr`
        reads those to discover the linked PR.
        """
        return await self._get_json(
            op="issue_timeline",
            caller=caller,
            url=(
                f"{self._config.api_base}/repos/{self._config.repo}"
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
            url=(
                f"{self._config.api_base}/repos/{self._config.repo}"
                f"/pulls/{number}"
            ),
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
                f"{self._config.api_base}/repos/{self._config.repo}"
                f"/pulls/{number}/reviews"
            ),
            correlation_id=correlation_id,
        )

    # --- on-box auto-fix drafter ops (Phase 1B, fix_drafter, FORGEJO) --------

    async def issue_list(
        self,
        *,
        labels: str,
        state: str,
        caller: str,
        correlation_id: str = "",
    ) -> list[dict[str, Any]]:
        """GET /repos/{repo}/issues — the open auto-fix work queue.

        FORGEJO-ONLY (forge-fenced). Forgejo shape-divergence, same
        silent-break class as :meth:`_issue_search_marker_forgejo`: the
        response is a **BARE LIST**, NOT GitHub's ``{"items": [...]}``.
        Parsing ``data.get("items")`` on a list returns ``None`` → the
        drafter would see an empty queue forever (silent dark). ``type=
        issues`` excludes PRs from the issue index; ``labels`` is the
        comma-name filter (``"auto-fix"``); ``state`` is the lifecycle
        filter (``"open"``). Returns the list of issue dicts (each with
        ``number``/``title``/``body``/``labels``); empty list on no match.
        """
        self._forge_fence(
            "issue_list", caller, correlation_id=correlation_id,
        )
        resp = await self._request_audited(
            op="issue_list",
            caller=caller,
            method="GET",
            url=f"{self._config.api_base}/repos/{self._config.repo}/issues",
            params={
                "type": "issues",   # exclude PRs from the issue index
                "state": state,
                "labels": labels,
                "limit": 50,
            },
            correlation_id=correlation_id,
        )
        data = resp.json()
        # Forgejo returns a BARE LIST (NOT GitHub's {"items": [...]}).
        issues = [i for i in data if isinstance(i, dict)] if isinstance(data, list) else []
        append_github_audit(
            self._config.audit_log_path,
            op="issue_list",
            repo=self._config.repo,
            caller=caller,
            outcome="ok",
            http_status=resp.status_code,
            correlation_id=correlation_id,
            extra={"count": len(issues)},
        )
        return issues

    async def pr_list(
        self,
        *,
        state: str,
        caller: str,
        correlation_id: str = "",
    ) -> list[dict[str, Any]]:
        """GET /repos/{repo}/pulls — crash-recovery dedup source.

        FORGEJO-ONLY (forge-fenced). BARE LIST parse (same firebreak as
        :meth:`issue_list`). ``state="all"`` so a closed/merged auto-fix
        PR is still found (never re-draft a landed fix). The caller
        filters ``head.ref`` client-side. Returns the list of PR dicts
        (each with ``number``/``html_url``/``head``); empty on no match.
        """
        self._forge_fence(
            "pr_list", caller, correlation_id=correlation_id,
        )
        resp = await self._request_audited(
            op="pr_list",
            caller=caller,
            method="GET",
            url=f"{self._config.api_base}/repos/{self._config.repo}/pulls",
            params={"state": state, "limit": 50},
            correlation_id=correlation_id,
        )
        data = resp.json()
        prs = [p for p in data if isinstance(p, dict)] if isinstance(data, list) else []
        append_github_audit(
            self._config.audit_log_path,
            op="pr_list",
            repo=self._config.repo,
            caller=caller,
            outcome="ok",
            http_status=resp.status_code,
            correlation_id=correlation_id,
            extra={"count": len(prs)},
        )
        return prs

    async def pr_create(
        self,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
        caller: str,
        issue_number: int | None = None,
        correlation_id: str = "",
    ) -> dict[str, Any]:
        """POST /repos/{repo}/pulls — the ONE PR write op. FORGEJO-ONLY.

        Forge-fenced: a github-config client raises
        :class:`GitHubOpsDenied` before any HTTP (GitHub Actions owns PR
        creation there). Body shape ``{head, base, title, body}`` —
        Forgejo's ``CreatePullRequestOption`` has NO ``draft`` boolean,
        so WIP-ness is signaled by the caller's ``"WIP: "`` title prefix
        (cosmetic; the HARD never-auto-merge guarantee is branch
        protection + the permanent ``pr_merge``-deny, not the prefix).
        Returns ``{number, html_url}``; audited ``outcome="created"``.
        """
        self._forge_fence(
            "pr_create", caller,
            correlation_id=correlation_id, issue_number=issue_number,
        )
        resp = await self._request_audited(
            op="pr_create",
            caller=caller,
            method="POST",
            url=f"{self._config.api_base}/repos/{self._config.repo}/pulls",
            json_body={
                "head": head,
                "base": base,
                "title": title,
                "body": body,
            },
            correlation_id=correlation_id,
        )
        data = resp.json()
        number = data.get("number")
        append_github_audit(
            self._config.audit_log_path,
            op="pr_create",
            repo=self._config.repo,
            caller=caller,
            outcome="created",
            issue_number=issue_number,
            http_status=resp.status_code,
            correlation_id=correlation_id,
            extra={"pr_number": number, "head": head, "base": base},
        )
        return {"number": number, "html_url": data.get("html_url", "")}


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
    "build_client_for_repo",
    "build_github_client",
    "issue_marker",
    "PROJECT_MARKER_TEMPLATE",
    "parse_project_marker",
    "project_marker",
    "load_github_config",
    "read_github_audit",
]
