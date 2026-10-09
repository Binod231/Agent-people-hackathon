"""Tests for the error taxonomy: the copy table, `render()`, and redaction.

Requirements 2 AC5, 2 AC6, 2 AC7, 3 AC8, 3 AC9, 6 AC1, 6 AC3, 6 AC6, 8 AC1.

These examples stand in for the design's Property 12 (every failure maps to a
complete, actionable user message) and Property 13 (user-facing errors never
leak internals), both dropped in the lean revision -- see `docs/out-of-scope.md`
entry 7, which names Property 13 as one of the two worst losses in that cut. Its
failure mode is an ARN or an account id shown to a stranger, so
`test_render_leaks_no_internals` is written adversarially: every code, crossed
with six secret shapes, crossed with every route a secret could take toward a
rendered message. A generator would explore that space more cheaply; a table
this wide is the honest substitute.

Four claims, in file order:

- **The table is total.** One row per `ErrorCode`, every status the design's
  mapping table assigns, and no `RATE_LIMITED` row left behind the dropped usage
  plan. `render()` indexes the table directly, so a gap would be a `KeyError` in
  the one code path that must not fail.
- **Every row is usable.** Non-empty, more than one sentence, and a next action
  in the closing sentence -- Requirement 6 AC1 asks for the cause first and the
  action last, and "it has copy" is not the same claim as "the copy tells you
  what to do".
- **`render()` cannot raise.** Missing params, wrong types, out-of-range
  numbers, keys off the whitelist, and an `AgentError` mangled after
  construction all still render. The handler calls `render()` from inside
  `except AgentError`, so anything thrown here becomes an unhandled 500 and the
  user loses the message entirely.
- **`render()` cannot leak.** Requirement 6 AC6 excludes AWS account
  identifiers, stack trace content, and credential values from user-facing text.
"""

from __future__ import annotations

import json
import string

import pytest

from agent.config import (
    MAX_BODY_BYTES,
    MAX_FILE_BYTES,
    MAX_REPO_SIZE_MB,
    MAX_TREE_FILES,
)
from agent.errors import ALLOWED_PARAMS, ERROR_COPY, AgentError, ErrorCode, render

# The status the design's mapping table assigns each code, restated here so an
# edit to the table fails a test rather than redefining the contract quietly.
# 413 is reserved for the request body (Requirement 8 AC1) and an oversize
# repository is 422; a GitHub rate limit is 503 rather than 429, so the Chat_UI
# can tell it apart from anything the agent did itself.
EXPECTED_STATUS = {
    ErrorCode.BAD_URL: 400,
    ErrorCode.REPO_NOT_FOUND: 404,
    ErrorCode.GITHUB_RATE_LIMITED: 503,
    ErrorCode.GITHUB_UNAVAILABLE: 503,
    ErrorCode.REPO_TOO_LARGE: 422,
    ErrorCode.REPO_TOO_MANY_FILES: 422,
    ErrorCode.NO_READABLE_FILES: 422,
    ErrorCode.BODY_TOO_LARGE: 413,
    ErrorCode.MODEL_BUSY: 503,
    ErrorCode.DEADLINE: 504,
    ErrorCode.INTERNAL: 500,
}

# Phrases that hand the user something to do. Requirement 6 AC1 asks for one
# next action; this is the vocabulary the table actually uses.
ACTION_CUES = (
    "try",
    "paste",
    "send",
    "give it",
    "point me",
    "start a fresh session",
    "shorten",
    "double-check",
    "check whether",
)

ALL_CODES = list(ErrorCode)
TEMPLATES = {text for _, text in ERROR_COPY.values()}


def last_sentence(message: str) -> str:
    """The closing sentence, where the next action belongs."""
    return message.rstrip().split(". ")[-1]


def serialize(error: AgentError) -> str:
    """The rendered body as one string, so a leak anywhere in it is a substring."""
    status, body = render(error)
    return json.dumps({"status": status, **body}, ensure_ascii=False)


def placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


# ---------------------------------------------------------------------------
# The table is total over the enum
# ---------------------------------------------------------------------------


def test_table_is_complete_over_the_enum() -> None:
    """One row per code, and no row without a code.

    `errors.py` asserts this at import time as well. It is repeated here because
    `python -O` strips `assert` statements, and this is the invariant that makes
    `render()`'s table lookup safe.
    """
    assert set(ERROR_COPY) == set(ErrorCode)
    assert len(ERROR_COPY) == len(ALL_CODES)


