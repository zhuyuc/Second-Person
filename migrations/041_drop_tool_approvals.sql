-- 041: 移除工具审批机制。
-- 本地单用户场景下，agent 工具不再走沙箱/权限审批（详见 docs/ARCHITECTURE_RULES.md）。
-- 表和历史挂起 turn 在此一次性收敛，避免旧库启动后 turn 永远停留在 awaiting_approval。
UPDATE agent_turns
SET status='failed',
    end_reason='approval_removed',
    updated_at=strftime('%Y-%m-%dT%H:%M:%S', 'now'),
    ended_at=strftime('%Y-%m-%dT%H:%M:%S', 'now')
WHERE status IN ('awaiting_approval','awaiting_input');

DROP INDEX IF EXISTS idx_tool_approvals_turn_status;
DROP TABLE IF EXISTS tool_approvals;
