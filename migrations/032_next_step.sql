-- 032_next_step.sql
-- 下一步建议模块：conversations 表新增 next_step_shown 字段，
-- 存储当轮 AI 输出的建议句文本与锚点类型（独立于 response_strategy_json，不破坏瘦身设计）。
-- 结构：{"text": "...", "anchor_kind": "deepen|verify|extend|contrast"} 或 NULL（未出建议）。
ALTER TABLE conversations
ADD COLUMN next_step_shown TEXT;