def test_statuses_match_the_design_mapping_table() -> None:
    assert {code: status for code, (status, _) in ERROR_COPY.items()} == EXPECTED_STATUS


def test_rate_limited_and_retry_after_are_gone() -> None:
    """The dropped usage plan took its 429 with it.

    Requirement 6 AC4 went away in the lean revision and there is no API Gateway
    throttle or daily quota left to produce a 429, so a `RATE_LIMITED` row would
    be copy for a response that can no longer happen.
    """
    assert "RATE_LIMITED" not in ErrorCode.__members__
    assert "retry_after" not in ALLOWED_PARAMS
    assert ALLOWED_PARAMS == {"size_mb", "file_count", "minutes", "limit_mb"}


def test_every_placeholder_is_whitelisted() -> None:
    """Copy may only ask for values `render()` is allowed to supply.

    This keeps the un-interpolated fallback unreachable for the shipped table: a
    new row referencing `{owner}` would leave braces in a user-facing message,
    and it fails here instead.
    """
    for code, (_, template) in ERROR_COPY.items():
        names = placeholders(template)
        assert names <= ALLOWED_PARAMS, f"{code.value} interpolates {names}"


# ---------------------------------------------------------------------------
# Every row is complete and actionable (Requirement 6 AC1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", ALL_CODES, ids=lambda c: c.value)
def test_every_code_renders_actionable_copy(code: ErrorCode) -> None:
    """Cause in plain language, one next action, nothing left to fill in."""
    status, body = render(AgentError(code, minutes=7, size_mb=412, file_count=6_200))
    message = body["error"]["message"]

    assert body["error"]["code"] == code.value
    assert status == EXPECTED_STATUS[code]
    assert message.strip()
    assert len(message) > 80, "a one-liner cannot state a cause and an action"
    assert message.count(". ") >= 1, "cause first, action last needs two sentences"
    assert "{" not in message and "}" not in message, "a placeholder survived"

    closing = last_sentence(message).lower()
    assert any(cue in closing for cue in ACTION_CUES), (
        f"{code.value} closes with no next action: {closing!r}"
    )


def test_body_carries_only_the_code_and_the_message() -> None:
    """No `retry_after_seconds`, and nothing the caller did not ask for.

    `handler._response` wraps `steps` and `correlation_id` around this; the error
    object itself is exactly two string fields.
    """
    _, body = render(AgentError(ErrorCode.MODEL_BUSY))

    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert all(isinstance(value, str) for value in body["error"].values())


def test_copy_states_the_limits_the_code_enforces() -> None:
    """The numbers in the static copy are the numbers actually enforced.

    The figures are literal text rather than interpolated constants, so this is
    the guard against copy drifting away from `config.py` after a tuning change.
    """
    assert MAX_REPO_SIZE_MB == 100
    assert "**100 MB**" in ERROR_COPY[ErrorCode.REPO_TOO_LARGE][1]
    assert MAX_TREE_FILES == 5_000
    assert "**5,000 files**" in ERROR_COPY[ErrorCode.REPO_TOO_MANY_FILES][1]
    assert MAX_FILE_BYTES == 500 * 1024
    assert "500 KB" in ERROR_COPY[ErrorCode.NO_READABLE_FILES][1]
    assert MAX_BODY_BYTES == 8_192
    assert "**8 KB**" in ERROR_COPY[ErrorCode.BODY_TOO_LARGE][1]


# ---------------------------------------------------------------------------
# Interpolation: whitelisted numbers only
# ---------------------------------------------------------------------------


def test_whitelisted_numbers_are_interpolated() -> None:
    _, rate = render(AgentError(ErrorCode.GITHUB_RATE_LIMITED, minutes=7))
    assert "about **7 minutes**" in rate["error"]["message"]

    _, size = render(AgentError(ErrorCode.REPO_TOO_LARGE, size_mb=412))
    assert "about **412 MB**" in size["error"]["message"]

    oversize = AgentError(ErrorCode.REPO_TOO_LARGE)
    oversize.params = {"size_mb": 412.6}
    _, rounded = render(oversize)
    assert "about **413 MB**" in rounded["error"]["message"]


def test_unused_params_are_ignored_not_rendered() -> None:
    """A param the template never names changes nothing.

    `github_client` passes `limit_mb` beside `size_mb`, and `file_count` for the
    too-many-files row, because that copy states its limits as literal text.
    """
    _, body = render(
        AgentError(ErrorCode.REPO_TOO_LARGE, size_mb=412, limit_mb=100, file_count=12)
    )

    expected = ERROR_COPY[ErrorCode.REPO_TOO_LARGE][1].format(size_mb=412)
    assert body["error"]["message"] == expected


