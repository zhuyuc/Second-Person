"""记忆模块内部常量（不暴露给用户，收敛自 PARAM_SCHEMA）。

这里的数字都是"算法调参"，用户看到不知道该拧到多少，也不该拧。
凡是新增内部数字优先放这里，不要再往 PARAM_SCHEMA 加 key。
"""
from __future__ import annotations

# ---- 记忆写入治理（收敛自 CFG-C）----------------------------------------
# 记忆被引用达到该次数后升级为 "重要"，不再轻易被判定过期
IMPORTANT_UPGRADE_COUNT = 3
# 过期记忆参与检索时的得分打折比例（0.7 = 打七折）
STALE_SCORE_FACTOR = 0.7
# 重要记忆在检索排序时的得分上浮倍率
IMPORTANT_MEMORY_FACTOR = 1.3
# 同一记忆被标记无关达到该次数后抑制相似候选自动写入
NEGATIVE_SUPPRESS_COUNT = 2
# 用户否认信号触发的抑制时间窗口（分钟）
DENIAL_SUPPRESS_WINDOW_MINUTES = 30
# 单次 distill 调用产出的候选数量硬上限
DISTILL_ITEMS_CAP = 8
# 同一个 domain 每天可以进入候选池的候选数上限
DOMAIN_DAILY_CAP = 20
# 新鲜度加权：近期创建的记忆得分上浮倍率
FRESHNESS_BOOST_FACTOR = 1.15
# 提取器连续 N 次 JSON 修复失败后推送系统通知
JSON_REPAIR_ALERT_THRESHOLD = 5

# ---- 其它工程参数（收敛自 CFG-D）---------------------------------------
# 相同工具连续调用达到这些次数向模型注入检查进展的宿主提醒
REPEAT_TOOL_THRESHOLDS = (3, 5, 8)
# AI 回复后该时间窗内的用户追问计入弱负向反馈证据
STRATEGY_FOLLOWUP_WINDOW_SECONDS = 60
# 输出画像信号批阈值：新增反馈信号累计达到该条数时提前触发一次提炼
OUTPUT_STYLE_SIGNAL_BATCH_THRESHOLD = 100
# 输出画像提炼窗口
OUTPUT_STYLE_SIGNAL_WINDOW_DAYS = 30
# 同一时刻允许排队等待处理的会话数
SESSION_QUEUE_LIMIT = 3
# 导入文档时按多大的 token 块切分
INGEST_CHUNK_TOKENS = 6000

# ---- 边缘工程参数（收敛自 CFG-I）---------------------------------------
# v7 沙箱统一化 + 自动压缩后：AGENT_MAX_STEPS 由"正常路径截断"降为"防死循环兜底"。
# 深度 agentic 探索（读多个文件、跨模块综合回答）会自然跨 20+ 步，压力管控交给
# CompactionEngine（token 阈值触发），这里保留一个较高上限只防止真的失控循环。
AGENT_MAX_STEPS = 64

# ---- 自动压缩阈值（v7 CompactionEngine，对齐 dsh-compaction-basic）----------
# thresholdRatio：请求的 uncached_tokens 超过 context_window × 此比例时触发压缩
COMPACTION_THRESHOLD_RATIO = 0.8
# retainRatio：压缩时保留最近对话的 tokens 占 context_window 的比例
COMPACTION_RETAIN_RATIO = 0.2
# 一次压缩后若仍超阈值最多再压 N 次
COMPACTION_MAX_RETRIES = 1
HANDOFF_SUMMARY_TOKEN_LIMIT = 10000
OUTPUT_STYLE_REVIEW_INTERVAL_DAYS = 7
OUTPUT_STYLE_SIGNAL_RETENTION_DAYS = 90
LOCAL_DIR_MAX_FILES_PER_SCAN = 50
IM_MESSAGE_MAX_CHARS = 4000
WEB_FETCH_TIMEOUT_SECONDS = 15
VECTOR_CACHE_MAX_MB = 512
GRAPH_MAX_NODES = 300
GRAPH_MAX_EDGES = 2000
BM25_RELATIVE_FLOOR = 0.3
RRF_K = 60
RECALL_FALLBACK_THRESHOLD = 0.35
MOOD_PATTERN_WINDOW_DAYS = 14
OVER_BUDGET_STRATEGY = "remind_only"

