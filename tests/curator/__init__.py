"""Curator test package.

Test surface (post backend-abstraction-collapse, 2026-05-25):

- ``test_pipeline_attribution.py`` — Stage 2 ``_resolve_entities``
  attribution-marker wrapping. Uses the pure-Python helper directly;
  does NOT exercise the LLM-dispatch pipeline (deleted with the
  OpenClaw backend).
- ``test_vault_context.py`` — slim-context rendering contract.
- ``test_idle_tick.py`` — heartbeat counter for empty-inbox runs.

The OpenClaw-pipeline test files (test_pipeline_stages,
test_pipeline_concurrency, test_pipeline_errors) plus the shared
``conftest.py`` + ``_fakes.py`` were removed in the
backend-abstraction-collapse arc. The 4-stage pipeline was
OpenClaw-only by design; production runs the legacy single-call
path via the Claude backend.
"""
