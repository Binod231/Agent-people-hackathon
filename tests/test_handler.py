"""Unit tests for `agent.handler`. Requirements 2 AC1, 6 AC4, 7 AC2, 8 AC1.

The handler is the one place where every other module meets, so these tests are
about the seams rather than the pipeline -- the pipeline's own guarantees are
asserted in the eleven modules' own test files.

Six groups:

- **The body guard short-circuits** (Requirement 8 AC1). An 8,193-byte body is
  refused with zero GitHub requests and zero Bedrock calls, an 8,192-byte body
  is not, and the measurement is on bytes rather than characters -- a
  multi-byte body under the character limit and over the byte limit is refused.
  The oversize-repository and too-many-files guards are asserted the same way:
  each costs the metered call that produced the measurement and nothing more.
- **Every outcome is a complete envelope.** Success, an `AgentError`, and a
  bare exception each return exactly one of `message` or `error`, a non-empty
  `steps` array covering the whole checklist, and a correlation id. The
  exception case also asserts that nothing from the exception reaches the user
  (Requirement 6 AC6).
- **The asset routes** serve the three allowlisted files with the content types
  the design's routing table names, 404 when a file is not in the package, and
  cannot be talked into serving anything else -- a traversal attempt is not a
  key in the table, and the sentinel file one directory up stays unreachable.
- **Anything else 404s**, including the right path with the wrong method.
- **Statelessness** (Requirement 12 AC4). Two successive invocations each
  re-issue their own metadata, tree, and raw requests; nothing is served from a
  cache left behind by the first.
- **The deadline comes from the mode** (Requirements 4 AC6, 5 AC6). The same
  elapsed time that leaves an orientation with no room for an inference leaves
  a follow-up turn with plenty.

`FakeGitHub` answers every GitHub request, `FakeBedrock` every model call, and
the autouse fixture makes building a real client an error, so nothing here
opens a socket or needs AWS credentials.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from agent import bedrock, handler as handler_module
from agent.config import (
    MAX_BODY_BYTES,
    MAX_REPO_SIZE_KB,
    MAX_TREE_FILES,
    MIN_INFERENCE_SECONDS,
    ORIENTATION_DEADLINE_MS,
    SOFT_DEADLINE_MS,
)
from agent.errors import ErrorCode
from agent.handler import ASSET_ROUTES, CHAT_PATH, handler
from agent.steps import STEP_ORDER
from conftest import FakeBedrock, FakeClock, FakeGitHub, RepoFixture, load_repo_fixture

ANSWER = (
    "This is the `pallets/flask` repository, a Python web framework.\n\n"
    "Start with `README.md`, then `pyproject.toml`, then `src/flask/app.py`.\n\n"
    "- What runs the tests?\n"
    "- Where does a request enter the code?\n"
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


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


@pytest.fixture
def assets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A stand-in package directory holding the three Chat_UI files.

    Pointed at by the handler for the duration of one test, so the asset
    assertions hold whether or not `ui/app.js` has been written yet (task 8.2).
    The sentinel one level up is what the traversal test tries to reach.
    """
    (tmp_path / "sentinel.txt").write_text("SENTINEL-NOT-SERVABLE\n", "utf-8")
    directory = tmp_path / "ui"
    directory.mkdir()
    (directory / "index.html").write_text(
        "<!doctype html><title>guide</title>", "utf-8"
    )
    (directory / "styles.css").write_text(":root { color-scheme: dark; }", "utf-8")
    (directory / "app.js").write_text("export const ready = true;\n", "utf-8")
    monkeypatch.setattr(handler_module, "_UI_DIR", directory)
    handler_module.reset_assets()
    yield directory
    handler_module.reset_assets()


def get_event(path: str) -> dict[str, Any]:
    """A payload-format-2.0 GET event, as a Function URL delivers it."""
    return {
        "version": "2.0",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"accept": "*/*"},
        "requestContext": {"http": {"method": "GET", "path": path}},
        "body": None,
        "isBase64Encoded": False,
    }


