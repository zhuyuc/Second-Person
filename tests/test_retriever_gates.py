"""检索"四门"契约测试（纯 fake 依赖，不碰真实 DB/Embedding）。

保护契约：
1. 否决门：第 2 层精筛判空 → 注入 0 条，gate=refine_empty，不再强制兜底 top-2
2. 降级链：普通问题精筛异常 → 不注入；明确回忆请求才按得分兜底
3. 扩散衰减门：1 跳关联记忆与线索余弦低于准入线 → 不注入
4. 线索门：向量路 embed 输入含上下文且当轮提问在末尾；FTS 路仅当轮提问
运行：python tests/test_retriever_gates.py（退出码 0 = 全部通过）
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.retriever import Retriever  # noqa: E402


class _FakeDB:
    def query_all(self, sql, params=()):
        return []          # FTS 路不命中，聚焦向量路契约


class _FakeVS:
    loaded = True
    dim = 4

    def __init__(self, hits, cosines=None):
        self._hits = hits              # search() 返回的 [(mid, score)]
        self._cosines = cosines or {}  # cosine_to 的 mid -> 分数

    def search(self, query_vec, top_k, threshold):
        return self._hits[:top_k]

    def cosine_to(self, mid, query_vec):
        return self._cosines.get(mid)


class _FakePalace:
    def __init__(self, rows, links=None):
        self._rows = rows
        self._links = links or {}

    def get(self, mid):
        return self._rows.get(mid)

    def get_many(self, ids):
        return {mid: self._rows[mid] for mid in ids if mid in self._rows}

    def outlinks(self, mid):
        return [{"target_id": t} for t in self._links.get(mid, [])]


class _Cfg(dict):
    def get(self, k, d=None):
        return super().get(k, d)


def _row(mid, lifecycle="active"):
    return {"id": mid, "title": f"标题{mid}", "summary": f"摘要{mid}",
            "lifecycle": lifecycle, "source_type": "memory",
            "confidence": "medium", "md_path": f"memories/x/{mid}.md"}


def _mk_retriever(tmp, vs, palace, refine_fn, embed_calls):
    async def embed_fn(texts):
        embed_calls.extend(texts)
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]
    return Retriever(_FakeDB(), vs, palace, _Cfg(), tmp,
                     embed_fn=embed_fn, llm_refine_fn=refine_fn)


async def main() -> int:
    tmp = Path(__file__).parent
    failures = []
    hits = [("mem_a", 0.61), ("mem_b", 0.58)]
    rows = {"mem_a": _row("mem_a"), "mem_b": _row("mem_b"),
            "mem_c": _row("mem_c"), "mem_d": _row("mem_d")}

    # ---- 1. 否决门：精筛判空 → 0 注入 + gate=refine_empty ----
    async def refine_empty(query, cands, session_id=None, context_text=None):
        return []
    embed_calls: list[str] = []
    r = _mk_retriever(tmp, _FakeVS(hits), _FakePalace(rows),
                      refine_empty, embed_calls)
    res = await r.retrieve("方案3，按上面的格式更新到文档", context_text="用户：备份方案讨论")
    if res.hits or res.related or res.loaded_ids:
        failures.append(f"否决门失效：判空仍注入 {len(res.loaded_ids)} 条")
    if res.diagnostics.get("gate") != "refine_empty":
        failures.append(
            f"gate 应为 refine_empty，实际 {res.diagnostics.get('gate')}")

    # ---- 2. 降级链：普通问题异常不注入，回忆请求才兜底 ----
    async def refine_boom(query, cands, session_id=None, context_text=None):
        raise RuntimeError("llm down")
    r = _mk_retriever(tmp, _FakeVS(hits), _FakePalace(rows), refine_boom, [])
    res = await r.retrieve("随便问点什么")
    if res.hits or "精筛不可用" not in res.degraded:
        failures.append(
            f"普通问题降级仍注入：hits={len(res.hits)} degraded={res.degraded!r}")
    res = await r.retrieve("你还记得我之前说过什么吗？")
    if len(res.hits) != 2:
        failures.append(f"回忆请求降级兜底失效：hits={len(res.hits)}")

    # ---- 3. 扩散衰减门：低余弦关联不注入，高余弦注入 ----
    async def refine_pick_a(query, cands, session_id=None, context_text=None):
        return ["mem_a"]
    vs = _FakeVS(hits, cosines={"mem_c": 0.30, "mem_d": 0.70})
    palace = _FakePalace(rows, links={"mem_a": ["mem_c", "mem_d"]})
    r = _mk_retriever(tmp, vs, palace, refine_pick_a, [])
    res = await r.retrieve("问题")
    rel_ids = [d["id"] for d in res.related]
    if "mem_c" in rel_ids:
        failures.append("扩散衰减门失效：低余弦(0.30)关联记忆被注入")
    if "mem_d" not in rel_ids:
        failures.append("扩散衰减门误伤：高余弦(0.70)关联记忆未注入")

    # ---- 4. 线索门：embed 输入 = 上下文+当轮（当轮在末尾）----
    if not embed_calls:
        failures.append("embed 未被调用")
    else:
        cue = embed_calls[0]
        if not cue.endswith("方案3，按上面的格式更新到文档"):
            failures.append(f"线索门失效：当轮提问未在线索末尾 {cue!r}")
        if "备份方案讨论" not in cue:
            failures.append(f"线索门失效：上下文未进入线索 {cue!r}")

    for f in failures:
        print("FAIL:", f)
    print("PASS" if not failures else f"{len(failures)} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
