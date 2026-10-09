"""Token accounting: the estimate, the budget allocation, and the fitting.

Requirement 3 AC6 caps one Bedrock request at 30,000 input tokens and AC7 caps
the conversation window at the 10 most recent turns. This module is the only
place either number is enforced.

There is no offline tokenizer for Nova, so the count is a character-ratio
estimate and the cap is enforced with a margin: fitting runs against
`TOKEN_BUDGET_EFFECTIVE` (27,000), which leaves 10% of headroom for the
estimator to be wrong in the direction that matters. Twelve files at 8,000
characters is 96,000 characters -- roughly 27,400 estimated tokens before a
tree summary or a single turn of history -- so **fitting is mandatory, not a
safety net**: the naive maximum does not fit and never did.

Three things make the budget a guarantee rather than an aspiration:

- **`fit_to_budget` prices what it admits.** Every cost is measured on the block
  as it will actually be rendered, by the renderer that will render it, so a
  header label or the delimiter-collision escape cannot add tokens behind the
  estimator's back. The shortened-prefix search only ever returns a length it
  has priced, which is why `remaining` can never go negative and why the
  returned total is a bound rather than a projection (Property 7).
- **Shortening composes with `truncate`.** A shortened block's content is a
  prefix of the content that came in, and a prefix of a prefix is still a
  prefix, so `FileBlock.content` stays a prefix of the fetched file however
  many times it was cut (Property 6).
- **Every drop is reported.** A file the budget could not fit becomes an
  "omitted for space" note in Repo_Context, so the model knows the manifest it
  was handed is wider than the text it can read, and can say so instead of
  inventing the contents of a file it never saw.

`history_window` only ever drops from the front, so its result is always a
contiguous suffix of the supplied history, in the original order (Property 8).

Pure apart from `log_calibration` and the `within_budget` tripwire, which write
log records. No network, no disk, no clock, no module state.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace

from .config import (
    BLOCK_OVERHEAD_TOKENS,
    CHARS_PER_TOKEN,
    MAX_HISTORY_TURNS,
    MIN_USEFUL_CHARS,
    RESERVE_HISTORY,
    RESERVE_SYSTEM,
    TOKEN_BUDGET,
    TOKEN_BUDGET_EFFECTIVE,
)
from .models import FileBlock

__all__ = [
    "OMITTED_NOTE_PREFIX",
    "Turn",
    "available_for_files",
    "estimate_history",
    "estimate_tokens",
    "fit_to_budget",
    "history_window",
    "log_calibration",
    "turn_tokens",
    "within_budget",
]

log = logging.getLogger(__name__)

# One conversation turn on the wire: {"role": "user"|"assistant", "content": str}.
# Dicts rather than a dataclass because this is exactly the request body shape
# (see the design's Wire Format); `chat.py` converts to the Converse envelope.
Turn = Mapping[str, str]

# Prefix of the Repo_Context note that owns up to a dropped file. Rendered into
# the context so the omission is visible to the model, not just to the logs.
OMITTED_NOTE_PREFIX = "omitted for space"


# ---------------------------------------------------------------------------
# Estimation
# ---------------------------------------------------------------------------


def estimate_tokens(text: str, *, overhead: int = BLOCK_OVERHEAD_TOKENS) -> int:
    """Deliberately pessimistic character-ratio estimate. Requirement 3 AC6.

    English prose runs about 4 characters per token. Source code tokenizes
    denser -- punctuation, operators, snake_case and camelCase splits, deep
    indentation -- closer to 3.0-3.5. `CHARS_PER_TOKEN` is 3.5 across the
    board, which over-counts prose and roughly matches code, so the estimate
    errs high on exactly the content that dominates Repo_Context.

    `overhead` is the per-block message-framing charge, added once per call. It
    defaults to `BLOCK_OVERHEAD_TOKENS` because the normal caller is pricing
    one content block or one turn. Pass `overhead=0` when measuring a string
    whose blocks were already charged individually -- the assembled context,
    for instance -- so the framing is not paid for twice.
    """
    return math.ceil(len(text) / CHARS_PER_TOKEN) + overhead


def turn_tokens(turn: Turn) -> int:
    """Estimated cost of one conversation turn, framing included."""
    return estimate_tokens(_turn_text(turn))


def estimate_history(turns: Iterable[Turn]) -> int:
    """Estimated cost of a conversation window."""
    return sum(turn_tokens(turn) for turn in turns)


def _turn_text(turn: Turn) -> str:
    """The text of a turn, coerced rather than validated.

    Request validation belongs to `handler.parse_request`. This module's job is
    a bound, and a bound that raises on a malformed turn would turn a bad
    request into a 500. Anything non-textual is stringified, which can only
    make the estimate larger.
    """
    content = turn.get("content", "")
    if isinstance(content, str):
        return content
    return "" if content is None else str(content)


def _is_user(turn: Turn) -> bool:
    """True for a user-role turn. Lenient on input, strict on output."""
    role = turn.get("role", "")
    return isinstance(role, str) and role.strip().lower() == "user"


# ---------------------------------------------------------------------------
# Budget allocation
# ---------------------------------------------------------------------------


def available_for_files(history_tokens: int = 0, tree_tokens: int = 0) -> int:
    """Tokens left for file blocks once the earlier claims are paid.

    The claims, in order: the system prompt takes a flat `RESERVE_SYSTEM` (it is
    static text, and task 5.1 asserts it stays inside the reserve), then the
    conversation history and the tree summary take what they *measure* rather
    than what they reserve, so an orientation request with no history spends the
    4,000 history tokens on file bodies instead of leaving them idle.

    With history inside `RESERVE_HISTORY` and the tree inside `BUDGET_TREE` this
    returns at least 20,700. Clamped at zero: a caller whose earlier claims have
    already eaten the budget gets "no room", not a negative allowance that would
    make the fitting arithmetic meaningless.
    """
    claimed = RESERVE_SYSTEM + max(0, history_tokens) + max(0, tree_tokens)
    return max(0, TOKEN_BUDGET_EFFECTIVE - claimed)


def within_budget(estimated: int, *, budget: int = TOKEN_BUDGET_EFFECTIVE) -> bool:
    """Final tripwire over the assembled request. Requirement 3 AC6.

    `fit_to_budget` prices every block it admits, so an overrun here means one
    of the *unfitted* claims was mis-measured -- a system prompt that grew past
    its reserve, a tree summary that was never trimmed. That is a bug in the
    caller rather than an expected condition, so it is logged at warning level
    and reported, not raised: a request that is 50 tokens over the 27,000
    working cap is still 2,950 under the 30,000 contractual one, and failing
    the user's request to protect a margin would be the wrong trade.
    """
    if estimated > budget:
        log.warning(
            json.dumps(
                {
                    "event": "token_budget_exceeded",
                    "estimated": estimated,
                    "budget": budget,
                    "contractual": TOKEN_BUDGET,
                }
            )
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Fitting file blocks (Requirement 3 AC6)
# ---------------------------------------------------------------------------


def fit_to_budget(
    blocks: Sequence[FileBlock],
    available: int,
    render: Callable[[FileBlock], str] = FileBlock.render,
) -> tuple[list[FileBlock], list[str]]:
    """Admit blocks in priority order until the budget runs out.

    Returns `(kept, notes)`. Invariants, asserted by Property 7:

    - the kept blocks' total estimated cost never exceeds `available`
    - output order equals input order; fitting never reorders
    - every kept block's content is a prefix of the content it arrived with
    - no text is invented, rewritten, or re-encoded

    `blocks` is expected in the Requirement 3 AC1 priority order that
    `selection.select` produces, which is what makes the budget's effect
    predictable: a README is never dropped so that a tier-5 source file can
    fit. The loop runs to the end rather than stopping at the first block that
    does not fit, so every omitted path reaches the note instead of just the
    first one.

    A block that does not fit whole is offered the longest prefix the remaining
    budget can pay for, and taken only if that prefix is still worth a slot
    (`MIN_USEFUL_CHARS`). Four hundred characters of a file is a useful look at
    its imports and its first function; forty is a tax with no information in
    it.

    `render` is injected rather than imported so this module stays below
    `context_builder` in the dependency graph. It defaults to the block's own
    `render`, and the cost of a block is always measured through it -- header,
    truncation label, delimiters, and the `=== ` collision escape included --
    so no rendering rule can spend tokens the estimator did not see.

    Raises `ValueError` on a negative `available`, which would otherwise leave
    the "total never exceeds `available`" invariant unsatisfiable: an empty
    result costs zero tokens, and zero exceeds a negative allowance.
    """
    if available < 0:
        raise ValueError(f"available must be non-negative, got {available}")

    kept: list[FileBlock] = []
    omitted: list[str] = []
    remaining = available

    for block in blocks:
        cost = estimate_tokens(render(block))
        if cost <= remaining:
            kept.append(block)
            remaining -= cost
            continue

        chars = _affordable_prefix(block, render, remaining)
        if chars >= MIN_USEFUL_CHARS:
            shortened = _shorten(block, chars)
            kept.append(shortened)
            remaining -= estimate_tokens(render(shortened))
        else:
            omitted.append(block.path)

    notes = [f"{OMITTED_NOTE_PREFIX}: {', '.join(omitted)}"] if omitted else []
    return kept, notes


def _affordable_prefix(
    block: FileBlock, render: Callable[[FileBlock], str], budget: int
) -> int:
    """The longest content prefix whose rendered cost fits in `budget`, or -1.

    A binary search over prefix lengths, priced with the real renderer, rather
    than the `remaining * CHARS_PER_TOKEN` arithmetic the design sketched. The
    arithmetic has to guess the block's framing overhead, and it guesses before
    the truncation label exists -- `[truncated: first 7,948 of 97,412 chars]`
    is a couple of dozen characters that only appear once the cut is made, and
    the collision escape can add a character per line. A search that prices
    candidates cannot be wrong about either.

    Rendered cost is non-decreasing in the prefix length, which is what makes
    the search find the *longest* fitting prefix. Correctness of the *bound*
    does not depend on that: `best` is only ever assigned a length that was
    priced and fit, so a renderer that broke monotonicity would cost precision,
    never the budget. Returns -1 when not even an empty body fits, which the
    caller reads as "drop it".
    """
    low, high = 0, len(block.content)
    best = -1
    while low <= high:
        mid = (low + high) // 2
        if estimate_tokens(render(_shorten(block, mid))) <= budget:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    return best


def _shorten(block: FileBlock, chars: int) -> FileBlock:
    """A copy of `block` whose content is its first `chars` characters.

    `original_chars` is left alone, so the header keeps naming the size of the
    real file rather than the size of the last cut, and the truncation label
    stays honest about how much the model cannot see. The flag is recomputed
    against the larger of the fetched length and the current content so it
    reads as "there is more text in this file than you can see here" even if a
    caller hands us a block whose two counts disagree.
    """
    if chars >= len(block.content):
        return block
    return replace(
        block,
        content=block.content[:chars],
        truncated=chars < max(block.original_chars, len(block.content)),
    )


# ---------------------------------------------------------------------------
# The conversation window (Requirement 3 AC7)
# ---------------------------------------------------------------------------


def history_window(
    messages: Sequence[Turn],
    reserve: int = RESERVE_HISTORY,
    max_turns: int = MAX_HISTORY_TURNS,
) -> list[Turn]:
    """The turns that go into the request. Requirement 3 AC7, Property 8.

    Three cuts, all from the oldest end:

    1. keep the `max_turns` most recent turns (Requirement 3 AC7),
    2. drop the oldest while the window costs more than `reserve`,
    3. drop the oldest while it is not a user-role turn.

    Dropping only ever from the front is what makes the result a contiguous
    suffix of `messages` in the original order, and it is also the right
    behavior: the newest turns are the ones the current question refers to.

    The user-role alignment is last on purpose. Converse rejects a message list
    that opens on an assistant turn, so aligning first and then trimming for
    the reserve could hand back a window that starts mid-answer. Aligning after
    the reserve trim can only drop more, never add, so both guarantees hold at
    once. A window with no user turn left comes back empty, which the caller
    reads as "no usable history" -- an orientation-shaped request.

    What this deliberately does not do is merge consecutive same-role turns or
    drop a trailing assistant turn. The last turn is the question being asked,
    and reshaping the tail would break the contiguous-suffix guarantee that
    Property 8 rests on. Converse envelope concerns belong to `chat.py`.

    Raises `ValueError` on a negative `reserve` or `max_turns`: both would
    silently invert the slicing and return something that is neither bounded
    nor a suffix.
    """
    if reserve < 0:
        raise ValueError(f"reserve must be non-negative, got {reserve}")
    if max_turns < 0:
        raise ValueError(f"max_turns must be non-negative, got {max_turns}")
    if max_turns == 0:
        return []

    # Requirement 3 AC7. Sliced explicitly rather than with `[-max_turns:]`,
    # which silently means "everything" when max_turns is 0.
    start = max(0, len(messages) - max_turns)
    window = list(messages[start:])

    cost = estimate_history(window)
    while window and cost > reserve:
        cost -= turn_tokens(window[0])
        del window[0]

    while window and not _is_user(window[0]):
        del window[0]

    return window


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


def log_calibration(
    estimated: int, actual: int, output_tokens: int = 0
) -> dict[str, int | float]:
    """Compare the estimate against the count Converse actually charged.

    `usage.inputTokens` is the only ground truth available -- there is no
    offline Nova tokenizer -- so every invocation is a free calibration sample.
    Returns `{"estimated", "actual", "output", "ratio"}` for the handler's
    single structured log line, which is where the ratio lives per request; a
    second info line per request would just double the log volume for the same
    numbers.

    `ratio` is actual over estimated, so below 1.0 means the estimator
    over-counted (the intended direction) and above 1.0 means it under-counted
    and the 10% margin is what kept the request inside the 30,000-token cap.
    That case is the actionable one, so it gets its own warning: a handful of
    those in the logs is the signal to set `CHARS_PER_TOKEN` from data instead
    of from reasoning.
    """
    ratio = round(actual / estimated, 3) if estimated > 0 else 0.0
    record: dict[str, int | float] = {
        "estimated": estimated,
        "actual": actual,
        "output": output_tokens,
        "ratio": ratio,
    }
    if actual > estimated:
        log.warning(json.dumps({"event": "token_estimate_under_counted", **record}))
    else:
        log.debug(json.dumps({"event": "token_estimate", **record}))
    return record
