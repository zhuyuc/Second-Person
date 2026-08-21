// 对话请求契约：值与后端 agent/contracts.py 保持一致。
export const THINK_MODES = Object.freeze(['auto', 'quick', 'deep'])

export function normalizeThinkMode(value) {
  return THINK_MODES.includes(value) ? value : 'auto'
}

