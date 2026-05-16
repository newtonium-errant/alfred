"""Per-agent scope enforcement for vault operations."""

from __future__ import annotations

from .schema import KNOWN_TYPES_KALLE, LEARN_TYPES


class ScopeError(Exception):
    """Raised when an operation is denied by scope policy."""


# ---------------------------------------------------------------------------
# Body-mutation tools — per-instance × per-type allowlist matrix
# ---------------------------------------------------------------------------
#
# Three body-mutation surfaces on ``vault_edit``:
#
#   1. ``body_append``      — add to end of doc (existing; gated by the
#                             binary ``allow_body_writes`` flag below).
#   2. ``body_insert_at``   — anchored mid-document insertion (NEW).
#                             Per-type allowlist via
#                             ``allow_body_insert_at`` dict.
#   3. ``body_replace``     — full body rewrite (NEW). Higher-risk —
#                             universally OFF by default; per-type
#                             allowlist via ``allow_body_replace`` dict.
#
# Design philosophy (Andrew 2026-05-04): scope-first, NOT "ship
# universal then restrict." Every per-type cell is an explicit
# decision in the matrix below. Adding a new mutation surface in the
# future means extending this matrix — not bolting another tool on
# top.
#
# Per-instance × per-type matrix (the principal artifact):
#
# | Caller        | append    | insert_at                        | replace                       |
# |---------------|-----------|----------------------------------|-------------------------------|
# | hypatia       | universal | note,concept,document,           | same set MINUS                |
# |               |           | template,fiction-*,              | practice-session (history     |
# |               |           | practice-session                 | preservation)                 |
# | talker(Salem) | universal | note,task,event(no-gcal-id)      | same — but refuses if         |
# |               |           |                                  | event has gcal_event_id       |
# | kalle         | universal | note,decision,principle,         | same set                      |
# |               |           | pattern (decisions stay rare)    |                               |
# | janitor       | universal | * (stub-flesh-out workflows)     | DENIED (autofix-loop risk)    |
# | janitor_enrich| universal | DENIED (Stage 3 only writes      | DENIED                        |
# |               |           | structured fields; bodies        |                               |
# |               |           | append-only via existing path)   |                               |
# | distiller     | universal | DENIED                           | DENIED                        |
# | curator       | universal | DENIED                           | DENIED                        |
# | surveyor      | universal | DENIED                           | DENIED                        |
# | instructor    | universal | * (operator-driven, trusted)     | * (operator-driven, trusted)  |
#
# ``"*"`` in an allowlist means "any type allowed" — distinct from
# the absence of a key (which means "no type allowed for this
# instance"). The empty dict ``{}`` and the missing key both deny
# all types.
#
# Universally-denied types — auto-generated/atomic records. These
# refuse body_insert_at AND body_replace under EVERY scope, regardless
# of the per-instance allowlist. An instance putting one of these in
# its allowlist still gets denied at the type-gate. Mutation here =
# history corruption (transcripts) or learning-record contradiction
# (epistemic atoms).
_BODY_MUTATE_DENIED_TYPES: frozenset[str] = frozenset({
    # Auto-generated transcripts / event records.
    "session", "conversation", "capture", "run", "input",
    # Atomic learning records — assumption/decision/constraint/
    # contradiction/synthesis. Mutation here would silently rewrite
    # an epistemic atom that other records cite. The right path for
    # changing a learning record is a NEW assumption/decision that
    # supersedes the old one (distiller's natural workflow).
    "assumption", "decision", "constraint", "contradiction", "synthesis",
})


