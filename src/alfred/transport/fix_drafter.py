"""KAL-LE on-box auto-fix drafter daemon (pipeline Phase 1B, FORGEJO-ONLY).

The Forgejo replacement for GitHub's ``claude-auto-fix.yml`` GH-Action.
On the sovereign Forgejo box there is no GitHub Actions, so KAL-LE itself
authors the draft fix PR: this daemon — the structural mirror of
``ticket_forward`` — scans open ``auto-fix``-labeled Forgejo issues every
``interval_minutes``, drafts a minimal fix inside a throwaway shallow
clone via a SANDBOXED ``claude`` run, pushes ``auto-fix/issue-N``, and
opens a draft PR (``Closes #N``). The operator is the only merge
authority (branch protection + the permanent ``pr_merge`` matrix-deny).

TWO-ZONE CONTAINMENT (the security spine, design facet 3):
  * TRUSTED zone — this daemon. Holds the Forgejo token; owns the ENTIRE
    mutating git lifecycle (ls-remote, clone, branch, add, commit, push)
    and the REST PR-open. The token lives only in an ephemeral 0600
    gitconfig referenced via ``GIT_CONFIG_GLOBAL`` — never in argv
    (ps-visible), never persisted to ``.git/config`` (the remote URL is
    token-less), never in the model's environment.
  * SANDBOXED zone — the ``claude`` model run, launched ONLY through the
    hardened ``systemd-run`` invocation built by :func:`build_sandbox_command`
    (``User=kalle-drafter``, ``ProtectHome=tmpfs``, ``IPAddressDeny=any``
    + localhost-only egress to the Anthropic proxy, empty caps, a
    dedicated drafter Anthropic key passed by NAME so it never hits argv).
    The model only EDITS files in the clone working tree — it cannot
    push, open PRs, merge, reach ``main``, read PHI, or hold the token.
    There is NO bare-invoke path (pinned by test) — the daemon refuses to
    run the model except through the hardened unit.

NEVER HALF-OPEN: a PR is only ever opened against a fully-pushed branch.
A crash between push and PR-open is resumable (state ``branch_pushed`` →
next tick resumes at ``pr_create`` only, no re-clone/re-draft). Ground
truth (``git ls-remote`` + ``pr_list``) is authoritative over local
state; state is deletable bookkeeping.

TRIPLE-GATE (FORGEJO-ONLY): (1) the orchestrator auto-start gate requires
``fix_drafter.enabled AND github.forge_type == "forgejo"``; (2) the
daemon runner ``_run_fix_drafter`` ``sys.exit(78)``s unless enabled +
forgejo + instance/work_root/vera_vault_root present; (3) the op-layer
forge-fence (``github_ops._forge_fence``) raises on a github client. On
master (github config, no ``fix_drafter:`` block) it never runs — the
code ships INERT.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .utils import get_logger

log = get_logger(__name__)


# Tool-scoped default per the CLAUDE.md state-path rule.
DEFAULT_FIX_DRAFTER_STATE_PATH = "./data/fix_drafter_state.json"

# Branch name the drafter owns; N is the integer issue number. The push
# refspec + the regex assertion both key on this exact shape.
_BRANCH_REGEX = re.compile(r"^auto-fix/issue-\d+$")

# The auto-fix label re-verify gate's join key (the single home of the
# auto-fix-is-bug-only invariant is intake; this is the drafter's own
# defensive re-check that holds even if a future trigger misfires).
_AUTO_FIX_LABEL = "auto-fix"

# The caller identity in the github_ops matrix (op×caller gate).
_CALLER = "fix_drafter"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


# The model's allowedTools (the first-line filter; the OS sandbox is the
# real boundary). File edits + READ-ONLY git introspection + LOCAL test
# runners. NO bare Bash, NO push/remote/fetch/clone, NO gh, NO network —
# and deliberately NO ``git add`` / ``git commit``: the DAEMON owns the
# entire mutating git lifecycle (the model only edits files), so granting
# the model commit-authority would be dead + misleading (``git commit`` is
# also hard-denied by the PreToolUse hook — pinned by test). The model's
# git surface is purely diagnostic (status/diff/log/show).
_DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = (
    "Read", "Edit", "Write", "Glob", "Grep",
    "Bash(pytest:*)", "Bash(npm test:*)", "Bash(npm run:*)",
    "Bash(npx vitest:*)", "Bash(tsc:*)",
    "Bash(git status:*)", "Bash(git diff:*)",
    "Bash(git log:*)", "Bash(git show:*)",
)


@dataclass
class ProjectConfig:
    """One entry of ``fix_drafter.projects`` (Option B, central bug-intake).

    Maps a project ``slug`` (stamped into the tracker issue via the
    ``algernon-project`` marker) → the APP repo the drafter opens the fix PR
    against. The app repo may be a DIFFERENT forge than the central sovereign
    tracker (e.g. GitHub app code + Forgejo tracker). ``token_env`` names the
    env var holding that repo's push/PR credential (never a literal)."""

    slug: str = ""
    repo: str = ""
    clone_base_url: str = ""   # empty → falls back to fix_drafter.clone_base_url
    forge_type: str = "github"
    api_base: str = ""         # REST base for the app-repo client (forgejo MUST set)
    base_branch: str = ""      # empty → falls back to fix_drafter.base_branch
    token_env: str = ""


@dataclass
class ProjectTarget:
    """The RESOLVED app-repo target for one issue: everything the drafter
    needs to clone/push/open-PR against the app repo. Built per-issue from
    the project marker (:func:`_resolve_project_target`). In the single-repo
    (no ``projects``) case it is derived from the central client → BYTE-
    IDENTICAL to today's behavior."""

    slug: str
    repo: str
    clone_base_url: str
    forge_type: str
    api_base: str
    base_branch: str
    token: str          # resolved credential value (never logged)
    client: Any         # app-repo github_ops client (pr_create / pr_list)

    def clone_url(self) -> str:
        # token-less remote — auth rides the ephemeral gitconfig extraHeader.
        return f"{self.clone_base_url.rstrip('/')}/{self.repo}.git"


