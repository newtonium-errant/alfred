"""c14n-v1 + chain-hash golden vectors (§4). A change to any assertion here is a
store-protocol bump, never a patch — the verifier's rules are frozen from day one.
"""
from __future__ import annotations

from alfred.evstore.chain import (
    CANONICALIZATION,
    GENESIS_PREV,
    canonical_json,
    entry_hash,
    recompute_entry_sha,
    sha256_hex,
)


def test_c14n_protocol_id_frozen():
    assert CANONICALIZATION == "c14n-v1"
    assert GENESIS_PREV == "0" * 64


def test_c14n_golden_vectors():
    # sorted keys, no whitespace, ASCII-escaped, list order preserved.
    assert canonical_json({"b": 1, "a": 2, "c": [3, 2, 1]}) == '{"a":2,"b":1,"c":[3,2,1]}'
    # unicode is \uXXXX-escaped (locale-independent byte stream); bool→true, None→null.
    assert canonical_json({"z": True, "y": None, "x": "é"}) == '{"x":"\\u00e9","y":null,"z":true}'


def test_entry_hash_golden_vector():
    entry = {
        "v": 1, "seq": 1, "ts": "2026-01-01T00:00:00+00:00", "stream": "clinical",
        "family": "meta", "kind": "stream.genesis", "subject_id": "", "actor": "",
        "actor_kind": "system",
        "payload": {"store_protocol": 1, "canonicalization": "c14n-v1",
                    "predecessor_file": "", "predecessor_sha256": ""},
        "prev": GENESIS_PREV,
    }
    assert entry_hash(GENESIS_PREV, entry) == (
        "425d213849cdc356cb39b59bca5c448eac7ef70d09880481b430b2597e4e8b2b"
    )


def test_entry_hash_key_order_independent():
    # sort_keys makes serialization order irrelevant — a reader that reconstructs the dict in
    # any order recomputes the same hash.
    a = {"seq": 1, "prev": GENESIS_PREV, "kind": "x"}
    b = {"kind": "x", "prev": GENESIS_PREV, "seq": 1}
    assert entry_hash(GENESIS_PREV, a) == entry_hash(GENESIS_PREV, b)


def test_recompute_strips_entry_sha():
    entry = {"seq": 1, "prev": GENESIS_PREV, "kind": "x"}
    sha = entry_hash(GENESIS_PREV, entry)
    full = dict(entry, entry_sha=sha)
    assert recompute_entry_sha(full) == sha


def test_prev_delimiter_prevents_collision():
    # the "\n" between prev and body means a crafted body can't impersonate prev||body.
    assert entry_hash("aa", {"k": "bb"}) != entry_hash("aabb", {"k": ""})


def test_sha256_hex_golden():
    assert sha256_hex("hello") == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )
    assert sha256_hex(b"hello") == sha256_hex("hello")
