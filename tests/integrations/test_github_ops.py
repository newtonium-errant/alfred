"""GitHub ops (pipeline c1) — matrix pin, gate, factory, client, audit.

The matrix (``GITHUB_OPS``) is the principal artifact (scope-first per
CLAUDE.md): the pin test below freezes it; intentional widenings must
update the pin in the SAME commit (pre-commit checklist #6).

Secret fixtures are obviously fake (``DUMMY_GITHUB_TEST_PAT``) — never
realistic prefixes, per the 2026-04-20 GitGuardian incident.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import structlog

from alfred.integrations.github_ops import (
    DEFAULT_AUDIT_LOG_PATH,
    GITHUB_OPS,
    GitHubOpsClient,
    GitHubOpsConfig,
    GitHubOpsDenied,
    GitHubOpsNotConfigured,
    GitHubOpsWrongInstance,
    ISSUE_MARKER_TEMPLATE,
    _check_github_op,
    append_github_audit,
    build_github_client,
    issue_marker,
    load_github_config,
    read_github_audit,
)
from alfred.integrations import github_ops as github_ops_mod


DUMMY_PAT = "DUMMY_GITHUB_TEST_PAT"
TEST_REPO = "newtonium-errant/transport-admin-portal"
TEST_OWNER, TEST_NAME = TEST_REPO.split("/")
GITHUB_API_BASE = "https://api.github.com"
# Sovereign Forgejo base — forge_type is config-selected; the forgejo
# fixtures opt in via _forgejo_config / _forgejo_raw. The DEFAULT _config
# is GitHub (forge_type/api_base omitted) — the byte-identical baseline.
FORGEJO_API_BASE = "https://git.algernon.test/api/v1"


def _config(tmp_path: Path, **overrides) -> GitHubOpsConfig:
    """GitHub-default config (forge_type/api_base left at the dataclass
    defaults) — the pre-port behavior baseline."""
    kwargs = dict(
        repo=TEST_REPO,
        pat=DUMMY_PAT,
        instance="kal-le",
        audit_log_path=str(tmp_path / "github_ops_audit.jsonl"),
    )
    kwargs.update(overrides)
    return GitHubOpsConfig(**kwargs)


def _forgejo_config(tmp_path: Path, **overrides) -> GitHubOpsConfig:
    """Forgejo-selected config (forge_type=forgejo + a Forgejo base)."""
    return _config(
        tmp_path,
        forge_type="forgejo",
        api_base=FORGEJO_API_BASE,
        **overrides,
    )


def _raw(tmp_path: Path, **overrides) -> dict:
    section = dict(
        repo=TEST_REPO,
        pat=DUMMY_PAT,
        instance="kal-le",
        audit_log_path=str(tmp_path / "github_ops_audit.jsonl"),
    )
    section.update(overrides)
    return {"github": section}


def _forgejo_raw(tmp_path: Path, **overrides) -> dict:
    return _raw(
        tmp_path,
        forge_type="forgejo",
        api_base=FORGEJO_API_BASE,
        **overrides,
    )


def _response(
    status: int,
    json_data,
    method: str = "GET",
    url: str = "https://api.github.com/test",
) -> httpx.Response:
    return httpx.Response(
        status, json=json_data, request=httpx.Request(method, url),
    )


class _CapturingRequest:
    """Monkeypatch stand-in for the module-level ``_github_request``."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.calls: list[dict] = []

    async def __call__(self, method, url, *, headers, params=None, json_body=None):
        self.calls.append({
            "method": method,
            "url": url,
            "headers": headers,
            "params": params,
            "json_body": json_body,
        })
        return self.response


class _RoutingRequest:
    """Monkeypatch stand-in routing each call to a (method, url-substring)
    response. Needed since ``issue_create`` now makes TWO calls — the
    Forgejo ``GET /labels`` name→id pre-fetch, then the issue POST. A call
    matching no route raises (a missed endpoint is a loud failure, never a
    silent None)."""

    def __init__(self, routes: list[tuple[str, str, httpx.Response]]) -> None:
        self.routes = routes
        self.calls: list[dict] = []

    async def __call__(self, method, url, *, headers, params=None, json_body=None):
        self.calls.append({
            "method": method,
            "url": url,
            "headers": headers,
            "params": params,
            "json_body": json_body,
        })
        for m, substr, resp in self.routes:
            if method == m and substr in url:
                return resp
        raise AssertionError(f"no route for {method} {url}")


# ---------------------------------------------------------------------------
# THE MATRIX PIN — contract freeze
# ---------------------------------------------------------------------------


class TestMatrixPin:
    def test_github_ops_matrix_exact(self) -> None:
        """CONTRACT PIN: freezes the op × caller matrix.

        An intentional widening (new op, new caller context) MUST
        update this literal in the same commit — that's the design
        (pre-commit checklist #6, same shape as the KALLE_CREATE_TYPES
        pins). A failure here means the matrix changed without its
        contract test.
        """
        assert GITHUB_OPS == {
            "issue_create":    frozenset({"ticket_intake"}),
            "issue_label_add": frozenset({"ticket_intake"}),
            "label_list":      frozenset({"ticket_intake"}),
            "issue_search":    frozenset({"ticket_intake"}),
            "issue_get":       frozenset({"digest", "fix_drafter"}),
            "issue_timeline":  frozenset({"digest"}),
            "pr_get":          frozenset({"digest"}),
            "pr_reviews":      frozenset({"digest"}),
            "issue_list":      frozenset({"fix_drafter"}),
            "pr_list":         frozenset({"fix_drafter"}),
            "pr_create":       frozenset({"fix_drafter"}),
        }

    def test_pr_writes_other_than_create_permanently_denied(self) -> None:
        """Deny-row pin: every PR write EXCEPT ``pr_create`` is
        permanently absent. ``pr_merge`` is the load-bearing deny —
        merge authority is the operator via branch protection, never
        KAL-LE. ``pr_create`` is now a Forgejo-only ``fix_drafter`` op
        (Phase 1B) so it is deliberately NOT in this denied set; its
        own pins are ``test_pr_create_is_fix_drafter_only`` +
        ``test_pr_create_denied_on_github_config``.
        """
        for denied_op in (
            "pr_merge", "pr_comment", "pr_review",
            "pr_close", "issue_comment", "issue_close",
        ):
            assert denied_op not in GITHUB_OPS

    def test_pr_create_is_fix_drafter_only(self) -> None:
        """``pr_create`` is allowed to exactly one caller — the on-box
        drafter. Never ticket_intake, never digest."""
        assert GITHUB_OPS["pr_create"] == frozenset({"fix_drafter"})

    def test_drafter_ops_are_fix_drafter_only(self) -> None:
        """The three drafter ops are gated to ``fix_drafter`` alone."""
        assert GITHUB_OPS["issue_list"] == frozenset({"fix_drafter"})
        assert GITHUB_OPS["pr_list"] == frozenset({"fix_drafter"})
        assert "fix_drafter" in GITHUB_OPS["issue_get"]


# ---------------------------------------------------------------------------
# Marker
# ---------------------------------------------------------------------------


class TestIssueMarker:
    def test_template_pin(self) -> None:
        # c3 composes this into issue bodies; the dedupe search keys on
        # the inner text. Changing the shape breaks recovery of every
        # already-filed issue — treat as frozen.
        assert ISSUE_MARKER_TEMPLATE == "<!-- algernon-ticket: {ticket_uid} -->"

    def test_issue_marker_renders_uid(self) -> None:
        assert issue_marker("t-123") == "<!-- algernon-ticket: t-123 -->"


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


