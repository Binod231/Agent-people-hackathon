"""Shared test configuration, test doubles, and recorded repository fixtures.

Puts `src/` on the import path so tests import the Lambda package as `agent.*`,
matching how it is imported at runtime from the deployment bundle. The
repository root goes on too, so `preflight/preflight.py` -- which is a runnable
script rather than part of the deployed package -- imports as
`preflight.preflight` regardless of how pytest was invoked.

Two pieces of shared infrastructure live here:

- **`FakeGitHub`** -- a recording transport satisfying the `Transport` protocol
  in `agent.github_client`. It answers metadata, tree, and raw-content requests
  from static data and records every outbound URL and header. That recording is
  what lets Property 2 assert "no `Authorization` header, ever, and no
  reference other than the default branch" and lets Property 9 assert "zero
  GitHub requests happened".
- **The three recorded trees** -- Flask (Python), Gin (Go), and Express
  (JavaScript), loaded from `tests/fixtures/tree_{name}.json` and exposed as
  the `flask_repo`, `gin_repo`, and `express_repo` fixtures plus the
  parametrized `repo_fixture`, which runs a test once per language.

The fixture JSON is hand-recorded from each public repository's layout and
trimmed to the shape the pipeline exercises; sizes are representative rather
than byte-exact, and the per-entry `mode`, `sha`, and `url` fields GitHub
returns are dropped because the client never reads them. Each file states its
own provenance in a `_provenance` key.

`FakeGitHub`, `RepoFixture`, and `load_repo_fixture` are also importable
directly (`from conftest import FakeGitHub`), which is how property tests reach
them: `@given` cannot take a function-scoped pytest fixture, so a property that
builds one double per generated example has to import the class.

Three more doubles serve the model call and the deadline guard:

- **`FakeBedrock`** -- a scripted stand-in for the `bedrock-runtime` client. It
  answers `converse` from a script of replies and exceptions and records every
  request, which is what lets `test_bedrock.py` assert the attempt count, the
  inference configuration, and the error mapping without a network.
- **`FakeClock`** -- a monotonic counter under test control, so a `Deadline`
  can be driven to its edge instead of waiting 26 real seconds.
- **`RecordingSleep`** -- a `time.sleep` stand-in that records each delay and
  advances a `FakeClock` by it, so backoff is measured rather than waited for
  and the deadline sees the time the backoff spent.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.parse
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
for _path in (SRC, ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from agent.models import RepoMeta, RepoRef, TreeEntry  # noqa: E402

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"

__all__ = [
    "API",
    "RAW",
    "REPO_FIXTURE_NAMES",
    "FakeBedrock",
    "FakeClock",
    "FakeGitHub",
    "FakeResponse",
    "RecordedCall",
    "RecordingSleep",
    "RepoFixture",
    "load_repo_fixture",
]


# ---------------------------------------------------------------------------
# The recording transport
# ---------------------------------------------------------------------------


class FakeResponse:
    """The slice of a response `github_client` reads: status, data, headers."""

    def __init__(
        self, status: int, data: bytes = b"", headers: Mapping[str, str] | None = None
    ) -> None:
        self.status = status
        self.data = data
        self.headers: Mapping[str, str] = dict(headers or {})


@dataclass(frozen=True)
class RecordedCall:
    """One outbound request, captured verbatim.

    `headers` is a read-only copy taken at call time, so a later mutation of the
    caller's mapping cannot rewrite history -- which matters, because the
    headers are the evidence for Requirement 2 AC3.
    """

    method: str
    url: str
    headers: Mapping[str, str]

    @property
    def parts(self) -> urllib.parse.SplitResult:
        return urllib.parse.urlsplit(self.url)

    @property
    def is_api(self) -> bool:
        return self.url.startswith(f"{API}/")

    @property
    def is_raw(self) -> bool:
        return self.url.startswith(f"{RAW}/")

    @property
    def header_names(self) -> frozenset[str]:
        """Header names, lowercased, for case-insensitive assertions."""
        return frozenset(name.lower() for name in self.headers)


class FakeGitHub:
    """A recording stand-in for `urllib3.PoolManager`. No sockets, no network.

    Satisfies `agent.github_client.Transport`, so it is passed as the optional
    `transport` argument to `get_metadata`, `list_tree`, and `read_files`.

    Routing is deliberately strict about the reference in the URL: a tree or raw
    request whose reference does not decode to `default_branch` is answered 404,
    exactly as GitHub answers a reference that does not exist. A client reading
    some other branch therefore fails visibly -- `list_tree` raises
    `REPO_NOT_FOUND` and `read_files` returns nothing -- instead of being quietly
    served. That is what keeps Property 2 from being vacuous, and
    `test_github_client.py` asserts the refusal directly so the strictness is
    itself covered.

    Everything the client needs to see is static data:

        github = FakeGitHub(
            RepoRef(owner="pallets", repo="flask"),
            default_branch="main",
            entries=[TreeEntry(path="README.md", kind="blob", size=12)],
            contents={"README.md": b"# Flask"},
        )

    Args:
        ref: The repository being served. Owner and repo are URL-escaped the
            same way the client escapes them, so routing matches on the wire
            form rather than the decoded names.
        default_branch: The branch the metadata answer reports, and the only
            reference tree and raw requests are answered for.
        entries: Tree entries to serve. Converted to the tree document GitHub
            returns. Ignored when `document` is given.
        document: A recorded tree document (`{"tree": [...], "truncated": ...}`)
            to serve verbatim -- this is what the three repository fixtures use.
        contents: Raw file bodies keyed by repository-relative path. A path
            absent from this map is answered 404, which is how the degraded-read
            path is exercised.
        metadata: Partial metadata document merged over the defaults, for cases
            that need a specific `size`, `language`, or `archived` value.
            `default_branch` is always taken from the argument above, so the
            reference the fake pins to and the one it reports cannot drift.
        truncated: Sets `truncated` on a tree document built from `entries`.
        api_status: When set, every `api.github.com` request is answered with
            this status, `api_body`, and `api_headers` instead of data. Used for
            the 403/429/5xx mappings.
    """

    def __init__(
        self,
        ref: RepoRef,
        *,
        default_branch: str = "main",
        entries: Iterable[TreeEntry] | None = None,
        document: Mapping[str, object] | None = None,
        contents: Mapping[str, bytes] | None = None,
        metadata: Mapping[str, object] | None = None,
        truncated: bool = False,
        api_status: int | None = None,
        api_body: bytes = b'{"message": "Not Found"}',
        api_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.ref = ref
        self.default_branch = default_branch
        self.entries: tuple[TreeEntry, ...] = tuple(entries or ())
        self.contents: Mapping[str, bytes] = MappingProxyType(dict(contents or {}))
        self.document: Mapping[str, object] = (
            dict(document)
            if document is not None
            else _tree_document(self.entries, truncated=truncated)
        )
        self.metadata: Mapping[str, object] = _metadata_document(
            ref, default_branch, metadata
        )
        self.api_status = api_status
        self.api_body = api_body
        self.api_headers: Mapping[str, str] = dict(api_headers or {})

        self.calls: list[RecordedCall] = []
        # `read_files` fetches on eight worker threads, so recording needs a
        # lock. Call *order* among raw fetches is still thread-dependent, which
        # is why assertions over `fetched_paths` compare sorted.
        self._lock = threading.Lock()

        owner, repo = _escape(ref.owner), _escape(ref.repo)
        self._metadata_path = f"/repos/{owner}/{repo}"
        self._tree_prefix = f"{self._metadata_path}/git/trees/"
        self._raw_prefix = f"/{owner}/{repo}/"
        # The wire form of the pinned reference. The tree endpoint takes the
        # branch as one path segment (`safe=""`); the raw host takes it inside
        # the path, so its slashes stay slashes.
        self._tree_ref = urllib.parse.quote(default_branch, safe="")
        self._raw_ref = urllib.parse.quote(default_branch)

    # -- the Transport protocol --------------------------------------------

    def request(
        self, method: str, url: str, *, headers: Mapping[str, str]
    ) -> FakeResponse:
        call = RecordedCall(
            method=method, url=url, headers=MappingProxyType(dict(headers))
        )
        with self._lock:
            self.calls.append(call)
        return self._answer(call)

    # -- inspection --------------------------------------------------------

    @property
    def urls(self) -> list[str]:
        return [call.url for call in self.calls]

    @property
    def api_calls(self) -> list[RecordedCall]:
        return [call for call in self.calls if call.is_api]

    @property
    def raw_calls(self) -> list[RecordedCall]:
        return [call for call in self.calls if call.is_raw]

    def references(self) -> list[str]:
        """The reference each recorded URL names, decoded, in call order.

        Derived from the recorded URL and nothing else, so it is evidence rather
        than bookkeeping. The metadata request names no reference and is skipped.
        A URL this fake cannot route -- a different host, a different repository,
        a reference that is not the pinned branch -- yields the offending URL
        itself, so `set(references()) == {branch}` fails with the deviation in
        the message instead of silently passing.
        """
        found: list[str] = []
        for call in self.calls:
            parts = call.parts
            if call.is_api and parts.path == self._metadata_path:
                continue
            if call.is_api and parts.path.startswith(self._tree_prefix):
                found.append(urllib.parse.unquote(parts.path[len(self._tree_prefix) :]))
                continue
            if call.is_raw and parts.path.startswith(self._raw_prefix):
                tail = parts.path[len(self._raw_prefix) :]
                if tail.startswith(f"{self._raw_ref}/"):
                    found.append(self.default_branch)
                    continue
            found.append(call.url)
        return found

    def fetched_paths(self) -> list[str]:
        """The repository-relative path each raw request asked for, decoded."""
        return [
            path
            for path in (self._raw_path(call) for call in self.raw_calls)
            if path is not None
        ]

    # -- routing -----------------------------------------------------------

    def _answer(self, call: RecordedCall) -> FakeResponse:
        parts = call.parts
        if call.is_api and parts.path == self._metadata_path:
            return self._api_answer(self.metadata)
        if call.is_api and parts.path.startswith(self._tree_prefix):
            if parts.path[len(self._tree_prefix) :] != self._tree_ref:
                return self._not_found()  # some reference other than the default
            return self._api_answer(self.document)
        path = self._raw_path(call)
        if path is not None and path in self.contents:
            return FakeResponse(200, self.contents[path])
        return self._not_found()

    def _api_answer(self, document: Mapping[str, object]) -> FakeResponse:
        if self.api_status is not None:
            return FakeResponse(self.api_status, self.api_body, self.api_headers)
        return FakeResponse(200, json.dumps(document).encode("utf-8"))

    def _not_found(self) -> FakeResponse:
        return FakeResponse(404, b'{"message": "Not Found"}')

    def _raw_path(self, call: RecordedCall) -> str | None:
        """The path a raw request asks for, or `None` if it is not one of ours.

        `None` covers a different host, a different repository, and -- the case
        Property 2 cares about -- a reference other than the pinned branch.
        """
        if not call.is_raw:
            return None
        parts = call.parts
        if not parts.path.startswith(self._raw_prefix):
            return None
        tail = parts.path[len(self._raw_prefix) :]
        prefix = f"{self._raw_ref}/"
        if not tail.startswith(prefix):
            return None
        return urllib.parse.unquote(tail[len(prefix) :])


def _escape(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _tree_document(
    entries: Sequence[TreeEntry], truncated: bool = False
) -> dict[str, object]:
    """Build the document `GET /git/trees/{ref}?recursive=1` returns.

    `size` is omitted for tree entries, as GitHub omits it, which keeps the
    client's "missing size means zero" coercion under test.
    """
    tree: list[dict[str, object]] = []
    for entry in entries:
        item: dict[str, object] = {"path": entry.path, "type": entry.kind}
        if entry.kind == "blob":
            item["size"] = entry.size
        tree.append(item)
    return {"sha": "0" * 40, "truncated": truncated, "tree": tree}


def _metadata_document(
    ref: RepoRef, default_branch: str, overrides: Mapping[str, object] | None
) -> dict[str, object]:
    document: dict[str, object] = {
        "full_name": ref.slug,
        "default_branch": default_branch,
        "size": 1_024,
        "language": None,
        "description": "",
        "html_url": f"https://github.com/{ref.slug}",
        "archived": False,
    }
    document.update(overrides or {})
    # The pinned reference and the reported one are the same value by
    # construction; an override cannot pull them apart.
    document["default_branch"] = default_branch
    return document


# ---------------------------------------------------------------------------
# The model call, the clock, and the backoff
# ---------------------------------------------------------------------------


class FakeBedrock:
    """A scripted stand-in for the `bedrock-runtime` client. No sockets.

    Satisfies `agent.bedrock.BedrockClient`, so it is passed as the `bedrock`
    argument to `agent.bedrock.converse`.

    `script` is consumed one entry per `converse` call:

    - an `Exception` is raised,
    - a `Mapping` is returned verbatim, which is how a malformed envelope is
      scripted,
    - anything else is returned as the assistant text of a well-formed answer.

    The final entry repeats once the script runs out, so "throttled forever" is
    a one-entry script and "throttled once, then answers" is two:

        bedrock = FakeBedrock(throttling_error(), "here is your tour")

    Every request is recorded in `calls`, so the model id and the inference
    configuration are assertable from outside the implementation.
    """

    def __init__(
        self,
        *script: object,
        input_tokens: int = 1_234,
        output_tokens: int = 321,
        stop_reason: str = "end_turn",
    ) -> None:
        self.script: list[object] = list(script) or ["ok"]
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.stop_reason = stop_reason
        self.calls: list[dict[str, object]] = []

    @property
    def attempts(self) -> int:
        """How many times `converse` was called."""
        return len(self.calls)

    def converse(self, **kwargs: object) -> Mapping[str, object]:
        self.calls.append(dict(kwargs))
        answer = self.script[min(self.attempts - 1, len(self.script) - 1)]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, Mapping):
            return answer
        return self.reply(str(answer))

    def reply(self, text: str) -> dict[str, object]:
        """One well-formed Converse response, shaped as Bedrock returns it."""
        return {
            "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
            "stopReason": self.stop_reason,
            "usage": {
                "inputTokens": self.input_tokens,
                "outputTokens": self.output_tokens,
                "totalTokens": self.input_tokens + self.output_tokens,
            },
        }


class FakeClock:
    """A monotonic counter in seconds, advanced only by the test.

    Shaped for `models.Deadline(started=..., budget_ms=..., clock=clock)` and
    for `StepRecorder(clock=clock)`: callable, never backwards, and `advance`
    is the only way time passes. A deadline can therefore be driven to its
    exact edge instead of waited out.
    """

    def __init__(self, now: float = 0.0) -> None:
        self.now = now
        self.reads = 0

    def __call__(self) -> float:
        self.reads += 1
        return self.now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("a monotonic clock does not go backwards")
        self.now += seconds


class RecordingSleep:
    """A `time.sleep` stand-in: records every delay, waits for none of them.

    Given a `FakeClock` it also advances it by each delay, so a backoff costs
    the deadline exactly what it would cost in production -- which is what
    makes "no retry is started that cannot finish" testable without spending
    real seconds.
    """

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.delays: list[float] = []
        self.clock = clock

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        if self.clock is not None:
            self.clock.advance(seconds)

    @property
    def total(self) -> float:
        return sum(self.delays)


# ---------------------------------------------------------------------------
# The recorded repository fixtures
# ---------------------------------------------------------------------------

REPO_FIXTURE_NAMES: tuple[str, ...] = ("flask", "gin", "express")


@dataclass(frozen=True)
class RepoFixture:
    """One recorded repository: its reference, metadata, tree, and file bodies.

    `contents` holds a handful of real file bodies per repository -- the readme,
    the manifest, the entry point -- short but genuine, so language detection
    and `package.json` entry-point resolution have something true to read.
    """

    name: str
    ref: RepoRef
    meta: RepoMeta
    entries: tuple[TreeEntry, ...]
    document: Mapping[str, object]
    contents: Mapping[str, bytes]

    @property
    def blob_paths(self) -> tuple[str, ...]:
        return tuple(e.path for e in self.entries if e.kind == "blob")

    def github(self, **overrides: object) -> FakeGitHub:
        """A fresh `FakeGitHub` serving this repository."""
        kwargs: dict[str, object] = {
            "default_branch": self.meta.default_branch,
            "document": self.document,
            "contents": self.contents,
            "metadata": {
                "full_name": self.meta.slug,
                "size": self.meta.size_kb,
                "language": self.meta.language,
                "description": self.meta.description,
                "html_url": self.meta.html_url,
                "archived": self.meta.archived,
            },
        }
        kwargs.update(overrides)
        return FakeGitHub(self.ref, **kwargs)  # type: ignore[arg-type]


_FIXTURE_META: Mapping[str, Mapping[str, object]] = {
    "flask": {
        "slug": "pallets/flask",
        "default_branch": "main",
        "size_kb": 11_032,
        "language": "Python",
        "description": "The Python micro framework for building web applications.",
        "html_url": "https://github.com/pallets/flask",
        "archived": False,
    },
    "gin": {
        "slug": "gin-gonic/gin",
        "default_branch": "master",
        "size_kb": 3_296,
        "language": "Go",
        "description": (
            "Gin is a HTTP web framework written in Go (Golang). "
            "It features a Martini-like API with much better performance."
        ),
        "html_url": "https://github.com/gin-gonic/gin",
        "archived": False,
    },
    "express": {
        "slug": "expressjs/express",
        "default_branch": "master",
        "size_kb": 5_112,
        "language": "JavaScript",
        "description": "Fast, unopinionated, minimalist web framework for node.",
        "html_url": "https://github.com/expressjs/express",
        "archived": False,
    },
}

_FIXTURE_CONTENTS: Mapping[str, Mapping[str, str]] = {
    "flask": {
        "README.md": (
            "# Flask\n\n"
            "Flask is a lightweight WSGI web application framework. It is "
            "designed to make getting started quick and easy, with the ability "
            "to scale up to complex applications.\n\n"
            "## A Simple Example\n\n"
            "```python\n"
            "from flask import Flask\n\n"
            "app = Flask(__name__)\n\n"
            '@app.route("/")\n'
            "def hello():\n"
            '    return "Hello, World!"\n'
            "```\n"
        ),
        "pyproject.toml": (
            "[project]\n"
            'name = "flask"\n'
            'description = "A simple framework for building complex web '
            'applications."\n'
            'readme = "README.md"\n'
            'requires-python = ">=3.9"\n'
            "dependencies = [\n"
            '    "Werkzeug>=3.0.0",\n'
            '    "Jinja2>=3.1.2",\n'
            '    "click>=8.1.3",\n'
            "]\n\n"
            "[project.scripts]\n"
            'flask = "flask.cli:main"\n'
        ),
        "src/flask/__init__.py": (
            "from .app import Flask as Flask\n"
            "from .blueprints import Blueprint as Blueprint\n"
            "from .globals import current_app as current_app\n"
            "from .globals import request as request\n"
        ),
        "src/flask/app.py": (
            "class Flask(App):\n"
            '    """The flask object implements a WSGI application and acts as\n'
            "    the central object. It is passed the name of the module or\n"
            '    package of the application."""\n\n'
            '    default_config = ImmutableDict({"DEBUG": None})\n'
        ),
        "src/flask/__main__.py": "from flask.cli import main\n\nmain()\n",
    },
    "gin": {
        "README.md": (
            "# Gin Web Framework\n\n"
            "Gin is a web framework written in Go. It features a martini-like "
            "API with performance that is up to 40 times faster thanks to "
            "httprouter.\n\n"
            "## Quickstart\n\n"
            "```go\n"
            "package main\n\n"
            'import "github.com/gin-gonic/gin"\n\n'
            "func main() {\n"
            "\tr := gin.Default()\n"
            '\tr.GET("/ping", func(c *gin.Context) {\n'
            '\t\tc.JSON(200, gin.H{"message": "pong"})\n'
            "\t})\n"
            "\tr.Run()\n"
            "}\n"
            "```\n"
        ),
        "go.mod": (
            "module github.com/gin-gonic/gin\n\n"
            "go 1.21\n\n"
            "require (\n"
            "\tgithub.com/gin-contrib/sse v0.1.0\n"
            "\tgithub.com/stretchr/testify v1.9.0\n"
            "\tgolang.org/x/net v0.25.0\n"
            ")\n"
        ),
        "gin.go": (
            "package gin\n\n"
            "// Engine is the framework's instance, it contains the muxer,\n"
            "// middleware and configuration settings.\n"
            "type Engine struct {\n"
            "\tRouterGroup\n"
            "\tRedirectTrailingSlash bool\n"
            "\tHandleMethodNotAllowed bool\n"
            "}\n\n"
            "// Default returns an Engine instance with the Logger and\n"
            "// Recovery middleware already attached.\n"
            "func Default() *Engine {\n"
            "\tengine := New()\n"
            "\tengine.Use(Logger(), Recovery())\n"
            "\treturn engine\n"
            "}\n"
        ),
        "Makefile": (
            "GO ?= go\n\n"
            ".PHONY: test\n"
            "test:\n"
            "\t$(GO) test -race -covermode=atomic ./...\n\n"
            ".PHONY: lint\n"
            "lint:\n"
            "\tgolangci-lint run ./...\n"
        ),
    },
    "express": {
        "Readme.md": (
            "# Express\n\n"
            "Fast, unopinionated, minimalist web framework for Node.js.\n\n"
            "```js\n"
            "const express = require('express')\n"
            "const app = express()\n\n"
            "app.get('/', (req, res) => res.send('Hello World'))\n\n"
            "app.listen(3000)\n"
            "```\n"
        ),
        "package.json": (
            "{\n"
            '  "name": "express",\n'
            '  "description": "Fast, unopinionated, minimalist web framework",\n'
            '  "version": "4.19.2",\n'
            '  "main": "index.js",\n'
            '  "scripts": {\n'
            '    "test": "mocha --require test/support/env --reporter spec"\n'
            "  },\n"
            '  "dependencies": {\n'
            '    "body-parser": "1.20.2",\n'
            '    "finalhandler": "1.2.0",\n'
            '    "router": "1.3.8"\n'
            "  }\n"
            "}\n"
        ),
        "index.js": "'use strict';\n\nmodule.exports = require('./lib/express');\n",
        "lib/express.js": (
            "'use strict';\n\n"
            "var mixin = require('merge-descriptors');\n"
            "var proto = require('./application');\n\n"
            "exports = module.exports = createApplication;\n\n"
            "function createApplication() {\n"
            "  var app = function (req, res, next) {\n"
            "    app.handle(req, res, next);\n"
            "  };\n"
            "  mixin(app, proto, false);\n"
            "  return app;\n"
            "}\n"
        ),
        "lib/router/index.js": (
            "'use strict';\n\n"
            "var proto = module.exports = function router(options) {\n"
            "  function router(req, res, next) {\n"
            "    router.handle(req, res, next);\n"
            "  }\n"
            "  return router;\n"
            "};\n"
        ),
    },
}


