-- 050_drop_elicitations.sql
-- 清理"追问式补充信息(elicitation)"功能的孤儿残留。
--
-- 背景：该功能已整体移除，除 033 迁移与本迁移外，全仓（py/js/vue/SSE/prompt）
-- 再无任何代码读写 `elicitations` 表或 `sessions.elicitation_blocked` 列。
-- 关联的提示词误导（base_rules_fs.md 让模型调用不存在的 ask_user_question 工具）
-- 已在代码侧一并修正。此处删除数据库层的死表 / 死列。

-- 孤儿表（3 个关联索引随 DROP TABLE 自动删除）
DROP TABLE IF EXISTS elicitations;

-- sessions 上无人使用的持久化标记列。
-- SQLite ≥ 3.35 支持 DROP COLUMN（本项目运行于 3.45）；该列不参与任何索引/约束，
-- 且迁移按序执行（033 先建列、050 再删列），故删除时列必然存在。
ALTER TABLE sessions DROP COLUMN elicitation_blocked;