@dataclass
class FixDrafterConfig:
    """Typed view of the ``fix_drafter:`` config section (KAL-LE-only).

    Security-critical, per-instance fields (``work_root``,
    ``vera_vault_root``) have NO safe default — empty fails loud at the
    daemon startup gate rather than guessing a wrong path (per the
    fail-loud-on-empty rule). The clone credential + repo + api_base come
    from the shared ``github:`` client (single validated source).

    Option B (central bug-intake): ``projects`` (slug → :class:`ProjectConfig`)
    + ``default_project`` decouple the drafter's PR target from the scan
    source. ABSENT/EMPTY ``projects`` == today's single-repo behavior against
    ``github.repo``, BYTE-IDENTICAL (the zero-config fallback).
    """

    enabled: bool = False
    instance: str = ""
    interval_minutes: int = 5
    # git http root (NOT the /api/v1 REST base — that's github.api_base).
    clone_base_url: str = "http://127.0.0.1:3001"
    base_branch: str = "main"
    branch_prefix: str = "auto-fix/issue-"
    # Throwaway clone root — MUST be outside /home so ProtectHome=tmpfs
    # doesn't mask it (ReadWritePaths re-exposes it). Empty → fail loud.
    work_root: str = ""
    # DAEMON-ONLY dir for the ephemeral Anthropic-key EnvironmentFile (0700,
    # daemon-owned). MUST NOT be under work_root or the audit dir (both are
    # sandbox-writable). Empty → derived as <work_root parent>/keys (e.g.
    # /var/lib/kalle-drafter/keys). The unit reads the 0600 keyfile as ROOT
    # via ``-p EnvironmentFile=`` (survives sudo env_reset); the value never
    # crosses the sudo env boundary and never lands in argv.
    keyfile_dir: str = ""
    claude_command: str = "claude"
    claude_timeout: int = 1200
    git_timeout: int = 300
    claude_allowed_tools: list[str] = field(
        default_factory=lambda: list(_DEFAULT_ALLOWED_TOOLS)
    )
    max_empty_diff_retries: int = 2
    # Persistent non-empty-diff failures (clone/model/push looping — e.g. a
    # branch-protection misconfig blocking the push) latch needs_human after
    # this many total attempts, so a stuck issue can't burn Claude minutes
    # forever (the empty-diff path has its own separate count-based latch).
    max_attempts: int = 5
    state_path: str = DEFAULT_FIX_DRAFTER_STATE_PATH
    # REST-plane audit (issue_get/issue_list/pr_list/pr_create) — defaults
    # to the github client's own audit path when empty. AUTHORITATIVE +
    # daemon-written; MUST live OUTSIDE every sandbox-writable dir (asserted
    # fail-closed in run_drafter_once).
    audit_log_path: str = ""
    # git/bash-plane audit (the sandbox PreToolUse hook). Its PARENT must be
    # a DEDICATED dir holding ONLY this file — it is the one sandbox-writable
    # dir besides the clone (ProtectHome masks /home; ReadWritePaths punches
    # back), so the model (same UID) CAN tamper with this best-effort
    # per-command log. That is acceptable BY DESIGN: it is NOT authoritative.
    # The authoritative records (drafter state, the REST github_ops audit,
    # the daemon log) are daemon-written OUTSIDE the sandbox. Keeping this in
    # its own dir is what lets the fail-closed assertion guarantee
    # ReadWritePaths never re-exposes state / the REST audit.
    hook_audit_path: str = "./data/fix_drafter_audit/fix_drafter_bash_audit.jsonl"
    commit_author_name: str = "kalle-bot"
    commit_author_email: str = "kalle-bot@algernon.local"
    # --- sandbox / security (infra-deployed at cutover; referenced here) ---
    sandbox_user: str = "kalle-drafter"
    sandbox_group: str = "kalle-drafter"
    # Prepended to the systemd-run argv if infra needs a privilege wrapper
    # (e.g. ["sudo"] or a narrow polkit shim). Default empty = bare call.
    sandbox_launch_prefix: list[str] = field(default_factory=list)
    sandbox_memory_max: str = "4G"
    sandbox_tasks_max: int = 256
    # localhost Anthropic forward-proxy (CONNECT-allowlists api.anthropic.com).
    anthropic_proxy_url: str = "http://127.0.0.1:8119"
    # Env var holding the DEDICATED drafter Anthropic key (NEVER hardcode
    # the value; referenced by name and imported into the unit by name).
    drafter_key_env: str = "ALGERNON_KALLE_DRAFTER_ANTHROPIC_KEY"
    # VERA's PHI vault root — disjointness assertion + InaccessiblePaths.
    # Empty → fail loud (can't prove the clone is disjoint from PHI).
    vera_vault_root: str = ""
    # Box .env path — belt-and-suspenders InaccessiblePaths entry.
    box_env_path: str = ""
    # --- Option B: cross-repo routing (ABSENT = single-repo byte-identical) ---
    # slug -> ProjectConfig. Empty → the drafter targets github.repo (the
    # central client) for every issue, exactly as today.
    projects: dict = field(default_factory=dict)
    default_project: str = ""


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_fix_drafter_config(raw: dict[str, Any]) -> FixDrafterConfig:
    """Build :class:`FixDrafterConfig` from the unified config dict.

    Tolerant of an absent block (returns all-default, ``enabled=False``)
    so the code is byte-inert on master. ``state.path`` nests under a
    ``state:`` sub-block (the tool-state convention); the
    ``_DATACLASS_MAP`` collision footgun is avoided because this loader is
    hand-rolled (not routed through the generic ``_build``).
    """
    section = raw.get("fix_drafter") or {}
    if not isinstance(section, dict):
        return FixDrafterConfig()

    state_raw = section.get("state") or {}
    state_path = ""
    if isinstance(state_raw, dict):
        state_path = str(state_raw.get("path", "") or "")

    claude_raw = section.get("claude") or {}
    claude_timeout = 1200
    if isinstance(claude_raw, dict):
        claude_timeout = _coerce_int(claude_raw.get("timeout", 1200), 1200)
        allowed = claude_raw.get("allowed_tools")
    else:
        allowed = None
    allowed_tools = (
        [str(t) for t in allowed]
        if isinstance(allowed, list) and allowed
        else list(_DEFAULT_ALLOWED_TOOLS)
    )

    sandbox_raw = section.get("sandbox") or {}
    if not isinstance(sandbox_raw, dict):
        sandbox_raw = {}
    launch_prefix_raw = sandbox_raw.get("launch_prefix")
    launch_prefix = (
        [str(t) for t in launch_prefix_raw]
        if isinstance(launch_prefix_raw, list)
        else []
    )

    # HAND-ROLLED (Option B): the ``projects`` map is a dict-of-dicts; routing
    # it through the generic ``_build`` would collide on ``_DATACLASS_MAP``
    # keys, per CLAUDE.md. Parse each entry into a ProjectConfig; a malformed
    # entry (no slug/repo) is skipped (tolerant loader). Absent → {} → the
    # single-repo fallback in ``_resolve_project_target``.
    projects_raw = section.get("projects") or {}
    projects: dict[str, ProjectConfig] = {}
    if isinstance(projects_raw, dict):
        for slug, praw in projects_raw.items():
            if not isinstance(praw, dict):
                continue
            slug_s = str(slug or "")
            repo_s = str(praw.get("repo", "") or "")
            if not slug_s or not repo_s:
                continue
            projects[slug_s] = ProjectConfig(
                slug=slug_s,
                repo=repo_s,
                clone_base_url=str(praw.get("clone_base_url", "") or ""),
                forge_type=str(praw.get("forge_type", "github") or "github"),
                api_base=str(praw.get("api_base", "") or ""),
                base_branch=str(praw.get("base_branch", "") or ""),
                token_env=str(praw.get("token_env", "") or ""),
            )

    return FixDrafterConfig(
        enabled=bool(section.get("enabled", False)),
        instance=str(section.get("instance", "") or ""),
        interval_minutes=_coerce_int(section.get("interval_minutes", 5), 5),
        clone_base_url=str(
            section.get("clone_base_url", "http://127.0.0.1:3001")
            or "http://127.0.0.1:3001"
        ),
        base_branch=str(section.get("base_branch", "main") or "main"),
        branch_prefix=str(
            section.get("branch_prefix", "auto-fix/issue-")
            or "auto-fix/issue-"
        ),
        work_root=str(section.get("work_root", "") or ""),
        keyfile_dir=str(section.get("keyfile_dir", "") or ""),
        claude_command=str(section.get("claude_command", "claude") or "claude"),
        claude_timeout=claude_timeout,
        git_timeout=_coerce_int(section.get("git_timeout", 300), 300),
        claude_allowed_tools=allowed_tools,
        max_empty_diff_retries=_coerce_int(
            section.get("max_empty_diff_retries", 2), 2
        ),
        max_attempts=_coerce_int(section.get("max_attempts", 5), 5),
        state_path=state_path or DEFAULT_FIX_DRAFTER_STATE_PATH,
        audit_log_path=str(section.get("audit_log_path", "") or ""),
        hook_audit_path=str(
            section.get("hook_audit_path", "")
            or "./data/fix_drafter_audit/fix_drafter_bash_audit.jsonl"
        ),
        commit_author_name=str(
            section.get("commit_author_name", "kalle-bot") or "kalle-bot"
        ),
        commit_author_email=str(
            section.get("commit_author_email", "kalle-bot@algernon.local")
            or "kalle-bot@algernon.local"
        ),
        sandbox_user=str(sandbox_raw.get("user", "kalle-drafter") or "kalle-drafter"),
        sandbox_group=str(sandbox_raw.get("group", "kalle-drafter") or "kalle-drafter"),
        sandbox_launch_prefix=launch_prefix,
        sandbox_memory_max=str(sandbox_raw.get("memory_max", "4G") or "4G"),
        sandbox_tasks_max=_coerce_int(sandbox_raw.get("tasks_max", 256), 256),
        anthropic_proxy_url=str(
            sandbox_raw.get("anthropic_proxy_url", "http://127.0.0.1:8119")
            or "http://127.0.0.1:8119"
        ),
        drafter_key_env=str(
            sandbox_raw.get("drafter_key_env", "ALGERNON_KALLE_DRAFTER_ANTHROPIC_KEY")
            or "ALGERNON_KALLE_DRAFTER_ANTHROPIC_KEY"
        ),
        vera_vault_root=str(sandbox_raw.get("vera_vault_root", "") or ""),
        box_env_path=str(sandbox_raw.get("box_env_path", "") or ""),
        projects=projects,
        default_project=str(section.get("default_project", "") or ""),
    )


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class FixDrafterEntry:
    """Per-issue drafter bookkeeping, keyed by ``issue_number``.

    ``status`` lifecycle: ``"" → drafting → branch_pushed → pr_open`` (or
    the terminal ``needs_human`` latch). ``branch_pushed`` is the
    never-half-open resume point — a crash there resumes at ``pr_create``.
    """

    issue_number: int = 0
    branch: str = ""
    status: str = ""  # "" | drafting | branch_pushed | pr_open | needs_human
    first_attempt_at: str = ""
    last_attempt_at: str = ""
    attempts: int = 0
    empty_diff_count: int = 0
    pr_number: int | None = None
    pr_url: str = ""
    # Option B cross-repo linkage: the issue lives on the CENTRAL tracker
    # (this entry's key), but the PR lives on the APP repo. These make the
    # linkage SELF-DESCRIBING so the effectiveness loop can find + poll the
    # cross-repo PR (see :func:`load_pr_links` + the kalle_digest seam).
    app_repo: str = ""
    app_forge_type: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "FixDrafterEntry":
        """Load-time schema-tolerance contract (per CLAUDE.md)."""
        known = {
            k: v for k, v in data.items() if k in cls.__dataclass_fields__
        }
        return cls(**known)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FixDrafterState:
    """Drafter state — ``str(issue_number)`` → :class:`FixDrafterEntry`.

    Atomic save (``.tmp`` → rename); defensive load (missing file →
    empty; corrupt file → log + empty). State loss is recoverable: the
    git/PR ground-truth dedup (ls-remote + pr_list) re-derives linkage and
    never double-opens a PR.
    """

    path: Path
    entries: dict[str, FixDrafterEntry] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "FixDrafterState":
        p = Path(path)
        if not p.exists():
            return cls(path=p)
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            log.warning(
                "fix_drafter.state_load_failed",
                path=str(p),
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
            return cls(path=p)
        entries_raw = data.get("entries") if isinstance(data, dict) else None
        entries: dict[str, FixDrafterEntry] = {}
        if isinstance(entries_raw, dict):
            for key, entry_data in entries_raw.items():
                if isinstance(entry_data, dict):
                    entries[str(key)] = FixDrafterEntry.from_dict(entry_data)
        return cls(path=p, entries=entries)

    def save(self) -> None:
        """Atomic write — ``.tmp`` then ``os.replace`` rename."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "entries": {
                key: entry.to_dict() for key, entry in self.entries.items()
            },
        }
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
        os.replace(tmp_path, self.path)


def load_pr_links(state_path: str | Path) -> dict[int, dict[str, Any]]:
    """The CROSS-REPO LINKAGE reader (Option B effectiveness-loop seam).

    Under Option B the issue lives on the CENTRAL tracker but the fix PR
    lives on the APP repo, so ``Closes #N`` / the tracker timeline can NEVER
    surface it — ``kalle_digest._first_cross_referenced_pr`` would return
    None → every ticket reports false ``stalled`` forever. The drafter writes
    the linkage into its OWN state as the durable write-back; this reader
    exposes it keyed by CENTRAL issue number:

        {issue_number: {pr_number, pr_url, app_repo, app_forge_type, status}}

    kalle_digest's ``_check_one_ticket_outcome`` should consult this (by the
    drafter ``state.path``) for a cross-repo entry and poll ``pr_get``/
    ``pr_reviews`` via a per-APP-repo client (``build_client_for_repo`` with
    the entry's ``app_repo``/``app_forge_type``) instead of the central
    client + timeline. Only entries that actually opened a PR are returned."""
    state = FixDrafterState.load(state_path)
    out: dict[int, dict[str, Any]] = {}
    for key, entry in state.entries.items():
        if entry.pr_number is None:
            continue
        try:
            n = int(key)
        except (TypeError, ValueError):
            continue
        out[n] = {
            "pr_number": entry.pr_number,
            "pr_url": entry.pr_url,
            "app_repo": entry.app_repo,
            "app_forge_type": entry.app_forge_type,
            "status": entry.status,
        }
    return out


def poll_client_for_app_repo(
    config: FixDrafterConfig,
    app_repo: str,
    app_forge_type: str = "github",
    *,
    audit_log_path: str = "",
) -> Any | None:
    """Build a READ-poll github_ops client for an app repo named in a
    cross-repo PR link (Option B effectiveness-loop consumer, C4).

    kalle_digest's ``_check_one_ticket_outcome`` calls this to poll
    ``pr_get``/``pr_reviews`` on the APP repo (where the fix PR actually
    lives) instead of the central tracker, so a cross-repo ticket resolves
    to its real disposition rather than a false ``stalled``. Resolves the
    matching ``fix_drafter.projects`` entry (by ``repo``) for its
    ``token_env`` + ``api_base``, then delegates to
    :func:`build_client_for_repo` — the SAME per-app-repo client the drafter
    opens the PR with, minus the mutating ops (the ``GITHUB_OPS`` matrix
    still gates every call; ``pr_merge`` stays permanently denied).

    Fail-SOFT (returns None, logged) rather than raising when the app repo
    is not a configured project or its token env is empty — the digest then
    falls back to the same-repo timeline path (reporting ``stalled`` only if
    that also finds nothing), never crashing the effectiveness pass.
    """
    from alfred.integrations.github_ops import (
        GITHUB_API_BASE,
        build_client_for_repo,
    )

    if not app_repo:
        return None
    proj = next(
        (
            p for p in config.projects.values()
            if getattr(p, "repo", "") == app_repo
        ),
        None,
    )
    if proj is None:
        # A cross-repo link names an app_repo with no matching project —
        # can't resolve a poll credential. Loud (a real config gap) but
        # non-fatal: the caller falls back to the same-repo timeline.
        log.warning(
            "fix_drafter.poll_client_unknown_app_repo",
            app_repo=app_repo,
            detail=(
                "a cross-repo PR link names an app_repo with no matching "
                "fix_drafter.projects entry — cannot resolve a poll token; "
                "the digest falls back to the same-repo timeline path"
            ),
        )
        return None
    token = _resolve_env_var(config, proj.token_env)
    if not token:
        log.warning(
            "fix_drafter.poll_client_missing_token",
            app_repo=app_repo,
            token_env=proj.token_env,
            detail=(
                "the app repo's project token env is empty — cannot poll the "
                "cross-repo PR (needs the box .env credential); the digest "
                "falls back to the same-repo timeline path"
            ),
        )
        return None
    forge = app_forge_type or proj.forge_type or "github"
    api_base = proj.api_base or (
        GITHUB_API_BASE if forge == "github" else ""
    )
    return build_client_for_repo(
        repo=app_repo,
        pat=token,
        forge_type=forge,
        api_base=api_base,
        audit_log_path=audit_log_path or config.audit_log_path,
        instance=config.instance,
    )


# ---------------------------------------------------------------------------
# Subprocess choke point — the single mockable point for git + the model
# ---------------------------------------------------------------------------


async def _run_subprocess(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout: float | None = None,
    cwd: str | None = None,
    umask: int | None = None,
) -> tuple[int, str, str]:
    """Run a subprocess; return ``(returncode, stdout, stderr)``.

    The SINGLE choke point for every git command AND the sandboxed model
    run, so tests mock exactly one function. A timeout returns code 124
    (matching ``timeout(1)``) with a synthetic stderr rather than raising,
    so the per-issue containment treats it as a loud failure.

    ``umask`` (when set) applies ONLY in the forked child via
    ``preexec_fn`` — it is CHILD-SCOPED and never touches the daemon's
    process-global umask. The clone step passes ``0o002`` so the working
    tree is group-writable (the cross-UID bridge: andrew clones,
    kalle-drafter edits via the shared group) WITHOUT leaking group-write
    onto the daemon's own state / REST-audit writes (which never route
    through here). os.umask is async-signal-safe → preexec_fn-safe.
    """
    preexec = (lambda: os.umask(umask)) if umask is not None else None
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE if input_text is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=preexec,
    )
    stdin_bytes = input_text.encode("utf-8") if input_text is not None else None
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(input=stdin_bytes), timeout=timeout
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return 124, "", f"timeout after {timeout}s"
    return (
        proc.returncode if proc.returncode is not None else -1,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )


# ---------------------------------------------------------------------------
# PreToolUse hook — the per-command audit + second gate behind allowedTools
# ---------------------------------------------------------------------------


# Self-contained gate script (GENERATED per run). Placeholders are filled
# by string-replace (NOT .format()) to avoid brace-escaping the body. It
# runs inside the home-masked sandbox where the alfred package is
# unreachable, so the denylist is baked in at write time (single source of
# truth: bash_exec._COMMAND_DENYLIST_SUBSTRINGS).
_HOOK_SCRIPT_TEMPLATE = '''#!/usr/bin/env python3
"""PreToolUse gate for the fix_drafter sandboxed model run (GENERATED).

