<script setup>
// 全局统一侧栏：产品名 / 新建 / 搜索 / 记忆 / 设置 / 工作区 / 历史会话（置顶+渠道+最近）
import { ref, computed, nextTick, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useSessions } from '@/stores/sessions'
import { useProjects } from '@/stores/projects'
import { useConfirm } from '@/stores/confirm'
import { useToast } from '@/stores/toast'
import { chatApi } from '@/api/chat'

const props = defineProps({ health: { type: String, default: 'healthy' } })

const route = useRoute()
const router = useRouter()
const sess = useSessions()
const projStore = useProjects()
const confirmDialog = useConfirm()
const toast = useToast()

const navs = [
  { path: '/memory', icon: 'ti-brain', label: '记忆' },
  { path: '/settings', icon: 'ti-settings', label: '设置' },
]

// 搜索面板开关：打开时用 SessionSearchPanel 顶替历史会话区
const searchOpen = ref(false)
function closeSearch() {
  searchOpen.value = false
}
// 侧栏"搜索对话"点一次开、再点一次关（用户容易点开却找不到出口）
function toggleSearch() {
  searchOpen.value = !searchOpen.value
  if (searchOpen.value) expandSection('sess')
}

// 全局快捷键：
// - Ctrl/⌘+K   搜索
// - Ctrl/⌘+P   快速切项目（弹项目选择器）
// - Ctrl/⌘+Shift+N 在当前项目下新建会话（无项目 → 新对话）
const projectSwitcherOpen = ref(false)
const projectSwitcherQuery = ref('')
const projectSwitcherHi = ref(0)
const projectSwitcherList = computed(() => {
  const q = (projectSwitcherQuery.value || '').toLowerCase().trim()
  const items = [{ id: null, title: '（无项目）新对话' }, ...projStore.list]
  if (!q) return items
  return items.filter(
    (p) => (p.title || '').toLowerCase().includes(q) || (p.path || '').toLowerCase().includes(q)
  )
})
function switchToProject(p) {
  projectSwitcherOpen.value = false
  projectSwitcherQuery.value = ''
  if (p.id) newSessionInProject(p.id)
  else goHome()
}
function projectSwitcherKey(e) {
  if (!projectSwitcherOpen.value) return
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    projectSwitcherHi.value = Math.min(
      projectSwitcherHi.value + 1,
      projectSwitcherList.value.length - 1
    )
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    projectSwitcherHi.value = Math.max(0, projectSwitcherHi.value - 1)
  } else if (e.key === 'Enter') {
    e.preventDefault()
    const p = projectSwitcherList.value[projectSwitcherHi.value]
    if (p) switchToProject(p)
  } else if (e.key === 'Escape') {
    projectSwitcherOpen.value = false
  }
}

function onKeydown(e) {
  const key = e.key?.toLowerCase()
  const mod = e.ctrlKey || e.metaKey
  if (mod && key === 'k' && !e.shiftKey) {
    e.preventDefault()
    toggleSearch()
    return
  }
  if (mod && key === 'p' && !e.shiftKey) {
    e.preventDefault()
    projectSwitcherOpen.value = !projectSwitcherOpen.value
    projectSwitcherQuery.value = ''
    projectSwitcherHi.value = 0
    return
  }
  if (mod && e.shiftKey && key === 'n') {
    e.preventDefault()
    // 若当前会话属于某项目 → 在该项目下新建；否则空白新对话
    const cur = sess.list.find((s) => s.session_id === sess.currentSid)
    if (cur && cur.project_id) newSessionInProject(cur.project_id)
    else goHome()
    return
  }
  if (projectSwitcherOpen.value) projectSwitcherKey(e)
}

// 手动归档单个会话（archived_source='manual'，独立于项目归档）
async function archiveManual(sid) {
  menuId.value = null
  const ok = await confirmDialog.ask({
    message: '归档该会话？归档后不会出现在侧栏，可在「设置 → 会话搜索包含已归档」中找回。',
  })
  if (!ok) return
  try {
    await chatApi.archiveSession(sid)
    if (sess.currentSid === sid) window.dispatchEvent(new CustomEvent('sp-new-chat'))
    sess.removeLocal(sid)
  } catch {
    /* toast */
  }
}
const lightColor = computed(
  () =>
    ({
      healthy: 'var(--succtx)',
      degraded: 'var(--warntx)',
      unhealthy: 'var(--dangtx)',
    })[props.health] || 'var(--muted)'
)

