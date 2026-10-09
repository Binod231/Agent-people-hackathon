"""The Orientation_Message guarantee: language detection, and validate-and-repair.

Requirement 5 asks the first assistant message of a session to carry five
specific things -- the repository name, a language or framework, a one-sentence
purpose, at least three ingested paths, and at least two follow-up questions --
and Requirement 5 AC6 gives the whole round trip 20 seconds to produce them from
a small, fast model.

A prompt cannot guarantee structure from a small model, and a second inference
call to fix a malformed answer would cost another 3 to 7 seconds and break the
budget outright. So the guarantee is made here, deterministically and locally:
`finalize` inspects the model's text, adds whatever is missing, and returns the
list of what it had to add. `ORIENTATION_PROMPT` is still what makes the model
produce all five on its own most of the time; this module is what makes
Requirement 5 true the rest of the time.

Three things worth knowing before editing it:

- **Repairs are appended, never substituted.** A good response is left exactly
  as the model wrote it and only the missing part is added, so the repair path
  cannot degrade an answer that was already right. `finalize` returns the repair
  list so the handler can log it: a rising repair rate is the signal that the
  prompt needs work, and in production it is the only way to know.
- **The word cap is paid for before the repairs are written, not after.**
  `postprocess.cap_words` trims from the right, so capping an over-long response
  and *then* appending repairs would either breach Requirement 4 AC4 or -- worse
  -- have the final cap cut the appended follow-up questions straight back off,
  silently undoing the repair that Requirement 5 AC5 depends on. Instead the
  cost of the repairs is measured first and the model's text is trimmed to what
  is left, so the repairs always survive and the total always fits.
- **An open code fence is closed before anything is appended.** `cap_words`
  closes a fence that *its own cut* left open, and deliberately leaves alone one
  the model wrote into a message short enough to keep -- that text is the
  model's. But appending a repair after an unterminated ```` ``` ```` would land
  the repair inside a fenced block, and the Chat_UI would render the reading
  order and the follow-up questions as source code. So the body is balanced
  first, and the closing fence is charged to the word budget like any other
  word.

`detect_language` takes GitHub's own `language` field as the first authority,
falls back to manifest evidence when GitHub cannot classify the repository, and
prefers a framework signal from a manifest's dependencies when it finds one --
"Python (Flask)" orients a newcomer better than "Python" alone, and better than
"Flask" with the language dropped.

**Deviation from the design sketch.** The sketch gave `detect_language` a
`RepoContext` parameter. It cannot have one: `context_builder.build_context`
takes `detected_language` as an *input* and renders it into the context's
metadata header, so detection has to run before the context exists. The
arguments here are what the handler holds at that point -- the metadata and the
fetched file bodies.

Pure: no network, no disk, no clock, no module state.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence

from .config import (
    MAX_RESPONSE_WORDS,
    ORIENTATION_MIN_PATHS,
    ORIENTATION_MIN_QUESTIONS,
)
from .models import RepoContext, RepoMeta
from .postprocess import cap_words
from .selection import decode_text

__all__ = [
    "FALLBACK_QUESTIONS",
    "FRAMEWORKS",
    "MANIFEST_LANGUAGES",
    "NO_LANGUAGE",
    "PURPOSE_MIN_WORDS",
    "QUESTION_LINE",
    "REPAIR_ORDER",
    "detect_language",
    "finalize",
]

FENCE = "```"

# The follow-up-question shape, pinned to the one `ORIENTATION_PROMPT` asks for
# and to the one `tests/test_prompts.py` asserts the prompt requests. A looser
# pattern would count prose as a question; a tighter one would repair every
# orientation even when the model did exactly what it was told.
QUESTION_LINE = re.compile(r"^\s*[-*]\s+.+\?\s*$", re.MULTILINE)

# Requirement 5 AC3 asks for "the repository purpose in one sentence". The
# proxy is cheap and deliberately forgiving: does the message open with
# something sentence-shaped rather than a bare title or a code fence. Four words
# is short enough that no real opening sentence trips it.
PURPOSE_MIN_WORDS = 4

# What the language repair says when nothing was detected. Requirement 5 AC2
# asks the message to name the primary language; stating plainly that there is
# no single one beats picking a language out of the air.
NO_LANGUAGE = "no single dominant language"

# Requirement 5 AC5's fallback. Two questions a newcomer to any repository
# genuinely wants answered, in the `- ... ?` shape `QUESTION_LINE` recognizes.
FALLBACK_QUESTIONS: tuple[str, ...] = (
    "What do I need installed to run this locally?",
    "Where does a request enter the code?",
)

# The repair kinds, in the order they are applied and reported. The repo name
# goes in front of the model's text (it is the message's opening claim); the
# other four are appended.
REPAIR_ORDER: tuple[str, ...] = (
    "repo_name",
    "language",
    "purpose",
    "paths",
    "questions",
)

# ---------------------------------------------------------------------------
# Language and framework evidence
# ---------------------------------------------------------------------------

# Manifest file name -> the language its presence implies. Consulted only when
# GitHub reports no language, which it does for repositories it cannot classify.
MANIFEST_LANGUAGES: Mapping[str, str] = {
    "Cargo.toml": "Rust",
    "CMakeLists.txt": "C++",
    "Gemfile": "Ruby",
    "Package.swift": "Swift",
    "Pipfile": "Python",
    "build.gradle": "Java",
    "build.gradle.kts": "Kotlin",
    "build.sbt": "Scala",
    "composer.json": "PHP",
    "deno.json": "TypeScript",
    "go.mod": "Go",
    "mix.exs": "Elixir",
    "package.json": "JavaScript",
    "pom.xml": "Java",
    "pubspec.yaml": "Dart",
    "pyproject.toml": "Python",
    "requirements.txt": "Python",
    "setup.py": "Python",
    "tsconfig.json": "TypeScript",
}

# Dependency name -> framework label. Insertion order is precedence, which
# settles a project that depends on several: the first key any dependency
# matches wins. The ordering within an ecosystem is a judgment call, not a fact
# -- a project using both React and Express is labeled Next.js/React here -- and
# it only ever changes which of two true things the orientation leads with.
#
# A key containing "/" is a module path and matches a dependency that equals it
# or extends it with another segment, so `github.com/gofiber/fiber/v2` matches
# `github.com/gofiber/fiber`. Every other key must match a dependency exactly:
# npm and PyPI names are exact, and a prefix match would read `express-session`
# as Express itself.
FRAMEWORKS: Mapping[str, str] = {
    # Python
    "django": "Django",
    "flask": "Flask",
    "fastapi": "FastAPI",
    "starlette": "Starlette",
    "aiohttp": "aiohttp",
    "tornado": "Tornado",
    "pyramid": "Pyramid",
    "streamlit": "Streamlit",
    "scrapy": "Scrapy",
    # JavaScript and TypeScript
    "next": "Next.js",
    "nuxt": "Nuxt",
    "@angular/core": "Angular",
    "@nestjs/core": "NestJS",
    "react": "React",
    "vue": "Vue",
    "svelte": "Svelte",
    "express": "Express",
    "koa": "Koa",
    "fastify": "Fastify",
    "@hapi/hapi": "hapi",
    # Go
    "github.com/gin-gonic/gin": "Gin",
    "github.com/labstack/echo": "Echo",
    "github.com/gofiber/fiber": "Fiber",
    "github.com/go-chi/chi": "chi",
    "github.com/gorilla/mux": "Gorilla",
    # Ruby
    "rails": "Rails",
    "sinatra": "Sinatra",
    # PHP
    "laravel/framework": "Laravel",
    "symfony/framework-bundle": "Symfony",
}

# Manifests whose dependency list this module knows how to read. A manifest
# outside this set still contributes its language (above); it just contributes
# no framework signal. JVM build files are the deliberate gap: `pom.xml` and
# `build.gradle` spell a dependency four different ways between them, and the
# orientation reads perfectly well as "Java" without the extra parser.
_JSON_MANIFESTS = frozenset({"package.json", "composer.json", "deno.json"})
_TOML_MANIFESTS = frozenset({"pyproject.toml", "Pipfile", "setup.py", "setup.cfg"})

# A PEP 508 requirement, matched over a *whole* candidate string so that
# `flask.cli:main` -- the entry point in Flask's own `pyproject.toml` -- cannot
# be mistaken for a dependency on Flask.
_PY_REQUIREMENT = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*(?:[<>=!~;,^].*)?$"
)
_QUOTED = re.compile(r'["\']([^"\'\n]*)["\']')
_GEM = re.compile(r'^\s*gem\s+["\']([^"\'\n]+)["\']', re.MULTILINE)


def detect_language(
    meta: RepoMeta,
    bodies: Mapping[str, bytes | str] | None = None,
    *,
    paths: Iterable[str] = (),
) -> str | None:
    """The language or framework label for Repo_Context. Requirement 5 AC2.

    Args:
        meta: What `GET /repos/{owner}/{repo}` reported. `meta.language` is
            GitHub's own classification and is the first authority.
        bodies: Fetched file bodies keyed by repository-relative path, as
            `github_client.read_files` returns them. Manifest bodies are read
            for a framework signal; anything undecodable is skipped, which is
            the same `selection.decode_text` check the context builder runs.
        paths: Extra selected paths with no body -- a manifest whose raw fetch
            failed still proves the language by existing, even though its
            dependencies cannot be read.

    Returns:
        `"Python"`, `"Python (Flask)"`, or `None` when neither GitHub nor the
        manifests say anything. `None` is a real answer, not a failure: the
        orientation then states that no single language was detected rather
        than guessing one.

    GitHub reports `None` for repositories it cannot classify -- a new project,
    a polyglot one, a repository of configuration -- and manifest evidence
    covers that gap. The framework, where one is found, is appended rather than
    substituted: the language is the fact GitHub asserted and the framework is
    the part that actually orients a newcomer, so the label keeps both.
    """
    contents = {path: body for path, body in (bodies or {}).items()}
    # Shallowest first, then alphabetical: a root manifest describes the project
    # and a nested one usually describes a subpackage. Sorted so the label is a
    # deterministic function of its inputs, which every downstream assertion --
    # the context header, `finalize`'s language check -- depends on.
    manifests = sorted({*contents, *paths}, key=lambda path: (path.count("/"), path))

    language = _github_language(meta) or _language_from_manifests(manifests)
    framework = _framework_from_manifests(manifests, contents, _repo_name(meta))
    if language and framework:
        return f"{language} ({framework})"
    return framework or language or None


def _github_language(meta: RepoMeta) -> str | None:
    return (meta.language or "").strip() or None


def _repo_name(meta: RepoMeta) -> str:
    return meta.slug.rsplit("/", 1)[-1].strip().lower()


def _language_from_manifests(manifests: Sequence[str]) -> str | None:
    for path in manifests:
        language = MANIFEST_LANGUAGES.get(_basename(path))
        if language is not None:
            return language
    return None


def _framework_from_manifests(
    manifests: Sequence[str], contents: Mapping[str, bytes | str], repo_name: str
) -> str | None:
    """The first framework in `FRAMEWORKS` that any manifest depends on."""
    tokens: set[str] = set()
    for path in manifests:
        text = _as_text(contents.get(path))
        if text:
            tokens |= _dependency_tokens(path, text)

    # A framework's own repository is not a user of that framework: Flask's
    # `pyproject.toml` names `flask` in `[project]`, and labeling pallets/flask
    # a "Flask app" would describe the wrong relationship.
    tokens = {
        token
        for token in tokens
        if token != repo_name and token.rsplit("/", 1)[-1] != repo_name
    }

    for key, label in FRAMEWORKS.items():
        for token in tokens:
            if token == key or ("/" in key and token.startswith(f"{key}/")):
                return label
    return None


def _as_text(body: bytes | str | None) -> str | None:
    """A fetched body as text, or `None` if there is none to read.

    Bytes go through `selection.decode_text` -- the same NUL scan and strict
    UTF-8 decode the context builder runs -- so a manifest that came back as a
    packed blob costs the framework signal and nothing else. It still names its
    language by existing.
    """
    if body is None or isinstance(body, str):
        return body
    return decode_text(body)


def _dependency_tokens(path: str, text: str) -> set[str]:
    """Dependency names declared by one manifest, lowercased.

    Each format gets a deliberately narrow reader rather than a scan of the
    whole file. A scan is what turns a manifest's own `name` field, or a console
    script that happens to reference the package, into a phantom dependency.
    """
    name = _basename(path)
    if name in _JSON_MANIFESTS:
        return _json_dependencies(text)
    if name == "go.mod":
        return _go_dependencies(text)
    if name == "requirements.txt":
        return _python_dependencies(line.split("#")[0] for line in text.splitlines())
    if name in _TOML_MANIFESTS:
        return _python_dependencies(_QUOTED.findall(text))
    if name == "Gemfile":
        return {match.lower() for match in _GEM.findall(text)}
    return set()


def _json_dependencies(text: str) -> set[str]:
    """Keys of the dependency maps in a `package.json`-shaped manifest."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    found: set[str] = set()
    for field in ("dependencies", "devDependencies", "peerDependencies", "require"):
        section = data.get(field)
        if isinstance(section, dict):
            found |= {str(key).strip().lower() for key in section}
    return {token for token in found if token}


