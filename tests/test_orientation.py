"""Unit tests for `agent.orientation`. Requirements 4 AC3, 5 AC2 through AC5.

Two halves, matching the module's two jobs.

**`finalize` is a guarantee, so it is tested as one.** Requirement 5 says the
Orientation_Message *shall* name the repository, state a language, describe the
purpose in a sentence, name three ingested paths, and offer two follow-up
questions. A prompt cannot promise that of a small model, so `finalize`
promises it in code -- and the cases that matter are the ones where the model
did not cooperate: empty output, output naming no paths, output asking no
questions, pure garbage, nine hundred words of prose, an unterminated code
fence. Every one of them has to come out carrying all five parts and no more
than `MAX_RESPONSE_WORDS` words. `_assert_complete` is the single predicate all
of them are checked against, so there is one definition of "complete" rather
than six.

The two cases that earn their keep by failing against the obvious
implementation are `test_a_long_garbage_answer_is_repaired_and_still_capped` --
which fails if the repairs are appended after the cap, because the final trim
cuts the follow-up questions straight back off -- and
`test_repairs_land_outside_an_unterminated_code_fence`, which fails if the body
is not balanced before the repairs are joined to it.

**`detect_language` is tested against the recorded repositories.** Flask, Gin,
and Express, each with GitHub's field present and each with it absent, so the
manifest-inference fallback is exercised on real `pyproject.toml`, `go.mod`, and
`package.json` content rather than on something written to pass. The framework
signal gets synthetic manifests, because none of the three fixtures *uses* a
framework -- they are the frameworks, which is itself a case worth pinning.

Property 11 generalized the `finalize` guarantee over arbitrary model output.
It was dropped with the other fourteen (see `docs/out-of-scope.md` entry 7);
these are the targeted cases that replace it.
"""

from __future__ import annotations

import pytest

from agent.config import (
    MAX_RESPONSE_WORDS,
    ORIENTATION_MIN_PATHS,
    ORIENTATION_MIN_QUESTIONS,
)
from agent.context_builder import build_orientation_context
from agent.models import RepoContext, RepoMeta
from agent.orientation import (
    NO_LANGUAGE,
    PURPOSE_MIN_WORDS,
    QUESTION_LINE,
    REPAIR_ORDER,
    detect_language,
    finalize,
)
from agent.selection import resolve_package_main, select

from conftest import load_repo_fixture

FENCE = "```"

# Filler for the selected paths the fixtures do not record a body for, so all
# twelve manifest slots are populated without inventing content that claims to
# be real.
FILLER = "# synthesized source\n" + "one line of plausible source code\n" * 40


def context_for(name: str, **overrides: object) -> RepoContext:
    """An orientation context for a recorded repository, built offline.

    Goes through the real `select` and `build_orientation_context`, so the
    manifest `finalize` repairs from is the manifest the model would have been
    shown -- including the `package.json` `main` resolution that puts
    `index.js` in Express's selection.
    """
    fixture = load_repo_fixture(name)
    bodies = {
        candidate.path: fixture.contents.get(
            candidate.path, f"# {candidate.path}\n{FILLER}".encode()
        )
        for candidate in select(fixture.entries)
    }
    package_json = bodies.get("package.json")
    candidates = select(
        fixture.entries,
        resolve_package_main(package_json.decode() if package_json else None),
    )
    meta = fixture.meta
    if overrides:
        meta = RepoMeta(
            **{
                **{
                    field: getattr(fixture.meta, field)
                    for field in RepoMeta.__dataclass_fields__
                },
                **overrides,
            }
        )
    return build_orientation_context(
        meta,
        fixture.entries,
        candidates,
        bodies,
        detected_language=detect_language(meta, bodies),
    )


@pytest.fixture(scope="module")
def flask_context() -> RepoContext:
    return context_for("flask")


def manifest(context: RepoContext) -> list[str]:
    return [block.path for block in context.files] + list(context.unread_paths)


def words(text: str) -> int:
    return len(text.split())


