"""Shared vault schema constants — record types, statuses, field definitions."""

from __future__ import annotations

# --- Known record types and their valid statuses ---

KNOWN_TYPES: set[str] = {
    "project", "task", "session", "input", "person", "org",
    "location", "note", "decision", "process", "run", "event",
    "account", "asset", "conversation", "assumption", "constraint",
    "contradiction", "synthesis",
}

# Stage 3.5: record types KAL-LE uses inside ``~/aftermath-lab/``. Kept
# in a separate set so the canonical ``KNOWN_TYPES`` (Salem's
# operational world) stays focused. The kalle scope check (see
# ``vault/scope.py::KALLE_CREATE_TYPES``) intersects these with its
# own create allowlist. ``pattern`` is a reusable development pattern;
# ``principle`` is a higher-level development principle.
KNOWN_TYPES_KALLE: set[str] = {
    "pattern", "principle", "architecture",
}
# ``architecture`` (added 2026-05-04) — multi-instance system design +
# information-sharing decisions. Descriptive of THIS system (vs
# ``pattern`` which is reusable how-to extracted FROM the system).
# Examples: architecture/canonical-authority.md,
# architecture/PHI-firewall-design.md, architecture/peer-protocol.md.
# KAL-LE-only — Salem and Hypatia have no use case for this type.
# aftermath-lab already has an architecture/ directory with operational
# docs (deployment.md, testing.md); this registration adds schema
# validation + scope-aware tooling to records placed there.

# Hypatia operates inside ``~/library-alexandria/`` (see
# ``library-alexandria/CLAUDE.md`` for directory layout + frontmatter
# shapes). Like the kalle set, kept separate from ``KNOWN_TYPES`` so
# Salem's operational vault doesn't gain Hypatia-only types. The
# ``hypatia`` scope check (see ``vault/scope.py::HYPATIA_CREATE_TYPES``)
# is the authoritative create allowlist.
#
# Phase 2.5 fiction posture (``project_hypatia_phase2_followups.md``):
# six ``fiction-{element}`` types added so both scaffolding paths
# (the slash command + the SKILL natural-language path) can call
# ``vault_create`` for fiction records — the slash command writes
# the directory + 5 element files atomically, but ongoing work
# (a new character record, a re-keyed structure file) goes through
# regular ``vault_create``. Without these types in the registry,
# every such write fails ``_validate_type``.
KNOWN_TYPES_HYPATIA: set[str] = {
    "document", "concept", "source", "citation", "template",
    "fiction-continuity", "fiction-story", "fiction-structure",
    "fiction-world", "fiction-voice", "fiction-character",
    # Practice-session (2026-05-06) — cross-domain practice logging
    # (DJ practice, fencing, workouts, future skill-building tracks).
    # Distinct from generic ``session`` records: practice-sessions
    # link to a skill tracker / project + carry a domain field so
    # progression aggregates over time. Filed 2026-05-04 from the DJ
    # skill-building arc (see ``note/DJ Skill Mastery Tracker.md``);
    # surfaced again in Hypatia conversation ``833bec8d`` when Andrew
    # asked for it and the type didn't exist yet. See scope.py
    # ``HYPATIA_CREATE_TYPES`` + the body-mutation matrix entry for
    # the per-instance gating.
    "practice-session",
    # Voice/method training (2026-05-07, /train + /method-source arc).
    # Four new top-level types — registered here so the Hypatia create
    # allowlist can admit them. The shape:
    #   - ``essay``        — raw published essay, lands at
    #                        ``document/essay/<slug>.md``. Distinct
    #                        from a generic ``note`` because the routing
    #                        is type-driven (TYPE_DIRECTORY entry below);
    #                        the f006c48e routing bug landed because
    #                        ``vault_create type=note`` was the outer
    #                        call and the inner ``type: essay`` in
    #                        set_fields was overridden by ops. Adding
    #                        ``essay`` as a first-class type fixes it.
    #   - ``voice``        — leaf voice profile, lands at
    #                        ``voice/<slug>.md``. One per source essay;
    #                        carries optional ``cluster`` frontmatter
    #                        for grouping into cluster-summary tier.
    #   - ``voice-cluster`` — cluster-tier voice summary, lands at
    #                        ``voice/cluster/<name>.md``. Built async
    #                        when ≥2 leaves share a ``cluster:`` tag.
    #   - ``method``       — method/system profile, lands at
    #                        ``method/<slug>.md``. Structured extraction
    #                        of a method document (paired with a raw
    #                        ``source`` record).
    # ``source`` (already in this set) acts as the leaf for /method-source.
    "essay", "voice", "voice-cluster", "method",
}


