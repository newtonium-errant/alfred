"""One-time migration scripts shipped inside the alfred package.

Each module here is an executable migration script that ships once for
a specific schema-cutover or data-cleanup event. They live inside the
package (rather than in the top-level ``scripts/`` tree) so they can:

  * Be imported normally by tests (avoids the importlib-spec /
    dataclass-introspection trap where ``cls.__module__`` resolves
    to a module not in ``sys.modules``).
  * Be invoked via ``python -m alfred.scripts.<module>`` without
    PYTHONPATH gymnastics.
  * Carry full type-checker / IDE support as proper package members.

The top-level ``scripts/`` tree retains thin wrapper shims for backward-
compat with the documented invocation paths (e.g.
``python scripts/migrate_2026-05-16_meditations_zettels.py``); each
shim defers to the corresponding ``alfred.scripts.<module>:main``.
"""
