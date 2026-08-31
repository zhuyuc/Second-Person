/** 思考时间线面板标题：执行工具 N 次 · 148s */
export function formatTimelineSummary(items) {
  const list = Array.isArray(items) ? items : []
  if (!list.length) return '处理进度'

  // 单步且无工具：直接展示该步摘要，避免「处理 1 步」信息量过低
  if (list.length === 1) {
    const it = list[0]
    if (it.kind === 'memory_stage') {
      if (it.status === 'skipped') return '记忆检索 · 已跳过'
      if (it.hit_count > 0) return `记忆检索 · 注入 ${it.hit_count} 条`
      if (it.stage === 'done' || it.status === 'ok') return '记忆检索 · 未命中'
    }
    if (it.kind === 'tool_call') return `${it.name || '工具'} · ${it.status === 'ok' ? '已完成' : it.status === 'fail' ? '失败' : '执行中'}`
  }

  const toolCount = list.filter(i => i.kind === 'tool_call').length
  let maxMs = 0
  for (const it of list) {
    if (it.elapsed_ms != null) maxMs = Math.max(maxMs, it.elapsed_ms)
  }
  const parts = []
  if (toolCount) parts.push(`执行工具 ${toolCount} 次`)
  else parts.push(`处理 ${list.length} 步`)
  const waiting = list.find(i => i.kind === 'step_wait' && i.status === 'running')
  if (waiting?.label) {
    const tail = waiting.detail ? ` · ${waiting.detail}` : ''
    return waiting.label + tail
  }
  if (maxMs >= 1000) parts.push(`${Math.round(maxMs / 1000)}s`)
  else if (maxMs > 0) parts.push(`${maxMs}ms`)
  return parts.join(' · ')
}
