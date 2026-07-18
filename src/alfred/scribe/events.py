"""STAY-C clinical facade over the generic :mod:`alfred.evstore` (design doc §2.2, §5).

Owns the clinical vocabulary the generic store deliberately does NOT: the KINDS registry (the ONE
reviewed schema, Ruling 3 — frozen by a widening pin), the typed emitters (the ONLY constructors
of clinical events — there is DELIBERATELY no generic ``emit`` verb, ever), the durable/best-effort
postures, the ``access_actor`` identity ContextVar + the vault read-hook, and the attested-digest
index (§7.4). Import direction (frozen): scribe.* → scribe.events → evstore; evstore never imports
back.

Activation (§2.4): ALWAYS-ON with scribe — there is no ``enabled`` knob (an evidence store that can
be configured off is not evidence). Clinical mode fails LOUD at open; non-clinical degrades to
inactive + one ``scribe.events.degraded`` line (fail-open-loud).
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import structlog

from alfred.evstore import AppendReceipt, EventStore, EventStoreError, sha256_hex
from alfred.scribe.config import SCRIBE_MODE_CLINICAL, load_from_unified

log = structlog.get_logger("scribe.events")

CLINICAL = "clinical"
ACCESS = "access"

# The legacy attest-audit sink whose sha256 is pinned into the clinical genesis (§3.3 — pin,
# don't launder; no row import, ever).
LEGACY_ATTEST_AUDIT = "clinical_attest_audit.jsonl"

# Envelope actor_kind allowlist (§3.2). The typed emitters HARDCODE their (valid) kind, so they
# enforce it by construction; the ONE caller-supplied path is ``access_read`` (the kind rides the
# ``access_actor`` ContextVar), which coerces an out-of-allowlist value to ``"unknown"`` via
# :func:`_coerce_actor_kind` rather than landing a novel kind in the chained envelope. (The store
# itself keeps ``actor_kind`` generic — the allowlist is a facade contract.)
ACTOR_KINDS = frozenset({"clinician", "pipeline", "operator", "system", "unknown"})


def _coerce_actor_kind(actor_kind: str) -> str:
    """Enforce the §3.2 allowlist on the caller-supplied ``access_read`` path: an unknown kind
    degrades to ``"unknown"`` (honest) instead of stamping a novel value into the med-legal chain."""
    return actor_kind if actor_kind in ACTOR_KINDS else "unknown"


def _consent_dt(event: dict | None) -> tuple[str, str]:
    """Split a consent event's ISO ``ts`` into ``(YYYY-MM-DD, HH:MM)`` for the deterministic
    consent line (§7.2). Missing/malformed ts → ``("unknown", "unknown")`` (never a crash, never
    ``datetime.now()`` — the line states WHEN consent was actually captured)."""
    ts = str((event or {}).get("ts") or "")
    if "T" in ts:
        date, _, rest = ts.partition("T")
        return date, rest[:5]        # HH:MM
    return "unknown", "unknown"


# ── Consent state machine (#12 §3.1) ─────────────────────────────────────────────
# The per-encounter consent state ∈ {"" (∅), "confirmed", "declined", "withdrawn"}. Legal
# transitions (design §3.1): ∅→confirmed, ∅→declined, confirmed→withdrawn. declined + withdrawn
# are TERMINAL; a second confirm, or any move out of a terminal state, is refused at the facade.
# The map is target → the set of source states it may be reached FROM (empty string == ∅).
CONSENT_STATES: frozenset[str] = frozenset({"confirmed", "declined", "withdrawn"})
_CONSENT_LEGAL_FROM: dict[str, frozenset[str]] = {
    "confirmed": frozenset({""}),          # ∅ → confirmed
    "declined": frozenset({""}),           # ∅ → declined
    "withdrawn": frozenset({"confirmed"}),  # confirmed → withdrawn
}


class ConsentTransitionError(EventStoreError):
    """An illegal consent state transition was refused at the facade (design §3.1/§5.6). Subclasses
    ``EventStoreError`` so a caller's existing ``except EventStoreError`` covers it — a route that
    already fails-closed on a durable-append error also fails-closed on an illegal transition."""


@dataclass(frozen=True)
class _Kind:
    kind: str
    family: str
    fields: frozenset
    stream: str
    durable: bool


# ── The ONE reviewed schema (Ruling 3) — frozen by the widening pin ──────────────
# #11 families (attestation/note/encounter/access/meta) get typed emitters below; consent (#12)
# and retention (#13) are CONTRACT-registered now so the allowlist + widening pin freeze the whole
# schema — a #12/#13 emitter cannot silently add a PHI field without tripping the pin. stream.genesis
# is store-owned (written on first open), so it is NOT in this facade registry.
KINDS: tuple[_Kind, ...] = (
    # META
    _Kind("store.heartbeat", "meta",
          frozenset({"count_attestation", "count_note", "count_encounter",
                     "count_consent", "count_retention"}), CLINICAL, False),
    _Kind("store.verified", "meta", frozenset({"ok", "entries"}), CLINICAL, False),
    _Kind("store.verified", "meta", frozenset({"ok", "entries"}), ACCESS, False),
    # ATTESTATION (#11)
    _Kind("attest.recorded", "attestation",
          frozenset({"from_status", "to_status", "creator", "forced", "completeness",
                     "body_sha", "grounding_flag_count", "grounding_reasons"}), CLINICAL, True),
    _Kind("attest.refused", "attestation",
          frozenset({"reason", "from_status", "to_status", "completeness", "forced"}),
          CLINICAL, False),
    # NOTE (#11)
    _Kind("note.draft_created", "note", frozenset({"body_sha"}), CLINICAL, False),
    _Kind("note.draft_regenerated", "note",
          frozenset({"body_sha", "marker", "grounding_flag_count"}), CLINICAL, False),
    _Kind("note.ready", "note",
          frozenset({"body_sha", "expected_final_seq", "folded_through"}), CLINICAL, False),
    _Kind("note.human_edit_detected", "note",
          frozenset({"body_sha_before", "body_sha_after"}), CLINICAL, False),
    _Kind("note.post_attest_audio", "note", frozenset(), CLINICAL, False),
    _Kind("note.marker_selfheal", "note", frozenset(), CLINICAL, False),
    _Kind("note.post_attest_edit_detected", "note",
          frozenset({"attested_body_sha", "current_body_sha"}), CLINICAL, False),
    # ENCOUNTER (#11)
    _Kind("encounter.opened", "encounter", frozenset(), CLINICAL, False),
    _Kind("encounter.closed", "encounter", frozenset({"final_seq"}), CLINICAL, False),
    _Kind("encounter.cap_hit", "encounter", frozenset({"cap"}), CLINICAL, False),
    _Kind("encounter.post_close_chunk_refused", "encounter", frozenset({"seq"}), CLINICAL, False),
    # ACCESS (#11) — access stream
    _Kind("access.read", "access",
          frozenset({"record_type", "status", "path_digest", "via"}), ACCESS, False),
    _Kind("access.system_reads_summary", "access",
          frozenset({"count", "window_start"}), ACCESS, False),
    # CONSENT (#12 — contract-registered now, emitters at #12)
    _Kind("consent.confirmed", "consent", frozenset({"method", "captured_by"}), CLINICAL, True),
    _Kind("consent.declined", "consent", frozenset({"method", "captured_by"}), CLINICAL, True),
    _Kind("consent.withdrawn", "consent", frozenset({"at_seq"}), CLINICAL, True),
    _Kind("consent.violation_refused", "consent", frozenset({"seq"}), CLINICAL, False),
    # RETENTION (#13 — contract-registered now, emitters at #13)
    _Kind("retention.schedule_published", "retention",
          frozenset({"schedule_version", "schedule_sha256", "effective_date"}), CLINICAL, True),
    _Kind("retention.sealed", "retention",
          frozenset({"chunk_count", "total_bytes", "manifest_sha256",
                     "sealed_to_key_fp", "cipher"}), CLINICAL, True),
    _Kind("retention.unsealed", "retention",
          frozenset({"reason_code", "ticket_ref"}), CLINICAL, True),
    _Kind("retention.destroy_intent", "retention",
          frozenset({"schedule_version", "manifest_sha256"}), CLINICAL, True),
    _Kind("retention.destroyed", "retention",
          frozenset({"schedule_version", "manifest_sha256"}), CLINICAL, True),
)


@dataclass
class AccessContext:
    """The identity threaded into a vault read-hook fire (§7.1.3). Registrars set it before a
    read burst; the hook attributes the ``access.read`` (or suppresses+counts a pipeline read)."""

    actor: str = "operator"
    actor_kind: str = "operator"
    via: str = "cli"


_access_ctx: contextvars.ContextVar[AccessContext] = contextvars.ContextVar(
    "access_actor", default=AccessContext()
)


class ScribeEvents:
    """The constructed facade — one per daemon / attest CLI invocation. Holds the store, the
    activation flag, and the derived index + suppression counter."""

    def __init__(self, store: EventStore, *, active: bool, events_dir: str | Path,
                 clock: Any = None) -> None:
        self._store = store
        self._active = active
        self._dir = Path(events_dir)
        self._clock = clock  # SAME clock as the store, so index attested_at == event ts
        self._suppressed_reads = 0
        self._suppressed_window_start: str | None = None

    # --- construction / activation ----------------------------------------

    @classmethod
    def from_config(
        cls,
        raw: dict,
        log_dir: str | Path,
        *,
        clock: Any = None,
        legacy_audit_path: str | Path | None = None,
    ) -> "ScribeEvents":
        """Construct + activate. Clinical mode fails LOUD at open (raises); non-clinical degrades
        to inactive + one ``scribe.events.degraded`` line. ``legacy_audit_path`` (when it exists)
        is sha256-pinned into the clinical genesis predecessor (§3.3)."""
        cfg = load_from_unified(raw)
        events_dir = cfg.events.dir or str(Path(log_dir) / "events")
        clinical = cfg.mode == SCRIBE_MODE_CLINICAL
        store = EventStore(events_dir, log=log, clock=clock)
        for k in KINDS:
            store.register_kind(k.kind, family=k.family, fields=k.fields,
                                stream=k.stream, durable=k.durable)
        # Genesis predecessor pin (§3.3 — pin, don't launder). BOTH fields are "" for a stream
        # with no predecessor: the file NAME is stamped ONLY when its sha was actually computed —
        # never a name-present + sha-empty half-pin baked into the immutable genesis row (a
        # greenfield install must not assert a predecessor that never existed; a real cutover must
        # not silently lose the digest). An existing-but-UNREADABLE legacy file is an open-time
        # misconfig: clinical mode fails LOUD (same class as preflight); non-clinical logs + skips.
        pred_file, pred_sha = "", ""
        if legacy_audit_path and Path(legacy_audit_path).exists():
            try:
                pred_sha = sha256_hex(Path(legacy_audit_path).read_bytes())
                pred_file = LEGACY_ATTEST_AUDIT
            except OSError as exc:
                if clinical:
                    raise EventStoreError(
                        f"legacy attest-audit {legacy_audit_path!s} exists but is unreadable "
                        f"({exc}) — refusing to write a half-pinned clinical genesis (§3.3). Fix "
                        f"the file perms and re-open.") from exc
                log.error(
                    "scribe.events.legacy_pin_unreadable", path=str(legacy_audit_path),
                    detail="legacy attest-audit unreadable — genesis pins NO predecessor (non-clinical degrade)")
        store.set_genesis_predecessor(
            CLINICAL, predecessor_file=pred_file, predecessor_sha256=pred_sha)
        log.info(
            "scribe.events.genesis_predecessor_decided",
            predecessor_file=pred_file or "(none)", has_sha=bool(pred_sha),
            detail="clinical genesis predecessor pin decided (both empty ⇒ greenfield / no legacy)")
        self = cls(store, active=True, events_dir=events_dir, clock=clock)
        try:
            store.preflight()
        except EventStoreError:
            if clinical:
                raise  # fail-LOUD at open (§2.4) — the caller refuses boot / refuses the attest
            log.error(
                "scribe.events.degraded", events_dir=str(events_dir),
                detail="event store failed to open — DEGRADED to inactive (non-clinical mode; "
                       "fail-open-loud). No events will be recorded this lifecycle.")
            self._active = False
        return self

    @property
    def active(self) -> bool:
        return self._active

    @property
    def store(self) -> EventStore:
        return self._store

    # --- postures ---------------------------------------------------------

    def _emit_durable(self, stream: str, kind: str, **kw) -> AppendReceipt:
        if not self._active:
            raise EventStoreError(f"event store inactive — cannot emit durable {kind!r}")
        return self._store.append(stream, kind, **kw)

    def _emit_capture(self, stream: str, kind: str, **kw) -> AppendReceipt | None:
        if not self._active:
            return None
        try:
            return self._store.append(stream, kind, **kw)
        except Exception as exc:  # noqa: BLE001 — observability must NEVER break the pipeline
            log.error("scribe.events.emit_failed", kind=kind, stream=stream,
                      error_class=type(exc).__name__,
                      detail="best-effort event emit failed — SWALLOWED (a dead emitter is loudly "
                             "distinguishable from a quiet day)")
            return None

    # --- ATTESTATION emitters ---------------------------------------------

    def attest_recorded(
        self, *, subject_id: str, attester: str, from_status: str, to_status: str, creator: str,
        forced: bool, completeness: str, body_sha: str, grounding_flag_count: int,
        grounding_reasons: list[str], rel_path: str = "", now: str | None = None,
    ) -> AppendReceipt:
        """Durable [D]. The attested-version pin. Updates the attested-digest index UNDER the same
        clinical lock (§7.4) so two concurrent attests can't race the index."""
        def _index(receipt: AppendReceipt) -> None:
            self._write_attested_index(subject_id, body_sha, receipt.seq, rel_path,
                                       attested_at=self._ts(now))
        return self._emit_durable(
            CLINICAL, "attest.recorded", subject_id=subject_id, actor=attester,
            actor_kind="clinician", now=now, post_append=_index,
            payload={"from_status": from_status, "to_status": to_status, "creator": creator,
                     "forced": bool(forced), "completeness": completeness, "body_sha": body_sha,
                     "grounding_flag_count": int(grounding_flag_count),
                     "grounding_reasons": list(grounding_reasons)})

    def attest_refused(
        self, *, subject_id: str, attester: str, reason: str, from_status: str, to_status: str,
        completeness: str, forced: bool, now: str | None = None,
    ) -> AppendReceipt | None:
        """Best-effort — a store failure must NEVER mask the refusal itself (emit-then-re-raise
        at the call site)."""
        return self._emit_capture(
            CLINICAL, "attest.refused", subject_id=subject_id, actor=attester,
            actor_kind="clinician", now=now,
            payload={"reason": reason, "from_status": from_status, "to_status": to_status,
                     "completeness": completeness, "forced": bool(forced)})

    # --- NOTE emitters (best-effort; daemon actor) ------------------------

    def note_draft_created(self, *, subject_id: str, body_sha: str, now: str | None = None):
        return self._emit_capture(CLINICAL, "note.draft_created", subject_id=subject_id,
                                  actor="stayc_scribe", actor_kind="pipeline", now=now,
                                  payload={"body_sha": body_sha})

    def note_draft_regenerated(self, *, subject_id: str, body_sha: str, marker: str,
                               grounding_flag_count: int, now: str | None = None):
        return self._emit_capture(CLINICAL, "note.draft_regenerated", subject_id=subject_id,
                                  actor="stayc_scribe", actor_kind="pipeline", now=now,
                                  payload={"body_sha": body_sha, "marker": marker,
                                           "grounding_flag_count": int(grounding_flag_count)})

    def note_ready(self, *, subject_id: str, body_sha: str, expected_final_seq: int,
                   folded_through: int, now: str | None = None):
        return self._emit_capture(CLINICAL, "note.ready", subject_id=subject_id,
                                  actor="stayc_scribe", actor_kind="pipeline", now=now,
                                  payload={"body_sha": body_sha,
                                           "expected_final_seq": int(expected_final_seq),
                                           "folded_through": int(folded_through)})

    def note_human_edit_detected(self, *, subject_id: str, body_sha_before: str,
                                 body_sha_after: str, now: str | None = None):
        return self._emit_capture(CLINICAL, "note.human_edit_detected", subject_id=subject_id,
                                  actor="stayc_scribe", actor_kind="pipeline", now=now,
                                  payload={"body_sha_before": body_sha_before,
                                           "body_sha_after": body_sha_after})

    def note_post_attest_audio(self, *, subject_id: str, now: str | None = None):
        return self._emit_capture(CLINICAL, "note.post_attest_audio", subject_id=subject_id,
                                  actor="stayc_scribe", actor_kind="pipeline", now=now, payload={})

    def note_marker_selfheal(self, *, subject_id: str, now: str | None = None):
        return self._emit_capture(CLINICAL, "note.marker_selfheal", subject_id=subject_id,
                                  actor="stayc_scribe", actor_kind="pipeline", now=now, payload={})

    def note_post_attest_edit_detected(self, *, subject_id: str, attested_body_sha: str,
                                       current_body_sha: str, now: str | None = None):
        return self._emit_capture(CLINICAL, "note.post_attest_edit_detected",
                                  subject_id=subject_id, actor="stayc_scribe",
                                  actor_kind="pipeline", now=now,
                                  payload={"attested_body_sha": attested_body_sha,
                                           "current_body_sha": current_body_sha})

    # --- ENCOUNTER emitters (best-effort) ---------------------------------

    def encounter_opened(self, *, subject_id: str, now: str | None = None):
        return self._emit_capture(CLINICAL, "encounter.opened", subject_id=subject_id,
                                  actor="stayc_scribe", actor_kind="system", now=now, payload={})

    def encounter_closed(self, *, subject_id: str, final_seq: int, now: str | None = None):
        return self._emit_capture(CLINICAL, "encounter.closed", subject_id=subject_id,
                                  actor="stayc_scribe", actor_kind="system", now=now,
                                  payload={"final_seq": int(final_seq)})

    def encounter_cap_hit(self, *, subject_id: str, cap: str, now: str | None = None):
        return self._emit_capture(CLINICAL, "encounter.cap_hit", subject_id=subject_id,
                                  actor="stayc_scribe", actor_kind="system", now=now,
                                  payload={"cap": cap})

    def encounter_post_close_chunk_refused(self, *, subject_id: str, seq: int, now: str | None = None):
        return self._emit_capture(CLINICAL, "encounter.post_close_chunk_refused",
                                  subject_id=subject_id, actor="stayc_scribe", actor_kind="system",
                                  now=now, payload={"seq": int(seq)})

    # --- CONSENT (#12) — state resolver + typed emitters ------------------
    # The typed emitters are the ONLY constructors of consent events (no generic emit verb, #11
    # §2.2). confirmed/declined/withdrawn are DURABLE (fsync, raise on failure — consent evidence
    # must be recorded before the act it gates is acknowledged); violation_refused is best-effort
    # (a marker of a refused chunk, never a state). Legality is enforced HERE at emit time via
    # ``consent_state()`` (§3.1) — the sole consent writer is the ingest_web process, serialized
    # per-encounter (§3.2), so the check-then-append cannot interleave with another consent write.

    def consent_state(self, subject_id: str) -> str:
        """Current per-encounter consent state ∈ {"", "confirmed", "declined", "withdrawn"} (the
        STATE kinds only — ``violation_refused`` is NOT a state and is ignored). '' == no consent
        set. Chain order == append order, so the last state-kind seen is the current state."""
        state = ""
        for e in self._store.query(CLINICAL, family="consent", subject_id=subject_id):
            k = e.get("kind", "")
            if k.startswith("consent.") and k.split(".", 1)[1] in CONSENT_STATES:
                state = k.split(".", 1)[1]
        return state

    def consent_line(self, subject_id: str, *, tool: str = "STAY-C") -> str:
        """Deterministic, LLM-free consent attestation line for the chart note (design §7.2). The
        string is FULLY determined by the durable consent events + fixed literals — the LLM never
        sees or generates it (un-hallucinatable); the pipeline prepends it to the note body. Reads
        each state event's ``ts`` for the date/time (WHEN consent was captured, never
        ``datetime.now()``). NO patient identifier — ``subject_id`` is the opaque encounter id and
        is never rendered. Returns '' only when the store is inactive (caller prepends nothing);
        every state — INCLUDING no-consent — renders an explicit line (ILB: silence is ambiguous)."""
        if not self._active:
            return ""
        state = self.consent_state(subject_id)
        if state == "confirmed":
            d, t = _consent_dt(self.latest(CLINICAL, family="consent", kind="consent.confirmed",
                                           subject_id=subject_id))
            return f"> Consent: patient verbally consented on {d} at {t}, using {tool}."
        if state == "declined":
            d, t = _consent_dt(self.latest(CLINICAL, family="consent", kind="consent.declined",
                                           subject_id=subject_id))
            return f"> Consent: patient DECLINED AI recording on {d} at {t}. No recording captured."
        if state == "withdrawn":
            cd, ct = _consent_dt(self.latest(CLINICAL, family="consent", kind="consent.confirmed",
                                             subject_id=subject_id))
            we = self.latest(CLINICAL, family="consent", kind="consent.withdrawn",
                             subject_id=subject_id)
            wd, wt = _consent_dt(we)
            at_seq = (we or {}).get("payload", {}).get("at_seq", "?")
            return (f"> Consent: patient verbally consented on {cd} at {ct}; consent WITHDRAWN at "
                    f"{wd} at {wt} (audio boundary seq {at_seq}). Recording stopped.")
        # state == "" — no consent recorded (synthetic/test encounter). The explicit ILB signal.
        return "> Consent: not recorded (synthetic/test encounter)."

    def consent_captured_by(self, subject_id: str) -> str:
        """The clinician slug pinned by this encounter's durable ``consent.confirmed`` event
        (§2.4). The withdrawal path resolves its actor from this UNCONDITIONALLY — the withdrawal
        is attributed to whoever OBTAINED the consent (the durable event IS the encounter→clinician
        binding), NEVER the live session (a shared device may have rebound to a different clinician
        mid-encounter; attributing the withdrawal to them would falsify the med-legal chain).
        ``''`` when no confirmed event exists (∅/declined — nothing to withdraw)."""
        e = self.latest(CLINICAL, family="consent", kind="consent.confirmed", subject_id=subject_id)
        if not e:
            return ""
        payload = e.get("payload") or {}
        return str(payload.get("captured_by") or "")

    def _assert_transition(self, subject_id: str, target: str) -> None:
        """Raise :class:`ConsentTransitionError` if ``target`` is not reachable from the encounter's
        current state (§3.1). PHI-free message — no raw subject_id (the target + current suffice)."""
        current = self.consent_state(subject_id)
        if current not in _CONSENT_LEGAL_FROM[target]:
            raise ConsentTransitionError(
                f"illegal consent transition {current or '∅'} → {target} "
                f"(legal from: {sorted(s or '∅' for s in _CONSENT_LEGAL_FROM[target])})")

    def consent_confirmed(self, *, subject_id: str, captured_by: str,
                          now: str | None = None) -> AppendReceipt:
        """Durable [D]. ∅ → confirmed. ``captured_by`` is the session-resolved clinician slug
        (§2.5) — the durable event PINS the encounter→clinician binding."""
        self._assert_transition(subject_id, "confirmed")
        return self._emit_durable(CLINICAL, "consent.confirmed", subject_id=subject_id,
                                  actor=captured_by, actor_kind="clinician", now=now,
                                  payload={"method": "verbal", "captured_by": captured_by})

    def consent_declined(self, *, subject_id: str, captured_by: str,
                         now: str | None = None) -> AppendReceipt:
        """Durable [D]. ∅ → declined (terminal). A declined visit produces consent events but no
        encounter dir / no audio (no chunk ever POSTs — the mic never opens)."""
        self._assert_transition(subject_id, "declined")
        return self._emit_durable(CLINICAL, "consent.declined", subject_id=subject_id,
                                  actor=captured_by, actor_kind="clinician", now=now,
                                  payload={"method": "verbal", "captured_by": captured_by})

    def consent_withdrawn(self, *, subject_id: str, at_seq: int, actor: str,
                          now: str | None = None) -> AppendReceipt:
        """Durable [D]. confirmed → withdrawn (terminal). ``at_seq`` is the on-disk max chunk seq
        at withdrawal — the audio boundary the withdrawal saw (§5). The durable append MUST land
        before capture-stop is acknowledged (the ordering contract rides ``_emit_durable``'s raise)."""
        self._assert_transition(subject_id, "withdrawn")
        return self._emit_durable(CLINICAL, "consent.withdrawn", subject_id=subject_id,
                                  actor=actor, actor_kind="clinician", now=now,
                                  payload={"at_seq": int(at_seq)})

    def consent_violation_refused(self, *, subject_id: str, seq: int,
                                  now: str | None = None) -> AppendReceipt | None:
        """Best-effort. A marker of a refused chunk (state != confirmed) — NOT a state transition,
        so no legality assert. System actor (the gate refused it, not a clinician)."""
        return self._emit_capture(CLINICAL, "consent.violation_refused", subject_id=subject_id,
                                  actor="stayc_scribe", actor_kind="system", now=now,
                                  payload={"seq": int(seq)})

    # --- ACCESS emitters + read hook + suppression ------------------------

    def access_read(self, *, subject_id: str, record_type: str, status: str, path_digest: str,
                    via: str, actor: str, actor_kind: str, now: str | None = None):
        return self._emit_capture(ACCESS, "access.read", subject_id=subject_id, actor=actor,
                                  actor_kind=_coerce_actor_kind(actor_kind), now=now,
                                  payload={"record_type": record_type, "status": status,
                                           "path_digest": path_digest, "via": via})

    def make_read_hook(self):
        """Return the closure registered into ``ops.register_read_hook``. Reads the
        ``access_actor`` ContextVar for identity; a ``pipeline`` read is SUPPRESSED + counted
        (§7.1.4 — the daily summary makes the suppression itself auditable), everything else
        emits ``access.read``. Never raises (ops fires it try/except-swallowed anyway)."""
        def _hook(vault_path: Any, rel_path: str, frontmatter: dict) -> None:
            ctx = _access_ctx.get()
            if ctx.actor_kind == "pipeline":
                self._suppressed_reads += 1
                if self._suppressed_window_start is None:
                    self._suppressed_window_start = self._utc_day()
                return
            fm = frontmatter or {}
            self.access_read(
                subject_id=str(fm.get("source_id") or ""),
                record_type=str(fm.get("type") or ""),
                status=str(fm.get("status") or ""),
                path_digest=sha256_hex(str(rel_path)),
                via=ctx.via, actor=ctx.actor, actor_kind=ctx.actor_kind)
        return _hook

    @contextlib.contextmanager
    def access_context(self, actor: str, actor_kind: str, via: str):
        """Bind the read-hook identity for the duration of a read burst (attest CAS reads, a
        daemon sweep, a dispatcher vault read)."""
        token = _access_ctx.set(AccessContext(actor=actor, actor_kind=actor_kind, via=via))
        try:
            yield
        finally:
            _access_ctx.reset(token)

    def flush_suppressed_reads(self, *, now: str | None = None) -> AppendReceipt | None:
        """Emit the daily ``access.system_reads_summary`` (§5.5) and reset the counter — the
        daemon calls this on the UTC-day latch so 'hook alive, zero human views' is provable
        from the chain, not just scribe.log. Emits even a zero count (intentionally-left-blank)."""
        count = self._suppressed_reads
        window_start = self._suppressed_window_start or self._utc_day()
        # Emit FIRST, reset ONLY on a successful append: a swallowed best-effort failure must not
        # silently drop the day's suppression count (it carries into the next flush instead).
        receipt = self._emit_capture(ACCESS, "access.system_reads_summary", subject_id="",
                                     actor="stayc_scribe", actor_kind="pipeline", now=now,
                                     payload={"count": int(count), "window_start": window_start})
        if receipt is not None:
            self._suppressed_reads = 0
            self._suppressed_window_start = None
        return receipt

    @property
    def suppressed_reads(self) -> int:
        return self._suppressed_reads

    # --- META emitters ----------------------------------------------------

    def store_heartbeat(self, *, counts: dict, now: str | None = None):
        payload = {f"count_{fam}": int(counts.get(fam, 0))
                   for fam in ("attestation", "note", "encounter", "consent", "retention")}
        return self._emit_capture(CLINICAL, "store.heartbeat", subject_id="", actor="stayc_scribe",
                                  actor_kind="pipeline", now=now, payload=payload)

    def record_verified(self, stream: str, *, entries: int, now: str | None = None):
        """Append the success-only ``store.verified`` (§4/§6.2 — best-effort; the verify RESULT
        reaches exit code + structlog regardless of this append)."""
        return self._emit_capture(stream, "store.verified", subject_id="", actor="operator",
                                  actor_kind="operator", now=now,
                                  payload={"ok": True, "entries": int(entries)})

    # --- attested-digest index (§7.4) -------------------------------------

    def _index_path(self) -> Path:
        return self._dir / "attested_digests.json"

    def _write_attested_index(self, subject_id: str, body_sha: str, seq: int, rel_path: str,
                              *, attested_at: str) -> None:
        idx = self._read_index()
        idx[subject_id] = {"body_sha": body_sha, "attested_at": attested_at, "seq": int(seq),
                           "rel_path": rel_path}
        self._atomic_write_index(idx)

    def _read_index(self) -> dict:
        p = self._index_path()
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, OSError):
            return {}

    def _atomic_write_index(self, idx: dict) -> None:
        self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        p = self._index_path()
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(idx, indent=2, sort_keys=True), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)

    def attested_digest(self, subject_id: str) -> dict | None:
        """The pinned attested ``{body_sha, attested_at, seq, rel_path}`` for an encounter — the
        post-attest-edit sweep's source of truth (§5.3)."""
        return self._read_index().get(subject_id)

    def attested_index(self) -> dict:
        """The FULL attested-digest index snapshot ({subject_id: {body_sha, attested_at, seq,
        rel_path}}). The post-attest-edit sweep iterates this ONCE (§5.3 / adjudication item 5) —
        index-driven — instead of a full clinical-stream scan plus a per-subject index re-parse."""
        return self._read_index()

    def rebuild_index(self) -> int:
        """Rebuild the attested-digest index from clinical.jsonl (``events verify --rebuild-index``).
        ``rel_path`` is index-only (never chained, §7.4) so a rebuild leaves it ``""`` — the
        consumer re-derives the note path from ``subject_id``. Returns the entry count.

        Runs the read-log → atomic-write UNDER the clinical-stream flock (§7.4) — otherwise a
        rebuild (daemon boot / operator ``--rebuild-index``) races a concurrent attest's
        post_append index update and last-writer-wins drops the fresh encounter's body_sha,
        reopening the exact silent post-attest-edit-detection gap the under-lock plumbing closes."""
        idx: dict = {}
        with self._store.stream_lock(CLINICAL):
            for e in self._store.query(CLINICAL, kind="attest.recorded"):
                sid = str(e.get("subject_id") or "")
                pl = e.get("payload") or {}
                if not sid or not isinstance(pl, dict):
                    continue
                idx[sid] = {"body_sha": str(pl.get("body_sha") or ""),
                            "attested_at": str(e.get("ts") or ""),
                            "seq": int(e.get("seq") or 0), "rel_path": ""}
            self._atomic_write_index(idx)
        return len(idx)

    # --- pass-throughs / query --------------------------------------------

    def preflight(self, stream: str | None = None) -> None:
        self._store.preflight(stream)

    def tip(self, stream: str = CLINICAL) -> dict:
        return self._store.tip(stream)

    def tip_line(self, stream: str = CLINICAL) -> str:
        """The ``chain tip: seq=N sha=…`` line printed after every attestation (§4)."""
        t = self._store.tip(stream)
        return f"chain tip: seq={t['seq']} sha={t['entry_sha']}"

    def verify(self, stream: str = CLINICAL):
        return self._store.verify(stream)

    def anchor(self, stream: str = CLINICAL, *, now: str | None = None) -> dict:
        return self._store.anchor(stream, now=now)

    def query(self, stream: str, **kw) -> list[dict]:
        return self._store.query(stream, **kw)

    def latest(self, stream: str, **kw) -> dict | None:
        return self._store.latest(stream, **kw)

    def audit_encounter(self, subject_id: str) -> list[dict]:
        """The cross-family single-encounter timeline (§8/§10): both streams merged by ``ts``,
        tiebroken ``(stream, seq)``, chain-position preserved — the auditor one-shot / CMPA demo."""
        rows = (self._store.query(CLINICAL, subject_id=subject_id)
                + self._store.query(ACCESS, subject_id=subject_id))
        return sorted(rows, key=lambda e: (str(e.get("ts", "")), str(e.get("stream", "")),
                                           int(e.get("seq", 0))))

    # --- helpers ----------------------------------------------------------

    def _ts(self, now: str | None) -> str:
        if now is not None:
            return now
        return self._clock() if self._clock is not None else datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _utc_day() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