@lru_cache(maxsize=None)
def load_repo_fixture(name: str) -> RepoFixture:
    """Load one recorded repository by name: "flask", "gin", or "express".

    Cached, and every field is immutable, so callers share one instance without
    being able to disturb each other.
    """
    if name not in REPO_FIXTURE_NAMES:
        raise KeyError(f"unknown repository fixture: {name!r}")

    document = json.loads((FIXTURE_DIR / f"tree_{name}.json").read_text("utf-8"))
    entries = tuple(
        TreeEntry(path=item["path"], kind=item["type"], size=int(item.get("size") or 0))
        for item in document["tree"]
    )
    meta = RepoMeta(**_FIXTURE_META[name])  # type: ignore[arg-type]
    owner, repo = meta.slug.split("/", 1)
    return RepoFixture(
        name=name,
        ref=RepoRef(owner=owner, repo=repo),
        meta=meta,
        entries=entries,
        document=MappingProxyType(document),
        contents=MappingProxyType(
            {
                path: body.encode("utf-8")
                for path, body in _FIXTURE_CONTENTS[name].items()
            }
        ),
    )


@pytest.fixture(scope="session")
def flask_repo() -> RepoFixture:
    """The recorded pallets/flask tree. Python, default branch `main`."""
    return load_repo_fixture("flask")


@pytest.fixture(scope="session")
def gin_repo() -> RepoFixture:
    """The recorded gin-gonic/gin tree. Go, default branch `master`."""
    return load_repo_fixture("gin")


@pytest.fixture(scope="session")
def express_repo() -> RepoFixture:
    """The recorded expressjs/express tree. JavaScript, default branch `master`."""
    return load_repo_fixture("express")


@pytest.fixture(params=REPO_FIXTURE_NAMES, ids=REPO_FIXTURE_NAMES, scope="session")
def repo_fixture(request: pytest.FixtureRequest) -> RepoFixture:
    """Each recorded repository in turn, so a test runs once per language."""
    return load_repo_fixture(request.param)