Re-checks every Bash command against the bash_exec denylist substrings
and HARD-DENIES (exit 2 blocks the Claude Code tool call) on a hit,
appending one JSONL audit row (bash_exec shape) per command. allowedTools
is a first-line filter, NOT a boundary; this hook is the per-command
audit + second gate. Self-contained: runs inside the home-masked sandbox.
"""
import json
import sys
from datetime import datetime, timezone

_DENYLIST = __DENYLIST__
_AUDIT_PATH = __AUDIT_PATH__


def _audit(command, decision, match):
    try:
        with open(_AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "phase": "model_bash",
                "command": command,
                "decision": decision,
                "match": match,
            }) + "\\n")
    except OSError:
        pass


def main():
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        # Unparseable hook input — fail CLOSED.
        print("fix_drafter hook: unparseable PreToolUse input", file=sys.stderr)
        sys.exit(2)
    if not isinstance(payload, dict):
        sys.exit(2)
    if payload.get("tool_name") != "Bash":
        # Non-Bash tools are governed by allowedTools; allow.
        sys.exit(0)
    tool_input = payload.get("tool_input") or {}
    command = str(tool_input.get("command") or "")
    lowered = command.lower()
    for bad in _DENYLIST:
        if bad in lowered:
            _audit(command, "denied", bad)
            print("fix_drafter hook: command denied (matched %r)" % bad,
                  file=sys.stderr)
            sys.exit(2)
    _audit(command, "allowed", "")
    sys.exit(0)


if __name__ == "__main__":
    main()
'''


def write_drafter_hook_files(
    target_dir: str | Path,
    *,
    audit_path: str,
) -> tuple[str, str]:
    """Write the PreToolUse gate script + drafter ``settings.json``.

    Returns ``(hook_path, settings_path)``. The denylist substrings are
    baked into the script at write time from
    ``bash_exec._COMMAND_DENYLIST_SUBSTRINGS`` (single source of truth).
    The hook hard-denies (exit 2) on a denylist hit and appends one JSONL
    audit row per Bash command. ``target_dir`` should be READ-ONLY to the
    sandbox (not in ReadWritePaths) so the model can't tamper with its own
    gate; the daemon writes it before launching the unit.
    """
    from alfred.telegram.bash_exec import _COMMAND_DENYLIST_SUBSTRINGS

    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    hook_path = target / "fix_drafter_pretooluse.py"
    settings_path = target / "fix_drafter_settings.json"

    script = (
        _HOOK_SCRIPT_TEMPLATE
        .replace("__DENYLIST__", repr(tuple(_COMMAND_DENYLIST_SUBSTRINGS)))
        .replace("__AUDIT_PATH__", json.dumps(str(audit_path)))
    )
    hook_path.write_text(script, encoding="utf-8")
    os.chmod(hook_path, 0o755)

    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"python3 {hook_path}",
                        }
                    ],
                }
            ]
        }
    }
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return str(hook_path), str(settings_path)


# ---------------------------------------------------------------------------
# Hardened sandbox invocation (the model run's only entry point)
# ---------------------------------------------------------------------------


def _resolve_drafter_key(config: FixDrafterConfig) -> str:
    """Resolve the dedicated drafter Anthropic key ROBUSTLY.

    The fix_drafter daemon runs in a RE-EXEC'd child that does NOT inherit
    the main process's runtime ``os.environ`` — where ``auto_load_dotenv``
    injected the box ``.env``. So ``os.environ.get(drafter_key_env)`` is
    empty in the daemon, and claude would run un-authed ("Not logged in").
    Resolution order:
      1. ``os.environ`` (respects an explicit env override, if inherited).
      2. the box ``.env`` file directly (``load_dotenv_file`` — the SAME
         source alfred uses for config substitution). The daemon CAN read
         it; ``box_env_path`` is only ``InaccessiblePaths`` for the SANDBOX.
    Returns "" when the key is nowhere (the caller fails loud).
    """
    return _resolve_env_var(config, config.drafter_key_env)


def _resolve_env_var(config: FixDrafterConfig, var_name: str) -> str:
    """Resolve a named env var ROBUSTLY (os.environ → the box .env).

    The re-exec'd daemon doesn't inherit the main process's runtime
    ``os.environ`` (where ``auto_load_dotenv`` injected the box .env), so a
    per-project push credential (``ProjectConfig.token_env``) — like the
    drafter key — must fall back to reading ``box_env_path`` directly (the
    daemon CAN; it's ``InaccessiblePaths`` for the SANDBOX only). Returns ""
    when nowhere (the caller fails loud)."""
    if not var_name:
        return ""
    val = os.environ.get(var_name, "")
    if val:
        return val
    if config.box_env_path:
        from alfred._env import load_dotenv_file
        env = load_dotenv_file(config.box_env_path)  # never raises; {} on missing
        return str(env.get(var_name, "") or "")
    return ""


# ---------------------------------------------------------------------------
# Option B — per-issue project-target resolution (cross-repo, cross-forge)
# ---------------------------------------------------------------------------


def _target_from_central(central_client: Any, config: FixDrafterConfig) -> ProjectTarget:
    """Single-repo (no ``projects``) target — derived ENTIRELY from the
    central client + config, so clone/push/pr_create all hit ``github.repo``
    with the central credential: BYTE-IDENTICAL to today."""
    cc = central_client.config
    return ProjectTarget(
        slug="",
        repo=cc.repo,
        clone_base_url=config.clone_base_url,
        forge_type=cc.forge_type,
        api_base=cc.api_base,
        base_branch=config.base_branch,
        token=cc.pat,
        client=central_client,
    )


def _build_target_from_project(
    proj: ProjectConfig, central_client: Any, config: FixDrafterConfig
) -> ProjectTarget:
    """Build the app-repo target for a resolved project: a SECOND github_ops
    client (may be a different forge than the central tracker) + the resolved
    push credential. The instance gate is deliberately bypassed
    (:func:`build_client_for_repo`) — the credential-holder identity was
    validated on the central client."""
    from alfred.integrations.github_ops import GITHUB_API_BASE, build_client_for_repo

    token = _resolve_env_var(config, proj.token_env)
    api_base = proj.api_base or (
        GITHUB_API_BASE if proj.forge_type == "github" else ""
    )
    app_client = build_client_for_repo(
        repo=proj.repo,
        pat=token,
        forge_type=proj.forge_type,
        api_base=api_base,
        audit_log_path=central_client.config.audit_log_path,
        instance=central_client.config.instance,
    )
    return ProjectTarget(
        slug=proj.slug,
        repo=proj.repo,
        clone_base_url=proj.clone_base_url or config.clone_base_url,
        forge_type=proj.forge_type,
        api_base=api_base,
        base_branch=proj.base_branch or config.base_branch,
        token=token,
        client=app_client,
    )


def _resolve_project_target(
    issue: dict[str, Any], config: FixDrafterConfig, central_client: Any
) -> tuple[ProjectTarget | None, str]:
    """Resolve the app-repo target for one issue. Returns ``(target, "")`` on
    success or ``(None, reason)`` for a fail-loud (needs_human).

    Fail-loud rules (NEVER silently draft against the wrong repo):
      * ``projects`` empty → single-repo central target (never fails).
      * markerless + N>1 projects → ``markerless_ambiguous`` (do NOT default
        once more than one app is live).
      * markerless + N==1 → the single project (default_project or the sole
        entry) — the Phase-1 soak path.
      * marker names an unknown slug → ``unknown_project:<slug>``.
      * resolved project's ``forge_type`` unsupported → ``invalid_forge:<slug>``
        (checked BEFORE building the client, so the per-issue reason is clean
        rather than a tick-aborting raise from ``build_client_for_repo``).
      * resolved project's token env is empty → ``missing_token:<slug>``.
    """
    from alfred.integrations.github_ops import FORGE_TYPES, parse_project_marker

    if not config.projects:
        return _target_from_central(central_client, config), ""

    slug = parse_project_marker(str(issue.get("body") or ""))
    if not slug:
        if len(config.projects) > 1:
            return None, "markerless_ambiguous"
        slug = config.default_project or next(iter(config.projects))

    proj = config.projects.get(slug)
    if proj is None:
        return None, f"unknown_project:{slug}"
    if proj.forge_type not in FORGE_TYPES:
        # Fail-loud per-issue (needs_human) rather than letting the
        # build_client_for_repo / _git_auth_header guards raise mid-build:
        # this call is OUTSIDE run_drafter_once's per-issue try, so a raw
        # raise here would abort the WHOLE tick, not just this issue.
        return None, f"invalid_forge:{slug}"
    target = _build_target_from_project(proj, central_client, config)
    if not target.token:
        return None, f"missing_token:{slug}"
    return target, ""


# Ephemeral keyfile naming — the startup sweep + the per-run cleanup both
# key on this prefix.
_KEYFILE_PREFIX = "drafter-key-"


def _keyfile_dir(config: FixDrafterConfig) -> Path:
    """The DAEMON-ONLY dir for the ephemeral Anthropic-key EnvironmentFile.
    Explicit ``keyfile_dir`` or, by default, ``<work_root parent>/keys`` —
    a SIBLING of work_root/audit (never UNDER them, so the sandbox's
    ReadWritePaths can never re-expose it)."""
    if config.keyfile_dir:
        return Path(config.keyfile_dir)
    return Path(config.work_root).parent / "keys"


def _write_keyfile(config: FixDrafterConfig, key: str) -> str:
    """Write the drafter key to an EPHEMERAL 0600 daemon-owned keyfile in the
    systemd ``EnvironmentFile`` format (``ANTHROPIC_API_KEY=<value>``).

    The unit reads this as ROOT (``-p EnvironmentFile=``), so the value
    NEVER crosses the sudo env boundary (which strips ``--setenv`` under
    ``env_reset``) and NEVER appears in argv. The 0600 mode blocks the
    kalle-drafter sandbox user; the dir is 0700 daemon-only + also named in
    the unit's ``InaccessiblePaths`` (belt-and-suspenders)."""
    key_dir = _keyfile_dir(config)
    key_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(key_dir, 0o700)  # daemon-only dir
    # mkstemp creates the file 0600 by design (umask-independent); the
    # explicit chmod is a belt-and-suspenders pin target.
    old_umask = os.umask(0o077)
    try:
        fd, path = tempfile.mkstemp(prefix=_KEYFILE_PREFIX, dir=str(key_dir))
    finally:
        os.umask(old_umask)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"ANTHROPIC_API_KEY={key}\n")
    os.chmod(path, 0o600)
    return path


