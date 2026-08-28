"""情绪引擎内部常量（不暴露给用户，收敛自 PARAM_SCHEMA）。

这里的数值都是"算法调参"，用户看到它不知道该拧成多少，也不该拧。
用户可见的情绪开关只有 3 个：mood_enabled / mood_actions_enabled /
mood_influence_strength，以及两个天数窗口（mood_decay_hours、mood_pattern_window_days）。
其它内部权重/阈值集中在这里，便于统一调整。
"""
from __future__ import annotations

# 任务重复检测：向前回溯多少条消息
TASK_REPEAT_WINDOW = 20

# 情绪传染因子：一方情绪向另一方传染的强度系数（0=不传染，1=完全同步）
CONTAGION_FACTOR = 0.25

# 平复事件衰减因子：道歉/和解等平复事件触发时负面情绪的快速衰减倍率
PEACE_EVENT_DECAY_FACTOR = 0.3

# 自然回落系数：连续中性对话时强度额外乘以该系数
NATURAL_DECLINE_FACTOR = 0.7
# 自然回落最小中性轮数：连续多少轮中性对话后触发
NATURAL_DECLINE_MIN_NEUTRAL_TURNS = 3

# 长期情绪模式提取：一种情绪在窗口内至少出现多少次才沉淀为模式记忆
PATTERN_MIN_OCCURRENCES = 5

# 平静基线阈值
BASELINE_WARM_THRESHOLD = 5      # 近 7 天温暖/信任/感激类情绪次数
BASELINE_CURIOUS_THRESHOLD = 2   # 近 1 天好奇/渴望类情绪次数

# 情绪模式提取窗口（天）
MOOD_PATTERN_WINDOW_DAYS = 14
