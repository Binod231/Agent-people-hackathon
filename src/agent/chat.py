"""One turn of the conversation: the mode, the window, the call, and the cap.

This is the seam between the handler and the model. `handler.run_turn` does the
repository work -- parse the URL, fetch the metadata and the tree, select, read,
build the context -- and then hands the finished `RepoContext` here. Nothing in
this module touches the network or the filesystem: it assembles a request,
delegates the call to `bedrock.converse`, and post-processes the answer.

Both modes come through `run_turn`, which is why there is one module and not
two. The mode is not a field the client sets; it is inferred from the request
shape (`messages` empty or absent means "this is a new session, write the
Orientation_Message"), and the inference has to happen before the context is
built, because the two modes use different context tiers. Splitting the
inference from the call would mean two callers deciding the same thing
separately, and the one that got it wrong would send a chat context to an
orientation prompt.

Four things this module is responsible for:

- **The mode** (Requirement 5 AC1). Inferred, per the design's Wire Format
  section, rather than carried in the request, so a client cannot ask for an
  orientation while sending ten turns of history.
- **The history window** (Requirement 3 AC7). `tokens.history_window` does the
  work; the guarantee it provides -- a contiguous suffix of at most ten turns,
  starting on a user message -- is restated here because it is what keeps the
  Converse envelope valid as well as the budget bounded.
- **The token budget** (Requirement 3 AC6). The assembled request is priced the
  way it will actually be sent and checked against the effective cap, which is
  the last tripwire over arithmetic that was done piecemeal upstream.
- **The word cap** (Requirement 4 AC4). Every answer goes through
  `postprocess.cap_words`; the orientation answer goes through
  `orientation.finalize`, which caps it as part of guaranteeing Requirement 5's
  five required parts.

**Where the context goes, and why.** Repo_Context is appended to the system
prompt rather than sent as a leading user message. The Converse API requires the
message list to alternate roles and to open on a user turn, and
`history_window` already guarantees the window opens on one -- so a synthesized
context turn in front of it would put two user messages back to back and earn a
validation error. The system field has no such constraint, and the system prompt
already says "Ground every statement in the REPOSITORY CONTEXT below", which is
now literally true. The token accounting is unchanged: `available_for_files`
reserved `RESERVE_SYSTEM` for the prompt and `build_context` priced the context
separately, and folding them into one content block costs slightly *less* than
the sum of the two, so the estimate stays on the conservative side.

Pure apart from the Bedrock call it delegates: no network of its own, no disk,
no clock, no module state.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .bedrock import BedrockClient, converse
from .models import Deadline, RepoContext
from .orientation import finalize
from .postprocess import cap_words
from .prompts import ORIENTATION_PROMPT, SYSTEM_PROMPT
from .tokens import (
    Turn,
    estimate_history,
    estimate_tokens,
    history_window,
    within_budget,
)

__all__ = [
    "Mode",
    "TurnRequest",
    "TurnResult",
    "assemble",
    "conversation",
    "history_tokens",
    "infer_mode",
    "run_turn",
]


class Mode(str, Enum):
    """Which of the two turns this request is.

    `str`-valued so it serializes into the response envelope and the log line
    without a conversion at every use.
    """

    ORIENTATION = "orientation"
    CHAT = "chat"


@dataclass(frozen=True)
class TurnRequest:
    """Everything the Converse call needs, priced before it is sent.

    Separated from `run_turn` so the assembly guarantees -- the history window,
    the budget, which prompt went where -- are assertable without a model call.
    """

    mode: Mode
    system: str
    messages: tuple[Mapping[str, Any], ...]  # the Converse envelope
    history: tuple[Turn, ...]  # the window, in request-body shape
    estimated_tokens: int
    within_budget: bool


@dataclass(frozen=True)
class TurnResult:
    """One answered turn, ready for the response envelope.

    `repairs` is empty on a chat turn and names the Orientation_Message parts
    that had to be added on an orientation turn; the handler logs it as the
    repair-rate metric. `estimated_tokens` against `input_tokens` is the
    calibration sample `tokens.log_calibration` writes.
    """

    message: str
    mode: Mode
    repairs: tuple[str, ...]
    history_turns: int
    estimated_tokens: int
    input_tokens: int
    output_tokens: int
    attempts: int


# ---------------------------------------------------------------------------
# The mode and the window
# ---------------------------------------------------------------------------


def infer_mode(messages: Sequence[Turn] | None) -> Mode:
    """Orientation when there is no usable history, chat otherwise.

    Requirement 5 AC1 wants the Orientation_Message to be the first assistant
    message of a session, before any user question. The request shape says so
    on its own: a client that has nothing in its transcript sends no `messages`
    array, or an empty one.

    "No usable history" is deliberately the same test `conversation` applies, so
    the mode and the window cannot disagree. An array holding only an assistant
    turn, or only turns the window drops, has no question in it to answer, and
    an orientation is the right answer to "tell me about this repository".
    """
    return Mode.CHAT if conversation(messages) else Mode.ORIENTATION


def conversation(messages: Sequence[Turn] | None) -> list[Turn]:
    """The turns that go on the wire. Requirement 3 AC7.

    A contiguous suffix of `messages`, in the original order, at most
    `MAX_HISTORY_TURNS` long, costing at most `RESERVE_HISTORY` estimated
    tokens, and starting on a user-role turn -- which is what Converse requires
    of a message list and what `tokens.history_window` guarantees.

    Idempotent, because the window of a window is the same window. That matters:
    the handler calls `history_tokens` before building the chat context, and
    `assemble` windows again when it builds the envelope. Either may be handed
    the raw client history or an already-trimmed one and both produce the same
    result.
    """
    return history_window(list(messages or ()))


def history_tokens(messages: Sequence[Turn] | None) -> int:
    """Estimated cost of the window, for `build_chat_context`.

    The chat context's file budget is what is left after the history is paid
    for, so this has to be computed before the context is built -- and from the
    *window*, not the whole transcript, or the budget would reserve space for
    turns that are never sent.

    `tokens.estimate_history` prices a turn by its content text, which is the
    same text `_envelope_tokens` prices in the Converse envelope, so the figure
    the context budget was derived from and the figure `assemble` reports are
    the same number.
    """
    return estimate_history(conversation(messages))


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def assemble(context: RepoContext, messages: Sequence[Turn] | None) -> TurnRequest:
    """Build the Converse request for one turn. Requirements 3 AC6, 4 AC1, AC5.

    The system field carries the grounding rules and Repo_Context together. The
    message list is either the history window (chat) or a single synthesized
    user turn holding `ORIENTATION_PROMPT` (orientation) -- the orientation
    prompt is not something the user typed, and it is sent as a user turn
    because that is the only role Converse accepts first.
    """
    mode = infer_mode(messages)
    history = conversation(messages)
    system = f"{SYSTEM_PROMPT}\n{context.render()}"
    envelope = (
        _wire([{"role": "user", "content": ORIENTATION_PROMPT}])
        if mode is Mode.ORIENTATION
        else _wire(history)
    )

    # Priced as it goes on the wire: one content block for the system field,
    # one per message. `build_context` already fitted the file bodies against
    # this same cap, so an overrun here means an unfitted claim was mis-measured
    # upstream -- which `within_budget` logs rather than raises, since a request
    # a little over the 27,000 working cap is still well under the 30,000
    # contractual one and failing the user to protect a margin is the wrong
    # trade.
    estimated = estimate_tokens(system) + _envelope_tokens(envelope)
    return TurnRequest(
        mode=mode,
        system=system,
        messages=envelope,
        history=tuple(history),
        estimated_tokens=estimated,
        within_budget=within_budget(estimated),
    )


def _wire(turns: Sequence[Turn]) -> tuple[Mapping[str, Any], ...]:
    """Request-body turns as Converse content blocks.

    `{"role": ..., "content": "text"}` on the wire into the Lambda becomes
    `{"role": ..., "content": [{"text": "text"}]}` on the wire to Bedrock. The
    role is normalized and the content coerced rather than validated: request
    validation belongs to `handler.parse_request`, and a malformed turn that
    reached this far should cost a worse answer, not a 500.
    """
    return tuple(
        {"role": _role(turn), "content": [{"text": _content(turn)}]} for turn in turns
    )


def _role(turn: Turn) -> str:
    role = turn.get("role", "")
    normalized = role.strip().lower() if isinstance(role, str) else ""
    return "assistant" if normalized == "assistant" else "user"


def _content(turn: Turn) -> str:
    content = turn.get("content", "")
    if isinstance(content, str):
        return content
    return "" if content is None else str(content)


def _envelope_tokens(envelope: Sequence[Mapping[str, Any]]) -> int:
    return sum(estimate_tokens(_message_text(message)) for message in envelope)


def _message_text(message: Mapping[str, Any]) -> str:
    blocks = message.get("content") or []
    return "".join(
        str(block.get("text", "")) for block in blocks if isinstance(block, Mapping)
    )


# ---------------------------------------------------------------------------
# The turn
# ---------------------------------------------------------------------------


def run_turn(
    context: RepoContext,
    messages: Sequence[Turn] | None,
    deadline: Deadline,
    *,
    bedrock: BedrockClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[float, float], float] = random.uniform,
) -> TurnResult:
    """Answer one turn. Requirements 3 AC7, 4 AC1, AC2, AC4, AC5; 5 AC1.

    Args:
        context: The Repo_Context the handler built, in the tier matching the
            mode -- the lean tier for an orientation, the full one for a chat
            turn.
        messages: The conversation history from the request body. Empty or
            absent means orientation.
        deadline: The handler's remaining budget, passed through to
            `bedrock.converse`, which refuses an attempt it cannot finish.
        bedrock: Client override, for tests.
        sleep: Backoff sleep, for tests.
        jitter: Backoff jitter source, for tests.

    Returns:
        The finished message with the counts the handler logs.

    Raises:
        AgentError: whatever `bedrock.converse` raises -- `MODEL_BUSY`,
            `DEADLINE`, or `INTERNAL`. Nothing is caught here: the handler owns
            the one error boundary in the codebase.

    An orientation answer goes through `orientation.finalize`, which is where
    Requirement 5 AC2 to AC5 become guarantees rather than hopes. A chat answer
    only needs the word cap -- its content requirements (grounding, citation,
    owning up to a file outside the selection) are prompt-level, and a
    deterministic repair cannot manufacture them.
    """
    request = assemble(context, messages)
    result = converse(
        request.system,
        request.messages,
        deadline,
        bedrock=bedrock,
        sleep=sleep,
        jitter=jitter,
    )

    if request.mode is Mode.ORIENTATION:
        message, repairs = finalize(result.text, context)
    else:
        message, repairs = cap_words(result.text.strip()), []

    return TurnResult(
        message=message,
        mode=request.mode,
        repairs=tuple(repairs),
        history_turns=len(request.history),
        estimated_tokens=request.estimated_tokens,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        attempts=result.attempts,
    )