def _sweep_stale_keyfiles(config: FixDrafterConfig) -> None:
    """Startup crash-defense: remove any keyfiles a prior crashed run left
    behind (the per-run cleanup unlinks in a finally, so at steady state the
    dir is empty). Never raises."""
    key_dir = _keyfile_dir(config)
    try:
        stale = list(key_dir.glob(f"{_KEYFILE_PREFIX}*"))
    except OSError:
        return
    for p in stale:
        _unlink_quiet(str(p))
    if stale:
        log.info("fix_drafter.keyfiles_swept", count=len(stale), dir=str(key_dir))


def build_sandbox_command(
    *,
    clone_dir: str,
    config: FixDrafterConfig,
    settings_path: str,
    keyfile_path: str,
) -> tuple[list[str], dict[str, str]]:
    """Construct the hardened ``systemd-run`` invocation for the model run.

    Returns ``(argv, subprocess_env)``. The dedicated drafter Anthropic key
    is passed via ``-p EnvironmentFile=<keyfile_path>`` — systemd reads that
    0600 daemon-owned file as ROOT, so the value SURVIVES the sudo env
    boundary (``--setenv`` is stripped by sudo ``env_reset`` without
    ``SETENV``) and NEVER appears in argv (``ps``-visible) NOR in
    ``subprocess_env`` (which now carries ONLY ``PATH``). The daemon writes
    + cleans up ``keyfile_path`` (see :func:`_write_keyfile` / the
    ``_fresh_draft`` finally). The keyfile's dir is added to
    ``InaccessiblePaths`` (the 0600 already blocks the sandbox user; this is
    explicit belt-and-suspenders). The Forgejo token is NEVER here — the
    model has no git capability. The directive list is the security
    contract; it is pinned by test.
    """
    audit_dir = str(Path(config.hook_audit_path).resolve().parent)
    keyfile_dir = str(Path(keyfile_path).resolve().parent)

    props: list[str] = [
        f"User={config.sandbox_user}",
        f"Group={config.sandbox_group}",
        f"WorkingDirectory={clone_dir}",
        # filesystem isolation
        "ProtectHome=tmpfs",        # masks /home: VERA vault, ~/.claude, box .env
        "ProtectSystem=strict",
        f"ReadWritePaths={clone_dir}",
        f"ReadWritePaths={audit_dir}",
        "PrivateTmp=yes",
        "ProtectProc=invisible",
        "ProcSubset=pid",
        # CROSS-UID BRIDGE: the model (kalle-drafter) edits a clone the
        # daemon (andrew) created — its own edits must stay group-writable
        # so the daemon can then commit them. Transient-unit-scoped, so no
        # process-global umask leak. Pairs with the clone's child-scoped
        # 0o002 (_run_subprocess) + the shared-group/setgid work_root (infra).
        "UMask=0002",
        # privilege drop
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "RestrictSUIDSGID=yes",
        "RestrictNamespaces=yes",
        "LockPersonality=yes",
        "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
        "SystemCallArchitectures=native",
        "SystemCallFilter=@system-service",
        # network egress: localhost ONLY -> Anthropic forward-proxy.
        # ACCEPTED RESIDUAL (depth-of-defense, not a hole): IPAddressAllow
        # admits ALL loopback ports, not just the proxy port — the model
        # could in principle reach another localhost service. The real
        # narrowing (unix-socket-only proxy / a dedicated netns) is
        # cutover-infra, not code. IPAddressDeny=any still bars every
        # non-loopback host (incl. the PHI Supabase), which is the load-
        # bearing guarantee here.
        "IPAddressDeny=any",
        "IPAddressAllow=127.0.0.1 ::1",
        # resource caps
        f"TimeoutStartSec={config.claude_timeout}",
        f"MemoryMax={config.sandbox_memory_max}",
        f"TasksMax={config.sandbox_tasks_max}",
    ]
    # belt-and-suspenders for any out-of-/home secret root + the keyfile dir
    # (the 0600 keyfile already blocks the sandbox user; be explicit anyway).
    for inaccessible in (config.vera_vault_root, config.box_env_path, keyfile_dir):
        if inaccessible:
            props.append(f"InaccessiblePaths={inaccessible}")
    # proxy URLs are not secret -> inline as unit env.
    props.append(f"Environment=HTTPS_PROXY={config.anthropic_proxy_url}")
    props.append(f"Environment=HTTP_PROXY={config.anthropic_proxy_url}")

    # The drafter key rides an EnvironmentFile that systemd reads as ROOT —
    # so it survives the sudo env boundary AND never appears in argv. Only
    # the PATH to the file is in argv; the VALUE is never here.
    props.append(f"EnvironmentFile={keyfile_path}")

    argv: list[str] = list(config.sandbox_launch_prefix)
    argv += ["systemd-run", "--pipe", "--wait", "--collect", "-q"]
    for prop in props:
        argv += ["-p", prop]
    argv.append("--")
    argv.append(config.claude_command)
    argv += ["-p", "-"]
    argv += ["--allowedTools", ",".join(config.claude_allowed_tools)]
    argv += ["--settings", settings_path]

    # The subprocess environment carries ONLY PATH. The Anthropic key rides
    # the EnvironmentFile (NOT this env — sudo's env_reset would strip it),
    # and the Forgejo token is deliberately absent (the model has no git).
    sub_env: dict[str, str] = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    return argv, sub_env


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clone_url(config: FixDrafterConfig, repo: str) -> str:
    """Token-less git http remote — auth rides the ephemeral gitconfig
    extraHeader, never the URL (which persists into .git/config)."""
    return f"{config.clone_base_url.rstrip('/')}/{repo}.git"


