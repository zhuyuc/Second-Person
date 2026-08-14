-- 036: 合并轻量模型槽位（convergence / mood / elicitation → intent）
-- 设置页从 8 槽位精简为 5 槽位，三个轻量槽位合并到 intent。
-- 迁移策略：intent 已有配置则直接删除旧记录；
--           intent 未配置则取 convergence/mood/elicitation 中第一个有值的迁入，再删除旧记录。
-- 1) intent 未配置时，取第一个可用的轻量槽位配置迁入
INSERT INTO model_assignment(task_type, provider_id, updated_at)
SELECT 'intent',
    src.provider_id,
    src.updated_at
FROM (
        SELECT provider_id,
            updated_at
        FROM model_assignment
        WHERE task_type IN ('convergence', 'mood', 'elicitation')
            AND provider_id IS NOT NULL
            AND provider_id != ''
        ORDER BY CASE
                task_type
                WHEN 'convergence' THEN 1
                WHEN 'mood' THEN 2
                WHEN 'elicitation' THEN 3
            END
        LIMIT 1
    ) AS src
WHERE NOT EXISTS (
        SELECT 1
        FROM model_assignment
        WHERE task_type = 'intent'
            AND provider_id IS NOT NULL
            AND provider_id != ''
    );
-- 2) 删除已合并的旧槽位记录
DELETE FROM model_assignment
WHERE task_type IN ('convergence', 'mood', 'elicitation');