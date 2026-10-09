"""Repository URL validation and canonicalization. Requirement 2 AC2, AC5.

Requirement 2 AC2 allows exactly one shape -- `https://github.com/{owner}/{repo}`
-- with an optional `.git` suffix or trailing slash. That gives four accepted
surface forms, and all four canonicalize to one identical `RepoRef` so nothing
downstream has to care which one the user pasted (Property 1).

What is rejected is as deliberate as what is accepted:

- `http://` -- no TLS, and silently upgrading a scheme the user typed is not
  this function's call to make.
- `git@github.com:owner/repo.git` -- the SSH form implies a credential we do not
  have and must never want (Requirement 2 AC3 is unauthenticated access only).
- `gist.github.com` -- a gist is not a repository tree.
- Deeper paths such as `/owner/repo/tree/main/src` -- a sub-path is a different
  request than the one this agent makes, which is always the default branch
  (Requirement 2 AC4).
- Credential-bearing URLs such as `https://user:pw@github.com/owner/repo` -- this
  one matters most. The parsed reference reaches logs and user-facing copy, so a
  URL carrying a secret must fail at the boundary rather than be sanitized later
  and hope every path remembered to.

Rejection is a single `AgentError(BAD_URL)` with no detail attached: the copy the
user sees names the expected form and one valid example (Requirement 2 AC5), and
echoing the offending string back would defeat the credential rejection above.
"""

from __future__ import annotations

import re

from .errors import AgentError, ErrorCode
from .models import RepoRef

__all__ = ["RESERVED_OWNERS", "RepoRef", "parse_repo_url"]

# The pattern is the authority on what an owner and a repository name may
# contain. GitHub owner: alphanumeric and hyphens, 1-39 characters, never
# leading or trailing a hyphen. GitHub repo: alphanumeric, hyphen, underscore,
# and period, 1-100 characters.
#
# The repo group is lazy so the optional `.git` suffix claims the last four
# characters instead of being absorbed into the name -- that is what makes
# `.../repo` and `.../repo.git` land on the same `RepoRef`. Both quantifiers are
# bounded, so there is no backtracking blowup to worry about on hostile input.
_PATTERN = re.compile(
    r"^https://(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/"
    r"(?P<repo>[A-Za-z0-9_.-]{1,100}?)"
    r"(?:\.git)?/?$"
)

# GitHub's reserved top-level paths. None can be an account name, so a URL
# shaped like `github.com/settings/repo` is a site page, not a repository.
RESERVED_OWNERS = frozenset(
    {
        "settings",
        "orgs",
        "topics",
        "sponsors",
        "collections",
        "marketplace",
        "explore",
        "notifications",
        "pulls",
        "issues",
    }
)


def parse_repo_url(raw: str | None) -> RepoRef:
    """Canonicalize a GitHub repository URL. Requirement 2 AC2, AC5.

    Args:
        raw: The submitted URL. Typed optional because this arrives from an
            untrusted JSON body, where the field may be missing or not a string;
            anything that is not a string is simply not the accepted form.

    Returns:
        The canonical `RepoRef`, identical across all four accepted surface
        forms of the same repository.

    Raises:
        AgentError: With `ErrorCode.BAD_URL` for every input that is not one of
            those four forms.
    """
    # Surrounding whitespace survives most copy-paste paths and says nothing
    # about intent. Everything inside the string still has to match exactly.
    candidate = raw.strip() if isinstance(raw, str) else ""
    match = _PATTERN.match(candidate)
    if not match:
        raise AgentError(ErrorCode.BAD_URL)

    owner, repo = match.group("owner"), match.group("repo")
    # ".", "..", and a bare ".git" are path-traversal shaped, not repo names.
    if repo in {".", ".."} or repo.startswith(".") or owner.lower() in RESERVED_OWNERS:
        raise AgentError(ErrorCode.BAD_URL)
    return RepoRef(owner=owner, repo=repo)
