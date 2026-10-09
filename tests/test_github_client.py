"""Tests for the GitHub client: status mapping, guards, degradation, Property 2.

Two layers, in that order in the file.

The example-based half comes first -- one case per row of the error mapping, the
kilobyte arithmetic, the rate-limit clamp's boundaries, and the degraded raw
fetch -- so a wrong status code or an off-by-one clamp fails on its own rather
than inside a property. `StubTransport` below is deliberately minimal for that:
a queue of answers and a record of what was asked.

Property 2 comes last and states the universal half over generated owners,
repository names, default branches, and file paths: no credential ever leaves
this module, and no request names a reference other than the default branch the
metadata reported. It drives `FakeGitHub` from `conftest.py`, which answers from
static data and records every URL and header.
"""

from __future__ import annotations

import urllib.parse

import pytest
import urllib3
from conftest import FakeGitHub  # tests/ is on sys.path; see conftest's docstring
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from agent import github_client as gc
from agent.config import MAX_REPO_SIZE_KB, MAX_TREE_FILES
from agent.errors import AgentError, ErrorCode
from agent.models import RepoMeta, RepoRef, TreeEntry

REF = RepoRef(owner="pallets", repo="flask")


class StubResponse:
    def __init__(self, status: int, data: bytes, headers: dict[str, str] | None = None):
        self.status = status
        self.data = data
        self.headers = headers or {}


class StubTransport:
    """Answers every request with a queued response and records the call."""

    def __init__(self, *responses: StubResponse | Exception):
        self._queue = list(responses)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def request(self, method, url, *, headers):  # noqa: ANN001, ANN202
        self.calls.append((url, dict(headers)))
        answer = self._queue.pop(0) if len(self._queue) > 1 else self._queue[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def meta_json(**overrides: object) -> bytes:
    import json

    document = {
        "full_name": "pallets/flask",
        "default_branch": "main",
        "size": 1_024,
        "language": "Python",
        "description": "The Python micro framework",
        "html_url": "https://github.com/pallets/flask",
    }
    document.update(overrides)
    return json.dumps(document).encode()


def tree_json(entries: list[dict], truncated: bool = False) -> bytes:
    import json

    return json.dumps({"tree": entries, "truncated": truncated}).encode()


# ---------------------------------------------------------------------------
# Status mapping (Requirements 2 AC6, AC7, 6 AC1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "headers", "body", "expected"),
    [
        (404, {}, b"{}", ErrorCode.REPO_NOT_FOUND),
        (451, {}, b"{}", ErrorCode.REPO_NOT_FOUND),
        (301, {}, b"", ErrorCode.REPO_NOT_FOUND),
        # 403 with allowance left is a refusal, not a wait.
        (403, {"x-ratelimit-remaining": "41"}, b"{}", ErrorCode.REPO_NOT_FOUND),
        (
            403,
            {"x-ratelimit-remaining": "0"},
            b"{}",
            ErrorCode.GITHUB_RATE_LIMITED,
        ),
        # A secondary limit explains itself in the body, not in the header.
        (
            403,
            {},
            b'{"message": "You have exceeded a secondary rate limit"}',
            ErrorCode.GITHUB_RATE_LIMITED,
        ),
        (429, {"x-ratelimit-remaining": "0"}, b"{}", ErrorCode.GITHUB_RATE_LIMITED),
        (500, {}, b"", ErrorCode.GITHUB_UNAVAILABLE),
        (502, {}, b"", ErrorCode.GITHUB_UNAVAILABLE),
        (200, {}, b"not json", ErrorCode.GITHUB_UNAVAILABLE),
    ],
)
def test_api_status_maps_to_error_code(status, headers, body, expected):
    transport = StubTransport(StubResponse(status, body, headers))
    with pytest.raises(AgentError) as caught:
        gc.get_metadata(REF, transport)
    assert caught.value.code is expected


def test_transport_failure_maps_to_github_unavailable():
    transport = StubTransport(urllib3.exceptions.ReadTimeoutError(None, "u", "slow"))
    with pytest.raises(AgentError) as caught:
        gc.get_metadata(REF, transport)
    assert caught.value.code is ErrorCode.GITHUB_UNAVAILABLE


def test_missing_default_branch_is_a_malformed_answer():
    """Requirement 2 AC4: guessing a branch would read the wrong reference."""
    transport = StubTransport(StubResponse(200, meta_json(default_branch=None)))
    with pytest.raises(AgentError) as caught:
        gc.get_metadata(REF, transport)
    assert caught.value.code is ErrorCode.GITHUB_UNAVAILABLE


