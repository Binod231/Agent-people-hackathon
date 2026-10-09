"""The word cap on an assistant message, and the fence it never leaves open.

Requirement 4 AC4 caps assistant messages at 350 words. `MAX_OUTPUT_TOKENS`
makes an overrun rare but not impossible -- a token budget is not a word budget,
and a model asked for "under 350 words" is being asked, not constrained. So the
cap is enforced here, deterministically, after the fact.

Three guarantees `orientation.finalize` and `chat` lean on (Property 10):

- **At most `limit` words out**, counting a word the way `str.split()` does, and
  counting the closing fence too. The fence is paid for out of the budget rather
  than added on top of it, so "350 words" stays true of what the user receives.
- **The result is a prefix of the input**, up to that one appended fence. The
  message is only ever shortened from the right, never re-wrapped or re-flowed,
  so the backtick-quoted paths Requirement 4 AC2 asks the model to cite survive
  intact and the newlines the Chat_UI splits blocks on stay where the model put
  them.
- **A cut never leaves an unterminated ```` ``` ````.** The Chat_UI renders
  fenced spans as code, so a cut landing inside a fenced block would set the
  remainder of the message in a runaway `<pre>`.

The prefix guarantee is why this does not go the obvious route of
`" ".join(text.split()[:limit])`. Splitting and rejoining collapses every
newline into a single space, which for *any* message over the cap would flatten
the paragraph breaks, the list items, and the code fences into one wall of
prose -- a formatting rewrite as the side effect of trimming a few words. The
word offsets are found in the original text instead, and the cut is a slice.

Under the cap the text is returned unchanged, byte for byte. Fence repair is
scoped to the cut because the cut is the only imbalance this module creates; an
imbalance the model wrote into a message short enough to keep is the model's
text, and rewriting it is not this function's job.

Pure: no network, no disk, no clock, no module state.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from itertools import islice

from .config import CAP_SENTENCE_WINDOW, MAX_RESPONSE_WORDS

__all__ = ["cap_words"]

FENCE = "```"

# A word is a run of non-whitespace, which is exactly what `str.split()` counts.
# Matched with offsets so the cut can be taken out of the original text.
_WORD = re.compile(r"\S+")

# A sentence end: terminal punctuation, any closing quote or bracket riding
# along with it, and then whitespace or the end of the text. The trailing
# context is what keeps the cut off the decimal point in `3.14`, the dot in
# `app.py`, and the middle of `e.g.` -- a cut there would read as a typo rather
# than as the end of a thought.
_SENTENCE_END = re.compile(r"[.!?][\"')\]]*(?=\s|$)")


def cap_words(text: str, limit: int = MAX_RESPONSE_WORDS) -> str:
    """Trim `text` to at most `limit` words, preferring a sentence boundary.

    Returns `text` unchanged when it already fits. Otherwise returns a prefix
    holding at most `limit` words -- cut at the last sentence end in the final
    `CAP_SENTENCE_WINDOW` of the trimmed text where there is one, so the message
    ends on a finished thought instead of mid-sentence -- with a closing ```` ```
    ```` appended if the cut would otherwise leave a fenced block open.
    Requirement 4 AC4.

    Raises `ValueError` on a negative `limit`, which would otherwise slice from
    the end and return something that is neither bounded by the cap nor the
    prefix the caller was promised. A `limit` of 0 is legitimate and yields the
    empty string for any text holding a word.
    """
    if limit < 0:
        raise ValueError(f"limit must be non-negative, got {limit}")

    # One word past the cap is all it takes to know whether a cut is needed and
    # where it lands, so a long message costs a walk of the words that matter
    # plus one rather than of every word it has.
    spans = list(islice(_WORD.finditer(text), limit + 1))
    if len(spans) <= limit:
        return text

    kept = _prefix(text, spans, limit)
    if _balanced(kept):
        return kept
    # The closing fence is itself a word. Re-cut a word shorter to pay for it,
    # rather than appending it and breaching the cap by one. The shorter cut can
    # come out balanced on its own -- the word dropped may have been the opening
    # fence -- in which case there is nothing left to close. `limit` is at least
    # 1 here: a cut to zero words is the empty string, which is balanced.
    kept = _prefix(text, spans, limit - 1)
    return kept if _balanced(kept) else f"{kept}\n{FENCE}"


def _prefix(text: str, spans: Sequence[re.Match[str]], count: int) -> str:
    """The prefix of `text` holding its first `count` words.

    Sliced at the end of the `count`-th word, so the cut never lands inside one,
    and then pulled back to a sentence end where the tail offers one.
    """
    return _to_sentence_end(text[: spans[count - 1].end()] if count else "")


def _to_sentence_end(kept: str) -> str:
    """Pull `kept` back to its last sentence end, if one sits in the tail.

    The search is bounded to the final `CAP_SENTENCE_WINDOW` so the courtesy can
    never cost more than a quarter of the words: a message with no terminal
    punctuation until its opening line -- a list, a single long sentence -- keeps
    its word-boundary cut rather than collapsing to a few words.
    """
    window_start = int(len(kept) * (1.0 - CAP_SENTENCE_WINDOW))
    end = 0
    for match in _SENTENCE_END.finditer(kept, window_start):
        end = match.end()
    return kept[:end] if end > 0 else kept


def _balanced(text: str) -> bool:
    """Whether every fence in `text` is matched by a closing one."""
    return text.count(FENCE) % 2 == 0