def _check_body_mutation_allowed(
    *,
    operation: str,
    scope: str,
    record_type: str,
    allowlist: dict[str, bool] | None,
    existing_frontmatter: dict | None,
) -> None:
    """Shared gate for body_insert_at and body_replace.

    Refuses if:
      - record_type is in the universally-denied set (auto-generated
        / atomic — never mutate via these tools).
      - allowlist is None or empty (instance hasn't opted in to this
        operation at all).
      - record_type is not in the allowlist AND ``"*"`` wildcard
        absent.
      - ``operation == "body_replace"``, record_type is ``event``,
        and the existing record has ``gcal_event_id`` set (would lose
        GCal sync state on rewrite). The operator must vault_delete
        the event first — which fires the GCal cancel hook and
        properly removes the calendar mirror — before any body
        rewrite. Refuse-at-scope keeps the contract centralised; the
        sync-layer-preserves alternative would have to live in
        every future sync hook.

    Raises ``ScopeError`` with a message naming the rule + the
    operator-actionable next step.
    """
    if record_type in _BODY_MUTATE_DENIED_TYPES:
        raise ScopeError(
            f"Operation '{operation}' is universally denied for "
            f"record type '{record_type}' (auto-generated or atomic — "
            f"mutation would corrupt history or contradict cited "
            f"epistemic content). The right path is a new record that "
            f"supersedes this one, not a body rewrite."
        )
    if not allowlist:
        raise ScopeError(
            f"Scope '{scope}' has no allowlist configured for "
            f"'{operation}' — operation not enabled for this instance."
        )
    if record_type not in allowlist and "*" not in allowlist:
        permitted = sorted(allowlist.keys())
        raise ScopeError(
            f"Scope '{scope}' may not '{operation}' on type "
            f"'{record_type}'. Permitted types: "
            f"{', '.join(permitted) if permitted else '(none)'}."
        )
    # gcal carve-out — Salem event with a synced GCal mirror.
    if (
        operation == "body_replace"
        and record_type == "event"
        and isinstance(existing_frontmatter, dict)
        and existing_frontmatter.get("gcal_event_id")
    ):
        raise ScopeError(
            f"Scope '{scope}' refuses 'body_replace' on event records "
            f"with a synced GCal mirror (gcal_event_id present). "
            f"Rewriting the body could lose sync state. To proceed, "
            f"first vault_delete the event (fires the GCal cancel "
            f"hook and removes the calendar mirror cleanly), then "
            f"vault_create the replacement."
        )


# Operation → {scope: checker_function}
# Checkers receive (operation, rel_path, record_type) and raise ScopeError if denied.

