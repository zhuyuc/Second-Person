"""
Provider 注册表 —— 从 providers / model_assignment / credentials 解析 ProviderSnapshot。

任务-模型分配（开发文档 §3.2 各环节使用哪个模型）：
  chat_model      → 第 4/6/7 步主链路
  agent_model     → 第 3 步第 2 层精筛、5 类系统 Agent、标题生成（未配回退 chat）
  intent_model    → 第 4 步意图识别（建议配非推理小模型；未配回退 agent→chat）
  embedding_model → 所有向量化
"""
from __future__ import annotations

import logging

from .llm_provider import ProviderSnapshot
from infrastructure.timeutil import now_cst

logger = logging.getLogger("second_person.provider_registry")


class ProviderRegistry:
    def __init__(self, db, credential_store):
        self.db = db
        self.creds = credential_store

    # ---- Provider CRUD ----------------------------------------------------
    def list_providers(self) -> list[dict]:
        rows = self.db.query_all("SELECT * FROM providers ORDER BY created_at")
        return [dict(r) for r in rows]

    def add_provider(self, pid: str, display_name: str, provider_type: str,
                     base_url: str, model_id: str, api_key: str,
                     input_price: float | None, output_price: float | None,
                     context_window: int) -> str:
        cred_id = self.creds.store(f"provider:{pid}", "connector", api_key)
        self.db.execute(
            "INSERT INTO providers(id,display_name,provider_type,base_url,model_id,"
            "credential_id,input_price,output_price,context_window,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,'healthy',?)",
            (pid, display_name, provider_type, base_url, model_id, cred_id,
             input_price, output_price, context_window,
             now_cst().isoformat(timespec="seconds")))
        return pid

    def update_provider(self, pid: str, fields: dict, api_key: str | None = None) -> None:
        row = self.db.query_one(
            "SELECT credential_id FROM providers WHERE id=?", (pid,))
        if not row:
            raise KeyError(pid)
        if api_key:
            self.creds.update(row["credential_id"], api_key)
        allowed = {"display_name", "provider_type", "base_url", "model_id",
                   "input_price", "output_price", "context_window", "status"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if sets:
            clause = ", ".join(f"{k}=?" for k in sets)
            self.db.execute(f"UPDATE providers SET {clause} WHERE id=?",
                            (*sets.values(), pid))

    def delete_provider(self, pid: str) -> None:
        row = self.db.query_one(
            "SELECT credential_id FROM providers WHERE id=?", (pid,))
        if row:
            self.creds.delete(row["credential_id"])
        self.db.execute("DELETE FROM providers WHERE id=?", (pid,))

    def next_provider_seq(self) -> int:
        row = self.db.query_one(
            "SELECT MAX(CAST(SUBSTR(id,6) AS INTEGER)) m"
            " FROM providers WHERE id LIKE 'prov_%'")
        return (row["m"] or 0) + 1

    # ---- 快照解析 ---------------------------------------------------------
    def snapshot(self, provider_id: str) -> ProviderSnapshot | None:
        row = self.db.query_one(
            "SELECT * FROM providers WHERE id=?", (provider_id,))
        if not row:
            return None
        api_key = self.creds.get(row["credential_id"]) or ""
        return ProviderSnapshot(
            provider_id=row["id"], provider_type=row["provider_type"],
            base_url=row["base_url"], api_key=api_key, model_id=row["model_id"],
            input_price=row["input_price"] or 0.0,
            output_price=row["output_price"] or 0.0,
            context_window=row["context_window"] or 128000)

    def assignment(self, task_type: str) -> str | None:
        row = self.db.query_one(
            "SELECT provider_id FROM model_assignment WHERE task_type=?", (task_type,))
        return row["provider_id"] if row else None

    def set_assignment(self, task_type: str, provider_id: str) -> None:
        self.db.execute(
            "INSERT INTO model_assignment(task_type,provider_id,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(task_type) DO UPDATE SET provider_id=excluded.provider_id, "
            "updated_at=excluded.updated_at",
            (task_type, provider_id, now_cst().isoformat(timespec="seconds")))

    def snapshot_for(self, task_type: str) -> ProviderSnapshot | None:
        """按任务类型解析快照；agent 未配则回退 chat；intent/vision/convergence
        未配则回退 intent→agent→chat。"""
        pid = self.assignment(task_type)
        if not pid and task_type == "agent":
            pid = self.assignment("chat")
            logger.info("未配置 agent 模型，回退使用 chat 模型")
        if not pid and task_type == "intent":
            pid = self.assignment("agent") or self.assignment("chat")
            logger.info("未配置 intent 模型，回退使用 agent/chat 模型")
        if not pid and task_type == "vision":
            pid = self.assignment("agent") or self.assignment("chat")
            logger.info("未配置 vision 模型，回退使用 agent/chat 模型")
        if not pid and task_type == "convergence":
            # 收敛分析（attention_focus/gap_detect）：轻量任务，优先走 intent，
            # 未配则回退 intent→agent→chat
            pid = self.assignment("intent") or self.assignment(
                "agent") or self.assignment("chat")
            logger.info("未配置 convergence 模型，回退使用 intent/agent/chat 模型")
        if not pid and task_type == "mood":
            # 情绪判定（mood_judge_v2）：轻量但语义敏感任务，
            # 专槽 DeepSeek-V4-Flash；未配则回退 mood→convergence→intent→agent→chat
            pid = (self.assignment("convergence") or self.assignment("intent")
                   or self.assignment("agent") or self.assignment("chat"))
            logger.info("未配置 mood 模型，回退使用 convergence/intent/agent/chat 模型")
        if not pid and task_type == "elicitation":
            # 追问判定（clarification_router / ask_user 补充决策）：轻量语义任务，
            # 优先走 intent 槽位（小模型足够），未配回退 intent→agent→chat
            pid = (self.assignment("intent")
                   or self.assignment("agent") or self.assignment("chat"))
            logger.info("未配置 elicitation 模型，回退使用 intent/agent/chat 模型")
        if not pid:
            return None
        return self.snapshot(pid)
