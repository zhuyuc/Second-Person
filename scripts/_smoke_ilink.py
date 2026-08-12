"""微信 iLink 直连冒烟测试（任务 6 验收脚本）。

用法：
  python scripts/_smoke_ilink.py selfcheck     # 无账号自检：AES 加解密 / 解析函数 / 请求头
  python scripts/_smoke_ilink.py qrcode        # 扫码绑定 + 收发闭环（需真实账号与 ClawBot 灰度）
  python scripts/_smoke_ilink.py qrcode --token data/temp/ilink_smoke_token.json
                                      # 复用已绑定 token（验证重启免扫码）
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)

from gateway.platforms.ilink_client import (ILinkClient, MSG_TEXT, extract_media,  # noqa: E402
                                            extract_text)

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} | {name}" +
          (f" | {detail}" if detail else ""))


# ---- 1. 无账号自检 ---------------------------------------------------------
def selfcheck() -> None:
    # AES-128-ECB 加解密往返（随机密钥与明文）
    from gateway.platforms.ilink_client import _decrypt_aes_ecb, _encrypt_aes_ecb
    key = bytes(range(16))
    plain = b"hello weixin ilink smoke test 0123456789"
    enc = _encrypt_aes_ecb(plain, key)
    import os
    key_b64 = base64.b64encode(key).decode()
    dec = _decrypt_aes_ecb(enc, key_b64)
    check("1a.AES-ECB base64 密钥往返", dec == plain)
    dec_hex = _decrypt_aes_ecb(enc, key.hex())
    check("1b.AES-ECB hex 密钥往返", dec_hex == plain)

    # X-WECHAT-UIN 防重放头（两次不同）
    from gateway.platforms.ilink_client import _uin_header
    u1, u2 = _uin_header(), _uin_header()
    check("1c.UIN 头随机防重放", u1 != u2 and len(u1) > 4, f"{u1} vs {u2}")

    # 消息解析：文本 / 媒体宽容匹配
    msg_text = {"item_list": [{"type": 1, "text_item": {"text": "你好"}}]}
    check("1d.文本提取", extract_text(msg_text) == "你好")
    msg_img = {"item_list": [{"type": 2, "media_item": {"url": "https://cdn/x",
                                                        "aes_key": "abc"}}]}
    mtype, item = extract_media(msg_img)
    from gateway.platforms.ilink_client import media_aes_key, media_url
    check("1e.媒体提取", mtype == 2 and media_url(item) == "https://cdn/x"
          and media_aes_key(item) == "abc")
    check("1f.纯文本无媒体", extract_media(msg_text) == (None, {}))

    # 协议端点常量（与官方文档一致）
    check("1g.消息类型常量", MSG_TEXT == 1)

    ok = all(r[1] for r in results)
    print(f"\n自检结果：{'全部通过' if ok else '存在失败'}"
          f"（{sum(1 for r in results if r[1])}/{len(results)}）")
    sys.exit(0 if ok else 1)


# ---- 2. 扫码 + 收发闭环 ----------------------------------------------------
async def qrcode_flow(token_path: Path) -> None:
    client = ILinkClient()
    try:
        # 2a. 已有 token → 直接进入收发验证（重启免扫码路径）
        if token_path.exists():
            saved = json.loads(token_path.read_text(encoding="utf-8"))
            client.bot_token = saved.get("bot_token", "")
            client.base_url = saved.get("baseurl", "") or client.base_url
            check("2a.复用已绑定 token", bool(client.bot_token),
                  "跳过扫码，直接进入收发验证")
            update_buf = saved.get("update_buf", "")
        else:
            data = await client.request_qrcode()
            qrcode = data.get("qrcode", "")
            img = data.get("qrcode_img_content", "")
            check("2a.获取登录二维码", bool(qrcode), f"qrcode={qrcode[:16]}…")
            if img:
                if img.startswith(("http://", "https://")):
                    print(f"    → 扫码链接：{img}（微信内打开识别，或浏览器打开后用微信扫）")
                else:
                    # 兼容 data:image/png;base64, 前缀与纯 base64
                    raw = img.split(",", 1)[1] if img.startswith(
                        "data:") else img
                    out = Path("data/temp/ilink_qrcode.png")
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(base64.b64decode(raw))
                    print(
                        f"    → 二维码已保存：{out.resolve()}，请用微信「我→设置→插件→ClawBot」扫码")
            else:
                print(f"    → 二维码内容无图片数据，qrcode={qrcode}")
            # 2b. 轮询扫码状态（最长 120s）
            confirmed = False
            deadline = time.time() + 120
            while time.time() < deadline:
                st = await client.poll_qrcode(qrcode)
                status = st.get("status", "pending")
                if status == "confirmed":
                    client.bot_token = st.get("bot_token", "")
                    b64 = st.get("baseurl", "")
                    if b64:
                        client.base_url = b64.rstrip("/")
                    check("2b.扫码确认获取 bot_token", bool(client.bot_token))
                    confirmed = True
                    break
                if status == "expired":
                    break
                await asyncio.sleep(1.5)
            if not confirmed:
                check("2b.扫码确认获取 bot_token", False, "超时/过期，请重试")
                sys.exit(1)
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(json.dumps(
                {"bot_token": client.bot_token, "baseurl": client.base_url,
                 "update_buf": ""}, ensure_ascii=False), encoding="utf-8")
            print(f"    → token 已持久化：{token_path}（重启后 --token 复用，免重扫）")
            update_buf = ""

        # 2c. 收发闭环：等待入站（最长 90s）→ 原样回发
        print("    → 请在微信中给 ClawBot 发送一条消息，例如：你好")
        got = False
        deadline = time.time() + 90
        while time.time() < deadline and not got:
            msgs, update_buf = await client.get_updates(update_buf)
            for m in msgs:
                text = extract_text(m)
                token = m.get("context_token", "")
                from_id = m.get("from_user_id", "")
                if not text or not token:
                    continue
                check("2c.收到入站消息", True, f"{from_id}: {text[:20]}")
                await client.send_message(
                    from_id, token,
                    [{"type": MSG_TEXT,
                      "text_item": {"text": f"【冒烟测试】已收到：{text[:30]}"}}])
                check("2d.回发成功", True)
                got = True
                break
            if not got:
                await asyncio.sleep(1)
        if not got:
            check("2c.收到入站消息", False, "90s 内未收到消息，请确认已在微信给 ClawBot 发消息")
        # 持久化游标（模拟适配器重启续传）
        token_path.write_text(json.dumps(
            {"bot_token": client.bot_token, "baseurl": client.base_url,
             "update_buf": update_buf}, ensure_ascii=False), encoding="utf-8")
        check("2e.游标持久化", True, f"update_buf={update_buf[:16]}…")
    finally:
        await client.aclose()

    ok = all(r[1] for r in results)
    print(f"\n冒烟结果：{'全部通过' if ok else '存在失败'}"
          f"（{sum(1 for r in results if r[1])}/{len(results)}）")
    sys.exit(0 if ok else 1)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "selfcheck"
    if mode == "selfcheck":
        selfcheck()
        return
    if mode == "qrcode":
        token_path = Path("data/temp/ilink_smoke_token.json")
        for i, a in enumerate(sys.argv):
            if a == "--token" and i + 1 < len(sys.argv):
                token_path = Path(sys.argv[i + 1])
        asyncio.run(qrcode_flow(token_path))
        return
    print(__doc__)
    sys.exit(2)


if __name__ == "__main__":
    main()
