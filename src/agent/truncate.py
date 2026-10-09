"""The per-file character cap, and the flag that owns up to it.

Requirement 3 AC4 caps each selected file at `MAX_FILE_CHARS` characters and
AC5 requires a truncated file be labeled as truncated. This module is both
halves: `kept` is what satisfies AC4, and `was_truncated` is the fact the label
is built from -- it becomes `FileBlock.truncated`, which task 3.6 renders into
the block header as `[truncated: first N of M chars]`.

Three guarantees the rest of the pipeline leans on (Property 6):

- **`kept` is a prefix of `content`.** The text is only ever shortened from the
  right, never rewritten, re-wrapped, or re-encoded. That is what lets the model
  quote a path or a signature out of a file block and be right about it, and it
  composes: `tokens.fit_to_budget` shortens a block again later, and a prefix of
  a prefix is still a prefix.
- **`len(kept) <= limit`.** Unconditional, including when the line-boundary
  search runs -- that search only ever moves the cut earlier.
- **`was_truncated` is exactly `len(kept) < len(content)`.** The label is an
  `iff`, not a hint, so "truncated" never appears on a whole file and never
  fails to appear on a cut one.

Measured in **characters**, not bytes. That is the requirement's wording, and a
code-point cut cannot land mid-codepoint and spend tokens on a mangled glyph.

`limit` is a parameter rather than a constant read from inside, because the same
bodies are cut to different lengths at different points: the lean orientation
tier uses `ORIENTATION_FILE_CHARS` to make the 20-second budget (Requirement 5
AC6), while a chat turn gets the full `MAX_FILE_CHARS`.

Pure: no network, no disk, no clock, no module state.
"""

from __future__ import annotations

from .config import MAX_FILE_CHARS, TRUNCATE_NEWLINE_WINDOW

__all__ = ["truncate"]


def truncate(content: str, limit: int = MAX_FILE_CHARS) -> tuple[str, bool]:
    """Cut `content` to at most `limit` characters, preferring a line boundary.

    Returns `(kept, was_truncated)`. `kept` is always a prefix of `content` and
    never longer than `limit`; `was_truncated` is true exactly when `kept` is
    shorter than `content`. Requirement 3 AC4, AC5.

    Raises `ValueError` on a negative `limit`, which would otherwise slice from
    the end and quietly return something that is neither bounded by the cap nor
    the prefix the caller was promised. A `limit` of 0 is legitimate and yields
    `("", True)` for any non-empty content.
    """
    if limit < 0:
        raise ValueError(f"limit must be non-negative, got {limit}")
    if len(content) <= limit:
        return content, False

    cut = content[:limit]
    # Prefer the last newline in the final tenth of the cap, so the cut lands
    # between lines instead of mid-token: a half-written signature invites the
    # model to describe a function that does not exist. The search window is
    # bounded so the courtesy can never cost more than a tenth of the budgeted
    # characters -- a minified bundle whose only newline sits near the start
    # keeps its hard cut rather than collapsing to a few hundred characters.
    boundary = cut.rfind("\n", int(limit * (1.0 - TRUNCATE_NEWLINE_WINDOW)))
    if boundary > 0:
        cut = cut[:boundary]
    return cut, True
