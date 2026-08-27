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
from . import sensitivity as _sensitivity

_CHANNELS = {"explicit", "confirmable", "session_only", "knowledge", "system"}
_ALLOWED_ATTRIBUTIONS = {"verified", "inferred", "imported"}
# 时态/一次性上下文标记（T1-B 扩展）：中英双语，覆盖打算/意向/临时环境三类
# Python re 的 \b 在中文相邻场景失效（中英均属 \w），所以中文类不加 \b；
# 英文类保留 \b 以避免子串误命中（如 goingto 之外的连字符情况）。
_TEMPORARY_PATTERNS = (
    r"(今天|明天|后天|现在|这次|本轮|暂时|先这样|目前在|正在|待会|一会|等下|马上就)",
    r"(我准备|我打算|我计划|这次帮我|当前任务|本轮任务|这个项目|本次会议|这次出差)",
    r"(?i)\b(for now|right now|this time|this session|temporarily|going to|planning to|about to|for the moment)\b",
)
# 意向/推断词：命中即使 LLM 判 verified 也强制降为 inferred + confidence=low
_TENTATIVE_PATTERNS = (
    r"(我想|我考虑|我可能会|我大概会|我也许|想试试|考虑一下|考虑要不要|回头再看)",
    r"(感觉|貌似|应该是|听说|据说|我猜|大概|似乎|好像|印象中)",
    r"(?i)\b(maybe|perhaps|might|probably|possibly|i guess|i think|i feel|seems like|likely)\b",
)
# 用户否认信号：命中即触发对该会话最近入池候选的即时抑制（P1-D）
_DENIAL_PATTERNS = (
    r"(不对|不是这样|不是的|别乱说|你搞错了|记错了|你记错|我没(?:说|讲|提)过|"
    r"我可没(?:说|讲|提)|哪有|才不是|你听错了|完全错|胡说|错了)",
    r"(?i)\b(no i didn'?t|that'?s wrong|you'?re wrong|i never said|not true|"
    r"never happened|misheard|nope|incorrect)\b",
)


def has_denial_signal(text: str) -> bool:
    """检测用户是否在否认 AI 上轮说的内容（P1-D）。"""
    return any(re.search(p, text or "") for p in _DENIAL_PATTERNS)


# 显式记忆锚点：命中允许 explicitness 到 1.0（否则 LLM 只能到 0.5）
_EXPLICIT_ANCHORS = (
    r"(?:请)?记住|记一下|以后都|从今以后|以后请|永远|一直用|不要再|别再",
    r"(?i)\b(remember this|from now on|always use|always|never again|please note)\b",
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


# 停用词（用于语义指纹的词袋归一化），只列高频且无信息量的：
# 常见虚词/连接词/礼貌语，尽量小以免误合并
_STOPWORDS = frozenset({
    "的", "了", "在", "是", "我", "你", "他", "她", "它", "有", "和", "跟", "都",
    "也", "会", "要", "把", "给", "对", "从", "被", "让", "使", "还", "就",
    "这", "那", "什么", "怎么", "哪里", "为什么", "所以", "但是", "因为", "如果",
    "the", "a", "an", "is", "am", "are", "was", "were", "be", "been", "being",
    "of", "to", "in", "on", "at", "and", "or", "but", "if", "so", "as",
    "for", "with", "by", "from", "this", "that", "these", "those",
    "i", "you", "he", "she", "it", "we", "they", "my", "your", "his", "her",
})


def _content_tokens(text: str) -> list[str]:
    """抽取内容词：中文按 2-gram 滑窗、英文按单词切分，剔除停用词与纯数字。

    没引入 jieba，用 2-gram 保证同义改写（"喝黑咖啡" / "黑咖啡"）也能命中；
    停用词过滤压噪。
    """
    if not text:
        return []
    tokens: list[str] = []
    for chunk in re.findall(r"[一-鿿]+|[A-Za-z][A-Za-z\-']{1,}", text):
        if re.match(r"[A-Za-z]", chunk):
            low = chunk.lower()
            if low in _STOPWORDS or low.isdigit() or len(low) < 2:
                continue
            tokens.append(low)
        else:
            # 中文按 2-gram 滑窗（"我喜欢喝黑咖啡" → 我喜/喜欢/欢喝/喝黑/黑咖/咖啡）
            if len(chunk) < 2:
                continue
            for i in range(len(chunk) - 1):
                bigram = chunk[i:i + 2]
                if bigram in _STOPWORDS:
                    continue
                tokens.append(bigram)
    return tokens


def fingerprint(item: dict[str, Any]) -> str:
    """精确指纹：原文空白归一后 hash，同文表达完全去重。"""
    text = re.sub(r"\s+", " ", _text(item).strip().lower())[:2000]
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def content_bucket(item: dict[str, Any]) -> str:
    """语义聚合桶：抽内容词/中文 2-gram，取交集稳定核心词哈希。

    与 fingerprint 分开：fingerprint 精确、bucket 宽松。
    bucket 用于「不同表述的同一事实」的聚合、拒绝保护、负反馈聚合。
    没有内容词时返回空串（回退到 fingerprint 精确匹配）。

    实现：从 tokens 里取词频 top-6（去重后按字典序稳定选择），
    这样同主题不同措辞的 top-6 有大量重合 → bucket 会重合。
    """
    tokens = _content_tokens(_text(item))
    if not tokens:
        return ""
    # 按 token 频次排序，取前 6 个高频内容词
    counter: dict[str, int] = {}
    for t in tokens:
        counter[t] = counter.get(t, 0) + 1
    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:6]
    keys = sorted(t for t, _ in ranked)
    return hashlib.sha256("|".join(keys).encode("utf-8")).hexdigest()[:12]


