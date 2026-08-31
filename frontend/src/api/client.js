// 统一 API 封装：按 {code,message,trace_id,details} 解析并 toast（开发文档 §6.21）
import { useToast } from '@/stores/toast'
import { friendlyError } from '@/utils/format'

const BASE = '/api'
let _toast
function getToast() {
  return (_toast ??= useToast())
}

async function request(method, path, body, isForm) {
  const opts = { method, headers: {} }
  if (body && !isForm) {
    opts.headers['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(body)
  } else if (isForm) {
    opts.body = body
  }
  // 超时兜底：后端 hang 时前端 Promise 不会永久 pending。
  // SSE 流式端点不走这里（streamClient 单独管理），30s 对 REST 足够。
  if (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) {
    opts.signal = AbortSignal.timeout(30_000)
  }
  let resp
  try {
    resp = await fetch(BASE + path, opts)
  } catch (e) {
    if (e.name === 'TimeoutError' || e.name === 'AbortError') {
      getToast().push('error', '请求超时，请稍后重试')
    } else {
      getToast().push('error', '网络错误，请检查服务是否运行')
    }
    throw e
  }
  const data = await resp.json().catch(() => ({}))
  if (data.code && data.code !== 200) {
    handleError(data, resp.status)
    throw new Error(data.message || '请求失败')
  }
  // 非标准响应兑底：HTTP 失败但响应体无 code（如 405/502/网关错误页），
  // 不能静默当成功返回，否则调用方会误报"操作成功"但后端实际未执行
  if (!resp.ok) {
    getToast().push('error', `请求失败（HTTP ${resp.status}），后端可能未重启或接口不存在`)
    throw new Error(`HTTP ${resp.status}`)
  }
  return data.data
}

function handleError(data) {
  const t = getToast()
  const code = data.code
  const traceId = data.trace_id || undefined
  if (code === 400) t.push('error', friendlyError(data.message, '请检查输入'), traceId)
  else if (code === 404) t.push('error', friendlyError(data.message, '资源不存在'), traceId)
  else if (code === 409) t.push('warning', friendlyError(data.message, '操作冲突'), traceId)
  else if (code === 429) t.push('warning', '系统正在自动重试', traceId)
  else if (code === 503)
    {t.push('error', friendlyError(data.message, '服务降级，请稍后重试'), traceId)}
  else t.push('error', friendlyError(data.message, '服务端错误'), traceId)
}

export const api = {
  get: (p) => request('GET', p),
  post: (p, b) => request('POST', p, b),
  put: (p, b) => request('PUT', p, b),
  patch: (p, b) => request('PATCH', p, b),
  del: (p, b) => request('DELETE', p, b),
  upload: (p, form) => request('POST', p, form, true),
}
