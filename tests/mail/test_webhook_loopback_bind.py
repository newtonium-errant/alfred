"""Regression pin: the mail webhook receiver binds loopback, not 0.0.0.0.

B5 (SovServ DO-NOW): ``run_webhook`` and the ``alfred mail webhook --host``
CLI arg default to ``127.0.0.1`` so the receiver does not listen on every
interface. The Cloudflare tunnel is the single ingress and proxies to
``localhost:5005``; the orchestrator's ``_run_mail_webhook`` does not pass a
``host`` so the function default governs the daemon's bind. The bearer-token
check is the auth layer and is independent of the bind host.

These pins run UNCONDITIONALLY (no optional-dep skip): both surfaces import
from the base install. Per ``feedback_regression_pin_unconditional.md`` a pin
for a security-relevant default must never hide behind ``importorskip``.
"""

from __future__ import annotations

import inspect


def test_run_webhook_default_host_is_loopback() -> None:
    """The ``run_webhook`` function defaults ``host`` to loopback.

    The orchestrator daemon entry point (``_run_mail_webhook``) calls
    ``run_webhook`` WITHOUT a ``host`` kwarg, so this default is the value the
    long-running daemon actually binds. If a future edit reverts it to
    ``0.0.0.0`` the receiver silently re-exposes on all interfaces.
    """
    from alfred.mail.webhook import run_webhook

    sig = inspect.signature(run_webhook)
    assert sig.parameters["host"].default == "127.0.0.1"


def test_cli_mail_webhook_host_default_is_loopback() -> None:
    """The ``alfred mail webhook`` parser defaults ``--host`` to loopback.

    Driven through the real ``build_parser`` so a rename or default change in
    the arg definition is caught, not just a literal-string assertion.
    """
    from alfred.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["mail", "webhook"])
    assert args.host == "127.0.0.1"