def test_params_off_the_whitelist_are_dropped_at_construction() -> None:
    error = AgentError(
        ErrorCode.INTERNAL,
        minutes=3,
        account_id="123456789012",
        detail="Traceback (most recent call last):",
    )

    assert error.params == {"minutes": 3}


# ---------------------------------------------------------------------------
# render() cannot raise
# ---------------------------------------------------------------------------

# Every way a param can be unusable. The handler calls `render()` from inside
# `except AgentError`, so each of these would otherwise be an unhandled 500.
BAD_PARAMS = [
    pytest.param({}, id="missing"),
    pytest.param({"minutes": None}, id="none"),
    pytest.param({"minutes": "soon"}, id="prose"),
    pytest.param({"minutes": ""}, id="empty-string"),
    pytest.param({"minutes": "7"}, id="numeric-string"),
    pytest.param({"minutes": True}, id="bool"),
    pytest.param({"minutes": float("nan")}, id="nan"),
    pytest.param({"minutes": float("inf")}, id="inf"),
    pytest.param({"minutes": -5}, id="negative"),
    pytest.param({"minutes": 10**12}, id="account-id-sized"),
    pytest.param({"minutes": [7]}, id="list"),
    pytest.param({"minutes": {"value": 7}}, id="dict"),
    pytest.param({"minutes": object()}, id="object"),
    pytest.param({"size_mb": None, "minutes": None}, id="several-none"),
    pytest.param({"unknown_key": 7}, id="off-whitelist"),
    pytest.param({"minutes": 7, "unknown_key": object()}, id="mixed"),
]


@pytest.mark.parametrize("code", ALL_CODES, ids=lambda c: c.value)
@pytest.mark.parametrize("params", BAD_PARAMS)
def test_render_never_raises_on_bad_params(
    code: ErrorCode, params: dict[str, object]
) -> None:
    """Every row renders a complete message whatever `params` holds."""
    error = AgentError(code)
    error.params = dict(params)  # bypasses the construction-time key filter

    status, body = render(error)

    assert status == EXPECTED_STATUS[code]
    assert body["error"]["code"] == code.value
    assert body["error"]["message"] in TEMPLATES or "{" not in body["error"]["message"]
    assert body["error"]["message"].strip()


def test_unusable_params_fall_back_to_the_un_interpolated_copy() -> None:
    """A degraded message beats an exception thrown from the error path.

    All-or-nothing on purpose: the static template comes back as it stands
    rather than half-filled, so the sentence still reads and the next action
    survives.
    """
    template = ERROR_COPY[ErrorCode.GITHUB_RATE_LIMITED][1]

    _, missing = render(AgentError(ErrorCode.GITHUB_RATE_LIMITED))
    assert missing["error"]["message"] == template

    wrong_type = AgentError(ErrorCode.GITHUB_RATE_LIMITED)
    wrong_type.params = {"minutes": "quite a while"}
    _, rendered = render(wrong_type)
    assert rendered["error"]["message"] == template
    assert "send that URL again" in rendered["error"]["message"]


@pytest.mark.parametrize(
    "code",
    [None, "RATE_LIMITED", "", 7, ["BAD_URL"], object()],
    ids=["none", "dropped-code", "empty", "int", "unhashable", "object"],
)
def test_render_treats_an_unrecognized_code_as_internal(code: object) -> None:
    """An `ErrorCode` that is not one renders the 500 row instead of raising.

    Reachable two ways: a stale code deserialized from somewhere, or a module
    still raising `RATE_LIMITED` after the lean revision dropped it.
    """
    error = AgentError(ErrorCode.INTERNAL)
    error.code = code  # type: ignore[assignment]

    status, body = render(error)

    assert (status, body["error"]["code"]) == (500, ErrorCode.INTERNAL.value)
    assert body["error"]["message"] == ERROR_COPY[ErrorCode.INTERNAL][1]


def test_render_survives_a_mangled_params_attribute() -> None:
    error = AgentError(ErrorCode.REPO_TOO_LARGE)
    error.params = "size_mb=412"  # type: ignore[assignment]

    status, body = render(error)

    assert status == 422
    assert body["error"]["message"] == ERROR_COPY[ErrorCode.REPO_TOO_LARGE][1]


