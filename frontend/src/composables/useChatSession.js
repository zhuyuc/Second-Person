// 会话消息加载与规范化（ChatView 抽取）
import { chatApi } from '@/api/chat'
import { TOAST_ONLY_NOTIF } from '@/utils/enumLabel'

// 仅提示类系统通知：Web 端已在导入时用 toast 实时反馈，无需在对话流中留存横幅（含历史）
export function stripToastNotifs(msgs) {
  return msgs.filter(
    (m) =>
      !(m.message_type === 'system_notification' && TOAST_ONLY_NOTIF.includes(m.notification_type))
  )
}

// 从历史消息的附件上下文前缀中还原各附件的名称与正文
export function extractAttachments(content) {
  const head = content.split('\n---\n').slice(0, -1).join('\n---\n')
  if (!head) return []
  const re = /【附件：([^】]+?)(?:（内容已截断）)?】|【选中的文本】/g
  const found = []
  let m,
    prev = null
  while ((m = re.exec(head))) {
    if (prev) prev.body = head.slice(prev.end, m.index).replace(/\n+$/, '').replace(/^\n+/, '')
    prev = { isQuote: !m[1], name: m[1] || '引用', end: re.lastIndex }
    found.push(prev)
  }
  if (prev) prev.body = head.slice(prev.end).replace(/\n+$/, '').replace(/^\n+/, '')
  return found.map((a) => {
    if (a.isQuote) {
      const body = a.body || ''
      const idx = body.indexOf('\n\n【用户评论】\n')
      const text = idx >= 0 ? body.slice(0, idx) : body
      const comment = idx >= 0 ? body.slice(idx + '\n\n【用户评论】\n'.length) : ''
      return { name: '引用', pasted: true, kind: 'quote', text, comment, chars: text.length }
    }
    return {
      name: a.name,
      pasted: /^粘贴的文本/.test(a.name),
      text: a.body || '',
      chars: (a.body || '').length,
    }
  })
}

export function normalizeMessageAttachments(messages) {
  for (const m of messages) {
    if (
      m.role === 'user' &&
      typeof m.content === 'string' &&
      m.content.includes('\n---\n') &&
      (m.content.includes('【附件：') || m.content.includes('【选中的文本】'))
    ) {
      m.atts = extractAttachments(m.content)
      m.content = m.content.split('\n---\n').pop()
    }
  }
  return messages
}

export async function fetchSessionMessages(sid, { before_id, limit } = {}) {
  const msgs = await chatApi.messages(sid, { before_id, limit })
  return stripToastNotifs(normalizeMessageAttachments(msgs))
}

export async function fetchSessionMetrics(sid) {
  return chatApi.sessionMetrics(sid).catch(() => null)
}
