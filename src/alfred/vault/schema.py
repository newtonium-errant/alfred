"""Shared vault schema — record types, statuses, field definitions.

This module is the single source of truth for vault record-type metadata.

Historical shape: parallel global dicts (``KNOWN_TYPES`` set,
``TYPE_DIRECTORY`` mapping, ``STATUS_BY_TYPE`` mapping, etc.) — adding a
new vault type meant updating 7+ separate globals and any one of them
could silently fall out of sync.

Current shape: a single ``TypeRegistry`` of ``TypeDefinition`` records
holds all per-type metadata. The historical global names
(``KNOWN_TYPES``, ``TYPE_DIRECTORY``, ``STATUS_BY_TYPE``,
``NAME_FIELD_BY_TYPE``, ``REQUIRED_FIELDS_BY_TYPE``, ``LEARN_TYPES``,
``LEAF_TYPES``, ``KNOWN_TYPES_HYPATIA``, ``KNOWN_TYPES_KALLE``,
``KNOWN_TYPES_BY_SCOPE``) are still exported at module load — they are
derived views off the registry, preserved verbatim for backward
compatibility with the 10+ files that import them today. Adding a new
type means appending one ``TypeDefinition`` to ``_DEFINITIONS`` below;
all derived globals update automatically.

Non-per-type registries (``LIST_FIELDS``, ``REQUIRED_FIELDS``,
``INSTRUCTION_FIELDS``, ``REMINDER_FIELDS``, ``EVENT_GCAL_FIELDS``)
stay as direct module-level constants — they're not keyed by type.

See ``tests/test_type_registry.py`` for the registry's API contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


# ---------------------------------------------------------------------------
# TypeDefinition + TypeRegistry — the canonical per-type metadata model.
# ---------------------------------------------------------------------------

# Sentinel scope names used by ``available_in_scopes`` to mark a type
# as either universally available (``"canonical"`` — every scope can
# create / validate it; the historical ``KNOWN_TYPES`` set) or
# scope-specific (``"hypatia"``, ``"kalle"`` — appears in that scope's
# extension set only). A type may carry MULTIPLE scope tags; the
# ``KNOWN_TYPES_BY_SCOPE`` derived view unions ``"canonical"`` with the
# requested scope.
SCOPE_CANONICAL = "canonical"


@dataclass(frozen=True)
class TypeDefinition:
    """Metadata for a single vault record type.

    Replaces the per-type entries that previously lived in seven
    parallel global dicts. One record per vault type; all per-type
    state lives here.

    Fields:
        name: canonical type string (e.g. ``"person"``, ``"zettel"``).
        directory: top-level vault directory for records of this type.
            ``None`` means "no explicit entry" — callers fall back to
            ``name`` via ``TypeRegistry.directory()``. The distinction
            is preserved because some consumers iterate
            ``TYPE_DIRECTORY.values()`` (e.g. ``janitor/scanner.py``'s
            ``_entity_dirs`` filter for body-link entity detection),
            and silently expanding the value set would change scan
            behavior. Keep historic explicit-vs-fallback semantics.
        statuses: valid statuses for the type. ``None`` means "no
            STATUS_BY_TYPE entry" (status validation skipped entirely);
            empty frozenset means "explicit empty entry" (used by
            ``event`` to declare 'no status constraint' as a deliberate
            decision). Distinction is load-bearing for
            ``if rec_type in STATUS_BY_TYPE`` checks.
        required_fields: per-type frontmatter fields required IN
            ADDITION TO the universal ``REQUIRED_FIELDS`` list.
        name_field: which frontmatter field holds the canonical name
            for this type. Defaults to ``"name"``; ``conversation`` and
            ``input`` use ``"subject"``.
        available_in_scopes: scopes that may create / validate this
            type. Use ``"canonical"`` for universally-available types
            (the historical ``KNOWN_TYPES`` set); ``"hypatia"`` /
            ``"kalle"`` for per-scope extensions. A scope sees this
            type if it is in this set OR if ``"canonical"`` is.
        is_learn_type: True for distiller-generated learning records
            (assumption, decision, constraint, contradiction,
            synthesis). Drives the ``LEARN_TYPES`` derived set.
        is_leaf: True for terminal-by-design types — records that no
            other record is expected to point at, so zero inbound
            wikilinks is the norm, not an ORPHAN001 defect. Drives the
            ``LEAF_TYPES`` derived set.
    """

    name: str
    directory: str | None = None
    statuses: frozenset[str] | None = None
    required_fields: tuple[str, ...] = ()
    name_field: str = "name"
    available_in_scopes: frozenset[str] = field(default_factory=frozenset)
    is_learn_type: bool = False
    is_leaf: bool = False


class TypeRegistry:
    """Single source of truth for vault type definitions.

    Replaces the parallel global dicts (KNOWN_TYPES, TYPE_DIRECTORY,
    STATUS_BY_TYPE, REQUIRED_FIELDS_BY_TYPE, NAME_FIELD_BY_TYPE,
    LEARN_TYPES, LEAF_TYPES, KNOWN_TYPES_HYPATIA, KNOWN_TYPES_KALLE,
    KNOWN_TYPES_BY_SCOPE) with a single registry of ``TypeDefinition``
    records. The historical globals are still exported at module load
    as derived views — see the bottom of this file.

    Callers may use the registry methods (``known_types``,
    ``directory``, ``statuses``, etc.) for new code; existing callers
    that import the historical globals continue to work unchanged.
    """

    def __init__(self, definitions: Iterable[TypeDefinition]):
        self._by_name: dict[str, TypeDefinition] = {}
        for d in definitions:
            if d.name in self._by_name:
                raise ValueError(
                    f"TypeRegistry: duplicate definition for type {d.name!r}. "
                    f"Each type may appear only once in _DEFINITIONS."
                )
            self._by_name[d.name] = d

    # --- lookup ---------------------------------------------------------

    def get(self, name: str) -> TypeDefinition | None:
        """Return the definition for ``name``, or None if unknown."""
        return self._by_name.get(name)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._by_name

    def __iter__(self):
        return iter(self._by_name.values())

    def __len__(self) -> int:
        return len(self._by_name)

    # --- per-scope type membership --------------------------------------

    def known_types(self, scope: str | None = None) -> frozenset[str]:
        """Return the set of type names visible to ``scope``.

        With ``scope=None`` (default), returns the canonical set — the
        types tagged with ``SCOPE_CANONICAL`` (historically
        ``KNOWN_TYPES``).

        With a non-None scope, returns canonical PLUS any types tagged
        with that scope (historically ``KNOWN_TYPES_BY_SCOPE[scope]``).
        Unknown scopes fall back to canonical only — same semantics as
        ``KNOWN_TYPES_BY_SCOPE.get(scope, KNOWN_TYPES)``.
        """
        if scope is None:
            return frozenset(
                d.name for d in self._by_name.values()
                if SCOPE_CANONICAL in d.available_in_scopes
            )
        return frozenset(
            d.name for d in self._by_name.values()
            if SCOPE_CANONICAL in d.available_in_scopes
            or scope in d.available_in_scopes
        )

    def types_in_scope(self, scope: str) -> frozenset[str]:
        """Return JUST the types tagged with ``scope`` (NO canonical merge).

        Use this for the per-scope extension sets (historically
        ``KNOWN_TYPES_HYPATIA``, ``KNOWN_TYPES_KALLE``). For the
        merged canonical+scope set (the ``_validate_type`` gate),
        use ``known_types(scope)``.
        """
        return frozenset(
            d.name for d in self._by_name.values()
            if scope in d.available_in_scopes
        )

    # --- per-type metadata ----------------------------------------------

    def directory(self, name: str) -> str:
        """Return the directory for ``name``, falling back to ``name``.

        Mirrors the historical ``TYPE_DIRECTORY.get(name, name)`` call
        pattern used by writers throughout the codebase. Returns the
        type name itself when no explicit directory entry exists.
        """
        d = self._by_name.get(name)
        if d is None or d.directory is None:
            return name
        return d.directory

    def statuses(self, name: str) -> frozenset[str]:
        """Return valid statuses for ``name``, or empty frozenset.

        Mirrors ``STATUS_BY_TYPE.get(name, set())``. Returns the
        explicit empty frozenset for types like ``event`` that declare
        no status constraint by design; returns empty frozenset also
        for unknown types and for types with no STATUS_BY_TYPE entry.
        Use ``has_status_entry()`` to distinguish.
        """
        d = self._by_name.get(name)
        if d is None or d.statuses is None:
            return frozenset()
        return d.statuses

    def has_status_entry(self, name: str) -> bool:
        """True iff ``name`` has an explicit status entry (possibly empty).

        Mirrors ``name in STATUS_BY_TYPE``. The distinction matters for
        the scanner: ``if rec_type in STATUS_BY_TYPE`` gates status
        validation only when the type has explicitly declared its
        constraint (or lack thereof, in event's case).
        """
        d = self._by_name.get(name)
        return d is not None and d.statuses is not None

    def required_fields(self, name: str) -> tuple[str, ...]:
        """Per-type ADDITIONAL required fields (beyond REQUIRED_FIELDS)."""
        d = self._by_name.get(name)
        return d.required_fields if d else ()

    def name_field(self, name: str) -> str:
        """Frontmatter field holding the canonical name for ``name``."""
        d = self._by_name.get(name)
        return d.name_field if d else "name"

    def is_learn_type(self, name: str) -> bool:
        d = self._by_name.get(name)
        return bool(d and d.is_learn_type)

    def is_leaf(self, name: str) -> bool:
        d = self._by_name.get(name)
        return bool(d and d.is_leaf)


# ---------------------------------------------------------------------------
# Canonical type definitions.
# ---------------------------------------------------------------------------
#
# One ``TypeDefinition`` per vault record type. The rationale comments
# that used to live next to the parallel-dict entries are preserved
# inline here. When adding a new vault type, append a definition to
# this list — every derived global below will update automatically.

_DEFINITIONS: list[TypeDefinition] = [
    # --- Canonical types (Salem's operational world) ------------------
    TypeDefinition(
        name="project",
        directory="project",
        statuses=frozenset({"active", "paused", "completed", "abandoned", "proposed"}),
        # ``{vera, vera_ops}`` (2026-06-15, vera-assistant arc) — VERA is
        # a general PHI-free business assistant; both roles create+edit
        # ``project`` (initiatives). ``project`` is also SCOPE_CANONICAL,
        # so gate 1 already admitted it for every scope; the explicit VERA
        # tags make the capability greppable for the SKILL-capability
        # audit (no-op for KNOWN_TYPES_BY_SCOPE). Gate 2 (the
        # VERA_*_CREATE_TYPES allowlist in scope.py) is the policy fence —
        # both gates must agree (the VERA-P1 trap class).
        available_in_scopes=frozenset({SCOPE_CANONICAL, "vera", "vera_ops"}),
    ),
    TypeDefinition(
        name="task",
        directory="task",
        statuses=frozenset({"todo", "active", "blocked", "done", "cancelled"}),
        # ``{vera, vera_ops}`` (2026-06-15, vera-assistant arc) — both
        # VERA roles create+edit ``task`` (action items). See the
        # ``project`` note above for the gate-1/gate-2 contract.
        #
        # HYPATIA (2026-07, clinic-capture Piece 3) also creates ``task`` (from a
        # capture's action_items), but is DELIBERATELY *not* tagged here: ``task``
        # is ``SCOPE_CANONICAL``, so gate 1 (``known_types("hypatia")`` =
        # canonical ∪ scope) ALREADY admits it — a ``"hypatia"`` tag would be a
        # gate-1 no-op AND would pollute ``KNOWN_TYPES_HYPATIA``
        # (``types_in_scope("hypatia")``), the hypatia-ONLY extension set pinned
        # to exclude Salem-core types. The lone fence for Hypatia is gate 2:
        # ``task`` in ``scope.HYPATIA_CREATE_TYPES``. (Contrast ``ticket``, which
        # IS non-canonical and therefore MUST carry its vera tag for gate 1.)
        available_in_scopes=frozenset({SCOPE_CANONICAL, "vera", "vera_ops"}),
    ),
    TypeDefinition(
        name="session",
        # No TYPE_DIRECTORY entry historically — session has flexible
        # placement (operator may organize under daily/, session/, etc.).
        directory=None,
        statuses=frozenset({"active", "completed"}),
        available_in_scopes=frozenset({SCOPE_CANONICAL}),
    ),
    TypeDefinition(
        name="input",
        # No TYPE_DIRECTORY entry historically — input has flexible placement.
        directory=None,
        statuses=frozenset({"unprocessed", "processed", "deferred"}),
        name_field="subject",
        available_in_scopes=frozenset({SCOPE_CANONICAL}),
    ),
    TypeDefinition(
        name="person",
        directory="person",
        statuses=frozenset({"active", "inactive"}),
        available_in_scopes=frozenset({SCOPE_CANONICAL}),
    ),
    TypeDefinition(
        name="org",
        directory="org",
        statuses=frozenset({"active", "inactive"}),
        available_in_scopes=frozenset({SCOPE_CANONICAL}),
    ),
    TypeDefinition(
        name="location",
        directory="location",
        statuses=frozenset({"active", "inactive"}),
        available_in_scopes=frozenset({SCOPE_CANONICAL}),
    ),
    TypeDefinition(
        name="note",
        directory="note",
        statuses=frozenset({"draft", "active", "living", "review", "final"}),
        # ``{vera, vera_ops}`` (2026-06-15, vera-assistant arc) — both
        # VERA roles create+edit ``note`` (jottings / meeting notes).
        # See the ``project`` note above for the gate-1/gate-2 contract.
        available_in_scopes=frozenset({SCOPE_CANONICAL, "vera", "vera_ops"}),
        # Leaf-by-design: 258 of 360 ORPHAN001s lived under note/ in the
        # 2026-04-30 residual categorization. Notes are mostly captured
        # emails / one-off jottings; the few that DO get linked already
        # register inbound. Skip ORPHAN001 for note/.
        is_leaf=True,
    ),
    TypeDefinition(
        name="decision",
        directory="decision",
        statuses=frozenset({"draft", "final", "superseded", "reversed"}),
        # ``{vera, vera_ops}`` (2026-06-15, vera-assistant arc) — both
        # VERA roles create+edit ``decision``, the ONE dual-nature type
        # VERA is granted: it captures OPERATIONAL business decisions
        # ("we decided to use vendor X"), distinct from the distiller's
        # epistemic decisions. Granting it does NOT grant the other four
        # learn types (the create gate checks set membership, not
        # is_learn_type). Body mutation stays denied via
        # _BODY_MUTATE_DENIED_TYPES (supersede-with-new-record is the
        # change path). See the ``project`` note above for the
        # gate-1/gate-2 contract.
        available_in_scopes=frozenset({SCOPE_CANONICAL, "vera", "vera_ops"}),
        # ``decision`` is in BOTH ``KNOWN_TYPES`` (the canonical
        # operational set) AND ``LEARN_TYPES`` — it's an entity type
        # that the distiller also produces. Preserved verbatim from
        # the pre-refactor literal LEARN_TYPES set (assumption,
        # decision, constraint, contradiction, synthesis).
        is_learn_type=True,
        # Distiller-generated. ``source_links`` carries the forward
        # reference back to source(s); back-refs would require mutating
        # source records on every distiller fire (breaks deterministic-
        # writer principle ratified during distiller rebuild).
        is_leaf=True,
    ),
    TypeDefinition(
        name="process",
        directory="process",
        statuses=frozenset({"active", "proposed", "design", "deprecated"}),
        available_in_scopes=frozenset({SCOPE_CANONICAL}),
    ),
    TypeDefinition(
        name="run",
        directory="run",
        statuses=frozenset({"active", "completed", "blocked", "cancelled"}),
        available_in_scopes=frozenset({SCOPE_CANONICAL}),
        # 15 ORPHAN001 entries in the 2026-04-30 residual were Morning
        # Briefs / daily-output records — terminal by design.
        is_leaf=True,
    ),
    TypeDefinition(
        name="event",
        directory="event",
        # Explicit empty status set — ``event`` deliberately declares
        # "no status constraint." Distinct from "no STATUS_BY_TYPE
        # entry"; matters for ``if rec_type in STATUS_BY_TYPE`` gating.
        statuses=frozenset(),
        available_in_scopes=frozenset({SCOPE_CANONICAL}),
    ),
    TypeDefinition(
        name="account",
        directory="account",
        statuses=frozenset({"active", "suspended", "closed", "pending"}),
        available_in_scopes=frozenset({SCOPE_CANONICAL}),
    ),
    TypeDefinition(
        name="asset",
        directory="asset",
        statuses=frozenset({"active", "retired", "maintenance", "disposed"}),
        available_in_scopes=frozenset({SCOPE_CANONICAL}),
    ),
    TypeDefinition(
        name="conversation",
        directory="conversation",
        statuses=frozenset({"active", "waiting", "resolved", "closed", "archived"}),
        name_field="subject",
        available_in_scopes=frozenset({SCOPE_CANONICAL}),
    ),

    # Learning types (distiller-generated). All flagged is_learn_type
    # and is_leaf (forward references via source_links, never inbound).
    TypeDefinition(
        name="assumption",
        directory="assumption",
        statuses=frozenset({"active", "challenged", "invalidated", "confirmed"}),
        available_in_scopes=frozenset({SCOPE_CANONICAL}),
        is_learn_type=True,
        is_leaf=True,
    ),
    TypeDefinition(
        name="constraint",
        directory="constraint",
        statuses=frozenset({"active", "expired", "waived", "superseded"}),
        available_in_scopes=frozenset({SCOPE_CANONICAL}),
        is_learn_type=True,
        is_leaf=True,
    ),
    TypeDefinition(
        name="contradiction",
        directory="contradiction",
        statuses=frozenset({"unresolved", "resolved", "accepted"}),
        available_in_scopes=frozenset({SCOPE_CANONICAL}),
        is_learn_type=True,
        is_leaf=True,
    ),
    TypeDefinition(
        name="synthesis",
        directory="synthesis",
        statuses=frozenset({"draft", "active", "superseded"}),
        available_in_scopes=frozenset({SCOPE_CANONICAL}),
        is_learn_type=True,
        is_leaf=True,
    ),

    # Operator-preference V1 (2026-05-24, project_operator_preferences_v1).
    # Cross-instance — Salem is the canonical authority for universal
    # operator-preference records (Shape A action gates + Shape B voice
    # directives that apply to ALL instances). Hypatia keeps local
    # instance-application records in her own vault (``library-
    # alexandria/preference/``); local-wins-over-canonical conflict
    # resolution lives at the talker system-prompt assembly layer.
    # KAL-LE has no preference records in V1 (not a heavy talker surface).
    # See ``project_operator_preferences_v1.md`` for the full contract.
    #
    # Required fields: ``name`` (unaddressable without), ``shape``
    # (consumers can't decide action-gate vs voice-block dispatch
    # without), ``scope`` (universal-vs-instance routing ambiguous
    # without). ``matcher`` is shape-A-only — that gate lives in the
    # consumer module, not the schema.
    #
    # Status set: ``active`` (preference applies), ``revoked``
    # (operator explicitly withdrew). NOT a supersedes-chain — revocation
    # is a status mutation on the same record so the canonical/local
    # resolver reads a single value per record.
    TypeDefinition(
        name="preference",
        directory="preference",
        statuses=frozenset({"active", "revoked"}),
        required_fields=("name", "shape", "scope"),
        available_in_scopes=frozenset({SCOPE_CANONICAL}),
    ),

    # --- VERA scope extension (Ben's RRTS ops co-pilot) ----------------
    #
    # VERA MVP (2026-06-09, project_vera_ops_assistant.md) — trouble-ticket
    # intake ONLY. Ben reports RRTS website bugs / improvement ideas via
    # Telegram; VERA scopes them into a dev-ready ``ticket`` record whose
    # body is a clean Claude-Code handoff brief (body format owned by the
    # prompt-tuner's vault-vera SKILL; the frontmatter contract is here).
    #
    # ``available_in_scopes={"vera", "vera_ops", "kalle", "vera_forwarder"}``
    # — NOT canonical. This is the schema-side gate of the two-gate
    # contract: ``_validate_type`` accepts ``ticket`` only under the
    # tagged scopes and REJECTS it everywhere else (Salem / Hypatia
    # can't create tickets — correct per-instance isolation).
    #
    # Scope roster (ratified VERA→KAL-LE→GitHub ticket pipeline,
    # 2026-06-11):
    #   * ``vera`` / ``vera_ops`` — ticket ORIGIN (VERA MVP 2026-06-09):
    #     Ben/Andrew file tickets via the VERA interview.
    #   * ``kalle`` — ticket BACKLOG KEEPER (pipeline c2): VERA pushes
    #     tickets over the peer protocol; KAL-LE's deterministic intake
    #     handler RECORDS them in aftermath-lab's ``ticket/`` queue and
    #     files the GitHub issue (KAL-LE is the single GitHub-credential
    #     holder — see ``integrations/github_ops.py``).
    #   * ``vera_forwarder`` — read+link-back only. Gate 1 fires on
    #     CREATE and LIST (``vault_list`` calls ``_validate_type``);
    #     the forwarder's ``list: True`` needs the tag even though its
    #     create stays denied at gate 2.
    #
    # The scope-side gate lives in ``vault/scope.py``
    # (``VERA_OPS_CREATE_TYPES`` / ``VERA_CREATE_TYPES`` /
    # ``KALLE_CREATE_TYPES`` / ``VERA_FORWARDER_EDIT_TYPES``); keep the
    # registries in sync — drift surfaces as "type accepted by
    # validator, rejected by scope" or vice versa (same failure class
    # the kalle / hypatia comments warn about).
    #
    # Status set (operator-ratified 2026-06-09): ``open`` (new, default on
    # create), ``in_progress`` (dev picked it up), ``resolved`` (fixed,
    # pending verification), ``closed`` (done + verified), ``wont_fix``
    # (triaged out). resolve/close are status edits, not moves/deletes —
    # see the ``vera_ops`` scope's move/delete=False rules.
    #
    # Required fields (gate creation; all determinable by VERA itself from
    # the interview + sender identity + the RRTS component list, so none
    # demand technical knowledge from a non-technical reporter): ``title``
    # (the name_field — short imperative summary), ``ticket_type``
    # (bug | enhancement), ``reporter`` (who filed it), ``area`` (RRTS
    # website component — free-text for P0 per Decision D; enum-later in
    # P1 once the component list lands). priority / environment /
    # screenshots and the body diagnostic fields (repro steps, expected /
    # actual) are OPTIONAL-BUT-ELICITED — the SKILL interviews for them
    # but the schema never gates on them. A ticket with
    # ``environment: unknown`` and no repro steps is a valid, creatable
    # ticket.
    #
    # ``name_field="title"`` — tickets are titled, not "name"d. Mirrors
    # ``conversation`` / ``input`` using ``subject`` as their name field.
    #
    # ``is_leaf=True`` — tickets are terminal: nothing in the VERA vault
    # links INTO a ticket, so zero inbound wikilinks is the norm, not an
    # ORPHAN001 defect (same reasoning as note / run / the learning types).
    #
    # Held-state fields (2026-06-29, RRTS bug-report → VERA lane) —
    # ``origin`` (e.g. ``"rrts"`` / ``"telegram"``) + ``de_phi_status``
    # (``"pending"`` default for rrts-origin, ``"cleared"`` set later by the
    # separate de-PHI arc, ``"n/a"`` for telegram-origin zero-PHI tickets)
    # are OPTIONAL free-text frontmatter (NOT in required_fields, NOT
    # status-gated). Schema-tolerance is automatic: they're plain
    # frontmatter, so a ticket WITHOUT them loads + validates exactly as
    # before (the forward guard reads them via ``fm.get(...)`` with a
    # default-deny on rrts-origin). 🔒 The forward guard
    # (``transport/ticket_forward.scan_tickets``) EXCLUDES any
    # ``origin: rrts`` ticket whose ``de_phi_status != "cleared"`` — the
    # held-state interlock. Nothing in this arc sets ``cleared``.
    TypeDefinition(
        name="ticket",
        directory="ticket",
        statuses=frozenset({
            "open", "in_progress", "resolved", "closed", "wont_fix",
        }),
        required_fields=("title", "ticket_type", "reporter", "area"),
        name_field="title",
        available_in_scopes=frozenset({
            "vera", "vera_ops", "kalle", "vera_forwarder",
            # ``vera_ticket_outcome`` (2026-06-15, pipeline c7) — the
            # VERA-side resolver for the KAL-LE→VERA outcome write-back.
            # Gate 1 (_validate_type) fires on list/edit, so the resolver
            # scope must tag here too or its vault_edit is rejected as
            # "Unknown type under scope 'vera_ticket_outcome'" before it
            # reaches the scope gate. See
            # scope.py::VERA_TICKET_OUTCOME_EDIT_FIELDS.
            "vera_ticket_outcome",
            # ``rrts_intake`` (2026-06-29, RRTS bug-report → VERA lane) —
            # the vouched web-relay intake scope files held tickets. Gate 1
            # must admit ``ticket`` under this scope or vault_create is
            # rejected as "Unknown type under scope 'rrts_intake'" before
            # the scope gate (``rrts_intake_ticket_only``) ever runs. See
            # scope.py::RRTS_INTAKE_CREATE_TYPES.
            "rrts_intake",
        }),
        is_leaf=True,
    ),

    # --- VERA-clinical scope extension (sovereign ambient scribe) -------
    #
    # ``clinical_note`` (2026-07, scribe P1-b) — the AI-drafted, human-
    # attested clinical note produced by the on-box sovereign scribe on the
    # VERA-clinical slot. This is the record the whole sovereign no-egress
    # boundary (scribe P1-a) exists to protect: it holds PHI and must NEVER
    # reach a cloud provider, so it is scoped, gated, and denied-from-egress
    # at every layer.
    #
    # ``available_in_scopes=frozenset({"stayc_clinical"})`` ONLY — NOT
    # canonical. This is the schema-side gate 1 of the two-gate contract:
    # ``_validate_type`` (via ``known_types("stayc_clinical")``) admits
    # ``clinical_note`` ONLY under the stayc_clinical scope and REJECTS it
    # everywhere else (Salem / KAL-LE / Hypatia / VERA-ops cannot create or
    # even validate a clinical note — correct per-instance isolation). This
    # AUTO-DERIVES ``KNOWN_TYPES_BY_SCOPE["stayc_clinical"]`` via the live
    # comprehension over ``TYPE_REGISTRY`` (VERA-P1 trap confirmed absent;
    # do NOT touch a KNOWN_TYPES_BY_SCOPE literal). Gate 2
    # (``stayc_clinical_types_only`` in scope.py) then enforces the
    # create policy.
    #
    # ``name_field="title"`` — clinical notes are titled by encounter (mirror
    # ``ticket`` / ``conversation`` using a non-"name" title field).
    #
    # Statuses (the attest lifecycle): ``ai_draft`` (default on create — the
    # machine draft, unsigned), ``attested`` (human clinician has reviewed +
    # signed; ``attested_by`` / ``attested_at`` set on the flip), ``amended``
    # (a correction was made AFTER attestation — via a NEW clinical_note that
    # supersedes, NOT a body rewrite; see ``_BODY_MUTATE_DENIED_TYPES``).
    #
    # ``required_fields=("title",)`` — minimal gate; the frozen frontmatter
    # contract (``ai_draft`` / ``synthetic`` / ``attested_by`` /
    # ``attested_at`` / ``draft_original`` / ``status``) is optional-at-schema
    # so a draft with only a title validates, but the scribe pipeline + the
    # vault-vera-clinical SKILL (prompt-tuner) populate the full set. The
    # ``synthetic: true`` provenance tag is the fail-closed mode line
    # (scribe.mode gate, P1-c) — enforced in the pipeline, not schema-gated.
    #
    # ``is_leaf=True`` — nothing links INTO a clinical note; zero inbound
    # wikilinks is the norm, not an ORPHAN001 defect (same as ticket / the
    # learning types).
    #
    # Anti-spoliation is enforced in scope.py: ``clinical_note`` is in BOTH
    # ``_DELETE_DENIED_TYPES`` (no scope may delete) and
    # ``_BODY_MUTATE_DENIED_TYPES`` (no body_insert_at/body_replace — amend =
    # NEW record); the ``stayc_clinical`` scope sets move/delete=False and its
    # attest-only edit gate freezes the body. And ``_NEVER_PUSH_TYPES``
    # (below) forbids it ever crossing an instance boundary.
    TypeDefinition(
        name="clinical_note",
        directory="clinical_note",
        statuses=frozenset({"ai_draft", "attested", "amended"}),
        required_fields=("title",),
        name_field="title",
        available_in_scopes=frozenset({"stayc_clinical"}),
        is_leaf=True,
    ),

    # --- KAL-LE scope extensions (``~/aftermath-lab/``) ----------------
    #
    # Stage 3.5: record types KAL-LE uses inside the aftermath-lab
    # vault. Kept separate from the canonical set so Salem's
    # operational world stays focused. The kalle scope check (see
    # ``vault/scope.py::KALLE_CREATE_TYPES``) intersects these with
    # its own create allowlist.
    #
    # ``pattern`` = reusable development pattern.
    # ``principle`` = higher-level development principle.
    TypeDefinition(
        name="pattern",
        directory=None,
        statuses=None,
        available_in_scopes=frozenset({"kalle"}),
    ),
    TypeDefinition(
        name="principle",
        directory=None,
        statuses=None,
        available_in_scopes=frozenset({"kalle"}),
    ),
    # ``architecture`` (added 2026-05-04) — multi-instance system design
    # + information-sharing decisions. Descriptive of THIS system (vs
    # ``pattern`` which is reusable how-to extracted FROM the system).
    # Examples: architecture/canonical-authority.md,
    # architecture/PHI-firewall-design.md, architecture/peer-protocol.md.
    # KAL-LE-only — Salem and Hypatia have no use case for this type.
    # aftermath-lab already has an architecture/ directory with operational
    # docs (deployment.md, testing.md); this registration adds schema
    # validation + scope-aware tooling to records placed there.
    #
    # Same status set as synthesis — drafts evolve, become active when
    # ratified, get superseded when the design changes. Strict-but-small;
    # widen via deliberate decision if a real workflow needs another state.
    TypeDefinition(
        name="architecture",
        directory="architecture",
        statuses=frozenset({"draft", "active", "superseded"}),
        available_in_scopes=frozenset({"kalle"}),
    ),

    # --- Hypatia scope extensions (``~/library-alexandria/``) ----------
    #
    # Hypatia operates inside library-alexandria (see
    # ``library-alexandria/CLAUDE.md`` for directory layout +
    # frontmatter shapes). Like the kalle set, kept separate from the
    # canonical set so Salem's operational vault doesn't gain
    # Hypatia-only types. The ``hypatia`` scope check (see
    # ``vault/scope.py::HYPATIA_CREATE_TYPES``) is the authoritative
    # create allowlist.

    TypeDefinition(
        name="document",
        directory=None,
        statuses=None,
        # ``web_ingest`` (2026-06-29, cross-instance ingest) — gate 1 must
        # admit ``document`` under the web_ingest scope. KNOWN_TYPES_BY_SCOPE
        # auto-derives, so tagging here is the ONLY edit needed (do NOT touch
        # a KNOWN_TYPES_BY_SCOPE literal). Gate 2 (web_ingest_types_only)
        # enforces the {document, note, source} create policy.
        available_in_scopes=frozenset({"hypatia", "web_ingest"}),
    ),
    TypeDefinition(
        name="concept",
        directory=None,
        statuses=None,
        available_in_scopes=frozenset({"hypatia"}),
    ),
    TypeDefinition(
        name="source",
        directory=None,
        statuses=None,
        # ``web_ingest`` (2026-06-29) — see the ``document`` note above.
        available_in_scopes=frozenset({"hypatia", "web_ingest"}),
    ),
    TypeDefinition(
        name="citation",
        directory=None,
        statuses=None,
        available_in_scopes=frozenset({"hypatia"}),
    ),
    # Hypatia ``template`` records — prose-form scaffolds (essay
    # scaffolds, reusable section structures, etc.). Routed to
    # ``prose-templates/`` to disambiguate from Obsidian's per-type
    # ``_templates/`` directory (the scaffold/_templates layer shipped
    # with the bundled vault contains placeholder-bearing per-record-
    # type markdown templates; Hypatia's ``template`` type is a
    # different concept entirely — operator-curated prose forms).
    # Latent orphan-path bug fixed 2026-05-12: SKILL was renamed
    # ``template/`` → ``prose-templates/`` in ``a14e0ab`` (vault-side
    # ``mv`` performed by team-lead), but TYPE_DIRECTORY had no entry
    # so the ``.get(record_type, record_type)`` fallback routed writes
    # to the now-empty ``template/`` orphan directory.
    TypeDefinition(
        name="template",
        directory="prose-templates",
        statuses=None,
        available_in_scopes=frozenset({"hypatia"}),
    ),

    # Phase 2.5 fiction posture (project_hypatia_phase2_followups.md):
    # six ``fiction-{element}`` types added so both scaffolding paths
    # (the slash command + the SKILL natural-language path) can call
    # ``vault_create`` for fiction records — the slash command writes
    # the directory + 5 element files atomically, but ongoing work
    # (a new character record, a re-keyed structure file) goes through
    # regular ``vault_create``. Without these types in the registry,
    # every such write fails ``_validate_type``.
    TypeDefinition(
        name="fiction-continuity",
        directory=None,
        statuses=None,
        available_in_scopes=frozenset({"hypatia"}),
    ),
    TypeDefinition(
        name="fiction-story",
        directory=None,
        statuses=None,
        available_in_scopes=frozenset({"hypatia"}),
    ),
    # Fiction-structure (2026-05-16, operator-template #2 ship — the
    # 24-Chapter Story Template). Lifecycle covers the four states an
    # outline / draft moves through: ``outlining`` (initial state —
    # operator filling in beats), ``drafting`` (chapters being written),
    # ``revising`` (post-draft editing), ``complete`` (finished work).
    # The other five fiction-* types (story, world, voice, character,
    # continuity) do not have status sets yet — added when their
    # respective templates ship.
    TypeDefinition(
        name="fiction-structure",
        directory=None,
        statuses=frozenset({"outlining", "drafting", "revising", "complete"}),
        available_in_scopes=frozenset({"hypatia"}),
    ),
    TypeDefinition(
        name="fiction-world",
        directory=None,
        statuses=None,
        available_in_scopes=frozenset({"hypatia"}),
    ),
    TypeDefinition(
        name="fiction-voice",
        directory=None,
        statuses=None,
        available_in_scopes=frozenset({"hypatia"}),
    ),
    TypeDefinition(
        name="fiction-character",
        directory=None,
        statuses=None,
        available_in_scopes=frozenset({"hypatia"}),
    ),

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
    #
    # Status set covers natural workflow: planned (scheduled ahead of
    # time), in_progress (mid-session, e.g. live practice update),
    # completed (most common — written after the session), skipped
    # (intended-to-do but didn't, useful signal for the tracker
    # aggregator).
    TypeDefinition(
        name="practice-session",
        directory="practice-session",
        statuses=frozenset({"planned", "in_progress", "completed", "skipped"}),
        available_in_scopes=frozenset({"hypatia"}),
    ),

    # Voice/method training (2026-05-07, /train + /method-source arc).
    # Four new top-level types — registered so the Hypatia create
    # allowlist can admit them. The shape:
    #   - ``essay``        — raw published essay, lands at
    #                        ``document/essay/<slug>.md``. Distinct
    #                        from a generic ``note`` because the routing
    #                        is type-driven; the f006c48e routing bug
    #                        landed because ``vault_create type=note``
    #                        was the outer call and the inner
    #                        ``type: essay`` in set_fields was
    #                        overridden by ops. Adding ``essay`` as
    #                        first-class fixes it.
    #   - ``voice``        — leaf voice profile at ``voice/<slug>.md``.
    #                        One per source essay; optional ``cluster``
    #                        frontmatter for grouping into cluster-
    #                        summary tier.
    #   - ``voice-cluster`` — cluster-tier voice summary at
    #                        ``voice/cluster/<name>.md``. Built async
    #                        when ≥2 leaves share a ``cluster:`` tag.
    #   - ``method``       — method/system profile at
    #                        ``method/<slug>.md``. Structured extraction
    #                        of a method document (paired with raw
    #                        ``source`` record).
    #
    # Status sets cover extraction-worker lifecycle. The
    # ``insufficient-evidence`` / ``no-overall-invariants`` /
    # ``incoherent-cluster`` / ``not-a-method`` values are intentionally-
    # left-blank sentinels (2026-05-07 prompt-tuner pass) — the writer
    # must pass through LLM-emitted "no signal" status WITHOUT silent
    # substitution to ``active`` (operator must SEE that extraction
    # emitted no signal rather than reading a fabricated profile).
    TypeDefinition(
        name="essay",
        directory="document/essay",
        statuses=frozenset({"draft", "published", "archived"}),
        available_in_scopes=frozenset({"hypatia"}),
    ),
    TypeDefinition(
        name="voice",
        directory="voice",
        statuses=frozenset({
            "pending", "active", "superseded", "failed",
            "insufficient-evidence", "no-overall-invariants",
        }),
        available_in_scopes=frozenset({"hypatia"}),
    ),
    TypeDefinition(
        name="voice-cluster",
        directory="voice/cluster",
        statuses=frozenset({"active", "stale", "incoherent-cluster"}),
        available_in_scopes=frozenset({"hypatia"}),
    ),
    TypeDefinition(
        name="method",
        directory="method",
        statuses=frozenset({
            "pending", "active", "superseded", "failed",
            "not-a-method",
        }),
        available_in_scopes=frozenset({"hypatia"}),
    ),

    # Author (2026-05-16, capture-mode source-anchor arc) — index works
    # by author. Filename = ``author/<last_name>.md``; ``last_name``
    # frontmatter field is the lookup key. Records of type ``source``
    # populate ``author`` as a wikilink (``[[author/<Lastname>]]``)
    # when the source has a registered author; free-text values stay
    # tolerated for backward compatibility with pre-2026-05-16 source
    # records (e.g. ``author: Carlo Atendido``).
    #
    # Status set intentionally small: ``active`` is default after
    # creation; ``merged`` flags a record consolidated into another
    # author entry (e.g. two ``Smith.md`` records resolved by operator)
    # — the merged record is kept for audit so existing wikilinks
    # don't dangle.
    TypeDefinition(
        name="author",
        directory="author",
        statuses=frozenset({"active", "merged"}),
        available_in_scopes=frozenset({"hypatia"}),
    ),

    # Zettelkasten schema cutover (2026-05-16, Phase 1) — five new
    # Hypatia-only types per ``project_hypatia_zettelkasten_redesign.md``
    # "LOCKED IMPLEMENTATION PLAN".
    #
    #   - ``memo`` — fleeting single-thought capture. Created by
    #     capture-mode auto-branch when session has <=1 user message at
    #     /end (the "I just had this thought" path that doesn't warrant
    #     a structured extraction). No status entry — lifecycle is
    #     implicit; ``_validate_status`` returns silently for types not
    #     in STATUS_BY_TYPE.
    #   - ``zettel`` — atomic Zettelkasten records: synthesis / category
    #     / definitional sub-shapes all covered by one flexible template
    #     (type-minimalism guardrail). Capture-mode multi-message
    #     extraction targets ``zettel/`` instead of the prior ``note/``
    #     default. Loose status set for category-shape Z's using a
    #     status header like "Seen, Unvalidated" — most zettels carry
    #     no status. Three lifecycle values: ``open`` → ``refined`` →
    #     ``superseded`` (supersede-by-default opinion-drift pattern).
    #   - ``MOC`` — Maps of Content. Topic organizers with hierarchical
    #     Contents trees. Mixed-case ``MOC`` literal preserved per
    #     Andrew's existing convention. No status — operator-led
    #     organizational artifact; Hypatia maintains member lists.
    #   - ``question`` — elevated atomic question records, spawned from
    #     inline ``# Follow Up Questions`` sections in source/zettel
    #     records when the question deserves tracking as its own atom.
    #     Status: ``open`` (initial), ``refined`` (text iterated),
    #     ``answered`` (resolution linked via ``answered_by``),
    #     ``superseded`` (replaced by sharper question).
    #   - ``research-pointer`` — elevated atomic research action, spawned
    #     from inline ``# Research Ideas`` similarly. Status: ``open`` →
    #     ``in-progress`` → ``completed`` (linked via ``produces``) /
    #     ``dropped``.
    TypeDefinition(
        name="memo",
        directory="memo",
        statuses=None,
        available_in_scopes=frozenset({"hypatia"}),
    ),
    TypeDefinition(
        name="zettel",
        directory="zettel",
        statuses=frozenset({"open", "refined", "superseded"}),
        available_in_scopes=frozenset({"hypatia"}),
    ),
    TypeDefinition(
        name="MOC",
        # Mixed-case ``MOC`` literal preserved per Andrew's existing
        # convention (``Practical Stoicism MOC.md`` etc.). WSL ext4 is
        # case-sensitive so this is unambiguous on the running filesystem.
        directory="MOC",
        statuses=None,
        available_in_scopes=frozenset({"hypatia"}),
    ),
    TypeDefinition(
        name="question",
        directory="question",
        statuses=frozenset({"open", "refined", "answered", "superseded"}),
        available_in_scopes=frozenset({"hypatia"}),
    ),
    TypeDefinition(
        name="research-pointer",
        # Hyphen preserved — directory names tolerate hyphens and the
        # type name is already hyphenated.
        directory="research-pointer",
        statuses=frozenset({"open", "in-progress", "completed", "dropped"}),
        available_in_scopes=frozenset({"hypatia"}),
    ),

    # Article (2026-05-17, operator-template #1 ship — Substack /
    # Andrew Errant / future-venue published-writing records). Distinct
    # from ``essay`` which is for source essays Andrew READS (those
    # route to ``document/essay/`` and feed the /train voice-extraction
    # workflow). ``article`` is for essays Andrew WRITES himself, with
    # a Hot-Take / Story / Takeaway / CTA structure baked into the
    # template. Lifecycle: ``draft`` (initial — operator writing),
    # ``scheduled`` (queued for publication on a future date — Substack
    # supports this natively), ``published`` (live), ``archived``
    # (operator-removed from active rotation).
    TypeDefinition(
        name="article",
        directory="article",
        statuses=frozenset({"draft", "scheduled", "published", "archived"}),
        available_in_scopes=frozenset({"hypatia"}),
    ),

    # Routine (2026-05-26, Phase 1 — replaces Andrew's Trello daily
    # templates). Salem-only canonical type: a routine defines a set
    # of recurring items (daily walks, weekly chores, monthly check-ins,
    # critical medication reminders) with per-item priority and an
    # append-only completion_log. The aggregator daemon reads all active
    # routine records each morning at 05:59 Halifax and writes a
    # derivative ``vault/daily/<date>.md`` note grouping items by
    # priority (Critical / Tracked / Aspirational). Brief integration
    # surfaces that note at 06:00 in the "Today's Routines" section.
    #
    # Cadence: a small dict on each record (see ``routine/cadence.py``)
    # — six shapes: daily, weekly (by weekday list), every_n_days
    # (with anchor), monthly (day-of-month, supports 'last'), monthly
    # (nth weekday), every_n_months (with anchor). Routed through a
    # hand-rolled dispatcher rather than rrule — six shapes cover every
    # operator template and the implementation is ~80 lines vs the
    # dateutil dependency.
    #
    # Required fields: ``name`` (display title — appears in brief
    # section), ``cadence`` (dict — without it the aggregator cannot
    # decide whether today is a fire day), ``items`` (list of dicts —
    # the unit of work being scheduled). The optional ``completion_log``
    # frontmatter accumulates per-item ISO date strings on each
    # ``alfred routine done <record> <item>`` call.
    #
    # Body is auto-rendered by the operator from the template (template
    # provides a placeholder ``# Items`` / ``# History`` section
    # pointing readers at the frontmatter source-of-truth). The type is
    # included in ``_BODY_MUTATE_DENIED_TYPES`` so insert_at / replace
    # are universally forbidden — agents touch the completion_log
    # via the CLI, not via body rewrites.
    #
    # Status set: ``active`` (firing), ``archived`` (operator-paused;
    # retained for completion-log audit but skipped by the aggregator).
    # Salem-only — Hypatia and KAL-LE have no use case in V1; future
    # instances may opt in by adding ``available_in_scopes`` entries.
    TypeDefinition(
        name="routine",
        directory="routine",
        statuses=frozenset({"active", "archived"}),
        required_fields=("name", "cadence", "items"),
        available_in_scopes=frozenset({SCOPE_CANONICAL}),
    ),
    # Daily aggregator record (2026-05-31, c6 of routine arc — registered
    # alongside talker tier_curation scope expansion). ``vault/daily/
    # <YYYY-MM-DD>.md`` records are written by the routine aggregator at
    # 05:59 ADT each morning AND may now be pre-written by the talker for
    # future-date tier_curation pre-set ("set tomorrow's tier list").
    #
    # Body content + most frontmatter fields (``date``, ``routines_
    # contributing``, ``critical_pending``) are aggregator-owned and
    # rewritten on every aggregator fire. The ONE field the talker is
    # permitted to pre-set / edit is ``tier_curation``, gated through
    # the new ``talker_tier_curation_only`` scope path (see scope.py:
    # ``TALKER_TIER_CURATION_TYPES`` + ``TALKER_TIER_CURATION_FIELDS``).
    # ``_load_existing_tier_curation`` in aggregator.py preserves any
    # pre-set block when the aggregator next fires.
    #
    # No status — daily records are timestamp-keyed and don't carry
    # operational status (the aggregator fires once per day; stale runs
    # overwrite). ``required_fields=("date",)`` because every daily
    # record carries the ISO date as a top-level frontmatter field;
    # the aggregator and talker both populate it. ``is_leaf`` because
    # other records aren't expected to wikilink TO a daily/ record
    # (the daily is a derivative aggregation, not a canonical record).
    TypeDefinition(
        name="daily",
        directory="daily",
        statuses=None,
        required_fields=("date",),
        available_in_scopes=frozenset({SCOPE_CANONICAL}),
        is_leaf=True,
    ),
]


# The canonical registry. Module-level singleton; consumers may import
# this directly OR use the derived globals below.
TYPE_REGISTRY: TypeRegistry = TypeRegistry(_DEFINITIONS)


# ---------------------------------------------------------------------------
# Derived globals — backward-compat views off TYPE_REGISTRY.
# ---------------------------------------------------------------------------
#
# These names are imported by 10+ files across the codebase. Preserving
# the names + shapes lets the consolidation refactor land WITHOUT
# touching consumers. New code should prefer the registry methods; the
# globals remain stable so the consumers can migrate at their own pace.
#
# CONTRACT: every assignment below must produce a shape (set / dict /
# tuple) IDENTICAL to what the pre-refactor literal declaration
# produced. Tests in ``tests/test_type_registry.py`` pin this.

KNOWN_TYPES: set[str] = set(TYPE_REGISTRY.known_types())

# Per-scope extension sets — JUST the types tagged with that scope,
# NOT the canonical merge. ``KNOWN_TYPES_BY_SCOPE`` below performs
# the union; consumers that want JUST the extension types
# (e.g. some SKILL.md cross-references) use these.
KNOWN_TYPES_HYPATIA: set[str] = set(TYPE_REGISTRY.types_in_scope("hypatia"))
KNOWN_TYPES_KALLE: set[str] = set(TYPE_REGISTRY.types_in_scope("kalle"))

# Per-scope union of known record types. ``vault.ops._validate_type``
# uses this to gate ``vault_create`` / ``vault_list`` against the right
# type set: a Hypatia agent legitimately creates ``document`` records
# (canonical-only would reject them); a Salem agent must NOT be able
# to create ``pattern`` records (KAL-LE-only).
#
# Scopes not listed here fall back to the canonical KNOWN_TYPES only.
# The dict's purpose is "which extension sets does this scope unlock?"
# — not "what may this scope create?" (that's the create allowlists in
# ``vault.scope`` — KALLE_CREATE_TYPES, HYPATIA_CREATE_TYPES,
# TALKER_CREATE_TYPES). Two-layer check: this gate lets the type
# through ``_validate_type``; the create allowlist in ``check_scope``
# then enforces the per-scope policy.
#
# Pattern-trigger note: when a future instance (V.E.R.A., STAY-C)
# adds its own scope extensions, add definitions to ``_DEFINITIONS``
# with the new scope name in ``available_in_scopes`` — this dict
# auto-populates from the registry.
#
# 2026-06-09 (VERA MVP): this dict is now ACTUALLY auto-populated from
# every non-canonical scope any ``TypeDefinition`` tags, rather than a
# hardcoded ``{"kalle", "hypatia"}`` literal. The prior literal silently
# omitted ``vera`` / ``vera_ops`` even though the ``ticket`` TypeDefinition
# tagged them — so ``_validate_type`` fell back to canonical KNOWN_TYPES
# and rejected ``ticket`` creation under the VERA scopes (the type-gate
# never consulted ``available_in_scopes``). Deriving the scope key set
# from the registry closes that gap permanently: the comment above
# ("auto-populates from the registry") is now true, and the NEXT
# instance's scope-tagged types validate without touching this line.
#
# ``SCOPE_CANONICAL`` is excluded — it's the every-scope base set, not a
# per-instance extension key. ``known_types(scope)`` already unions
# canonical with the scope's tagged types, so each value is the full
# valid set for that scope (matching the prior literal's shape).
_EXTENSION_SCOPES: set[str] = {
    s
    for d in TYPE_REGISTRY
    for s in d.available_in_scopes
    if s != SCOPE_CANONICAL
}
KNOWN_TYPES_BY_SCOPE: dict[str, set[str]] = {
    scope_name: set(TYPE_REGISTRY.known_types(scope_name))
    for scope_name in _EXTENSION_SCOPES
}


LEARN_TYPES: set[str] = {
    d.name for d in TYPE_REGISTRY if d.is_learn_type
}


# Record types that must NEVER cross an instance boundary — not over the
# peer protocol, not via propose, not in a query response — even
# de-identified. Co-located with the type metadata (schema.py) so every
# outbound serializer imports ONE source of truth.
#
# ``clinical_note`` (scribe P1-b) holds PHI. De-identified cross-instance
# transit waits on a legal de-id standard that does not exist yet, so the
# answer today is a flat NO — the record stays on the sovereign box. This is
# a LATENT belt at present: the sovereign VERA-clinical slot wires no
# transport at all (the P1-a barrier-(d) allowlist forbids ``transport`` /
# peer-push), so nothing on that box can push anything. The guard is
# load-bearing the day ANY instance that holds clinical records gains a
# transport surface — the outbound serializers (``peer_propose_canonical_
# record``, the ticket forwarder, any future push path) refuse a never-push
# type before it can leave the box.
_NEVER_PUSH_TYPES: frozenset[str] = frozenset({"clinical_note"})


def is_never_push(record_type: str) -> bool:
    """Return True iff ``record_type`` must never cross an instance boundary.

    Import this at every outbound push/propose serializer and refuse (or
    skip) a record whose type it flags. One source of truth
    (:data:`_NEVER_PUSH_TYPES`) so a new never-push type is enforced
    everywhere by a single edit here.
    """
    return record_type in _NEVER_PUSH_TYPES


STATUS_BY_TYPE: dict[str, set[str]] = {
    d.name: set(d.statuses) for d in TYPE_REGISTRY if d.statuses is not None
}


# Type → expected top-level directory. ONLY types with an explicit
# directory entry appear here; types relying on the default fallback
# (``TYPE_DIRECTORY.get(t, t)`` returns ``t`` for missing entries) are
# omitted. The omission is load-bearing — ``set(TYPE_DIRECTORY.values())``
# is used by ``janitor/scanner.py`` to identify entity-directory body
# wikilinks; silently expanding the values set would change scan
# behavior. See ``TypeDefinition.directory`` docstring.
TYPE_DIRECTORY: dict[str, str] = {
    d.name: d.directory for d in TYPE_REGISTRY if d.directory is not None
}


# Per-type ADDITIONAL required fields. ``_validate_required_fields``
# checks the universal ``REQUIRED_FIELDS`` list (below) for every
# record, then checks the per-type extras here when the record's type
# has an entry. Empty tuples are omitted (= same semantics as
# ``dict.get(t, [])``).
REQUIRED_FIELDS_BY_TYPE: dict[str, list[str]] = {
    d.name: list(d.required_fields)
    for d in TYPE_REGISTRY
    if d.required_fields
}


# Types that use a different frontmatter field than the default
# ``"name"`` for their canonical title.
NAME_FIELD_BY_TYPE: dict[str, str] = {
    d.name: d.name_field for d in TYPE_REGISTRY if d.name_field != "name"
}


# Record types that are terminal-by-design — no other record is
# expected to point at them, so they should NOT fire ORPHAN001 just
# for having zero inbound wikilinks.
#
# Conservative starting set, validated against the 2026-04-30 residual
# categorization (`project_distiller_janitor_sweep_log.md` "Janitor
# 1182 residual categorization"). 2026-05-06 expansion added the
# epistemic types (synthesis/contradiction/decision/assumption/
# constraint) — distiller-generated learnings extracted FROM source
# records carry forward references via ``source_links``; back-refs
# would require mutating source records on every distiller fire,
# which breaks the deterministic-writer principle.
#
# Deliberately omitted (separate policy decisions):
#   - ``task``: mixed bag. Some real (sub-task hierarchies), some
#     terminal — needs a different rule (link-by-status?).
#
# Adding a new type here should be backed by data showing the type is
# overwhelmingly terminal — don't generalize from one example. To add
# a type, set ``is_leaf=True`` on its ``TypeDefinition``.
LEAF_TYPES: set[str] = {d.name for d in TYPE_REGISTRY if d.is_leaf}


# ---------------------------------------------------------------------------
# Non-per-type registries — left as direct module-level constants.
# ---------------------------------------------------------------------------
#
# These aren't keyed by record type; they're orthogonal frontmatter-
# field registries used across multiple types. Keeping them as
# top-level constants (rather than threading them through
# ``TypeDefinition``) is the simpler shape.


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
#   - ``gcal_sync``           — per-event sync POLICY (consolidation
#     Step 4, event↔GCal decouple). ``"sync"`` (project to Google
#     Calendar — the default) or ``"none"`` (never project; remind-only,
#     e.g. birthdays/anniversaries — the brief's upcoming-events still
#     surfaces them since reminders read the record directly, not GCal).
#     The event's IDENTITY is the record; GCal is one OPTIONAL output
#     channel. ABSENT field → treated as ``"sync"`` so every existing
#     event keeps its current behaviour (behavior-preserving default).
#     Resolved via ``alfred.integrations.gcal_sync.resolve_sync_policy``;
#     the gate lives INSIDE the four sync funcs so all entry points
#     (hooks, backfill CLI, peer propose-create) honour it un-bypassably.
#   - ``gcal_collapse_key``   — same-day collapse SERIES LABEL (consolidation
#     Step 4 §3, the rTMS umbrella). A CLEAN label (e.g. ``"rTMS"``), NOT
#     date-stamped: events sharing ``(gcal_collapse_key, date)`` project to
#     ONE GCal entry spanning earliest-start → latest-end (title auto-summary
#     ``"<key> — N sessions (HH:MM–HH:MM)"``). The first synced / existing-id
#     member is the PRIMARY (owns the single ``gcal_event_id``); siblings are
#     "synced via the primary" (carry the key, no own id). ABSENT → no
#     collapse (the plain per-event path; today's behaviour). Operators /
#     Salem set it via ``vault_edit`` (rides the talker ``edit`` permission,
#     like ``gcal_title``); the sync layer's ``sync_collapse_group``
#     coordinator does the grouping. Resolved via
#     ``alfred.integrations.gcal_sync.resolve_collapse_key``.
#   - ``gcal_collapse_synced`` — internal sync-STATE cache (NOT operator-set,
#     like ``gcal_event_id``). The last-synced
#     ``"<start_iso>|<end_iso>|<title>"`` signature, written direct-frontmatter
#     to the collapse PRIMARY by ``sync_collapse_group`` (via
#     ``_write_primary_id``). Lets the next recompute SKIP a redundant GCal
#     PATCH when the projected span+title are unchanged (the skip-unchanged
#     short-circuit, arc-followup §2 / NOTE-A). Cleared off demoted secondaries
#     by ``_clear_gcal_ids``. One opaque string — compared whole, never split.
#
# Five are opt-in operator/Salem knobs (gcal_keep_on_cancel, gcal_title,
# gcal_sync, gcal_collapse_key) plus the identity id; gcal_event_id, gcal_
# calendar and gcal_collapse_synced are internal sync-state (never operator-set).
# None are required.
EVENT_GCAL_FIELDS: tuple[str, ...] = (
    "gcal_event_id",
    "gcal_calendar",
    "gcal_keep_on_cancel",
    "gcal_title",
    "gcal_sync",
    "gcal_collapse_key",
    "gcal_collapse_synced",
)

# Optional frontmatter field on ``task`` records that participates in
# the tier "today" view (the V2 due-window model). Salem-only by
# virtue of brief integration — non-Salem instances simply don't
# populate this field and the brief never reaches the render path.
#
#   - ``escalate_at_days``  (int)       — days BEFORE ``due`` when the
#     task auto-surfaces into the tier "today" view (the auto-T1
#     candidate window). **Opt-in per task**: omitting means the task
#     never auto-surfaces by deadline proximity, even when ``due`` is
#     set. The window is ``2 <= days_to_due <= escalate_at_days``
#     (the 0-day "due today" and 1-day "due tomorrow" cases are
#     surfaced unconditionally, ahead of this knob). Consumed by
#     ``alfred.tier.compute.compute_auto_t1_candidates`` — see that
#     function for the full surfacing contract.
#
# The ``due`` field (already standard on task records) is the deadline
# the window is relative to. Tier PLACEMENT (T1/T2/T3) is computed by
# the voice/surfacing layer (``alfred.tier``), not stored on the
# record — there is no ``base_tier`` / ``escalate_to`` / ``tier`` field.
#
# HISTORY: the V1 tier model (per-task ``base_tier`` + ``escalate_to``
# stored fields + a ``compute_effective_tier`` projection) was retired
# 2026-05-29 (Ship 3 atomic drop). ``base_tier`` and ``escalate_to``
# have no live consumer; they were removed from this surface 2026-06-25
# (Step 1 of the routine-systems consolidation) so the schema stops
# describing a dead model. Only ``escalate_at_days`` — the live V2
# due-window knob — remains.
TIER_FIELDS: tuple[str, ...] = (
    "escalate_at_days",
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
    # Routine (2026-05-26, Phase 1). ``items`` is a list of dicts —
    # the unit of scheduled work. Operator hand-edits may drop the
    # outer list shape on a single-item value; the coerce-from-scalar
    # pass collapses ``items: "Walk dog"`` to
    # ``items: [{"text": "Walk dog"}]`` at the top-level.
    #
    # ``completion_log`` IS NOT REGISTERED HERE despite the routine
    # frontmatter carrying it. Schema relaxation 2026-05-28: the
    # field's runtime shape is ``dict[str, list[str]]`` (item text →
    # list of ISO dates) and the existing fixtures on disk
    # (``vault/routine/Core Daily.md``, ``For Self Health.md``, etc.)
    # ship with ``completion_log: {}`` (empty dict). The original
    # 2026-05-26 ship registered ``completion_log`` in LIST_FIELDS
    # for scalar→list coerce hygiene, but this surfaced as a hard
    # validator failure on the 2026-05-28 tier Phase 1 migration:
    # ``"must be a list, got dict"`` when the migration script tried
    # to create ``Standing Practices.md`` with an empty dict. The
    # runtime aggregator (``routine/aggregator.py:201``) and
    # mutator (``routine/cli.py:132``) both treat the field as
    # dict-of-lists; the validator was the lone surface demanding
    # list. The relaxation removes the validator's demand; the
    # runtime is the source of truth. Both shapes are now valid at
    # create time:
    #   - ``completion_log: {}``                    (canonical empty)
    #   - ``completion_log: []``                    (alt-empty; migration
    #                                                 script writes this)
    #   - ``completion_log: {"Reading": ["2026-..."]}`` (populated)
    # Per-item value-list mutation is owned by ``alfred routine done``
    # in ``routine/cli.py`` — that code path stays unchanged.
    "items",
    # Ticket (2026-06-09, VERA MVP). ``screenshots`` is a list of
    # vault-relative image paths attached to a ticket. VERA writes it as
    # a list, but an operator hand-edit (or a single-screenshot create)
    # may drop the outer list shape (``screenshots: "ticket/img/foo.png"``);
    # the scalar→list coerce collapses it to a one-element list at
    # create/edit time.
    "screenshots",
}

# Required fields for all records
REQUIRED_FIELDS: list[str] = ["type", "created"]
