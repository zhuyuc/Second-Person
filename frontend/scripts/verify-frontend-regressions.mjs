import { readFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(fileURLToPath(new URL('.', import.meta.url)), '..')

async function source(relativePath) {
  return readFile(resolve(root, relativePath), 'utf8')
}

function expect(condition, message) {
  if (!condition) throw new Error(message)
}

const [chat, canvas, sessions, modal, styles, chatApi, memoryView, settingsView, onboarding] = await Promise.all([
  source('src/views/ChatView.vue'),
  source('src/components/graph/CanvasGraph.vue'),
  source('src/stores/sessions.js'),
  source('src/components/BaseModal.vue'),
  source('src/style.css'),
  source('src/api/chat.js'),
  source('src/views/MemoryView.vue'),
  source('src/views/SettingsView.vue'),
  source('src/components/Onboarding.vue'),
])

expect(
  chat.includes("const AsyncDiagramRenderer = defineAsyncComponent(() => import('@/components/diagram/DiagramRenderer.vue'))"),
  'DiagramRenderer must remain lazy-loaded'
)
expect(
  !chat.includes("import DiagramRenderer from '@/components/diagram/DiagramRenderer.vue'"),
  'DiagramRenderer must not be statically imported'
)
expect(chat.includes('let openSessionToken = 0'), 'Session switching must guard stale responses')
expect(chat.includes('let sendStarting = false'), 'New-session sends must have a synchronous lock')
expect(!chat.includes("document.querySelector('textarea')"), 'Composer must use its scoped textarea ref')

expect(canvas.includes('function requestFrame()'), 'Canvas must schedule frames on demand')
expect(
  canvas.includes('if (simulation.running.value || pulseNode || entryAnimating) requestFrame()'),
  'Canvas must only continue frames for active simulation or animation'
)

expect(sessions.includes('const SESSION_PAGE_SIZE = 50'), 'Session list page size must stay bounded')
expect(sessions.includes('nextCursor'), 'Session list must retain cursor pagination state')
expect(sessions.includes('loadMore()'), 'Session list must expose incremental loading')
expect(sessions.includes("cursor: reset ? '' : this.nextCursor"), 'First session page must use keyset pagination')
expect(sessions.includes('chatApi.sessionSummary(sid)'), 'Title refresh must use the targeted summary endpoint')
expect(chatApi.includes('new URLSearchParams({'), 'Session API must preserve the empty keyset cursor')

expect(modal.includes("if (e.key !== 'Tab') return"), 'Modal must trap Tab navigation')
expect(styles.includes('@media (prefers-reduced-motion: reduce)'), 'Reduced-motion preference must be respected')
expect(memoryView.includes('<TabbedPageHeader'), 'MemoryView must use the shared tab header')
expect(settingsView.includes('<TabbedPageHeader'), 'SettingsView must use the shared tab header')

expect(onboarding.includes('第 {{ step }}/2 步'), 'Onboarding must remain a two-step flow')
expect(onboarding.includes("const current = await soulApi.soul()"), 'Onboarding must load the existing SOUL draft')
expect(!onboarding.includes('Embedding'), 'Embedding configuration must stay in Settings')
expect(!onboarding.includes('welcome'), 'Welcome chat must not be part of onboarding')

console.log('frontend performance and accessibility regression checks passed')