# ---------------------------------------------------------------------------
# Metadata and the size guard (Requirements 2 AC4, 3 AC8)
# ---------------------------------------------------------------------------


def test_get_metadata_reads_the_reported_fields():
    transport = StubTransport(StubResponse(200, meta_json(archived=True)))
    meta = gc.get_metadata(REF, transport)

    assert meta == RepoMeta(
        slug="pallets/flask",
        default_branch="main",
        size_kb=1_024,
        language="Python",
        description="The Python micro framework",
        html_url="https://github.com/pallets/flask",
        archived=True,
    )
    url, headers = transport.calls[0]
    assert url == "https://api.github.com/repos/pallets/flask"
    assert "Authorization" not in headers


def test_size_is_kilobytes_so_a_one_megabyte_repo_passes():
    """A repository GitHub reports as 1 024 is 1 MB, not 1 GB. Requirement 3 AC8."""
    transport = StubTransport(StubResponse(200, meta_json(size=1_024)))
    assert gc.get_metadata(REF, transport).size_kb == 1_024


@pytest.mark.parametrize("size_kb", [MAX_REPO_SIZE_KB, MAX_REPO_SIZE_KB - 1, 0])
def test_repo_at_or_under_the_limit_is_accepted(size_kb):
    gc.guard_repo_size(_meta(size_kb=size_kb))


def test_oversize_repo_raises_with_the_megabyte_figures():
    with pytest.raises(AgentError) as caught:
        gc.guard_repo_size(_meta(size_kb=422_000))
    assert caught.value.code is ErrorCode.REPO_TOO_LARGE
    assert caught.value.params == {"size_mb": 412, "limit_mb": 100}


def test_oversize_guard_runs_before_a_tree_can_be_listed():
    """Property 9: the guard rejects before any further content request."""
    transport = StubTransport(StubResponse(200, meta_json(size=MAX_REPO_SIZE_KB + 1)))
    with pytest.raises(AgentError) as caught:
        gc.get_metadata(REF, transport)
    assert caught.value.code is ErrorCode.REPO_TOO_LARGE
    assert len(transport.calls) == 1


# ---------------------------------------------------------------------------
# Tree listing and the file-count guard (Requirements 2 AC4, 3 AC9)
# ---------------------------------------------------------------------------


def test_list_tree_escapes_a_slashed_branch_as_one_segment():
    """Requirement 2 AC4: `release/2.0` is a branch name, not a path."""
    transport = StubTransport(StubResponse(200, tree_json([])))
    gc.list_tree(REF, "release/2.0", transport)

    url, headers = transport.calls[0]
    assert url == (
        "https://api.github.com/repos/pallets/flask/git/trees/release%2F2.0?recursive=1"
    )
    assert "Authorization" not in headers


def test_list_tree_returns_entries_and_skips_malformed_ones():
    transport = StubTransport(
        StubResponse(
            200,
            tree_json(
                [
                    {"path": "README.md", "type": "blob", "size": 4_096},
                    {"path": "src", "type": "tree"},
                    {"type": "blob", "size": 10},  # no path
                    {"path": "big.bin", "type": "blob", "size": "nonsense"},
                ]
            ),
        )
    )
    assert gc.list_tree(REF, "main", transport) == [
        TreeEntry(path="README.md", kind="blob", size=4_096),
        TreeEntry(path="src", kind="tree", size=0),
        TreeEntry(path="big.bin", kind="blob", size=0),
    ]


def test_truncated_tree_is_too_many_files_with_no_invented_count():
    """A partial tree cannot support an honest count. Requirement 3 AC9."""
    transport = StubTransport(
        StubResponse(200, tree_json([{"path": "a.py", "type": "blob"}], truncated=True))
    )
    with pytest.raises(AgentError) as caught:
        gc.list_tree(REF, "main", transport)
    assert caught.value.code is ErrorCode.REPO_TOO_MANY_FILES
    assert caught.value.params == {}


