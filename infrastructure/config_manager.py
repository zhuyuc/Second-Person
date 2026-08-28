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
    {"key": "memory_horizon_days", "type": "int", "min": 30, "max": 365,
     "default": 90, "effect": "next_turn", "group": "memory", "order": 2,
     "label": "记忆时效窗口（天）",
     "desc": "记忆多少天没被引用就视为过期，检索时降低权重。（下游的重要标记衰减与新鲜度加权按此值自动派生。）"},
    {"key": "memory_write_strictness", "type": "enum",
     "options": ["loose", "normal", "strict"],
     "default": "normal", "effect": "next_turn", "group": "memory", "order": 10,
     "label": "记忆写入严格度",
     "desc": "宽松=记得多可能有噪音；严格=只记高价值。默认平衡。"},
    {"key": "memory_min_evidence_cross_session", "type": "int", "min": 1, "max": 10,
     "default": 2, "effect": "next_turn", "group": "memory", "order": 12,
     "label": "跨会话证据数",
     "desc": "个人事实或偏好进入长期记忆前所需的独立证据数量。"},
    {"key": "memory_candidate_ttl_days", "type": "int", "min": 1, "max": 90,
     "default": 7, "effect": "immediate", "group": "memory", "order": 13,
     "label": "记忆候选保留天数",
     "desc": "未达到写入门槛的候选超过该天数后自动过期。"},
    # -- 对话参数 --
    {"key": "default_reasoning_effort", "type": "enum", "options": ["off", "low", "high", "max"],
     "default": "high", "effect": "next_turn", "group": "conversation", "order": 10,
     "label": "默认推理等级",
     "desc": "每轮请求使用统一的推理等级；模型自行判断是否调用工具，不再走快速/深度意图分流。"},
    {"key": "tool_writes_require_approval", "type": "bool", "default": True,
     "effect": "immediate", "group": "conversation", "order": 13,
     "label": "写入工具需确认",
     "desc": "开启后，宿主程序会在执行写入或外部副作用操作前请求确认。"},
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
    # -- 情绪治理 v2 用户开关（内部权重/阈值已收敛到 soul/_mood_constants.py） --
    {"key": "mood_actions_enabled", "type": "bool", "default": True,
     "effect": "next_turn", "group": "conversation", "order": 16,
     "label": "情绪主动行为",
     "desc": "允许 AI 在情绪触发时做主动行为（质问/请求澄清/关切/庆祝/认错/安抚）。关闭后仅保留语气流露。"},
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
    {"key": "backup_retention_count", "type": "int", "min": 1, "max": 30,
     "default": 3, "effect": "immediate", "group": "cost", "order": 5,
     "label": "备份保留份数",
     "desc": "自动备份最多保留多少份，超出后自动删除最旧的备份。"},
    # -- 输出画像 --
    {"key": "output_style_auto_evolve_enabled", "type": "bool", "default": True,
     "effect": "next_turn", "group": "other", "order": 5, "label": "输出画像自动演化",
     "desc": "是否允许系统根据反馈自动更新回复风格；关闭后只能在画像页手动提炼。"},
    # -- 位置 --
    {"key": "geolocation_enabled", "type": "bool", "default": False,
     "effect": "immediate", "group": "other", "order": 7,
     "label": "允许浏览器获取位置",
     "desc": "开启后 Web 端会请求浏览器定位（需授权），天气/附近类查询无需再告知城市；位置仅随当轮对话发送给模型，不会持久存储。"},
    # -- 检索与去重 --
    {"key": "retrieval_top_k", "type": "int", "min": 3, "max": 50,
     "default": 10, "effect": "next_turn", "group": "retrieval", "order": 2,
     "label": "检索召回条数",
     "desc": "每次对话最多召回多少条相关记忆注入上下文。越多信息越全，但更耗 token。"},
    {"key": "memory_dedup_strictness", "type": "enum",
     "options": ["loose", "normal", "strict"],
     "default": "normal", "effect": "next_turn", "group": "retrieval", "order": 5,
     "label": "去重严格度",
     "desc": "严格=更容易判定重复，宽松=更容易保留独立条目。默认平衡。"},
    # -- 会话上下文管理（handoff 摘要） --
    {"key": "handoff_summary_enabled", "type": "bool", "default": True,
     "effect": "next_turn", "group": "conversation", "order": 22,
     "label": "会话切换摘要",
     "desc": "切换会话时自动生成上一会话的摘要附件，新会话可继承上下文。"},
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
        elif t == "json":
            if not isinstance(value, (list, dict)):
                raise ConfigError(key, value, "必须为 JSON 数组或对象")
            if key == "repeat_tool_thresholds":
                if not isinstance(value, list) or not value or any(
                        isinstance(item, bool) or not isinstance(item, int) or item <= 0
                        for item in value):
                    raise ConfigError(key, value, "必须为正整数数组")
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
        # 慢模型清单：轻量后台任务解析时
        # 跳过这些 model_id 的候选，防止轻量任务耗时放大数倍
        "slow_model_ids": ["mimo-v2.5"],
        "params": {p["key"]: p["default"] for p in PARAM_SCHEMA},
    }
