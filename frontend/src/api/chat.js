import { api } from './client'
import { withQuery } from '@/utils/query'

// 对话域 API 封装：ChatView 与会话侧栏共用，隐藏原始路径
export const chatApi = {
  // ---- 会话生命周期 ----
  createSession: (payload) => api.post('/chat/session/create', payload),
  handoff: (fromSessionId) => api.post('/chat/session/handoff', { from_session_id: fromSessionId }),
  activeRequest: (sid) => api.get(`/chat/session/${sid}/active-request`),
  cancel: (clientRequestId) => api.post('/chat/cancel', { client_request_id: clientRequestId }),
  feedback: (payload) => api.post('/chat/feedback', payload),
  switchVersion: (payload) => api.post('/chat/switch-version', payload),
  versionSiblings: (versionGroupId) =>
    api.get(withQuery('/chat/version-siblings', { version_group_id: versionGroupId })),
  memoryFeedback: (payload) => api.post('/memory/feedback', payload),

  // ---- 会话列表/操作（SessionSidebar）----
  sessions: (pageSize = 500) => api.get(`/chat/sessions?page_size=${pageSize}`),
  archiveSession: (sessionId) => api.post('/chat/session/archive', { session_id: sessionId }),
  renameSession: (sessionId, title) => api.post('/chat/session/rename', { session_id: sessionId, title }),
  pinSession: (sessionId, pinned) => api.post('/chat/session/pin', { session_id: sessionId, pinned }),
  deleteSession: (sessionId) => api.del(`/chat/session/${sessionId}`),

  // ---- 搜索（SessionSearchPanel）----
  search: (params) => api.get(withQuery('/chat/search', params)),

  // ---- 系统/引导 ----
  health: () => api.get('/health'),
  onboardingStatus: () => api.get('/onboarding/status'),

  // ---- 模型/参数 ----
  providers: () => api.get('/settings/providers'),
  modelAssignment: () => api.get('/settings/model-assignment'),
  setModelAssignment: (payload) => api.put('/settings/model-assignment', payload),
  reasoningEfforts: () => api.get('/chat/reasoning-efforts'),
  params: () => api.get('/settings/params'),

  // ---- 附件/导入 ----
  uploadAttachment: (form) => api.upload('/chat/attachment', form),
  importDocument: (form) => api.upload('/import/document', form),
  messages: (sid, { before_id, limit } = {}) =>
    api.get(withQuery('/chat/messages', { session_id: sid, before_id, limit })),
  sessionMetrics: (sid) => api.get(`/chat/session/${sid}/metrics`),
}
