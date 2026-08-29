<script setup>
// 全局统一侧栏：产品名 / 新建 / 搜索 / 记忆 / 设置 / 历史会话（置顶+渠道+最近）
import { ref, computed, nextTick, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useSessions } from '@/stores/sessions'
import { useConfirm } from '@/stores/confirm'
import ChannelIcon from '@/components/ChannelIcon.vue'
import SessionSearchPanel from '@/components/SessionSearchPanel.vue'

const props = defineProps({ health: { type: String, default: 'healthy' } })

const route = useRoute()
const router = useRouter()
const sess = useSessions()
const confirmDialog = useConfirm()

const navs = [
  { path: '/memory', icon: 'ti-brain', label: '记忆' },
  { path: '/settings', icon: 'ti-settings', label: '设置' },
]

// 搜索面板开关：打开时用 SessionSearchPanel 顶替历史会话区
const searchOpen = ref(false)
function openSearch() { searchOpen.value = true }
function closeSearch() { searchOpen.value = false }
// 侧栏"搜索对话"点一次开、再点一次关（用户容易点开却找不到出口）
function toggleSearch() { searchOpen.value = !searchOpen.value }

// 全局快捷键：Ctrl/⌘+K 切换搜索（打开时再按一次收起，跟入口一致）
function onKeydown(e) {
  const key = e.key?.toLowerCase()
  if ((e.ctrlKey || e.metaKey) && key === 'k') {
    e.preventDefault()
    toggleSearch()
  }
}
const lightColor = computed(() => ({
  healthy: 'var(--succtx)', degraded: 'var(--warntx)', unhealthy: 'var(--dangtx)',
}[props.health] || 'var(--muted)'))

// 点击产品名 / 新建对话：回到空白新对话（欢迎页）；顺手收起搜索面板
function goHome() {
  closeSearch()
  window.dispatchEvent(new CustomEvent('sp-new-chat'))
  if (route.path !== '/chat') router.push('/chat')
}

// 点击历史会话：切到对话页并打开该会话
function openSession(sid) {
  closeSearch()
  sess.setCurrent(sid)
  if (route.path !== '/chat') router.push('/chat')
  window.dispatchEvent(new CustomEvent('sp-open-session', { detail: sid }))
}

// 路由切换（记忆/设置）：搜索面板自动收起，避免用户返回后残留
function goRoute(path) {
  closeSearch()
  router.push(path)
}

// 会话分组：置顶区 / 渠道区（IM 来源）/ 最近区，各自可折叠
const collapsed = ref({ pinned: false, channel: false, recent: false })
const sessionGroups = computed(() => [
  { key: 'pinned', label: '置顶', items: sess.list.filter(s => s.pinned) },
  { key: 'channel', label: '渠道', items: sess.list.filter(s => !s.pinned && s.channel) },
  { key: 'recent', label: '最近', items: sess.list.filter(s => !s.pinned && !s.channel) },
])

// 渠道来源中文名（未知渠道直接显示原值）
const CHANNEL_NAMES = { feishu: '飞书', dingtalk: '钉钉', telegram: 'Telegram', wecom: '企业微信', weixin: '微信' }
function channelName(ch) { return CHANNEL_NAMES[ch] || ch }

// 会话项：更多菜单 / 重命名 / 置顶 / 删除
const menuId = ref(null)
const menuPos = ref({ top: 0, left: 0, flipUp: false })
const editingId = ref(null)
const editTitle = ref('')
// 菜单最多 4 项（handoff/pin/rename/delete），估算 ~180px；父容器 .side-sess 有
// overflow:auto，用 position:fixed 逃出裁剪区，再按剩余空间决定上翻还是下翻
const MENU_ESTIMATED_HEIGHT = 180
const MENU_MIN_WIDTH = 168
const MENU_GAP = 4
function toggleMenu(sid, ev) {
  if (menuId.value === sid) { menuId.value = null; return }
  const rect = ev?.currentTarget?.getBoundingClientRect()
  if (rect) {
    const spaceBelow = window.innerHeight - rect.bottom
    const spaceAbove = rect.top
    const flipUp = spaceBelow < MENU_ESTIMATED_HEIGHT && spaceAbove > spaceBelow
    const top = flipUp ? rect.top - MENU_GAP : rect.bottom + MENU_GAP
    let left = rect.right - MENU_MIN_WIDTH
    if (left < 8) left = 8
    menuPos.value = { top, left, flipUp }
  }
  menuId.value = sid
}
function closeMenu() { menuId.value = null }
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

