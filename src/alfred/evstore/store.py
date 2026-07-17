"""Append-only, hash-chained, flock-serialized event store — the separable product core.

Tamper-EVIDENT, not tamper-PROOF (§4 threat-model honesty). On-box artifacts (this chain, the
scribe.log echo, vault git snapshots) raise the consistency bar an in-place edit must clear;
the only genuinely OFF-box artifacts are the per-attest printed chain tips and the ``anchor``
exports. A root attacker who rewrites every on-box copy consistently is defeated ONLY by those
off-box tips/anchors. Never overclaim.

Owns (Ruling 1 — separable, zero ``alfred.*`` imports): the envelope + chain protocol, the
storage layout, durability (fsync on durable appends), flock serialization, verify + torn-tail
recovery, the tolerant query surface, permissions (0700 dirs / 0600 files), and — pushed into
the module so PHI-free-by-construction is a property of the PRODUCT, not of caller discipline —
structural payload enforcement: an append REFUSES (fail-loud) any unregistered ``(stream, kind)``,
any payload field outside the kind's registered frozenset, and any payload value that is not a
scalar (``str|int|float|bool|None``) or a flat list of scalars. The module cannot *know* PHI; it
makes undeclared free text structurally impossible.

Does NOT own: which kinds exist, family semantics, actor resolution, choke-point wiring, config
loading, CLI registration — those are the STAY-C facade's job (`alfred.scribe.events`).
"""
from __future__ import annotations

import fcntl
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import structlog

from .chain import GENESIS_PREV, canonical_json, entry_hash, recompute_entry_sha

# Envelope + store protocol versions. Strict ints (bool excluded); a bump is a genesis-declared
# migration, never a re-chain (§9).
ENVELOPE_V = 1
STORE_PROTOCOL = 1

# The one canonicalization the store writes + verifies under (mirrors chain.CANONICALIZATION).
from .chain import CANONICALIZATION  # noqa: E402  (re-export for genesis payload)

_GENESIS_KIND = "stream.genesis"
_GENESIS_FAMILY = "meta"
# Frozen genesis payload field set (flat scalars only — passes the store's OWN §2.1 enforcement).
_GENESIS_FIELDS = frozenset(
    {"store_protocol", "canonicalization", "predecessor_file", "predecessor_sha256"}
)

# Tail-read window for tip resolution: entries are a few hundred bytes, so the last complete
# line always lives in the final block (§3.1 — no head.json cache; the log is the sole truth).
_TAIL_BLOCK = 8192


class EventStoreError(Exception):
    """An append/preflight/registration was refused fail-loud — unregistered kind, a payload
    field outside the registered set, a non-scalar payload value, or an I/O failure that must
    surface at open rather than mid-operation."""


@dataclass(frozen=True)
class Actor:
    """WHO acted. ``name`` is a config-validated staff slug (or ``stayc_scribe`` / ``operator`` /
    ``""``); ``kind`` is a coarse class. Never a patient (PHI-minimization §11)."""

    name: str = ""
    kind: str = ""


@dataclass(frozen=True)
class AppendReceipt:
    """Outcome of one durable/best-effort append — the caller prints the tip from this (§4)."""

    stream: str
    seq: int
    entry_sha: str


@dataclass(frozen=True)
class VerifyReport:
    """Result of a linear verify scan (§4). ``ok`` false ⇒ tamper/corruption at ``first_bad_seq``;
    ``torn_tail`` true ⇒ a crash-mid-append fragment on the final line (benign, pass-with-warning);
    ``sealed_fragments`` ⇒ count of NON-final skipped fragments the chain links across — a sealed
    crash fragment OR smuggled non-entry bytes; pass-with-warning like torn_tail, but a nonzero
    value means the evidence file carries lines no chain row attests to (the operator must eyeball
    the raw JSONL). ``days_since_last_anchor`` None ⇒ never anchored (the zero-config ILB nag)."""

    ok: bool
    entries: int
    head_seq: int
    head_sha: str
    first_bad_seq: int | None
    torn_tail: bool
    days_since_last_anchor: int | None
    sealed_fragments: int = 0


@dataclass(frozen=True)
class _KindSpec:
    kind: str
    family: str
    fields: frozenset
    stream: str
    durable: bool


