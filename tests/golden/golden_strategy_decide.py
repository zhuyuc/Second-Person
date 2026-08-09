"""黄金测试集 1：strategy_decide 决策质量（v3 §五·实施顺序 5）。

20 条代表性消息经真实链路决策，从 Langfuse strategy_decision span 断言：
- 枚举合法性（form/tone）、值域（depth 0-3 / complexity 0-10）
- narrative 非空且不含字段名（v3 R4）
- 简单消息短路/复杂消息 LLM 决策的分布不退化（v3 R2）

用法：python tests/golden/golden_strategy_decide.py [--limit N]
要求：服务运行中（python start.py）。
"""
import sys
import time
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from infrastructure.config_manager import ConfigManager  # noqa: E402
from observability_langfuse.config import LangfuseConfig  # noqa: E402

BASE = "http://localhost:8000/api"
FORM_ENUM = {"结论型", "分析型", "确认型", "对话型", "共情型"}
TONE_ENUM = {"严肃", "轻松", "共情", "克制", "激励", "中性"}
FIELD_WORDS = ("depth", "form=", "tone=", "complexity_score")

# (消息, 期望 complexity 上界或 None, 说明)
CASES = [
    ("你好呀", 3, "寒暄短路"),
    ("早上好", 3, "寒暄短路"),
    ("1+1等于", 3, "极简事实"),
    ("今天星期几", 4, "简单事实"),
    ("帮我查一下最近的AI新闻", 5, "外部查询"),
    ("我数据库里有多少条记忆", 4, "记忆查询"),
    ("帮我算一下 137*29", 3, "计算工具"),
    ("记住我喜欢喝咖啡", 3, "记忆指令"),
    ("你觉得我该不该换工作？", 9, "价值判断"),
    ("分析一下我最近效率低的原因", 9, "分析归因"),
    ("帮我设计一个团队激励方案", 9, "方案设计"),
    ("这两个技术选型哪个更适合我们团队", 9, "决策求助"),
    ("我很难过，感觉什么都做不好", 8, "情绪倾诉"),
    ("上次说的事情怎么样了", 8, "悬空指代"),
    ("你怎么总是答非所问", 6, "用户纠正"),
    ("给我讲讲量子计算的基本原理", 7, "概念深度"),
    ("写一封礼貌的拒绝邮件", 6, "写作任务"),
    ("帮我整理一下这段话的逻辑", 6, "文本加工"),
    ("创业公司怎么活过第一年", 9, "深度咨询"),
    ("嗯", 2, "极简确认"),
]


def main():
    limit = int(sys.argv[sys.argv.index("--limit") + 1]
                ) if "--limit" in sys.argv else len(CASES)
    cases = CASES[:limit]
    cm = ConfigManager(ROOT / "data" / "config.yaml")
    cm.load()
    cfg = LangfuseConfig.from_sources(cm.get)
    auth = HTTPBasicAuth(cfg.public_key, cfg.secret_key)

    t0 = time.strftime("%Y-%m-%dT%H:%M:%S")
    sid = requests.post(
        f"{BASE}/chat/session/create").json()["data"]["session_id"]
    for msg, _, note in cases:
        resp = requests.post(f"{BASE}/chat/send",
                             json={"session_id": sid, "message": msg},
                             stream=True, timeout=120)
        for line in resp.iter_lines(decode_unicode=True):
            if line and line.startswith("data:") and '"text"' in line:
                resp.close()
                break
        time.sleep(0.3)
    print(f"已发送 {len(cases)} 条，等待 Langfuse 上报…")
    time.sleep(15)

    r = requests.get(f"{cfg.host}/api/public/observations",
                     params={"type": "SPAN", "limit": 200}, auth=auth, timeout=20)
    spans = [o for o in r.json().get("data", [])
             if o.get("name") == "strategy_decision"
             and (o.get("startTime") or "") >= t0]
    fails = []
    low = high = 0
    for s in spans:
        out = s.get("output") or {}
        form, tone = out.get("form"), out.get("tone")
        depth, score = out.get("depth"), out.get("complexity_score")
        narr = str(out.get("strategy_narrative") or "")
        if form not in FORM_ENUM:
            fails.append(f"form 非法: {form}")
        if tone not in TONE_ENUM:
            fails.append(f"tone 非法: {tone}")
        if not (isinstance(depth, int) and 0 <= depth <= 3):
            fails.append(f"depth 越界: {depth}")
        if not (isinstance(score, int) and 0 <= score <= 10):
            fails.append(f"complexity 越界: {score}")
        if not narr or any(w in narr for w in FIELD_WORDS):
            fails.append(f"narrative 不合规: {narr[:40]}")
        if isinstance(score, int):
            low += score < 3
            high += score >= 7
    n = len(spans)
    print(f"采集 strategy_decision span {n}/{len(cases)} 条")
    if n:
        print(f"低复杂占比 {low/n:.0%} | 高复杂占比 {high/n:.0%}")
        if n >= 10 and (low / n >= 0.9 or high / n >= 0.5):
            fails.append("分布退化（R2 验收不通过）")
    for f in fails[:10]:
        print("FAIL |", f)
    ok = n >= max(1, len(cases) // 2) and not fails
    print("PASS | 策略决策黄金集" if ok else "FAIL | 策略决策黄金集")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
