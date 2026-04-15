"""Stage 4: OpenRouter LLM labeling — cluster tags + relationship suggestions."""

from __future__ import annotations

import asyncio
import json

import structlog
from openai import AsyncOpenAI

from .config import LabelerConfig, OpenRouterConfig
from .parser import VaultRecord

log = structlog.get_logger()

CLUSTER_LABEL_PROMPT = """\
You are labeling a cluster of related documents from an Obsidian vault.

Each document has a type, name, and body preview. Based on the thematic content, assign 1-3 descriptive tags that capture what this cluster is about.

Tags should be:
- Hierarchical where appropriate (e.g. "construction/residential", "finance/invoicing")
- Lowercase, using / for hierarchy
- Descriptive of the shared theme, not the document types

Documents in this cluster:
{members}

Respond with ONLY a JSON array of tag strings. Example: ["construction/residential", "project-management"]
"""

RELATIONSHIP_PROMPT = """\
You are analyzing documents from an Obsidian vault that were found to be semantically related (in the same cluster) but don't currently link to each other.

For each pair, decide whether a REAL relationship exists — grounded in concrete facts, not generic theme.

GROUNDEDNESS RULE (hard requirement):
Only suggest a relationship if both records share an explicit factual anchor — a named person, organization, project, product, date/date-range, location, or event mentioned in BOTH records. Generic thematic similarity ("both are organizations", "both offer services", "both are tech companies", "both are marketing emails") is NOT an acceptable anchor. If you cannot point to a concrete shared entity present in both texts, emit nothing for that pair.

You must cite a short verbatim phrase from each side as "source_anchor" and "target_anchor". If you cannot cite both, DO NOT emit the relationship — drop the pair.

Allowed relationship types (use exactly one, with the definition shown):
- "related-to": both records reference the same named entity (person/org/project/event/location) but neither depends on nor supports the other.
- "supports": the target provides evidence, documentation, or justification for a specific claim or decision stated in the source.
- "depends-on": the source cannot function, be completed, or be understood without the target (prerequisite or required input).
- "part-of": the source is a component, subset, phase, or deliverable of a larger whole named in the target.
- "supersedes": the source explicitly replaces, overrides, or obsoletes the target (same subject, later version or decision).

Do NOT use any other relationship type. In particular, do NOT emit "contradicts" — contradiction analysis is handled elsewhere, not here.

NEGATIVE EXAMPLE (do not do this):
BAD: `org/DigitalOcean.md → org/Marriott.md` type "related-to" with rationale "both are large enterprises offering services" — REJECTED. No named person, project, event, date, or location appears in both records. Generic "both are companies" is not a factual anchor. Drop the pair.

Documents:
{pairs}

Respond with ONLY a JSON array of objects, each with:
- "source": source file path
- "target": target file path
- "type": one of the allowed relationship types above
- "context": brief explanation naming the shared anchor (max 80 chars)
- "confidence": float 0-1
- "source_anchor": short verbatim phrase (<= 80 chars) from the source that mentions the shared entity
- "target_anchor": short verbatim phrase (<= 80 chars) from the target that mentions the same shared entity

Only include pairs where confidence >= 0.65 AND both anchors are present. If no grounded relationships are found, return [].

Return the JSON array directly with no markdown code fences, no ```json wrapping, no prose explanation.
"""


def _strip_code_fences(text: str) -> str:
    """Strip markdown code fences from an LLM response, if present.

    Handles:
    - ```json\\n[...]\\n``` (with language tag)
    - ```\\n[...]\\n``` (without language tag)
    - [...] (raw, no fences — passthrough)
    - Leading/trailing whitespace
    - Surrounding prose before/after a fenced block (extracts the fenced content)
    """
    if not text:
        return text
    stripped = text.strip()
    fence_start = stripped.find("```")
    if fence_start == -1:
        return stripped
    # Everything after the opening fence
    after_open = stripped[fence_start + 3 :]
    # Drop an optional language tag on the same line as the opening fence
    newline_idx = after_open.find("\n")
    if newline_idx != -1:
        first_line = after_open[:newline_idx]
        # If the first line is a language tag (letters/digits only), skip it
        if first_line.strip() and all(c.isalnum() for c in first_line.strip()):
            after_open = after_open[newline_idx + 1 :]
    # Find the closing fence
    fence_end = after_open.find("```")
    if fence_end == -1:
        # Unterminated fence — return what we have after the opener
        return after_open.strip()
    return after_open[:fence_end].strip()

# Rate limiting
API_CALL_DELAY = 1.0
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0