def _assert_complete(message: str, context: RepoContext) -> None:
    """The five parts Requirement 5 requires, plus the cap it has to fit in.

    One predicate, used by every `finalize` case, so "complete" means the same
    thing in all of them.
    """
    lowered = message.lower()

    # Requirement 5 AC2 -- the repository is named.
    assert context.meta.slug.rsplit("/", 1)[-1].lower() in lowered

    # Requirement 5 AC2 -- a language is stated, or its absence is.
    label = context.detected_language or context.meta.language
    if label:
        tokens = [
            part.lower() for part in label.replace("(", " ").replace(")", " ").split()
        ]
        assert any(token in lowered for token in tokens), f"no language in {message!r}"
    else:
        assert NO_LANGUAGE in lowered

    # Requirement 5 AC3 -- a purpose sentence is present. Either the model
    # opened on one, or the repair added the explicit clause. The opening-
    # sentence word count is `finalize`'s detector of *absence*; it is not a
    # definition of presence, since a repaired purpose sentence is appended and
    # the message may still open on a short line the model wrote.
    opening = len(message.split(".")[0].split())
    assert opening >= PURPOSE_MIN_WORDS or "What it is for:" in message

    # Requirement 5 AC4 -- at least three ingested paths are named.
    named = [path for path in manifest(context) if path in message]
    assert len(named) >= ORIENTATION_MIN_PATHS, f"only named {named}"

    # Requirement 5 AC5 -- at least two follow-up questions are offered.
    assert len(QUESTION_LINE.findall(message)) >= ORIENTATION_MIN_QUESTIONS

    # Requirement 4 AC4 -- and all of it inside the word cap.
    assert words(message) <= MAX_RESPONSE_WORDS

    # A repair must never leave the reader staring at a runaway code block.
    assert message.count(FENCE) % 2 == 0


# ---------------------------------------------------------------------------
# `finalize` against uncooperative model output (Requirement 5 AC2 to AC5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("", id="empty"),
        pytest.param("   \n\n\t ", id="whitespace"),
        pytest.param("asdf qwerty zxcv", id="garbage"),
        pytest.param("!!! ??? ...", id="punctuation"),
        pytest.param("Hello", id="one_word"),
        pytest.param('{"error": null}', id="json"),
        pytest.param("```\n\n```", id="fence_only"),
    ],
)
def test_every_part_is_supplied_when_the_model_supplies_none(
    raw: str, flask_context: RepoContext
) -> None:
    """Requirement 5 AC2 to AC5 hold for any model output at all.

    This is the case the repair path exists for. The model returning nothing
    useful is not hypothetical on a small model with a 250-word structural
    instruction, and a blank first message would be the worst possible version
    of Requirement 5's "warm orientation instead of an empty chat box".
    """
    message, repairs = finalize(raw, flask_context)

    _assert_complete(message, flask_context)
    assert set(repairs) == set(REPAIR_ORDER), repairs
    assert repairs == list(REPAIR_ORDER), "reported in a stable order"


def test_output_naming_no_paths_gains_a_reading_order(
    flask_context: RepoContext,
) -> None:
    """Requirement 5 AC4. The one repair that has to come from the manifest.

    A reading order of invented paths would be worse than none -- it sends a
    newcomer looking for files that do not exist -- so the repaired paths are
    drawn from what was actually ingested.
    """
    raw = (
        "This is pallets/flask, a Python project built on the Flask framework "
        "itself. It gives you a small WSGI application framework to build on. "
        "Have a look around the source tree to get your bearings.\n\n"
        "- What do I need installed to run this locally?\n"
        "- Where does a request enter the code?\n"
    )

    message, repairs = finalize(raw, flask_context)

    assert repairs == ["paths"]
    _assert_complete(message, flask_context)
    # Appended, not substituted: the model's own prose survives intact.
    assert raw.strip().split("\n")[0] in message
    named = [path for path in manifest(flask_context) if path in message]
    assert set(named) <= set(manifest(flask_context))


def test_output_asking_no_questions_gains_two(flask_context: RepoContext) -> None:
    """Requirement 5 AC5. The part most likely to be lost to a trim, since it
    lives at the end of the message."""
    paths = manifest(flask_context)[:ORIENTATION_MIN_PATHS]
    raw = (
        "This is pallets/flask, a Python web framework. It exists to make a "
        "small WSGI application quick to start and able to grow. Read "
        f"`{paths[0]}` first, then `{paths[1]}`, then `{paths[2]}`."
    )

    message, repairs = finalize(raw, flask_context)

    assert repairs == ["questions"]
    _assert_complete(message, flask_context)
    assert message.startswith(raw)


