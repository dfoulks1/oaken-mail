# SPDX-License-Identifier: Apache-2.0
"""The chunker: sizes, overlap, boundaries, hashes and termination."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from oaken.mail.chunk import Chunk, chunk_text, content_hash
from oaken.mail.tokens import EstimateTokenizer

TOK = EstimateTokenizer()


def sentence(i: int) -> str:
    return f"Sentence number {i} says that the plan is on track."


def para(n: int, start: int = 0) -> str:
    return " ".join(sentence(start + i) for i in range(n))


def test_empty_and_blank_text_give_no_chunks() -> None:
    assert chunk_text("") == []
    assert chunk_text("  \n\n \t ") == []


def test_short_text_is_one_chunk() -> None:
    [c] = chunk_text("Just a short note.")
    assert c == Chunk(0, "Just a short note.", TOK.count("Just a short note."), c.content_hash)
    assert c.content_hash == content_hash("Just a short note.")


def test_paragraphs_stay_whole_when_they_fit() -> None:
    text = "\n\n".join(para(2, i * 2) for i in range(6))
    chunks = chunk_text(text, target=60, overlap=0)
    assert len(chunks) > 1
    for c in chunks:
        assert c.tokens <= 60
        # every chunk is built from whole paragraphs: it never starts or ends mid-sentence
        assert c.text.startswith("Sentence")
        assert c.text.endswith("track.")
    assert "\n\n".join(c.text for c in chunks) == text


def test_a_long_paragraph_is_cut_on_sentences() -> None:
    chunks = chunk_text(para(30), target=50, overlap=0)
    assert len(chunks) > 3
    for c in chunks:
        assert c.tokens <= 50
        assert c.text.endswith("track.")
        assert c.text.startswith("Sentence")


def test_overlap_repeats_whole_trailing_sentences() -> None:
    chunks = chunk_text(para(30), target=50, overlap=20)
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        last = prev.text.split(". ")[-1]
        assert nxt.text.startswith(sentence(0)[:8])  # starts at a sentence
        assert last in nxt.text  # the previous chunk's last sentence reappears


def test_overlap_that_does_not_fit_is_dropped() -> None:
    big = " ".join(["word"] * 40) + "."  # 40+ tokens, as one sentence
    chunks = chunk_text(f"{big} {big} {big}", target=60, overlap=10)
    assert all(c.tokens <= 60 for c in chunks)
    assert len(chunks) == 3  # one sentence each; none small enough to repeat


def test_a_sentence_longer_than_a_chunk_is_cut_on_words() -> None:
    text = " ".join(f"w{i}" for i in range(500))
    chunks = chunk_text(text, target=40, overlap=0)
    assert all(c.tokens <= 40 for c in chunks)
    assert " ".join(c.text for c in chunks) == text


def test_one_unbroken_string_cannot_make_a_huge_chunk() -> None:
    chunks = chunk_text("x" * 5000, target=64, overlap=0)
    assert len(chunks) > 1
    assert all(c.tokens <= 64 for c in chunks)
    assert "".join(c.text for c in chunks) == "x" * 5000


def test_chunks_are_numbered_and_hashed() -> None:
    chunks = chunk_text(para(30), target=50, overlap=8)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert all(c.content_hash == content_hash(c.text) for c in chunks)
    assert len({c.content_hash for c in chunks}) == len(chunks)


def test_same_text_same_chunks() -> None:
    assert chunk_text(para(40), target=64, overlap=8) == chunk_text(para(40), target=64, overlap=8)


def test_an_unchanged_prefix_keeps_its_chunk_hashes() -> None:
    a = chunk_text(para(30), target=50, overlap=0)
    b = chunk_text(para(30) + " A new final sentence appears here.", target=50, overlap=0)
    assert [c.content_hash for c in a[:-1]] == [c.content_hash for c in b[: len(a) - 1]]


def test_a_custom_tokenizer_plugs_in() -> None:
    class PerChar:
        name = "chars"
        exact = True

        def count(self, text: str) -> int:
            return len(text)

    chunks = chunk_text("ab cd ef gh ij kl", target=6, overlap=0, tokenizer=PerChar())
    assert all(c.tokens <= 6 for c in chunks)
    assert " ".join(c.text for c in chunks) == "ab cd ef gh ij kl"


@pytest.mark.parametrize(("target", "overlap"), [(0, 0), (10, 10), (10, 11), (5, -1)])
def test_bad_sizes_are_refused(target: int, overlap: int) -> None:
    with pytest.raises(ValueError, match="target"):
        chunk_text("text", target=target, overlap=overlap)


@given(
    st.text(alphabet=st.characters(exclude_categories=["Cs"]), max_size=1500),
    st.integers(8, 80),
    st.integers(0, 30),
)
def test_any_text_terminates_and_respects_the_target(text: str, target: int, overlap: int) -> None:
    overlap = min(overlap, target - 1)
    chunks = chunk_text(text, target=target, overlap=overlap)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    for c in chunks:
        assert c.text == c.text.strip()
        assert c.tokens <= target or "\n" not in c.text and len(c.text.split()) <= 1
