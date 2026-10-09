<script setup>
// 对话搜索浮层：居中弹层（参考 DeepSeek 搜索），三路命中 + 高亮
import { ref, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useSessions } from '@/stores/sessions'
import { chatApi } from '@/api/chat'
import { sanitizeHtml } from '@/utils/sanitize'
import { formatCompactTime } from '@/utils/format'

const emit = defineEmits(['close'])

const route = useRoute()
const router = useRouter()
const sess = useSessions()

const q = ref('')
const scope = ref('all')
const loading = ref(false)
const results = ref([])
const totalSessions = ref(0)
const hasQueried = ref(false)
const inputRef = ref(null)

const SCOPES = [
  { key: 'all', label: '全部' },
  { key: 'title', label: '标题' },
  { key: 'user', label: '我的提问' },
  { key: 'assistant', label: 'AI 回复' },
]

let debounceTimer = null
function scheduleFetch() {
  window.clearTimeout(debounceTimer)
  debounceTimer = window.setTimeout(fetchSearch, 250)
}

async function fetchSearch() {
  const query = q.value.trim()
  if (!query) {
    results.value = []
    totalSessions.value = 0
    hasQueried.value = false
    loading.value = false
    return
  }
  loading.value = true
  try {
    const d = await chatApi.search({ q: query, scope: scope.value, limit: 50 })
    results.value = d?.sessions || []
    totalSessions.value = d?.total_sessions || 0
    hasQueried.value = true
  } catch {
    results.value = []
    totalSessions.value = 0
    hasQueried.value = true
  } finally {
    loading.value = false
  }
}

watch(q, scheduleFetch)
watch(scope, fetchSearch)

function clearQuery() {
  q.value = ''
  results.value = []
  totalSessions.value = 0
  hasQueried.value = false
  nextTick(() => inputRef.value?.focus())
}

function close() {
  emit('close')
}

function onKeyDown(e) {
  if (e.key === 'Escape') {
    e.stopPropagation()
    if (q.value) clearQuery()
    else close()
  }
}

function openSession(sid, messageId = null) {
  sess.setCurrent(sid)
  if (route.path !== '/chat') router.push('/chat')
  window.dispatchEvent(
    new CustomEvent('sp-open-session', {
      detail: messageId ? { sid, messageId } : sid,
    })
  )
  close()
}

function renderHtml(html) {
  return sanitizeHtml(html || '')
}

function previewSnippet(r) {
  const hit = (r.hits || [])[0]
  if (hit?.snippet_html) return hit.snippet_html
  return ''
}

function formatSearchDate(iso) {
  if (!iso) return ''
  const d = new Date(String(iso).replace(' ', 'T'))
  if (isNaN(d.getTime())) return ''
  const now = new Date()
  if (d.toDateString() === now.toDateString()) return formatCompactTime(iso)
  const m = d.getMonth() + 1
  const day = d.getDate()
  if (d.getFullYear() === now.getFullYear()) return `${m}月${day}日`
  return `${d.getFullYear()}年${m}月${day}日`
}

onMounted(() => {
  nextTick(() => inputRef.value?.focus())
})
onUnmounted(() => window.clearTimeout(debounceTimer))

defineExpose({ focus: () => inputRef.value?.focus() })
</script>

<template>
  <Teleport to="body">
    <div class="sess-search-overlay" @keydown="onKeyDown" @mousedown.self="close">
      <div
        class="sess-search-modal"
        role="dialog"
        aria-modal="true"
        aria-label="搜索对话"
        @mousedown.stop
      >
        <div class="sess-search-input-wrap">
          <i class="ti ti-search"></i>
          <input
            ref="inputRef"
            v-model="q"
            class="sess-search-input"
            placeholder="搜索标题、我的提问、AI 回复…"
          />
          <i
            v-if="q"
            class="ti ti-x sess-search-clear"
            title="清空 (Esc)"
            @click="clearQuery"
          ></i>
        </div>

        <div class="sess-search-scopes">
          <button
            v-for="s in SCOPES"
            :key="s.key"
            type="button"
            class="sess-search-chip"
            :class="{ active: scope === s.key }"
            @click="scope = s.key"
          >
            {{ s.label }}
          </button>
        </div>

        <div class="sess-search-body">
          <div v-if="loading" class="sess-search-hint">
            <i class="ti ti-loader-2 sp-spin"></i> 搜索中…
          </div>
          <div v-else-if="!q.trim()" class="sess-search-hint">
            输入关键字，同时命中会话标题、你的提问与 AI 回复。
          </div>
          <div v-else-if="hasQueried && !results.length" class="sess-search-hint">
            没有找到匹配「{{ q.trim() }}」的会话
          </div>
          <div v-else class="sess-search-list">
            <div
              v-for="r in results"
              :key="r.session_id"
              class="sess-search-row"
              :class="{ active: r.session_id === sess.currentSid && route.path === '/chat' }"
              @click="openSession(r.session_id, r.hits?.[0]?.message_id)"
            >
              <div class="sess-search-row-icon">
                <i
                  v-if="r.pinned || !r.channel"
                  class="ti"
                  :class="r.pinned ? 'ti-pin' : 'ti-message'"
                ></i>
                <ChannelIcon v-else :platform="r.channel" :size="16" />
              </div>
              <div class="sess-search-row-main">
                <div class="sess-search-row-top">
                  <div class="sess-search-title" v-html="renderHtml(r.title_html)"></div>
                  <span class="sess-search-date">{{ formatSearchDate(r.last_active) }}</span>
                </div>
                <div
                  v-if="previewSnippet(r)"
                  class="sess-search-preview"
                  v-html="renderHtml(previewSnippet(r))"
                ></div>
                <div v-else-if="r.title_hit" class="sess-search-preview muted">标题命中</div>
              </div>
            </div>
            <div v-if="totalSessions" class="sess-search-summary">
              {{ totalSessions }} 个会话命中
            </div>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>
