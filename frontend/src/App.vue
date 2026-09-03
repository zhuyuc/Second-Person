<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { useToast } from '@/stores/toast'
import { useConfirm } from '@/stores/confirm'
import { chatApi } from '@/api/chat'
import SideChatDrawer from '@/components/SideChatDrawer.vue'

const router = useRouter()
const toast = useToast()
const confirm = useConfirm()
const health = ref('healthy')
const onboarded = ref(true)
const loading = ref(true)

async function refreshHealth() {
  try {
    const h = await chatApi.health()
    health.value = h.status
  } catch {
    health.value = 'unhealthy'
  }
}

let healthTimer = null

function startHealthTimer() {
  if (healthTimer) return
  healthTimer = setInterval(refreshHealth, 30000)
}
function stopHealthTimer() {
  if (healthTimer) {
    clearInterval(healthTimer)
    healthTimer = null
  }
}

// 页面隐藏时暂停健康轮询：省电、省流量；重新可见时立即刷新一次并恢复
function onVisibilityChange() {
  if (document.hidden) {
    stopHealthTimer()
  } else {
    refreshHealth()
    startHealthTimer()
  }
}

// 路由 idle prefetch：首屏空闲时预取 MemoryView / SettingsView 的 chunk
function prefetchRoutes() {
  const load = () => {
    import('./views/MemoryView.vue')
    import('./views/SettingsView.vue')
  }
  if (typeof window.requestIdleCallback === 'function') {
    window.requestIdleCallback(load, { timeout: 3000 })
  } else {
    setTimeout(load, 2000)
  }
}

onMounted(async () => {
  try {
    const st = await chatApi.onboardingStatus()
    onboarded.value = st.completed
  } catch {
    /* 服务未就绪时忽略，进入默认状态 */
  }
  await refreshHealth()
  startHealthTimer()
  document.addEventListener('visibilitychange', onVisibilityChange)
  loading.value = false
  prefetchRoutes()
})
onUnmounted(() => {
  stopHealthTimer()
  document.removeEventListener('visibilitychange', onVisibilityChange)
})

function onOnboarded() {
  onboarded.value = true
  router.push('/chat')
}
function copyTraceId(tid) {
  navigator.clipboard.writeText(tid).catch(() => {})
}

// 划词「侧边会话」：主视图 ChatView emit('open-aside') → 交给右侧抽屉开/续侧边会话
const asideDrawer = ref(null)
function onOpenAside(quote) {
  asideDrawer.value?.openAside(quote)
}
</script>

<template>
  <div v-if="!loading && !onboarded">
    <Onboarding @done="onOnboarded" />
  </div>
  <div v-else-if="!loading" class="app">
    <SessionSidebar :health="health" />
    <div class="main">
      <router-view v-slot="{ Component }">
        <keep-alive :max="2">
          <component :is="Component" @open-aside="onOpenAside" />
        </keep-alive>
      </router-view>
    </div>
    <SideChatDrawer ref="asideDrawer" />
  </div>

  <div class="toast-wrap">
    <div v-for="t in toast.items" :key="t.id" class="toast" :class="'toast-' + t.type">
      <span class="toast-message">{{ t.message }}</span>
      <button
        v-if="t.traceId && t.type === 'error'"
        type="button"
        class="toast-action"
        aria-label="复制 trace_id"
        title="复制 trace_id"
        @click="copyTraceId(t.traceId); toast.remove(t.id)"
      >
        复制ID
      </button>
      <button type="button" class="toast-close" aria-label="关闭提示" @click="toast.remove(t.id)">
        ×
      </button>
    </div>
  </div>

  <!-- 系统内置确认弹窗（替代原生 window.confirm） -->
  <BaseModal
    v-if="confirm.item"
    :title="confirm.item.title"
    size="sm"
    confirm-layer
    @close="confirm.cancel()"
  >
    <p class="confirm-message">{{ confirm.item.message }}</p>
    <label v-if="confirm.item.checkbox" class="confirm-check">
      <input v-model="confirm.checked" type="checkbox" />
      <span>{{ confirm.item.checkbox }}</span>
    </label>
    <template #footer>
      <button type="button" @click="confirm.cancel()">{{ confirm.item.cancelText }}</button>
      <button
        type="button"
        :class="confirm.item.danger ? 'dang' : 'btn-primary'"
        @click="confirm.confirm()"
      >
        {{ confirm.item.confirmText }}
      </button>
    </template>
  </BaseModal>
</template>