SCOPE_RULES: dict[str, dict[str, bool | str | set[str]]] = {
    "curator": {
        "read": True,
        "search": True,
        "list": True,
        "context": True,
        "create": True,
        "edit": True,
        "move": "inbox_only",
        "delete": False,
        # Curator writes full record bodies at creation time and during
        # enrichment. Body writes stay allowed.
        "allow_body_writes": True,
        # body_insert_at + body_replace DENIED — curator's writes happen
        # via vault_create (full body at creation) or vault_edit's
        # body_append for late additions. Mid-document insertion and
        # full rewrites are operator-only territory; an autonomous
        # curator mutating canonical record bodies mid-document would
        # corrupt user-authored content.
        "allow_body_insert_at": {},
        "allow_body_replace": {},
    },
    "janitor": {
        "read": True,
        "search": True,
        "list": True,
        "context": True,
        # Janitor may create task records only when they carry the
        # alfred_triage: true frontmatter flag (Layer 3 triage queue).
        "create": "triage_tasks_only",
        # Stage 1/2 (autofix + link repair) narrows frontmatter writes to
        # a deliberate allowlist. Any field outside this set is rejected
        # by ``check_scope`` so the janitor can't mutate user-authored
        # content. Stage 3 enrichment runs under the separate
        # ``janitor_enrich`` scope below with its own allowlist.
        "edit": "field_allowlist",
        "edit_fields_allowlist": {
            "janitor_note",
            "type", "status",              # FM002/FM003 autofix
            "name", "subject",             # FM001 title
            "created",                     # FM001 mtime
            "related",                     # LINK002 autofix, DUP001 retargeting
            "tags",                        # FM004 scalar→list coercion
            "alfred_triage", "alfred_triage_kind", "alfred_triage_id",
            "candidates", "priority",      # triage task creation
        },
        "move": False,
        "delete": True,
        # Q3 body-write loophole: the field allowlist above gates
        # set_fields/append_fields on frontmatter, but ``vault edit``
        # also accepts ``--body-append`` / ``--body-stdin`` which would
        # otherwise let a Stage 1/2 agent rewrite entire record bodies
        # and sidestep the allowlist. Janitor's structural fixes never
        # need to write body content — the body is user-authored and
        # stays immutable under this scope.
        "allow_body_writes": False,
        # body_insert_at: stub-flesh-out workflows enabled (the spec's
        # "*" wildcard). The structural autofix loop NEVER triggers
        # body_insert_at on its own; this opens the door for future
        # janitor-driven content insertion (e.g. linking existing stub
        # bodies to a missing-section template) without re-shipping
        # scope. Every real body_insert_at call still gates on
        # allow_body_writes=False above — so the wildcard here is
        # currently a NO-OP at runtime. The allowlist is documented +
        # tested for the day allow_body_writes is widened or replaced
        # by per-mutation-tool gates (the natural extension path).
        "allow_body_insert_at": {"*": True},
        # body_replace DENIED — ratified per spec (autofix-loop risk:
        # a misbehaving structural fix could replace user-authored
        # bodies wholesale). Yesterday's slug-drift Path B
        # ``allow_body_replace`` for janitor was killed empirically;
        # do NOT resurrect.
        "allow_body_replace": {},
    },
    # Stage 3 enrichment writes substantive content (description, role,
    # email, etc.) onto existing stub person/org records. Split out as
    # its own scope so the Stage 1/2 allowlist stays tight. Stage 3 never
    # creates or moves records — it only fills in fields on records the
    # structural scanner flagged as STUB001.
    "janitor_enrich": {
        "read": True,
        "search": True,
        "list": True,
        "context": True,
        "create": False,
        "edit": "field_allowlist",
        "edit_fields_allowlist": {
            "description", "role", "org", "email", "org_type",
            "website", "phone", "aliases", "related", "tags",
        },
        "move": False,
        "delete": False,
        # Stage 3 may append a brief description paragraph to stub
        # bodies when the frontmatter allowlist isn't enough. Body
        # writes stay allowed under this scope.
        "allow_body_writes": True,
        # Stage 3 enriches stub bodies via body_append only — no
        # mid-document insertion or full rewrite needed for the
        # structured-field-fill workflow.
        "allow_body_insert_at": {},
        "allow_body_replace": {},
    },
    "distiller": {
        "read": True,
        "search": True,
        "list": True,
        "context": True,
        "create": "learn_types_only",
        # Distiller writes distiller_signals and distiller_learnings
        # back to source records (see distiller/pipeline.py).
        "edit": "distiller_fields_only",
        "move": False,
        "delete": False,
        "allow_body_writes": True,
        # Distiller writes ATOMIC learning records (assumption /
        # decision / constraint / contradiction / synthesis) — every
        # one of which is in _BODY_MUTATE_DENIED_TYPES. So even if a
        # future distiller path wanted body_insert_at / body_replace,
        # the universal-deny set would refuse. Empty allowlist keeps
        # the contract explicit.
        "allow_body_insert_at": {},
        "allow_body_replace": {},
    },
    "surveyor": {
        "read": True,
        "search": True,
        "list": True,
        "context": True,
        "create": False,
        # Surveyor writes alfred_tags and relationships to frontmatter
        # (see surveyor/writer.py). Content is read-only.
        "edit": "tags_only",
        "move": False,
        "delete": False,
        # Surveyor writes only frontmatter tags; leave the default
        # permissive body-write rule on to avoid surprising a
        # hypothetical future surveyor body-rewrite.
        "allow_body_writes": True,
        # Surveyor only writes frontmatter (alfred_tags / relationships).
        # No body mutation tools needed.
        "allow_body_insert_at": {},
        "allow_body_replace": {},
    },
    "talker": {
        "read": True,
        "search": True,
        "list": True,
        "context": True,
        # Talker (Telegram voice bot) may only create a limited set of
        # conversational record types (see ``talker_types_only``).
        "create": "talker_types_only",
        "edit": True,
        "move": False,
        "delete": False,
        # Talker creates notes / sessions / conversations with body
        # content synthesised from the voice turn — body writes stay on.
        "allow_body_writes": True,
        # Salem (talker) — body_insert_at + body_replace per spec
        # matrix. note/task for typical conversational mid-doc edits;
        # event for calendar-relevant content additions. ``event``
        # body_replace is gated INSIDE _check_body_mutation_allowed
        # by the gcal_event_id carve-out: an event with a synced GCal
        # mirror refuses body_replace and points the operator at the
        # vault_delete-then-vault_create path instead. So the entry
        # below ALLOWS event in the dict but the carve-out runtime-
        # enforces "only events without gcal mirrors".
        "allow_body_insert_at": {
            "note": True, "task": True, "event": True,
        },
        "allow_body_replace": {
            "note": True, "task": True, "event": True,
        },
    },
    # Stage 3.5: KAL-LE — coding instance operating on the
    # aftermath-lab vault. Broader than talker because curation
    # legitimately adds pattern/principle records and edits bodies;
    # narrower than instructor because move + delete stay denied
    # (curation is additive — Andrew is the only one who removes
    # canonical content).
    "kalle": {
        "read": True,
        "search": True,
        "list": True,
        "context": True,
        # KAL-LE may create its own record set — the standard
        # talker types plus the two kalle-only types (pattern,
        # principle). Enforced via ``kalle_types_only``.
        "create": "kalle_types_only",
        "edit": True,
        "move": False,
        "delete": False,
        # Pattern/principle curation writes substantive bodies.
        "allow_body_writes": True,
        # KAL-LE — per spec matrix: note + decision (kalle's curation
        # records) + principle + pattern. ``decision`` here is KAL-LE's
        # OWN decision records (kalle scope creates them); they're NOT
        # in _BODY_MUTATE_DENIED_TYPES because that set's ``decision``
        # entry refers to the canonical-distiller atomic learning
        # record. The deny set takes precedence — calling body_insert_at
        # on a ``decision`` record from kalle scope STILL refuses,
        # because the universal-deny is the right call here too: a
        # decision's body should be append-only or new-record. The
        # matrix entry keeps the SPEC explicit; the runtime denies it.
        #
        # ``architecture`` (added 2026-05-04) — multi-instance system
        # design records. Mid-doc insertion + full rewrite both make
        # sense: design docs evolve as the system changes, sometimes
        # needing inserted sections (peer-protocol amendments) and
        # sometimes wholesale rewrites (canonical-authority's first
        # → second iteration). The Salem event/gcal carve-out doesn't
        # apply here — architecture records have no sync-state mirror.
        "allow_body_insert_at": {
            "note": True, "principle": True, "pattern": True,
            "architecture": True,
        },
        "allow_body_replace": {
            "note": True, "principle": True, "pattern": True,
            "architecture": True,
        },
    },
    # Hypatia — scholar/scribe instance operating on the
    # library-alexandria vault. Mirrors curator's "create + edit but
    # never delete" shape: drafting, editing, and zettelkasten upkeep
    # are all additive. Move stays denied for Phase 1 — Andrew
    # reorganises the library tree by hand; if Phase 2 wants
    # type-internal moves we'll narrow the rule then.
    "hypatia": {
        "read": True,
        "search": True,
        "list": True,
        "context": True,
        # Restrict create to the seven Hypatia record types per
        # ``library-alexandria/CLAUDE.md``. Enforced via
        # ``hypatia_types_only``.
        "create": "hypatia_types_only",
        "edit": True,
        "move": False,
        "delete": False,
        # Drafting essays, business docs, concept notes — bodies are
        # the whole point. Body writes stay allowed.
        "allow_body_writes": True,
        # Hypatia — per spec matrix: note, concept, document,
        # template, fiction-* types. The mid-document insertion case
        # was the ORIGINATING use case for this whole arc (Andrew's
        # MPC addendum to a DJ skill tracker — a note record).
        #
        # ``practice-session`` (added 2026-05-06) — anchored mid-doc
        # updates allowed (operator adds an observation against a
        # specific exercise heading mid-session). Body REPLACE
        # explicitly DENIED below — a practice-session is a historical
        # record; full rewrite would erase the in-session progression
        # the record is meant to capture. body_append (different gate,
        # ``allow_body_writes: True`` above) is the right tool for
        # adding observations during/after a session.
        "allow_body_insert_at": {
            "note": True,
            "concept": True,
            "document": True,
            "template": True,
            "fiction-continuity": True,
            "fiction-story": True,
            "fiction-structure": True,
            "fiction-world": True,
            "fiction-voice": True,
            "fiction-character": True,
            "practice-session": True,
        },
        "allow_body_replace": {
            "note": True,
            "concept": True,
            "document": True,
            "template": True,
            "fiction-continuity": True,
            "fiction-story": True,
            "fiction-structure": True,
            "fiction-world": True,
            "fiction-voice": True,
            "fiction-character": True,
            # practice-session deliberately OMITTED — see
            # ``allow_body_insert_at`` comment above.
            #
            # Voice/method training types (2026-05-07): the structured
            # records (voice, voice-cluster, method) are re-written by
            # the async extraction worker on re-extraction or cluster
            # rebuild. Raw records (essay, source) are write-once and
            # NOT in the replace allowlist — re-running /train on the
            # same essay produces a NEW voice profile, not a body
            # rewrite of the original essay record. body_insert_at
            # stays empty for all four types — no anchored mid-doc
            # insertion is part of the workflow; the worker writes
            # whole bodies, not patches.
            "voice": True,
            "voice-cluster": True,
            "method": True,
        },
    },
    # Instructor executes natural-language directives parked in the
    # ``alfred_instructions`` frontmatter field. Broader than janitor
    # (may create + move + write bodies; no frontmatter allowlist) but
    # narrower than talker — delete is denied because removing a record
    # is always an explicit operator task, never an instruction the
    # watcher should execute on its own. Part of the 6-commit
    # alfred_instructions watcher rollout.
    "instructor": {
        "read": True,
        "search": True,
        "list": True,
        "context": True,
        "create": True,
        "edit": True,
        "move": True,
        "delete": False,
        # Instructor may write full record bodies — directives can ask
        # for drafting, restructuring, or inserting content into an
        # existing record body.
        "allow_body_writes": True,
        # Instructor — operator-driven, trusted path. ``"*"`` wildcard
        # means "any type passes the allowlist gate" — but the
        # universal-deny set in _BODY_MUTATE_DENIED_TYPES still
        # refuses session/conversation/learning records. Operator can
        # override by directly editing the file outside the
        # alfred_instructions watcher (operator has filesystem
        # access; the gate guards only the agent path).
        "allow_body_insert_at": {"*": True},
        "allow_body_replace": {"*": True},
    },
}