def test_a_complete_answer_is_returned_untouched(flask_context: RepoContext) -> None:
    """No repairs means no edits: the repair-rate metric has to mean something.

    If a good answer still collected repairs, the metric would be noise and the
    production signal the module exists to provide would be lost.
    """
    paths = manifest(flask_context)[:ORIENTATION_MIN_PATHS]
    raw = (
        "This is pallets/flask, and it is a Python project. It gives you a "
        "lightweight WSGI web framework that starts small and scales up.\n\n"
        f"Start with `{paths[0]}` for the overview, then `{paths[1]}` for the "
        f"dependencies, then `{paths[2]}` for the application object.\n\n"
        "- What do I need installed to run this locally?\n"
        "- Where does a request enter the code?\n"
    )

    message, repairs = finalize(raw, flask_context)

    assert repairs == []
    assert message == raw.strip()
    _assert_complete(message, flask_context)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(
            " ".join(f"token{index}" for index in range(900)), id="one_long_run"
        ),
        pytest.param(
            " ".join(f"token{index}." for index in range(900)), id="many_sentences"
        ),
        pytest.param(
            "```\n" + "\n".join(f"call{index}()" for index in range(900)), id="fenced"
        ),
    ],
)
def test_a_long_answer_is_repaired_and_still_capped(
    raw: str, flask_context: RepoContext
) -> None:
    """The cap and the repairs have to hold at the same time. Requirement 4 AC4.

    Nine hundred words carrying none of the required parts. Capping first and
    appending after would breach the 350-word cap; capping again afterwards
    would cut the appended follow-up questions straight back off, quietly
    undoing the repair Requirement 5 AC5 depends on -- and the questions are
    last in the message, so they are the first thing a trim takes. The repairs
    are therefore priced and paid for out of the budget before the model's text
    is trimmed.
    """
    message, repairs = finalize(raw, flask_context)

    _assert_complete(message, flask_context)
    assert {"repo_name", "language", "paths", "questions"} <= set(repairs)
    # The cap is spent, not wasted: the model's text is trimmed to exactly what
    # the repairs left over, rather than to some safe-looking fraction of it.
    # One word short is the budgeted-but-unused closing fence, which is reserved
    # whenever the answer holds a fence at all and spent only when the trim
    # lands inside one.
    assert MAX_RESPONSE_WORDS - 1 <= words(message) <= MAX_RESPONSE_WORDS
    assert raw.split()[0] in message, "the model's own text is kept, from the front"


def test_repairs_land_outside_an_unterminated_code_fence(
    flask_context: RepoContext,
) -> None:
    """An open fence would render the whole repair as source code.

    `cap_words` closes a fence its own cut opened and leaves alone one the model
    wrote into a short message -- that text is the model's. But appending after
    it would put the reading order and the follow-up questions inside a
    ``<pre>`` in the Chat_UI, so `finalize` balances the body first.
    """
    raw = "This is pallets/flask in Python.\n\n```python\nfrom flask import Flask\n"

    message, repairs = finalize(raw, flask_context)

    assert "questions" in repairs
    _assert_complete(message, flask_context)
    # Everything the repair added sits after the closing fence.
    tail = message[message.rindex(FENCE) + len(FENCE) :]
    assert len(QUESTION_LINE.findall(tail)) >= ORIENTATION_MIN_QUESTIONS


def test_an_undetected_language_is_stated_rather_than_guessed() -> None:
    """Requirement 5 AC2's other branch.

    GitHub reports no language and the repository has no manifest this module
    can read, so the honest answer is that there is no single dominant one --
    not a language picked to fill the slot.
    """
    context = context_for("flask", language=None)
    stripped = RepoContext(
        meta=context.meta,
        detected_language=None,
        tree_lines=context.tree_lines,
        tree_overflow=context.tree_overflow,
        files=context.files,
        unread_paths=context.unread_paths,
        notes=context.notes,
        estimated_tokens=context.estimated_tokens,
    )

    message, repairs = finalize("", stripped)

    assert "language" in repairs
    assert NO_LANGUAGE in message
    _assert_complete(message, stripped)


