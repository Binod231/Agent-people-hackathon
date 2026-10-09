"""The only module that talks to GitHub. Unauthenticated, by construction.

Three operations, two hosts, and one guarantee that has to be structural rather
than remembered.

| Operation    | Endpoint                                             | Metered |
|--------------|------------------------------------------------------|---------|
| `get_metadata` | `api.github.com/repos/{owner}/{repo}`              | yes     |
| `list_tree`    | `api.github.com/repos/{o}/{r}/git/trees/{branch}`  | yes     |
| `read_files`   | `raw.githubusercontent.com/{o}/{r}/{branch}/{path}`| no      |

File bodies come from `raw.githubusercontent.com` rather than the REST
`contents` endpoint for one reason: the unauthenticated core API allowance is
roughly 60 requests per hour per source IP. Reading twelve files through the API
would cost fourteen metered requests per chat turn -- about four turns an hour,
which is not a usable demo. Raw fetches are not metered against that allowance,
so only two metered calls remain per request, and raw returns plain text instead
of base64 inside a JSON envelope.

Four things here are easy to get wrong, so each is called out where it happens:

- **No `Authorization` header is ever set** (Requirement 2 AC3). `HEADERS` is a
  module constant wrapped in `MappingProxyType`, so there is no credential path
  into it -- not from configuration, not from the environment, and not from a
  caller reaching in to add a key. The guarantee is structural, which is what
  Property 2 asserts.
- **`size` from the metadata endpoint is in kilobytes.** Requirement 3 AC8's
  100 MB limit is `size_kb > 102_400`. Reading the field as bytes would reject
  every repository over 100 KB.
- **Branch names are URL-escaped.** A default branch may contain `/` and other
  characters needing escaping. The tree endpoint takes the branch as a single
  path segment, so it is quoted with `safe=""`; the raw host takes it as part of
  the path, so its slashes are left intact. Both encode exactly the reported
  default branch and no other reference (Requirement 2 AC4, Property 2).
- **A single failed raw fetch degrades, it does not fail.** Twelve files are
  requested and one of them may 404 -- a symlink, a submodule pointer, a race
  against a force-push. The context is then built from the rest and the path is
  reported through `unread_paths`.

Guards live here too, next to the call that produces the data they guard, so
there is no window in which an oversized repository has been measured but not
yet rejected. `get_metadata` raises before a tree can be listed and `list_tree`
raises before a body can be read, which is what makes "zero GitHub content
requests" in Property 9 true by construction rather than by call ordering.

Retries are deliberately absent on the GitHub side. The one retry policy in this
system is the bounded Bedrock backoff (Requirement 6 AC2); adding another here
would spend the request deadline twice over for a host that is either answering
or rate limiting us.
"""

from __future__ import annotations

import concurrent.futures
import json
import math
import time
import urllib.parse
from collections.abc import Iterable, Mapping, Sequence
from types import MappingProxyType
from typing import Protocol

import urllib3

from .config import (
    GITHUB_CONNECT_TIMEOUT_SECONDS,
    GITHUB_READ_TIMEOUT_SECONDS,
    MAX_REPO_SIZE_KB,
    MAX_REPO_SIZE_MB,
    MAX_TREE_FILES,
    RATE_LIMIT_WAIT_FALLBACK_SECONDS,
    RATE_LIMIT_WAIT_MAX_MINUTES,
    RATE_LIMIT_WAIT_MIN_MINUTES,
    RAW_FETCH_WORKERS,
)
from .errors import AgentError, ErrorCode
from .models import RepoMeta, RepoRef, TreeEntry

__all__ = [
    "API",
    "HEADERS",
    "RAW",
    "RAW_HEADERS",
    "USER_AGENT",
    "get_metadata",
    "guard_repo_size",
    "guard_tree_file_count",
    "list_tree",
    "read_files",
    "unread_paths",
]

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"

# GitHub rejects API requests that arrive without a User-Agent.
USER_AGENT = "codebase-welcome-guide/1.0 (+aws-builder-challenge)"

