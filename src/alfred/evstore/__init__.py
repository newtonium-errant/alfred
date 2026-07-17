"""``alfred.evstore`` — a generic, separable, append-only, hash-chained event store.

Tamper-EVIDENT (not tamper-proof): any in-place edit, deletion, or reorder breaks every
downstream hash in an O(n) ``verify``. PHI-free-by-construction: an append refuses any
undeclared payload field or non-scalar value, so free text is structurally impossible.

Ruling 1 (separable): stdlib + ``structlog`` only, ZERO ``alfred.*`` imports — extraction is a
``git mv`` + a pyproject entry. The import-purity property is pinned by an AST scan
(`tests/test_evstore_import_purity.py`).
"""
from __future__ import annotations

from .chain import CANONICALIZATION, GENESIS_PREV, canonical_json, entry_hash, sha256_hex
from .store import (
    ENVELOPE_V,
    STORE_PROTOCOL,
    Actor,
    AppendReceipt,
    EventStore,
    EventStoreError,
    VerifyReport,
)

__all__ = [
    "EventStore",
    "Actor",
    "AppendReceipt",
    "VerifyReport",
    "EventStoreError",
    "ENVELOPE_V",
    "STORE_PROTOCOL",
    "CANONICALIZATION",
    "GENESIS_PREV",
    "canonical_json",
    "entry_hash",
    "sha256_hex",
]
