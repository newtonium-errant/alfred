"""Curator pipeline helpers — surviving utilities after backend collapse.

History: this module previously contained the 4-stage curator pipeline
(``run_pipeline`` + Stage 1 LLM ``_call_llm`` + Stages 2-4) used only when
``agent.backend == "openclaw"``. The 4-stage path was OpenClaw-only by
design; Claude (the production backend) always ran the legacy single-call
path in ``daemon.py``.

The backend-abstraction-collapse arc (2026-05-25) removed the OpenClaw
backend and with it the dead pipeline orchestration. What survives here
are the pure-Python helpers that other call sites still use:

- :func:`_apply_preference_filter` — Stage 1.5 operator-preference gate
  (V1 ``skip_event_if``). Today called only from
  ``tests/preferences/test_curator_preference_filter.py``; the live
  curator daemon's legacy single-call path doesn't currently invoke it
  (that re-wiring is a separate arc). Kept here because the operator
  preferences V1 contract is still load-bearing — the next-gen curator
  agent (whatever shape it takes) MUST re-enter through this gate, so
  the helper has to stay reachable.
- :func:`_resolve_entities` + supporting helpers (:func:`_normalize_name`,
  :func:`_entity_exists`) — Stage 2 entity-resolution logic. Used by
  ``tests/curator/test_pipeline_attribution.py`` to pin the
  attribution-marker wrapping contract on agent-inferred entity bodies.
  Pure Python — reachable from any future Stage-2 caller.

Future re-introduction of a multi-stage pipeline (Q3 MCP migration or
similar) should either re-add the orchestration here or move these
helpers into a more-aptly-named module (e.g. ``entity_resolver.py``).
The deliberate choice to leave them in ``pipeline.py`` is to avoid
introducing new files in a pure-subtractive cleanup commit.
"""

from __future__ import annotations

from pathlib import Path

from alfred.preferences.loader import Preference
from alfred.preferences.matchers import evaluate
from alfred.vault.mutation_log import log_mutation
from alfred.vault.ops import VaultError, vault_create

from .utils import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Operator-preference action gates (V1)
# ---------------------------------------------------------------------------
#
# Entity manifests carry mixed-type candidates (person, org, event,
# project, etc.). The action-gate filter runs BEFORE entity creation so
# a "skip open-house events" preference cannot leave a half-created
# event in the vault if the filter fires mid-flow.
#
# Per project_operator_preferences_v1.md Hard Contract #1+#2: V1
# scope is event records via ``skip_event_if`` (the originating
# 2026-05-21 friction). Future widening (skip_task_if,
# skip_org_if) extends the dispatch dict — same shape.

_CURATOR_RULE_BY_TYPE: dict[str, str] = {
    "event": "skip_event_if",
}


# P10 / Ship 3 (2026-06-07) — inbox-stage filter rule. Distinct from
# the manifest-stage ``_CURATOR_RULE_BY_TYPE`` dispatch because the
# inbox filter runs BEFORE entity extraction (no manifest yet — the
# gate is per-file, keyed off sender metadata only). Lifted as a
# module-level constant so the test surface can pin the rule name
# without spelunking the filter function body.
_INBOX_FILTER_RULE: str = "skip_inbox_if_sender_matches"


