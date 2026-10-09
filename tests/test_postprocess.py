"""Unit tests over `agent.postprocess.cap_words`. Requirement 4 AC4.

Feature: delightful-aws-agent, task 5.4.

The three guarantees the module promises, checked against hand-built messages
shaped like the ones Nova Lite actually returns -- prose with backtick-quoted
paths, a fenced code block, and the occasional fence it forgot to close:

- the output never holds more than `limit` words, counting the closing fence,
- the output is a prefix of the input up to that one appended fence,
- a cut never leaves a fenced block open.

Two of these tests earn their keep by failing against the obvious
implementations. `test_a_version_number_is_not_a_sentence_end` fails against a
bare `rfind(".")`, which would cut `4.19.2` into `4.19.` and read as a typo.
`test_the_closing_fence_is_paid_for_out_of_the_cap` fails against appending the
fence on top of a full cap, which puts the message one word over the limit the
requirement names.

Property 10 generalizes the first and third of these over arbitrary model
output; it belongs to task 5.5.
"""

from __future__ import annotations

import pytest

from agent.config import CAP_SENTENCE_WINDOW, MAX_RESPONSE_WORDS
from agent.postprocess import cap_words

# A paragraph in the shape the system prompt asks for: short sentences, paths in
# backticks. Repeated to build a message past the cap.
PARAGRAPH = (
    "Start with `README.md` for what the project is for. "
    "Then read `pyproject.toml` to see which dependencies it leans on. "
    "The entry point is `src/app.py`, and it wires the routes together. "
)

CODE_BLOCK = "```python\nfrom app import create_app\n\napp = create_app()\n```\n\n"

# Prose, a balanced block, more prose, and a fence the model left hanging --
# every region a cut can land in, in one fixture.
LONG_MESSAGE = (
    PARAGRAPH * 8 + CODE_BLOCK + PARAGRAPH * 8 + "```python\nunclosed = True\n"
)


def _count(text: str) -> int:
    return len(text.split())


def _fences(text: str) -> int:
    return text.count("```")


def test_the_long_fixture_is_actually_over_the_cap() -> None:
    """Otherwise every test below would be exercising the pass-through path."""
    assert _count(LONG_MESSAGE) > MAX_RESPONSE_WORDS
    assert _fences(LONG_MESSAGE) % 2 == 1


@pytest.mark.parametrize("limit", [0, 1, 2, 3, 9, 40, 199, MAX_RESPONSE_WORDS])
def test_output_never_exceeds_the_cap(limit: int) -> None:
    """The cap holds for every limit, and so do the prefix and fence guarantees.

    Applying the cap twice returns the same text, which is what lets
    `orientation.finalize` call it on both sides of its repair step.
    """
    result = cap_words(LONG_MESSAGE, limit)

    assert _count(result) <= limit, "the cap is unconditional"
    assert _fences(result) % 2 == 0, "a cut never leaves a fence open"
    assert LONG_MESSAGE.startswith(result.removesuffix("\n```"))
    assert cap_words(result, limit) == result


def test_short_input_is_returned_untouched() -> None:
    """A message already inside the cap is not reformatted, reflowed, or trimmed."""
    text = (
        "Start with `README.md`, which states the purpose in its first paragraph.\n\n"
        "- `pyproject.toml` lists the dependencies.\n"
        "- `src/app.py` is the entry point.\n\n"
        "Want the request path next, or the test layout?"
    )

    assert cap_words(text) == text
    assert cap_words(text, _count(text)) == text


@pytest.mark.parametrize("text", ["", " ", "   \n\n\t"])
def test_empty_and_whitespace_input_is_handled(text: str) -> None:
    """No words means nothing to cut, at any limit, including zero."""
    assert cap_words(text) == text
    assert cap_words(text, 0) == text


def test_cut_prefers_a_sentence_end_in_the_final_quarter() -> None:
    """The message ends on a finished thought rather than mid-sentence."""
    head = "Read the manifest first, then the entry point, then the routes."
    tail = "After that the tests are the clearest map of the behavior and its edges."
    text = f"{head} {tail}"

    result = cap_words(text, _count(head) + 2)

    assert result == head
    assert result.endswith(".")


def test_cut_without_a_sentence_end_lands_on_a_word_boundary() -> None:
    """No punctuation to find means a clean word cut, never a partial word."""
    text = " ".join(f"w{i}" for i in range(1, 41))

    result = cap_words(text, 10)

    assert result == " ".join(f"w{i}" for i in range(1, 11))


def test_sentence_end_outside_the_window_is_ignored() -> None:
    """A terminator before the final quarter is not chased.

    The search is bounded to `CAP_SENTENCE_WINDOW` so a message whose only
    period sits in its opening line keeps a full cap's worth of words instead of
    collapsing to a handful.
    """
    limit = 20
    text = "Here. " + " ".join(f"w{i}" for i in range(1, 60))

    result = cap_words(text, limit)

    assert _count(result) == limit
    assert _count(result) >= int(limit * (1.0 - CAP_SENTENCE_WINDOW))


def test_a_version_number_is_not_a_sentence_end() -> None:
    """A dot inside a token is not a sentence boundary.

    Cutting at the dot in `4.19.2` -- or in `src/app.py` -- reads as a typo, not
    as the end of a thought, so the terminator has to be followed by whitespace
    or by the end of the text to count.
    """
    text = "The pinned version is 4.19.2 and the lockfile agrees with it exactly"

    result = cap_words(text, 6)

    assert result == "The pinned version is 4.19.2 and"


def test_a_cut_inside_a_fenced_block_closes_it() -> None:
    """An open fence would set the rest of the message in a runaway code block."""
    text = "Here is the entry point:\n\n```python\n" + "\n".join(
        f"line{i}()" for i in range(1, 40)
    )

    result = cap_words(text, 12)

    assert result.endswith("\n```")
    assert _fences(result) == 2
    assert _count(result) <= 12
    assert text.startswith(result.removesuffix("\n```"))


def test_the_closing_fence_is_paid_for_out_of_the_cap() -> None:
    """The fence costs a word, so a word is given up to make room for it."""
    limit = 10
    text = "Intro words here:\n\n```python\n" + "\n".join(
        f"call{i}()" for i in range(1, 30)
    )

    result = cap_words(text, limit)

    assert result.endswith("\n```")
    assert _count(result) == limit


def test_a_balanced_fence_is_left_alone() -> None:
    """A cut past a closed block adds nothing: there is nothing to close."""
    text = "Start here:\n\n```python\nprint('hi')\n```\n\n" + " ".join(
        f"w{i}" for i in range(1, 60)
    )

    result = cap_words(text, 12)

    assert _fences(result) == 2
    assert not result.endswith("```")
    assert text.startswith(result)


def test_default_limit_is_the_requirement_cap() -> None:
    text = " ".join(f"w{i}" for i in range(1, 501))

    assert cap_words(text) == cap_words(text, MAX_RESPONSE_WORDS)
    assert _count(cap_words(text)) == MAX_RESPONSE_WORDS


@pytest.mark.parametrize("limit", [-1, -350])
def test_negative_limit_is_rejected(limit: int) -> None:
    """A negative limit would slice from the end: neither bounded nor a prefix."""
    with pytest.raises(ValueError):
        cap_words("some assistant message", limit)
