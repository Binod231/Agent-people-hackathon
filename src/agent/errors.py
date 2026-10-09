"""The error taxonomy: one code per failure, one exception type at the boundary.

`ERROR_COPY` is the single source of truth for every failure the user can be
told about: the HTTP status and the exact user-facing text, one row per
`ErrorCode`. There is no second place where error copy is written. Requirement 6
AC1 asks every error to state its cause in plain language and name one next
action, so each row does both -- the cause first, the action in the closing
sentence.

`AgentError` is the only exception type that ever becomes a user-facing message.
Two filters stand between an exception and the rendered text:

- **At construction**, `params` is narrowed to the keys in `ALLOWED_PARAMS`.
- **At render**, a param is interpolated only if its value is a small plain
  number. A string is refused even under a whitelisted key, because a string is
  the one type that could carry an ARN, an account id, a path, or a token.

Rendered copy is therefore static text plus small numbers, which is what makes
Requirement 6 AC6 structural rather than a filter someone has to maintain. And
`render()` cannot raise: every lookup and substitution has a fallback, so a
failure in the error path can never turn a clean message into an unhandled 500.

Criterion numbers here are the post-revision ones (see the design's
`## Lean Deployment Revision`). `RATE_LIMITED` is gone with the API Gateway
usage plan that produced it: no quota, no 429, no copy to explain one.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from string import Formatter

__all__ = ["ALLOWED_PARAMS", "ERROR_COPY", "AgentError", "ErrorCode", "render"]


class ErrorCode(str, Enum):
    """Every failure the user can be told about.

    Each member names the acceptance criterion it serves; `ERROR_COPY` carries
    the HTTP status and the copy.
    """

    BAD_URL = "BAD_URL"  # Requirement 2 AC5
    REPO_NOT_FOUND = "REPO_NOT_FOUND"  # Requirement 2 AC6
    GITHUB_RATE_LIMITED = "GITHUB_RATE_LIMITED"  # Requirement 2 AC7
    GITHUB_UNAVAILABLE = "GITHUB_UNAVAILABLE"  # Requirement 6 AC1
    REPO_TOO_LARGE = "REPO_TOO_LARGE"  # Requirement 3 AC8
    REPO_TOO_MANY_FILES = "REPO_TOO_MANY_FILES"  # Requirement 3 AC9
    NO_READABLE_FILES = "NO_READABLE_FILES"  # Requirement 3 AC10, AC11
    BODY_TOO_LARGE = "BODY_TOO_LARGE"  # Requirement 8 AC1
    MODEL_BUSY = "MODEL_BUSY"  # Requirement 6 AC2, AC3
    DEADLINE = "DEADLINE"  # Requirement 4 AC6, 5 AC6
    INTERNAL = "INTERNAL"  # Requirement 6 AC4


# The only keys that may be interpolated into user-facing copy. Every one is a
# number the user needs in order to act on the message. Requirement 6 AC6.
ALLOWED_PARAMS = {"size_mb", "file_count", "minutes", "limit_mb"}


@dataclass
class AgentError(Exception):
    """The only exception type that ever reaches the user.

    `params` is restricted to the whitelist in ALLOWED_PARAMS. Exception text,
    tracebacks, ARNs and account ids have no path into the rendered copy -- that
    is what makes Requirement 6 AC6 structural rather than a convention someone
    has to remember.
    """

    code: ErrorCode
    params: dict[str, str | int] = field(default_factory=dict)

    def __init__(self, code: ErrorCode, **params: str | int) -> None:
        self.code = code
        self.params = {k: v for k, v in params.items() if k in ALLOWED_PARAMS}
        super().__init__(code.value)


# ---------------------------------------------------------------------------
# The mapping table (design: Component 5 -> The Mapping Table)
#
# One row per ErrorCode: (HTTP status, user-facing copy). The text is the
# design's verbatim, including the two placeholders, which are the only values
# render() will ever substitute.
#
# Two status choices are deliberate and defended in the design: 413 belongs to
# the request body alone (Requirement 8 AC1), so an oversize *repository* is 422
# -- the request was fine, the referenced resource was not; and a GitHub rate
# limit is 503 rather than 429, so the Chat_UI can tell "GitHub is throttling
# me" from anything the agent itself did.
# ---------------------------------------------------------------------------

ERROR_COPY: dict[ErrorCode, tuple[int, str]] = {
    ErrorCode.BAD_URL: (
        400,
        "That does not look like a GitHub repository URL. I need the plain"
        " repository address, like `https://github.com/pallets/flask` — no"
        " `/tree/main` or `/blob/` path on the end. Paste the repository's home"
        " page URL and I will take a look.",
    ),
    ErrorCode.REPO_NOT_FOUND: (
        404,
        "I could not reach that repository. It is either private or it does not"
        " exist — I can only read public repositories, since I talk to GitHub"
        " without any credentials. Double-check the owner and repository name,"
        " or try a public repository instead.",
    ),
    ErrorCode.GITHUB_RATE_LIMITED: (
        503,
        "GitHub is rate limiting me at the moment. I read repositories"
        " anonymously, so I get a modest hourly allowance and this demo shares"
        " it. Give it about **{minutes} minutes** and send that URL again — it"
        " should go straight through.",
    ),
    ErrorCode.GITHUB_UNAVAILABLE: (
        503,
        "GitHub did not answer that request. This is usually brief and nothing"
        " to do with your repository. Try again in a few seconds, and if it"
        " keeps failing, check whether GitHub is reporting an incident.",
    ),
    ErrorCode.REPO_TOO_LARGE: (
        422,
        "That repository is about **{size_mb} MB**, and I stop at **100 MB** so"
        " I can stay fast and cheap on a public demo. Point me at a smaller"
        " repository, or at one service out of a monorepo if the project is"
        " split that way.",
    ),
    ErrorCode.REPO_TOO_MANY_FILES: (
        422,
        "That repository has more than **5,000 files** on its default branch,"
        " which is past what I can survey in one pass. I am built for a quick"
        " orientation rather than a full index — try a smaller repository, or a"
        " single package from a larger one.",
    ),
    ErrorCode.NO_READABLE_FILES: (
        422,
        "I reached that repository, but I could not find any readable text files"
        " on its default branch — everything I looked at was binary, over my"
        " 500 KB per-file limit, or the branch is empty. Try a repository with"
        " source files or a README near the top level.",
    ),
    ErrorCode.BODY_TOO_LARGE: (
        413,
        "That request is larger than the **8 KB** I accept, counting the whole"
        " conversation so far. The cap keeps this demo affordable. Start a fresh"
        " session to drop the history, or shorten the question and send it"
        " again.",
    ),
    ErrorCode.MODEL_BUSY: (
        503,
        "The model is busy right now. I already tried three times with a pause"
        " between attempts, so this is load on my side rather than anything"
        " about your question. Give it a few seconds and send that again.",
    ),
    ErrorCode.DEADLINE: (
        504,
        "That took longer than I allow myself for one answer, so I stopped"
        " rather than leave you watching a spinner. Large repositories are the"
        " usual cause. Try again, or try a smaller repository.",
    ),
    ErrorCode.INTERNAL: (
        500,
        "Something broke on my side, and it is not your repository's fault. I"
        " have written the details to my logs. Try that again, and if it keeps"
        " happening try a different repository so I can tell whether it is"
        " specific to this one.",
    ),
}


# Every whitelisted param is a human-scale count: minutes clamps to 1..60,
# size_mb tops out in the hundreds, file_count in the thousands. The magnitude
# bound is what keeps an account-id-shaped integer (twelve digits) from reaching
# the user through a numeric param -- Requirement 6 AC6 again, from the other
# side.
_PARAM_MAX = 10_000_000


def _placeholders(template: str) -> set[str]:
    """The field names `str.format` would substitute in `template`.

    Returns the empty set for a template `Formatter` cannot parse, which keeps
    the import-time check and `render()` honest about malformed copy instead of
    raising on it.
    """
    try:
        return {name for _, name, _, _ in Formatter().parse(template) if name}
    except ValueError:
        return set()


# Import-time completeness. render() indexes ERROR_COPY by code, so a missing
# row would be a KeyError in the one code path that must never fail. Asserting
# it here means a new ErrorCode without copy cannot even be imported, let alone
# deployed. tests/test_errors.py asserts the same thing, since `python -O`
# strips assert statements.
assert set(ERROR_COPY) == set(ErrorCode), (
    "ERROR_COPY must have exactly one row per ErrorCode; missing "
    f"{sorted(c.value for c in set(ErrorCode) - set(ERROR_COPY))}, unexpected "
    f"{sorted(str(c) for c in set(ERROR_COPY) - set(ErrorCode))}"
)

# And every placeholder in the copy is whitelisted, so the un-interpolated
# fallback in render() is unreachable for the shipped table rather than merely
# unlikely.
assert not {
    name
    for _, text in ERROR_COPY.values()
    for name in _placeholders(text)
    if name not in ALLOWED_PARAMS
}, "ERROR_COPY may only interpolate names in ALLOWED_PARAMS"


def _numeric(value: object) -> str | None:
    """Render one param value, or `None` if it is not a small plain number.

    The narrowness is the point. A `str` is refused even under a whitelisted
    key: it is the one type that could carry an ARN, an account id, a file path,
    or a token, and Requirement 6 AC6 keeps all three out of user-facing text.
    `bool` is refused because it is an `int` subclass and "about True minutes"
    is not a message. Negatives, non-finite floats, and anything past
    `_PARAM_MAX` are refused because no legitimate value is near them.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if not 0 <= value <= _PARAM_MAX:
        return None
    return str(value) if isinstance(value, int) else str(round(value))


