"""Build a compact vault context snapshot for the agent prompt."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import frontmatter

from .utils import get_logger

log = get_logger(__name__)

_FROM_RE = re.compile(r"\*\*From:\*\*\s*(\S+@\S+)")
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")


@dataclass
class RecordSummary:
    path: str  # relative to vault root
    name: str
    status: str = ""
    extra: dict[str, str] = field(default_factory=dict)


@dataclass
class VaultContext:
    records_by_type: dict[str, list[RecordSummary]] = field(default_factory=dict)

    @property
    def total_records(self) -> int:
        return sum(len(v) for v in self.records_by_type.values())

    def to_prompt_text(self) -> str:
        """Compact entity index grouped by type.

        Emits a slim name-only index instead of full record listings.
        The LLM only needs entity names for dedup awareness — Stage 2
        Python does the actual filesystem check.
        Ref: ssdavidai/alfred#14 (curator token reduction)
        """
        lines: list[str] = []
        for rec_type in sorted(self.records_by_type.keys()):
            records = self.records_by_type[rec_type]
            names = [r.name for r in sorted(records, key=lambda r: r.name)]
            lines.append(f"### {rec_type} ({len(names)})")
            # Comma-separated names, wrapping at ~120 chars per line
            current_line = ""
            for name in names:
                addition = name if not current_line else f", {name}"
                if current_line and len(current_line) + len(addition) > 120:
                    lines.append(current_line)
                    current_line = name
                else:
                    current_line += addition
            if current_line:
                lines.append(current_line)
            lines.append("")
        return "\n".join(lines)


def build_vault_context(
    vault_path: Path,
    ignore_dirs: list[str] | None = None,
) -> VaultContext:
    """Walk vault, parse frontmatter of every .md, group by type."""
    ignore = set(ignore_dirs or [])
    ignore.add(".obsidian")
    ctx = VaultContext()

    for md_file in vault_path.rglob("*.md"):
        # Skip ignored directories
        rel = md_file.relative_to(vault_path)
        parts = rel.parts
        if any(p in ignore for p in parts):
            continue
        # Skip inbox files
        if parts[0] == "inbox":
            continue

        try:
            post = frontmatter.load(str(md_file))
        except Exception:
            continue

        rec_type = post.metadata.get("type", "")
        if not rec_type:
            continue

        name = md_file.stem
        status = str(post.metadata.get("status", ""))
        rel_path = str(rel).replace("\\", "/")
        # Remove .md extension for wikilink style
        if rel_path.endswith(".md"):
            rel_path = rel_path[:-3]

        summary = RecordSummary(path=rel_path, name=name, status=status)
        ctx.records_by_type.setdefault(rec_type, []).append(summary)

    log.info(
        "context.built",
        types=len(ctx.records_by_type),
        total=ctx.total_records,
    )
    return ctx


def extract_sender_email(inbox_content: str) -> str | None:
    """Extract sender email address from **From:** line in inbox content."""
    m = _FROM_RE.search(inbox_content)
    if not m:
        return None
    email = m.group(1).strip("<>").removeprefix("mailto:")
    return email if "@" in email else None


def gather_sender_context(
    vault_path: Path,
    sender_email: str,
    ignore_dirs: list[str] | None = None,
) -> str:
    """Find person record matching sender email, return linked context."""
    person_dir = vault_path / "person"
    if not person_dir.is_dir():
        return ""

    sender_lower = sender_email.lower()
    person_path: Path | None = None
    person_fm: dict = {}

    # Find person record with matching email
    for md_file in person_dir.glob("*.md"):
        try:
            post = frontmatter.load(str(md_file))
        except Exception:
            continue
        fm_email = str(post.metadata.get("email", "")).strip("<>").removeprefix("mailto:")
        if fm_email.lower() == sender_lower:
            person_path = md_file
            person_fm = post.metadata
            break

    if not person_path:
        return ""

    person_name = person_path.stem
    rel = str(person_path.relative_to(vault_path)).replace("\\", "/")
    if rel.endswith(".md"):
        rel = rel[:-3]

    lines = [f"## Sender Context: {person_name}", ""]

    # Person summary
    status = person_fm.get("status", "")
    role = person_fm.get("role", "")
    person_parts = [f"**Person:** [[{rel}|{person_name}]]"]
    if status:
        person_parts.append(f"status: {status}")
    if role:
        person_parts.append(f"role: {role}")
    lines.append(" — ".join(person_parts))

    # Collect all wikilink targets from person's frontmatter
    linked_paths: list[str] = []
    for key in ("org", "project", "related", "relationships"):
        val = person_fm.get(key)
        if isinstance(val, str):
            linked_paths.extend(_WIKILINK_RE.findall(val))
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, str):
                    linked_paths.extend(_WIKILINK_RE.findall(item))

    # Read linked records and group by type
    ignore = set(ignore_dirs or [])
    linked_by_type: dict[str, list[str]] = {}
    for link_target in linked_paths:
        # Resolve wikilink to file path
        target_file = vault_path / (link_target + ".md")
        if not target_file.is_file():
            continue
        target_rel = target_file.relative_to(vault_path)
        if any(p in ignore for p in target_rel.parts):
            continue
        try:
            post = frontmatter.load(str(target_file))
        except Exception:
            continue
        fm = post.metadata
        rec_type = fm.get("type", "unknown")
        rec_status = fm.get("status", "")
        rec_name = target_file.stem
        link_path = str(target_rel).replace("\\", "/")
        if link_path.endswith(".md"):
            link_path = link_path[:-3]
        entry = f"[[{link_path}|{rec_name}]]"
        if rec_status:
            entry += f" ({rec_status})"
        linked_by_type.setdefault(rec_type, []).append(entry)

    # Search for conversations and tasks mentioning this person
    for search_type, search_dir in [("conversation", "conversation"), ("task", "task")]:
        type_dir = vault_path / search_dir
        if not type_dir.is_dir():
            continue
        for md_file in type_dir.glob("*.md"):
            target_rel = md_file.relative_to(vault_path)
            link_path = str(target_rel).replace("\\", "/")
            if link_path.endswith(".md"):
                link_path = link_path[:-3]
            # Skip if already found via frontmatter links
            if any(link_path in entry for entry in linked_by_type.get(search_type, [])):
                continue
            try:
                content = md_file.read_text(encoding="utf-8")
            except Exception:
                continue
            if person_name not in content:
                continue
            try:
                post = frontmatter.load(str(md_file))
            except Exception:
                continue
            fm = post.metadata
            rec_status = fm.get("status", "")
            rec_name = md_file.stem
            entry = f"[[{link_path}|{rec_name}]]"
            if rec_status:
                entry += f" ({rec_status})"
            if search_type == "task":
                due = fm.get("due", "")
                priority = fm.get("priority", "")
                if due:
                    entry += f" due: {due}"
                if priority:
                    entry += f" priority: {priority}"
            elif search_type == "conversation":
                last = fm.get("last_activity", "")
                if last:
                    entry += f" last: {last}"
            linked_by_type.setdefault(search_type, []).append(entry)

    # Format output
    type_labels = {
        "org": "Organizations",
        "project": "Projects",
        "conversation": "Conversations",
        "task": "Open Tasks",
        "account": "Accounts",
        "note": "Recent Notes",
    }
    for rec_type, label in type_labels.items():
        entries = linked_by_type.get(rec_type, [])
        if entries:
            lines.append(f"**{label}:** {', '.join(entries[:10])}")

    # Include any other linked types not in the label map
    for rec_type, entries in linked_by_type.items():
        if rec_type not in type_labels and entries:
            lines.append(f"**{rec_type.title()}:** {', '.join(entries[:5])}")

    if len(lines) <= 2:
        # Only header, no linked records found
        return ""

    log.info("context.sender_found", person=person_name, linked=sum(len(v) for v in linked_by_type.values()))
    return "\n".join(lines)
