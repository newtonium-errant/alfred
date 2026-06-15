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
# |               |           | practice-session,zettel,MOC,     | preservation); memo not in    |
# |               |           | question,research-pointer,       | either matrix (write-once)    |
# |               |           | article                          |                               |
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
    # Operator-preference V1 (2026-05-24). Preferences are operator-
    # canonical commitments — body mutation via insert_at/replace
    # would silently rewrite the source_quote / matcher / policy text
    # that downstream consumers cite. The right path for changing a
    # preference is ``status: revoked`` on the existing record + a
    # new ``preference/`` record for the replacement (mirrors the
    # supersede flow for decision records). See
    # ``project_operator_preferences_v1.md`` Hard Contract #4.
    "preference",
    # Routine (2026-05-26, Phase 1). The body is auto-rendered from
    # the bundled template (``# Items`` / ``# History`` placeholder
    # sections pointing readers at the frontmatter source-of-truth);
    # the operational state lives in the ``items`` / ``completion_log``
    # frontmatter fields. Mid-document insertion and full rewrite are
    # both wrong tools — completion-log mutation goes through
    # ``alfred routine done`` (which appends a date string to the
    # frontmatter list), and item additions go through ``vault_edit``
    # set_fields on ``items``. The right path for changing the rendered
    # body sections is editing the template itself, not a body rewrite
    # on a record.
    "routine",
})