def test_a_missing_description_says_so(flask_context: RepoContext) -> None:
    """Requirement 5 AC3 with nothing to draw on.

    The purpose sentence comes from Repo_Context or it does not come at all.
    Inventing one is the failure the system prompt spends a paragraph
    forbidding, so the repair owns up instead.
    """
    context = context_for("flask", description="")

    message, repairs = finalize("", context)

    assert "purpose" in repairs
    assert "does not state its purpose" in message
    _assert_complete(message, context)


@pytest.mark.parametrize("name", ["flask", "gin", "express"])
def test_the_guarantee_holds_for_every_recorded_repository(name: str) -> None:
    """Three languages, three tree shapes, one guarantee."""
    context = context_for(name)

    message, repairs = finalize("", context)

    _assert_complete(message, context)
    assert set(repairs) == set(REPAIR_ORDER)


def test_a_mentioned_framework_satisfies_the_language_check() -> None:
    """Naming the framework counts as naming the language.

    The detected label carries both halves, so demanding the language word when
    the model named the framework would repair a good answer.
    """
    context = context_for("flask")
    assert context.detected_language == "Python"

    paths = manifest(context)[:ORIENTATION_MIN_PATHS]
    raw = (
        "This is pallets/flask, written in Python. It is a small web framework "
        f"for building applications. Read `{paths[0]}`, `{paths[1]}` and "
        f"`{paths[2]}`.\n\n- What runs the tests?\n- Where is the entry point?\n"
    )

    message, repairs = finalize(raw, context)

    assert repairs == []
    _assert_complete(message, context)


def test_a_substring_is_not_a_language_mention(flask_context: RepoContext) -> None:
    """A substring is not a mention: "Pythonic" is not "Python".

    The language check matches whole words. Substring matching would let
    `"go" in "good places to start"` stand in for naming Go, which is the
    silent failure Requirement 5 AC2 exists to prevent: a message that never
    told the reader what the project is written in, passing the check that
    exists to notice exactly that.
    """
    paths = manifest(flask_context)[:ORIENTATION_MIN_PATHS]
    raw = (
        "This is pallets/flask, and it follows Pythonic conventions "
        "throughout. It gives you a small web framework to build on. Good "
        f"places to start are `{paths[0]}`, `{paths[1]}` and `{paths[2]}`.\n\n"
        "- What runs the tests?\n- Where is the entry point?\n"
    )

    message, repairs = finalize(raw, flask_context)

    assert repairs == ["language"], "the word Python is never written"
    assert "Primary language: Python." in message
    _assert_complete(message, flask_context)


# ---------------------------------------------------------------------------
# `detect_language` (Requirement 5 AC2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [("flask", "Python"), ("gin", "Go"), ("express", "JavaScript")],
)
def test_github_language_is_the_first_authority(name: str, expected: str) -> None:
    """GitHub classified all three, so its answer is the one that is used."""
    fixture = load_repo_fixture(name)

    assert detect_language(fixture.meta, dict(fixture.contents)) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [("flask", "Python"), ("gin", "Go"), ("express", "JavaScript")],
)
def test_manifests_cover_the_gap_when_github_reports_nothing(
    name: str, expected: str
) -> None:
    """GitHub returns `None` for a repository it cannot classify.

    The manifest is the evidence that fills it: `pyproject.toml` means Python,
    `go.mod` means Go, `package.json` means JavaScript. Read from the recorded
    file bodies, not from a path list, so the inference is exercised against
    real manifest text.
    """
    fixture = load_repo_fixture(name)
    unclassified = RepoMeta(
        **{
            **{
                field: getattr(fixture.meta, field)
                for field in RepoMeta.__dataclass_fields__
            },
            "language": None,
        }
    )

    assert detect_language(unclassified, dict(fixture.contents)) == expected


def test_a_manifest_with_no_body_still_proves_the_language() -> None:
    """A raw fetch can fail. The path alone is still evidence."""
    meta = RepoMeta(
        slug="owner/repo",
        default_branch="main",
        size_kb=64,
        language=None,
        description="",
        html_url="https://github.com/owner/repo",
        archived=False,
    )

    assert detect_language(meta, {}, paths=["Cargo.toml", "src/main.rs"]) == "Rust"


