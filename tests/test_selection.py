"""Property 4 over `agent.selection`. Requirement 3 AC1, AC3.

Feature: delightful-aws-agent, Property 4: File selection honors the priority
order and the twelve-file cap.

`assert_property_4` is the whole property in one place, and every test in this
file runs the generated trees or a recorded one through it. It asserts the four
clauses the design states:

1. **The cap.** At most `MAX_SELECTED_FILES` candidates -- and, the non-vacuity
   half, exactly `min(cap, candidates)`, so a `select` that returned nothing
   could not satisfy it. The cap counts *files*: a path repeated in the tree
   cannot spend more than one slot.
2. **Non-decreasing tier order.** The list reads tier 1 first, tier 5 last.
3. **No tier inversion.** Nothing of a lower priority is selected while a
   higher-priority candidate was passed over. This is the clause the cap makes
   observable: when tiers 1 to 4 overflow twelve slots, no tier-5 file may
   appear at all.
4. **Determinism.** Selecting twice yields an identical list, and so does
   selecting from the same entries in any other order -- GitHub's tree order is
   not a contract, so the selection must not depend on it.

Plus: every candidate is a real blob from the tree carrying its own tier and
size, and the input sequence comes back untouched. `select` is pure, and the
rest of the pipeline -- the paths the orientation names, the manifest the model
cites, the token estimate -- is only reproducible because of it.

The three recorded trees from task 2.4 are run as concrete cases with their
exact expected selections, which is where the generated trees are weakest: each
one is a real repository shape, and between them they cover a tier-4 entry point
found by name (Flask), a repository with no tier-3 or tier-4 candidate at all
(Gin), all five tiers with entry points at two depths (Express), and a
`package.json` whose `main` field feeds the second selection pass.

`tree_entries` is importable (`from test_selection import tree_entries`) for the
later tree properties, the same way `conftest` exports `FakeGitHub`.

Out of scope here: Property 5 and the exclusion classes it covers -- lock
files, noise directories, oversize files, and non-UTF-8 bodies -- land with
task 3.8. Exclusions appear below only where they shape the priority order,
as with Gin having no entry point because its only `main.go` sits under
`examples/`.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from conftest import RepoFixture  # tests/ is on sys.path; see conftest's docstring
from hypothesis import given, settings
from hypothesis import strategies as st

from agent.config import MAX_FILE_BYTES, MAX_SELECTED_FILES
from agent.models import Candidate, TreeEntry
from agent.selection import (
    P1_README,
    P2_MANIFEST,
    P3_CONFIG,
    P4_ENTRY,
    P5_SOURCE,
    TIER_ORDER,
    classify,
    resolve_package_main,
    select,
)

# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


def assert_property_4(
    entries: Sequence[TreeEntry], package_main: str | None = None
) -> list[Candidate]:
    """Assert every clause of Property 4 and return the selection.

    Takes a sequence of entries with distinct paths -- which is what GitHub
    returns -- because clause 3 needs one tier per path to say which candidates
    were passed over. `test_repeated_paths_do_not_fill_the_cap_with_one_file`
    covers the duplicate case against the public behavior instead.
    """
    paths = [entry.path for entry in entries]
    assert len(set(paths)) == len(paths), "this helper assumes distinct paths"
    before = list(entries)

    chosen = select(entries, package_main)
    chosen_paths = [candidate.path for candidate in chosen]
    tiers = [candidate.tier for candidate in chosen]

    # Every entry `classify` admits, whether or not it was selected. This is
    # the set clause 3 is stated over.
    census = {
        entry.path: tier
        for entry in entries
        if (tier := classify(entry, package_main)) is not None
    }
    sizes = {entry.path: entry.size for entry in entries}

    # -- clause 1: the twelve-file cap (Requirement 3 AC3) -----------------
    assert len(chosen) <= MAX_SELECTED_FILES
    # And it is a cap, not a budget: every free slot is spent while a candidate
    # remains. Without this the other three clauses hold of an empty list.
    assert len(chosen) == min(MAX_SELECTED_FILES, len(census))
    assert len(set(chosen_paths)) == len(chosen_paths), "one slot per file"

    # -- nothing invented: each candidate is a blob from this tree ---------
    for candidate in chosen:
        assert candidate.tier in TIER_ORDER
        assert candidate.tier == census[candidate.path]
        assert candidate.size == sizes[candidate.path]

    # -- clause 2: non-decreasing tier order (Requirement 3 AC1) -----------
    assert tiers == sorted(tiers)

    # -- clause 3: no tier inversion ---------------------------------------
    # Tier numbers run the other way from priority, so the *lowest* priority
    # selected is the largest tier number present.
    selected = set(chosen_paths)
    lowest_selected = max(tiers, default=0)
    passed_over = {
        path
        for path, tier in census.items()
        if tier < lowest_selected and path not in selected
    }
    assert not passed_over, (
        f"tier-{lowest_selected} file selected while higher-priority "
        f"candidates were passed over: {sorted(passed_over)}"
    )

    # -- clause 4: determinism ---------------------------------------------
    assert select(entries, package_main) == chosen, "repeated selection differs"
    assert select(list(reversed(before)), package_main) == chosen, "order-dependent"

    # -- purity: the caller's sequence is untouched ------------------------
    assert list(entries) == before
    return chosen


# ---------------------------------------------------------------------------
# Generated trees
# ---------------------------------------------------------------------------

# One name per interesting classification outcome, so a drawn tree lands in
# several tiers at once rather than exercising one branch at a time.
README_NAMES = ("README.md", "README.rst", "Readme.md", "README")
MANIFEST_NAMES = (
    "package.json",
    "requirements.txt",
    "pyproject.toml",
    "go.mod",
    "Cargo.toml",
    "Makefile",
    "Dockerfile",
    "pom.xml",
    "Gemfile",
    "docker-compose.yml",
    "Api.csproj",
)
CONFIG_NAMES = (
    "tsconfig.json",
    "setup.cfg",
    ".eslintrc.yml",
    "vite.config.ts",
    "webpack.config.js",
    "main.tf",
    "Procfile",
    "serverless.yml",
)
ENTRY_NAMES = (
    "main.py",
    "app.py",
    "__main__.py",
    "index.js",
    "server.ts",
    "main.go",
    "main.rs",
    "Main.java",
)
SOURCE_NAMES = (
    "handler.py",
    "client.go",
    "view.tsx",
    "styles.css",
    "schema.sql",
    "notes.md",
    "types.ts",
    "helpers.rb",
    "query.graphql",
)
# Lock files, binaries, and extensions the tier-5 allowlist does not recognize.
# A tree of nothing but these selects nothing, which is a case the cap clause
# has to survive.
EXCLUDED_NAMES = (
    "package-lock.json",
    "yarn.lock",
    "go.sum",
    "Cargo.lock",
    "logo.png",
    "bundle.min.js",
    "weights.safetensors",
    "libfoo.so",
    "archive.tar.gz",
    "mystery.xyz",
    "LICENSE",
    ".gitignore",
)

HIGH_TIER_NAMES = README_NAMES + MANIFEST_NAMES + CONFIG_NAMES + ENTRY_NAMES
ALL_NAMES = HIGH_TIER_NAMES + SOURCE_NAMES + EXCLUDED_NAMES

# Ordinary source directories plus the noise directories that must be skipped,
# so generated paths land on both sides of the exclusion.
DIRECTORIES = (
    "src",
    "lib",
    "app",
    "pkg",
    "internal",
    "cmd",
    "api",
    "web",
    "docs",
    "tests",
    "node_modules",
    "vendor",
    "dist",
    ".github",
    "examples",
)

# Blobs dominate; trees and submodule pointers are there because `classify`
# must skip anything that is not a blob however it is named.
KINDS = st.sampled_from(("blob", "blob", "blob", "blob", "tree", "commit"))

ORDINARY_SIZES = st.integers(min_value=0, max_value=80_000)
SIZES = st.one_of(
    ORDINARY_SIZES,
    # The 500 KB filter is `>`, so the limit itself is admitted. Both sides of
    # that boundary are drawn explicitly; uniform sizes would never land on it.
    st.integers(min_value=MAX_FILE_BYTES - 1, max_value=MAX_FILE_BYTES + 1),
    st.integers(min_value=MAX_FILE_BYTES + 1, max_value=50_000_000),
)


@st.composite
def _paths(draw: st.DrawFn, names: Sequence[str], max_depth: int) -> str:
    """One repository-relative path, up to `max_depth` directories deep.

    A depth range of 0 to 3 straddles every bound `classify` applies: tiers 1
    and 2 stop at one level, tiers 3 to 5 at two, and nothing is classified
    below that.
    """
    name = draw(st.sampled_from(tuple(names)))
    depth = draw(st.integers(min_value=0, max_value=max_depth))
    segments = [draw(st.sampled_from(DIRECTORIES)) for _ in range(depth)]
    return "/".join([*segments, name])


@st.composite
def tree_entries(
    draw: st.DrawFn,
    *,
    names: Sequence[str] = ALL_NAMES,
    min_size: int = 0,
    max_size: int = 40,
    max_depth: int = 3,
    kinds: st.SearchStrategy[str] = KINDS,
    sizes: st.SearchStrategy[int] = SIZES,
) -> tuple[TreeEntry, ...]:
    """A recursive tree listing shaped like GitHub's: blobs plus their parents.

    Paths are distinct, as GitHub's are. The parent directory entries are added
    because the real document carries them, and a `tree` entry whose name reads
    like a manifest is exactly the sort of thing that should not be selected.

    `names`, `max_depth`, `kinds`, and `sizes` are narrowed by the crowded and
    unselectable flavors below to aim a tree at one region of the input space.
    """
    paths = draw(
        st.lists(
            _paths(names, max_depth), min_size=min_size, max_size=max_size, unique=True
        )
    )
    entries = [
        TreeEntry(path=path, kind=draw(kinds), size=draw(sizes)) for path in paths
    ]
    parents = {parent for path in paths for parent in _parents(path)} - set(paths)
    entries.extend(
        TreeEntry(path=parent, kind="tree", size=0) for parent in sorted(parents)
    )
    return tuple(entries)


def _parents(path: str) -> list[str]:
    segments = path.split("/")[:-1]
    return ["/".join(segments[: i + 1]) for i in range(len(segments))]


TREES = st.one_of(
    # Mixed: several tiers and several exclusion classes in one tree.
    tree_entries(min_size=8),
    # Crowded with tiers 1 to 4, so the cap regularly cuts mid-tier and clause 3
    # has to refuse a tier-5 file rather than merely not needing one. Shallow,
    # all blobs, and none of them oversize, because every one of those three
    # exclusions is a way for a tree to quietly fall short of twelve candidates.
    tree_entries(
        names=HIGH_TIER_NAMES,
        min_size=16,
        max_size=30,
        max_depth=1,
        kinds=st.just("blob"),
        sizes=ORDINARY_SIZES,
    ),
    # The mirror image: a readme, two manifests, and a lot of source files, so
    # tier 5 fills most of the twelve slots and the round-robin across
    # top-level directories decides which files those are.
    tree_entries(
        names=("README.md", "go.mod", "package.json") + SOURCE_NAMES,
        min_size=16,
        max_size=30,
        max_depth=2,
        kinds=st.just("blob"),
        sizes=ORDINARY_SIZES,
    ),
    # Nothing selectable at all.
    tree_entries(names=EXCLUDED_NAMES, min_size=1),
)


@st.composite
def tree_and_main(draw: st.DrawFn) -> tuple[tuple[TreeEntry, ...], str | None]:
    """A tree paired with the `package.json` entry point the second pass supplies.

    Drawn from the tree's own blobs, so the promotion to tier 4 actually
    happens, and sometimes from a path under `dist/` that is not in the tree at
    all: a `main` field naming a missing or excluded file must leave the
    selection valid rather than conjure a candidate.
    """
    entries = draw(TREES)
    blobs = [entry.path for entry in entries if entry.kind == "blob"]
    options = [st.none(), st.just("dist/index.js")]
    if blobs:
        options.append(st.sampled_from(blobs))
    return entries, draw(st.one_of(*options))


# The design sets 100 examples as the floor and runs the selection property at
# 300: it is one of the two load-bearing ones.
@settings(max_examples=300)
@given(tree_and_main())
def test_property_4_selection_honors_the_priority_order_and_the_cap(
    case: tuple[tuple[TreeEntry, ...], str | None],
) -> None:
    """Feature: delightful-aws-agent, Property 4: File selection honors the
    priority order and the twelve-file cap.

    For any repository tree, the selected file list contains at most 12 entries,
    its entries appear in non-decreasing priority tier order, no file of a
    lower-priority tier is selected while an unselected candidate of a
    higher-priority tier exists, and selecting twice from the same tree yields
    identical results.

    **Validates: Requirements 3.1, 3.3**
    """
    entries, package_main = case
    assert_property_4(list(entries), package_main)


@settings(max_examples=200)
@given(st.data())
def test_property_4_is_independent_of_the_order_github_returns(
    data: st.DataObject,
) -> None:
    """Determinism, stated over permutations rather than repeated calls.

    GitHub's tree order is not a documented contract, so a selection that
    depended on it would be reproducible in tests and unstable in production.
    """
    entries, package_main = data.draw(tree_and_main())
    shuffled = data.draw(st.permutations(entries))

    assert select(shuffled, package_main) == select(entries, package_main)


# ---------------------------------------------------------------------------
# The three recorded trees
# ---------------------------------------------------------------------------


def _package_main(fixture: RepoFixture) -> str | None:
    """The entry point this repository's `package.json` names, if it has one.

    The same two-pass call the handler makes: tier 2 reads `package.json`, then
    selection runs again knowing what `main` points at.
    """
    body = fixture.contents.get("package.json")
    return resolve_package_main(body.decode("utf-8") if body else None)


def test_property_4_on_the_recorded_trees(repo_fixture: RepoFixture) -> None:
    """Property 4 against Flask, Gin, and Express -- real repository shapes.

    **Validates: Requirements 3.1, 3.3**
    """
    chosen = assert_property_4(list(repo_fixture.entries), _package_main(repo_fixture))

    # All three have well over twelve candidates, so each one exercises the cap
    # rather than running out of files first.
    assert len(chosen) == MAX_SELECTED_FILES


FLASK_SELECTION = (
    (P1_README, "README.md"),
    (P2_MANIFEST, "pyproject.toml"),
    (P4_ENTRY, "src/flask/app.py"),
    (P4_ENTRY, "src/flask/__main__.py"),
    (P5_SOURCE, "CHANGES.rst"),
    (P5_SOURCE, "requirements/dev.txt"),
    (P5_SOURCE, "src/flask/cli.py"),
    (P5_SOURCE, "CONTRIBUTING.rst"),
    (P5_SOURCE, "requirements/docs.txt"),
    (P5_SOURCE, "src/flask/helpers.py"),
    (P5_SOURCE, "LICENSE.txt"),
    (P5_SOURCE, "requirements/tests.txt"),
)

GIN_SELECTION = (
    (P1_README, "README.md"),
    (P2_MANIFEST, "Makefile"),
    (P2_MANIFEST, "go.mod"),
    (P5_SOURCE, "CHANGELOG.md"),
    (P5_SOURCE, "binding/binding.go"),
    (P5_SOURCE, "ginS/gins.go"),
    (P5_SOURCE, "internal/bytesconv/bytesconv.go"),
    (P5_SOURCE, "render/json.go"),
    (P5_SOURCE, "context.go"),
    (P5_SOURCE, "binding/default_validator.go"),
    (P5_SOURCE, "internal/json/json.go"),
    (P5_SOURCE, "render/html.go"),
)

EXPRESS_SELECTION = (
    (P1_README, "Readme.md"),
    (P2_MANIFEST, "package.json"),
    (P3_CONFIG, ".eslintrc.yml"),
    (P4_ENTRY, "index.js"),
    (P4_ENTRY, "lib/router/index.js"),
    (P5_SOURCE, "History.md"),
    (P5_SOURCE, "benchmarks/middleware.js"),
    (P5_SOURCE, "lib/response.js"),
    (P5_SOURCE, "Contributing.md"),
    (P5_SOURCE, "lib/application.js"),
    (P5_SOURCE, "Security.md"),
    (P5_SOURCE, "lib/request.js"),
)


def _pairs(chosen: Sequence[Candidate]) -> tuple[tuple[int, str], ...]:
    return tuple((candidate.tier, candidate.path) for candidate in chosen)


def test_flask_selection_is_exactly_this_list(flask_repo: RepoFixture) -> None:
    """Python, 62 entries, tiers 1/2/4/5, and the cap reached mid-tier-5.

    No tier-3 candidate exists: Flask's configuration lives in `pyproject.toml`,
    which is already the tier-2 manifest. `src/flask/app.py` is a tier-4 entry
    point found by name at depth 2, and it outranks `__main__.py` in the same
    tier because it is the larger file.
    """
    chosen = select(flask_repo.entries)

    assert _pairs(chosen) == FLASK_SELECTION

    # The twelve slots are spent across the repository rather than inside one
    # directory: tier 5 fills round-robin over the top-level directories.
    tier5 = [path for tier, path in _pairs(chosen) if tier == P5_SOURCE]
    assert {path.split("/")[0] if "/" in path else "." for path in tier5} == {
        ".",
        "requirements",
        "src",
    }


def test_gin_has_no_config_or_entry_point_candidate(gin_repo: RepoFixture) -> None:
    """Go, 55 entries, tiers 1/2/5 only -- a shape the other two do not cover.

    Two absences do the work. `examples/basic/main.go` is the only file in the
    tree with an entry-point name, and it sits under a noise directory, so Gin
    reaches tier 5 with nothing in tiers 3 or 4. And `go.sum` is excluded by
    name while `go.mod` is selected: both are manifests of a sort, and only one
    tells a newcomer anything.
    """
    chosen = select(gin_repo.entries)
    tiers = {tier for tier, _ in _pairs(chosen)}
    census = {
        entry.path: tier
        for entry in gin_repo.entries
        if (tier := classify(entry)) is not None
    }

    assert _pairs(chosen) == GIN_SELECTION
    assert tiers == {P1_README, P2_MANIFEST, P5_SOURCE}
    # Not merely unselected -- not candidates at all, which is why clause 3
    # permits the tier-5 files above.
    assert P3_CONFIG not in census.values()
    assert P4_ENTRY not in census.values()
    assert "examples/basic/main.go" in gin_repo.blob_paths
    assert "examples/basic/main.go" not in census

    assert "go.sum" in gin_repo.blob_paths
    assert "go.sum" not in census
    assert census["go.mod"] == P2_MANIFEST


def test_express_covers_all_five_tiers(express_repo: RepoFixture) -> None:
    """JavaScript, 42 entries, every tier represented, entry points at two depths.

    `Readme.md` proves the tier-1 match is case-insensitive, `.eslintrc.yml` is
    the only tier-3 candidate across the three fixtures, and the two tier-4
    files sit at depths 0 and 2 -- both inside the two-level bound, ordered
    shallower first.
    """
    chosen = select(express_repo.entries)

    assert _pairs(chosen) == EXPRESS_SELECTION
    assert {tier for tier, _ in _pairs(chosen)} == set(TIER_ORDER)

    entry_points = [path for tier, path in _pairs(chosen) if tier == P4_ENTRY]
    assert [path.count("/") for path in entry_points] == [0, 2]


def test_the_second_pass_over_a_real_package_json_is_idempotent(
    express_repo: RepoFixture,
) -> None:
    """`main` resolves to `index.js`, which selection had already found by name.

    File bodies are read after selection, so `package.json`'s `main` field can
    only be honored on a second pass. Here the two passes agree, which is the
    common case and the one that must not disturb anything.
    """
    assert _package_main(express_repo) == "index.js"
    assert select(express_repo.entries, "index.js") == select(express_repo.entries)


def test_a_manifest_declared_entry_point_outranks_tier_five(
    express_repo: RepoFixture,
) -> None:
    """A `main` field naming an unconventional path promotes it to tier 4.

    `lib/express.js` is a tier-5 source file by name alone. Declared as the
    entry point it moves ahead of every tier-5 candidate and costs one of their
    slots -- the priority order asserted on a file whose tier changed rather
    than on one the name already decided.
    """
    chosen = assert_property_4(list(express_repo.entries), "lib/express.js")
    pairs = _pairs(chosen)

    assert (P4_ENTRY, "lib/express.js") in pairs
    assert pairs[:6] == (
        (P1_README, "Readme.md"),
        (P2_MANIFEST, "package.json"),
        (P3_CONFIG, ".eslintrc.yml"),
        (P4_ENTRY, "index.js"),
        (P4_ENTRY, "lib/express.js"),
        (P4_ENTRY, "lib/router/index.js"),
    )
    # One tier-5 file is displaced, not added to: the cap still holds.
    assert len(chosen) == MAX_SELECTED_FILES
    displaced = {path for _, path in _pairs(select(express_repo.entries))} - {
        path for _, path in pairs
    }
    assert displaced == {"lib/request.js"}


# ---------------------------------------------------------------------------
# The cap under pressure
# ---------------------------------------------------------------------------


def test_the_cap_cuts_mid_tier_before_any_lower_tier_file() -> None:
    """Sixteen manifests, and tier 3 never gets a slot.

    The sharpest form of clause 3. With more tier-2 candidates than the cap
    allows, selection stops inside tier 2: the tier-3 config, the tier-4 entry
    point, and the tier-5 source file are all passed over even though they are
    perfectly good candidates.
    """
    manifests = [
        f"{directory}/{name}"
        for directory in ("api", "web", "core", "worker")
        for name in ("package.json", "go.mod", "Cargo.toml", "pom.xml")
    ]
    lower_tiers = ("tsconfig.json", "main.py", "src/handler.py")
    entries = [
        TreeEntry(path="README.md", kind="blob", size=100),
        *(TreeEntry(path=path, kind="blob", size=200) for path in manifests),
        *(TreeEntry(path=path, kind="blob", size=300) for path in lower_tiers),
    ]

    chosen = assert_property_4(entries)
    chosen_paths = {candidate.path for candidate in chosen}

    assert len(chosen) == MAX_SELECTED_FILES
    assert chosen[0].path == "README.md"
    assert {candidate.tier for candidate in chosen} == {P1_README, P2_MANIFEST}
    assert len(chosen_paths & set(manifests)) == 11  # of sixteen candidates
    assert not chosen_paths & set(lower_tiers)


def test_repeated_paths_do_not_fill_the_cap_with_one_file() -> None:
    """The cap counts files, not tree entries.

    GitHub never repeats a path, so this is a guard rather than a case: fifteen
    copies of one readme must still leave eleven slots for everything else.
    Which copy wins when the sizes differ is an exclusion question, and belongs
    to Property 5.
    """
    entries = [
        *(TreeEntry(path="README.md", kind="blob", size=100 + i) for i in range(15)),
        TreeEntry(path="main.py", kind="blob", size=500),
    ]

    chosen = select(entries)

    assert _pairs(chosen) == ((P1_README, "README.md"), (P4_ENTRY, "main.py"))
    assert select(entries) == chosen


@pytest.mark.parametrize("entries", [[], [TreeEntry(path="src", kind="tree", size=0)]])
def test_a_tree_with_no_candidates_selects_nothing(entries: list[TreeEntry]) -> None:
    """An empty tree and a tree of directories both select an empty list."""
    assert assert_property_4(entries) == []
