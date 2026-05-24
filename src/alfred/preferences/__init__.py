"""Operator-preference V1 — vault-backed contract for persistent operator policies.

Two preference shapes:
- **Shape A (action)** — extraction/inclusion gates consumed by curator
  + brief. Matchers dispatch via a small enum (skip_event_if,
  skip_brief_event_if, skip_brief_task_if).
- **Shape B (voice)** — talker system-prompt directives loaded at
  session start. Subdivided into B1 universal (all instances) and B2
  instance-specific.

Storage:
- Salem canonical: ``<salem-vault>/preference/<slug>.md``
- Hypatia local instance-application: ``<library-alexandria>/preference/<slug>.md``
- KAL-LE: no local preference records in V1 (not a heavy talker surface)

Cross-instance reads are filesystem-level (not peer-protocol). Hypatia
+ KAL-LE read Salem's preference directory directly at session start;
conflict resolution (local-wins) lives at the talker system-prompt
assembly layer.

See ``project_operator_preferences_v1.md`` for the full contract.
"""
from __future__ import annotations

from .loader import Preference, load_active_preferences
from .matchers import KNOWN_RULES, MatcherResult, evaluate

__all__ = [
    "KNOWN_RULES",
    "MatcherResult",
    "Preference",
    "evaluate",
    "load_active_preferences",
]
