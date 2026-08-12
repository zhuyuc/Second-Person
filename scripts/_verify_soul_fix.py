"""端到端验证：读取类操作不再触发 soul_reloaded；真实修改正常触发。"""
import os
import sqlite3
import sys
import time
from pathlib import Path

import urllib.request

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)

STYLE = Path("data/soul/SOUL_STYLE.md")
orig = STYLE.read_text(encoding="utf-8")
orig_mtime = STYLE.stat().st_mtime_ns


def max_notify_id():
    conn = sqlite3.connect("file:data/palace.db?mode=ro", uri=True)
    r = conn.execute(
        "SELECT COALESCE(MAX(id),0) FROM conversations "
        "WHERE notification_type='soul_reloaded'").fetchone()[0]
    conn.close()
    return r


def count_since(mid):
    conn = sqlite3.connect("file:data/palace.db?mode=ro", uri=True)
    r = conn.execute(
        "SELECT count(*) FROM conversations WHERE notification_type='soul_reloaded' "
        "AND id>?", (mid,)).fetchone()[0]
    conn.close()
    return r


# 1) 读取触发验证：调用 /api/soul 与 /api/soul/style/history（读文件 → atime 事件）
base_id = max_notify_id()
for path in ("/api/soul", "/api/soul/style/history?source=dialog"):
    try:
        urllib.request.urlopen(
            "http://127.0.0.1:8000" + path, timeout=10).read()
    except Exception as e:
        print("API 调用失败:", path, e)
time.sleep(4)  # 等防抖 1.5s + 通知落库
n1 = count_since(base_id)
print(f"[验证1] 读取 SOUL 后新增通知数 = {n1}（期望 0）")
assert n1 == 0, "读取仍触发 soul_reloaded！"

# 2) 真实修改验证：修改 SOUL_STYLE.md 内容 → 应触发 1 条
# （等 65s：通知去重窗口 60s，避免上轮同内容通知吞掉本次触发）
time.sleep(65)
STYLE.write_text(orig + "\n<!-- e2e-verify -->\n", encoding="utf-8")
time.sleep(4)
n2 = count_since(base_id)
print(f"[验证2] 真实修改后新增通知数 = {n2}（期望 1）")
assert n2 == 1, f"真实修改未触发或触发次数异常：{n2}"

# 3) 恢复原内容并还原 mtime：恢复写入本身是内容变化会触发，
# 但通知内容与 60s 内的修改通知相同，被通知去重吞掉 → 计数不变
STYLE.write_text(orig, encoding="utf-8")
os.utime(STYLE, ns=(orig_mtime, orig_mtime))
time.sleep(4)
n3 = count_since(base_id)
print(f"[验证3] 恢复内容后新增通知数 = {n3}（期望 1：触发但被 60s 去重）")
assert n3 == 1, f"恢复后触发次数异常：{n3}"

# 4) 恢复后再读取 → 不应新增
for path in ("/api/soul",):
    urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=10).read()
time.sleep(4)
n4 = count_since(base_id)
print(f"[验证4] 恢复后再读取新增通知数 = {n4 - 1}（期望 0）")
assert n4 == 1, "恢复后读取仍误报！"

print("\n=== 端到端验证全部通过 ===")