# Per-type delete deny set — operator-canonical types that even the
# janitor (which holds the only ``delete: True`` permission besides
# instructor) must NOT delete autonomously. Mirrors the body-mutation
# deny set's reasoning: removing a preference record would silently
# drop a forward-policy commitment from every consumer's view, and
# the operator's recovery path (re-read the source conversation,
# restate the commitment) is expensive. Operator can still delete
# via direct filesystem access; this gate guards only the agent path.
_DELETE_DENIED_TYPES: frozenset[str] = frozenset({
    # Operator-preference V1 (2026-05-24). Per dispatch Hard Contract:
    # "janitor cannot delete (preferences are operator-canonical, treat
    # like decisions)." Status flip (``status: revoked``) is the
    # authorised path for removing a preference from active effect;
    # the record itself stays for audit.
    "preference",
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
#
# Operation taxonomy:
#
#   * Top-level CRUD verbs (``read``, ``search``, ``list``, ``context``,
#     ``create``, ``edit``, ``move``, ``delete``) each get an entry in
#     every scope's rules dict.
#   * Body-mutation tools (``body_insert_at``, ``body_replace``) each
#     get their own per-type allowlist dict per the c1 matrix above.
#   * ``body_append`` rides on the binary ``allow_body_writes`` flag.
#   * ``unset`` rides on ``edit`` — there is no separate
#     ``allow_unset`` flag. Field removal via ``vault edit --unset
#     <field>`` (or programmatic ``vault_edit(unset_fields=[...])``)
#     gates through ``check_scope("edit", ..., fields=[...])`` with
#     the unset target names included in ``fields``. A scope whose
#     ``edit`` permission is True can unset any field; a scope under
#     ``field_allowlist`` can only unset fields in its allowlist;
#     a scope with ``edit: False`` cannot unset at all.
#
#     The refusal-to-unset-REQUIRED_FIELDS gate lives in ``ops.py``
#     (vault_edit), not here — required-field protection is a schema
#     concern, not a per-scope policy. Mirroring it at this layer
#     would duplicate logic across every scope's rules dict.
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
    # Phase 2B B1 (2026-05-30) — Conversational completion narrow scope.
    # Tighter than ``talker``: only the ``completion_log`` field on
    # ``routine`` records may be mutated; everything else (create,
    # other types, other fields, body writes) is denied. Used by the
    # ``routine_done`` talker tool subprocess path; the talker scope
    # itself stays broad for the rest of its surface.
    #
    # Read/search/list/context stay on so the tool can resolve the
    # routine record + look at existing completion_log entries before
    # appending. ``edit`` is the special permission
    # ``talker_routine_completion_only`` which combines type-restriction
    # AND field-allowlist enforcement.
    #
    # Phase 2B B3 will likely widen this for general conversational
    # editing of routine fields; until then this scope is the
    # narrow conversational-completion surface.
    "talker_routine_completion": {
        "read": True,
        "search": True,
        "list": True,
        "context": True,
        "create": False,
        "edit": "talker_routine_completion_only",
        "move": False,
        "delete": False,
        # Routine records are in _BODY_MUTATE_DENIED_TYPES regardless,
        # but pin body-write off here too for defense-in-depth +
        # operator-visible scope clarity.
        "allow_body_writes": False,
        "allow_body_insert_at": {},
        "allow_body_replace": {},
    },
    # Phase 2B B3 (2026-05-30) — Conversational routine item-CRUD scope.
    # Broader than ``talker_routine_completion`` (B1's narrow scope):
    # the ``items`` AND ``completion_log`` fields are mutable atomically
    # via this scope so that text-rename can migrate completion_log
    # keys (history preserved under new key) + remove can strip dead
    # completion_log entries.
    #
    # All other routine fields (cadence top-level, status, alfred_tags,
    # etc.) remain OUT of bounds — the talker can't change a routine's
    # firing rhythm or rename the record via this path. Those land
    # in separate ships (rename) or are covered by the broader
    # ``talker`` scope's general edit (alfred_tags via surveyor).
    "talker_routine_item": {
        "read": True,
        "search": True,
        "list": True,
        "context": True,
        "create": False,
        "edit": "talker_routine_item_only",
        "move": False,
        "delete": False,
        # Routine records are in _BODY_MUTATE_DENIED_TYPES regardless,
        # but pin body-write off here too for defense-in-depth +
        # operator-visible scope clarity (mirrors B1's narrow scope).
        "allow_body_writes": False,
        "allow_body_insert_at": {},
        "allow_body_replace": {},
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
            # Zettelkasten schema cutover (2026-05-16, Phase 1).
            # ``zettel``, ``MOC``, ``question``, ``research-pointer``
            # all evolve over time — zettels accrue Notes paragraphs
            # and refined Premise content; MOCs accrue hierarchical
            # Contents trees; questions accrue Exploration notes and
            # an eventual Answer; research-pointers accrue progress
            # Notes. Anchored mid-document insertion is the right tool
            # for all four growth shapes.
            #
            # ``memo`` deliberately OMITTED — memos are fleeting
            # single-thought captures, write-once by definition. If a
            # memo needs more substance, the operator promotes it to
            # a zettel (a new record, not a body rewrite).
            "zettel": True,
            "MOC": True,
            "question": True,
            "research-pointer": True,
            # Article co-write scope extension (2026-05-17). The
            # article template + type shipped earlier today (b12b5e6 +
            # c40e7a4) registered ``article`` in HYPATIA_CREATE_TYPES
            # but omitted it from the body-mutation allowlists.
            # Andrew ratified Option B: Hypatia is a true co-writer
            # on articles, not append-only. Anchored mid-doc inserts
            # (``add a paragraph between graf 3 and 4 of Part 2``) and
            # full-body replaces (``rewrite Part 3``) are both
            # operator-on-request workflows; both belong here.
            "article": True,
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
            # Zettelkasten schema cutover (2026-05-16, Phase 1). Same
            # rationale as the insert_at matrix above — zettels / MOCs /
            # questions / research-pointers are operator-curated
            # documents that legitimately need full-body rewrites
            # (e.g. operator refines a zettel's Premise + Notes
            # together, or restructures a MOC's Contents tree from
            # flat to hierarchical). Memo deliberately OMITTED —
            # write-once by design.
            "zettel": True,
            "MOC": True,
            "question": True,
            "research-pointer": True,
            # Article co-write scope extension (2026-05-17). Mirror of
            # the insert_at entry above — Hypatia rewrites a full Part
            # on operator request (e.g. ``rewrite Part 3, keep the
            # rest``). The same Hypatia-only-by-default discipline
            # applies: Salem's scope does not see ``article`` (it's
            # outside HYPATIA_CREATE_TYPES + the canonical KNOWN_TYPES);
            # operator's only path to article body mutation is via
            # Hypatia.
            "article": True,
        },
    },
    # --- VERA (RRTS team business assistant — first multi-user instance) ---
    #
    # VERA (2026-06-09, project_vera_ops_assistant.md) is the first
    # instance where vault scope depends on the SENDING USER's role, not
    # just the instance. Both Andrew (owner) and Ben (ops) hit the same
    # VERA daemon / same ``tool_set: vera``, but route to DIFFERENT scopes
    # via ``conversation.resolve_scope(tool_set, role)``:
    #   * owner (Andrew) → ``vera`` scope
    #   * ops   (Ben)    → ``vera_ops`` scope
    #
    # **Capability expansion 2026-06-15 (vera-assistant arc).** VERA grew
    # from a ticket bot into a general PHI-free business assistant for the
    # whole RRTS team. BOTH roles now create+edit the SAME five record
    # types — ``ticket`` (intake) + the four business types ``note`` /
    # ``task`` / ``decision`` / ``project`` (see VERA_*_CREATE_TYPES).
    # The two scopes are operationally identical today (same create set,
    # same edit posture); they stay separate scope keys for role-based
    # routing + future per-role divergence. Zero-PHI is structural; learn
    # types (except dual-nature ``decision``), canonical/PHI types, move,
    # and delete all stay DENIED.
    #
    # ``vera_ops`` — Ben's scope. Full ops on the five-type business
    # surface (create / read / search / list / context / edit). Ticket
    # resolve + close are status edits riding on ``edit: True`` (set
    # ``status: resolved | closed``). Locked OUT of non-allowlisted vault
    # writes by the ``vera_ops_types_only`` create gate, and out of move /
    # delete (Decision B, ratified): resolution is a status flip, never a
    # relocation or a destruction of queue history. The scope is the
    # security fence (the command-layer ops gate in bot.py is the first,
    # UX-facing fence).
    "vera_ops": {
        "read": True,
        "search": True,
        "list": True,
        "context": True,
        # → VERA_OPS_CREATE_TYPES = {ticket, note, task, decision, project}
        "create": "vera_ops_types_only",
        # Full field edits on the allowlisted record types (resolve/close
        # = status edit). No field allowlist — the role owns the whole
        # record's frontmatter on its types.
        "edit": True,
        # Decision B (ratified): both False. Resolution is a status flip,
        # not a move/delete. Ben can never relocate a record out of its
        # directory or destroy queue history.
        "move": False,
        "delete": False,
        # VERA writes record bodies at create time (Claude-Code ticket
        # brief, note/task/decision/project bodies).
        "allow_body_writes": True,
        # Ops doesn't patch bodies mid-doc or rewrite them wholesale —
        # only owner (vera scope) does. Empty allowlists deny both.
        "allow_body_insert_at": {},
        "allow_body_replace": {},
    },
    # ``vera`` — Andrew's (owner) scope on the VERA vault. Same five-type
    # create surface as ``vera_ops`` (see the expansion note above), plus
    # body patch / rewrite on the business types. Move + delete stay
    # denied (parity with hypatia's "owner deletes via filesystem"
    # posture; widen later if a real workflow needs it).
    "vera": {
        "read": True,
        "search": True,
        "list": True,
        "context": True,
        # → VERA_CREATE_TYPES = {ticket, note, task, decision, project}
        "create": "vera_owner_types_only",
        "edit": True,
        "move": False,
        "delete": False,
        "allow_body_writes": True,
        # Owner may anchored-insert + full-rewrite the business-type
        # bodies (refine a Claude-Code brief, restructure a note,
        # flesh out a project plan). ``decision`` is deliberately OMITTED
        # here — it's in _BODY_MUTATE_DENIED_TYPES (atomic learning
        # record; the deny set takes precedence over any per-scope
        # allowlist, so listing it would be DEAD config that still
        # refuses — same as the kalle scope's decision handling). The
        # right path for changing a decision is a NEW decision record
        # that supersedes the old one; body_append (the separate
        # ``allow_body_writes: True`` gate, NOT subject to the deny set)
        # still works for appending to a decision.
        "allow_body_insert_at": {
            "ticket": True, "note": True, "task": True, "project": True,
        },
        "allow_body_replace": {
            "ticket": True, "note": True, "task": True, "project": True,
        },
    },
    # ``vera_forwarder`` — the VERA-side deterministic forwarder
    # daemon's write authority for GitHub issue link-back ONLY
    # (2026-06-11, pipeline c2 of the ratified VERA→KAL-LE→GitHub
    # ticket design). After KAL-LE files the GitHub issue and answers
    # over the peer protocol, the forwarder writes the link-back
    # (ticket_uid / github_issue / github_url / forwarded_at) onto the
    # originating ticket record — and can do NOTHING else:
    #
    #   * ``edit`` uses the combined type+field gate
    #     ``vera_forwarder_link_back_only`` (same shape as
    #     ``talker_routine_completion_only``): ticket records only,
    #     fields restricted to ``VERA_FORWARDER_EDIT_FIELDS``,
    #     fail-closed when the caller omits the field list.
    #   * ``create`` DENIED — ticket creation belongs to the interview
    #     paths (vera / vera_ops); a forwarder that could create
    #     records could fabricate queue entries.
    #   * ``move`` / ``delete`` DENIED — queue history preserved (same
    #     Decision B posture as the other VERA scopes).
    #   * Body writes + body-mutation tools DENIED — the Claude-Code
    #     brief body is interview-owned; link-back is frontmatter-only.
    #
    # Read/search/list/context stay on so the daemon can resolve the
    # ticket record by uid before writing. NOTE: ``list`` requires the
    # ``vera_forwarder`` tag on the ticket TypeDefinition's
    # ``available_in_scopes`` (gate 1 fires on create AND list).
    "vera_forwarder": {
        "read": True,
        "search": True,
        "list": True,
        "context": True,
        "create": False,
        "edit": "vera_forwarder_link_back_only",
        "move": False,
        "delete": False,
        "allow_body_writes": False,
        "allow_body_insert_at": {},
        "allow_body_replace": {},
    },
    # ``vera_ticket_outcome`` — the VERA-side resolver's write authority
    # for the KAL-LE→VERA outcome write-back ONLY (2026-06-15, pipeline
    # c7). After KAL-LE's effectiveness loop sees a tracked issue reach a
    # terminal disposition, it pushes ``kind=ticket_outcome`` over the
    # peer protocol; VERA's registered resolver flips the originating
    # ticket out of the open worklist by editing the four outcome fields
    # — and can do NOTHING else:
    #
    #   * ``edit`` uses the combined type+field gate
    #     ``vera_ticket_outcome_only`` (same shape as
    #     ``vera_forwarder_link_back_only``): ticket records only, fields
    #     restricted to ``VERA_TICKET_OUTCOME_EDIT_FIELDS``, fail-closed
    #     when the caller omits the field list or the record type.
    #   * ``create`` DENIED — outcome write-back never mints records; a
    #     resolver that could create could fabricate resolved tickets.
    #   * ``move`` / ``delete`` DENIED — queue history preserved (same
    #     Decision B posture as the other VERA scopes).
    #   * Body writes + body-mutation tools DENIED — outcome write-back
    #     is frontmatter-only (status flip + disposition record).
    #
    # Read/search/list/context stay on so the resolver can locate the
    # ticket record by ``ticket_uid`` before writing. NOTE: ``list``
    # requires the ``vera_ticket_outcome`` tag on the ticket
    # TypeDefinition's ``available_in_scopes`` (gate 1 fires on list too).
    "vera_ticket_outcome": {
        "read": True,
        "search": True,
        "list": True,
        "context": True,
        "create": False,
        "edit": "vera_ticket_outcome_only",
        "move": False,
        "delete": False,
        "allow_body_writes": False,
        "allow_body_insert_at": {},
        "allow_body_replace": {},
    },
    # Migration scope — one-shot operational scripts under
    # ``scripts/migrate_*.py`` that perform schema rewrites against the
    # LIVE vault. NOT a daemon scope: this scope is never assigned to a
    # long-running process; the migration script sets it for the
    # duration of one CLI session and the audit log records every write
    # under tool="cli" with the script's session ID for traceability.
    #
    # ``unset`` rides on ``edit`` in this scope (no separate verb in
    # SCOPE_RULES): any scope whose ``edit`` permission is ``True`` or
    # passes a field-allowlist check can call ``vault edit --unset
    # <field>``, with the same fields-against-allowlist gate.
    # Migration scope sets ``edit: True`` so all fields are unset-able
    # (no allowlist). Refusing to unset REQUIRED_FIELDS happens at the
    # ``ops.py`` layer, not here — a scope-layer carve-out for
    # required fields would duplicate the schema gate.
    #
    # ``create`` is type-narrowed via ``migration_types_only`` →
    # ``MIGRATION_CREATE_TYPES``. The set covers operational records
    # the tier Phase 1 migration needs (task + routine) plus the five
    # canonical learning types so future migrations covering schema
    # rewrites on epistemic records work without scope amendments.
    # Auto-generated transcripts (session / conversation / capture /
    # input / run) + operator-canonical types (event / preference /
    # person / org / location) are NOT in the allowlist — a migration
    # accidentally creating one of those types fails loud at the scope
    # gate (not just the type-validator gate, which would only catch
    # unknown types). ``move`` + ``delete`` stay denied to avoid
    # accidental destructive moves during migration; if a future
    # migration genuinely needs to move records, the script can call
    # ``vault retype`` (which has its own per-call gate) instead of
    # widening this scope.
    "migration": {
        "read": True,
        "search": True,
        "list": True,
        "context": True,
        "create": "migration_types_only",
        "edit": True,
        "move": False,
        "delete": False,
        # Migration scripts write structured bodies on routine/task
        # records (e.g. appending a "Migration note" section to a
        # cancelled task). body_append is the canonical surface.
        "allow_body_writes": True,
        # body_insert_at + body_replace deliberately NOT enabled for
        # the migration scope — migrations either set frontmatter,
        # unset frontmatter, or body_append. Mid-document insertion
        # and full-body rewrite are higher-risk surfaces appropriate
        # for the interactive instructor path, not for automation
        # against the live vault. If a future migration genuinely
        # needs them, widen here with explicit per-type allowlist.
        "allow_body_insert_at": {},
        "allow_body_replace": {},
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
    # Operator-preference V1 (2026-05-24, project_operator_preferences_v1).
    # Salem is the canonical authority for preference records — when the
    # operator commits to a forward policy mid-conversation ("don't auto-
    # track open-house events from now on"), Salem persists it as a
    # ``preference/`` record. Hypatia is the other allowlisted writer
    # (her own local instance-application records); KAL-LE is NOT —
    # she's not a heavy talker surface in V1.
    "preference",
    # Routine Phase 2B B2 (2026-05-30) — conversational routine record
    # creation. Salem can now create new ``routine`` records via
    # ``vault_create`` when the operator names a new recurring practice
    # mid-conversation ("create a routine: walk the dog every 3 days").
    # B1 already shipped per-item completion via the narrow
    # ``talker_routine_completion`` scope; B2 adds the create surface.
    # B3 will add item-level editing of EXISTING routines (cadence
    # adjustment, add/remove items in place); until then, item-level
    # changes go via CLI or a fresh vault_edit.
    #
    # **Per-instance isolation is SINGLE-GATE, not belt-and-suspenders**
    # (review-clarified 2026-05-30). ``routine`` is tagged
    # ``available_in_scopes=frozenset({SCOPE_CANONICAL})`` in schema.py
    # — that means the TYPE VALIDATOR (``_validate_type`` via
    # ``known_types(scope)``) accepts ``routine`` under EVERY scope
    # (canonical types union with per-scope extensions for every named
    # scope). The non-Salem refusal lives ONLY at the scope layer:
    # ``kalle_types_only`` / ``hypatia_types_only`` reject ``routine``
    # because it isn't in ``KALLE_CREATE_TYPES`` / ``HYPATIA_CREATE_TYPES``.
    # Don't expect belt-and-suspenders defense from the validator —
    # the scope gate is the only enforcement surface.
    "routine",
    # c6 (2026-05-31) — talker tier_curation pre-set on future daily
    # files. Operator conversation 2026-06-01 00:01 surfaced the
    # capability gap: "set tomorrow's tier list" couldn't go through
    # vault_create because ``daily`` wasn't in this set. The
    # aggregator at 05:59 ADT each day preserves any pre-existing
    # ``tier_curation`` block per ``_load_existing_tier_curation``
    # (aggregator.py:828), so mechanically the talker pre-write is
    # safe — this set was the missing piece.
    #
    # The CREATE permission here is wide-open to ``daily`` type, but
    # the FIELD allowlist (``TALKER_TIER_CURATION_FIELDS`` below) is
    # enforced separately at the conversation.py dispatch layer when
    # ``record_type == "daily"`` — operator pre-set is restricted to
    # the ``tier_curation`` field; ``type``, ``date``,
    # ``routines_contributing``, ``critical_pending`` remain
    # aggregator-owned. Body content stays empty on talker pre-write
    # (the aggregator's next fire will fill it via
    # ``render_daily_body``).
    "daily",
}


# c6 (2026-05-31) — talker tier_curation field-allowlist scope set.
#
# Mirrors B1's ``TALKER_COMPLETION_LOG_*`` and B3's
# ``TALKER_ROUTINE_ITEM_*`` shape. Two constants:
#
#   * ``TALKER_TIER_CURATION_TYPES`` — record types this gate applies
#     to (just ``daily``; tier_curation isn't a meaningful field on
#     any other vault type).
#   * ``TALKER_TIER_CURATION_FIELDS`` — frontmatter fields this gate
#     allows when ``record_type`` is in TYPES (just ``tier_curation``).
#
# Unlike B1/B3, this gate is NOT a standalone SCOPE_RULES entry — the
# talker scope's ``create: talker_types_only`` + ``edit: True``
# permissions stay intact for other types (note/task/event/etc.). The
# per-type carve-out is enforced at the conversation.py dispatch
# layer via :func:`check_talker_tier_curation_fields` below, which is
# called only when the dispatch detects ``record_type == "daily"``.
#
# This shape avoids broadening the narrow-scope-rule design B1/B3
# established (each narrow scope = one subprocess tool path) — the
# tier_curation case is invoked from the same LLM vault_create /
# vault_edit dispatch as every other talker write, just with a
# per-type field check spliced in.
TALKER_TIER_CURATION_TYPES: set[str] = {"daily"}
TALKER_TIER_CURATION_FIELDS: set[str] = {"tier_curation"}


def check_talker_tier_curation_fields(
    record_type: str,
    fields: list[str] | None,
) -> None:
    """Per-type field-allowlist check for talker writes on ``daily``.

    Called from the conversation.py vault_create + vault_edit dispatch
    when ``record_type == "daily"``. Three checks, all must pass:

      1. ``record_type`` must be in ``TALKER_TIER_CURATION_TYPES`` —
         this is the dispatcher's responsibility to gate on, but we
         re-check here for defense-in-depth.
      2. ``fields`` must be supplied (fail-closed — same shape as the
         B1/B3 narrow scopes' field-allowlist enforcement). An LLM
         that hits vault_create with type=daily but supplies no
         set_fields would otherwise create a daily record with no
         tier_curation — that's a no-op + a stub file the aggregator
         will overwrite, but explicit fail-loud is better.
      3. ``fields`` must be a subset of ``TALKER_TIER_CURATION_FIELDS``.
         Defends against the LLM trying to pre-set
         ``routines_contributing`` or any other aggregator-owned
         field via the same write.

    Raises:
        ScopeError: when any of the three checks fails.
    """
    if record_type not in TALKER_TIER_CURATION_TYPES:
        # Defensive — the dispatcher should only call this when
        # record_type is in the type set. Fail loud if a future
        # refactor wires the helper at the wrong call site.
        raise ScopeError(
            f"Tier-curation field allowlist only applies to record types "
            f"({', '.join(sorted(TALKER_TIER_CURATION_TYPES))}). "
            f"Got: '{record_type}'."
        )
    if fields is None:
        raise ScopeError(
            f"Talker writes on '{record_type}' record types must restrict "
            f"to the tier_curation field allowlist "
            f"({', '.join(sorted(TALKER_TIER_CURATION_FIELDS))}); "
            f"caller did not supply the field list."
        )
    rejected = [f for f in fields if f not in TALKER_TIER_CURATION_FIELDS]
    if rejected:
        raise ScopeError(
            f"Talker writes on '{record_type}' record types may only "
            f"touch fields in the tier_curation allowlist "
            f"({', '.join(sorted(TALKER_TIER_CURATION_FIELDS))}). "
            f"Rejected: {', '.join(rejected)}. The fields "
            f"``type``, ``date``, ``routines_contributing``, "
            f"``critical_pending`` and body content are aggregator-"
            f"owned; the aggregator at 05:59 ADT each day will "
            f"fill them and preserve any pre-set ``tier_curation`` "
            f"via ``_load_existing_tier_curation``."
        )


# Phase 2B B1 (2026-05-30) — Conversational completion narrow scope.
#
# When the talker invokes the ``routine_done`` tool path (conversational
# completion of a routine item), the underlying mutation is a tightly
# scoped frontmatter edit on a ``routine`` record: append the completion
# date to ``completion_log[item_text]``. The talker scope's broad
# ``edit: True`` permission would allow much more than this — e.g.
# rewriting ``due_pattern``, removing ``items``, mutating ``cadence``.
# The conversational completion path should NEVER need any of that.
#
# Rather than narrowing the existing ``talker`` scope (which would risk
# breaking other talker tools that legitimately edit non-completion
# fields on other types), this ship adds a SEPARATE scope
# ``talker_routine_completion`` modeled on the ``janitor`` /
# ``janitor_enrich`` precedent: same agent, narrower surface, used by
# one specific tool path.
#
# Phase 2B B3 (deferred per dispatch) may widen this for general
# conversational editing of routine records (e.g., changing
# ``warn_after_gap_days`` via voice). Until then, only ``completion_log``
# may be touched.
#
# The two sets below are the canonical contract:
#   * ``TALKER_COMPLETION_LOG_TYPES`` — record types this scope may edit
#     (just ``routine``; mutating a task's completion_log would be a
#     category error since tasks don't carry one).
#   * ``TALKER_COMPLETION_LOG_FIELDS`` — frontmatter fields this scope
#     may write (just ``completion_log``).
#
# Both sets are enforced together via the ``talker_routine_completion_only``
# permission handler in :func:`check_scope`. Either check failing fails
# the whole edit.
TALKER_COMPLETION_LOG_TYPES: set[str] = {"routine"}
TALKER_COMPLETION_LOG_FIELDS: set[str] = {"completion_log"}


# Phase 2B B3 (2026-05-30) — Conversational routine item-CRUD scope.
#
# B1 shipped per-item completion (``talker_routine_completion`` scope,
# completion_log only). B3 widens the talker's routine surface to
# include item-level add/remove/edit operations. Both fields must be
# mutable atomically: ``items`` for the add/remove/edit itself, and
# ``completion_log`` because text-renames migrate keys (old_text →
# new_text preserves history) and removes strip dead entries.
#
# The two scopes coexist intentionally:
#   * ``talker_routine_completion`` — narrow B1 path, marks-done only.
#     Used by the ``routine_done`` tool subprocess.
#   * ``talker_routine_item`` (NEW) — broader B3 path, items + completion_log
#     atomic mutations. Used by the ``routine_item`` tool subprocess.
#
# Why two scopes instead of widening B1's to cover B3's surface? The
# B1 scope is documented as "completion only" in operator-facing prose
# (SKILL's "Scope is narrow" subsection); widening it would make the
# scope label misleading. Two scopes with two tool paths keeps the
# operator-facing semantics crisp + the per-tool gate matches its
# documented contract.
#
# Other fields (cadence top-level, status, name, alfred_tags, etc.)
# remain OUT of bounds for this scope — the talker can't change a
# routine's firing rhythm or rename it via this path. Those land in
# separate ships if friction surfaces (renaming) or are covered by
# the broader talker scope's general edit permission (alfred_tags
# already works there for the surveyor-driven tagging path).
TALKER_ROUTINE_ITEM_TYPES: set[str] = {"routine"}
TALKER_ROUTINE_ITEM_FIELDS: set[str] = {"items", "completion_log"}


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
    # ``ticket`` (added 2026-06-11, pipeline c2) — KAL-LE is the
    # backlog keeper of the ratified VERA→KAL-LE→GitHub ticket
    # pipeline: tickets are pushed from VERA over the peer protocol
    # and RECORDED in aftermath-lab's ``ticket/`` queue by the
    # deterministic intake handler (c3). The scope layer can't
    # distinguish the intake handler from the KAL-LE talker agent —
    # both run under scope "kalle" — so the create surface widens for
    # the scope as a whole; the pipeline's privilege boundary lives in
    # ``integrations/github_ops.py``, not here.
    "ticket",
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
    # Zettelkasten schema cutover (2026-05-16, Phase 1). Five new
    # Hypatia-only types per ``project_hypatia_zettelkasten_redesign.md``
    # "LOCKED IMPLEMENTATION PLAN". Keep in sync with
    # ``KNOWN_TYPES_HYPATIA`` in schema.py — drift between the two
    # would surface as "type accepted by validator, rejected by scope"
    # or vice versa.
    #
    #   - ``memo``            — capture-mode auto-branch path. Created
    #                           by Hypatia at session-close when the
    #                           capture session has <=1 user message.
    #   - ``zettel``          — capture-mode multi-message extraction
    #                           target (replaces ``note/`` for Hypatia
    #                           captures). Operator-curated subsequently.
    #   - ``MOC``             — Maps of Content. Operator-led; Hypatia
    #                           auto-creates members in response to
    #                           ``# Indexing & MOCs`` wikilinks.
    #   - ``question``        — elevated atomic question records.
    #   - ``research-pointer`` — elevated atomic research actions.
    "memo", "zettel", "MOC", "question", "research-pointer",
    # Article (2026-05-17, operator-template #1 ship). Hypatia-only —
    # published-writing records for Substack / Andrew Errant / future
    # venues. Distinct from ``essay`` (source essays Andrew reads, in
    # the same scope). Operator creates via ``vault_create`` at draft
    # time. Keep in sync with ``KNOWN_TYPES_HYPATIA`` in schema.py.
    "article",
    # Operator-preference V1 (2026-05-24, project_operator_preferences_v1).
    # Hypatia writes LOCAL instance-application preference records
    # (``library-alexandria/preference/<slug>.md``) that override or
    # extend Salem's canonical preferences for the Hypatia talker
    # surface. Universal preferences (Shape B1 — applies to all
    # instances) are Salem's authority; local instance preferences
    # (Shape B2 ``applies_to_instance: Hypatia``) are Hypatia's
    # authority. Conflict resolution: local wins. See
    # ``project_operator_preferences_v1.md`` Hard Contract #6 + #8.
    "preference",
}