// 点击产品名 / 新建对话：回到空白新对话（欢迎页）；顺手收起搜索面板
function goHome() {
  closeSearch()
  window.dispatchEvent(new CustomEvent('sp-new-chat'))
  if (route.path !== '/chat') router.push('/chat')
}

// 点击历史会话：切到对话页并打开该会话
function openSession(sid) {
  closeSearch()
  // M5.1：__pending__ 是「待建」占位，只导航不改状态
  if (sid === '__pending__') {
    if (route.path !== '/chat') router.push('/chat')
    return
  }
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
// v5: 仅显示无项目会话（project_id=NULL）；项目会话在上方「工作区」段展示
const collapsed = ref({ pinned: false, channel: false, recent: false })
const sessionGroups = computed(() => {
  const bare = sess.list.filter((s) => !s.project_id)
  return [
    { key: 'pinned', label: '置顶', items: bare.filter((s) => s.pinned) },
    { key: 'channel', label: '渠道', items: bare.filter((s) => !s.pinned && s.channel) },
    { key: 'recent', label: '最近', items: bare.filter((s) => !s.pinned && !s.channel) },
  ]
})

// 工作区：active 项目 + 各自会话（按 last_active 倒序）
const wsCollapsedKey = 'sp_ws_collapsed'
const wsCollapsed = ref(JSON.parse(localStorage.getItem(wsCollapsedKey) || '{}'))
function toggleWs(pid) {
  wsCollapsed.value[pid] = !wsCollapsed.value[pid]
  localStorage.setItem(wsCollapsedKey, JSON.stringify(wsCollapsed.value))
}

// 区块级展开/收起（工作区 / 会话区），localStorage 持久化
const sectionCollapsedKey = 'sp_section_collapsed'
const sectionCollapsed = ref(JSON.parse(localStorage.getItem(sectionCollapsedKey) || '{}'))
function toggleSection(key) {
  sectionCollapsed.value[key] = !sectionCollapsed.value[key]
  localStorage.setItem(sectionCollapsedKey, JSON.stringify(sectionCollapsed.value))
}
function expandSection(key) {
  if (!sectionCollapsed.value[key]) return
  sectionCollapsed.value[key] = false
  localStorage.setItem(sectionCollapsedKey, JSON.stringify(sectionCollapsed.value))
}
const workspaceProjects = computed(() => {
  const byProject = {}
  for (const s of sess.list) {
    if (s.project_id) (byProject[s.project_id] ??= []).push(s)
  }
  return projStore.list.map((p) => {
    const list = (byProject[p.id] || []).sort((a, b) =>
      (b.last_active || '').localeCompare(a.last_active || '')
    )
    // M5.1：在选中「待建」项目会话时，展示一个占位 pending 会话，
    // 用户首条消息发送后被真实 session 替换（sess.load 触发）
    if (sess.pendingProjectId === p.id && !sess.currentSid) {
      list.unshift({
        session_id: '__pending__',
        title: '新对话',
        pinned: false,
        readonly: false,
        channel: null,
        project_id: p.id,
        archived: false,
        is_pending: true,
      })
    }
    return { ...p, sessions: list }
  })
})

// 添加项目对话框
const addProjOpen = ref(false)
function openAddProject() {
  addProjOpen.value = true
}
async function onProjectCreated(p) {
  addProjOpen.value = false
  expandSection('ws')
  await projStore.load()
  toast.push('success', `项目「${p.title}」已加载`)
}

// 项目菜单
const projMenuId = ref(null)
const projMenuPos = ref({ top: 0, left: 0 })
function toggleProjMenu(pid, ev) {
  if (projMenuId.value === pid) {
    projMenuId.value = null
    return
  }
  const rect = ev?.currentTarget?.getBoundingClientRect()
  if (rect) {
    projMenuPos.value = {
      top: rect.bottom + 4,
      left: Math.max(8, rect.right - 168),
    }
  }
  projMenuId.value = pid
}
function closeProjMenu() {
  projMenuId.value = null
}

function newSessionInProject(pid) {
  // M5.1：延迟建 —— 只记 pendingProjectId + 走欢迎页；
  // 首条消息 send 时才 POST /chat/send 由后端带 project_id 建库
  closeProjMenu()
  sess.setPendingProject(pid)
  sess.setCurrent(null)
  window.dispatchEvent(new CustomEvent('sp-new-chat', { detail: { projectId: pid } }))
  if (route.path !== '/chat') router.push('/chat')
}

const renamingProjId = ref(null)
const renameProjTitle = ref('')
function startRenameProject(p) {
  closeProjMenu()
  renamingProjId.value = p.id
  renameProjTitle.value = p.title
  nextTick(() => {
    const el = document.getElementById('proj-rename-' + p.id)
    if (el) {
      el.focus()
      el.select()
    }
  })
}
async function saveRenameProject(p) {
  const t = renameProjTitle.value.trim()
  renamingProjId.value = null
  if (t && t !== p.title) {
    try {
      await projStore.rename(p.id, t)
    } catch {
      /* toast 已弹 */
    }
  }
}

async function archiveProject(p) {
  closeProjMenu()
  const ok = await confirmDialog.ask({
    title: '归档项目',
    message: `归档「${p.title}」后，该项目下所有会话也会同步归档。可在设置 → 已归档项目中恢复或永久删除。`,
    confirmText: '归档',
    cancelText: '取消',
  })
  if (!ok) return
  try {
    const r = await projStore.archive(p.id)
    await sess.load()
    toast.push('success', `已归档，联动归档 ${r.archived_sessions} 个会话`)
  } catch {
    /* toast 已弹 */
  }
}

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
  if (menuId.value === sid) {
    menuId.value = null
    return
  }
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
function closeMenu() {
  menuId.value = null
}
function startRename(s) {
  editingId.value = s.session_id
  editTitle.value = s.title
  menuId.value = null
  nextTick(() => {
    const el = document.getElementById('rename-' + s.session_id)
    if (el) {
      el.focus()
      el.select()
    }
  })
}
async function saveRename(s) {
  const t = editTitle.value.trim()
  editingId.value = null
  if (t && t !== s.title) {
    await chatApi.renameSession(s.session_id, t)
    sess.applyPatch(s.session_id, { title: t, title_source: 'user' })
  }
}
async function togglePin(s) {
  menuId.value = null
  const newPinned = !s.pinned
  await chatApi.pinSession(s.session_id, newPinned)
  sess.applyPatch(s.session_id, { pinned: newPinned })
}
async function deleteSession(sid) {
  menuId.value = null
  if (!(await confirmDialog.ask({ message: '删除该会话？提炼出的记忆会保留。', danger: true })))
    {return}
  await chatApi.deleteSession(sid)
  // 删除的是当前会话 → 通知 ChatView 清空回到欢迎页
  if (sess.currentSid === sid) window.dispatchEvent(new CustomEvent('sp-new-chat'))
  sess.removeLocal(sid)
}

// 从此会话开启新会话（会话上下文管理方案 v2）
async function startHandoffFrom(sid) {
  menuId.value = null
  try {
    const d = await chatApi.handoff(sid)
    sess.setCurrent(d.new_session_id)
    if (route.path !== '/chat') router.push('/chat')
    window.dispatchEvent(new CustomEvent('sp-open-session', { detail: d.new_session_id }))
    await sess.load()
  } catch {
    /* api 层已提示 */
  }
}

onMounted(() => {
  sess.load()
  projStore.load()
  window.addEventListener('keydown', onKeydown)
  window.addEventListener('resize', closeMenu)
  window.addEventListener('resize', closeProjMenu)
  // 归档 / 恢复 / 永久删除后（在设置页触发）会派发此事件
  window.addEventListener('sp-projects-changed', onProjectsChanged)
})
onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown)
  window.removeEventListener('resize', closeMenu)
  window.removeEventListener('resize', closeProjMenu)
  window.removeEventListener('sp-projects-changed', onProjectsChanged)
})
async function onProjectsChanged() {
  await projStore.load()
  await sess.load()
}
</script>