def test_file_count_guard_counts_blobs_only():
    """Requirement 3 AC9 counts files, so trees do not push a repo over."""
    trees = [TreeEntry(path=f"d{i}", kind="tree", size=0) for i in range(100)]
    blobs = [
        TreeEntry(path=f"f{i}.py", kind="blob", size=1) for i in range(MAX_TREE_FILES)
    ]
    gc.guard_tree_file_count(blobs + trees, truncated=False)

    with pytest.raises(AgentError) as caught:
        gc.guard_tree_file_count(
            [*blobs, TreeEntry(path="one-more.py", kind="blob", size=1)],
            truncated=False,
        )
    assert caught.value.code is ErrorCode.REPO_TOO_MANY_FILES
    assert caught.value.params == {"file_count": MAX_TREE_FILES + 1}


# ---------------------------------------------------------------------------
# Raw reads and degradation (Requirements 2 AC3, AC4)
# ---------------------------------------------------------------------------


def test_read_files_uses_the_raw_host_with_no_credentials():
    transport = StubTransport(StubResponse(200, b"print('hi')"))
    bodies = gc.read_files(REF, "release/2.0", ["src/flask/app.py"], transport)

    assert bodies == {"src/flask/app.py": b"print('hi')"}
    url, headers = transport.calls[0]
    assert url == (
        "https://raw.githubusercontent.com/pallets/flask/release/2.0/src/flask/app.py"
    )
    assert dict(headers) == {"User-Agent": gc.USER_AGENT}


def test_a_single_failed_fetch_degrades_to_unread_rather_than_raising():
    transport = StubTransport(
        StubResponse(200, b"# readme"),
        StubResponse(404, b"404: Not Found"),  # a symlink or submodule pointer
        urllib3.exceptions.ProtocolError("connection reset"),
    )
    requested = ["README.md", "link.py", "src/app.py"]
    bodies = gc.read_files(REF, "main", requested, transport)

    assert bodies == {"README.md": b"# readme"}
    assert gc.unread_paths(requested, bodies) == ["link.py", "src/app.py"]


def test_read_files_preserves_request_order_and_drops_duplicates():
    transport = StubTransport(StubResponse(200, b"x"))
    paths = ["b.py", "a.py", "b.py"]
    bodies = gc.read_files(REF, "main", paths, transport)

    assert list(bodies) == ["b.py", "a.py"]
    assert len(transport.calls) == 2


def test_read_files_makes_no_request_for_an_empty_selection():
    transport = StubTransport(StubResponse(200, b"x"))
    assert gc.read_files(REF, "main", [], transport) == {}
    assert transport.calls == []


# ---------------------------------------------------------------------------
# The rate-limit wait (Requirement 2 AC7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, 11),  # missing: the 600 s fallback, rounded up
        ("", 11),
        ("not-a-number", 11),
        ("nan", 11),
        ("inf", 11),
        ("900", 1),  # already in the past: never a negative wait
        ("1000", 1),  # right now
        ("1001", 1),
        # Pessimistic at the minute boundary on purpose: a wait of exactly 60 s
        # reports 2, because sending the user back a second early costs them a
        # second failed request and overstating by a minute costs nothing.
        ("1060", 2),
        ("1061", 2),  # 61 s out rounds up, never down
        ("4540", 60),  # 59 minutes
        ("4600", 60),  # exactly 60
        ("99999999999", 60),  # absurd: clamped, not nine hours
    ],
)
def test_reset_minutes_clamps_to_one_through_sixty(header, expected):
    assert gc._reset_minutes(header, now=1000.0) == expected


def test_rate_limited_error_carries_only_the_minutes_param():
    transport = StubTransport(
        StubResponse(
            403,
            b'{"message": "API rate limit exceeded"}',
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "99999999999"},
        )
    )
    with pytest.raises(AgentError) as caught:
        gc.get_metadata(REF, transport)
    assert caught.value.code is ErrorCode.GITHUB_RATE_LIMITED
    assert caught.value.params == {"minutes": 60}


# ---------------------------------------------------------------------------
# The structural no-credentials guarantee (Requirement 2 AC3)
# ---------------------------------------------------------------------------


def test_headers_hold_no_credential_and_cannot_be_given_one():
    for table in (gc.HEADERS, gc.RAW_HEADERS):
        assert not any(key.lower() == "authorization" for key in table)
        with pytest.raises(TypeError):
            table["Authorization"] = "token hunter2"  # type: ignore[index]


def _meta(**overrides: object) -> RepoMeta:
    fields: dict = {
        "slug": "pallets/flask",
        "default_branch": "main",
        "size_kb": 0,
        "language": "Python",
        "description": "",
        "html_url": "https://github.com/pallets/flask",
        "archived": False,
    }
    fields.update(overrides)
    return RepoMeta(**fields)


