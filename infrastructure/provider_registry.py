"""
Provider 注册表 —— 从 providers / model_assignment / credentials 解析 ProviderSnapshot。

任务-模型分配（开发文档 §3.2 各环节使用哪个模型）：
  chat_model      → 第 4/6/7 步主链路
  agent_model     → 第 3 步第 2 层精筛、5 类系统 Agent、标题生成（未配回退 chat）
  intent_model    → 第 4 步意图识别 + 收敛分析 + 情绪判定 + 追问决策（未配回退 agent→chat）
  deep_analysis   → 深度问题模型、需求覆盖质量修复与长文分节交付（未配回退 agent→chat）
  embedding_model → 所有向量化
  vision          → 图片文字解析（未配回退 agent→chat）

槽位治理（TASK_SLOTS 单一事实来源）：
- 全部槽位统一注册：label/desc 供设置页清晰展示"这个槽位做什么"，fallback 定义回退链
- ensure_slot_assignments：启动时幂等补齐缺失槽位（仅补缺失，绝不覆盖用户配置）
- audit_slot_assignments：启动时健康检查（未配置/悬空引用/轻量槽位误配慢模型）汇总告警
- lightweight 槽位解析时跳过 slow_model_ids 慢模型候选，防止轻量任务耗时放大数倍
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .llm_provider import ProviderSnapshot
from infrastructure.timeutil import now_cst

logger = logging.getLogger("second_person.provider_registry")


@dataclass(frozen=True)
class TaskSlot:
    """任务槽位元数据（设置页展示与回退解析的单一事实来源）。"""
    key: str                        # 槽位 ID（model_assignment.task_type）
    label: str                      # 中文显示名（设置页展示）
    desc: str                       # 中文职责描述（设置页展示，须说明"这个槽位做什么"）
    fallback: tuple = ()            # 未显式配置时的回退链（按序尝试）
    lightweight: bool = False       # 轻量任务：解析时跳过 slow_model_ids 中的慢模型


# 槽位注册表（有序字典：设置页按此顺序渲染）
TASK_SLOTS: dict[str, TaskSlot] = {
    "chat": TaskSlot(
        key="chat",
        label="对话模型",
        desc="负责日常对话的回复生成与文档撰写，直接决定回答质量与语言风格。",
    ),
    "agent": TaskSlot(
        key="agent",
        label="系统 Agent 模型",
        desc="用于记忆蒸馏、上下文压缩、被动回顾、画像重建、标题生成等系统后台任务。",
        fallback=("chat",),
    ),
    "intent": TaskSlot(
        key="intent",
        label="意图识别模型",
        desc="解析用户消息的意图，决定后续检索与工具编排；同时承担收敛分析（注意力聚焦、缺口检测）、情绪判定与追问决策等轻量任务；建议配非推理小模型，可大幅降低每轮对话的首响应延迟。",
        fallback=("agent", "chat"),
        lightweight=True,
    ),
    "deep_analysis": TaskSlot(
        key="deep_analysis",
        label="深度分析模型",
        desc="用于深度模式的问题建模、需求覆盖修复和长文分节交付。建议配置高质量模型；未配置时回退系统 Agent 或对话模型。",
        fallback=("agent", "chat"),
    ),
    "embedding": TaskSlot(
        key="embedding",
        label="Embedding 模型",
        desc="将记忆与知识库内容向量化，支撑语义检索；切换后需重新向量化全部记忆。",
    ),
    "vision": TaskSlot(
        key="vision",
        label="视觉模型",
        desc="用于知识库图片与文档内嵌图的文字解析，让图片内容可被检索与引用。",
        fallback=("agent", "chat"),
    ),

}


class ProviderRegistry:
    def __init__(self, db, credential_store,
                 slow_model_ids: set[str] | None = None):
        self.db = db
        self.creds = credential_store
        # 慢模型清单（config.yaml slow_model_ids）：轻量槽位解析时跳过这些候选
        self.slow_model_ids = set(slow_model_ids or [])

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
            input_price=row["input_price"],   # 未配置保留 None，费用不计入
            output_price=row["output_price"],
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
        """按 TASK_SLOTS 注册表解析快照：显式配置优先；未配置沿回退链解析，
        轻量槽位跳过慢模型候选。"""
        slot = TASK_SLOTS.get(task_type)
        if slot is None:
            logger.warning("未知 task_type=%s，拒绝解析（未注册于 TASK_SLOTS）", task_type)
            return None
        # 1) 显式配置优先：用户意图优先，即使慢模型也仅告警不拦截
        pid = self.assignment(task_type)
        if pid:
            snap = self.snapshot(pid)
            if snap is None:
                logger.warning("槽位 %s 配置的 provider %s 不存在或已删除", task_type, pid)
            elif slot.lightweight and snap.model_id in self.slow_model_ids:
                logger.warning("轻量槽位 %s 显式配置了慢模型 %s（%s），该任务耗时将放大数倍",
                               task_type, snap.model_id, pid)
            return snap
        # 2) 未显式配置：沿回退链自动解析；轻量槽位跳过慢模型候选
        fallback_snap: ProviderSnapshot | None = None
        for fb in slot.fallback:
            pid = self.assignment(fb)
            if not pid:
                continue
            snap = self.snapshot(pid)
            if snap is None:
                continue
            if fallback_snap is None:
                fallback_snap = snap  # 兜底：至少保留一个可用候选
            if slot.lightweight and snap.model_id in self.slow_model_ids:
                continue
            logger.debug("未配置 %s 模型，回退使用 %s 模型", task_type, fb)
            return snap
        if fallback_snap is not None:
            logger.warning("轻量槽位 %s 回退链候选均为慢模型，降级使用 %s",
                           task_type, fallback_snap.model_id)
        elif slot.fallback:
            logger.warning("task_type=%s 未配置且回退链无可解析候选", task_type)
        return fallback_snap


def ensure_slot_assignments(registry: ProviderRegistry) -> list[str]:
    """槽位配置幂等补齐：未显式配置的槽位按回退链第一可用候选自动写入。

    仅补齐缺失，绝不覆盖用户已有配置；补齐后槽位在设置页可见、可审计、可修改。
    返回本次补齐的槽位清单（无补齐时为空）。"""
    filled = []
    for slot in TASK_SLOTS.values():
        if registry.assignment(slot.key):
            continue
        for fb in slot.fallback:
            pid = registry.assignment(fb)
            if pid and registry.snapshot(pid):
                registry.set_assignment(slot.key, pid)
                logger.warning("槽位 %s（%s）未配置，已按回退链自动补齐为 %s 模型（%s）",
                               slot.key, slot.label, fb, pid)
                filled.append(slot.key)
                break
    return filled


def audit_slot_assignments(registry: ProviderRegistry) -> None:
    """槽位健康检查：未配置 / 悬空引用 / 轻量槽位误配慢模型，汇总为一条 WARNING。"""
    issues = []
    for slot in TASK_SLOTS.values():
        pid = registry.assignment(slot.key)
        if not pid:
            issues.append(f"{slot.key}=未配置")
            continue
        row = registry.db.query_one(
            "SELECT display_name, model_id FROM providers WHERE id=?", (pid,))
        if row is None:
            issues.append(f"{slot.key}=悬空引用({pid} 不存在)")
            continue
        if slot.lightweight and (row["model_id"] or "") in registry.slow_model_ids:
            issues.append(f"{slot.key}=轻量任务误配慢模型 {row['model_id']}")
    if issues:
        logger.warning("槽位健康检查发现 %d 个问题：%s", len(issues), "; ".join(issues))
