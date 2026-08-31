// 用户画像 / SOUL / 输出样式 API（对齐 app/routes/soul.py 契约）
import { api } from './client'
import { withQuery } from '@/utils/query'

export const soulApi = {
  profile: () => api.get('/profile'),
  buildProfile: () => api.post('/profile/build-now', {}),
  soul: () => api.get('/soul'),
  updateSoulCore: (content) => api.put('/soul/core', { content }),
  resetSoulCore: () => api.post('/soul/core/reset', {}),
  outputStyle: () => api.get('/output-style'),
  updateOutputStyle: (content) => api.put('/output-style', { content }),
  buildOutputStyle: () => api.post('/output-style/build-now', {}),
  toggleOutputStyleAuto: (enabled) => api.post('/output-style/toggle-auto', { enabled }),
  styleHistory: (source) => api.get(withQuery('/soul/style/history', { source })),
  styleDiff: (source, from, to) =>
    api.get(withQuery('/soul/style/diff', { source, from_: from, to })),
  styleRollback: (source, version) => api.post('/soul/style/rollback', { source, version }),
  pending: () => api.get('/soul/pending'),
  confirmPending: (pendingId, approved) =>
    api.post('/soul/pending/confirm', { pending_id: pendingId, approved }),
  responseStrategy: () => api.get('/response-strategy'),
  profileReviewPending: (reviewType) =>
    api.get(withQuery('/profile-review/pending', { review_type: reviewType })),
  confirmProfileReview: (id) => api.post('/profile-review/confirm', { id }),
  rejectProfileReview: (id) => api.post('/profile-review/reject', { id }),
}
