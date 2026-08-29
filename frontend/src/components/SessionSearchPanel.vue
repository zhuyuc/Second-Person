<script setup>
// 侧栏搜索面板：三路命中（标题 / 用户消息 / AI 回复）+ 高亮
// 打开时替换 SessionSidebar 的历史会话区域；关闭由父级控制
import { ref, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useSessions } from '@/stores/sessions'
import { withQuery } from '@/utils/query'
import { sanitizeHtml } from '@/utils/sanitize'
import ChannelIcon from '@/components/ChannelIcon.vue'

const emit = defineEmits(['close'])

const route = useRoute()
const router = useRouter()
const sess = useSessions()

const q = ref('')
const scope = ref('all')
const loading = ref(false)
const results = ref([])          // [{session_id, title, title_html, hits, ...}]
const totalSessions = ref(0)
const hasQueried = ref(false)
const inputRef = ref(null)

const SCOPES = [
  { key: 'all', label: '全部' },
  { key: 'title', label: '标题' },
  { key: 'user', label: '我的提问' },
  { key: 'assistant', label: 'AI 回复' },
]

const CHANNEL_NAMES = { feishu: '飞书', dingtalk: '钉钉', telegram: 'Telegram', wecom: '企业微信', weixin: '微信' }
function channelName(ch) { return CHANNEL_NAMES[ch] || ch }

// 防抖 250ms；空查询清空结果、不发请求
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
    const d = await api.get(withQuery('/chat/search', {
      q: query, scope: scope.value, limit: 50,
    }))
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

function onKeyDown(e) {
  // Esc：先清空输入，若已空则关闭面板
  if (e.key === 'Escape') {
    if (q.value) clearQuery()
    else emit('close')
  }
}

function openSession(sid, messageId = null) {
  sess.setCurrent(sid)
  if (route.path !== '/chat') router.push('/chat')
  window.dispatchEvent(new CustomEvent('sp-open-session', {
    detail: messageId ? { sid, messageId } : sid,
  }))
}

function renderHtml(html) {
  return sanitizeHtml(html || '')
}

function roleLabel(role) {
  return role === 'user' ? '我' : role === 'assistant' ? 'AI' : role
}

function fmtTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const now = new Date()
  const sameDay = d.toDateString() === now.toDateString()
  const pad = (n) => String(n).padStart(2, '0')
  if (sameDay) return `${pad(d.getHours())}:${pad(d.getMinutes())}`
  return `${d.getMonth() + 1}/${d.getDate()}`
}

onMounted(() => {
  nextTick(() => inputRef.value?.focus())
})
onUnmounted(() => window.clearTimeout(debounceTimer))

defineExpose({ focus: () => inputRef.value?.focus() })
</script>

<template>
  <div class="sess-search" @keydown="onKeyDown">
    <div class="sess-search-hd">
      <i class="ti ti-arrow-left sess-search-back" title="返回会话列表 (Esc)"
        @click="$emit('close')"></i>
      <span class="sess-search-hd-title">搜索对话</span>
    </div>
    <div class="sess-search-input-wrap">
      <i class="ti ti-search"></i>
      <input ref="inputRef" v-model="q" class="sess-search-input"
        placeholder="搜索标题、我的提问、AI 回复…" />
      <i v-if="q" class="ti ti-x sess-search-clear" title="清空 (Esc)"
        @click="clearQuery"></i>
    </div>
    <div class="sess-search-scopes">
      <button v-for="s in SCOPES" :key="s.key" type="button"
        class="sess-search-chip" :class="{ active: scope === s.key }"
        @click="scope = s.key">{{ s.label }}</button>
    </div>

    <div class="sess-search-body">
      <div v-if="loading" class="sess-search-hint">
        <i class="ti ti-loader-2 sp-spin"></i> 搜索中…
      </div>
      <div v-else-if="!q.trim()" class="sess-search-hint">
        输入关键字，同时命中会话标题、你的提问与 AI 回复。
      </div>
      <div v-else-if="hasQueried && !results.length" class="sess-search-hint">
        没有找到匹配"{{ q.trim() }}"的会话
      </div>
      <div v-else>
        <div class="sess-search-summary" v-if="totalSessions">
          {{ totalSessions }} 个会话命中
        </div>
        <div v-for="r in results" :key="r.session_id" class="sess-search-card"
          :class="{ active: r.session_id === sess.currentSid && route.path === '/chat' }">
          <div class="sess-search-card-hd" @click="openSession(r.session_id)">
            <i v-if="r.pinned || !r.channel" class="ti sess-icon"
              :class="r.pinned ? 'ti-pin' : 'ti-message'"></i>
            <ChannelIcon v-else :platform="r.channel" :size="16" class="sess-icon" />
            <div class="sess-search-title" v-html="renderHtml(r.title_html)"></div>
            <span v-if="r.channel" class="sess-channel-badge">{{ channelName(r.channel) }}</span>
            <span v-if="r.readonly" class="sess-readonly-badge">已结束</span>
            <span class="sess-search-count" v-if="r.hit_count">{{ r.hit_count }} 处</span>
          </div>
          <div v-for="h in r.hits" :key="h.message_id" class="sess-search-hit"
            @click="openSession(r.session_id, h.message_id)">
            <span class="sess-search-role" :class="'role-' + h.role">{{ roleLabel(h.role) }}</span>
            <span class="sess-search-time">{{ fmtTime(h.created_at) }}</span>
            <span class="sess-search-snip" v-html="renderHtml(h.snippet_html)"></span>
          </div>
          <div v-if="r.hit_count > r.hits.length" class="sess-search-more"
            @click="openSession(r.session_id)">
            还有 {{ r.hit_count - r.hits.length }} 处命中，打开会话查看
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
