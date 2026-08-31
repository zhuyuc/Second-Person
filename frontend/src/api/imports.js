// 知识库导入 API：页面只处理展示状态，不直接管理 fetch/SSE 协议。
import { api } from '@/api/client'
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

export const importApi = {
  documents: () => api.get('/import/documents'),
  document: (id) => api.get(`/import/documents/${id}`),
  confirmDocument: (docId, selected) =>
    api.post(`/import/documents/${docId}/confirm`, { selected }),
  deleteDocument: (id, cascade = false) =>
    api.del(`/import/documents/${id}${cascade ? '?cascade=true' : ''}`),
  localDirs: () => api.get('/import/local-dirs'),
  addLocalDir: (path, recursive) => api.post('/import/local-dirs', { path, recursive }),
  deleteLocalDir: (id) => api.del(`/import/local-dirs/${id}`),
  updateLocalDir: (id, payload) => api.put(`/import/local-dirs/${id}`, payload),
  scanLocalDirs: () => api.post('/import/local-dirs/scan', {}),
  localDirFiles: (id) => api.get(`/import/local-dirs/${id}/files`),
}
