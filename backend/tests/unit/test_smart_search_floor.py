"""The semantic arm must not answer a query it has no answer for.

A nearest-neighbour lookup always returns *something*: with no similarity
floor, searching for a word that appears nowhere in the vault still returned
the 200 least-unrelated documents, so an unrelated video showed up under a
name it has nothing to do with.  The floor is what makes "no semantic match"
an expressible outcome, and it is configurable because how close is close
enough depends on the vault.
"""

from __future__ import annotations

import pytest

from app.core import config_service
from app.jobs import embed_service

np = pytest.importorskip("numpy", reason="the vector arm needs numpy")


class _StubService(embed_service.EmbedService):
    """Real search(), stubbed model: the ranking maths is what is under test."""

    def __init__(self, scores: dict[str, float]):  # noqa: D107 - test fixture
        self._uids = list(scores)
        self._kinds = ["model"] * len(scores)
        self._scores = np.array(list(scores.values()), dtype="float32")
        self._state = embed_service.STATE_READY

    def embed_query(self, text: str):
        return np.array([1.0], dtype="float32")

    def matrix(self):
        return self._scores.reshape(-1, 1), self._uids, self._kinds


CORPUS = {"model:1": 0.82, "model:2": 0.41, "model:3": 0.22, "model:4": 0.05}


def test_weak_neighbours_are_not_returned():
    svc = _StubService(CORPUS)
    hits = svc.search("anything", min_score=0.30)
    assert [uid for uid, _kind, _score in hits] == ["model:1", "model:2"], (
        "documents below the floor were offered as semantic matches")


def test_a_query_matching_nothing_returns_nothing():
    """The 'vovka' case: no near neighbour means no semantic result at all."""
    svc = _StubService({"model:1": 0.11, "model:2": 0.04})
    assert svc.search("vovka", min_score=0.30) == []


def test_a_lower_floor_admits_more():
    svc = _StubService(CORPUS)
    assert len(svc.search("anything", min_score=0.20)) == 3
    assert len(svc.search("anything", min_score=0.01)) == 4


def test_results_stay_ranked_best_first():
    svc = _StubService(CORPUS)
    scores = [score for _uid, _kind, score in svc.search("anything", min_score=0.01)]
    assert scores == sorted(scores, reverse=True)


def test_the_configured_floor_is_what_the_vector_arm_uses(temp_vault, monkeypatch):
    from app.search import vec

    svc = _StubService(CORPUS)
    monkeypatch.setattr(vec, "get_embed_service", lambda: svc)

    config_service.set_config({"smart_search_min_score": 0.45})
    assert [uid for uid, _k, _s in vec.search("anything")] == ["model:1"]

    config_service.set_config({"smart_search_min_score": 0.20})
    assert [uid for uid, _k, _s in vec.search("anything")] == [
        "model:1", "model:2", "model:3"]


@pytest.mark.parametrize("value,expected", [(0.0, 0.05), (99.0, 0.9), (0.45, 0.45)])
def test_the_floor_is_clamped_to_a_band_that_still_means_something(
        value, expected, temp_vault):
    cfg = config_service.set_config({"smart_search_min_score": value})
    assert cfg.smart_search_min_score == expected
