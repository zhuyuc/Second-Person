"""端到端回归：提议—确认闭环（AI 提议 → 用户"可以" → 动作真实执行）。

Part A（确定性单测，无需服务）：
1. is_confirm_ack 确认词判定正反例
2. detect_proposal_sentence 散文式提议兜底捕获
3. detect_fake_claim 过去式假声明 + 未来式假承诺检测

Part B（需服务运行于 http://127.0.0.1:8000）：
4. 建会话并直插一条携带 pending 提议的 assistant 消息（模拟上一轮 AI 提议）
5. 以 think_mode=quick 发送"可以"（复现原故障条件：快速通道）
6. 校验：思考过程出现【提议承接】、实际调用了工具、
   pending 提议被标记 consumed、回复无未闭环假承诺
7. 收尾：删除测试会话
"""
import json
import os
import re
import sqlite3
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)

from agent.context_signals import (  # noqa: E402
    detect_fake_claim,
    detect_proposal_sentence,
    is_confirm_ack,
    map_proposal_tools,
)

BASE = "http://127.0.0.1:8000/api"
DB_PATH = "data/palace.db"
FAILED = []

PROPOSAL_TEXT = ("拆解 Pi 仓库（github.com/earendil-works/pi）的 unified "
                 "LLM API 与 agent loop 目录结构，并对照 Second Person 现有代码给出落点")


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" | {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


# ---- Part A：确定性单测 -------------------------------------------------------

def part_a():
    print("== Part A 确定性单测 ==")
    # 1. 确认词正反例
    for w in ["可以", "好", "好的", "行吧", "嗯", "OK", "去吧", "开始吧",
              "可以。", "继续"]:
        check(f"确认词命中：{w!r}", is_confirm_ack(w))
    for w in ["好的，知道了，你先忙", "可以的，不过我想先看下报价",
              "你好，介绍一下你自己", "可以帮我顺便再查下天气吗", ""]:
        check(f"非确认词放行：{w!r}", not is_confirm_ack(w))

    # 2. 散文式提议兜底捕获（本次线上故障的原句形态）
    trap = ("因此，Pi Agent 简单说就是一个反重型的开源 Agent 框架。"
            "如果你要我下一步帮你拆它的 agent loop 实现，或者对比它和你现在"
            "产品里对话编排的差异，直接说，我可以继续挖。")
    prop = detect_proposal_sentence(trap)
    check("兜底捕获散文式提议", prop is not None and len(prop) >= 10,
          f"captured={prop}")
    check("普通回复不误报提议",
          detect_proposal_sentence("结论：RAG 是检索增强生成架构。") is None)

    # 3. 假声明/假承诺检测
    check("过去式假声明命中", detect_fake_claim("文件已生成，可以下载了。"))
    check("未来式假承诺命中（原故障句）",
          detect_fake_claim("好，那我现在就去看 Pi 仓库，稍等，我查完直接贴结论。"))
    check("正常回复不误报",
          not detect_fake_claim("结论：Pi 是一个极简的开源 Agent 工具包。"))

    # 4. 指导句排除（缺陷 A：触发指引不是待确认提议）
    instr = ("好的。如果你要我直接去仓库抓 Pi 的文件，"
             "明确说一声\"查 Pi 仓库目录\"，就能触发。")
    check("指导句不落 pending", detect_proposal_sentence(instr) is None)

    # 5. 确定性工具映射（决策 2）
    t1 = map_proposal_tools(
        "拆解 Pi 仓库的 unified LLM API 与 agent loop 目录结构",
        ["web_search", "web_fetch", "memory_search"])
    check("工具映射：仓库拆解→query_external+web_search",
          t1[0] == "query_external" and "web_search" in t1[1], f"{t1}")
    t2 = map_proposal_tools("把前面讨论的方案导出为 Word 文档",
                            ["generate_document", "web_search"])
    check("工具映射：导出→file_op+generate_document",
          t2 == ("file_op", ["generate_document"]), f"{t2}")
    t3 = map_proposal_tools("随便聊聊别的", [])
    check("工具映射：无可用工具时不硬塞",
          t3[1] == [], f"{t3}")


# ---- Part B：端到端闭环（需服务运行） -------------------------------------------