# Per-scope union of known record types. ``vault.ops._validate_type``
# uses this to gate ``vault_create`` / ``vault_list`` against the right
# type set: a Hypatia agent legitimately creates ``document`` records
# (canonical-vault-only ``KNOWN_TYPES`` would reject them); a Salem
# agent must NOT be able to create ``pattern`` records (KAL-LE-only).
#
# Scopes not listed here fall back to the canonical ``KNOWN_TYPES``
# only. The dict's purpose is "which extension sets does this scope
# unlock?" — not "what may this scope create?" (that's the create
# allowlists in ``vault.scope`` — ``KALLE_CREATE_TYPES``,
# ``HYPATIA_CREATE_TYPES``, ``TALKER_CREATE_TYPES``). Two-layer check:
# this gate lets the type through ``_validate_type``; the create
# allowlist in ``check_scope`` then enforces the per-scope policy.
#
# Pattern-trigger note: when a future instance (V.E.R.A., STAY-C)
# adds its own ``KNOWN_TYPES_<NAME>`` set, also add an entry here —
# otherwise ``_validate_type`` will silently reject the new types
# before scope enforcement gets a chance to run. See CLAUDE.md
# "Vault Operations Layer" for the rationale.
KNOWN_TYPES_BY_SCOPE: dict[str, set[str]] = {
    "kalle": KNOWN_TYPES | KNOWN_TYPES_KALLE,
    "hypatia": KNOWN_TYPES | KNOWN_TYPES_HYPATIA,
}


LEARN_TYPES: set[str] = {
    "assumption", "decision", "constraint", "contradiction", "synthesis",
}