# ---------------------------------------------------------------------------
# Property 2: every request unauthenticated, every reference the default branch
# (Requirements 2 AC3, AC4)
# ---------------------------------------------------------------------------

# Every spelling of "here is a credential" that either GitHub host accepts.
_CREDENTIAL_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "x-access-token",
        "x-github-token",
        "private-token",
        "x-api-key",
    }
)

# Query parameters that have carried a token to GitHub or to a mirror of it.
_TOKEN_PARAMS = frozenset(
    {
        "access_token",
        "token",
        "auth",
        "api_key",
        "apikey",
        "oauth_token",
        "client_id",
        "client_secret",
        "private_token",
    }
)

# `git check-ref-format` forbids control characters, space, `~^:?*[\`, and `..`
# anywhere in a ref, so the branch alphabet is what remains -- which still holds
# every class of character that has to be escaped on the way into a URL. `%` and
# `#` are the dangerous two: left unescaped, the first invents an escape
# sequence and the second truncates the URL at a fragment.
_BRANCH_ALPHABET = "abzAZ09-_.+#%&=',()!;$@üé日"

# Paths may hold what refs may not: a space is legal in a tree entry.
_PATH_ALPHABET = _BRANCH_ALPHABET + " "


def _branch_segment(raw: str) -> str:
    """Sanitize one generated ref component into git's grammar."""
    cleaned = raw.strip().strip(".")
    while ".." in cleaned:
        cleaned = cleaned.replace("..", ".")
    return cleaned or "x"


def _path_segment(raw: str) -> str:
    """Sanitize one generated path component. A leading dot stays: `.github`."""
    cleaned = raw.strip()
    while ".." in cleaned:
        cleaned = cleaned.replace("..", ".")
    return "x" if cleaned in {"", "."} else cleaned


_refs = st.builds(
    RepoRef,
    owner=st.from_regex(
        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,8}[A-Za-z0-9])?", fullmatch=True
    ),
    repo=st.from_regex(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,10}", fullmatch=True),
)

_branches = st.one_of(
    st.sampled_from(
        [
            "main",
            "master",
            "trunk",
            "release/2.0",  # a slash is one segment on the API, a path on raw
            "feature/ünïcødé",
            "v1.0+hotfix",
            "100%done",
            "fix#1234",
            "dev&qa=1",
            "owner's-branch",
        ]
    ),
    st.lists(
        st.text(alphabet=_BRANCH_ALPHABET, min_size=1, max_size=10).map(
            _branch_segment
        ),
        min_size=1,
        max_size=3,
    ).map("/".join),
)

_file_names = st.one_of(
    st.sampled_from(
        [
            "README.md",
            "pyproject.toml",
            "go.mod",
            "index.js",
            "a file with spaces.py",
            # Shaped like a query string: the `?` has to be escaped, or the rest
            # of the file name arrives as a parameter on the wire.
            "?access_token=hunter2",
            "100%.md",
            "café.txt",
            "c#.cs",
            "a+b.ts",
        ]
    ),
    st.text(alphabet=_PATH_ALPHABET, min_size=1, max_size=10)
    .map(_path_segment)
    .map(lambda name: f"{name}.py"),
)

_paths = st.builds(
    lambda folders, name: "/".join([*folders, name]),
    st.lists(
        st.text(alphabet=_PATH_ALPHABET, min_size=1, max_size=8).map(_path_segment),
        max_size=2,
    ),
    _file_names,
)


def _assert_unauthenticated(call) -> None:  # noqa: ANN001 -- conftest.RecordedCall
    """No credential, in any of the four places a request could carry one."""
    assert call.method == "GET"
    parts = call.parts
    assert parts.scheme == "https"
    # Exact authority, so no userinfo: `https://token@github.com/...` would send
    # a credential and still read as a GitHub URL.
    assert parts.netloc in {"api.github.com", "raw.githubusercontent.com"}
    assert not call.header_names & _CREDENTIAL_HEADERS
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    assert not set(query) & _TOKEN_PARAMS
    # Requirement 2 AC3 is structural: the headers sent are the module constants
    # themselves, so there is no per-request path for a credential to arrive by.
    assert dict(call.headers) == dict(gc.HEADERS if call.is_api else gc.RAW_HEADERS)