def _is_scalar(v: Any) -> bool:
    # bool IS allowed in payloads (it is a subclass of int); the strict-int bool-exclusion applies
    # only to the store-stamped envelope ints (v / seq), never to payload values.
    return v is None or isinstance(v, (str, int, float, bool))


def _is_flat_scalar_list(v: Any) -> bool:
    return isinstance(v, list) and all(_is_scalar(e) for e in v)


def _is_chain_entry(e: Any) -> bool:
    """The ONE structural chain-entry predicate — shared by ``_iter_entries`` (the query surface:
    query/latest/tail/audit_encounter/rebuild_index), ``_last_valid`` (tip resolution), AND
    ``verify``, so the three can never drift again (the H1 class). A row is a chain entry iff it is
    a dict carrying ``entry_sha`` + a ``str`` ``prev`` + a ``seq`` that parses as ``int``. This is
    the FULL effective predicate ``verify`` needs BEFORE calling ``recompute_entry_sha`` (which does
    ``prev + "\\n"`` — a non-str ``prev`` would raise) and taking ``int(seq)``. Anything failing it
    is a non-entry: never served as evidence, never a tip candidate, counted as a sealed fragment by
    verify — so a forgery cannot be query-servable yet verify-invisible, and no malformed field can
    crash tip resolution / verify (the in-band DoS). Value validity (the sha itself) is verify's job,
    not this structural gate."""
    if not (isinstance(e, dict) and "entry_sha" in e and "prev" in e and "seq" in e):
        return False
    if not isinstance(e["prev"], str):
        return False
    try:
        int(e["seq"])
    except (TypeError, ValueError):
        return False
    return True


