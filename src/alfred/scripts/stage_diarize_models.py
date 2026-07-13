"""Stage the STAY-C pyannote diarization models OFFLINE + materialize the
repo-id-free local pipeline config (scribe P4-4).

Operator-run, ON-BOX, one-time. Downloads the three pyannote repos the
speaker-diarization-3.1 pipeline needs into the relocated STAY-C HF cache, then
MATERIALIZES a pipeline config YAML whose sub-model references are ABSOLUTE LOCAL
PATHS (no repo ids). This is the load mechanism the runtime engine uses because
pyannote's ``from_pretrained`` does NOT reliably honor ``local_files_only`` — a
repo-id-bearing config still triggers a hub revision GET even when everything is
cached. Loading FROM the materialized local-path config is what keeps the engine
offline; ``HF_HUB_OFFLINE=1`` + the sovereign requests-guard are the fail-closed
BACKSTOPS (see ``scribe.diarize._run_pyannote_pipeline``).

Mirrors the ``install_stayc_unit.verify_or_stage_model`` (#67 F3) precedent:
pure path/transform helpers (unit-tested, no network) + an I/O ``main`` that does
the download + writes the materialized config.

THE THREE REPOS:
  * ``pyannote/segmentation-3.0``                  — GATED (operator accepted).
  * ``pyannote/speaker-diarization-3.1``           — GATED (operator accepted;
    carries the pipeline config.yaml this script materializes).
  * ``pyannote/wespeaker-voxceleb-resnet34-LM``    — UNGATED (verified 2026-07-13).

TOKEN — read at RUNTIME ONLY from ``$HF_TOKEN`` or ``--token-file``; NEVER
persisted by this script, NEVER written to the unit env, NEVER committed, NEVER
logged (it is passed straight to ``snapshot_download`` and dropped). The
operator's token is stashed dev-side at ``~/.secrets/hf_token`` (0600) — pass
``--token-file ~/.secrets/hf_token`` (this script does NOT read that path
implicitly).

Usage::

    python -m alfred.scripts.stage_diarize_models \\
        --hf-home /data/algernon/stayc-clinical/models/hf \\
        --token-file ~/.secrets/hf_token

Then set ``scribe.diarize.provider: pyannote``, ``enabled: true``, and
``pipeline_config: <printed materialized-config path>`` in the STAY-C config.
"""

from __future__ import annotations

import argparse
import copy
import os
import sys
from pathlib import Path
from typing import Any, Sequence

import yaml

# The three repos the speaker-diarization-3.1 pipeline pulls. Constants (not flags)
# — the pipeline VERSION is pinned by the [scribe-diarize] extra + the materialize
# validation below; changing repos is a deliberate code edit, not a runtime knob.
SEGMENTATION_REPO = "pyannote/segmentation-3.0"          # GATED
DIARIZATION_REPO = "pyannote/speaker-diarization-3.1"    # GATED (carries config.yaml)
EMBEDDING_REPO = "pyannote/wespeaker-voxceleb-resnet34-LM"  # UNGATED

#: The pipeline config.yaml basename inside the diarization snapshot.
PIPELINE_CONFIG_NAME = "config.yaml"

#: Where the materialized repo-id-free config is written (under HF_HOME so it
#: travels with the cache). The operator points ``scribe.diarize.pipeline_config``
#: at this file.
MATERIALIZED_CONFIG_NAME = "speaker-diarization-3.1.local.yaml"

#: A checkpoint basename preferred inside a model snapshot (pyannote loads a local
#: checkpoint file for segmentation/embedding). Falls back to the snapshot dir.
_CHECKPOINT_BASENAME = "pytorch_model.bin"


# ---------------------------------------------------------------------------
# Pure helpers (no network, no torch) — unit-tested
# ---------------------------------------------------------------------------


def hub_cache_dirname(repo_id: str) -> str:
    """``org/name`` → the huggingface_hub cache dir name ``models--org--name``."""
    return "models--" + repo_id.replace("/", "--")


