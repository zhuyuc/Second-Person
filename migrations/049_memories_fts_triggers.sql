-- 049: memories_fts 触发器兜底同步。
--
-- memories_fts 是"非"外部内容 FTS（不是 content=memories 模式）：
--   CREATE VIRTUAL TABLE memories_fts USING fts5(
--       memory_id, project_id UNINDEXED, title, summary, detail, domain
--   );
-- 其中 detail 只能从 md 文件层读取（memories 表不存 detail 字段），
-- 因此不能用标准 external-content 三触发器模式，而是靠 palace.sync_fts
-- 应用层双写。
--
-- 现状：所有已知的 UPDATE title/summary/domain/project_id 路径都显式调用
-- palace.sync_fts（如 memory/lint.py 的 drift 修复）。触发器仅作**兜底**，
-- 防止未来新增代码遗漏 sync_fts 导致 FTS 漂移。
--
-- 关键设计：
-- 1) 不加 INSERT 触发器 —— 首次入库由 palace.upsert_index + sync_fts 一并处理，
--    额外触发器会导致 fts 表出现两行（无唯一约束）。
-- 2) UPDATE 触发器用 UPDATE memories_fts SET ... 而不是 DELETE+INSERT，
--    这样不会覆盖 sync_fts 已写入的 detail 字段。
-- 3) UPDATE 只在展示字段真正变化时才触发（用 IS 语义处理 NULL 比较）。
-- 4) DELETE 触发器兜底清 fts 行，与 palace.delete_all_indexes 的显式 DELETE
--    幂等叠加（DELETE ... WHERE memory_id=? 命中空集合不报错）。

CREATE TRIGGER IF NOT EXISTS mem_au_sync_fts
AFTER UPDATE OF title, summary, domain, project_id ON memories
FOR EACH ROW
WHEN new.title IS NOT old.title
  OR new.summary IS NOT old.summary
  OR new.domain IS NOT old.domain
  OR new.project_id IS NOT old.project_id
BEGIN
    UPDATE memories_fts
    SET title = new.title,
        summary = COALESCE(new.summary, ''),
        domain = new.domain,
        project_id = new.project_id
    WHERE memory_id = new.id;
END;

CREATE TRIGGER IF NOT EXISTS mem_ad_sync_fts
AFTER DELETE ON memories
FOR EACH ROW
BEGIN
    DELETE FROM memories_fts WHERE memory_id = old.id;
END;
