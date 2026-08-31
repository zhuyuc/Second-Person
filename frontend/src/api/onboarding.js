import { api } from './client'

// 首次引导流程 API：Onboarding 组件专用
export const onboardingApi = {
  welcomeStart: () => api.post('/onboarding/welcome-chat/start', {}),
  welcomeFinish: () => api.post('/onboarding/welcome-chat/finish', {}),
  testConnection: (providerConfig) =>
    api.post('/onboarding/test-connection', { provider_config: providerConfig }),
  testEmbedding: (providerConfig) =>
    api.post('/onboarding/test-embedding', { provider_config: providerConfig }),
  soulConfirm: (soul) => api.post('/onboarding/soul/confirm', soul),
}
