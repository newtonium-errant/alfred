"""Config-compat pins for the RETIRED batch STT shadow-capture (R2, 2026-08-20).

``alfred/telegram/stt_shadow.py`` is DELETED — the operator ruled "agreed
retire" and the streaming twin (``web/voice_stt_shadow.py``) carries the
capability forward. These three pins moved here from the deleted
``tests/telegram/test_stt_shadow.py`` because they do NOT test the retired
module: they test the ``telegram.stt.shadow_capture`` CONFIG BLOCK, which is
deliberately KEPT.

WHY THE BLOCK IS KEPT (the premise these pins defend) — ``telegram/config.py``
builds ``STTConfig`` through ``_build``, which has NO unknown-key filter
(``kwargs[key] = value`` then ``cls(**kwargs)``). So a live YAML that still
carries ``telegram: stt: shadow_capture:`` raises ``TypeError`` at load the
moment the field stops existing, and the box carries the block on all four
instances. The field is a config-compat shell, exactly like
``TodayCommandConfig`` and ``InstanceConfig.aliases`` kept by T5
(feca775f / 656d1a87).

What that failure looks like, DRIVEN against the field-removed mutant with an
unmutated control rather than reasoned about:
  * LOUD, and this is the dominant half — ``load_from_unified`` raises, so the
    talker DAEMON fails to start, as do the talker CLI paths.
  * QUIET, and misattributing — ``skill_audit``'s ``except TypeError`` around
    ``load_talker_config`` exists for the missing-``instance.name`` case but
    catches this one too, reporting ``instance_missing`` ("telegram.instance
    config incomplete") when the instance config is perfectly fine.
    ``_check_skill_capability_audit`` then returns SKIP, a QUIET health
    status, so no attention card is raised. Control OK -> mutant SKIP.
  * NOT the health-aggregator swallow. An earlier draft of this module
    blamed ``_load_tool_checks`` dropping the talker probe; that was FALSE —
    under the mutation ``alfred.telegram.health`` still imports and ``talker``
    still registers, since removing a dataclass field is not an ImportError.
    Corrected here rather than only in commit history, because this docstring
    is where the next reader meets the claim.

``test_unknown_stt_key_still_raises`` is the POSITIVE CONTROL for that premise:
it proves the tolerance is specific to the KEPT FIELD and not a blanket
loader permissiveness. Without it the other pins would read identically
against a ``_build`` that silently swallowed every unknown key — i.e. they
would pass while proving nothing about why the field must stay.
"""
from __future__ import annotations

import pytest

from alfred.telegram.config import (
    SttShadowCaptureConfig,
    load_from_unified,
)


def _cfg(stt: dict | None = None) -> dict:
    """Minimal talker config. ``instance.name`` is REQUIRED — the fail-loud
    guarantee from feedback_hardcoding_and_alfred_naming; omitting it hits the
    ``_build`` empty-dict-into-required-field trap, not the code under test.
    """
    tool: dict = {"instance": {"name": "Salem"}}
    if stt is not None:
        tool["stt"] = stt
    return {"telegram": tool}


# ---------------------------------------------------------------------------
# The kept block still loads (the back-compat property itself)
# ---------------------------------------------------------------------------


def test_config_loads_shadow_capture_block():
    cfg = load_from_unified(_cfg({"shadow_capture": {"enabled": True, "dir": "/data/x"}}))
    assert isinstance(cfg.stt.shadow_capture, SttShadowCaptureConfig)
    assert cfg.stt.shadow_capture.enabled is True
    assert cfg.stt.shadow_capture.dir == "/data/x"


def test_config_omitted_shadow_capture_defaults_off():
    cfg = load_from_unified(_cfg({"api_key": "k", "model": "whisper-large-v3"}))
    assert cfg.stt.shadow_capture.enabled is False
    assert cfg.stt.shadow_capture.dir == "./data/stt_corpus"


def test_config_no_stt_block_defaults_off():
    cfg = load_from_unified(_cfg())
    assert cfg.stt.shadow_capture.enabled is False
    assert cfg.stt.shadow_capture.dir == "./data/stt_corpus"


# ---------------------------------------------------------------------------
# POSITIVE CONTROL — the tolerance is field-specific, not loader-wide
# ---------------------------------------------------------------------------


def test_unknown_stt_key_still_raises():
    """An stt key with no matching field DOES raise.

    This is what makes the three pins above non-vacuous. ``_build`` passes
    every key straight to the constructor, so tolerating ``shadow_capture``
    is a property of the RETAINED DATACLASS FIELD and of nothing else. If
    someone "cleans up" the retired field, this control keeps passing while
    the three pins above go red — which is the correct signal, because the
    breakage is real: a deployed YAML carrying the block stops loading.
    """
    with pytest.raises(TypeError, match="shadow_capture_retired_probe"):
        load_from_unified(_cfg({"shadow_capture_retired_probe": {"enabled": True}}))


def test_retired_capture_module_is_gone():
    """The module the block once fed is DELETED — pin its absence.

    Positive control on the LIVE path: the sibling module that DID survive
    (``stt_backends``, which the streaming twin still imports) must import
    cleanly in the same test. Without that second leg this pin would pass
    identically if ``alfred.telegram`` were unimportable altogether.
    """
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("alfred.telegram.stt_shadow")

    # The live neighbour still resolves — proves the failure above is the
    # deletion, not a broken package.
    assert importlib.import_module("alfred.telegram.stt_backends") is not None
