# SPDX-License-Identifier: Apache-2.0
import math

from oaken.evals.retrieval.metrics import mean, ndcg_at, recall_at, reciprocal_rank


def test_recall_and_rank_metrics() -> None:
    ranked = ["a", "b", "c", "d"]
    assert recall_at(ranked, {"c"}, 2) == 0.0 and recall_at(ranked, {"c"}, 3) == 1.0
    assert recall_at(ranked, {"a", "z"}, 4) == 0.5
    assert reciprocal_rank(ranked, {"c"}) == 1 / 3 and reciprocal_rank(ranked, {"z"}) == 0.0


def test_ndcg_rewards_the_hit_being_higher() -> None:
    assert ndcg_at(["a", "b"], {"a"}) == 1.0
    assert ndcg_at(["b", "a"], {"a"}) == 1 / math.log2(3)
    assert ndcg_at(["b", "c"], {"a"}) == 0.0
    assert ndcg_at([f"x{i}" for i in range(10)] + ["a"], {"a"}, 10) == 0.0  # past the cutoff


def test_mean_of_nothing_is_zero() -> None:
    assert mean([]) == 0.0 and mean([1.0, 3.0]) == 2.0