def _go_dependencies(text: str) -> set[str]:
    """Module paths from a `go.mod`'s `require` directives.

    The `module` line is skipped on purpose. It names the repository itself, and
    reading it as a dependency would label every framework's own repository as a
    user of itself -- `gin-gonic/gin` being the case in the test fixtures.
    """
    found: set[str] = set()
    in_block = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if in_block:
            if line.startswith(")"):
                in_block = False
            else:
                found.add(line.split()[0].lower())
            continue
        if line.startswith("require ("):
            in_block = True
        elif line.startswith("require "):
            parts = line.split()
            if len(parts) > 1:
                found.add(parts[1].lower())
    return {token for token in found if token}


def _python_dependencies(candidates: Iterable[str]) -> set[str]:
    """Requirement names out of strings that are *entirely* a requirement."""
    found: set[str] = set()
    for candidate in candidates:
        match = _PY_REQUIREMENT.match(candidate.strip())
        if match:
            found.add(match.group(1).lower())
    return found


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# Validate and repair (Requirement 5 AC2 through AC5)
# ---------------------------------------------------------------------------


def finalize(raw: str, ctx: RepoContext) -> tuple[str, list[str]]:
    """Guarantee the Orientation_Message carries its required parts.

    Args:
        raw: The model's answer, exactly as Bedrock returned it. Any string at
            all, including the empty one.
        ctx: The orientation context the answer was generated from. Its
            manifest is where repaired file paths come from, so a repaired
            reading order names files that were genuinely ingested
            (Requirement 5 AC4).

    Returns:
        `(message, repairs)`. `message` holds all five required parts and at
        most `MAX_RESPONSE_WORDS` words. `repairs` names the parts that had to
        be added, in `REPAIR_ORDER`, and is empty when the model produced all
        five on its own -- the handler logs it as the repair-rate metric.

    How the two bounds are kept at once, which is the whole subtlety here:

    The repairs are written first (their text depends only on `ctx`), their word
    cost is measured, and the model's text is trimmed to `MAX_RESPONSE_WORDS`
    minus that cost. So the repairs are never the part that gets cut, and the
    total never breaches Requirement 4 AC4. Trimming the model's text harder can
    of course remove a part that was present before the trim, which would call
    for a repair that was not budgeted -- so the assessment runs again against
    the shorter text, and the repair set accumulates. Each pass can only add to
    it, and there are five kinds, so this settles in at most five passes.
    """
    source = cap_words((raw or "").strip())
    needed: tuple[str, ...] = ()
    body = source

    for _ in range(len(REPAIR_ORDER) + 1):
        body = cap_words(source, _allowance(needed, source, ctx))
        merged = _merge(needed, _missing(body, ctx))
        if merged == needed:
            break
        needed = merged
    else:  # pragma: no cover - the repair set grows strictly, five kinds exist
        body = cap_words(source, _allowance(needed, source, ctx))

    return _assemble(body, needed, ctx), list(needed)


