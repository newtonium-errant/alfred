"""CLI-handler tests for the Phase 4 pattern miner.

Pins:
- ``alfred distiller mine-patterns`` returns gracefully when the
  config block is absent or ``enabled: false``
- Surveyor-state-missing case prints an explicit empty-state ack
  per the universal "intentionally left blank" rule
- Empty-result case emits ``pattern_miner.no_candidates`` log AND
  writes the .gitkeep marker
- Happy path: gates clusters from a fixture surveyor state, writes
  proposals, records state, prints a summary table
- Dry-run skips disk mutations
- The end-to-end mine_patterns log emission contract (per
  feedback_log_emission_test_pattern.md — observability tests
  drive the production code path)
- The drafter LLM call gets mocked via httpx.MockTransport so tests
  run without a live Ollama
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import structlog

from alfred.distiller import cli as dcli
from alfred.distiller import pattern_miner as pm_mod
from alfred.distiller.config import (
    DistillerConfig,
    PatternMinerConfig,
    PatternMinerOpenRouterConfig,
    PatternMinerStateConfig,
    VaultConfig,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _surveyor_state(clusters: dict[str, dict]) -> dict:
    return {
        "version": 1,
        "last_run": "2026-05-10T00:00:00+00:00",
        "files": {},
        "clusters": clusters,
    }


def _config(
    *,
    vault_path: Path,
    state_path: Path,
    surveyor_state_path: Path,
    enabled: bool = True,
    drafter_endpoint: str = "",
    drafter_model: str = "",
    canonical_dirs: list[str] | None = None,
) -> DistillerConfig:
    cfg = DistillerConfig(vault=VaultConfig(path=str(vault_path)))
    if enabled:
        cfg.pattern_miner = PatternMinerConfig(
            enabled=True,
            surveyor_state_path=str(surveyor_state_path),
            proposed_dir="inbox/proposed-canonical",
            min_cluster_size=3,
            canonical_match_dirs=canonical_dirs or ["architecture", "principles", "stack"],
            label_denylist=[],
            state=PatternMinerStateConfig(path=str(state_path)),
            openrouter=PatternMinerOpenRouterConfig(
                base_url=drafter_endpoint,
                model=drafter_model,
            ),
        )
    return cfg


def _patch_httpx_for_drafter(monkeypatch, dispatch) -> None:  # noqa: ANN001
    """Replace httpx.Client in pattern_miner with a MockTransport-backed
    client so the drafter call never hits the network.

    Mirrors the pattern in test_distiller_ollama_backend.py:_patch_httpx
    but for the sync path (httpx.Client, not AsyncClient — pattern_miner
    runs synchronously inside the CLI handler).
    """
    real_client = httpx.Client

    def _make_client(*args, **kwargs):  # noqa: ANN001
        return real_client(
            transport=httpx.MockTransport(dispatch),
            timeout=kwargs.get("timeout"),
        )

    # call_drafter does ``import httpx`` lazily inside the function —
    # patch the module-level httpx attribute that import resolves to.
    import httpx as httpx_module
    monkeypatch.setattr(httpx_module, "Client", _make_client)


def _drafter_response(content: str) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "qwen2.5:14b",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            },
        ],
    }


# ---------------------------------------------------------------------------
# Disabled-config + missing-block paths
# ---------------------------------------------------------------------------


class TestDisabledOrMissingConfig:
    def test_no_pattern_miner_block_prints_not_enabled(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        cfg = DistillerConfig(vault=VaultConfig(path=str(tmp_path)))
        # cfg.pattern_miner is None by default.
        dcli.cmd_mine_patterns(cfg, dry_run=False)
        out = capsys.readouterr().out
        assert "Pattern miner not enabled in this config." in out

    def test_explicit_disabled_prints_not_enabled(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        cfg = _config(
            vault_path=tmp_path,
            state_path=tmp_path / "s.json",
            surveyor_state_path=tmp_path / "surv.json",
            enabled=True,
        )
        # Flip after construction.
        cfg.pattern_miner.enabled = False  # type: ignore[union-attr]
        dcli.cmd_mine_patterns(cfg, dry_run=False)
        out = capsys.readouterr().out
        assert "Pattern miner not enabled in this config." in out


# ---------------------------------------------------------------------------
# Surveyor-state-missing path (intentionally left blank)
# ---------------------------------------------------------------------------


class TestSurveyorStateMissing:
    def test_missing_surveyor_state_prints_empty_ack(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        cfg = _config(
            vault_path=tmp_path,
            state_path=tmp_path / "s.json",
            surveyor_state_path=tmp_path / "missing_surveyor.json",
        )
        dcli.cmd_mine_patterns(cfg, dry_run=False)
        out = capsys.readouterr().out
        assert "Surveyor state file not found" in out
        assert "Run the surveyor daemon first" in out


# ---------------------------------------------------------------------------
# Empty-state observability — no candidates pass the gate
# ---------------------------------------------------------------------------


class TestEmptyResults:
    def test_no_candidates_emits_log_and_gitkeep(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Surveyor state with one cluster too small to pass min_size=3.
        surveyor_state_path = tmp_path / "surv.json"
        surveyor_state_path.write_text(json.dumps(_surveyor_state({
            "semantic_0": {
                "label": ["topic/x"],
                "member_files": ["a.md"],  # only 1 — fails substantive gate
            },
        })))
        cfg = _config(
            vault_path=tmp_path,
            state_path=tmp_path / "s.json",
            surveyor_state_path=surveyor_state_path,
        )

        with structlog.testing.capture_logs() as captured:
            dcli.cmd_mine_patterns(cfg, dry_run=False)

        # Per feedback_log_emission_test_pattern.md: pin the
        # observability log AND the placeholder file.
        no_cands = [c for c in captured if c.get("event") == "pattern_miner.no_candidates"]
        assert len(no_cands) == 1
        assert no_cands[0]["evaluated"] == 1
        assert no_cands[0]["dry_run"] is False

        # Placeholder marker file written so `ls inbox/proposed-canonical/`
        # shows the daemon ran with last-mined timestamp.
        marker = tmp_path / "inbox" / "proposed-canonical" / ".gitkeep"
        assert marker.is_file()
        content = marker.read_text()
        assert "Candidates evaluated: 1" in content
        assert "Candidates that passed the gate: 0" in content

        out = capsys.readouterr().out
        assert "no new patterns surfaced this run." in out


# ---------------------------------------------------------------------------
# Happy path — gates clusters, writes proposals, records state
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_passes_gate_and_writes_proposal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Surveyor state with one substantive, labeled, non-canonical
        # cluster. Member files exist on disk so the drafter's
        # _build_members_block can read frontmatter.
        (tmp_path / "assumption").mkdir()
        for stem in ("a", "b", "c"):
            (tmp_path / "assumption" / f"{stem}.md").write_text(
                f"---\nname: {stem}\n---\n\nbody {stem}\n",
            )

        surveyor_state_path = tmp_path / "surv.json"
        surveyor_state_path.write_text(json.dumps(_surveyor_state({
            "semantic_5": {
                "label": ["llm/quantization", "performance/benchmarking"],
                "member_files": [
                    "assumption/a.md",
                    "assumption/b.md",
                    "assumption/c.md",
                ],
            },
        })))

        # Mock the drafter LLM. Returns a paragraph + TYPE/SLUG trailer
        # in the format the prompt asks for.
        def _dispatch(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json=_drafter_response(
                    "Local LLM hardware constraints silently degrade extraction.\n"
                    "TYPE: principles\n"
                    "SLUG: local-llm-hardware-constraints\n"
                ),
            )
        _patch_httpx_for_drafter(monkeypatch, _dispatch)

        cfg = _config(
            vault_path=tmp_path,
            state_path=tmp_path / "s.json",
            surveyor_state_path=surveyor_state_path,
            drafter_endpoint="http://172.22.0.1:11434/v1",
            drafter_model="qwen2.5:14b",
        )
        dcli.cmd_mine_patterns(cfg, dry_run=False)

        # Proposal file written with the LLM's slug suggestion.
        target = tmp_path / "inbox" / "proposed-canonical" / "local-llm-hardware-constraints.md"
        assert target.is_file()
        body = target.read_text()
        assert "Local LLM hardware constraints silently degrade extraction." in body
        assert 'proposed_canonical_type: "principles"' in body

        # State file recorded the entry.
        state_path = tmp_path / "s.json"
        assert state_path.is_file()
        state_data = json.loads(state_path.read_text())
        assert len(state_data["proposals"]) == 1
        proposal = next(iter(state_data["proposals"].values()))
        assert proposal["proposed_slug"] == "local-llm-hardware-constraints"

    def test_dry_run_skips_disk_mutations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (tmp_path / "assumption").mkdir()
        for stem in ("a", "b", "c"):
            (tmp_path / "assumption" / f"{stem}.md").write_text(
                f"---\nname: {stem}\n---\n\nbody\n",
            )
        surveyor_state_path = tmp_path / "surv.json"
        surveyor_state_path.write_text(json.dumps(_surveyor_state({
            "semantic_5": {
                "label": ["topic/x"],
                "member_files": [
                    "assumption/a.md",
                    "assumption/b.md",
                    "assumption/c.md",
                ],
            },
        })))

        # Drafter shouldn't be called in dry-run; if it is, the test
        # explodes (no transport patched here).
        cfg = _config(
            vault_path=tmp_path,
            state_path=tmp_path / "s.json",
            surveyor_state_path=surveyor_state_path,
            drafter_endpoint="http://x",
            drafter_model="qwen",
        )
        dcli.cmd_mine_patterns(cfg, dry_run=True)

        # Neither the proposal file nor the state file got written.
        proposal = tmp_path / "inbox" / "proposed-canonical" / "topic-x.md"
        assert not proposal.exists()
        state_path = tmp_path / "s.json"
        assert not state_path.exists()

        # Output still shows the dry-run summary with the would-be
        # candidate.
        out = capsys.readouterr().out
        assert "dry_run=True" in out
        assert "topic-x" in out
        assert "would write" in out

    def test_min_cluster_size_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 2-member cluster — would fail the default min_cluster_size=3,
        # but the override lets it through.
        (tmp_path / "assumption").mkdir()
        for stem in ("a", "b"):
            (tmp_path / "assumption" / f"{stem}.md").write_text(
                f"---\nname: {stem}\n---\n\nbody\n",
            )
        surveyor_state_path = tmp_path / "surv.json"
        surveyor_state_path.write_text(json.dumps(_surveyor_state({
            "semantic_5": {
                "label": ["topic/x"],
                "member_files": ["assumption/a.md", "assumption/b.md"],
            },
        })))

        # Mock drafter — returns a placeholder.
        def _dispatch(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json=_drafter_response("a claim\nTYPE: architecture\nSLUG: topic-x\n"),
            )
        _patch_httpx_for_drafter(monkeypatch, _dispatch)

        cfg = _config(
            vault_path=tmp_path,
            state_path=tmp_path / "s.json",
            surveyor_state_path=surveyor_state_path,
            drafter_endpoint="http://x",
            drafter_model="qwen",
        )
        dcli.cmd_mine_patterns(cfg, dry_run=False, min_cluster_size=2)

        target = tmp_path / "inbox" / "proposed-canonical" / "topic-x.md"
        assert target.is_file()


# ---------------------------------------------------------------------------
# Drafter failure logging — subprocess-failure-logging discipline
# ---------------------------------------------------------------------------


class TestDrafterFailureLogging:
    def test_http_error_logged_with_stdout_tail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Per the subprocess-failure-logging discipline: every non-2xx
        # path must emit pattern_miner.llm_failed with stderr-equivalent
        # AND stdout_tail (populated for HTTP non-200).
        (tmp_path / "assumption").mkdir()
        for stem in ("a", "b", "c"):
            (tmp_path / "assumption" / f"{stem}.md").write_text(
                f"---\nname: {stem}\n---\n\nbody\n",
            )
        surveyor_state_path = tmp_path / "surv.json"
        surveyor_state_path.write_text(json.dumps(_surveyor_state({
            "semantic_5": {
                "label": ["topic/x"],
                "member_files": [
                    "assumption/a.md", "assumption/b.md", "assumption/c.md",
                ],
            },
        })))

        def _dispatch(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=503,
                text="Ollama out of memory; reduce model size",
            )
        _patch_httpx_for_drafter(monkeypatch, _dispatch)

        cfg = _config(
            vault_path=tmp_path,
            state_path=tmp_path / "s.json",
            surveyor_state_path=surveyor_state_path,
            drafter_endpoint="http://x",
            drafter_model="qwen",
        )

        with structlog.testing.capture_logs() as captured:
            dcli.cmd_mine_patterns(cfg, dry_run=False)

        # The drafter failure MUST be logged with the contract fields.
        # Not just "any failure" — specifically pattern_miner.llm_failed
        # with the status + stdout_tail.
        failures = [c for c in captured if c.get("event") == "pattern_miner.llm_failed"]
        assert len(failures) == 1, (
            f"Expected exactly one pattern_miner.llm_failed log; got {len(failures)}: "
            f"{[c.get('event') for c in captured]}"
        )
        f = failures[0]
        assert f["status"] == 503
        # stdout_tail field MUST be present (not just stderr) — that's
        # the load-bearing part of the discipline.
        assert "stdout_tail" in f
        assert "Ollama out of memory" in f["stdout_tail"]
        # Summary should follow the f"<class>: <detail>" pattern.
        assert "503" in f["summary"]

        # The proposal still gets written — placeholder paragraph
        # because the drafter failed.
        target = tmp_path / "inbox" / "proposed-canonical" / "topic-x.md"
        assert target.is_file()
        assert "Drafter LLM unavailable" in target.read_text()


# ---------------------------------------------------------------------------
# Top-level mine_patterns log emission — "ran, nothing to do"
# ---------------------------------------------------------------------------


class TestRunCompleteLog:
    def test_run_complete_emitted_with_counts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Per the log-emission test discipline: pin the run-complete
        # log AND assert on the count fields, not just the event name.
        (tmp_path / "assumption").mkdir()
        for stem in ("a", "b", "c"):
            (tmp_path / "assumption" / f"{stem}.md").write_text(
                f"---\nname: {stem}\n---\n\nbody\n",
            )
        surveyor_state_path = tmp_path / "surv.json"
        surveyor_state_path.write_text(json.dumps(_surveyor_state({
            "semantic_5": {
                "label": ["topic/x"],
                "member_files": [
                    "assumption/a.md", "assumption/b.md", "assumption/c.md",
                ],
            },
        })))

        def _dispatch(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json=_drafter_response("claim\nTYPE: architecture\nSLUG: topic-x\n"),
            )
        _patch_httpx_for_drafter(monkeypatch, _dispatch)

        cfg = _config(
            vault_path=tmp_path,
            state_path=tmp_path / "s.json",
            surveyor_state_path=surveyor_state_path,
            drafter_endpoint="http://x",
            drafter_model="qwen",
        )

        with structlog.testing.capture_logs() as captured:
            dcli.cmd_mine_patterns(cfg, dry_run=False)

        completes = [c for c in captured if c.get("event") == "pattern_miner.run_complete"]
        assert len(completes) == 1
        c = completes[0]
        assert c["evaluated"] == 1
        assert c["survivors"] == 1
        assert c["proposed"] == 1
        assert c["drafter_failures"] == 0
        assert c["dry_run"] is False
