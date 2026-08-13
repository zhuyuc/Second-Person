<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useToast } from '@/stores/toast'
import { useConfirm } from '@/stores/confirm'
import Onboarding from '@/components/Onboarding.vue'
import SessionSidebar from '@/components/SessionSidebar.vue'
import BaseModal from '@/components/BaseModal.vue'

const router = useRouter()
const toast = useToast()
const confirm = useConfirm()
const health = ref('healthy')
const onboarded = ref(true)
const loading = ref(true)

async function refreshHealth() {
  try {
    const h = await api.get('/health')
    health.value = h.status
  } catch { health.value = 'unhealthy' }
}

let healthTimer = null
onMounted(async () => {
  try {
    const st = await api.get('/onboarding/status')
    onboarded.value = st.completed
  } catch { }
  await refreshHealth()
  healthTimer = setInterval(refreshHealth, 30000)
  loading.value = false
})
onUnmounted(() => { if (healthTimer) clearInterval(healthTimer) })

function onOnboarded() { onboarded.value = true; router.push('/chat') }
function copyTraceId(tid) { navigator.clipboard.writeText(tid).catch(() => { }) }
</script>

<template>
  <div v-if="!loading && !onboarded">
    <Onboarding @done="onOnboarded" />
  </div>
  <div class="app" v-else-if="!loading">
    <SessionSidebar :health="health" />
    <div class="main">
      <router-view v-slot="{ Component }">
        <keep-alive :max="2">
          <component :is="Component" />
        </keep-alive>
      </router-view>
    </div>
  </div>

  <div class="toast-wrap">
    <div v-for="t in toast.items" :key="t.id" class="toast" :class="'toast-' + t.type">
      <span class="toast-message">{{ t.message }}</span>
      <button v-if="t.traceId && t.type === 'error'" type="button" class="toast-action" aria-label="复制 trace_id"
        title="复制 trace_id" @click="copyTraceId(t.traceId); toast.remove(t.id)">复制ID</button>
      <button type="button" class="toast-close" aria-label="关闭提示" @click="toast.remove(t.id)">×</button>
    </div>
  </div>

  <!-- 系统内置确认弹窗（替代原生 window.confirm） -->
  <BaseModal v-if="confirm.item" :title="confirm.item.title" size="sm" confirm-layer @close="confirm.cancel()">
    <p class="confirm-message">{{ confirm.item.message }}</p>
    <label v-if="confirm.item.checkbox" class="confirm-check">
      <input v-model="confirm.checked" type="checkbox" />
      <span>{{ confirm.item.checkbox }}</span>
    </label>
    <template #footer>
      <button type="button" @click="confirm.cancel()">{{ confirm.item.cancelText }}</button>
      <button type="button" :class="confirm.item.danger ? 'dang' : 'btn-primary'" @click="confirm.confirm()">{{
        confirm.item.confirmText }}</button>
    </template>
  </BaseModal>
</template>
