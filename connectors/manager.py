"""
连接器管理器（产品文档 §外部系统连接器 / 开发文档 §3.5）。

- CRUD：添加/编辑/删除/启停/刷新工具
- 添加或重连时 tools/list 拉取，工具加前缀 {connector_id}__{tool_name} 注册到 ToolRegistry
- env 敏感值加密存 credentials；config 存非敏感部分
- [断开] 软断开 enabled=0，工具下线；[删除] 彻底移除配置/凭证/工具注册
- OAuth 2.1 PKCE：state 暂存 5 分钟，回调换 token 加密存储，过期前 5 分钟自动续期
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime

from tools.base import ToolSpec

from .mcp_client import MCPClient
from .tool_filter import apply_tool_filter

logger = logging.getLogger("second_person.connectors")


class ConnectorManager:
    def __init__(self, db, credential_store, tool_registry):
        self.db = db
        self.creds = credential_store
        self.registry = tool_registry
        self._clients: dict[str, MCPClient] = {}

    def _connector_id(self) -> str:
        return f"conn_{uuid.uuid4().hex[:8]}"

    # ---- CRUD -------------------------------------------------------------
    async def add(self, name: str, transport: str, config: dict, timeout: int = 120,
                  tools_filter: dict | None = None) -> str:
        cid = self._connector_id()
        # 拆出敏感 env / auth 加密
        sensitive = {}
        clean_config = dict(config)
        if transport == "stdio" and config.get("env"):
            sensitive["env"] = config["env"]
            clean_config = {
                **config, "env": {k: "••••••" for k in config["env"]}}
        elif transport == "http" and config.get("auth", {}).get("value"):
            sensitive["auth_value"] = config["auth"]["value"]
        cred_id = self.creds.store(f"connector:{cid}", "connector",
                                   json.dumps(sensitive, ensure_ascii=False)) if sensitive else None
        self.db.execute(
            "INSERT INTO connectors(id,name,transport,config,credential_id,timeout,"
            "tools_filter,status,created_at) VALUES(?,?,?,?,?,?,?,'connected',?)",
            (cid, name, transport, json.dumps(clean_config, ensure_ascii=False), cred_id,
             timeout, json.dumps(tools_filter or {}, ensure_ascii=False),
             datetime.now().isoformat(timespec="seconds")))
        try:
            await self.connect(cid)
        except Exception:  # noqa: BLE001
            # 连接失败回滚状态：避免库中残留虚假 connected
            self.db.execute(
                "UPDATE connectors SET status='error' WHERE id=?", (cid,))
            raise
        return cid

    def _resolve_config(self, row) -> dict:
        config = json.loads(row["config"])
        if row["credential_id"]:
            sec = self.creds.get(row["credential_id"])
            if sec:
                sensitive = json.loads(sec)
                if "env" in sensitive:
                    config["env"] = sensitive["env"]
                if "auth_value" in sensitive:
                    config.setdefault("auth", {})[
                        "value"] = sensitive["auth_value"]
        return config

    async def connect(self, connector_id: str) -> list[dict]:
        row = self.db.query_one(
            "SELECT * FROM connectors WHERE id=?", (connector_id,))
        if not row:
            raise KeyError(connector_id)
        config = self._resolve_config(row)
        client = MCPClient(row["transport"], config, row["timeout"])
        await client.connect()
        self._clients[connector_id] = client
        return await self.refresh_tools(connector_id)

    async def refresh_tools(self, connector_id: str) -> list[dict]:
        client = self._clients.get(connector_id)
        row = self.db.query_one(
            "SELECT * FROM connectors WHERE id=?", (connector_id,))
        if not client or not row:
            return []
        tools = await client.list_tools()
        tfilter = json.loads(row["tools_filter"] or "{}")
        tools = apply_tool_filter(tools, tfilter)
        # 注册到 ToolRegistry，加前缀
        self.registry.unregister_connector(connector_id)
        for t in tools:
            prefixed = f"{connector_id}__{t['name']}"
            self._register_mcp_tool(connector_id, prefixed, t)
        self.db.execute("UPDATE connectors SET tools_cache=? WHERE id=?",
                        (json.dumps(tools, ensure_ascii=False), connector_id))
        return tools

    def _register_mcp_tool(self, connector_id: str, prefixed_name: str, tool: dict) -> None:
        raw_name = tool["name"]

        async def _invoke(**kwargs):
            client = self._clients.get(connector_id)
            if not client or not client.is_alive():
                raise RuntimeError(f"连接器 {connector_id} 不可用，请检查配置")
            return await client.call_tool(raw_name, kwargs)

        spec = ToolSpec(
            name=prefixed_name,
            description=tool.get("description", ""),
            parameters=tool.get(
                "inputSchema", {"type": "object", "properties": {}}),
            source="mcp", connector_id=connector_id,
            destructive=self._guess_destructive(raw_name))
        self.registry.register_function(spec, _invoke)

    @staticmethod
    def _guess_destructive(name: str) -> bool:
        return any(k in name.lower() for k in
                   ("create", "delete", "update", "write", "remove", "merge", "push"))

    async def toggle(self, connector_id: str, enabled: bool) -> None:
        if enabled:
            self.db.execute(
                "UPDATE connectors SET status='connected' WHERE id=?", (connector_id,))
            try:
                await self.connect(connector_id)
            except Exception:  # noqa: BLE001
                self.db.execute(
                    "UPDATE connectors SET status='error' WHERE id=?", (connector_id,))
                raise
        else:
            self.db.execute(
                "UPDATE connectors SET status='disabled' WHERE id=?", (connector_id,))
            self.registry.unregister_connector(connector_id)
            client = self._clients.pop(connector_id, None)
            if client:
                await client.disconnect()

    async def update(self, connector_id: str, name: str, transport: str, config: dict,
                     timeout: int, tools_filter: dict | None) -> None:
        # 全行查询：_resolve_config 需读 config 列回填占位敏感值
        row = self.db.query_one("SELECT * FROM connectors WHERE id=?",
                                (connector_id,))
        # 处理 •••••• 占位：保留原敏感值；stdio 的 env 与 http 的 auth 同口径加密
        clean_config = dict(config)
        sensitive = {}
        old = self._resolve_config(row) if row else {}
        if transport == "stdio" and config.get("env"):
            real_env = {}
            for k, v in config["env"].items():
                real_env[k] = old.get("env", {}).get(k) if v == "••••••" else v
            sensitive["env"] = real_env
            clean_config["env"] = {k: "••••••" for k in real_env}
        if transport == "http" and config.get("auth", {}).get("value"):
            val = config["auth"]["value"]
            real_val = old.get("auth", {}).get(
                "value") if val == "••••••" else val
            if real_val:
                sensitive["auth_value"] = real_val
                clean_config["auth"] = {
                    **config["auth"], "value": "••••••"}
        cred_id = row["credential_id"] if row else None
        if sensitive:
            if cred_id:
                self.creds.update(cred_id,
                                  json.dumps(sensitive, ensure_ascii=False))
            else:
                # 原无凭证的连接器新增敏感值：创建凭证而非静默丢弃
                cred_id = self.creds.store(f"connector:{connector_id}", "connector",
                                           json.dumps(sensitive, ensure_ascii=False))
        self.db.execute(
            "UPDATE connectors SET name=?,transport=?,config=?,timeout=?,tools_filter=?,"
            "credential_id=? WHERE id=?",
            (name, transport, json.dumps(clean_config, ensure_ascii=False), timeout,
             json.dumps(tools_filter or {}, ensure_ascii=False), cred_id, connector_id))
        # 重连
        old_client = self._clients.pop(connector_id, None)
        if old_client:
            await old_client.disconnect()
        await self.connect(connector_id)

    async def delete(self, connector_id: str) -> None:
        row = self.db.query_one("SELECT credential_id FROM connectors WHERE id=?",
                                (connector_id,))
        client = self._clients.pop(connector_id, None)
        if client:
            await client.disconnect()
        self.registry.unregister_connector(connector_id)
        if row and row["credential_id"]:
            self.creds.delete(row["credential_id"])
        self.db.execute("DELETE FROM connectors WHERE id=?", (connector_id,))

    def list_connectors(self) -> list[dict]:
        rows = self.db.query_all(
            "SELECT * FROM connectors ORDER BY created_at")
        out = []
        for r in rows:
            tools = json.loads(r["tools_cache"] or "[]")
            out.append({
                "id": r["id"], "name": r["name"], "transport": r["transport"],
                "status": r["status"], "timeout": r["timeout"],
                "tools": tools, "tool_count": len(tools),
                "tools_filter": json.loads(r["tools_filter"] or "{}"),
                "created_at": r["created_at"],
            })
        return out

    async def reconnect_all(self) -> None:
        """启动时重连所有 status=connected 的连接器。"""
        for r in self.db.query_all("SELECT id FROM connectors WHERE status='connected'"):
            try:
                await self.connect(r["id"])
            except Exception:  # noqa: BLE001
                logger.warning("连接器 %s 重连失败", r["id"])
