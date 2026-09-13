"""优化项契约测试（纯 fake 依赖，不碰真实 DB/网络）。

覆盖本轮架构优化的关键契约：
1. http_client.timeout_for：分级超时按 profile 返回，未知回退 default
2. Retriever._degrade_pick：精筛不可用时按相对得分过滤弱尾，至少保留 1 条
3. Palace.get_many：批量取行、去重、空输入返回空、占位符个数与 id 一致
4. Database.wal_checkpoint：非法 mode 拒绝（白名单校验）

运行：python tests/test_optimizations.py（退出码 0 = 全部通过）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

failures: list[str] = []


def check(cond: bool, msg: str, bucket: list[str] | None = None) -> None:
    if not cond:
        (bucket if bucket is not None else failures).append(msg)


# ---- 1. http_client.timeout_for ----
def test_timeout_for() -> None:
    from infrastructure.http_client import timeout_for, TIMEOUTS
    local: list[str] = []
    for prof in ("default", "embedding", "stream", "quick", "web"):
        check(timeout_for(prof)
              is TIMEOUTS[prof], f"timeout_for({prof}) 应返回对应超时", local)
    check(timeout_for("nonexistent") is TIMEOUTS["default"],
          "未知 profile 应回退 default", local)
    check(TIMEOUTS["embedding"].read < TIMEOUTS["default"].read,
          "embedding 读超时应短于 default", local)
    assert not local, local


# ---- 2. Retriever._degrade_pick ----
def test_degrade_pick() -> None:
    from memory.retriever import Retriever, Candidate

    class _Cfg(dict):
        def get(self, k, d=None):
            return super().get(k, d)

    r = Retriever(None, None, None, _Cfg(), Path("."))
    local: list[str] = []

    def _cand(mid, score):
        c = Candidate(mid, "", "", "active")
        c.final_score = score
        return c

    cands = [_cand("a", 1.0), _cand("b", 0.6), _cand("c", 0.2)]
    picked = r._degrade_pick(cands)
    check(picked == ["a", "b"], f"应过滤弱尾 c，实际 {picked}", local)
    check(r._degrade_pick([]) == [], "空候选应返回空", local)
    only = r._degrade_pick([_cand("x", 0.0), _cand("y", 0.0)])
    check(only == ["x", "y"], f"全 0 分应保留候选，实际 {only}", local)
    assert not local, local


# ---- 3. Palace.get_many ----
def test_get_many() -> None:
    from memory.palace import Palace

    class _FakeDB:
        def __init__(self):
            self.last_sql = None
            self.last_params = None

        def query_all(self, sql, params=()):
            self.last_sql, self.last_params = sql, tuple(params)
            store = {"mem_a": {"id": "mem_a"}, "mem_b": {"id": "mem_b"}}
            return [store[p] for p in params if p in store]

    db = _FakeDB()
    p = Palace(db)
    local: list[str] = []
    check(p.get_many([]) == {}, "空 ids 应返回空且不查库", local)
    check(db.last_sql is None, "空 ids 不应触发查询", local)
    res = p.get_many(["mem_a", "mem_a", "mem_b", "mem_x"])
    check(set(res.keys()) == {"mem_a", "mem_b"},
          f"应命中 a/b，实际 {set(res.keys())}", local)
    check(db.last_params == ("mem_a", "mem_b", "mem_x"),
          f"去重后参数应为 3 个，实际 {db.last_params}", local)
    check(db.last_sql.count("?") == 3, "占位符个数应与去重后 id 数一致", local)
    assert not local, local


# ---- 4. Database.wal_checkpoint 白名单 ----
def test_wal_checkpoint_whitelist() -> None:
    import tempfile
    from infrastructure.db import Database
    local: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "t.db")
        try:
            raised = False
            try:
                db.wal_checkpoint("DROP TABLE x")
            except ValueError:
                raised = True
            check(raised, "非法 wal_checkpoint 模式应抛 ValueError", local)
            db.wal_checkpoint("truncate")
        finally:
            db.close()
    assert not local, local


def main() -> int:
    for fn in (test_timeout_for, test_degrade_pick, test_get_many,
               test_wal_checkpoint_whitelist):
        try:
            fn()
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures.append(f"{fn.__name__} 抛异常：{e}")
    for f in failures:
        print("FAIL:", f)
    print("PASS" if not failures else f"{len(failures)} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
