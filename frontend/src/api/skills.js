import { api } from './client'

export const skillsApi = {
  list: (params = {}) => {
    const qs = new URLSearchParams()
    if (params.q) qs.set('q', params.q)
    if (params.category) qs.set('category', params.category)
    const q = qs.toString()
    return api.get(q ? `/skills?${q}` : '/skills')
  },
}