def _apply_preference_filter(
    manifest: list[dict],
    prefs: list[Preference],
) -> list[dict]:
    """Filter an entity manifest against active action preferences.

    Returns the manifest with skipped candidates removed. Logs one
    ``curator.preference_filter_dropped`` per drop, carrying the
    matched preference slug + the matcher reason, so the operator can
    grep "why was this event silently dropped" against the daemon
    log. Per ``feedback_intentionally_left_blank.md``: the empty-drop
    case (zero drops applied) also emits a ``curator.preference_filter_run``
    summary log with ``drops=0`` so an operator can distinguish
    "filter ran, nothing matched" from "filter never ran."
    """
    if not prefs:
        # No active preferences in this vault — filter is a no-op.
        # Still emit the run signal so the operator-grep workflow can
        # confirm the call site fired.
        log.info(
            "curator.preference_filter_run",
            preferences_loaded=0,
            candidates_in=len(manifest),
            drops=0,
        )
        return list(manifest)

    kept: list[dict] = []
    drops = 0
    for entity in manifest:
        entity_type = entity.get("type", "")
        rule = _CURATOR_RULE_BY_TYPE.get(entity_type)
        if rule is None:
            # No registered gate for this entity type — keep it.
            kept.append(entity)
            continue
        candidate = {
            "name": entity.get("name", ""),
            "title": entity.get("name", ""),
        }
        # Apply each preference whose matcher targets this rule. First
        # skip wins (operator only needs ONE preference to gate a
        # candidate).
        skipped_by: Preference | None = None
        skipped_reason = ""
        for pref in prefs:
            matcher = pref.matcher or {}
            if matcher.get("rule") != rule:
                continue
            if matcher.get("domain") not in (None, "curator"):
                continue
            result = evaluate(rule, matcher.get("args", {}), candidate)
            if result.skip:
                skipped_by = pref
                skipped_reason = result.reason
                break
        if skipped_by is not None:
            log.info(
                "curator.preference_filter_dropped",
                preference_slug=skipped_by.slug,
                preference_name=skipped_by.name,
                entity_type=entity_type,
                entity_name=entity.get("name", ""),
                rule=rule,
                reason=skipped_reason,
            )
            drops += 1
            continue
        kept.append(entity)

    log.info(
        "curator.preference_filter_run",
        preferences_loaded=len(prefs),
        candidates_in=len(manifest),
        drops=drops,
        candidates_out=len(kept),
    )
    return kept


# ---------------------------------------------------------------------------
# Inbox-stage preference filter (P10 / Ship 3 — 2026-06-07)
# ---------------------------------------------------------------------------
#
# Distinct from ``_apply_preference_filter`` above which gates per-entity
# AFTER the agent's stage 1 extraction. This one gates per-FILE BEFORE
# any LLM call — sender-based blocklist for empty-body promotional traffic
# (Salem's recent inbox is ~99% empty-body, ~29% Substack-platform-routed).
#
# The two filters are separate functions (rather than a polymorphic
# dispatch on "phase") because:
#   1. Inputs differ — manifest filter takes ``list[dict]`` candidates,
#      inbox filter takes ``str | None`` sender.
#   2. Output differs — manifest returns filtered ``list[dict]``, inbox
#      returns ``(should_skip, reason, matching_pref)`` so the daemon
#      can short-circuit + log + move + bump stats.
#   3. The two filters fire at different points in ``_process_file`` and
#      composing them via a single ``apply_filters`` would mask the
#      cost-saving "no LLM at all" property of the inbox stage.


