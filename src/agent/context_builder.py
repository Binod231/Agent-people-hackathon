"""Repo_Context assembly: the tree summary, the manifest, and the block format.

This module owns every rendering rule in the pipeline. `models.FileBlock.render`
and `models.RepoContext.render` delegate here, so there is exactly one place
that decides what the model sees -- which matters more than it sounds, because
`tokens.fit_to_budget` prices a block by rendering it. A format rule that lived
anywhere else could spend tokens the budget never saw.

Four things it is responsible for:

- **The tree summary** (Requirement 3 AC2). The first two directory levels --
  root items and one level below, so at most one `/` in a path -- capped at
  `MAX_TREE_ENTRIES`, directories before files, and noise directories collapsed
  to a single entry so a vendored `node_modules/` cannot spend 200 slots on
  code nobody is here to read.
- **The block format.** Plain delimiters, not JSON or XML. JSON would escape
  every newline in every source file, which is an enormous token tax, and XML
  tags invite the model to answer in markup. The truncation label states both
  counts -- `[truncated: first 7,948 of 97,412 chars]` -- so the model knows the
  size of what it cannot see and can say so instead of guessing.
- **The delimiter-collision guard.** Repository content is untrusted input going
  into a prompt, so any line in a file body that opens with `=== ` is rewritten
  to `==== ` before rendering. A hostile file therefore cannot forge a block
  boundary or close the context early. Paired with the system prompt's
  statement that file content is data rather than instruction, this bounds
  prompt injection; the residual risk is low because the model has no tools, no
  writes, and its output returns only to the caller who supplied the URL.
- **The two context tiers.** Orientation gets the top `ORIENTATION_FILE_COUNT`
  bodies at `ORIENTATION_FILE_CHARS` each to make the 20-second budget
  (Requirement 5 AC6); a chat turn gets up to `MAX_SELECTED_FILES` bodies at
  `MAX_FILE_CHARS` and has 30 seconds (Requirement 4 AC6).

**Both tiers carry the full manifest.** Every selected path is listed in
`INGESTED FILES`, with a qualifier saying whether its contents are present,
truncated, name-only, or unreadable, even in the lean tier where only four
bodies are included. That is what makes Requirement 5 AC4 ("name at least three
ingested repository file paths") satisfiable from the orientation context: the
paths are genuinely part of Repo_Context, and the model is told which ones it
can read rather than left to infer it. Requirement 4 AC5 ("name the files that
were ingested") draws on the same manifest.

A path that could not be read -- a failed fetch, a NUL byte, a body that is not
UTF-8 -- stays in the manifest as `not readable` and in `RepoContext`'s
`unread_paths`. Nothing is quietly dropped: a gap the model can see is a gap it
can own up to, and a gap it cannot see is a gap it will fill in by inventing
something.

Pure: no network, no disk, no clock, no module state.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from .config import (
    BUDGET_TREE,
    MAX_FILE_CHARS,
    MAX_SELECTED_FILES,
    MAX_TREE_DEPTH,
    MAX_TREE_ENTRIES,
    ORIENTATION_FILE_CHARS,
    ORIENTATION_FILE_COUNT,
)
from .errors import AgentError, ErrorCode
from .models import Candidate, FileBlock, RepoContext, RepoMeta, TreeEntry
from .selection import NOISE_DIRS, TIER_LABELS, decode_text
from .tokens import available_for_files, estimate_tokens, fit_to_budget
from .truncate import truncate

__all__ = [
    "CHAT_TIER",
    "DELIMITER",
    "ORIENTATION_TIER",
    "ContextTier",
    "build_chat_context",
    "build_context",
    "build_orientation_context",
    "context_tokens",
    "escape_delimiters",
    "render_block",
    "render_context",
    "tree_summary",
]

# The block delimiter, and the prefix a file line is escaped away from. A line
# opening with `=== ` is the only shape that can be mistaken for a boundary.
DELIMITER = "==="
_OPENER = f"{DELIMITER} "

# Manifest qualifiers. The model reads these to tell what it can actually see.
_Q_TRUNCATED = "truncated"
_Q_NAME_ONLY = "name only"
_Q_UNREADABLE = "not readable"
_Q_EMPTY = "empty"


# ---------------------------------------------------------------------------
# The two context tiers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextTier:
    """How many file bodies a tier includes, and how much of each.

    The manifest is deliberately not a field: both tiers carry every selected
    path, and only the bodies differ.

    The orientation numbers are the design's escape hatch for Requirement 5
    AC6. If measured p95 latency lands above 15 seconds, dropping
    `ORIENTATION_FILE_COUNT` to 2 and `ORIENTATION_FILE_CHARS` to 1,500 in
    `config.py` cuts the orientation input to roughly 2,500 tokens without a
    code change here.
    """

    name: str
    file_count: int
    file_chars: int


ORIENTATION_TIER = ContextTier(
    name="orientation",
    file_count=ORIENTATION_FILE_COUNT,
    file_chars=ORIENTATION_FILE_CHARS,
)
CHAT_TIER = ContextTier(
    name="chat",
    file_count=MAX_SELECTED_FILES,
    file_chars=MAX_FILE_CHARS,
)


# ---------------------------------------------------------------------------
# The directory tree summary (Requirement 3 AC2)
# ---------------------------------------------------------------------------


def tree_summary(
    entries: Iterable[TreeEntry],
    *,
    max_entries: int = MAX_TREE_ENTRIES,
    budget: int = BUDGET_TREE,
) -> tuple[list[str], int]:
    """Summarize a repository tree. Requirement 3 AC2, Property 3.

    Returns `(lines, overflow)` -- the entries that fit, and how many were
    dropped. `overflow` is rendered as `… and N more entries`, so a truncated
    summary says it is truncated rather than implying the repository is small.

    Three bounds, all unconditional:

    - **Depth.** "First two directory levels" means root items and one level
      below, so at most `MAX_TREE_DEPTH` slashes in a path.
    - **Count.** At most `max_entries` lines.
    - **Tokens.** The summary is also trimmed to `budget` tokens. Two hundred
      ordinary paths cost roughly 1,100 estimated tokens, so this normally does
      nothing; a monorepo whose top-level names run 80 characters each is why
      it exists.

    Directories sort before files, each group alphabetically, which gives the
    model the shape of the project before its contents. Noise directories
    collapse to a single trailing-slash entry -- `node_modules/` appears, its
    children do not -- and the entry is synthesized when the listing shows only
    children, so the collapse never erases the fact that the directory is there.

    Duplicate paths collapse (a set, then a sort), so the bounds hold per path
    rather than per entry. Submodule pointers (`kind == "commit"`) are skipped:
    they name a commit in another repository, and nothing of theirs is readable
    from here.
    """
    if max_entries < 0:
        raise ValueError(f"max_entries must be non-negative, got {max_entries}")
    if budget < 0:
        raise ValueError(f"budget must be non-negative, got {budget}")

    dirs: set[str] = set()
    files: set[str] = set()
    for entry in entries:
        path = entry.path
        if not path:
            continue
        head, slash, _ = path.partition("/")
        # Noise first, then depth: a collapsed entry has to be reachable from a
        # deep child, which the depth bound would otherwise have discarded.
        # Lowercased to match `selection.classify`, so `Tests/` collapses the
        # same way `tests/` does.
        if slash and head.lower() in NOISE_DIRS:
            dirs.add(f"{head}/")
            continue
        if path.count("/") > MAX_TREE_DEPTH:
            continue
        if entry.kind == "tree":
            dirs.add(f"{path}/")
        elif entry.kind == "blob":
            files.add(path)

    merged = sorted(dirs) + sorted(files)
    kept = merged[:max_entries]
    overflow = len(merged) - len(kept)
    # Re-prices the whole summary on each pop, which is why the common case
    # exits before the first iteration: 200 paths are well inside the budget,
    # and the loop is a bound on the pathological tree rather than a hot path.
    while kept and estimate_tokens("\n".join(kept), overhead=0) > budget:
        kept.pop()
        overflow += 1
    return kept, overflow


# ---------------------------------------------------------------------------
# Block rendering and the delimiter-collision guard
# ---------------------------------------------------------------------------


def escape_delimiters(content: str) -> str:
    """Rewrite any line opening with `=== ` to `==== `.

    The whole of the injection guard on the rendering side. A file cannot forge
    `=== END FILE: ... ===` or open a block of its own, because every line that
    could be read as a boundary gains a character first. An already-escaped
    line is left alone: `==== ` is not a delimiter, so it needs no second pass.

    The containment check is not an optimization for its own sake -- it is the
    common case. Source files do not open lines with `=== `, so almost every
    body returns without being split and rejoined.
    """
    if _OPENER not in content:
        return content
    return "\n".join(
        f"={line}" if line.startswith(_OPENER) else line for line in content.split("\n")
    )


def render_block(block: FileBlock) -> str:
    """Render one delimited file block. Requirement 3 AC5.

    The truncation label states both counts, so the model can tell the
    difference between a short file and the first tenth of a long one:

        === FILE: src/flask/app.py [truncated: first 7,948 of 97,412 chars] ===
        ...
        === END FILE: src/flask/app.py ===

    The total is `max(original_chars, len(content))` rather than
    `original_chars` outright, so a block whose two counts disagree still reads
    as "there is more text here than you can see" instead of claiming the
    visible text is longer than the file.

    This is the function `tokens.fit_to_budget` prices with, so everything it
    adds -- the header, the label, the escape -- is paid for out of the budget
    rather than charged to the model behind the estimator's back.
    """
    path = _one_line(block.path)
    label = ""
    if block.truncated:
        total = max(block.original_chars, len(block.content))
        label = f" [truncated: first {len(block.content):,} of {total:,} chars]"
    return (
        f"{DELIMITER} FILE: {path}{label} {DELIMITER}\n"
        f"{escape_delimiters(block.content)}\n"
        f"{DELIMITER} END FILE: {path} {DELIMITER}"
    )


# ---------------------------------------------------------------------------
# Context rendering
# ---------------------------------------------------------------------------


def render_context(context: RepoContext) -> str:
    """Render the whole of Repo_Context as the model will receive it."""
    prose, blocks = _split(context)
    return "\n\n".join([prose, *(render_block(block) for block in blocks)])


def context_tokens(context: RepoContext) -> int:
    """Estimated input cost of `context`, priced the way fitting priced it.

    The prose sections are charged as one content block and each file block is
    charged individually, which is exactly the arithmetic `build_context` ran
    against the budget. A caller adding the system prompt and the conversation
    history on top compares `RESERVE_SYSTEM + history + this` against
    `tokens.within_budget` to get the final tripwire over the whole request.
    """
    prose, blocks = _split(context)
    return estimate_tokens(prose) + sum(
        estimate_tokens(render_block(block)) for block in blocks
    )


def _split(context: RepoContext) -> tuple[str, list[FileBlock]]:
    """The prose sections joined, and the blocks that have a body to show.

    Rendering and pricing both go through here, so the number the budget was
    checked against and the text that is actually sent can never describe
    different things.
    """
    sections = [
        _repository_section(context.meta, context.detected_language),
        _tree_section(context.tree_lines, context.tree_overflow),
        _manifest_section(_manifest_rows(context.files, context.unread_paths)),
    ]
    notes = _notes_section(context.notes, context.unread_paths)
    if notes:
        sections.append(notes)
    return "\n\n".join(sections), [block for block in context.files if block.content]


def _repository_section(meta: RepoMeta, detected_language: str | None) -> str:
    """The metadata header. Requirement 2 AC4, and Requirement 5 AC2's source.

    Both language lines are always present, and both say so explicitly when
    they have nothing: Property 11 requires the Orientation_Message to state a
    language *or* state that none was detected, which it can only do if the
    context distinguishes "not detected" from "not mentioned".
    """
    return "\n".join(
        [
            f"{DELIMITER} REPOSITORY {DELIMITER}",
            f"name: {_one_line(meta.slug)}",
            f"default branch: {_one_line(meta.default_branch)}",
            f"language: {_one_line(detected_language) or 'not detected'}",
            f"github language: {_one_line(meta.language) or 'not reported'}",
            f"description: {_one_line(meta.description) or 'none given'}",
            f"size: {meta.size_kb / 1024:.1f} MB",
        ]
    )


def _tree_section(lines: Sequence[str], overflow: int) -> str:
    body = list(lines)
    if overflow > 0:
        body.append(f"… and {overflow} more entries")
    head = (
        f"{DELIMITER} DIRECTORY TREE "
        f"(first two levels, {len(lines)} entries shown) {DELIMITER}"
    )
    return "\n".join([head, *body])


def _manifest_section(rows: Sequence[str]) -> str:
    head = f"{DELIMITER} INGESTED FILES ({len(rows)}) {DELIMITER}"
    return "\n".join([head, *rows])


def _manifest_rows(files: Sequence[FileBlock], unread: Sequence[str]) -> list[str]:
    """One numbered row per selected path, readable ones first.

    The unreadable paths are appended rather than interleaved back into
    priority order: `RepoContext` keeps them as bare strings, so their tier is
    not available here, and their position is cosmetic anyway since there is no
    body to read. What matters is that they are present and labeled.
    """
    rows = [
        f"{index}. {_one_line(block.path)} ({_label(block.tier)}{_qualifier(block)})"
        for index, block in enumerate(files, start=1)
    ]
    rows += [
        f"{index}. {_one_line(path)} ({_Q_UNREADABLE})"
        for index, path in enumerate(unread, start=len(rows) + 1)
    ]
    return rows


def _notes_section(notes: Sequence[str], unread: Sequence[str]) -> str:
    """Omissions, stated plainly. Empty when there is nothing to own up to."""
    lines = [_one_line(note) for note in notes if note and note.strip()]
    if unread:
        lines.append(f"could not read: {', '.join(_one_line(p) for p in unread)}")
    if not lines:
        return ""
    return "\n".join([f"{DELIMITER} NOTES {DELIMITER}", *lines])


def _label(tier: int) -> str:
    return TIER_LABELS.get(tier, "source")


def _qualifier(block: FileBlock) -> str:
    """What the model can see of this file, as a manifest suffix."""
    if block.original_chars == 0:
        return f", {_Q_EMPTY}"
    if not block.content:
        return f", {_Q_NAME_ONLY}"
    if block.truncated:
        return f", {_Q_TRUNCATED}"
    return ""


def _one_line(value: str | None) -> str:
    """Flatten newlines out of a single-line field.

    Paths, descriptions, and branch names are rendered inside one line of the
    context. A POSIX file name may legally contain a newline, and the tree API
    returns it decoded, so without this a crafted path could carry its own
    `=== END FILE: ... ===` into the header -- the same forgery
    `escape_delimiters` closes on the body side. Real repositories never hit
    this; the guard costs one `replace` per field.
    """
    if not value:
        return ""
    return value.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").strip()


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build_context(
    meta: RepoMeta,
    entries: Iterable[TreeEntry],
    candidates: Sequence[Candidate],
    bodies: Mapping[str, bytes],
    tier: ContextTier,
    *,
    detected_language: str | None = None,
    history_tokens: int = 0,
) -> RepoContext:
    """Assemble Repo_Context for one tier. Requirement 3 AC2, AC4, AC6; 4 AC5.

    Args:
        meta: What `GET /repos/{owner}/{repo}` reported.
        entries: The full recursive tree, for the summary.
        candidates: `selection.select`'s output, in priority order. This is the
            manifest, and every path in it reaches the model in some form.
        bodies: Raw file bodies from `github_client.read_files`, keyed by path.
            A path absent from the map could not be fetched.
        tier: `ORIENTATION_TIER` or `CHAT_TIER`.
        detected_language: `orientation.detect_language`'s result. Falls back to
            GitHub's field, which is what `detect_language` consults first
            anyway, so the fallback cannot disagree with it.
        history_tokens: Estimated cost of the conversation window, so the file
            budget is what is actually left rather than what was reserved.

    Raises:
        AgentError: `NO_READABLE_FILES` when nothing decoded. Every selected
            file was unfetchable, binary, or not UTF-8, so there is nothing to
            ground an answer in and saying so beats asking the model to
            improvise (Requirement 3 AC10, AC11).

    The order of operations is what makes the budget hold:

    1. Decode every fetched body. `selection.decode_text` is the hard binary
       check, and this is the only place raw bytes become text, so Property 5's
       "never reaches the model" is a statement about one function call.
    2. Give the tier's quota of bodies their text, truncated to the tier's
       per-file cap. The rest stay in the manifest as name-only.
    3. Price the prose sections *before* fitting, against a worst case where
       every body is omitted, so the claim the file budget is computed from can
       only be larger than what is finally rendered.
    4. Fit the bodies to what is left. A body the budget cannot pay for becomes
       name-only and is named in a note, so the manifest stays complete and the
       omission is visible.
    """
    readable: list[tuple[Candidate, str]] = []
    unread: list[str] = []
    for candidate in _unique(candidates):
        raw = bodies.get(candidate.path)
        text = None if raw is None else decode_text(raw)
        if text is None:
            unread.append(candidate.path)
        else:
            readable.append((candidate, text))

    if not readable:
        raise AgentError(ErrorCode.NO_READABLE_FILES)

    blocks = [
        _block(candidate, text, tier.file_chars)
        for candidate, text in readable[: tier.file_count]
    ]
    named = [
        _name_only(_block(candidate, text, tier.file_chars))
        for candidate, text in readable[tier.file_count :]
    ]
    tree_lines, tree_overflow = tree_summary(entries)
    language = detected_language or meta.language

    # The prose claim: the tree summary, the metadata header, the manifest, and
    # the notes. None of them is fitted, so all of them are charged up front.
    # The manifest rows and the notes are priced at their widest -- every body
    # dropped, every path named in the omission note -- which over-provisions
    # by a few dozen tokens and can therefore never under-provision.
    worst_case = RepoContext(
        meta=meta,
        detected_language=language,
        tree_lines=tree_lines,
        tree_overflow=tree_overflow,
        files=[_name_only(block) for block in blocks] + named,
        unread_paths=unread,
        notes=_omission_notes(blocks),
        estimated_tokens=0,
    )
    prose, _ = _split(worst_case)

    kept, notes = fit_to_budget(
        blocks,
        available_for_files(history_tokens, estimate_tokens(prose)),
        render_block,
    )
    survived = {block.path for block in kept}
    files = _in_manifest_order(
        kept + [_name_only(b) for b in blocks if b.path not in survived] + named,
        candidates,
    )

    context = RepoContext(
        meta=meta,
        detected_language=language,
        tree_lines=tree_lines,
        tree_overflow=tree_overflow,
        files=files,
        unread_paths=unread,
        notes=notes,
        estimated_tokens=0,
    )
    return replace(context, estimated_tokens=context_tokens(context))


def build_orientation_context(
    meta: RepoMeta,
    entries: Iterable[TreeEntry],
    candidates: Sequence[Candidate],
    bodies: Mapping[str, bytes],
    *,
    detected_language: str | None = None,
) -> RepoContext:
    """The lean tier: four bodies, the full manifest. Requirement 5 AC6.

    No `history_tokens`: an orientation is the first message of a session, so
    there is no history to pay for (Requirement 5 AC1).
    """
    return build_context(
        meta,
        entries,
        candidates,
        bodies,
        ORIENTATION_TIER,
        detected_language=detected_language,
    )


def build_chat_context(
    meta: RepoMeta,
    entries: Iterable[TreeEntry],
    candidates: Sequence[Candidate],
    bodies: Mapping[str, bytes],
    *,
    detected_language: str | None = None,
    history_tokens: int = 0,
) -> RepoContext:
    """The full tier: twelve bodies at the full per-file cap. Requirement 4."""
    return build_context(
        meta,
        entries,
        candidates,
        bodies,
        CHAT_TIER,
        detected_language=detected_language,
        history_tokens=history_tokens,
    )


def _block(candidate: Candidate, text: str, limit: int) -> FileBlock:
    kept, was_truncated = truncate(text, limit)
    return FileBlock(
        path=candidate.path,
        tier=candidate.tier,
        content=kept,
        original_chars=len(text),
        truncated=was_truncated,
    )


def _name_only(block: FileBlock) -> FileBlock:
    """The same file with its body withheld: a manifest row and nothing else.

    `original_chars` is kept, so the row still knows the file was not empty and
    the qualifier reads `name only` rather than `empty`.
    """
    return replace(block, content="", truncated=block.original_chars > 0)


def _omission_notes(blocks: Sequence[FileBlock]) -> list[str]:
    """The note `fit_to_budget` would write if it could afford nothing.

    Used only to price the prose, so the figure the file budget is derived from
    bounds the text that is finally rendered.
    """
    if not blocks:
        return []
    _, notes = fit_to_budget(blocks, 0, render_block)
    return notes


def _in_manifest_order(
    blocks: Iterable[FileBlock], candidates: Sequence[Candidate]
) -> list[FileBlock]:
    """Sort blocks back into `candidates` order, which is priority order.

    Fitting preserves input order but splits the list in two -- admitted, then
    demoted to name-only -- and a manifest that listed the survivors first
    would imply a priority order that is not the one selection applied.
    """
    position = {candidate.path: index for index, candidate in enumerate(candidates)}
    fallback = len(position)
    return sorted(
        blocks, key=lambda block: (position.get(block.path, fallback), block.path)
    )


def _unique(candidates: Sequence[Candidate]) -> list[Candidate]:
    """Collapse duplicate paths, keeping the first. Order-preserving.

    `selection.select` cannot return a duplicate, so against real input this is
    the identity. It runs because the manifest count and the file blocks are
    derived from this list, and one repeated path would render one file twice
    and claim thirteen files in a twelve-file manifest.
    """
    seen: set[str] = set()
    unique: list[Candidate] = []
    for candidate in candidates:
        if candidate.path not in seen:
            seen.add(candidate.path)
            unique.append(candidate)
    return unique
