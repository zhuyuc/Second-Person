<script setup>
// 划词「侧边会话」宿主抽屉：右侧滑出，内部复用 ChatView（asideMode 第二实例）。
//
// 语义（产品定稿）：
//  - 侧边会话绑定「发起它的主会话」。切换主会话时其抽屉隐藏、状态保留；切回未关闭者继续显示。
//  - 每个主会话至多一个 open 侧边；再划词把引用追加进该已开侧边，不新开窗口。
//  - 显式关闭(X) / 刷新 → 前端销毁，内容消失、不显历史（后端已留痕，此处不管）。
//  - 内容隔离：aside 会话独立成话、不进列表/搜索（后端 channel='aside' 保证）。
//
// 实例常驻（v-show 而非 v-if）以保活消息 + 进行中流 + 未发草稿；切主会话仅切换可见性。
import { ref, computed, defineAsyncComponent, nextTick, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { useSessions } from '@/stores/sessions'
import { loadAsideChatView } from '@/components/asideChatLoader'
// 侧边会话只有用户主动划词后才会出现；不要因抽屉常驻而把完整 ChatView
// （及其图表依赖）并入首屏入口。
const ChatView = defineAsyncComponent(loadAsideChatView)

const route = useRoute()
const sessStore = useSessions()
const activeMainSid = computed(() => sessStore.currentSid)
const onChatRoute = computed(() => route.path === '/chat' || route.path.startsWith('/chat/'))
const onNotesRoute = computed(() => route.path === '/notes' || route.path.startsWith('/notes/'))
// 记事本侧边绑定键：note:<id>；对话侧边仍用主会话 id
const activeNotesBindKey = computed(() => {
  if (!onNotesRoute.value) return null
  const nid = window.__spActiveNoteId
  return nid ? `note:${nid}` : null
})
const activeBindKey = computed(() =>
  onNotesRoute.value ? activeNotesBindKey.value : activeMainSid.value,
)

// entries: [{ mainSid(bindKey), projectId, sessionId, noteId? }]
const entries = ref([])
const asideRefs = new Map()
const pendingQuotes = new Map()
function applyQuote(inst, quote) {
  if (!inst || !quote?.text) return
  inst.injectQuote({
    text: quote.text,
    comment: quote.comment || '',
    sourceMsgId: quote.sourceMsgId,
    sourceRole: quote.sourceRole,
  })
}
function flushPendingQuotes(mainSid, inst) {
  const queue = pendingQuotes.get(mainSid)
  if (!queue?.length || !inst) return
  pendingQuotes.delete(mainSid)
  // 下一帧再注入，确保 aside 实例内部 composer/attachments 已就绪
  nextTick(() => {
    for (const quote of queue) applyQuote(inst, quote)
  })
}
function setAsideRef(mainSid, el) {
  if (el) {
    asideRefs.set(mainSid, el)
    flushPendingQuotes(mainSid, el)
  } else {
    asideRefs.delete(mainSid)
  }
}

const activeEntry = computed(
  () => entries.value.find((e) => e.mainSid === activeBindKey.value) || null
)
const visible = computed(
  () =>
    !!activeEntry.value &&
    ((onChatRoute.value && !!activeMainSid.value) ||
      (onNotesRoute.value && !!activeNotesBindKey.value)),
)

// 抽屉宽度：可拖拽调整，localStorage 记住下次打开复用
const WIDTH_KEY = 'sp_aside_drawer_width'
const DEFAULT_WIDTH = 460
const MIN_WIDTH = 320
function maxWidth() {
  return Math.max(MIN_WIDTH, Math.floor(window.innerWidth * 0.92))
}
function clampWidth(w) {
  return Math.min(maxWidth(), Math.max(MIN_WIDTH, Math.round(w)))
}
function loadWidth() {
  const raw = Number(localStorage.getItem(WIDTH_KEY))
  return Number.isFinite(raw) && raw > 0 ? Math.round(raw) : DEFAULT_WIDTH
}
// preferredWidth 是用户偏好；显示宽度再按当前视口钳制，窗口缩小不覆盖偏好
const preferredWidth = ref(loadWidth())
const resizing = ref(false)
// 视口变化时强制重算钳制（maxWidth 依赖 innerWidth）
const viewportTick = ref(0)
const drawerWidth = computed(() => {
  void viewportTick.value
  return clampWidth(preferredWidth.value)
})

function persistWidth() {
  localStorage.setItem(WIDTH_KEY, String(preferredWidth.value))
}

function onResizeMove(e) {
  // 从右侧贴边往左拉：宽度 = 视口右缘 − 指针 x
  preferredWidth.value = clampWidth(window.innerWidth - e.clientX)
}
function onResizeEnd() {
  if (!resizing.value) return
  resizing.value = false
  document.body.style.cursor = ''
  document.body.style.userSelect = ''
  window.removeEventListener('pointermove', onResizeMove)
  window.removeEventListener('pointerup', onResizeEnd)
  window.removeEventListener('pointercancel', onResizeEnd)
  persistWidth()
}
function onResizeStart(e) {
  if (e.button != null && e.button !== 0) return
  e.preventDefault()
  resizing.value = true
  document.body.style.cursor = 'col-resize'
  document.body.style.userSelect = 'none'
  window.addEventListener('pointermove', onResizeMove)
  window.addEventListener('pointerup', onResizeEnd)
  window.addEventListener('pointercancel', onResizeEnd)
}

function onWindowResize() {
  viewportTick.value++
}

onMounted(() => window.addEventListener('resize', onWindowResize))
onUnmounted(() => {
  window.removeEventListener('resize', onWindowResize)
  onResizeEnd()
})

function resolveProject(mainSid) {
  const s = sessStore.list.find((x) => x.session_id === mainSid)
  return (s && s.project_id) || null
}

// 由主视图 @open-aside 调用：确保当前主会话有侧边条目，显示并注入选中文本。
// ChatView 为 defineAsyncComponent：刷新后首次打开时 nextTick 仍拿不到实例，
// 故已挂载则立即注入，否则写入 pendingQuotes，等 setAsideRef 再消费。
async function openAside(quote) {
  let bindKey = activeBindKey.value
  let noteId = null
  if (onNotesRoute.value) {
    noteId = quote?.noteId || window.__spActiveNoteId || null
    if (!noteId) return
    bindKey = `note:${noteId}`
  } else if (!bindKey) {
    return // 欢迎页无主会话
  }
  let entry = entries.value.find((e) => e.mainSid === bindKey)
  if (!entry) {
    entry = {
      mainSid: bindKey,
      projectId: onNotesRoute.value ? null : resolveProject(bindKey),
      sessionId: null,
      noteId: noteId || null,
    }
    entries.value.push(entry)
  }
  // 允许无划词仅打开抽屉（记事本「AI 写文档」入口）
  if (!quote?.text) {
    await nextTick()
    return
  }
  // 记事本：把 note_id 写进评论前缀，方便模型调用 notes_* 工具
  const enriched = { ...quote }
  if (noteId && !String(enriched.comment || '').includes(noteId)) {
    const tip =
      `【当前记事本 note_id=${noteId}；改文档请用 notes_apply_edit：` +
      `划词改写用 replace_selection；在光标处插入用 insert_at_cursor（光标已由前端同步）；` +
      `文末追加用 append；整篇重写仅在用户明确要求时用 replace_document】`
    enriched.comment = enriched.comment ? `${tip}\n${enriched.comment}` : tip
  }
  await nextTick()
  const inst = asideRefs.get(bindKey)
  if (inst) {
    applyQuote(inst, enriched)
  } else {
    const queue = pendingQuotes.get(bindKey) || []
    queue.push(enriched)
    pendingQuotes.set(bindKey, queue)
  }
}

function onAsideCreated(mainSid, sid) {
  const e = entries.value.find((x) => x.mainSid === mainSid)
  if (e) e.sessionId = sid
}

function closeActive() {
  const bindKey = activeBindKey.value
  if (!bindKey) return
  entries.value = entries.value.filter((e) => e.mainSid !== bindKey)
  asideRefs.delete(bindKey)
  pendingQuotes.delete(bindKey)
}

defineExpose({ openAside })
</script>

<template>
  <div
    class="aside-drawer"
    :class="{ open: visible, resizing }"
    :style="{ width: drawerWidth + 'px' }"
    aria-label="侧边会话"
  >
    <div
      class="aside-resize"
      role="separator"
      aria-orientation="vertical"
      aria-label="拖动调整侧边会话宽度"
      title="拖动调整宽度"
      @pointerdown="onResizeStart"
    />
    <div class="aside-head">
      <span class="aside-title"><i class="ti ti-messages"></i> 侧边会话</span>
      <button
        type="button"
        class="aside-close"
        title="关闭（内容用完即走，不进列表；后台已留痕）"
        @click="closeActive"
      >
        <i class="ti ti-x"></i>
      </button>
    </div>
    <div class="aside-body">
      <ChatView
        v-for="e in entries"
        v-show="e.mainSid === activeBindKey"
        :key="e.mainSid"
        :ref="(el) => setAsideRef(e.mainSid, el)"
        aside-mode
        :aside-session-id="e.sessionId"
        :aside-project-id="e.projectId"
        :aside-from-session="e.noteId ? null : e.mainSid"
        @aside-session-created="(sid) => onAsideCreated(e.mainSid, sid)"
      />
    </div>
  </div>
</template>

<style scoped>
.aside-drawer {
  position: fixed;
  top: 0;
  right: 0;
  height: 100vh;
  width: 460px;
  max-width: 92vw;
  background: var(--surface, var(--bg));
  border-left: 1px solid var(--bd);
  box-shadow: var(--shadow-2);
  z-index: var(--z-drawer, 900);
  display: flex;
  flex-direction: column;
  transform: translateX(100%);
  transition: transform var(--dur, 0.22s) ease;
  will-change: transform;
}
.aside-drawer.open {
  transform: translateX(0);
}
.aside-drawer.resizing {
  transition: none;
  user-select: none;
}
.aside-resize {
  position: absolute;
  top: 0;
  left: -3px;
  width: 6px;
  height: 100%;
  cursor: col-resize;
  z-index: 2;
  touch-action: none;
}
.aside-resize::after {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  left: 2px;
  width: 2px;
  background: transparent;
  transition: background 0.15s ease;
}
.aside-resize:hover::after,
.aside-drawer.resizing .aside-resize::after {
  background: var(--acc);
}
.aside-head {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  border-bottom: 1px solid var(--bd);
  background: var(--surface-1, var(--surface));
}
.aside-title {
  font-size: var(--fs-md, 14px);
  font-weight: 600;
  color: var(--fg);
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.aside-close {
  border: none;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  padding: 4px 6px;
  border-radius: var(--radius-sm, 6px);
  display: inline-flex;
  align-items: center;
  font-size: 18px;
  line-height: 1;
}
.aside-close:hover {
  background: var(--surface-2);
  color: var(--fg);
}
.aside-body {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  overflow: hidden;
}
/* 复用的 ChatView 在抽屉里铺满可用空间。
   主视图里 .chat-root 用 height:100vh + 负 margin 去撑满 .main 内边距；放进抽屉
   会导致：① 负 margin 让内容超出抽屉宽度被裁掉；② 100vh 比抽屉正文（顶部有标题栏）
   高，把底部输入框顶出可视区。这里统一重置为填满抽屉正文，交由内部 flex 列自适应。 */
.aside-body :deep(.chat-root) {
  flex: 1 1 auto;
  height: 100%;
  width: 100%;
  max-width: 100%;
  min-width: 0;
  min-height: 0;
  margin: 0;
  overflow: hidden;
}
/* 消息区内容宽度贴合窄抽屉，收窄左右留白 */
.aside-body :deep(.chat-scroller-inner) {
  max-width: 100%;
  padding: 16px 14px;
}
.aside-body :deep(.composer-wrap) {
  padding-left: 14px;
  padding-right: 14px;
}
/* 宽内容（长串/代码/表格）在各自容器内横向滚动，绝不撑破抽屉宽度 */
.aside-body :deep(.chat-main) {
  min-width: 0;
  overflow-x: hidden;
}
</style>