def _apply_inbox_preference_filter(
    sender_email: str | None,
    prefs: list[Preference],
) -> tuple[bool, str | None, Preference | None]:
    """Apply inbox-stage operator-preference filter to one inbox file.

    Operates on inbox-file metadata (sender) BEFORE any LLM call. The
    caller (``curator.daemon._process_file``) short-circuits the file
    when ``should_skip`` is True: marks the file ``filtered_by_preference``
    via :func:`writer.mark_filtered`, records a state row with
    ``backend_used="preference_filter_inbox"``, bumps the daily-summary
    stats bucket, and returns without invoking the backend.

    Args:
        sender_email: The sender extracted from the inbox file's
            ``**From:**`` line by
            :func:`alfred.curator.context.extract_sender_email`. ``None``
            (or empty) means the file is not email-derived; the filter
            is a no-op for those.
        prefs: Active preferences loaded from ``<vault>/preference/``.
            Only entries with ``shape=action``, ``matcher.domain=curator``,
            and ``matcher.rule=skip_inbox_if_sender_matches`` are
            considered; everything else is silently passed over (the
            other-rule prefs may be valid for other consumers).

    Returns:
        ``(should_skip, reason, matching_pref)``:
            * ``should_skip`` — True if the caller should drop this file.
            * ``reason`` — operator-grep-able motivation string when
              ``should_skip`` is True; None otherwise.
            * ``matching_pref`` — the :class:`Preference` whose matcher
              fired (so the caller can name the slug in the drop log
              and update the daily-summary stats bucket); None otherwise.

    Per ``feedback_intentionally_left_blank.md``: every code path emits
    a ``curator.preference_filter_inbox_run`` log so an operator can
    distinguish "filter ran, nothing to drop" from "filter never ran"
    via grep. The DROP log (``curator.preference_filter_inbox_dropped``)
    fires at the caller site where the inbox filename + state-mgr
    context lives — keeping it there avoids threading the filename
    through this helper.
    """
    if not sender_email:
        log.info(
            "curator.preference_filter_inbox_run",
            preferences_loaded=len(prefs),
            result="no_sender",
            detail=(
                "inbox file has no extractable sender — non-email "
                "or unparseable From line; rule does not fire"
            ),
        )
        return (False, None, None)

    if not prefs:
        log.info(
            "curator.preference_filter_inbox_run",
            preferences_loaded=0,
            sender=sender_email,
            result="no_preferences",
        )
        return (False, None, None)

    # Filter to inbox-relevant preferences only. ``prefs`` may carry
    # mixed-rule entries (skip_event_if for the manifest filter,
    # skip_brief_event_if for brief, etc.); we iterate the full list
    # and dispatch only those matching this filter's rule + domain.
    candidate = {"sender": sender_email}
    inbox_prefs_considered = 0
    for pref in prefs:
        matcher = pref.matcher or {}
        if matcher.get("rule") != _INBOX_FILTER_RULE:
            continue
        # ``matcher.domain`` is optional in v1 of the preference schema
        # (Andrew's authored prefs may omit it). Treat missing as
        # "matches every consumer"; only explicit other-domain values
        # filter the pref out.
        if matcher.get("domain") not in (None, "curator"):
            continue
        inbox_prefs_considered += 1
        result = evaluate(_INBOX_FILTER_RULE, matcher.get("args", {}), candidate)
        if result.skip:
            log.info(
                "curator.preference_filter_inbox_run",
                preferences_loaded=len(prefs),
                preferences_considered=inbox_prefs_considered,
                sender=sender_email,
                result="match",
                preference_slug=pref.slug,
            )
            return (True, result.reason, pref)

    # No match across any inbox-relevant preference. Emit the run
    # signal so the empty-match case stays observable.
    log.info(
        "curator.preference_filter_inbox_run",
        preferences_loaded=len(prefs),
        preferences_considered=inbox_prefs_considered,
        sender=sender_email,
        result="no_match",
    )
    return (False, None, None)


# ---------------------------------------------------------------------------
# Entity resolution (Stage 2 helpers)
# ---------------------------------------------------------------------------


def _normalize_name(name: str, entity_type: str) -> str:
    """Normalize entity name for matching."""
    name = name.strip()
    if entity_type == "person":
        # Title case for persons
        name = name.title()
    return name


def _entity_exists(vault_path: Path, entity_type: str, name: str) -> str | None:
    """Check if an entity already exists (case-insensitive).

    Returns the canonical rel_path as it lives on disk (preserving the
    existing file's casing) if a case-insensitive stem match is found,
    otherwise None. This prevents the entity-resolution stage from
    creating duplicate records like ``org/PocketPills.md`` when
    ``org/Pocketpills.md`` already exists.
    """
    from alfred.vault.schema import TYPE_DIRECTORY

    directory = TYPE_DIRECTORY.get(entity_type, entity_type)
    type_dir = vault_path / directory
    if not type_dir.is_dir():
        return None
    target = name.casefold()
    for candidate in type_dir.glob("*.md"):
        if candidate.stem.casefold() == target:
            return f"{directory}/{candidate.stem}.md"
    return None


