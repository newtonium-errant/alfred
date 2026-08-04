"""Shared assertion — a gate parameter must be keyword-only with NO default.

**Why this module exists rather than a bare ``pytest.raises(TypeError)``.**

builder.md's optional-gate rule says a defaulted gate parameter is a standing
trap: tests thread it, production doesn't, every pin stays green, and the
feature is accepted-then-ignored in the field. The natural pin is "calling
without it raises TypeError". That pin is **hollow**, and arc #18 shipped it
that way before a reviewer measured it.

The failure: under the minimal regression shape — someone simply adds
``= None`` to the signature and changes nothing else — the call does NOT fail
at the boundary. It proceeds, and something *downstream* raises TypeError
incidentally:

    TypeError: argument should be a str or an os.PathLike object where
               __fspath__ returns a str, not 'NoneType'

That is ``Path(None)`` inside ``resolve_in_vault``, not Python refusing the
call. ``pytest.raises(TypeError)`` cannot tell the two apart, so the pin passes
against exactly the build it exists to forbid (measured: 31/31 green under the
signature-only mutation).

Note the asymmetry that makes this subtle: the sibling pin on
``resolve_in_vault``'s own ``writer`` parameter DOES bind, because no incidental
TypeError is available there — a defaulted ``writer`` just produces a working
call. So "does a bare raises() pin bind?" depends on what the parameter is
LATER used for, which is not a property you can eyeball. Hence: assert the
signature structurally, and bind the message when asserting behaviour.

Use BOTH halves. The structural half is the real guarantee (it cannot be
satisfied incidentally); the behavioural half documents what a caller sees.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

#: Matches CPython's own missing-keyword-only-argument message, e.g.
#: "mark_routine_item_done() missing 1 required keyword-only argument:
#: 'vault_path'". Pair with ``pytest.raises(TypeError, match=...)`` so an
#: INCIDENTAL downstream TypeError cannot satisfy the pin.
MISSING_KWARG_RE = r"missing \d+ required keyword-only argument"


def assert_required_keyword_only(fn: Callable[..., Any], name: str) -> None:
    """Assert ``name`` is a KEYWORD_ONLY parameter of ``fn`` with no default.

    Structural, so it cannot be satisfied by an incidental downstream raise.
    This is the binding half of the optional-gate pin; ``MISSING_KWARG_RE``
    covers the behavioural half.
    """
    sig = inspect.signature(fn)
    param = sig.parameters.get(name)
    assert param is not None, (
        f"{fn.__name__}() has no {name!r} parameter at all — the gate is gone"
    )
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
        f"{fn.__name__}()'s {name!r} must be KEYWORD-ONLY so a positional "
        f"caller cannot supply it by accident; got {param.kind.description}"
    )
    assert param.default is inspect.Parameter.empty, (
        f"{fn.__name__}()'s {name!r} has default {param.default!r}. A defaulted "
        f"gate parameter is the optional-gate trap: production stops threading "
        f"it, the gate silently no-ops, and every other pin stays green."
    )


__all__ = ["MISSING_KWARG_RE", "assert_required_keyword_only"]
