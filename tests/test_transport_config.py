"""Smoke tests for ``alfred.transport.config``.

Mirrors ``tests/test_instructor_config.py``: defaults round-trip,
overrides apply, ``${VAR}`` substitution fires before dataclass
construction, and the per-peer ``auth.tokens`` schema round-trips so
Stage 3.5 can extend in place.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from alfred.transport.config import (
    AuthTokenEntry,
    TransportConfig,
    load_from_unified,
)


def test_load_from_empty_unified_returns_defaults() -> None:
    """An empty unified config produces a fully-defaulted TransportConfig."""
    cfg = load_from_unified({})
    assert isinstance(cfg, TransportConfig)

    # Server defaults — localhost fixed port per ratified recommendation 1.
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.port == 8891

    # Scheduler knobs match ratified recs 7 + 8.
    assert cfg.scheduler.poll_interval_seconds == 30
    assert cfg.scheduler.stale_reminder_max_minutes == 180

    # Auth dict starts empty — deliberate: a missing config is a
    # mis-deploy and the server's auth middleware fails closed.
    assert cfg.auth.tokens == {}

    # State defaults.
    assert cfg.state.path == "./data/transport_state.json"
    assert cfg.state.dead_letter_max_age_days == 30


def test_load_from_unified_applies_section_overrides() -> None:
    """Values under ``transport:`` override dataclass defaults."""
    raw = {
        "transport": {
            "server": {"host": "0.0.0.0", "port": 9999},
            "scheduler": {
                "poll_interval_seconds": 15,
                "stale_reminder_max_minutes": 60,
            },
            "auth": {
                "tokens": {
                    "local": {
                        "token": "DUMMY_TRANSPORT_TEST_TOKEN",
                        "allowed_clients": ["scheduler", "brief"],
                    },
                    "kal-le": {
                        "token": "DUMMY_KAL_LE_TEST_TOKEN",
                        "allowed_clients": ["kal-le"],
                    },
                },
            },
            "state": {
                "path": "./custom/transport.json",
                "dead_letter_max_age_days": 7,
            },
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.server.host == "0.0.0.0"
    assert cfg.server.port == 9999
    assert cfg.scheduler.poll_interval_seconds == 15
    assert cfg.scheduler.stale_reminder_max_minutes == 60

    # Per-peer tokens dict — Stage 3.5 pre-commit: more than one entry
    # populates the dict without any schema rewrite.
    assert set(cfg.auth.tokens.keys()) == {"local", "kal-le"}
    local = cfg.auth.tokens["local"]
    assert isinstance(local, AuthTokenEntry)
    assert local.token == "DUMMY_TRANSPORT_TEST_TOKEN"
    assert local.allowed_clients == ["scheduler", "brief"]

    kal = cfg.auth.tokens["kal-le"]
    assert kal.token == "DUMMY_KAL_LE_TEST_TOKEN"
    assert kal.allowed_clients == ["kal-le"]

    assert cfg.state.path == "./custom/transport.json"
    assert cfg.state.dead_letter_max_age_days == 7


def test_load_from_unified_substitutes_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``${VAR}`` placeholders resolve against the process environment."""
    monkeypatch.setenv("TEST_TRANSPORT_TOKEN", "DUMMY_TRANSPORT_TEST_TOKEN")
    raw = {
        "transport": {
            "auth": {
                "tokens": {
                    "local": {
                        "token": "${TEST_TRANSPORT_TOKEN}",
                        "allowed_clients": ["scheduler"],
                    },
                },
            },
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.auth.tokens["local"].token == "DUMMY_TRANSPORT_TEST_TOKEN"


def test_load_from_unified_leaves_placeholder_when_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing env vars leave the literal placeholder intact.

    Matches every other tool's config policy — downstream callers
    (server auth middleware) treat the placeholder as "unset".
    """
    monkeypatch.delenv("ABSENT_TRANSPORT_TOKEN", raising=False)
    raw = {
        "transport": {
            "auth": {
                "tokens": {
                    "local": {
                        "token": "${ABSENT_TRANSPORT_TOKEN}",
                        "allowed_clients": ["scheduler"],
                    },
                },
            },
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.auth.tokens["local"].token == "${ABSENT_TRANSPORT_TOKEN}"


def test_load_from_unified_ignores_unknown_scalar_keys() -> None:
    """Unknown keys under nested sections are dropped, not raised.

    Keeps forward-compat room for Stage 3.5 additions.
    """
    raw = {
        "transport": {
            "server": {"host": "127.0.0.1", "future_field": 42},
            "auth": {
                "tokens": {
                    "local": {
                        "token": "DUMMY_TRANSPORT_TEST_TOKEN",
                        "allowed_clients": ["local"],
                        "future_permission_list": ["read"],
                    },
                },
            },
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.server.host == "127.0.0.1"
    assert cfg.auth.tokens["local"].token == "DUMMY_TRANSPORT_TEST_TOKEN"


def test_example_config_transport_section_loads() -> None:
    """The ``transport:`` section in the example config loads cleanly.

    Catches a common break: the example drifts from the dataclass
    shape and every fresh install loads a broken config.
    """
    import yaml
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    example = repo_root / "config.yaml.example"
    raw = yaml.safe_load(example.read_text(encoding="utf-8"))
    assert "transport" in raw, "config.yaml.example must ship a transport section"
    cfg = load_from_unified(raw)
    assert cfg.server.port == 8891
    assert "local" in cfg.auth.tokens
    assert cfg.auth.tokens["local"].allowed_clients


# ---------------------------------------------------------------------------
# P1 (2026-06-09) — PeerFieldRules.query back-compat + parse
# ---------------------------------------------------------------------------


def test_existing_shape_peer_permissions_loads_with_query_none() -> None:
    """BACK-COMPAT GUARD: a field-only peer_permissions entry (every
    existing config) loads with ``query=None`` — filtered queries stay
    denied, the by-name path is unchanged."""
    raw = {
        "transport": {
            "canonical": {
                "owner": True,
                "peer_permissions": {
                    "kal-le": {
                        "person": {"fields": ["name", "email"]},
                    },
                },
            },
        },
    }
    cfg = load_from_unified(raw)
    person_rules = cfg.canonical.peer_permissions["kal-le"]["person"]
    assert person_rules.fields == ["name", "email"]
    # The crux: no query block in YAML → query is None (filtered denied).
    assert person_rules.query is None


def test_query_block_parses_filter_dims_sort_and_limits() -> None:
    """A ``query`` sub-block parses into PeerQueryRules with the dims/ops."""
    raw = {
        "transport": {
            "canonical": {
                "owner": True,
                "peer_permissions": {
                    "hypatia": {
                        "event": {
                            "fields": ["title", "date", "participants"],
                            "query": {
                                "filter_dims": {
                                    "participants": {"op": ["eq", "contains"]},
                                    "date": {"op": ["gte", "lte", "between"]},
                                },
                                "sort": ["date"],
                                "max_limit": 8,
                                "default_limit": 3,
                            },
                        },
                    },
                },
            },
        },
    }
    cfg = load_from_unified(raw)
    q = cfg.canonical.peer_permissions["hypatia"]["event"].query
    assert q is not None
    assert set(q.filter_dims.keys()) == {"participants", "date"}
    assert q.filter_dims["participants"].op == ["eq", "contains"]
    assert q.sort == ["date"]
    assert q.max_limit == 8
    assert q.default_limit == 3


def test_query_block_drops_unknown_operator() -> None:
    """An operator not in FILTER_OPERATORS is dropped at load (never granted)."""
    raw = {
        "transport": {
            "canonical": {
                "owner": True,
                "peer_permissions": {
                    "hypatia": {
                        "event": {
                            "fields": ["date"],
                            "query": {
                                "filter_dims": {
                                    "date": {"op": ["gte", "regex", "sql_inject"]},
                                },
                            },
                        },
                    },
                },
            },
        },
    }
    cfg = load_from_unified(raw)
    q = cfg.canonical.peer_permissions["hypatia"]["event"].query
    # Only the valid 'gte' survives; 'regex' / 'sql_inject' dropped.
    assert q.filter_dims["date"].op == ["gte"]


def test_query_block_clamps_max_limit_to_ceiling() -> None:
    """max_limit can't exceed FILTER_LIMIT_CEILING at load time."""
    from alfred.transport.config import FILTER_LIMIT_CEILING

    raw = {
        "transport": {
            "canonical": {
                "owner": True,
                "peer_permissions": {
                    "hypatia": {
                        "event": {
                            "fields": ["date"],
                            "query": {"max_limit": 99999},
                        },
                    },
                },
            },
        },
    }
    cfg = load_from_unified(raw)
    q = cfg.canonical.peer_permissions["hypatia"]["event"].query
    assert q.max_limit == FILTER_LIMIT_CEILING


# ---------------------------------------------------------------------------
# Ingest block (cross-instance verbatim ingest, 2026-06-29)
# ---------------------------------------------------------------------------


def test_ingest_defaults_disabled() -> None:
    """No ``ingest`` block → disabled, default 256 KiB cap, no type narrow."""
    from alfred.transport.config import DEFAULT_INGEST_MAX_BODY_CHARS, IngestConfig

    cfg = load_from_unified({})
    assert isinstance(cfg.ingest, IngestConfig)
    assert cfg.ingest.enabled is False
    assert cfg.ingest.max_body_chars == DEFAULT_INGEST_MAX_BODY_CHARS == 262144
    assert cfg.ingest.types == []


def test_web_mirror_of_ingest_body_limit_has_not_drifted() -> None:
    """The browser's ``MAX_INGEST_CHARS`` must equal this default exactly.

    #57: the ingest form refuses an over-limit upload CLIENT-side and names the
    limit in the refusal, so the operator never meets the ceiling as a relayed
    413. That promise holds only while the two constants agree — if this default
    moved down and the TypeScript mirror did not, the form would accept a body
    the box then bounces, which is precisely the surprise the client-side check
    exists to prevent. Nothing else ties the literal in ``web/lib/algernon/
    schemas.ts`` to this one, so this is the drift detector.

    A missing or unparseable file FAILS rather than skips: a pin that quietly
    stops running is how the mirror gets to drift unobserved.
    """
    from alfred.transport.config import DEFAULT_INGEST_MAX_BODY_CHARS

    schemas = Path(__file__).resolve().parents[1] / "web" / "lib" / "algernon" / "schemas.ts"
    assert schemas.is_file(), f"web ingest schema not found at {schemas}"
    match = re.search(
        r"export const MAX_INGEST_CHARS\s*=\s*(\d+)\s*;",
        schemas.read_text(encoding="utf-8"),
    )
    assert match is not None, f"MAX_INGEST_CHARS declaration not found in {schemas}"
    assert int(match.group(1)) == DEFAULT_INGEST_MAX_BODY_CHARS


def test_web_mirror_of_pdf_byte_limit_has_not_drifted() -> None:
    """The #57 sibling of the pin above, on the OTHER axis.

    Bytes, not characters — and they are genuinely separate limits, because a
    10 MiB bank statement is an ordinary file whose extracted text sits well
    under the character cap. The form refuses an oversize PDF client-side and
    names the figure; that promise holds only while the browser's
    ``MAX_INGEST_PDF_BYTES`` equals ``alfred.documents.pdf.MAX_PDF_BYTES``.

    The value is ALSO the Telegram attachment path's cap, deliberately: one
    number across both doors so an operator never has to work out which
    entrance has the smaller ceiling.

    Missing or unparseable FAILS rather than skips — a pin that quietly stops
    running is how a mirror drifts unobserved.
    """
    from alfred.documents.pdf import MAX_PDF_BYTES

    schemas = Path(__file__).resolve().parents[1] / "web" / "lib" / "algernon" / "schemas.ts"
    assert schemas.is_file(), f"web ingest schema not found at {schemas}"
    match = re.search(
        r"export const MAX_INGEST_PDF_BYTES\s*=\s*([0-9*\s]+?)\s*;",
        schemas.read_text(encoding="utf-8"),
    )
    assert match is not None, (
        f"MAX_INGEST_PDF_BYTES declaration not found in {schemas}"
    )
    # The TS side spells it ``10 * 1024 * 1024`` for legibility, so the pin
    # evaluates the product rather than demanding a bare literal — the point is
    # that the two NUMBERS agree, not that they are typed the same way.
    factors = [int(p) for p in match.group(1).split("*")]
    product = 1
    for f in factors:
        product *= f
    assert product == MAX_PDF_BYTES == 10 * 1024 * 1024


def test_the_transport_client_max_size_sits_above_the_pdf_cap() -> None:
    """Ordering pin (#57), and it is the whole reason the number was raised.

    aiohttp enforces ``client_max_size`` in its own layer and answers with a
    bare HTML 413 BEFORE any handler runs. If it sat at or below the route's
    byte cap, an oversize upload would meet aiohttp's wall instead of the
    route's JSON taxonomy — the operator would get a browser error page rather
    than a sentence naming which limit they hit, and the ILB work on those
    refusals would be dead code for the case it most matters.

    Base64 inflates by 4/3, so the ceiling must clear the byte cap by at least
    a third plus JSON envelope.
    """
    from alfred.documents.pdf import MAX_PDF_BYTES
    from alfred.transport.config import DEFAULT_TRANSPORT_CLIENT_MAX_BYTES

    inflated = MAX_PDF_BYTES * 4 // 3
    assert DEFAULT_TRANSPORT_CLIENT_MAX_BYTES > inflated, (
        "a base64'd max-size PDF must still REACH the handler"
    )
    # And it must be above aiohttp's own 1 MiB default, or the raise did nothing.
    assert DEFAULT_TRANSPORT_CLIENT_MAX_BYTES > 1024 * 1024


def test_the_built_app_actually_carries_the_raised_ceiling(tmp_path) -> None:
    """The threading pin. A constant that never reaches ``web.Application`` is
    the standing trap: the value is right, the pin above is green, and the box
    still bounces every PDF at 1 MiB. Asserted on the BUILT app."""
    from alfred.transport.config import DEFAULT_TRANSPORT_CLIENT_MAX_BYTES
    from alfred.transport.server import build_app
    from alfred.transport.state import TransportState

    state = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(TransportConfig(), state)
    assert app._client_max_size == DEFAULT_TRANSPORT_CLIENT_MAX_BYTES


def test_ingest_block_overrides_apply() -> None:
    raw = {
        "transport": {
            "ingest": {
                "enabled": True,
                "max_body_chars": 1000,
                "types": ["document", "note"],
            },
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.ingest.enabled is True
    assert cfg.ingest.max_body_chars == 1000
    assert cfg.ingest.types == ["document", "note"]


def test_ingest_max_body_chars_floored_at_one() -> None:
    """A zero/negative cap can't wedge the route into an always-413 state."""
    cfg = load_from_unified(
        {"transport": {"ingest": {"enabled": True, "max_body_chars": 0}}}
    )
    assert cfg.ingest.max_body_chars == 1
