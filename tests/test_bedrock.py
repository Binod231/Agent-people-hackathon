"""Unit tests for `agent.bedrock`. Requirement 6 AC2, AC3.

Requirement 6 AC2 is a count and a shape: at most two retries after the initial
attempt, with exponential backoff between them. Both are asserted from outside
the implementation -- the attempt count by how many times `FakeBedrock.converse`
was called, the backoff by what `RecordingSleep` was asked to wait for -- so a
future rewrite of the loop is held to the same bound.

What the tests cover, in the order they appear:

- the success path, including the `usage` counts that make every call a free
  calibration sample for `tokens.log_calibration`
- the request itself: `maxTokens: 600`, `temperature: 0.3`, the model id from
  the environment, and the prompt passed through untouched
- sustained throttling: exactly three attempts, two backoffs, `MODEL_BUSY`
- the backoff shape: strictly increasing for either jitter extreme
- the deadline guard: no attempt without room for an inference, and no retry
  whose backoff plus that inference window would overrun the budget
- the error mapping: throttle codes retried then `MODEL_BUSY`, everything else
  `INTERNAL` on the first failure, transport errors and malformed envelopes
  included
- that botocore's own retry layer is off, which is the one claim that needs a
  real client to settle

Nothing here opens a socket. The autouse fixture replaces `boto3.client` with a
tripwire, so a test that forgets to inject a double fails on an assertion
instead of reaching AWS; the two tests that need a real client object restore
the factory explicitly and still make no call.
"""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from botocore.exceptions import (
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from agent import bedrock
from agent.bedrock import (
    DEFAULT_MODEL_ID,
    THROTTLE_CODES,
    ConverseResult,
    backoff_delay,
    converse,
    model_id,
)
from agent.config import (
    BACKOFF_JITTER_SECONDS,
    BASE_BACKOFF_SECONDS,
    MAX_BEDROCK_RETRIES,
    MAX_OUTPUT_TOKENS,
    MIN_INFERENCE_SECONDS,
    MODEL_TEMPERATURE,
    MODEL_TOP_P,
    SOFT_DEADLINE_MS,
)
from agent.errors import AgentError, ErrorCode
from agent.models import Deadline
from agent.tokens import log_calibration
from conftest import FakeBedrock, FakeClock, RecordingSleep

# Captured before the tripwire goes up, for the two tests that need to build a
# real client object (no call is made on it).
REAL_BOTO3_CLIENT = boto3.client

SYSTEM = "You are a codebase welcome guide."
MESSAGES: list[dict[str, Any]] = [
    {"role": "user", "content": [{"text": "where do I start?"}]}
]

# Jitter at its extremes, so the backoff assertions hold for every draw rather
# than for one seeded sample.
NO_JITTER: Any = lambda _low, high: 0.0  # noqa: E731
MAX_JITTER: Any = lambda _low, high: high  # noqa: E731


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Forget the developer's AWS environment and forbid real clients.

    `model_id` reads `MODEL_ID` and the client factory reads `TARGET_REGION`,
    so an exported value would otherwise decide the outcome of several of these
    tests. The cached client is dropped on both sides of every test so one
    test's fake cannot answer another's call.
    """
    for name in (
        "MODEL_ID",
        "TARGET_REGION",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_PROFILE",
    ):
        monkeypatch.delenv(name, raising=False)

    def forbidden(*args: object, **kwargs: object) -> Any:
        raise AssertionError(f"a test reached boto3.client{args!r} -- inject a fake")

    monkeypatch.setattr(bedrock.boto3, "client", forbidden)
    bedrock.reset_client()
    yield
    bedrock.reset_client()


def throttle(code: str = "ThrottlingException") -> Exception:
    """A throttling `ClientError`, as botocore raises it."""
    return client_error(code)


def client_error(code: str) -> Exception:
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": "scripted"}}, "Converse")


def budget(ms: int, clock: FakeClock | None = None) -> Deadline:
    """A `Deadline` with `ms` of room, driven by a clock the test controls."""
    the_clock = FakeClock() if clock is None else clock
    return Deadline(started=the_clock.now, budget_ms=ms, clock=the_clock)


# ---------------------------------------------------------------------------
# The success path and the calibration counts
# ---------------------------------------------------------------------------


def test_one_attempt_returns_the_text_and_the_usage_counts() -> None:
    fake = FakeBedrock("here is your tour", input_tokens=9_001, output_tokens=240)

    result = converse(SYSTEM, MESSAGES, budget(SOFT_DEADLINE_MS), bedrock=fake)

    assert result == ConverseResult(
        text="here is your tour",
        input_tokens=9_001,
        output_tokens=240,
        stop_reason="end_turn",
        attempts=1,
    )
    assert fake.attempts == 1


def test_usage_counts_feed_the_calibration_log() -> None:
    """The counts are returned so the handler can price its own estimate.

    `usage.inputTokens` is the only ground truth for the character-ratio
    estimate -- there is no offline Nova tokenizer -- so the pair has to survive
    the call intact and be usable by `tokens.log_calibration` as-is.
    """
    fake = FakeBedrock("ok", input_tokens=24_000, output_tokens=300)

    result = converse(SYSTEM, MESSAGES, budget(SOFT_DEADLINE_MS), bedrock=fake)
    record = log_calibration(26_400, result.input_tokens, result.output_tokens)

    assert record == {
        "estimated": 26_400,
        "actual": 24_000,
        "output": 300,
        "ratio": round(24_000 / 26_400, 3),
    }


def test_an_empty_answer_is_returned_rather_than_raised() -> None:
    """`orientation.finalize` repairs empty model output; it cannot repair a 500."""
    fake = FakeBedrock("")

    result = converse(SYSTEM, MESSAGES, budget(SOFT_DEADLINE_MS), bedrock=fake)

    assert result.text == ""
    assert result.attempts == 1


@pytest.mark.parametrize(
    ("label", "usage"),
    [
        ("absent", None),
        ("empty", {}),
        ("null_counts", {"inputTokens": None, "outputTokens": None}),
        ("text_counts", {"inputTokens": "lots", "outputTokens": "some"}),
        ("negative", {"inputTokens": -5, "outputTokens": -5}),
    ],
)
def test_missing_usage_costs_the_sample_not_the_answer(
    label: str, usage: dict | None
) -> None:
    """The counts divide inside `log_calibration`, so they must be real ints."""
    envelope = FakeBedrock().reply("still useful")
    if usage is None:
        del envelope["usage"]
    else:
        envelope["usage"] = usage
    fake = FakeBedrock(envelope)

    result = converse(SYSTEM, MESSAGES, budget(SOFT_DEADLINE_MS), bedrock=fake)

    assert result.text == "still useful"
    assert (result.input_tokens, result.output_tokens) == (0, 0)


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


def test_the_inference_config_is_the_one_the_design_specifies() -> None:
    fake = FakeBedrock("ok")

    converse(SYSTEM, MESSAGES, budget(SOFT_DEADLINE_MS), bedrock=fake)

    assert fake.calls[0]["inferenceConfig"] == {
        "maxTokens": MAX_OUTPUT_TOKENS,
        "temperature": MODEL_TEMPERATURE,
        "topP": MODEL_TOP_P,
    }
    # The literals, asserted here so a tuning edit to config.py that breaks
    # Requirement 4 AC4's headroom fails a test rather than shipping quietly.
    assert (MAX_OUTPUT_TOKENS, MODEL_TEMPERATURE) == (600, 0.3)


def test_the_prompt_and_the_messages_pass_through_untouched() -> None:
    fake = FakeBedrock("ok")

    converse(SYSTEM, MESSAGES, budget(SOFT_DEADLINE_MS), bedrock=fake)

    assert fake.calls[0]["system"] == [{"text": SYSTEM}]
    assert fake.calls[0]["messages"] == MESSAGES


def test_the_model_id_defaults_to_the_id_the_preflight_confirmed() -> None:
    fake = FakeBedrock("ok")

    converse(SYSTEM, MESSAGES, budget(SOFT_DEADLINE_MS), bedrock=fake)

    assert model_id() == DEFAULT_MODEL_ID == "amazon.nova-lite-v1:0"
    assert fake.calls[0]["modelId"] == DEFAULT_MODEL_ID


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("us.amazon.nova-lite-v1:0", "us.amazon.nova-lite-v1:0"),
        ("  amazon.nova-micro-v1:0  ", "amazon.nova-micro-v1:0"),
        ("", DEFAULT_MODEL_ID),
        ("   ", DEFAULT_MODEL_ID),
    ],
)
def test_the_model_id_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: str
) -> None:
    """`scripts/deploy.sh` passes the id out of the `.preflight-ok` marker.

    A region that only serves Nova through the cross-region inference profile
    therefore needs no code change, and an empty variable falls back rather
    than calling Bedrock with an empty model id.
    """
    monkeypatch.setenv("MODEL_ID", value)
    fake = FakeBedrock("ok")

    converse(SYSTEM, MESSAGES, budget(SOFT_DEADLINE_MS), bedrock=fake)

    assert fake.calls[0]["modelId"] == expected


# ---------------------------------------------------------------------------
# The attempt bound (Requirement 6 AC2)
# ---------------------------------------------------------------------------


def test_sustained_throttling_makes_exactly_three_attempts() -> None:
    """Initial attempt plus two retries, then `MODEL_BUSY`. Requirement 6 AC2, AC3."""
    clock = FakeClock()
    sleep = RecordingSleep(clock)
    fake = FakeBedrock(throttle())

    with pytest.raises(AgentError) as caught:
        converse(
            SYSTEM,
            MESSAGES,
            budget(SOFT_DEADLINE_MS, clock),
            bedrock=fake,
            sleep=sleep,
            jitter=MAX_JITTER,
        )

    assert caught.value.code is ErrorCode.MODEL_BUSY
    assert fake.attempts == MAX_BEDROCK_RETRIES + 1 == 3
    assert len(sleep.delays) == MAX_BEDROCK_RETRIES == 2


def test_a_throttle_that_clears_is_not_retried_again() -> None:
    clock = FakeClock()
    sleep = RecordingSleep(clock)
    fake = FakeBedrock(throttle(), "here is your tour")

    result = converse(
        SYSTEM,
        MESSAGES,
        budget(SOFT_DEADLINE_MS, clock),
        bedrock=fake,
        sleep=sleep,
    )

    assert (result.text, result.attempts) == ("here is your tour", 2)
    assert len(sleep.delays) == 1


@pytest.mark.parametrize("code", sorted(THROTTLE_CODES))
def test_every_throttle_code_is_retried_then_reported_as_model_busy(code: str) -> None:
    clock = FakeClock()
    sleep = RecordingSleep(clock)
    fake = FakeBedrock(throttle(code))

    with pytest.raises(AgentError) as caught:
        converse(
            SYSTEM,
            MESSAGES,
            budget(SOFT_DEADLINE_MS, clock),
            bedrock=fake,
            sleep=sleep,
        )

    assert caught.value.code is ErrorCode.MODEL_BUSY
    assert fake.attempts == 3


@pytest.mark.parametrize(
    "code",
    [
        "ValidationException",
        "AccessDeniedException",
        "ResourceNotFoundException",
        "ModelErrorException",
        "ModelTimeoutException",
        "InternalServerException",
        "SerializationException",
        "",
    ],
)
def test_a_non_throttling_error_is_internal_and_is_not_retried(code: str) -> None:
    """None of these get better on a second try, and the budget is finite."""
    sleep = RecordingSleep()
    fake = FakeBedrock(client_error(code))

    with pytest.raises(AgentError) as caught:
        converse(SYSTEM, MESSAGES, budget(SOFT_DEADLINE_MS), bedrock=fake, sleep=sleep)

    assert caught.value.code is ErrorCode.INTERNAL
    assert fake.attempts == 1
    assert sleep.delays == []


def test_a_throttle_followed_by_a_hard_error_stops_on_the_hard_error() -> None:
    clock = FakeClock()
    sleep = RecordingSleep(clock)
    fake = FakeBedrock(throttle(), client_error("ValidationException"))

    with pytest.raises(AgentError) as caught:
        converse(
            SYSTEM,
            MESSAGES,
            budget(SOFT_DEADLINE_MS, clock),
            bedrock=fake,
            sleep=sleep,
        )

    assert caught.value.code is ErrorCode.INTERNAL
    assert fake.attempts == 2
    assert len(sleep.delays) == 1


# ---------------------------------------------------------------------------
# The backoff shape (Requirement 6 AC2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "jitter"), [("none", NO_JITTER), ("max", MAX_JITTER)]
)
def test_backoff_delays_increase_for_either_jitter_extreme(
    label: str, jitter: Any
) -> None:
    """0.5 s then 1.0 s, each plus up to 0.25 s.

    The jitter ceiling is smaller than the gap between consecutive delays, so
    strictly increasing holds for every draw -- 0.75 s is the worst first delay
    and 1.0 s the best second one.
    """
    clock = FakeClock()
    sleep = RecordingSleep(clock)
    fake = FakeBedrock(throttle())

    with pytest.raises(AgentError):
        converse(
            SYSTEM,
            MESSAGES,
            budget(SOFT_DEADLINE_MS, clock),
            bedrock=fake,
            sleep=sleep,
            jitter=jitter,
        )

    base, jit = BASE_BACKOFF_SECONDS, BACKOFF_JITTER_SECONDS
    first, second = sleep.delays

    assert second > first, "each backoff must exceed the one before it"
    assert base <= first <= base + jit
    assert 2 * base <= second <= 2 * base + jit


def test_backoff_delay_is_exponential_in_the_attempt_number() -> None:
    assert [backoff_delay(n, NO_JITTER) for n in range(3)] == [0.5, 1.0, 2.0]
    assert backoff_delay(0, MAX_JITTER) == BASE_BACKOFF_SECONDS + BACKOFF_JITTER_SECONDS


def test_a_negative_attempt_is_rejected() -> None:
    with pytest.raises(ValueError):
        backoff_delay(-1)


# ---------------------------------------------------------------------------
# The deadline guard (Requirements 4 AC6, 5 AC6)
# ---------------------------------------------------------------------------


def test_no_attempt_is_started_without_room_for_an_inference() -> None:
    """A call that cannot answer in time is not placed at all."""
    fake = FakeBedrock("ok")

    with pytest.raises(AgentError) as caught:
        converse(
            SYSTEM,
            MESSAGES,
            budget(int(MIN_INFERENCE_SECONDS * 1000)),  # exactly spent, not enough
            bedrock=fake,
        )

    assert caught.value.code is ErrorCode.DEADLINE
    assert fake.attempts == 0


def test_a_retry_the_deadline_cannot_accommodate_is_refused() -> None:
    """Room for one inference, not for a backoff plus another one.

    6 500 ms clears the 6 000 ms inference window but not the 6 750 ms a worst-
    case first backoff plus an inference would need, so the retry is refused
    and the user gets `MODEL_BUSY` now instead of a dropped connection later.
    """
    clock = FakeClock()
    sleep = RecordingSleep(clock)
    fake = FakeBedrock(throttle())

    with pytest.raises(AgentError) as caught:
        converse(
            SYSTEM,
            MESSAGES,
            budget(6_500, clock),
            bedrock=fake,
            sleep=sleep,
            jitter=MAX_JITTER,
        )

    assert caught.value.code is ErrorCode.MODEL_BUSY
    assert fake.attempts == 1
    assert sleep.delays == [], "a refused retry must not sleep"


def test_a_shrinking_budget_stops_the_retries_early() -> None:
    """Room for two attempts and one backoff, not for the second retry.

    The clock advances with each backoff, so the second retry is priced against
    what is actually left rather than against the original budget.
    """
    clock = FakeClock()
    sleep = RecordingSleep(clock)
    fake = FakeBedrock(throttle())

    with pytest.raises(AgentError) as caught:
        converse(
            SYSTEM,
            MESSAGES,
            budget(7_300, clock),
            bedrock=fake,
            sleep=sleep,
            jitter=MAX_JITTER,
        )

    assert caught.value.code is ErrorCode.MODEL_BUSY
    assert fake.attempts == 2
    assert sleep.delays == [BASE_BACKOFF_SECONDS + BACKOFF_JITTER_SECONDS]
    assert clock.now == pytest.approx(0.75)


def test_a_generous_deadline_allows_the_full_three_attempts() -> None:
    clock = FakeClock()
    sleep = RecordingSleep(clock)
    fake = FakeBedrock(throttle())

    with pytest.raises(AgentError):
        converse(
            SYSTEM,
            MESSAGES,
            budget(SOFT_DEADLINE_MS, clock),
            bedrock=fake,
            sleep=sleep,
            jitter=MAX_JITTER,
        )

    assert fake.attempts == 3
    assert sleep.total == pytest.approx(0.75 + 1.25)


# ---------------------------------------------------------------------------
# Transport failures and malformed answers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "error"),
    [
        ("read_timeout", ReadTimeoutError(endpoint_url="https://bedrock.invalid")),
        (
            "connect_timeout",
            ConnectTimeoutError(endpoint_url="https://bedrock.invalid"),
        ),
        (
            "endpoint_unreachable",
            EndpointConnectionError(endpoint_url="https://bedrock.invalid"),
        ),
    ],
)
def test_a_transport_failure_is_internal_and_is_not_retried(
    label: str, error: Exception
) -> None:
    """A 25-second read timeout has already spent what a retry would need."""
    sleep = RecordingSleep()
    fake = FakeBedrock(error)

    with pytest.raises(AgentError) as caught:
        converse(SYSTEM, MESSAGES, budget(SOFT_DEADLINE_MS), bedrock=fake, sleep=sleep)

    assert caught.value.code is ErrorCode.INTERNAL
    assert fake.attempts == 1
    assert sleep.delays == []


@pytest.mark.parametrize(
    ("label", "envelope"),
    [
        ("empty", {}),
        ("no_message", {"output": {}}),
        ("no_content", {"output": {"message": {}}}),
        ("empty_content", {"output": {"message": {"content": []}}}),
        ("no_text", {"output": {"message": {"content": [{"image": {}}]}}}),
        ("text_not_a_string", {"output": {"message": {"content": [{"text": 42}]}}}),
        ("content_not_a_list", {"output": {"message": {"content": "hello"}}}),
    ],
)
def test_a_malformed_envelope_is_internal(label: str, envelope: dict) -> None:
    """A response we cannot read is a bug on our side, not a busy model."""
    fake = FakeBedrock(envelope)

    with pytest.raises(AgentError) as caught:
        converse(SYSTEM, MESSAGES, budget(SOFT_DEADLINE_MS), bedrock=fake)

    assert caught.value.code is ErrorCode.INTERNAL
    assert fake.attempts == 1


def test_no_error_carries_interpolation_parameters() -> None:
    """`MODEL_BUSY` and `INTERNAL` copy names no number, so none is attached."""
    for scripted in (throttle(), client_error("ValidationException")):
        with pytest.raises(AgentError) as caught:
            converse(
                SYSTEM,
                MESSAGES,
                budget(SOFT_DEADLINE_MS, FakeClock()),
                bedrock=FakeBedrock(scripted),
                sleep=RecordingSleep(),
            )
        assert caught.value.params == {}


# ---------------------------------------------------------------------------
# The client (botocore's retries, the region, the cache)
# ---------------------------------------------------------------------------


def test_botocore_retries_are_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one claim that needs a real client object. No call is made on it.

    `retries.max_attempts` in a client `Config` counts *retries*: botocore adds
    one for the initial request, so 0 means a single attempt and the effective
    `total_max_attempts` reads 1. That is what keeps this module's loop the
    only thing that retries -- leaving the default legacy mode on would
    multiply three attempts into fifteen and make Requirement 6 AC2 false.

    Dummy credentials are set so botocore's credential chain resolves from the
    environment instead of reaching for instance metadata.
    """
    monkeypatch.setenv("TARGET_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/EXAMPLEKEY")
    monkeypatch.setattr(bedrock.boto3, "client", REAL_BOTO3_CLIENT)

    built = bedrock.client()

    assert built.meta.config.retries == {"mode": "standard", "total_max_attempts": 1}
    # The declared literal survives client construction, which normalizes the
    # Config's own copy of it in place.
    assert dict(bedrock.RETRY_CONFIG) == {"max_attempts": 0, "mode": "standard"}


def test_the_client_is_built_for_the_target_region_and_then_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One client per process: a warm Lambda reuses the connection."""
    built: list[tuple[str, str]] = []

    def record(service: str, *, region_name: str, config: object) -> FakeBedrock:
        built.append((service, region_name))
        assert config is bedrock.CLIENT_CONFIG
        return FakeBedrock("ok")

    monkeypatch.setenv("TARGET_REGION", "eu-west-1")
    monkeypatch.setattr(bedrock.boto3, "client", record)

    first = bedrock.client()
    second = bedrock.client()

    assert first is second
    assert built == [("bedrock-runtime", "eu-west-1")]


def test_the_lambda_runtime_region_is_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`AWS_REGION` is the function's own region, which is the Target_Region."""
    seen: list[str] = []

    def record(service: str, *, region_name: str, config: object) -> FakeBedrock:
        seen.append(region_name)
        return FakeBedrock("ok")

    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    monkeypatch.setattr(bedrock.boto3, "client", record)

    bedrock.client()

    assert seen == ["ap-south-1"]


def test_no_region_at_all_is_an_internal_error() -> None:
    """A deployment mistake, not anything the user did. Requirement 6 AC5."""
    with pytest.raises(AgentError) as caught:
        bedrock.client()

    assert caught.value.code is ErrorCode.INTERNAL


def test_converse_uses_the_module_client_when_none_is_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeBedrock("from the module client")
    monkeypatch.setenv("TARGET_REGION", "us-east-1")
    monkeypatch.setattr(bedrock.boto3, "client", lambda *args, **kwargs: fake)

    result = converse(SYSTEM, MESSAGES, budget(SOFT_DEADLINE_MS))

    assert result.text == "from the module client"
    assert fake.attempts == 1