@settings(
    max_examples=200,
    # The raw reads run on a thread pool, so a per-example wall-clock deadline
    # measures the host's scheduler rather than this code.
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    ref=_refs,
    branch=_branches,
    paths=st.lists(_paths, min_size=1, max_size=5, unique=True),
)
def test_property_2_requests_are_unauthenticated_and_branch_pinned(
    ref: RepoRef, branch: str, paths: list[str]
) -> None:
    """Feature: delightful-aws-agent, Property 2: Every GitHub request is
    unauthenticated and pinned to the default branch.

    For any repository reference and any default branch name the metadata
    reports -- including names containing slashes or characters requiring URL
    escaping -- every outbound GitHub request carries no `Authorization` header
    and no token parameter, and every tree and file request encodes exactly that
    default branch and no other reference.

    **Validates: Requirements 2.3, 2.4**
    """
    github = FakeGitHub(
        ref,
        default_branch=branch,
        entries=[TreeEntry(path=path, kind="blob", size=64) for path in paths],
        contents=dict.fromkeys(paths, b"file body"),
    )

    # Driven the way the handler will drive it: nothing supplies a branch, the
    # pipeline only ever hands back what the metadata answer reported.
    meta = gc.get_metadata(ref, github)
    entries = gc.list_tree(ref, meta.default_branch, github)
    bodies = gc.read_files(ref, meta.default_branch, [e.path for e in entries], github)

    assert meta.default_branch == branch
    # `FakeGitHub` answers a tree or raw request only when the reference in the
    # URL decodes to the branch it reported, so a complete answer is itself
    # evidence of the pinning. The next test proves that refusal has teeth.
    assert [entry.path for entry in entries] == paths
    assert set(bodies) == set(paths)
    assert len(github.api_calls) == 2  # metadata, tree -- the two metered calls
    assert len(github.raw_calls) == len(paths)

    for call in github.calls:
        _assert_unauthenticated(call)

    # Requirement 2 AC4: one reference, the reported default branch, everywhere.
    assert set(github.references()) == {branch}
    # And the escaping is reversible, so "pinned" means the branch itself rather
    # than something that merely starts with it.
    assert sorted(github.fetched_paths()) == sorted(paths)

    metadata_call, tree_call = github.api_calls
    assert metadata_call.parts.query == ""
    assert tree_call.parts.query == "recursive=1"


def test_fake_github_refuses_a_reference_other_than_the_default_branch() -> None:
    """The double's pinning check has teeth, so Property 2 is not vacuous.

    Asking for any other reference is answered 404, the way GitHub answers a ref
    that does not exist: the tree call fails outright and the raw call comes back
    empty. A client that read a branch other than the reported default would
    therefore fail the property loudly rather than be quietly served.
    """
    github = FakeGitHub(
        REF,
        default_branch="main",
        entries=[TreeEntry(path="README.md", kind="blob", size=8)],
        contents={"README.md": b"# Flask"},
    )

    with pytest.raises(AgentError) as caught:
        gc.list_tree(REF, "release/2.0", github)
    assert caught.value.code is ErrorCode.REPO_NOT_FOUND
    assert gc.read_files(REF, "release/2.0", ["README.md"], github) == {}

    # The deviation surfaces either decoded, for a tree request, or as the whole
    # unroutable URL, for a raw one -- never as the pinned branch.
    assert github.references() == [
        "release/2.0",
        "https://raw.githubusercontent.com/pallets/flask/release/2.0/README.md",
    ]


def test_recorded_trees_are_served_unauthenticated_from_their_default_branch(
    repo_fixture,  # noqa: ANN001 -- conftest.RepoFixture, parametrized
) -> None:
    """Property 2's claim against the three recorded repositories.

    Flask on `main`, Gin and Express on `master`: the whole client path over real
    tree shapes, which is the concrete complement to the generated inputs above.
    """
    github = repo_fixture.github()
    wanted = list(repo_fixture.contents)

    meta = gc.get_metadata(repo_fixture.ref, github)
    entries = gc.list_tree(repo_fixture.ref, meta.default_branch, github)
    bodies = gc.read_files(repo_fixture.ref, meta.default_branch, wanted, github)

    assert meta == repo_fixture.meta
    assert tuple(entries) == repo_fixture.entries
    assert bodies == dict(repo_fixture.contents)
    assert gc.unread_paths(wanted, bodies) == []
    assert set(github.references()) == {repo_fixture.meta.default_branch}
    for call in github.calls:
        _assert_unauthenticated(call)