def _missing(text: str, ctx: RepoContext) -> tuple[str, ...]:
    """Which required parts `text` does not carry, in `REPAIR_ORDER`.

    Every check reads the model's text alone, never a partly repaired version.
    Assessing sequentially -- the way the design sketch did, each check seeing
    the previous repair's output -- means a repair can trigger another one:
    prepending "This is `owner/repo`." makes the first sentence three words
    long, which fails the purpose check even when the model wrote a perfectly
    good purpose sentence two lines down. That inflates the repair-rate metric
    with repairs the model did not cause, and the metric is the only production
    signal that the prompt needs work.
    """
    found: list[str] = []
    if _repo_name(ctx.meta) not in text.lower():
        found.append("repo_name")  # Requirement 5 AC2
    if not _mentions_language(text, ctx):
        found.append("language")  # Requirement 5 AC2
    if len(text.split(".")[0].split()) < PURPOSE_MIN_WORDS:
        found.append("purpose")  # Requirement 5 AC3
    if len(_named_paths(text, ctx)) < ORIENTATION_MIN_PATHS:
        found.append("paths")  # Requirement 5 AC4
    if len(QUESTION_LINE.findall(text)) < ORIENTATION_MIN_QUESTIONS:
        found.append("questions")  # Requirement 5 AC5
    return tuple(found)


