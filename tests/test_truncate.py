"""Property 6 over `agent.truncate`. Requirement 3 AC4, AC5.

Feature: delightful-aws-agent, Property 6: Truncation is bounded and honestly
labeled.

The three guarantees the rest of the pipeline leans on, asserted directly
against generated content:

- `kept` is a prefix of `content` -- the text is only shortened from the right,
  never rewritten, so a path or signature the model quotes out of a file block
  is still a quote.
- `len(kept) <= limit` unconditionally, including when the line-boundary search
  runs.
- `was_truncated` is exactly `len(kept) < len(content)` -- an `iff`, so
  "truncated" never labels a whole file and never goes missing from a cut one.

The design states Property 6 over the *final* Repo_Context, after budget
fitting shortens a block a second time. `test_property_6_survives_a_second_cut`
is the inductive step that carries the guarantees from here to there: a prefix
of a prefix is a prefix, the tighter bound wins, and the two labels or-together
into an honest one. The rendering half of AC5 -- the
`[truncated: first N of M chars]` header -- belongs to `context_builder` in
task 3.6.

Content is generated with emoji, CJK, and newline-rich shapes on purpose. The
cap is measured in characters, not bytes, so a generator that only ever emits
ASCII would never notice the difference.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agent.config import (
    MAX_FILE_CHARS,
    ORIENTATION_FILE_CHARS,
    TRUNCATE_NEWLINE_WINDOW,
)
from agent.truncate import truncate

# Glyphs that cost more than one byte each: emoji, a ZWJ, CJK, Greek, and
# typographic punctuation. One character of any of these is two to four bytes,
# which is what makes the character-versus-byte distinction observable.
GLYPHS = "🚀🙂🧪✅\u200d日本語αβγ—…"

_LINE = st.text(alphabet=st.characters(exclude_characters="\n"), max_size=24)
_GLYPH_LINE = st.text(alphabet=GLYPHS, max_size=24)


def _repeat(line: str, times: int) -> str:
    return ((line or "x") + "\n") * times


CONTENT = st.one_of(
    # Arbitrary unicode, including the empty string.
    st.text(max_size=300),
    # Newline-rich: many short lines, so the boundary search has candidates.
    st.lists(_LINE, min_size=1, max_size=80).map("\n".join),
    # Multi-byte throughout, with and without newlines.
    st.text(alphabet=GLYPHS, max_size=300),
    st.lists(_GLYPH_LINE, min_size=1, max_size=40).map("\n".join),
    # Minified: one newline near the start and nothing after it, so the
    # boundary search finds nothing inside its window.
    st.builds(
        lambda head, body: "x" * head + "\n" + "y" * body,
        st.integers(min_value=0, max_value=20),
        st.integers(min_value=200, max_value=2_000),
    ),
    # Longer than the real caps, so MAX_FILE_CHARS and ORIENTATION_FILE_CHARS
    # are exercised as truncating limits rather than as no-ops.
    st.builds(_repeat, st.one_of(_LINE, _GLYPH_LINE), st.integers(400, 900)),
)


@st.composite
def content_and_limit(draw: st.DrawFn) -> tuple[str, int]:
    """Content paired with a limit biased toward the interesting cut points.

    A uniform limit over a wide range would almost always land far from the
    content length, where truncation is a no-op. The boundary values and the
    two limits the pipeline actually passes are drawn explicitly.
    """
    content = draw(CONTENT)
    length = len(content)
    limit = draw(
        st.one_of(
            st.integers(min_value=0, max_value=length + 8),
            st.sampled_from([0, 1, max(length - 1, 0), length, length + 1]),
            st.sampled_from([ORIENTATION_FILE_CHARS, MAX_FILE_CHARS]),
        )
    )
    return content, limit


@given(content_and_limit())
def test_property_6_truncation_is_bounded_and_honestly_labeled(
    case: tuple[str, int],
) -> None:
    """Prefix, bound, and `iff` label, for any content and any limit."""
    content, limit = case

    kept, was_truncated = truncate(content, limit)

    assert content.startswith(kept), "kept must be a prefix of content"
    assert len(kept) <= limit, "the cap is unconditional"
    assert was_truncated == (len(kept) < len(content)), "the label is an iff"


@given(content_and_limit(), st.integers(min_value=0, max_value=MAX_FILE_CHARS))
def test_property_6_survives_a_second_cut(case: tuple[str, int], second: int) -> None:
    """A later shortening keeps all three guarantees against the original.

    `tokens.fit_to_budget` cuts an already-truncated block again. This is why
    asserting Property 6 on `truncate` is enough to claim it of the final
    Repo_Context.
    """
    content, limit = case

    kept, was_truncated = truncate(content, limit)
    again, again_truncated = truncate(kept, second)

    assert content.startswith(again)
    assert len(again) <= min(limit, second)
    assert again_truncated == (len(again) < len(kept))
    assert (was_truncated or again_truncated) == (len(again) < len(content))


@given(st.text(alphabet=GLYPHS, min_size=1, max_size=200))
def test_property_6_counts_characters_not_bytes(content: str) -> None:
    """The cap is in characters, and a cut never lands mid-codepoint.

    Requirement 3 AC4 says characters. Content of `n` multi-byte glyphs is
    returned whole under a limit of `n` even though its UTF-8 encoding is two
    to four times longer, and every retained character survives intact.
    """
    kept, was_truncated = truncate(content, len(content))

    assert (kept, was_truncated) == (content, False)
    assert len(kept.encode("utf-8")) > len(kept)

    half = len(content) // 2
    cut, cut_truncated = truncate(content, half)
    assert cut == content[:half]
    assert cut_truncated == (half < len(content))
    assert cut.encode("utf-8").decode("utf-8") == cut


@pytest.mark.parametrize("limit", [-1, -8_000])
def test_negative_limit_is_rejected(limit: int) -> None:
    """A negative limit would slice from the end: neither bounded nor a prefix."""
    with pytest.raises(ValueError):
        truncate("some content", limit)


def test_zero_limit_is_a_legitimate_total_cut() -> None:
    assert truncate("some content", 0) == ("", True)
    assert truncate("", 0) == ("", False)


@given(CONTENT)
def test_default_limit_is_the_requirement_cap(content: str) -> None:
    assert truncate(content) == truncate(content, MAX_FILE_CHARS)


def test_cut_prefers_the_last_newline_inside_the_window() -> None:
    """The cut lands between lines rather than mid-token."""
    content = "a" * 50 + "\n" + "b" * 44 + "\n" + "c" * 100

    kept, was_truncated = truncate(content, 100)

    assert kept == content[:95]
    assert was_truncated
    assert not kept.endswith("\n")


def test_minified_content_keeps_its_hard_cut() -> None:
    """A newline outside the window is ignored, not chased.

    The boundary search is bounded to the final `TRUNCATE_NEWLINE_WINDOW` of
    the cap so a bundle whose only newline sits near the start keeps a full
    cap's worth of characters instead of collapsing to a handful.
    """
    limit = 100
    content = "x" * 10 + "\n" + "y" * 500

    kept, was_truncated = truncate(content, limit)

    assert len(kept) == limit
    assert was_truncated
    assert len(kept) >= int(limit * (1.0 - TRUNCATE_NEWLINE_WINDOW))