# Record types the talker scope is allowed to create. Kept as a module-level
# constant so the rule handler below and any future callers share one source
# of truth.
#
# ``person`` was added 2026-04-21 after Salem created a ``note`` stub for
# "Alex Newton" instead of a ``person`` record — when Andrew names a new
# person, the canonical record is a ``person/`` record, not a generic note.
#
# ``org`` and ``location`` were added 2026-04-25 after Salem repeatedly
# hit the scope wall when Andrew named a new business or address mid-
# conversation. ``project``, ``constraint``, and ``contradiction`` were
# added in the same change as forward-looking additions: ``project`` is
# legitimately conversational (Andrew often kicks off a new initiative
# in voice); ``constraint`` and ``contradiction`` are learn types that
# Salem may surface during reflection when the distiller hasn't yet
# caught up. The two-gate design (``_validate_type`` + per-scope
# allowlist) keeps these types canonical-only.
TALKER_CREATE_TYPES: set[str] = {
    "task", "note", "decision", "event",
    "session", "conversation", "assumption", "synthesis",
    "person",
    "org", "location", "project", "constraint", "contradiction",
}


# Stage 3.5: record types KAL-LE may create. Superset of talker
# minus operational types (task, event) — KAL-LE is the coding
# instance, not an operational one — plus the kalle-only types
# (pattern, principle, architecture) from KNOWN_TYPES_KALLE.
#
# ``architecture`` (added 2026-05-04) — multi-instance system design
# records. Distinct from ``pattern`` (reusable how-to extracted FROM
# the system); architecture describes the SYSTEM (canonical-authority,
# PHI-firewall-design, peer-protocol). KAL-LE-only.
KALLE_CREATE_TYPES: set[str] = {
    "note", "session", "conversation",
    "decision", "assumption", "synthesis",
    "pattern", "principle", "architecture",
}


