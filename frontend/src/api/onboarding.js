import { api } from './client'

// 首次引导流程 API：Onboarding 组件专用
export const onboardingApi = {
  testConnection: (providerConfig) =>
    api.post('/onboarding/test-connection', { provider_config: providerConfig }),
  soulConfirm: (soul) => api.post('/onboarding/soul/confirm', soul),
}