def sensitivity_level(text: str) -> str:
    """委托给 memory.sensitivity（三档：none/medium/high）。

    历史签名保持返回字符串，medium 级也如实返回；调用方决定拒/脱敏。
    """
    return _sensitivity.detect_level(text)


def has_tentative_marker(text: str) -> bool:
    """命中意向/推断词 → 强制降为 inferred + low。"""
    return any(re.search(p, text or "") for p in _TENTATIVE_PATTERNS)


def has_explicit_anchor(text: str) -> bool:
    """命中显式记忆锚点（'记住/以后都/from now on'），explicitness 可到 1.0。"""
    return any(re.search(p, text or "") for p in _EXPLICIT_ANCHORS)


def _first_person_signals(text: str) -> int:
    """粗略计数第一人称/所属代词，用于 user_specificity 规则上限。"""
    if not text:
        return 0
    return len(re.findall(r"(?:我|我的|我在|我是|我做|我用|我常|my|i'm|i am|i'd|i've)", text, flags=re.IGNORECASE))


# P1-C：常见 LLM 兜底/模板串（用于识别"占位候选"）
_PLACEHOLDER_PATTERNS = (
    r"^(untitled|未命名|待补充|待完善|无标题|N/A|none|null|todo|待定)$",
    r"^(用户偏好未明|no content|placeholder|示例内容|测试记忆)$",
)


def is_well_formed(item: dict[str, Any]) -> bool:
    """判定候选是否结构完整，值得进入 gate 打分。

    要求：
    1. title/summary/detail 三项里至少两项非空且非模板占位；
    2. 全文包含至少 1 个实体（items.entities）或 1 个第一人称指代信号，
       否则很可能是 LLM 兜底"用户偏好未明"式模板。
    """
    fields = ("title", "summary", "detail")

    def _is_placeholder(v: str) -> bool:
        s = (v or "").strip().lower()
        if not s:
            return True
        return any(re.match(p, s, flags=re.IGNORECASE) for p in _PLACEHOLDER_PATTERNS)

    non_placeholder = sum(0 if _is_placeholder(item.get(f, "")) else 1 for f in fields)
    if non_placeholder < 2:
        return False
    text = _text(item)
    entities = item.get("entities") or []
    has_entity = any(bool(e) for e in entities) if isinstance(entities, list) else False
    has_first_person = _first_person_signals(text) > 0
    return has_entity or has_first_person


