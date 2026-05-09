You are synthesizing a voice CLUSTER profile from multiple leaf voice profiles that share a cluster tag. The cluster represents a posture Andrew uses across several pieces (e.g. "veteran" for veteran-affairs writing, "technical" for systems writeups, "personal-essay" for substack drafts).

You will be given the leaf voice profiles in order. Each leaf already contains evidence-anchored frontmatter (comic_moves, punctuation_tics, lexicon_tells, etc.) with verbatim quotes attached. Your job is to **aggregate by COUNTING across leaves**, not to re-characterise from scratch. If 4 of 5 leaves list ``deadpan-after-technical-detail`` as a comic move, that's a signature move of this cluster. If only 1 of 5 does, it's leaf-specific noise — drop it.

## Aggregation rules

  - **Union with frequency**: for each list field (comic_moves, punctuation_tics, lexicon_tells), count how many leaves include the entry. Sort descending by count. Drop entries that appear in only 1 leaf unless the cluster has only 2 leaves.
  - **Preserve evidence**: each retained entry should keep ONE representative verbatim quote from the leaves (pick the most characteristic one).
  - **Consolidated labels** (register, paragraph_rhythm): if the leaves disagree, name the disagreement (``register: casual-with-academic-asides``) rather than averaging.

## Required frontmatter fields

  cluster_name: <name>
  leaf_count: <n>
  leaf_titles: list[str]         # the file basenames or essay titles of the leaves used (so downstream readers can trace back)
  register: <consolidated label, may be hybrid; name disagreement if present>
  paragraph_rhythm: <consolidated>
  comic_moves:                   # ordered by leaf-frequency desc
    - move: <name>
      seen_in: <n_of_total>
      with: "<one representative ≤12-word quote>"
  punctuation_tics:              # same shape
    - tic: <name>
      seen_in: <n_of_total>
      with: "<quote>"
  lexicon_tells:                 # phrases in ≥2 leaves
    - "<verbatim phrase>"
  signature_moves: list[str]     # 3-6 moves present in ≥60% of leaves (the cluster's fingerprint — distinct from comic_moves; can include structural patterns like "opens with a scene then pivots")
  voice_signature: one descriptive sentence (≤30 words) capturing the cluster's posture; concrete

Body (after frontmatter):

  - ## What this cluster sounds like
    2-3 paragraphs in plain prose describing the cluster's voice. Concrete. Cite 2-3 short verbatim phrases drawn from the leaves, each in quotes, each tagged with the leaf it came from (``"…" — from <leaf-title>``).

  - ## What's distinctive about this posture
    1-2 paragraphs describing what makes this cluster recognisable on its own terms — the specific stance, audience-stance, register, or rhetorical move that defines it. (You don't have the other clusters in front of you; describe THIS cluster's defining shape without comparison, and trust that distinctiveness will emerge by contrast at the overall-profile stage.)

  - ## Worked example sketch
    A 3-5 sentence pseudo-paragraph in the cluster's voice, on a made-up topic Andrew has not actually written about, demonstrating the consolidated feel. CRITICAL: this must be a fresh demonstration of the cluster's signature_moves and lexicon — NOT a remix of any single leaf. Pick a topic clearly outside what's in the leaves (e.g. if the leaves are about veteran affairs and Substack process, write the sketch about train timetables or sourdough bread). Use ≥2 of the signature_moves and ≥1 lexicon_tell visibly.

## When the cluster doesn't actually cohere

If the leaves don't share a recognisable voice (the cluster tag was likely wrong, or the leaves span genuinely different postures), return:

  status: incoherent-cluster
  incoherent_reason: "<one sentence on what doesn't fit>"

with a body section ``## Cluster does not cohere`` describing which leaves seem to belong together vs which seem misfiled. Don't manufacture a fake fingerprint to satisfy the schema.

Output only the Markdown document.
