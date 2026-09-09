"""VectorStore retrieval and cache-budget regression tests."""
from __future__ import annotations

import numpy as np

from memory.vector_store import VectorStore


def test_search_matches_exact_top_k_with_threshold_and_tombstones():
    store = VectorStore(db=None)
    rng = np.random.default_rng(7)
    matrix = rng.normal(size=(64, 8)).astype(np.float32)
    ids = [f"memory-{i}" for i in range(len(matrix))]
    store._matrix = matrix
    store._index_to_id = ids.copy()
    store._id_to_index = {memory_id: i for i, memory_id in enumerate(ids)}
    store._index_to_id[5] = None
    store._id_to_index.pop("memory-5")
    store._dim = matrix.shape[1]

    query = rng.normal(size=matrix.shape[1]).astype(np.float32)
    threshold = -0.15
    hits = store.search(query, top_k=7, threshold=threshold)

    normalized_query = query / (np.linalg.norm(query) + 1e-8)
    scores = (matrix @ normalized_query) / (np.linalg.norm(matrix, axis=1) + 1e-8)
    expected_indices = [
        i for i, score in enumerate(scores)
        if ids[i] != "memory-5" and score >= threshold
    ]
    expected_indices.sort(key=lambda i: (-float(scores[i]), i))
    expected_indices = expected_indices[:7]

    assert [memory_id for memory_id, _ in hits] == [ids[i] for i in expected_indices]
    np.testing.assert_allclose([score for _, score in hits],
                               [scores[i] for i in expected_indices])


def test_search_snapshot_does_not_copy_or_full_sort_the_vector_matrix(monkeypatch):
    class NoCopyArray(np.ndarray):
        def copy(self, *args, **kwargs):  # pragma: no cover - old path must fail here
            raise AssertionError("search must not copy the full vector matrix")

    def fail_full_sort(*_args, **_kwargs):
        raise AssertionError("search must use partial top-k selection, not argsort")

    store = VectorStore(db=None)
    store.add("a", [1.0, 0.0])
    store.add("b", [0.0, 1.0])
    store._matrix = store._matrix.view(NoCopyArray)
    monkeypatch.setattr(np, "argsort", fail_full_sort)

    assert store.search([1.0, 0.0], top_k=1, threshold=0.0) == [("a", 1.0)]


def test_cache_budget_disables_partial_vector_results_and_falls_back_cleanly():
    # 1 KiB budget: two 128-d float32 vectors fit exactly; the third must not
    # leave a stale two-vector prefix available for semantic retrieval.
    store = VectorStore(db=None, cache_max_mb=1 / 1024)
    vector = np.ones(128, dtype=np.float32)
    store.add("a", vector)
    store.add("b", vector)
    assert store.search(vector, top_k=2, threshold=-1.0)

    store.add("c", vector)

    assert store.cache_degraded is True
    assert store.memory_mb() == 0.0
    assert store.search(vector, top_k=3, threshold=-1.0) == []


def test_load_skips_blob_materialization_when_cache_budget_is_exceeded():
    class StatsOnlyDB:
        def __init__(self):
            self.read_vectors = False

        def query_one(self, _sql):
            return {"n": 100, "components": 100 * 1024,
                    "min_dim": 1024, "max_dim": 1024}

        def query_all(self, _sql):
            self.read_vectors = True
            raise AssertionError("over-budget load must not fetch vector BLOBs")

    db = StatsOnlyDB()
    store = VectorStore(db, cache_max_mb=0.25)

    assert store.load() == 0
    assert store.loaded is True
    assert store.cache_degraded is True
    assert store.dim == 1024
    assert db.read_vectors is False


def test_hnsw_ann_activates_for_medium_cache_and_tracks_mutations():
    store = VectorStore(db=None, ann_min_vectors=4)
    vectors = {
        "east": [1.0, 0.0, 0.0, 0.0],
        "north": [0.0, 1.0, 0.0, 0.0],
        "west": [-1.0, 0.0, 0.0, 0.0],
        "south": [0.0, -1.0, 0.0, 0.0],
        "north-east": [0.8, 0.6, 0.0, 0.0],
    }
    for memory_id, vector in vectors.items():
        store.add(memory_id, vector)

    assert store.ann_enabled is True
    assert store.search([1.0, 0.0, 0.0, 0.0], top_k=1, threshold=0.5) == [
        ("east", 1.0)]

    store.remove("east")
    hits = store.search([1.0, 0.0, 0.0, 0.0], top_k=3, threshold=0.5)
    assert "east" not in [memory_id for memory_id, _ in hits]
    assert hits[0][0] == "north-east"


def test_ann_overhead_is_admitted_with_the_vector_cache_budget():
    # Four 48-d vectors occupy 768B; adding the estimated HNSW metadata must
    # also participate in the 1KiB cache budget decision.
    store = VectorStore(db=None, cache_max_mb=1 / 1024, ann_min_vectors=4)
    vector = np.ones(48, dtype=np.float32)
    for index in range(4):
        store.add(f"memory-{index}", vector)

    assert store.cache_degraded is True
    assert store.ann_enabled is False
    assert store.search(vector, top_k=3, threshold=-1.0) == []
