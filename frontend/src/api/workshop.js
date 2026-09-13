// 视频工坊 API
import { api } from './client'
import { withQuery } from '@/utils/query'
import { parseSSE, postJsonStream } from './streamClient'

export const workshopApi = {
  capabilities: () => api.get('/workshop/capabilities'),
  types: () => api.get('/workshop/types'),
  list: (params = {}) =>
    api.get(withQuery('/workshop/projects', {
      type: params.type || undefined,
      q: params.q || undefined,
      status: params.status || undefined,
      limit: params.limit,
      offset: params.offset,
    })),
  get: (id) => api.get(`/workshop/projects/${id}`),
  create: (payload) => api.post('/workshop/projects', payload),
  patch: (id, payload) => api.patch(`/workshop/projects/${id}`, payload),
  /** multipart 上传参考图（落盘 chat_images，与主对话一致） */
  uploadRefs: (id, form) => api.upload(`/workshop/projects/${id}/refs`, form),
  remove: (id) => api.del(`/workshop/projects/${id}`),
  ensureSession: (id) => api.post(`/workshop/projects/${id}/ensure-session`, {}),
  /** SSE 出片：onEvent(eventName, data) */
  async render(id, { onEvent, signal } = {}) {
    const resp = await postJsonStream(`/workshop/projects/${id}/render`, {}, { signal })
    const reader = resp.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    const flush = (chunk) => {
      const parsed = parseSSE(chunk)
      if (parsed && onEvent) onEvent(parsed.event, parsed.data)
    }
    while (true) {
      const { done, value } = await reader.read()
      if (done) {
        // 流结束时必须冲刷尾包，否则最后一条 workshop_error/done 会丢，前端卡在 doing
        buf += decoder.decode()
        if (buf.trim()) flush(buf)
        break
      }
      buf += decoder.decode(value, { stream: true })
      const parts = buf.split(/\n\n/)
      buf = parts.pop() || ''
      for (const chunk of parts) flush(chunk)
    }
  },
  cancel: (id) => api.post(`/workshop/projects/${id}/cancel`, {}),
}
