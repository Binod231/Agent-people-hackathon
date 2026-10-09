"""Tests for `agent.url_parse`. Requirement 2 AC2, AC5.

Two halves. The example-based cases come first: the four accepted surface forms
and one named instance of each rejection class the design calls out, so a wrong
charset or a missing guard fails on its own rather than inside a property.
Property 1 follows at the bottom -- the universal statement that every accepted
form of one repository canonicalizes to one `RepoRef`, and that every string
outside the accepted shapes raises `BAD_URL` and never yields a `RepoRef`.
"""

from __future__ import annotations

import string
from collections.abc import Callable

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agent.errors import AgentError, ErrorCode
from agent.models import RepoRef
from agent.url_parse import RESERVED_OWNERS, parse_repo_url

# The four surface forms Requirement 2 AC2 permits: bare, trailing slash,
# `.git` suffix, and both.
FOUR_FORMS = [
    "https://github.com/pallets/flask",
    "https://github.com/pallets/flask/",
    "https://github.com/pallets/flask.git",
    "https://github.com/pallets/flask.git/",
]


@pytest.mark.parametrize("url", FOUR_FORMS)
def test_accepts_each_surface_form(url: str) -> None:
    assert parse_repo_url(url) == RepoRef(owner="pallets", repo="flask")


def test_all_four_forms_canonicalize_identically() -> None:
    """One repository, one `RepoRef`, whichever form the user pasted."""
    refs = {parse_repo_url(url) for url in FOUR_FORMS}
    assert len(refs) == 1


def test_surrounding_whitespace_is_tolerated() -> None:
    """Copy-paste picks up whitespace; it says nothing about intent."""
    assert parse_repo_url("  https://github.com/pallets/flask\n") == RepoRef(
        owner="pallets", repo="flask"
    )


@pytest.mark.parametrize(
    ("label", "url"),
    [
        ("no_tls", "http://github.com/pallets/flask"),
        ("ssh_scp_form", "git@github.com:pallets/flask.git"),
        ("ssh_url_form", "ssh://git@github.com/pallets/flask.git"),
        ("gist", "https://gist.github.com/pallets/aaaa1111"),
        ("deeper_path_tree", "https://github.com/pallets/flask/tree/main/src"),
        ("deeper_path_blob", "https://github.com/pallets/flask/blob/main/README.md"),
        ("credentials_userinfo", "https://user:pw@github.com/pallets/flask"),
        ("credentials_user_only", "https://pallets@github.com/pallets/flask"),
        ("owner_only", "https://github.com/pallets"),
        ("host_only", "https://github.com/"),
        ("other_host", "https://gitlab.com/pallets/flask"),
        ("lookalike_host", "https://github.com.evil.example/pallets/flask"),
        ("query_string", "https://github.com/pallets/flask?tab=readme"),
        ("fragment", "https://github.com/pallets/flask#install"),
        ("reserved_owner", "https://github.com/settings/flask"),
        ("dot_repo", "https://github.com/pallets/."),
        ("dotdot_repo", "https://github.com/pallets/.."),
        ("bare_dotgit_repo", "https://github.com/pallets/.git"),
        ("empty_owner", "https://github.com//flask"),
        ("embedded_newline", "https://github.com/pallets/flask\nrm -rf /"),
        ("empty_string", ""),
    ],
)
def test_rejects_every_non_accepted_form(label: str, url: str) -> None:
    with pytest.raises(AgentError) as caught:
        parse_repo_url(url)
    assert caught.value.code is ErrorCode.BAD_URL


@pytest.mark.parametrize("raw", [None, 123, [], {}])
def test_rejects_non_string_input(raw: object) -> None:
    """The URL arrives from an untrusted JSON body; the field may be anything."""
    with pytest.raises(AgentError) as caught:
        parse_repo_url(raw)  # type: ignore[arg-type]
    assert caught.value.code is ErrorCode.BAD_URL


