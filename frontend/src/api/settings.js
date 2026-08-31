// 设置域 API（对齐 app/routes/settings.py 契约）
import { api } from './client'
import { withQuery } from '@/utils/query'

export const settingsApi = {
  providers: () => api.get('/settings/providers'),
  providerKey: (id) => api.get(`/settings/providers/${id}/key`),
  createProvider: (payload) => api.post('/settings/providers', payload),
  updateProvider: (id, payload) => api.put(`/settings/providers/${id}`, payload),
  deleteProvider: (id) => api.del(`/settings/providers/${id}`),
  testConnection: (cfg) => api.post('/settings/providers/test-connection', cfg),
  modelAssignment: () => api.get('/settings/model-assignment'),
  setModelAssignment: (payload) => api.put('/settings/model-assignment', payload),
  taskSlots: () => api.get('/settings/task-slots'),
  connectors: () => api.get('/settings/connectors'),
  createConnector: (payload) => api.post('/settings/connectors', payload),
  updateConnector: (id, payload) => api.put(`/settings/connectors/${id}`, payload),
  testConnector: (payload) => api.post('/settings/connectors/test', payload),
  toggleConnector: (id, enabled) => api.post(`/settings/connectors/${id}/toggle`, { enabled }),
  deleteConnector: (id) => api.del(`/settings/connectors/${id}`),
  refreshConnectorTools: (id) => api.post(`/settings/connectors/${id}/refresh-tools`, {}),
  platforms: () => api.get('/settings/platforms'),
  createPlatform: (payload) => api.post('/settings/platforms', payload),
  platformDetail: (id) => api.get(`/settings/platforms/${id}/detail`),
  updatePlatform: (id, payload) => api.put(`/settings/platforms/${id}`, payload),
  testPlatform: (payload) => api.post('/settings/platforms/test', payload),
  weixinQrcode: () => api.post('/settings/platforms/weixin/qrcode', {}),
  weixinQrcodeStatus: (qrcode) =>
    api.get(withQuery('/settings/platforms/weixin/qrcode/status', { qrcode })),
  enablePlatform: (id) => api.post(`/settings/platforms/${id}/enable`, {}),
  disablePlatform: (id) => api.post(`/settings/platforms/${id}/disable`, {}),
  resumePlatform: (id) => api.post(`/settings/platforms/${id}/resume`, {}),
  params: () => api.get('/settings/params'),
  saveParams: (params) => api.put('/settings/params', params),
  resetParams: () => api.post('/settings/params/reset', {}),
  usageSummary: (filters = {}) => api.get(withQuery('/settings/usage/summary', filters)),
  usageDistribution: (filters = {}) => api.get(withQuery('/settings/usage/distribution', filters)),
  usageTrend: (period, filters = {}) =>
    api.get(withQuery('/settings/usage/trend', { period, ...filters })),
  monthCost: () => api.get('/settings/usage/month-cost'),
  backups: () => api.get('/settings/backups'),
  createBackup: (label) => api.post('/settings/backups/create', { label: label || undefined }),
  exportBackup: () => api.post('/settings/backups/export', {}),
  importBackup: (form) => api.upload('/settings/backups/import', form),
  restoreBackup: (backupId) => api.post('/settings/backups/restore', { backup_id: backupId }),
  status: () => api.get('/settings/status'),
  tasks: () => api.get('/settings/tasks'),
  runTask: (taskId) => api.post(`/settings/tasks/${taskId}/run`, {}),
  taskLogs: (taskId) => api.get(`/settings/tasks/${taskId}/logs`),
  embeddingEstimate: (targetProviderId) =>
    api.post('/settings/embedding/estimate', { target_provider_id: targetProviderId }),
  embeddingMigrate: (targetProviderId) =>
    api.post('/settings/embedding/migrate', {
      target_provider_id: targetProviderId,
      confirm: true,
    }),
}
