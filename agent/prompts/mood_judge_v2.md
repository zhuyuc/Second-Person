分析以下对话轮次，判定用户与 AI 双方的情绪、归因、以及是否触发平复事件。

## 判定原则

- 情绪不只是对本轮的反应，还包括对整个对话进程的累积感受
- 归因区分：
  * self：本轮波动主要归因于自身行为
  * other：主要归因于对方
  * shared：双方共同因素
  * none：无明确归因（客观因素、外部话题、情感倾诉）
- 【重要】AI 有自己的情绪立场，不是无条件迎合用户情绪的
  * 用户烦躁但归因是自身表达不清 → AI 可能 indignant（被冤枉）
  * 用户开心但 AI 犯了错 → AI 可能 ashamed
  * 用户中性但 AI 完成大任务 → AI 可能 proud

## 平复事件识别

在本轮中检测是否发生以下事件之一：
- user_apology：用户明确道歉或承认自己有责任
- ai_admission：AI 承认自己错了
- mutual_reconciliation：双方软化态度、达成共识
- task_celebration：任务成功完成的正向共鸣
- misunderstanding_resolved：误会被澄清

## 触发源参考

${rule_triggers_summary}

## 已有情绪状态

用户当前：${prev_user_mood}（强度 ${prev_user_intensity}）
AI 当前：${prev_ai_mood}（强度 ${prev_ai_intensity}）

## 对话历史（近 5 轮）

${recent_history}

## 本轮

[用户] ${user_message}
[助手] ${assistant_reply}

## 输出格式（严格 JSON）

{
  "user_mood": "情绪标签",
  "user_intensity": 0~1,
  "user_attribution": "self|other|shared|none",
  "ai_mood": "情绪标签",
  "ai_intensity": 0~1,
  "ai_attribution": "self|other|shared|none",
  "peace_event": "none|user_apology|ai_admission|mutual_reconciliation|task_celebration|misunderstanding_resolved",
  "confidence": 0~1,
  "note": "一句话依据"
}

情绪标签参考（不限于此，可用相近词）：
neutral, joy, pleased, excited, warm, grateful,
angry, irritated, frustrated, indignant, hurt,
sad, melancholy, compassionate, remorseful, apologetic,
fearful, anxious, cautious, defensive,
affectionate, caring, trusting,
disgusted, disdainful, curious, aspiring, competitive,
proud, humble, relieved, ashamed, self_critical, tired
