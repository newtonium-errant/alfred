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
        # Stage 2b widened this message to cover split markers too —
        # "no new patterns surfaced" alone would lie when only NO-CLAIM /
        # SPLIT outcomes happened. Pin the new wording.
        assert "no new patterns or split markers surfaced this run." in out


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


# ---------------------------------------------------------------------------
# Stage 2b parser — _parse_drafter_response branches on three outcomes
# (2026-05-10). NO-CLAIM and SPLIT sentinels added to the prompt at
# stage 2a (commit 5ed62bf); this builder pass adds parser detection
# + orchestrator routing.
# ---------------------------------------------------------------------------


class TestParseDrafterResponse:
    """Pure parser unit tests. No HTTP, no fs.

    Sentinel detection MUST anchor on start-of-content so a happy-
    path paragraph that quotes the prompt (mentioning "NO-CLAIM"
    inline) can't trigger a misclass.
    """

    def test_happy_path_returns_proposal_outcome(self) -> None:
        from alfred.distiller.pattern_miner import (
            OUTCOME_PROPOSAL,
            _parse_drafter_response,
        )
        result = _parse_drafter_response(
            "Hardcoding a single log destination routes events wrong. "
            "The fix is a tool kwarg.\n"
            "TYPE: principles\n"
            "SLUG: log-routing-tool-kwarg\n"
        )
        assert result.outcome == OUTCOME_PROPOSAL
        assert "Hardcoding a single log destination" in result.paragraph
        assert result.llm_type_suggestion == "principles"
        assert result.llm_slug_suggestion == "log-routing-tool-kwarg"
        # Sentinel-only fields stay empty on the happy path.
        assert result.reason == ""
        assert result.themes == []

    def test_no_claim_with_reason_parses_outcome_and_reason(self) -> None:
        from alfred.distiller.pattern_miner import (
            OUTCOME_NO_CLAIM,
            _parse_drafter_response,
        )
        result = _parse_drafter_response(
            "NO-CLAIM\n"
            "REASON: members share the word 'alias' but discuss "
            "alias-skip policy, field placement, and Telegram "
            "/calibration alias separately.\n"
        )
        assert result.outcome == OUTCOME_NO_CLAIM
        assert "alias-skip policy" in result.reason
        # Happy-path fields stay empty.
        assert result.paragraph == ""
        assert result.llm_type_suggestion == ""
        assert result.llm_slug_suggestion == ""
        assert result.themes == []

    def test_no_claim_without_reason_still_routes_to_no_claim(
        self,
    ) -> None:
        # Per the dispatch decision: emitting the NO-CLAIM token is
        # the load-bearing skip signal; reason is operator-helpful
        # but not required. A bare NO-CLAIM still routes correctly.
        from alfred.distiller.pattern_miner import (
            OUTCOME_NO_CLAIM,
            _parse_drafter_response,
        )
        result = _parse_drafter_response("NO-CLAIM\n")
        assert result.outcome == OUTCOME_NO_CLAIM
        assert result.reason == ""

    def test_split_with_themes_parses_outcome_and_themes(self) -> None:
        from alfred.distiller.pattern_miner import (
            OUTCOME_SPLIT,
            _parse_drafter_response,
        )
        result = _parse_drafter_response(
            "SPLIT\n"
            "THEMES:\n"
            "- regex-perf: assumption/regex-cache.md, "
            "constraint/regex-backtracking.md\n"
            "- regex-portability: assumption/posix-vs-pcre.md\n"
        )
        assert result.outcome == OUTCOME_SPLIT
        assert len(result.themes) == 2
        assert "regex-perf" in result.themes[0]
        assert "regex-portability" in result.themes[1]
        # Happy-path fields stay empty.
        assert result.paragraph == ""

    def test_split_with_no_themes_still_routes_split(self) -> None:
        # Defensive: SPLIT token without a parseable THEMES list
        # still routes to the split outcome — the split-marker
        # writer has a graceful "no themes parsed" fallback so the
        # operator still gets a file + the source members.
        from alfred.distiller.pattern_miner import (
            OUTCOME_SPLIT,
            _parse_drafter_response,
        )
        result = _parse_drafter_response("SPLIT\n")
        assert result.outcome == OUTCOME_SPLIT
        assert result.themes == []

    def test_split_tolerates_asterisk_bullets(self) -> None:
        # The prompt asks for ``- <theme>`` shape but the LLM may
        # emit ``* <theme>`` instead. Both forms count.
        from alfred.distiller.pattern_miner import (
            OUTCOME_SPLIT,
            _parse_drafter_response,
        )
        result = _parse_drafter_response(
            "SPLIT\n"
            "THEMES:\n"
            "* theme one\n"
            "* theme two\n"
        )
        assert result.outcome == OUTCOME_SPLIT
        assert result.themes == ["theme one", "theme two"]

    def test_paragraph_quoting_no_claim_inline_does_not_misclass(
        self,
    ) -> None:
        # Pin the start-of-content anchor: a happy-path paragraph
        # that legitimately contains "NO-CLAIM" inline (e.g., the
        # cluster's subject is "the NO-CLAIM sentinel itself")
        # MUST stay routed to the proposal outcome.
        from alfred.distiller.pattern_miner import (
            OUTCOME_PROPOSAL,
            _parse_drafter_response,
        )
        result = _parse_drafter_response(
            "The NO-CLAIM sentinel is the prompt's refusal token.\n"
            "TYPE: architecture\n"
            "SLUG: no-claim-sentinel\n"
        )
        assert result.outcome == OUTCOME_PROPOSAL
        assert result.llm_slug_suggestion == "no-claim-sentinel"

    def test_paragraph_quoting_split_inline_does_not_misclass(
        self,
    ) -> None:
        # Mirror of above for SPLIT — anchor must be start-of-content.
        from alfred.distiller.pattern_miner import (
            OUTCOME_PROPOSAL,
            _parse_drafter_response,
        )
        result = _parse_drafter_response(
            "When a cluster needs SPLIT, the LLM emits a sentinel.\n"
            "TYPE: architecture\n"
            "SLUG: split-sentinel\n"
        )
        assert result.outcome == OUTCOME_PROPOSAL


# ---------------------------------------------------------------------------
# Stage 2b orchestrator — NO-CLAIM outcome routing
# ---------------------------------------------------------------------------


