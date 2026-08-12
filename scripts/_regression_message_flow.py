"""端到端回归：消息流程（建会话→发消息→SSE接收→落库校验）。

需服务运行于 http://127.0.0.1:8000。
验证点：
1. POST /api/chat/session/create 返回新 session_id
2. POST /api/chat/send SSE 流正常产出 delta 与 done 事件
3. GET /api/chat/messages 返回 user + assistant 两条消息
4. 直读 SQLite 校验 conversations 表行完整性（15 列均有正确落位）
5. sessions.message_count 正确递增
"""
import json
import sqlite3
import sys
import urllib.request

BASE = "http://127.0.0.1:8000/api"
DB_PATH = "data/palace.db"
FAILED = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" | {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def post_json(path, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def get_json(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode())


def consume_sse(session_id, message, timeout_s=180):
    """发送消息并消费 SSE 流，返回 (事件列表, 是否出现 done)。"""
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=timeout_s)
    body = json.dumps({"session_id": session_id, "message": message})
    conn.request("POST", "/api/chat/send", body=body,
                 headers={"Content-Type": "application/json",
                          "Accept": "text/event-stream"})
    resp = conn.getresponse()
    events, got_done, got_error = [], False, None
    ename, edata = None, ""

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
    got_done = any(e[0] in ("done", "turn_completed") for e in events)
    err = next((e[1] for e in events if e[0] == "error"), None)
    conn.close()
    return events, got_done, err


def main():
    msg = "回归测试消息：请只回复“收到”两个字"

    # 1. 建会话
    r = post_json("/chat/session/create", {})
    sid = r.get("data", {}).get("session_id")
    check("创建会话", bool(sid), f"session_id={sid}")
    if not sid:
        return

    # 2. 发消息 + SSE 流
    events, got_done, got_error = consume_sse(sid, msg)
    check("SSE 流正常结束(turn_completed)", got_done, f"事件总数={len(events)}")
    check("SSE 无 error 事件", got_error is None,
          f"error={got_error}" if got_error else "")
    delta_count = sum(1 for e in events if e[0] == "content_delta")
    check("收到流式 content_delta", delta_count > 0, f"delta 数={delta_count}")

    # 3. API 读回消息（data 直接为消息列表）
    r = get_json(f"/chat/messages?session_id={sid}&limit=10")
    msgs = r.get("data") or []
    roles = [m.get("role") for m in msgs]
    check("user 消息已落库", "user" in roles, f"roles={roles}")
    check("assistant 回复已落库", "assistant" in roles, f"roles={roles}")
    user_msg = next((m for m in msgs if m.get("role") == "user"), None)
    asst_msg = next((m for m in msgs if m.get("role") == "assistant"), None)
    check("user 内容一致", user_msg and user_msg.get("content") == msg)
    check("assistant 回复非空",
          bool(asst_msg and asst_msg.get("content", "").strip()))

    # 4. 直读 SQLite 校验行完整性
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cols = [c[1] for c in conn.execute("PRAGMA table_info(conversations)")]
    # 表含 id 主键共 16 列；INSERT 显式写 15 列（id 自增）
    check("conversations 表 16 列(含 id)", len(cols) == 16, f"列数={len(cols)}")
    rows = conn.execute(
        "SELECT * FROM conversations WHERE session_id=? ORDER BY id",
        (sid,)).fetchall()
    check("DB 行数与 API 一致", len(rows) == len(msgs),
          f"DB={len(rows)} API={len(msgs)}")
    for row in rows:
        d = dict(row)
        check(f"行#{d['id']} role={d['role']} 必填字段完整",
              bool(d["session_id"]) and d["role"] in ("user", "assistant")
              and d["content"] and d["create_time"]
              and d["feedback"] == 0 and d["message_type"] == "normal")
    # 5. message_count 递增校验
    srow = conn.execute(
        "SELECT message_count FROM sessions WHERE session_id=?",
        (sid,)).fetchone()
    check("sessions.message_count 与消息数一致",
          srow and srow["message_count"] == len(rows),
          f"count={srow['message_count'] if srow else None} rows={len(rows)}")
    conn.close()

    print()
    if FAILED:
        print(f"回归失败 {len(FAILED)} 项: {FAILED}")
        sys.exit(1)
    print("回归通过：消息流程全链路正常")


if __name__ == "__main__":
    main()
