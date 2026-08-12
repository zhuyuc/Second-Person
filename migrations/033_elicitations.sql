-- 033_elicitations.sql
-- 追问式补充信息模块（elicitation）：存储追问瞬态交互数据（仅 SQLite，不落 md）。
--
-- 字段说明：
--   id              使用 tool_use_id 作为主键
--   session_id      关联会话
--   questions_json  ask_user 工具入参原样落盘
--   answers_json    前端提交的结构化答案（一次性提交全部题目）
--   status          pending / answered_all / answered_partial / closed / expired
--   trigger_source  meta_cog / intent_low_conf / strategy_gap
--   close_reason    user_x / interrupt / session_switch
--   platform        web / feishu / dingtalk / telegram / wecom / weixin
--   created_at      触发时间
--   resolved_at     完成/关闭/过期时间
--   expires_at      Web 30 分钟 / IM 24 小时
CREATE TABLE IF NOT EXISTS elicitations (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    questions_json TEXT NOT NULL,
    answers_json TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    trigger_source TEXT,
    close_reason TEXT,
    platform TEXT NOT NULL DEFAULT 'web',
    created_at INTEGER NOT NULL,
    resolved_at INTEGER,
    expires_at INTEGER NOT NULL
);
-- 按会话+状态查询活跃 elicitation（高频查询）
CREATE INDEX IF NOT EXISTS idx_elicit_session_status ON elicitations(session_id, status);
-- 过期扫描：按 status + expires_at 定位待过期记录
CREATE INDEX IF NOT EXISTS idx_elicit_expires ON elicitations(status, expires_at);
-- 按会话时间排序（断线恢复用）
CREATE INDEX IF NOT EXISTS idx_elicit_session_time ON elicitations(session_id, created_at);
-- sessions 表新增：同一 session 内关闭追问后不再触发追问的持久化标记
ALTER TABLE sessions
ADD COLUMN elicitation_blocked INTEGER NOT NULL DEFAULT 0;