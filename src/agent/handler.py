"""The Lambda entry point: the router, the guards, and the one error boundary.

This is where the two halves of the product meet. One function behind one
Lambda Function URL serves the Chat_UI *and* the chat API, so `handler` is both
a static file server and the top of the agent pipeline:

| Method and path  | Response                                      | Criterion |
|------------------|-----------------------------------------------|-----------|
| `GET /`          | `index.html`, `text/html; charset=utf-8`      | 7 AC2     |
| `GET /styles.css`| `styles.css`, `text/css; charset=utf-8`      | 7 AC2     |
| `GET /app.js`    | `app.js`, `text/javascript; charset=utf-8`   | 7 AC2     |
| `POST /api/chat` | the response envelope, `application/json`     | 2 AC1     |
| anything else    | 404, plain text                               | --        |

Serving both from one function makes the UI and the API same-origin by
construction, which is why there is no CORS configuration anywhere in this
codebase (design: Lean Deployment Revision -> Function URL Routing).

Five things here are load-bearing, so each is called out where it happens:

- **The asset allowlist is a mapping from request path to file name**, and the
  file name never comes from the request. `ASSET_ROUTES` is consulted by exact
  path, so a traversal attempt (`/../config.json`, `/%2e%2e/etc/passwd`) is not
  sanitized -- it simply is not a key, and 404s. Joining a request path onto a
  directory would be a file-disclosure hole on a public unauthenticated
  endpoint, so it is not done at all.
- **The body guard is the first statement on the chat route** (Requirement 8
  AC1), measured on *bytes* rather than characters and before any outbound
  call. A multi-byte body would slip past a `len()` check on the decoded string,
  and a guard that ran after the metadata fetch would already have cost a
  GitHub request and a model call.
- **The deadline is set from the mode, not from the route.** An orientation has
  18 s (Requirement 5 AC6 minus margin) and a follow-up turn has 26 s
  (Requirement 4 AC6 minus margin), and the mode is only known once the body is
  parsed -- so the deadline starts at the chat budget and is tightened in place,
  keeping `started` so the orientation budget is measured from the moment the
  invocation began rather than from the moment it was recognized.
- **There is exactly one error boundary**, and it is the whole of `handler`.
  Everything below raises `AgentError` or an ordinary exception and nothing
  below formats a response. The bare `except Exception` here is the only such
  clause in the codebase, so there is one place where an unexpected failure
  becomes a user-facing message (Requirement 6 AC4, AC5).
- **The `finally` writes one structured log line per invocation**, whatever
  happened, including the deadline path. The per-step timings that calibrate the
  design's latency table and the token ratio that calibrates `CHARS_PER_TOKEN`
  are only ever available here, so losing them to a failure would mean losing
  them exactly when they are most interesting.

**Where the assets live.** `infra/template.yaml` builds the deployment package
through the Makefile's `build-AgentFunction` target, which puts the three
Chat_UI files *inside* the package, one directory below this module:
`agent/ui/index.html`. In a source checkout that directory does not exist and
`ui/` at the repository root does. Both candidates are tried, in that order,
once at import. Nothing is resolved against a working directory, which Lambda
does not guarantee.

**Response shape.** The success envelope carries `message` as the assistant
text, a `mode`, a `repo` summary, the `steps` array, and the `correlation_id`;
the failure envelope replaces `message` and `repo` with `error`. Exactly one of
`message` or `error` is present and `steps` is always present, on every path,
including the ones that fail before a single step has run.

This deviates from the design's Wire Format in one place: `message` is the
assistant text as a string rather than a `{"role", "content"}` object. The task
brief for this module specifies the flat form, and it matches `error.message`
in the same envelope. `ui/app.js` (task 8.2) reads `message` as a string.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import random
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import chat
from .bedrock import BedrockClient
from .chat import Mode, TurnResult, history_tokens
from .config import (
    MAX_BODY_BYTES,
    ORIENTATION_DEADLINE_MS,
    SOFT_DEADLINE_MS,
)
from .context_builder import build_chat_context, build_orientation_context
from .errors import AgentError, ErrorCode, render
from .github_client import Transport, get_metadata, list_tree, read_files
from .models import Deadline, RepoContext, RepoMeta
from .orientation import detect_language
from .selection import decode_text, resolve_package_main, select
from .steps import STEP_ORDER, StepName, StepRecorder
from .tokens import Turn, log_calibration
from .url_parse import parse_repo_url

__all__ = [
    "ASSET_ROUTES",
    "CHAT_PATH",
    "ChatRequest",
    "TurnOutcome",
    "handler",
    "parse_request",
    "reset_assets",
    "run_turn",
    "ui_dir",
]

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Routing table
# ---------------------------------------------------------------------------

CHAT_PATH = "/api/chat"

# Requirement 7 AC2. An explicit allowlist from request path to
# `(file name, content type)`: three paths, three files, and no arithmetic
# between them. The file name is a constant in this table, never a value taken
# from the request, which is what makes path traversal impossible rather than
# merely filtered.
ASSET_ROUTES: Mapping[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}

# Where those three files are. First the packaged layout the Makefile produces
# (`agent/ui/` -- see that file's header and the template's), then the source
# checkout (`ui/` at the repository root, which is `parents[2]` from
# `src/agent/handler.py`). Resolved relative to this module's own file, because
# Lambda makes no promise about the working directory.
ASSET_CANDIDATES: tuple[Path, ...] = (
    Path(__file__).resolve().parent / "ui",
    Path(__file__).resolve().parents[2] / "ui",
)

_UI_DIR: Path = next(
    (path for path in ASSET_CANDIDATES if path.is_dir()), ASSET_CANDIDATES[0]
)

_JSON_HEADERS: Mapping[str, str] = {
    "Content-Type": "application/json; charset=utf-8",
    # The answer is specific to one repository at one moment and is never worth
    # replaying from a cache.
    "Cache-Control": "no-store",
}

# Revalidate rather than forbid: a UI change is a redeploy here, with no cache
# invalidation step to forget, so the browser has to ask whether its copy is
# still current.
_ASSET_CACHE_CONTROL = "no-cache"

_NOT_FOUND_BODY = (
    "Not found.\n\n"
    "This agent serves the chat page at / and answers questions at "
    "POST /api/chat.\n"
)


def ui_dir() -> Path:
    """The directory the three Chat_UI assets are served from.

    Exposed for `scripts/local_server.py` and for the tests that assert the
    packaged layout, so neither has to restate the candidate list.
    """
    return _UI_DIR


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChatRequest:
    """A validated `POST /api/chat` body. The design's Wire Format, parsed.

    `repo_url` is whatever the body carried, coerced to a string but not yet
    validated: `url_parse.parse_repo_url` is the authority on the form, and it
    runs inside the `validating_url` step so a rejection shows up in the step
    record rather than arriving with an empty one.

    `mode` is inferred from `messages` rather than read from a field, so a
    client cannot ask for an Orientation_Message while sending ten turns of
    history (design: Wire Format; Requirement 5 AC1).
    """

    repo_url: str
    messages: tuple[Turn, ...]
    mode: Mode


@dataclass(frozen=True)
class TurnOutcome:
    """What `run_turn` produced: the repository, its context, and the answer."""

    meta: RepoMeta
    context: RepoContext
    turn: TurnResult


def parse_request(body: bytes) -> ChatRequest:
    """Parse and sanitize a chat request body. Requirement 2 AC1.

    Args:
        body: The raw request bytes, after the size guard and after any
            base64 transport encoding has been undone.

    Returns:
        The parsed request, with `mode` inferred from the history.

    Raises:
        AgentError: `BAD_URL` for a body that is not a JSON object -- including
            an empty one. The design maps a malformed body to `BAD_URL` rather
            than to a generic failure because the realistic cause is a user
            pasting something odd, and that copy names the expected URL form
            and one valid example (Requirement 2 AC5).

    The history is sanitized here rather than downstream. `tokens.history_window`
    and `chat._wire` call `.get` on each turn, so a JSON array holding a string
    or a number would raise an `AttributeError` deep in the pipeline and surface
    as "something broke on my side" when the truthful answer is that the request
    was malformed. Anything that is not a `{"role": str, "content": str}` pair
    with content in it is dropped.
    """
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AgentError(ErrorCode.BAD_URL) from exc

    try:
        document = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise AgentError(ErrorCode.BAD_URL) from exc
    if not isinstance(document, Mapping):
        raise AgentError(ErrorCode.BAD_URL)

    raw_url = document.get("repo_url")
    messages = _turns(document.get("messages"))
    return ChatRequest(
        repo_url=raw_url if isinstance(raw_url, str) else "",
        messages=messages,
        mode=chat.infer_mode(messages),
    )


def _turns(raw: object) -> tuple[Turn, ...]:
    """The conversation history, keeping only well-formed turns."""
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return ()
    kept: list[Turn] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        role, content = item.get("role"), item.get("content")
        if not isinstance(role, str) or not isinstance(content, str):
            continue
        if not content.strip():
            continue
        kept.append({"role": role, "content": content})
    return tuple(kept)


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


def run_turn(
    request: ChatRequest,
    recorder: StepRecorder,
    deadline: Deadline,
    *,
    transport: Transport | None = None,
    bedrock: BedrockClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[float, float], float] = random.uniform,
) -> TurnOutcome:
    """Ingest the repository and answer one turn. Requirements 2, 3, 4, 5.

    Args:
        request: The parsed body.
        recorder: Times each step and keeps the record every response carries
            (Requirement 5 AC7).
        deadline: The remaining budget, already tightened to the mode. Passed
            through to `bedrock.converse`, which refuses an attempt it cannot
            finish.
        transport: GitHub transport override, for tests.
        bedrock: Bedrock client override, for tests.
        sleep: Backoff sleep, for tests.
        jitter: Backoff jitter source, for tests.

    Raises:
        AgentError: every failure in the pipeline. Nothing is caught here --
            `handler` owns the one boundary.

    Each `StepName` is recorded exactly once, in `STEP_ORDER`, because the
    Chat_UI renders the array as a checklist and a repeated name would render a
    repeated line. That is why the second selection pass lives inside
    `reading_files`: it depends on a fetched body, so it cannot run in
    `selecting_files`, and giving it a step of its own would invent a name
    `ui/app.js` does not know.

    No deadline check sits between the steps. Everything before the model call
    is bounded by `github_client`'s own connect and read timeouts, and the one
    operation that can overrun the budget is the inference -- which checks for
    itself and raises `DEADLINE` rather than starting an attempt that would
    answer after the user has gone.
    """
    with recorder.step(StepName.VALIDATING_URL):
        ref = parse_repo_url(request.repo_url)  # Requirement 2 AC2, AC5

    with recorder.step(StepName.FETCHING_METADATA):
        # Raises REPO_TOO_LARGE before a tree can be listed (Requirement 3 AC8).
        meta = get_metadata(ref, transport)

    with recorder.step(StepName.LISTING_FILES):
        # Raises REPO_TOO_MANY_FILES before a body can be read (3 AC9), and
        # reads the default branch and nothing else (2 AC4).
        entries = list_tree(ref, meta.default_branch, transport)

    with recorder.step(StepName.SELECTING_FILES):
        candidates = select(entries)  # Requirement 3 AC1, AC3

    with recorder.step(StepName.READING_FILES):
        bodies = dict(
            read_files(
                ref, meta.default_branch, [c.path for c in candidates], transport
            )
        )
        # `package.json`'s `main` names an entry point no name list can guess,
        # and it is only readable once the bodies are in hand -- hence a second
        # pass. `select` is pure, so re-running it over the same tree with the
        # resolved path is idempotent over the first pass.
        package_main = resolve_package_main(_as_text(bodies.get("package.json")))
        if package_main is not None:
            candidates = select(entries, package_main)
            # The new pass may admit a path the first one never asked for. One
            # more raw fetch is cheaper than the alternative, which is reporting
            # a file as unreadable when it was simply never requested -- a
            # statement the context would then make to the model.
            missing = [c.path for c in candidates if c.path not in bodies]
            if missing:
                bodies.update(read_files(ref, meta.default_branch, missing, transport))

    with recorder.step(StepName.BUILDING_CONTEXT):
        language = detect_language(  # Requirement 5 AC2
            meta, bodies, paths=[c.path for c in candidates]
        )
        if request.mode is Mode.ORIENTATION:
            context = build_orientation_context(
                meta, entries, candidates, bodies, detected_language=language
            )
        else:
            context = build_chat_context(
                meta,
                entries,
                candidates,
                bodies,
                detected_language=language,
                history_tokens=history_tokens(request.messages),
            )

    with recorder.step(StepName.ASKING_NOVA):
        turn = chat.run_turn(
            context,
            request.messages,
            deadline,
            bedrock=bedrock,
            sleep=sleep,
            jitter=jitter,
        )

    return TurnOutcome(meta=meta, context=context, turn=turn)


def _as_text(raw: bytes | None) -> str | None:
    """A fetched body as text, through the same binary check as everything else."""
    return None if raw is None else decode_text(raw)


# ---------------------------------------------------------------------------
# The entry point and the error boundary
# ---------------------------------------------------------------------------


def handler(
    event: Mapping[str, Any] | None,
    context: object = None,
    *,
    transport: Transport | None = None,
    bedrock: BedrockClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[float, float], float] = random.uniform,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Serve one Function URL request. Requirements 2 AC1, 6 AC4, 7 AC2, 8 AC1.

    Args:
        event: A Lambda Function URL event in API Gateway payload format 2.0.
            The method comes from `requestContext.http.method` and the path from
            `rawPath`; a base64-encoded body is decoded before it is measured.
        context: The Lambda context object. Unused -- the deadlines are this
            module's own, taken from `config.py`, because they implement a
            promise to the user rather than a reaction to the function timeout.
        transport: GitHub transport override, for tests.
        bedrock: Bedrock client override, for tests.
        sleep: Backoff sleep, for tests.
        jitter: Backoff jitter source, for tests.
        clock: Monotonic source for the deadline and the step timings, for
            tests.

    Returns:
        A Function URL response: `statusCode`, `headers`, `body`, and
        `isBase64Encoded`. Never raises.

    The structure is the design's Error Handling section verbatim: the body
    guard first, then parse, then run, with one `except AgentError`, one bare
    `except Exception`, and a `finally` that logs.
    """
    correlation_id = str(uuid.uuid4())
    recorder = StepRecorder(clock=clock)
    deadline = Deadline(started=clock(), budget_ms=SOFT_DEADLINE_MS, clock=clock)
    code: ErrorCode | None = None
    route = "- -"
    mode: Mode | None = None
    status = 500
    outcome: TurnOutcome | None = None

    try:
        method, path = _route(event)
        route = f"{method or '-'} {path or '-'}"

        if method == "GET" and path in ASSET_ROUTES:
            response = _asset(path)
            status = response["statusCode"]
            return response

        if method != "POST" or path != CHAT_PATH:
            status = 404
            return _not_found()

        # Requirement 8 AC1 -- first, before any outbound call. Bytes, not
        # characters: a multi-byte body would otherwise slip past a len() check.
        raw = _request_bytes(event)
        if len(raw) > MAX_BODY_BYTES:
            raise AgentError(ErrorCode.BODY_TOO_LARGE)

        request = parse_request(raw)
        mode = request.mode
        # Requirement 5 AC6 is tighter than Requirement 4 AC6, and which one
        # applies is only knowable now. `started` is preserved, so the
        # orientation budget runs from the start of the invocation.
        deadline = replace(deadline, budget_ms=_budget_ms(request.mode))

        outcome = run_turn(
            request,
            recorder,
            deadline,
            transport=transport,
            bedrock=bedrock,
            sleep=sleep,
            jitter=jitter,
        )
        status = 200
        return _ok(outcome, recorder, correlation_id)

    except AgentError as exc:
        code = exc.code
        status, body = render(exc)
        return _response(status, body, recorder, correlation_id, mode)

    except Exception:
        # Requirement 6 AC4, AC5 -- generic to the user, full detail to the log.
        # The only bare `except` in the codebase.
        code = ErrorCode.INTERNAL
        log.exception("unhandled", extra={"correlation_id": correlation_id})
        status, body = render(AgentError(ErrorCode.INTERNAL))
        return _response(status, body, recorder, correlation_id, mode)

    finally:
        # One line per invocation, whatever happened. `default=str` means a
        # value that surprises `json.dumps` degrades to its repr instead of
        # raising out of a `finally` and replacing the response with a crash.
        log.info(
            json.dumps(
                _summary(correlation_id, route, status, code, mode, recorder, outcome),
                default=str,
            )
        )