<template>
  <div class="side-panel">
    <!-- 产品名（点击回首页） -->
    <div class="side-brand" title="回首页" @click="goHome">
      <div class="brand-logo"><i class="ti ti-brain"></i></div>
      <span class="brand-name">Second Person</span>
      <span
        class="dot"
        :style="{ background: lightColor, width: '8px', height: '8px' }"
        :title="'系统状态：' + health"
      ></span>
    </div>

    <!-- 顶部动作：新建对话 / 搜索对话（弱化为 nav 条目风格，与记忆/设置同级） -->
    <div class="side-nav side-nav-top">
      <div class="side-nav-item" @click="goHome">
        <i class="ti ti-plus"></i><span>新建对话</span>
      </div>
      <div
        class="side-nav-item"
        :class="{ active: searchOpen }"
        :title="searchOpen ? '再点一次收起搜索' : '打开搜索 (Ctrl/⌘+K)'"
        @click="toggleSearch"
      >
        <i class="ti" :class="searchOpen ? 'ti-x' : 'ti-search'"></i>
        <span>{{ searchOpen ? '关闭搜索' : '搜索对话' }}</span>
        <span class="side-nav-kbd" title="Ctrl/⌘+K">⌘K</span>
      </div>
    </div>

    <!-- 记忆 / 设置 -->
    <div class="side-nav">
      <div
        v-for="n in navs"
        :key="n.path"
        class="side-nav-item"
        :class="{ active: route.path === n.path }"
        @click="goRoute(n.path)"
      >
        <i class="ti" :class="n.icon"></i><span>{{ n.label }}</span>
      </div>
    </div>

    <!-- 工作区（v5 新增） -->
    <div
      v-if="!searchOpen"
      class="side-workspace"
      :class="{ 'ws-grow': sectionCollapsed.sess && !sectionCollapsed.ws }"
    >
      <div class="ws-hd">
        <div class="hd-toggle" @click="toggleSection('ws')">
          <i class="ti" :class="sectionCollapsed.ws ? 'ti-chevron-right' : 'ti-chevron-down'"></i>
          <span>工作区</span>
        </div>
        <i class="ti ti-plus ws-add" title="添加项目" @click="openAddProject"></i>
      </div>
      <div v-show="!sectionCollapsed.ws">
        <div v-if="!workspaceProjects.length" class="ws-empty">
          还没有工作区项目<br />点击 + 添加一个本地目录
        </div>
        <div v-for="p in workspaceProjects" :key="p.id" class="ws-project">
          <div class="ws-project-row" @click="toggleWs(p.id)">
            <i class="ti" :class="wsCollapsed[p.id] ? 'ti-chevron-right' : 'ti-chevron-down'"></i>
            <i class="ti ti-folder" :class="{ 'ws-missing': p.path_missing }"></i>
            <input
              v-if="renamingProjId === p.id"
              :id="'proj-rename-' + p.id"
              v-model="renameProjTitle"
              class="sess-rename-input"
              @click.stop
              @blur="saveRenameProject(p)"
              @keydown.enter.prevent="saveRenameProject(p)"
            />
            <span v-else class="ws-title" :title="p.path">{{ p.title }}</span>
            <span v-if="p.path_missing" class="ws-badge-miss" title="目录已丢失">丢失</span>
            <span class="ws-count">[{{ p.session_count }}]</span>
            <i class="ti ti-dots ws-dots" @click.stop="toggleProjMenu(p.id, $event)"></i>
          </div>
          <div v-if="!wsCollapsed[p.id]" class="ws-sessions">
            <div
              v-for="s in p.sessions"
              :key="s.session_id"
              class="sess-item"
              :class="{ active: s.session_id === sess.currentSid && route.path === '/chat' }"
              style="position: relative"
              @click="openSession(s.session_id)"
            >
              <div style="display: flex; align-items: center; gap: 10px">
                <i class="ti sess-icon" :class="s.pinned ? 'ti-pin' : 'ti-message'"></i>
                <div style="min-width: 0; flex: 1">
                  <div style="display: flex; align-items: center; gap: 6px">
                    <input
                      v-if="editingId === s.session_id"
                      :id="'rename-' + s.session_id"
                      v-model="editTitle"
                      class="sess-rename-input"
                      @click.stop
                      @blur="saveRename(s)"
                      @keydown.enter.prevent="saveRename(s)"
                    />
                    <div v-else class="sess-title">{{ s.title }}</div>
                    <span
                      v-if="s.readonly && editingId !== s.session_id"
                      class="sess-readonly-badge"
                      >已结束</span
                    >
                  </div>
                </div>
                <i class="ti ti-dots sess-dots" @click.stop="toggleMenu(s.session_id, $event)"></i>
              </div>
              <div
                v-if="menuId === s.session_id"
                class="sess-menu"
                :style="{
                  top: menuPos.top + 'px',
                  left: menuPos.left + 'px',
                  transform: menuPos.flipUp ? 'translateY(-100%)' : 'none',
                }"
                @click.stop
              >
                <div v-if="!s.readonly" @click="startHandoffFrom(s.session_id)">
                  <i class="ti ti-arrow-forward"></i> 从此会话开启新会话
                </div>
                <div @click="togglePin(s)">
                  <i class="ti ti-pin"></i> {{ s.pinned ? '取消置顶' : '置顶' }}
                </div>
                <div @click="startRename(s)"><i class="ti ti-edit"></i> 重命名</div>
                <div @click="archiveManual(s.session_id)">
                  <i class="ti ti-archive"></i> 归档会话
                </div>
                <div class="dang" @click="deleteSession(s.session_id)">
                  <i class="ti ti-trash"></i> 删除
                </div>
              </div>
            </div>
            <div v-if="!p.sessions.length" class="ws-sessions-empty">点上方 ⋯ → 新建会话</div>
          </div>
          <div
            v-if="projMenuId === p.id"
            class="proj-menu"
            :style="{ top: projMenuPos.top + 'px', left: projMenuPos.left + 'px' }"
            @click.stop
          >
            <div @click="newSessionInProject(p.id)"><i class="ti ti-plus"></i> 新建会话</div>
            <div @click="startRenameProject(p)"><i class="ti ti-edit"></i> 重命名</div>
            <div class="dang" @click="archiveProject(p)">
              <i class="ti ti-archive"></i> 归档项目
            </div>
          </div>
        </div>
      </div>
      <div
        v-if="projMenuId"
        style="position: fixed; inset: 0; z-index: var(--z-menu)"
        @click="projMenuId = null"
      ></div>
    </div>

    <!-- 会话区（置顶 / 渠道 / 最近，或搜索面板） -->
    <div class="side-sessions" :class="{ 'sess-min': sectionCollapsed.sess }">
      <div v-if="!searchOpen" class="sess-hd" @click="toggleSection('sess')">
        <i class="ti" :class="sectionCollapsed.sess ? 'ti-chevron-right' : 'ti-chevron-down'"></i>
        <span>会话区</span>
        <i class="ti ti-plus sess-add" title="新建会话" @click.stop="goHome"></i>
      </div>

      <!-- 搜索面板：打开时替换历史会话区 -->
      <div v-if="searchOpen" v-show="!sectionCollapsed.sess" class="side-sess">
        <SessionSearchPanel @close="closeSearch" />
      </div>

      <!-- 历史会话（置顶 / 渠道 / 最近） -->
      <div v-else v-show="!sectionCollapsed.sess" class="side-sess">
        <div v-if="!sess.list.length" class="empty" style="padding: 32px 8px">
          <i class="ti ti-messages"></i>还没有会话<br />发送第一条消息开始吧
        </div>
        <div
          v-for="grp in sessionGroups"
          v-show="grp.items.length"
          :key="grp.key"
          style="margin-bottom: 5px"
        >
          <div class="sess-group-hd" @click="collapsed[grp.key] = !collapsed[grp.key]">
            <i class="ti" :class="collapsed[grp.key] ? 'ti-chevron-right' : 'ti-chevron-down'"></i>
            <span style="flex: 1">{{ grp.label }}</span>
            <span class="sess-group-count">{{ grp.items.length }}</span>
          </div>
          <div v-show="!collapsed[grp.key]" class="sess-group-body">
            <div
              v-for="s in grp.items"
              :key="s.session_id"
              class="sess-item"
              :class="{ active: s.session_id === sess.currentSid && route.path === '/chat' }"
              style="position: relative"
              @click="openSession(s.session_id)"
            >
              <div style="display: flex; align-items: center; gap: 10px">
                <i
                  v-if="s.pinned || !s.channel"
                  class="ti sess-icon"
                  :class="s.pinned ? 'ti-pin' : 'ti-message'"
                ></i>
                <ChannelIcon v-else :platform="s.channel" :size="16" class="sess-icon" />
                <div style="min-width: 0; flex: 1">
                  <div style="display: flex; align-items: center; gap: 6px">
                    <input
                      v-if="editingId === s.session_id"
                      :id="'rename-' + s.session_id"
                      v-model="editTitle"
                      class="sess-rename-input"
                      @click.stop
                      @blur="saveRename(s)"
                      @keydown.enter.prevent="saveRename(s)"
                    />
                    <div v-else class="sess-title">{{ s.title }}</div>
                    <span
                      v-if="s.readonly && editingId !== s.session_id"
                      class="sess-readonly-badge"
                      >已结束</span
                    >
                  </div>
                </div>
                <i class="ti ti-dots sess-dots" @click.stop="toggleMenu(s.session_id, $event)"></i>
              </div>
              <div
                v-if="menuId === s.session_id"
                class="sess-menu"
                :style="{
                  top: menuPos.top + 'px',
                  left: menuPos.left + 'px',
                  transform: menuPos.flipUp ? 'translateY(-100%)' : 'none',
                }"
                @click.stop
              >
                <!-- 从此会话开启新会话（会话上下文管理方案 v2） -->
                <div v-if="!s.readonly" @click="startHandoffFrom(s.session_id)">
                  <i class="ti ti-arrow-forward"></i> 从此会话开启新会话
                </div>
                <div @click="togglePin(s)">
                  <i class="ti ti-pin"></i> {{ s.pinned ? '取消置顶' : '置顶' }}
                </div>
                <div @click="startRename(s)"><i class="ti ti-edit"></i> 重命名</div>
                <div @click="archiveManual(s.session_id)">
                  <i class="ti ti-archive"></i> 归档会话
                </div>
                <div class="dang" @click="deleteSession(s.session_id)">
                  <i class="ti ti-trash"></i> 删除
                </div>
              </div>
            </div>
          </div>
        </div>
        <div
          v-if="menuId"
          style="position: fixed; inset: 0; z-index: var(--z-menu)"
          @click="menuId = null"
        ></div>
      </div>
    </div>

    <!-- 添加项目对话框 -->
    <AddProjectModal v-if="addProjOpen" @close="addProjOpen = false" @created="onProjectCreated" />

    <!-- Ctrl+P 项目切换器（v5 §九 9.11） -->
    <div
      v-if="projectSwitcherOpen"
      class="proj-switcher-mask"
      @click="projectSwitcherOpen = false"
    ></div>
    <div v-if="projectSwitcherOpen" class="proj-switcher">
      <input
        v-model="projectSwitcherQuery"
        class="ps-input"
        placeholder="输入项目名或路径过滤（Enter 选择，Esc 关闭）"
        autofocus
        @input="projectSwitcherHi = 0"
      />
      <div class="ps-list">
        <div
          v-for="(p, i) in projectSwitcherList"
          :key="p.id || 'none'"
          class="ps-item"
          :class="{ hi: i === projectSwitcherHi }"
          @click="switchToProject(p)"
          @mouseenter="projectSwitcherHi = i"
        >
          <i class="ti" :class="p.id ? 'ti-folder' : 'ti-plus'"></i>
          <span class="ps-title">{{ p.title }}</span>
          <span v-if="p.path" class="ps-path muted">{{ p.path }}</span>
        </div>
        <div v-if="!projectSwitcherList.length" class="ps-empty">没有匹配的项目</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.side-workspace {
  border-top: 1px solid var(--stroke);
  padding: 5px 0 3px;
  margin-top: 3px;
}
.side-sessions {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  border-top: 1px solid var(--stroke);
  margin-top: 3px;
  padding-top: 3px;
}
.sess-hd {
  flex: 0 0 auto;
  padding: 3px 12px 4px;
  font-size: var(--fs-xs, 12px);
  color: var(--muted);
  font-weight: 500;
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  user-select: none;
}
.sess-hd:hover {
  color: var(--fg);
}
.ws-hd {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 3px 12px 4px;
  font-size: var(--fs-xs, 12px);
  color: var(--muted);
  font-weight: 500;
}
.ws-hd .hd-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  user-select: none;
}
.ws-hd .hd-toggle:hover,
.sess-hd:hover {
  color: var(--fg);
}
.side-workspace.ws-grow {
  flex: 0 1 auto;
  min-height: 0;
  overflow-y: auto;
  scrollbar-gutter: stable;
}
.side-sessions.sess-min {
  flex: 0 0 auto;
}
.ws-add,
.sess-add {
  cursor: pointer;
  padding: 2px 4px;
  border-radius: 4px;
}
.ws-add:hover,
.sess-add:hover {
  background: var(--bg-hover, rgba(127, 127, 127, 0.12));
  color: var(--acctx);
}
.sess-add {
  margin-left: auto;
}
.ws-empty {
  padding: 12px;
  text-align: center;
  color: var(--muted);
  font-size: var(--fs-xs, 12px);
  line-height: 1.6;
}
.ws-project {
  margin-bottom: 1px;
}
.ws-project-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 12px;
  cursor: pointer;
  user-select: none;
  border-radius: 4px;
}
.ws-project-row:hover {
  background: var(--bg-hover, rgba(127, 127, 127, 0.08));
}
.ws-project-row .ws-title {
  flex: 1;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: var(--fs-sm, 13px);
}
.ws-project-row .ti-folder {
  color: var(--acctx);
  flex-shrink: 0;
}
.ws-project-row .ws-missing {
  color: var(--muted);
}
.ws-badge-miss {
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 3px;
  background: var(--warntx-bg, rgba(200, 120, 0, 0.15));
  color: var(--warntx, #c87800);
}
.ws-count {
  font-size: var(--fs-xs, 12px);
  color: var(--muted);
  flex-shrink: 0;
}
.ws-dots {
  padding: 2px 4px;
  cursor: pointer;
  opacity: 0.5;
}
.ws-project-row:hover .ws-dots {
  opacity: 1;
}
.ws-sessions {
  padding-left: 20px;
}
.ws-sessions-empty {
  padding: 6px 12px;
  color: var(--muted);
  font-size: var(--fs-xs, 12px);
  font-style: italic;
}
.proj-menu {
  position: fixed;
  min-width: 168px;
  background: var(--bg-elev, var(--bg));
  border: 1px solid var(--stroke);
  border-radius: 6px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
  z-index: calc(var(--z-menu, 100) + 1);
  padding: 4px;
}
.proj-menu > div {
  padding: 6px 12px;
  cursor: pointer;
  border-radius: 4px;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--fs-sm, 13px);
}
.proj-menu > div:hover {
  background: var(--bg-hover, rgba(127, 127, 127, 0.12));
}
.proj-menu > div.dang:hover {
  background: var(--dangtx-bg, rgba(200, 0, 0, 0.1));
  color: var(--dangtx);
}

