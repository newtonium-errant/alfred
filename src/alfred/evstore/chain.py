"""Canonicalization + hash-chain primitives for the append-only event store.

Frozen as **c14n-v1** (§4 of the event-store design). A change to any rule here is a
store-protocol bump, not a patch — the verifier's rules are consumer-fields-from-day-one, so
they are pinned by a golden-vector test (`tests/test_evstore_chain.py`).

Pure stdlib (`json`, `hashlib`). Zero `alfred.*` imports — this module is the separable
product's chain core (Ruling 1); the import-purity pin AST-scans the whole package.
"""
from __future__ import annotations

import hashlib
import json

# The canonicalization protocol id. Bump ⇒ new store_protocol (never re-chain old rows).
CANONICALIZATION = "c14n-v1"

# Genesis linkage: the first entry of every stream chains to 64 zero-nibbles.
GENESIS_PREV = "0" * 64


def canonical_json(obj: object) -> str:
    """c14n-v1: deterministic JSON — sorted keys, no whitespace, ASCII-escaped.

    ``ensure_ascii=True`` keeps the byte stream stable regardless of locale/terminal and makes
    the golden vectors reproducible on any host. ``sort_keys`` makes key order irrelevant, so a
    reader that reserializes an entry (minus ``entry_sha``) reproduces the exact bytes that were
    hashed. ``separators`` strips the spaces ``json.dumps`` inserts by default."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def entry_hash(prev: str, entry_without_sha: dict) -> str:
    """The chain hash for one entry: ``sha256(prev + "\\n" + canonical_json(entry))`` as hex.

    ``entry_without_sha`` is the full envelope EXCEPT its own ``entry_sha`` field (which does not
    exist yet at hash time). ``prev`` is the previous entry's ``entry_sha`` (or :data:`GENESIS_PREV`
    for the first entry). The ``\\n`` separator makes ``prev`` unambiguously delimited from the
    canonical body so no crafted body can collide with a ``prev||body`` concatenation."""
    material = prev + "\n" + canonical_json(entry_without_sha)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def recompute_entry_sha(entry: dict) -> str:
    """Recompute the chain hash of a fully-formed entry (verify path).

    Strips the stored ``entry_sha`` and rehashes over ``prev`` + the rest — version-agnostic by
    design (§9): it needs no semantic understanding of the row, so an unknown-``v`` entry still
    verifies. Raises :class:`KeyError` only if ``prev`` is absent (a structurally broken row)."""
    body = {k: v for k, v in entry.items() if k != "entry_sha"}
    return entry_hash(entry["prev"], body)


def sha256_hex(data: bytes | str) -> str:
    """sha256 hex of bytes (or a str, UTF-8 encoded) — the genesis predecessor-pin +
    path-digest helper. Kept here so the one hashing convention lives in one place."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()