# Hypatia create allowlist — the seven record types defined in
# ``library-alexandria/CLAUDE.md`` plus the six Phase-2.5 fiction-
# element types. ``note`` overlaps with talker's set but lives in
# ``research/note/`` for Hypatia (the directory routing is the
# writer's responsibility, not scope's); ``session`` overlaps too
# but with a different ``mode`` field shape. The other five
# (document, concept, source, citation, template) are
# Hypatia-specific.
#
# Fiction posture types (Phase 2.5): see ``KNOWN_TYPES_HYPATIA`` in
# ``schema.py`` for the rationale. Both registries must list the same
# types — the schema layer gates ``_validate_type`` and the scope
# layer gates ``check_scope("create", ...)``. Drift between the two
# would surface as "type accepted by validator, rejected by scope" or
# vice versa, breaking the slash-command and natural-language
# scaffolding paths in different ways.
HYPATIA_CREATE_TYPES: set[str] = {
    "document", "session", "concept", "note",
    "source", "citation", "template",
    "fiction-continuity", "fiction-story", "fiction-structure",
    "fiction-world", "fiction-voice", "fiction-character",
    # Practice-session (2026-05-06) — Hypatia-only for now. Salem
    # could conceivably want it later (RRTS-related practice logging),
    # but the originating use case (DJ skill mastery, fencing,
    # workouts) is Hypatia's domain. Operator can extend this set
    # later if Salem needs it. Keep this set in sync with
    # ``KNOWN_TYPES_HYPATIA`` in schema.py — drift between the two
    # would surface as "type accepted by validator, rejected by scope"
    # or vice versa.
    "practice-session",
    # Voice/method training types (2026-05-07, /train + /method-source).
    # Hypatia is the originating instance; Salem/KAL-LE can opt in via
    # config later (the worker module is instance-neutral). Keep this
    # set in sync with ``KNOWN_TYPES_HYPATIA`` in schema.py — drift
    # between the two would surface as "type accepted by validator,
    # rejected by scope" or vice versa.
    "essay", "voice", "voice-cluster", "method",
    # Author (2026-05-16, capture-source-anchor arc). Hypatia-only —
    # author records index works by author, populated by capture-mode
    # opening-pattern resolver (``I'm reading X by Y``) and operator-
    # initiated /method-source workflows. Salem has no use case for
    # this type. Keep in sync with ``KNOWN_TYPES_HYPATIA`` in schema.py.
    "author",
}


