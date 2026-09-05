import { fmtDuration } from '@/utils/format'

/**
 * 记忆检索时间线药丸文案。
 * 「未命中」仅用于最终 done 且 hit_count=0；中间阶段（预筛/精筛等）
 * 完成时不得误标为未命中。
 *
 * @param {object} item memory_stage 时间线条目
 * @param {{ effectiveStatus?: string }} [opts] 前端对 stuck-running 的纠偏状态
 */
export function formatMemoryStageBadge(item, opts = {}) {
  if (!item || item.kind === 'tool_call') return ''
  const stage = item.stage || ''
  const status = opts.effectiveStatus || item.status
  const candidates = item.candidates
  const hitCount = item.hit_count

  if (status === 'skipped' || item.status === 'skipped') {
    if (stage === 'presearch' && candidates === 0) return '预筛无候选'
    return '已跳过'
  }

  if (status === 'running') {
    return (
      {
        embed: '生成向量…',
        presearch: '预筛中…',
        graph: '关联扩展…',
        refine: '精筛中…',
      }[stage] || '检索中…'
    )
  }

  // ---- 已完成（ok / 被后续阶段纠偏为完成）----
  if (stage === 'done') {
    if (hitCount > 0) return `注入 ${hitCount} 条`
    return '未命中'
  }
  if (hitCount > 0) return `注入 ${hitCount} 条`

  if (stage === 'embed') return '向量已就绪'
  if (stage === 'presearch') {
    if (candidates > 0) return `预筛 ${candidates} 条`
    return '预筛无候选'
  }
  if (stage === 'graph') {
    if (candidates > 0) return `扩展至 ${candidates} 条`
    return '关联扩展完成'
  }
  if (stage === 'refine') {
    if (candidates > 0) return `精筛 ${candidates} 条候选`
    return '精筛完成'
  }
  if (stage === 'skipped') return '已跳过'
  return '已完成'
}

/** 思考时间线面板标题：执行工具 N 次 · 2分28秒 */
export function formatTimelineSummary(items) {
  const list = Array.isArray(items) ? items : []
  if (!list.length) return '处理进度'

  // 单步且无工具：直接展示该步摘要，避免「处理 1 步」信息量过低
  if (list.length === 1) {
    const it = list[0]
    if (it.kind === 'memory_stage') {
      return `记忆检索 · ${formatMemoryStageBadge(it)}`
    }
    if (it.kind === 'tool_call')
      {return `${it.name || '工具'} · ${it.status === 'ok' ? '已完成' : it.status === 'fail' ? '失败' : '执行中'}`}
  }

  // 多步时若有最终记忆结果且尚无工具，优先展示记忆结论
  const toolCount = list.filter((i) => i.kind === 'tool_call').length
  if (!toolCount) {
    const memDone = [...list]
      .reverse()
      .find((i) => i.kind === 'memory_stage' && i.stage === 'done')
    if (memDone) return `记忆检索 · ${formatMemoryStageBadge(memDone)}`
  }

  let maxMs = 0
  for (const it of list) {
    if (it.elapsed_ms !== null && it.elapsed_ms !== undefined) maxMs = Math.max(maxMs, it.elapsed_ms)
  }
  const parts = []
  if (toolCount) parts.push(`执行工具 ${toolCount} 次`)
  else parts.push(`处理 ${list.length} 步`)
  const waiting = list.find((i) => i.kind === 'step_wait' && i.status === 'running')
  if (waiting?.label) {
    const tail = waiting.detail ? ` · ${waiting.detail}` : ''
    return waiting.label + tail
  }
  if (maxMs > 0) parts.push(fmtDuration(maxMs))
  return parts.join(' · ')
}
