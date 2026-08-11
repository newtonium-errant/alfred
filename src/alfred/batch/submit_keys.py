"""Wire-level idempotency for batch submission — the seen-key store (#100).

THE FAILURE THIS EXISTS TO PREVENT. A submit reaches the box, the box saves
thirty scans, mints the carried record and answers — and the ANSWER is lost
(phone drops to cell, tunnel blips, the BFF's own timeout fires). The operator
sees "couldn't reach the instance" and presses Submit again. Without a key on
the wire the second request is indistinguishable from a first, so it mints a
SECOND batch: two records, sixty drip items, and the vision spend paid twice for
one stack of paper. Content-addressed image dedupe does NOT catch this — it
dedupes WITHIN one submission, and these are two.

So the client mints a key per STAGED SET (not per attempt — a retry resends the
same key), the key rides the wire, and this module remembers what it answered.
A repeat key gets the ORIGINAL receipt back, and no second batch is minted.

MIRRORS THE CHAT TURN'S S6 CONTRACT (``alfred.web.routes_chat._dedup_check``) in
semantics — client-minted key, resent on retry, server replays the stored result
rather than re-acting, ``deduped`` flag on the replayed response — with two
deliberate divergences, both forced by the shape of this route:

  * **The key rides a HEADER, not the body.** ``/chat/turn`` takes JSON and can
    carry ``idempotency_key`` as a field. This route takes MULTIPART, and the
    BFF in front of it is a byte pipe that must not parse the body (a second
    multipart implementation there would have its own limits — see
    ``routes_batch``'s module docstring). A form field would also only be
    readable mid-stream, after the batch directory had been created.
  * **There is no ``stale`` branch.** S6 can compare a message hash and re-run
    on "same key, different message". Here the content is not known at check
    time, so a key that is reused with different content REPLAYS rather than
    re-running. That direction is deliberate: replaying is the answer that
    cannot double-bill, and the client's key is derived from the staged set, so
    changing the instruction or the files mints a new key on its own.

WHERE IT LIVES. ``<data_dir>/batch/<instance-slug>/submit_keys.json`` — beside
the batch directories it describes, INSTANCE-SCOPED through the same
:func:`~alfred.batch.paths.batch_root` every other batch path derives from. A
cwd-relative or unscoped store would be one file shared by Salem, KAL-LE,
Hypatia and VERA on a box where every instance shares a WorkingDirectory and
differs only by ``--config`` — the #53 shape that already bit for real.

SURVIVES RESTART, which is the whole point: the lost-response retry usually
arrives seconds later, but a box that restarted in between must still refuse to
double-mint. Hence a file rather than a process-lifetime dict.

BOUNDED, by two limits that are named rather than implied:

  * :data:`SUBMIT_KEY_TTL_SECONDS` — 24h. An hour would be enough for the retry
    this defends against; a day matches the transport's existing send-dedupe
    window and costs nothing.
  * :data:`MAX_SUBMIT_KEYS` — 500 entries. The TTL alone does not bound a busy
    day, and this file is read on every submission.

CONCURRENCY, honestly. The read-modify-write is wrapped in the shared
``file_rmw_lock``, so two processes cannot clobber each other's entries. It does
NOT serialize the check against the whole submission: two genuinely SIMULTANEOUS
requests bearing the same key could both miss the check and both mint. That race
is not the defended failure (a retry follows a failed attempt, and both clients
disable their submit button while one is in flight), and closing it would mean
holding a lock across a 128 MiB upload.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from .paths import batch_root

log = structlog.get_logger(__name__)

#: Store filename, under ``<data_dir>/batch/<instance-slug>/``.
_STORE_NAME = "submit_keys.json"

#: How long a key is remembered. A retry-after-lost-response arrives in seconds;
#: a day is generous headroom and matches the transport's existing 24h dedupe
#: window rather than inventing a second number for the same idea.
SUBMIT_KEY_TTL_SECONDS = 24 * 60 * 60

#: Hard cap on remembered keys, newest kept. The TTL does not bound a busy day,
#: and this file is read on every submission — an unbounded list would make the
#: cheapest possible check grow without limit.
MAX_SUBMIT_KEYS = 500

#: Ceiling on an accepted key. The key is client-minted (a UUID is 36 chars);
#: anything longer is a client bug or an attempt to grow the store, and is
#: refused as absent rather than stored.
MAX_SUBMIT_KEY_CHARS = 200


def store_path(data_dir: Path | str, instance: str) -> Path:
    """``<data_dir>/batch/<instance-slug>/submit_keys.json``.

    Derived through :func:`~alfred.batch.paths.batch_root` so the store is
    instance-scoped by exactly the same rule as the batch directories, and a
    blank instance is a hard error here too rather than a shared file.
    """
    return batch_root(data_dir, instance) / _STORE_NAME


#: Well-formed keys. Deliberately narrow — a UUID (the client's normal mint)
#: matches, and anything carrying whitespace or control characters does not.
#: Mirrors the client's ``BATCH_IDEMPOTENCY_KEY_RE`` and the same discipline
#: ``routes_stt`` applies to its own key header.
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,%d}$" % MAX_SUBMIT_KEY_CHARS)


def clean_key(raw: str | None) -> str:
    """Validate an incoming key; ``""`` when absent or malformed.

    An empty result means "no key was supplied", which every caller treats as
    "run normally, remember nothing" — the pre-feature behaviour. A malformed
    key is DROPPED rather than repaired: truncating or sanitising one could land
    it on a key another submission already owns, and replaying the wrong
    receipt is far worse than not deduping.

    A present-but-malformed key gets ONE info log so a mis-wired client is
    observable — never at error level, and NEVER carrying the value itself.
    """
    key = (raw or "").strip()
    if not key:
        return ""
    if not _KEY_RE.match(key):
        log.info(
            "batch.submit_keys.key_ignored",
            reason="malformed",
            key_len=len(key),
            detail="the idempotency key was not a bounded header-safe token — "
                   "this submission proceeds WITHOUT dedupe protection",
        )
        return ""
    return key


@dataclass
class SubmitKeyEntry:
    """One remembered submission: the key, when it was answered, and what with.

    Every field carries a default so a store written by another version — with
    a field this one has never heard of, or missing one it expects — loads
    instead of crashing. That is the load-time schema-tolerance contract every
    ``state.py`` in this repo follows; an entry that survives the filter but has
    no ``key`` is dropped by :func:`_entries_from`, since a keyless entry can
    never match anything.
    """

    key: str = ""
    created_at: str = ""
    receipt: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SubmitKeyEntry":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        entry = cls(**known)
        # Coerce rather than trust: a hand-edited or half-written store must not
        # put a non-dict receipt into a response body.
        if not isinstance(entry.receipt, dict):
            entry.receipt = {}
        return entry

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "created_at": self.created_at,
            "receipt": self.receipt,
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_fresh(entry: SubmitKeyEntry, *, now: datetime) -> bool:
    """True while an entry is inside the TTL.

    An UNPARSEABLE timestamp is treated as EXPIRED. That direction is chosen
    deliberately: keeping it forever would let one corrupt row occupy the store
    permanently, and the cost of dropping it is one un-deduped retry — where the
    cost of the opposite mistake is a receipt replayed for the wrong batch.
    """
    stamp = _parse_ts(entry.created_at)
    if stamp is None:
        return False
    return (now - stamp).total_seconds() < SUBMIT_KEY_TTL_SECONDS


def _entries_from(raw: Any) -> list[SubmitKeyEntry]:
    """Parse the on-disk shape into entries, dropping anything unusable."""
    rows = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return []
    entries: list[SubmitKeyEntry] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        entry = SubmitKeyEntry.from_dict(row)
        if entry.key:
            entries.append(entry)
    return entries


def load_entries(path: Path, *, now: datetime | None = None) -> list[SubmitKeyEntry]:
    """Read the store, dropping expired and malformed rows. Never raises.

    A missing store is an empty list — the ordinary state on an instance that
    has never taken a submission. A CORRUPT store is also an empty list, and is
    logged: the alternative is a submission route that 502s because a bookkeeping
    file got truncated, which would take out the feature to protect a cache.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        log.warning(
            "batch.submit_keys.unreadable",
            path=str(path),
            error_type=type(exc).__name__,
            detail=str(exc)[:200],
            reason="the idempotency store could not be read — treating it as "
                   "empty, so this submission runs normally rather than "
                   "failing. A retry of an in-flight batch may not dedupe.",
        )
        return []
    moment = now or _now()
    return [e for e in _entries_from(raw) if _is_fresh(e, now=moment)]