class TestNoClaimOutcome:
    def test_no_claim_skips_cluster_no_file_no_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # NO-CLAIM contract: no proposal file written, no state entry
        # recorded. Re-running the miner against the same cluster
        # should re-evaluate (LLM may judge differently if labels
        # shift). Pinning "no state" is load-bearing — without it,
        # a stale no_claim entry would silently block legitimate
        # future proposals on the same fingerprint.
        (tmp_path / "assumption").mkdir()
        for stem in ("a", "b", "c"):
            (tmp_path / "assumption" / f"{stem}.md").write_text(
                f"---\nname: {stem}\n---\n\nbody\n",
            )
        surveyor_state_path = tmp_path / "surv.json"
        surveyor_state_path.write_text(json.dumps(_surveyor_state({
            "semantic_5": {
                "label": ["topic/alias"],
                "member_files": [
                    "assumption/a.md",
                    "assumption/b.md",
                    "assumption/c.md",
                ],
            },
        })))

        def _dispatch(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json=_drafter_response(
                    "NO-CLAIM\n"
                    "REASON: members share the word 'alias' but "
                    "discuss separate concerns.\n"
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

        # No proposal file written.
        proposed_dir = tmp_path / "inbox" / "proposed-canonical"
        assert not list(proposed_dir.glob("*.md")) or all(
            p.name == ".gitkeep" for p in proposed_dir.glob("*")
        )

        # State file: either absent (no proposals AND no reconcile
        # transitions to save), or present-but-empty proposals dict.
        # Either way, NO entry for the NO-CLAIM cluster's fingerprint.
        state_path = tmp_path / "s.json"
        if state_path.is_file():
            state_data = json.loads(state_path.read_text())
            assert state_data.get("proposals") in (None, {})

    def test_no_claim_emits_log_with_reason_and_increments_counter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Per feedback_intentionally_left_blank.md + the log-
        # emission test discipline: skip-without-signal is the bug
        # we're closing. Pin the operator-visible log line + the
        # MineResult counter that surfaces through CLI summary.
        (tmp_path / "assumption").mkdir()
        for stem in ("a", "b", "c"):
            (tmp_path / "assumption" / f"{stem}.md").write_text(
                f"---\nname: {stem}\n---\n\nbody\n",
            )
        surveyor_state_path = tmp_path / "surv.json"
        surveyor_state_path.write_text(json.dumps(_surveyor_state({
            "semantic_99": {
                "label": ["topic/cosmetic"],
                "member_files": [
                    "assumption/a.md",
                    "assumption/b.md",
                    "assumption/c.md",
                ],
            },
        })))

        def _dispatch(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json=_drafter_response(
                    "NO-CLAIM\n"
                    "REASON: cosmetic surface match only.\n"
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

        with structlog.testing.capture_logs() as captured:
            dcli.cmd_mine_patterns(cfg, dry_run=False)

        # The cluster_no_claim log MUST fire with the reason field
        # so an operator can grep for "why did the LLM refuse" without
        # re-running the cluster against the LLM.
        no_claim_logs = [
            c for c in captured
            if c.get("event") == "pattern_miner.cluster_no_claim"
        ]
        assert len(no_claim_logs) == 1
        nc = no_claim_logs[0]
        assert nc["cluster_id"] == "semantic_99"
        assert "cosmetic surface match only" in nc["reason"]

        # run_complete log surfaces the counter (for the BIT-side /
        # Daily-Sync-side eyes the operator might consult instead of
        # the per-event no_claim log).
        completes = [
            c for c in captured
            if c.get("event") == "pattern_miner.run_complete"
        ]
        assert len(completes) == 1
        assert completes[0]["skipped_no_claim"] == 1
        assert completes[0]["proposed"] == 0


# ---------------------------------------------------------------------------
# Stage 2b orchestrator — SPLIT outcome routing
# ---------------------------------------------------------------------------


class TestSplitOutcome:
    def test_split_writes_marker_file_with_themes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # SPLIT contract: write a split-marker file at
        # ``<proposed_dir>/<slug>-needs-split.md`` (distinct filename
        # so an operator's `ls` shows which cluster needs which
        # action). Frontmatter type ``proposed-canonical-split``;
        # body lists the themes the LLM identified.
        (tmp_path / "assumption").mkdir()
        for stem in ("a", "b", "c", "d"):
            (tmp_path / "assumption" / f"{stem}.md").write_text(
                f"---\nname: {stem}\n---\n\nbody\n",
            )
        surveyor_state_path = tmp_path / "surv.json"
        surveyor_state_path.write_text(json.dumps(_surveyor_state({
            "semantic_42": {
                "label": ["topic/regex"],
                "member_files": [
                    "assumption/a.md",
                    "assumption/b.md",
                    "assumption/c.md",
                    "assumption/d.md",
                ],
            },
        })))

        def _dispatch(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json=_drafter_response(
                    "SPLIT\n"
                    "THEMES:\n"
                    "- regex-perf concerns: a.md, b.md\n"
                    "- regex-portability concerns: c.md, d.md\n"
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

        # Marker file at the expected path with the -needs-split
        # suffix. NOT a happy-path proposal file.
        proposed_dir = tmp_path / "inbox" / "proposed-canonical"
        marker = proposed_dir / "topic-regex-needs-split.md"
        proposal = proposed_dir / "topic-regex.md"
        assert marker.is_file()
        assert not proposal.exists()

        body = marker.read_text()
        assert "type: proposed-canonical-split" in body
        assert "status: split_pending" in body
        assert "regex-perf concerns: a.md, b.md" in body
        assert "regex-portability concerns: c.md, d.md" in body
        # Source members rendered as wikilinks.
        assert "[[assumption/a]]" in body
        assert "[[assumption/d]]" in body

    def test_split_records_state_with_split_pending_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Unlike NO-CLAIM (no state), SPLIT records state so reconcile
        # can detect operator action (split into N records → promoted;
        # rm marker → discarded). Pin the status field is exactly
        # ``split_pending`` (the new lifecycle value).
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

        def _dispatch(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json=_drafter_response(
                    "SPLIT\n"
                    "THEMES:\n"
                    "- theme alpha\n"
                    "- theme beta\n"
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

        state_path = tmp_path / "s.json"
        assert state_path.is_file()
        state_data = json.loads(state_path.read_text())
        assert len(state_data["proposals"]) == 1
        entry = next(iter(state_data["proposals"].values()))
        assert entry["status"] == "split_pending"
        # The proposed_path points at the marker file (so reconcile
        # will find it).
        assert entry["proposed_path"].endswith("topic-x-needs-split.md")
        # The proposed_slug stays bare (operator promotes to
        # architecture/<slug>.md or principles/<slug>.md, not to
        # something with ``-needs-split`` in the name).
        assert entry["proposed_slug"] == "topic-x"

    def test_split_emits_log_and_increments_counter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Pin the cluster_multi_theme log + the flagged_split counter
        # surfacing through run_complete.
        (tmp_path / "assumption").mkdir()
        for stem in ("a", "b", "c"):
            (tmp_path / "assumption" / f"{stem}.md").write_text(
                f"---\nname: {stem}\n---\n\nbody\n",
            )
        surveyor_state_path = tmp_path / "surv.json"
        surveyor_state_path.write_text(json.dumps(_surveyor_state({
            "semantic_99": {
                "label": ["topic/x"],
                "member_files": [
                    "assumption/a.md",
                    "assumption/b.md",
                    "assumption/c.md",
                ],
            },
        })))

        def _dispatch(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json=_drafter_response(
                    "SPLIT\n"
                    "THEMES:\n"
                    "- theme one\n"
                    "- theme two\n"
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

        with structlog.testing.capture_logs() as captured:
            dcli.cmd_mine_patterns(cfg, dry_run=False)

        multi_theme_logs = [
            c for c in captured
            if c.get("event") == "pattern_miner.cluster_multi_theme"
        ]
        assert len(multi_theme_logs) == 1
        mt = multi_theme_logs[0]
        assert mt["cluster_id"] == "semantic_99"
        assert mt["themes"] == ["theme one", "theme two"]

        completes = [
            c for c in captured
            if c.get("event") == "pattern_miner.run_complete"
        ]
        assert len(completes) == 1
        assert completes[0]["flagged_split"] == 1
        # Split outcomes do NOT count toward proposed (the existing
        # contract — proposed = happy-path proposals). Pin so a
        # future regression that lumps split into proposed surfaces.
        assert completes[0]["proposed"] == 0


# ---------------------------------------------------------------------------
# Stage 2b CLI summary — counters surface through the printed output
# ---------------------------------------------------------------------------


class TestStage2bCLISummarySurface:
    def test_cli_summary_includes_no_claim_and_split_counters(
        self, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Mixed run: one happy path proposal + one NO-CLAIM + one
        # SPLIT, all surfacing in the summary line. Catches a
        # regression where one of the counters gets dropped from the
        # print format.
        (tmp_path / "assumption").mkdir()
        for stem in ("a", "b", "c", "d", "e", "f", "g", "h", "i"):
            (tmp_path / "assumption" / f"{stem}.md").write_text(
                f"---\nname: {stem}\n---\n\nbody\n",
            )
        surveyor_state_path = tmp_path / "surv.json"
        surveyor_state_path.write_text(json.dumps(_surveyor_state({
            "semantic_1": {
                "label": ["topic/happy"],
                "member_files": [
                    "assumption/a.md", "assumption/b.md", "assumption/c.md",
                ],
            },
            "semantic_2": {
                "label": ["topic/refuse"],
                "member_files": [
                    "assumption/d.md", "assumption/e.md", "assumption/f.md",
                ],
            },
            "semantic_3": {
                "label": ["topic/multi"],
                "member_files": [
                    "assumption/g.md", "assumption/h.md", "assumption/i.md",
                ],
            },
        })))

        # Per-cluster dispatch: assignment by URL inspection isn't
        # possible (single endpoint), so route by call order using a
        # closure-shared counter. The miner walks survivors in the
        # surveyor-state's ordering; on this state the order is
        # semantic_1 -> semantic_2 -> semantic_3.
        call_count = {"n": 0}
        responses = [
            "Happy path claim.\nTYPE: architecture\nSLUG: topic-happy\n",
            "NO-CLAIM\nREASON: surface tag only.\n",
            "SPLIT\nTHEMES:\n- alpha\n- beta\n",
        ]

        def _dispatch(request: httpx.Request) -> httpx.Response:
            content = responses[call_count["n"] % len(responses)]
            call_count["n"] += 1
            return httpx.Response(
                status_code=200,
                json=_drafter_response(content),
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

        out = capsys.readouterr().out
        # Both new counters MUST appear in the print output —
        # operators are the principal consumer of this surface.
        assert "skipped_no_claim=1" in out
        assert "flagged_split=1" in out
        # Existing counters still present.
        assert "proposed=1" in out


# ---------------------------------------------------------------------------
# Stage 2b reconcile — split_pending entries reconcile like pending
# ---------------------------------------------------------------------------


class TestSplitPendingReconcile:
    def test_split_pending_marker_present_stays_split_pending(
        self, tmp_path: Path,
    ) -> None:
        # Reconcile sweep contract: a split_pending entry whose
        # marker file is still on disk stays split_pending. Mirrors
        # pending's "still here, no action yet" behavior.
        from alfred.distiller.pattern_miner import reconcile_state
        from alfred.distiller.pattern_miner_state import (
            STATUS_SPLIT_PENDING,
            PatternMinerState,
            ProposalEntry,
        )

        state_path = tmp_path / "s.json"
        state = PatternMinerState(state_path)
        marker_rel = "inbox/proposed-canonical/topic-x-needs-split.md"
        (tmp_path / "inbox" / "proposed-canonical").mkdir(parents=True)
        (tmp_path / marker_rel).write_text("---\n---\n", encoding="utf-8")

        state.proposals["fp1"] = ProposalEntry(
            fingerprint="fp1",
            cluster_id="semantic_5",
            proposed_path=marker_rel,
            proposed_slug="topic-x",
            status=STATUS_SPLIT_PENDING,
        )

        result = reconcile_state(
            state, tmp_path, ["architecture", "principles", "stack"],
        )
        assert result["still_pending"] == 1
        assert result["promoted"] == 0
        assert result["discarded"] == 0
        assert state.proposals["fp1"].status == STATUS_SPLIT_PENDING

    def test_split_pending_promoted_when_canonical_appears(
        self, tmp_path: Path,
    ) -> None:
        # Operator split the cluster: marker file gone, a canonical
        # artifact with the matching slug exists. Reconcile flips
        # split_pending → promoted (same as the pending case).
        from alfred.distiller.pattern_miner import reconcile_state
        from alfred.distiller.pattern_miner_state import (
            STATUS_PROMOTED,
            STATUS_SPLIT_PENDING,
            PatternMinerState,
            ProposalEntry,
        )

        state_path = tmp_path / "s.json"
        state = PatternMinerState(state_path)
        # Marker file absent (operator removed it).
        # Canonical artifact present:
        (tmp_path / "architecture").mkdir()
        (tmp_path / "architecture" / "topic-x.md").write_text(
            "---\n---\n", encoding="utf-8",
        )

        state.proposals["fp1"] = ProposalEntry(
            fingerprint="fp1",
            cluster_id="semantic_5",
            proposed_path="inbox/proposed-canonical/topic-x-needs-split.md",
            proposed_slug="topic-x",
            status=STATUS_SPLIT_PENDING,
        )

        result = reconcile_state(
            state, tmp_path, ["architecture", "principles", "stack"],
        )
        assert result["promoted"] == 1
        assert state.proposals["fp1"].status == STATUS_PROMOTED

    def test_split_pending_discarded_when_marker_removed_no_match(
        self, tmp_path: Path,
    ) -> None:
        # Operator deleted the marker without splitting. Reconcile
        # flips split_pending → discarded.
        from alfred.distiller.pattern_miner import reconcile_state
        from alfred.distiller.pattern_miner_state import (
            STATUS_DISCARDED,
            STATUS_SPLIT_PENDING,
            PatternMinerState,
            ProposalEntry,
        )

        state_path = tmp_path / "s.json"
        state = PatternMinerState(state_path)
        # No marker file, no canonical artifact.

        state.proposals["fp1"] = ProposalEntry(
            fingerprint="fp1",
            cluster_id="semantic_5",
            proposed_path="inbox/proposed-canonical/topic-x-needs-split.md",
            proposed_slug="topic-x",
            status=STATUS_SPLIT_PENDING,
        )

        result = reconcile_state(
            state, tmp_path, ["architecture", "principles", "stack"],
        )
        assert result["discarded"] == 1
        assert state.proposals["fp1"].status == STATUS_DISCARDED


# ---------------------------------------------------------------------------
# Fingerprint-based reconcile (2026-05-11 extension). Closes the
# slug-rename-on-promote misclassification: when an operator promotes
# a proposal but renames the slug at promote time, the slug-match
# step misses; the fingerprint banner ``canonical_promotion_banner``
# embeds in the canonical record provides a fallback signal.
# ---------------------------------------------------------------------------


def _write_canonical_with_banner(
    vault_path: Path,
    rel_path: str,
    *,
    fingerprint_short: str,
    title: str = "Some Title",
) -> Path:
    """Write a canonical record that carries the fingerprint banner
    in the body — matches what ``cmd_promote_proposal`` writes when
    the CLI handler runs.
    """
    full = vault_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    content = (
        f"# {title}\n"
        f"\n"
        f"> Promoted from inbox/proposed-canonical on 2026-05-11. "
        f"Sources: 5 records (fingerprint: {fingerprint_short}).\n"
        f"\n"
        f"## Mined claim\n"
        f"\n"
        f"Body text here.\n"
    )
    full.write_text(content, encoding="utf-8")
    return full


class TestFingerprintReconcile:
    """Pin the fingerprint-grep fallback step in reconcile_state.

    Closes the slug-rename-on-promote misclassification — when
    operator renames slug at promote time, slug-match misses but
    the fingerprint banner still carries the signal.
    """

    def test_fingerprint_match_promotes_with_correct_promoted_to(
        self, tmp_path: Path,
    ) -> None:
        from alfred.distiller.pattern_miner import reconcile_state
        from alfred.distiller.pattern_miner_state import (
            STATUS_PROMOTED,
            PatternMinerState,
            ProposalEntry,
        )

        state_path = tmp_path / "s.json"
        state = PatternMinerState(state_path)
        # Operator promoted to a RENAMED slug (`renamed-thing.md`
        # instead of the original `topic-x.md`). Slug-match misses;
        # fingerprint-grep should catch it.
        _write_canonical_with_banner(
            tmp_path,
            "architecture/renamed-thing.md",
            fingerprint_short="fpabcdef1234",
        )
        state.proposals["fpabcdef1234567890ab"] = ProposalEntry(
            fingerprint="fpabcdef1234567890ab",
            cluster_id="semantic_5",
            proposed_path="inbox/proposed-canonical/topic-x.md",
            proposed_slug="topic-x",  # original slug; doesn't match
            status="pending",
        )

        result = reconcile_state(
            state, tmp_path, ["architecture", "principles", "stack"],
        )
        assert result["promoted"] == 1
        assert result["discarded"] == 0
        entry = state.proposals["fpabcdef1234567890ab"]
        assert entry.status == STATUS_PROMOTED
        # promoted_to reflects the actual renamed canonical path.
        assert entry.promoted_to == "architecture/renamed-thing.md"
        # promoted_at populated by reconcile.
        assert entry.promoted_at != ""

    def test_slug_match_wins_when_both_signals_present(
        self, tmp_path: Path,
    ) -> None:
        # Defensive: when BOTH slug-match and fingerprint-grep would
        # fire, slug-match wins (step 2 is more deterministic + faster).
        # Edge case is rare but pinned so the priority stays explicit.
        from alfred.distiller.pattern_miner import reconcile_state
        from alfred.distiller.pattern_miner_state import (
            STATUS_PROMOTED,
            PatternMinerState,
            ProposalEntry,
        )

        # Slug-match path: architecture/topic-x.md (matches proposed_slug)
        (tmp_path / "architecture").mkdir(parents=True)
        (tmp_path / "architecture" / "topic-x.md").write_text(
            "# Topic X\n\nSlug-match wins.\n",
            encoding="utf-8",
        )
        # Fingerprint-grep would also fire: another file with the banner
        _write_canonical_with_banner(
            tmp_path,
            "principles/renamed-via-fingerprint.md",
            fingerprint_short="fpabcdef1234",
        )

        state_path = tmp_path / "s.json"
        state = PatternMinerState(state_path)
        state.proposals["fpabcdef1234567890ab"] = ProposalEntry(
            fingerprint="fpabcdef1234567890ab",
            proposed_path="inbox/proposed-canonical/topic-x.md",
            proposed_slug="topic-x",
            status="pending",
        )

        result = reconcile_state(
            state, tmp_path, ["architecture", "principles", "stack"],
        )
        assert result["promoted"] == 1
        entry = state.proposals["fpabcdef1234567890ab"]
        assert entry.status == STATUS_PROMOTED
        # Slug-match path won — promoted_to points at topic-x.md, NOT
        # renamed-via-fingerprint.md.
        assert entry.promoted_to == "architecture/topic-x.md"

    def test_no_match_anywhere_falls_through_to_discarded(
        self, tmp_path: Path,
    ) -> None:
        # Backstop case: no slug-match AND no fingerprint hit.
        # Existing behavior preserved.
        from alfred.distiller.pattern_miner import reconcile_state
        from alfred.distiller.pattern_miner_state import (
            STATUS_DISCARDED,
            PatternMinerState,
            ProposalEntry,
        )

        state_path = tmp_path / "s.json"
        state = PatternMinerState(state_path)
        state.proposals["fp_nothing"] = ProposalEntry(
            fingerprint="fp_nothing",
            proposed_path="inbox/proposed-canonical/lost.md",
            proposed_slug="lost",
            status="pending",
        )
        # No canonical files at all → fall through to discarded.
        result = reconcile_state(
            state, tmp_path, ["architecture", "principles", "stack"],
        )
        assert result["discarded"] == 1
        entry = state.proposals["fp_nothing"]
        assert entry.status == STATUS_DISCARDED
        # discarded_at populated by reconcile (new behavior per
        # 2026-05-11 contract widening).
        assert entry.discarded_at != ""

    def test_multiple_fingerprint_matches_warn_and_pick_first(
        self, tmp_path: Path,
    ) -> None:
        # Operator copy-paste mistake: fingerprint banner appears in
        # multiple canonical files. Reconcile picks the first match
        # and logs a warning so the operator can dedup.
        import structlog

        from alfred.distiller.pattern_miner import reconcile_state
        from alfred.distiller.pattern_miner_state import (
            STATUS_PROMOTED,
            PatternMinerState,
            ProposalEntry,
        )

        # Two canonical files both carrying the same fingerprint banner.
        _write_canonical_with_banner(
            tmp_path,
            "architecture/first.md",
            fingerprint_short="fpaaaaaa1111",
        )
        _write_canonical_with_banner(
            tmp_path,
            "principles/second.md",
            fingerprint_short="fpaaaaaa1111",
        )

        state_path = tmp_path / "s.json"
        state = PatternMinerState(state_path)
        state.proposals["fpaaaaaa1111bbbbcccc"] = ProposalEntry(
            fingerprint="fpaaaaaa1111bbbbcccc",
            proposed_path="inbox/proposed-canonical/topic.md",
            proposed_slug="topic",
            status="pending",
        )

        with structlog.testing.capture_logs() as captured:
            result = reconcile_state(
                state, tmp_path, ["architecture", "principles", "stack"],
            )
        assert result["promoted"] == 1
        entry = state.proposals["fpaaaaaa1111bbbbcccc"]
        assert entry.status == STATUS_PROMOTED
        # First match picked (walk order: architecture first per
        # canonical_match_dirs order).
        assert entry.promoted_to in (
            "architecture/first.md",
            "principles/second.md",
        )
        # Warning fired with both paths for operator review.
        multi_logs = [
            c for c in captured
            if c.get("event") == "pattern_miner.fingerprint_multiple_matches"
        ]
        assert len(multi_logs) == 1
        ml = multi_logs[0]
        assert len(ml["matching_paths"]) == 2

    def test_terminal_status_entries_skipped(
        self, tmp_path: Path,
    ) -> None:
        # Existing behavior pin: promoted/discarded/superseded entries
        # are terminal — reconcile leaves them alone even if a slug-
        # or fingerprint-match would fire.
        from alfred.distiller.pattern_miner import reconcile_state
        from alfred.distiller.pattern_miner_state import (
            STATUS_PROMOTED,
            PatternMinerState,
            ProposalEntry,
        )

        # Canonical file that would match the slug.
        (tmp_path / "architecture").mkdir(parents=True)
        (tmp_path / "architecture" / "topic-x.md").write_text(
            "# Topic X\n", encoding="utf-8",
        )

        state_path = tmp_path / "s.json"
        state = PatternMinerState(state_path)
        # Entry is ALREADY promoted with stale promoted_to. Reconcile
        # must NOT touch it.
        state.proposals["fp1"] = ProposalEntry(
            fingerprint="fp1",
            proposed_path="inbox/proposed-canonical/topic-x.md",
            proposed_slug="topic-x",
            status=STATUS_PROMOTED,
            promoted_to="architecture/topic-x.md",
            promoted_at="2026-05-10T00:00:00+00:00",
        )

        result = reconcile_state(
            state, tmp_path, ["architecture", "principles", "stack"],
        )
        # No transitions counted.
        assert result["promoted"] == 0
        assert result["discarded"] == 0
        # Entry untouched — same promoted_at as before reconcile.
        entry = state.proposals["fp1"]
        assert entry.promoted_at == "2026-05-10T00:00:00+00:00"

    def test_unreadable_canonical_file_logs_and_continues(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Per subprocess-failure-logging discipline: an unreadable
        # canonical file MUST NOT crash the reconcile sweep. Log +
        # skip + continue with other files.
        import structlog

        from alfred.distiller.pattern_miner import (
            _find_canonical_by_fingerprint,
        )

        # Set up: one canonical dir with two files. We'll monkey-patch
        # Path.read_text to raise on the first file and succeed on the
        # second.
        (tmp_path / "architecture").mkdir()
        f1 = tmp_path / "architecture" / "first.md"
        f2 = tmp_path / "architecture" / "second.md"
        f1.write_text(
            "# First\n\n> fingerprint: fpxxxx111111\n",
            encoding="utf-8",
        )
        f2.write_text(
            "# Second\n\n> fingerprint: fpxxxx111111\n",
            encoding="utf-8",
        )

        original_read_text = Path.read_text

        def _patched_read_text(self, *args, **kwargs):
            if self == f1:
                raise OSError("simulated permission denied")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _patched_read_text)

        with structlog.testing.capture_logs() as captured:
            matches = _find_canonical_by_fingerprint(
                tmp_path, ["architecture"], "fpxxxx111111",
            )

        # The good file still matches; the bad file skipped.
        assert len(matches) == 1
        assert matches[0] == f2
        # Warn log fired for the unreadable file.
        read_failed = [
            c for c in captured
            if c.get("event") == "pattern_miner.fingerprint_grep_read_failed"
        ]
        assert len(read_failed) == 1
        assert "first.md" in read_failed[0]["path"]

    def test_reconcile_transition_log_emitted_per_entry(
        self, tmp_path: Path,
    ) -> None:
        # Per feedback_intentionally_left_blank.md: each per-entry
        # transition emits an operator-grep-able log line. Pin the
        # event name + key fields so an operator can grep "what just
        # got reclassified this run."
        import structlog

        from alfred.distiller.pattern_miner import reconcile_state
        from alfred.distiller.pattern_miner_state import (
            PatternMinerState,
            ProposalEntry,
        )

        # One slug-match transition + one no-match (discard) transition.
        (tmp_path / "architecture").mkdir(parents=True)
        (tmp_path / "architecture" / "promoted-thing.md").write_text(
            "# Promoted\n", encoding="utf-8",
        )

        state_path = tmp_path / "s.json"
        state = PatternMinerState(state_path)
        state.proposals["fp_a"] = ProposalEntry(
            fingerprint="fp_a",
            proposed_path="inbox/proposed-canonical/promoted-thing.md",
            proposed_slug="promoted-thing",
            status="pending",
        )
        state.proposals["fp_b"] = ProposalEntry(
            fingerprint="fp_b",
            proposed_path="inbox/proposed-canonical/never-acted-on.md",
            proposed_slug="never-acted-on",
            status="pending",
        )

        with structlog.testing.capture_logs() as captured:
            reconcile_state(
                state, tmp_path, ["architecture", "principles", "stack"],
            )

        # Two transition logs: one slug_match → promoted, one no_match
        # → discarded.
        transitions = [
            c for c in captured
            if c.get("event") == "pattern_miner.reconcile_transition"
        ]
        assert len(transitions) == 2
        by_rule = {t["detection_rule"]: t for t in transitions}
        assert "slug_match" in by_rule
        assert "no_match" in by_rule
        # The slug-match transition carries promoted_to; the no-match
        # one doesn't (discard has no canonical path to record).
        assert by_rule["slug_match"]["to_status"] == "promoted"
        assert by_rule["slug_match"]["promoted_to"] == "architecture/promoted-thing.md"
        assert by_rule["no_match"]["to_status"] == "discarded"


# ---------------------------------------------------------------------------
# Phase 4 operator-promote tracking (2026-05-11) — CLI commands
# ``promote-proposal`` and ``discard-proposal``. Closes the 3 deferred
# follow-ups: slug-rename-on-promote silently miscounted, no audit
# trail, no scaffolding-strip automation.
# ---------------------------------------------------------------------------


def _seed_inbox_proposal(
    tmp_path: Path,
    *,
    slug: str = "topic-x",
    canonical_type: str = "architecture",
    body: str | None = None,
    fingerprint: str = "abc123def456",
) -> tuple[Path, Path]:
    """Seed an inbox proposal file + return (inbox_file, state_path).

    Mirrors what mine_patterns would write for a happy-path
    proposal — frontmatter, banner, claim, source members, footer.
    Stage-2b SPLIT marker files have a different shape and aren't
    needed for these tests.
    """
    inbox_dir = tmp_path / "inbox" / "proposed-canonical"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    proposal_file = inbox_dir / f"{slug}.md"
    if body is None:
        body = (
            "Hardcoding a single log destination routes every CLI "
            "subcommand's events to the wrong file. The fix is a "
            "``tool`` kwarg with a backward-compatible default — each "
            "dispatcher passes its own tool name."
        )
    proposal_file.write_text(
        f"---\n"
        f"type: proposed-canonical\n"
        f'proposed_at: "2026-05-10T12:00:00+00:00"\n'
        f'source_cluster_id: "semantic_5"\n'
        f"source_member_count: 3\n"
        f'proposed_canonical_type: "{canonical_type}"\n'
        f'proposed_slug: "{slug}"\n'
        f'fingerprint: "{fingerprint}"\n'
        f"status: proposed\n"
        f"---\n"
        f"\n"
        f"# Topic X\n"
        f"\n"
        f"> Phase 4 pattern miner surfaced this cluster on "
        f"2026-05-10. Andrew should read the source members below.\n"
        f"\n"
        f"## Mined claim\n"
        f"\n"
        f"{body}\n"
        f"\n"
        f"## Source members\n"
        f"\n"
        f"- [[assumption/a]]\n"
        f"- [[assumption/b]]\n"
        f"- [[assumption/c]]\n"
        f"\n"
        f"## Suggested next step\n"
        f"\n"
        f"Promote with: `alfred --config config.yaml vault move "
        f'"inbox/proposed-canonical/{slug}.md" '
        f'"{canonical_type}/{slug}.md"`\n'
        f"\n"
        f"Or `rm` to discard.\n",
        encoding="utf-8",
    )
    state_path = tmp_path / "data" / "state.json"
    return proposal_file, state_path


def _seed_state_with_proposal(
    state_path: Path,
    *,
    fingerprint: str = "abc123def456",
    slug: str = "topic-x",
    canonical_type: str = "architecture",
    status: str = "pending",
    extra: dict | None = None,
) -> None:
    """Write a state file with one proposal entry."""
    from alfred.distiller.pattern_miner_state import (
        PatternMinerState,
        ProposalEntry,
    )

    state = PatternMinerState(state_path)
    entry = ProposalEntry(
        fingerprint=fingerprint,
        cluster_id="semantic_5",
        labels=["topic/x"],
        member_count=3,
        proposed_at="2026-05-10T12:00:00+00:00",
        proposed_path=f"inbox/proposed-canonical/{slug}.md",
        proposed_slug=slug,
        proposed_canonical_type=canonical_type,
        status=status,
    )
    if extra:
        for k, v in extra.items():
            setattr(entry, k, v)
    state.record_proposal(entry)
    state.save()


def _config_for_promote(
    tmp_path: Path, state_path: Path,
) -> DistillerConfig:
    """Build a DistillerConfig pointing at tmp_path as vault."""
    cfg = DistillerConfig(vault=VaultConfig(path=str(tmp_path)))
    cfg.pattern_miner = PatternMinerConfig(
        enabled=True,
        surveyor_state_path=str(tmp_path / "unused-surveyor-state.json"),
        proposed_dir="inbox/proposed-canonical",
        min_cluster_size=3,
        canonical_match_dirs=["architecture", "principles", "stack"],
        label_denylist=[],
        state=PatternMinerStateConfig(path=str(state_path)),
        openrouter=PatternMinerOpenRouterConfig(),
    )
    return cfg


# ---------------------------------------------------------------------------
# Scaffolding-strip helpers (pure functions; no CLI involvement)
# ---------------------------------------------------------------------------


class TestStripProposalScaffolding:
    """Pin the four scaffolding-strip rules from the design memo."""

    def test_strips_frontmatter(self) -> None:
        from alfred.distiller.pattern_miner import strip_proposal_scaffolding

        content = (
            "---\n"
            "type: proposed-canonical\n"
            "proposed_slug: foo\n"
            "---\n"
            "\n"
            "# Foo\n"
            "\n"
            "The real claim.\n"
        )
        stripped = strip_proposal_scaffolding(content)
        assert "---" not in stripped
        assert "type: proposed-canonical" not in stripped
        assert "# Foo" in stripped
        assert "The real claim." in stripped

    def test_strips_phase4_banner(self) -> None:
        from alfred.distiller.pattern_miner import strip_proposal_scaffolding

        content = (
            "# Topic\n"
            "\n"
            "> Phase 4 pattern miner surfaced this cluster on 2026-05-10. Andrew should read.\n"
            "\n"
            "## Mined claim\n"
            "\n"
            "The claim.\n"
        )
        stripped = strip_proposal_scaffolding(content)
        assert "Phase 4 pattern miner surfaced" not in stripped
        assert "## Mined claim" in stripped
        assert "The claim." in stripped

    def test_strips_suggested_next_step_footer(self) -> None:
        from alfred.distiller.pattern_miner import strip_proposal_scaffolding

        content = (
            "# Topic\n"
            "\n"
            "The claim.\n"
            "\n"
            "## Suggested next step\n"
            "\n"
            "Promote with: `alfred vault move ...`\n"
            "\n"
            "Or `rm` to discard.\n"
        )
        stripped = strip_proposal_scaffolding(content)
        assert "Suggested next step" not in stripped
        assert "alfred vault move" not in stripped
        assert "rm` to discard" not in stripped
        assert "The claim." in stripped

    def test_strips_empty_fenced_code_blocks(self) -> None:
        from alfred.distiller.pattern_miner import strip_proposal_scaffolding

        content = (
            "# Topic\n"
            "\n"
            "Claim text.\n"
            "\n"
            "```\n"
            "\n"
            "```\n"
            "\n"
            "More text.\n"
        )
        stripped = strip_proposal_scaffolding(content)
        # Empty fences gone.
        assert "```\n\n```" not in stripped
        # Surrounding content preserved.
        assert "Claim text." in stripped
        assert "More text." in stripped

    def test_preserves_non_empty_code_fences(self) -> None:
        # Defensive: a legitimate fenced code block with content MUST
        # NOT be stripped. The empty-fence regex requires zero non-
        # whitespace content between fences.
        from alfred.distiller.pattern_miner import strip_proposal_scaffolding

        content = (
            "# Topic\n"
            "\n"
            "```python\n"
            "print('hello')\n"
            "```\n"
        )
        stripped = strip_proposal_scaffolding(content)
        assert "```python" in stripped
        assert "print('hello')" in stripped

    def test_strips_all_four_categories_together(self) -> None:
        # End-to-end: input is the full proposal shape mine_patterns
        # writes; output is the body that lands at architecture/
        # canonical_record.md after promote.
        from alfred.distiller.pattern_miner import strip_proposal_scaffolding

        content = (
            "---\n"
            "type: proposed-canonical\n"
            "proposed_slug: foo\n"
            "---\n"
            "\n"
            "# Foo\n"
            "\n"
            "> Phase 4 pattern miner surfaced this cluster on 2026-05-10. "
            "Andrew should read.\n"
            "\n"
            "## Mined claim\n"
            "\n"
            "The load-bearing claim.\n"
            "\n"
            "```\n"
            "\n"
            "```\n"
            "\n"
            "## Source members\n"
            "\n"
            "- [[a]]\n"
            "\n"
            "## Suggested next step\n"
            "\n"
            "Promote with: `alfred vault move ...`\n"
        )
        stripped = strip_proposal_scaffolding(content)
        # All four scaffolding kinds gone.
        assert "type: proposed-canonical" not in stripped
        assert "Phase 4 pattern miner surfaced" not in stripped
        assert "```\n\n```" not in stripped
        assert "Suggested next step" not in stripped
        # Real content kept.
        assert "# Foo" in stripped
        assert "## Mined claim" in stripped
        assert "The load-bearing claim." in stripped
        assert "## Source members" in stripped
        assert "[[a]]" in stripped


class TestCanonicalPromotionBanner:
    def test_includes_date_member_count_short_fingerprint(self) -> None:
        from alfred.distiller.pattern_miner import canonical_promotion_banner

        banner = canonical_promotion_banner(
            promoted_at_iso="2026-05-11T10:00:00+00:00",
            member_count=11,
            fingerprint="abcdef1234567890abcd",
        )
        assert "2026-05-11" in banner
        assert "11 records" in banner
        # Short fingerprint = 12 chars per the helper contract.
        assert "abcdef123456" in banner
        # Full fingerprint NOT in banner (truncated for readability).
        assert "abcdef1234567890abcd" not in banner
        assert banner.startswith(">")


# ---------------------------------------------------------------------------
# insert_promotion_banner_after_title — title-then-banner ordering
# (2026-05-11 cosmetic fix: match the cli-logging.md convention)
# ---------------------------------------------------------------------------


class TestInsertPromotionBannerAfterTitle:
    """Pin the title-line-1 / banner-line-3 layout. The prior
    implementation inverted this (banner above title); operator-
    visible output didn't match the canonical convention.
    """

    def test_banner_lands_after_title_with_blank_line_separator(
        self,
    ) -> None:
        from alfred.distiller.pattern_miner import (
            insert_promotion_banner_after_title,
        )

        body = "# Topic X\n\nThe claim body.\n"
        banner = "> Promoted from inbox/proposed-canonical on 2026-05-11.\n\n"
        result = insert_promotion_banner_after_title(body, banner)

        lines = result.split("\n")
        # Title is line 1 (index 0).
        assert lines[0] == "# Topic X"
        # Line 2 (index 1) is blank.
        assert lines[1] == ""
        # Line 3 (index 2) is the banner.
        assert lines[2].startswith("> Promoted from inbox/proposed-canonical")
        # Subsequent lines preserve body content.
        assert "The claim body." in result

    def test_banner_does_not_prepend_above_title(self) -> None:
        # Regression pin against the legacy prepend-above-title shape.
        from alfred.distiller.pattern_miner import (
            insert_promotion_banner_after_title,
        )

        body = "# Topic\n\nClaim.\n"
        banner = "> Promoted line\n\n"
        result = insert_promotion_banner_after_title(body, banner)

        # The title MUST appear before the banner in the output —
        # catches a regression that restores the legacy prepend
        # behavior.
        title_pos = result.find("# Topic")
        banner_pos = result.find("> Promoted line")
        assert title_pos >= 0
        assert banner_pos >= 0
        assert title_pos < banner_pos

    def test_no_h1_falls_back_to_prepend_with_log(self) -> None:
        # Edge case: body without an H1 heading (malformed proposal,
        # or strip ran on a body that never had a title). Fallback
        # is "prepend to top" so the banner doesn't get lost; an
        # info log fires so post-mortem can grep for this shape.
        import structlog

        from alfred.distiller.pattern_miner import (
            insert_promotion_banner_after_title,
        )

        body = "Just some text without a heading.\n"
        banner = "> Promoted banner\n\n"

        with structlog.testing.capture_logs() as captured:
            result = insert_promotion_banner_after_title(body, banner)

        # Banner lands at top (prepend fallback).
        assert result.startswith("> Promoted banner")
        # Body preserved after banner.
        assert "Just some text" in result
        # No-title fallback log emitted with operator-grep-able event.
        no_title_logs = [
            c for c in captured
            if c.get("event") == "pattern_miner.promotion_banner_no_title"
        ]
        assert len(no_title_logs) == 1

    def test_h2_alone_does_not_count_as_title(self) -> None:
        # Defensive: an ``## Subhead`` without a preceding ``# Title``
        # is NOT a title. Fallback to prepend.
        from alfred.distiller.pattern_miner import (
            insert_promotion_banner_after_title,
        )

        body = "## Subhead\n\nText.\n"
        banner = "> Banner\n\n"
        result = insert_promotion_banner_after_title(body, banner)

        # Banner lands at the top (prepend) — the ## subhead doesn't
        # match _TITLE_RE so the title-aware path is skipped.
        assert result.startswith("> Banner")

    def test_first_h1_is_used_when_multiple_present(self) -> None:
        # Defensive: a body with multiple H1s (rare but possible —
        # operator hand-edited the proposal) inserts after the FIRST
        # one. Doesn't try to be clever about choosing.
        from alfred.distiller.pattern_miner import (
            insert_promotion_banner_after_title,
        )

        body = "# First Title\n\nText.\n\n# Second Title\n\nMore.\n"
        banner = "> Banner\n\n"
        result = insert_promotion_banner_after_title(body, banner)

        # Banner lands right after First Title, NOT after Second.
        first_pos = result.find("# First Title")
        banner_pos = result.find("> Banner")
        second_pos = result.find("# Second Title")
        assert first_pos < banner_pos < second_pos


# ---------------------------------------------------------------------------
# cmd_promote_proposal — happy path + error cases
# ---------------------------------------------------------------------------


class TestPromoteProposalHappyPath:
    def test_pending_proposal_promoted_state_updated_inbox_removed(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        inbox_file, state_path = _seed_inbox_proposal(tmp_path)
        _seed_state_with_proposal(state_path)
        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)

        cfg = _config_for_promote(tmp_path, state_path)
        dcli.cmd_promote_proposal(cfg, slug="topic-x")

        # Inbox file gone, canonical file written.
        assert not inbox_file.exists()
        target = tmp_path / "architecture" / "topic-x.md"
        assert target.is_file()

        # State updated.
        from alfred.distiller.pattern_miner_state import PatternMinerState
        state = PatternMinerState(state_path)
        state.load()
        entry = next(iter(state.proposals.values()))
        assert entry.status == "promoted"
        assert entry.promoted_to == "architecture/topic-x.md"
        assert entry.promoted_at != ""

        # Stdout confirmation.
        out = capsys.readouterr().out
        assert "promoted: topic-x" in out
        assert "architecture/topic-x.md" in out

    def test_strip_scaffolding_default_on_strips_and_inserts_banner_after_title(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        inbox_file, state_path = _seed_inbox_proposal(tmp_path)
        _seed_state_with_proposal(state_path)
        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)

        cfg = _config_for_promote(tmp_path, state_path)
        dcli.cmd_promote_proposal(cfg, slug="topic-x")

        target = tmp_path / "architecture" / "topic-x.md"
        content = target.read_text()
        # Scaffolding gone.
        assert "type: proposed-canonical" not in content
        assert "Phase 4 pattern miner surfaced" not in content
        assert "Suggested next step" not in content
        # Canonical promotion banner present.
        assert "Promoted from inbox/proposed-canonical" in content
        # Real content preserved.
        assert "## Mined claim" in content
        assert "Hardcoding a single log destination" in content
        # NEW (2026-05-11): banner lands AFTER the title, not before.
        # Matches the cli-logging.md convention.
        title_pos = content.find("# Topic X")
        banner_pos = content.find("> Promoted from inbox/proposed-canonical")
        assert title_pos >= 0
        assert banner_pos >= 0
        assert title_pos < banner_pos, (
            "Banner must land AFTER the title line per "
            "cli-logging.md convention; got banner at "
            f"{banner_pos} but title at {title_pos}"
        )
        # Also pin the line-3 structure (title / blank / banner).
        lines = content.split("\n")
        assert lines[0] == "# Topic X", (
            f"Title should be line 1; got {lines[0]!r}"
        )
        assert lines[1] == "", (
            f"Line 2 should be blank (separator); got {lines[1]!r}"
        )
        assert lines[2].startswith("> Promoted from inbox/proposed-canonical"), (
            f"Line 3 should be the banner; got {lines[2]!r}"
        )

    def test_no_strip_scaffolding_preserves_verbatim(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        inbox_file, state_path = _seed_inbox_proposal(tmp_path)
        _seed_state_with_proposal(state_path)
        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)

        cfg = _config_for_promote(tmp_path, state_path)
        dcli.cmd_promote_proposal(
            cfg, slug="topic-x", strip_scaffolding=False,
        )

        target = tmp_path / "architecture" / "topic-x.md"
        content = target.read_text()
        # Verbatim: frontmatter + banner + footer all present.
        assert "type: proposed-canonical" in content
        assert "Phase 4 pattern miner surfaced" in content
        assert "Suggested next step" in content
        # No canonical promotion banner (no strip = no banner).
        assert "Promoted from inbox/proposed-canonical" not in content

    def test_to_override_uses_explicit_target_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        inbox_file, state_path = _seed_inbox_proposal(tmp_path)
        _seed_state_with_proposal(state_path)
        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)

        cfg = _config_for_promote(tmp_path, state_path)
        dcli.cmd_promote_proposal(
            cfg, slug="topic-x", to="principles/renamed-thing.md",
        )

        # Target lands at the override path; default-derived path NOT
        # used.
        override = tmp_path / "principles" / "renamed-thing.md"
        default = tmp_path / "architecture" / "topic-x.md"
        assert override.is_file()
        assert not default.exists()

        # State records the actual target.
        from alfred.distiller.pattern_miner_state import PatternMinerState
        state = PatternMinerState(state_path)
        state.load()
        entry = next(iter(state.proposals.values()))
        assert entry.promoted_to == "principles/renamed-thing.md"

    def test_auto_derived_path_uses_canonical_type_and_slug(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Pin the default-target derivation rule:
        # <proposed_canonical_type>/<proposed_slug>.md
        inbox_file, state_path = _seed_inbox_proposal(
            tmp_path, slug="foo", canonical_type="principles",
        )
        _seed_state_with_proposal(
            state_path, slug="foo", canonical_type="principles",
        )
        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)

        cfg = _config_for_promote(tmp_path, state_path)
        dcli.cmd_promote_proposal(cfg, slug="foo")  # no --to

        target = tmp_path / "principles" / "foo.md"
        assert target.is_file()

    def test_audit_log_written_with_distiller_tool_and_2_rows(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        inbox_file, state_path = _seed_inbox_proposal(tmp_path)
        _seed_state_with_proposal(state_path)
        audit_log = tmp_path / "vault_audit.log"
        monkeypatch.setenv("ALFRED_VAULT_AUDIT_LOG", str(audit_log))

        cfg = _config_for_promote(tmp_path, state_path)
        dcli.cmd_promote_proposal(cfg, slug="topic-x")

        assert audit_log.is_file()
        rows = [
            json.loads(line)
            for line in audit_log.read_text().splitlines()
            if line.strip()
        ]
        # 2 rows: create canonical + delete inbox.
        assert len(rows) == 2
        creates = [r for r in rows if r["op"] == "create"]
        deletes = [r for r in rows if r["op"] == "delete"]
        assert len(creates) == 1
        assert len(deletes) == 1
        assert creates[0]["path"] == "architecture/topic-x.md"
        assert deletes[0]["path"] == "inbox/proposed-canonical/topic-x.md"
        # Tool field is "distiller" (NOT "cli" or "vault").
        assert creates[0]["tool"] == "distiller"
        assert deletes[0]["tool"] == "distiller"
        # Detail field carries fingerprint context.
        assert "fingerprint" in creates[0]["detail"]


class TestPromoteProposalErrors:
    def test_slug_not_found_errors(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state_path = tmp_path / "data" / "state.json"
        _seed_state_with_proposal(state_path, slug="topic-x")
        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)

        cfg = _config_for_promote(tmp_path, state_path)
        with pytest.raises(SystemExit) as exc_info:
            dcli.cmd_promote_proposal(cfg, slug="nonexistent")
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "no proposal found for slug" in out

    def test_already_promoted_errors(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state_path = tmp_path / "data" / "state.json"
        _seed_state_with_proposal(
            state_path, slug="topic-x", status="promoted",
        )
        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)

        cfg = _config_for_promote(tmp_path, state_path)
        with pytest.raises(SystemExit) as exc_info:
            dcli.cmd_promote_proposal(cfg, slug="topic-x")
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "not promotable" in out

    def test_already_discarded_errors(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state_path = tmp_path / "data" / "state.json"
        _seed_state_with_proposal(
            state_path, slug="topic-x", status="discarded",
        )
        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)

        cfg = _config_for_promote(tmp_path, state_path)
        with pytest.raises(SystemExit) as exc_info:
            dcli.cmd_promote_proposal(cfg, slug="topic-x")
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "not promotable" in out

    def test_ambiguous_slug_errors_with_fingerprints_listed(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Two entries sharing the same slug (post-collision-resolve
        # scenario; mine_patterns can produce e.g. topic-x +
        # topic-x-2 with the same proposed_slug after recovery).
        from alfred.distiller.pattern_miner_state import (
            PatternMinerState,
            ProposalEntry,
        )

        state_path = tmp_path / "data" / "state.json"
        state = PatternMinerState(state_path)
        for fp in ("fp_aaa111", "fp_bbb222"):
            state.record_proposal(ProposalEntry(
                fingerprint=fp,
                cluster_id="semantic_5",
                proposed_slug="topic-x",  # collision: same slug
                proposed_path=f"inbox/proposed-canonical/topic-x-{fp}.md",
                status="pending",
            ))
        state.save()
        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)

        cfg = _config_for_promote(tmp_path, state_path)
        with pytest.raises(SystemExit) as exc_info:
            dcli.cmd_promote_proposal(cfg, slug="topic-x")
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "multiple proposals with slug" in out
        # Fingerprints listed for operator disambiguation.
        assert "fp_aaa111" in out or "fp_aaa111"[:12] in out
        assert "fp_bbb222" in out or "fp_bbb222"[:12] in out

    def test_fingerprint_disambiguation_works(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from alfred.distiller.pattern_miner_state import (
            PatternMinerState,
            ProposalEntry,
        )

        # Two slug-colliding entries. Seed corresponding inbox files
        # so the chosen one can actually be promoted.
        inbox_dir = tmp_path / "inbox" / "proposed-canonical"
        inbox_dir.mkdir(parents=True)
        for stem in ("topic-x-aaa", "topic-x-bbb"):
            (inbox_dir / f"{stem}.md").write_text(
                "---\ntype: proposed-canonical\n---\n\n"
                "# Topic\n\nBody text.\n",
                encoding="utf-8",
            )

        state_path = tmp_path / "data" / "state.json"
        state = PatternMinerState(state_path)
        state.record_proposal(ProposalEntry(
            fingerprint="fp_aaa111",
            proposed_slug="topic-x",
            proposed_path="inbox/proposed-canonical/topic-x-aaa.md",
            proposed_canonical_type="architecture",
            status="pending",
        ))
        state.record_proposal(ProposalEntry(
            fingerprint="fp_bbb222",
            proposed_slug="topic-x",
            proposed_path="inbox/proposed-canonical/topic-x-bbb.md",
            proposed_canonical_type="principles",
            status="pending",
        ))
        state.save()
        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)

        cfg = _config_for_promote(tmp_path, state_path)
        # Pick the second one explicitly by fingerprint.
        dcli.cmd_promote_proposal(
            cfg,
            slug="topic-x",
            fingerprint="fp_bbb222",
            to="principles/disambiguated.md",
        )

        # Chosen entry promoted; the other one untouched.
        state2 = PatternMinerState(state_path)
        state2.load()
        assert state2.proposals["fp_bbb222"].status == "promoted"
        assert state2.proposals["fp_aaa111"].status == "pending"

    def test_target_path_already_exists_errors(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Operator-safety check: don't overwrite an existing canonical
        # record. Explicit error so the operator can rm first or
        # choose a different --to path.
        inbox_file, state_path = _seed_inbox_proposal(tmp_path)
        _seed_state_with_proposal(state_path)
        # Pre-create the canonical target.
        (tmp_path / "architecture").mkdir()
        (tmp_path / "architecture" / "topic-x.md").write_text(
            "existing canonical record\n", encoding="utf-8",
        )
        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)

        cfg = _config_for_promote(tmp_path, state_path)
        with pytest.raises(SystemExit) as exc_info:
            dcli.cmd_promote_proposal(cfg, slug="topic-x")
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "already exists" in out
        # Inbox file NOT removed (atomic: either both target-write
        # and inbox-delete succeed, or neither).
        assert inbox_file.exists()
        # State unchanged.
        from alfred.distiller.pattern_miner_state import PatternMinerState
        state = PatternMinerState(state_path)
        state.load()
        entry = next(iter(state.proposals.values()))
        assert entry.status == "pending"


# ---------------------------------------------------------------------------
# cmd_discard_proposal — happy path + error cases
# ---------------------------------------------------------------------------


class TestDiscardProposalHappyPath:
    def test_pending_proposal_discarded_state_updated_inbox_removed(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        inbox_file, state_path = _seed_inbox_proposal(tmp_path)
        _seed_state_with_proposal(state_path)
        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)

        cfg = _config_for_promote(tmp_path, state_path)
        dcli.cmd_discard_proposal(cfg, slug="topic-x")

        assert not inbox_file.exists()
        from alfred.distiller.pattern_miner_state import PatternMinerState
        state = PatternMinerState(state_path)
        state.load()
        entry = next(iter(state.proposals.values()))
        assert entry.status == "discarded"
        assert entry.discarded_at != ""

        out = capsys.readouterr().out
        assert "discarded: topic-x" in out

    def test_reason_recorded_in_state(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        inbox_file, state_path = _seed_inbox_proposal(tmp_path)
        _seed_state_with_proposal(state_path)
        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)

        cfg = _config_for_promote(tmp_path, state_path)
        dcli.cmd_discard_proposal(
            cfg, slug="topic-x",
            reason="overlaps with principles/foo.md",
        )

        from alfred.distiller.pattern_miner_state import PatternMinerState
        state = PatternMinerState(state_path)
        state.load()
        entry = next(iter(state.proposals.values()))
        assert entry.discarded_reason == "overlaps with principles/foo.md"

    def test_audit_log_written_with_delete_row_and_reason_in_detail(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        inbox_file, state_path = _seed_inbox_proposal(tmp_path)
        _seed_state_with_proposal(state_path)
        audit_log = tmp_path / "vault_audit.log"
        monkeypatch.setenv("ALFRED_VAULT_AUDIT_LOG", str(audit_log))

        cfg = _config_for_promote(tmp_path, state_path)
        dcli.cmd_discard_proposal(
            cfg, slug="topic-x", reason="duplicate theme",
        )

        rows = [
            json.loads(line)
            for line in audit_log.read_text().splitlines()
            if line.strip()
        ]
        # Single row: delete inbox.
        assert len(rows) == 1
        assert rows[0]["op"] == "delete"
        assert rows[0]["path"] == "inbox/proposed-canonical/topic-x.md"
        assert rows[0]["tool"] == "distiller"
        # Reason carried in detail.
        assert "duplicate theme" in rows[0]["detail"]
        assert "fingerprint" in rows[0]["detail"]

    def test_inbox_already_absent_still_records_state(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Defensive: operator manually rm'd the inbox file before
        # running discard-proposal. State mutation is the load-
        # bearing record; the missing file is non-fatal.
        state_path = tmp_path / "data" / "state.json"
        _seed_state_with_proposal(state_path)
        # NO inbox file seeded.
        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)

        cfg = _config_for_promote(tmp_path, state_path)
        dcli.cmd_discard_proposal(cfg, slug="topic-x")

        from alfred.distiller.pattern_miner_state import PatternMinerState
        state = PatternMinerState(state_path)
        state.load()
        entry = next(iter(state.proposals.values()))
        assert entry.status == "discarded"
        out = capsys.readouterr().out
        assert "already absent" in out


class TestDiscardProposalErrors:
    def test_slug_not_found_errors(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state_path = tmp_path / "data" / "state.json"
        _seed_state_with_proposal(state_path)
        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)

        cfg = _config_for_promote(tmp_path, state_path)
        with pytest.raises(SystemExit) as exc_info:
            dcli.cmd_discard_proposal(cfg, slug="nonexistent")
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "no proposal found for slug" in out

    def test_already_terminal_errors(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state_path = tmp_path / "data" / "state.json"
        _seed_state_with_proposal(
            state_path, slug="topic-x", status="promoted",
        )
        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)

        cfg = _config_for_promote(tmp_path, state_path)
        with pytest.raises(SystemExit) as exc_info:
            dcli.cmd_discard_proposal(cfg, slug="topic-x")
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "not discardable" in out


# ---------------------------------------------------------------------------
# Parent dispatcher — cmd_distiller injects ALFRED_VAULT_AUDIT_LOG
# (mirrors cmd_vault precedent from issue #64)
# ---------------------------------------------------------------------------


class TestCmdDistillerDispatcherWiring:
    """Pin that cmd_distiller injects ALFRED_VAULT_AUDIT_LOG for the
    promote-proposal / discard-proposal subcommands. The actual
    subcommand handler is stubbed so this test surface stays tight
    to the env-wiring contract (other test classes cover the
    handlers themselves).
    """

    def _write_config(
        self,
        path: Path,
        *,
        log_dir: str,
        vault_path: Path,
    ) -> None:
        path.write_text(
            f"vault:\n"
            f"  path: {vault_path}\n"
            f"logging:\n"
            f"  dir: {log_dir}\n",
            encoding="utf-8",
        )

    def _make_args(
        self, config_path: Path, distiller_cmd: str,
    ):
        import argparse

        ns = argparse.Namespace(
            config=str(config_path),
            distiller_cmd=distiller_cmd,
            # promote/discard kwargs
            slug="topic-x",
            to=None,
            no_strip_scaffolding=False,
            reason=None,
            fingerprint=None,
        )
        return ns

    def _stub_handler(
        self,
        monkeypatch: pytest.MonkeyPatch,
        captured: dict,
        handler_name: str,
    ) -> None:
        """Replace dcli.cmd_promote_proposal / cmd_discard_proposal
        with a stub that captures ``ALFRED_VAULT_AUDIT_LOG`` at call
        time. The dispatcher's contract is "set the env var BEFORE
        delegating," so capturing from inside the stub pins ordering.
        """
        import os

        def _stub(*args, **kwargs) -> None:
            captured["audit_log"] = os.environ.get("ALFRED_VAULT_AUDIT_LOG")
        monkeypatch.setattr(
            f"alfred.distiller.cli.{handler_name}", _stub,
        )

    def test_promote_proposal_injects_audit_log_env_var(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from alfred.cli import cmd_distiller

        vault = tmp_path / "vault"
        vault.mkdir()
        log_dir = tmp_path / "data"
        log_dir.mkdir()
        config = tmp_path / "config.yaml"
        self._write_config(
            config, log_dir=str(log_dir), vault_path=vault,
        )

        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)
        captured: dict = {}
        self._stub_handler(
            monkeypatch, captured, "cmd_promote_proposal",
        )

        cmd_distiller(self._make_args(config, "promote-proposal"))
        assert (
            captured["audit_log"] == str(log_dir / "vault_audit.log")
        )

    def test_discard_proposal_injects_audit_log_env_var(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from alfred.cli import cmd_distiller

        vault = tmp_path / "vault"
        vault.mkdir()
        log_dir = tmp_path / "data"
        log_dir.mkdir()
        config = tmp_path / "config.yaml"
        self._write_config(
            config, log_dir=str(log_dir), vault_path=vault,
        )

        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)
        captured: dict = {}
        self._stub_handler(
            monkeypatch, captured, "cmd_discard_proposal",
        )

        cmd_distiller(self._make_args(config, "discard-proposal"))
        assert (
            captured["audit_log"] == str(log_dir / "vault_audit.log")
        )

    def test_other_distiller_subcommands_do_not_inject_audit_log(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Non-mutating subcommands (status, history, rank-week, etc.)
        # don't need the audit context — pin that the dispatcher
        # ONLY injects for the two new subcommands. Catches a
        # regression where the injection block accidentally widens
        # to all subcommands.
        from alfred.cli import cmd_distiller

        vault = tmp_path / "vault"
        vault.mkdir()
        log_dir = tmp_path / "data"
        log_dir.mkdir()
        config = tmp_path / "config.yaml"
        self._write_config(
            config, log_dir=str(log_dir), vault_path=vault,
        )

        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)
        captured: dict = {}

        import os

        def _stub_status(*args, **kwargs) -> None:
            captured["audit_log"] = os.environ.get("ALFRED_VAULT_AUDIT_LOG")
        monkeypatch.setattr(
            "alfred.distiller.cli.cmd_status", _stub_status,
        )

        import argparse
        ns = argparse.Namespace(
            config=str(config),
            distiller_cmd="status",
        )
        cmd_distiller(ns)
        # No env var set by dispatcher → still None.
        assert captured["audit_log"] is None

    def test_dispatcher_respects_caller_override(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # If the caller / test harness has already set
        # ALFRED_VAULT_AUDIT_LOG, the dispatcher MUST NOT overwrite
        # it. Mirrors the standard ALFRED_* env-var precedence.
        from alfred.cli import cmd_distiller

        vault = tmp_path / "vault"
        vault.mkdir()
        log_dir = tmp_path / "data"
        log_dir.mkdir()
        config = tmp_path / "config.yaml"
        self._write_config(
            config, log_dir=str(log_dir), vault_path=vault,
        )

        override = str(tmp_path / "test-only-audit.log")
        monkeypatch.setenv("ALFRED_VAULT_AUDIT_LOG", override)
        captured: dict = {}
        self._stub_handler(
            monkeypatch, captured, "cmd_promote_proposal",
        )

        cmd_distiller(self._make_args(config, "promote-proposal"))
        # Override preserved.
        assert captured["audit_log"] == override
