-- ============================================================
-- 046 沙箱下沉到会话层（v6）
-- - 废除 legacy-workspace 档位，老数据全部归一到 workspace-write
-- - 覆盖三处入口：sessions.sandbox_mode / projects.sandbox_mode
--   / session_policy_events.payload（JSON 内嵌，用字符串替换归一）
-- - 未设 sandbox_mode 的会话保持 NULL，运行时由 PolicyStore 默认 workspace-write
-- 无破坏性回退：legacy-workspace 与 workspace-write 的 policy 输出等价
-- ============================================================

-- 1) sessions.sandbox_mode 归一
UPDATE sessions
   SET sandbox_mode = 'workspace-write'
 WHERE sandbox_mode = 'legacy-workspace';

-- 2) projects.sandbox_mode 归一（历史脏数据保险）
UPDATE projects
   SET sandbox_mode = 'workspace-write'
 WHERE sandbox_mode = 'legacy-workspace';

-- 3) session_policy_events.payload 归一（JSON 字段里的 mode 值）
UPDATE session_policy_events
   SET payload = REPLACE(payload,
                         '"mode": "legacy-workspace"',
                         '"mode": "workspace-write"')
 WHERE event_type = 'sandbox_mode_change'
   AND payload LIKE '%"mode": "legacy-workspace"%';