def _budget_ms(mode: Mode) -> int:
    """The soft deadline for a mode. Requirements 4 AC6, 5 AC6."""
    return ORIENTATION_DEADLINE_MS if mode is Mode.ORIENTATION else SOFT_DEADLINE_MS


# ---------------------------------------------------------------------------
# Reading the event (API Gateway payload format 2.0)
# ---------------------------------------------------------------------------


def _route(event: Mapping[str, Any] | None) -> tuple[str, str]:
    """The method and path of a payload-format-2.0 event.

    A Function URL always delivers this shape, and `scripts/local_server.py`
    synthesizes it, so there is one shape to read rather than two. Every lookup
    tolerates a missing or wrongly typed key and yields `("", "")`, which
    matches no route and 404s -- an event this function cannot read is not an
    event it should guess at.

    `rawPath` carries no query string (that is `rawQueryString`), so no
    splitting is needed. An empty path is normalized to `/`, which is what a
    request for the site root looks like if a proxy ever drops it.
    """
    if not isinstance(event, Mapping):
        return "", ""
    request_context = event.get("requestContext")
    http = request_context.get("http") if isinstance(request_context, Mapping) else None
    raw_method = http.get("method") if isinstance(http, Mapping) else None
    method = raw_method.strip().upper() if isinstance(raw_method, str) else ""
    raw_path = event.get("rawPath")
    path = raw_path if isinstance(raw_path, str) and raw_path else "/"
    return method, path


