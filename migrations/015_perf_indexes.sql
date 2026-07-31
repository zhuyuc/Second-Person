-- ============================================================
-- 015：补齐高频查询路径缺失的索引（纯增量，安全）
-- 单写线程串行执行，索引维护开销可忽略；随数据增长避免全表扫描。
-- ============================================================
-- conversations：按时间范围的统计/清理/游标查询（原仅有 session_id 索引）
CREATE INDEX IF NOT EXISTS idx_conv_create_time ON conversations(create_time);
-- memories：stats/orphans/domains/过期检测/检索补元数据均按 lifecycle 过滤
CREATE INDEX IF NOT EXISTS idx_memories_lifecycle ON memories(lifecycle);
-- memories：领域分组（图谱/领域统计）与领域标签联动
CREATE INDEX IF NOT EXISTS idx_memories_domain ON memories(domain);
-- memories：low 待确认候选扫描（confidence + lifecycle + created_at 排序）
CREATE INDEX IF NOT EXISTS idx_memories_confidence ON memories(confidence, lifecycle);
-- token_usage：按时间聚合费用/用量统计
CREATE INDEX IF NOT EXISTS idx_token_usage_create_time ON token_usage(create_time);