// 统一 API 封装：按 {code,message,trace_id,details} 解析并 toast（开发文档 §6.21）
import { useToast } from '@/stores/toast'

const BASE = '/api'

async function request(method, path, body, isForm) {
    const opts = { method, headers: {} }
    if (body && !isForm) {
        opts.headers['Content-Type'] = 'application/json'
        opts.body = JSON.stringify(body)
    } else if (isForm) {
        opts.body = body
    }
    let resp
    try {
        resp = await fetch(BASE + path, opts)
    } catch (e) {
        useToast().push('error', '网络错误，请检查服务是否运行')
        throw e
    }
    const data = await resp.json().catch(() => ({}))
    if (data.code && data.code !== 200) {
        handleError(data, resp.status)
        throw new Error(data.message || '请求失败')
    }
    return data.data
}

function handleError(data, httpStatus) {
    const t = useToast()
    const code = data.code
    const traceId = data.trace_id || undefined
    if (code === 400) t.push('error', data.message || '请检查输入', traceId)
    else if (code === 404) t.push('error', data.message || '资源不存在', traceId)
    else if (code === 409) t.push('warning', data.message || '操作冲突', traceId)
    else if (code === 429) t.push('warning', '系统正在自动重试', traceId)
    else if (code === 503) t.push('error', '降级模式：' + (data.message || ''), traceId)
    else t.push('error', (data.message || '服务端错误'), traceId)
}

export const api = {
    get: (p) => request('GET', p),
    post: (p, b) => request('POST', p, b),
    put: (p, b) => request('PUT', p, b),
    del: (p, b) => request('DELETE', p, b),
    upload: (p, form) => request('POST', p, form, true),
}
