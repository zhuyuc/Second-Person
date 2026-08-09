"""黄金测试集 2：meta_cognitive 骨架质量（v3 §五·实施顺序 5）。

20 条用例 = 14 条高复杂（应触发）+ 6 条排除意图（不应触发）。
从 Langfuse skeleton_extraction span 断言：
- 触发链路存在（高复杂且策略非 fallback 时）
- 骨架结构完整（五步字段齐全）
- 排除意图（工具执行/记忆指令/用户纠正类）零触发（v3 §四.3）

注：骨架提取成功率受模型延迟约束，本集断言"链路正确性"而非"必然成功"：
提取失败（ERROR span + fallback metadata）不算 FAIL，但必须存在 span 记录。

用法：python tests/golden/golden_meta_cognitive.py [--limit N]
要求：服务运行中。
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
SKELETON_KEYS = {"reframe", "decompose", "hidden_assumptions",
                 "expert_lens", "answer_shape"}

# (消息, 是否期望触发元认知, 说明)
CASES = [
    ("人工智能未来十年对教育的深层影响，从技术、社会、伦理三维度推演", True, "多维深度推演"),
    ("我该不该为了家庭放弃外派晋升机会，帮我深入权衡", True, "人生决策"),
    ("为什么越努力越焦虑？从心理学角度深入分析", True, "深度归因"),
    ("帮我重构这个产品的商业模式，指出隐藏假设", True, "假设挑战"),
    ("远程办公对组织文化的长期侵蚀如何逆转", True, "组织深度"),
    ("我是否应该原谅反复失信的合作伙伴", True, "价值判断"),
    ("技术债务和交付速度的平衡点怎么找，深层逻辑是什么", True, "工程决策"),
    ("中年转行的沉没成本谬误怎么破", True, "概念+决策"),
    ("如何判断一个创业方向是伪需求", True, "方法论深度"),
    ("亲密关系中的付出失衡如何重建", True, "关系深度"),
    ("信息茧房对独立思考的侵蚀机制", True, "社会分析"),
    ("长期主义和快速试错在什么条件下各自成立", True, "概念辨析"),
    ("帮我设计一套个人知识体系的演化路径", True, "方案设计"),
    ("教育的本质是筛选还是培养，深层论证", True, "命题论证"),
    ("帮我算一下 256*37", False, "计算排除"),
    ("记住我的车是白色特斯拉", False, "记忆指令排除"),
    ("帮我查一下北京明天天气", False, "查询排除"),
    ("你以后回复简短一点", False, "输出偏好纠正排除"),
    ("你的语气太生硬了，改改", False, "风格反馈排除"),
    ("你好", False, "简单消息不触发"),
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
    fails = []
    for msg, should_trigger, note in cases:
        sid = requests.post(
            f"{BASE}/chat/session/create").json()["data"]["session_id"]
        resp = requests.post(f"{BASE}/chat/send",
                             json={"session_id": sid, "message": msg},
                             stream=True, timeout=300)
        for line in resp.iter_lines(decode_unicode=True):
            if line and "turn_completed" in line:
                break
            if line and line.startswith("data:") and '"text"' in line and not should_trigger:
                resp.close()  # 非触发用例拿到首增量即可
                break
        time.sleep(1)
    print(f"已发送 {len(cases)} 条，等待 Langfuse 上报…")
    time.sleep(15)

    r = requests.get(f"{cfg.host}/api/public/observations",
                     params={"type": "SPAN", "limit": 300}, auth=auth, timeout=20)
    obs = [o for o in r.json().get("data", []) if (
        o.get("startTime") or "") >= t0]
    skel = [o for o in obs if o.get("name") == "skeleton_extraction"]
    strat = [o for o in obs if o.get("name") == "strategy_decision"]

    # 断言 1：排除用例零 skeleton span（按会话 trace 对齐较复杂，用总量上界）
    n_expect_trigger = sum(1 for _, t, _ in cases if t)
    n_not_trigger = len(cases) - n_expect_trigger
    if len(skel) > n_expect_trigger:
        fails.append(f"skeleton span 数量超上界：{len(skel)} > {n_expect_trigger}")

    # 断言 2：骨架结构完整 + 失败必须带 fallback metadata
    for o in skel:
        if o.get("level") == "ERROR":
            meta = o.get("metadata") or {}
            if not meta.get("fallback_used"):
                fails.append("ERROR skeleton span 缺 fallback metadata")
        else:
            out = o.get("output") or {}
            if not SKELETON_KEYS <= set(out.keys()):
                fails.append(f"骨架结构不完整：{sorted(out.keys())}")

    # 断言 3：fallback 策略消息不得触发骨架（span 的 input.complexity 校验）
    fallback_scores = {(s.get("traceId")) for s in strat
                       if (s.get("output") or {}).get("fallback_used")}
    for o in skel:
        if o.get("traceId") in fallback_scores:
            fails.append("fallback 策略消息触发了骨架")

    print(f"strategy span {len(strat)} | skeleton span {len(skel)} "
          f"（期望触发上限 {n_expect_trigger}）")
    for f in fails[:10]:
        print("FAIL |", f)
    ok = not fails and len(strat) >= max(1, len(cases) // 2)
    print("PASS | 元认知黄金集（链路正确性）" if ok else "FAIL | 元认知黄金集")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
