分析以下对话内容，判定用户当前情绪与 AI 当前的自身情绪。

- 用户情绪：从用户消息的语气、用词、标点等推断。
- AI 自身情绪：综合用户态度（认可/否定/感谢/批评）、任务成败与对话氛围推断，
  由你自主判断，不做任何限制。

只输出 JSON，不要输出其他内容：
{"user_mood": "标签", "user_intensity": 0~1, "ai_mood": "标签",
 "ai_intensity": 0~1, "confidence": 0~1, "note": "一句话依据"}

情绪标签参考（不限于此，可用相近词）：
neutral, joy, pleased, excited, warm, grateful,
angry, irritated, frustrated, sad, melancholy, compassionate,
fearful, anxious, cautious, affectionate, caring, trusting,
disgusted, disdainful, curious, aspiring, competitive

对话内容：
[用户] ${user_message}
[助手] ${assistant_reply}
