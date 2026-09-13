// 统一 API 封装：按 {code,message,trace_id,details} 解析并 toast（开发文档 §6.21）
import { useToast } from '@/stores/toast'
import { friendlyError } from '@/utils/format'

const BASE = '/api'
const DEFAULT_TIMEOUT_MS = 30_000
let _toast
function getToast() {
  return (_toast ??= useToast())
}

function resolveTimeoutMs(path, options = {}) {
  if (typeof options.timeoutMs === 'number' && options.timeoutMs > 0) {
    return options.timeoutMs
  }
  // 知识库导入要跑 LLM 提炼，常远超普通 REST；给足等待时间。
  if (String(path || '').includes('/import/document')) return 600_000
  // 聊天附件解析（PDF/DOCX）偶发偏慢，略放宽。
  if (String(path || '').includes('/chat/attachment')) return 120_000
  return DEFAULT_TIMEOUT_MS
}

function timeoutToastMessage(path, options = {}) {
  if (options.timeoutMessage) return options.timeoutMessage
  const p = String(path || '')
  if (p.includes('/import/document')) {
    return '文档写入知识库较慢，当前对话不受影响；可稍后到「记忆」页查看导入结果'
  }
  if (p.includes('/chat/attachment')) {
    return '附件解析超时，请换一份文件或稍后重试'
  }
  if (p.includes('/backup') || p.includes('/export') || p.includes('/import')) {
    return '备份/导入导出耗时较长，请稍后在设置页查看结果'
  }
  return '等待服务响应超时，请稍后重试'
}

function networkToastMessage(options = {}) {
  return options.networkMessage || '无法连接本地服务，请确认 Second Person 已启动后重试'
}

function httpToastMessage(status) {
  if (status === 404) {
    return '接口不存在。若刚更新过程序，请刷新页面后再试'
  }
  if (status === 502 || status === 503 || status === 504) {
    return '服务暂时不可用，请稍后重试'
  }
  if (status === 405) {
    return '当前操作不被支持，请刷新页面后再试'
  }
  return `请求失败（状态码 ${status}），请刷新页面或稍后重试`
}

async function request(method, path, body, isForm, options = {}) {
  const opts = { method, headers: {} }
  if (body && !isForm) {
    opts.headers['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(body)
  } else if (isForm) {
    opts.body = body
  }
  // 超时兜底：后端 hang 时前端 Promise 不会永久 pending。
  // SSE 流式端点不走这里（streamClient 单独管理）。
  const timeoutMs = resolveTimeoutMs(path, options)
  if (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) {
    opts.signal = AbortSignal.timeout(timeoutMs)
  }
  let resp
  try {
    resp = await fetch(BASE + path, opts)
  } catch (e) {
    if (!options.silent) {
      if (e.name === 'TimeoutError' || e.name === 'AbortError') {
        getToast().push('warning', timeoutToastMessage(path, options))
      } else {
        getToast().push('error', networkToastMessage(options))
      }
      e.alreadyToasted = true
    }
    throw e
  }
  const data = await resp.json().catch(() => ({}))
  if (data.code && data.code !== 200) {
    if (!options.silent) handleError(data, resp.status)
    const err = new Error(data.message || '请求失败')
    err.code = data.code
    err.alreadyToasted = true
    throw err
  }
  // 非标准响应兑底：HTTP 失败但响应体无 code（如 405/502/网关错误页），
  // 不能静默当成功返回，否则调用方会误报"操作成功"但后端实际未执行
  if (!resp.ok) {
    const httpErr = new Error(`HTTP ${resp.status}`)
    if (!options.silent) {
      getToast().push('error', httpToastMessage(resp.status))
      httpErr.alreadyToasted = true
    }
    throw httpErr
  }
  return data.data
}

function handleError(data) {
  const t = getToast()
  const code = data.code
  const traceId = data.trace_id || undefined
  if (code === 400) {
    t.push('error', friendlyError(data.message, '输入有误，请检查后重试'), traceId)
  } else if (code === 404) {
    t.push('error', friendlyError(data.message, '要找的内容不存在，可能已被删除'), traceId)
  } else if (code === 409) {
    t.push('warning', friendlyError(data.message, '操作冲突，请刷新后重试'), traceId)
  } else if (code === 413) {
    t.push(
      'error',
      friendlyError(data.message, '内容过大，请压缩图片或减少附件后再试'),
      traceId,
    )
  } else if (code === 429) {
    t.push('warning', '操作太频繁，系统正在自动重试，请稍候', traceId)
  } else if (code === 503) {
    t.push('error', friendlyError(data.message, '服务暂时繁忙，请稍后再试'), traceId)
  } else {
    t.push('error', friendlyError(data.message, '服务出错了，请稍后重试'), traceId)
  }
}

export const api = {
  get: (p, options) => request('GET', p, null, false, options || {}),
  post: (p, b, options) => request('POST', p, b, false, options || {}),
  put: (p, b, options) => request('PUT', p, b, false, options || {}),
  patch: (p, b, options) => request('PATCH', p, b, false, options || {}),
  del: (p, b, options) => request('DELETE', p, b, false, options || {}),
  upload: (p, form, options) => request('POST', p, form, true, options || {}),
}
