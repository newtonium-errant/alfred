You are extracting a structured voice profile from a single piece of Andrew Errant's writing. The goal is a fixture future ghostwriting / draft-tuning calls can read to match Andrew's voice precisely on similar work.

You will be given the FULL essay text. Your output is a Markdown document with structured frontmatter + a brief prose summary in the body. The downstream consumer parses the frontmatter directly and reads the body for context.

## Evidence-anchoring rule (load-bearing)

Every label, list entry, or characterisation in this profile must be **quotable**. A profile that says ``comic_moves: [deadpan, escalation]`` without naming WHERE in the essay those moves appear is useless for calibration — it could describe almost any writer. For every label, you should be able to point to a verbatim ≤12-word quote from the essay that demonstrates it. Lists below specify a ``with: "<short verbatim quote>"`` per entry where applicable. Quote exactly — do not paraphrase, do not insert ellipses inside the quote.

## Required frontmatter fields

All strings unless noted. Use YAML inline syntax (e.g. ``comic_moves: [deadpan, escalation]``) for short lists; use block syntax for the evidence-bearing lists below.

  register: formal | casual | intimate | declarative | conversational | academic | hybrid (1-3 hybrid labels OK, e.g. "casual-declarative")
  paragraph_rhythm: short-paragraphs | medium-paragraphs | long-paragraphs | mixed-rhythm
  single_sentence_paragraphs_frequency: rare | occasional | frequent | dominant
  comic_moves:                  # 2-5 entries, evidence-anchored
    - move: deadpan-after-technical-detail
      with: "Some arts and crafts with a map"
    - move: escalation
      with: "..."
  opening_style:                # object form, evidence-anchored
    description: "1-line description of the typical opening shape"
    with: "<verbatim ≤12-word quote of the actual opening>"
  closing_style:                # same object shape as opening_style
    description: "1-line description of the typical closing shape"
    with: "<verbatim ≤12-word quote of the actual closing>"
  transition_style:             # same object shape (description + evidence)
    description: "linking phrases? section breaks? em-dashes mid-paragraph?"
    with: "<one verbatim example transition from the essay>"
  footnote_conventions: present | absent | inline-asides-instead | parenthetical-heavy
  punctuation_tics:             # 2-5 entries, evidence-anchored
    - tic: em-dash-mid-paragraph
      with: "the navigator — and yes I mean the role"
    - tic: italics-for-emphasis
      with: "..."
  lexicon_tells:                # 4-8 verbatim phrases / sentence starters / framings; pull verbatim from the essay; NO paraphrase
    - "..."
    - "..."
  voice_signature: one descriptive sentence (≤30 words) capturing the voice; concrete, not generic

## YAML-safety rules (load-bearing — parse failures break the consumer)

The downstream consumer parses your output as YAML directly. A single malformed value crashes the whole load. Two failure shapes to avoid:

  - **No content after a closed quote on the same line.** Do NOT write ``some_field: "<description>" — "<quote>"`` (description, em-dash separator, then a second quoted phrase on the same line). YAML rejects content after a quoted scalar on the same line: it parses ``"description"`` then errors on the unexpected `` — "quote"``. The em-dash is a common trigger but the rule is general — never put inline content after a closed quote on the same line. The object form (``description:`` + ``with:`` on separate lines, as the schema specifies for ``opening_style`` / ``closing_style`` / ``transition_style``) is the parse-safe shape — use it.
  - **Single-line values with internal quotes need outer single-quotes or block scalars.** If a value naturally contains a double quote (e.g. a verbatim quote inside a longer description), wrap the whole value in single quotes (``field: 'He said "no" and meant it'``) or use a block scalar (``field: |\n  He said "no" and meant it``). Do NOT mix unescaped quotes inside an unquoted value.

When in doubt, prefer the **block / object form** for any field that combines a description with an evidence quote — it never mis-parses.

Body (after the frontmatter):

  - One paragraph (3-5 sentences) describing the overall voice in plain prose. Concrete, NOT generic. Cite 2-3 short verbatim phrases from the essay, each in quotes.
  - One paragraph describing what NOT to do — voice elements another draft might falsely add (e.g. "do not add corporate buzzwords; do not write headline subheadings within paragraphs"). Be specific to this essay's posture, not generic writing-advice.

## When the essay has no clear voice

If the input is a fragment, a rough draft, or otherwise too thin to profile (under ~400 words, or stylistically inconsistent in a way that suggests Andrew was just typing not crafting), DO NOT fabricate a voice. Instead, return the frontmatter with:

  status: insufficient-evidence
  insufficient_reason: "<one sentence on what's missing>"

and a body that says ``This input was insufficient to extract a voice profile. <reason>.`` Do NOT pad with generic descriptors to look useful — silent absence is worse than honest absence here per the ``intentionally left blank`` rule.

Output only the Markdown document — no commentary, no code fence, nothing before the frontmatter and nothing after the body. The frontmatter starts with ``---`` on the first line.