class TestGate:
    def test_allowed_pair_passes(self) -> None:
        # No raise == allowed.
        _check_github_op("issue_create", "ticket_intake")
        _check_github_op("pr_get", "digest")

    def test_wrong_caller_denied(self) -> None:
        with pytest.raises(GitHubOpsDenied):
            _check_github_op("issue_create", "digest")

    def test_unknown_op_denied(self) -> None:
        with pytest.raises(GitHubOpsDenied):
            _check_github_op("pr_merge", "ticket_intake")

    async def test_client_gate_denial_audits_and_logs(
        self, tmp_path: Path
    ) -> None:
        """Denied call → audit row outcome=denied + github_ops.denied
        log (capture_logs pin, builder checklist #9) + no HTTP."""
        cfg = _config(tmp_path)
        client = GitHubOpsClient(cfg)
        with structlog.testing.capture_logs() as captured:
            with pytest.raises(GitHubOpsDenied):
                await client.issue_create(
                    title="t",
                    body="b",
                    labels=["auto-fix"],
                    ticket_uid="t-1",
                    caller="digest",  # write op from the read-only context
                )
        rows = read_github_audit(cfg.audit_log_path)
        assert len(rows) == 1
        assert rows[0]["op"] == "issue_create"
        assert rows[0]["outcome"] == "denied"
        assert rows[0]["caller"] == "digest"
        assert rows[0]["ticket_uid"] == "t-1"
        denied_logs = [
            c for c in captured if c.get("event") == "github_ops.denied"
        ]
        assert len(denied_logs) == 1
        assert denied_logs[0]["op"] == "issue_create"
        assert denied_logs[0]["caller"] == "digest"
        assert denied_logs[0]["repo"] == TEST_REPO

    async def test_client_unknown_op_denied_via_get_json(
        self, tmp_path: Path
    ) -> None:
        cfg = _config(tmp_path)
        client = GitHubOpsClient(cfg)
        with pytest.raises(GitHubOpsDenied):
            await client._get_json(
                op="pr_merge", caller="digest", url="https://api.github.com/x",
            )
        rows = read_github_audit(cfg.audit_log_path)
        assert rows[0]["outcome"] == "denied"


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


