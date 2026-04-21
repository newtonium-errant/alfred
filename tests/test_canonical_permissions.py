"""Tests for the c2 canonical-permissions + audit machinery.

Pure-function tests — no HTTP, no state, just the config loader, the
field-filter, and the audit appender. The handlers that compose these
land in c3.
"""

from __future__ import annotations

from pathlib import Path

from alfred.transport.canonical import apply_field_permissions
from alfred.transport.canonical_audit import append_audit, read_audit
from alfred.transport.config import (
    CanonicalConfig,
    PeerEntry,
    PeerFieldRules,
    load_from_unified,
)


# ---------------------------------------------------------------------------
# apply_field_permissions
# ---------------------------------------------------------------------------


SAMPLE_FM = {
    "name": "Andrew Newton",
    "email": "andrew@example.com",
    "phone": "+15551234567",
    "timezone": "America/Halifax",
    "preferences": {"coding": "python", "writing": "voice"},
}


def test_default_deny_empty_perms():
    """Empty perms dict → nothing granted, everything denied."""
    filtered, granted, denied = apply_field_permissions(
        peer="kal-le",
        record_type="person",
        frontmatter=SAMPLE_FM,
        perms={},
    )
    assert filtered == {}
    assert granted == []
    # All top-level keys should be denied.
    assert set(denied) == set(SAMPLE_FM.keys())


def test_default_deny_unknown_peer():
    """Peer not in perms dict → default deny."""
    perms = {"stay-c": {"person": PeerFieldRules(fields=["name"])}}
    filtered, granted, denied = apply_field_permissions(
        peer="kal-le",
        record_type="person",
        frontmatter=SAMPLE_FM,
        perms=perms,
    )
    assert filtered == {}
    assert granted == []


def test_default_deny_unknown_type():
    """Peer exists but not for this record type → deny."""
    perms = {"kal-le": {"org": PeerFieldRules(fields=["name"])}}
    filtered, _, _ = apply_field_permissions(
        peer="kal-le",
        record_type="person",
        frontmatter=SAMPLE_FM,
        perms=perms,
    )
    assert filtered == {}


def test_flat_field_grant():
    """Explicit field list is respected."""
    perms = {
        "kal-le": {
            "person": PeerFieldRules(fields=["name", "email"]),
        },
    }
    filtered, granted, denied = apply_field_permissions(
        peer="kal-le",
        record_type="person",
        frontmatter=SAMPLE_FM,
        perms=perms,
    )
    assert filtered == {"name": "Andrew Newton", "email": "andrew@example.com"}
    assert set(granted) == {"name", "email"}
    assert "phone" in denied
    assert "timezone" in denied


def test_dotted_field_access():
    """``preferences.coding`` reads the nested key and writes it nested."""
    perms = {
        "kal-le": {
            "person": PeerFieldRules(
                fields=["name", "preferences.coding"],
            ),
        },
    }
    filtered, granted, _ = apply_field_permissions(
        peer="kal-le",
        record_type="person",
        frontmatter=SAMPLE_FM,
        perms=perms,
    )
    assert filtered == {
        "name": "Andrew Newton",
        "preferences": {"coding": "python"},
    }
    # Writing-side: grant is reported with the dotted name.
    assert "preferences.coding" in granted
    # preferences.writing is NOT exposed even though preferences top-level
    # key "exists" in the source.
    assert "writing" not in filtered["preferences"]


def test_dotted_field_absent_in_source():
    """Named dotted field missing from source is silently dropped."""
    perms = {
        "kal-le": {
            "person": PeerFieldRules(fields=["name", "preferences.missing"]),
        },
    }
    filtered, granted, _ = apply_field_permissions(
        peer="kal-le",
        record_type="person",
        frontmatter=SAMPLE_FM,
        perms=perms,
    )
    # Only name made it through.
    assert filtered == {"name": "Andrew Newton"}
    assert granted == ["name"]


def test_empty_fields_list_denies_all():
    """``fields: []`` in the peer entry = deny everything."""
    perms = {"kal-le": {"person": PeerFieldRules(fields=[])}}
    filtered, granted, _ = apply_field_permissions(
        peer="kal-le",
        record_type="person",
        frontmatter=SAMPLE_FM,
        perms=perms,
    )
    assert filtered == {}
    assert granted == []


def test_stayc_phi_firewall_scenario():
    """Realistic: STAY-C can only read name, not phone/email/etc."""
    perms = {
        "stay-c": {
            "person": PeerFieldRules(fields=["name"]),
        },
    }
    filtered, granted, denied = apply_field_permissions(
        peer="stay-c",
        record_type="person",
        frontmatter=SAMPLE_FM,
        perms=perms,
    )
    assert filtered == {"name": "Andrew Newton"}
    assert granted == ["name"]
    # Everything else is denied (PHI firewall).
    assert set(denied) >= {"email", "phone", "timezone", "preferences"}


def test_none_frontmatter_returns_empty():
    filtered, granted, denied = apply_field_permissions(
        peer="kal-le",
        record_type="person",
        frontmatter={},
        perms={"kal-le": {"person": PeerFieldRules(fields=["name"])}},
    )
    assert filtered == {}
    assert granted == []
    assert denied == []


