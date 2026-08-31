// 项目工作区 API 客户端（对齐 app/routes/projects.py 契约）
import { api } from './client'

export const projectsApi = {
  list: (status = 'active') => api.get(`/projects?status=${encodeURIComponent(status)}`),
  create: (payload) => api.post('/projects', payload),
  patch: (id, payload) => api.patch(`/projects/${id}`, payload),
  archive: (id) => api.post(`/projects/${id}/archive`),
  unarchive: (id) => api.post(`/projects/${id}/unarchive`),
  purge: (id) => api.del(`/projects/${id}`),
  browseNative: () => api.post('/projects/browse/native'),
  setSandboxMode: (sid, mode, reason) =>
    api.post(`/chat/session/${sid}/sandbox-mode`, { mode, reason }),
  getSandboxMode: (sid) => api.get(`/chat/session/${sid}/sandbox-mode`),
  // M4：项目内文件浏览
  tree: (id, path = '', depth = 1) =>
    api.get(`/projects/${id}/tree?path=${encodeURIComponent(path)}&depth=${depth}`),
  search: (id, payload) => api.post(`/projects/${id}/search`, payload),
}