class TestLoadGithubConfig:
    def test_absent_section_returns_none(self) -> None:
        assert load_github_config({}) is None
        assert load_github_config({"vault": {"path": "./vault"}}) is None

    def test_loads_fields(self, tmp_path: Path) -> None:
        # Base labels deliberately exclude `auto-fix`: the invariant guard
        # (2026-06-13) strips auto-fix from base labels, so a non-auto-fix
        # base label keeps this test focused on field-loading. The
        # auto-fix-in-base strip path has its own dedicated pin below.
        raw = _raw(
            tmp_path,
            labels=["tracked", "from-vera"],
            label_map={"bug": "bug", "p1": "priority-high"},
        )
        cfg = load_github_config(raw)
        assert cfg is not None
        assert cfg.repo == TEST_REPO
        assert cfg.pat == DUMMY_PAT
        assert cfg.instance == "kal-le"
        assert cfg.labels == ["tracked", "from-vera"]
        # Bare-string map values normalize to 1-element lists (back-compat:
        # the value type is now str | list[str], stored uniformly as list).
        assert cfg.label_map == {"bug": ["bug"], "p1": ["priority-high"]}

    def test_loads_list_label_map_values(self, tmp_path: Path) -> None:
        """List-valued label_map (the auto-fix-gating shape, 2026-06-13):
        a single ticket_type can map to MULTIPLE labels."""
        raw = _raw(
            tmp_path,
            labels=[],
            label_map={"bug": ["bug", "auto-fix"], "enhancement": ["enhancement"]},
        )
        cfg = load_github_config(raw)
        assert cfg is not None
        assert cfg.labels == []
        assert cfg.label_map == {
            "bug": ["bug", "auto-fix"],
            "enhancement": ["enhancement"],
        }

    def test_label_map_mixed_and_blank_values_tolerated(
        self, tmp_path: Path,
    ) -> None:
        """Mixed str / list values coexist; blank/None entries become []."""
        raw = _raw(
            tmp_path,
            label_map={
                "bug": ["bug", "auto-fix"],
                "p1": "priority-high",   # bare string -> 1-element list
                "blank": "",             # empty string -> []
                "none": None,            # None -> []
                "spacey": ["  keep  ", "  ", ""],  # blanks dropped, trimmed
            },
        )
        cfg = load_github_config(raw)
        assert cfg is not None
        assert cfg.label_map == {
            "bug": ["bug", "auto-fix"],
            "p1": ["priority-high"],
            "blank": [],
            "none": [],
            "spacey": ["keep"],
        }

    def test_defaults(self) -> None:
        # The legacy base-labels default is ["auto-fix"], but the auto-fix
        # invariant guard (2026-06-13) strips auto-fix from base labels —
        # auto-fix may live ONLY under label_map["bug"]. So an omitted
        # `labels` key resolves to [] after the guard, with a warning.
        with structlog.testing.capture_logs() as captured:
            cfg = load_github_config({"github": {"repo": TEST_REPO}})
        assert cfg is not None
        assert cfg.labels == []
        assert cfg.label_map == {}
        assert cfg.audit_log_path == DEFAULT_AUDIT_LOG_PATH
        stripped = [
            c for c in captured
            if c.get("event") == "github.config.auto_fix_label_stripped"
        ]
        assert len(stripped) == 1
        assert stripped[0]["location"] == "base-labels"

    def test_api_base_is_config_driven(self, tmp_path: Path) -> None:
        """The forge base is config-driven: an explicit ``api_base`` is
        loaded verbatim; omitting it falls back to the GitHub default."""
        cfg = load_github_config(_raw(tmp_path, api_base=FORGEJO_API_BASE))
        assert cfg is not None
        assert cfg.api_base == FORGEJO_API_BASE

    def test_api_base_defaults_to_github_when_absent(self) -> None:
        cfg = load_github_config({"github": {"repo": TEST_REPO}})
        assert cfg is not None
        assert cfg.api_base == GITHUB_API_BASE

    def test_forge_type_defaults_to_github(self, tmp_path: Path) -> None:
        """BACKWARD-COMPAT: an omitted forge_type → 'github' (the
        byte-identical-to-pre-port path). A box still on GitHub config is
        unaffected by this deploy."""
        cfg = load_github_config(_raw(tmp_path))  # no forge_type key
        assert cfg is not None
        assert cfg.forge_type == "github"
        assert cfg.api_base == GITHUB_API_BASE

    def test_forge_type_loads_forgejo(self, tmp_path: Path) -> None:
        cfg = load_github_config(_forgejo_raw(tmp_path))
        assert cfg is not None
        assert cfg.forge_type == "forgejo"
        assert cfg.api_base == FORGEJO_API_BASE

    def test_forge_type_normalized_lowercase(self, tmp_path: Path) -> None:
        cfg = load_github_config(_raw(tmp_path, forge_type="ForgeJo"))
        assert cfg is not None
        assert cfg.forge_type == "forgejo"

    def test_unknown_forge_type_fails_loud(
        self, tmp_path: Path,
    ) -> None:
        """Forge-type guard (operator directive 2026-07-02): a typo
        (``forgejoo``) FAILS LOUD at config load — raises GitHubOpsError with
        the FORGE_TYPES message — rather than warning + coercing to github
        (which would talk the wrong API to the box). No silent forge coerce
        anywhere."""
        with pytest.raises(github_ops_mod.GitHubOpsError) as exc:
            load_github_config(_raw(tmp_path, forge_type="forgejoo"))
        assert "forge_type must be one of" in str(exc.value)
        assert "forgejoo" in str(exc.value)
        assert "forgejo" in str(exc.value) and "github" in str(exc.value)

    def test_build_client_for_repo_rejects_unsupported_forge(self) -> None:
        """Forge-type guard: the Option-B per-app-repo factory FAILS LOUD on
        an unsupported forge_type (raises GitHubOpsError) rather than silently
        coercing to github — which would give the REST plane a GitHub Bearer
        client while the drafter's git plane defaults to the Forgejo token
        scheme (a silent cross-plane auth mismatch)."""
        with pytest.raises(github_ops_mod.GitHubOpsError) as exc:
            github_ops_mod.build_client_for_repo(
                repo="org/app1", pat=DUMMY_PAT, forge_type="gitlab",
            )
        # the message names the offending value + the valid set.
        assert "gitlab" in str(exc.value)
        assert "forgejo" in str(exc.value) and "github" in str(exc.value)

    def test_build_client_for_repo_builds_valid_forges(self) -> None:
        """The two supported forges build fine, forge_type preserved (no
        coercion): github (default GitHub base) + forgejo (explicit base)."""
        gh = github_ops_mod.build_client_for_repo(
            repo="org/app1", pat=DUMMY_PAT, forge_type="github",
        )
        assert gh.config.forge_type == "github"
        fj = github_ops_mod.build_client_for_repo(
            repo="org/app1", pat=DUMMY_PAT, forge_type="forgejo",
            api_base=FORGEJO_API_BASE,
        )
        assert fj.config.forge_type == "forgejo"

    def test_env_substitution_is_local(self, monkeypatch) -> None:
        """The unified loader does NOT substitute ${VAR}; this loader
        must do it itself (verified against _load_unified_config)."""
        monkeypatch.setenv("ALGERNON_TEST_GITHUB_PAT", DUMMY_PAT)
        cfg = load_github_config(
            {"github": {"repo": TEST_REPO, "pat": "${ALGERNON_TEST_GITHUB_PAT}"}}
        )
        assert cfg is not None
        assert cfg.pat == DUMMY_PAT

    def test_unset_env_var_left_as_placeholder(self, monkeypatch) -> None:
        monkeypatch.delenv("ALGERNON_TEST_GITHUB_PAT", raising=False)
        cfg = load_github_config(
            {"github": {"repo": TEST_REPO, "pat": "${ALGERNON_TEST_GITHUB_PAT}"}}
        )
        assert cfg is not None
        assert cfg.pat == "${ALGERNON_TEST_GITHUB_PAT}"

    def test_pat_excluded_from_repr(self, tmp_path: Path) -> None:
        """The PAT is a credential — ``repr(config)`` must not leak it
        (``field(repr=False)`` on the dataclass). The attribute itself
        stays readable for the HTTP client."""
        cfg = _config(tmp_path)
        assert cfg.pat == DUMMY_PAT
        assert DUMMY_PAT not in repr(cfg)
        assert TEST_REPO in repr(cfg)  # non-secret fields still render

    # ----- auto-fix invariant guard (code-enforced, 2026-06-13) ---------
    # `load_github_config` enforces "auto-fix may appear ONLY under
    # label_map['bug']": it WARNS (intentionally-left-blank: misconfig is
    # observable) and STRIPS auto-fix from any other location. These pins
    # drive the production code path AND assert the warning fires
    # (builder checklist #9, capture_logs).

    def test_auto_fix_under_non_bug_key_warned_and_stripped(
        self, tmp_path: Path,
    ) -> None:
        """auto-fix leaked under an `enhancement` key → warned + stripped;
        the resolved enhancement labels exclude auto-fix. bug's auto-fix
        is left untouched."""
        raw = _raw(
            tmp_path,
            labels=[],
            label_map={
                "bug": ["bug", "auto-fix"],
                "enhancement": ["enhancement", "auto-fix"],  # leak
            },
        )
        with structlog.testing.capture_logs() as captured:
            cfg = load_github_config(raw)
        assert cfg is not None
        # enhancement no longer carries auto-fix; bug still does.
        assert cfg.label_map["enhancement"] == ["enhancement"]
        assert cfg.label_map["bug"] == ["bug", "auto-fix"]
        assert cfg.labels == []
        stripped = [
            c for c in captured
            if c.get("event") == "github.config.auto_fix_label_stripped"
        ]
        assert len(stripped) == 1
        assert stripped[0]["location"] == "label_map['enhancement']"

    def test_auto_fix_in_base_labels_warned_and_stripped(
        self, tmp_path: Path,
    ) -> None:
        """auto-fix in base `labels` → warned + stripped; it survives
        only under label_map['bug']."""
        raw = _raw(
            tmp_path,
            labels=["auto-fix", "from-vera"],
            label_map={"bug": ["bug", "auto-fix"]},
        )
        with structlog.testing.capture_logs() as captured:
            cfg = load_github_config(raw)
        assert cfg is not None
        assert cfg.labels == ["from-vera"]  # auto-fix stripped, rest kept
        assert cfg.label_map["bug"] == ["bug", "auto-fix"]
        stripped = [
            c for c in captured
            if c.get("event") == "github.config.auto_fix_label_stripped"
        ]
        assert len(stripped) == 1
        assert stripped[0]["location"] == "base-labels"

    def test_correct_config_passes_through_untouched(
        self, tmp_path: Path,
    ) -> None:
        """The live KAL-LE shape (labels: [], bug: [bug, auto-fix]) is the
        intended config — no warning, bug still resolves [bug, auto-fix]."""
        raw = _raw(
            tmp_path,
            labels=[],
            label_map={
                "bug": ["bug", "auto-fix"],
                "enhancement": ["enhancement"],
            },
        )
        with structlog.testing.capture_logs() as captured:
            cfg = load_github_config(raw)
        assert cfg is not None
        assert cfg.labels == []
        assert cfg.label_map == {
            "bug": ["bug", "auto-fix"],
            "enhancement": ["enhancement"],
        }
        stripped = [
            c for c in captured
            if c.get("event") == "github.config.auto_fix_label_stripped"
        ]
        assert stripped == []  # correct config: untouched, no warning


# ---------------------------------------------------------------------------
# Fail-loud factory
# ---------------------------------------------------------------------------


