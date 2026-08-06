"""
ConfigManager —— 集中管理 config.yaml 的所有可调参数。

设计要点（对齐产品文档 §ConfigManager 与开发文档 §3.4）：
- 全部可调参数声明 type / 值域 / 默认值 / 生效时机 / 分组，前端据此动态渲染表单
- 参数校验在保存时统一执行，非法值直接拒绝并返回具体原因（不保存后运行时报错）
- 生效时机三类：immediate（立即） / next_turn（下一轮对话） / next_session（下次会话）
- 支持默认值 + 用户覆盖；用户调参不需改代码
- 非参数配置（providers、onboarding_completed 等）也存 config.yaml，但不进 PARAM_SCHEMA
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# 参数 schema（开发文档 §3.4 参数清单 + 校验规则表）
# effect: immediate / next_turn / next_session
# group : memory / conversation / cost / retrieval / visualization / other
# ---------------------------------------------------------------------------
PARAM_SCHEMA: list[dict[str, Any]] = [
    # -- 记忆参数 --
    {"key": "passive_review_interval_days", "type": "int", "min": 1, "max": 30,
     "default": 3, "effect": "next_turn", "group": "memory", "order": 1,
     "label": "被动记忆间隔（天）",
     "desc": "每隔多少天自动回顾近期对话并提炼新记忆。间隔越短记忆越及时，后台开销越大。"},
    {"key": "vector_threshold", "type": "float", "min": 0.0, "max": 1.0,
     "default": 0.55, "effect": "next_turn", "group": "retrieval", "order": 1,
     "label": "向量检索阈值",
     "desc": "记忆与当前问题的语义相似度达到该值才会被召回。越高越精准，过高可能漏召回。"},
    {"key": "stale_detection_days", "type": "int", "min": 7, "max": 365,
     "default": 90, "effect": "next_turn", "group": "memory", "order": 2,
     "label": "过期检测天数",
     "desc": "一条记忆超过多少天未被使用就标记为“过期”，在检索时降低权重。"},
    {"key": "important_upgrade_count", "type": "int", "min": 1, "max": 100,
     "default": 3, "effect": "next_turn", "group": "memory", "order": 3,
     "label": "重要记忆升级阈值（次）",
     "desc": "一条记忆被引用达到该次数后升级为“重要”，不再轻易被判定过期。"},
    {"key": "stale_score_factor", "type": "float", "min": 0.1, "max": 1.0,
     "default": 0.7, "effect": "next_turn", "group": "memory", "order": 4,
     "label": "过期记忆降权系数",
     "desc": "过期记忆参与检索时的得分打折比例，越小越不易被召回（0.7 相当于打七折）。"},
    {"key": "important_memory_factor", "type": "float", "min": 1.0, "max": 2.0,
     "default": 1.3, "effect": "next_turn", "group": "memory", "order": 5,
     "label": "重要记忆加权系数",
     "desc": "被标记为重要的记忆在检索排序时的得分上浮倍率（1.3 相当于加 30% 权重）。"},
    # -- 对话参数 --
    {"key": "compression_trigger_rounds", "type": "int", "min": 4, "max": 100,
     "default": 20, "effect": "next_session", "group": "conversation", "order": 1,
     "label": "压缩触发轮数",
     "desc": "水位之后累积多少轮对话时，自动把较早的内容压缩成摘要。"},
    {"key": "head_protected_rounds", "type": "int", "min": 1, "max": 10,
     "default": 2, "effect": "next_session", "group": "conversation", "order": 2,
     "label": "开头保留轮数",
     "desc": "压缩时开头始终完整保留的对话轮数（通常是背景设定），不被摘要。"},
    {"key": "tail_protected_rounds", "type": "int", "min": 1, "max": 10,
     "default": 3, "effect": "next_session", "group": "conversation", "order": 3,
     "label": "结尾保留轮数",
     "desc": "压缩时结尾始终完整保留的最近对话轮数，保证紧邻上下文不丢。"},
    {"key": "compression_model_window", "type": "int", "min": 1000, "max": 2000000,
     "default": 80000, "effect": "next_session", "group": "conversation", "order": 4,
     "label": "压缩模型窗口基准",
     "desc": "压缩执行时用于校验压缩模型上下文窗口是否足够的基准值（需 ≥ 该值的 1.3 倍），不参与压缩触发。"},
    # -- 情绪 --
    {"key": "mood_enabled", "type": "bool", "default": True,
     "effect": "next_turn", "group": "conversation", "order": 4,
     "label": "情绪感知",
     "desc": "AI 感知用户情绪与自身情绪，在回复语气中自然体现。关闭后回复语气与关闭前完全一致。"},
    {"key": "mood_decay_hours", "type": "float", "min": 0.1, "max": 168,
     "default": 2.0, "effect": "next_turn", "group": "conversation", "order": 5,
     "label": "情绪衰减半衰期（小时）",
     "desc": "情绪随时间的消退速度，越短消退越快（2 小时表示强度每 2 小时减半）。"},
    {"key": "mood_influence_strength", "type": "float", "min": 0.0, "max": 1.0,
     "default": 0.5, "effect": "next_turn", "group": "conversation", "order": 6,
     "label": "情绪影响强度",
     "desc": "0 表示仅记录不注入（回复不受影响）；越大情绪在回复语气中体现越明显。"},
    {"key": "replan_max_per_request", "type": "int", "min": 0, "max": 10,
     "default": 3, "effect": "next_turn", "group": "conversation", "order": 7,
     "label": "Replan 最大次数",
     "desc": "单次请求中工具执行失败后最多允许几次重新规划（Replan），超出后放弃重试。"},
    {"key": "silent_doc_import", "type": "bool", "default": True,
     "effect": "next_turn", "group": "memory", "order": 5, "label": "文档静默导入",
     "desc": "上传文档后是否自动在后台解析并提炼为记忆，无需每次手动确认。"},
    {"key": "image_parse_engine", "type": "enum", "options": ["vlm", "ocr", "off"],
     "default": "vlm", "effect": "next_turn", "group": "memory", "order": 6,
     "label": "图片解析引擎",
     "desc": "上传图片时如何提取内容：vlm=视觉大模型（可识文字+理解图表，需模型支持视觉）；ocr=本地 OCR（仅识文字、离线）；off=不解析仅缓存。"},
    # -- 本地目录接入 --
    {"key": "local_dir_scan_interval_hours", "type": "int", "min": 1, "max": 168,
     "default": 24, "effect": "immediate", "group": "memory", "order": 7,
     "label": "本地目录扫描间隔（小时）",
     "desc": "每隔多少小时自动扫描一次已接入的本地目录，发现新增或修改的文件并提炼为记忆。"},
    {"key": "local_dir_max_files_per_scan", "type": "int", "min": 1, "max": 500,
     "default": 50, "effect": "immediate", "group": "memory", "order": 8,
     "label": "本地目录单轮扫描文件上限",
     "desc": "每轮扫描最多处理多少个新增/变更文件，超出留待下轮，避免一次性导入过多长时间占用资源。"},
    {"key": "local_dir_include_images", "type": "bool", "default": False,
     "effect": "immediate", "group": "memory", "order": 9,
     "label": "本地目录扫描图片",
     "desc": "是否把目录中的图片（PNG/JPG 等）也纳入扫描并经 VLM/OCR 解析提炼。默认关闭以控制视觉模型成本。"},
    # -- 成本控制 --
    {"key": "daily_token_budget", "type": "int", "min": 0, "max": None,
     "default": 500000, "effect": "immediate", "group": "cost", "order": 1,
     "label": "每日 Token 预算",
     "desc": "每天允许消耗的 token 总量，用于用量预警。填 0 表示不限制。"},
    {"key": "monthly_token_budget", "type": "int", "min": 0, "max": None,
     "default": 10000000, "effect": "immediate", "group": "cost", "order": 2,
     "label": "每月 Token 预算",
     "desc": "每月允许消耗的 token 总量，用于用量预警。填 0 表示不限制。"},
    {"key": "budget_alert_ratio", "type": "int", "min": 0, "max": 100,
     "default": 80, "effect": "immediate", "group": "cost", "order": 3,
     "label": "预算告警比例（%）",
     "desc": "用量达到预算的该百分比时，用量进度条变红提示。"},
    {"key": "over_budget_strategy", "type": "enum", "options": ["remind_only"],
     "default": "remind_only", "effect": "immediate", "group": "cost", "order": 4,
     "label": "超预算策略",
     "desc": "超出预算后的处理方式，目前仅支持“仅提醒、不阻断对话”。"},
    {"key": "backup_retention_count", "type": "int", "min": 1, "max": 30,
     "default": 3, "effect": "immediate", "group": "cost", "order": 5,
     "label": "备份保留份数",
     "desc": "自动备份最多保留多少份，超出后自动删除最旧的备份。"},
    # -- 输出画像 --
    {"key": "output_style_review_interval_days", "type": "int", "min": 1, "max": 30,
     "default": 7, "effect": "next_turn", "group": "other", "order": 1,
     "label": "输出画像提炼间隔（天）",
     "desc": "每隔多少天根据你的点赞/点踩等反馈自动提炼一次回复风格偏好。"},
    {"key": "output_style_signal_batch_threshold", "type": "int", "min": 10, "max": 1000,
     "default": 100, "effect": "immediate", "group": "other", "order": 2,
     "label": "输出画像信号批阈值",
     "desc": "新增反馈信号累计达到该条数时，提前触发一次输出风格提炼。"},
    {"key": "output_style_signal_retention_days", "type": "int", "min": 7, "max": 365,
     "default": 90, "effect": "immediate", "group": "other", "order": 3,
     "label": "反馈信号保留天数",
     "desc": "你的回复反馈信号最多保留多少天，过期自动清理。"},
    {"key": "output_style_signal_window_days", "type": "int", "min": 7, "max": 90,
     "default": 30, "effect": "immediate", "group": "other", "order": 4,
     "label": "输出画像提炼窗口（天）",
     "desc": "每次提炼输出风格时只统计最近多少天内的反馈信号。"},
    {"key": "output_style_auto_evolve_enabled", "type": "bool", "default": True,
     "effect": "next_turn", "group": "other", "order": 5, "label": "输出画像自动演化",
     "desc": "是否允许系统根据反馈自动更新回复风格；关闭后只能在画像页手动提炼。"},
    # -- 位置 --
    {"key": "geolocation_enabled", "type": "bool", "default": False,
     "effect": "immediate", "group": "other", "order": 7,
     "label": "允许浏览器获取位置",
     "desc": "开启后 Web 端会请求浏览器定位（需授权），天气/附近类查询无需再告知城市；位置仅随当轮对话发送给模型，不会持久存储。"},
    # -- 缓存 --
    {"key": "vector_cache_max_mb", "type": "int", "min": 64, "max": 8192,
     "default": 512, "effect": "next_turn", "group": "other", "order": 6,
     "label": "向量缓存上限（MB）",
     "desc": "记忆向量在内存中缓存占用的上限，越大检索越快但更耗内存。"},
    # -- 检索与去重 --
    {"key": "retrieval_top_k", "type": "int", "min": 3, "max": 50,
     "default": 10, "effect": "next_turn", "group": "retrieval", "order": 2,
     "label": "检索召回条数",
     "desc": "每次对话最多召回多少条相关记忆注入上下文。越多信息越全，但更耗 token。"},
    {"key": "recall_fallback_threshold", "type": "float", "min": 0.0, "max": 1.0,
     "default": 0.35, "effect": "next_turn", "group": "retrieval", "order": 3,
     "label": "召回兜底阈值", "lt": "vector_threshold",
     "desc": "高阈值没召回到记忆时，用该较低阈值兜底召回，避免完全无记忆。须小于向量检索阈值。"},
    {"key": "bm25_relative_floor", "type": "float", "min": 0.0, "max": 1.0,
     "default": 0.3, "effect": "next_turn", "group": "retrieval", "order": 4,
     "label": "关键词检索相对下限",
     "desc": "关键词检索中得分低于最高分该比例的结果会被过滤，控制关键词召回质量。"},
    {"key": "dedup_merge_threshold", "type": "float", "min": 0.5, "max": 1.0,
     "default": 0.85, "effect": "next_turn", "group": "retrieval", "order": 5,
     "label": "去重合并阈值", "gt": "dedup_link_threshold",
     "desc": "两条记忆相似度达到该值时判定为重复并自动合并。须大于建链阈值。"},
    {"key": "dedup_link_threshold", "type": "float", "min": 0.0, "max": 1.0,
     "default": 0.6, "effect": "next_turn", "group": "retrieval", "order": 6,
     "label": "建链阈值",
     "desc": "两条记忆相似度达到该值（但未到合并阈值）时，建立关联引用而不合并。"},
    {"key": "lint_duplicate_threshold", "type": "float", "min": 0.5, "max": 1.0,
     "default": 0.9, "effect": "next_turn", "group": "retrieval", "order": 7,
     "label": "重复检测提示阈值", "gte": "dedup_merge_threshold",
     "desc": "健康检查中判定“疑似重复”并提示的相似度门槛。须不低于去重合并阈值。"},
    {"key": "rrf_k", "type": "int", "min": 1, "max": 200,
     "default": 60, "effect": "next_turn", "group": "retrieval", "order": 8,
     "label": "混合排序平滑系数",
     "desc": "向量与关键词两路检索结果融合排序时的平滑系数，值越大越弱化靠前项的优势。"},
    {"key": "personal_query_knowledge_factor", "type": "float", "min": 0.1, "max": 1.0,
     "default": 0.7, "effect": "next_turn", "group": "retrieval", "order": 9,
     "label": "个人问题下知识库降权系数",
     "desc": "问题明显指向个人信息（如“我的偏好”“你记得我…”）时，知识库条目的检索得分打折比例，避免知识噪音淹没个人记忆。"},
    {"key": "knowledge_query_memory_factor", "type": "float", "min": 0.1, "max": 1.0,
     "default": 0.85, "effect": "next_turn", "group": "retrieval", "order": 10,
     "label": "知识问题下个人记忆降权系数",
     "desc": "问题明显是查资料/查概念（如“什么是…”“文档里…”）时，个人记忆条目的检索得分打折比例，让知识库结果优先。"},
    # -- 可视化 --
    {"key": "graph_max_nodes", "type": "int", "min": 50, "max": 2000,
     "default": 300, "effect": "immediate", "group": "visualization", "order": 1,
     "label": "图谱最大节点数",
     "desc": "知识图谱一次最多渲染的记忆节点数，超出按权重截断以保证流畅。"},
    {"key": "graph_max_edges", "type": "int", "min": 100, "max": 20000,
     "default": 2000, "effect": "immediate", "group": "visualization", "order": 2,
     "label": "图谱最大边数",
     "desc": "知识图谱一次最多渲染的关联连线数，超出按权重截断。"},
    # -- 其他 --
    {"key": "session_queue_limit", "type": "int", "min": 1, "max": 10,
     "default": 3, "effect": "immediate", "group": "other", "order": 7,
     "label": "会话排队上限",
     "desc": "同一时刻允许排队等待处理的会话数，超出的新请求会被拒绝。"},
    {"key": "ingest_chunk_tokens", "type": "int", "min": 1000, "max": 32000,
     "default": 6000, "effect": "immediate", "group": "other", "order": 8,
     "label": "文档切分块大小（token）",
     "desc": "导入文档时按多大的 token 块切分，再逐块提炼记忆。过大易漏细节，过小更耗时。"},
    {"key": "web_fetch_timeout_seconds", "type": "int", "min": 5, "max": 120,
     "default": 15, "effect": "immediate", "group": "other", "order": 9,
     "label": "网页抓取超时（秒）",
     "desc": "联网抓取网页工具的最长等待时间，超时判定为失败。"},
    {"key": "im_message_max_chars", "type": "int", "min": 500, "max": 30000,
     "default": 4000, "effect": "immediate", "group": "other", "order": 11,
     "label": "IM 单条消息上限",
     "desc": "通过飞书/钉钉等渠道回复时单条消息的最大字符数，超出会自动分条发送。"},
    # -- 联网搜索 --
    {"key": "mimo_builtin_search_enabled", "type": "bool", "default": True,
     "effect": "next_turn", "group": "other", "order": 12,
     "label": "模型内置联网搜索（mimo）",
     "desc": "对话模型为小米 mimo 且已在 mimo 控制台开通联网插件时，联网问题由模型内置搜索（博查源）执行，质量更高但按次计费；关闭或非 mimo 模型时回退内置 Bing 搜索。"},
    {"key": "builtin_search_max_keyword", "type": "int", "min": 1, "max": 5,
     "default": 3, "effect": "next_turn", "group": "other", "order": 13,
     "label": "内置搜索关键词数上限",
     "desc": "一轮内置联网搜索最多并发的关键词数，直接影响联网插件计费次数与搜索覆盖面。"},
]

_SCHEMA_BY_KEY = {p["key"]: p for p in PARAM_SCHEMA}


class ConfigError(ValueError):
    """参数校验失败，携带字段/收到值/期望值供 400 响应使用。"""

    def __init__(self, field: str, received: Any, expected: str):
        self.field = field
        self.received = received
        self.expected = expected
        super().__init__(f"参数校验失败：{field} {expected}，收到 {received!r}")


class ConfigManager:
    """线程安全的配置读写单例封装。"""

    def __init__(self, config_path: str | Path):
        self._path = Path(config_path)
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}
        self.load()

    # ---- 加载 / 持久化 ----------------------------------------------------
    def load(self) -> None:
        with self._lock:
            if self._path.exists():
                self._data = yaml.safe_load(
                    self._path.read_text(encoding="utf-8")) or {}
            else:
                self._data = {}
            # 用默认值补齐缺失参数（幂等，不覆盖已存在项）
            params = self._data.setdefault("params", {})
            for spec in PARAM_SCHEMA:
                params.setdefault(spec["key"], spec["default"])
            # 清理 schema 中已移除的孤儿参数，避免前端回传时触发未知参数校验失败
            for k in [k for k in params if k not in _SCHEMA_BY_KEY]:
                del params[k]

    def save(self) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                yaml.safe_dump(self._data, allow_unicode=True,
                               sort_keys=False),
                encoding="utf-8",
            )

    # ---- 参数读取 ---------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        """读取一个参数值（先查 params，再查顶层非参数配置）。"""
        with self._lock:
            if key in self._data.get("params", {}):
                return self._data["params"][key]
            return self._data.get(key, default)

    def all_params(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data.get("params", {}))

    def schema(self) -> list[dict[str, Any]]:
        return [dict(p) for p in PARAM_SCHEMA]

    # ---- 顶层（非 schema）配置 --------------------------------------------
    def get_raw(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set_raw(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
        self.save()

    # ---- 参数写入（带校验） ----------------------------------------------
    def update_params(self, updates: dict[str, Any]) -> dict[str, Any]:
        """部分更新参数；先校验全部（含跨参数约束），全部通过才落盘。"""
        with self._lock:
            merged = dict(self._data.get("params", {}))
            for k, v in updates.items():
                if k not in _SCHEMA_BY_KEY:
                    raise ConfigError(k, v, "未知参数")
                merged[k] = self._coerce_and_check(_SCHEMA_BY_KEY[k], v)
            self._check_cross_constraints(merged)
            self._data["params"] = merged
        self.save()
        return merged

    def reset_defaults(self) -> dict[str, Any]:
        with self._lock:
            self._data["params"] = {p["key"]: p["default"]
                                    for p in PARAM_SCHEMA}
        self.save()
        return self.all_params()

    # ---- 校验内部实现 -----------------------------------------------------
    @staticmethod
    def _coerce_and_check(spec: dict[str, Any], value: Any) -> Any:
        t = spec["type"]
        key = spec["key"]
        if t == "int":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(key, value, "必须为整数")
            value = int(value)
            lo, hi = spec.get("min"), spec.get("max")
            if lo is not None and value < lo:
                raise ConfigError(key, value, f"必须 ≥ {lo}")
            if hi is not None and value > hi:
                raise ConfigError(key, value, f"必须 ≤ {hi}")
        elif t == "float":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(key, value, "必须为数字")
            value = float(value)
            lo, hi = spec.get("min"), spec.get("max")
            if lo is not None and value < lo:
                raise ConfigError(key, value, f"必须 ≥ {lo}")
            if hi is not None and value > hi:
                raise ConfigError(key, value, f"必须 ≤ {hi}")
        elif t == "bool":
            if not isinstance(value, bool):
                raise ConfigError(key, value, "必须为布尔值")
        elif t == "enum":
            if value not in spec["options"]:
                raise ConfigError(key, value, f"必须为 {spec['options']} 之一")
        return value

    def _check_cross_constraints(self, params: dict[str, Any]) -> None:
        """跨参数约束（开发文档 §3.4 校验规则表中的 '且必须' 条款）。"""
        for spec in PARAM_SCHEMA:
            key = spec["key"]
            if "lt" in spec and not params[key] < params[spec["lt"]]:
                raise ConfigError(
                    key, params[key], f"必须 < {spec['lt']}({params[spec['lt']]})")
            if "gt" in spec and not params[key] > params[spec["gt"]]:
                raise ConfigError(
                    key, params[key], f"必须 > {spec['gt']}({params[spec['gt']]})")
            if "gte" in spec and not params[key] >= params[spec["gte"]]:
                raise ConfigError(
                    key, params[key], f"必须 ≥ {spec['gte']}({params[spec['gte']]})")


def default_config() -> dict[str, Any]:
    """生成首次启动的默认 config.yaml 内容。"""
    return {
        "onboarding_completed": False,
        "port": 8000,
        "workspace_whitelist": [],
        "allow_private_network_fetch": False,
        "params": {p["key"]: p["default"] for p in PARAM_SCHEMA},
    }
