"""Per-project review surface for KAL-LE.

KAL-LE writes review markdown files into ``<project-vault>/teams/alfred/reviews/``
so a per-project Claude can see them in-tree. Frontmatter shape is
distinct from the existing human-authored review convention so list /
read / mark-addressed never touches a human-authored file.

Disagreement archive convention (no automation in this module): when
project-Claude disagrees with a KAL-LE review, project-Claude writes a
sibling ``<same-name>—claude-disagreement.md`` or appends a
``## Claude Code Response`` section to the original. The cross-project
digest surfaces these later (see :mod:`alfred.digest`).
"""