STATUS_BY_TYPE: dict[str, set[str]] = {
    "project": {"active", "paused", "completed", "abandoned", "proposed"},
    "task": {"todo", "active", "blocked", "done", "cancelled"},
    "session": {"active", "completed"},
    "input": {"unprocessed", "processed", "deferred"},
    "person": {"active", "inactive"},
    "org": {"active", "inactive"},
    "location": {"active", "inactive"},
    "note": {"draft", "active", "living", "review", "final"},
    "decision": {"draft", "final", "superseded", "reversed"},
    "process": {"active", "proposed", "design", "deprecated"},
    "run": {"active", "completed", "blocked", "cancelled"},
    "event": set(),  # no status constraint
    "account": {"active", "suspended", "closed", "pending"},
    "asset": {"active", "retired", "maintenance", "disposed"},
    "conversation": {"active", "waiting", "resolved", "closed", "archived"},
    "assumption": {"active", "challenged", "invalidated", "confirmed"},
    "constraint": {"active", "expired", "waived", "superseded"},
    "contradiction": {"unresolved", "resolved", "accepted"},
    "synthesis": {"draft", "active", "superseded"},
    # KAL-LE-only ``architecture`` records (multi-instance system
    # design notes). Same status set as synthesis — drafts evolve,
    # become active when ratified, get superseded when the design
    # changes. Strict-but-small set; widen via deliberate decision
    # if a real workflow needs another state.
    "architecture": {"draft", "active", "superseded"},
    # Hypatia-only ``practice-session`` records (cross-domain
    # practice logging — DJ / fencing / workout / language / etc.).
    # Status set covers the natural workflow: planned (scheduled
    # ahead of time), in_progress (mid-session, e.g. live practice
    # update), completed (most common — written after the session),
    # skipped (intended-to-do but didn't, useful signal for the
    # tracker aggregator).
    "practice-session": {"planned", "in_progress", "completed", "skipped"},
    # Voice/method training types (2026-05-07).
    # ``essay`` — raw published essay records. Statuses match the
    # operator's natural workflow: drafting → published → archived.
    # The f006c48e example used ``status: published``; that's the
    # most common state (these get registered AFTER publication).
    "essay": {"draft", "published", "archived"},
    # ``voice`` — leaf voice profiles. Pending = extraction queued
    # but not yet completed by the worker; active = profile written;
    # superseded = a re-extraction replaced this record (kept for
    # audit, not deleted). Failed = extraction worker hit an error
    # and the operator should re-run the slash command.
    #
    # Intentionally-left-blank sentinels (2026-05-07 prompt-tuner pass):
    #   ``insufficient-evidence`` — leaf voice extraction: input was
    #     too thin / fragmentary to extract a real voice profile.
    #   ``no-overall-invariants`` — overall voice profile (also a
    #     ``voice`` record by type — see maybe_rebuild_overall):
    #     cluster summaries genuinely diverge, no real always_true
    #     items emerged.
    # Both pinned here so the writer can pass through the LLM-emitted
    # status WITHOUT _validate_status rejecting it. Per the
    # ``intentionally left blank`` rule, silent absence (i.e. dropping
    # the sentinel and substituting ``active``) is forbidden — the
    # operator must SEE that extraction emitted "no signal" rather
    # than reading a fabricated profile that fills the schema.
    "voice": {
        "pending", "active", "superseded", "failed",
        "insufficient-evidence", "no-overall-invariants",
    },
    # ``voice-cluster`` — aggregated cluster summaries built by the
    # async cluster-summary builder. Status flips to ``stale`` when a
    # new leaf with the same cluster tag lands (the next builder
    # tick rewrites it back to ``active``).
    # ``incoherent-cluster`` (2026-05-07) — same intentionally-left-
    # blank pattern as voice's insufficient-evidence: the leaves under
    # one cluster tag don't actually share a recognisable posture.
    "voice-cluster": {"active", "stale", "incoherent-cluster"},
    # ``method`` — structured method/system profiles, paired with a
    # raw ``source`` record. Same status set as voice (extraction is
    # the same async-worker shape).
    # ``not-a-method`` (2026-05-07) — intentionally-left-blank exit:
    # the source isn't actually a method (opinion essay / anecdote /
    # ramble that doesn't formalise into principles + procedure).
    "method": {
        "pending", "active", "superseded", "failed",
        "not-a-method",
    },
}

# Type → expected top-level directory
TYPE_DIRECTORY: dict[str, str] = {
    "project": "project",
    "task": "task",
    "person": "person",
    "org": "org",
    "location": "location",
    "note": "note",
    "decision": "decision",
    "process": "process",
    "run": "run",
    "event": "event",
    "account": "account",
    "asset": "asset",
    "conversation": "conversation",
    "assumption": "assumption",
    "constraint": "constraint",
    "contradiction": "contradiction",
    "synthesis": "synthesis",
    "architecture": "architecture",
    # Hypatia practice-session records — typically land at
    # ``practice-session/<title>.md`` per the per-type-directory
    # convention. The writer (``vault_create``) routes them via this
    # entry; the operator can move them post-create if a different
    # tree (e.g. ``practice-session/dj/<title>.md``) makes sense for
    # a particular skill domain.
    "practice-session": "practice-session",
    # Voice/method training types (2026-05-07). Each routes to its
    # own top-level directory:
    #   - essay         → document/essay/<slug>.md  (nested under
    #                     ``document/`` because essays are a kind of
    #                     finished document; matches the f006c48e
    #                     operator-set frontmatter ``path: document/
    #                     essay/...`` that the routing bug exposed)
    #   - voice         → voice/<slug>.md
    #   - voice-cluster → voice/cluster/<slug>.md  (sub-path; the
    #                     cluster summaries live under voice/ so
    #                     Obsidian's tree view groups them with the
    #                     leaf profiles they aggregate)
    #   - method        → method/<slug>.md
    # ``source`` (Hypatia type) keeps the default ``source/`` directory
    # via TYPE_DIRECTORY.get(record_type, record_type) fallback — no
    # explicit entry needed here.
    "essay": "document/essay",
    "voice": "voice",
    "voice-cluster": "voice/cluster",
    "method": "method",
    # Hypatia ``template`` records — prose-form scaffolds (essay
    # scaffolds, reusable section structures, etc.). Routed to
    # ``prose-templates/`` to disambiguate from Obsidian's per-type
    # ``_templates/`` directory (the scaffold/_templates layer shipped
    # with the bundled vault contains placeholder-bearing per-record-
    # type markdown templates; Hypatia's ``template`` type is a
    # different concept entirely — operator-curated prose forms).
    # Latent orphan-path bug fixed 2026-05-12: SKILL was renamed
    # ``template/`` → ``prose-templates/`` in ``a14e0ab`` (vault-side
    # ``mv`` performed by team-lead), but ``TYPE_DIRECTORY`` had no
    # entry so the ``.get(record_type, record_type)`` fallback routed
    # writes to the now-empty ``template/`` orphan directory.
    "template": "prose-templates",
    # session, input have flexible placement
}

