# SPDX-License-Identifier: Apache-2.0
from oaken.evals.retrieval.corpus import KINDS, generate


def test_the_corpus_is_the_same_for_the_same_seed_and_differs_for_another() -> None:
    a, b, c = generate(120, seed=3), generate(120, seed=3), generate(120, seed=4)
    assert [m.raw for m in a.messages] == [m.raw for m in b.messages]
    assert [q.text for q in a.questions] == [q.text for q in b.questions]
    assert [m.raw for m in a.messages] != [m.raw for m in c.messages]


def test_every_kind_of_question_is_present_and_answerable_ones_point_at_a_real_message() -> None:
    corpus = generate(200, seed=1, per_kind=5)
    assert {q.kind for q in corpus.questions} == set(KINDS)
    have = {m.provider_id for m in corpus.messages}
    for q in corpus.questions:
        assert (set(q.relevant) <= have) and (bool(q.relevant) == (q.kind != "unanswerable"))


def test_the_corpus_has_no_real_addresses() -> None:
    for m in generate(60, seed=2).messages:
        assert b"@example." in m.raw and b"@gmail" not in m.raw