def test_rejection_carries_no_detail() -> None:
    """A rejected URL may hold a credential, so none of it is echoed back.

    Requirement 2 AC5's copy names the expected form and one valid example; it
    never quotes the offending string. Requirement 6 AC7 is why.
    """
    with pytest.raises(AgentError) as caught:
        parse_repo_url("https://user:AKIAIOSFODNN7EXAMPLE@github.com/o/r")
    assert caught.value.params == {}
    assert "AKIA" not in str(caught.value)


def test_dotted_repo_name_keeps_its_suffix() -> None:
    """Only a trailing `.git` is the clone suffix; other dots are the name."""
    assert parse_repo_url("https://github.com/owner/repo.github.io") == RepoRef(
        owner="owner", repo="repo.github.io"
    )


def test_slug_is_the_canonical_pair() -> None:
    assert parse_repo_url("https://github.com/gin-gonic/gin.git").slug == "gin-gonic/gin"


def test_www_subdomain_is_a_fifth_accepted_form() -> None:
    """Observed behavior, recorded so the property below can exclude it.

    Requirement 2 AC2 and Property 1 both describe four surface forms. The
    implemented pattern carries an optional `www.`, which makes `www.github.com`
    a fifth accepted spelling of each of them. It is harmless -- the host
    redirects to the apex and the `RepoRef` is identical -- but the rejection
    property has to know about it, so it is asserted here rather than hidden
    inside a generator filter. Flagged for the design text.
    """
    assert parse_repo_url("https://www.github.com/pallets/flask") == RepoRef(
        owner="pallets", repo="flask"
    )
    assert parse_repo_url("https://www.github.com/pallets/flask.git/") == RepoRef(
        owner="pallets", repo="flask"
    )


# ---------------------------------------------------------------------------
# Property 1: Repository URL canonicalization and rejection
# ---------------------------------------------------------------------------

_ALNUM = string.ascii_letters + string.digits
_OWNER_CHARS = _ALNUM + "-"
_REPO_CHARS = _ALNUM + "_.-"

# Characters outside both charsets. `/` is left out deliberately: it does not
# garble a URL, it reshapes it into a different (possibly valid) one, which the
# deeper-path and trailing-slash mutators below cover on purpose.
_FORBIDDEN_CHARS = "~!$&'()*+,;=:%@<>^|\\{}[]\"`? #\t"

_ACCEPTED_PREFIXES = ("https://github.com/", "https://www.github.com/")


def _is_owner(segment: str) -> bool:
    """GitHub's owner charset: 1-39 chars, alphanumeric at both ends."""
    return (
        1 <= len(segment) <= 39
        and segment[0] in _ALNUM
        and segment[-1] in _ALNUM
        and all(char in _OWNER_CHARS for char in segment)
        and segment.lower() not in RESERVED_OWNERS
    )


def _is_repo(segment: str) -> bool:
    """GitHub's repository charset, minus the path-traversal shapes."""
    return (
        1 <= len(segment) <= 100
        and not segment.startswith(".")
        and all(char in _REPO_CHARS for char in segment)
    )


def _is_accepted_form(raw: str) -> bool:
    """A second, independent reading of which strings Requirement 2 AC2 accepts.

    Built from plain string surgery rather than from `url_parse`'s regex: a
    filter that reused the implementation's own pattern would make the rejection
    property a tautology. Its only job is to let the generators below skip the
    rare mutation that lands back on an accepted shape, so it answers *is this
    one of the accepted spellings*, not *what does it canonicalize to*.
    """
    candidate = raw.strip()
    # No accepted form carries interior whitespace, and checking that first
    # keeps this oracle away from the normalization stdlib URL parsers apply.
    if any(char.isspace() for char in candidate):
        return False
    for prefix in _ACCEPTED_PREFIXES:
        if candidate.startswith(prefix):
            rest = candidate[len(prefix) :]
            break
    else:
        return False
    # Same suffix order as the accepted form: `.git` then an optional slash.
    if rest.endswith("/"):
        rest = rest[:-1]
    if rest.endswith(".git"):
        rest = rest[:-4]
    owner, slash, repo = rest.partition("/")
    return bool(slash) and _is_owner(owner) and _is_repo(repo)


