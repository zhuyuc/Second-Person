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

// 未收录值原样兜底显示
export const confidenceLabel = (v) => CONF_MAP[v] || v
export const lifecycleLabel = (v) => LIFE_MAP[v] || v
export const sourceLabel = (v) => SRC_MAP[v] || v
export const attributionLabel = (v) => ATTR_MAP[v] || v
