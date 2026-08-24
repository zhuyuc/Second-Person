// 对话请求契约：值与后端 agent/contracts.py 保持一致。
export const REASONING_EFFORTS = Object.freeze(['off', 'low', 'high', 'max'])

export function normalizeReasoningEffort(value) {
  return REASONING_EFFORTS.includes(value) ? value : 'high'
}
