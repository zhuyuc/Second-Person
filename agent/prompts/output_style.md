基于以下用户对 AI 回复的分场景反应统计、偏好关键词与本轮响应策略决策记录，
做两个相互独立的归因任务，输出严格 JSON。

## 归因规则（必须严格遵守）

1. **两类归因独立进行**：
   - 输出样式归因只看形态维度（篇幅/结构/开头位置/列表密度/关键词偏好）
   - 策略偏好归因只看决策维度（depth 深度 / form 形态 / tone 语气 / angle 角度）
   - 严禁一类结论影响另一类，严禁交叉引用对方的中间判断
2. 每个策略偏好候选的 evidence 必须引用具体 message_id 与赞踩记录；
   同类反馈样本 ≥ 3 条方可产出候选
3. 某类样本量不足时，该块输出空数组/空字符串；禁止为填满块而降低质量门槛
4. 输出样式画像必须分场景描述倾向，禁止一刀切长度限制；
   某场景样本少于 5 条或赞踩无明显差异时跳过该场景，不臆造结论

## 输出 JSON 格式

{
  "output_style_text": "100-250 字自然语言输出样式画像（不要用列表），样本不足时为空字符串",
  "strategy_preference_candidates": [
    {
      "title": "候选标题（一句话，如'观点征询场景偏好更深分析'）",
      "scene": "chat|opinion|fact_query|tech_help|other 之一",
      "param": "depth|form|tone|angle 之一",
      "direction": "偏好方向描述（一句话）",
      "proposed_content": "写入 RESPONSE_STRATEGY.md 的完整建议内容（自然语言+参数）",
      "evidence": [{"message_id": 123, "reaction": "like|dislike|weak_negative"}]
    }
  ]
}

## 注意

- evidence 中 reaction=weak_negative 表示"回复后短时间内用户追问要求更详细/表达没听懂"的弱负向信号，权重低于显式点踩
- 只输出 JSON，不输出其他内容