class TestBuildGithubClient:
    def test_happy_path(self, tmp_path: Path) -> None:
        client = build_github_client(_raw(tmp_path), "kal-le")
        assert isinstance(client, GitHubOpsClient)
        assert client.config.repo == TEST_REPO

    def test_missing_section_fails_loud_and_audits(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # The denied row for a configless build lands at the DEFAULT
        # audit path (no config to read one from) — chdir to tmp so
        # the relative default stays inside the test sandbox.
        monkeypatch.chdir(tmp_path)
        with pytest.raises(GitHubOpsNotConfigured) as exc_info:
            build_github_client({}, "kal-le")
        assert "section absent" in str(exc_info.value)
        rows = read_github_audit(tmp_path / "data" / "github_ops_audit.jsonl")
        assert len(rows) == 1
        assert rows[0]["outcome"] == "denied"
        assert rows[0]["caller"] == "kal-le"

    def test_unsubstituted_pat_fails_loud(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # delenv guard (CLAUDE.md dispatcher env-var test-hygiene
        # contract): the operator's REAL .env sets this var, and other
        # tests in the full suite load .env into os.environ — without
        # the delenv, the placeholder substitutes and the fail-loud
        # path never fires (caught in the c1+c2 full-suite run).
        monkeypatch.delenv("ALGERNON_KALLE_GITHUB_PAT", raising=False)
        raw = _raw(tmp_path, pat="${ALGERNON_KALLE_GITHUB_PAT}")
        with pytest.raises(GitHubOpsNotConfigured) as exc_info:
            build_github_client(raw, "kal-le")
        # The message names what's missing — env var not set.
        assert "pat" in str(exc_info.value)
        assert "ALGERNON_KALLE_GITHUB_PAT" in str(exc_info.value)
        rows = read_github_audit(tmp_path / "github_ops_audit.jsonl")
        assert len(rows) == 1
        assert rows[0]["outcome"] == "denied"

    def test_empty_pat_fails_loud(self, tmp_path: Path) -> None:
        with pytest.raises(GitHubOpsNotConfigured):
            build_github_client(_raw(tmp_path, pat=""), "kal-le")
        rows = read_github_audit(tmp_path / "github_ops_audit.jsonl")
        assert rows[0]["outcome"] == "denied"

    def test_empty_repo_fails_loud(self, tmp_path: Path) -> None:
        with pytest.raises(GitHubOpsNotConfigured) as exc_info:
            build_github_client(_raw(tmp_path, repo=""), "kal-le")
        assert "repo" in str(exc_info.value)
        rows = read_github_audit(tmp_path / "github_ops_audit.jsonl")
        assert rows[0]["outcome"] == "denied"

    def test_empty_instance_fails_loud(self, tmp_path: Path) -> None:
        with pytest.raises(GitHubOpsNotConfigured) as exc_info:
            build_github_client(_raw(tmp_path, instance=""), "kal-le")
        assert "instance" in str(exc_info.value)

    def test_wrong_instance_fails_loud_and_audits(self, tmp_path: Path) -> None:
        """The privilege-boundary gate: Salem can't build KAL-LE's client."""
        with pytest.raises(GitHubOpsWrongInstance) as exc_info:
            build_github_client(_raw(tmp_path), "salem")
        # Message names BOTH instances.
        assert "kal-le" in str(exc_info.value)
        assert "salem" in str(exc_info.value)
        rows = read_github_audit(tmp_path / "github_ops_audit.jsonl")
        assert len(rows) == 1
        assert rows[0]["outcome"] == "denied"
        assert rows[0]["caller"] == "salem"
        assert rows[0]["repo"] == TEST_REPO


# ---------------------------------------------------------------------------
# Client methods (mocked transport — watches-module monkeypatch convention)
# ---------------------------------------------------------------------------


class TestIssueCreate:
    @staticmethod
    def _labels_response(labels: list[dict]) -> httpx.Response:
        return _response(200, labels, url=f"{FORGEJO_API_BASE}/x/labels")

    async def test_github_path_posts_label_names_unchanged(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """BACKWARD-COMPAT: on the default GitHub config, issue_create
        POSTs label NAME strings directly — NO labels GET pre-fetch,
        single call, github.com URL, and the EXACT pre-port headers
        (``Bearer`` + ``application/vnd.github+json``) — byte-identical to
        pre-port."""
        cfg = _config(tmp_path)  # github default
        client = GitHubOpsClient(cfg)
        fake = _CapturingRequest(_response(
            201,
            {"number": 42, "html_url": f"https://github.com/{TEST_REPO}/issues/42"},
            method="POST",
        ))
        monkeypatch.setattr(github_ops_mod, "_github_request", fake)

        result = await client.issue_create(
            title="VERA: portal 500 on login",
            body=f"details\n\n{issue_marker('t-42')}",
            labels=["auto-fix", "bug"],
            ticket_uid="t-42",
            caller="ticket_intake",
            correlation_id="corr-1",
        )
        assert result["number"] == 42
        assert len(fake.calls) == 1  # POST only — no labels GET on github
        call = fake.calls[0]
        assert call["method"] == "POST"
        assert call["url"] == f"https://api.github.com/repos/{TEST_REPO}/issues"
        # GitHub headers byte-identical to pre-port (Bearer + vnd.github+json).
        assert call["headers"]["Authorization"] == f"Bearer {DUMMY_PAT}"
        assert call["headers"]["Accept"] == "application/vnd.github+json"
        # Names POSTed unchanged (NOT resolved to ints).
        assert call["json_body"]["labels"] == ["auto-fix", "bug"]
        rows = read_github_audit(cfg.audit_log_path)
        assert [r["op"] for r in rows] == ["issue_create"]  # no label_list row
        assert rows[0]["outcome"] == "created"

    async def test_forgejo_resolves_label_ids(self, tmp_path: Path, monkeypatch) -> None:
        """Forgejo flow: a GET /labels name→id pre-fetch resolves the
        config NAME strings to integer IDs, then the POST body carries
        []int64 — never the names (Forgejo drops name-labels silently).
        Headers use the ``token`` scheme + ``application/json``."""
        cfg = _forgejo_config(tmp_path)
        client = GitHubOpsClient(cfg)
        labels_resp = self._labels_response([
            {"id": 10, "name": "auto-fix"},
            {"id": 20, "name": "bug"},
            {"id": 30, "name": "enhancement"},
        ])
        create_resp = _response(
            201,
            {"number": 42, "html_url": f"{FORGEJO_API_BASE}/{TEST_REPO}/issues/42"},
            method="POST",
        )
        fake = _RoutingRequest([
            ("GET", f"/repos/{TEST_OWNER}/{TEST_NAME}/labels", labels_resp),
            ("POST", f"/repos/{TEST_REPO}/issues", create_resp),
        ])
        monkeypatch.setattr(github_ops_mod, "_github_request", fake)

        result = await client.issue_create(
            title="VERA: portal 500 on login",
            body=f"details\n\n{issue_marker('t-42')}",
            labels=["auto-fix", "bug"],
            ticket_uid="t-42",
            caller="ticket_intake",
            correlation_id="corr-1",
        )

        assert result == {
            "number": 42,
            "html_url": f"{FORGEJO_API_BASE}/{TEST_REPO}/issues/42",
        }
        # Two calls: labels GET (name→id), then issue POST.
        assert len(fake.calls) == 2
        labels_call = fake.calls[0]
        assert labels_call["method"] == "GET"
        assert labels_call["url"] == (
            f"{FORGEJO_API_BASE}/repos/{TEST_OWNER}/{TEST_NAME}/labels"
        )
        post_call = fake.calls[1]
        assert post_call["method"] == "POST"
        assert post_call["url"] == f"{FORGEJO_API_BASE}/repos/{TEST_REPO}/issues"
        assert post_call["headers"]["Authorization"] == f"token {DUMMY_PAT}"
        assert post_call["headers"]["Accept"] == "application/json"
        assert post_call["headers"]["User-Agent"] == "algernon-github-ops"
        # The KEY assertion: names resolved to integer IDs ([]int64).
        assert post_call["json_body"]["labels"] == [10, 20]
        assert post_call["json_body"]["title"] == "VERA: portal 500 on login"

        rows = read_github_audit(cfg.audit_log_path)
        # One label_list (ok) row + one issue_create (created) row.
        assert [r["op"] for r in rows] == ["label_list", "issue_create"]
        created = rows[1]
        assert created["outcome"] == "created"
        assert created["issue_number"] == 42
        assert created["ticket_uid"] == "t-42"
        assert created["correlation_id"] == "corr-1"

    async def test_forgejo_label_names_resolve_to_ids(self, tmp_path: Path, monkeypatch) -> None:
        """Name→id resolution pin: each config label NAME maps to its
        Forgejo integer id via the labels map; case-insensitive."""
        cfg = _forgejo_config(tmp_path)
        client = GitHubOpsClient(cfg)
        labels_resp = self._labels_response([
            {"id": 7, "name": "Bug"},        # mixed case in the forge
            {"id": 9, "name": "auto-fix"},
        ])
        fake = _RoutingRequest([
            ("GET", "/labels", labels_resp),
            ("POST", "/issues", _response(201, {"number": 1, "html_url": ""}, method="POST")),
        ])
        monkeypatch.setattr(github_ops_mod, "_github_request", fake)
        await client.issue_create(
            title="t", body="b", labels=["bug", "auto-fix"],
            ticket_uid="t-1", caller="ticket_intake",
        )
        post_call = fake.calls[1]
        assert post_call["json_body"]["labels"] == [7, 9]

    async def test_forgejo_unresolved_label_warns_and_is_dropped_not_silent(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """SILENT-FAILURE FIREBREAK (forgejo path): a label name with no
        matching forge id must WARN loudly (``github_ops.label_unresolved``)
        and be dropped — never silently swallowed (a dropped ``auto-fix``
        takes the whole auto-fix flow dark while looking healthy). The
        resolved labels still carry the names that DID match."""
        cfg = _forgejo_config(tmp_path)
        client = GitHubOpsClient(cfg)
        labels_resp = self._labels_response([
            {"id": 20, "name": "bug"},
            # NOTE: no "auto-fix" label exists in this forge repo.
        ])
        fake = _RoutingRequest([
            ("GET", "/labels", labels_resp),
            ("POST", "/issues", _response(201, {"number": 1, "html_url": ""}, method="POST")),
        ])
        monkeypatch.setattr(github_ops_mod, "_github_request", fake)
        with structlog.testing.capture_logs() as captured:
            await client.issue_create(
                title="t", body="b", labels=["bug", "auto-fix"],
                ticket_uid="t-1", caller="ticket_intake",
            )
        post_call = fake.calls[1]
        assert post_call["json_body"]["labels"] == [20]  # auto-fix dropped
        warns = [
            c for c in captured
            if c.get("event") == "github_ops.label_unresolved"
        ]
        assert len(warns) == 1
        assert warns[0]["label"] == "auto-fix"
        assert warns[0]["repo"] == TEST_REPO
        assert warns[0]["ticket_uid"] == "t-1"

    async def test_forgejo_empty_labels_skips_label_list_fetch(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """No labels → no GET /labels round-trip (early return); the POST
        body carries an empty []int64."""
        cfg = _forgejo_config(tmp_path)
        client = GitHubOpsClient(cfg)
        fake = _CapturingRequest(_response(201, {"number": 1, "html_url": ""}, method="POST"))
        monkeypatch.setattr(github_ops_mod, "_github_request", fake)
        await client.issue_create(
            title="t", body="b", labels=[],
            ticket_uid="t-1", caller="ticket_intake",
        )
        assert len(fake.calls) == 1  # POST only — no labels GET
        assert fake.calls[0]["method"] == "POST"
        assert fake.calls[0]["json_body"]["labels"] == []

    async def test_forgejo_create_denial_skips_label_fetch(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """On the forgejo path the create op is gated BEFORE the label
        pre-fetch — a denied caller audits ONE issue_create denied row and
        makes NO labels GET."""
        cfg = _forgejo_config(tmp_path)
        client = GitHubOpsClient(cfg)
        called: list = []

        async def _should_not_run(method, url, *, headers, params=None, json_body=None):
            called.append(url)
            return _response(200, [])

        monkeypatch.setattr(github_ops_mod, "_github_request", _should_not_run)
        with pytest.raises(GitHubOpsDenied):
            await client.issue_create(
                title="t", body="b", labels=["auto-fix"],
                ticket_uid="t-1", caller="digest",  # wrong context
            )
        assert called == []  # no labels GET, no POST
        rows = read_github_audit(cfg.audit_log_path)
        assert len(rows) == 1
        assert rows[0]["op"] == "issue_create"
        assert rows[0]["outcome"] == "denied"

    async def test_forgejo_label_list_500_raises_and_makes_no_issue_post(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """FIREBREAK PIN (auto-fix-never-silently-lost): if the forgejo
        label_list GET fails (HTTP 500), issue_create must RAISE
        (HTTPStatusError) and make ZERO issue POSTs — an issue is NEVER
        created without its resolved labels (which would drop ``auto-fix``
        and take the whole flow dark while looking healthy). c3's
        containment then retries the entire intake; the marker-search
        guard prevents a duplicate on retry."""
        cfg = _forgejo_config(tmp_path)
        client = GitHubOpsClient(cfg)
        posts: list = []

        async def _req(method, url, *, headers, params=None, json_body=None):
            if method == "GET" and "/labels" in url:
                return _response(500, {"message": "Internal Server Error"})
            if method == "POST" and "/issues" in url:
                posts.append(url)
                return _response(201, {"number": 1, "html_url": ""}, method="POST")
            raise AssertionError(f"unexpected {method} {url}")

        monkeypatch.setattr(github_ops_mod, "_github_request", _req)
        with pytest.raises(httpx.HTTPStatusError):
            await client.issue_create(
                title="t", body="b", labels=["auto-fix"],
                ticket_uid="t-1", caller="ticket_intake",
            )
        # The load-bearing assertion: NO issue was ever POSTed.
        assert posts == []
        # And no "created" issue row — only the label_list error row.
        rows = read_github_audit(cfg.audit_log_path)
        assert not any(r["outcome"] == "created" for r in rows)
        assert any(
            r["op"] == "label_list" and r["outcome"] == "error"
            and r["http_status"] == 500
            for r in rows
        )

    async def test_http_error_audited_and_propagates(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """422/500 → audit outcome=error with http_status; the
        HTTPStatusError propagates (c3 owns containment)."""
        cfg = _config(tmp_path)
        client = GitHubOpsClient(cfg)
        fake = _CapturingRequest(_response(
            422, {"message": "Validation Failed"}, method="POST",
        ))
        monkeypatch.setattr(github_ops_mod, "_github_request", fake)

        with pytest.raises(httpx.HTTPStatusError):
            await client.issue_create(
                title="t", body="b", labels=[], ticket_uid="t-9",
                caller="ticket_intake",
            )
        rows = read_github_audit(cfg.audit_log_path)
        assert len(rows) == 1
        assert rows[0]["outcome"] == "error"
        assert rows[0]["http_status"] == 422
        assert rows[0]["ticket_uid"] == "t-9"

    async def test_transport_error_audited_and_propagates(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        cfg = _config(tmp_path)
        client = GitHubOpsClient(cfg)

        async def _boom(method, url, *, headers, params=None, json_body=None):
            raise httpx.ConnectTimeout("connect timeout")

        monkeypatch.setattr(github_ops_mod, "_github_request", _boom)
        with pytest.raises(httpx.ConnectTimeout):
            await client.issue_create(
                title="t", body="b", labels=[], ticket_uid="t-9",
                caller="ticket_intake",
            )
        rows = read_github_audit(cfg.audit_log_path)
        assert rows[0]["outcome"] == "error"
        assert rows[0]["http_status"] is None
        assert "ConnectTimeout" in rows[0]["error"]


class TestIssueSearchMarker:
    @staticmethod
    def _issue(number: int, *, marker_uid: str | None, state: str = "open") -> dict:
        """A Forgejo issue dict; its body carries the marker iff
        ``marker_uid`` is set."""
        body = "some ticket text"
        if marker_uid is not None:
            body = f"{body}\n\n{issue_marker(marker_uid)}"
        return {
            "number": number,
            "html_url": f"{FORGEJO_API_BASE}/{TEST_REPO}/issues/{number}",
            "state": state,
            "body": body,
        }

    # ----- GitHub path (pre-port behavior, unchanged) -------------------

    async def test_github_hit_returns_first_match(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """BACKWARD-COMPAT: github config → global /search/issues +
        ``{items}`` parse + the mandatory ``is:issue`` query."""
        cfg = _config(tmp_path)  # github default
        client = GitHubOpsClient(cfg)
        fake = _CapturingRequest(_response(200, {
            "total_count": 1,
            "items": [{
                "number": 7,
                "html_url": f"https://github.com/{TEST_REPO}/issues/7",
                "state": "open",
            }],
        }))
        monkeypatch.setattr(github_ops_mod, "_github_request", fake)

        hit = await client.issue_search_marker(
            ticket_uid="t-7", caller="ticket_intake",
        )
        assert hit == {
            "number": 7,
            "html_url": f"https://github.com/{TEST_REPO}/issues/7",
            "state": "open",
        }
        call = fake.calls[0]
        assert call["url"] == "https://api.github.com/search/issues"
        assert call["params"]["q"] == (
            f'repo:{TEST_REPO} is:issue in:body "algernon-ticket: t-7"'
        )
        rows = read_github_audit(cfg.audit_log_path)
        assert rows[0]["op"] == "issue_search"
        assert rows[0]["issue_number"] == 7
        assert rows[0]["match_count"] == 1

    async def test_github_miss_returns_none(self, tmp_path: Path, monkeypatch) -> None:
        cfg = _config(tmp_path)  # github default
        client = GitHubOpsClient(cfg)
        fake = _CapturingRequest(_response(200, {"total_count": 0, "items": []}))
        monkeypatch.setattr(github_ops_mod, "_github_request", fake)
        hit = await client.issue_search_marker(
            ticket_uid="t-none", caller="ticket_intake",
        )
        assert hit is None
        rows = read_github_audit(cfg.audit_log_path)
        assert rows[0]["issue_number"] is None
        assert rows[0]["match_count"] == 0

    async def test_github_query_contains_mandatory_qualifiers(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """REGRESSION PIN (2026-06-11 422 outage): GitHub's /search/issues
        REQUIRES a type qualifier — a query missing ``is:issue`` 422s."""
        cfg = _config(tmp_path)  # github default
        client = GitHubOpsClient(cfg)
        fake = _CapturingRequest(_response(200, {"total_count": 0, "items": []}))
        monkeypatch.setattr(github_ops_mod, "_github_request", fake)
        await client.issue_search_marker(
            ticket_uid="t-pin", caller="ticket_intake",
        )
        q = fake.calls[0]["params"]["q"]
        assert "is:issue" in q
        assert f"repo:{TEST_REPO}" in q
        assert "in:body" in q
        assert "algernon-ticket: t-pin" in q

    # ----- Forgejo path -------------------------------------------------

    async def test_forgejo_hit_returns_first_body_matched_issue(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        cfg = _forgejo_config(tmp_path)
        client = GitHubOpsClient(cfg)
        # Forgejo returns a BARE LIST (not GitHub's {"items": [...]}).
        fake = _CapturingRequest(_response(
            200, [self._issue(7, marker_uid="t-7", state="open")],
        ))
        monkeypatch.setattr(github_ops_mod, "_github_request", fake)

        hit = await client.issue_search_marker(
            ticket_uid="t-7", caller="ticket_intake",
        )
        assert hit == {
            "number": 7,
            "html_url": f"{FORGEJO_API_BASE}/{TEST_REPO}/issues/7",
            "state": "open",
        }
        # Forgejo per-repo issue endpoint + the type/state/q/limit params.
        call = fake.calls[0]
        assert call["url"] == (
            f"{FORGEJO_API_BASE}/repos/{TEST_OWNER}/{TEST_NAME}/issues"
        )
        assert call["params"] == {
            "type": "issues", "state": "all", "q": "t-7", "limit": 50,
        }
        rows = read_github_audit(cfg.audit_log_path)
        assert rows[0]["op"] == "issue_search"
        assert rows[0]["outcome"] == "ok"
        assert rows[0]["issue_number"] == 7
        assert rows[0]["match_count"] == 1

    async def test_forgejo_miss_returns_none_on_bare_empty_list(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        cfg = _forgejo_config(tmp_path)
        client = GitHubOpsClient(cfg)
        fake = _CapturingRequest(_response(200, []))
        monkeypatch.setattr(github_ops_mod, "_github_request", fake)

        hit = await client.issue_search_marker(
            ticket_uid="t-none", caller="ticket_intake",
        )
        assert hit is None
        rows = read_github_audit(cfg.audit_log_path)
        assert rows[0]["outcome"] == "ok"
        assert rows[0]["issue_number"] is None
        assert rows[0]["match_count"] == 0

    async def test_forgejo_dedup_firebreak_bare_list_body_match_state_all(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """REGRESSION PIN — the duplicate-mint firebreak (forgejo path).

        Three stacked correctness checks, each a duplicate-mint cause if
        broken (verified to FAIL against the pre-port GitHub code):

        1. **BARE LIST parse** — the response is a list, not
           ``{"items": [...]}``. The old ``data.get("items")`` returns
           None on a list → no hit → re-mint. Here a hit MUST be found.
        2. **``state=all``** — the matching issue is CLOSED (a wont_fix /
           resolved ticket). Forgejo defaults to ``state=open``; without
           ``state=all`` it'd be invisible → re-mint.
        3. **client-side body match is AUTHORITATIVE** — the list also
           contains a coarse-``q`` false positive (an OPEN issue whose
           body does NOT carry the marker). The dedupe must pick the
           CLOSED marker-bearing issue, not the first/open false positive.
        """
        cfg = _forgejo_config(tmp_path)
        client = GitHubOpsClient(cfg)
        fake = _CapturingRequest(_response(200, [
            # coarse-q false positive: no marker in body, returned first
            self._issue(11, marker_uid=None, state="open"),
            # the real match: marker in body, and CLOSED (state=all surfaces it)
            self._issue(7, marker_uid="t-7", state="closed"),
        ]))
        monkeypatch.setattr(github_ops_mod, "_github_request", fake)

        hit = await client.issue_search_marker(
            ticket_uid="t-7", caller="ticket_intake",
        )
        assert hit is not None
        assert hit["number"] == 7           # the marker-bearing one, not #11
        assert hit["state"] == "closed"     # state=all surfaced it
        assert fake.calls[0]["params"]["state"] == "all"
        rows = read_github_audit(cfg.audit_log_path)
        assert rows[0]["match_count"] == 1       # only the body-matched issue
        assert rows[0]["prefilter_count"] == 2   # the coarse list had two


class TestDigestReads:
    # Read ops are forge-agnostic (same path shape; only the base differs).
    # Default github config → github.com URLs, matching pre-port behavior.
    async def test_issue_get(self, tmp_path: Path, monkeypatch) -> None:
        cfg = _config(tmp_path)
        client = GitHubOpsClient(cfg)
        fake = _CapturingRequest(_response(200, {"number": 5, "state": "closed"}))
        monkeypatch.setattr(github_ops_mod, "_github_request", fake)

        data = await client.issue_get(number=5, caller="digest")
        assert data["state"] == "closed"
        assert fake.calls[0]["url"] == (
            f"https://api.github.com/repos/{TEST_REPO}/issues/5"
        )
        rows = read_github_audit(cfg.audit_log_path)
        assert rows[0]["op"] == "issue_get"
        assert rows[0]["outcome"] == "ok"
        assert rows[0]["issue_number"] == 5

    async def test_issue_timeline(self, tmp_path: Path, monkeypatch) -> None:
        cfg = _config(tmp_path)
        client = GitHubOpsClient(cfg)
        fake = _CapturingRequest(_response(200, [{"event": "cross-referenced"}]))
        monkeypatch.setattr(github_ops_mod, "_github_request", fake)

        data = await client.issue_timeline(number=5, caller="digest")
        assert data[0]["event"] == "cross-referenced"
        assert fake.calls[0]["url"] == (
            f"https://api.github.com/repos/{TEST_REPO}/issues/5/timeline"
        )

    async def test_pr_get_and_reviews(self, tmp_path: Path, monkeypatch) -> None:
        cfg = _config(tmp_path)
        client = GitHubOpsClient(cfg)
        fake = _CapturingRequest(_response(200, {"merged": True}))
        monkeypatch.setattr(github_ops_mod, "_github_request", fake)

        await client.pr_get(number=12, caller="digest")
        await client.pr_reviews(number=12, caller="digest")
        assert fake.calls[0]["url"] == (
            f"https://api.github.com/repos/{TEST_REPO}/pulls/12"
        )
        assert fake.calls[1]["url"] == (
            f"https://api.github.com/repos/{TEST_REPO}/pulls/12/reviews"
        )
        rows = read_github_audit(cfg.audit_log_path)
        assert [r["op"] for r in rows] == ["pr_get", "pr_reviews"]
        assert all(r["outcome"] == "ok" for r in rows)

    async def test_forgejo_base_threaded_into_digest_reads(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The config api_base threads into the read ops too — a forgejo
        config hits the forgejo base."""
        cfg = _forgejo_config(tmp_path)
        client = GitHubOpsClient(cfg)
        fake = _CapturingRequest(_response(200, {"merged_at": None}))
        monkeypatch.setattr(github_ops_mod, "_github_request", fake)
        await client.pr_get(number=12, caller="digest")
        assert fake.calls[0]["url"] == (
            f"{FORGEJO_API_BASE}/repos/{TEST_REPO}/pulls/12"
        )

    async def test_digest_read_denied_for_intake_caller(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Cross-context denial: the intake context can't read PRs."""
        cfg = _config(tmp_path)
        client = GitHubOpsClient(cfg)
        with pytest.raises(GitHubOpsDenied):
            await client.pr_get(number=12, caller="ticket_intake")


# ---------------------------------------------------------------------------
# On-box auto-fix drafter ops (Phase 1B, fix_drafter, FORGEJO)
# ---------------------------------------------------------------------------


def _raises_if_called():
    async def _should_not_run(*a, **k):  # pragma: no cover - asserts non-call
        raise AssertionError("HTTP must not be attempted on a forge-denied op")
    return _should_not_run


class TestDrafterOps:
    async def test_issue_list_bare_list_parse_forgejo(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Forgejo issue_list returns a BARE LIST (not {"items": [...]}) —
        the silent-break firebreak. Params carry type/state/labels/limit."""
        cfg = _forgejo_config(tmp_path)
        client = GitHubOpsClient(cfg)
        fake = _CapturingRequest(_response(200, [
            {"number": 7, "title": "bug a", "body": "b", "labels": [{"name": "auto-fix"}]},
            {"number": 9, "title": "bug b", "body": "b2", "labels": [{"name": "auto-fix"}]},
        ]))
        monkeypatch.setattr(github_ops_mod, "_github_request", fake)

        issues = await client.issue_list(
            labels="auto-fix", state="open", caller="fix_drafter",
        )
        assert [i["number"] for i in issues] == [7, 9]
        call = fake.calls[0]
        assert call["url"] == (
            f"{FORGEJO_API_BASE}/repos/{TEST_REPO}/issues"
        )
        assert call["params"] == {
            "type": "issues", "state": "open", "labels": "auto-fix", "limit": 50,
        }
        rows = read_github_audit(cfg.audit_log_path)
        assert rows[0]["op"] == "issue_list"
        assert rows[0]["outcome"] == "ok"
        assert rows[0]["count"] == 2

    async def test_issue_list_non_list_response_is_empty(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A {"items": [...]} (GitHub-shaped) body on the forgejo path is
        treated as EMPTY, not parsed — the bare-list contract is strict."""
        cfg = _forgejo_config(tmp_path)
        client = GitHubOpsClient(cfg)
        fake = _CapturingRequest(_response(200, {"items": [{"number": 7}]}))
        monkeypatch.setattr(github_ops_mod, "_github_request", fake)
        issues = await client.issue_list(
            labels="auto-fix", state="open", caller="fix_drafter",
        )
        assert issues == []

    async def test_pr_list_bare_list_parse_forgejo(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        cfg = _forgejo_config(tmp_path)
        client = GitHubOpsClient(cfg)
        fake = _CapturingRequest(_response(200, [
            {"number": 3, "html_url": "u3", "head": {"ref": "auto-fix/issue-7"}},
            {"number": 4, "html_url": "u4", "head": {"ref": "other"}},
        ]))
        monkeypatch.setattr(github_ops_mod, "_github_request", fake)

        prs = await client.pr_list(state="all", caller="fix_drafter")
        assert [p["number"] for p in prs] == [3, 4]
        call = fake.calls[0]
        assert call["url"] == f"{FORGEJO_API_BASE}/repos/{TEST_REPO}/pulls"
        assert call["params"] == {"state": "all", "limit": 50}
        rows = read_github_audit(cfg.audit_log_path)
        assert rows[0]["op"] == "pr_list"
        assert rows[0]["count"] == 2

    async def test_pr_create_posts_head_base_title_body(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        cfg = _forgejo_config(tmp_path)
        client = GitHubOpsClient(cfg)
        fake = _CapturingRequest(_response(
            201, {"number": 42, "html_url": "pr-url"}, method="POST",
        ))
        monkeypatch.setattr(github_ops_mod, "_github_request", fake)

        out = await client.pr_create(
            head="auto-fix/issue-7", base="main",
            title="WIP: bug a", body="Closes #7\n\nsummary",
            caller="fix_drafter", issue_number=7,
        )
        assert out == {"number": 42, "html_url": "pr-url"}
        call = fake.calls[0]
        assert call["method"] == "POST"
        assert call["url"] == f"{FORGEJO_API_BASE}/repos/{TEST_REPO}/pulls"
        # No `draft` key — Forgejo signals WIP via the title prefix.
        assert call["json_body"] == {
            "head": "auto-fix/issue-7", "base": "main",
            "title": "WIP: bug a", "body": "Closes #7\n\nsummary",
        }
        rows = read_github_audit(cfg.audit_log_path)
        assert rows[0]["op"] == "pr_create"
        assert rows[0]["outcome"] == "created"
        assert rows[0]["issue_number"] == 7
        assert rows[0]["pr_number"] == 42

    async def test_pr_create_follows_app_repo_forge_on_github(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """OPTION B FORGE-FENCE REVERSAL: pr_create is NO LONGER forge-fenced
        — on a github-config client it PROCEEDS (the app repo may be GitHub),
        makes the HTTP POST, and is NOT forge-denied. (Inverts the old
        ``test_pr_create_denied_on_github_config``.)"""
        cfg = _config(tmp_path)  # github default (forge_type omitted)
        client = GitHubOpsClient(cfg)
        fake = _CapturingRequest(_response(
            201, {"number": 42, "html_url": "u"}, method="POST",
        ))
        monkeypatch.setattr(github_ops_mod, "_github_request", fake)
        with structlog.testing.capture_logs() as captured:
            out = await client.pr_create(
                head="auto-fix/issue-7", base="main",
                title="WIP: x", body="ref #7",
                caller="fix_drafter", issue_number=7,
            )
        assert out == {"number": 42, "html_url": "u"}
        assert len(fake.calls) == 1                # HTTP happened (not denied)
        rows = read_github_audit(cfg.audit_log_path)
        assert rows[-1]["op"] == "pr_create" and rows[-1]["outcome"] == "created"
        # NO forge_denied log for pr_create any more.
        assert not [c for c in captured if c.get("event") == "github_ops.forge_denied"]

    async def test_pr_list_follows_app_repo_forge_on_github(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """pr_list is no longer forge-fenced either — it PROCEEDS on a github
        client (crash-recovery dedup runs against the app repo)."""
        cfg = _config(tmp_path)  # github default
        client = GitHubOpsClient(cfg)
        fake = _CapturingRequest(_response(200, []))
        monkeypatch.setattr(github_ops_mod, "_github_request", fake)
        prs = await client.pr_list(state="all", caller="fix_drafter")
        assert prs == []
        assert len(fake.calls) == 1                # HTTP happened (not denied)

    async def test_issue_list_STILL_forge_pinned_on_github(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """issue_list STAYS Forgejo-pinned — the drafter only ever SCANS the
        sovereign tracker, never GitHub. A github client → GitHubOpsDenied,
        ZERO HTTP, a forge_denied log."""
        cfg = _config(tmp_path)  # github default
        client = GitHubOpsClient(cfg)
        monkeypatch.setattr(
            github_ops_mod, "_github_request", _raises_if_called(),
        )
        with structlog.testing.capture_logs() as captured:
            with pytest.raises(GitHubOpsDenied):
                await client.issue_list(
                    labels="auto-fix", state="open", caller="fix_drafter",
                )
        rows = read_github_audit(cfg.audit_log_path)
        assert rows[-1]["op"] == "issue_list" and rows[-1]["outcome"] == "denied"
        forge_logs = [
            c for c in captured if c.get("event") == "github_ops.forge_denied"
        ]
        assert len(forge_logs) == 1 and forge_logs[0]["op"] == "issue_list"

    def test_pr_merge_permanently_denied_on_both_forges(self) -> None:
        """pr_merge stays a PERMANENT deny UNDER OPTION B — matrix-absent, so
        the op×caller gate rejects it for ANY caller (the matrix has no forge
        dimension → the denial holds on both github AND forgejo), and it is
        NOT smuggled in via the forge set either."""
        from alfred.integrations.github_ops import _FORGEJO_ONLY_OPS
        assert "pr_merge" not in GITHUB_OPS
        assert "pr_merge" not in _FORGEJO_ONLY_OPS
        for caller in ("fix_drafter", "digest", "ticket_intake"):
            with pytest.raises(GitHubOpsDenied):
                _check_github_op("pr_merge", caller)

    def test_forge_only_ops_is_issue_list_only(self) -> None:
        """CONTRACT PIN (Option B): the forge set is exactly {issue_list}.
        pr_create/pr_list follow the app-repo forge; issue_list stays pinned."""
        from alfred.integrations.github_ops import _FORGEJO_ONLY_OPS
        assert _FORGEJO_ONLY_OPS == frozenset({"issue_list"})

    async def test_drafter_op_denied_for_wrong_caller_on_forgejo(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Even on forgejo, pr_create is denied to a non-drafter caller —
        the op×caller gate runs after the forge-fence passes."""
        cfg = _forgejo_config(tmp_path)
        client = GitHubOpsClient(cfg)
        monkeypatch.setattr(
            github_ops_mod, "_github_request", _raises_if_called(),
        )
        with pytest.raises(GitHubOpsDenied):
            await client.pr_create(
                head="auto-fix/issue-7", base="main",
                title="WIP: x", body="Closes #7", caller="digest",
            )


# ---------------------------------------------------------------------------
# Audit helper
# ---------------------------------------------------------------------------


class TestAppendGithubAudit:
    def test_writes_row_and_creates_parent_dir(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "audit.jsonl"
        append_github_audit(
            path,
            op="issue_create",
            repo=TEST_REPO,
            caller="ticket_intake",
            outcome="created",
            ticket_uid="t-1",
            issue_number=1,
            http_status=201,
            from_peer="vera",
            correlation_id="c-1",
        )
        rows = read_github_audit(path)
        assert len(rows) == 1
        assert rows[0]["from_peer"] == "vera"
        assert rows[0]["http_status"] == 201
        assert rows[0]["ts"]

    def test_extra_cannot_corrupt_core_fields(self, tmp_path: Path) -> None:
        """c5 effectiveness-loop hook: extra adds fields; core wins on
        conflict (same contract as canonical_audit.append_audit)."""
        path = tmp_path / "audit.jsonl"
        append_github_audit(
            path,
            op="pr_get",
            repo=TEST_REPO,
            caller="digest",
            outcome="ok",
            extra={
                "op": "EVIL-overwrite",
                "pr_number": 12,
                "pr_state": "merged",
                "disposition": "merged_after_rework",
                "latency_days": 3,
            },
        )
        rows = read_github_audit(path)
        assert rows[0]["op"] == "pr_get"  # core wins
        assert rows[0]["pr_number"] == 12
        assert rows[0]["disposition"] == "merged_after_rework"

    def test_never_raises_on_unwritable_path(self, tmp_path: Path) -> None:
        """Never-raises pin + capture_logs pin on the failure warning."""
        blocker = tmp_path / "blocker"
        blocker.write_text("a file, not a dir", encoding="utf-8")
        bad_path = blocker / "sub" / "audit.jsonl"  # mkdir will raise
        with structlog.testing.capture_logs() as captured:
            append_github_audit(
                bad_path,
                op="issue_create",
                repo=TEST_REPO,
                caller="ticket_intake",
                outcome="created",
            )  # must not raise
        events = [
            c for c in captured
            if c.get("event") == "github_ops.audit_write_failed"
        ]
        assert len(events) == 1
        assert events[0]["error_type"]
        assert events[0]["path"] == str(bad_path)

    def test_empty_path_is_noop(self) -> None:
        append_github_audit(
            "",
            op="issue_create",
            repo=TEST_REPO,
            caller="ticket_intake",
            outcome="created",
        )  # no raise, nothing written anywhere