def _merge(needed: tuple[str, ...], found: tuple[str, ...]) -> tuple[str, ...]:
    """Union of two repair sets, in `REPAIR_ORDER`.

    A union rather than a replacement, so the set only ever grows and the loop
    in `finalize` is guaranteed to settle. It can only grow by over-repairing --
    adding a part the current text turns out to carry after all -- which costs a
    redundant sentence and never a missing one.
    """
    both = set(needed) | set(found)
    return tuple(kind for kind in REPAIR_ORDER if kind in both)


def _allowance(needed: tuple[str, ...], source: str, ctx: RepoContext) -> int:
    """Words left for the model's text once the repairs are paid for.

    The closing fence is charged whenever the text holds a fence at all, which
    over-reserves by one word in the balanced case and never under-reserves.
    """
    head, suffixes = _repair_text(needed, ctx)
    spent = _words(head) + sum(_words(part) for part in suffixes)
    if FENCE in source:
        spent += 1
    return max(0, MAX_RESPONSE_WORDS - spent)


def _assemble(body: str, needed: tuple[str, ...], ctx: RepoContext) -> str:
    """The model's text with the repairs around it, balanced and capped.

    The final `cap_words` is a backstop, not the cap: `_allowance` already made
    room, so it has nothing to cut. It stays because the arithmetic above is the
    kind that a later edit could get wrong, and Requirement 4 AC4 should fail
    loudly in a test rather than quietly in production.
    """
    head, suffixes = _repair_text(needed, ctx)
    pieces = [part for part in (head, _close_fence(body), *suffixes) if part.strip()]
    return cap_words("\n\n".join(pieces))