@pytest.mark.parametrize(
    ("label", "path", "body", "expected"),
    [
        (
            "django",
            "pyproject.toml",
            '[project]\nname = "site"\ndependencies = ["Django>=4.2", "psycopg2"]\n',
            "Python (Django)",
        ),
        (
            "flask_app",
            "requirements.txt",
            "flask==3.0.0\ngunicorn==21.2.0\n# a comment\n",
            "Python (Flask)",
        ),
        (
            "express_app",
            "package.json",
            '{"name": "my-api", "dependencies": {"express": "^4.19.2"}}',
            "JavaScript (Express)",
        ),
        (
            "gin_app",
            "go.mod",
            "module example.com/api\n\ngo 1.21\n\nrequire (\n"
            "\tgithub.com/gin-gonic/gin v1.9.1\n)\n",
            "Go (Gin)",
        ),
        (
            "fiber_app",
            "go.mod",
            "module example.com/api\n\nrequire github.com/gofiber/fiber/v2 v2.52.0\n",
            "Go (Fiber)",
        ),
        (
            "rails_app",
            "Gemfile",
            'source "https://rubygems.org"\ngem "rails", "~> 7.1"\n',
            "Ruby (Rails)",
        ),
    ],
)
def test_a_framework_dependency_is_added_to_the_language(
    label: str, path: str, body: str, expected: str
) -> None:
    """Requirement 5 AC2 allows "language or framework"; both is better.

    The language is what GitHub or the manifest asserts; the framework is the
    part that actually tells a newcomer what they are looking at. Neither is
    dropped.
    """
    meta = RepoMeta(
        slug="owner/repo",
        default_branch="main",
        size_kb=64,
        language=None,
        description="",
        html_url="https://github.com/owner/repo",
        archived=False,
    )

    assert detect_language(meta, {path: body.encode()}) == expected


@pytest.mark.parametrize("name", ["flask", "gin", "express"])
def test_a_frameworks_own_repository_is_not_labeled_a_user_of_itself(
    name: str,
) -> None:
    """Flask's `pyproject.toml` names `flask`; Gin's `go.mod` names Gin.

    "Flask app" would describe the wrong relationship for `pallets/flask`, and
    it would come from reading a manifest's own identity as a dependency on
    itself -- which is also how a console script like `flask.cli:main` sneaks
    in if the manifest is scanned line by line rather than parsed.
    """
    fixture = load_repo_fixture(name)

    label = detect_language(fixture.meta, dict(fixture.contents))

    assert label == fixture.meta.language
    assert "(" not in (label or "")


def test_nothing_known_is_reported_as_nothing() -> None:
    """`None` is an answer, not a failure. `finalize` says so out loud."""
    meta = RepoMeta(
        slug="owner/repo",
        default_branch="main",
        size_kb=12,
        language=None,
        description="",
        html_url="https://github.com/owner/repo",
        archived=False,
    )

    assert detect_language(meta) is None
    assert detect_language(meta, {"LICENSE": b"MIT"}) is None


def test_an_undecodable_manifest_costs_the_framework_not_the_answer() -> None:
    """The same binary check the context builder runs, applied here.

    A `package.json` that comes back as a packed blob still proves the language
    by existing; its dependencies simply cannot be read.
    """
    meta = RepoMeta(
        slug="owner/repo",
        default_branch="main",
        size_kb=12,
        language=None,
        description="",
        html_url="https://github.com/owner/repo",
        archived=False,
    )

    assert detect_language(meta, {"package.json": b"\x00\x01\x02"}) == "JavaScript"


def test_detection_is_deterministic_for_a_polyglot_repository() -> None:
    """Two manifests, one answer, the same answer every time.

    Every downstream assertion -- the context's metadata header, `finalize`'s
    language check -- reads this value, so an answer that depended on dict
    ordering would make them all flaky.
    """
    meta = RepoMeta(
        slug="owner/repo",
        default_branch="main",
        size_kb=12,
        language=None,
        description="",
        html_url="https://github.com/owner/repo",
        archived=False,
    )
    bodies = {
        "package.json": b'{"name": "ui"}',
        "go.mod": b"module example.com/api\n",
        "api/pyproject.toml": b'[project]\nname = "api"\n',
    }

    answers = {detect_language(meta, bodies) for _ in range(5)}

    assert answers == {"Go"}, "shallowest then alphabetical, every time"