def _git_auth_header(pat: str, forge_type: str = "forgejo") -> str:
    """The HTTP ``Authorization`` extraHeader value for the GIT plane,
    FORGE-DISPATCHED (Option B, D1) — the git-http auth scheme differs by
    forge and is NOT the same as the REST plane's ``github_ops._headers``.

    * ``github`` — GitHub git-over-HTTPS wants HTTP **Basic** auth with the
      PAT as the password (username conventionally ``x-access-token``, which
      works for both PATs and App installation tokens). GitHub does NOT
      accept the Gitea/Forgejo ``Authorization: token <pat>`` scheme for
      git-http (that scheme is REST-API-only). Confirmed against GitHub's
      own ``actions/checkout`` git-auth-helper, which sets
      ``AUTHORIZATION: basic <base64("x-access-token:" + token)>``.
    * ``forgejo`` (+ the ``forge_type="forgejo"`` default) — ``Authorization:
      token <pat>``, UNCHANGED (Forgejo git-http accepts it; Phase-0 byte-
      identical).
    * any other value — raises ``ValueError`` (forge-type guard): only
      ``github``/``forgejo`` are supported (``FORGE_TYPES``); an unsupported
      forge must never silently pick a scheme (the git-plane half of the
      cross-plane mismatch the REST-plane ``build_client_for_repo`` guard
      also closes).

    The value rides an extraHeader in a 0600 temp gitconfig (never argv,
    never the clone URL / reflog) — same containment as before, for both
    schemes.
    """
    if forge_type == "github":
        basic = base64.b64encode(
            f"x-access-token:{pat}".encode()
        ).decode("ascii")
        return f"Authorization: Basic {basic}"
    if forge_type == "forgejo":
        return f"Authorization: token {pat}"
    # FAIL-LOUD on any other forge (defense-in-depth, forge-type guard):
    # never silently pick a git-plane scheme for an unsupported forge — that
    # is the mismatch trap the REST-plane guard (github_ops.build_client_for_
    # repo) also closes. Only github/forgejo are supported (FORGE_TYPES); the
    # drafter pre-validates in _resolve_project_target, so this is a backstop.
    from alfred.integrations.github_ops import FORGE_TYPES

    raise ValueError(
        f"unsupported forge_type {forge_type!r} for the git plane; "
        f"must be one of {sorted(FORGE_TYPES)}"
    )


def _write_temp_gitconfig(pat: str, forge_type: str = "forgejo") -> str:
    """Write a daemon-private 0600 gitconfig carrying the token as an HTTP
    extraHeader. Referenced via GIT_CONFIG_GLOBAL for the daemon's git
    only — NOT under work_root, NOT under /home (the sandbox's PrivateTmp
    + ProtectHome cannot see it regardless), and unlinked after the run.

    The extraHeader's auth scheme is forge-dispatched (see
    :func:`_git_auth_header`): GitHub git-http needs Basic auth, Forgejo
    keeps the ``token`` scheme. Only ONE remote is contacted per run, so a
    global ``[http] extraHeader`` never leaks to an unintended host.
    """
    fd, path = tempfile.mkstemp(prefix="fix-drafter-gitconfig-")
    os.close(fd)
    os.chmod(path, 0o600)
    with open(path, "w", encoding="utf-8") as f:
        f.write("[http]\n")
        f.write(f"\textraHeader = {_git_auth_header(pat, forge_type)}\n")
    return path


def _git_env(gitconfig_path: str) -> dict[str, str]:
    """Daemon git environment: the ephemeral token-bearing gitconfig as
    GIT_CONFIG_GLOBAL, system config disabled, never prompt for auth."""
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = gitconfig_path
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _unlink_quiet(path: str | None) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def _label_names(labels: Any) -> set[str]:
    """Normalize a Forgejo/GitHub labels field to a set of name strings.
    Handles both ``[{"name": ...}]`` and bare ``["name"]`` shapes."""
    out: set[str] = set()
    if isinstance(labels, list):
        for lbl in labels:
            if isinstance(lbl, dict):
                name = str(lbl.get("name") or "").strip()
            else:
                name = str(lbl or "").strip()
            if name:
                out.add(name)
    return out


def _find_pr_for_branch(
    prs: list[dict[str, Any]], branch: str
) -> dict[str, Any] | None:
    """Client-side head.ref filter (the Forgejo pr_list is unfiltered)."""
    for pr in prs:
        head = pr.get("head")
        ref = head.get("ref") if isinstance(head, dict) else None
        if ref == branch:
            return pr
    return None


def _assert_clone_disjoint_from_vault(
    clone_dir: str, vera_vault_root: str, work_root: str
) -> bool:
    """Fail-closed disjointness assertion (security gate before the model
    run): the resolved clone root must be under ``work_root`` AND neither
    contain nor be contained by VERA's PHI vault root. An empty vault root
    fails closed (can't prove disjointness)."""
    if not vera_vault_root:
        return False
    try:
        clone = Path(clone_dir).resolve()
        work = Path(work_root).resolve()
        vault = Path(vera_vault_root).resolve()
    except OSError:
        return False
    if not clone.is_relative_to(work):
        return False
    if clone.is_relative_to(vault) or vault.is_relative_to(clone):
        return False
    return True


def _sandbox_rw_dirs(config: FixDrafterConfig) -> list[Path]:
    """The dirs the hardened unit grants the model write access to
    (``ReadWritePaths``): the per-issue clones (all under ``work_root``)
    and the DEDICATED bash-audit dir. Anything under these is reachable +
    mutable by the sandboxed model (same UID, ProtectHome punched through)."""
    dirs: list[Path] = []
    if config.work_root:
        try:
            dirs.append(Path(config.work_root).resolve())
        except OSError:
            pass
    try:
        dirs.append(Path(config.hook_audit_path).resolve().parent)
    except OSError:
        pass
    return dirs


def _records_outside_sandbox(
    config: FixDrafterConfig, audit_paths: list[tuple[str, str]]
) -> tuple[bool, str]:
    """Fail-closed guard (run before any drafting): the AUTHORITATIVE
    records — the drafter ``state.path`` and the REST ``github_ops`` audit
    — MUST NOT live under any sandbox-writable dir (:func:`_sandbox_rw_dirs`),
    or the model could tamper with them (``ReadWritePaths`` punches through
    ``ProtectHome``). Returns ``(ok, offending_detail)``. A misconfig that
    co-locates state/REST-audit with the clone or the bash-audit dir is
    refused, never silently re-exposed."""
    rw = _sandbox_rw_dirs(config)
    checks = [("state", config.state_path)] + list(audit_paths)
    for label, raw_path in checks:
        if not raw_path:
            continue
        try:
            p = Path(raw_path).resolve()
        except OSError:
            continue
        for d in rw:
            if p == d or p.is_relative_to(d):
                return False, (
                    f"{label} path {p} is under sandbox-writable dir {d} "
                    "— the model could tamper with an authoritative record"
                )
    return True, ""


_DRAFTER_PREAMBLE = (
    "You are drafting a minimal, focused fix for the bug described below, "
    "inside a checked-out git working tree.\n"
    "RULES:\n"
    "- Edit only the files needed for this fix. Keep the change small.\n"
    "- Do NOT commit, push, open PRs, merge, or touch CI/workflows/secrets.\n"
    "- You may run the project's tests (pytest / npm test / etc.) to verify.\n"
    "- PRIVACY (LOAD-BEARING): the issue text below may contain SENSITIVE "
    "REPORTED DATA (personal / patient information, names, dates, IDs, "
    "screenshots-turned-text). This fix PR may land on a PUBLIC repo. NEVER "
    "copy ANY reported data, quoted issue text, user-supplied values, names, "
    "dates, or identifiers into the code you write — no test fixtures, "
    "comments, log lines, or strings derived from the report. Describe and "
    "fix the underlying defect GENERICALLY; invent neutral placeholder data "
    "if a test needs a value.\n"
    "- When done, print a one-paragraph summary of what you changed "
    "(the summary is NOT used in the PR — it is discarded).\n\n"
)


def _build_drafter_prompt(title: str, body: str) -> str:
    return f"{_DRAFTER_PREAMBLE}# Issue: {title}\n\n{body}\n"


