-- AI 回复思考过程持久化（意图理解/任务拆解/工具调用/模型原生推理）
ALTER TABLE conversations
ADD COLUMN thinking TEXT;