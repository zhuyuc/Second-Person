-- ============================================================
-- 006 知识图谱布局坐标（graph_layout）
-- 后端预计算 ForceAtlas 风格坐标，前端零布局直接渲染（v3.0 方案 C）
-- 惰性重算：实体集合变化时按需重算；夜间维护链兜底全量重算
-- ============================================================
CREATE TABLE IF NOT EXISTS graph_layout (
    entity_id TEXT PRIMARY KEY,
    -- 关联 memory_entities.entity_id
    x REAL NOT NULL,
    y REAL NOT NULL,
    updated_at TEXT NOT NULL
);