"""Production wiring for the RRTS invoices-export route.

THE TRAP THIS FILE EXISTS FOR, and this time it did not stay hypothetical.
The receiver shipped with its kwargs wired into ``wire_transport_app`` and
into its own tests — and never into the daemon's call. Every pin was green,
23 of them, and the route was never mounted on a real instance: the box
answered **404 to a correctly authenticated peer**. The skip-log fired on
every startup at DEBUG, under the production level, so the one signal that
would have said so was invisible.

That is the standing hazard the builder doctrine names in as many words: a
gate parameter tested only by direct invocation is a feature accepted-then-
ignored in the field. ``test_routes_rrts.py`` calls ``register_rrts_routes``
itself — which proves the registrar works and proves nothing whatever about
whether anything CALLS it.

So these pins walk the real chain: the talker daemon's ``wire_transport_app``
call, ``wire_transport_app``'s registrar call, and the transport config that
supplies the flag. All three must agree or the route never mounts.

Modelled on ``test_jeeves_wiring.py``, deliberately — the same trap already
had a canonical shape in this repo, and a second spelling of it would be one
more thing to keep in sync.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
import structlog
from aiohttp import web

from alfred.common import rrts_export as rrts_export_module
from alfred.telegram import daemon as telegram_daemon
from alfred.transport import server as transport_server
from alfred.transport.config import (
    AuthConfig,
    AuthTokenEntry,
    RrtsExportConfig,
    ServerConfig,
    StateConfig,
    TransportConfig,
    load_from_unified,
)
from alfred.transport.routes_rrts import RRTS_EXPORT_PEER_NAME
from alfred.transport.server import build_app, wire_transport_app
from alfred.transport.state import TransportState

DUMMY_RRTS_EXPORT_TOKEN = "DUMMY_RRTS_EXPORT_TEST_TOKEN"
_HEADERS = {
    "Authorization": f"Bearer {DUMMY_RRTS_EXPORT_TOKEN}",
    "X-Alfred-Client": "rrts",
}


def _wire_call_kwargs() -> set[str]:
    """Keyword names the daemon passes to ``wire_transport_app``.

    An AST walk rather than a grep: the call spans ninety-odd lines and a
    substring search would match the explanatory comment as happily as the
    argument — which is exactly how a comment ABOUT threading a parameter
    can sit directly above a call that does not thread it.
    """
    tree = ast.parse(Path(telegram_daemon.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "wire_transport_app"
        ):
            return {kw.arg for kw in node.keywords if kw.arg}
    raise AssertionError("the daemon no longer calls wire_transport_app")


# --- the production call site ------------------------------------------------


@pytest.mark.parametrize(
    "kwarg", ["rrts_export_enabled", "rrts_export_config", "rrts_data_dir"],
)
def test_the_daemon_threads_every_rrts_parameter(kwarg: str) -> None:
    """The pin that would have caught the 404. Each parameter separately, so
    a failure names WHICH one went missing rather than "the wiring changed".
    """
    assert kwarg in _wire_call_kwargs(), (
        f"the talker daemon does not pass {kwarg!r} to wire_transport_app — "
        f"the route will not mount on a real instance however green the "
        f"route's own tests are"
    )


def test_wire_transport_app_accepts_exactly_those_parameters() -> None:
    """The other end of the same handshake: the daemon can only thread what
    the signature accepts, and a rename on either side breaks the chain
    silently."""
    params = inspect.signature(transport_server.wire_transport_app).parameters
    for kwarg in ("rrts_export_enabled", "rrts_export_config", "rrts_data_dir"):
        assert kwarg in params


def test_the_transport_config_exposes_the_flag_the_daemon_reads() -> None:
    """The third link. The daemon reads ``transport_config.rrts_export``; if
    the config block stopped loading, the daemon would thread a default and
    the route would go dark with nothing failing."""
    config = load_from_unified({
        "transport": {"rrts_export": {"enabled": True, "max_bytes": 4096}}
    })
    assert isinstance(config.rrts_export, RrtsExportConfig)
    assert config.rrts_export.enabled is True
    assert config.rrts_export.max_bytes == 4096


# --- end to end: does the route actually MOUNT ---------------------------------


def _config() -> TransportConfig:
    return TransportConfig(
        server=ServerConfig(),
        auth=AuthConfig(
            tokens={
                RRTS_EXPORT_PEER_NAME: AuthTokenEntry(
                    token=DUMMY_RRTS_EXPORT_TOKEN, allowed_clients=["rrts"],
                ),
            }
        ),
        state=StateConfig(),
    )


async def test_enabled_through_wire_transport_app_the_route_answers(
    aiohttp_client, tmp_path,
) -> None:
    """THE pin the doctrine asks for: drive the production composition, not
    the registrar, and assert the route is REACHABLE. A per-layer pin cannot
    catch this class — that is the whole point of the paragraph."""
    state = TransportState.create(tmp_path / "state.json")
    app = build_app(_config(), state)
    wire_transport_app(
        app,
        _config(),
        instance_name="VERA",
        rrts_export_enabled=True,
        rrts_export_config=RrtsExportConfig(enabled=True, max_bytes=8192),
        rrts_data_dir=str(tmp_path / "data"),
    )
    client = await aiohttp_client(app)
    resp = await client.post(
        "/rrts/export",
        json={"exported_at": "2026-08-13T05:30:00Z", "invoices": []},
        headers=_HEADERS,
    )
    assert resp.status == 200, (
        "the route did not mount through the production wiring path — this "
        "is the 404 the live box returned"
    )
    body = await resp.json()
    assert body["ok"] is True
    assert body["exported_at"] == "2026-08-13T05:30:00Z"

    # And it landed under the INSTANCE data dir, not a guessed path.
    landed = tmp_path / "data" / "rrts-export" / "invoices.json"
    assert landed.is_file()


async def test_disabled_through_wire_transport_app_the_route_is_absent(
    aiohttp_client, tmp_path,
) -> None:
    """The twin. Opt-in inertness has to be real: an instance that does not
    configure the receiver must 404, and must SAY it skipped."""
    state = TransportState.create(tmp_path / "state.json")
    app = build_app(_config(), state)
    with structlog.testing.capture_logs() as captured:
        wire_transport_app(app, _config(), instance_name="Salem")
    client = await aiohttp_client(app)
    resp = await client.post(
        "/rrts/export",
        json={"exported_at": "2026-08-13T05:30:00Z"},
        headers=_HEADERS,
    )
    assert resp.status == 404

    skips = [
        c for c in captured
        if c.get("event") == "transport.wire_transport_app.rrts_export_skipped"
    ]
    assert len(skips) == 1


def test_the_skip_log_is_emitted_at_INFO() -> None:
    """The level is part of the contract, and this is why.

    The skip fired on every production startup while the route was
    unmounted — at DEBUG, under the level production runs at, so the one
    signal that would have said "not wired" was invisible. A log that exists
    to distinguish not-wired from forgotten must be readable at the level
    anyone reads. It fires once per startup; there is no spam case.
    """
    app = web.Application()
    with structlog.testing.capture_logs() as captured:
        wire_transport_app(app, _config(), instance_name="Salem")
    skips = [
        c for c in captured
        if c.get("event") == "transport.wire_transport_app.rrts_export_skipped"
    ]
    assert len(skips) == 1
    assert skips[0]["log_level"] == "info", (
        "the skip-log must be INFO — at DEBUG it is invisible in production, "
        "which is how an unmounted route went unnoticed until a peer got a 404"
    )


# --- item 0: the derivation has ONE home -----------------------------------------
#
# The reviewer's WARN from the receiver's gate: the directory segment was an
# inline f-string in the wiring, and one resolution existed only because one
# END existed. P2 adds the second end — the reader — so the segment is lifted
# before any reader code can spell it. A second spelling is how a writer and a
# reader land on different files while both look correct, and the failure is
# silent and total: the snapshot arrives where nothing looks for it.
#
# THE HOME MOVED ONCE MORE when the reader was actually built, and the reason
# is worth keeping. The first lift put the segment in ``routes_rrts`` — correct
# while the receiver was the only end. But ``routes_rrts`` imports aiohttp,
# which is the OPTIONAL ``voice`` extra, and the reader is the reconciler,
# which is base-install. A base-install reader importing the writer's module
# to learn its own input path is a crash; spelling the segment itself is the
# duplication this pin exists to forbid. So the fact moved to a
# dependency-free module both tiers can import, and the writer re-exports it
# for its existing callers. One END existed, so one resolution existed — the
# same lesson, one layer out.


def test_the_segment_is_spelled_in_exactly_one_place() -> None:
    """Source-level, because behaviour cannot see a duplicate that agrees.

    Two copies of ``rrts-export`` would pass every functional test on the
    day they were written and diverge later — which is precisely why this
    asserts on the source rather than on a resolved path.
    """
    from alfred.common import rrts_export
    from alfred.reconcile import config as reconcile_config
    from alfred.transport import routes_rrts

    # BARE substring, not the quoted form. The first version of this pin
    # looked for `"rrts-export"` with its quotes and scored ZERO against a
    # mutation that re-spelled the segment inside an f-string — where the
    # token never appears quoted. That is misaim rather than vacuity: the
    # pin fired correctly at a shape the duplication does not take.
    #
    # The property is "outside its home, ZERO occurrences" rather than "one
    # occurrence in total": the home file names it in the constant AND in
    # the docstrings that explain it, and counting those would make the pin
    # fail on prose.
    home = Path(rrts_export.__file__)
    # BOTH ends are now covered, which is the point of the move: the writer
    # (routes_rrts) and the reader (reconcile.config) are the two files most
    # able to grow a private copy, and neither may spell it.
    others = [
        Path(routes_rrts.__file__),
        Path(reconcile_config.__file__),
        Path(transport_server.__file__),
        Path(telegram_daemon.__file__),
    ]

    assert "rrts-export" in home.read_text(encoding="utf-8"), (
        "the segment vanished from its home — the constant was renamed or "
        "removed and this pin would now pass trivially"
    )

    strays: list[str] = []
    for f in others:
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if "rrts-export" in line:
                strays.append(f"{f.name}:{i}: {line.strip()[:70]}")
    assert not strays, (
        "the directory segment is spelled outside alfred/common/rrts_export.py, "
        "which is how a writer and a reader land on different files while both "
        f"look correct:\n  " + "\n  ".join(strays)
    )


def test_the_reader_reaches_the_derivation_without_the_optional_extra() -> None:
    """The reason the home moved: the reader is base-install, aiohttp is not.

    ``alfred.common.rrts_export`` must import with nothing but the standard
    library behind it. If it ever grows a transport import, a base install
    loses the ability to find its own invoices export — and the symptom is
    the reconcile CLI crashing on startup, not a clear missing-extra message.
    """
    import ast as _ast

    source = Path(rrts_export_module.__file__).read_text(encoding="utf-8")
    tree = _ast.parse(source)
    imported: list[str] = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            imported.extend(a.name for a in node.names)
        elif isinstance(node, _ast.ImportFrom) and node.module:
            imported.append(node.module)

    forbidden = [
        m for m in imported
        if m.split(".")[0] in {"aiohttp", "anthropic", "telegram"}
        or m.startswith("alfred.transport")
    ]
    assert not forbidden, (
        f"the shared landing-path module imports {forbidden}, which puts an "
        f"optional extra back between the reader and its own input path"
    )


def test_export_dir_for_is_the_derivation() -> None:
    from alfred.transport.routes_rrts import EXPORT_DIR_NAME, export_dir_for

    assert export_dir_for("/srv/vera/data") == f"/srv/vera/data/{EXPORT_DIR_NAME}"
    # Relative anchors keep their exact string — a pathlib join would
    # normalise the leading ./ away and move an existing landing path.
    assert export_dir_for("./data") == f"./data/{EXPORT_DIR_NAME}"
    assert export_dir_for("/srv/data/") == f"/srv/data/{EXPORT_DIR_NAME}"


def test_an_empty_data_dir_yields_no_path_rather_than_a_guess() -> None:
    """The caller's cue to refuse. Guessing the process cwd is the defect
    this family of helpers exists to prevent."""
    from alfred.transport.routes_rrts import export_dir_for, export_path_for

    assert export_dir_for("") == ""
    assert export_dir_for("   ") == ""
    assert export_path_for("") == ""


def test_export_path_for_composes_both_segments() -> None:
    from alfred.transport.routes_rrts import (
        EXPORT_DIR_NAME,
        EXPORT_FILENAME,
        export_path_for,
    )

    assert export_path_for("/srv/vera/data") == (
        f"/srv/vera/data/{EXPORT_DIR_NAME}/{EXPORT_FILENAME}"
    )


async def test_the_wiring_resolves_through_the_helper(
    aiohttp_client, tmp_path,
) -> None:
    """End to end: the path the RECEIVER writes is the path the helper
    names. This is the half a source-level pin cannot prove."""
    from alfred.transport.routes_rrts import export_path_for

    state = TransportState.create(tmp_path / "state.json")
    app = build_app(_config(), state)
    data_dir = str(tmp_path / "data")
    wire_transport_app(
        app, _config(), instance_name="VERA",
        rrts_export_enabled=True,
        rrts_export_config=RrtsExportConfig(enabled=True, max_bytes=8192),
        rrts_data_dir=data_dir,
    )
    client = await aiohttp_client(app)
    resp = await client.post(
        "/rrts/export",
        json={"exported_at": "2026-08-13T22:40:00.116Z", "invoices": []},
        headers=_HEADERS,
    )
    assert resp.status == 200
    assert Path(export_path_for(data_dir)).is_file()