/* Ctrl+P 项目切换器 */
.proj-switcher-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.35);
  z-index: var(--z-drawer);
}
.proj-switcher {
  position: fixed;
  top: 20%;
  left: 50%;
  transform: translateX(-50%);
  width: 520px;
  max-width: 90vw;
  background: var(--bg-elev, var(--bg));
  border: 1px solid var(--stroke);
  border-radius: 8px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.3);
  z-index: calc(var(--z-drawer) + 1);
  overflow: hidden;
}
.proj-switcher .ps-input {
  width: 100%;
  box-sizing: border-box;
  padding: 12px 16px;
  border: none;
  border-bottom: 1px solid var(--stroke);
  background: transparent;
  color: var(--fg);
  font-size: 14px;
  outline: none;
}
.proj-switcher .ps-list {
  max-height: 400px;
  overflow-y: auto;
}
.proj-switcher .ps-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 16px;
  cursor: pointer;
  font-size: 13px;
}
.proj-switcher .ps-item.hi,
.proj-switcher .ps-item:hover {
  background: var(--acctx-bg, rgba(60, 120, 220, 0.15));
}
.proj-switcher .ps-item .ti {
  color: var(--acctx);
}
.proj-switcher .ps-item .ps-title {
  font-weight: 500;
}
.proj-switcher .ps-item .ps-path {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  font-family: monospace;
}
.proj-switcher .ps-empty {
  padding: 20px;
  text-align: center;
  color: var(--muted);
}
</style>