def materialize_pipeline_config(
    config: dict[str, Any], *, segmentation_path: str, embedding_path: str,
) -> dict[str, Any]:
    """Rewrite the pyannote pipeline config's sub-model REPO IDS to absolute LOCAL
    PATHS (the repo-id-free materialization).

    Returns a NEW dict (the input is not mutated). Substitutes
    ``pipeline.params.segmentation`` and ``pipeline.params.embedding`` — the two
    repo-id references speaker-diarization-3.1 carries — with the given local
    paths. FAIL-LOUD (``ValueError``) if the expected structure/keys are absent:
    the pipeline-config format is pyannote-version-sensitive, so an unexpected
    shape must surface HERE (at staging) rather than silently leave a repo id that
    triggers a runtime hub GET. Keeps every OTHER key (version, clustering params,
    thresholds) byte-for-byte."""
    if not isinstance(config, dict):
        raise ValueError(
            f"pyannote pipeline config must be a mapping; got {type(config).__name__}. "
            f"The downloaded {PIPELINE_CONFIG_NAME} is malformed."
        )
    out = copy.deepcopy(config)
    pipeline = out.get("pipeline")
    params = pipeline.get("params") if isinstance(pipeline, dict) else None
    if not isinstance(params, dict):
        raise ValueError(
            "pyannote pipeline config has no 'pipeline.params' mapping — the "
            "speaker-diarization config format changed (version skew). Re-check the "
            "[scribe-diarize] pyannote.audio pin against the materialize transform."
        )
    missing = [k for k in ("segmentation", "embedding") if k not in params]
    if missing:
        raise ValueError(
            f"pyannote pipeline config 'pipeline.params' is missing {missing} — the "
            f"repo-id references this script rewrites are absent (version skew). "
            f"Re-check the pyannote.audio pin vs the materialize transform."
        )
    params["segmentation"] = segmentation_path
    params["embedding"] = embedding_path
    return out


def _pick_local_model_path(snapshot_dir: Path) -> str:
    """The local path to hand pyannote for a model snapshot: the checkpoint file
    when present, else the snapshot directory. (pyannote accepts either a local
    checkpoint path or a dir; the exact preference is confirmed on-box, the
    operator's half.)"""
    ckpt = snapshot_dir / _CHECKPOINT_BASENAME
    return str(ckpt if ckpt.is_file() else snapshot_dir)


def resolve_token(*, token_file: Path | None, env: dict[str, str]) -> str:
    """Resolve the HF token at RUNTIME — ``--token-file`` first, then ``$HF_TOKEN``.

    Read-and-drop: never persisted, never returned to any caller that logs it (the
    only consumer is ``snapshot_download``). Fail-LOUD with a hint if neither
    source is present — a gated download without a token 401s with a confusing
    error, so surface the real cause + the stash-path hint here."""
    if token_file is not None:
        try:
            token = token_file.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise RuntimeError(
                f"--token-file {token_file} is not readable: {e}. Point it at the "
                f"operator token (stashed at ~/.secrets/hf_token, 0600)."
            ) from e
        if not token:
            raise RuntimeError(f"--token-file {token_file} is empty.")
        return token
    token = (env.get("HF_TOKEN") or "").strip()
    if not token:
        raise RuntimeError(
            "no HF token — set $HF_TOKEN or pass --token-file (the operator token "
            "is stashed at ~/.secrets/hf_token, 0600). Two of the three pyannote "
            "repos are GATED and need it to download. The token is used only for "
            "this download and is never persisted or logged."
        )
    return token


# ---------------------------------------------------------------------------
# I/O (network + huggingface_hub) — the operator-run download half
# ---------------------------------------------------------------------------


def _snapshot_download(repo_id: str, *, hf_home: Path, token: str) -> Path:
    """Download ``repo_id`` into ``hf_home`` (lazy-imports huggingface_hub — never
    imported in torch-free CI). Returns the local snapshot dir."""
    from huggingface_hub import snapshot_download  # lazy — heavy/optional dep

    local = snapshot_download(
        repo_id, cache_dir=str(hf_home / "hub"), token=token,
    )
    return Path(local)