# Migration create allowlist — operational scripts under
# ``scripts/migrate_*.py`` may only create records of these types.
# Mirrors the narrowing rationale of TALKER_CREATE_TYPES /
# KALLE_CREATE_TYPES / HYPATIA_CREATE_TYPES — type-narrowing the
# create surface keeps automation from accidentally producing
# auto-generated transcripts (session / conversation / capture /
# input / run) or operator-canonical records (event / preference /
# person / org / location) that need explicit operator review.
#
# Current entries:
#   - ``task``    : Operational records the migration scripts read +
#                   rewrite. Tier Phase 1 migration uses this for
#                   the standing-practices task cancellation flow.
#   - ``routine`` : Recurring-action records. Tier Phase 1 migration
#                   creates a ``routine/Standing Practices.md``
#                   aggregator for the migrated practices.
#   - Five canonical learning types (assumption / decision /
#                   constraint / contradiction / synthesis) —
#                   forward-looking room for future migrations that
#                   need to seed epistemic atoms when rewriting their
#                   schema. Not currently used by any shipped migration.
#
# Adding a new type here requires: (a) a concrete migration use case,
# (b) confirmation the type is NOT in any universal-deny set, (c)
# review that the scope-layer add doesn't undermine an existing
# per-instance type registry's authority. See SCOPE_RULES["migration"]
# for the canonical reference + ``check_scope`` ``migration_types_only``
# branch for the gate handler.
MIGRATION_CREATE_TYPES: set[str] = {
    "task",
    "routine",
    "assumption",
    "decision",
    "constraint",
    "contradiction",
    "synthesis",
}