# ---- 检索链路（收敛自 retriever.py 内联默认值）----------------------------
# LLM 精筛后主命中最多保留几条（configurable 也可覆盖）
RETRIEVAL_REFINE_MAX = 5
# 图扩展 seed 池大小：candidates 前 N 条作为 seed 参与图扩展
GRAPH_EXPAND_SEED_POOL = 10
# 图扩展节点入池后，与 seed 向量余弦门槛（低于此门槛的关联邻居直接砍掉）
GRAPH_EXPAND_SEED_THRESHOLD = 0.6
# 精筛未选中时，图邻居 top-N 作为「关联记忆」保留（仅在 chosen_ids 非空时生效）
GRAPH_EXTRA_RELATED_CAP = 3
# 实体共现召回的邻居数上限
GRAPH_ENTITY_NEIGHBOR_CAP = 30
# 共引召回的邻居数上限
GRAPH_CITATION_NEIGHBOR_CAP = 15
# 共引召回时间窗（天）
GRAPH_CITATION_WINDOW_DAYS = 30
# 图扩展三源合并后总量硬帽，避免大账户下失控
GRAPH_NEIGHBOR_HARD_CAP = 15
# 无向量的邻居（迁移期/老库）按 rrf_score 排序保留的最大条数
GRAPH_UNCOMPUTABLE_KEEP = 2
# 候选池送入 LLM 精筛的硬帽（v7 精筛提速：20→10）
# hybrid 预筛已经按 BM25/vector 分数排序，尾部（11-20 位）与用户 query 相关性显著低于头部，
# 送去精筛只会拖长 input tokens 与首字延迟；10 条已经覆盖强相关候选。
CANDIDATE_POOL_HARD_CAP = 10
# 候选池小于该数量时跳过 LLM 精筛，走 degrade_pick 兜底
# 设为 2 意味着"仅 1 个候选时才跳过"——省下 1 次 refine 调用，同时保留
# ≥2 个候选场景下 LLM 判断的契约（对齐 test_retriever_gates 期望）
RETRIEVAL_REFINE_MIN_CANDIDATES = 2
# 极短寒暄短路阈值：查询字符数 ≤ 此值且携带 context_text 又非回忆意图 → 跳过整条检索
MIN_QUERY_CHARS_FOR_CONTEXT = 3
# 精筛超时秒数
RETRIEVAL_REFINE_TIMEOUT_SECONDS = 10

# ---- v7 精筛结果 LRU cache（对齐"重生成/异常重试"场景省一次 LLM 调用）------
# key = (session_id, query, tuple(sorted(candidate_ids)))
RETRIEVER_REFINE_CACHE_SIZE = 128
RETRIEVER_REFINE_CACHE_TTL_SECONDS = 300   # 5 分钟：覆盖典型重生成窗口
# LLM 精筛判空时按候选池 top-K 写入 retrieval_negative_count（负样本反馈）
REFINE_NEGATIVE_FEEDBACK_TOP_K = 3
# LLM 精筛不可用时的相对得分兜底比例
REFINE_DEGRADE_SCORE_RATIO = 0.5
# 唯一候选且 vector 分 ≥ 此值时跳过 LLM 精筛（快路径）
REFINE_FAST_PATH_MIN_SCORE = 0.85
# top-1 相对 top-2 显著领先时的快路径门槛
REFINE_FAST_PATH_GAP_RATIO = 2.0
REFINE_FAST_PATH_GAP_MIN_SCORE = 0.75

# ---- 待确认记忆频次（收敛自 agent/core.py 硬编码 UX 规则）-----------------
# 用户消息 < 该字符数时不追加「待确认记忆」块（避免打扰"你好"这种寒暄）
LOW_CONFIRM_MIN_MSG_CHARS = 6

# ---- 时效感知（CFG-E 合并 3 个多少天为 memory_horizon_days）------------
# 用户可拧 memory_horizon_days（默认 90）；下面三个下游派生
HORIZON_DEFAULT_DAYS = 90


def stale_days(cfg) -> int:
    """多少天没用就 stale。"""
    return int(cfg.get("memory_horizon_days", HORIZON_DEFAULT_DAYS))


def important_decay_days(cfg) -> int:
    """重要标记多少天不用就衰减（时效窗口 / 3，最小 7 天）。"""
    return max(7, stale_days(cfg) // 3)


def freshness_boost_days(cfg) -> int:
    """新记忆多少天内享受加权（时效窗口 / 3，最小 7 天）。"""
    return max(7, stale_days(cfg) // 3)


# ---- 记忆写入严格度（CFG-F 合并 3 个分数为 memory_write_strictness enum）-
_WRITE_STRICTNESS_MAP = {
    # (candidate_min, auto_write, knowledge_min)
    # loose 用比 normal 更低的入池门槛与知识条目门槛；auto_write 与 normal 持平
    # （宽松只是"更容易进候选池观察"，不代表"更容易跳过复核直接写入"）
    "loose":  (45, 85, 40),
    "normal": (70, 85, 55),
    "strict": (80, 92, 65),
}


def write_strictness_thresholds(cfg) -> tuple[int, int, int]:
    level = cfg.get("memory_write_strictness", "normal")
    return _WRITE_STRICTNESS_MAP.get(level, _WRITE_STRICTNESS_MAP["normal"])


# ---- 去重严格度（CFG-G 合并 3 个阈值为 memory_dedup_strictness enum）----
_DEDUP_STRICTNESS_MAP = {
    # (merge, link, lint_hint)  严格度越高 = 阈值越低 = 更容易判定为重复
    "loose":  (0.90, 0.70, 0.95),
    "normal": (0.85, 0.60, 0.90),
    "strict": (0.80, 0.50, 0.85),
}


def dedup_thresholds(cfg) -> tuple[float, float, float]:
    level = cfg.get("memory_dedup_strictness", "normal")
    return _DEDUP_STRICTNESS_MAP.get(level, _DEDUP_STRICTNESS_MAP["normal"])
