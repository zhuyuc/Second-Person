// 流式请求统一封装：保留 ReadableStream 能力，同时复用统一错误提示语义
import { useToast } from '@/stores/toast'
import { friendlyError } from '@/utils/format'

const BASE = '/api'

async function parseError(resp) {
  const data = await resp
    .clone()
    .json()
    .catch(() => null)
  if (data && data.message) return friendlyError(data.message)
  const text = await resp.text().catch(() => '')
  return friendlyError(text, `请求失败（HTTP ${resp.status}）`)
}

export async function postJsonStream(path, body, { signal } = {}) {
  let resp
  try {
    resp = await fetch(BASE + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
      signal,
    })
  } catch (e) {
    if (e.name !== 'AbortError') useToast().push('error', '网络错误，请检查服务是否运行')
    throw e
  }
  if (!resp.ok) {
    const msg = await parseError(resp)
    useToast().push('error', msg)
    throw new Error(msg)
  }
  if (!resp.body) {
    const msg = '服务未返回可读取的流式响应'
    useToast().push('error', msg)
    throw new Error(msg)
  }
  return resp
}

export async function postFormStream(path, form, { signal } = {}) {
  let resp
  try {
    resp = await fetch(BASE + path, { method: 'POST', body: form, signal })
  } catch (e) {
    if (e.name !== 'AbortError') useToast().push('error', '网络错误，请检查服务是否运行')
    throw e
  }
  if (!resp.ok) {
    const msg = await parseError(resp)
    useToast().push('error', msg)
    throw new Error(msg)
  }
  if (!resp.body) {
    const msg = '服务未返回可读取的流式响应'
    useToast().push('error', msg)
    throw new Error(msg)
  }
  return resp
}

// SSE 文本解析属于传输层，页面与组合式逻辑不得各自实现不同版本。
export function parseSSE(chunk) {
  let event = 'message'
  let data = ''
  for (const line of chunk.split(/\r?\n/)) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) data += line.slice(5).trim()
  }
  if (!data) return null
  try {
    return { event, data: JSON.parse(data) }
  } catch {
    return { event, data: {} }
  }
}