def _resolve_entities(
    manifest: list[dict],
    vault_path: Path,
    session_path: str,
) -> dict[str, str]:
    """For each entity in a manifest, check if it exists or create it.

    Returns a map: "type/Name" -> rel_path (e.g. "person/John Smith" ->
    "person/John Smith.md"). Mutates the vault via ``vault_create``,
    appending to the mutation log at ``session_path`` for each
    successful create.

    Agent-inferred bodies (manifest carries a ``body`` field) are
    wrapped in BEGIN_INFERRED markers and emit an
    ``attribution_audit`` frontmatter entry per c4 (calibration audit).
    """
    from alfred.vault.schema import TYPE_DIRECTORY

    resolved: dict[str, str] = {}

    for entity in manifest:
        entity_type = entity.get("type", "")
        name = entity.get("name", "")
        description = entity.get("description", "")
        fields = entity.get("fields", {})

        if not entity_type or not name:
            log.warning("pipeline.s2_skip_invalid", entity=entity)
            continue

        name = _normalize_name(name, entity_type)
        directory = TYPE_DIRECTORY.get(entity_type, entity_type)
        entity_key = f"{directory}/{name}"

        # Check if already resolved in this batch
        if entity_key in resolved:
            continue

        # Check if exists in vault
        existing_path = _entity_exists(vault_path, entity_type, name)
        if existing_path:
            resolved[entity_key] = existing_path
            log.info("pipeline.s2_entity_exists", entity=entity_key)
            continue

        # Use the full body from the manifest if provided (upstream
        # cbedd04 schema shift: manifests carry complete markdown bodies,
        # not stubs). Fall back to description-as-body for manifests
        # emitted before the schema shift landed (e.g., in-flight retries).
        manifest_body = entity.get("body", "")
        if manifest_body and manifest_body.strip():
            body = manifest_body
            if not body.endswith("\n"):
                body += "\n"
        else:
            body = f"# {name}\n\n{description}\n" if description else f"# {name}\n"

        # Parse fields — strip wrapping quotes from wikilink values
        set_fields: dict = {}
        for k, v in fields.items():
            if isinstance(v, str):
                # Remove outer escaped quotes: \"[[...]]\" -> [[...]]
                v = v.strip('"')
            set_fields[k] = v

        # Calibration audit gap (c4): the manifest body is composed by
        # the curator agent (LLM) from inbox content, so the body that
        # lands here is agent-inferred. Wrap it in BEGIN_INFERRED
        # markers and append an audit_entry to frontmatter. Only wrap
        # when there's actual body content (a bare ``# {name}``
        # placeholder is template scaffold, not inference — we still
        # wrap because the model's *choice* of name/description IS the
        # inference, and the wrap-vs-skip decision is "did the model
        # write prose here?").
        from alfred.vault import attribution
        if body and body.strip():
            wrapped_body, audit_entry = attribution.with_inferred_marker(
                body,
                section_title=name or entity_key,
                agent="curator",
                reason="curator stage 2 entity create (inbox source)",
            )
            body = wrapped_body
            existing_audit = set_fields.get("attribution_audit")
            audit_list: list = (
                list(existing_audit) if isinstance(existing_audit, list) else []
            )
            tmp_fm: dict = {"attribution_audit": audit_list}
            attribution.append_audit_entry(tmp_fm, audit_entry)
            set_fields["attribution_audit"] = tmp_fm["attribution_audit"]

        try:
            result = vault_create(
                vault_path,
                entity_type,
                name,
                set_fields=set_fields,
                body=body,
            )
            rel_path = result["path"]
            resolved[entity_key] = rel_path
            log_mutation(session_path, "create", rel_path)
            log.info("pipeline.s2_entity_created", entity=entity_key, path=rel_path)
        except VaultError as e:
            details = getattr(e, "details", None) or {}
            # Near-match collision: vault_create refused because a record
            # with a matching casefolded name already exists. Reuse the
            # canonical path from the structured error details so
            # downstream callers reference the existing record instead
            # of dropping the entity.
            if details.get("reason") == "near_match" and details.get("canonical_path"):
                canonical_path = details["canonical_path"]
                resolved[entity_key] = canonical_path
                log.info(
                    "pipeline.s2_entity_near_match_reused",
                    entity=entity_key,
                    attempted_path=f"{directory}/{name}.md",
                    canonical_path=canonical_path,
                )
                continue
            log.warning("pipeline.s2_create_failed", entity=entity_key, error=str(e))
            # If creation failed because it already exists, record the path anyway
            if "already exists" in str(e).lower():
                resolved[entity_key] = f"{directory}/{name}.md"

    return resolved