def stage(
    *, hf_home: Path, token: str, out_path: Path | None = None,
) -> Path:
    """Download the three repos + write the materialized repo-id-free config.

    Returns the path to the materialized config (the value for
    ``scribe.diarize.pipeline_config``). Network I/O; not exercised in CI (the pure
    helpers above are)."""
    seg_dir = _snapshot_download(SEGMENTATION_REPO, hf_home=hf_home, token=token)
    emb_dir = _snapshot_download(EMBEDDING_REPO, hf_home=hf_home, token=token)
    diar_dir = _snapshot_download(DIARIZATION_REPO, hf_home=hf_home, token=token)

    pipeline_cfg_path = diar_dir / PIPELINE_CONFIG_NAME
    if not pipeline_cfg_path.is_file():
        raise RuntimeError(
            f"{DIARIZATION_REPO} snapshot has no {PIPELINE_CONFIG_NAME} at "
            f"{pipeline_cfg_path} — the repo layout changed; cannot materialize."
        )
    raw_cfg = yaml.safe_load(pipeline_cfg_path.read_text(encoding="utf-8")) or {}
    materialized = materialize_pipeline_config(
        raw_cfg,
        segmentation_path=_pick_local_model_path(seg_dir),
        embedding_path=_pick_local_model_path(emb_dir),
    )
    out_path = out_path or (hf_home / MATERIALIZED_CONFIG_NAME)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml.safe_dump(materialized, sort_keys=False), encoding="utf-8")
    return out_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Stage the STAY-C pyannote diarization models offline + materialize the "
            "repo-id-free local pipeline config (scribe P4-4). Operator-run, on-box, "
            "one-time. The HF token is read at runtime only ($HF_TOKEN or "
            "--token-file, e.g. ~/.secrets/hf_token) and is never persisted or logged."
        ),
    )
    parser.add_argument(
        "--hf-home", type=Path, required=True,
        help="The relocated STAY-C HF cache root (HF_HOME) to download into.",
    )
    parser.add_argument(
        "--token-file", type=Path, default=None,
        help="File holding the HF token (e.g. ~/.secrets/hf_token, 0600). Falls "
             "back to $HF_TOKEN. Read at runtime only — never persisted or logged.",
    )
    parser.add_argument(
        "--out", type=Path, default=None,
        help=f"Where to write the materialized config (default: "
             f"<hf-home>/{MATERIALIZED_CONFIG_NAME}).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Resolve the token + print the plan without downloading or writing.",
    )
    args = parser.parse_args(argv)

    hf_home = args.hf_home.expanduser()
    try:
        token = resolve_token(token_file=args.token_file, env=dict(os.environ))
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print("STAY-C pyannote diarization model staging (P4-4)")
    print(f"  HF_HOME:       {hf_home}")
    print(f"  repos:         {SEGMENTATION_REPO} (gated), {DIARIZATION_REPO} "
          f"(gated), {EMBEDDING_REPO} (ungated)")
    print(f"  token:         resolved ({'--token-file' if args.token_file else '$HF_TOKEN'}) "
          f"— not logged")  # NEVER print the token value
    out_path = args.out.expanduser() if args.out else (hf_home / MATERIALIZED_CONFIG_NAME)
    print(f"  materialize →  {out_path}")

    if args.dry_run:
        print("--- DRY-RUN — no download, no write. ---")
        return 0

    try:
        written = stage(hf_home=hf_home, token=token, out_path=out_path)
    except Exception as e:  # noqa: BLE001 — surface a clean actionable error (never the token)
        print(f"error: staging failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(f"  wrote materialized config: {written}")
    print()
    print("Set in the STAY-C config:")
    print("  scribe:")
    print("    diarize:")
    print("      provider: pyannote")
    print("      enabled: true")
    print(f"      pipeline_config: {written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