def post_event(
    body: str | None,
    *,
    path: str = CHAT_PATH,
    method: str = "POST",
    encoded: bool = False,
) -> dict[str, Any]:
    """A payload-format-2.0 POST event, optionally base64-encoded."""
    payload = body
    if encoded and body is not None:
        payload = base64.b64encode(body.encode("utf-8")).decode("ascii")
    return {
        "version": "2.0",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"content-type": "application/json"},
        "requestContext": {"http": {"method": method, "path": path}},
        "body": payload,
        "isBase64Encoded": encoded,
    }


def chat_body(
    fixture: RepoFixture, messages: list[dict[str, str]] | None = None
) -> str:
    document: dict[str, Any] = {"repo_url": f"https://github.com/{fixture.meta.slug}"}
    if messages is not None:
        document["messages"] = messages
    return json.dumps(document)


def padded_body(fixture: RepoFixture, total_bytes: int, filler: str = "x") -> str:
    """A chat body whose encoded size is exactly `total_bytes`.

    Built by growing one user message, so the padding is a legitimate part of
    the request rather than a key the parser would ignore.

    `ensure_ascii=False`, which is what a browser's `JSON.stringify` plus
    `fetch` produces: a non-ASCII character goes on the wire as its UTF-8 bytes
    rather than as a six-byte `\\uXXXX` escape. That is the whole point of the
    multi-byte case -- escaped, every character would be one byte again.
    """
    template: dict[str, Any] = {
        "repo_url": f"https://github.com/{fixture.meta.slug}",
        "messages": [{"role": "user", "content": ""}],
    }
    overhead = len(json.dumps(template, ensure_ascii=False).encode("utf-8"))
    width = len(filler.encode("utf-8"))
    count, remainder = divmod(total_bytes - overhead, width)
    template["messages"][0]["content"] = filler * count + "x" * remainder
    body = json.dumps(template, ensure_ascii=False)
    assert len(body.encode("utf-8")) == total_bytes
    return body


def clock_for(elapsed: float = 0.0) -> FakeClock:
    """A monotonic source that starts at zero and is advanced by the test."""
    clock = FakeClock()
    clock.advance(elapsed)
    return clock