def _interpolate(template: str, params: Mapping[str, object]) -> str:
    """Substitute whitelisted numeric params, or return the copy as it stands.

    All-or-nothing on purpose: if any placeholder is missing, non-numeric, or
    not on the whitelist, the static template is returned un-interpolated rather
    than half-filled. A visible `{minutes}` in a degraded message is a worse
    read than a number, and a much better outcome than an exception thrown from
    the error path.
    """
    values: dict[str, str] = {}
    for name in _placeholders(template):
        if name not in ALLOWED_PARAMS:
            return template
        rendered = _numeric(params.get(name))
        if rendered is None:
            return template
        values[name] = rendered
    try:
        return template.format(**values)
    except (IndexError, KeyError, ValueError):  # malformed copy, not bad input
        return template


def render(error: AgentError) -> tuple[int, dict[str, dict[str, str]]]:
    """Turn an `AgentError` into `(http_status, body)`. Requirement 6 AC1, AC6.

    The copy comes from a static template keyed by code; the only values
    interpolated are whitelisted numeric params. No exception string, ARN,
    account id, path, or traceback can reach this output -- none of them is ever
    in `params`, and a non-numeric value under a whitelisted key is dropped
    rather than printed.

    This function cannot raise. An unrecognized code renders as `INTERNAL`, a
    mangled `params` renders as an empty one, and a template that cannot be
    filled renders un-interpolated. The handler's `except AgentError` branch
    calls it, so anything it threw would surface as an unhandled 500 and lose
    the message the user was supposed to see.
    """
    try:
        code = ErrorCode(error.code)
    except (ValueError, TypeError, AttributeError):
        code = ErrorCode.INTERNAL

    status, template = ERROR_COPY[code]
    params = getattr(error, "params", None)
    message = _interpolate(template, params if isinstance(params, Mapping) else {})
    return status, {"error": {"code": code.value, "message": message}}
