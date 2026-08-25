"""长期记忆写入门禁与候选池。

事实提取和长期记忆写入是两个不同阶段。此模块只做确定性规则、评分、
候选状态机和敏感信息检查；模型负责的语义提取仍由 Distiller 完成。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from infrastructure.timeutil import now_cst

_CHANNELS = {"explicit", "confirmable", "session_only", "knowledge", "system"}
_ALLOWED_ATTRIBUTIONS = {"verified", "inferred", "imported"}
_TEMPORARY_PATTERNS = (
    r"\b(今天|明天|后天|现在|这次|本轮|暂时|先这样|目前在|正在)\b",
    r"(我准备|我打算|我计划|这次帮我|当前任务|本轮任务)",
)
_SENSITIVE_PATTERNS = (
    r"(?i)(api[_ -]?key|secret|access[_ -]?token|密码|口令|验证码|私钥)",
    r"(?i)(身份证|银行卡|信用卡|支付密码|护照号)",
    r"(?i)(sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{12,})",
)


@dataclass(frozen=True)
class GateDecision:
    """写入门禁结果。"""

    allowed: bool
    channel: str
    score: float
    status: str
    reason: str
    sensitivity: str = "none"


def _cfg(config: Any, key: str, default: Any) -> Any:
    getter = getattr(config, "get", None)
    return getter(key, default) if getter else default


def _text(item: dict[str, Any]) -> str:
    return " ".join(str(item.get(k) or "") for k in ("title", "summary", "detail"))


def fingerprint(item: dict[str, Any]) -> str:
    """生成跨轮次幂等指纹，避免重复表达不断新增候选。"""
    text = re.sub(r"\s+", " ", _text(item).strip().lower())[:2000]
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def sensitivity_level(text: str) -> str:
    return "high" if any(re.search(p, text or "") for p in _SENSITIVE_PATTERNS) else "none"


def classify_channel(item: dict[str, Any], source_type: str = "memory",
                     explicit: bool = False) -> str:
    """将提炼结果归入长期写入通道。"""
    if explicit:
        return "explicit"
    if source_type == "knowledge" or item.get("attribution") == "imported":
        return "knowledge"
    if source_type in {"mood_pattern", "system"}:
        return "system"
    raw = str(item.get("channel") or "").strip().lower()
    if raw in _CHANNELS:
        return raw
    if item.get("attribution") in {"verified", "inferred"}:
        return "confirmable"
    return "session_only"


def score_item(item: dict[str, Any], source_type: str = "memory",
               explicit: bool = False, evidence_count: int = 1,
               negative_count: int = 0) -> tuple[float, dict[str, float]]:
    """计算可解释的长期记忆价值分。"""
    text = _text(item)
    stability = float(item.get("stability", 0.7 if item.get("attribution") == "verified" else 0.35))
    reuse = float(item.get("reuse", 0.7 if item.get("attribution") == "verified" else 0.35))
    specificity = float(item.get("user_specificity", 0.8 if source_type == "memory" else 0.2))
    explicitness = 1.0 if explicit else float(item.get("explicitness", 0.25))
    evidence = min(1.0, max(0.0, evidence_count / 2.0))
    temporary = 1.0 if any(re.search(p, text) for p in _TEMPORARY_PATTERNS) else 0.0
    sensitivity = 1.0 if sensitivity_level(text) == "high" else 0.0
    duplicate = min(1.0, max(0.0, negative_count / 2.0))
    parts = {
        "stability": min(1.0, max(0.0, stability)),
        "reuse": min(1.0, max(0.0, reuse)),
        "user_specificity": min(1.0, max(0.0, specificity)),
        "explicitness": min(1.0, max(0.0, explicitness)),
        "evidence_quality": evidence,
        "temporariness": temporary,
        "sensitivity_risk": sensitivity,
        "duplicate_penalty": duplicate,
    }
    score = (25 * parts["stability"] + 25 * parts["reuse"]
             + 20 * parts["user_specificity"] + 15 * parts["explicitness"]
             + 15 * parts["evidence_quality"] - 25 * temporary
             - 30 * sensitivity - 20 * duplicate)
    return round(max(0.0, min(100.0, score)), 2), parts


class MemoryWriteGate:
    """统一执行长期记忆写入门禁和候选池状态流转。"""

    def __init__(self, db, config):
        self.db = db
        self.config = config

    def evaluate(self, item: dict[str, Any], source_type: str = "memory",
                 *, explicit: bool = False, evidence_count: int = 1,
                 negative_count: int = 0) -> GateDecision:
        channel = classify_channel(item, source_type, explicit)
        text = _text(item)
        if not text.strip():
            return GateDecision(False, channel, 0.0, "rejected", "候选内容为空")
        sensitivity = (sensitivity_level(text)
                       if _cfg(self.config, "memory_sensitive_scan_enabled", True)
                       else "none")
        if sensitivity == "high":
            return GateDecision(False, channel, 0.0, "rejected", "命中敏感信息，禁止写入原文", sensitivity)
        if channel == "session_only":
            return GateDecision(False, channel, 0.0, "rejected", "仅当前会话有效，不进入长期记忆")
        score, _ = score_item(item, source_type, explicit, evidence_count, negative_count)
        if channel == "knowledge":
            return GateDecision(True, channel, score, "approved", "外部知识进入知识记忆通道", sensitivity)
        min_score = float(_cfg(self.config, "memory_candidate_min_score", 70))
        auto_score = float(_cfg(self.config, "memory_auto_write_score", 85))
        if explicit:
            return GateDecision(True, channel, score, "approved", "用户明确要求保存", sensitivity)
        if score < min_score:
            return GateDecision(False, channel, score, "rejected", "长期复用价值不足")
        negative_threshold = int(_cfg(self.config, "memory_negative_suppress_count", 2))
        if negative_count >= negative_threshold:
            return GateDecision(False, channel, score, "pending", "近期检索负反馈，转人工确认", sensitivity)
        min_evidence = int(_cfg(self.config, "memory_min_evidence_cross_session", 2))
        if evidence_count >= min_evidence and score >= auto_score:
            return GateDecision(True, channel, score, "approved", "证据充分且达到自动写入阈值")
        return GateDecision(False, channel, score, "pending", "等待第二次独立证据或用户确认")

    def enqueue(self, item: dict[str, Any], source_type: str = "memory",
                *, session_id: str | None = None, message_id: int | None = None,
                evidence_count: int = 1, evidence: dict | None = None) -> str | None:
        """将候选幂等写入候选池，并合并同指纹证据。"""
        now = now_cst().isoformat(timespec="seconds")
        fp = fingerprint(item)
        negative_count = 0
        try:
            row = self.db.query_one(
                "SELECT MAX(retrieval_negative_count) AS n FROM memories m "
                "JOIN memories_fts f ON f.memory_id=m.id WHERE f.title LIKE ?",
                (f"%{str(item.get('title') or '')[:20]}%",))
            negative_count = int((row or {}).get("n") or 0)
        except Exception:  # noqa: BLE001
            negative_count = 0
        decision = self.evaluate(item, source_type, evidence_count=evidence_count,
                                 negative_count=negative_count)
        _, metrics = score_item(item, source_type, False, evidence_count, negative_count)
        try:
            rejected = self.db.query_one(
                "SELECT 1 FROM memory_write_candidates WHERE fingerprint=? "
                "AND status='rejected' AND updated_at>=? LIMIT 1",
                (fp, (now_cst() - timedelta(days=30)).isoformat(timespec="seconds")))
            if rejected and not item.get("user_confirmed"):
                return None
        except Exception:  # noqa: BLE001
            pass
        existing = self.db.query_one(
            "SELECT * FROM memory_write_candidates WHERE fingerprint=? "
            "AND status IN ('pending','approved') ORDER BY updated_at DESC LIMIT 1", (fp,))
        evidence_rows = []
        if existing:
            try:
                evidence_rows = json.loads(existing["evidence_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                evidence_rows = []
            if evidence:
                evidence_key = str(evidence.get("source_ref") or evidence.get("session_id") or evidence)
                existing_keys = {
                    str(ref.get("source_ref") or ref.get("session_id") or ref)
                    for ref in evidence_rows if isinstance(ref, dict)
                }
                if evidence_key not in existing_keys:
                    evidence_rows.append(evidence)
            count = max(int(existing["evidence_count"] or 0), len(evidence_rows), evidence_count)
            merged_item = dict(item)
            score, _ = score_item(merged_item, source_type, False, count,
                                  negative_count)
            revised = self.evaluate(merged_item, source_type, evidence_count=count,
                                    negative_count=negative_count)
            self.db.execute(
                "UPDATE memory_write_candidates SET evidence_count=?,evidence_json=?,"
                "score=?,status=?,decision_reason=?,updated_at=? WHERE candidate_id=?",
                (count, json.dumps(evidence_rows, ensure_ascii=False), score,
                 revised.status, revised.reason, now, existing["candidate_id"]))
            return existing["candidate_id"]
        if decision.status == "rejected":
            self.db.execute(
                "INSERT INTO memory_write_candidates(candidate_id,fingerprint,source_message_id,"
                "session_id,source_type,attribution,title,summary,detail,domain,score,"
                "stability,reuse,user_specificity,evidence_count,evidence_json,status,"
                "decision_reason,expires_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"cand_{uuid_hex()}", fp, message_id, session_id, source_type,
                 item.get("attribution", "verified"), item.get("title", ""),
                 item.get("summary", ""), item.get("detail", ""), item.get("domain", "general"),
                 decision.score, metrics["stability"], metrics["reuse"], metrics["user_specificity"],
                 evidence_count, json.dumps([evidence] if evidence else [], ensure_ascii=False),
                 "rejected", decision.reason, now, now, now))
            return None
        ttl = int(_cfg(self.config, "memory_candidate_ttl_days", 7))
        expires = (now_cst() + timedelta(days=ttl)).isoformat(timespec="seconds")
        cid = f"cand_{uuid_hex()}"
        self.db.execute(
            "INSERT INTO memory_write_candidates(candidate_id,fingerprint,source_message_id,"
            "session_id,source_type,attribution,title,summary,detail,domain,score,"
            "stability,reuse,user_specificity,evidence_count,evidence_json,status,"
            "decision_reason,expires_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, fp, message_id, session_id, source_type,
             item.get("attribution", "verified"), item.get("title", ""),
             item.get("summary", ""), item.get("detail", ""), item.get("domain", "general"),
             decision.score, metrics["stability"], metrics["reuse"], metrics["user_specificity"],
             evidence_count, json.dumps([evidence] if evidence else [], ensure_ascii=False),
             decision.status, decision.reason, expires, now, now))
        return cid

    def expire(self) -> int:
        """标记过期候选并返回数量。"""
        now = now_cst().isoformat(timespec="seconds")
        cur = self.db.execute(
            "UPDATE memory_write_candidates SET status='expired',updated_at=? "
            "WHERE status IN ('pending','approved') AND expires_at IS NOT NULL AND expires_at<?",
            (now, now))
        return int(getattr(cur, "rowcount", 0) or 0)

    def list_candidates(self, status: str = "pending", limit: int = 100) -> list[dict]:
        """返回候选池条目，供回顾 Agent 和记忆中心使用。"""
        limit = min(max(1, int(limit)), 500)
        if status == "all":
            return self.db.query_all(
                "SELECT * FROM memory_write_candidates ORDER BY updated_at DESC LIMIT ?",
                (limit,))
        return self.db.query_all(
            "SELECT * FROM memory_write_candidates WHERE status=? "
            "ORDER BY score DESC,updated_at DESC LIMIT ?", (status, limit))

    def confirm(self, candidate_id: str) -> bool:
        """用户确认候选，进入可写状态。"""
        now = now_cst().isoformat(timespec="seconds")
        cur = self.db.execute(
            "UPDATE memory_write_candidates SET status='approved',confirmed_at=?,"
            "updated_at=?,decision_reason='用户确认' WHERE candidate_id=? "
            "AND status IN ('pending','approved')", (now, now, candidate_id))
        return bool(getattr(cur, "rowcount", 0))

    def reject(self, candidate_id: str, reason: str = "用户拒绝") -> bool:
        """用户拒绝候选，后续同指纹在保护窗口内不会自动写入。"""
        now = now_cst().isoformat(timespec="seconds")
        cur = self.db.execute(
            "UPDATE memory_write_candidates SET status='rejected',updated_at=?,"
            "decision_reason=? WHERE candidate_id=? AND status IN ('pending','approved')",
            (now, reason[:300], candidate_id))
        return bool(getattr(cur, "rowcount", 0))

    async def promote_ready(self, distiller, limit: int = 50) -> int:
        """消费证据充分或用户确认的候选，幂等写入 L3。"""
        self.expire()
        min_evidence = int(_cfg(self.config, "memory_min_evidence_cross_session", 2))
        rows = self.db.query_all(
            "SELECT * FROM memory_write_candidates WHERE status IN ('pending','approved') "
            "AND (confirmed_at IS NOT NULL OR evidence_count>=?) "
            "ORDER BY score DESC,updated_at ASC LIMIT ?", (min_evidence, limit))
        written = 0
        for row in rows:
            now = now_cst().isoformat(timespec="seconds")
            self.db.execute(
                "UPDATE memory_write_candidates SET status='writing',updated_at=? "
                "WHERE candidate_id=? AND status IN ('pending','approved')", (now, row["candidate_id"]))
            try:
                item = {"title": row["title"], "summary": row["summary"],
                        "detail": row["detail"], "domain": row["domain"],
                        "attribution": row["attribution"],
                        "evidence_count": row["evidence_count"],
                        "write_score": row["score"], "write_channel": "confirmable",
                        "sensitivity_level": row.get("sensitivity_level", "none"),
                        "expires_at": row.get("expires_at"),
                        "evidence_refs": json.loads(row["evidence_json"] or "[]")}
                mid = await distiller.write_item(item, source_type=row["source_type"],
                                                force_write=True)
                if mid:
                    self.db.execute(
                        "UPDATE memory_write_candidates SET status='written',written_memory_id=?,"
                        "updated_at=? WHERE candidate_id=?", (mid, now, row["candidate_id"]))
                    written += 1
                else:
                    self.db.execute(
                        "UPDATE memory_write_candidates SET status='pending',updated_at=? "
                        "WHERE candidate_id=?", (now, row["candidate_id"]))
            except Exception as exc:  # noqa: BLE001
                self.db.execute(
                    "UPDATE memory_write_candidates SET status='failed',decision_reason=?,updated_at=? "
                    "WHERE candidate_id=?", (str(exc)[:300], now, row["candidate_id"]))
        return written


def uuid_hex() -> str:
    import uuid
    return uuid.uuid4().hex[:16]