def invoke(
    event: Mapping[str, Any],
    *,
    github: FakeGitHub | None = None,
    model: FakeBedrock | None = None,
    clock: FakeClock | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Call the handler and return `(response, decoded body)`."""
    response = handler(
        event,
        None,
        transport=github,
        bedrock=model,
        sleep=lambda _seconds: None,
        jitter=lambda _low, _high: 0.0,
        clock=clock or FakeClock(),
    )
    raw = response.get("body")
    document = json.loads(raw) if isinstance(raw, str) and raw.startswith("{") else {}
    return response, document


def step_names(document: Mapping[str, Any]) -> list[str]:
    return [step["name"] for step in document["steps"]]


def statuses(document: Mapping[str, Any]) -> dict[str, str]:
    return {step["name"]: step["status"] for step in document["steps"]}


# ---------------------------------------------------------------------------
# The body guard (Requirement 8 AC1)
# ---------------------------------------------------------------------------


def test_an_oversize_body_is_refused_before_any_outbound_call(
    flask_repo: RepoFixture,
) -> None:
    github, model = flask_repo.github(), FakeBedrock(ANSWER)
    body = padded_body(flask_repo, MAX_BODY_BYTES + 1)

    response, document = invoke(post_event(body), github=github, model=model)

    assert response["statusCode"] == 413
    assert document["error"]["code"] == ErrorCode.BODY_TOO_LARGE.value
    assert github.calls == [], "the guard must run before any GitHub request"
    assert model.attempts == 0, "the guard must run before any model call"


def test_a_body_at_the_limit_is_accepted(flask_repo: RepoFixture) -> None:
    github, model = flask_repo.github(), FakeBedrock(ANSWER)
    body = padded_body(flask_repo, MAX_BODY_BYTES)

    response, document = invoke(post_event(body), github=github, model=model)

    assert response["statusCode"] == 200
    assert document["message"]
    assert model.attempts == 1


def test_the_guard_measures_bytes_not_characters(flask_repo: RepoFixture) -> None:
    """A body inside the limit in characters and over it in bytes is refused."""
    github, model = flask_repo.github(), FakeBedrock(ANSWER)
    # "é" is two bytes in UTF-8, so this body is about 4,200 characters long
    # and 8,193 bytes long. A `len()` check on the decoded string would pass it.
    body = padded_body(flask_repo, MAX_BODY_BYTES + 1, filler="é")
    assert len(body) < MAX_BODY_BYTES, "the case is only interesting under the cap"

    response, document = invoke(post_event(body), github=github, model=model)

    assert response["statusCode"] == 413
    assert document["error"]["code"] == ErrorCode.BODY_TOO_LARGE.value
    assert github.calls == []
    assert model.attempts == 0


def test_a_base64_body_is_measured_after_decoding(flask_repo: RepoFixture) -> None:
    """Base64 inflates by a third; the guard must not measure the envelope."""
    github, model = flask_repo.github(), FakeBedrock(ANSWER)
    body = padded_body(flask_repo, MAX_BODY_BYTES)
    encoded = base64.b64encode(body.encode("utf-8")).decode("ascii")
    assert len(encoded) > MAX_BODY_BYTES, "the case is only interesting when it is"

    response, document = invoke(
        post_event(body, encoded=True), github=github, model=model
    )

    assert response["statusCode"] == 200
    assert document["message"]
    assert len(encoded.encode("utf-8")) > MAX_BODY_BYTES


def test_an_oversize_repository_short_circuits(flask_repo: RepoFixture) -> None:
    """Requirement 3 AC8: measured once, then nothing else is spent."""
    github = flask_repo.github(metadata={"size": MAX_REPO_SIZE_KB + 1})
    model = FakeBedrock(ANSWER)

    response, document = invoke(
        post_event(chat_body(flask_repo)), github=github, model=model
    )

    assert response["statusCode"] == 422
    assert document["error"]["code"] == ErrorCode.REPO_TOO_LARGE.value
    assert len(github.api_calls) == 1, "the tree must not be listed"
    assert github.raw_calls == [], "no file body may be fetched"
    assert model.attempts == 0


@pytest.mark.parametrize("kind", ["truncated", "over_the_cap"])
def test_a_repository_with_too_many_files_short_circuits(
    flask_repo: RepoFixture, kind: str
) -> None:
    """Requirement 3 AC9, both ways the guard can fire."""
    if kind == "truncated":
        document_tree: dict[str, Any] = {"truncated": True, "tree": []}
    else:
        document_tree = {
            "truncated": False,
            "tree": [
                {"path": f"src/file_{index}.py", "type": "blob", "size": 10}
                for index in range(MAX_TREE_FILES + 1)
            ],
        }
    github = flask_repo.github(document=document_tree)
    model = FakeBedrock(ANSWER)

    response, body = invoke(
        post_event(chat_body(flask_repo)), github=github, model=model
    )

    assert response["statusCode"] == 422
    assert body["error"]["code"] == ErrorCode.REPO_TOO_MANY_FILES.value
    assert github.raw_calls == [], "no file body may be fetched"
    assert model.attempts == 0


# ---------------------------------------------------------------------------
# Every outcome is a complete envelope
# ---------------------------------------------------------------------------


def test_a_successful_orientation_returns_a_complete_envelope(
    repo_fixture: RepoFixture,
) -> None:
    github, model = repo_fixture.github(), FakeBedrock(ANSWER)

    response, document = invoke(
        post_event(chat_body(repo_fixture)), github=github, model=model
    )

    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"] == "application/json; charset=utf-8"
    assert response["isBase64Encoded"] is False

    assert isinstance(document["message"], str) and document["message"].strip()
    assert "error" not in document
    assert document["mode"] == "orientation"

    repo = document["repo"]
    assert repo["slug"] == repo_fixture.meta.slug
    assert repo["default_branch"] == repo_fixture.meta.default_branch
    assert set(repo) == {
        "slug",
        "default_branch",
        "detected_language",
        "ingested_paths",
    }
    assert repo["ingested_paths"], "an orientation reads at least one file"

    assert step_names(document) == [name.value for name in STEP_ORDER]
    assert set(statuses(document).values()) == {"ok"}
    assert uuid.UUID(document["correlation_id"])


def test_a_follow_up_turn_is_answered_in_chat_mode(flask_repo: RepoFixture) -> None:
    github, model = flask_repo.github(), FakeBedrock("Tests run through pytest.")
    history = [
        {"role": "user", "content": "where do I start?"},
        {"role": "assistant", "content": ANSWER},
        {"role": "user", "content": "what runs the tests?"},
    ]

    response, document = invoke(
        post_event(chat_body(flask_repo, history)), github=github, model=model
    )

    assert response["statusCode"] == 200
    assert document["mode"] == "chat"
    assert document["message"].strip()
    assert step_names(document) == [name.value for name in STEP_ORDER]


def test_an_agent_error_returns_a_complete_envelope(flask_repo: RepoFixture) -> None:
    github, model = flask_repo.github(), FakeBedrock(ANSWER)
    body = json.dumps({"repo_url": "https://github.com/pallets/flask/tree/main/src"})

    response, document = invoke(post_event(body), github=github, model=model)

    assert response["statusCode"] == 400
    assert document["error"]["code"] == ErrorCode.BAD_URL.value
    assert document["error"]["message"].strip()
    assert set(document["error"]) == {"code", "message"}, "no retry_after_seconds"
    assert "message" not in document and "repo" not in document

    # The whole checklist, so the Chat_UI can show how far the run got.
    assert step_names(document) == [name.value for name in STEP_ORDER]
    marks = statuses(document)
    assert marks["validating_url"] == "error"
    assert marks["fetching_metadata"] == "skipped"
    assert marks["asking_nova"] == "skipped"
    assert uuid.UUID(document["correlation_id"])
    assert github.calls == [] and model.attempts == 0


@pytest.mark.parametrize(
    "body",
    ["", "not json at all", "[1, 2, 3]", '{"repo_url": 7}', '{"messages": []}'],
    ids=["empty", "garbage", "array", "wrong-type", "no-url"],
)
def test_a_malformed_body_is_a_bad_url_message(
    flask_repo: RepoFixture, body: str
) -> None:
    github, model = flask_repo.github(), FakeBedrock(ANSWER)

    response, document = invoke(post_event(body), github=github, model=model)

    assert response["statusCode"] == 400
    assert document["error"]["code"] == ErrorCode.BAD_URL.value
    assert document["steps"], "steps are present even when nothing ran"
    assert github.calls == [] and model.attempts == 0


def test_a_bare_exception_becomes_a_generic_internal_message(
    flask_repo: RepoFixture,
) -> None:
    """Requirement 6 AC4, AC6: 500, generic copy, nothing leaked."""
    secret = "arn:aws:iam::123456789012:role/AgentFunctionRole AKIAIOSFODNN7EXAMPLE"

    class ExplodingTransport:
        def request(self, method: str, url: str, **kwargs: object) -> object:
            raise RuntimeError(secret)

    model = FakeBedrock(ANSWER)
    response, document = invoke(
        post_event(chat_body(flask_repo)),
        github=ExplodingTransport(),  # type: ignore[arg-type]
        model=model,
    )

    assert response["statusCode"] == 500
    assert document["error"]["code"] == ErrorCode.INTERNAL.value
    assert "message" not in document and "repo" not in document
    assert step_names(document) == [name.value for name in STEP_ORDER]
    assert statuses(document)["fetching_metadata"] == "error"

    rendered = response["body"]
    for leak in (secret, "123456789012", "AKIA", "RuntimeError", "Traceback"):
        assert leak not in rendered, f"{leak!r} must not reach the user"
    assert model.attempts == 0


def test_a_correlation_id_is_unique_per_invocation(flask_repo: RepoFixture) -> None:
    ids = set()
    for _ in range(3):
        _, document = invoke(
            post_event(chat_body(flask_repo)),
            github=flask_repo.github(),
            model=FakeBedrock(ANSWER),
        )
        ids.add(document["correlation_id"])
    assert len(ids) == 3


# ---------------------------------------------------------------------------
# The Chat_UI assets (Requirement 7 AC2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/", "text/html; charset=utf-8"),
        ("/styles.css", "text/css; charset=utf-8"),
        ("/app.js", "text/javascript; charset=utf-8"),
    ],
)
def test_each_asset_route_returns_its_content_type(
    assets: Path, path: str, content_type: str
) -> None:
    response = handler(get_event(path))

    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"] == content_type
    assert response["isBase64Encoded"] is False

    name, _ = ASSET_ROUTES[path]
    assert response["body"] == (assets / name).read_text("utf-8")


def test_an_asset_missing_from_the_package_is_a_404(assets: Path) -> None:
    (assets / "app.js").unlink()
    handler_module.reset_assets()

    response = handler(get_event("/app.js"))

    assert response["statusCode"] == 404
    assert response["headers"]["Content-Type"] == "text/plain; charset=utf-8"


def test_the_asset_candidates_match_the_packaged_layout() -> None:
    """The Makefile puts `ui/` inside the package; a checkout has it at the root."""
    module = Path(handler_module.__file__).resolve()
    assert handler_module.ASSET_CANDIDATES == (
        module.parent / "ui",
        module.parents[2] / "ui",
    )
    assert handler_module.ui_dir() in handler_module.ASSET_CANDIDATES


@pytest.mark.parametrize(
    "path",
    [
        "/../sentinel.txt",
        "/../../sentinel.txt",
        "/..%2fsentinel.txt",
        "/ui/../sentinel.txt",
        "/styles.css/../../sentinel.txt",
        "//sentinel.txt",
        "/./styles.css",
        "/index.html",
        "/handler.py",
        "/config.json",
    ],
)
def test_a_path_outside_the_allowlist_is_refused(assets: Path, path: str) -> None:
    """The file name comes from the table, never from the request."""
    response = handler(get_event(path))

    assert response["statusCode"] == 404
    assert "SENTINEL" not in response["body"]
    assert "Not found." in response["body"]


# ---------------------------------------------------------------------------
# Anything else 404s
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/chat"),
        ("POST", "/"),
        ("POST", "/api/chats"),
        ("PUT", "/api/chat"),
        ("DELETE", "/api/chat"),
        ("GET", "/favicon.ico"),
        ("GET", "/robots.txt"),
    ],
)
def test_an_unknown_route_is_a_404(assets: Path, method: str, path: str) -> None:
    response = handler(post_event(None, path=path, method=method))

    assert response["statusCode"] == 404
    assert response["headers"]["Content-Type"] == "text/plain; charset=utf-8"


def test_an_unreadable_event_is_a_404_rather_than_a_crash() -> None:
    for event in (None, {}, {"rawPath": "/"}, {"requestContext": {}}):
        response = handler(event)  # type: ignore[arg-type]
        assert response["statusCode"] == 404


# ---------------------------------------------------------------------------
# Statelessness (Requirement 12 AC4)
# ---------------------------------------------------------------------------


def test_each_invocation_reissues_its_own_github_requests(
    flask_repo: RepoFixture,
) -> None:
    """Requirement 12 AC4: read at request time, in each session."""
    first, second = flask_repo.github(), flask_repo.github()
    body = post_event(chat_body(flask_repo))

    one, _ = invoke(body, github=first, model=FakeBedrock(ANSWER))
    two, _ = invoke(body, github=second, model=FakeBedrock(ANSWER))

    assert one["statusCode"] == 200 and two["statusCode"] == 200
    assert len(first.api_calls) == len(second.api_calls) >= 2
    assert sorted(first.fetched_paths()) == sorted(second.fetched_paths())
    assert first.fetched_paths(), "file bodies are read on every invocation"


def test_a_shared_transport_sees_both_invocations(flask_repo: RepoFixture) -> None:
    """The same assertion from the other side: nothing is cached in the module."""
    github = flask_repo.github()
    body = post_event(chat_body(flask_repo))

    invoke(body, github=github, model=FakeBedrock(ANSWER))
    after_one = len(github.calls)
    invoke(body, github=github, model=FakeBedrock(ANSWER))

    assert len(github.calls) == 2 * after_one


# ---------------------------------------------------------------------------
# The deadline comes from the mode (Requirements 4 AC6, 5 AC6)
# ---------------------------------------------------------------------------


class SlowGitHub:
    """A transport that spends real budget on each metered call.

    Only the two `api.github.com` calls advance the clock: they happen on the
    calling thread, where `read_files`' eight workers would race each other for
    the counter.
    """

    def __init__(self, inner: FakeGitHub, clock: FakeClock, seconds: float) -> None:
        self.inner = inner
        self.clock = clock
        self.seconds = seconds

    def request(self, method: str, url: str, **kwargs: object) -> object:
        if url.startswith("https://api.github.com/"):
            self.clock.advance(self.seconds)
        return self.inner.request(method, url, **kwargs)  # type: ignore[arg-type]


def test_an_orientation_runs_on_the_tighter_deadline(flask_repo: RepoFixture) -> None:
    """The same elapsed time fits a chat turn and not an orientation.

    13 s of ingestion leaves 5 s of the 18 s orientation budget, which is less
    than the window an inference needs, and 13 s of the 26 s chat budget, which
    is more.
    """
    spent = 6.5  # per metered call, twice
    assert ORIENTATION_DEADLINE_MS / 1000 - 2 * spent < MIN_INFERENCE_SECONDS
    assert SOFT_DEADLINE_MS / 1000 - 2 * spent > MIN_INFERENCE_SECONDS

    clock = FakeClock()
    model = FakeBedrock(ANSWER)
    response, document = invoke(
        post_event(chat_body(flask_repo)),
        github=SlowGitHub(flask_repo.github(), clock, spent),  # type: ignore[arg-type]
        model=model,
        clock=clock,
    )

    assert response["statusCode"] == 504
    assert document["error"]["code"] == ErrorCode.DEADLINE.value
    assert document["mode"] == "orientation"
    assert model.attempts == 0, "an inference that cannot finish is not started"

    clock = FakeClock()
    model = FakeBedrock("Tests run through pytest.")
    history = [{"role": "user", "content": "what runs the tests?"}]
    response, document = invoke(
        post_event(chat_body(flask_repo, history)),
        github=SlowGitHub(flask_repo.github(), clock, spent),  # type: ignore[arg-type]
        model=model,
        clock=clock,
    )

    assert response["statusCode"] == 200
    assert document["mode"] == "chat"
    assert model.attempts == 1


# ---------------------------------------------------------------------------
# parse_request
# ---------------------------------------------------------------------------


def test_malformed_history_entries_are_dropped_rather_than_crashing() -> None:
    """A JSON array of anything must not reach `turn.get` downstream."""
    body = json.dumps(
        {
            "repo_url": "https://github.com/pallets/flask",
            "messages": [
                "a bare string",
                42,
                None,
                [],
                {"role": "user"},
                {"content": "no role"},
                {"role": "user", "content": "   "},
                {"role": "user", "content": "the only real turn"},
            ],
        }
    ).encode("utf-8")

    request = handler_module.parse_request(body)

    assert request.messages == ({"role": "user", "content": "the only real turn"},)
    assert request.mode.value == "chat"


def test_an_absent_history_means_orientation() -> None:
    body = json.dumps({"repo_url": "https://github.com/pallets/flask"}).encode("utf-8")
    assert handler_module.parse_request(body).mode.value == "orientation"

    body = json.dumps(
        {"repo_url": "https://github.com/pallets/flask", "messages": []}
    ).encode("utf-8")
    assert handler_module.parse_request(body).mode.value == "orientation"


def test_the_ingested_paths_are_the_files_the_model_could_read(
    flask_repo: RepoFixture,
) -> None:
    """A path whose fetch failed is reported as unread, not as ingested."""
    served = dict(flask_repo.contents)
    served.pop("pyproject.toml")
    github = flask_repo.github(contents=served)

    _, document = invoke(
        post_event(chat_body(flask_repo)), github=github, model=FakeBedrock(ANSWER)
    )

    paths = document["repo"]["ingested_paths"]
    assert paths, "the readable files are still ingested"
    assert "pyproject.toml" not in paths
    assert set(paths) <= set(served)


def test_the_fixtures_are_the_three_recorded_repositories() -> None:
    """A guard on the fixtures these tests lean on, not on the handler."""
    for name in ("flask", "gin", "express"):
        fixture = load_repo_fixture(name)
        assert fixture.blob_paths
        assert fixture.contents
