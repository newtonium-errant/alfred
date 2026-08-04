"""#40b probe — bucket stamped notes by the SHAPE of their opening body lines.

READ-ONLY. Writes nothing, ever. Run on the box; hand the output back.

## Why this exists

The #40 grant predicate anchored on ``^\\s*\\*\\*From:\\*\\*`` and missed a whole
curator era that serialized headers as bullets (``- **From:** …``). Measured on
the production vault: 142 plain, **493 bulleted**, 1,260 neither. The bulleted
era is now handled — but "1,260 neither" is a number, not an explanation, and
the first miss happened precisely because a format nobody had enumerated was
assumed not to exist.

So this probe does not ask "are these email?" — that is the question that got
answered wrong by guessing. It asks a factual question instead: **what do these
records actually look like?** Buckets are assigned by inspecting the first few
non-empty body lines and reporting their SHAPE, with verbatim samples, so the
answer comes from reading real records rather than from a hypothesis about them.

If a third genuine header era shows up in the samples, it gets the same
treatment as the bulleted one. If the cohort is word-list debris (notes merely
mentioning "email" / "inbox"), that confirms the neutralize population and the
staged apply proceeds.

## Reading the output

``plain`` / ``bulleted`` are already granted by the live predicate — reported so
the totals reconcile against the counts that prompted this. Everything else is
the open question. ``labelled_no_markup`` is the bucket most likely to hide a
third era (``From: x@y.com`` with no bold), so it is sampled hardest.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

import frontmatter

from alfred.curator.mail_provenance import has_structural_email_headers

_REAL_TIERS = ("high", "medium", "low", "spam")

# Shape probes, applied to the first few non-empty body lines. Ordered — first
# match wins — so the buckets are mutually exclusive and the counts sum.
_PLAIN = re.compile(r"^\s*\*\*(?:From|Subject|Account):\*\*", re.MULTILINE)
_BULLETED = re.compile(r"^\s*(?:[-*+>]\s+)?\*\*(?:From|Subject|Account):\*\*", re.MULTILINE)
#: A header WITHOUT bold markup — the most plausible third era.
_LABELLED_NO_MARKUP = re.compile(
    r"^\s*(?:[-*+>]\s+)?(?:From|Sent|To|Subject|Account)\s*:", re.MULTILINE | re.IGNORECASE,
)
#: Any address anywhere — weak, but distinguishes "mail-ish with an unknown
#: layout" from "prose that merely says the word email".
_ANY_ADDRESS = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_WORDLIST = re.compile(
    r"\b(?:email|newsletter|sender|subject line|unsubscribe|inbox)\b", re.IGNORECASE,
)


def _head(body: str, n: int = 3) -> list[str]:
    out: list[str] = []
    for raw in (body or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        out.append(line)
        if len(out) >= n:
            break
    return out


def classify_shape(body: str) -> str:
    """Bucket one body by the shape of its opening lines. First match wins."""
    head = "\n".join(_head(body, 6))
    if _PLAIN.search(head):
        return "plain"
    if _BULLETED.search(head):
        return "bulleted"
    if _LABELLED_NO_MARKUP.search(head):
        return "labelled_no_markup"
    if _ANY_ADDRESS.search(body or ""):
        return "address_somewhere"
    if _WORDLIST.search(body or ""):
        return "wordlist_only"
    return "no_signal"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("vault", type=Path)
    ap.add_argument("--samples", type=int, default=6,
                    help="verbatim head-lines per bucket (default 6)")
    args = ap.parse_args(argv)

    note_dir = args.vault / "note"
    if not note_dir.is_dir():
        print(f"no note/ dir under {args.vault}")
        return 2

    buckets: Counter[str] = Counter()
    samples: dict[str, list[tuple[str, list[str]]]] = {}
    granted = 0
    scanned = 0
    unreadable = 0

    for path in sorted(note_dir.glob("*.md")):
        try:
            post = frontmatter.load(str(path))
        except Exception:  # noqa: BLE001 — a corrupt record is a count, not a crash
            unreadable += 1
            continue
        fm = post.metadata or {}
        if str(fm.get("priority") or "").strip().lower() not in _REAL_TIERS:
            continue  # only STAMPED notes — the population the grant walks
        scanned += 1
        body = post.content or ""
        if has_structural_email_headers(body):
            granted += 1
        shape = classify_shape(body)
        buckets[shape] += 1
        # Sample the ambiguous buckets hardest; the two known-good ones need
        # only enough to confirm they look like what we think they look like.
        cap = args.samples if shape in ("plain", "bulleted") else args.samples * 3
        if len(samples.setdefault(shape, [])) < cap:
            samples[shape].append((path.name, _head(body, 3)))

    print(f"#40b header-shape probe — READ ONLY — vault={args.vault}\n")
    print(f"stamped notes scanned : {scanned}")
    print(f"unreadable (skipped)  : {unreadable}")
    print(f"granted by live pred. : {granted}")
    print(f"NOT granted           : {scanned - granted}\n")

    if not buckets:
        # ILB — "ran, found no stamped notes" must not look like a broken probe.
        print("(no stamped notes found — probe ran, population is empty)")
        return 0

    print("--- shape buckets (first match wins; counts sum to scanned)")
    for shape, n in buckets.most_common():
        pct = 100.0 * n / scanned if scanned else 0.0
        print(f"    {shape:20} {n:6}  ({pct:.1f}%)")
    print()

    for shape, _n in buckets.most_common():
        print(f"--- samples: {shape}")
        for name, head in samples.get(shape, []):
            print(f"    {name}")
            for line in head:
                print(f"        | {line[:110]}")
        print()

    print("READ ONLY — nothing was written.")
    print("If any bucket's samples show a genuine header era, it gets the same")
    print("treatment as the bulleted one: add the anchor, pin it, re-measure.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