class Labeler:
    def __init__(self, openrouter_cfg: OpenRouterConfig, labeler_cfg: LabelerConfig) -> None:
        self.client = AsyncOpenAI(
            api_key=openrouter_cfg.api_key,
            base_url=openrouter_cfg.base_url,
        )
        self.model = openrouter_cfg.model
        # Force deterministic labeling regardless of config value.
        # Non-zero temperature caused identical clusters to produce different tag
        # sets across sweeps, which drove continuous re-writes of alfred_tags on
        # member files (including session notes). Hardcoded to 0 until/unless
        # there's a strong reason to make it configurable again.
        self.temperature = 0.0
        self.max_files = labeler_cfg.max_files_per_cluster_context
        self.body_preview_chars = labeler_cfg.body_preview_chars
        self.min_cluster_size = labeler_cfg.min_cluster_size_to_label
        self.min_relationship_confidence = labeler_cfg.min_relationship_confidence

    async def label_cluster(
        self,
        cluster_id: int,
        member_paths: list[str],
        records: dict[str, VaultRecord],
    ) -> list[str]:
        """Get 1-3 descriptive tags for a cluster from the LLM."""
        if len(member_paths) < self.min_cluster_size:
            return []

        # Build member summaries
        members_text = self._build_member_summaries(member_paths, records)
        prompt = CLUSTER_LABEL_PROMPT.format(members=members_text)

        response = await self._llm_call(prompt)
        if response is None:
            return []

        try:
            tags = json.loads(_strip_code_fences(response))
            if isinstance(tags, list) and all(isinstance(t, str) for t in tags):
                return tags[:3]
        except (json.JSONDecodeError, TypeError):
            log.warning("labeler.parse_error", cluster_id=cluster_id, response=response[:200])

        return []

    async def suggest_relationships(
        self,
        cluster_id: int,
        member_paths: list[str],
        records: dict[str, VaultRecord],
    ) -> list[dict]:
        """Suggest relationships for co-clustered files that lack links between them."""
        if len(member_paths) < 2:
            return []

        # Find pairs that don't already link to each other
        unlinked_pairs = self._find_unlinked_pairs(member_paths, records)
        if not unlinked_pairs:
            return []

        # Truncate pairs for context
        unlinked_pairs = unlinked_pairs[:10]

        pairs_text = self._build_pairs_text(unlinked_pairs, records)
        prompt = RELATIONSHIP_PROMPT.format(pairs=pairs_text)

        response = await self._llm_call(prompt)
        if response is None:
            return []

        try:
            rels = json.loads(_strip_code_fences(response))
            if isinstance(rels, list):
                return [
                    r for r in rels
                    if isinstance(r, dict)
                    and all(k in r for k in ("source", "target", "type", "context", "confidence", "source_anchor", "target_anchor"))
                    and r["confidence"] >= self.min_relationship_confidence
                ]
        except (json.JSONDecodeError, TypeError):
            log.warning("labeler.rel_parse_error", cluster_id=cluster_id, response=response[:200])

        return []

    def _build_member_summaries(
        self, paths: list[str], records: dict[str, VaultRecord]
    ) -> str:
        """Build text summaries of cluster members for the LLM."""
        lines: list[str] = []
        for path in paths[: self.max_files]:
            record = records.get(path)
            if record is None:
                lines.append(f"- [{path}] (no content available)")
                continue
            name = record.frontmatter.get("name", path)
            rtype = record.record_type
            preview = record.body[: self.body_preview_chars].replace("\n", " ").strip()
            lines.append(f"- [{rtype}] {name}: {preview}")
        return "\n".join(lines)

    def _find_unlinked_pairs(
        self, paths: list[str], records: dict[str, VaultRecord]
    ) -> list[tuple[str, str]]:
        """Find pairs of files in the cluster that don't link to each other."""
        # Build set of existing links for each file
        link_sets: dict[str, set[str]] = {}
        for path in paths:
            record = records.get(path)
            if record:
                link_sets[path] = set(record.wikilinks)
            else:
                link_sets[path] = set()

        pairs: list[tuple[str, str]] = []
        for i, p1 in enumerate(paths):
            for p2 in paths[i + 1 :]:
                # Check if either links to the other (by name or path)
                p1_name = p1.rsplit("/", 1)[-1].replace(".md", "")
                p2_name = p2.rsplit("/", 1)[-1].replace(".md", "")
                if p2_name not in link_sets.get(p1, set()) and p1_name not in link_sets.get(p2, set()):
                    pairs.append((p1, p2))
        return pairs

    def _build_pairs_text(
        self, pairs: list[tuple[str, str]], records: dict[str, VaultRecord]
    ) -> str:
        lines: list[str] = []
        for src, tgt in pairs:
            src_rec = records.get(src)
            tgt_rec = records.get(tgt)
            src_name = src_rec.frontmatter.get("name", src) if src_rec else src
            tgt_name = tgt_rec.frontmatter.get("name", tgt) if tgt_rec else tgt
            src_type = src_rec.record_type if src_rec else "unknown"
            tgt_type = tgt_rec.record_type if tgt_rec else "unknown"
            lines.append(f"- [{src_type}] {src_name} ({src}) ↔ [{tgt_type}] {tgt_name} ({tgt})")
        return "\n".join(lines)

    async def _llm_call(self, prompt: str) -> str | None:
        """Make an LLM call with rate limiting and retry."""
        for attempt in range(MAX_RETRIES):
            try:
                resp = await self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=self.temperature,
                )
                if resp.usage:
                    log.info(
                        "labeler.usage",
                        total_tokens=resp.usage.total_tokens,
                        prompt_tokens=resp.usage.prompt_tokens,
                        completion_tokens=resp.usage.completion_tokens,
                    )
                await asyncio.sleep(API_CALL_DELAY)
                return resp.choices[0].message.content
            except Exception as e:
                error_str = str(e)
                if "429" in error_str:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    log.warning("labeler.rate_limited", attempt=attempt + 1, delay=delay)
                    await asyncio.sleep(delay)
                else:
                    log.error("labeler.llm_error", error=error_str)
                    return None
        log.error("labeler.llm_failed", max_retries=MAX_RETRIES)
        return None
