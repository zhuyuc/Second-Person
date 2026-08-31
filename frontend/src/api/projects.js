// 项目工作区 API 客户端（对齐 app/routes/projects.py 契约）
import { api } from './client'

export const projectsApi = {
  list: (status = 'active') => api.get(`/projects?status=${encodeURIComponent(status)}`),
  get: (id) => api.get(`/projects/${id}`),
  create: (payload) => api.post('/projects', payload),
  patch: (id, payload) => api.patch(`/projects/${id}`, payload),
  archive: (id) => api.post(`/projects/${id}/archive`),
  unarchive: (id) => api.post(`/projects/${id}/unarchive`),
  purge: (id) => api.del(`/projects/${id}`),
  relocate: (id, new_path) => api.post(`/projects/${id}/relocate`, { new_path }),
  browse: (path = '') => api.get(`/projects/browse?path=${encodeURIComponent(path)}`),
  mkdir: (parent, name) => api.post('/projects/browse/mkdir', { parent, name }),
  browseNative: () => api.post('/projects/browse/native'),
  setSandboxMode: (sid, mode, reason) =>
    api.post(`/chat/session/${sid}/sandbox-mode`, { mode, reason }),
  getSandboxMode: (sid) => api.get(`/chat/session/${sid}/sandbox-mode`),
  // M4：项目内文件浏览
  tree: (id, path = '', depth = 1) =>
    api.get(`/projects/${id}/tree?path=${encodeURIComponent(path)}&depth=${depth}`),
  preview: (id, path, offset = 1, limit = 200) =>
    api.get(`/projects/${id}/preview?path=${encodeURIComponent(path)}&offset=${offset}&limit=${limit}`),
  search: (id, payload) => api.post(`/projects/${id}/search`, payload),
}
