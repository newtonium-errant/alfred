"""#40 cleanup + one-shot marker grant — RUN ON THE BOX, dry-run by default.

Three passes, each independently selectable, all dry-run unless ``--apply``:

  --grant      grant the provenance marker to genuine PRE-CUTOVER email notes
  --neutralize strip stray priority stamps from notes that are NOT email
  --dedupe     collapse duplicate ``related`` entries left by the #21 merges

Nothing here runs automatically. It is an operator tool executed once at the
deploy moment, in the same slot as the #21 merge passes.

## Why a marker GRANT and not a marker backfill

The code fix makes the marker REQUIRED to be sampled, which is what makes a
stray priority stamp unsampleable — unconditionally, in code, without depending
on this script having run. The cost is that pre-cutover email notes have no
marker and would drop out of calibration. ``--grant`` restores them.

The grant uses ``mail_provenance.has_structural_email_headers`` — the curator's
own header shape — and NOT ``backfill._EMAIL_BODY_MARKERS``. That distinction is
the whole point. The permissive heuristic admits any note that MENTIONS email or
inbox (a Daily Sync bug report qualifies) or merely quotes an address; granting
the marker on that basis would faithfully re-import every false positive it has
already produced, wearing a name that now asserts provenance with confidence.
A soft, visibly-wrong proxy would become a hard, trusted, wrong claim.

So the grant is deliberately CONSERVATIVE: it under-grants rather than
over-grants. A genuine email note that somehow lacks headers stays out of
calibration until it is classified again — one missed batch slot. The opposite
error puts a non-email judgment into the email corpus, which is the incident.

## Safety

* Dry-run default; ``--apply`` required to write.
* Every write goes through ``vault_edit`` (contained path + scope + field
  allowlist), never a raw file write.
* ``--limit`` bounds any pass.
* Per-record failures are collected and reported, never fatal — a partial run
  is resumable because every pass is idempotent.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter

from alfred.curator.mail_provenance import (
    EMAIL_PROVENANCE_FIELD,
    has_structural_email_headers,
    note_is_email_derived,
)

#: The stamps that make a record look email-classified.
_STAMP_FIELDS = ("priority", "action_hint", "priority_reasoning")

_REAL_TIERS = ("high", "medium", "low", "spam")


@dataclass
class PassResult:
    scanned: int = 0
    matched: int = 0
    changed: int = 0
    paths: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)


def _iter_notes(vault: Path):
    note_dir = vault / "note"
    if not note_dir.is_dir():
        return
    for p in sorted(note_dir.glob("*.md")):
        try:
            yield p, frontmatter.load(str(p))
        except Exception as exc:  # noqa: BLE001 — a corrupt record is reportable, not fatal
            yield p, exc


def _has_real_stamp(fm: dict[str, Any]) -> bool:
    return str(fm.get("priority") or "").strip().lower() in _REAL_TIERS


def grant_markers(vault: Path, *, apply: bool, limit: int) -> PassResult:
    """Grant the marker to pre-cutover notes with genuine email header shape."""
    res = PassResult()
    for path, loaded in _iter_notes(vault):
        if isinstance(loaded, Exception):
            res.errors.append((path.name, str(loaded)))
            continue
        res.scanned += 1
        fm = dict(loaded.metadata or {})
        if note_is_email_derived(fm):
            continue  # already marked — idempotent
        if not has_structural_email_headers(loaded.content or ""):
            continue
        res.matched += 1
        res.paths.append(f"note/{path.name}")
        if apply:
            try:
                from alfred.vault.ops import vault_edit

                vault_edit(
                    vault, f"note/{path.name}",
                    set_fields={EMAIL_PROVENANCE_FIELD: True},
                )
                res.changed += 1
            except Exception as exc:  # noqa: BLE001
                res.errors.append((path.name, str(exc)))
        # The limit bounds the DRY RUN as well as the apply. The dry run IS the
        # preview of the apply, so a `--limit 3` that reports 10 candidates and
        # then touches 3 makes the preview describe a different operation than
        # the one that runs — worse than no preview, because it looks careful.
        if limit and res.matched >= limit:
            break
    return res


def neutralize_stray_stamps(vault: Path, *, apply: bool, limit: int) -> PassResult:
    """Strip priority stamps from notes that FAIL the mail discriminator.

    These are the records the permissive backfill heuristic classified by
    mistake. Removing the stamp retires any open review card for them (the card
    is born from the sampler) and takes them out of the rotation permanently,
    rather than relying on the sampler's marker gate alone.

    Only touches records that are BOTH unmarked AND lack email header shape —
    so a genuine email note that simply has not been granted yet is never
    stripped. Run ``--grant`` first; the two passes are ordered for that reason.
    """
    res = PassResult()
    for path, loaded in _iter_notes(vault):
        if isinstance(loaded, Exception):
            res.errors.append((path.name, str(loaded)))
            continue
        res.scanned += 1
        fm = dict(loaded.metadata or {})
        if not _has_real_stamp(fm):
            continue
        if note_is_email_derived(fm):
            continue  # genuinely mail — the stamp belongs
        if has_structural_email_headers(loaded.content or ""):
            # Looks like real mail but is unmarked: --grant's job, not ours.
            # Stripping here would destroy a real classification.
            continue
        res.matched += 1
        res.paths.append(f"note/{path.name}")
        if apply:
            try:
                from alfred.vault.ops import vault_edit

                # REMOVE the keys, don't null them. `set_fields={k: None}` writes
                # three explicit null keys into every touched record — a strip
                # that leaves litter, and a record carrying `priority: null`
                # still reads as "the classifier ran here" to a human.
                # `unset_fields` is vault_edit's own removal API.
                vault_edit(
                    vault, f"note/{path.name}",
                    unset_fields=list(_STAMP_FIELDS),
                )
                res.changed += 1
            except Exception as exc:  # noqa: BLE001
                res.errors.append((path.name, str(exc)))
        if limit and res.matched >= limit:
            break
    return res


def dedupe_related(vault: Path, *, apply: bool, limit: int) -> PassResult:
    """Collapse duplicate ``related`` entries left by the #21 merge rewrites.

    The link-rewrite mapped several aliases onto one canonical target without
    de-duplicating, so a record can now carry the same wikilink up to 5×.
    Order-preserving (first occurrence wins) so the operator's own ordering
    survives; only exact duplicates are removed.
    """
    res = PassResult()
    for path, loaded in _iter_notes(vault):
        if isinstance(loaded, Exception):
            res.errors.append((path.name, str(loaded)))
            continue
        res.scanned += 1
        fm = dict(loaded.metadata or {})
        related = fm.get("related")
        if not isinstance(related, list) or len(related) < 2:
            continue
        seen: set[str] = set()
        deduped: list[Any] = []
        for entry in related:
            key = str(entry).strip()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(entry)
        if len(deduped) == len(related):
            continue
        res.matched += 1
        res.paths.append(
            f"note/{path.name} ({len(related)} → {len(deduped)})"
        )
        if apply:
            try:
                from alfred.vault.ops import vault_edit

                vault_edit(
                    vault, f"note/{path.name}", set_fields={"related": deduped},
                )
                res.changed += 1
            except Exception as exc:  # noqa: BLE001
                res.errors.append((path.name, str(exc)))
        if limit and res.matched >= limit:
            break
    return res


def audit_corpus(corpus_path: Path, stray_paths: list[str]) -> list[str]:
    """Which stray records already have rows in the email calibration corpus?

    A corpus row for a non-email record is a judgment about something that was
    never email — it pollutes the few-shot the classifier learns from. This
    pass only REPORTS; removing rows from an append-only corpus is an operator
    decision, not a script's.
    """
    if not corpus_path.exists():
        return []
    wanted = {p.split(" (")[0] for p in stray_paths}
    hits: list[str] = []
    for raw in corpus_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        # Parse and compare the RECORD-PATH FIELD. A substring scan false-hits
        # whenever a path appears inside some other field (a reasoning string
        # quoting the filename, a cluster member list), which would report
        # corpus pollution that isn't there — and this pass exists to tell the
        # operator whether to go edit an append-only record.
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        path = str(row.get("record_path") or "").strip()
        if path and path in wanted:
            hits.append(path)
    return sorted(set(hits))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("vault", type=Path)
    ap.add_argument("--grant", action="store_true")
    ap.add_argument("--neutralize", action="store_true")
    ap.add_argument("--dedupe", action="store_true")
    ap.add_argument("--corpus", type=Path, default=None,
                    help="email calibration corpus JSONL — audited, never written")
    ap.add_argument("--apply", action="store_true",
                    help="WRITE. Omit for a dry run (the default).")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)

    if not any((args.grant, args.neutralize, args.dedupe)):
        print("nothing selected — pass --grant / --neutralize / --dedupe")
        return 2
    if not args.vault.is_dir():
        print(f"no such vault: {args.vault}")
        return 2

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"#40 mail-provenance cleanup — {mode} — vault={args.vault}\n")

    stray: list[str] = []
    # Order matters: grant BEFORE neutralize, so a genuine email note is marked
    # rather than stripped.
    for flag, name, fn in (
        (args.grant, "grant markers", grant_markers),
        (args.neutralize, "neutralize stray stamps", neutralize_stray_stamps),
        (args.dedupe, "dedupe related", dedupe_related),
    ):
        if not flag:
            continue
        res = fn(args.vault, apply=args.apply, limit=args.limit)
        print(f"--- {name}")
        print(f"    scanned={res.scanned} matched={res.matched} changed={res.changed}")
        for p in res.paths[:40]:
            print(f"      {p}")
        if len(res.paths) > 40:
            print(f"      … and {len(res.paths) - 40} more")
        if not res.paths:
            # ILB — "ran, nothing matched" must be distinguishable from "did not run".
            print("      (nothing matched — pass ran, found no candidates)")
        for path, err in res.errors:
            print(f"      ERROR {path}: {err}")
        print()
        if fn is neutralize_stray_stamps:
            stray = res.paths

    if args.corpus and stray:
        hits = audit_corpus(args.corpus, stray)
        print("--- calibration corpus audit")
        if hits:
            print("    POLLUTED — rows exist for non-email records:")
            for h in hits:
                print(f"      {h}")
            print("    These are email judgments about non-email records.")
            print("    Removal is an operator decision — not done here.")
        else:
            print("    clean — no corpus rows reference any stray record")
        print()

    if not args.apply:
        print("DRY RUN — nothing was written. Re-run with --apply to commit.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