class EventStore:
    """One events directory: two (or more) hash-chained JSONL streams + an anchors dir.

    Concurrency (chaining forces it — daemon + attest CLI append concurrently): per-stream
    ``flock(<stream>.lock, LOCK_EX)`` → tail-read tip → build entry → append (+fsync if durable)
    → unlock. POSIX flock on local ext4 is sufficient.
    """

    def __init__(
        self,
        events_dir: str | Path,
        *,
        log: Any = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self._dir = Path(events_dir)
        self._log = log if log is not None else structlog.get_logger("scribe.events")
        self._clock = clock
        self._kinds: dict[tuple[str, str], _KindSpec] = {}
        self._genesis_pred: dict[str, tuple[str, str]] = {}

    # --- registration ------------------------------------------------------

    def register_kind(
        self, kind: str, *, family: str, fields: frozenset, stream: str, durable: bool
    ) -> None:
        """Register a ``(stream, kind)`` → payload contract. Idempotent on an identical spec
        (so a facade may list a kind once per stream, e.g. ``store.verified`` on both). A
        CONFLICTING re-registration is a programming error → fail-loud."""
        spec = _KindSpec(
            kind=kind, family=family, fields=frozenset(fields), stream=stream, durable=durable
        )
        existing = self._kinds.get((stream, kind))
        if existing is not None and existing != spec:
            raise EventStoreError(
                f"kind {kind!r} on stream {stream!r} already registered with a different spec"
            )
        self._kinds[(stream, kind)] = spec

    def set_genesis_predecessor(
        self, stream: str, *, predecessor_file: str = "", predecessor_sha256: str = ""
    ) -> None:
        """Pin the legacy predecessor for a stream's genesis (§3.3 — pin, don't launder). Must be
        set BEFORE the stream's first append; a no-predecessor stream leaves both ``""``."""
        self._genesis_pred[stream] = (predecessor_file, predecessor_sha256)

    def streams(self) -> list[str]:
        return sorted({s for (s, _k) in self._kinds})

    # --- append ------------------------------------------------------------

    def append(
        self,
        stream: str,
        kind: str,
        *,
        subject_id: str = "",
        actor: str = "",
        actor_kind: str = "",
        payload: dict | None = None,
        now: str | None = None,
        post_append: "Callable[[AppendReceipt], None] | None" = None,
    ) -> AppendReceipt:
        """Append one event to ``stream``. Fail-loud on an unregistered ``(stream, kind)`` or an
        illegal payload; durable kinds fsync. Serialized by the stream flock; auto-writes genesis
        on the stream's first append.

        ``post_append`` (optional) runs WHILE the stream lock is still held, after the line is
        written — so a derived index (e.g. the facade's attested-digest index, §7.4) can be
        updated inside the SAME critical section, defeating a last-writer-wins race between two
        concurrent appenders. Its exceptions propagate (an index the caller declared critical
        must not silently drift); a best-effort caller wraps its own callback."""
        spec = self._kinds.get((stream, kind))
        if spec is None:
            raise EventStoreError(f"unregistered event kind {kind!r} on stream {stream!r}")
        payload = dict(payload or {})
        self._validate_payload(kind, spec.fields, payload)
        self._ensure_dir()
        with self._locked(stream):
            self._ensure_genesis(stream, now=now)
            receipt = self._append_locked(
                stream, spec, subject_id, actor, actor_kind, payload, now, spec.durable
            )
            if post_append is not None:
                post_append(receipt)  # runs under the still-held stream lock (§7.4)
        return receipt

    def _validate_payload(self, kind: str, fields: frozenset, payload: dict) -> None:
        extra = set(payload) - fields
        if extra:
            raise EventStoreError(
                f"payload field(s) {sorted(extra)} not allowed for kind {kind!r} "
                f"(registered: {sorted(fields)})"
            )
        for k, v in payload.items():
            if not (_is_scalar(v) or _is_flat_scalar_list(v)):
                raise EventStoreError(
                    f"payload field {k!r} for kind {kind!r} is not a scalar / flat scalar list "
                    f"(got {type(v).__name__}) — undeclared structure is PHI-risky, refused"
                )

    def _append_locked(
        self, stream, spec, subject_id, actor, actor_kind, payload, now, durable
    ) -> AppendReceipt:
        seq0, prev = self._tip(stream)
        seq = seq0 + 1
        entry = self._build_entry(
            seq, prev, stream, spec.family, spec.kind, subject_id, actor, actor_kind, payload, now
        )
        self._write_line(stream, entry, fsync=durable)
        self._log.info(
            "scribe.events.appended", stream=stream, seq=seq, entry_sha=entry["entry_sha"]
        )
        return AppendReceipt(stream=stream, seq=seq, entry_sha=entry["entry_sha"])

    def _ensure_genesis(self, stream: str, *, now: str | None) -> None:
        jsonl = self._jsonl(stream)
        if jsonl.exists() and self._tip(stream)[0] > 0:
            return  # a valid entry already exists → genesis was written
        pred_file, pred_sha = self._genesis_pred.get(stream, ("", ""))
        gpayload = {
            "store_protocol": STORE_PROTOCOL,
            "canonicalization": CANONICALIZATION,
            "predecessor_file": pred_file,
            "predecessor_sha256": pred_sha,
        }
        self._validate_payload(_GENESIS_KIND, _GENESIS_FIELDS, gpayload)
        entry = self._build_entry(
            1, GENESIS_PREV, stream, _GENESIS_FAMILY, _GENESIS_KIND, "", "", "system", gpayload, now
        )
        self._write_line(stream, entry, fsync=True)
        self._log.info(
            "scribe.events.appended", stream=stream, seq=1, entry_sha=entry["entry_sha"]
        )

    def _build_entry(
        self, seq, prev, stream, family, kind, subject_id, actor, actor_kind, payload, now
    ) -> dict:
        ts = now if now is not None else self._now()
        entry = {
            "v": ENVELOPE_V,
            "seq": int(seq),
            "ts": ts,
            "stream": stream,
            "family": family,
            "kind": kind,
            "subject_id": subject_id,
            "actor": actor,
            "actor_kind": actor_kind,
            "payload": payload,
            "prev": prev,
        }
        entry["entry_sha"] = entry_hash(prev, entry)
        return entry

    def _write_line(self, stream: str, entry: dict, *, fsync: bool) -> None:
        jsonl = self._jsonl(stream)
        created = not jsonl.exists()
        # Torn-tail (§4): if the file doesn't end in a newline, a prior append crashed mid-write.
        # Never truncate/rewrite — seal the fragment with a bare newline so it becomes one
        # non-parsing (skipped) line, then chain the new entry to the last VALID tip.
        if jsonl.exists() and jsonl.stat().st_size > 0:
            with open(jsonl, "rb") as f:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    fd = os.open(jsonl, os.O_WRONLY | os.O_APPEND)
                    try:
                        os.write(fd, b"\n")
                    finally:
                        os.close(fd)
        line = (canonical_json(entry) + "\n").encode("utf-8")
        fd = os.open(jsonl, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
            if fsync:
                os.fsync(fd)
        finally:
            os.close(fd)
        if fsync and created:
            self._fsync_dir()

    # --- tip / read --------------------------------------------------------

    def _tip(self, stream: str) -> tuple[int, str]:
        """Resolve the chain tip by tail-reading the last block and taking the last COMPLETE,
        parseable line (torn final fragment skipped). ``(0, GENESIS_PREV)`` if no valid entry."""
        jsonl = self._jsonl(stream)
        if not jsonl.exists() or jsonl.stat().st_size == 0:
            return (0, GENESIS_PREV)
        with open(jsonl, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _TAIL_BLOCK))
            data = f.read()
        found = self._last_valid(data.decode("utf-8", errors="replace"))
        if found is not None:
            return found
        # Pathological: no complete line in the tail block (entry > block). Full re-read.
        found = self._last_valid(jsonl.read_text(encoding="utf-8", errors="replace"))
        return found if found is not None else (0, GENESIS_PREV)

    @staticmethod
    def _last_valid(text: str) -> tuple[int, str] | None:
        """The tip (last chain-valid entry). Gates on the shared ``_is_chain_entry`` predicate
        (entry_sha + str prev + int-parseable seq — same as verify / _iter_entries, H1) AND
        RECOMPUTES the candidate's sha before accepting it — a forged tail row with a plausible
        ``seq`` but a garbage ``entry_sha`` must not become the tip, else the next legitimate append
        chains onto the forgery and every future entry verify-fails (an unrecoverable in-band DoS the
        never-truncate rule forbids). The predicate also makes ``recompute_entry_sha`` crash-safe
        (a non-str ``prev`` is a non-entry, never reaches the recompute)."""
        for raw in reversed(text.split("\n")):
            s = raw.strip()
            if not s:
                continue
            try:
                e = json.loads(s)
            except ValueError:
                continue
            if not _is_chain_entry(e):
                continue
            if recompute_entry_sha(e) != e["entry_sha"]:
                continue  # tip candidate fails its own sha — skip to the last GENUINE entry
            return (int(e["seq"]), str(e["entry_sha"]))
        return None

    def tip(self, stream: str) -> dict:
        """Public tip for the CLI: ``{stream, seq, entry_sha}``."""
        seq, sha = self._tip(stream)
        return {"stream": stream, "seq": seq, "entry_sha": sha}

    def _iter_entries(self, stream: str) -> Iterator[dict]:
        """Tolerant reader (enroll_learning.py:233–236 discipline): yield each parseable dict
        entry in chain order; skip blank / torn / non-entry lines.

        Gates on the shared ``_is_chain_entry`` predicate — ALIGNED with ``verify`` (H1) so a
        schema-partial OR type-malformed forged line (missing ``prev``/``seq``, a non-str ``prev``,
        or a non-int-parseable ``seq``) is chain-INVISIBLE to verify AND therefore never served here
        as evidence / pinned into the attested-digest index. Otherwise a forgery is query-servable
        yet verify-green forever. Structurally-valid entries only; verify recomputes the sha."""
        jsonl = self._jsonl(stream)
        if not jsonl.exists():
            return
        with open(jsonl, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                s = raw.strip()
                if not s:
                    continue
                try:
                    e = json.loads(s)
                except ValueError:
                    continue
                if _is_chain_entry(e):
                    yield e

    def query(
        self,
        stream: str,
        *,
        family: str | None = None,
        kind: str | None = None,
        subject_id: str | None = None,
        actor: str | None = None,
        since: str | None = None,
        until: str | None = None,
        path_digest: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Tolerant filtered read (chain order). ``since``/``until`` compare ISO ``ts`` strings
        lexically (UTC isoformat sorts correctly). ``limit`` keeps the last N (most recent)."""
        out: list[dict] = []
        for e in self._iter_entries(stream):
            if family is not None and e.get("family") != family:
                continue
            if kind is not None and e.get("kind") != kind:
                continue
            if subject_id is not None and e.get("subject_id") != subject_id:
                continue
            if actor is not None and e.get("actor") != actor:
                continue
            if since is not None and str(e.get("ts", "")) < since:
                continue
            if until is not None and str(e.get("ts", "")) > until:
                continue
            if path_digest is not None:
                pl = e.get("payload") or {}
                if not isinstance(pl, dict) or pl.get("path_digest") != path_digest:
                    continue
            out.append(e)
        if limit is not None and limit >= 0:
            out = out[-limit:] if limit else []
        return out

    def latest(
        self,
        stream: str,
        *,
        family: str | None = None,
        kind: str | None = None,
        subject_id: str | None = None,
    ) -> dict | None:
        """The most recent matching entry (consent-legality + attested-index feed, §5.6/§7.4)."""
        result: dict | None = None
        for e in self._iter_entries(stream):
            if family is not None and e.get("family") != family:
                continue
            if kind is not None and e.get("kind") != kind:
                continue
            if subject_id is not None and e.get("subject_id") != subject_id:
                continue
            result = e
        return result

    def tail(self, stream: str, n: int) -> list[dict]:
        """The last ``n`` parseable entries (chain order)."""
        entries = list(self._iter_entries(stream))
        return entries[-n:] if n and n > 0 else []

    # --- verify ------------------------------------------------------------

    def verify(self, stream: str) -> VerifyReport:
        """Linear scan: recompute every ``entry_sha``, check ``prev`` linkage + seq continuity.

        PURE — never appends (the ``store.verified`` success row is the caller's, §4/§6.2).
        Continuity semantics (design §4): a torn/sealed fragment ANYWHERE is skipped; any resulting
        continuity break (recomputed sha / prev / seq) is ``first_bad_seq`` + ``ok=False``; skipped
        fragments are COUNTED — the FINAL one as ``torn_tail`` (a crash artifact), every non-final
        one as ``sealed_fragments`` (a sealed crash fragment OR smuggled non-entry bytes the chain
        links across). A nonzero ``sealed_fragments`` is pass-with-warning, not a pass: the evidence
        file carries lines no chain row attests to. The structural gate here is the shared
        ``_is_chain_entry`` predicate — IDENTICAL to the tolerant readers (``_iter_entries`` /
        ``_last_valid``) so that any query-visible row is necessarily chain-covered — a forgery
        cannot be query-servable yet verify-invisible (H1). It also makes the ``recompute_entry_sha``
        call below crash-safe: a non-str ``prev`` / non-int ``seq`` is a non-entry (a sealed
        fragment), never reaching the recompute."""
        das = self._days_since_anchor(stream)
        jsonl = self._jsonl(stream)
        if not jsonl.exists() or jsonl.stat().st_size == 0:
            return VerifyReport(True, 0, 0, GENESIS_PREV, None, False, das, 0)
        physical = jsonl.read_text(encoding="utf-8", errors="replace").split("\n")
        last_nonempty = max((i for i, ln in enumerate(physical) if ln.strip()), default=-1)
        prev_sha = GENESIS_PREV
        prev_seq = 0
        entries = 0
        torn_tail = False
        sealed_fragments = 0
        first_bad: int | None = None
        for i, ln in enumerate(physical):
            s = ln.strip()
            if not s:
                continue
            try:
                e = json.loads(s)
            except ValueError:
                e = None
            if not _is_chain_entry(e):
                if i == last_nonempty:
                    torn_tail = True  # crash-mid-append fragment on the final line — benign
                else:
                    sealed_fragments += 1  # non-final skipped line — counted, not silently blessed
                continue  # skip; continuity check below catches a break
            seq = int(e["seq"])  # predicate guarantees this parses
            if (
                recompute_entry_sha(e) != e["entry_sha"]
                or e["prev"] != prev_sha
                or seq != prev_seq + 1
            ):
                first_bad = seq
                break
            prev_sha = e["entry_sha"]
            prev_seq = seq
            entries += 1
        return VerifyReport(
            first_bad is None, entries, prev_seq, prev_sha, first_bad, torn_tail, das,
            sealed_fragments,
        )

    # --- preflight / anchor ------------------------------------------------

    def preflight(self, stream: str | None = None) -> None:
        """Open + flock acquire/release + tip resolution, NO append (§5.2). Raises
        :class:`EventStoreError` on any failure so misconfig surfaces at open, never mid-attest."""
        try:
            self._ensure_dir()
            targets = [stream] if stream is not None else (self.streams() or ["clinical"])
            for st in targets:
                with self._locked(st):
                    self._tip(st)
        except OSError as exc:
            raise EventStoreError(f"event store preflight failed: {exc}") from exc

    def anchor(self, stream: str, *, now: str | None = None) -> dict:
        """Export ``{stream, head_seq, head_sha, ts, store_protocol}`` to ``anchors/anchor-<ts>.json``
        (atomic temp→replace) and return it — the off-box custody artifact (§4)."""
        seq, sha = self._tip(stream)
        ts = now if now is not None else self._now()
        rec = {
            "stream": stream,
            "head_seq": seq,
            "head_sha": sha,
            "ts": ts,
            "store_protocol": STORE_PROTOCOL,
        }
        adir = self._dir / "anchors"
        adir.mkdir(mode=0o700, parents=True, exist_ok=True)
        safe = ts.replace(":", "").replace("+", "Z")
        path = adir / f"anchor-{stream}-{safe}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rec, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return rec

    def _days_since_anchor(self, stream: str) -> int | None:
        adir = self._dir / "anchors"
        if not adir.exists():
            return None
        latest_ts: datetime | None = None
        for p in adir.glob(f"anchor-{stream}-*.json"):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
                dt = datetime.fromisoformat(str(rec.get("ts", "")).replace("Z", "+00:00"))
            except (ValueError, OSError):
                continue
            if latest_ts is None or dt > latest_ts:
                latest_ts = dt
        if latest_ts is None:
            return None
        # Measure staleness against the SAME clock the anchor ts was stamped with (``self._now``):
        # in production (clock=None) that is real UTC now; under an injected clock (tests / a
        # deterministic replay) it stays consistent, so "anchored just now" reads 0 days regardless
        # of the wall calendar. (Was ``datetime.now`` unconditionally → a fixed-clock anchor went
        # stale by the real-world date, a date-boundary flake.)
        try:
            now_dt = datetime.fromisoformat(self._now().replace("Z", "+00:00"))
        except (ValueError, TypeError):
            now_dt = datetime.now(timezone.utc)
        delta = now_dt - latest_ts
        return max(0, delta.days)

    # --- fs helpers --------------------------------------------------------

    def _now(self) -> str:
        if self._clock is not None:
            return self._clock()
        return datetime.now(timezone.utc).isoformat()

    def _jsonl(self, stream: str) -> Path:
        return self._dir / f"{stream}.jsonl"

    def _lock(self, stream: str) -> Path:
        return self._dir / f"{stream}.lock"

    def _ensure_dir(self) -> None:
        self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _fsync_dir(self) -> None:
        dfd = os.open(str(self._dir), os.O_RDONLY)
        try:
            os.fsync(dfd)
        except OSError:
            pass
        finally:
            os.close(dfd)

    class _Locked:
        def __init__(self, path: Path):
            self._path = path
            self._fh = None

        def __enter__(self):
            self._fh = open(self._path, "w")
            os.chmod(self._path, 0o600)
            fcntl.flock(self._fh, fcntl.LOCK_EX)
            return self

        def __exit__(self, *exc):
            try:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
            finally:
                self._fh.close()

    def _locked(self, stream: str) -> "EventStore._Locked":
        self._ensure_dir()
        return EventStore._Locked(self._lock(stream))

    def stream_lock(self, stream: str) -> "EventStore._Locked":
        """Public per-stream flock context (§7.4). Lets the facade wrap a multi-step read+write
        (e.g. ``rebuild_index``: read the log → atomic-replace the index) in the SAME critical
        section ``append`` holds — so a rebuild can't race a concurrent attest's post_append index
        update (last-writer-wins). Usage: ``with store.stream_lock(CLINICAL): ...``."""
        return self._locked(stream)