# VERA create allowlists. Two sets — one per role.
#
# **Capability expansion 2026-06-15 (vera-assistant arc).** VERA grew
# from a ticket bot into a general PHI-free business assistant for the
# whole RRTS team. Both roles now create+edit the same FOUR business
# record types — ``note`` (jottings / meeting notes), ``task`` (action
# items), ``decision`` (OPERATIONAL business decisions — "we decided to
# use vendor X", NOT epistemic/distiller-style extractions), and
# ``project`` (initiatives — operator-confirmed Ben owns these too) —
# plus the existing ``ticket`` intake. Operator-confirmed capability
# matrix; the SKILL advertises exactly this set.
#
# Zero-PHI is STRUCTURAL (Dame-Bluebird has no patient records); this
# arc adds only business record types, never a PHI surface. The four
# distiller LEARNING types (``assumption`` / ``constraint`` /
# ``contradiction`` / ``synthesis``) stay DENIED — ``decision`` is the
# ONE dual-nature type granted (it's in both KNOWN_TYPES and
# LEARN_TYPES), and granting it does NOT leak the others (the create
# gate checks set membership only, not is_learn_type). Canonical/PHI
# types (person/org/location/event) stay denied — VERA's vault doesn't
# model RRTS entities. move + delete stay denied (unchanged).
#
# Keep these sets in sync with the ``available_in_scopes`` tags on the
# ``ticket`` / ``note`` / ``task`` / ``decision`` / ``project``
# TypeDefinitions in schema.py (gate 1) — the VERA-P1 trap class is
# "type accepted by one gate, rejected by the other." BOTH gates must
# agree. The four business types are also SCOPE_CANONICAL, so gate 1
# already admitted them; the explicit ``{vera, vera_ops}`` tags make the
# VERA capability greppable for the SKILL-capability audit (no-op for
# KNOWN_TYPES_BY_SCOPE, which already includes all canonical types).
#
# Both sets are deliberately IDENTICAL today but kept as two named
# constants for intent clarity + future per-role divergence. Contract-
# pinned in tests/test_vera_scope.py — widening either set must update
# the pin in the same commit.
VERA_OPS_CREATE_TYPES: set[str] = {
    "ticket", "note", "task", "decision", "project",
}