def _request_bytes(event: Mapping[str, Any]) -> bytes:
    """The request body as bytes, undoing base64 transport encoding.

    The size guard is stated in bytes (Requirement 8 AC1), so the measurement
    has to happen on the body itself rather than on a base64 envelope that is
    a third larger than its payload -- that would reject a legitimate 6 KB
    conversation.

    `validate=True` is deliberate. A client that sets `isBase64Encoded` on a
    plain JSON body raises here and falls through to measuring the string's own
    bytes, which is the honest size; decoding loosely would discard every
    character outside the base64 alphabet and under-report a body that should
    have been refused.
    """
    raw = event.get("body")
    if raw is None:
        return b""
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    if not isinstance(raw, str):
        return str(raw).encode("utf-8")
    if event.get("isBase64Encoded"):
        try:
            return base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            return raw.encode("utf-8")
    return raw.encode("utf-8")


# ---------------------------------------------------------------------------
# The Chat_UI assets (Requirement 7 AC2)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=len(ASSET_ROUTES))
def _load_asset(name: str) -> str | None:
    """One asset's text, or `None` when it is not in the package.

    Cached, so a warm container serves the three files from memory. Read lazily
    rather than at import because a cold start pays for whatever it imports,
    and because an asset that does not exist yet -- `app.js` until task 8.2 --
    should 404 rather than make the module unimportable.

    `name` only ever arrives from the values in `ASSET_ROUTES`, so this never
    sees a path from a request.
    """
    try:
        return (_UI_DIR / name).read_text("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def reset_assets() -> None:
    """Drop the cached asset text.

    For `scripts/local_server.py`, which calls it per request so an edit to the
    UI shows on a reload, and for tests that point `_UI_DIR` somewhere else.
    """
    _load_asset.cache_clear()


def _asset(path: str) -> dict[str, Any]:
    """Serve one allowlisted asset. Requirement 7 AC2."""
    name, content_type = ASSET_ROUTES[path]
    text = _load_asset(name)
    if text is None:
        return _not_found()
    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": content_type,
            "Cache-Control": _ASSET_CACHE_CONTROL,
        },
        "body": text,
        "isBase64Encoded": False,
    }


