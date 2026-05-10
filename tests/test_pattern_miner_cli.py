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


# ---------------------------------------------------------------------------
# WARN-2 (2026-05-10 code-review) — LLM-emitted slug must pass through
# slugify() so trailing hyphens / non-alnum garbage can't leak to disk.
# ---------------------------------------------------------------------------


class TestLLMSlugReSlugified:
    def test_trailing_hyphen_in_llm_slug_normalized(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The LLM emits a slug with a trailing hyphen — _SLUG_LINE_RE
        # permits it, but the writer must NOT use it as-is or the file
        # lands as ``local-llm-hardware-.md`` (legal but ugly). The
        # production code re-slugifies before assignment.
        (tmp_path / "assumption").mkdir()
        for stem in ("a", "b", "c"):
            (tmp_path / "assumption" / f"{stem}.md").write_text(
                f"---\nname: {stem}\n---\n\nbody\n",
            )
        surveyor_state_path = tmp_path / "surv.json"
        surveyor_state_path.write_text(json.dumps(_surveyor_state({
            "semantic_5": {
                "label": ["llm/quantization"],
                "member_files": [
                    "assumption/a.md", "assumption/b.md", "assumption/c.md",
                ],
            },
        })))

        # LLM trailer with trailing hyphen on the slug line.
        def _dispatch(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json=_drafter_response(
                    "claim about hardware\n"
                    "TYPE: principles\n"
                    "SLUG: local-llm-hardware-\n"
                ),
            )
        _patch_httpx_for_drafter(monkeypatch, _dispatch)

        cfg = _config(
            vault_path=tmp_path,
            state_path=tmp_path / "s.json",
            surveyor_state_path=surveyor_state_path,
            drafter_endpoint="http://x",
            drafter_model="qwen",
        )
        dcli.cmd_mine_patterns(cfg, dry_run=False)

        # File MUST land at the slugified path, NOT the trailing-hyphen
        # path. Both checks present so a future regression on either
        # half (drop the slugify call OR loosen the regex) is caught.
        normalized_path = (
            tmp_path / "inbox" / "proposed-canonical"
            / "local-llm-hardware.md"
        )
        ill_formed_path = (
            tmp_path / "inbox" / "proposed-canonical"
            / "local-llm-hardware-.md"
        )
        assert normalized_path.is_file()
        assert not ill_formed_path.exists()

        # Also pin the state entry's proposed_slug field — the writer
        # AND the state record both flow from the same variable, so
        # one must match the other.
        state_data = json.loads((tmp_path / "s.json").read_text())
        proposal = next(iter(state_data["proposals"].values()))
        assert proposal["proposed_slug"] == "local-llm-hardware"

    def test_garbage_llm_slug_falls_back_to_heuristic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # If slugify() of the LLM's suggestion produces an empty string
        # (pathological all-non-alnum input), the heuristic-derived slug
        # from the cluster's first label must be used instead — we never
        # ship a file named ``.md``.
        (tmp_path / "assumption").mkdir()
        for stem in ("a", "b", "c"):
            (tmp_path / "assumption" / f"{stem}.md").write_text(
                f"---\nname: {stem}\n---\n\nbody\n",
            )
        surveyor_state_path = tmp_path / "surv.json"
        surveyor_state_path.write_text(json.dumps(_surveyor_state({
            "semantic_5": {
                "label": ["llm/quantization"],
                "member_files": [
                    "assumption/a.md", "assumption/b.md", "assumption/c.md",
                ],
            },
        })))

        # Note: the _SLUG_LINE_RE regex requires the LLM slug to start
        # with [a-z0-9], so a pure-garbage slug like "////" never even
        # parses out — the captured group is empty and the heuristic
        # is the only path used. Pinning that contract here so a future
        # regex relaxation doesn't silently let garbage through.
        def _dispatch(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                # No SLUG line at all — just paragraph + TYPE.
                json=_drafter_response(
                    "claim\n"
                    "TYPE: architecture\n"
                ),
            )
        _patch_httpx_for_drafter(monkeypatch, _dispatch)

        cfg = _config(
            vault_path=tmp_path,
            state_path=tmp_path / "s.json",
            surveyor_state_path=surveyor_state_path,
            drafter_endpoint="http://x",
            drafter_model="qwen",
        )
        dcli.cmd_mine_patterns(cfg, dry_run=False)

        # Heuristic-derived slug from "llm/quantization" → "llm-quantization".
        target = (
            tmp_path / "inbox" / "proposed-canonical"
            / "llm-quantization.md"
        )
        assert target.is_file()


# ---------------------------------------------------------------------------
# WARN-1 (2026-05-10 code-review) — same-run + cross-run slug collision
# resolution. Two distinct fingerprints competing for the same slug
# must both land, with the second uniquified rather than overwriting
# the first.
# ---------------------------------------------------------------------------


class TestSlugCollisionResolution:
    def test_same_run_collision_uniquifies_second(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Two clusters whose first label slugifies identically — both
        # would derive the same proposed_slug. The second must land at
        # ``<slug>-2.md`` rather than overwrite the first.
        (tmp_path / "assumption").mkdir()
        for stem in ("a", "b", "c", "d", "e", "f"):
            (tmp_path / "assumption" / f"{stem}.md").write_text(
                f"---\nname: {stem}\n---\n\nbody\n",
            )
        surveyor_state_path = tmp_path / "surv.json"
        surveyor_state_path.write_text(json.dumps(_surveyor_state({
            "semantic_5": {
                # First label slugifies to "topic-x".
                "label": ["topic/x"],
                "member_files": [
                    "assumption/a.md", "assumption/b.md", "assumption/c.md",
                ],
            },
            "semantic_7": {
                # Different cluster, distinct fingerprint — but first
                # label ALSO slugifies to "topic-x". The collision
                # resolver must uniquify rather than silently overwrite.
                "label": ["Topic X"],
                "member_files": [
                    "assumption/d.md", "assumption/e.md", "assumption/f.md",
                ],
            },
        })))

        # Drafter returns no SLUG line so each cluster uses its
        # heuristic-derived slug ("topic-x" / "topic-x" — the
        # collision).
        def _dispatch(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json=_drafter_response("claim\nTYPE: architecture\n"),
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

        proposed_dir = tmp_path / "inbox" / "proposed-canonical"
        first = proposed_dir / "topic-x.md"
        second = proposed_dir / "topic-x-2.md"
        assert first.is_file()
        assert second.is_file()

        # Both fingerprints recorded in state with their respective
        # paths — neither was overwritten.
        state_data = json.loads((tmp_path / "s.json").read_text())
        assert len(state_data["proposals"]) == 2
        slugs = sorted(p["proposed_slug"] for p in state_data["proposals"].values())
        assert slugs == ["topic-x", "topic-x-2"]

        # The collision-resolved log MUST fire with the original_slug +
        # resolved_slug fields so an operator can grep for collisions.
        # Per feedback_log_emission_test_pattern.md: pin the event AND
        # the field shape, not just the count.
        resolved = [
            c for c in captured
            if c.get("event") == "pattern_miner.slug_collision_resolved"
        ]
        assert len(resolved) == 1
        r = resolved[0]
        assert r["original_slug"] == "topic-x"
        assert r["resolved_slug"] == "topic-x-2"

        # Run-complete log surfaces the collision counter so the CLI
        # summary stays in sync with the production behavior.
        completes = [
            c for c in captured
            if c.get("event") == "pattern_miner.run_complete"
        ]
        assert len(completes) == 1
        assert completes[0]["slug_collisions_resolved"] == 1

    def test_prior_run_state_blocks_new_slug_claim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A prior run already owns ``topic-x`` (state entry exists,
        # status pending). A NEW cluster with a DIFFERENT fingerprint
        # whose slug derives to ``topic-x`` must NOT overwrite the
        # prior run's file or state entry — must uniquify to
        # ``topic-x-2``.
        from alfred.distiller.pattern_miner_state import (
            PatternMinerState,
            ProposalEntry,
            STATUS_PENDING,
        )

        (tmp_path / "assumption").mkdir()
        for stem in ("a", "b", "c"):
            (tmp_path / "assumption" / f"{stem}.md").write_text(
                f"---\nname: {stem}\n---\n\nbody\n",
            )

        # Pre-seed state with a pending proposal owning "topic-x".
        state_path = tmp_path / "s.json"
        prior_state = PatternMinerState(state_path)
        prior_state.record_proposal(ProposalEntry(
            fingerprint="prior_fp_xyz",
            cluster_id="semantic_3",
            labels=["unrelated/label"],
            member_count=4,
            proposed_at="2026-05-09T00:00:00+00:00",
            proposed_path="inbox/proposed-canonical/topic-x.md",
            proposed_slug="topic-x",
            proposed_canonical_type="architecture",
            status=STATUS_PENDING,
        ))
        prior_state.save()

        # Also write a real file so reconcile_state sees it as still-
        # pending (otherwise reconcile would mark it discarded — that's
        # a separate behavior, not under test here).
        proposed_dir = tmp_path / "inbox" / "proposed-canonical"
        proposed_dir.mkdir(parents=True)
        (proposed_dir / "topic-x.md").write_text(
            "prior run's proposal — must not be overwritten",
        )

        # NEW cluster whose first label slugifies to "topic-x" (collides
        # with the prior run's claim).
        surveyor_state_path = tmp_path / "surv.json"
        surveyor_state_path.write_text(json.dumps(_surveyor_state({
            "semantic_99": {
                "label": ["topic/x"],
                "member_files": [
                    "assumption/a.md", "assumption/b.md", "assumption/c.md",
                ],
            },
        })))

        def _dispatch(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json=_drafter_response("new claim\nTYPE: architecture\n"),
            )
        _patch_httpx_for_drafter(monkeypatch, _dispatch)

        cfg = _config(
            vault_path=tmp_path,
            state_path=state_path,
            surveyor_state_path=surveyor_state_path,
            drafter_endpoint="http://x",
            drafter_model="qwen",
        )
        dcli.cmd_mine_patterns(cfg, dry_run=False)

        # Prior file's contents intact.
        assert (proposed_dir / "topic-x.md").read_text() == (
            "prior run's proposal — must not be overwritten"
        )
        # New proposal lives at -2.
        assert (proposed_dir / "topic-x-2.md").is_file()

        # Both state entries present, one each.
        state_data = json.loads(state_path.read_text())
        assert len(state_data["proposals"]) == 2
        slugs_by_fp = {
            fp: e["proposed_slug"]
            for fp, e in state_data["proposals"].items()
        }
        assert slugs_by_fp["prior_fp_xyz"] == "topic-x"
        # The new fingerprint (computed at runtime) maps to topic-x-2.
        new_fps = [fp for fp in slugs_by_fp if fp != "prior_fp_xyz"]
        assert len(new_fps) == 1
        assert slugs_by_fp[new_fps[0]] == "topic-x-2"

    def test_three_collisions_in_one_run_yield_three_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Three clusters all wanting "topic-x" — must land as
        # topic-x.md, topic-x-2.md, topic-x-3.md.
        (tmp_path / "assumption").mkdir()
        for stem in "abcdefghi":
            (tmp_path / "assumption" / f"{stem}.md").write_text(
                f"---\nname: {stem}\n---\n\nbody\n",
            )
        surveyor_state_path = tmp_path / "surv.json"
        surveyor_state_path.write_text(json.dumps(_surveyor_state({
            "semantic_1": {
                "label": ["topic/x"],
                "member_files": [
                    "assumption/a.md", "assumption/b.md", "assumption/c.md",
                ],
            },
            "semantic_2": {
                "label": ["Topic X"],
                "member_files": [
                    "assumption/d.md", "assumption/e.md", "assumption/f.md",
                ],
            },
            "semantic_3": {
                "label": ["TOPIC-X"],
                "member_files": [
                    "assumption/g.md", "assumption/h.md", "assumption/i.md",
                ],
            },
        })))

        def _dispatch(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json=_drafter_response("claim\nTYPE: architecture\n"),
            )
        _patch_httpx_for_drafter(monkeypatch, _dispatch)

        cfg = _config(
            vault_path=tmp_path,
            state_path=tmp_path / "s.json",
            surveyor_state_path=surveyor_state_path,
            drafter_endpoint="http://x",
            drafter_model="qwen",
        )
        dcli.cmd_mine_patterns(cfg, dry_run=False)

        proposed_dir = tmp_path / "inbox" / "proposed-canonical"
        assert (proposed_dir / "topic-x.md").is_file()
        assert (proposed_dir / "topic-x-2.md").is_file()
        assert (proposed_dir / "topic-x-3.md").is_file()