VERA_CREATE_TYPES: set[str] = {
    "ticket", "note", "task", "decision", "project",
}


# ``vera_forwarder`` link-back surface (2026-06-11, pipeline c2). The
# VERA-side deterministic forwarder daemon may edit EXACTLY these four
# frontmatter fields on ticket records — the GitHub issue link-back
# written after KAL-LE files the issue and answers over the peer
# protocol — and nothing else. Fail-loud beyond: any other field
# (status, title, ...) or any other record type is a ScopeError at the
# ``vera_forwarder_link_back_only`` gate in ``check_scope``.
# Contract-pinned in tests/test_ticket_pipeline_scope.py — widening
# either set must update the pin in the same commit.
VERA_FORWARDER_EDIT_TYPES: set[str] = {"ticket"}
VERA_FORWARDER_EDIT_FIELDS: set[str] = {
    "ticket_uid", "github_issue", "github_url", "forwarded_at",
}


# ``vera_ticket_outcome`` write-back surface (2026-06-15, pipeline c7).
# The KAL-LE→VERA outcome write-back: after KAL-LE's nightly
# effectiveness loop (``brief.kalle_digest.check_ticket_outcomes``)
# observes a tracked GitHub issue reach a TERMINAL disposition
# (merged_clean / merged_after_rework / closed_unmerged), KAL-LE pushes
# the outcome to VERA over the peer protocol (``kind=ticket_outcome``)
# and VERA's resolver edits EXACTLY these four frontmatter fields on the
# originating ticket record — flipping it out of VERA's open worklist —
# and nothing else.
#
# A DEDICATED scope (NOT a widen of ``vera_forwarder``): the forwarder is
# the VERA-INITIATED outbound daemon's identity, pinned to the link-back
# fields by design (status + content edits explicitly belong elsewhere,
# see the ``vera_forwarder_link_back_only`` gate). The outcome write-back
# is the OPPOSITE direction (inbound, KAL-LE-initiated) and DOES flip
# ``status`` — conflating the two trust surfaces would muddy both. Same
# three-check gate shape (``vera_ticket_outcome_only``): ticket records
# only, field-allowlist subset, fail-closed on missing type / fields.
#
# Fields: ``status`` (open→resolved|closed), ``ticket_disposition`` (the
# merged/closed-no-merge record), ``resolved_at`` (resolution
# timestamp), ``github_pr`` (the linked PR number, informational).
# Contract-pinned in tests/test_ticket_pipeline_scope.py — widening
# either set must update the pin in the same commit.
VERA_TICKET_OUTCOME_EDIT_TYPES: set[str] = {"ticket"}
VERA_TICKET_OUTCOME_EDIT_FIELDS: set[str] = {
    "status", "ticket_disposition", "resolved_at", "github_pr",
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
        record_type: Record type — used by the type-based create gates
            AND the type-restricted edit gates
            (``talker_routine_completion_only`` /
            ``talker_routine_item_only`` /
            ``vera_forwarder_link_back_only``), which fail CLOSED when
            it is empty. ``vault_edit`` parses it from the target
            record's frontmatter and passes it through.
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

    # Per-type delete denylist — operator-canonical types (preference
    # records as of V1) that no agent scope may delete autonomously,
    # even if its rules carry ``delete: True``. Mirrors the universal
    # body-mutation deny set's reasoning; the recovery cost of an
    # accidental delete is too high to gate via scope-level toggles
    # alone. Applies to every scope; instructor included (operator-
    # driven, but the watcher path runs without human-in-the-loop on
    # each directive — too risky for canonical records). Operator
    # retains filesystem-level delete.
    if operation == "delete" and record_type in _DELETE_DENIED_TYPES:
        raise ScopeError(
            f"Delete of record type '{record_type}' is universally "
            f"denied for agent scopes (operator-canonical — recovery "
            f"cost too high to gate via per-scope toggle). The "
            f"authorised path for removing a preference from active "
            f"effect is ``status: revoked`` on the existing record; "
            f"the record itself stays for audit. Operator may delete "
            f"via direct filesystem access if truly needed."
        )

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

    if permission == "talker_routine_completion_only":
        # Phase 2B B1 (2026-05-30) — Conversational completion narrow
        # gate. Three checks, all must pass:
        #   1. ``record_type`` must be in TALKER_COMPLETION_LOG_TYPES.
        #      Defends against the talker's conversational-completion
        #      path accidentally pointing at a task or person record.
        #   2. ``fields`` must be supplied (fail-closed — same shape as
        #      generic field_allowlist below).
        #   3. ``fields`` must be a subset of TALKER_COMPLETION_LOG_FIELDS.
        #      Defends against an LLM hallucination that decided to
        #      also adjust ``cadence`` or ``items`` in the same edit.
        # Frontmatter check is the narrow-gate path; the broader
        # ``edit`` permission ``True`` on the regular ``talker`` scope
        # is unaffected.
        #
        # Fail-CLOSED on a missing type (2026-06-12 review WARN-3):
        # an empty record_type used to SKIP the type restriction,
        # silently letting the scope edit its fields on any type.
        if not record_type:
            raise ScopeError(
                f"Scope '{scope}' gate 'talker_routine_completion_only' "
                f"is type-restricted but the record type is unavailable "
                f"(empty) — failing closed. Callers must pass "
                f"record_type (vault_edit parses it from the target "
                f"record's frontmatter)."
            )
        if record_type not in TALKER_COMPLETION_LOG_TYPES:
            raise ScopeError(
                f"Scope '{scope}' may only edit record types "
                f"({', '.join(sorted(TALKER_COMPLETION_LOG_TYPES))}). "
                f"Got: '{record_type}'. Use the regular 'talker' scope "
                f"for general conversational edits to other types."
            )
        if fields is None:
            raise ScopeError(
                f"Scope '{scope}' may only edit fields in the allowlist "
                f"({', '.join(sorted(TALKER_COMPLETION_LOG_FIELDS))}); "
                f"caller did not supply the field list."
            )
        rejected = [
            f for f in fields if f not in TALKER_COMPLETION_LOG_FIELDS
        ]
        if rejected:
            raise ScopeError(
                f"Scope '{scope}' may only edit fields in the allowlist "
                f"({', '.join(sorted(TALKER_COMPLETION_LOG_FIELDS))}). "
                f"Rejected: {', '.join(rejected)}. The conversational "
                f"completion path narrows to ``completion_log`` only; "
                f"Phase 2B B3 will widen this for general conversational "
                f"editing."
            )
        return

    if permission == "talker_routine_item_only":
        # Phase 2B B3 (2026-05-30) — Conversational item-CRUD gate.
        # Same three-check shape as ``talker_routine_completion_only``
        # above (type-restriction + fail-closed-on-missing-fields +
        # field-allowlist subset). The allowlist is broader: items
        # AND completion_log, since text-rename + remove both mutate
        # both fields atomically.
        #
        # Fail-CLOSED on a missing type (2026-06-12 review WARN-3) —
        # see talker_routine_completion_only above.
        if not record_type:
            raise ScopeError(
                f"Scope '{scope}' gate 'talker_routine_item_only' is "
                f"type-restricted but the record type is unavailable "
                f"(empty) — failing closed. Callers must pass "
                f"record_type (vault_edit parses it from the target "
                f"record's frontmatter)."
            )
        if record_type not in TALKER_ROUTINE_ITEM_TYPES:
            raise ScopeError(
                f"Scope '{scope}' may only edit record types "
                f"({', '.join(sorted(TALKER_ROUTINE_ITEM_TYPES))}). "
                f"Got: '{record_type}'. Use the regular 'talker' scope "
                f"for general conversational edits to other types."
            )
        if fields is None:
            raise ScopeError(
                f"Scope '{scope}' may only edit fields in the allowlist "
                f"({', '.join(sorted(TALKER_ROUTINE_ITEM_FIELDS))}); "
                f"caller did not supply the field list."
            )
        rejected = [
            f for f in fields if f not in TALKER_ROUTINE_ITEM_FIELDS
        ]
        if rejected:
            raise ScopeError(
                f"Scope '{scope}' may only edit fields in the allowlist "
                f"({', '.join(sorted(TALKER_ROUTINE_ITEM_FIELDS))}). "
                f"Rejected: {', '.join(rejected)}. The conversational "
                f"item-CRUD path narrows to ``items`` + ``completion_log`` "
                f"only; other routine fields (cadence, status, etc.) "
                f"need a separate ship if conversational mutation is "
                f"required."
            )
        return

    if permission == "migration_types_only":
        # Migration scripts may only create the types in
        # MIGRATION_CREATE_TYPES (task / routine / 5 learning types).
        # See MIGRATION_CREATE_TYPES docstring for rationale; bare
        # ``"create": True`` was rejected in code review because a
        # typo migration accidentally creating a ``session`` or
        # ``event`` record would silently produce an auto-generated-
        # transcript-shaped record that no operator review path
        # caught. The narrow allowlist fails loud at this gate
        # instead.
        if record_type not in MIGRATION_CREATE_TYPES:
            raise ScopeError(
                f"Scope '{scope}' can only create migration types "
                f"({', '.join(sorted(MIGRATION_CREATE_TYPES))}). "
                f"Got: '{record_type}'. If a future migration needs "
                f"this type, extend MIGRATION_CREATE_TYPES in scope.py "
                f"with a comment naming the migration's use case."
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

    if permission == "vera_ops_types_only":
        # VERA MVP (2026-06-09) — Ben's (ops) create gate. Ticket-type
        # ONLY. No canonical-type propose hint (VERA owns its own
        # tickets; there's no peer Salem to propose to in MVP), so this
        # branch is simpler than the kalle / hypatia ones above. The
        # narrow allowlist is the schema-side enforcement of "Ben can't
        # write non-ticket records."
        if record_type not in VERA_OPS_CREATE_TYPES:
            raise ScopeError(
                f"Scope '{scope}' can only create vera-ops types "
                f"({', '.join(sorted(VERA_OPS_CREATE_TYPES))}). "
                f"Got: '{record_type}'. The ops role is locked to the "
                f"ticket queue; non-ticket vault writes are owner-only."
            )
        return

    if permission == "vera_owner_types_only":
        # VERA MVP (2026-06-09) — Andrew's (owner) create gate. Ticket +
        # note (Decision C). Same shape as the ops gate above, broader
        # allowlist. No canonical-type propose hint for the same reason.
        if record_type not in VERA_CREATE_TYPES:
            raise ScopeError(
                f"Scope '{scope}' can only create vera types "
                f"({', '.join(sorted(VERA_CREATE_TYPES))}). "
                f"Got: '{record_type}'"
            )
        return

    if permission == "vera_forwarder_link_back_only":
        # Pipeline c2 (2026-06-11) — VERA-side forwarder link-back
        # gate. Same three-check shape as
        # ``talker_routine_completion_only``: type restriction +
        # fail-closed-on-missing-fields + field-allowlist subset. The
        # forwarder writes ONLY the GitHub issue link-back fields onto
        # ticket records; anything else fails loud here.
        #
        # Fail-CLOSED on a missing type (2026-06-12 review WARN-3) —
        # see talker_routine_completion_only above.
        if not record_type:
            raise ScopeError(
                f"Scope '{scope}' gate 'vera_forwarder_link_back_only' "
                f"is type-restricted but the record type is unavailable "
                f"(empty) — failing closed. Callers must pass "
                f"record_type (vault_edit parses it from the target "
                f"record's frontmatter)."
            )
        if record_type not in VERA_FORWARDER_EDIT_TYPES:
            raise ScopeError(
                f"Scope '{scope}' may only edit record types "
                f"({', '.join(sorted(VERA_FORWARDER_EDIT_TYPES))}). "
                f"Got: '{record_type}'. The forwarder's write authority "
                f"is the GitHub issue link-back on ticket records only."
            )
        if fields is None:
            raise ScopeError(
                f"Scope '{scope}' may only edit fields in the allowlist "
                f"({', '.join(sorted(VERA_FORWARDER_EDIT_FIELDS))}); "
                f"caller did not supply the field list."
            )
        rejected = [
            f for f in fields if f not in VERA_FORWARDER_EDIT_FIELDS
        ]
        if rejected:
            raise ScopeError(
                f"Scope '{scope}' may only edit fields in the allowlist "
                f"({', '.join(sorted(VERA_FORWARDER_EDIT_FIELDS))}). "
                f"Rejected: {', '.join(rejected)}. The forwarder path "
                f"narrows to the GitHub issue link-back; ticket status "
                f"and content edits belong to the vera / vera_ops scopes."
            )
        return

    if permission == "vera_ticket_outcome_only":
        # Pipeline c7 (2026-06-15) — VERA-side outcome write-back gate.
        # Same three-check shape as ``vera_forwarder_link_back_only``:
        # type restriction + fail-closed-on-missing-fields + field-
        # allowlist subset. The resolver flips the originating ticket out
        # of the open worklist (status + disposition + resolved_at +
        # github_pr); anything else fails loud here.
        #
        # Fail-CLOSED on a missing type — same posture as the forwarder
        # gate above; vault_edit parses record_type from the target's
        # frontmatter, so an empty value here is a caller bug, not a
        # licence to edit any type.
        if not record_type:
            raise ScopeError(
                f"Scope '{scope}' gate 'vera_ticket_outcome_only' "
                f"is type-restricted but the record type is unavailable "
                f"(empty) — failing closed. Callers must pass "
                f"record_type (vault_edit parses it from the target "
                f"record's frontmatter)."
            )
        if record_type not in VERA_TICKET_OUTCOME_EDIT_TYPES:
            raise ScopeError(
                f"Scope '{scope}' may only edit record types "
                f"({', '.join(sorted(VERA_TICKET_OUTCOME_EDIT_TYPES))}). "
                f"Got: '{record_type}'. The outcome write-back's write "
                f"authority is the resolution flip on ticket records only."
            )
        if fields is None:
            raise ScopeError(
                f"Scope '{scope}' may only edit fields in the allowlist "
                f"({', '.join(sorted(VERA_TICKET_OUTCOME_EDIT_FIELDS))}); "
                f"caller did not supply the field list."
            )
        rejected = [
            f for f in fields if f not in VERA_TICKET_OUTCOME_EDIT_FIELDS
        ]
        if rejected:
            raise ScopeError(
                f"Scope '{scope}' may only edit fields in the allowlist "
                f"({', '.join(sorted(VERA_TICKET_OUTCOME_EDIT_FIELDS))}). "
                f"Rejected: {', '.join(rejected)}. The outcome write-back "
                f"narrows to the resolution flip; ticket creation and "
                f"content edits belong to the vera / vera_ops scopes."
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
