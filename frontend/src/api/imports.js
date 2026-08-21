// 知识库导入 API：页面只处理展示状态，不直接管理 fetch/SSE 协议。
import { postFormStream, parseSSE } from '@/api/streamClient'

export async function uploadDocumentStream(file, onEvent) {
    const form = new FormData()
    form.append('file', file)
    const response = await postFormStream('/import/document/stream', form)
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const parts = buffer.split(/\r?\n\r?\n/)
        buffer = parts.pop()
        for (const part of parts) {
            const event = parseSSE(part)
            if (event) onEvent(event.event, event.data)
        }
    }
    const finalEvent = parseSSE(buffer)
    if (finalEvent) onEvent(finalEvent.event, finalEvent.data)
}
