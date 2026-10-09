"""Frozen dataclasses shared across the pipeline.

Nothing here is mutated after construction, which is what makes the ingestion
pipeline genuinely pure and the correctness properties straightforward to
assert: a function that takes a `RepoContext` cannot have changed it.

`StepName`, `StepRecord`, and `Deadline` are defined here, with the module as
the leaf of the dependency graph, and re-exported from `steps.py` alongside
`StepRecorder` so callers can import the whole step vocabulary from one place.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "Candidate",
    "Deadline",
    "FileBlock",
    "RepoContext",
    "RepoMeta",
    "RepoRef",
    "StepName",
    "StepRecord",
    "TreeEntry",
]


@dataclass(frozen=True)
class RepoRef:
    """A canonicalized GitHub repository reference. Requirement 2 AC2."""

    owner: str
    repo: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True)
class RepoMeta:
    """What `GET /repos/{owner}/{repo}` tells us. Requirement 2 AC4."""

    slug: str  # "pallets/flask"
    default_branch: str
    size_kb: int  # GitHub reports KILOBYTES -- Requirement 3 AC8 is 102_400 of these
    language: str | None  # GitHub's guess; None when it cannot classify the repo
    description: str
    html_url: str
    archived: bool


@dataclass(frozen=True)
class TreeEntry:
    """One entry from the recursive default-branch tree listing."""

    path: str
    kind: str  # "blob" | "tree" | "commit" (submodule pointer)
    size: int  # bytes; 0 for trees


@dataclass(frozen=True)
class Candidate:
    """A file selection picked, before its body has been read."""

    path: str
    tier: int  # 1..5, the Requirement 3 AC1 priority order
    size: int


@dataclass(frozen=True)
class FileBlock:
    """One file's body as it will appear in Repo_Context.

    `content` is always a prefix of the fetched text -- truncation and budget
    fitting only ever shorten it, never rewrite it (Property 6, Property 7).
    """

    path: str
    tier: int
    content: str  # always a prefix of the fetched text
    original_chars: int
    truncated: bool  # true iff len(content) < original_chars

    def render(self) -> str:
        """Render the delimited file block.

        The plain-delimiter format, the truncation label, and the
        delimiter-collision guard all live in `context_builder`, which owns
        every rendering rule in one place.

        The import is deferred to call time because `context_builder` imports
        this module: resolving it here instead of at module scope keeps the
        dependency graph acyclic at import time, and lets
        `tokens.fit_to_budget` keep this method as its default renderer and
        still price a block exactly as it will be rendered.
        """
        from .context_builder import render_block

        return render_block(self)


@dataclass(frozen=True)
class RepoContext:
    """The bounded context handed to the model. Requirement 3."""

    meta: RepoMeta
    detected_language: str | None
    tree_lines: list[str]
    tree_overflow: int  # entries dropped past MAX_TREE_ENTRIES
    files: list[FileBlock]  # the ingested manifest, in priority order
    unread_paths: list[str]  # selected but unfetchable
    notes: list[str]  # "omitted for space: ..."
    estimated_tokens: int

    def render(self) -> str:
        """Render the whole context block.

        Delegates to `context_builder.render_context`, which assembled this
        object and owns the format. Deferred import, for the reason given on
        `FileBlock.render`.
        """
        from .context_builder import render_context

        return render_context(self)


class StepName(str, Enum):
    """The step vocabulary shared by the handler and `ui/app.js`.

    Declaration order is execution order. Requirement 5 AC7 needs these names
    to reach the Chat_UI, so they are a contract, not an implementation detail:
    renaming a member is a breaking change to the UI.
    """

    VALIDATING_URL = "validating_url"
    FETCHING_METADATA = "fetching_metadata"
    LISTING_FILES = "listing_files"
    SELECTING_FILES = "selecting_files"
    READING_FILES = "reading_files"
    BUILDING_CONTEXT = "building_context"
    ASKING_NOVA = "asking_nova"


@dataclass(frozen=True)
class StepRecord:
    """One step's outcome and duration, as returned on every response."""

    name: StepName
    status: str  # "ok" | "error" | "skipped"
    ms: int

    def as_dict(self) -> dict[str, str | int]:
        return {"name": self.name.value, "status": self.status, "ms": self.ms}


@dataclass(frozen=True)
class Deadline:
    """Monotonic budget guard. Requirements 4 AC6, 5 AC6.

    `clock` is injected so tests drive the deadline with a fake monotonic
    counter instead of waiting 26 real seconds (Property 16).
    """

    started: float
    budget_ms: int
    clock: Callable[[], float] = field(default=time.monotonic)

    def remaining_ms(self) -> int:
        spent_ms = (self.clock() - self.started) * 1000.0
        return int(self.budget_ms - spent_ms)

    def allows(self, seconds: float) -> bool:
        """True when `seconds` of work still fits inside the budget.

        Strictly greater, not greater-or-equal: the budget is spent against
        estimates, so an attempt that exactly consumes what is left is refused.
        """
        return self.remaining_ms() > seconds * 1000.0
