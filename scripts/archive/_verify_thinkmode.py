"""验证：auto 自主路由与 quick/deep 显式覆盖的 SSE 模式事件。"""
import json

import requests

BASE = "http://localhost:8000/api"


def collect_events(mode):
    r = requests.post(f"{BASE}/chat/send",
                      json={"message": "你好", "think_mode": mode},
                      stream=True, timeout=180)
    ev, texts, decisions = "", [], []
    for line in r.iter_lines(decode_unicode=True):
        if not line:
            continue
        if line.startswith("event:"):
            ev = line[6:].strip()
            if ev in ("turn_completed", "error"):
                break
        elif line.startswith("data:") and ev == "thinking_delta":
            try:
                texts.append(json.loads(line[5:].strip()).get("text", ""))
            except json.JSONDecodeError:
                pass
        elif line.startswith("data:") and ev == "mode_decision":
            try:
                decisions.append(json.loads(line[5:].strip()))
            except json.JSONDecodeError:
                pass
    return "".join(texts), decisions


for mode in ("auto", "quick", "deep"):
    t, decisions = collect_events(mode)
    expect = "用户指定快速回复" if mode == "quick" else (
        "用户指定深度思考" if mode == "deep" else "")
    ok = bool(decisions) and (not expect or expect in t)
    print(
        f"{'PASS' if ok else 'FAIL'} | think_mode={mode} | "
        f"effective={decisions[0].get('effective_mode') if decisions else 'none'} | "
        f"首行：{t.split(chr(10))[0] if t else ''}")