@st.composite
def owners(draw: st.DrawFn) -> str:
    """Owner names from GitHub's permitted set, hyphens interior only."""
    head = draw(st.sampled_from(_ALNUM))
    middle = draw(st.text(alphabet=_OWNER_CHARS, max_size=37))
    # A single-character owner is legal; anything longer must also end
    # alphanumeric, so the drawn middle gets an alphanumeric cap.
    tail = "" if not middle and draw(st.booleans()) else draw(st.sampled_from(_ALNUM))
    return head + middle + tail


def repos() -> st.SearchStrategy[str]:
    """Repository names from GitHub's permitted set.

    Two exclusions, both of them GitHub's own rules rather than conveniences:
    a name may not begin with `.` (the implementation rejects the
    path-traversal shapes), and a name may not end with `.git`. The second one
    is why the four forms collapse at all -- `github.com/o/x.git` cannot be both
    the repository `x.git` and the repository `x` with a clone suffix, and
    GitHub resolves that ambiguity by refusing the name.
    """
    return st.text(alphabet=_REPO_CHARS, min_size=1, max_size=100).filter(
        lambda repo: not repo.startswith(".") and not repo.endswith(".git")
    )


@given(owner=owners().filter(lambda o: o.lower() not in RESERVED_OWNERS), repo=repos())
def test_property_1_four_surface_forms_canonicalize_identically(
    owner: str, repo: str
) -> None:
    """Feature: delightful-aws-agent, Property 1: Repository URL canonicalization and rejection.

    For any owner and repository name drawn from GitHub's permitted character
    sets, all four accepted surface forms of that repository's URL -- bare, with
    a trailing slash, with a `.git` suffix, and with both -- parse to one
    identical `RepoRef`.

    **Validates: Requirements 2.2**
    """
    base = f"https://github.com/{owner}/{repo}"
    forms = [base, f"{base}/", f"{base}.git", f"{base}.git/"]

    refs = {parse_repo_url(form) for form in forms}

    assert refs == {RepoRef(owner=owner, repo=repo)}


def _mutators() -> tuple[Callable[[str, str], str], ...]:
    """One rejection class per entry, each built around a valid owner and repo.

    Keeping the valid pair in place is the point: these inputs differ from an
    accepted URL in exactly one respect, so a pattern that is loose in one
    place cannot hide behind a path that is wrong in three.
    """
    return (
        lambda o, r: f"http://github.com/{o}/{r}",  # no TLS
        lambda o, r: f"git@github.com:{o}/{r}.git",  # SSH, scp form
        lambda o, r: f"ssh://git@github.com/{o}/{r}.git",  # SSH, URL form
        lambda o, r: f"git://github.com/{o}/{r}.git",
        lambda o, r: f"https://gist.github.com/{o}/{r}",  # a gist is not a tree
        lambda o, r: f"https://raw.githubusercontent.com/{o}/{r}",
        lambda o, r: f"https://api.github.com/repos/{o}/{r}",
        lambda o, r: f"https://github.com/{o}/{r}/tree/main/src",  # deeper path
        lambda o, r: f"https://github.com/{o}/{r}/blob/main/README.md",
        lambda o, r: f"https://github.com/{o}/{r}/issues/1",
        lambda o, r: f"https://user:pw@github.com/{o}/{r}",  # credential-bearing
        lambda o, r: f"https://{o}:ghp_AKIAIOSFODNN7@github.com/{o}/{r}",
        lambda o, r: f"https://{o}@github.com/{o}/{r}",
        lambda o, r: f"https://github.com/{o}",  # owner only
        lambda o, r: f"https://github.com//{r}",  # empty owner
        lambda o, r: f"https://github.com/{o}//",  # empty repo
        lambda o, r: f"https://github.com/{o}/{r}?tab=readme",  # query
        lambda o, r: f"https://github.com/{o}/{r}#installation",  # fragment
        lambda o, r: f"https://GitHub.com/{o}/{r}",  # host case
        lambda o, r: f"https://github.COM/{o}/{r}",
        lambda o, r: f"HTTPS://github.com/{o}/{r}",  # scheme case
        lambda o, r: f"https://gitlab.com/{o}/{r}",  # another forge
        lambda o, r: f"https://bitbucket.org/{o}/{r}",
        lambda o, r: f"https://github.com.evil.example/{o}/{r}",  # lookalike host
        lambda o, r: f"https://notgithub.com/{o}/{r}",
        lambda o, r: f"https://wwwgithub.com/{o}/{r}",
        lambda o, r: f"//github.com/{o}/{r}",  # scheme-relative
        lambda o, r: f"github.com/{o}/{r}",  # no scheme
        lambda o, r: f"https://github.com/{o}/.{r}",  # dot-leading repo
        lambda o, r: f"https://github.com/{o}{'a' * 40}/{r}",  # owner over 39
        lambda o, r: f"https://github.com/{o}/{r}{'a' * 101}",  # repo over 100
    )