def derive_rule_signals(item: dict[str, Any]) -> dict[str, float]:
    """从文本模式派生 stability/reuse/user_specificity/explicitness 的**规则上限**。

    LLM 自评分再高，最终也不能超过这里给出的上限。这是防止 LLM 灌水
    stability=0.9 直接跳过 gate 阈值的核心校验。
    """
    text = _text(item)
    lowered = text.lower()
    temporary = any(re.search(p, text) for p in _TEMPORARY_PATTERNS)
    tentative = has_tentative_marker(text)
    explicit_anchor = has_explicit_anchor(text)
    first_person = _first_person_signals(text)
    entities_count = len(item.get("entities") or [])

    # stability：命中临时/意向词直接压顶；否则按属性给基线
    if temporary or tentative:
        stability_cap = 0.35
    elif item.get("attribution") == "verified":
        stability_cap = 0.7
    else:
        stability_cap = 0.5

    # reuse：意向/一次性不复用；短句子（<30 字）与偏泛内容降上限
    if temporary or tentative:
        reuse_cap = 0.35
    elif len(text) < 30:
        reuse_cap = 0.5
    else:
        reuse_cap = 0.75

    # user_specificity：第一人称信号 + 实体密度决定
    if first_person >= 2 or (first_person >= 1 and entities_count >= 1):
        specificity_cap = 0.9
    elif first_person >= 1:
        specificity_cap = 0.6
    elif entities_count >= 2:
        specificity_cap = 0.4
    else:
        specificity_cap = 0.25

    # explicitness：只有命中显式锚点才允许到 1.0；LLM 自评顶多到 0.5
    explicitness_cap = 1.0 if explicit_anchor else 0.5

    return {
        "stability_cap": stability_cap,
        "reuse_cap": reuse_cap,
        "user_specificity_cap": specificity_cap,
        "explicitness_cap": explicitness_cap,
        "temporary": temporary,
        "tentative": tentative,
        "explicit_anchor": explicit_anchor,
    }


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
    """计算可解释的长期记忆价值分。

    LLM 自评分只能作为“不超过规则上限”的建议值：`min(llm, rule_cap)`。
    不再给 attribution=verified 自动的 0.7 送分；explicit=True 保持
    explicitness=1.0（用户主动调用 memory_save 才有此特权）。
    """
    text = _text(item)
    caps = derive_rule_signals(item)

    def _cap(field: str, fallback: float, cap_key: str) -> float:
        raw = item.get(field)
        base = fallback if raw is None else float(raw)
        return min(1.0, max(0.0, min(base, caps[cap_key])))

    stability = _cap("stability", caps["stability_cap"], "stability_cap")
    reuse = _cap("reuse", caps["reuse_cap"], "reuse_cap")
    specificity_default = caps["user_specificity_cap"] if source_type == "memory" else 0.2
    specificity = _cap("user_specificity", specificity_default, "user_specificity_cap")
    if explicit:
        explicitness = 1.0
    elif caps["explicit_anchor"]:
        # 命中"请记住/以后都/from now on" → 视为用户显式意愿，explicitness=1.0
        explicitness = 1.0
    else:
        explicitness = _cap("explicitness", 0.25, "explicitness_cap")
    evidence = min(1.0, max(0.0, evidence_count / 2.0))
    temporary = 1.0 if caps["temporary"] else 0.0
    # high 才算“敏感风险”减分；medium 不打分惩罚（脱敏路径承担安全责任）
    sensitivity = 1.0 if sensitivity_level(text) == "high" else 0.0
    duplicate = min(1.0, max(0.0, negative_count / 2.0))
    parts = {
        "stability": stability,
        "reuse": reuse,
        "user_specificity": specificity,
        "explicitness": explicitness,
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
        # P1-C：空/占位候选拦截 —— title/summary/detail 至少两项非空非模板，
        # 且含至少 1 个实体或第一人称指代（否则很可能是 LLM 兜底产出的模板）
        if not is_well_formed(item):
            return GateDecision(False, channel, 0.0, "rejected",
                                "候选结构不完整（缺内容/实体/主体指代）", sensitivity_level(text))
        # T1-B：命中意向/推断词（"打算/我想/感觉/probably"）→ 强制降为 inferred + low，
        # 无论 LLM 判什么。这里直接修改 item 让下游 _write_memory 也拿到降级后的属性。
        if not explicit and has_tentative_marker(text) \
                and item.get("attribution") != "session_fact":
            item["attribution"] = "inferred"
            item["confidence"] = "low"
        sensitivity = (sensitivity_level(text)
                       if _cfg(self.config, "memory_sensitive_scan_enabled", True)
                       else "none")
        if sensitivity == "high":
            return GateDecision(False, channel, 0.0, "rejected", "命中敏感信息，禁止写入原文", sensitivity)
        if channel == "session_only":
            return GateDecision(False, channel, 0.0, "rejected", "仅当前会话有效，不进入长期记忆")
        score, _ = score_item(item, source_type, explicit, evidence_count, negative_count)
        if channel == "knowledge":
            # T1-C：knowledge 通道也过最低分门禁；文档里"描述第三人"的候选（低
            # user_specificity）会被这里自然拦下，不再裸奔进 L3
            knowledge_min = float(_cfg(
                self.config, "memory_knowledge_min_score", 55))
            if score < knowledge_min:
                return GateDecision(False, channel, score, "rejected",
                                    "知识条目复用价值不足（低于 knowledge 门槛）", sensitivity)
            return GateDecision(True, channel, score, "approved",
                                "外部知识进入知识记忆通道", sensitivity)
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
        """将候选幂等写入候选池，并合并同指纹证据。

        evidence 的 excerpt 在落库前一律过 sensitivity.redact_evidence：
        - high：excerpt 只保留 [REDACTED:...] 占位 + hash
        - medium：excerpt 用脱敏后版本落库（保留结构可审计）
        """
        now = now_cst().isoformat(timespec="seconds")
        fp = fingerprint(item)
        bucket = content_bucket(item)
        # P4-D：domain 每日入池上限。已 rejected 不计（不阻塞 rejected 记录）
        domain = item.get("domain", "general")
        daily_cap = int(_cfg(self.config, "memory_domain_daily_cap", 20))
        if daily_cap > 0 and domain and self.db:
            today = now[:10]
            try:
                cnt_row = self.db.query_one(
                    "SELECT COUNT(*) c FROM memory_write_candidates "
                    "WHERE domain=? AND SUBSTR(created_at,1,10)=? "
                    "AND status IN ('pending','approved','writing','written','deferred')",
                    (domain, today))
                if int((cnt_row or {}).get("c") or 0) >= daily_cap:
                    return None  # 静默限流；下次 distill 重试时自然重来
            except Exception:  # noqa: BLE001
                pass
        if evidence is not None:
            evidence = _sensitivity.redact_evidence(evidence)
            # 落库前把 session_id 塞进 evidence dict，供后续跨会话去重比对
            if session_id and not evidence.get("session_id"):
                evidence = {**evidence, "session_id": session_id}
        # 负反馈聚合改按 content_bucket（P1-A）：
        # 同一记忆的不同措辞被反复标记无关时，累计 negative_count 正确抑制候选
        negative_count = 0
        try:
            if bucket:
                # 按 memories 的 content_bucket 聚合 negative（memories 表未存
                # bucket，改从最近同 bucket 已写入记忆的 negative 上限取）
                bucket_neg = self.db.query_one(
                    "SELECT MAX(m.retrieval_negative_count) AS n FROM memories m "
                    "JOIN memory_write_candidates c ON c.written_memory_id=m.id "
                    "WHERE c.content_bucket=? AND m.retrieval_negative_count>0",
                    (bucket,))
                negative_count = int((bucket_neg or {}).get("n") or 0)
            if not negative_count:
                # 兜底：按 title 前缀 LIKE（旧口径，向后兼容）
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
        # 拒绝保护：先查语义 bucket，再兜底查 fingerprint（旧库无 bucket 也能生效）
        try:
            cutoff = (now_cst() - timedelta(days=30)).isoformat(timespec="seconds")
            rejected = None
            if bucket:
                rejected = self.db.query_one(
                    "SELECT 1 FROM memory_write_candidates "
                    "WHERE content_bucket=? AND status='rejected' AND updated_at>=? LIMIT 1",
                    (bucket, cutoff))
            if not rejected:
                rejected = self.db.query_one(
                    "SELECT 1 FROM memory_write_candidates "
                    "WHERE fingerprint=? AND status='rejected' AND updated_at>=? LIMIT 1",
                    (fp, cutoff))
            if rejected and not item.get("user_confirmed"):
                return None
        except Exception:  # noqa: BLE001
            pass
        # 聚合查找：优先按 bucket 找同语义候选，兜底按 fingerprint
        existing = None
        if bucket:
            existing = self.db.query_one(
                "SELECT * FROM memory_write_candidates "
                "WHERE content_bucket=? AND status IN ('pending','approved') "
                "ORDER BY updated_at DESC LIMIT 1", (bucket,))
        if not existing:
            existing = self.db.query_one(
                "SELECT * FROM memory_write_candidates "
                "WHERE fingerprint=? AND status IN ('pending','approved') "
                "ORDER BY updated_at DESC LIMIT 1", (fp,))
        evidence_rows = []
        if existing:
            try:
                evidence_rows = json.loads(existing["evidence_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                evidence_rows = []
            if evidence:
                # 独立证据以 session_id 为主键：同一会话内多次引用只算 1 条独立证据，
                # 避免"同一话题反复重复"被算作多次跨会话证据（产品方案 §4.2）
                evidence_session = str(
                    evidence.get("session_id") or session_id or "")
                existing_sessions = {
                    str(ref.get("session_id") or "")
                    for ref in evidence_rows if isinstance(ref, dict)
                }
                if evidence_session and evidence_session in existing_sessions:
                    pass  # 同 session 已有证据，不再累计
                else:
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
                "INSERT INTO memory_write_candidates(candidate_id,fingerprint,content_bucket,"
                "source_message_id,session_id,source_type,attribution,title,summary,detail,domain,"
                "score,stability,reuse,user_specificity,evidence_count,evidence_json,status,"
                "decision_reason,expires_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"cand_{uuid_hex()}", fp, bucket, message_id, session_id, source_type,
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
            "INSERT INTO memory_write_candidates(candidate_id,fingerprint,content_bucket,"
            "source_message_id,session_id,source_type,attribution,title,summary,detail,domain,"
            "score,stability,reuse,user_specificity,evidence_count,evidence_json,status,"
            "decision_reason,expires_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, fp, bucket, message_id, session_id, source_type,
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

    # ---- P1-D：用户否认语触发的即时抑制 ---------------------------------
    def suppress_recent_from_denial(self, session_id: str,
                                    minutes: int = 30) -> int:
        """用户在会话中说"不对/记错了/别乱说"时调用：把该会话最近 N 分钟入池
        的候选一律 rejected + 30 天保护。

        避免下轮回顾链把同一批被否认的候选又拾回。
        session_id 必须精确匹配（不做 bucket 传染，防止误杀不相关候选）。
        返回被抑制的候选数量。
        """
        if not session_id:
            return 0
        cutoff = (now_cst() - timedelta(minutes=minutes)
                  ).isoformat(timespec="seconds")
        rows = self.db.query_all(
            "SELECT candidate_id FROM memory_write_candidates "
            "WHERE session_id=? AND status IN ('pending','approved') "
            "AND created_at>=?", (session_id, cutoff))
        if not rows:
            return 0
        now = now_cst().isoformat(timespec="seconds")
        self.db.executemany(
            "UPDATE memory_write_candidates SET status='rejected', "
            "decision_reason='用户即时否认', updated_at=? WHERE candidate_id=?",
            [(now, r["candidate_id"]) for r in rows])
        return len(rows)

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
                    # P3-B：write_item 返回 None（例如 gate 二次评估还未过）
                    # → 标 deferred 而不是回到 pending，避免"取出→回落→再取出"死循环
                    self.db.execute(
                        "UPDATE memory_write_candidates SET status='deferred',"
                        "decision_reason='写入路径未通过 gate 二次校验',updated_at=? "
                        "WHERE candidate_id=?", (now, row["candidate_id"]))
            except Exception as exc:  # noqa: BLE001
                self.db.execute(
                    "UPDATE memory_write_candidates SET status='failed',decision_reason=?,updated_at=? "
                    "WHERE candidate_id=?", (str(exc)[:300], now, row["candidate_id"]))
        return written


def uuid_hex() -> str:
    import uuid
    return uuid.uuid4().hex[:16]
