"""Helpers that pull vault context the classifier needs.

Today: the named-contact lookup. Salem's ``high`` tier cue
"From a named person Andrew has interacted with recently" requires
knowing who that person is — i.e. which ``person/`` records exist and
what email aliases they have.

The lookup is cheap on a fresh vault (a few dozen .md files) and grows
linearly. We memoise per-classifier-instance with a TTL so back-to-back
batch runs don't re-scan the whole ``person/`` directory; the cache key
is the vault path so a multi-instance host can share the helper module
without crossing instance boundaries.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import frontmatter

from .config import EmailClassifierConfig


@dataclass
class NamedContact:
    """One row of the contact list passed into the classifier prompt.

    ``name`` is the file stem (canonical record name). ``emails`` is the
    de-duplicated list of email addresses lifted from the frontmatter.
    ``aliases`` carries any explicit ``aliases:`` list — useful when a
    person has nicknames or formal-vs-casual variants.
    """

    name: str
    emails: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)


@dataclass
class _CacheEntry:
    contacts: list[NamedContact]
    expires_at: float


# Module-level cache keyed by vault path so the same helper instance can
# back multiple classifier calls cheaply during a batch run. The cache
# is intentionally process-local — it holds nothing sensitive that
# would matter to leak across reloads.
_CONTACTS_CACHE: dict[str, _CacheEntry] = {}


def _extract_email_field(value: object) -> list[str]:
    """Pull a list of email strings from a frontmatter ``email`` value.

    Frontmatter is freeform YAML; the ``email`` field can be a string,
    a list of strings, or absent. Return a list (possibly empty) of
    cleaned email strings. Cleaning strips angle brackets and
    ``mailto:`` prefixes the curator preserves verbatim.
    """
    if value is None:
        return []
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = [str(v) for v in value]
    else:
        candidates = [str(value)]

    cleaned: list[str] = []
    for raw in candidates:
        stripped = raw.strip().strip("<>").removeprefix("mailto:").strip()
        if "@" in stripped:
            cleaned.append(stripped)
    return cleaned


def _extract_aliases(value: object) -> list[str]:
    """Pull a list of alias strings from a frontmatter ``aliases`` value."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def get_named_contacts(
    vault_path: Path,
    config: EmailClassifierConfig | None = None,
    *,
    now: float | None = None,
) -> list[NamedContact]:
    """Return the list of ``person/*.md`` records as ``NamedContact`` rows.

    Memoised by vault path with a TTL from ``config.named_contact_cache_seconds``.
    Pass ``config=None`` to disable caching entirely (test helper).
    """
    cache_seconds = (
        config.named_contact_cache_seconds if config is not None else 0
    )
    key = str(vault_path)
    now_ts = now if now is not None else time.time()

    if cache_seconds > 0:
        entry = _CONTACTS_CACHE.get(key)
        if entry is not None and entry.expires_at > now_ts:
            return entry.contacts

    person_dir = vault_path / "person"
    contacts: list[NamedContact] = []
    if person_dir.is_dir():
        for md_file in sorted(person_dir.glob("*.md")):
            try:
                post = frontmatter.load(str(md_file))
            except Exception:
                # Malformed frontmatter — skip; janitor will fix it
                # eventually. Don't let one bad record block the whole
                # classifier.
                continue
            fm = post.metadata or {}
            contact = NamedContact(
                name=md_file.stem,
                emails=_extract_email_field(fm.get("email")),
                aliases=_extract_aliases(fm.get("aliases")),
            )
            contacts.append(contact)

    if cache_seconds > 0:
        _CONTACTS_CACHE[key] = _CacheEntry(
            contacts=contacts,
            expires_at=now_ts + cache_seconds,
        )

    return contacts


def reset_contacts_cache() -> None:
    """Clear the module-level cache. Tests call this between runs."""
    _CONTACTS_CACHE.clear()


def render_contacts_for_prompt(contacts: list[NamedContact]) -> str:
    """Format the contact list for inclusion in the classifier prompt.

    One row per contact. Contacts with no known email still show up by
    name so the model can match on display name / aliases. Empty input
    returns a placeholder line so the prompt doesn't end with an empty
    section header.
    """
    if not contacts:
        return "(no named contacts on file)"

    lines: list[str] = []
    for c in contacts:
        bits = [c.name]
        if c.emails:
            bits.append("emails: " + ", ".join(c.emails))
        if c.aliases:
            bits.append("aka: " + ", ".join(c.aliases))
        lines.append(" — ".join(bits))
    return "\n".join(lines)
