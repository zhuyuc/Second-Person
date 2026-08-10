// 流式请求统一封装：保留 ReadableStream 能力，同时复用统一错误提示语义
import { useToast } from '@/stores/toast'

const BASE = '/api'

async function parseError(resp) {
    const data = await resp.clone().json().catch(() => null)
    if (data && data.message) return data.message
    const text = await resp.text().catch(() => '')
    return text || `请求失败（HTTP ${resp.status}）`
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