def _repair_text(
    needed: tuple[str, ...], ctx: RepoContext
) -> tuple[str, tuple[str, ...]]:
    """`(head, suffixes)` for a repair set. A function of `ctx` alone.

    Body-independent on purpose: `_allowance` has to price the repairs before
    the body it will be appended to exists, and a cost that depended on the body
    would make that arithmetic circular.
    """
    # Phrased as a full sentence rather than the sketch's "This is `slug`."
    # so the repaired message opens on something the purpose heuristic reads as
    # a sentence. A three-word opening would make a message that *has* a purpose
    # sentence look like one that does not.
    head = f"This is the `{ctx.meta.slug}` repository." if "repo_name" in needed else ""
    suffixes: list[str] = []
    if "language" in needed:
        suffixes.append(f"Primary language: {_language_label(ctx) or NO_LANGUAGE}.")
    if "purpose" in needed:
        suffixes.append(f"What it is for: {_purpose(ctx)}.")
    if "paths" in needed:
        suffixes.append(_reading_order(ctx))
    if "questions" in needed:
        lines = "\n".join(f"- {question}" for question in FALLBACK_QUESTIONS)
        suffixes.append(f"You could ask me:\n{lines}")
    return head, tuple(suffixes)


def _purpose(ctx: RepoContext) -> str:
    """One sentence on what the repository is for. Requirement 5 AC3.

    GitHub's description is the honest source. When there is none, say there is
    none: the alternative is inventing a purpose, which is the one failure mode
    the system prompt spends a paragraph forbidding.
    """
    description = _one_line(ctx.meta.description).rstrip(" .")
    return (
        description or "The repository does not state its purpose in the files I read"
    )


