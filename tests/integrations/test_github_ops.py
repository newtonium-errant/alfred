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


def _config(tmp_path: Path, **overrides) -> GitHubOpsConfig:
    kwargs = dict(
        repo=TEST_REPO,
        pat=DUMMY_PAT,
        instance="kal-le",
        audit_log_path=str(tmp_path / "github_ops_audit.jsonl"),
    )
    kwargs.update(overrides)
    return GitHubOpsConfig(**kwargs)


def _raw(tmp_path: Path, **overrides) -> dict:
    section = dict(
        repo=TEST_REPO,
        pat=DUMMY_PAT,
        instance="kal-le",
        audit_log_path=str(tmp_path / "github_ops_audit.jsonl"),
    )
    section.update(overrides)
    return {"github": section}


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
            "issue_search":    frozenset({"ticket_intake"}),
            "issue_get":       frozenset({"digest"}),
            "issue_timeline":  frozenset({"digest"}),
            "pr_get":          frozenset({"digest"}),
            "pr_reviews":      frozenset({"digest"}),
        }

    def test_no_pr_write_ops_in_matrix(self) -> None:
        """Deny-row pin: PR writes are permanently absent (GHA owns
        PRs; merge authority is the operator via branch protection)."""
        for denied_op in (
            "pr_create", "pr_merge", "pr_comment", "pr_review",
            "pr_close", "issue_comment", "issue_close",
        ):
            assert denied_op not in GITHUB_OPS


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
        raw = _raw(
            tmp_path,
            labels=["auto-fix", "from-vera"],
            label_map={"bug": "bug", "p1": "priority-high"},
        )
        cfg = load_github_config(raw)
        assert cfg is not None
        assert cfg.repo == TEST_REPO
        assert cfg.pat == DUMMY_PAT
        assert cfg.instance == "kal-le"
        assert cfg.labels == ["auto-fix", "from-vera"]
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
        cfg = load_github_config({"github": {"repo": TEST_REPO}})
        assert cfg is not None
        assert cfg.labels == ["auto-fix"]
        assert cfg.label_map == {}
        assert cfg.audit_log_path == DEFAULT_AUDIT_LOG_PATH

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
    async def test_happy_path(self, tmp_path: Path, monkeypatch) -> None:
        cfg = _config(tmp_path)
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

        assert result == {
            "number": 42,
            "html_url": f"https://github.com/{TEST_REPO}/issues/42",
        }
        assert len(fake.calls) == 1
        call = fake.calls[0]
        assert call["method"] == "POST"
        assert call["url"] == f"https://api.github.com/repos/{TEST_REPO}/issues"
        assert call["headers"]["Authorization"] == f"Bearer {DUMMY_PAT}"
        assert call["headers"]["Accept"] == "application/vnd.github+json"
        assert call["headers"]["User-Agent"] == "algernon-github-ops"
        assert call["json_body"]["labels"] == ["auto-fix", "bug"]
        assert call["json_body"]["title"] == "VERA: portal 500 on login"

        rows = read_github_audit(cfg.audit_log_path)
        assert len(rows) == 1
        assert rows[0]["op"] == "issue_create"
        assert rows[0]["outcome"] == "created"
        assert rows[0]["issue_number"] == 42
        assert rows[0]["ticket_uid"] == "t-42"
        assert rows[0]["correlation_id"] == "corr-1"

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
    async def test_hit_returns_first_match(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        cfg = _config(tmp_path)
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
        # The search queries the INNER marker text on the right repo.
        call = fake.calls[0]
        assert call["url"] == "https://api.github.com/search/issues"
        assert call["params"]["q"] == (
            f'repo:{TEST_REPO} is:issue in:body "algernon-ticket: t-7"'
        )
        rows = read_github_audit(cfg.audit_log_path)
        assert rows[0]["op"] == "issue_search"
        assert rows[0]["outcome"] == "ok"
        assert rows[0]["issue_number"] == 7
        assert rows[0]["match_count"] == 1

    async def test_miss_returns_none(self, tmp_path: Path, monkeypatch) -> None:
        cfg = _config(tmp_path)
        client = GitHubOpsClient(cfg)
        fake = _CapturingRequest(_response(200, {"total_count": 0, "items": []}))
        monkeypatch.setattr(github_ops_mod, "_github_request", fake)

        hit = await client.issue_search_marker(
            ticket_uid="t-none", caller="ticket_intake",
        )
        assert hit is None
        rows = read_github_audit(cfg.audit_log_path)
        assert rows[0]["outcome"] == "ok"
        assert rows[0]["issue_number"] is None
        assert rows[0]["match_count"] == 0

    async def test_query_contains_mandatory_qualifiers(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """REGRESSION PIN (2026-06-11 422 outage): /search/issues now
        REQUIRES a type qualifier — every query missing ``is:issue``
        422s with "Query must include 'is:issue' or 'is:pull-request'"
        (60/60 audit-row failure on KAL-LE's first live tick).

        String-level on purpose: a refactor that rebuilds the query
        must not silently drop any of these four parts.
        """
        cfg = _config(tmp_path)
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


class TestDigestReads:
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

    async def test_digest_read_denied_for_intake_caller(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Cross-context denial: the intake context can't read PRs."""
        cfg = _config(tmp_path)
        client = GitHubOpsClient(cfg)
        with pytest.raises(GitHubOpsDenied):
            await client.pr_get(number=12, caller="ticket_intake")


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
