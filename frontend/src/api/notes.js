import { api } from './client'

export const notesApi = {
  tree: () => api.get('/notes/tree'),
  list: () => api.get('/notes'),
  search: (q, limit = 50) =>
    api.get(`/notes/search?q=${encodeURIComponent(q || '')}&limit=${limit}`),
  create: (payload) => api.post('/notes', payload || {}),
  get: (id) => api.get(`/notes/${id}`),
  patch: (id, payload) => api.patch(`/notes/${id}`, payload),
  delete: (id) => api.del(`/notes/${id}`),
  createFolder: (payload) => api.post('/notes/folders', payload || {}),
  patchFolder: (id, payload) => api.patch(`/notes/folders/${id}`, payload),
  deleteFolder: (id) => api.del(`/notes/folders/${id}`),
  applyEdit: (id, payload) => api.post(`/notes/${id}/apply_edit`, payload),
  setCursor: (id, payload) => api.post(`/notes/${id}/cursor`, payload || {}),
  revisions: (id, { limit = 10, includeBody = true } = {}) =>
    api.get(
      `/notes/${id}/revisions?limit=${limit}&include_body=${includeBody ? 'true' : 'false'}`,
    ),
  createRevision: (id, source = 'checkpoint') =>
    api.post(`/notes/${id}/revisions`, { source }),
  getRevision: (id, revId) => api.get(`/notes/${id}/revisions/${revId}`),
  restore: (id, revId) =>
    api.post(`/notes/${id}/revisions/restore`, revId ? { rev_id: revId } : {}),
  uploadAsset: (id, file) => {
    const form = new FormData()
    form.append('file', file)
    return api.upload(`/notes/${id}/assets`, form)
  },
}
