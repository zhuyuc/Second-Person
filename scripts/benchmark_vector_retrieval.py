"""Measure vector retrieval across 1k/10k/100k memory records.

The script keeps the benchmark synthetic and deterministic so it can run in
CI or on a developer machine without an embedding provider.  It emits JSON
with P95 latency, Recall@K versus exact numpy search, cache memory planning,
and the backend selected by VectorStore.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

import numpy as np

# Running ``python scripts/benchmark_vector_retrieval.py`` puts ``scripts``
# first on sys.path, so explicitly retain the repository package root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.vector_store import VectorStore


def _store_for(matrix: np.ndarray, ann_min_vectors: int, cache_max_mb: float) -> VectorStore:
    store = VectorStore(
        db=None, cache_max_mb=cache_max_mb, ann_min_vectors=ann_min_vectors)
    ids = [f"mem_{index}" for index in range(len(matrix))]
    store._matrix = matrix
    store._index_to_id = ids
    store._id_to_index = {memory_id: index for index, memory_id in enumerate(ids)}
    store._dim = matrix.shape[1]
    store.loaded = True
    store._rebuild_ann()
    return store


def _exact_ids(matrix: np.ndarray, query: np.ndarray, top_k: int) -> list[str]:
    normalized = query / (np.linalg.norm(query) + 1e-8)
    scores = (matrix @ normalized) / (np.linalg.norm(matrix, axis=1) + 1e-8)
    selected = np.argpartition(-scores, top_k - 1)[:top_k]
    ordered = sorted(selected, key=lambda index: (-float(scores[index]), int(index)))
    return [f"mem_{int(index)}" for index in ordered]


def _measure(size: int, dim: int, queries: int, top_k: int, seed: int) -> dict:
    rng = np.random.default_rng(seed + size)
    matrix = rng.normal(size=(size, dim)).astype(np.float32)
    store = _store_for(matrix, ann_min_vectors=10_000, cache_max_mb=2048)
    timings: list[float] = []
    recalls: list[float] = []
    for _ in range(queries):
        query = rng.normal(size=dim).astype(np.float32)
        exact = _exact_ids(matrix, query, top_k)
        started = time.perf_counter()
        actual = store.search(query, top_k=top_k, threshold=-1.0)
        timings.append((time.perf_counter() - started) * 1000)
        actual_ids = {memory_id for memory_id, _ in actual}
        recalls.append(len(actual_ids.intersection(exact)) / top_k)
    return {
        "memories": size,
        "dimension": dim,
        "queries": queries,
        "top_k": top_k,
        "backend": "hnsw" if store.ann_enabled else "exact_numpy",
        "p95_ms": round(float(np.percentile(timings, 95)), 3),
        "recall_at_k": round(float(np.mean(recalls)), 4),
        "cache_mb": round(store.memory_mb(), 3),
        "degraded": store.cache_degraded,
        "degrade_reason": "cache_budget" if store.cache_degraded else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="1000,10000,100000")
    parser.add_argument("--dimension", type=int, default=64)
    parser.add_argument("--queries", type=int, default=30)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20260909)
    args = parser.parse_args()
    sizes = [int(value) for value in args.sizes.split(",") if value.strip()]
    if not sizes or min(sizes) <= 0 or args.dimension <= 0 or args.queries <= 0 or args.top_k <= 0:
        raise SystemExit("sizes, dimension, queries, and top-k must be positive")
    if args.top_k > min(sizes):
        raise SystemExit("top-k must not exceed the smallest benchmark size")
    print(json.dumps([
        _measure(size, args.dimension, args.queries, args.top_k, args.seed)
        for size in sizes
    ], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
