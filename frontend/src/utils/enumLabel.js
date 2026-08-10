// 枚举值中文化统一映射（展示中文、提交英文原值；单一来源，禁止各视图重复定义）
// 新增枚举值只需扩展本文件映射表

// 记忆置信度
export const CONF_MAP = { strong: '强', medium: '中', low: '弱', disputed: '争议' }
// 记忆生命周期
export const LIFE_MAP = { active: '活跃', stable: '稳定', stale: '过期', archived: '已归档', missing: '缺失' }
// 记忆来源类型
export const SRC_MAP = { memory: '对话记忆', knowledge: '外部知识' }
// 提炼归因
export const ATTR_MAP = { imported: '外部导入', verified: '已验证经验', inferred: '待验证推断' }

// handoff 摘要状态
export const HANDOFF_MAP = { generating: '生成中', ready: '已就绪', failed: '生成失败' }
// 会话状态
export const SESSION_STATE_MAP = { readonly: '已结束' }
// 时间线事件类型
export const EVT_MAP = { created: '新建', updated: '更新', evolved: '演变', imported: '导入', archived: '归档', merged: '合并' }
// 用户画像维度状态
export const DIM_STATUS_MAP = { active: '活跃', building: '积累中', stable: '稳定', mature: '成熟', sparse: '样本较少', empty: '暂无内容' }
// SOUL 风格来源
export const SOUL_SRC_MAP = { dialog: '对话确认', auto: '自动演化' }
// 健康度扣分维度
export const DEDUCT_MAP = {
    disputed: '矛盾记忆', low_unconfirmed: '低置信未确认', stale: '过期记忆',
    orphan: '孤立记忆', duplicate: '疑似重复', missing: 'md 文件缺失',
    failed_writes: '写入失败',
}
// 接入渠道类型
export const PLATFORM_MAP = { web: 'Web', feishu: '飞书', telegram: 'Telegram', dingtalk: '钉钉', wecom: '企业微信', weixin: '微信' }
// 用量来源
export const SOURCE_NAMES = {
    main_chat: 'AI对话', agent: '工具prompt', system_agent: '系统prompt', title_gen: '标题生成',
    embedding: '向量分析', vision: '图片解析', intent_parse: '意图解析', tool_infer: '工具推断',
    attention_focus: '注意力聚焦', converge_intent: '意图收敛', gap_detect: '缺口检测',
    honest_clarify: '诚实澄清', mood: '情绪分析', quick_intent: '快速意图', replan: '重规划',
    profile_conflict: '画像冲突扫描',
}
// 仅提示类系统通知：Web 端用 toast 实时反馈，无需留存在对话流横幅
export const TOAST_ONLY_NOTIF = ['doc_imported']

// 未收录值原样兜底显示
export const confidenceLabel = (v) => CONF_MAP[v] || v
export const lifecycleLabel = (v) => LIFE_MAP[v] || v
export const sourceLabel = (v) => SRC_MAP[v] || v
export const handoffStatusLabel = (v) => HANDOFF_MAP[v] || v
export const sessionStateLabel = (v) => SESSION_STATE_MAP[v] || v
export const eventLabel = (v) => EVT_MAP[v] || v
export const dimStatusLabel = (v) => DIM_STATUS_MAP[v] || v
export const soulSourceLabel = (v) => SOUL_SRC_MAP[v] || v
export const platformLabel = (v) => PLATFORM_MAP[v] || v
export const usageSourceLabel = (v) => SOURCE_NAMES[v] || v
