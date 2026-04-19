"""Curator pipeline test package.

See ``conftest.py`` for the shared fake backend / seeded vault / seeded inbox
fixtures. Tests are split by concern:

- ``test_pipeline_stages.py`` — stage 1 → 4 happy-path coverage
- ``test_pipeline_concurrency.py`` — parallel processing + failure semantics
- ``test_vault_context.py`` — slim-context rendering contract
- ``test_pipeline_errors.py`` — manifest parse, malformed stdout, edge cases
"""
