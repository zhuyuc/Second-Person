// 记忆域 API（对齐 app/routes/memory.py 契约）
import { api } from './client'
import { withQuery } from '@/utils/query'

export const memoryApi = {
  list: (payload) => api.post('/memory/list', payload),
  domains: () => api.get('/memory/domains'),
  detail: (id) => api.get(withQuery('/memory/detail', { id })),
  archive: (id) => api.post('/memory/archive', { id }),
  restore: (id) => api.post('/memory/restore', { id }),
  delete: (id) => api.post('/memory/delete', { id }),
  updateAttributes: (id, payload) => api.put(`/memory/${id}/attributes`, payload),
  timeline: (days, eventType) =>
    api.get(
      withQuery('/memory/timeline', {
        days,
        ...(eventType ? { event_type: eventType } : {}),
      })
    ),
  health: () => api.get('/memory/health'),
  governance: () => api.get('/memory/governance'),
  candidates: (status = 'pending', limit = 100) =>
    api.get(withQuery('/memory/candidates', { status, limit })),
  confirmCandidate: (id) => api.post(`/memory/candidates/${id}/confirm`, {}),
  rejectCandidate: (id, reason) => api.post(`/memory/candidates/${id}/reject`, { reason }),
  resolveGovernance: (itemId, action) =>
    api.post(`/memory/governance/${itemId}/resolve`, { action }),
  graph: () => api.get('/memory/graph'),
  graphNeighbors: (entityId, params = {}) =>
    api.get(withQuery(`/memory/graph/entity/${entityId}/neighbors`, params)),
  graphEntityMemories: (entityId) => api.get(`/memory/graph/entity/${entityId}/memories`),
  neighbors: (id, depth = 1) => api.get(withQuery('/memory/neighbors', { id, depth })),
  conflicts: () => api.get('/memory/conflicts'),
  resolveConflict: (conflictId, resolution) =>
    api.post('/memory/conflicts/resolve', { conflict_id: conflictId, resolution }),
  runLint: () => api.post('/memory/lint/run', {}),
  resolveDuplicate: (suggestionId, resolution) =>
    api.post('/memory/lint/duplicates/resolve', { suggestion_id: suggestionId, resolution }),
  acceptSuggestion: (suggestionId) =>
    api.post('/memory/lint/suggestions/accept', { suggestion_id: suggestionId }),
  dismissSuggestion: (suggestionId, reason) =>
    api.post('/memory/lint/suggestions/dismiss', { suggestion_id: suggestionId, reason }),
}