@st.composite
def near_miss_urls(draw: st.DrawFn) -> str:
    """A valid owner and repository wrapped in a shape that is not accepted."""
    owner, repo = draw(owners()), draw(repos())
    return draw(st.sampled_from(_mutators()))(owner, repo)


@st.composite
def reserved_owner_urls(draw: st.DrawFn) -> str:
    """GitHub's own top-level pages, which are site paths and not repositories."""
    owner = draw(st.sampled_from(sorted(RESERVED_OWNERS)))
    cased = draw(st.sampled_from([owner, owner.upper(), owner.capitalize()]))
    return f"https://github.com/{cased}/{draw(repos())}"


@st.composite
def garbled_urls(draw: st.DrawFn) -> str:
    """An accepted URL with one character the charsets do not allow spliced in."""
    url = f"https://github.com/{draw(owners())}/{draw(repos())}"
    bad = draw(st.sampled_from(_FORBIDDEN_CHARS))
    # Interior only. A stray character at the very end could be whitespace,
    # which `parse_repo_url` strips before matching -- that is tolerance of
    # copy-paste, not a loose pattern.
    at = draw(st.integers(min_value=len("https://github.com/"), max_value=len(url) - 1))
    return url[:at] + bad + url[at:]


NON_FORMS = [
    "",
    "   ",
    "\n",
    "https://github.com",
    "https://github.com/",
    "https://www.github.com/",
    "github.com",
    "pallets/flask",
    "not a url at all",
    "javascript:alert(1)",
    "file:///etc/passwd",
    "data:text/html,<script>alert(1)</script>",
    "https://github.com/pallets/flask/../../etc/passwd",
    "https://github.com/pallets/flask%2F..%2Fsecrets",
    "https://github.com/pallets/flask\nrm -rf /",
]

rejected_strings = st.one_of(
    st.text(max_size=120),
    # URL-shaped alphabet: raises the odds of a draw that is interesting rather
    # than obviously junk, without constraining it to the mutators above.
    st.text(alphabet="abzAZ09-._~:/?#@!$&'()*+,;=%" + "github.com", max_size=60),
    st.sampled_from(NON_FORMS),
    near_miss_urls(),
    reserved_owner_urls(),
    garbled_urls(),
).filter(lambda candidate: not _is_accepted_form(candidate))


# The design sets 100 examples as the floor. This half draws from a far wider
# space than the canonicalization half, so it gets more.
@settings(max_examples=300)
@given(url=rejected_strings)
def test_property_1_every_string_outside_the_accepted_forms_is_rejected(
    url: str,
) -> None:
    """Feature: delightful-aws-agent, Property 1: Repository URL canonicalization and rejection.

    For any string that does not match the accepted form, parsing raises
    `BAD_URL` and never yields a `RepoRef`.

    **Validates: Requirements 2.2, 2.5**
    """
    with pytest.raises(AgentError) as caught:
        parse_repo_url(url)

    assert caught.value.code is ErrorCode.BAD_URL