# ---------------------------------------------------------------------------
# append_audit + read_audit
# ---------------------------------------------------------------------------


def test_audit_entry_shape(tmp_path: Path):
    log_path = tmp_path / "canonical_audit.jsonl"
    append_audit(
        log_path,
        peer="kal-le",
        record_type="person",
        name="Andrew Newton",
        requested=["name", "email"],
        granted=["name", "email"],
        denied=["phone"],
        correlation_id="abc123def456",
    )
    entries = read_audit(log_path)
    assert len(entries) == 1
    e = entries[0]
    assert e["peer"] == "kal-le"
    assert e["type"] == "person"
    assert e["name"] == "Andrew Newton"
    assert e["requested"] == ["name", "email"]
    assert e["granted"] == ["name", "email"]
    assert e["denied"] == ["phone"]
    assert e["correlation_id"] == "abc123def456"
    assert "ts" in e


def test_audit_appends_multiple_lines(tmp_path: Path):
    log_path = tmp_path / "canonical_audit.jsonl"
    for i in range(3):
        append_audit(
            log_path,
            peer="kal-le",
            record_type="person",
            name=f"Person {i}",
            requested=["name"],
            granted=["name"],
            denied=[],
            correlation_id=f"cid-{i:04d}",
        )
    entries = read_audit(log_path)
    assert len(entries) == 3
    assert [e["name"] for e in entries] == ["Person 0", "Person 1", "Person 2"]


def test_audit_denied_only_case(tmp_path: Path):
    """403 path — peer tried to read a field it isn't allowed."""
    log_path = tmp_path / "audit.jsonl"
    append_audit(
        log_path,
        peer="kal-le",
        record_type="person",
        name="Andrew Newton",
        requested=["phone"],
        granted=[],
        denied=["phone"],
        correlation_id="deny-example",
    )
    entries = read_audit(log_path)
    assert entries[0]["granted"] == []
    assert entries[0]["denied"] == ["phone"]


def test_audit_missing_dir_is_created(tmp_path: Path):
    """Parent directory doesn't exist yet — append_audit creates it."""
    log_path = tmp_path / "nested" / "deeper" / "audit.jsonl"
    append_audit(
        log_path,
        peer="kal-le",
        record_type="person",
        name="Test",
        requested=["name"],
        granted=["name"],
        denied=[],
    )
    assert log_path.exists()


# ---------------------------------------------------------------------------
# Config loader — CanonicalConfig + peers
# ---------------------------------------------------------------------------


def test_canonical_config_defaults():
    """No ``canonical`` section → all defaults (owner=False, empty perms)."""
    config = load_from_unified({"transport": {}})
    assert config.canonical.owner is False
    assert config.canonical.peer_permissions == {}
    assert config.canonical.audit_log_path == "./data/canonical_audit.jsonl"


def test_canonical_config_loads_peer_permissions():
    raw = {
        "transport": {
            "canonical": {
                "owner": True,
                "audit_log_path": "./data/audit.jsonl",
                "peer_permissions": {
                    "kal-le": {
                        "person": {
                            "fields": ["name", "email", "timezone"],
                            "bodies": False,
                        },
                    },
                    "stay-c": {
                        "person": {"fields": ["name"]},
                    },
                },
            },
        },
    }
    config = load_from_unified(raw)
    assert config.canonical.owner is True
    assert config.canonical.audit_log_path == "./data/audit.jsonl"
    assert "kal-le" in config.canonical.peer_permissions
    kal_rules = config.canonical.peer_permissions["kal-le"]["person"]
    assert kal_rules.fields == ["name", "email", "timezone"]
    assert kal_rules.bodies is False
    stayc_rules = config.canonical.peer_permissions["stay-c"]["person"]
    assert stayc_rules.fields == ["name"]


def test_peers_dict_loads():
    raw = {
        "transport": {
            "peers": {
                "kal-le": {
                    "base_url": "http://127.0.0.1:8892",
                    "token": "peer-token-abc",
                },
                "stay-c": {
                    "base_url": "http://127.0.0.1:8893",
                    "token": "peer-token-xyz",
                },
            },
        },
    }
    config = load_from_unified(raw)
    assert "kal-le" in config.peers
    assert config.peers["kal-le"].base_url == "http://127.0.0.1:8892"
    assert config.peers["kal-le"].token == "peer-token-abc"
    assert config.peers["stay-c"].base_url == "http://127.0.0.1:8893"


def test_peers_empty_by_default():
    """Salem-with-no-peers is a valid config shape."""
    config = load_from_unified({"transport": {}})
    assert config.peers == {}


def test_apply_perms_with_config_loaded_rules():
    """Sanity: config-loaded PeerFieldRules flow cleanly into apply_field_permissions."""
    raw = {
        "transport": {
            "canonical": {
                "peer_permissions": {
                    "kal-le": {
                        "person": {"fields": ["name", "email"]},
                    },
                },
            },
        },
    }
    config = load_from_unified(raw)
    filtered, _, _ = apply_field_permissions(
        peer="kal-le",
        record_type="person",
        frontmatter=SAMPLE_FM,
        perms=config.canonical.peer_permissions,
    )
    assert filtered == {
        "name": "Andrew Newton",
        "email": "andrew@example.com",
    }
