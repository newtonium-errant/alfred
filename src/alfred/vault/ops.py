"""Core vault operations — create, read, edit, search, move, delete.

When Obsidian is running (1.12+), operations automatically use the Obsidian CLI
for search, link resolution, and moves (which updates wikilinks vault-wide).
Falls back to filesystem operations when Obsidian is unavailable.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Callable

import frontmatter
import structlog
import yaml

from . import obsidian
from .schema import (
    KNOWN_TYPES,
    KNOWN_TYPES_BY_SCOPE,
    LIST_FIELDS,
    NAME_FIELD_BY_TYPE,
    REQUIRED_FIELDS,
    STATUS_BY_TYPE,
    TYPE_DIRECTORY,
)
from .scope import check_scope

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Event hooks (Phase A+ — vault-ops integration with external syncers)
# ---------------------------------------------------------------------------
#
# Three registries fire after successful vault operations on
# ``event/`` records. The talker daemon registers a GCal sync function
# as the implementation of all three at startup, so a Salem-authored
# event from any source (Telegram chat, instructor executor, daily-sync
# dispatcher, future agents) mirrors to GCal automatically without each
# call site duplicating the sync logic.
#
# Hook signatures:
#   create: (vault_path: Path, rel_path: str, frontmatter: dict) -> None
#   update: (vault_path: Path, rel_path: str, frontmatter: dict,
#            fields_changed: list[str]) -> None
#   delete: (vault_path: Path, rel_path: str,
#            pre_delete_frontmatter: dict) -> None
#
# Hooks MUST NOT raise — exceptions are caught and logged so a broken
# hook can never break vault_create / vault_edit / vault_delete (the
# vault is canonical; external sync is a projection). Hooks SHOULD be
# fast (no blocking I/O on the request hot path) — the GCal sync
# function uses synchronous googleapiclient under the hood, which is
# acceptable for v1 single-user scale.
#
# The update hook fires after every successful vault_edit on event
# records — the hook closure decides what to do based on post-edit
# frontmatter. Two relevant states:
#   * ``gcal_event_id`` present → patch the existing GCal mirror
#   * ``gcal_event_id`` absent BUT ``start`` + ``end`` present →
#     "first-sync promotion": the record just became GCal-eligible,
#     push as if it were a fresh create
# The earlier registry-level gate on ``gcal_event_id`` blocked the
# promotion path and silently no-op'd. Decision authority lives in
# the hook now (it has the full picture; the registry doesn't).
# The delete hook reads frontmatter BEFORE the file is removed so it
# has access to ``gcal_event_id`` for the GCal-side delete.

EventCreateHook = Callable[[Path, str, dict], None]
EventUpdateHook = Callable[[Path, str, dict, list], None]
EventDeleteHook = Callable[[Path, str, dict], None]

_EVENT_CREATE_HOOKS: list[EventCreateHook] = []
_EVENT_UPDATE_HOOKS: list[EventUpdateHook] = []
_EVENT_DELETE_HOOKS: list[EventDeleteHook] = []


def register_event_create_hook(func: EventCreateHook) -> None:
    """Register a callable to fire after every successful vault_create on
    an ``event/`` record.

    Idempotent on (function-identity); registering the same callable
    twice is a no-op so daemon restarts that re-import this module
    don't double-fire.
    """
    if func not in _EVENT_CREATE_HOOKS:
        _EVENT_CREATE_HOOKS.append(func)


def register_event_update_hook(func: EventUpdateHook) -> None:
    """Register a callable to fire after every successful vault_edit
    on an ``event/`` record.

    Decision authority lives in the hook — it sees the post-edit
    frontmatter + the list of changed fields and decides:
      * ``gcal_event_id`` present → patch the GCal mirror
      * ``gcal_event_id`` absent AND ``start``/``end`` now set →
        first-sync promotion (push as a fresh create + writeback ID)
      * anything else → no-op (no datetimes yet; nothing to sync)
    """
    if func not in _EVENT_UPDATE_HOOKS:
        _EVENT_UPDATE_HOOKS.append(func)


def register_event_delete_hook(func: EventDeleteHook) -> None:
    """Register a callable to fire after vault_delete on event records.

    The pre-delete frontmatter is captured BEFORE the file is removed
    and passed to the hook so it has access to ``gcal_event_id``
    (needed for the GCal-side delete; can't read it after the file
    is gone).
    """
    if func not in _EVENT_DELETE_HOOKS:
        _EVENT_DELETE_HOOKS.append(func)


def clear_event_hooks() -> None:
    """Test helper — wipe all three registries.

    Production code never calls this; tests use it to isolate per-test
    hook state when the registries are otherwise process-global.
    """
    _EVENT_CREATE_HOOKS.clear()
    _EVENT_UPDATE_HOOKS.clear()
    _EVENT_DELETE_HOOKS.clear()


def _fire_create_hooks(
    vault_path: Path, rel_path: str, fm: dict,
) -> list[dict]:
    """Iterate registered create hooks; each exception is logged + swallowed.

    Returns a list of dict-shaped hook return values (non-dict / ``None``
    returns are filtered out). The caller uses this to surface side-effect
    status (e.g. GCal sync state) up to its own return contract — see
    :func:`vault_create`, which forwards a single dict result to the
    caller under the ``gcal_sync`` key when present.

    The "single dict result bubbled up" rule in ``vault_create`` is a v1
    pragmatism: the only registered hook today is the GCal sync, and
    multiplexing multiple hook results into one key would muddle the
    contract the LLM tool_result depends on. If a second hook lands
    later that wants to surface state, extend ``vault_create`` to emit
    a list (or per-hook keyed dict) rather than collapsing both into
    ``gcal_sync``.

    Pre-2026-05-13 this returned ``None`` and discarded hook return
    values; the GCal sync hook called ``sync_event_create_to_gcal``,
    got back a ``{"error": {...}}`` on auth_failed, and silently
    dropped it on the floor — so the talker tool_result said "vault
    create succeeded" with no GCal-failure trace and the LLM narrated
    "GCal updated" to Andrew over two consecutive auth_failed events
    (May 12 18:45 ADT, May 13 03:32 ADT). The collection-and-forward
    contract closes that gap.
    """
    results: list[dict] = []
    for hook in list(_EVENT_CREATE_HOOKS):
        try:
            ret = hook(vault_path, rel_path, fm)
            if isinstance(ret, dict):
                results.append(ret)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "vault.event_create_hook_failed",
                hook=getattr(hook, "__name__", repr(hook)),
                rel_path=rel_path,
                error=str(exc),
            )
    return results


def _fire_update_hooks(
    vault_path: Path, rel_path: str, fm: dict, fields_changed: list,
) -> list[dict]:
    """Iterate registered update hooks; each exception is logged + swallowed.

    Fires unconditionally on event records (post-refactor). The hook
    itself decides whether the post-edit state warrants a GCal call —
    see :func:`register_event_update_hook` for the three states the
    hook discriminates between (patch / promote-to-create / no-op).

    Pre-refactor this function gated on ``fm.get("gcal_event_id")`` and
    silently no-op'd otherwise. That gate blocked the "vault_edit adds
    start+end to a previously-no-time event" promotion path: vault
    record gained datetimes, but GCal never got the event because the
    hook never fired. User-visible misleading: Salem said "will appear
    on your phone shortly" but nothing happened.

    Returns dict-shaped hook results so ``vault_edit`` can surface the
    sync status under ``gcal_sync`` in its own return dict. The
    contract is identical to :func:`_fire_create_hooks`; see that
    docstring for the rationale on single-result bubbling and the
    May 2026 incident that motivated the fix.
    """
    results: list[dict] = []
    for hook in list(_EVENT_UPDATE_HOOKS):
        try:
            ret = hook(vault_path, rel_path, fm, list(fields_changed))
            if isinstance(ret, dict):
                results.append(ret)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "vault.event_update_hook_failed",
                hook=getattr(hook, "__name__", repr(hook)),
                rel_path=rel_path,
                error=str(exc),
            )
    return results


def _fire_delete_hooks(
    vault_path: Path, rel_path: str, pre_delete_fm: dict,
) -> list[dict]:
    """Iterate registered delete hooks; each exception is logged + swallowed.

    Returns dict-shaped hook results so ``vault_delete`` can surface
    sync status (e.g. ``gcal_sync: {status: failed, ...}``) up to the
    LLM tool_result. Mirrors :func:`_fire_create_hooks` /
    :func:`_fire_update_hooks` — see those docstrings for the
    rationale.
    """
    results: list[dict] = []
    for hook in list(_EVENT_DELETE_HOOKS):
        try:
            ret = hook(vault_path, rel_path, pre_delete_fm)
            if isinstance(ret, dict):
                results.append(ret)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "vault.event_delete_hook_failed",
                hook=getattr(hook, "__name__", repr(hook)),
                rel_path=rel_path,
                error=str(exc),
            )
    return results


# Maximum length of the ``error`` detail string we surface to the LLM
# tool_result. The full message lives in the daemon's warning log; the
# LLM only needs the gist so it can phrase "GCal didn't update — looks
# like an auth refresh" without dumping the whole token-refresh stack
# trace into the chat reply.
_GCAL_SYNC_ERROR_MAX_LEN = 200


def translate_gcal_sync_result(result: dict | None) -> dict | None:
    """Translate one ``sync_event_*_to_gcal`` return dict to the LLM-facing shape.

    Single-result variant of :func:`_extract_gcal_sync_status`. Both
    the in-process vault-ops hooks (which receive a ``list[dict]`` from
    the hook registry) and the out-of-process peer-handler path (which
    calls ``sync_event_create_to_gcal`` directly and gets ONE dict
    back) need the same translation, so the per-result logic lives
    here and the list-shaped helper just delegates.

    Input shapes (documented at the top of
    :mod:`alfred.integrations.gcal_sync`):

      * ``None`` / ``{}`` — gcal not configured / disabled. Returns
        ``None`` (caller omits ``gcal_sync``).
      * ``{"event_id": "<id>", "calendar_label": "<label>"}`` — success
        on create / update.
      * ``{"deleted": True, "event_id": "<id>"}`` — success on delete.
      * ``{"noop": "<reason>"}`` — record has no ``gcal_event_id`` to
        patch / remove. Returns ``None`` (no GCal action; same posture
        as disabled from the LLM's perspective).
      * ``{"error": {"code": "<code>", "detail": "<msg>"}}`` — sync
        failed; vault op succeeded but GCal did not.

    Output shape (the contract the LLM tool_result depends on):

      * ``None`` — no GCal action attempted (disabled OR noop). The
        caller MUST NOT include a ``gcal_sync`` key in the tool_result.
        Absent ≠ silently failed; absent = "GCal didn't participate."
      * ``{"status": "ok"}`` — sync succeeded.
      * ``{"status": "failed", "error_code": "<code>", "error": "<msg>"}``
        — sync failed. ``error_code`` is the stable classification
        from :func:`alfred.integrations.gcal_sync.classify_gcal_error`
        (``auth_failed`` / ``api_error`` / ``stale_gcal_id`` /
        ``calendar_id_missing`` / ``missing_dependency`` / etc.);
        ``error`` is the truncated detail message.
    """
    # Disabled / noop — no GCal action attempted. Omit gcal_sync entirely.
    if not result or "noop" in result:
        return None

    # Failure path — surface the structured error.
    err = result.get("error")
    if isinstance(err, dict):
        code = str(err.get("code", "") or "unknown")
        detail = str(err.get("detail", "") or "")
        if len(detail) > _GCAL_SYNC_ERROR_MAX_LEN:
            detail = detail[: _GCAL_SYNC_ERROR_MAX_LEN - 1] + "…"
        return {
            "status": "failed",
            "error_code": code,
            "error": detail,
        }

    # Success path — create / update / delete all land here.
    if (
        "event_id" in result
        or result.get("deleted") is True
    ):
        return {"status": "ok"}

    # Unrecognized shape — surface as failed with a synthetic code so
    # the LLM still sees "something didn't work" rather than narrating
    # success. Cheap defense-in-depth against future return-shape drift
    # in the sync layer.
    return {
        "status": "failed",
        "error_code": "unknown",
        "error": "unrecognized gcal_sync hook return shape",
    }


def _extract_gcal_sync_status(
    hook_results: list[dict],
) -> dict | None:
    """Translate hook return values into the LLM-facing ``gcal_sync`` shape.

    List-shaped variant for the vault-ops hook registry: the event
    hooks call ``sync_event_*_to_gcal`` and forward its return dict
    back through the bubble-up contract in :func:`_fire_create_hooks`
    / :func:`_fire_update_hooks` / :func:`_fire_delete_hooks`.

    Multiple hook results: only the first dict-shaped hook return
    value is honored — v1 pragmatism, only the GCal hook is registered
    today. See :func:`_fire_create_hooks` for the rationale and the
    extension path for a second consumer.

    Per-result translation lives in :func:`translate_gcal_sync_result`
    so the peer-handler (which gets a single dict directly from
    ``sync_event_create_to_gcal``) can reuse the same shape contract.
    """
    if not hook_results:
        return None
    # Honor the first dict result. ``_fire_*_hooks`` already filtered
    # out non-dict / None returns so any entry here is a real hook reply.
    return translate_gcal_sync_result(hook_results[0])


class VaultError(Exception):
    """Raised when a vault operation fails validation.

    Optional ``details`` dict carries structured error metadata that the CLI
    layer surfaces to callers (e.g., the canonical path of a near-match
    collision so the agent can pivot to ``vault_edit``).
    """

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.details = details


def _resolve_vault_path(vault_path: Path, rel_path: str) -> Path:
    """Resolve a relative path within the vault, preventing traversal."""
    full = (vault_path / rel_path).resolve()
    if not str(full).startswith(str(vault_path.resolve())):
        raise VaultError(f"Path traversal denied: {rel_path}")
    return full


def is_ignored_path(rel_path: str | Path, ignore_dirs: set[str] | list[str]) -> bool:
    """Return True if ``rel_path`` should be skipped under ``ignore_dirs``.

    Supports two entry shapes in ``ignore_dirs`` so callers can choose the
    right granularity without changing schema:

    - **Single component** (no ``/`` in the entry) — matches any path
      component. ``".obsidian"`` matches ``foo/.obsidian/bar.md``. This is
      the legacy behavior every scanner already relies on, and the only
      shape fresh installs use today.
    - **Nested path** (contains ``/``) — matches a path prefix. The entry
      ``"inbox/processed"`` matches ``inbox/processed/x.md`` but does NOT
      match ``inbox/x.md`` or ``notes/inbox/processed/x.md``. This lets
      janitor/distiller exclude the curator's audit directory without
      excluding the whole ``inbox/`` (curator needs to see fresh inbox
      items, just not their processed copies).

    Path separators are normalized to ``/`` before matching so Windows
    paths work the same as POSIX.
    """
    rel_str = str(rel_path).replace("\\", "/").strip("/")
    if not rel_str:
        return False
    parts = rel_str.split("/")
    for ig in ignore_dirs:
        if "/" in ig:
            prefix = ig.strip("/")
            if rel_str == prefix or rel_str.startswith(prefix + "/"):
                return True
        else:
            if ig in parts:
                return True
    return False


def _parse_record(file_path: Path) -> tuple[dict, str]:
    """Parse a vault file into (frontmatter_dict, body_str).

    Raises VaultError if the file contains malformed YAML frontmatter.
    """
    try:
        post = frontmatter.load(str(file_path))
    except yaml.YAMLError as exc:
        raise VaultError(
            f"Malformed YAML frontmatter in {file_path.name}: {exc}"
        ) from exc
    return dict(post.metadata), post.content


def _serialize_record(fm: dict, body: str) -> str:
    """Serialize frontmatter + body back to a vault markdown file."""
    post = frontmatter.Post(body, **fm)
    return frontmatter.dumps(post) + "\n"


def _validate_type(record_type: str, scope: str | None = None) -> None:
    """Validate that ``record_type`` is known to the active scope.

    Two-layer contract:

    - ``scope=None`` (default) — preserve the historical behavior:
      only the canonical ``KNOWN_TYPES`` (Salem's 20-type set) are
      accepted. Every CLI / executor path that doesn't propagate a
      scope through stays byte-for-byte unchanged.
    - ``scope`` set — accept the union of canonical types plus any
      scope-specific extension set declared in
      ``schema.KNOWN_TYPES_BY_SCOPE`` (e.g. ``"kalle"`` unlocks
      ``pattern`` / ``principle``; ``"hypatia"`` unlocks ``document``
      / ``concept`` / ``source`` / ``citation`` / ``template``).

    This is the **first** of two gates on ``vault_create`` — it lets
    the type through. The **second** gate is ``check_scope``'s create
    allowlist (``KALLE_CREATE_TYPES``, ``HYPATIA_CREATE_TYPES``,
    ``TALKER_CREATE_TYPES``, etc.), which enforces the per-scope policy.
    Salem-scope agents calling ``vault_create("pattern", scope="talker")``
    will pass ``_validate_type`` here (canonical types only, no extension)
    and then be rejected by ``check_scope``'s ``talker_types_only`` rule.

    Originally this gate hardcoded ``KNOWN_TYPES`` and ran *before*
    ``check_scope``, which silently blocked Hypatia ``document`` and
    KAL-LE ``pattern`` creates with a misleading "Unknown type" error
    even though the type was legal under their scope's allowlist.
    Code-reviewer P1 #2 — release-blocker for Phase 1 Hypatia.
    """
    valid = KNOWN_TYPES_BY_SCOPE.get(scope, KNOWN_TYPES) if scope else KNOWN_TYPES
    if record_type not in valid:
        scope_hint = f" under scope '{scope}'" if scope else ""
        raise VaultError(
            f"Unknown type: '{record_type}'{scope_hint}. "
            f"Valid: {', '.join(sorted(valid))}"
        )


def _validate_status(record_type: str, status: str) -> None:
    if not status:
        return
    valid = STATUS_BY_TYPE.get(record_type, set())
    if valid and status not in valid:
        raise VaultError(
            f"Invalid status '{status}' for type '{record_type}'. "
            f"Valid: {', '.join(sorted(valid))}"
        )


def _coerce_list_fields(fields: dict) -> None:
    """Normalise list-field values so downstream code always sees a list.

    - ``None`` or empty string → ``[]``
    - Non-empty string         → ``[string]``   (single-item list)
    - Already a list           → no change

    Mutates *fields* in place.  Must be called **before** ``_validate_list_fields``.
    """
    for field_name in LIST_FIELDS:
        if field_name not in fields:
            continue
        val = fields[field_name]
        if val is None or val == "":
            fields[field_name] = []
        elif isinstance(val, str):
            fields[field_name] = [val]


def _validate_list_fields(fields: dict) -> None:
    for field_name in LIST_FIELDS:
        val = fields.get(field_name)
        if val is not None and not isinstance(val, list):
            raise VaultError(
                f"Field '{field_name}' must be a list, got {type(val).__name__}"
            )


def _validate_required_fields(fm: dict) -> None:
    for req in REQUIRED_FIELDS:
        if not fm.get(req):
            raise VaultError(f"Missing required field: {req}")


# Frontmatter field names that MUST NEVER be written into YAML
# frontmatter via ``set_fields``. These keys overlap with structural
# concepts that have a separate write path (``body=`` for the
# document body, ``body_append=`` / ``body_rewriter=`` for edits) and
# leak content corruption into ``vault_read`` consumers when an agent
# mis-routes them.
#
# Bug class (P1 from QA 2026-05-04): Hypatia in the DJ-tracker
# conversation called ``vault_edit(set_fields={"body": "..."})``
# instead of ``body_append=...`` (vault_edit's schema declares
# ``body_append`` not ``body``; vault_create declares ``body`` at
# top level — easy LLM confusion). The literal "body" key landed in
# YAML frontmatter, ``vault_read`` returned frontmatter containing a
# stale ``body`` field that didn't match the on-disk markdown body,
# downstream consumers (distiller scoring, surveyor entity_links)
# saw confused ground-truth.
#
# Fix is at the vault-ops gate so EVERY caller (talker, instructor,
# capture, calibration, future agents) gets the protection without
# each call site re-implementing the filter.
_FRONTMATTER_RESERVED_KEYS: frozenset[str] = frozenset({"body"})


def _filter_reserved_keys(
    set_fields: dict, *, op: str, rel_path: str = "",
) -> dict:
    """Strip reserved keys from ``set_fields`` and warn loudly.

    Returns a NEW dict (does not mutate the input). Emits one
    structured ``vault.ops.body_in_set_fields_filtered`` warning per
    filtered key so an operator can grep for the regression class.
    Mirrors the SDK quirk centralization principle: one filter at
    the gate beats N defensive checks at every call site.
    """
    if not set_fields:
        return set_fields or {}
    filtered: dict = {}
    leaked_keys: list[str] = []
    for k, v in set_fields.items():
        if k in _FRONTMATTER_RESERVED_KEYS:
            leaked_keys.append(k)
            continue
        filtered[k] = v
    if leaked_keys:
        log.warning(
            "vault.ops.body_in_set_fields_filtered",
            op=op,
            rel_path=rel_path,
            leaked_keys=leaked_keys,
            detail=(
                "Reserved frontmatter key(s) routed through set_fields "
                "and stripped at the vault-ops gate. Body content "
                "belongs in the body= (vault_create) or body_append= / "
                "body_rewriter= (vault_edit) parameters, NOT in YAML "
                "frontmatter. Likely an agent confusion between "
                "vault_create's body= top-level arg and vault_edit's "
                "body_append=. The write proceeded with the leaked "
                "key dropped; the operator should review the calling "
                "agent's prompt for body-handling guidance."
            ),
        )
    return filtered


def _check_directory(record_type: str, rel_path: str) -> str | None:
    """Return a warning string if file is in the wrong directory, else None.

    Sub-path support: some types in ``TYPE_DIRECTORY`` use a multi-segment
    expected directory (e.g. ``voice-cluster`` → ``voice/cluster``,
    ``essay`` → ``document/essay``). The check must compare ``rel_path``'s
    leading segments against the FULL expected sub-path, not just the
    first segment. Pre-fix the comparator did ``parts[0] != expected_dir``
    which compared ``"voice"`` against ``"voice/cluster"`` and fired a
    false-positive warning on every canonical voice-cluster create — even
    though the file landed at the correct ``voice/cluster/<name>.md``
    path. Per Hypatia voice-profile rebuild 2026-05-09 (NOTE-1) and
    identical class for any future sub-path type. Regression test:
    ``test_vault_ops_subpath_directory_warning.py``.
    """
    expected_dir = TYPE_DIRECTORY.get(record_type)
    if not expected_dir:
        return None
    rel_norm = rel_path.replace("\\", "/")
    expected_norm = expected_dir.replace("\\", "/").strip("/")
    if not expected_norm:
        return None
    expected_parts = expected_norm.split("/")
    actual_parts = rel_norm.split("/")
    # Need at least one filename segment plus every expected directory
    # segment for the path to be canonically placed. A path with too few
    # segments to encode the full expected sub-path can't be canonical, so
    # warn against the first-segment mismatch as before.
    if len(actual_parts) <= len(expected_parts):
        if len(actual_parts) > 1 and actual_parts[0] != expected_parts[0]:
            return (
                f"Type '{record_type}' expected in '{expected_norm}/', "
                f"found in '{actual_parts[0]}/'"
            )
        return None
    actual_prefix = actual_parts[: len(expected_parts)]
    if actual_prefix != expected_parts:
        found_prefix = "/".join(actual_prefix)
        return (
            f"Type '{record_type}' expected in '{expected_norm}/', "
            f"found in '{found_prefix}/'"
        )
    return None


def _check_near_match(
    vault_path: Path, record_type: str, name: str
) -> tuple[str, str] | None:
    """Return ``(canonical_rel_path, message)`` if a case-insensitive filename
    collision exists, else ``None``.

    Prevents accidental dedup misses like 'PocketPills' vs 'Pocketpills'.
    This is a hard gate inside ``vault_create``: the caller raises ``VaultError``
    with ``details={"canonical_path": ..., "reason": "near_match"}`` so the
    requesting agent can pivot to ``vault_edit`` on the existing record.
    """
    directory = TYPE_DIRECTORY.get(record_type, record_type)
    type_dir = vault_path / directory
    if not type_dir.is_dir():
        return None
    target = name.casefold()
    for existing in type_dir.glob("*.md"):
        if existing.stem.casefold() == target and existing.stem != name:
            canonical = f"{directory}/{existing.stem}.md"
            message = (
                f"Near-match exists: '{canonical}' "
                f"(case-insensitive match for '{name}'). "
                f"Use vault_edit on the existing record instead of creating a duplicate."
            )
            return canonical, message
    return None


def _extract_wikilink_targets(body: str, fm: dict) -> set[str]:
    """Extract all wikilink targets from body text and frontmatter values."""
    link_re = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")
    targets: set[str] = set()
    for m in link_re.finditer(body):
        targets.add(m.group(1))
    for v in fm.values():
        if isinstance(v, str):
            for m in link_re.finditer(v):
                targets.add(m.group(1))
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str):
                    for m in link_re.finditer(item):
                        targets.add(m.group(1))
    return targets


def _check_wikilinks(body: str, fm: dict, vault_path: Path) -> list[str]:
    """Return list of warning strings for unresolved wikilinks."""
    targets = _extract_wikilink_targets(body, fm)
    if not targets:
        return []

    warnings: list[str] = []
    for target in targets:
        candidate = vault_path / f"{target}.md"
        if not candidate.exists():
            if not (vault_path / target).exists():
                warnings.append(f"Unresolved wikilink: [[{target}]]")
    return warnings


def _load_template(vault_path: Path, record_type: str) -> tuple[dict, str] | None:
    """Load a template from _templates/ if it exists. Returns (fm, body) or None."""
    template_path = vault_path / "_templates" / f"{record_type}.md"
    if not template_path.exists():
        return None
    return _parse_record(template_path)


_BASE_EMBED_RE = re.compile(r"^(##\s+.+\n)?!\[\[.+\.base#.+\]\]$", re.MULTILINE)


def _extract_base_embeds(template_body: str, name: str) -> str:
    """Extract section-heading + base-embed lines from a template body.

    Returns a string like:
        ## Assumptions
        ![[project.base#Assumptions]]

        ## Decisions
        ![[project.base#Decisions]]
    """
    lines = template_body.replace("{{title}}", name).splitlines()
    result: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Check for "## Section\n![[*.base#*]]" pairs
        if line.startswith("## ") and i + 1 < len(lines) and "![[" in lines[i + 1] and ".base#" in lines[i + 1]:
            if result:
                result.append("")
            result.append(line)
            result.append(lines[i + 1])
            i += 2
            continue
        # Standalone base embed without heading
        if "![[" in line and ".base#" in line:
            if result:
                result.append("")
            result.append(line)
        i += 1
    return "\n".join(result) + "\n" if result else ""


# --- Public operations ---


def vault_read(vault_path: Path, rel_path: str) -> dict:
    """Read a vault record. Returns {path, frontmatter, body}."""
    file_path = _resolve_vault_path(vault_path, rel_path)
    if not file_path.exists():
        raise VaultError(f"File not found: {rel_path}")
    if not file_path.suffix == ".md":
        raise VaultError(f"Not a markdown file: {rel_path}")

    fm, body = _parse_record(file_path)
    return {"path": rel_path, "frontmatter": fm, "body": body}


def vault_search(
    vault_path: Path,
    *,
    glob_pattern: str | None = None,
    grep_pattern: str | None = None,
    ignore_dirs: list[str] | None = None,
) -> list[dict]:
    """Search vault files. Returns list of {path, name, type, status}.

    When Obsidian is running, content searches (grep) use Obsidian's live
    index for faster, more accurate results. Falls back to filesystem search.
    """
    # Both branches below filter via ``is_ignored_path`` so callers can
    # use single-component entries (``"_templates"``) AND nested-path
    # entries (``"inbox/processed"``) — matching the public contract
    # documented on ``is_ignored_path``. Pre-2026-05-01 the filter used
    # an inline ``part in ignore`` check that silently dropped slash
    # entries, which made callers think they were filtering ``inbox/processed``
    # while actually filtering nothing. The pipeline-stage constant
    # (``STAGE_LOOKUP_NEVER_INDEX`` in janitor/pipeline.py) relies on
    # the slash entry working.
    ignore = ignore_dirs or []

    # Try Obsidian CLI for content search (grep without glob filter)
    if grep_pattern and not glob_pattern and obsidian.is_available():
        obs_results = obsidian.search_content(grep_pattern)
        if obs_results is not None:
            results: list[dict] = []
            for item in obs_results:
                path = item.get("path", item.get("file", ""))
                if is_ignored_path(path, ignore):
                    continue
                results.append({
                    "path": path,
                    "name": item.get("name", Path(path).stem),
                    "type": item.get("type", ""),
                    "status": item.get("status", ""),
                })
            return results

    # Filesystem fallback
    results: list[dict] = []

    if glob_pattern:
        matches = list(vault_path.glob(glob_pattern))
    else:
        matches = list(vault_path.rglob("*.md"))

    for md_file in sorted(matches):
        rel = md_file.relative_to(vault_path)
        if is_ignored_path(rel, ignore):
            continue

        # If grep, check content
        if grep_pattern:
            try:
                content = md_file.read_text(encoding="utf-8")
                if not re.search(re.escape(grep_pattern), content, re.IGNORECASE):
                    continue
            except (OSError, UnicodeDecodeError):
                continue

        # Parse frontmatter for metadata
        try:
            post = frontmatter.load(str(md_file))
            fm = post.metadata
        except Exception:
            fm = {}

        rel_str = str(rel).replace("\\", "/")
        results.append({
            "path": rel_str,
            "name": fm.get("name") or fm.get("subject") or md_file.stem,
            "type": fm.get("type", ""),
            "status": fm.get("status", ""),
        })

    return results


def vault_list(
    vault_path: Path,
    record_type: str,
    ignore_dirs: list[str] | None = None,
    *,
    scope: str | None = None,
) -> list[dict]:
    """List all records of a given type. Returns list of {path, name, status}.

    ``scope`` (kwarg-only) widens the type-validation gate so a Hypatia
    agent can list ``document`` records and a KAL-LE agent can list
    ``pattern`` records. Default ``None`` preserves the canonical
    ``KNOWN_TYPES`` gate for callers that don't propagate scope.
    """
    _validate_type(record_type, scope=scope)
    ignore = set(ignore_dirs or [])
    results: list[dict] = []

    for md_file in vault_path.rglob("*.md"):
        rel = md_file.relative_to(vault_path)
        if any(part in ignore for part in rel.parts):
            continue
        try:
            post = frontmatter.load(str(md_file))
            if post.metadata.get("type") != record_type:
                continue
        except Exception:
            continue

        rel_str = str(rel).replace("\\", "/")
        results.append({
            "path": rel_str,
            "name": post.metadata.get("name") or post.metadata.get("subject") or md_file.stem,
            "status": post.metadata.get("status", ""),
        })

    return sorted(results, key=lambda r: r["name"])


def vault_context(
    vault_path: Path,
    ignore_dirs: list[str] | None = None,
) -> dict:
    """Build a compact vault summary grouped by type."""
    ignore = set(ignore_dirs or [])
    ignore.add(".obsidian")
    by_type: dict[str, list[dict]] = {}

    for md_file in vault_path.rglob("*.md"):
        rel = md_file.relative_to(vault_path)
        parts = rel.parts
        if any(p in ignore for p in parts):
            continue
        if parts[0] == "inbox":
            continue

        try:
            post = frontmatter.load(str(md_file))
        except Exception:
            continue

        rec_type = post.metadata.get("type", "")
        if not rec_type:
            continue

        rel_str = str(rel).replace("\\", "/")
        if rel_str.endswith(".md"):
            rel_str = rel_str[:-3]

        by_type.setdefault(rec_type, []).append({
            "path": rel_str,
            "name": md_file.stem,
            "status": str(post.metadata.get("status", "")),
        })

    return {"records_by_type": by_type, "total": sum(len(v) for v in by_type.values())}


def vault_create(
    vault_path: Path,
    record_type: str,
    name: str,
    *,
    set_fields: dict | None = None,
    body: str | None = None,
    scope: str | None = None,
) -> dict:
    """Create a new vault record. Returns {path, warnings}.

    Optional ``scope`` runs ``check_scope`` before the write; default
    ``None`` preserves the historical unrestricted behavior. The same
    ``scope`` is also threaded into ``_validate_type`` so scope-specific
    extension types (Hypatia's ``document`` / ``concept`` / ..., KAL-LE's
    ``pattern`` / ``principle``) pass the type gate. Without this,
    ``_validate_type`` rejects extension types before ``check_scope``
    ever runs — release-blocker for Phase 1 Hypatia create operations.
    """
    _validate_type(record_type, scope=scope)
    set_fields = set_fields or {}
    # Strip reserved frontmatter keys before any downstream processing.
    # Pre-filter rather than post-filter so the scope check sees the
    # ACTUAL frontmatter shape that will land on disk; otherwise
    # ``check_scope`` could approve a write whose payload contradicted
    # the declared scope contract.
    directory = TYPE_DIRECTORY.get(record_type, record_type)
    rel_path_for_log = f"{directory}/{name}.md"
    set_fields = _filter_reserved_keys(
        set_fields, op="vault_create", rel_path=rel_path_for_log,
    )
    if scope is not None:
        check_scope(
            scope,
            "create",
            record_type=record_type,
            frontmatter=set_fields,
            body_write=body is not None,
        )

    # Determine directory and path (already computed above for the
    # filter's log message — reuse so they don't drift).
    rel_path = rel_path_for_log
    file_path = _resolve_vault_path(vault_path, rel_path)

    if file_path.exists():
        raise VaultError(f"File already exists: {rel_path}")

    # Case-insensitive near-match is a hard refusal. This runs before any
    # file write so a duplicate spelling can never land on disk. The caller
    # gets the canonical path in VaultError.details so it can pivot to
    # vault_edit on the existing record.
    near = _check_near_match(vault_path, record_type, name)
    if near is not None:
        canonical_path, message = near
        log.error(
            "vault_create.refused",
            reason="near_match",
            attempted_path=rel_path,
            canonical_path=canonical_path,
        )
        raise VaultError(
            message,
            details={
                "canonical_path": canonical_path,
                "reason": "near_match",
                "attempted_path": rel_path,
            },
        )

    # Load template if available
    template = _load_template(vault_path, record_type)
    if template:
        fm, template_body = template
    else:
        fm = {}
        template_body = f"# {name}\n"

    # Set core fields
    fm["type"] = record_type
    title_field = NAME_FIELD_BY_TYPE.get(record_type, "name")
    fm[title_field] = name
    if "created" not in fm or fm["created"] == "{{date}}":
        fm["created"] = date.today().isoformat()

    # Apply user-provided fields
    for k, v in set_fields.items():
        fm[k] = v

    # Coerce + validate
    _coerce_list_fields(fm)
    _validate_status(record_type, fm.get("status", ""))
    _validate_list_fields(fm)
    _validate_required_fields(fm)

    # Self-supersede rejection — fail fast before any I/O. Mirrors the
    # near-match refusal pattern above (raise VaultError, no on-disk
    # mutation). The zettel_hooks dispatcher post-write also defends
    # against self-supersede (defense-in-depth), but raising here gives
    # the operator a clear error rather than a swallowed warning. The
    # check normalizes both forms — operator may have typed the
    # ``supersedes:`` value as bare path or full wikilink.
    if record_type == "zettel":
        supersedes_raw = fm.get("supersedes")
        if supersedes_raw:
            from .zettel_hooks import _normalize_wikilink_target
            target = _normalize_wikilink_target(supersedes_raw)
            if target:
                if "/" not in target:
                    target = f"zettel/{target}"
                expected_self = f"{directory}/{name}"
                if target == expected_self:
                    raise VaultError(
                        f"Zettel cannot supersede itself: "
                        f"supersedes={supersedes_raw!r} points at the "
                        f"same record being created ({expected_self}). "
                        f"Set supersedes: to the older zettel being "
                        f"replaced, or remove the field if no chain."
                    )

    # Resolve body
    if body is not None:
        final_body = body
        # Append base-view embeds from template so entity records get their
        # Dataview sections even when a custom body is provided.
        if template:
            base_embeds = _extract_base_embeds(template_body, name)
            if base_embeds:
                # Check each embed line individually to avoid false negatives
                # from whitespace differences (blank lines, trailing newlines).
                missing = [
                    line for line in base_embeds.splitlines()
                    if line.strip() and "![[" in line and ".base#" in line
                    and line.strip() not in final_body
                ]
                if missing:
                    final_body = final_body.rstrip("\n") + "\n\n" + "\n".join(missing) + "\n"
    else:
        # Process template body — replace {{title}} and {{date}}
        final_body = template_body.replace("{{title}}", name).replace("{{date}}", date.today().isoformat())

    # Check directory placement (soft warning — still writes)
    warnings: list[str] = []
    dir_warn = _check_directory(record_type, rel_path)
    if dir_warn:
        warnings.append(dir_warn)

    # Check wikilinks
    wl_warns = _check_wikilinks(final_body, fm, vault_path)
    warnings.extend(wl_warns)

    # Write file
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(_serialize_record(fm, final_body), encoding="utf-8")

    # Fire create hooks for event records (Phase A+ vault-ops integration).
    # Anything else (person, project, task, learn types) is a no-op since
    # no hooks are registered for those types in the v1 hook surface.
    #
    # Collect hook return values so the GCal sync status can bubble up
    # into the tool_result the LLM sees (May 2026 silent-fail fix).
    # ``_extract_gcal_sync_status`` returns ``None`` when no GCal action
    # was attempted (disabled, no datetimes, etc.); we only add the
    # ``gcal_sync`` key when there's something concrete to report.
    out: dict = {"path": rel_path, "warnings": warnings}
    if record_type == "event":
        hook_results = _fire_create_hooks(vault_path, rel_path, dict(fm))
        gcal_sync = _extract_gcal_sync_status(hook_results)
        if gcal_sync is not None:
            out["gcal_sync"] = gcal_sync

    # Zettel auto-maintenance hooks (Phase 3 + Phase 4, 2026-05-18).
    # Failure-isolated — every helper catches its own exceptions and
    # logs; vault_create returns success even if a hook fails, because
    # the canonical record IS on disk. Cross-record mirroring is a
    # projection, not part of the create contract.
    #
    # Phase 3 hooks (zettel-only): supersede chain mirror + author
    # Contents append.
    #
    # Phase 4 hook (zettel + source + question + research-pointer):
    # MOC member append. Triggered when the record's ``mocs:``
    # frontmatter list is non-empty. The trigger-type gate lives
    # inside ``dispatch_moc_appends`` so this call site stays
    # type-agnostic.
    # Zettel hook dispatch needs a scope to route writes through the
    # create-allowlist gate. The legacy unrestricted-create path
    # (``scope is None``) is preserved for non-zettelkasten record
    # types, but zettelkasten hooks require a scope. Log + skip rather
    # than silently substituting a single-instance literal — per
    # ``feedback_hardcoding_and_alfred_naming.md`` the fail-loud
    # guarantee must not be weakened by a defensive default.
    _zettel_hook_types = (
        "zettel", "source", "question", "research-pointer",
    )
    if record_type in _zettel_hook_types and scope is None:
        log.warning(
            "vault.zettel_hooks.dispatch_skipped_no_scope",
            rel_path=rel_path,
            record_type=record_type,
            reason="scope_required_for_zettelkasten_hooks",
        )
    elif record_type == "zettel":
        from . import zettel_hooks as _zhooks
        try:
            if fm.get("supersedes"):
                _zhooks.mirror_supersedes_chain(
                    vault_path, rel_path, fm.get("supersedes"), scope=scope,
                )
            if fm.get("author"):
                _zhooks.append_to_author_contents(
                    vault_path, fm.get("author"), rel_path, scope=scope,
                )
            if fm.get("mocs"):
                _zhooks.dispatch_moc_appends(
                    vault_path, rel_path, record_type, fm.get("mocs"),
                    scope=scope,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "vault.zettel_hooks.dispatch_failed",
                rel_path=rel_path,
                error=str(exc),
            )
    elif record_type in ("source", "question", "research-pointer"):
        from . import zettel_hooks as _zhooks
        try:
            if fm.get("mocs"):
                _zhooks.dispatch_moc_appends(
                    vault_path, rel_path, record_type, fm.get("mocs"),
                    scope=scope,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "vault.zettel_hooks.dispatch_failed",
                rel_path=rel_path,
                error=str(exc),
            )

    # Inventory MOC dispatch (Phase 4 Sub-arc B, 2026-05-18). Fires
    # for ``question`` + ``research-pointer`` creates so the
    # ``MOC/_Open Questions.md`` + ``MOC/_Open Research Pointers.md``
    # bullets land on first qualifying create. ``pre_fm=None`` flags
    # a fresh create (truth-table left column = False), so any
    # post-create predicate match → "add". The trigger-type gate
    # lives inside ``dispatch_inventory_mocs`` (iterates the
    # ``INVENTORY_MOC_DISPATCH`` table); this call site stays
    # type-agnostic. Skipped when scope is None (handled by the
    # zettelkasten-hooks-no-scope guard above).
    if record_type in ("question", "research-pointer") and scope is not None:
        from . import zettel_hooks as _zhooks
        try:
            _zhooks.dispatch_inventory_mocs(
                vault_path,
                rel_path,
                record_type,
                pre_fm=None,
                post_fm=dict(fm),
                scope=scope,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "vault.zettel_hooks.dispatch_failed",
                rel_path=rel_path,
                error=str(exc),
            )

    return out


def _apply_body_insert_at(
    body: str, *, marker: str, position: str, content: str,
) -> str:
    """Insert ``content`` before/after the line matching ``marker`` exactly.

    Marker semantics: line-exact match. The whole line (after rstrip)
    must equal ``marker``. Predictable, easy to reason about, no regex
    injection risk; real-world markers are full headings (``## Hardware-
    specific drills``) or distinctive lines.

    Raises ``VaultError`` if:
      - marker not found in body (clean error, body untouched)
      - marker matches multiple lines (ambiguous; operator must use a
        more distinctive marker)
      - position is not "before" or "after"

    Inserts ``content`` as a separate paragraph: a blank line on each
    side (unless the body already has one there). Caller-supplied
    content is inserted verbatim — newline normalization is the
    caller's responsibility.
    """
    if position not in ("before", "after"):
        raise VaultError(
            f"body_insert_at position must be 'before' or 'after', "
            f"got: {position!r}"
        )
    lines = body.split("\n")
    matches = [
        i for i, line in enumerate(lines) if line.rstrip() == marker
    ]
    if not matches:
        raise VaultError(
            f"body_insert_at marker not found (line-exact match): "
            f"{marker!r}"
        )
    if len(matches) > 1:
        raise VaultError(
            f"body_insert_at marker matches {len(matches)} lines "
            f"(ambiguous; pick a more distinctive marker): {marker!r}"
        )
    idx = matches[0]
    insert_at = idx if position == "before" else idx + 1
    # Wrap the insertion in blank-line padding for clean paragraph
    # separation. Strip trailing whitespace so we don't accumulate
    # blanks across repeated insertions.
    block = ["", content.rstrip(), ""]
    new_lines = lines[:insert_at] + block + lines[insert_at:]
    return "\n".join(new_lines)


def vault_edit(
    vault_path: Path,
    rel_path: str,
    *,
    set_fields: dict | None = None,
    append_fields: dict | None = None,
    body_append: str | None = None,
    body_rewriter: Callable[[str], str] | None = None,
    body_insert_at: dict | None = None,
    body_replace: str | None = None,
    scope: str | None = None,
) -> dict:
    """Edit a vault record. Returns {path, fields_changed}.

    Body-mutation surfaces (mutually exclusive — pick at most one per
    call):

      * ``body_append: str`` — add to end of doc. Universal across
        all instances; gated by ``allow_body_writes``.
      * ``body_rewriter: Callable[[str], str]`` — function that takes
        the current body and returns a new body. Used by the telegram
        calibration writer for marker-fenced surgical rewrites; not
        exposed to LLM agents.
      * ``body_insert_at: {marker, position, content}`` — anchored
        mid-document insertion. ``marker`` is a line-exact match
        (full line content, not substring or regex); ``position`` is
        ``"before"`` or ``"after"``. Per-instance × per-type
        allowlist via ``check_scope("body_insert_at", ...)``.
      * ``body_replace: str`` — full body rewrite (frontmatter
        preserved). Per-instance × per-type allowlist via
        ``check_scope("body_replace", ...)``. Salem ``event`` records
        with ``gcal_event_id`` are refused at the scope layer; the
        operator must vault_delete first to clear the GCal mirror.

    Mutual exclusion: at most one body-mutation kwarg per call. If
    multiple are supplied, raises VaultError naming the conflict.
    Reasoning: each shapes the body differently; combining them
    silently would surprise the operator and complicate audit.

    Optional ``scope`` runs ``check_scope`` before the write; default
    ``None`` preserves historical unrestricted behavior.
    """
    # Mutual-exclusion gate FIRST — fail fast before any I/O. The
    # gate covers all four body-mutation surfaces; calibration writer
    # routes via body_rewriter so it inherits the gate too (it never
    # combines with body_append in practice).
    body_mutation_args = {
        "body_append": body_append is not None,
        "body_rewriter": body_rewriter is not None,
        "body_insert_at": body_insert_at is not None,
        "body_replace": body_replace is not None,
    }
    active = [name for name, present in body_mutation_args.items() if present]
    if len(active) > 1:
        raise VaultError(
            f"vault_edit accepts at most ONE body-mutation kwarg per "
            f"call (body_append, body_rewriter, body_insert_at, "
            f"body_replace). Got: {', '.join(active)}. Combine in "
            f"separate edits or pick the surface that matches intent."
        )

    # No-op detection — fail-loud when no mutation param is supplied.
    # Per ``feedback_intentionally_left_blank.md``: silent success on
    # a vault_edit that does nothing is indistinguishable from a real
    # edit landing, and operator-visible only when they later notice
    # the file body didn't change. The Hypatia 2026-05-21 incident
    # (essay-planning conversation ``2026-05-21-depression-checklist-
    # essay-planning-e166d40d.md``) cost ~5 turns of debugging on a
    # vault_edit dispatched with ONLY ``path`` — tool_use input was
    # max_tokens-truncated mid-emission, the ``body_append`` field
    # never arrived. vault_edit returned ``{"path": ..., "fields_
    # changed": []}`` with no error; Salem narrated success while
    # the file body stayed at its pre-edit state.
    #
    # The actionable error names every accepted mutation kwarg so the
    # model can see what failed and retry. This surfaces through the
    # talker dispatcher's tool_result error field (see
    # ``telegram/conversation.py:~2086`` for the dispatch path).
    #
    # ``body_rewriter`` IS counted as a mutation surface here — it
    # writes the body even if the rewriter returns identical content
    # (the ``fields_changed`` check at the write site filters that
    # case). The no-op gate is "did the caller supply ANY mutation
    # intent?", not "did the mutation produce a diff?"
    has_set_fields = bool(set_fields)
    has_append_fields = bool(append_fields)
    has_body_mutation = len(active) >= 1
    if not (has_set_fields or has_append_fields or has_body_mutation):
        raise VaultError(
            "vault_edit called with no mutation parameter — at least "
            "one of set_fields, append_fields, body_append, "
            "body_replace, body_insert_at, body_rewriter is required. "
            "If the tool_use input was truncated mid-emission "
            "(stop_reason=max_tokens), retry with a smaller payload "
            "or split the operation across multiple edits."
        )

    # Strip reserved frontmatter keys before scope check + downstream
    # processing. See ``_filter_reserved_keys`` for the rationale +
    # bug-class history (Hypatia DJ-tracker conversation 2026-05-04).
    if set_fields:
        set_fields = _filter_reserved_keys(
            set_fields, op="vault_edit", rel_path=rel_path,
        )

    file_path = _resolve_vault_path(vault_path, rel_path)
    if not file_path.exists():
        raise VaultError(f"File not found: {rel_path}")

    # Parse FIRST so the body-mutation scope checks (which need the
    # existing frontmatter for the gcal carve-out) have the data they
    # need. The existing ``edit`` scope check below stays in its
    # original position; only the new body-mutation gates need
    # existing_frontmatter.
    fm, body = _parse_record(file_path)
    record_type = fm.get("type", "")

    # Snapshot pre-edit frontmatter for hooks that need to detect
    # transitions (Phase 4 Sub-arc B inventory MOC pattern needs
    # predicate(pre_fm) vs predicate(post_fm) — without the snapshot
    # the in-place ``fm[k] = v`` below loses the pre-edit state).
    # ``dict(fm)`` is a shallow copy — adequate because predicates
    # only read top-level scalar fields like ``status``.
    pre_edit_fm = dict(fm)

    if scope is not None:
        fields_list = (
            list((set_fields or {}).keys()) + list((append_fields or {}).keys())
        )
        body_write_requested = (
            body_append is not None or body_rewriter is not None
            or body_insert_at is not None or body_replace is not None
        )
        check_scope(
            scope,
            "edit",
            rel_path=rel_path,
            fields=fields_list,
            body_write=body_write_requested,
        )
        # Body-mutation tools have their own per-instance × per-type
        # gate (per the c1 matrix). Run only if the corresponding kwarg
        # is set so callers that don't use these tools are unaffected.
        if body_insert_at is not None:
            check_scope(
                scope,
                "body_insert_at",
                rel_path=rel_path,
                record_type=record_type,
                existing_frontmatter=fm,
            )
        if body_replace is not None:
            check_scope(
                scope,
                "body_replace",
                rel_path=rel_path,
                record_type=record_type,
                existing_frontmatter=fm,
            )

    fields_changed: list[str] = []

    # Set fields (overwrite)
    if set_fields:
        for k, v in set_fields.items():
            fm[k] = v
            fields_changed.append(k)

    # Append fields (add to lists)
    if append_fields:
        for k, v in append_fields.items():
            existing = fm.get(k)
            if existing is None:
                fm[k] = [v] if k in LIST_FIELDS else v
            elif isinstance(existing, list):
                existing.append(v)
            else:
                fm[k] = [existing, v]
            fields_changed.append(k)

    # Coerce + validate after edits — re-read record_type in case
    # set_fields touched it.
    _coerce_list_fields(fm)
    record_type = fm.get("type", "")
    if record_type:
        _validate_status(record_type, fm.get("status", ""))
    _validate_list_fields(fm)

    # Body mutation — exactly one of the four surfaces, enforced above.
    if body_replace is not None:
        # Empty-content guard — feature parity with body_insert_at's
        # ``not marker or not content`` check above. An agent calling
        # ``vault_edit body_replace=""`` would silently nuke the
        # entire body; the gate denies with the same shape. Whitespace-
        # only content (e.g. ``" "``) IS allowed — mirrors
        # body_insert_at's contract (``not "  "`` is False, truthy
        # string passes). Operator who wants to set a body to literal
        # whitespace chose that explicitly.
        if not body_replace:
            raise VaultError(
                "body_replace requires non-empty content."
            )
        # Full-body rewrite. Frontmatter preserved (caller's set_fields
        # / append_fields above already applied, but the body string
        # comes wholesale from the caller). Trailing newline added so
        # the file ends cleanly.
        body = body_replace.rstrip("\n") + "\n"
        fields_changed.append("body")
    elif body_insert_at is not None:
        marker = str(body_insert_at.get("marker", "") or "")
        position = str(body_insert_at.get("position", "") or "")
        content = str(body_insert_at.get("content", "") or "")
        if not marker or not content:
            raise VaultError(
                "body_insert_at requires non-empty 'marker' and "
                "'content' fields."
            )
        body = _apply_body_insert_at(
            body, marker=marker, position=position, content=content,
        )
        fields_changed.append("body")
    elif body_append:
        # Append to body
        body = body.rstrip() + "\n\n" + body_append + "\n"
        fields_changed.append("body")
    elif body_rewriter is not None:
        # Rewrite body (wk3 commit 7 — calibration writer's marker-fenced
        # surgical rewrite path).
        new_body = body_rewriter(body)
        if new_body != body:
            body = new_body
            if "body" not in fields_changed:
                fields_changed.append("body")

    # Write back
    file_path.write_text(_serialize_record(fm, body), encoding="utf-8")

    # Fire update hooks for event records (Phase A+ vault-ops integration).
    # Pass the post-edit ``fm`` so the hook sees what the file now
    # holds, not the pre-edit state. The hook closure (registered by
    # the talker daemon) decides whether to PATCH / PROMOTE / CANCEL /
    # no-op based on the post-edit state — see
    # :func:`register_event_update_hook` for the four branches.
    #
    # Collect hook return values so the GCal sync status can bubble up
    # into the tool_result the LLM sees (May 2026 silent-fail fix:
    # vault edit succeeded, GCal failed with auth_failed, Salem
    # narrated phantom "GCal updated" success because the tool_result
    # carried no GCal-failure signal). ``_extract_gcal_sync_status``
    # returns ``None`` when no GCal action was attempted (no-op hook
    # branch); we only add the ``gcal_sync`` key when there's
    # something concrete to report.
    out: dict = {"path": rel_path, "fields_changed": fields_changed}
    if record_type == "event":
        hook_results = _fire_update_hooks(
            vault_path, rel_path, dict(fm), list(fields_changed),
        )
        gcal_sync = _extract_gcal_sync_status(hook_results)
        if gcal_sync is not None:
            out["gcal_sync"] = gcal_sync

    # Zettel auto-maintenance hooks (Phase 3 + Phase 4, 2026-05-18).
    # Only fires when the relevant fields changed in THIS edit —
    # re-runs against the same value are no-ops at the helper level,
    # but skipping the dispatch entirely when fields_changed doesn't
    # include them avoids cascading vault_edit calls on every minor
    # edit. Failure-isolated to match vault_create's contract.
    #
    # Phase 4 (MOC member append) fires on the four trigger types
    # (zettel + source + question + research-pointer) when ``mocs``
    # is in fields_changed AND the post-edit value is non-empty.
    # The trigger-type gate lives inside ``dispatch_moc_appends`` so
    # this call site only needs to filter on fields_changed.
    # Zettel hook dispatch needs a scope to route writes through the
    # create-allowlist gate. The legacy unrestricted-edit path
    # (``scope is None``) is preserved for non-zettelkasten record
    # types, but zettelkasten hooks require a scope. Log + skip rather
    # than silently substituting a single-instance literal — mirrors
    # the same guard in ``vault_create`` above.
    _zettel_hook_types = (
        "zettel", "source", "question", "research-pointer",
    )
    if record_type in _zettel_hook_types and scope is None:
        log.warning(
            "vault.zettel_hooks.dispatch_skipped_no_scope",
            rel_path=rel_path,
            record_type=record_type,
            reason="scope_required_for_zettelkasten_hooks",
        )
    elif record_type == "zettel":
        from . import zettel_hooks as _zhooks
        try:
            if "supersedes" in fields_changed and fm.get("supersedes"):
                _zhooks.mirror_supersedes_chain(
                    vault_path, rel_path, fm.get("supersedes"), scope=scope,
                )
            if "author" in fields_changed and fm.get("author"):
                _zhooks.append_to_author_contents(
                    vault_path, fm.get("author"), rel_path, scope=scope,
                )
            if "mocs" in fields_changed and fm.get("mocs"):
                _zhooks.dispatch_moc_appends(
                    vault_path, rel_path, record_type, fm.get("mocs"),
                    scope=scope,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "vault.zettel_hooks.dispatch_failed",
                rel_path=rel_path,
                error=str(exc),
            )
    elif record_type in ("source", "question", "research-pointer"):
        from . import zettel_hooks as _zhooks
        try:
            if "mocs" in fields_changed and fm.get("mocs"):
                _zhooks.dispatch_moc_appends(
                    vault_path, rel_path, record_type, fm.get("mocs"),
                    scope=scope,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "vault.zettel_hooks.dispatch_failed",
                rel_path=rel_path,
                error=str(exc),
            )

    # Inventory MOC dispatch (Phase 4 Sub-arc B, 2026-05-18). Fires
    # on every edit of ``question`` / ``research-pointer`` records
    # so status transitions can trigger add/remove against the
    # corresponding inventory MOC. The predicate-pre vs predicate-
    # post diff inside ``dispatch_inventory_mocs`` short-circuits
    # the no-transition case to a "skipped" count (no write), so
    # editing an unrelated field on a question is cheap.
    #
    # Unlike the topic-MOC dispatch above, this is NOT gated on
    # ``fields_changed`` — the predicate only depends on ``status``,
    # but we run the dispatch on every edit to be defensive against
    # future predicates that may depend on multiple fields. The
    # per-call cost is one predicate evaluation per matching
    # dispatch entry; cheap. Skipped when scope is None (handled by
    # the zettelkasten-hooks-no-scope guard above).
    if record_type in ("question", "research-pointer") and scope is not None:
        from . import zettel_hooks as _zhooks
        try:
            _zhooks.dispatch_inventory_mocs(
                vault_path,
                rel_path,
                record_type,
                pre_fm=pre_edit_fm,
                post_fm=dict(fm),
                scope=scope,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "vault.zettel_hooks.dispatch_failed",
                rel_path=rel_path,
                error=str(exc),
            )

    return out


def vault_move(
    vault_path: Path,
    from_path: str,
    to_path: str,
    *,
    scope: str | None = None,
) -> dict:
    """Move a vault record. Returns {from, to}.

    When Obsidian is running, uses the Obsidian CLI which automatically
    updates all wikilinks across the vault that reference the moved file.

    Optional ``scope`` runs ``check_scope`` before the move.
    """
    if scope is not None:
        check_scope(scope, "move", rel_path=from_path)

    src = _resolve_vault_path(vault_path, from_path)
    dst = _resolve_vault_path(vault_path, to_path)

    if not src.exists():
        raise VaultError(f"Source not found: {from_path}")
    if dst.exists():
        raise VaultError(f"Destination already exists: {to_path}")

    # Try Obsidian CLI — updates wikilinks vault-wide
    if obsidian.is_available():
        src_name = from_path.removesuffix(".md")
        if obsidian.move_file(src_name, to_path):
            return {"from": from_path, "to": to_path, "wikilinks_updated": True}

    # Filesystem fallback — no wikilink updates
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)

    return {"from": from_path, "to": to_path}


def vault_delete(
    vault_path: Path,
    rel_path: str,
    *,
    scope: str | None = None,
) -> dict:
    """Delete a vault record. Returns {path, deleted}.

    When Obsidian is running, uses the Obsidian CLI which respects the
    configured deletion behavior (system trash, Obsidian trash, or permanent).

    Optional ``scope`` runs ``check_scope`` before the delete.
    """
    if scope is not None:
        check_scope(scope, "delete", rel_path=rel_path)

    file_path = _resolve_vault_path(vault_path, rel_path)
    if not file_path.exists():
        raise VaultError(f"File not found: {rel_path}")

    # Capture frontmatter BEFORE the delete so the event-delete hook
    # has access to ``gcal_event_id`` (needed for the GCal-side delete;
    # can't read it once the file is gone). Defensive try/except — a
    # malformed file shouldn't block the delete; we just skip the hook
    # for that record.
    pre_delete_fm: dict = {}
    try:
        pre_delete_fm, _ = _parse_record(file_path)
    except Exception as exc:  # noqa: BLE001
        log.debug(
            "vault_delete.pre_delete_fm_parse_failed",
            rel_path=rel_path,
            error=str(exc),
        )

    record_type = pre_delete_fm.get("type", "")

    # Local helper: build the delete return dict, surfacing gcal_sync
    # when the hook reported something concrete. Same shape as
    # vault_create / vault_edit returns. Lifted into a closure so both
    # the Obsidian-CLI path and the filesystem-fallback path go through
    # the identical extraction, no copy-paste drift.
    def _build_delete_result() -> dict:
        result: dict = {"path": rel_path, "deleted": True}
        if record_type == "event":
            hook_results = _fire_delete_hooks(
                vault_path, rel_path, dict(pre_delete_fm),
            )
            gcal_sync = _extract_gcal_sync_status(hook_results)
            if gcal_sync is not None:
                result["gcal_sync"] = gcal_sync
        return result

    # Try Obsidian CLI — respects user's trash settings
    if obsidian.is_available():
        file_name = rel_path.removesuffix(".md")
        if obsidian.delete_file(file_name):
            return _build_delete_result()

    # Filesystem fallback — permanent delete
    file_path.unlink()
    return _build_delete_result()
