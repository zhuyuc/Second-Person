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
AGENT_MAX_STEPS = 8
TOOL_APPROVAL_TTL_MINUTES = 10
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