# Frontmatter field names that carry instructor directives. Part of the
# alfred_instructions watcher contract:
#   - ``alfred_instructions`` — pending queue. Each entry is a directive
#     string the instructor daemon picks up and executes.
#   - ``alfred_instructions_last`` — completed archive. Each entry is a
#     dict of ``{text, executed_at, result}`` describing the directive
#     and its outcome.
INSTRUCTION_FIELDS: tuple[str, ...] = (
    "alfred_instructions",
    "alfred_instructions_last",
)

# Optional frontmatter fields on ``task`` records that carry reminder
# state. Part of the outbound-push transport contract:
#   - ``remind_at``     — pending reminder timestamp (ISO 8601, UTC).
#     When present and in the past, the transport scheduler fires a
#     reminder via Telegram.
#   - ``reminded_at``   — set by the scheduler on successful dispatch.
#     Clears ``remind_at``. Updating ``remind_at`` to a later value
#     (where ``reminded_at < remind_at``) re-arms the reminder.
#   - ``reminder_text`` — optional verbatim text that overrides the
#     default ``"Reminder: {title} (due {due})"`` template.
#
# Values are date or datetime when written from Python; the scheduler
# tolerates ISO-string, date-only, and tz-aware timestamps. None of
# these fields are required — they are opt-in per task.
REMINDER_FIELDS: tuple[str, ...] = (
    "remind_at",
    "reminded_at",
    "reminder_text",
)

# Optional frontmatter fields on ``event`` records that interact with
# the Google Calendar sync layer. Per ``project_inter_instance_communication``
# (Phase A+) GCal events are a projection of vault records — these
# fields steer the projection without changing the canonical record:
#
#   - ``gcal_event_id``       — GCal event ID written back by the sync
#     layer after a successful create. Used by the update / cancel
#     hooks as the patch / delete target. Operators MUST NOT set this
#     by hand; it's a sync-layer artifact.
#   - ``gcal_calendar``       — short label for the destination calendar
#     (e.g. ``"alfred"``, ``"rrts"``, ``"stayc"``). Config-driven
#     per-instance via ``GCalConfig.alfred_calendar_label``.
#   - ``gcal_keep_on_cancel`` — bool; when true, a cancel edit
#     (``status: cancelled``) PATCHes the GCal event to
#     ``status="cancelled"`` (struck-through, kept on calendar) instead
#     of deleting it. Off by default — most cancellations should remove
#     the calendar entry entirely.
#   - ``gcal_title``          — operator-set override for the GCal
#     event title (vault filename / ``name`` stays as-is). Decouples
#     vault-side disambiguator suffixes (e.g. ``"Novaket — May 13"``
#     for filename uniqueness) from the GCal entry the user actually
#     sees on their phone (just ``"Novaket"`` — GCal already shows the
#     date in its own UI). Optional, no auto-derivation: when absent,
#     the sync layer falls back to the existing
#     ``fm.title or fm.name`` chain (regression-safe). Operators
#     populate via ``vault_edit`` set; the create / update / promote
#     hooks pick it up automatically. See
#     ``alfred.integrations.gcal_sync.resolve_gcal_title`` for the
#     resolution helper.
#
# All four are opt-in per record. None are required.
EVENT_GCAL_FIELDS: tuple[str, ...] = (
    "gcal_event_id",
    "gcal_calendar",
    "gcal_keep_on_cancel",
    "gcal_title",
)