def _not_found() -> dict[str, Any]:
    """404, plain text, naming the two paths that do exist.

    The requested path is deliberately not echoed: reflecting request input
    into a response body is a habit worth not having on a public endpoint, and
    the two real routes are more useful to the reader anyway.
    """
    return {
        "statusCode": 404,
        "headers": {
            "Content-Type": "text/plain; charset=utf-8",
            "Cache-Control": "no-store",
        },
        "body": _NOT_FOUND_BODY,
        "isBase64Encoded": False,
    }


# ---------------------------------------------------------------------------
# The response envelope (design: Wire Format)
# ---------------------------------------------------------------------------


def _ok(
    outcome: TurnOutcome, recorder: StepRecorder, correlation_id: str
) -> dict[str, Any]:
    """The success envelope. Requirement 2 AC1, 4 AC5, 5 AC7."""
    context = outcome.context
    return _json(
        200,
        {
            "message": outcome.turn.message,
            "mode": outcome.turn.mode.value,
            "repo": {
                "slug": context.meta.slug,
                "default_branch": context.meta.default_branch,
                "detected_language": context.detected_language,
                # The files whose contents actually reached the model, which is
                # what "files I read" means to the person reading the
                # disclosure. A selected path with no body is in the context's
                # manifest and is named there as such, but claiming it here
                # would overstate what the answer is grounded in.
                "ingested_paths": [
                    block.path for block in context.files if block.content
                ],
            },
            "steps": _steps(recorder),
            "correlation_id": correlation_id,
        },
    )


