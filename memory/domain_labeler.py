"""
DomainLabeler —— 领域中文标签缓存（方案 B）。

领域名由 LLM 蒸馏动态产生（英文小写 slug），前端需展示中文：
- 内置种子映射覆盖常见领域，命中即不消耗 LLM 调用
- 新领域首次出现时（记忆写入触发 mark_dirty）异步调 LLM 翻译一次并入库
- /memory/domains 请求时对漏网领域兜底补翻（fire-and-forget）
- 本身为中文的领域名原样展示，不入缓存
"""
from __future__ import annotations

import asyncio
import logging
from infrastructure.timeutil import now_iso
from typing import Awaitable, Callable

logger = logging.getLogger("second_person.domain_labeler")

# 常见领域种子映射（键为规范化 slug：小写、连字符转下划线）
SEED_LABELS = {
    "ai": "人工智能",
    "computing": "计算机",
    "finance": "金融",
    "investment": "投资",
    "technology": "技术",
    "frontend_architecture": "前端架构",
    "storage_architecture": "存储架构",
    "system_configuration": "系统配置",
    "product_design": "产品设计",
    "product_development": "产品开发",
    "product_management": "产品管理",
    "project_management": "项目管理",
    "software_development": "软件开发",
    "web_development": "Web开发",
    "software_engineering": "软件工程",
    "data_science": "数据科学",
    "machine_learning": "机器学习",
    "security": "安全",
    "business": "商业",
    "marketing": "市场营销",
    "design": "设计",
    "education": "教育",
    "health": "健康",
    "lifestyle": "生活方式",
    "career": "职业发展",
    "psychology": "心理学",
    "general": "通用",
}

# LLM 翻译结果的标签长度上限（防脏输出污染缓存）
_MAX_LABEL_LEN = 16


def _norm(domain: str) -> str:
    return str(domain).strip().lower().replace("-", "_")


class DomainLabeler:
    """translate_fn: async (domains: list[str]) -> dict[domain, 中文标签]。"""

    def __init__(self, db,
                 translate_fn: Callable[[list[str]], Awaitable[dict]]):
        self.db = db
        self.translate_fn = translate_fn
        self._inflight: set[str] = set()
        self._seed()

    def _seed(self) -> None:
        now = now_iso()
        self.db.executemany(
            "INSERT OR IGNORE INTO domain_labels(domain,label,source,created_at) "
            "VALUES(?,?,?,?)",
            [(k, v, "seed", now) for k, v in SEED_LABELS.items()])

    # ---- 查询 -------------------------------------------------------------
    def _cached(self) -> dict[str, str]:
        rows = self.db.query_all("SELECT domain, label FROM domain_labels")
        return {r["domain"]: r["label"] for r in rows}

    def map_for(self, domains: list[str]) -> dict[str, str]:
        """返回 {原始领域名: 中文标签}；未缓存/本身中文的不在结果中。"""
        cached = self._cached()
        out: dict[str, str] = {}
        for d in domains:
            if not d:
                continue
            label = cached.get(d) or cached.get(_norm(d))
            if label:
                out[d] = label
        return out

    def _missing(self, domains: list[str]) -> list[str]:
        cached = self._cached()
        seen: set[str] = set()
        out: list[str] = []
        for d in domains:
            if not d or not str(d).isascii():
                continue  # 中文领域名原样展示，无需翻译
            if d in cached or _norm(d) in cached:
                continue
            if d in self._inflight or d in seen:
                continue
            seen.add(d)
            out.append(d)
        return out

    # ---- 翻译入库 ----------------------------------------------------------
    async def ensure(self, domains: list[str]) -> None:
        """对未缓存的英文领域批量调 LLM 翻译并入库；幂等、并发安全。"""
        need = self._missing(list(domains))
        if not need:
            return
        self._inflight.update(need)
        try:
            result = await self.translate_fn(need) or {}
            now = now_iso()
            rows = []
            for d in need:
                label = str(result.get(d) or result.get(
                    _norm(d)) or "").strip()
                if 0 < len(label) <= _MAX_LABEL_LEN:
                    rows.append((d, label, "llm", now))
            if rows:
                self.db.executemany(
                    "INSERT OR REPLACE INTO domain_labels(domain,label,source,"
                    "created_at) VALUES(?,?,?,?)", rows)
                logger.info("已翻译 %d 个新领域标签：%s",
                            len(rows), "、".join(r[0] for r in rows))
        except Exception:  # noqa: BLE001
            logger.warning("领域标签翻译失败：%s", need, exc_info=True)
        finally:
            self._inflight.difference_update(need)

    def schedule(self, *domains: str) -> None:
        """fire-and-forget 翻译；无运行中事件循环时静默跳过（下次请求兜底）。"""
        pending = [d for d in domains if d]
        if not pending:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.ensure(pending))
