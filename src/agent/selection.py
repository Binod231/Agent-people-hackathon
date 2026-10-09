"""Five-tier file selection: which twelve files the model gets to see.

Requirement 3 AC1 fixes the priority order and this module is its only
implementation:

    1  README files
    2  dependency and build manifests
    3  configuration files
    4  program entry points
    5  top-level source directory representatives

`classify` maps one tree entry to a tier or to `None`, `select` turns a whole
tree into at most `MAX_SELECTED_FILES` candidates, and `decode_text` is the hard
binary check that runs after the bodies come back.

Three guarantees the rest of the pipeline leans on:

- **Pure.** No network, no disk, no clock, no module state. Tree data in,
  candidates out. That is what makes the whole ingestion path testable offline.
- **Deterministic.** The same tree and the same `package_main` always yield the
  same list, in the same order. Every in-tier ordering is an explicit `sorted`
  over a total key, and nothing iterates a set in a way that can change the
  result. Downstream assertions -- the paths the orientation names, the manifest
  the model cites, the token estimate -- all depend on this (Property 4).
- **Priority-respecting.** Tiers are drained in order, so a tier-5 file is never
  selected while a tier-4 candidate was passed over (Property 4).

The extension denylist is a *pre-filter* that saves a fetch, not the binary
guarantee. `decode_text` is the guarantee (Requirement 3 AC11, Property 5): a
`.txt` file full of binary passes the pre-filter and is rejected there.

`package.json` entry-point resolution is a second pass, since file bodies are
read after selection: resolve `main` from the fetched `package.json` with
`resolve_package_main`, then call `select` again with it. `select` is a pure
function of its arguments, so the second pass is idempotent over the first.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable, Mapping
from fnmatch import fnmatchcase

from .config import MAX_FILE_BYTES, MAX_SELECTED_FILES
from .models import Candidate, TreeEntry

__all__ = [
    "BINARY_EXT",
    "CONFIGS",
    "CONFIG_GLOBS",
    "ENTRYPOINTS",
    "MANIFESTS",
    "NOISE_DIRS",
    "NOISE_FILES",
    "P1_README",
    "P2_MANIFEST",
    "P3_CONFIG",
    "P4_ENTRY",
    "P5_SOURCE",
    "TEXT_EXT",
    "TIER_LABELS",
    "TIER_ORDER",
    "classify",
    "decode_text",
    "resolve_package_main",
    "select",
]

# ---------------------------------------------------------------------------
# The five tiers (Requirement 3 AC1). Lower number wins.
# ---------------------------------------------------------------------------

P1_README, P2_MANIFEST, P3_CONFIG, P4_ENTRY, P5_SOURCE = 1, 2, 3, 4, 5

TIER_ORDER: tuple[int, ...] = (P1_README, P2_MANIFEST, P3_CONFIG, P4_ENTRY, P5_SOURCE)

# Rendered beside each path in the context manifest, so the model knows why a
# file was chosen rather than guessing from the name.
TIER_LABELS: Mapping[int, str] = {
    P1_README: "readme",
    P2_MANIFEST: "manifest",
    P3_CONFIG: "config",
    P4_ENTRY: "entry point",
    P5_SOURCE: "source",
}

# ---------------------------------------------------------------------------
# Vocabulary. Frozen so a caller cannot mutate the classifier's behavior.
# ---------------------------------------------------------------------------

MANIFESTS = frozenset(
    {
        "package.json",
        "requirements.txt",
        "pyproject.toml",
        "setup.py",
        "Pipfile",
        "go.mod",
        "Cargo.toml",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "Gemfile",
        "composer.json",
        "Makefile",
        "CMakeLists.txt",
        "Dockerfile",
        "pubspec.yaml",
        "mix.exs",
        "build.sbt",
        "deno.json",
        "Package.swift",
    }
)
CONFIGS = frozenset(
    {
        "tsconfig.json",
        "setup.cfg",
        "tox.ini",
        "serverless.yml",
        "template.yaml",
        ".env.example",
        "app.yaml",
        "netlify.toml",
        "vercel.json",
        "Procfile",
    }
)
CONFIG_GLOBS: tuple[str, ...] = (
    "*.config.js",
    "*.config.ts",
    "*.config.mjs",
    "vite.config.*",
    "next.config.*",
    "webpack.config.*",
    ".eslintrc*",
    "*.tf",
)
ENTRYPOINTS = frozenset(
    {
        "main.py",
        "app.py",
        "__main__.py",
        "manage.py",
        "wsgi.py",
        "asgi.py",
        "index.js",
        "index.ts",
        "main.js",
        "main.ts",
        "server.js",
        "server.ts",
        "app.js",
        "app.ts",
        "main.go",
        "main.rs",
        "lib.rs",
        "Main.java",
        "Program.cs",
        "main.cpp",
        "main.c",
    }
)

# Directories whose contents explain tooling, tests, or vendored code rather
# than the project itself. `context_builder.tree_summary` collapses these to a
# single entry so the shape of the repository stays legible.
NOISE_DIRS = frozenset(
    {
        ".git",
        ".github",
        ".idea",
        ".vscode",
        "node_modules",
        "vendor",
        "dist",
        "build",
        "out",
        "target",
        ".venv",
        "venv",
        "__pycache__",
        ".next",
        "coverage",
        "test",
        "tests",
        "spec",
        "__tests__",
        "docs",
        "doc",
        "examples",
        "example",
        "assets",
        "static",
        "public",
        "fixtures",
        "locale",
        "locales",
        "i18n",
        "migrations",
        "third_party",
    }
)

# Lock files: large, machine-generated, and they say nothing a newcomer needs.
# Excluded by name rather than by size, because a small lock file is no more
# informative than a big one.
NOISE_FILES = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "Cargo.lock",
        "Gemfile.lock",
        "composer.lock",
        "go.sum",
        "pdm.lock",
    }
)

# Requirement 3 AC11, cheap layer. Matched as suffixes, so compound endings
# like ".min.js" and ".tar.gz" work without special casing.
BINARY_EXT = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".svg",
        ".bmp",
        ".tiff",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".jar",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".mp3",
        ".mp4",
        ".wav",
        ".mov",
        ".avi",
        ".webm",
        ".so",
        ".dylib",
        ".dll",
        ".exe",
        ".bin",
        ".dat",
        ".class",
        ".pyc",
        ".pyo",
        ".wasm",
        ".parquet",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".pkl",
        ".onnx",
        ".pt",
        ".safetensors",
        ".min.js",
        ".min.css",
        ".map",
    }
)

# Tier 5 admits a file only if its extension is recognizably source or prose.
# An allowlist, not a denylist: an unknown extension in a source directory is
# far more likely to be data than code, and a missed source file costs one slot
# while a blob of data costs a fetch and a chunk of the token budget.
TEXT_EXT = frozenset(
    {
        # Python, Ruby, PHP, Perl, shell
        ".py",
        ".pyi",
        ".pyx",
        ".rb",
        ".rake",
        ".php",
        ".pl",
        ".pm",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".ps1",
        # JavaScript and TypeScript
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".mts",
        ".cts",
        ".vue",
        ".svelte",
        # JVM and .NET
        ".java",
        ".kt",
        ".kts",
        ".scala",
        ".groovy",
        ".clj",
        ".cljs",
        ".cs",
        ".fs",
        ".vb",
        # systems
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".cxx",
        ".hpp",
        ".hh",
        ".hxx",
        ".m",
        ".mm",
        ".go",
        ".rs",
        ".swift",
        ".zig",
        ".nim",
        ".d",
        # functional and scientific
        ".ex",
        ".exs",
        ".erl",
        ".hs",
        ".ml",
        ".lua",
        ".r",
        ".jl",
        ".dart",
        ".sol",
        # schema, query, and interface definitions
        ".sql",
        ".graphql",
        ".gql",
        ".proto",
        ".thrift",
        ".avsc",
        # markup, style, and prose
        ".html",
        ".htm",
        ".css",
        ".scss",
        ".sass",
        ".less",
        ".md",
        ".mdx",
        ".rst",
        ".txt",
        ".adoc",
        # configuration and build
        ".json",
        ".jsonc",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".properties",
        ".env",
        ".tf",
        ".tfvars",
        ".gradle",
        ".cmake",
        ".mk",
        ".bazel",
        ".bzl",
        ".nix",
    }
)

# Sorted tuples for the suffix scans: `str.endswith` takes a tuple at C speed,
# and sorting pins the iteration order so nothing about the outcome can depend
# on set hashing.
_BINARY_SUFFIXES: tuple[str, ...] = tuple(sorted(BINARY_EXT))
_TEXT_SUFFIXES: tuple[str, ...] = tuple(sorted(TEXT_EXT))


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify(entry: TreeEntry, package_main: str | None = None) -> int | None:
    """Return the priority tier for one tree entry, or `None` to skip it.

    Exclusions run first and are unconditional (Requirements 3 AC10, AC11), so
    no amount of priority can pull a lock file, a vendored file, an oversize
    file, or a binary into the selection. Tiers are then tested in Requirement 3
    AC1 order, first match wins.

    `package_main` is the repository-relative path named by `package.json`'s
    `main` field, already resolved by `resolve_package_main`. It is matched
    against the full path, not the file name.
    """
    if entry.kind != "blob":
        return None

    path = entry.path
    name = path.rsplit("/", 1)[-1]
    lower = name.lower()
    depth = path.count("/")

    # --- exclusions -------------------------------------------------------
    if entry.size > MAX_FILE_BYTES:  # Requirement 3 AC10
        return None
    if name in NOISE_FILES:
        return None
    if lower.endswith(_BINARY_SUFFIXES):  # Requirement 3 AC11, pre-filter only
        return None
    # Parent segments only: a *file* called "build" is fine, a "build/"
    # directory is not. Compared lowercased, so "Tests/" and "Docs/" are caught
    # the same way as their lowercase spellings.
    if any(segment.lower() in NOISE_DIRS for segment in path.split("/")[:-1]):
        return None

    # --- tiers, in Requirement 3 AC1 order --------------------------------
    if lower.startswith("readme") and depth <= 1:
        return P1_README
    if name in MANIFESTS and depth <= 1:
        return P2_MANIFEST
    if lower.startswith("docker-compose.") and depth == 0:
        return P2_MANIFEST
    if name.endswith(".csproj"):  # .NET projects nest; no depth bound
        return P2_MANIFEST
    if (name in CONFIGS or _matches_any(lower, CONFIG_GLOBS)) and depth <= 2:
        return P3_CONFIG
    if name in ENTRYPOINTS and depth <= 2:
        return P4_ENTRY
    if package_main is not None and path == package_main:
        return P4_ENTRY
    if depth <= 2 and _looks_textual(lower):
        return P5_SOURCE
    return None


def _matches_any(lower_name: str, patterns: Iterable[str]) -> bool:
    """Glob match on an already-lowercased name.

    `fnmatchcase`, not `fnmatch`: the latter folds case using the host
    platform's rules, which would make classification depend on where the code
    runs. The patterns are lowercase and the name is lowercased by the caller,
    so matching stays case-insensitive without the platform dependency.
    """
    return any(fnmatchcase(lower_name, pattern) for pattern in patterns)


def _looks_textual(lower_name: str) -> bool:
    return lower_name.endswith(_TEXT_SUFFIXES)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def select(
    entries: Iterable[TreeEntry], package_main: str | None = None
) -> list[Candidate]:
    """Pick at most `MAX_SELECTED_FILES` files from a repository tree.

    Requirement 3 AC1 (priority order), AC3 (the twelve-file cap), AC10 and
    AC11 (exclusions, via `classify`).

    Tiers 1 to 4 are drained in order and the cap can cut off mid-tier, which is
    what makes the priority order observable: a tier-5 file appears only when
    every tier-1 to tier-4 candidate has already been taken. Tier 5 then fills
    whatever is left round-robin across top-level directories.
    """
    tiers: dict[int, list[TreeEntry]] = {tier: [] for tier in TIER_ORDER}
    for entry in _unique_by_path(entries):
        tier = classify(entry, package_main)
        if tier is not None:
            tiers[tier].append(entry)

    chosen: list[Candidate] = []
    for tier in (P1_README, P2_MANIFEST, P3_CONFIG, P4_ENTRY):
        for entry in sorted(tiers[tier], key=_rank):
            if len(chosen) >= MAX_SELECTED_FILES:
                return chosen
            chosen.append(Candidate(path=entry.path, tier=tier, size=entry.size))

    if len(chosen) >= MAX_SELECTED_FILES:
        return chosen

    # Tier 5 round-robin. A "largest remaining file" fill on a monorepo spends
    # every free slot inside one package; breadth-first across top-level
    # directories gives the model a map instead of a core sample.
    buckets: dict[str, list[TreeEntry]] = {}
    for entry in sorted(tiers[P5_SOURCE], key=_rank):
        buckets.setdefault(_top_level(entry.path), []).append(entry)

    # Sorted keys fix the first-pass order; rotation preserves it afterwards.
    # Root-level files bucket under "." and sort first, which is intended: a
    # file at the root explains a project better than one three levels down.
    queues = deque(deque(buckets[key]) for key in sorted(buckets))
    while queues and len(chosen) < MAX_SELECTED_FILES:
        queue = queues.popleft()
        entry = queue.popleft()
        chosen.append(Candidate(path=entry.path, tier=P5_SOURCE, size=entry.size))
        if queue:
            queues.append(queue)
    return chosen


def _rank(entry: TreeEntry) -> tuple[int, int, str]:
    """Total order within a tier: shallower, then larger, then path.

    Shallower first because a top-level file explains a project better than a
    nested one; larger next because more bytes usually means more substance;
    the path last as the tie-break that makes the order total. Two distinct
    paths can never compare equal, so the sort is stable by construction and
    repeated runs are byte-identical (Property 4).
    """
    return (entry.path.count("/"), -entry.size, entry.path)


def _top_level(path: str) -> str:
    """The top-level directory of a path, or "." for a root-level file."""
    head, _, tail = path.partition("/")
    return head if tail else "."


def _unique_by_path(entries: Iterable[TreeEntry]) -> list[TreeEntry]:
    """Collapse duplicate paths, keeping the entry most likely to be excluded.

    GitHub never repeats a path in one tree, so against real input this is a
    no-op. It matters for generated trees: if the same path arrived twice with
    different sizes, a path-keyed exclusion guarantee ("no file over 500 KB is
    selected") would otherwise depend on which copy was classified. Keeping the
    worst copy -- a non-blob, else the largest -- makes the exclusions hold per
    path rather than per entry.
    """
    best: dict[str, TreeEntry] = {}
    for entry in entries:
        incumbent = best.get(entry.path)
        if incumbent is None or _dedupe_key(entry) < _dedupe_key(incumbent):
            best[entry.path] = entry
    return [best[path] for path in sorted(best)]


def _dedupe_key(entry: TreeEntry) -> tuple[bool, int]:
    return (entry.kind == "blob", -entry.size)


# ---------------------------------------------------------------------------
# package.json entry-point resolution
# ---------------------------------------------------------------------------


def resolve_package_main(package_json: str | None) -> str | None:
    """Resolve the entry point a `package.json` names, or `None`.

    `main` is the field that matters; `scripts.start` is consulted only when
    `main` is absent, and then only for a token that looks like a source path
    ("node src/server.js --port 3000" yields "src/server.js"). Anything
    unparseable, absolute, or path-traversal shaped yields `None`, and a value
    that names no file in the tree simply never matches in `classify`, so a bad
    field degrades to the conventional-name list instead of breaking selection.
    """
    if not package_json:
        return None
    try:
        data = json.loads(package_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    resolved = _normalize_main(data.get("main"))
    if resolved is None:
        resolved = _path_from_start_script(data.get("scripts"))
    return resolved


def _normalize_main(raw: object) -> str | None:
    """Canonicalize a manifest-declared path to a repository-relative one."""
    if not isinstance(raw, str):
        return None
    path = raw.strip().strip("\"'").replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    path = path.lstrip("/")
    if not path or path.endswith("/") or ".." in path.split("/"):
        return None
    return path


def _path_from_start_script(scripts: object) -> str | None:
    """Pull the first source-looking path out of a `scripts.start` command."""
    if not isinstance(scripts, dict):
        return None
    command = scripts.get("start")
    if not isinstance(command, str):
        return None
    for token in command.replace("&&", " ").split():
        if token.startswith("-"):
            continue
        candidate = _normalize_main(token)
        if candidate and _looks_textual(candidate.rsplit("/", 1)[-1].lower()):
            return candidate
    return None


# ---------------------------------------------------------------------------
# The hard binary check (Requirement 3 AC11)
# ---------------------------------------------------------------------------


def decode_text(raw: bytes) -> str | None:
    """Decode a fetched body as text, or return `None` to drop the file.

    This is the guarantee behind Requirement 3 AC11, not `BINARY_EXT`. An
    extension denylist cannot be complete -- a `.txt` holding a thumbnail, a
    `.json` holding a packed tensor, a UTF-16 source file -- so every body goes
    through a NUL scan and a strict UTF-8 decode before it can reach the model
    (Property 5).

    The NUL scan covers the whole body, not a leading window. The design
    sketched `raw[:4096]`, which is cheaper but leaks: NUL is valid UTF-8, so a
    file whose first NUL falls past the window decodes cleanly and reaches the
    model. Property 5 is stated over the whole body, so the scan has to be too.
    Scanning everything is also the cheaper code: `b"\\x00" in raw` is one
    `memchr` with no slice copy, where the windowed form copies the window
    first.

    `errors="strict"` is the default and is load-bearing: `"replace"` would turn
    a binary body into a page of replacement characters and spend real tokens on
    it.
    """
    if b"\x00" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