def _response(
    status: int,
    body: Mapping[str, Any],
    recorder: StepRecorder,
    correlation_id: str,
    mode: Mode | None,
) -> dict[str, Any]:
    """The failure envelope: `errors.render`'s body plus steps and the id.

    Same shape as the success envelope with `error` in place of `message` and
    `repo`, so the Chat_UI renders a failure as a chat message through the same
    path as an answer (Requirement 6 AC1). `mode` appears only when the request
    got far enough to infer one.
    """
    payload: dict[str, Any] = dict(body)
    if mode is not None:
        payload["mode"] = mode.value
    payload["steps"] = _steps(recorder)
    payload["correlation_id"] = correlation_id
    return _json(status, payload)


def _json(status: int, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": dict(_JSON_HEADERS),
        "body": json.dumps(payload),
        "isBase64Encoded": False,
    }


def _steps(recorder: StepRecorder) -> list[dict[str, str | int]]:
    """The step array, completed with the steps that never ran.

    A failure response carries the whole checklist -- the steps that succeeded,
    the one that failed, and the rest as `skipped` -- so the Chat_UI can show
    how far the run got instead of a progress list that stops mid-sentence
    (Requirement 5 AC7, and the design's failure example). On the success path
    every step is already recorded and this adds nothing.
    """
    recorded = {record.name for record in recorder.steps}
    for name in STEP_ORDER:
        if name not in recorded:
            recorder.skip(name)
    return recorder.as_dicts()


