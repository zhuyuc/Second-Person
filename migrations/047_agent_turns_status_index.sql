-- 047: agent_turns 复合索引，加速 session_metrics 路由的
-- "最近一条已完成 turn" 查询。原 idx_agent_turns_session_created 只覆盖
-- (session_id, created_at DESC)，status='completed' 过滤需回表扫；
-- 大 session 下 completed/failed/running 混排时扫描成本线性放大。
CREATE INDEX IF NOT EXISTS idx_agent_turns_session_status_created
ON agent_turns(session_id, status, created_at DESC);
