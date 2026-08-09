"""追问弱信号窗口 P75 校准（意图理解与响应质量优化方案 v3 R3）。

统计"AI 回复 → 用户下一条消息"的间隔分布，输出 P50/P75/P90；
建议窗口 = P75 钳制到 PARAM_SCHEMA 值域 [10, 600] 秒。
上线后第一个完整周运行一次，将建议值写入设置页"追问弱信号窗口"。

用法：python scripts/calibrate_followup_window.py [--apply]
  --apply 同时写入 config.yaml（strategy_followup_window_seconds）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sqlite3  # noqa: E402
from datetime import datetime  # noqa: E402


def main() -> None:
    con = sqlite3.connect(ROOT / "data" / "palace.db")
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT c.session_id, c.create_time AS reply_time,
               (SELECT MIN(c2.create_time) FROM conversations c2
                WHERE c2.session_id = c.session_id AND c2.id > c.id
                  AND c2.role = 'user') AS next_user_time
        FROM conversations c
        WHERE c.role = 'assistant' AND c.message_type = 'normal'
    """).fetchall()
    gaps = []
    for r in rows:
        if not r["next_user_time"]:
            continue
        try:
            gap = (datetime.fromisoformat(r["next_user_time"])
                   - datetime.fromisoformat(r["reply_time"])).total_seconds()
        except ValueError:
            continue
        if 0 < gap < 3600:  # 1 小时以上的间隔不属于追问语义
            gaps.append(gap)
    if len(gaps) < 20:
        print(f"样本不足（{len(gaps)} 条 < 20），暂不校准")
        return
    gaps.sort()

    def pct(p: float) -> float:
        return gaps[min(len(gaps) - 1, int(len(gaps) * p))]

    p50, p75, p90 = pct(0.5), pct(0.75), pct(0.9)
    suggested = max(10, min(600, int(p75)))
    print(f"样本 {len(gaps)} 条 | P50={p50:.0f}s P75={p75:.0f}s P90={p90:.0f}s")
    print(f"建议窗口值：{suggested}s（P75 钳制到 [10, 600]）")
    if "--apply" in sys.argv:
        from infrastructure.config_manager import ConfigManager
        cm = ConfigManager(ROOT / "data" / "config.yaml")
        cm.load()
        cm.update_params({"strategy_followup_window_seconds": suggested})
        print(f"已写入 config.yaml：strategy_followup_window_seconds={suggested}")


if __name__ == "__main__":
    main()
