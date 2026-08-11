"""Drip-drain config surface (#44b, closing the D1 gap).

The #44 machinery takes its budgets as REQUIRED keyword arguments to
:func:`~alfred.drip.runner.run_increment`. That is correct for the runner — it
should not know where a number came from — but it left the ratified design's §4
config layer unbuilt, so the operator's ruled numbers (12/run, 60/week) lived
only in whatever a caller happened to pass. There were no callers. This module
is the layer that turns those arguments into operator-controlled settings.

## Hand-rolled on purpose — the ``_build`` collision footgun

Every other tool's ``config.py`` uses a recursive ``_build`` helper that
dispatches by KEY NAME through a module-global ``_DATACLASS_MAP``. Per
CLAUDE.md, a nested block whose sub-key collides with a mapped name (``state``,
``agent``, ``schedule``, ``openrouter``) silently constructs the WRONG
dataclass. A per-campaign block is exactly that shape, so this loader is
hand-rolled end to end — the ``pattern_miner`` precedent in
``distiller/config.py``. There is no ``_build`` here to collide with, which is
the point: the footgun is absent by construction rather than avoided by care.

## Deploy-inert

An instance with no ``drip:`` block loads a config with no campaigns, and every
consumer treats that as "the feature is off" — no state files, no CLI work, no
brief section. Adding this module to the tree changes no instance's behaviour
until someone writes the block. That property is pinned, not assumed.

## Per-instance, never a literal

Campaign paths (a staging directory, a frozen work-list) are per-instance and
have NO defaults — an empty value is a hard error at build time when the
campaign is enabled, rather than a plausible-looking Salem path that would
quietly do the wrong thing on KAL-LE. Per
``feedback_hardcoding_and_alfred_naming.md``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)

ENV_RE = re.compile(r"\$\{(\w+)\}")


class DripConfigError(ValueError):
    """A drip campaign cannot be built or scoped from its config.

    Loud on purpose. The alternative — skipping an unbuildable campaign — is a
    campaign that is configured, believed to be draining, and silently doing
    nothing, which is the failure this whole feature is shaped against.

    Lives HERE rather than in ``wiring`` (#66) so the layers that detect a
    config problem can raise it without an import cycle: ``wiring`` imports
    ``state``, so ``state`` could not import back from ``wiring``, which is why
    ``campaign_state_path``'s missing-instance refusal used to be a bare
    ``ValueError`` and escaped the CLI's ``except`` as a traceback. ``config``
    imports nothing from the package, so everything can reach it.

    Still subclasses ``ValueError``, so existing handlers that catch
    ``ValueError`` (``brief/daemon.py`` does) are unaffected. ``wiring``
    re-exports it, so every ``from .wiring import DripConfigError`` keeps
    working.
    """

#: Operator ruling 2026-08-04 (D1, halved from the first draft). These are the
#: DEFAULTS, not the values — an instance's YAML always wins. They live here so
#: that a config which omits them still gets the ruled numbers rather than a
#: runner-side accident.
DEFAULT_MAX_ITEMS_PER_RUN = 12
DEFAULT_MAX_ITEMS_PER_WEEK = 60

#: Retry/circuit-breaker defaults. Carried in config for the same reason as the
#: budgets: the runner requires them, so something must own them, and an
#: operator-visible file is the honest owner.
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MAX_FAILURES_PER_RUN = 5
DEFAULT_MAX_AWAITING_RUNS = 5

#: Campaign kinds this config layer knows how to build. Kept as a frozenset
#: rather than derived from ``campaigns.CAMPAIGN_KINDS`` so a config error is a
#: config-layer error with a config-layer message, and so importing config does
#: not drag in the campaign implementations.
KNOWN_CAMPAIGN_KINDS: frozenset[str] = frozenset({
    "gmail_backlog", "link001_repair", "batch_image",
})

#: Last-resort ``batch_image`` model, used ONLY when neither
#: ``drip.campaigns.<name>.model`` nor ``telegram.anthropic.model`` is set.
#: The normal path is inheritance — see :attr:`CampaignConfig.model`.
#:
#: WHY THE DEFAULT IS NOT OPUS ANY MORE. A batch is the one workload where the
#: model choice multiplies: the cost is per scan, so a thirty-scan batch pays
#: the difference thirty times over. The two vision tiers are not close:
#:
#:   high-resolution (opus-5)  — up to 4784 visual tokens per image
#:   standard        (sonnet)  — capped at 1568 visual tokens per image
#:
#: That is roughly 3x the visual tokens per scan before any price difference,
#: and it buys fidelity that the live reconciliation which motivated this
#: feature did not need — it ran fine on VERA's configured sonnet-4-6. So the
#: default follows the instance's OWN talker model, which is the model the
#: operator already chose for that instance's work, and an operator who wants
#: the high-resolution tier for a dense-scan batch sets ``model:`` explicitly.
#: Both directions stay a one-line config change; neither requires a deploy.
DEFAULT_BATCH_MODEL_FALLBACK = "claude-sonnet-4-6"

#: ``max_tokens`` is deliberately generous: on current models thinking is on by
#: default and ``max_tokens`` caps thinking PLUS response text together, so a
#: tight value truncates the extraction mid-answer.
DEFAULT_BATCH_MAX_TOKENS = 8192

#: Ceiling on the prior-results context carried into each per-scan call (#83).
#:
#: This is a COST LEVER disguised as a formatting knob. The carried context is
#: the batch's own accumulated results, and it used to be the WHOLE rendered
#: body — so scan N carried N-1 results, and the tokens paid across a batch
#: grew with the SQUARE of its size. On a sixty-scan batch that is the dominant
#: cost, and it is spent re-reading text the model is explicitly told not to
#: re-process.
#:
#: What the bound preserves is what the context is FOR: consistency of format
#: and terminology with the scans just processed, which the most recent rows
#: carry as well as all of them. What it drops is distant rows — and nothing is
#: lost by dropping them, because the ledger still holds every result and the
#: record still renders all of them.
DEFAULT_BATCH_CARRIED_CONTEXT_MAX_CHARS = 8000


def _substitute_env(value: Any) -> Any:
    """Recursively replace ``${VAR}`` placeholders. Same contract as every
    other tool's loader — ``load_from_unified`` receives the raw YAML dict and
    is responsible for its own substitution."""
    if isinstance(value, str):
        return ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value)
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(v) for v in value]
    return value


def _known_only(cls: type, data: dict) -> dict:
    """The load-time schema-tolerance filter (CLAUDE.md state contract).

    A block written by a newer version with extra keys loads on rollback; an
    older block loads forward. Applied per sub-block by hand because there is
    no recursive builder here to apply it for us.
    """
    return {k: v for k, v in data.items() if k in cls.__dataclass_fields__}


@dataclass
class CampaignConfig:
    """One campaign's operator-controlled settings.

    ``enabled`` defaults **False**: a campaign spends real credits (or performs
    irreversible vault edits), so it opts in explicitly. A configured-but-
    disabled campaign is not silent — the brief renders a "disabled, N left,
    not draining" line for it, which is why the disabled state is modelled here
    rather than expressed by omitting the block.
    """

    kind: str = ""
    enabled: bool = False

    max_items_per_run: int = DEFAULT_MAX_ITEMS_PER_RUN
    max_items_per_week: int = DEFAULT_MAX_ITEMS_PER_WEEK
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    max_failures_per_run: int = DEFAULT_MAX_FAILURES_PER_RUN
    max_awaiting_runs: int = DEFAULT_MAX_AWAITING_RUNS

    #: ``gmail_backlog``: the cohort staged OUTSIDE the watched inbox, and the
    #: watched inbox itself. Both required when that campaign is enabled.
    staging_dir: str = ""
    inbox_dir: str = ""

    #: ``link001_repair``: path to the FROZEN work-list, one
    #: ``<rel_path>::<target>::<annotate|remove>`` item per line.
    #:
    #: A file, not a live scan, and that is a correctness requirement rather
    #: than a convenience. D4a freezes each item's branch at list-BUILD because
    #: re-deriving it per run would let the same link annotate on Monday and
    #: delete on Tuesday depending on whether a ``learn/`` record happened to
    #: exist in between. A campaign whose behaviour depends on when it ran is
    #: exactly what the removal branch — which is unrecoverable data loss — must
    #: not be.
    worklist_path: str = ""

    # --- batch_image only (#83) ---
    #: Vision model for per-scan extraction. A cost lever; see the
    #: DEFAULT_BATCH_* rationale above.
    #:
    #: ``""`` means INHERIT ``telegram.anthropic.model`` — this instance's own
    #: configured model, which is the one the operator already chose for its
    #: work. Same convention as ``transport.peers.*.nl_broker.model``. Set it
    #: explicitly to override in either direction (e.g. the high-resolution
    #: tier for a batch of dense scans).
    model: str = ""
    max_tokens: int = DEFAULT_BATCH_MAX_TOKENS
    #: Ceiling on prior-results context carried into each per-scan call. See
    #: DEFAULT_BATCH_CARRIED_CONTEXT_MAX_CHARS — this bounds a cost that would
    #: otherwise grow with the SQUARE of the batch size.
    carried_context_max_chars: int = DEFAULT_BATCH_CARRIED_CONTEXT_MAX_CHARS
    #: ``${ANTHROPIC_API_KEY}`` in YAML; _substitute_env resolves it.
    api_key: str = ""


@dataclass
class DripConfig:
    """The ``drip:`` block. Absent block ⇒ no campaigns ⇒ feature off."""

    enabled: bool = True
    vault_path: str = ""
    data_dir: str = "./data"
    #: Instance display name, from ``telegram.instance.name``. Load-bearing:
    #: ``campaign_state_path`` REFUSES an unscoped path because every instance
    #: on the box shares one WorkingDirectory, and two instances sharing a
    #: campaign cursor would each advance the other past items neither
    #: processed. Empty here is not fixed up with a default — it fails at the
    #: point of use, loudly.
    instance: str = ""
    #: This instance's ``telegram.anthropic.model``, carried here so
    #: ``build_campaign`` can resolve a campaign that inherits rather than
    #: names its own model. Read at load rather than re-read from ``raw`` at
    #: build time, because ``build_campaign`` never sees ``raw``.
    talker_model: str = ""
    campaigns: dict[str, CampaignConfig] = field(default_factory=dict)

    def enabled_campaigns(self) -> dict[str, CampaignConfig]:
        """Campaigns that should actually run this invocation."""
        if not self.enabled:
            return {}
        return {n: c for n, c in self.campaigns.items() if c.enabled}


def load_from_unified(raw: dict[str, Any]) -> DripConfig:
    """Build :class:`DripConfig` from the pre-loaded unified config dict.

    Emits an explicit signal when the block is absent — "ran, found nothing to
    configure" is a different fact from "failed to load", and a background
    feature that renders nothing is precisely where that ambiguity hides.
    """
    from alfred.audit import instance_name_from_raw

    raw = _substitute_env(raw)
    block = raw.get("drip")

    vault_path = ""
    vault_raw = raw.get("vault")
    if isinstance(vault_raw, dict):
        vault_path = str(vault_raw.get("path") or "")

    data_dir = "./data"
    log_raw = raw.get("logging")
    if isinstance(log_raw, dict) and log_raw.get("dir"):
        data_dir = str(log_raw["dir"])

    instance = instance_name_from_raw(raw)

    # This instance's configured model, for campaigns that inherit rather than
    # name their own. Read defensively — an absent telegram block is normal on
    # a non-talker instance, and it simply means there is nothing to inherit.
    talker_model = ""
    telegram_raw = raw.get("telegram")
    if isinstance(telegram_raw, dict):
        anthropic_raw = telegram_raw.get("anthropic")
        if isinstance(anthropic_raw, dict):
            talker_model = str(anthropic_raw.get("model") or "").strip()

    if not isinstance(block, dict) or not block:
        # ILB: deploy-inert is a legitimate steady state on every instance that
        # has not turned drip on. Say so, so "no campaigns configured" is
        # distinguishable from "the config failed to parse".
        log.info(
            "drip.config.absent",
            instance=instance or "(unset)",
            detail="no drip: block — feature off, no campaigns, no state files",
        )
        return DripConfig(
            enabled=False, vault_path=vault_path, data_dir=data_dir,
            instance=instance, talker_model=talker_model,
        )

    top = _known_only(DripConfig, block)
    top.pop("campaigns", None)      # built by hand below
    cfg = DripConfig(
        **{
            **top,
            "vault_path": str(block.get("vault_path") or vault_path),
            "data_dir": str(block.get("data_dir") or data_dir),
            "instance": instance,
            "talker_model": talker_model,
        }
    )

    raw_campaigns = block.get("campaigns")
    if isinstance(raw_campaigns, dict):
        for name, body in raw_campaigns.items():
            if not isinstance(body, dict):
                continue
            fields = _known_only(CampaignConfig, body)
            # ``kind`` defaults to the block's own key, so the common case
            # (``gmail_backlog:``) needs no redundant ``kind: gmail_backlog``.
            fields.setdefault("kind", str(name))
            cfg.campaigns[str(name)] = CampaignConfig(**fields)

    if not cfg.enabled_campaigns():
        # ILB again, and a DIFFERENT fact from the absent-block case above: the
        # operator wrote a drip block, and nothing in it will run.
        log.info(
            "drip.config.no_enabled_campaigns",
            instance=instance or "(unset)",
            configured=sorted(cfg.campaigns),
            detail="drip block present but no campaign is enabled — nothing "
                   "will drain",
        )
    return cfg


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_AWAITING_RUNS",
    "DEFAULT_MAX_FAILURES_PER_RUN",
    "DEFAULT_MAX_ITEMS_PER_RUN",
    "DEFAULT_MAX_ITEMS_PER_WEEK",
    "KNOWN_CAMPAIGN_KINDS",
    "CampaignConfig",
    "DripConfig",
    "load_from_unified",
]