// 从此会话开启新会话（会话上下文管理方案 v2）
async function startHandoffFrom(sid) {
  menuId.value = null
  try {
    const d = await api.post('/chat/session/handoff', { from_session_id: sid })
    sess.setCurrent(d.new_session_id)
    if (route.path !== '/chat') router.push('/chat')
    window.dispatchEvent(new CustomEvent('sp-open-session', { detail: d.new_session_id }))
    await sess.load()
  } catch { /* api 层已提示 */ }
}

onMounted(() => {
  sess.load()
  window.addEventListener('keydown', onKeydown)
  // 侧栏滚动 / 视窗尺寸变化 → 菜单坐标失效，直接收起
  window.addEventListener('resize', closeMenu)
})
onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown)
  window.removeEventListener('resize', closeMenu)
})
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

    <!-- 顶部动作：新建对话 / 搜索对话（弱化为 nav 条目风格，与记忆/设置同级） -->
    <div class="side-nav side-nav-top">
      <div class="side-nav-item" @click="goHome">
        <i class="ti ti-plus"></i><span>新建对话</span>
      </div>
      <div class="side-nav-item" :class="{ active: searchOpen }"
        :title="searchOpen ? '再点一次收起搜索' : '打开搜索 (Ctrl/⌘+K)'"
        @click="toggleSearch">
        <i class="ti" :class="searchOpen ? 'ti-x' : 'ti-search'"></i>
        <span>{{ searchOpen ? '关闭搜索' : '搜索对话' }}</span>
        <span class="side-nav-kbd" title="Ctrl/⌘+K">⌘K</span>
      </div>
    </div>

    <!-- 记忆 / 设置 -->
    <div class="side-nav">
      <div v-for="n in navs" :key="n.path" class="side-nav-item" :class="{ active: route.path === n.path }"
        @click="goRoute(n.path)">
        <i class="ti" :class="n.icon"></i><span>{{ n.label }}</span>
      </div>
    </div>

    <!-- 搜索面板：打开时替换历史会话区 -->
    <div v-if="searchOpen" class="side-sess">
      <SessionSearchPanel @close="closeSearch" />
    </div>

    <!-- 历史会话（置顶 / 渠道 / 最近） -->
    <div v-else class="side-sess">
      <div v-if="!sess.list.length" class="empty" style="padding:32px 8px"><i class="ti ti-messages"></i>还没有会话<br>发送第一条消息开始吧</div>
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
            <div style="display:flex;align-items:center;gap:10px">
              <i v-if="s.pinned || !s.channel" class="ti sess-icon" :class="s.pinned ? 'ti-pin' : 'ti-message'"></i>
              <ChannelIcon v-else :platform="s.channel" :size="16" class="sess-icon" />
              <div style="min-width:0;flex:1">
                <div style="display:flex;align-items:center;gap:6px">
                  <input v-if="editingId === s.session_id" :id="'rename-' + s.session_id" v-model="editTitle"
                    class="sess-rename-input" @click.stop @blur="saveRename(s)"
                    @keydown.enter.prevent="saveRename(s)" />
                  <div v-else class="sess-title">{{ s.title }}</div>
                  <span v-if="s.channel && editingId !== s.session_id" class="sess-channel-badge">{{
                    channelName(s.channel) }}</span>
                  <span v-if="s.readonly && editingId !== s.session_id" class="sess-readonly-badge">已结束</span>
                </div>
              </div>
              <i class="ti ti-dots sess-dots" @click.stop="toggleMenu(s.session_id, $event)"></i>
            </div>
            <div v-if="menuId === s.session_id" class="sess-menu"
              :style="{ top: menuPos.top + 'px', left: menuPos.left + 'px',
                        transform: menuPos.flipUp ? 'translateY(-100%)' : 'none' }"
              @click.stop>
              <!-- 从此会话开启新会话（会话上下文管理方案 v2） -->
              <div v-if="!s.readonly" @click="startHandoffFrom(s.session_id)"><i class="ti ti-arrow-forward"></i> 从此会话开启新会话</div>
              <div @click="togglePin(s)"><i class="ti ti-pin"></i> {{ s.pinned ? '取消置顶' : '置顶' }}</div>
              <div @click="startRename(s)"><i class="ti ti-edit"></i> 重命名</div>
              <div class="dang" @click="deleteSession(s.session_id)"><i class="ti ti-trash"></i> 删除</div>
            </div>
          </div>
        </div>
      </div>
      <div v-if="menuId" @click="menuId = null" style="position:fixed;inset:0;z-index:var(--z-menu)"></div>
    </div>
  </div>
</template>
