"""The only module that talks to Amazon Bedrock. Three attempts, by construction.

One function matters: `converse`. It wraps the Converse API with the bounded
retry Requirement 6 AC2 specifies -- an initial attempt plus at most two
retries, each preceded by an exponentially growing backoff -- and it is the only
retry policy in the system (`github_client` deliberately has none, so the
request deadline is never spent twice over).

Five things here are easy to get wrong, so each is called out where it happens:

- **botocore's own retries are switched off.** `retries={"max_attempts": 0}` in
  a client `Config` means *zero retries*, one total attempt: botocore counts
  retries rather than attempts and adds one for the initial request. The built
  client's effective `total_max_attempts` reads 1, which is what
  `test_bedrock.py` asserts. Leaving the default legacy mode on would multiply
  this module's three attempts by botocore's five, and "up to 2 retries" in
  Requirement 6 AC2 would be fiction.
- **The attempt bound is the loop, not a condition inside it.** `converse`
  iterates `range(MAX_BEDROCK_RETRIES + 1)`, so three attempts is structural:
  a wrong comparison inside the body could cost a retry, never add one.
- **A retry is only started if it can finish.** Each backoff is priced against
  the remaining budget plus `MIN_INFERENCE_SECONDS` before it is slept. A retry
  that answers after API Gateway has hung up is worse for the user than an
  immediate `MODEL_BUSY` message, so the refusal is deliberate.
- **Only `AgentError` leaves this module.** Throttling becomes `MODEL_BUSY`,
  everything else -- validation, access denied, a read timeout, a response whose
  shape surprised us -- becomes `INTERNAL`. The handler's boundary would map a
  stray exception to `INTERNAL` anyway; doing it here keeps the exception text
  away from a caller that might be tempted to show it (Requirement 6 AC7).
- **The client is built once, lazily, and is injectable.** Lazily because
  importing this module must not require a region (tests import it, and so does
  anything that only wants `THROTTLE_CODES`); once because a warm Lambda
  reusing the connection is worth about a second of the 20-second orientation
  budget; injectable because every test here substitutes a scripted double,
  the same seam `github_client.Transport` and `preflight._client` use.

`ConverseResult` carries `input_tokens` and `output_tokens` straight out of
`usage`. That is the only ground truth available for the token estimate -- there
is no offline Nova tokenizer -- so every call is a free calibration sample. This
module does not log it: the estimate it would be compared against lives with the
caller, and `tokens.log_calibration` writes the one line per request from the
handler.

Model id and region come from the environment at call time. `scripts/deploy.sh`
reads both out of the `.preflight-ok` marker and passes them to the function, so
the id this module invokes is the one the preflight actually confirmed and the
one the IAM policy authorizes.
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .config import (
    BACKOFF_JITTER_SECONDS,
    BASE_BACKOFF_SECONDS,
    BEDROCK_CONNECT_TIMEOUT_SECONDS,
    BEDROCK_READ_TIMEOUT_SECONDS,
    MAX_BEDROCK_RETRIES,
    MAX_OUTPUT_TOKENS,
    MIN_INFERENCE_SECONDS,
    MODEL_TEMPERATURE,
    MODEL_TOP_P,
)
from .errors import AgentError, ErrorCode
from .models import Deadline

__all__ = [
    "CLIENT_CONFIG",
    "DEFAULT_MODEL_ID",
    "RETRY_CONFIG",
    "THROTTLE_CODES",
    "BedrockClient",
    "ConverseResult",
    "backoff_delay",
    "client",
    "converse",
    "model_id",
    "reset_client",
]

log = logging.getLogger(__name__)

# The id the preflight confirmed for this account in us-east-1. `MODEL_ID` from
# the environment wins when the deploy script sets it, which is how a Target_
# Region that only serves Nova through the cross-region inference profile gets
# the `us.` prefixed id without a code change.
DEFAULT_MODEL_ID = "amazon.nova-lite-v1:0"

# Error codes that mean "the model is busy, try again", as opposed to "this
# request is wrong". Everything outside this set is a bug or a misconfiguration
# on our side, and retrying it would only spend the deadline to fail twice.
# `ModelNotReadyException` is in here because it is transient by definition:
# the model is warming, not refusing.
THROTTLE_CODES = frozenset(
    {
        "ThrottlingException",
        "TooManyRequestsException",
        "ServiceUnavailableException",
        "ModelNotReadyException",
    }
)

# botocore's retry layer, disabled. `max_attempts` counts *retries* here: 0
# means one attempt, so the effective `total_max_attempts` on the built client
# is 1 and this module's loop is the only thing that retries.
#
# Held separately from `CLIENT_CONFIG`, and copied into it, because botocore
# normalizes a Config's `retries` mapping *in place* when it builds a client --
# `max_attempts` is replaced by the resolved `total_max_attempts`. Keeping the
# declared literal in a read-only mapping of its own means the intent stays
# inspectable after a client has been built, and that building one cannot
# rewrite what the next one asks for.
RETRY_CONFIG: Mapping[str, Any] = MappingProxyType(
    {"max_attempts": 0, "mode": "standard"}
)

# Explicit timeouts. The read timeout sits inside SOFT_DEADLINE_MS so a hung
# socket cannot outlive the handler's budget (Requirement 4 AC6).
CLIENT_CONFIG = Config(
    connect_timeout=BEDROCK_CONNECT_TIMEOUT_SECONDS,
    read_timeout=BEDROCK_READ_TIMEOUT_SECONDS,
    retries=dict(RETRY_CONFIG),
)


class BedrockClient(Protocol):
    """The slice of the `bedrock-runtime` client this module calls.

    `**kwargs` rather than a named signature because boto3 clients are
    generated at runtime and their methods are keyword-only; a scripted double
    satisfies this in a dozen lines, which is how the tests here assert the
    attempt count without a network.
    """

    def converse(self, **kwargs: Any) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class ConverseResult:
    """One successful Converse call.

    `input_tokens` and `output_tokens` are what Bedrock charged, for the
    calibration log (`tokens.log_calibration`). `attempts` is how many calls it
    took, which is what Requirement 6 AC2's bound is asserted against.
    """

    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str
    attempts: int


# ---------------------------------------------------------------------------
# Environment and the client seam
# ---------------------------------------------------------------------------


def model_id() -> str:
    """The model id to invoke, from the environment. Requirement 8 AC8.

    Read per call rather than at import so a cold start cannot be decided by an
    import-time snapshot, and so a test can set the variable without reloading
    the module. Falls back to the id the preflight confirmed, which is also the
    id `infra/template.yaml` pins the IAM policy to.
    """
    return (os.environ.get("MODEL_ID") or "").strip() or DEFAULT_MODEL_ID


def _resolve_region() -> str:
    """The region to call Bedrock in.

    `TARGET_REGION` is what the deploy script passes, taken from the marker the
    preflight wrote, so it names the region where model access was actually
    confirmed. `AWS_REGION` is the fallback and is not an implicit inheritance
    the way Requirement 1 AC4 forbids for the preflight: the Lambda runtime
    sets it to the region the function is deployed in, which *is* the
    Target_Region once `infra/template.yaml` has had its say.

    Neither present is a deployment mistake rather than anything the user did,
    so it reads as `INTERNAL` with a log line naming the missing variable.
    """
    region = (os.environ.get("TARGET_REGION") or "").strip()
    if region:
        return region
    region = (os.environ.get("AWS_REGION") or "").strip()
    if region:
        return region
    log.error(
        json.dumps({"event": "bedrock_region_unset", "expected": "TARGET_REGION"})
    )
    raise AgentError(ErrorCode.INTERNAL)


_client: BedrockClient | None = None


def client() -> BedrockClient:
    """The process-wide `bedrock-runtime` client, built on first use.

    Cached across invocations on purpose: a warm Lambda reuses the TLS
    connection, which is worth roughly a second against the 20-second
    Orientation_Message budget (Requirement 5 AC6).
    """
    global _client
    if _client is None:
        _client = boto3.client(
            "bedrock-runtime", region_name=_resolve_region(), config=CLIENT_CONFIG
        )
    return _client


def reset_client() -> None:
    """Drop the cached client. For tests that change the environment."""
    global _client
    _client = None


# ---------------------------------------------------------------------------
# Backoff (Requirement 6 AC2)
# ---------------------------------------------------------------------------


def backoff_delay(
    attempt: int, jitter: Callable[[float, float], float] = random.uniform
) -> float:
    """Seconds to wait before retry number `attempt` (zero-based).

    0.5 s then 1.0 s, each plus up to 0.25 s of jitter. The jitter ceiling is
    deliberately smaller than the gap between consecutive delays, so the
    sequence is strictly increasing for every draw -- 0.75 s is the worst first
    delay and 1.0 s the best second one -- and the "each delay is greater than
    the last" half of Property 14 holds without the test having to pin the
    random seed.

    `jitter` matches `random.uniform`'s signature so it is a drop-in default
    and a test can pass the extremes instead of sampling them.
    """
    if attempt < 0:
        raise ValueError(f"attempt must be non-negative, got {attempt}")
    return BASE_BACKOFF_SECONDS * (2**attempt) + jitter(0.0, BACKOFF_JITTER_SECONDS)


# ---------------------------------------------------------------------------
# The call (Requirements 6 AC2, AC3)
# ---------------------------------------------------------------------------


def converse(
    system: str,
    messages: Sequence[Mapping[str, Any]],
    deadline: Deadline,
    *,
    bedrock: BedrockClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[float, float], float] = random.uniform,
) -> ConverseResult:
    """Invoke Nova Lite with bounded retries. Requirement 6 AC2, AC3.

    Args:
        system: The system prompt, sent as a single system content block.
        messages: The Converse message list, newest turn last. Built by
            `chat.py`; this module does not inspect or reshape it.
        deadline: The handler's remaining budget. Consulted before every
            attempt and before every backoff.
        bedrock: Client override. Defaults to the module's cached client.
        sleep: Backoff sleep, injected so tests record delays instead of
            waiting for them.
        jitter: Backoff jitter source, injected so tests can pin the extremes.

    Returns:
        The assistant text with the token counts Bedrock charged and the number
        of attempts it took.

    Raises:
        AgentError: `DEADLINE` when there was never room for an attempt,
            `MODEL_BUSY` when throttling outlasted the retries or a retry could
            not have finished in time, `INTERNAL` for every other failure --
            including transport errors and a malformed response.
    """
    service = client() if bedrock is None else bedrock
    request = {
        "modelId": model_id(),
        "system": [{"text": system}],
        "messages": list(messages),
        "inferenceConfig": {
            "maxTokens": MAX_OUTPUT_TOKENS,  # ~350 words of headroom, 4 AC4
            "temperature": MODEL_TEMPERATURE,  # grounded, not creative
            "topP": MODEL_TOP_P,
        },
    }

    # Requirement 6 AC2 is a bound on attempts, so it is the range of the loop
    # rather than a test inside it: the body can cost an attempt, never add one.
    for attempt in range(MAX_BEDROCK_RETRIES + 1):
        if not deadline.allows(MIN_INFERENCE_SECONDS):
            # No room for an inference. On the first pass the budget was spent
            # upstream; on a later one the pre-backoff check below already
            # reserved this window, so reaching here means real time ran away
            # mid-flight -- and `DEADLINE` is the honest answer either way.
            raise AgentError(ErrorCode.DEADLINE)

        try:
            response = service.converse(**request)
        except ClientError as exc:
            code = _error_code(exc)
            if code not in THROTTLE_CODES:
                # Validation (usually an over-budget payload), access denied, a
                # bad model id. None of them get better on a second try.
                log.error(
                    json.dumps(
                        {"event": "bedrock_error", "code": code, "attempt": attempt + 1}
                    )
                )
                raise AgentError(ErrorCode.INTERNAL) from exc
            if attempt == MAX_BEDROCK_RETRIES:
                raise AgentError(ErrorCode.MODEL_BUSY) from exc

            delay = backoff_delay(attempt, jitter)
            if not deadline.allows(delay + MIN_INFERENCE_SECONDS):
                # The retry would answer after API Gateway has given up. An
                # immediate, honest "the model is busy" beats a dropped
                # connection (Requirements 4 AC6, 5 AC6).
                raise AgentError(ErrorCode.MODEL_BUSY) from exc
            log.warning(
                json.dumps(
                    {
                        "event": "bedrock_throttled",
                        "code": code,
                        "attempt": attempt + 1,
                        "backoff_ms": int(delay * 1000),
                    }
                )
            )
            sleep(delay)
        except BotoCoreError as exc:
            # Connect and read timeouts, DNS and protocol failures. Not retried:
            # the read timeout alone is most of what is left of the budget by
            # the time one fires.
            log.error(
                json.dumps(
                    {"event": "bedrock_transport_error", "kind": type(exc).__name__}
                )
            )
            raise AgentError(ErrorCode.INTERNAL) from exc
        else:
            usage = response.get("usage") or {}
            return ConverseResult(
                text=_extract_text(response),
                input_tokens=_as_int(usage.get("inputTokens")),
                output_tokens=_as_int(usage.get("outputTokens")),
                stop_reason=str(response.get("stopReason") or ""),
                attempts=attempt + 1,
            )

    # Unreachable: the loop either returns or raises on its final iteration.
    # Kept so the bound cannot be defeated by a future edit to the body.
    raise AgentError(ErrorCode.MODEL_BUSY)


# ---------------------------------------------------------------------------
# Response reading
# ---------------------------------------------------------------------------


def _error_code(exc: ClientError) -> str:
    """The `Error.Code` of a `ClientError`, defensively.

    `ClientError.response` is a plain dict a caller could have built by hand,
    and a `KeyError` in the error path would replace a clean `MODEL_BUSY` with
    an unhandled exception. An unreadable code is simply not a throttle code.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, Mapping):
        return ""
    error = response.get("Error")
    if not isinstance(error, Mapping):
        return ""
    code = error.get("Code")
    return code if isinstance(code, str) else ""


def _extract_text(response: Mapping[str, Any]) -> str:
    """The assistant text out of the Converse envelope.

    An empty string is a legitimate answer -- `orientation.finalize` is
    validate-and-repair over arbitrary model output, including nothing at all --
    so only a structurally wrong envelope raises. That is `INTERNAL` rather than
    a retry: a response we cannot read is not a busy model.
    """
    try:
        text = response["output"]["message"]["content"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AgentError(ErrorCode.INTERNAL) from exc
    if not isinstance(text, str):
        raise AgentError(ErrorCode.INTERNAL)
    return text


def _as_int(value: object) -> int:
    """Coerce a usage count to a non-negative int, defaulting to 0.

    The counts feed `tokens.log_calibration`, which divides by them. A missing
    or malformed `usage` field must cost the calibration sample, not the
    user's answer.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if not math.isfinite(value):
        return 0
    return max(0, int(value))