# ---------------------------------------------------------------------------
# The log line (Requirements 6 AC5, 12 AC3)
# ---------------------------------------------------------------------------


def _summary(
    correlation_id: str,
    route: str,
    status: int,
    code: ErrorCode | None,
    mode: Mode | None,
    recorder: StepRecorder,
    outcome: TurnOutcome | None,
) -> dict[str, Any]:
    """The one structured line per invocation.

    Carries what cannot be reconstructed afterwards: the correlation id the
    response also returned, the per-step durations the design's latency table
    is waiting on, and -- when a model call happened -- the token estimate
    against the count Bedrock actually charged, which is the only calibration
    signal `CHARS_PER_TOKEN` will ever get.

    Primitives only. This runs inside `finally`, so a value that could not be
    serialized would turn a clean response into a crash.
    """
    record: dict[str, Any] = {
        "event": "request",
        "correlation_id": correlation_id,
        "route": route,
        "status": status,
        "error_code": code.value if code is not None else None,
        "mode": mode.value if mode is not None else None,
        "total_ms": recorder.total_ms,
        "steps": {record.name.value: record.ms for record in recorder.steps},
    }
    if outcome is None:
        return record

    turn = outcome.turn
    record.update(
        {
            "repo": outcome.meta.slug,
            "default_branch": outcome.meta.default_branch,
            "detected_language": outcome.context.detected_language,
            "ingested": len(outcome.context.files),
            "unread": len(outcome.context.unread_paths),
            "history_turns": turn.history_turns,
            "attempts": turn.attempts,
            "repairs": list(turn.repairs),
            "tokens": log_calibration(
                turn.estimated_tokens, turn.input_tokens, turn.output_tokens
            ),
        }
    )
    return record
