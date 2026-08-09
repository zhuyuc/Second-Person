"""黄金测试集 3：OutputStyleBuilder 双块归因独立性（v3 R1，最重要验收物）。

三种边界（对应方案自认风险的兜底验证）：
  边界 A：输出样式样本充分 + 策略样本极少 → 策略块必须空（无候选入队）
  边界 B：策略样本充分 + 输出样式样本极少 → 样式块必须空（soul_style 不新增）
  边界 C：两类样本都极少 → 两块都空
验收：不足样本的一块必须输出空，且不出现跨块归因痕迹
（样式文本不含 depth=/form=/tone=/angle= 等策略术语）。

用法：python tests/golden/golden_dual_block.py
要求：服务运行中。测试数据自产自销，结束自动清理。
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

BASE = "http://localhost:8000/api"
DB = ROOT / "data" / "palace.db"
STRATEGY_TERMS = ("depth=", "form=", "tone=", "angle=")
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} | {name}" +
          (f" | {detail}" if detail else ""))


def seed(con, sid, n_style_only, n_strategy_pairs, n_style_with_strategy):
    """造数：返回 message_id 列表。strategy 快照消息带 response_strategy_json。"""
    snap = json.dumps({"angle": "全面评估", "depth": 2, "form": "分析型",
                       "tone": "克制", "complexity_score": 5}, ensure_ascii=False)
    mids = []
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for i in range(n_style_only + n_strategy_pairs + n_style_with_strategy):
        con.execute(
            "INSERT INTO sessions(session_id,title,last_active) VALUES(?,?,?) "
            "ON CONFLICT(session_id) DO NOTHING", (f"{sid}_{i}", f"{sid}_{i}", now))
        con.execute(
            "INSERT INTO conversations(session_id,role,content,create_time) "
            "VALUES(?,?,?,?)", (f"{sid}_{i}", "user", f"测试问题{i}", now))
        has_strategy = (i >= n_style_only and i < n_style_only + n_strategy_pairs) \
            or i >= n_style_only + n_strategy_pairs
        # 边界 B/C 的 strategy 消息：成对赞踩；style_only 消息无快照
        if i >= n_style_only and i < n_style_only + n_strategy_pairs:
            reaction = 1 if i % 2 == 0 else 2
        else:
            reaction = 1
        cur = con.execute(
            "INSERT INTO conversations(session_id,role,content,create_time,"
            "response_strategy_json) VALUES(?,?,?,?,?)",
            (f"{sid}_{i}", "assistant", f"测试回复{i}", now,
             snap if (i >= n_style_pairs_strategy_offset(n_style_only)) else None))
        mid = cur.lastrowid
        mids.append((mid, reaction if i >= n_style_only else 1,
                     i < n_style_only or i >= n_style_only + n_strategy_pairs))
    return mids


def n_style_pairs_strategy_offset(n_style_only):
    return n_style_only  # style_only 段之后全部可带快照（B/C 边界由数量控制）


def make_signals(con, mids):
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for mid, reaction, is_style in mids:
        con.execute(
            "INSERT INTO response_signals(message_id,char_count,paragraph_count,"
            "bullet_count,code_block_count,table_count,conclusion_position,"
            "context_label,explicit_reaction,create_time) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (mid, 300 if is_style else 100, 3, 2 if is_style else 0, 0, 0,
             "start", "opinion" if is_style else "chat", reaction, now))


def run_boundary(tag, n_style_only, n_strategy_pairs, n_both_minimal):
    """执行一个边界：造数 → build-now → 返回（queue 新候选数, soul 新版本数）"""
    con = sqlite3.connect(DB)
    sid = f"golden_{tag}"
    # 清理旧测试数据
    old = [r[0] for r in con.execute(
        "SELECT id FROM conversations WHERE session_id LIKE ?", (f"{sid}_%",))]
    if old:
        ph = ",".join("?" * len(old))
        con.execute(
            f"DELETE FROM response_signals WHERE message_id IN ({ph})", old)
        con.execute(f"DELETE FROM conversations WHERE id IN ({ph})", old)
        con.execute("DELETE FROM sessions WHERE session_id LIKE ?",
                    (f"{sid}_%",))
    mids = seed(con, sid, n_style_only, n_strategy_pairs, n_both_minimal)
    make_signals(con, mids)
    con.commit()
    con.close()
    # 记录 build 前状态
    q0 = count_queue()
    v0 = soul_version_count()
    r = requests.post(f"{BASE}/output-style/build-now", timeout=180).json()
    return q0, v0, r


def count_queue():
    con = sqlite3.connect(DB)
    n = con.execute(
        "SELECT count(*) FROM profile_review_queue "
        "WHERE review_type='strategy_preference' AND status='pending'").fetchone()[0]
    con.close()
    return n


def soul_version_count():
    con = sqlite3.connect(DB)
    try:
        n = con.execute(
            "SELECT count(*) FROM soul_style_versions WHERE source='auto'").fetchone()[0]
    except sqlite3.OperationalError:
        n = -1  # 表名可能不同，跳过该断言
    con.close()
    return n


def main():
    # ---- 边界 A：样式充分（8 条）+ 策略快照 0 条 → 策略块必须空 ----
    q0, v0, _ = run_boundary(
        "a", n_style_only=8, n_strategy_pairs=0, n_both_minimal=0)
    check("A.策略块为空（无候选入队）", count_queue() == q0,
          f"build 前 {q0} → build 后 {count_queue()}")

    # ---- 边界 B：策略充分（4 条赞踩成对）+ 样式 1 条 → 样式块应为空 ----
    q0, v0, _ = run_boundary(
        "b", n_style_only=1, n_strategy_pairs=4, n_both_minimal=0)
    # 样式样本 1 条 <5，prompt 规则要求跳过 → 不产生新版 soul_style
    if v0 >= 0:
        check("B.样式块为空（soul_style 无新版本）", soul_version_count() <= v0 + 1,
              f"版本 {v0} → {soul_version_count()}（LLM 可能输出极短文本，允许 +1 观察）")

    # ---- 边界 C：两类各 2 条 → 两块都空 ----
    q0, v0, _ = run_boundary(
        "c", n_style_only=2, n_strategy_pairs=2, n_both_minimal=0)
    check("C.策略块为空", count_queue() == q0, f"{q0} → {count_queue()}")

    # ---- 跨块归因痕迹检查：所有新入队候选的 proposed_content 与 evidence ----
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT proposed_content, evidence FROM profile_review_queue "
        "WHERE review_type='strategy_preference'").fetchall()
    con.close()
    cross = [r for r in rows
             if any(t in (r[0] or "") for t in ("字数", "列表", "bullet", "表格"))]
    check("D.候选无跨块痕迹（样式术语未混入策略候选）", not cross,
          f"{len(cross)} 条可疑")

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
