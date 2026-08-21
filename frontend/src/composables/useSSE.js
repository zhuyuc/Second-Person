// useSSE：连接 /api/chat/send，处理 keepalive/断线重连/轮询降级/多标签接管
// （开发文档 §断线重连与缓冲区规则 / §6.21）
// 支持 handoff_ready 事件（会话上下文管理方案 v2）
import { parseSSE, postJsonStream } from '@/api/streamClient'
import { normalizeThinkMode } from '@/utils/chatContract'

export function useSSE() {
    let controller = null

    async function send({ sessionId, message, images, clientRequestId, regenerateMessageId, editMessageId, location, onEvent, onError, handoffPath, thinkMode = 'auto' }) {
        const crid = clientRequestId || genId()
        sessionStorage.setItem('sp_active_crid', crid)
        // 首次发送携带 message；断线重连时同 crid 重推（服务端缓冲区断点续推）
        const MAX_RETRY = 2
        let attempt = 0
        let done = false
        while (attempt <= MAX_RETRY && !done) {
            controller = new AbortController()
            let gotFirst = false
            const timeout = setTimeout(() => {
                if (!gotFirst) onEvent && onEvent('degrade', { mode: 'polling' })
            }, 10000)
            try {
                const resp = await postJsonStream('/chat/send', {
                    session_id: sessionId,
                    message,
                    images,
                    client_request_id: crid,
                    regenerate_message_id: regenerateMessageId,
                    edit_message_id: editMessageId || undefined,
                    location: location || undefined,
                    handoff_path: handoffPath || undefined,
                    // auto=模型自主路由；quick/deep 为用户显式覆盖。
                    think_mode: normalizeThinkMode(thinkMode),
                }, { signal: controller.signal })
                const reader = resp.body.getReader()
                const decoder = new TextDecoder()
                let buffer = ''
                while (true) {
                    const { done: rdone, value } = await reader.read()
                    if (rdone) break
                    gotFirst = true
                    clearTimeout(timeout)
                    buffer += decoder.decode(value, { stream: true })
                    const parts = buffer.split(/\r?\n\r?\n/)
                    buffer = parts.pop()
                    for (const part of parts) {
                        const evt = parseSSE(part)
                        if (evt) {
                            onEvent && onEvent(evt.event, evt.data)
                            if (evt.event === 'turn_completed' || evt.event === 'error') done = true
                        }
                    }
                }
                clearTimeout(timeout)
                done = true
            } catch (e) {
                clearTimeout(timeout)
                if (e.name === 'AbortError') { done = true; break }
                attempt++
                if (attempt > MAX_RETRY) { onError && onError(e); break }
                // 断线重连：稍候同 crid 重推（不重复计费，服务端缓冲区续推）
                await new Promise(r => setTimeout(r, 800 * attempt))
            }
        }
        sessionStorage.removeItem('sp_active_crid')
    }

    function abort() {
        if (controller) controller.abort()
    }

    return { send, abort }
}

function genId() {
    return 'cr_' + Math.random().toString(36).slice(2, 12)
}
