"""Unit tests for `agent.chat`. Requirements 3 AC7, 4 AC1, AC2, AC4, AC5.

Four things this module promises, and the four groups of tests below:

- **The mode is inferred, never declared.** Absent, empty, and populated
  `messages` arrays, plus the two shapes that look populated but hold no
  question -- an assistant-only transcript and a transcript long enough that
  the window drops everything. Getting this wrong sends an orientation prompt
  at a chat context or the reverse, and the client has no field it can set to
  correct it.
- **The history that goes on the wire is a contiguous suffix of at most ten
  turns starting on a user message** (Requirement 3 AC7). Asserted against a
  twenty-five turn transcript, as a suffix of the input rather than as a
  recorded list, so the guarantee is checked rather than the implementation
  restated.
- **The assembled request fits the budget** (Requirement 3 AC6). A context of
  twelve full-size file bodies and a history that would cost eight thousand
  tokens on its own, which together come to roughly twice the effective cap
  before anything is trimmed. `test_an_unbounded_pair_really_would_overrun`
  proves the case is not vacuous, which is the test that makes the budget test
  worth having.
- **Both modes end in a capped message** (Requirement 4 AC4). The orientation
  answer through `orientation.finalize`, the chat answer through
  `postprocess.cap_words`.

`FakeBedrock` answers every model call and the autouse fixture makes a real
client impossible to build, so nothing here opens a socket or needs AWS
credentials.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent import bedrock, chat
from agent.chat import (
    Mode,
    assemble,
    conversation,
    history_tokens,
    infer_mode,
    run_turn,
)
from agent.config import (
    MAX_HISTORY_TURNS,
    MAX_RESPONSE_WORDS,
    ORIENTATION_DEADLINE_MS,
    RESERVE_HISTORY,
    SOFT_DEADLINE_MS,
    TOKEN_BUDGET,
    TOKEN_BUDGET_EFFECTIVE,
)
from agent.context_builder import build_chat_context, build_orientation_context
from agent.errors import AgentError, ErrorCode
from agent.models import Deadline, RepoContext
from agent.orientation import QUESTION_LINE, detect_language
from agent.prompts import ORIENTATION_PROMPT, SYSTEM_PROMPT
from agent.selection import resolve_package_main, select
from agent.tokens import Turn, estimate_history, estimate_tokens
from conftest import FakeBedrock, FakeClock, RecordingSleep, load_repo_fixture

# A file body longer than the chat tier's 8,000-character cap, so the context
# is as large as the pipeline can make it.
FILLER = "# synthesized source\n" + "one line of plausible source code\n" * 600

# One turn of history at a realistic size: a long paragraph of question or
# answer. Ten of these cost well over RESERVE_HISTORY, so the window has to trim
# past the ten-turn cap as well as down to it.
TURN_TEXT = "Where does the request handling actually begin in this code base? " * 30


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> Any:
    """No sockets, and no AWS environment to accidentally depend on."""
    for name in ("MODEL_ID", "TARGET_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(name, raising=False)

    def forbidden(*args: object, **kwargs: object) -> Any:
        raise AssertionError(f"a test reached boto3.client{args!r} -- inject a fake")

    monkeypatch.setattr(bedrock.boto3, "client", forbidden)
    bedrock.reset_client()
    yield
    bedrock.reset_client()


def budget(ms: int = SOFT_DEADLINE_MS) -> Deadline:
    clock = FakeClock()
    return Deadline(started=clock.now, budget_ms=ms, clock=clock)


def turns(count: int, text: str = "turn") -> list[Turn]:
    """An alternating transcript, oldest first, as the request body carries it."""
    return [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"{text} {index}",
        }
        for index in range(count)
    ]


def context_for(name: str, *, lean: bool = False, history: int = 0) -> RepoContext:
    """A Repo_Context for a recorded repository, built offline.

    Goes through the real `select` and the real context builder, so the budget
    assertions are made against a context the pipeline would actually produce
    rather than a hand-sized one.
    """
    fixture = load_repo_fixture(name)
    first_pass = select(fixture.entries)
    bodies = {
        candidate.path: fixture.contents.get(
            candidate.path, f"# {candidate.path}\n{FILLER}".encode()
        )
        for candidate in first_pass
    }
    package_json = bodies.get("package.json")
    candidates = select(
        fixture.entries,
        resolve_package_main(package_json.decode() if package_json else None),
    )
    language = detect_language(fixture.meta, bodies)
    if lean:
        return build_orientation_context(
            fixture.meta,
            fixture.entries,
            candidates,
            bodies,
            detected_language=language,
        )
    return build_chat_context(
        fixture.meta,
        fixture.entries,
        candidates,
        bodies,
        detected_language=language,
        history_tokens=history,
    )


@pytest.fixture(scope="module")
def chat_context() -> RepoContext:
    return context_for("flask")


@pytest.fixture(scope="module")
def orientation_context() -> RepoContext:
    return context_for("flask", lean=True)


def words(text: str) -> int:
    return len(text.split())


def sent_text(request: chat.TurnRequest) -> list[str]:
    return [message["content"][0]["text"] for message in request.messages]


# ---------------------------------------------------------------------------
# Mode inference (Requirement 5 AC1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("messages", "expected"),
    [
        pytest.param(None, Mode.ORIENTATION, id="absent"),
        pytest.param([], Mode.ORIENTATION, id="empty"),
        pytest.param(turns(1), Mode.CHAT, id="one_user_turn"),
        pytest.param(turns(3), Mode.CHAT, id="populated"),
        pytest.param(
            [{"role": "assistant", "content": "an orphaned answer"}],
            Mode.ORIENTATION,
            id="assistant_only",
        ),
    ],
)
def test_the_mode_comes_from_the_request_shape(
    messages: list[Turn] | None, expected: Mode
) -> None:
    """Requirement 5 AC1, via the design's Wire Format.

    An empty or absent `messages` array means "this is a new session, write the
    Orientation_Message". Inferring it beats a `mode` field the client could set
    inconsistently with the history it sent -- and an assistant-only transcript
    has no question in it to answer, so it reads as a new session too.
    """
    assert infer_mode(messages) is expected


def test_the_mode_and_the_window_cannot_disagree() -> None:
    """Both read the same window, so one cannot say chat while the other is empty.

    If they could diverge, a chat-mode request would reach Bedrock with an empty
    message list, which Converse rejects outright.
    """
    for count in range(0, 25):
        messages = turns(count)
        assert bool(conversation(messages)) == (infer_mode(messages) is Mode.CHAT)


def test_an_orientation_request_sends_the_orientation_prompt(
    orientation_context: RepoContext,
) -> None:
    """The synthesized user turn, and nothing else. Requirement 5 AC1."""
    request = assemble(orientation_context, [])

    assert request.mode is Mode.ORIENTATION
    assert sent_text(request) == [ORIENTATION_PROMPT]
    assert [message["role"] for message in request.messages] == ["user"]
    assert request.history == ()


# ---------------------------------------------------------------------------
# The history window (Requirement 3 AC7)
# ---------------------------------------------------------------------------


def test_the_sent_history_is_a_contiguous_suffix_of_at_most_ten_turns(
    chat_context: RepoContext,
) -> None:
    """Requirement 3 AC7, asserted as a property of the input.

    Three things at once: at most ten turns, contiguous and in order, and
    opening on a user message -- the last because Converse rejects a message
    list that starts mid-answer.
    """
    history = turns(25)

    request = assemble(chat_context, history)

    assert len(request.history) <= MAX_HISTORY_TURNS
    assert list(request.history) == history[len(history) - len(request.history) :]
    assert request.history[0]["role"] == "user"
    # And the envelope is that window, in order, nothing added or reshaped.
    assert sent_text(request) == [turn["content"] for turn in request.history]
    assert [message["role"] for message in request.messages] == [
        turn["role"] for turn in request.history
    ]


def test_the_newest_turn_is_always_sent(chat_context: RepoContext) -> None:
    """Trimming is from the oldest end: the newest turn is the question."""
    history = turns(25)

    request = assemble(chat_context, history)

    assert sent_text(request)[-1] == history[-1]["content"]


def test_a_long_history_is_trimmed_to_its_reserve(chat_context: RepoContext) -> None:
    """Ten paragraph-sized turns cost more than the history reserve.

    The ten-turn cap is not the only bound -- a transcript of long turns is
    trimmed further, from the oldest end, until it fits `RESERVE_HISTORY`.
    """
    history = turns(10, TURN_TEXT)
    assert estimate_history(history) > RESERVE_HISTORY, "otherwise nothing is trimmed"

    window = conversation(history)

    assert 0 < len(window) < MAX_HISTORY_TURNS
    assert estimate_history(window) <= RESERVE_HISTORY
    assert window == history[len(history) - len(window) :]
    assert window[0]["role"] == "user"


def test_windowing_is_idempotent(chat_context: RepoContext) -> None:
    """The handler windows once to price the context; `assemble` windows again.

    Both have to produce the same turns, or the context's file budget would have
    been computed against a history that is not the one sent.
    """
    history = turns(25, TURN_TEXT)

    once = conversation(history)

    assert conversation(once) == once
    assert history_tokens(once) == history_tokens(history)
    assert sent_text(assemble(chat_context, once)) == sent_text(
        assemble(chat_context, history)
    )


def test_a_malformed_turn_costs_an_answer_not_a_crash(
    chat_context: RepoContext,
) -> None:
    """Request validation belongs to the handler; this is the last line.

    A turn with a missing role or non-string content should produce a worse
    answer, never a 500 -- the user pasted a URL and asked a question, and a
    shape surprise in the transcript is not their problem.
    """
    history: list[Any] = [
        {"role": "user", "content": "where do I start?"},
        {"role": "ASSISTANT", "content": None},
        {"content": "no role at all"},
        {"role": "user", "content": 42},
    ]

    request = assemble(chat_context, history)

    assert [message["role"] for message in request.messages] == [
        "user",
        "assistant",
        "user",
        "user",
    ]
    assert sent_text(request) == ["where do I start?", "", "no role at all", "42"]


# ---------------------------------------------------------------------------
# The token budget (Requirement 3 AC6)
# ---------------------------------------------------------------------------


def test_an_unbounded_pair_really_would_overrun() -> None:
    """The budget test below is only worth having if the inputs are too big.

    Twelve file bodies at the full per-file cap, plus a twenty-five turn
    transcript of paragraph-sized turns, priced with nothing trimmed. If this
    ever stops exceeding the cap, the test underneath has become vacuous and
    the fixtures need to grow.
    """
    fixture = load_repo_fixture("flask")
    candidates = select(fixture.entries)
    naive_files = sum(
        estimate_tokens(f"# {candidate.path}\n{FILLER}"[:8_000])
        for candidate in candidates
    )
    naive_history = estimate_history(turns(25, TURN_TEXT))

    assert naive_files > TOKEN_BUDGET_EFFECTIVE, "the file bodies alone overrun"
    assert naive_history > RESERVE_HISTORY * 2, "and so does the transcript"
    assert naive_files + naive_history > TOKEN_BUDGET_EFFECTIVE * 1.5


def test_a_history_and_context_pair_that_would_overrun_still_fits() -> None:
    """Requirement 3 AC6, end to end over the two independent claims.

    The context is fitted against what the history leaves, the history is
    trimmed to its reserve, and the assembled request is priced the way it goes
    on the wire: one content block for the system field and Repo_Context
    together, one per turn. That total has to sit under the 27,000-token working
    cap, which itself sits under the 30,000 the requirement names.
    """
    history = turns(25, TURN_TEXT)
    context = context_for("flask", history=history_tokens(history))

    request = assemble(context, history)

    assert request.within_budget
    assert request.estimated_tokens <= TOKEN_BUDGET_EFFECTIVE
    assert request.estimated_tokens <= TOKEN_BUDGET
    # Not vacuously small either: the budget is being used, not avoided.
    assert request.estimated_tokens > TOKEN_BUDGET_EFFECTIVE // 2


def test_an_orientation_request_is_far_cheaper_than_a_chat_turn(
    orientation_context: RepoContext, chat_context: RepoContext
) -> None:
    """The lean tier is what buys Requirement 5 AC6's 20 seconds.

    Four bodies at 3,000 characters against twelve at 8,000, both carrying the
    full manifest. If these two ever converge, the orientation has lost the
    headroom the latency budget depends on.
    """
    lean = assemble(orientation_context, [])
    full = assemble(chat_context, turns(4))

    assert lean.estimated_tokens < full.estimated_tokens
    assert lean.estimated_tokens < TOKEN_BUDGET_EFFECTIVE // 2


def test_the_reported_estimate_prices_what_is_actually_sent(
    chat_context: RepoContext,
) -> None:
    """The calibration sample is only meaningful if it measures the real payload.

    `tokens.log_calibration` compares this figure against `usage.inputTokens`,
    so an estimate over something other than the assembled request would
    calibrate the estimator against the wrong thing.
    """
    history = turns(6)

    request = assemble(chat_context, history)

    expected = estimate_tokens(request.system) + sum(
        estimate_tokens(text) for text in sent_text(request)
    )
    assert request.estimated_tokens == expected


# ---------------------------------------------------------------------------
# Assembly: where the prompt and the context go
# ---------------------------------------------------------------------------


def test_the_system_field_carries_the_rules_and_the_context(
    chat_context: RepoContext,
) -> None:
    """Requirement 4 AC1, AC5.

    Repo_Context rides in the system field rather than as a leading user turn:
    `history_window` already guarantees the message list opens on a user turn,
    and a synthesized context turn in front of it would put two user messages
    back to back, which Converse rejects. The system prompt's "REPOSITORY
    CONTEXT below" is then literally true.
    """
    request = assemble(chat_context, turns(3))

    assert request.system.startswith(SYSTEM_PROMPT)
    assert "=== REPOSITORY ===" in request.system
    assert "=== INGESTED FILES" in request.system
    # Requirement 4 AC5 -- the ingested manifest reaches the model, so an answer
    # about a file outside the selection can name what was inside it.
    for block in chat_context.files:
        assert block.path in request.system


def test_the_message_list_alternates_and_opens_on_a_user_turn(
    chat_context: RepoContext,
) -> None:
    """What Converse requires of a message list, for both modes."""
    for messages in ([], turns(1), turns(9), turns(25)):
        request = assemble(chat_context, messages)
        roles = [message["role"] for message in request.messages]

        assert roles[0] == "user"
        assert all(
            earlier != later for earlier, later in zip(roles, roles[1:], strict=False)
        )


# ---------------------------------------------------------------------------
# The turn (Requirements 4 AC4, 5 AC1)
# ---------------------------------------------------------------------------


def test_a_chat_answer_is_capped(chat_context: RepoContext) -> None:
    """Requirement 4 AC4. The model is asked for 350 words; this enforces it."""
    answer = " ".join(f"word{index}" for index in range(900))
    fake = FakeBedrock(answer, input_tokens=24_000, output_tokens=600)

    result = run_turn(chat_context, turns(3), budget(), bedrock=fake)

    assert result.mode is Mode.CHAT
    assert words(result.message) == MAX_RESPONSE_WORDS
    assert answer.startswith(result.message)
    assert result.repairs == ()
    assert (result.input_tokens, result.output_tokens) == (24_000, 600)
    assert result.attempts == 1
    assert result.history_turns == 3


def test_a_short_chat_answer_is_passed_through(chat_context: RepoContext) -> None:
    """Under the cap means untouched: no reflow, no reformatting."""
    answer = (
        "Routing starts in `src/flask/app.py`, which builds the WSGI "
        "application.\n\n- `src/flask/__init__.py` re-exports the public names."
    )
    fake = FakeBedrock(answer)

    result = run_turn(chat_context, turns(1), budget(), bedrock=fake)

    assert result.message == answer


def test_an_orientation_answer_goes_through_the_repair_step(
    orientation_context: RepoContext,
) -> None:
    """Requirement 5 AC1 to AC5 ride on this call being the one that is made.

    An empty answer from the model is the case that proves the orientation path
    is wired to `orientation.finalize` rather than to the plain word cap: the
    cap would hand back an empty first message, and the whole point of
    Requirement 5 is that the first message is never empty.
    """
    fake = FakeBedrock("")

    result = run_turn(
        orientation_context, None, budget(ORIENTATION_DEADLINE_MS), bedrock=fake
    )

    assert result.mode is Mode.ORIENTATION
    assert result.repairs != ()
    assert orientation_context.meta.slug in result.message
    assert len(QUESTION_LINE.findall(result.message)) >= 2
    assert words(result.message) <= MAX_RESPONSE_WORDS
    assert fake.calls[0]["messages"][0]["content"][0]["text"] == ORIENTATION_PROMPT


def test_a_good_orientation_answer_collects_no_repairs(
    orientation_context: RepoContext,
) -> None:
    """The repair list is a metric, so it has to be zero when nothing was wrong."""
    paths = [block.path for block in orientation_context.files][:3]
    answer = (
        "This is pallets/flask, a Python project. It gives you a small WSGI "
        f"framework that starts simple and scales up.\n\nRead `{paths[0]}`, "
        f"then `{paths[1]}`, then `{paths[2]}`.\n\n"
        "- What do I need installed to run this locally?\n"
        "- Where does a request enter the code?\n"
    )
    fake = FakeBedrock(answer)

    result = run_turn(orientation_context, [], budget(), bedrock=fake)

    assert result.repairs == ()
    assert result.message == answer.strip()


def test_the_request_reaches_bedrock_as_assembled(chat_context: RepoContext) -> None:
    """`assemble` is what is sent, not something rebuilt on the way out."""
    history = turns(25)
    request = assemble(chat_context, history)
    fake = FakeBedrock("fine")

    run_turn(chat_context, history, budget(), bedrock=fake)

    assert fake.calls[0]["system"] == [{"text": request.system}]
    assert fake.calls[0]["messages"] == list(request.messages)


def test_a_model_failure_is_not_caught_here(chat_context: RepoContext) -> None:
    """The handler owns the one error boundary in the codebase.

    `bedrock.converse` already maps a throttle to `MODEL_BUSY`; swallowing it
    here would mean two places decide what the user is told.
    """
    from botocore.exceptions import ClientError

    throttled = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "scripted"}}, "Converse"
    )
    clock = FakeClock()
    sleep = RecordingSleep(clock)
    fake = FakeBedrock(throttled)

    with pytest.raises(AgentError) as caught:
        run_turn(
            chat_context,
            turns(1),
            Deadline(started=clock.now, budget_ms=SOFT_DEADLINE_MS, clock=clock),
            bedrock=fake,
            sleep=sleep,
            jitter=lambda _low, high: 0.0,
        )

    assert caught.value.code is ErrorCode.MODEL_BUSY
    assert fake.attempts == 3


def test_a_spent_deadline_never_reaches_the_model(chat_context: RepoContext) -> None:
    """Requirements 4 AC6, 5 AC6. No call is placed that cannot answer in time."""
    fake = FakeBedrock("too late")

    with pytest.raises(AgentError) as caught:
        run_turn(chat_context, turns(1), budget(0), bedrock=fake)

    assert caught.value.code is ErrorCode.DEADLINE
    assert fake.attempts == 0