# Canonical record types — the ones Salem owns as authoritative source-
# of-truth for entity identity + time. Phase A inter-instance comms
# (2026-05-01) explicitly carves these out from peer-instance
# ``vault_create`` paths: KAL-LE and Hypatia must NEVER create local
# person/org/location/event records — they propose to Salem via the
# transport's propose-create or queued-propose flows instead.
#
# The check runs INSIDE the per-scope ``*_types_only`` handler so the
# error message can name the appropriate propose tool. Keeping it in
# scope.py rather than in ops.py means a future scope (V.E.R.A.,
# STAY-C) inherits the same guard the moment it routes through the
# canonical guard rather than having to be re-added at every type-
# allowlist site.
CANONICAL_RECORD_TYPES: set[str] = {
    "person", "org", "location", "event",
}


# Per-scope hint mapping: when a peer instance attempts vault_create on
# a canonical type, the error message points at the right propose tool.
# Salem (talker scope) is the canonical owner — it creates these types
# directly via vault_create and does NOT route through propose. The
# guard below skips the talker scope entirely.
_PROPOSE_TOOL_HINT: dict[str, str] = {
    "person":   "propose_person",
    "org":      "propose_org",
    "location": "propose_location",
    "event":    "propose_event",
}


def check_scope(
    scope: str | None,
    operation: str,
    rel_path: str = "",
    record_type: str = "",
    frontmatter: dict | None = None,
    fields: list[str] | None = None,
    body_write: bool = False,
    existing_frontmatter: dict | None = None,
) -> None:
    """Check if an operation is allowed under the given scope.

    Args:
        scope: The agent scope (curator, janitor, distiller, surveyor) or None for unrestricted.
        operation: The vault operation (read, search, list, context, create, edit, move, delete,
            body_insert_at, body_replace).
        rel_path: Relative path of the target file (for path-based checks).
        record_type: Record type (for type-based checks on create).
        frontmatter: Optional frontmatter dict of the record being written
            (used by ``triage_tasks_only`` to enforce ``alfred_triage: true``).
            Defaults to None — rules that require it fail closed when absent.
        fields: Optional list of frontmatter field names being written
            (used by ``field_allowlist`` to constrain which fields a scope
            may mutate). Defaults to None — ``field_allowlist`` fails closed
            when absent so callers must always pass the fields being written.
        body_write: True if the caller is asking to write record body
            content (``--body-append`` / ``--body-stdin`` on ``vault edit``,
            or ``body`` on ``vault create``). Scopes that carry
            ``allow_body_writes: False`` reject the operation when this
            flag is set — closes the Q3 body-write loophole where the
            janitor could bypass the frontmatter allowlist by rewriting
            bodies. Defaults to False — callers that don't supply a body
            are trivially compliant.
        existing_frontmatter: For ``body_replace`` the gate needs to
            inspect the EXISTING (on-disk) frontmatter — specifically
            ``gcal_event_id`` on event records — to enforce the Salem
            event carve-out (refuse rewrite when a synced GCal mirror
            exists). Defaults to None for callers that don't yet plumb
            it; non-event types and non-replace operations are
            unaffected. The vault_edit gate populates this from the
            parsed file before calling.

    Raises:
        ScopeError: If the operation is denied.
    """
    if not scope:
        return  # No scope set → unrestricted (manual CLI usage)

    rules = SCOPE_RULES.get(scope)
    if rules is None:
        raise ScopeError(f"Unknown scope: '{scope}'")

    # Body-write gate — applied independently of the operation-level
    # permission because ``edit`` may succeed under an allowlist while
    # still needing to refuse body writes. Default True preserves
    # backwards compat for any scope (or explicit True) that hasn't
    # opted out. Checked before the operation permission so a caller
    # attempting a forbidden body write gets a body-specific error
    # rather than a misleading allowlist message.
    if body_write and rules.get("allow_body_writes", True) is False:
        raise ScopeError(
            f"Scope '{scope}' may not write record body content "
            f"(body_append / body_stdin). This scope is restricted "
            f"to frontmatter edits via its field allowlist."
        )

    # Body-mutation tools (body_insert_at + body_replace) — per-type
    # allowlist + universal-deny set. Gated independently of the
    # ``edit`` permission because ``edit`` may succeed for frontmatter
    # while body mutation is still denied. See
    # ``_check_body_mutation_allowed`` for the rule shape.
    if operation in ("body_insert_at", "body_replace"):
        allowlist_key = (
            "allow_body_insert_at"
            if operation == "body_insert_at"
            else "allow_body_replace"
        )
        allowlist = rules.get(allowlist_key)
        if not isinstance(allowlist, dict):
            allowlist = None  # treat missing/wrong-type as empty (deny-all)
        _check_body_mutation_allowed(
            operation=operation,
            scope=scope,
            record_type=record_type,
            allowlist=allowlist,  # type: ignore[arg-type]
            existing_frontmatter=existing_frontmatter,
        )
        return

    permission = rules.get(operation)
    if permission is None:
        raise ScopeError(f"Unknown operation: '{operation}'")

    if permission is True:
        return

    if permission is False:
        raise ScopeError(
            f"Operation '{operation}' denied for scope '{scope}'"
        )

    # Special rules
    if permission == "inbox_only":
        norm = rel_path.replace("\\", "/")
        if not norm.startswith("inbox/"):
            raise ScopeError(
                f"Operation '{operation}' only allowed on inbox/ paths for scope '{scope}'. "
                f"Got: {rel_path}"
            )
        return

    if permission == "learn_types_only":
        if record_type not in LEARN_TYPES:
            raise ScopeError(
                f"Scope '{scope}' can only create learn types "
                f"({', '.join(sorted(LEARN_TYPES))}). Got: '{record_type}'"
            )
        return

    if permission == "talker_types_only":
        if record_type not in TALKER_CREATE_TYPES:
            raise ScopeError(
                f"Scope '{scope}' can only create talker types "
                f"({', '.join(sorted(TALKER_CREATE_TYPES))}). Got: '{record_type}'"
            )
        return

    if permission == "kalle_types_only":
        # Canonical-type guard (Phase A inter-instance comms 2026-05-01).
        # KAL-LE must not create local person/org/location/event
        # records — those are Salem's canonical authority and route
        # through the transport propose flows. Surface a hint rather
        # than a generic "scope mismatch" so the agent prompt knows
        # which tool to use instead.
        if record_type in CANONICAL_RECORD_TYPES:
            tool_hint = _PROPOSE_TOOL_HINT.get(record_type, "propose tool")
            raise ScopeError(
                f"Scope '{scope}' may not create local '{record_type}' "
                f"records — those are Salem's canonical authority. "
                f"Use the '{tool_hint}' tool to propose creation on "
                f"Salem instead."
            )
        if record_type not in KALLE_CREATE_TYPES:
            raise ScopeError(
                f"Scope '{scope}' can only create kalle types "
                f"({', '.join(sorted(KALLE_CREATE_TYPES))}). Got: '{record_type}'"
            )
        return

    if permission == "hypatia_types_only":
        # Same canonical-type guard for Hypatia. See kalle branch above.
        if record_type in CANONICAL_RECORD_TYPES:
            tool_hint = _PROPOSE_TOOL_HINT.get(record_type, "propose tool")
            raise ScopeError(
                f"Scope '{scope}' may not create local '{record_type}' "
                f"records — those are Salem's canonical authority. "
                f"Use the '{tool_hint}' tool to propose creation on "
                f"Salem instead."
            )
        if record_type not in HYPATIA_CREATE_TYPES:
            raise ScopeError(
                f"Scope '{scope}' can only create hypatia types "
                f"({', '.join(sorted(HYPATIA_CREATE_TYPES))}). Got: '{record_type}'"
            )
        return

    # Distiller may only edit distiller_signals / distiller_learnings fields.
    # Field-level enforcement is the caller's responsibility; this gate
    # permits the edit operation to proceed.
    if permission == "distiller_fields_only":
        return

    # Surveyor may only edit alfred_tags / relationships fields.
    # Field-level enforcement is the caller's responsibility; this gate
    # permits the edit operation to proceed.
    if permission == "tags_only":
        return

    # Generic field-allowlist rule. The allowlist lives at
    # ``rules[f"{operation}_fields_allowlist"]`` as an iterable of field
    # names. ``fields`` is the list of frontmatter field names the caller
    # intends to write; this rule fails closed when ``fields`` is None so
    # callers can't accidentally bypass the check by omitting the argument.
    if permission == "field_allowlist":
        allowlist_key = f"{operation}_fields_allowlist"
        allowlist_raw = rules.get(allowlist_key)
        if allowlist_raw is None:
            raise ScopeError(
                f"Scope '{scope}' configured with field_allowlist for "
                f"'{operation}' but no '{allowlist_key}' set in SCOPE_RULES."
            )
        allowlist = set(allowlist_raw)  # type: ignore[arg-type]
        if fields is None:
            raise ScopeError(
                f"Scope '{scope}' may only {operation} fields in the "
                f"allowlist ({', '.join(sorted(allowlist))}); caller did "
                f"not supply the field list."
            )
        rejected = [f for f in fields if f not in allowlist]
        if rejected:
            raise ScopeError(
                f"Scope '{scope}' may only {operation} fields in the "
                f"allowlist ({', '.join(sorted(allowlist))}). Rejected: "
                f"{', '.join(rejected)}"
            )
        return

    # Janitor may create task records only when they carry the
    # alfred_triage: true frontmatter flag. Fails closed when no
    # frontmatter is passed by the caller.
    if permission == "triage_tasks_only":
        if record_type != "task":
            raise ScopeError(
                f"Scope '{scope}' may only create 'task' records "
                f"(with alfred_triage: true). Got: '{record_type}'"
            )
        if not frontmatter or not frontmatter.get("alfred_triage"):
            raise ScopeError(
                f"Scope '{scope}' may only create task records with "
                f"'alfred_triage: true' set in frontmatter."
            )
        return
