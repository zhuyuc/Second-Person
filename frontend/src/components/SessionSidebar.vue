<script setup>
// 全局统一侧栏：产品名 / 新建对话 / 记忆 / 设置 / 历史会话（置顶+最近）
import { ref, computed, nextTick, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useSessions } from '@/stores/sessions'
import { useConfirm } from '@/stores/confirm'

const props = defineProps({ health: { type: String, default: 'healthy' } })

const route = useRoute()
const router = useRouter()
const sess = useSessions()
const confirmDialog = useConfirm()

const navs = [
  { path: '/memory', icon: 'ti-brain', label: '记忆' },
  { path: '/settings', icon: 'ti-settings', label: '设置' },
]
const lightColor = computed(() => ({
  healthy: 'var(--succtx)', degraded: 'var(--warntx)', unhealthy: 'var(--dangtx)',
}[props.health] || 'var(--muted)'))

// 点击产品名 / 新建对话：回到空白新对话（欢迎页）
function goHome() {
  window.dispatchEvent(new CustomEvent('sp-new-chat'))
  if (route.path !== '/chat') router.push('/chat')
}

// 点击历史会话：切到对话页并打开该会话
function openSession(sid) {
  sess.currentSid = sid
  if (route.path !== '/chat') router.push('/chat')
  window.dispatchEvent(new CustomEvent('sp-open-session', { detail: sid }))
}

// 会话分组：置顶区 / 渠道区（IM 来源）/ 最近区，各自可折叠
const collapsed = ref({ pinned: false, channel: false, recent: false })
const sessionGroups = computed(() => [
  { key: 'pinned', label: '置顶', items: sess.list.filter(s => s.pinned) },
  { key: 'channel', label: '渠道', items: sess.list.filter(s => !s.pinned && s.channel) },
  { key: 'recent', label: '最近', items: sess.list.filter(s => !s.pinned && !s.channel) },
])

// 渠道来源中文名（未知渠道直接显示原值）
const CHANNEL_NAMES = { feishu: '飞书', dingtalk: '钉钉', telegram: 'Telegram', wecom: '企业微信' }
function channelName(ch) { return CHANNEL_NAMES[ch] || ch }

// 会话项：更多菜单 / 重命名 / 置顶 / 删除
const menuId = ref(null)
const editingId = ref(null)
const editTitle = ref('')
function toggleMenu(sid) { menuId.value = menuId.value === sid ? null : sid }
function startRename(s) {
  editingId.value = s.session_id; editTitle.value = s.title; menuId.value = null
  nextTick(() => {
    const el = document.getElementById('rename-' + s.session_id)
    if (el) { el.focus(); el.select() }
  })
}
async function saveRename(s) {
  const t = editTitle.value.trim()
  editingId.value = null
  if (t && t !== s.title) {
    await api.post('/chat/session/rename', { session_id: s.session_id, title: t })
    await sess.load()
  }
}
async function togglePin(s) {
  menuId.value = null
  await api.post('/chat/session/pin', { session_id: s.session_id, pinned: !s.pinned })
  await sess.load()
}
async function deleteSession(sid) {
  menuId.value = null
  if (!await confirmDialog.ask({ message: '删除该会话？提炼出的记忆会保留。', danger: true })) return
  await api.del('/chat/session/' + sid)
  // 删除的是当前会话 → 通知 ChatView 清空回到欢迎页
  if (sess.currentSid === sid) window.dispatchEvent(new CustomEvent('sp-new-chat'))
  await sess.load()
}

onMounted(() => sess.load())
</script>

<template>
  <div class="side-panel">
    <!-- 产品名（点击回首页） -->
    <div class="side-brand" title="回首页" @click="goHome">
      <div class="brand-logo"><i class="ti ti-brain"></i></div>
      <span class="brand-name">Second Person</span>
      <span class="dot" :style="{ background: lightColor, width: '8px', height: '8px' }"
        :title="'系统状态：' + health"></span>
    </div>

    <!-- 新建对话 -->
    <button class="btn-primary side-new-chat" @click="goHome">
      <i class="ti ti-plus"></i> 新建对话</button>

    <!-- 记忆 / 设置 -->
    <div class="side-nav">
      <div v-for="n in navs" :key="n.path" class="side-nav-item" :class="{ active: route.path === n.path }"
        @click="router.push(n.path)">
        <i class="ti" :class="n.icon"></i><span>{{ n.label }}</span>
      </div>
    </div>

    <!-- 历史会话（置顶 / 渠道 / 最近） -->
    <div class="side-sess">
      <div v-if="!sess.list.length" class="empty" style="padding:32px 8px"><i class="ti ti-messages"></i>还没有会话</div>
      <div v-for="grp in sessionGroups" :key="grp.key" v-show="grp.items.length" style="margin-bottom:8px">
        <div class="sess-group-hd" @click="collapsed[grp.key] = !collapsed[grp.key]">
          <i class="ti" :class="collapsed[grp.key] ? 'ti-chevron-right' : 'ti-chevron-down'"></i>
          <span style="flex:1">{{ grp.label }}</span>
          <span class="sess-group-count">{{ grp.items.length }}</span>
        </div>
        <div v-show="!collapsed[grp.key]">
          <div v-for="s in grp.items" :key="s.session_id" class="sess-item"
            :class="{ active: s.session_id === sess.currentSid && route.path === '/chat' }"
            @click="openSession(s.session_id)" style="position:relative">
            <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:6px">
              <div style="min-width:0;flex:1">
                <div style="display:flex;align-items:center;gap:5px">
                  <i v-if="s.pinned" class="ti ti-pin" style="font-size:12px;color:var(--acctx);flex-shrink:0"></i>
                  <input v-if="editingId === s.session_id" :id="'rename-' + s.session_id" v-model="editTitle"
                    class="sess-rename-input" @click.stop @blur="saveRename(s)"
                    @keydown.enter.prevent="saveRename(s)" />
                  <div v-else class="sess-title"
                    style="font-size:13px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{{
                      s.title }}</div>
                  <span v-if="s.channel && editingId !== s.session_id" class="sess-channel-badge">{{
                    channelName(s.channel) }}</span>
                </div>
              </div>
              <i class="ti ti-dots sess-dots" @click.stop="toggleMenu(s.session_id)"></i>
            </div>
            <div v-if="menuId === s.session_id" class="sess-menu" @click.stop>
              <div @click="togglePin(s)"><i class="ti ti-pin"></i> {{ s.pinned ? '取消置顶' : '置顶' }}</div>
              <div @click="startRename(s)"><i class="ti ti-edit"></i> 重命名</div>
              <div class="dang" @click="deleteSession(s.session_id)"><i class="ti ti-trash"></i> 删除</div>
            </div>
          </div>
        </div>
      </div>
      <div v-if="menuId" @click="menuId = null" style="position:fixed;inset:0;z-index:15"></div>
    </div>
  </div>
</template>
