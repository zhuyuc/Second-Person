"""设置页业务逻辑：Provider、备份、IM 渠道等。"""
from __future__ import annotations

import json
import logging
import tempfile
import zipfile
from pathlib import Path

from infrastructure.timeutil import now_iso

logger = logging.getLogger("second_person.settings_service")

BACKUP_IMPORT_MAX_BYTES = 500 * 1024 * 1024  # 500MB


class SettingsService:
    def __init__(self, container) -> None:
        self.c = container
        self._weixin_qrcode: dict = {}

    # ---- Provider -----------------------------------------------------------
    @staticmethod
    def clean_provider_fields(body: dict) -> dict:
        """清洗 Provider 表单字段：去首尾空格/换行。"""
        out = dict(body)
        for k in ("display_name", "provider_type", "base_url", "model_id", "api_key"):
            if isinstance(out.get(k), str):
                out[k] = out[k].strip()
        return out

    def validate_provider_required(self, body: dict) -> None:
        for field, label in (("base_url", "Base URL"), ("model_id", "模型 ID"),
                             ("api_key", "API Key")):
            if not (body.get(field) or "").strip():
                raise ValueError(f"请先填写{label}")

    async def probe_snapshot(self, snap) -> dict:
        """连通性探测：先试 chat，失败再试 embed。"""
        try:
            await self.c.llm.chat(snap, [{"role": "user", "content": "ping"}],
                                  source="main_chat", max_tokens=10)
            return {"ok": True}
        except Exception as chat_err:  # noqa: BLE001
            try:
                await self.c.llm.embed(snap, ["ping"])
                return {"ok": True}
            except Exception:  # noqa: BLE001
                return {"ok": False, "error": str(chat_err)}

    async def test_provider(self, body: dict) -> dict:
        from tools.web_fetch import validate_base_url
        from infrastructure.llm_provider import ProviderSnapshot
        body = self.clean_provider_fields(body)
        url_err = await validate_base_url(body.get("base_url", ""))
        if url_err:
            return {"ok": False, "error": url_err}
        snap = ProviderSnapshot("test", body["provider_type"], body["base_url"],
                                body["api_key"], body["model_id"])
        return await self.probe_snapshot(snap)

    def add_or_update_provider(self, body: dict) -> dict:
        """去重入库：同 base_url + model_id 视为同一模型。"""
        c = self.c
        for ex in c.providers.list_providers():
            if ex["base_url"] == body["base_url"] and ex["model_id"] == body["model_id"]:
                c.providers.update_provider(ex["id"], {
                    "display_name": body.get("display_name", ex["display_name"]),
                    "provider_type": body["provider_type"],
                    "input_price": body.get("input_price"),
                    "output_price": body.get("output_price"),
                    "context_window": body.get("context_window", 128000),
                }, body.get("api_key"))
                return {"id": ex["id"], "deduped": True}
        from memory.naming import provider_id as mk
        pid = mk(c.providers.next_provider_seq())
        c.providers.add_provider(
            pid, body.get("display_name") or body["model_id"], body["provider_type"],
            body["base_url"], body["model_id"], body["api_key"],
            body.get("input_price"), body.get("output_price"),
            body.get("context_window", 128000))
        c.oplog.log("provider_add", pid)
        return {"id": pid}

    def mask_credential(self, key: str) -> str:
        return key[:3] + "****" + key[-4:] if len(key) > 8 else "****"

    # ---- 备份 ---------------------------------------------------------------
    async def restore_backup(self, backup_id: str) -> None:
        from memory.recovery import rebuild_index
        c = self.c
        await c.backup.restore(backup_id, lambda: rebuild_index(c.db, c.data_dir))

    async def import_backup_bytes(self, content: bytes, filename: str) -> None:
        """导入 zip 备份包的核心逻辑。"""
        if len(content) > BACKUP_IMPORT_MAX_BYTES:
            raise ValueError(
                f"备份文件过大（上限 {BACKUP_IMPORT_MAX_BYTES // 1024 // 1024}MB）")
        c = self.c
        from memory.recovery import rebuild_index
        tmp = Path(tempfile.mktemp(suffix=".zip"))
        tmp.write_bytes(content)
        await c.backup.create(label="pre_import", protective=True)
        with zipfile.ZipFile(tmp) as z:
            names = z.namelist()
            if "config.yaml" not in names:
                raise ValueError("导入包缺少 config.yaml")
            data_root = Path(c.data_dir).resolve()
            for name in names:
                if name.startswith("md/"):
                    dst = (data_root / name[3:]).resolve()
                    if not str(dst).startswith(str(data_root)):
                        logger.warning("备份导入跳过路径穿越条目：%s", name)
                        continue
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(name) as src:
                        dst.write_bytes(src.read())
                elif name == "config.yaml":
                    z.extract(name, c.data_dir)
        rebuild_index(c.db, c.data_dir)
        c.vs.load()
        c.oplog.log("data_import", filename)

    # ---- IM 渠道 ------------------------------------------------------------
    def validate_add_platform(self, ptype: str) -> None:
        if not ptype or ptype == "web":
            raise ValueError("无效的平台类型")
        if ptype == "weixin":
            raise ValueError("微信渠道请使用扫码绑定（设置页 → 添加渠道 → 微信）")

    def validate_platform_credentials(self, ptype: str, bot_token: str,
                                      app_secret: str) -> None:
        if not bot_token:
            raise ValueError("Bot Token 不能为空")
        if ptype in ("feishu", "dingtalk", "wecom") and not app_secret:
            raise ValueError("App Secret 不能为空")

    def add_platform(self, body: dict) -> str:
        c = self.c
        ptype = (body.get("platform_type") or "").strip()
        self.validate_add_platform(ptype)
        bot_token = (body.get("bot_token") or "").strip()
        app_secret = (body.get("app_secret") or "").strip()
        self.validate_platform_credentials(ptype, bot_token, app_secret)
        cred_id = c.creds.store(f"platform:{ptype}", "platform_bot", json.dumps({
            "bot_token": bot_token, "app_secret": app_secret}))
        pid = f"{ptype}_1"
        c.db.execute(
            "INSERT OR REPLACE INTO platforms(id,platform_type,enabled,status,"
            "whitelist_user_id,callback_url,credential_id,created_at) "
            "VALUES(?,?,0,'healthy',?,?,?,?)",
            (pid, ptype, body.get("whitelist_user_id"), body.get("callback_url"), cred_id,
             now_iso()))
        return pid

    def get_platform_detail(self, pid: str) -> dict:
        c = self.c
        row = c.db.query_one("SELECT * FROM platforms WHERE id=?", (pid,))
        if not row:
            raise ValueError("渠道不存在")
        if row["platform_type"] == "weixin":
            return {
                "id": row["id"], "platform_type": "weixin",
                "bot_token": "", "app_secret": "",
                "whitelist_user_id": row["whitelist_user_id"] or "",
                "callback_url": row["callback_url"] or ""}
        bot_token, app_secret = "", ""
        if row["credential_id"]:
            raw = c.creds.get(row["credential_id"])
            if raw:
                try:
                    d = json.loads(raw)
                    bot_token = d.get("bot_token", "")
                    app_secret = d.get("app_secret", "")
                except Exception:  # noqa: BLE001
                    pass
        return {
            "id": row["id"], "platform_type": row["platform_type"],
            "bot_token": bot_token, "app_secret": app_secret,
            "bot_token_masked": self.mask_credential(bot_token),
            "app_secret_masked": self.mask_credential(app_secret),
            "whitelist_user_id": row["whitelist_user_id"] or "",
            "callback_url": row["callback_url"] or ""}

    async def edit_platform(self, pid: str, body: dict) -> None:
        c = self.c
        row = c.db.query_one("SELECT * FROM platforms WHERE id=?", (pid,))
        if not row:
            raise ValueError("渠道不存在")
        ptype = row["platform_type"]
        if ptype == "weixin":
            c.db.execute(
                "UPDATE platforms SET whitelist_user_id=?, callback_url=? WHERE id=?",
                (body.get("whitelist_user_id"), body.get("callback_url"), pid))
        elif ptype != "web":
            bot_token = (body.get("bot_token") or "").strip()
            app_secret = (body.get("app_secret") or "").strip()
            self.validate_platform_credentials(ptype, bot_token, app_secret)
            payload = json.dumps({"bot_token": bot_token, "app_secret": app_secret})
            if row["credential_id"]:
                c.creds.update(row["credential_id"], payload)
                cred_id = row["credential_id"]
            else:
                cred_id = c.creds.store(f"platform:{ptype}", "platform_bot", payload)
            c.db.execute(
                "UPDATE platforms SET whitelist_user_id=?, callback_url=?, credential_id=? WHERE id=?",
                (body.get("whitelist_user_id"), body.get("callback_url"), cred_id, pid))
        else:
            c.db.execute("UPDATE platforms SET whitelist_user_id=?, callback_url=? WHERE id=?",
                         (body.get("whitelist_user_id"), body.get("callback_url"), pid))
        c.oplog.log("platform_update", pid)
        if row["enabled"] and hasattr(c, "adapters"):
            await c.adapters.reload()

    async def enable_platform(self, pid: str) -> list[str]:
        c = self.c
        row = c.db.query_one("SELECT * FROM platforms WHERE id=?", (pid,))
        if not row:
            raise ValueError("渠道不存在")
        if row["platform_type"] != "web" and not row["credential_id"]:
            raise ValueError("该渠道未配置凭证，无法启用")
        disabled = c.db.query_all(
            "SELECT id FROM platforms WHERE platform_type!='web' AND id!=? AND enabled=1", (pid,))
        c.db.execute(
            "UPDATE platforms SET enabled=0 WHERE platform_type!='web' AND id!=?", (pid,))
        c.db.execute(
            "UPDATE platforms SET enabled=1, status='healthy' WHERE id=?", (pid,))
        if hasattr(c, "adapters"):
            await c.adapters.reload()
        return [r["id"] for r in disabled]

    async def test_push(self) -> dict:
        c = self.c
        ad = getattr(c, "adapters", None)
        active = ad.active if ad else None
        if not active:
            raise ValueError("当前未启用任何 IM 渠道")
        if active.platform_type == "weixin":
            raise ValueError(
                "微信渠道不支持主动推送（iLink 官方限制），"
                "系统通知仅推送到 Web 端和已启用的其他 IM 渠道")
        target = ad.resolve_push_target()
        if not target:
            raise ValueError(
                "无可用推送目标：请先在渠道配置填写白名单用户"
                "，或先用该账号给机器人发一条消息")
        await active.send_message(
            target, "【测试通知】Second Person 主动推送通道验证，收到即代表配置正常。")
        return {"target": target}

    def weixin_qrcode_png(self, content: str) -> str:
        """把扫码链接生成为二维码 PNG 的 data URL。"""
        try:
            import base64 as _b64
            import io
            import qrcode
            qr = qrcode.QRCode(border=2, box_size=10,
                               error_correction=qrcode.constants.ERROR_CORRECT_M)
            qr.add_data(content)
            qr.make(fit=True)
            buf = io.BytesIO()
            qr.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
            return "data:image/png;base64," + _b64.b64encode(buf.getvalue()).decode()
        except Exception as e:  # noqa: BLE001
            logger.warning("微信二维码生成失败（前端将显示链接兜底）：%s", e)
            return ""

    async def request_weixin_qrcode(self) -> dict:
        c = self.c
        pid = "weixin_1"
        row = c.db.query_one("SELECT * FROM platforms WHERE id=?", (pid,))
        if not row:
            c.db.execute(
                "INSERT OR REPLACE INTO platforms(id,platform_type,enabled,status,"
                "whitelist_user_id,callback_url,credential_id,created_at) "
                "VALUES(?,?,0,'healthy','',NULL,NULL,?)", (pid, "weixin", now_iso()))
        from gateway.platforms.ilink_client import ILinkClient
        client = ILinkClient()
        try:
            data = await client.request_qrcode()
        finally:
            await client.aclose()
        qrcode = data.get("qrcode", "")
        img_url = data.get("qrcode_img_content", "")
        qrcode_img = self.weixin_qrcode_png(img_url or qrcode)
        self._weixin_qrcode["qrcode"] = qrcode
        return {"qrcode": qrcode, "qrcode_img": qrcode_img, "qrcode_url": img_url}

    async def poll_weixin_qrcode_status(self, qrcode: str = "") -> dict:
        c = self.c
        qrcode = qrcode or self._weixin_qrcode.get("qrcode", "")
        if not qrcode:
            raise ValueError("缺少二维码参数，请重新发起绑定")
        from gateway.platforms.ilink_client import ILinkClient
        client = ILinkClient()
        try:
            data = await client.poll_qrcode(qrcode)
        finally:
            await client.aclose()
        status = data.get("status", "pending")
        if status == "expired":
            self._weixin_qrcode.pop("qrcode", None)
            return {"status": "expired"}
        if status != "confirmed":
            return {"status": status or "pending"}
        bot_token = data.get("bot_token", "")
        if not bot_token:
            raise ValueError("扫码确认但未返回 bot_token")
        payload = json.dumps({"bot_token": bot_token,
                              "baseurl": data.get("baseurl", ""),
                              "context_token": "", "update_buf": ""},
                             ensure_ascii=False)
        pid = "weixin_1"
        row = c.db.query_one("SELECT * FROM platforms WHERE id=?", (pid,))
        if not row:
            raise ValueError("微信渠道记录不存在，请重新发起绑定")
        if row["credential_id"]:
            c.creds.update(row["credential_id"], payload)
        else:
            cred_id = c.creds.store("platform:weixin", "platform_bot", payload)
            c.db.execute("UPDATE platforms SET credential_id=? WHERE id=?",
                         (cred_id, pid))
        c.oplog.log("platform_update", pid)
        return {"status": "confirmed"}

    async def test_platform_connectivity(self, ptype: str, cfg: dict) -> dict:
        try:
            if not cfg.get("bot_token"):
                raise RuntimeError("请先填写 Bot Token / App ID")
            if ptype == "telegram":
                import httpx
                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.get(
                        f"https://api.telegram.org/bot{cfg['bot_token']}/getMe")
                    if r.status_code != 200:
                        raise RuntimeError(f"Telegram API 返回 {r.status_code}")
            elif ptype == "feishu":
                if not cfg.get("app_secret"):
                    raise RuntimeError("飞书需填写 Bot Token 和 App Secret")
                import httpx
                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.post(
                        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                        json={"app_id": cfg["bot_token"], "app_secret": cfg["app_secret"]})
                    if r.status_code != 200:
                        raise RuntimeError(f"飞书 API 返回 {r.status_code}")
                    data = r.json()
                    if data.get("code") != 0:
                        raise RuntimeError(f"飞书鉴权失败：{data.get('msg', '未知错误')}")
            elif ptype == "dingtalk":
                if not cfg.get("app_secret"):
                    raise RuntimeError("钉钉需填写 App ID 和 App Secret")
                import httpx
                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.post(
                        "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                        json={"appKey": cfg["bot_token"], "appSecret": cfg["app_secret"]})
                    if r.status_code != 200:
                        raise RuntimeError(
                            f"钉钉鉴权失败（HTTP {r.status_code}），请检查 AppKey/AppSecret")
            elif ptype == "wecom":
                if not cfg.get("app_secret"):
                    raise RuntimeError("企业微信需填写 CorpID 和 App Secret")
                import httpx
                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.get(
                        "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                        params={"corpid": cfg["bot_token"],
                                "corpsecret": cfg["app_secret"]})
                    if r.status_code != 200:
                        raise RuntimeError(f"企微 API 返回 {r.status_code}")
                    data = r.json()
                    if data.get("errcode") != 0:
                        raise RuntimeError(
                            f"企微鉴权失败：{data.get('errmsg', '未知错误')}")
            else:
                raise RuntimeError(f"未知平台类型：{ptype or '未选择'}")
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
