"""验证：思考模式用户指定路由（quick/deep → 思考面板文案标注来源）。"""
import json

import requests

BASE = "http://localhost:8000/api"


def collect_thinking(mode):
    r = requests.post(f"{BASE}/chat/send",
                      json={"message": "你好", "think_mode": mode},
                      stream=True, timeout=180)
    ev, texts = "", []
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
    return "".join(texts)


for mode in ("quick", "deep"):
    t = collect_thinking(mode)
    expect = "用户指定快速回复" if mode == "quick" else "用户指定深度思考"
    ok = expect in t
    print(
        f"{'PASS' if ok else 'FAIL'} | think_mode={mode} | 首行：{t.split(chr(10))[0]}")