# Light de-PHI scan on the drafted diff (Option B). BOUNDED, not proven-clean
# — operator-ACCEPTED residual — pairs with the preamble hardening. Scans only
# ADDED lines (the model's contribution). Returns the matched pattern CLASSES,
# never the matched values (so a PHI hit is never itself logged).
_PHI_DIFF_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    # a run of 9+ digits (allowing spaces/dashes): phone / health-card / SIN shape.
    ("long_digit_run", re.compile(r"\b\d[\d\s-]{7,}\d\b")),
    ("phi_keyword", re.compile(
        r"(?i)\b(patient|health\s*card|ohip|\bsin\b|date\s*of\s*birth|\bdob\b|"
        r"medical\s*record|\bmrn\b)\b"
    )),
)


def _scan_diff_for_phi(diff_text: str) -> list[str]:
    """Return the DISTINCT PHI pattern-CLASSES found on ADDED diff lines
    (never the matched values). Empty list = clean by this bounded scan."""
    hits: set[str] = set()
    for line in (diff_text or "").splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        added = line[1:]
        for name, rx in _PHI_DIFF_PATTERNS:
            if rx.search(added):
                hits.add(name)
    return sorted(hits)


def _extract_summary(model_stdout: str) -> str:
    text = (model_stdout or "").strip()
    if not text:
        return "(no summary produced by the drafter)"
    return text[-2000:]


# ---------------------------------------------------------------------------
# Scan + eligibility
# ---------------------------------------------------------------------------


async def scan_auto_fix_issues(client: Any) -> list[dict[str, Any]]:
    """The work queue: open Forgejo issues carrying the auto-fix label."""
    return await client.issue_list(
        labels=_AUTO_FIX_LABEL, state="open", caller=_CALLER,
    )


def select_eligible(
    issues: list[dict[str, Any]], state: FixDrafterState
) -> list[dict[str, Any]]:
    """Drop issues already terminal in own state (``pr_open`` /
    ``needs_human``). Fresh + resumable (``branch_pushed``/``drafting``)
    issues pass through to :func:`draft_one`, which ground-truth-dedups."""
    eligible: list[dict[str, Any]] = []
    for issue in issues:
        num = issue.get("number")
        if num is None:
            continue
        entry = state.entries.get(str(int(num)))
        if entry is not None and entry.status in {"pr_open", "needs_human"}:
            continue
        eligible.append(issue)
    return eligible


# ---------------------------------------------------------------------------
# PHI-clean PR surface (Option B — the app repo may be PUBLIC GitHub)
# ---------------------------------------------------------------------------


def _phi_clean_pr_title(issue_number: int) -> str:
    """Neutral, id-only WIP title — NO raw issue title (the issue title can
    carry PHI, and the model was fed the full PHI body)."""
    return f"WIP: auto-fix draft (tracker issue {issue_number})"


def _phi_clean_pr_body(issue_number: int, target: ProjectTarget) -> str:
    """Bare cross-repo reference to the CENTRAL tracker issue by ID ONLY.

    Deliberately: NO raw title, NO ``_extract_summary(model_out)`` stdout tail
    (the model read the PHI issue body), and NO ``Closes #N`` — the issue is
    on a DIFFERENT (central sovereign) repo, so ``#N`` would false-link/close
    an app-repo issue. The reverse linkage (app-repo PR ↔ tracker issue) is
    written back onto the tracker issue separately (the effectiveness-loop
    seam). Plain ``id`` (no ``#``) so no wrong same-repo auto-link fires."""
    return (
        f"Automated draft fix for central bug-intake tracker issue id "
        f"{issue_number}.\n\n"
        "References the tracker issue by id only — carries no reported "
        "content. Operator review + merge required (never auto-merged)."
    )


# ---------------------------------------------------------------------------
# PR-open (shared by fresh-draft + resume; never half-open)
# ---------------------------------------------------------------------------


