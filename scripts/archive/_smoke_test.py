"""冒烟测试：覆盖本轮改动（渠道分组/中断补救/纯导出）+ 核心对话链路。"""
import json
import time

import requests

BASE = "http://localhost:8000/api"
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""))


def sse_send(payload, on_event=None, stop_after_chars=None, timeout=300):
    """发送 /chat/send 并收流。返回 (content, thinking, completed)。"""
    content, thinking, evt, completed = [], [], "", False
    resp = requests.post(f"{BASE}/chat/send", json=payload,
                         stream=True, timeout=timeout)
    for line in resp.iter_lines(decode_unicode=True):
        if line and line.startswith("event:"):
            evt = line[6:].strip()
            if evt == "turn_completed":
                completed = True
        elif line and line.startswith("data:"):
            try:
                d = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if evt == "content_delta":
                content.append(d.get("text", ""))
            elif evt == "thinking_delta":
                thinking.append(d.get("text", ""))
            if on_event:
                on_event(evt, d)
        if stop_after_chars and sum(len(x) for x in content) > stop_after_chars:
            resp.close()
            return "".join(content), "".join(thinking), False
        if completed:
            break
    return "".join(content), "".join(thinking), completed


test_sids = []

# ---- 1. 健康检查 ----
h = requests.get(f"{BASE}/health").json()
check("1.健康检查", h["data"]["status"] == "healthy",
      ",".join(k for k, v in h["data"]["checks"].items() if v != "ok") or "全部ok")

# ---- 2. 会话列表含 channel 字段 & 飞书会话归属 ----
sl = requests.get(f"{BASE}/chat/sessions").json()["data"]["list"]
check("2a.列表含channel字段", all("channel" in s for s in sl))
feishu = [s for s in sl if s.get("channel") == "feishu"]
check("2b.飞书渠道会话存在", len(feishu) >= 1,
      f"{len(feishu)} 条: {','.join(s['session_id'] for s in feishu)}")

# ---- 3. 基本对话（创建/发送/落库/标题） ----
sid = requests.post(f"{BASE}/chat/session/create").json()["data"]["session_id"]
test_sids.append(sid)
c, t, done = sse_send({"session_id": sid, "message": "你好，简单回复一句即可"})
check("3a.SSE完整走完", done and len(c) > 0, f"回复 {len(c)} 字符")
msgs = requests.get(f"{BASE}/chat/messages", params={"session_id": sid}).json()["data"]
roles = [m["role"] for m in msgs]
check("3b.消息落库(user+assistant)", "user" in roles and "assistant" in roles)
new_web = [s for s in requests.get(f"{BASE}/chat/sessions").json()["data"]["list"]
           if s["session_id"] == sid]
check("3c.网页端会话channel为空", bool(new_web) and not new_web[0]["channel"])

# ---- 4. 置顶 / 重命名 ----
requests.post(f"{BASE}/chat/session/pin", json={"session_id": sid, "pinned": True})
requests.post(f"{BASE}/chat/session/rename",
              json={"session_id": sid, "title": "冒烟测试会话"})
s4 = [s for s in requests.get(f"{BASE}/chat/sessions").json()["data"]["list"]
      if s["session_id"] == sid][0]
check("4.置顶+重命名", s4["pinned"] and s4["title"] == "冒烟测试会话")

# ---- 5. 中断补救：流式中途断开 → 回复最终必须落库
# 合法结果有两种（取决于服务端检测到断连的时机）：
#   a) 取消及时触发 → 部分回复落库带“本回复未完成”标注
#   b) 检测较慢、流水线跑完 → 完整回复正常落库
sid5 = requests.post(f"{BASE}/chat/session/create").json()["data"]["session_id"]
test_sids.append(sid5)
c5, _, done5 = sse_send(
    {"session_id": sid5, "message": "请详细介绍宋词的发展历程，分北宋南宋展开，写长一些"},
    stop_after_chars=150)
a5, mode5 = [], ""
for _ in range(24):  # 最多等 120 秒：覆盖未取消时流水线跑完的情况
    time.sleep(5)
    msgs5 = requests.get(f"{BASE}/chat/messages",
                         params={"session_id": sid5}).json()["data"]
    a5 = [m for m in msgs5 if m["role"] == "assistant"]
    if a5:
        mode5 = ("中断补救(部分+标注)" if "本回复未完成" in a5[-1]["content"]
                 else "未取消跑完(完整落库)")
        break
check("5.断连后回复不丢失", bool(a5),
      f"{mode5}，落库 {len(a5[-1]['content']) if a5 else 0} 字符")

# ---- 6. 纯导出模式：只回卡片不展示正文 ----
sid6 = requests.post(f"{BASE}/chat/session/create").json()["data"]["session_id"]
test_sids.append(sid6)
c6, t6, done6 = sse_send({
    "session_id": sid6,
    "message": "帮我写一份详细的《新员工入职指引》文档，含入职流程、账号开通、"
               "制度须知等章节，直接导出为word给我，不用在对话里展示内容"})
card6 = "/api/files/" in c6
short6 = len(c6) < 400
check("6a.纯导出:卡片+短回复", done6 and card6 and short6,
      f"对话展示 {len(c6)} 字符")
check("6b.纯导出:抑制提示外露", "正文将直接写入文档" in t6)

# ---- 7. 普通对话不受导出逻辑影响（回归） ----
c7, _, done7 = sse_send({"session_id": sid, "message": "1+1等于几？直接答"})
check("7.普通对话回归", done7 and c7.strip() != "" and "/api/files/" not in c7,
      f"回复: {c7.strip()[:30]}")

# ---- 8. 会话删除（清理测试数据） ----
del_ok = True
for s in test_sids:
    r = requests.delete(f"{BASE}/chat/session/{s}").json()
    del_ok = del_ok and r["code"] == 200
left = [s["session_id"] for s in
        requests.get(f"{BASE}/chat/sessions").json()["data"]["list"]]
check("8.会话删除清理", del_ok and not any(s in left for s in test_sids))

# ---- 汇总 ----
print("\n========== 冒烟测试汇总 ==========")
failed = [n for n, ok, _ in results if not ok]
print(f"共 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
if failed:
    print("失败项：", "; ".join(failed))