# Requirement 2 AC3. Read-only by construction: `MappingProxyType` has no
# `__setitem__`, so neither a caller nor this module can add an `Authorization`
# header or a token parameter after import. Adding credentials would mean
# editing this literal, which is a reviewable change rather than a runtime one.
HEADERS: Mapping[str, str] = MappingProxyType(
    {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
)

# The raw host serves file bytes, not JSON, so the API negotiation headers are
# meaningless there. Same construction, same guarantee.
RAW_HEADERS: Mapping[str, str] = MappingProxyType({"User-Agent": USER_AGENT})

# API statuses that mean "there is no repository here to read". A renamed or
# transferred repository answers 3xx with a `Location`, and since redirects are
# not followed the honest outcome is the same as a miss -- and `REPO_NOT_FOUND`
# copy already asks the user to double-check the owner and repository name,
# which is exactly the next action for a rename.
_NOT_FOUND_STATUSES = frozenset({301, 302, 303, 307, 308, 404, 451})


class _Response(Protocol):
    """The slice of a response this module reads."""

    @property
    def status(self) -> int: ...

    @property
    def data(self) -> bytes: ...

    @property
    def headers(self) -> Mapping[str, str]: ...


class Transport(Protocol):
    """The seam tests substitute for `urllib3`.

    `urllib3.PoolManager` satisfies this as-is. A recording double that captures
    every outbound URL and header satisfies it in about fifteen lines, which is
    how Property 2 checks the no-credentials guarantee without a network.
    """

    def request(
        self, method: str, url: str, *, headers: Mapping[str, str]
    ) -> _Response: ...


_POOL: Transport = urllib3.PoolManager(
    maxsize=RAW_FETCH_WORKERS,  # one connection per raw-fetch worker
    retries=False,  # the retry policy is ours, and there isn't one here
    timeout=urllib3.Timeout(
        connect=GITHUB_CONNECT_TIMEOUT_SECONDS,
        read=GITHUB_READ_TIMEOUT_SECONDS,
    ),
)


# ---------------------------------------------------------------------------
# The metered API path
# ---------------------------------------------------------------------------


def _api_get(path: str, transport: Transport | None = None) -> dict:
    """GET a JSON document from `api.github.com`, mapping failure to a code.

    Only 200 is success. Every other status becomes an `AgentError`, so no
    caller ever receives a half-answer: a 3xx body is empty, a 403 body is a
    rate-limit notice, and parsing either as repository data would turn a clear
    failure into a confusing one.
    """
    pool = _POOL if transport is None else transport
    try:
        response = pool.request("GET", f"{API}{path}", headers=HEADERS)
    except urllib3.exceptions.HTTPError as exc:
        # Covers connect and read timeouts, DNS failure, and protocol errors.
        raise AgentError(ErrorCode.GITHUB_UNAVAILABLE) from exc

    status = response.status
    if status in _NOT_FOUND_STATUSES:  # Requirement 2 AC6
        raise AgentError(ErrorCode.REPO_NOT_FOUND)
    if status in (403, 429):
        raise _forbidden_error(response)
    if status != 200:  # 5xx, and anything else GitHub invents
        raise AgentError(ErrorCode.GITHUB_UNAVAILABLE)

    try:
        document = json.loads(response.data)
    except (TypeError, ValueError) as exc:
        raise AgentError(ErrorCode.GITHUB_UNAVAILABLE) from exc
    if not isinstance(document, dict):
        raise AgentError(ErrorCode.GITHUB_UNAVAILABLE)
    return document


def _forbidden_error(response: _Response) -> AgentError:
    """Tell a rate limit apart from a plain refusal. Requirement 2 AC7.

    GitHub spends 403 on both, and the two need different copy: a rate limit is
    a wait, a refusal is not. `x-ratelimit-remaining: 0` is the reliable signal;
    the body scan is the fallback for secondary limits, which carry the
    explanation in prose instead of in a header.
    """
    headers = response.headers
    remaining = headers.get("x-ratelimit-remaining")
    body = response.data[:400].decode("utf-8", "replace").lower()
    if remaining == "0" or "rate limit" in body:
        return AgentError(
            ErrorCode.GITHUB_RATE_LIMITED,
            minutes=_reset_minutes(headers.get("x-ratelimit-reset")),
        )
    return AgentError(ErrorCode.REPO_NOT_FOUND)


def _reset_minutes(reset_header: str | None, now: float | None = None) -> int:
    """Turn `x-ratelimit-reset` into a wait the copy can state. Requirement 2 AC7.

    Clamped to 1..60 minutes on every path. The header is a Unix timestamp from
    someone else's clock, so it can be missing, malformed, already in the past,
    or absurdly far out, and none of those may reach the user as "retry in -3
    minutes" or "retry in 9 hours". A missing or unparseable value falls back to
    ten minutes, which is the right order of magnitude for the hourly window.

    `now` is injected so tests pin the arithmetic instead of racing the clock.
    """
    moment = time.time() if now is None else now
    try:
        seconds = float(reset_header) - moment  # type: ignore[arg-type]
    except (TypeError, ValueError):
        seconds = float(RATE_LIMIT_WAIT_FALLBACK_SECONDS)
    if not math.isfinite(seconds):  # "nan" and "inf" parse as floats
        seconds = float(RATE_LIMIT_WAIT_FALLBACK_SECONDS)
    # Round up: a 61-second wait reported as "1 minute" sends the user back too
    # early, and being a minute pessimistic costs nothing.
    minutes = int(seconds // 60) + 1
    return max(RATE_LIMIT_WAIT_MIN_MINUTES, min(RATE_LIMIT_WAIT_MAX_MINUTES, minutes))


# ---------------------------------------------------------------------------
# Metadata (Requirements 2 AC4, AC6, AC7, 3 AC8)
# ---------------------------------------------------------------------------


def get_metadata(ref: RepoRef, transport: Transport | None = None) -> RepoMeta:
    """Read repository metadata and reject a repository that is too large.

    The size guard runs here rather than in the caller so that an oversized
    repository cannot be measured and then listed anyway (Requirement 3 AC8,
    Property 9). Returning a `RepoMeta` therefore means the repository is within
    every bound this module knows how to check.

    `default_branch` is the one field with no sensible fallback. Requirement 2
    AC4 is to read the default branch, so guessing `main` when GitHub did not
    say so would quietly read a different reference -- or a nonexistent one.
    An absent value is a malformed answer and is reported as such.
    """
    data = _api_get(f"/repos/{_segment(ref.owner)}/{_segment(ref.repo)}", transport)

    branch = data.get("default_branch")
    if not isinstance(branch, str) or not branch.strip():
        raise AgentError(ErrorCode.GITHUB_UNAVAILABLE)

    language = data.get("language")
    meta = RepoMeta(
        slug=str(data.get("full_name") or ref.slug),
        default_branch=branch,
        size_kb=_as_int(data.get("size")),  # KILOBYTES, see the module docstring
        language=language if isinstance(language, str) and language else None,
        description=str(data.get("description") or "").strip(),
        html_url=str(data.get("html_url") or f"https://github.com/{ref.slug}"),
        archived=bool(data.get("archived")),
    )
    guard_repo_size(meta)
    return meta


def guard_repo_size(meta: RepoMeta) -> None:
    """Reject a repository over 100 MB. Requirement 3 AC8.

    `size_kb` is kilobytes, so the comparison is against 102_400 rather than
    100_000_000. Both figures go into the error so the copy can name the
    repository's actual size alongside the limit.
    """
    if meta.size_kb > MAX_REPO_SIZE_KB:
        raise AgentError(
            ErrorCode.REPO_TOO_LARGE,
            size_mb=max(1, round(meta.size_kb / 1024)),
            limit_mb=MAX_REPO_SIZE_MB,
        )


# ---------------------------------------------------------------------------
# Tree listing (Requirements 2 AC4, 3 AC9)
# ---------------------------------------------------------------------------


def list_tree(
    ref: RepoRef, branch: str, transport: Transport | None = None
) -> list[TreeEntry]:
    """List every entry on `branch` in one recursive call.

    `?recursive=1` is what keeps this to a single metered request instead of one
    per directory. The branch is quoted with `safe=""` because the endpoint
    takes it as a single path segment: a default branch named `release/2.0`
    would otherwise read as a path and 404.

    Returns the entries rather than the `(entries, truncated)` pair the design
    sketch showed. `truncated` is not a value a caller can act on -- a partial
    tree cannot support an honest file-count guard, so it is always a
    `REPO_TOO_MANY_FILES` failure -- and returning a flag that is only ever
    `False` would be dead surface inviting a caller to ignore it.

    Raises:
        AgentError: `REPO_TOO_MANY_FILES` when GitHub truncated the listing or
            the branch holds more than 5,000 files (Requirement 3 AC9), and the
            codes `_api_get` maps for transport and status failures.
    """
    quoted = urllib.parse.quote(branch, safe="")
    data = _api_get(
        f"/repos/{_segment(ref.owner)}/{_segment(ref.repo)}"
        f"/git/trees/{quoted}?recursive=1",
        transport,
    )

    raw_entries = data.get("tree")
    if not isinstance(raw_entries, list):
        raise AgentError(ErrorCode.GITHUB_UNAVAILABLE)

    entries = [
        TreeEntry(
            path=path, kind=str(item.get("type") or ""), size=_as_int(item.get("size"))
        )
        for item in raw_entries
        if isinstance(item, dict) and isinstance(path := item.get("path"), str) and path
    ]
    guard_tree_file_count(entries, truncated=bool(data.get("truncated")))
    return entries


def guard_tree_file_count(entries: Sequence[TreeEntry], truncated: bool) -> None:
    """Reject a tree past 5,000 files, or one GitHub itself cut short.

    Requirement 3 AC9 counts files, so trees and submodule pointers do not
    count against the limit -- only blobs.

    `truncated: true` means GitHub stopped listing before the tree ended. The
    count in hand is then a floor, not a measurement, and a guard that cannot
    measure honestly must refuse rather than pass a partial tree downstream and
    present an incomplete survey as a complete one. No `file_count` is attached
    in that case: the real number is unknown, and the copy does not name it.
    """
    if truncated:
        raise AgentError(ErrorCode.REPO_TOO_MANY_FILES)
    blobs = sum(1 for entry in entries if entry.kind == "blob")
    if blobs > MAX_TREE_FILES:
        raise AgentError(ErrorCode.REPO_TOO_MANY_FILES, file_count=blobs)


# ---------------------------------------------------------------------------
# Raw file bodies (Requirements 2 AC3, AC4)
# ---------------------------------------------------------------------------


def read_files(
    ref: RepoRef,
    branch: str,
    paths: Iterable[str],
    transport: Transport | None = None,
) -> dict[str, bytes]:
    """Fetch file bodies in parallel, skipping whatever will not come back.

    Eight workers against twelve files is two waves of I/O-bound waiting, which
    is what threads are for -- the work is entirely spent blocked on a socket,
    so the GIL never becomes the bottleneck. `maxsize` on the pool matches the
    worker count so the connections are reused rather than contended.

    A fetch that fails for any reason is simply absent from the result: a 404
    from a symlink or submodule pointer, a timeout, a connection reset. One
    unreadable file out of twelve must not cost the user their orientation, so
    the caller pairs this with `unread_paths` and reports the gap in the
    context instead (Requirement 3 AC10, AC11 handle the content-based skips).

    Returns:
        Bodies keyed by path, in the order the paths were given. Insertion order
        is preserved so everything downstream of here stays deterministic.
    """
    # `dict.fromkeys` is the order-preserving unique: a duplicate path would
    # otherwise spend a second fetch to overwrite itself.
    wanted = list(dict.fromkeys(paths))
    if not wanted:
        return {}

    pool = _POOL if transport is None else transport
    # The raw host takes the branch inside the path, so its slashes stay as
    # slashes -- `safe="/"` is the default and is correct here, where `safe=""`
    # was correct for the tree endpoint's single segment.
    prefix = (
        f"{RAW}/{_segment(ref.owner)}/{_segment(ref.repo)}/{urllib.parse.quote(branch)}"
    )

    def fetch(path: str) -> tuple[str, bytes | None]:
        try:
            response = pool.request(
                "GET", f"{prefix}/{urllib.parse.quote(path)}", headers=RAW_HEADERS
            )
        except (urllib3.exceptions.HTTPError, OSError):
            return path, None
        return path, response.data if response.status == 200 else None

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=RAW_FETCH_WORKERS, thread_name_prefix="raw-fetch"
    ) as workers:
        # `map` yields in input order, which is what keeps the result ordered.
        results = list(workers.map(fetch, wanted))
    return {path: body for path, body in results if body is not None}


def unread_paths(requested: Iterable[str], bodies: Mapping[str, bytes]) -> list[str]:
    """The paths that were asked for and did not arrive, in request order.

    Separate from `read_files` so the degradation is something the caller holds
    and reports rather than something it can forget to notice. Content-based
    skips -- binary, undecodable -- are `selection.decode_text`'s to add.
    """
    return [path for path in dict.fromkeys(requested) if path not in bodies]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _segment(value: str) -> str:
    """Escape one path segment.

    `url_parse` already constrains owner and repository names to characters that
    need no escaping, so against a parsed `RepoRef` this is the identity. It
    runs anyway, because a `RepoRef` built by hand in a later task should not be
    able to inject a path.
    """
    return urllib.parse.quote(value, safe="")


def _as_int(value: object) -> int:
    """Coerce a JSON number to a non-negative int, defaulting to 0.

    GitHub omits `size` on tree entries and reports `0` for directories, and a
    malformed value must not raise: a `TypeError` here would surface as
    `INTERNAL` ("something broke on my side") when the truthful answer is that
    GitHub sent something unexpected about one file.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if not math.isfinite(value):
        return 0
    return max(0, int(value))