async def _open_pr(
    config: FixDrafterConfig,
    target: ProjectTarget,
    state: FixDrafterState,
    entry: FixDrafterEntry,
    branch: str,
    title: str,
    *,
    summary: str,
    outcome: str,
) -> dict[str, Any]:
    """Open the draft PR on the APP repo (``target``) against an ALREADY-
    pushed branch. A 409 (PR already exists) is treated as adopt. On any
    other failure the entry stays ``branch_pushed`` so the next tick resumes
    here — never half-open, never re-draft.

    PHI-CLEAN PR (Option B): the title/body carry NO raw issue title and NO
    model-stdout summary — only a neutral id reference (see
    :func:`_phi_clean_pr_title` / :func:`_phi_clean_pr_body`)."""
    issue_number = entry.issue_number
    pr_title = _phi_clean_pr_title(issue_number)
    pr_body = _phi_clean_pr_body(issue_number, target)
    try:
        pr = await target.client.pr_create(
            head=branch,
            base=target.base_branch,
            title=pr_title,
            body=pr_body,
            caller=_CALLER,
            issue_number=issue_number,
        )
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code if exc.response is not None else 0
        if status_code == 409:
            adopted = await _adopt_existing_pr(target, state, entry, branch)
            if adopted is not None:
                return adopted
        log.warning(
            "fix_drafter.pr_open_failed",
            issue_number=issue_number,
            http_status=status_code,
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        entry.status = "branch_pushed"
        state.save()
        return {"issue_number": issue_number, "outcome": "pr_open_failed"}
    except Exception as exc:  # noqa: BLE001 — isolate per issue
        log.warning(
            "fix_drafter.pr_open_failed",
            issue_number=issue_number,
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        entry.status = "branch_pushed"
        state.save()
        return {"issue_number": issue_number, "outcome": "pr_open_failed"}

    entry.pr_number = pr.get("number")
    entry.pr_url = str(pr.get("html_url") or "")
    entry.app_repo = target.repo          # cross-repo linkage (self-describing)
    entry.app_forge_type = target.forge_type
    entry.status = "pr_open"
    state.save()
    log.info(
        "fix_drafter.pr_opened",
        issue_number=issue_number,
        pr_number=entry.pr_number,
        app_repo=target.repo,
        outcome=outcome,
    )
    return {
        "issue_number": issue_number,
        "outcome": outcome,
        "pr_number": entry.pr_number,
    }


async def _adopt_existing_pr(
    target: ProjectTarget,
    state: FixDrafterState,
    entry: FixDrafterEntry,
    branch: str,
) -> dict[str, Any] | None:
    """Layer-3 adopt: a PR already exists for this branch on the APP repo →
    record it (+ the self-describing cross-repo linkage) and skip. Returns
    the result dict, or ``None`` if no PR is found."""
    prs = await target.client.pr_list(state="all", caller=_CALLER)
    match = _find_pr_for_branch(prs, branch)
    if match is None:
        return None
    entry.pr_number = match.get("number")
    entry.pr_url = str(match.get("html_url") or "")
    entry.app_repo = target.repo
    entry.app_forge_type = target.forge_type
    entry.status = "pr_open"
    state.save()
    log.info(
        "fix_drafter.adopted",
        issue_number=entry.issue_number,
        pr_number=entry.pr_number,
    )
    return {
        "issue_number": entry.issue_number,
        "outcome": "adopted",
        "pr_number": entry.pr_number,
    }


def _handle_empty_diff(
    state: FixDrafterState,
    entry: FixDrafterEntry,
    config: FixDrafterConfig,
) -> dict[str, Any]:
    """An empty diff means the model produced no change. Bump the count;
    after ``max_empty_diff_retries`` latch ``needs_human`` (stops the
    infinite retry, surfaces to the operator — self-correcting + ILB).
    NEVER push an empty branch."""
    issue_number = entry.issue_number
    entry.empty_diff_count += 1
    log.warning(
        "fix_drafter.empty_diff",
        issue_number=issue_number,
        empty_diff_count=entry.empty_diff_count,
    )
    if entry.empty_diff_count >= config.max_empty_diff_retries:
        entry.status = "needs_human"
        state.save()
        log.warning(
            "fix_drafter.needs_human",
            issue_number=issue_number,
            reason="empty_diff",
            empty_diff_count=entry.empty_diff_count,
        )
        return {"issue_number": issue_number, "outcome": "needs_human"}
    state.save()
    return {"issue_number": issue_number, "outcome": "empty_diff"}


# ---------------------------------------------------------------------------
# The draft lifecycle
# ---------------------------------------------------------------------------


async def draft_one(
    issue: dict[str, Any],
    config: FixDrafterConfig,
    central_client: Any,
    state: FixDrafterState,
    *,
    target: ProjectTarget,
) -> dict[str, Any]:
    """Draft (or adopt/resume) one auto-fix issue. Owns the ephemeral
    gitconfig lifecycle; delegates the fresh-draft clone lifecycle to
    :func:`_fresh_draft`. Three-layer dedup, ground-truth authoritative,
    never half-open.

    Option B: the ISSUE lives on the CENTRAL tracker (``central_client`` —
    the label re-verify reads it there); clone/push/pr_create/pr_list/adopt
    run against the APP repo (``target``). Single-repo == both are the same
    central client/repo (byte-identical)."""
    issue_number = int(issue.get("number") or 0)
    title = str(issue.get("title") or "")
    body = str(issue.get("body") or "")
    branch = f"{config.branch_prefix}{issue_number}"
    key = str(issue_number)

    entry = state.entries.get(key)
    if entry is None:
        entry = FixDrafterEntry(issue_number=issue_number, branch=branch)
        state.entries[key] = entry
    entry.branch = branch
    if not entry.first_attempt_at:
        entry.first_attempt_at = _now_iso()
    entry.last_attempt_at = _now_iso()
    entry.attempts += 1

    # --- Layer 1: own state (defense-in-depth; select_eligible filters too).
    if entry.status in {"pr_open", "needs_human"}:
        return {"issue_number": issue_number, "outcome": "skipped"}

    clone_url = target.clone_url()
    # Forge-dispatch the git-plane auth by the target's forge (D1): a GitHub
    # app repo needs Basic auth, a Forgejo tracker keeps the token scheme.
    gitconfig_path = _write_temp_gitconfig(target.token, target.forge_type)
    git_env = _git_env(gitconfig_path)
    try:
        # --- Layer 2: git ground-truth — does the branch already exist?
        #     (on the APP repo — target.client / target.clone_url).
        rc, out, err = await _run_subprocess(
            ["git", "ls-remote", "--heads", clone_url, branch],
            env=git_env,
            timeout=config.git_timeout,
        )
        if rc != 0:
            log.warning(
                "fix_drafter.ls_remote_failed",
                issue_number=issue_number,
                code=rc,
                stderr=err[:500],
                stdout_tail=out[-2000:] if out else "",
            )
            return {"issue_number": issue_number, "outcome": "ls_remote_failed"}

        branch_exists = bool(out.strip())
        if branch_exists:
            # --- Layer 3: PR existence — adopt or resume-at-pr_create ONLY
            #     (pr_list / pr_create on the APP repo = target.client).
            adopted = await _adopt_existing_pr(target, state, entry, branch)
            if adopted is not None:
                return adopted
            # Branch pushed on a prior tick but PR-open never landed →
            # RESUME at pr_create only (no re-clone, no re-draft).
            entry.status = "branch_pushed"
            state.save()
            return await _open_pr(
                config, target, state, entry, branch, title,
                summary="(resumed: branch already pushed on a prior tick)",
                outcome="resumed",
            )

        # --- Fresh draft path (branch absent). Cap persistent failures:
        # a fresh draft re-clones + re-runs the model every tick, so a
        # stuck issue (e.g. a branch-protection misconfig blocking the
        # auto-fix/* push) would burn Claude minutes forever. Latch
        # needs_human once total attempts exceed max_attempts. (Adopt /
        # resume above are NOT capped — they never re-burn the model. The
        # empty-diff path has its own separate count-based latch.)
        if entry.attempts > config.max_attempts:
            entry.status = "needs_human"
            state.save()
            log.warning(
                "fix_drafter.needs_human",
                issue_number=issue_number,
                reason="max_attempts_exceeded",
                attempts=entry.attempts,
            )
            return {"issue_number": issue_number, "outcome": "needs_human"}

        # Re-verify the auto-fix label BEFORE any clone — on the CENTRAL
        # tracker (that's where the issue lives).
        issue_data = await central_client.issue_get(number=issue_number, caller=_CALLER)
        labels = _label_names(issue_data.get("labels"))
        if _AUTO_FIX_LABEL not in labels:
            from alfred.integrations.github_ops import append_github_audit
            append_github_audit(
                config.audit_log_path or central_client.config.audit_log_path,
                op="fix_drafter_label_reverify",
                repo=central_client.config.repo,
                caller=_CALLER,
                outcome="denied",
                issue_number=issue_number,
                error="not_auto_fix_labeled",
            )
            log.warning(
                "fix_drafter.label_reverify_refused",
                issue_number=issue_number,
                reason="not_auto_fix_labeled",
                labels=sorted(labels),
            )
            return {
                "issue_number": issue_number,
                "outcome": "refused_not_auto_fix",
            }

        return await _fresh_draft(
            config, target, state, entry, branch, title, body,
            git_env=git_env, clone_url=clone_url,
        )
    finally:
        _unlink_quiet(gitconfig_path)


async def _fresh_draft(
    config: FixDrafterConfig,
    target: ProjectTarget,
    state: FixDrafterState,
    entry: FixDrafterEntry,
    branch: str,
    title: str,
    body: str,
    *,
    git_env: dict[str, str],
    clone_url: str,
) -> dict[str, Any]:
    """Clone → branch → sandboxed model → detect → commit → push →
    pr_create, in a try/finally that always removes the work dir. The
    daemon owns every mutating git step; the model only edits files."""
    issue_number = entry.issue_number
    entry.status = "drafting"
    work_item_dir = tempfile.mkdtemp(
        prefix=f"issue-{issue_number}-", dir=config.work_root
    )
    # GAP-1 (dir level): mkdtemp hardcodes 0o700 — even under a setgid,
    # group=drafter work_root the GROUP gets no traverse (x) bit, so the
    # sandboxed model (kalle-drafter, a drafter member) hits EACCES
    # traversing INTO the per-issue dir and can't reach the clone. Grant
    # group r-x traverse + keep setgid (0o2750: owner rwx, group r-x, +sgid)
    # so the clone subtree stays group=drafter. The umask fix below makes the
    # clone FILES group-writable; this makes the PATH to them traversable.
    os.chmod(work_item_dir, 0o2750)
    clone_dir = os.path.join(work_item_dir, "repo")
    control_dir = os.path.join(work_item_dir, "control")
    # The ephemeral Anthropic-key EnvironmentFile — written mid-flow (step
    # 5), ALWAYS unlinked in the finally (even on failure/timeout/exception).
    keyfile_path: str | None = None
    try:
        # 1. disjointness assertion FIRST — fail-closed BEFORE any write
        #    (clone_dir is already known), so a misconfigured work_root
        #    inside the PHI vault is refused before a single byte is cloned.
        if not _assert_clone_disjoint_from_vault(
            clone_dir, config.vera_vault_root, config.work_root
        ):
            log.warning(
                "fix_drafter.disjointness_refused",
                issue_number=issue_number,
                clone_dir=clone_dir,
                vera_vault_root=config.vera_vault_root,
                work_root=config.work_root,
            )
            return {
                "issue_number": issue_number,
                "outcome": "refused_disjointness",
            }

        # 2. shallow single-branch clone (token-less remote URL). CHILD-
        # SCOPED umask 0o002 so the working tree is GROUP-WRITABLE — the
        # cross-UID bridge: the daemon (andrew) clones, the sandboxed model
        # (kalle-drafter) edits via the shared `drafter` group. preexec_fn
        # confines the umask to the git-clone child; the daemon's own
        # process umask is untouched, so state / REST-audit stay 0644 (NOT
        # group-writable — no records-integrity leak). Pairs with the
        # sandbox unit's ``UMask=0002`` (build_sandbox_command) + the
        # shared-group/setgid work_root (infra, per the cutover runbook).
        rc, out, err = await _run_subprocess(
            [
                "git", "clone", "--depth", "1", "--single-branch",
                "--branch", target.base_branch, clone_url, clone_dir,
            ],
            env=git_env,
            timeout=config.git_timeout,
            umask=0o002,
        )
        if rc != 0:
            log.warning(
                "fix_drafter.clone_failed",
                issue_number=issue_number,
                code=rc,
                stderr=err[:500],
                stdout_tail=out[-2000:] if out else "",
            )
            return {"issue_number": issue_number, "outcome": "clone_failed"}

        # 3. cut the branch + set the commit identity (LOCAL, no token).
        rc, out, err = await _run_subprocess(
            ["git", "-C", clone_dir, "switch", "-c", branch]
        )
        if rc != 0:
            log.warning(
                "fix_drafter.branch_failed",
                issue_number=issue_number,
                code=rc,
                stderr=err[:500],
                stdout_tail=out[-2000:] if out else "",
            )
            return {"issue_number": issue_number, "outcome": "draft_failed"}
        await _run_subprocess(
            ["git", "-C", clone_dir, "config", "user.name", config.commit_author_name]
        )
        await _run_subprocess(
            ["git", "-C", clone_dir, "config", "user.email", config.commit_author_email]
        )

        # 4. write the PreToolUse hook + settings into the READ-ONLY
        #    control dir (not in the sandbox's ReadWritePaths → the model
        #    cannot tamper with its own gate).
        _hook_path, settings_path = write_drafter_hook_files(
            control_dir, audit_path=config.hook_audit_path
        )

        # 5. resolve the drafter key + FAIL LOUD before launching claude.
        #    The re-exec'd daemon doesn't inherit the main process's runtime
        #    os.environ (where .env was auto-loaded), so the key is resolved
        #    from os.environ OR the box .env file directly. An empty key
        #    would otherwise surface as a cryptic claude "Not logged in" —
        #    ILB: name drafter_key_env + box_env_path and stop before the run.
        drafter_key = _resolve_drafter_key(config)
        if not drafter_key:
            log.error(
                "fix_drafter.drafter_key_missing",
                issue_number=issue_number,
                drafter_key_env=config.drafter_key_env,
                box_env_path=config.box_env_path,
                detail=(
                    "the dedicated drafter Anthropic key resolved EMPTY (not "
                    "in os.environ, not in the box .env) — claude would run "
                    "un-authed; refusing to launch the model"
                ),
            )
            return {"issue_number": issue_number, "outcome": "drafter_key_missing"}

        # 5b. write the resolved key to an ephemeral 0600 daemon-owned
        #     EnvironmentFile (systemd reads it as ROOT → survives the sudo
        #     env boundary that strips --setenv; value never in argv/env).
        #     ALWAYS unlinked in the finally below.
        keyfile_path = _write_keyfile(config, drafter_key)

        # 5c. run the sandboxed model — ONLY through the hardened unit.
        argv, sub_env = build_sandbox_command(
            clone_dir=clone_dir, config=config, settings_path=settings_path,
            keyfile_path=keyfile_path,
        )
        rc, model_out, model_err = await _run_subprocess(
            argv, env=sub_env, input_text=_build_drafter_prompt(title, body),
            timeout=config.claude_timeout,
        )
        if rc != 0:
            detail = (model_out[:200] or model_err[:200] or "(no output)")
            log.warning(
                "fix_drafter.draft_failed",
                issue_number=issue_number,
                code=rc,
                stderr=model_err[:500],
                stdout_tail=model_out[-2000:] if model_out else "",
                summary=f"Exit code {rc}: {detail}",
            )
            return {"issue_number": issue_number, "outcome": "draft_failed"}

        # 6. detect work — empty diff NEVER pushes.
        rc, status_out, err = await _run_subprocess(
            ["git", "-C", clone_dir, "status", "--porcelain"]
        )
        if rc != 0:
            log.warning(
                "fix_drafter.status_failed",
                issue_number=issue_number,
                code=rc,
                stderr=err[:500],
                stdout_tail=status_out[-2000:] if status_out else "",
            )
            return {"issue_number": issue_number, "outcome": "draft_failed"}
        if not status_out.strip():
            return _handle_empty_diff(state, entry, config)

        # 7. stage (LOCAL, no token) — add BEFORE the de-PHI scan so new
        #    files show in the staged diff.
        rc, out, err = await _run_subprocess(
            ["git", "-C", clone_dir, "add", "-A"]
        )
        if rc != 0:
            log.warning(
                "fix_drafter.commit_failed",
                issue_number=issue_number, code=rc, stderr=err[:500],
                stdout_tail=out[-2000:] if out else "",
            )
            return {"issue_number": issue_number, "outcome": "draft_failed"}

        # 7b. LIGHT de-PHI scan on the staged diff BEFORE commit/push (Option
        #     B — the PR may land on a PUBLIC repo). Bounded, not proven-clean
        #     (operator-accepted residual) + preamble hardening. A hit REFUSES
        #     (needs_human) — NEVER push a diff carrying obvious PHI. The log
        #     names the pattern CLASSES only, never the matched values.
        rc, diff_out, _ = await _run_subprocess(
            ["git", "-C", clone_dir, "diff", "--cached"]
        )
        phi_hits = _scan_diff_for_phi(diff_out) if rc == 0 else []
        if phi_hits:
            entry.status = "needs_human"
            state.save()
            log.error(
                "fix_drafter.phi_scan_refused",
                issue_number=issue_number,
                pattern_classes=phi_hits,
                detail=(
                    "the drafted diff matched obvious-PHI pattern classes on "
                    "added lines — refusing to push to a possibly-public app "
                    "repo (needs_human). Values are NOT logged."
                ),
            )
            return {"issue_number": issue_number, "outcome": "phi_scan_refused"}

        # 8. commit (LOCAL, no token). NON-linking tracker ref (C4): a bare
        #    ``#N`` auto-links on the APP repo to an UNRELATED same-numbered
        #    app issue (the number is the CENTRAL tracker's id, not the app
        #    repo's). "tracker issue N" is plain text — it can't create a
        #    stray cross-link on whatever forge the app repo lives on.
        rc, out, err = await _run_subprocess(
            ["git", "-C", clone_dir, "commit", "-m",
             f"auto-fix: draft for tracker issue {issue_number}"]
        )
        if rc != 0:
            log.warning(
                "fix_drafter.commit_failed",
                issue_number=issue_number, code=rc, stderr=err[:500],
                stdout_tail=out[-2000:] if out else "",
            )
            return {"issue_number": issue_number, "outcome": "draft_failed"}

        # 8. branch-regex assertion — never push anything but auto-fix/issue-N.
        if not _BRANCH_REGEX.match(branch):
            log.warning(
                "fix_drafter.branch_regex_refused",
                issue_number=issue_number, branch=branch,
            )
            return {"issue_number": issue_number, "outcome": "refused_branch_regex"}

        # 9. push — single explicit refspec (never --all/--mirror/--tags),
        #    ephemeral token via the daemon gitconfig.
        rc, out, err = await _run_subprocess(
            ["git", "-C", clone_dir, "push", "origin", f"{branch}:{branch}"],
            env=git_env,
            timeout=config.git_timeout,
        )
        if rc != 0:
            log.warning(
                "fix_drafter.push_failed",
                issue_number=issue_number, code=rc, stderr=err[:500],
                stdout_tail=out[-2000:] if out else "",
            )
            return {"issue_number": issue_number, "outcome": "push_failed"}

        # 10. record branch_pushed BEFORE pr_create (never half-open).
        entry.status = "branch_pushed"
        state.save()

        # 11. open the draft PR (PHI-clean surface — summary is IGNORED now,
        #     kept only for the resume-path signature symmetry).
        return await _open_pr(
            config, target, state, entry, branch, title,
            summary="", outcome="drafted",
        )
    finally:
        # Delete the ephemeral keyfile FIRST — even on failure / timeout /
        # exception the secret never outlives the run.
        _unlink_quiet(keyfile_path)
        shutil.rmtree(work_item_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# One tick — the testable unit + the CLI probe surface
# ---------------------------------------------------------------------------


_TICK_FIELDS = (
    "scanned", "eligible", "drafted", "adopted",
    "resumed", "failed", "needs_human", "skipped",
)


async def run_drafter_once(
    config: FixDrafterConfig,
    raw: dict[str, Any],
) -> dict[str, Any]:
    """Run one drafter tick. Returns the summary dict for CLI/tests.

    Per-issue isolated (one failure never kills the tick or starves later
    issues). ILB: ``fix_drafter.tick`` is logged EVERY tick — zero-work
    ticks included — so an idle drafter (no open auto-fix issues) is
    distinguishable from a broken one.
    """
    from alfred.integrations.github_ops import (
        GitHubOpsError,
        build_github_client,
    )

    counts = dict.fromkeys(_TICK_FIELDS, 0)
    results: list[dict[str, Any]] = []

    try:
        client = build_github_client(raw, config.instance)
    except GitHubOpsError as exc:
        log.warning(
            "fix_drafter.client_build_failed",
            instance=config.instance,
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        log.info("fix_drafter.tick", **counts)
        return {**counts, "results": results}

    # `client` is the CENTRAL tracker client (scan + label re-verify). The
    # per-issue APP-repo target is resolved in the loop below (Option B).
    central_client = client

    # FAIL-CLOSED records guard (gate A): refuse the whole tick if an
    # authoritative record (drafter state, the REST github_ops audit) would
    # sit under a sandbox-writable dir (the clone tree or the bash-audit
    # dir) — a co-located misconfig would let the model tamper with state.
    ok, detail = _records_outside_sandbox(
        config,
        [
            ("github_ops_audit", client.config.audit_log_path),
            ("fix_drafter_audit_override", config.audit_log_path),
        ],
    )
    if not ok:
        log.error("fix_drafter.records_exposed_refused", detail=detail)
        log.info("fix_drafter.tick", **counts)
        return {**counts, "results": results}

    # Pre-create the DEDICATED bash-audit dir before any unit launch
    # (ReadWritePaths requires the path to exist at unit start).
    try:
        Path(config.hook_audit_path).resolve().parent.mkdir(
            parents=True, exist_ok=True
        )
    except OSError as exc:
        log.warning(
            "fix_drafter.audit_dir_create_failed",
            path=config.hook_audit_path,
            error=str(exc),
            error_type=exc.__class__.__name__,
        )

    state = FixDrafterState.load(config.state_path)

    try:
        issues = await scan_auto_fix_issues(client)
    except Exception as exc:  # noqa: BLE001 — a scan failure is one bad tick
        log.warning(
            "fix_drafter.scan_failed",
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        log.info("fix_drafter.tick", **counts)
        return {**counts, "results": results}

    counts["scanned"] = len(issues)
    eligible = select_eligible(issues, state)
    counts["eligible"] = len(eligible)

    for issue in eligible:
        issue_number = issue.get("number")
        # Option B: resolve the APP-repo target from the issue's project
        # marker. A None target is a FAIL-LOUD (needs_human) — never silently
        # draft against the wrong repo.
        target, reason = _resolve_project_target(issue, config, central_client)
        if target is None:
            key = str(int(issue_number)) if issue_number is not None else ""
            if key:
                entry = state.entries.get(key)
                if entry is None:
                    entry = FixDrafterEntry(issue_number=int(issue_number))
                    state.entries[key] = entry
                entry.status = "needs_human"
            log.warning(
                "fix_drafter.project_unresolved",
                issue_number=issue_number,
                reason=reason,
                project_count=len(config.projects),
            )
            counts["needs_human"] += 1
            results.append({
                "issue_number": issue_number,
                "outcome": "needs_human",
                "reason": reason,
            })
            continue
        try:
            res = await draft_one(
                issue, config, central_client, state, target=target,
            )
        except Exception as exc:  # noqa: BLE001 — isolate per issue
            log.warning(
                "fix_drafter.draft_one_error",
                issue_number=issue_number,
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
            counts["failed"] += 1
            results.append({
                "issue_number": issue_number,
                "outcome": "draft_one_error",
            })
            continue
        outcome = res.get("outcome", "")
        if outcome in counts:
            counts[outcome] += 1
        else:
            # Every *_failed / refused_* / empty_diff outcome tallies as a
            # (non-terminal) failure for the tick summary.
            counts["failed"] += 1
        results.append(res)

    state.save()

    log.info("fix_drafter.tick", **counts)
    return {**counts, "results": results}


# ---------------------------------------------------------------------------
# Daemon loop
# ---------------------------------------------------------------------------


async def run_daemon(
    config: FixDrafterConfig,
    raw: dict[str, Any],
) -> None:
    """Interval loop: tick, sleep ``interval_minutes``, repeat.

    Per-tick exception containment: a bad tick logs + continues; the
    daemon never dies to a single failure. Cadence is draft-bound, not
    clock-aligned (a long model run simply pushes the next tick later).
    """
    log.info(
        "fix_drafter.daemon.starting",
        interval_minutes=config.interval_minutes,
        instance=config.instance,
        work_root=config.work_root,
        state_path=config.state_path,
    )
    # Crash defense: sweep any ephemeral keyfiles a prior crashed run left
    # behind (steady state the per-run finally already unlinks them).
    _sweep_stale_keyfiles(config)
    while True:
        try:
            await run_drafter_once(config, raw)
        except Exception:  # noqa: BLE001 — daemon-level safety net
            log.exception("fix_drafter.daemon.tick_error")
        await asyncio.sleep(config.interval_minutes * 60)


__all__ = [
    "DEFAULT_FIX_DRAFTER_STATE_PATH",
    "FixDrafterConfig",
    "FixDrafterEntry",
    "FixDrafterState",
    "build_sandbox_command",
    "draft_one",
    "load_fix_drafter_config",
    "load_pr_links",
    "poll_client_for_app_repo",
    "run_daemon",
    "run_drafter_once",
    "scan_auto_fix_issues",
    "select_eligible",
    "write_drafter_hook_files",
]