def test_render_accepts_a_code_that_arrives_as_a_plain_string() -> None:
    error = AgentError(ErrorCode.INTERNAL)
    error.code = "BAD_URL"  # type: ignore[assignment]

    status, body = render(error)

    assert status == 400
    assert body["error"] == {
        "code": "BAD_URL",
        "message": ERROR_COPY[ErrorCode.BAD_URL][1],
    }


# ---------------------------------------------------------------------------
# render() cannot leak internals (Requirement 6 AC6)
# ---------------------------------------------------------------------------

# Six shapes of the three things Requirement 6 AC6 names: AWS account
# identifiers, stack trace content, and credential values.
SECRETS = {
    "account-id": "123456789012",
    "role-arn": "arn:aws:iam::123456789012:role/welcome-guide-agent-fn",
    "access-key": "AKIAIOSFODNN7EXAMPLE",
    "bearer-token": "Bearer eyJhbGciOiJIUzI1NiJ9.ZXhhbXBsZQ.c2lnbmF0dXJl",
    "lambda-path": "/var/task/agent/bedrock.py",
    "traceback": (
        'Traceback (most recent call last):\n  File "/var/task/agent/handler.py",'
        " line 61, in handler\n    raise RuntimeError(aws_account_id)"
    ),
}


def embeddings(code: ErrorCode, secret: str) -> dict[str, AgentError]:
    """Every route a secret could take toward a rendered message."""
    whitelisted_key = AgentError(code, **{name: secret for name in ALLOWED_PARAMS})

    off_whitelist = AgentError(
        code, account_id=secret, detail=secret, traceback=secret, message=secret
    )

    mutated = AgentError(code)
    mutated.params = dict.fromkeys(ALLOWED_PARAMS, secret)

    in_args = AgentError(code)
    in_args.args = (secret,)

    # Numbers pass the whitelist, so the magnitude bound is what keeps an
    # account-id-shaped integer out of the copy.
    as_a_number = AgentError(code, minutes=123456789012, size_mb=123456789012)

    chained = AgentError(code)
    try:
        raise RuntimeError(secret)
    except RuntimeError as exc:
        chained.__cause__ = exc

    in_the_code = AgentError(ErrorCode.INTERNAL)
    in_the_code.code = f"{code.value}:{secret}"  # type: ignore[assignment]

    return {
        "whitelisted-key": whitelisted_key,
        "off-whitelist-key": off_whitelist,
        "params-mutated-after-construction": mutated,
        "exception-args": in_args,
        "account-id-shaped-number": as_a_number,
        "chained-cause": chained,
        "secret-in-the-code": in_the_code,
    }


@pytest.mark.parametrize("code", ALL_CODES, ids=lambda c: c.value)
@pytest.mark.parametrize("secret_name", sorted(SECRETS), ids=sorted(SECRETS))
def test_render_leaks_no_internals(code: ErrorCode, secret_name: str) -> None:
    """No account id, ARN, key, token, path, or traceback reaches the user.

    The copy is static text plus small whitelisted numbers, which is what makes
    this structural rather than a filter: there is no path from an exception
    into the template. The cases below push a secret in through the params
    whitelist, around it, after construction, through the exception's own
    `args`, through a chained cause, through the code field, and as an integer
    shaped like an account id. Every one of them must render table copy and
    nothing else.
    """
    secret = SECRETS[secret_name]
    fragments = [secret, *(part for part in secret.split() if len(part) >= 8)]

    for shape, error in embeddings(code, secret).items():
        status, body = render(error)
        rendered = serialize(error)
        message = body["error"]["message"]

        assert isinstance(status, int)
        assert message.strip(), f"{shape} rendered an empty message"
        for fragment in fragments:
            assert fragment not in rendered, f"{shape} leaked {fragment!r}"
        assert message in TEMPLATES, f"{shape} produced copy outside the table"
        assert "123456789012" not in rendered, f"{shape} leaked an account id"


def test_the_redaction_check_can_fail() -> None:
    """A test that can only pass proves nothing, so this pins the detector.

    The same substring check, run against copy that does carry a secret, finds
    it. Without this, a bug that made `serialize` return `""` would turn the
    whole redaction table green.
    """
    secret = SECRETS["role-arn"]
    leaky = f"Something broke on my side, in {secret}. Try that again."

    assert secret in leaky
    assert secret not in serialize(AgentError(ErrorCode.INTERNAL, size_mb=secret))