def find_receipt(
    path: Path, key: str, *, now: datetime | None = None,
) -> dict[str, Any] | None:
    """The stored receipt for ``key``, or ``None`` when it has not been seen.

    ``None`` for a blank key without touching disk — "no key supplied" is the
    normal pre-feature path and must not pay a file read.
    """
    if not key:
        return None
    for entry in load_entries(path, now=now):
        if entry.key == key:
            return dict(entry.receipt)
    return None


def record_receipt(
    path: Path,
    key: str,
    receipt: dict[str, Any],
    *,
    now: datetime | None = None,
) -> None:
    """Remember ``receipt`` under ``key``. Never raises.

    Read-modify-write under the shared cross-process lock, then an atomic
    ``.tmp`` → ``os.replace``: the lock closes lost updates between two
    instances' routes, the atomic rename closes torn reads for whoever reads
    next. Both are needed — see ``alfred.common.file_lock``.

    A write failure is logged and swallowed. The batch itself is already saved
    and answered by the time this is called, and turning a bookkeeping failure
    into a 502 would tell the operator their scans were lost when they were not.
    The cost of the swallow is exactly one un-deduped retry.
    """
    if not key:
        return
    from alfred.common.file_lock import file_rmw_lock

    moment = now or _now()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_rmw_lock(path):
            entries = [e for e in load_entries(path, now=moment) if e.key != key]
            entries.append(SubmitKeyEntry(
                key=key,
                created_at=moment.isoformat(),
                receipt=dict(receipt),
            ))
            # Newest kept: the oldest keys are the least likely to be retried.
            if len(entries) > MAX_SUBMIT_KEYS:
                entries = entries[-MAX_SUBMIT_KEYS:]
            payload = {"version": 1, "entries": [e.to_dict() for e in entries]}
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8",
            )
            os.replace(tmp, path)
    except OSError as exc:
        log.warning(
            "batch.submit_keys.write_failed",
            path=str(path),
            error_type=type(exc).__name__,
            detail=str(exc)[:200],
            reason="the batch was saved and answered; only the idempotency "
                   "record failed, so a retry of THIS submission would mint a "
                   "second batch",
        )


__all__ = [
    "MAX_SUBMIT_KEYS",
    "MAX_SUBMIT_KEY_CHARS",
    "SUBMIT_KEY_TTL_SECONDS",
    "SubmitKeyEntry",
    "clean_key",
    "find_receipt",
    "load_entries",
    "record_receipt",
    "store_path",
]
