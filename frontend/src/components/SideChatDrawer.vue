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
import { ref, computed, nextTick } from 'vue'
import { useSessions } from '@/stores/sessions'
import ChatView from '@/views/ChatView.vue'

const sessStore = useSessions()
const activeMainSid = computed(() => sessStore.currentSid)

// entries: [{ mainSid, projectId, sessionId(aside sid, 首条发送后回填) }]
const entries = ref([])
// mainSid -> ChatView 实例（用于 injectQuote）
const asideRefs = new Map()
function setAsideRef(mainSid, el) {
  if (el) asideRefs.set(mainSid, el)
  else asideRefs.delete(mainSid)
}

const activeEntry = computed(
  () => entries.value.find((e) => e.mainSid === activeMainSid.value) || null
)
const visible = computed(() => !!activeEntry.value)

function resolveProject(mainSid) {
  const s = sessStore.list.find((x) => x.session_id === mainSid)
  return (s && s.project_id) || null
}

// 由主视图 @open-aside 调用：确保当前主会话有侧边条目，显示并注入选中文本。
async function openAside(quote) {
  const mainSid = activeMainSid.value
  if (!mainSid) return // 欢迎页无主会话，无从划词，忽略
  let entry = entries.value.find((e) => e.mainSid === mainSid)
  if (!entry) {
    entry = { mainSid, projectId: resolveProject(mainSid), sessionId: null }
    entries.value.push(entry)
  }
  await nextTick()
  const inst = asideRefs.get(mainSid)
  if (inst && quote && quote.text) {
    inst.injectQuote({
      text: quote.text,
      comment: quote.comment || '',
      sourceMsgId: quote.sourceMsgId,
      sourceRole: quote.sourceRole,
    })
  }
}

function onAsideCreated(mainSid, sid) {
  const e = entries.value.find((x) => x.mainSid === mainSid)
  if (e) e.sessionId = sid
}

// 显式关闭当前主会话的侧边：销毁前端状态（后端已留痕，不受影响）
function closeActive() {
  const mainSid = activeMainSid.value
  entries.value = entries.value.filter((e) => e.mainSid !== mainSid)
  asideRefs.delete(mainSid)
}

defineExpose({ openAside })
</script>

<template>
  <div class="aside-drawer" :class="{ open: visible }" aria-label="侧边会话">
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
        v-show="e.mainSid === activeMainSid"
        :key="e.mainSid"
        :ref="(el) => setAsideRef(e.mainSid, el)"
        aside-mode
        :aside-session-id="e.sessionId"
        :aside-project-id="e.projectId"
        :aside-from-session="e.mainSid"
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
  width: min(460px, 92vw);
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