# Fields that should be lists
LIST_FIELDS: set[str] = {
    "tags", "aliases", "related", "relationships", "participants",
    "outputs", "depends_on", "blocked_by", "based_on", "supports",
    "challenged_by", "approved_by", "confirmed_by", "invalidated_by",
    "cluster_sources", "governed_by", "references", "project",
    # Instruction fields — both are lists (pending queue + executed archive).
    "alfred_instructions", "alfred_instructions_last",
    # Practice-session (2026-05-06) — list of skills worked on during
    # the session. The ``related_persons`` / ``related_orgs`` /
    # ``related_projects`` fields are also list-shaped on
    # practice-session records but are written as lists by every
    # producer in the wild (surveyor + the new template), so they
    # don't need coerce-from-scalar handling. ``skills_practiced`` is
    # genuinely new — operators may type a single skill as a string
    # and rely on the create-time coerce.
    "skills_practiced",
}

# Required fields for all records
REQUIRED_FIELDS: list[str] = ["type", "created"]

# Types that use "subject" instead of "name" as their title field
NAME_FIELD_BY_TYPE: dict[str, str] = {
    "conversation": "subject",
    "input": "subject",
}

# Record types that are terminal-by-design — no other record is
# expected to point at them, so they should NOT fire ORPHAN001 just
# for having zero inbound wikilinks.
#
# Conservative starting set, validated against the 2026-04-30 residual
# categorization (`project_distiller_janitor_sweep_log.md` "Janitor
# 1182 residual categorization"):
#
#   - ``note``: 258 of the 360 ORPHAN001 entries lived under ``note/``.
#     Notes are mostly captured emails / one-off jottings — orphan-
#     by-nature. The few that DO get linked already register inbound;
#     this rule just stops surfacing the rest as actionable issues.
#   - ``run``: 15 entries; all Morning Briefs / daily-output records.
#     Terminal by design — nothing in the vault is expected to link
#     to a specific run.
#
# 2026-05-06 expansion (epistemic types) — driven by ORPHAN001
# residual breakdown (38 epistemic of 91 total: 32 synthesis,
# 3 contradiction, 1 decision, 1 constraint, 1 assumption):
#
#   - ``synthesis`` / ``contradiction`` / ``decision`` / ``assumption``
#     / ``constraint``: distiller-generated learnings extracted FROM
#     source records. Each carries a forward reference via the
#     ``source_links`` field pointing back to the source(s); the
#     operator's "is this learning real?" check works via that
#     forward-link lookup, NOT via inbound walk. Adding back-references
#     would require mutating the SOURCE record on every distiller
#     fire, which breaks the deterministic-writer principle the
#     distiller rebuild ratified (each fire writes new records, never
#     touches existing source records). Earlier comment said "Andrew
#     may want to see synthesis flagged" — the actual signal Andrew
#     needs is "operational-record orphans" (task/person/org/event
#     gaps), and ORPHAN001 surfacing 38 epistemic records was noise
#     that buried the 53 operational orphans that actually matter.
#
# Deliberately omitted (separate policy decisions):
#   - ``task``: 34 entries, mixed bag. Some real (sub-task hierarchies),
#     some terminal — needs a different rule (link-by-status?).
#
# Adding a new type here should be backed by data showing the type is
# overwhelmingly terminal — don't generalize from one example.
LEAF_TYPES: set[str] = {
    "note",
    "run",
    # Epistemic types — distiller-generated, source_links is the
    # forward reference; back-refs would require mutating source
    # records on every distiller fire (breaks deterministic-writer
    # principle).
    "synthesis",
    "contradiction",
    "decision",
    "assumption",
    "constraint",
}
