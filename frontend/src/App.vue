<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useToast } from '@/stores/toast'
import { useConfirm } from '@/stores/confirm'
import Onboarding from '@/components/Onboarding.vue'
import SessionSidebar from '@/components/SessionSidebar.vue'

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

onMounted(async () => {
  try {
    const st = await api.get('/onboarding/status')
    onboarded.value = st.completed
  } catch { }
  await refreshHealth()
  setInterval(refreshHealth, 30000)
  loading.value = false
})

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
        <keep-alive>
          <component :is="Component" />
        </keep-alive>
      </router-view>
    </div>
  </div>

  <div class="toast-wrap">
    <div v-for="t in toast.items" :key="t.id" class="toast" :class="'toast-' + t.type">
      <span style="flex:1;min-width:0;word-break:break-word">{{ t.message }}</span>
      <span v-if="t.traceId && t.type === 'error'"
        style="cursor:pointer;font-size:var(--fs-xs);opacity:.7;margin-right:6px" title="复制 trace_id"
        @click="copyTraceId(t.traceId); toast.remove(t.id)">复制ID</span>
      <span style="cursor:pointer" @click="toast.remove(t.id)">×</span>
    </div>
  </div>

  <!-- 系统内置确认弹窗（替代原生 window.confirm） -->
  <div v-if="confirm.item" class="overlay" style="z-index:var(--z-confirm)" @click.self="confirm.cancel()">
    <div class="modal modal-sm">
      <div class="mt">{{ confirm.item.title }}</div>
      <p style="color:var(--sec);margin:6px 0 20px;line-height:1.6;white-space:pre-wrap">{{ confirm.item.message }}</p>
      <label v-if="confirm.item.checkbox" class="fg"
        style="gap:8px;align-items:flex-start;cursor:pointer;margin:-10px 0 18px;font-size:var(--fs-base);color:var(--sec)">
        <input v-model="confirm.checked" type="checkbox" style="width:auto;margin-top:3px;flex-shrink:0" />
        <span>{{ confirm.item.checkbox }}</span>
      </label>
      <div class="fg" style="justify-content:flex-end;gap:8px">
        <button @click="confirm.cancel()">{{ confirm.item.cancelText }}</button>
        <button :class="confirm.item.danger ? 'dang' : 'btn-primary'" @click="confirm.confirm()">{{
          confirm.item.confirmText }}</button>
      </div>
    </div>
  </div>
</template>
