-- ============================================================
-- 026：响应策略快照（意图理解与响应质量优化方案 v3 §数据模型）
-- 策略引擎决策结果 + 元认知骨架随 assistant 消息落库：
--   - 反馈闭环归因经 message_id 关联读取（不新建 strategy_feedback 表）
--   - 只存归因所需的 5 个决策字段（瘦身版），可解释性信息进 Langfuse
-- ============================================================
ALTER TABLE conversations
ADD COLUMN response_strategy_json TEXT;
-- ResponseStrategy 瘦身快照：{"depth","form","tone","angle","complexity_score"}
ALTER TABLE conversations
ADD COLUMN cognitive_skeleton_json TEXT;
-- 元认知骨架全量（触发率低：complexity_score>=7 且非排除意图）