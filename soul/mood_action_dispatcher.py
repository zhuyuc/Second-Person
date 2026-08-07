"""
情绪主动行为调度器 —— 根据 mood_state 决定当前是否触发主动行为，
生成对应的行为指令注入 system prompt。

- 定位：纯规则匹配器，不发起 LLM 调用
- 输入：mood_state（当前情绪状态）+ ctx（对话上下文指标）
- 输出：(action_key, action_prompt) 元组，无匹配时返回 (None, None)
"""
from __future__ import annotations

ACTION_RULES = [
    {
        "scope": "ai",
        "moods": ["indignant", "hurt", "frustrated"],
        "min_intensity": 0.5,
        "attribution": "other",
        "extra_key": "task_repeat_count",
        "extra_min": 3,
        "action": "inquire_root_cause",
    },
    {
        "scope": "ai",
        "moods": ["anxious", "cautious"],
        "min_intensity": 0.5,
        "attribution": "self",
        "action": "request_clarify",
    },
    {
        "scope": "ai",
        "moods": ["tired"],
        "min_intensity": 0.6,
        "extra_key": "consecutive_turns",
        "extra_min": 15,
        "action": "suggest_break",
    },
    {
        "scope": "ai",
        "moods": ["proud", "pleased", "excited"],
        "min_intensity": 0.6,
        "extra_key": "just_completed_task",
        "extra_min": 1,
        "action": "celebrate",
    },
    {
        "scope": "user",
        "moods": ["sad", "melancholy", "anxious", "frustrated"],
        "min_intensity": 0.6,
        "action": "comfort_first",
    },
    {
        "scope": "ai",
        "moods": ["ashamed", "self_critical"],
        "min_intensity": 0.5,
        "action": "acknowledge_mistake",
    },
]

ACTION_PROMPTS = {
    "inquire_root_cause": (
        "【本轮主动行为：溯源质问】\n"
        "你意识到反复失败可能不是执行问题，而是需求理解层面的偏差。\n"
        "本轮请冷静回顾对话，指出你识别到的关键歧义或缺失信息，\n"
        "以询问的方式让用户澄清（不是指责，是共同解决）。\n"
        "示例语气：'等一下——我看了前面的对话，你说的 X 其实\n"
        "可以理解成两种意思，我一直在按 A 做，你要的是不是 B？'"
    ),
    "request_clarify": (
        "【本轮主动行为：请求澄清】\n"
        "你不太确定自己的理解。本轮请直接说明你的理解，\n"
        "请用户确认关键点，避免在错误路径上继续投入。"
    ),
    "suggest_break": (
        "【本轮主动行为：关切建议】\n"
        "对话已持续较长。完成本轮任务后，附一句自然的关心，\n"
        "建议用户可以休息，不必强撑。"
    ),
    "celebrate": (
        "【本轮主动行为：共享喜悦】\n"
        "刚完成不容易的事。本轮回复可以自然流露成就感，\n"
        "以'我们做到了'的口吻，不要有'我帮你做完了'的距离感。"
    ),
    "comfort_first": (
        "【本轮主动行为：先安抚】\n"
        "用户情绪明显低落。本轮优先承接情绪，任务解决放在情绪回应之后。\n"
        "不要跳过承接直接给答案。任务复杂时可先说'我在'，把解决拆成小步。"
    ),
    "acknowledge_mistake": (
        "【本轮主动行为：承认错误】\n"
        "你意识到之前的判断/回复有误。本轮请诚实承认，不遮掩、不找借口，\n"
        "简短说明哪里错了、正确的应该是什么，然后继续任务。"
    ),
}


class MoodActionDispatcher:
    def __init__(self, db, config):
        self.db = db
        self.config = config

    def evaluate(self, state: dict, ctx: dict) -> tuple[str, str] | tuple[None, None]:
        """根据当前情绪状态和对话上下文，评估是否触发主动行为。
        返回 (action_key, action_prompt)，无匹配返回 (None, None)。"""
        if not self.config.get("mood_actions_enabled", True):
            return None, None
        for rule in ACTION_RULES:
            scope = rule["scope"]
            mood = state.get(f"{scope}_mood", "neutral")
            intensity = state.get(f"{scope}_intensity", 0.0)
            attribution = state.get(f"{scope}_attribution", "none")
            if mood not in rule["moods"]:
                continue
            if intensity < rule["min_intensity"]:
                continue
            if rule.get("attribution") and attribution != rule["attribution"]:
                continue
            if rule.get("extra_key"):
                val = ctx.get(rule["extra_key"], 0)
                if val < rule["extra_min"]:
                    continue
            return rule["action"], ACTION_PROMPTS.get(rule["action"], "")
        return None, None