def consume_sse(session_id, message, timeout_s=300):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=timeout_s)
    body = json.dumps({"session_id": session_id, "message": message,
                       "think_mode": "quick"})
    conn.request("POST", "/api/chat/send", body=body,
                 headers={"Content-Type": "application/json",
                          "Accept": "text/event-stream"})
    resp = conn.getresponse()
    events, ename, edata = [], None, ""

    def flush():
        nonlocal ename, edata
        if ename:
            try:
                payload = json.loads(edata) if edata else {}
            except json.JSONDecodeError:
                payload = {"raw": edata}
            events.append((ename, payload))
        ename, edata = None, ""

    while True:
        line = resp.readline()
        if not line:
            break
        line = line.decode("utf-8", errors="replace").rstrip("\r\n")
        if line == "":
            flush()
        elif line.startswith("event:"):
            ename = line[6:].strip()
        elif line.startswith("data:"):
            edata += line[5:].strip()
    flush()
    conn.close()
    return events


def post_json(path, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def part_b():
    print("== Part B 端到端闭环（需服务运行） ==")
    try:
        urllib.request.urlopen(BASE.replace("/api", "/api/health"), timeout=5)
    except Exception as e:  # noqa: BLE001
        print(f"[SKIP] 服务未运行（{e}），跳过端到端部分")
        return

    # 4. 建会话 + 直插 pending 提议的 assistant 消息（模拟上一轮 AI 提议）
    r = post_json("/chat/session/create", {})
    sid = r.get("data", {}).get("session_id")
    check("创建测试会话", bool(sid), f"session_id={sid}")
    if not sid:
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    now = datetime.now().isoformat(timespec="seconds")
    payload = json.dumps({"text": PROPOSAL_TEXT,
                          "kind": "proposal", "status": "pending"},
                         ensure_ascii=False)
    conn.execute(
        "INSERT INTO sessions(session_id,title,title_source,last_active,"
        "message_count,channel,from_session,created_at) "
        "SELECT ?, '提议闭环回归', 'manual', ?, 0, NULL, NULL, ? "
        "WHERE NOT EXISTS(SELECT 1 FROM sessions WHERE session_id=?)",
        (sid, now, now, sid))
    cur = conn.execute(
        "INSERT INTO conversations(session_id,role,message_type,notification_type,"
        "content,citations,feedback,create_time,thinking,images,visuals,"
        "response_strategy_json,cognitive_skeleton_json,protected_from_compression,"
        "next_step_shown,parent_id,version_group_id,is_active) "
        "VALUES(?,'assistant','normal',NULL,?,NULL,0,?,NULL,NULL,NULL,NULL,NULL,0,?,NULL,NULL,1)",
        (sid, "上一轮分析内容（略）。", now, payload))
    conn.execute("UPDATE conversations SET version_group_id=id WHERE id=?",
                 (cur.lastrowid,))
    conn.execute("UPDATE sessions SET message_count=message_count+1 "
                 "WHERE session_id=?", (sid,))
    conn.commit()
    proposal_msg_id = cur.lastrowid
    check("pending 提议消息已直插", proposal_msg_id > 0,
          f"msg_id={proposal_msg_id}")

    # 5. 以快速通道发送"可以"（复现原故障条件）
    events = consume_sse(sid, "可以")
    check("SSE 流结束(turn_completed)",
          any(e[0] in ("done", "turn_completed") for e in events),
          f"事件总数={len(events)}")
    check("SSE 无 error 事件",
          next((e[1] for e in events if e[0] == "error"), None) is None)
    thinking = "".join(e[1].get("text", "")
                       for e in events if e[0] == "thinking_delta")

    # 6.1 绑定发生：思考过程出现【提议承接】
    check("提议绑定生效（【提议承接】外露）", "【提议承接】" in thinking)
    # 6.2 动作真实执行：思考过程出现工具调用（web_search/web_fetch）
    tool_called = bool(re.search(
        r"【工具调用】.*(web_search|web_fetch)", thinking))
    check("确认触发了工具执行", tool_called,
          "thinking 未见 web_search/web_fetch 调用" if not tool_called else "")
    # 6.3 pending 提议已消费
    row = conn.execute(
        "SELECT next_step_shown FROM conversations WHERE id=?",
        (proposal_msg_id,)).fetchone()
    consumed = False
    if row and row["next_step_shown"]:
        consumed = json.loads(
            row["next_step_shown"]).get("status") == "consumed"
    check("pending 提议已标记 consumed", consumed,
          f"next_step_shown={row['next_step_shown'] if row else None}")
    # 6.4 回复落库且无未闭环假承诺（命中检测则必须带诚实纠偏）
    arow = conn.execute(
        "SELECT content FROM conversations WHERE session_id=? AND role='assistant' "
        "ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
    content = arow["content"] if arow else ""
    check("确认轮 assistant 回复已落库", bool(content.strip()),
          f"len={len(content)}")
    promise_hit = detect_fake_claim(content)
    has_correction = "更正：我这边没有后台执行能力" in content
    check("无未闭环假承诺（命中则必须带纠偏）",
          (not promise_hit) or has_correction,
          f"promise_hit={promise_hit} correction={has_correction}")
    # 6.5 实质性交付：工具已执行时回复必须交付成品，不得再次推请确认
    deflect = any(w in content for w in
                  ("随时说", "按这个方向继续", "如果你想开始下一步", "要不要开始"))
    check("实质性交付（非推请式短回复）",
          len(content) >= 100 and not deflect,
          f"len={len(content)} deflect={deflect}")
    conn.close()

    # 7. 清理测试会话
    try:
        req = urllib.request.Request(
            f"{BASE}/chat/session/{sid}", method="DELETE")
        urllib.request.urlopen(req, timeout=30)
        print(f"[INFO] 测试会话 {sid} 已清理")
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 测试会话清理失败：{e}")


def part_c():
    print("== Part C 存量惰性自愈（旧格式 nss=NULL → 回扫补落 → 确认闭环） ==")
    try:
        urllib.request.urlopen(BASE.replace("/api", "/api/health"), timeout=5)
    except Exception as e:  # noqa: BLE001
        print(f"[SKIP] 服务未运行（{e}），跳过 Part C")
        return

    r = post_json("/chat/session/create", {})
    sid = r.get("data", {}).get("session_id")
    check("[C] 创建存量模拟会话", bool(sid), f"session_id={sid}")
    if not sid:
        return

    # 模拟旧代码提议轮：尾部含散文式提议，next_step_shown 为 NULL
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    now = datetime.now().isoformat(timespec="seconds")
    legacy_content = (
        "结论：Pi 是一个极简的开源 Agent 工具包，核心是统一 LLM API + agent loop。"
        "如果你愿意，我下一步可以帮你拆解 Pi 仓库的 unified LLM API 与 "
        "agent loop 目录结构，对着我们的代码看怎么落。")
    cur = conn.execute(
        "INSERT INTO conversations(session_id,role,message_type,notification_type,"
        "content,citations,feedback,create_time,thinking,images,visuals,"
        "response_strategy_json,cognitive_skeleton_json,protected_from_compression,"
        "next_step_shown,parent_id,version_group_id,is_active) "
        "VALUES(?,'assistant','normal',NULL,?,NULL,0,?,NULL,NULL,NULL,NULL,NULL,0,"
        "NULL,NULL,NULL,1)",
        (sid, legacy_content, now))
    conn.execute("UPDATE conversations SET version_group_id=id WHERE id=?",
                 (cur.lastrowid,))
    conn.commit()
    legacy_msg_id = cur.lastrowid

    events = consume_sse(sid, "可以")
    check("[C] SSE 流结束", any(e[0] in ("done", "turn_completed")
                             for e in events), f"事件总数={len(events)}")
    thinking = "".join(e[1].get("text", "")
                       for e in events if e[0] == "thinking_delta")
    check("[C] 惰性自愈后绑定生效（【提议承接】）", "【提议承接】" in thinking)
    check("[C] 确认触发了工具执行",
          bool(__import__("re").search(
              r"【工具调用】.*(web_search|web_fetch)", thinking)))
    row = conn.execute(
        "SELECT next_step_shown FROM conversations WHERE id=?",
        (legacy_msg_id,)).fetchone()
    status = ""
    if row and row["next_step_shown"]:
        status = json.loads(row["next_step_shown"]).get("status", "")
    check("[C] 存量消息已自愈并消费（status=consumed）", status == "consumed",
          f"status={status} nss={row['next_step_shown'] if row else None}")
    arow = conn.execute(
        "SELECT content FROM conversations WHERE session_id=? AND role='assistant' "
        "ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
    content = arow["content"] if arow else ""
    deflect = any(w in content for w in
                  ("随时说", "按这个方向继续", "如果你想开始下一步", "要不要开始"))
    check("[C] 实质性交付（非推请式短回复）",
          len(content) >= 100 and not deflect,
          f"len={len(content)} deflect={deflect}")
    conn.close()

    try:
        req = urllib.request.Request(
            f"{BASE}/chat/session/{sid}", method="DELETE")
        urllib.request.urlopen(req, timeout=30)
        print(f"[INFO] Part C 测试会话 {sid} 已清理")
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] Part C 会话清理失败：{e}")


def main():
    part_a()
    part_b()
    part_c()
    print()
    if FAILED:
        print(f"回归失败 {len(FAILED)} 项: {FAILED}")
        sys.exit(1)
    print("回归通过：提议—确认闭环全链路正常（含存量自愈）")


if __name__ == "__main__":
    main()