def _reading_order(ctx: RepoContext) -> str:
    """Starting points drawn from the ingested manifest. Requirement 5 AC4.

    The top of the manifest is the top of `selection`'s priority order -- the
    readme, the dependency manifest, the entry point -- which is the reading
    order a newcomer wants anyway. Paths with a body come first, so a suggested
    file is one the model could actually have described.
    """
    paths = _manifest(ctx)[:ORIENTATION_MIN_PATHS]
    if not paths:
        return (
            "I could not read any files in this repository, so I have no "
            "starting points to suggest"
        )
    joined = ", ".join(f"`{path}`" for path in paths)
    return f"Good places to start: {joined}."


def _manifest(ctx: RepoContext) -> list[str]:
    """Every ingested path, readable ones first. Requirements 4 AC5, 5 AC4."""
    with_body = [block.path for block in ctx.files if block.content]
    name_only = [block.path for block in ctx.files if not block.content]
    return [*with_body, *name_only, *ctx.unread_paths]


def _named_paths(text: str, ctx: RepoContext) -> list[str]:
    return [path for path in _manifest(ctx) if path in text]


def _language_label(ctx: RepoContext) -> str | None:
    """The detected label, falling back to GitHub's field.

    The fallback cannot disagree with detection: `detect_language` consults
    GitHub's field first, so a context built without a detected language holds
    the same value detection would have produced from metadata alone.
    """
    return (ctx.detected_language or ctx.meta.language or "").strip() or None


def _mentions_language(text: str, ctx: RepoContext) -> bool:
    """Whether `text` names the detected language or its framework.

    Either half of a `"Python (Flask)"` label counts. A model that writes "a
    Flask application" has oriented the reader exactly as Requirement 5 AC2
    intends, and demanding the word "Python" as well would repair a good answer.

    Nothing detected means nothing to find, so the repair fires and states that
    plainly -- which is the other branch Requirement 5 AC2 allows.
    """
    label = _language_label(ctx)
    if not label:
        return False
    return any(_mentions(text, token) for token in _language_tokens(label))


def _language_tokens(label: str) -> list[str]:
    """The searchable words of a label: `"Python (Flask)"` -> Python, Flask.

    `+`, `#`, and `.` stay inside a token so `C++`, `C#`, and `Next.js` survive
    intact. Single-character tokens are dropped when longer ones exist, since a
    bare "C" from "Objective-C" would match almost any text -- unless the whole
    label is one character, as it is for R and C, where it is all there is.
    """
    tokens = re.findall(r"[A-Za-z0-9+#.]+", label)
    return [token for token in tokens if len(token) > 1] or tokens


def _mentions(text: str, token: str) -> bool:
    """Case-insensitive whole-word containment.

    Word-boundary, not substring: `"go" in "good places to start"` is true and
    would let the language repair be skipped on a message that never named Go.
    `(?<!\\w)`/`(?!\\w)` rather than `\\b` because a token can end in `+` or `#`,
    after which `\\b` asserts the opposite of what is wanted.
    """
    pattern = rf"(?<!\w){re.escape(token)}(?!\w)"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _close_fence(text: str) -> str:
    """Close an unterminated code fence, so appended repairs land outside it."""
    if text.count(FENCE) % 2 == 0:
        return text
    return f"{text}\n{FENCE}"


def _one_line(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.split())


def _words(text: str) -> int:
    """Words the way `cap_words` counts them: runs of non-whitespace."""
    return len(text.split())
