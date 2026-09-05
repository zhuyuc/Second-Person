<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { chatApi } from '@/api/chat'
import { useSSE } from '@/composables/useSSE'
import { useLiveThroughput } from '@/composables/useLiveThroughput'
import { useToast } from '@/stores/toast'
import { useSessions } from '@/stores/sessions'
import { useProjects } from '@/stores/projects'
import { projectsApi } from '@/api/projects'
import { memoryApi } from '@/api/memory'
import { resolveLocation, cachedLocation } from '@/composables/useGeolocation'
import DiagramRenderer from '@/components/diagram/DiagramRenderer.vue'
import { marked } from '@/utils/chatMarkedRenderer'
import { useMessageSelection } from '@/composables/useMessageSelection'
import { applyMermaidTheme } from '@/utils/mermaidTheme'
import { svgToPngBlob } from '@/utils/svgExport'
import { formatRelative, formatTimeFull, fmtSize, nowLocalIso, friendlyError } from '@/utils/format'
import { confidenceLabel, lifecycleLabel } from '@/utils/enumLabel'
import { sanitizeHtml } from '@/utils/sanitize'
import { enhanceResponseHtml } from '@/utils/responsePresentation'
import {
  bindInlineMermaidInteractions,
  cleanupInlineMermaidInteractions,
  resetInlineMermaid,
  zoomInlineMermaid,
} from '@/utils/inlineMermaidInteractions'
import { normalizeReasoningEffort } from '@/utils/chatContract'
import {
  fetchSessionMessages,
  fetchSessionMetrics,
  stripToastNotifs,
  extractAttachments,
} from '@/composables/useChatSession'
import { useChatStream } from '@/composables/useChatStream'
import { formatTimelineSummary } from '@/utils/timelineSummary'
import { loadMermaid } from '@/utils/mermaidLoader'

// Mermaid 主题：CSS 变量驱动（与 MermaidChart 同源），自动跟随系统深浅色；手动触发 run
// 注意：mermaid 库已改为懒加载，主题初始化在首次渲染前异步执行
// 自定义 marked 代码块渲染器已抽取到 @/utils/chatMarkedRenderer 模块

const toast = useToast()
const sse = useSSE()
const sessStore = useSessions() // 会话列表/当前会话共享状态（侧栏在 SessionSidebar）
const projStore = useProjects() // 项目工作区（v5）

// 侧边会话模式：本组件既是主对话视图，也被 SideChatDrawer 以第二实例复用。
// asideMode 下"当前会话"走 asideSessionId（本地维护，首条发送时创建），且不触碰
// 全局会话列表/当前会话（setCurrent/load/ensurePlaceholder 等）—— 内容隔离、不进列表。
const props = defineProps({
  asideMode: { type: Boolean, default: false },
  asideSessionId: { type: String, default: null },
  asideProjectId: { type: String, default: null },
  asideFromSession: { type: String, default: null },
})
const emit = defineEmits(['open-aside', 'aside-session-created', 'aside-close'])

const asideLocalSid = ref(props.asideSessionId)
watch(
  () => props.asideSessionId,
  (v) => {
    asideLocalSid.value = v
  }
)
const currentSid = computed(() =>
  props.asideMode ? asideLocalSid.value : sessStore.currentSid
)

// 本次流式请求的 crid：主模式沿用全局 sp_active_crid 支持刷新续推；aside 模式用
// 本地 crid（临时窗口不做刷新续推），stop 时据此取消对应流，避免误取消主对话。
function genCrid() {
  return 'crid_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 8)
}
const streamCrid = ref(null)

const currentProject = computed(() => {
  // aside 模式：项目继承自发起它的主会话（不在全局 list 里，直接用 prop 解析）
  if (props.asideMode) {
    return props.asideProjectId ? projStore.byId(props.asideProjectId) || null : null
  }
  const sid = currentSid.value
  if (sid) {
    const s = sessStore.list.find((x) => x.session_id === sid)
    if (s && s.project_id) return projStore.byId(s.project_id) || null
    return null
  }
  // M5.1：未建库的「待建」项目会话，用 pendingProjectId 显示
  if (sessStore.pendingProjectId) {
    return projStore.byId(sessStore.pendingProjectId) || null
  }
  return null
})

// 新对话（会话未建）时的沙箱档位预选：chip 展示用 fallback，首条消息建会话后落库
const pendingSandboxMode = ref(null)
const sandboxFallback = computed(
  () => pendingSandboxMode.value || currentProject.value?.sandbox_mode || 'workspace-write'
)

// M4：@文件面板
const filePickerVisible = ref(false)
const filePickerQuery = ref('')
const filePickerRef = ref(null)

function onComposerInput(e) {
  // 复用原有 autoGrow；这里叠加 @ 触发面板逻辑
  autoGrow()
  const ta = e?.target || document.querySelector('textarea')
  if (!ta || !currentProject.value) {
    filePickerVisible.value = false
    return
  }
  const val = ta.value || ''
  const pos = ta.selectionStart || 0
  // 找到光标前最近的 @，判定是否处于「@词」状态
  const before = val.slice(0, pos)
  const atIdx = before.lastIndexOf('@')
  if (atIdx < 0 || (atIdx > 0 && !/\s/.test(before[atIdx - 1]))) {
    filePickerVisible.value = false
    return
  }
  const seg = before.slice(atIdx + 1)
  if (/[\s\r\n]/.test(seg)) {
    filePickerVisible.value = false
    return
  }
  filePickerQuery.value = seg
  filePickerVisible.value = true
}

function onFilePicked(f) {
  const ta = document.querySelector('textarea')
  if (!ta) return
  const val = ta.value || ''
  const pos = ta.selectionStart || 0
  const before = val.slice(0, pos)
  const atIdx = before.lastIndexOf('@')
  if (atIdx < 0) return
  const after = val.slice(pos)
  const insertion = `@${f.path} `
  input.value = val.slice(0, atIdx) + insertion + after
  filePickerVisible.value = false
  nextTick(() => {
    const newPos = atIdx + insertion.length
    ta.focus()
    ta.setSelectionRange(newPos, newPos)
  })
}

function onComposerKeyDown(e) {
  if (filePickerVisible.value && filePickerRef.value) {
    // 让面板先处理方向键/Enter/Esc
    if (['ArrowDown', 'ArrowUp', 'Enter', 'Escape'].includes(e.key)) {
      filePickerRef.value.onKey(e)
      if (e.defaultPrevented) return
    }
  }
  if (e.key === 'Escape' && editingId.value) {
    e.preventDefault()
    cancelEdit()
    return
  }
  if (e.key === 'Enter' && !e.shiftKey && !filePickerVisible.value) {
    e.preventDefault()
    send()
  }
}
// 消息气泡文字选中 → 悬浮 toolbar（复制/引用）
// 传入本实例滚动容器，使主对话与侧边会话的选区互不串扰（各弹各的浮条）
const selection = useMessageSelection({ getRoot: () => scroller.value })
const messages = ref([])
// 历史消息分页：后端默认返回最近 50 条，"加载更早"通过 before_id 游标向前拉。
const PAGE_SIZE = 50
const hasMoreMessages = ref(false)
const loadingMore = ref(false)
const visibleMessages = computed(() => messages.value)
function resetMessageWindow(total) {
  // 兼容旧调用点（reloadMessages/openSession）：首屏满页则可能还有更早历史
  hasMoreMessages.value = total >= PAGE_SIZE
}
async function showOlderMessages() {
  if (loadingMore.value || !messages.value.length) return
  const firstId = messages.value[0]?.id
  if (!firstId) return
  loadingMore.value = true
  try {
    const older = await fetchSessionMessages(currentSid.value, {
      before_id: firstId,
      limit: PAGE_SIZE,
    })
    if (!older.length) {
      hasMoreMessages.value = false
      return
    }
    // 记录当前首条元素位置，prepend 后保持滚动视口稳定（避免跳到顶）
    const scrollerEl = scroller.value
    const prevScrollHeight = scrollerEl ? scrollerEl.scrollHeight : 0
    const prevScrollTop = scrollerEl ? scrollerEl.scrollTop : 0
    messages.value = [...older, ...messages.value]
    hasMoreMessages.value = older.length >= PAGE_SIZE
    await nextTick()
    if (scrollerEl) {
      const delta = scrollerEl.scrollHeight - prevScrollHeight
      scrollerEl.scrollTop = prevScrollTop + delta
    }
  } catch {
    /* 拉取失败：保留现有消息，按钮仍可用以便重试 */
  } finally {
    loadingMore.value = false
  }
}
const input = ref('')
const streamSrcOpen = ref(false) // 流式回复的联网来源面板：默认收起
const sessionMetrics = ref(null)
const currentTurnMetrics = ref(null)
// 实时 tok/s：deepseek-harness 的 sessionStats 只在步边界刷新，这里 chunk 级估算
const liveThroughput = useLiveThroughput()
const scroller = ref(null)
const ta = ref(null) // 输入框，用于自适应高度

// 当前会话的消息定位轨道：只为用户消息建立锚点，避免 AI 回复重复占位。
const messageElements = new Map()
const messageElementKeys = new WeakMap()
let localMessageKeySeq = 0

const thresholdBreached = ref(null) // null / 'soft' / 'hard'
const softToastShown = ref(false)
// handoff 附件状态
const handoffStatus = ref(null) // null / 'generating' / 'ready' / 'failed'
const handoffData = ref(null) // { summary_tokens, original_turns }
const handoffPreview = ref(null)
const pendingMessage = ref(null) // 摘要生成中暂存的消息

function stripTail(t, visuals) {
  let s = (t || '').replace(/\s*\{\s*"citations"\s*:\s*\[[^\]]*\]\s*\}\s*/g, '\n')
  s = s.replace(/\s*\{\s*"memory_confirm"\s*:\s*\{[^}]*\}\s*\}\s*/g, '\n')
  s = s.replace(/```[a-zA-Z]*\s*```/g, '').trimEnd()
  s = s.replace(
    /<antartifact[^>]*type=["']text\/html["'][^>]*>([\s\S]*?)<\/antartifact>/gi,
    (_, content) => '\n\n```html\n' + content.trim() + '\n```\n'
  )
  s = s.replace(/<tool_call>[\s\S]*?<\/tool_call>/gi, '')
  s = s.replace(/<工具调用>[\s\S]*?<\/工具调用>/g, '')
  const hasVisual = Array.isArray(visuals) && visuals.length > 0
  if (hasVisual) {
    s = s.replace(/```(?:mermaid|flowchart)\s*\n[\s\S]*?\n```/gi, '')
  }
  return s
}

async function reloadMessages(sid, { preserveLocalTail = false } = {}) {
  try {
    const localTail = preserveLocalTail ? messages.value[messages.value.length - 1] : null
    const msgs = await fetchSessionMessages(sid)
    if (currentSid.value !== sid) return
    const keepLocalTail =
      preserveLocalTail &&
      localTail?.role === 'assistant' &&
      !localTail.id &&
      msgs[msgs.length - 1]?.role !== 'assistant'
    messages.value = keepLocalTail ? [...msgs, localTail] : msgs
  } catch {
    /* 重拉失败：保留内存部分回复不降级 */
  }
}

function handleThreshold(threshold) {
  const { session_total_tokens, soft_threshold, hard_threshold, breached } = threshold
  if (breached === 'hard' || session_total_tokens >= hard_threshold) {
    thresholdBreached.value = 'hard'
  } else if (breached === 'soft' || session_total_tokens >= soft_threshold) {
    if (!softToastShown.value) {
      toast.push('warning', '此会话已接近容量，建议尽快收尾或开启新会话')
      softToastShown.value = true
      sessionStorage.setItem(`sp_soft_toast_shown_${currentSid.value}`, '1')
    }
    thresholdBreached.value = 'soft'
  }
}

const scrollBridge = { maybe: () => {}, think: () => {}, code: () => {} }

const {
  generating,
  streamText,
  thinkText,
  reasoningText,
  decisionNotices,
  toolEvents,
  timeline,
  thinkOpen,
  preBodyPhase,
  streamSid,
  streamVisuals,
  degraded,
  streamPushSuppressed,
  showLiveThinkPanel,
  showProcessingPlaceholder,
  timelineLive,
  showThinkLiveDots,
  liveThinkSummary,
  beginStream,
  handleEvent,
  finishStream,
  cleanupRaf,
  resetTimeline,
} = useChatStream({
  messages,
  sessStore,
  // 侧边会话实例把自己的当前会话 id 注入流式状态机，确保回复完成后正确入列
  getCurrentSid: () => currentSid.value,
  liveThroughput,
  toast,
  reloadMessages,
  friendlyError,
  nowLocalIso,
  stripTail,
  onMaybeScroll: () => scrollBridge.maybe(),
  onScrollThink: () => scrollBridge.think(),
  onScrollStreamCode: () => scrollBridge.code(),
  sessionMetrics,
  currentTurnMetrics,
  onHandleThreshold: handleThreshold,
  onHandoffReady: (data) => {
    handoffStatus.value = data.status
    handoffData.value = data
    if (pendingMessage.value) {
      const m = pendingMessage.value
      pendingMessage.value = null
      input.value = m.text
      attachments.value = m.atts
      nextTick(() => {
        autoGrow()
        send()
      })
    }
  },
})

// ---- handoff 操作 ----
async function startHandoff() {
  // 侧边会话是临时窗口，不支持 handoff（会切换全局当前会话，破坏隔离）
  if (props.asideMode) return
  if (!currentSid.value) return
  try {
    const d = await chatApi.handoff(currentSid.value)
    sessStore.setCurrent(d.new_session_id)
    messages.value = []
    sessionMetrics.value = null
    currentTurnMetrics.value = null
    handoffStatus.value = 'generating'
    handoffData.value = null
    thresholdBreached.value = null
  } catch {
    toast.push('error', '创建新会话失败')
  }
}

function removeHandoff() {
  handoffStatus.value = null
  handoffData.value = null
  pendingMessage.value = null
}

// 模型选择器
const providers = ref([])
const chatModelId = ref(null)
const modelControlOpen = ref(false)
const modelControlPanel = ref('overview')
const selectedModelLabel = computed(
  () => providers.value.find((p) => p.id === chatModelId.value)?.display_name || '未配置模型'
)
async function loadProviders() {
    const all = await chatApi.providers()
    const a = await chatApi.modelAssignment()
  // 隐藏 embedding 专用模型（如本地 BGE-M3），它不能用于对话
  const embId = a.embedding_model?.provider_id
  providers.value = all.filter((p) => p.id !== embId)
  // 若当前 chat 分配指向被隐藏的模型，回退到首个可用模型
  const cur = a.chat_model?.provider_id
  chatModelId.value = providers.value.some((p) => p.id === cur)
    ? cur
    : (providers.value[0]?.id ?? null)
  await loadModelCapabilities()
}
async function loadModelCapabilities() {
  try {
    // 兼容旧版后端：该接口早于 model-capabilities 投影，现由同一能力目录支撑
    const result = await chatApi.reasoningEfforts()
    const values = Array.isArray(result)
      ? result.map((item) => item.value).filter(Boolean)
      : result?.reasoning_efforts || []
    reasoningOptions.value = values.length
      ? REASONING_EFFORT_OPTIONS.filter((item) => values.includes(item.value))
      : [...REASONING_EFFORT_OPTIONS]
    if (!reasoningOptions.value.some((item) => item.value === reasoningEffort.value)) {
      reasoningEffort.value = reasoningOptions.value[0]?.value || 'off'
    }
  } catch {
    reasoningOptions.value = [...REASONING_EFFORT_OPTIONS]
  }
}
async function switchModel(pid) {
  chatModelId.value = pid
  // 仅切换对话模型，不动 agent 模型分配（设置页的精细分配不被覆盖）
  await chatApi.setModelAssignment({ chat_model: pid })
  await loadModelCapabilities()
  toast.push('success', '已切换，下一轮对话生效')
}

// ---- 每轮统一由宿主传递推理等级，模型在该预算内自行决定工具调用。 ----
const reasoningEffort = ref('high')
const REASONING_EFFORT_OPTIONS = [
  { value: 'off', label: '关闭推理' },
  { value: 'low', label: '低推理' },
  { value: 'high', label: '高推理' },
  { value: 'max', label: '最大推理' },
]
const reasoningOptions = ref([...REASONING_EFFORT_OPTIONS])
const reasoningEffortLabel = computed(
  () =>
    (
      reasoningOptions.value.find((m) => m.value === reasoningEffort.value) ||
      reasoningOptions.value[0] ||
      REASONING_EFFORT_OPTIONS[2]
    ).label
)
const reasoningEffortCompactLabel = computed(
  () =>
    ({
      off: 'Off',
      low: 'Low',
      high: 'High',
      max: 'Max',
    })[reasoningEffort.value] || 'High'
)

function toggleModelControl() {
  modelControlOpen.value = !modelControlOpen.value
  if (modelControlOpen.value) modelControlPanel.value = 'overview'
}

function openModelControlPanel(panel) {
  modelControlPanel.value = panel
}

function closeModelControl() {
  modelControlOpen.value = false
  modelControlPanel.value = 'overview'
}

async function pickChatModel(pid) {
  if (pid !== chatModelId.value) await switchModel(pid)
  closeModelControl()
}

function pickReasoningEffort(v) {
  reasoningEffort.value = normalizeReasoningEffort(v)
  closeModelControl()
}

// 点击面板外部关闭模型与推理等级菜单（菜单与入口按钮自身的事件已 stop）
function onDocClickModelControl(e) {
  if (!modelControlOpen.value) return
  if (e.target.closest('.model-control-menu') || e.target.closest('.model-control-btn')) return
  closeModelControl()
}

// 仅提示类系统通知由 useChatSession 统一处理

/** 历史消息默认折叠思考过程；仅用户点开时展开。 */
function messageThinkExpanded(m) {
  return m.thinkOpen === true
}
function toggleMessageThink(m) {
  m.thinkOpen = !messageThinkExpanded(m)
}

function findMessageIndexById(id) {
  if (id === null || id === undefined) return -1
  const key = String(id)
  return messages.value.findIndex((m) => m.id !== null && m.id !== undefined && String(m.id) === key)
}

/** 编辑提交：从被编辑的用户消息起截断后续 UI（含旧 AI 回复） */
function trimMessagesFromEdit(editMsgId) {
  const idx = findMessageIndexById(editMsgId)
  if (idx === -1) return false
  messages.value = messages.value.slice(0, idx)
  return true
}

async function openSession(sid, opts = {}) {
  try {
    if (editingId.value) cancelEdit()
    // aside 模式不切换全局当前会话（否则会连累主对话侧栏高亮/加载）
    if (!props.asideMode) sessStore.setCurrent(sid)
    const [msgs, metrics] = await Promise.all([fetchSessionMessages(sid), fetchSessionMetrics(sid)])
    messages.value = msgs
    resetMessageWindow(msgs.length)
    sessionMetrics.value = metrics
    currentTurnMetrics.value = metrics?.current_turn || null
    if (opts.messageId) scrollToMessage(opts.messageId)
    else scrollBottom()
    tryReattach(sid)
  } catch (e) {
    toast.push('error', friendlyError(e?.message, '加载会话失败'))
  }
}

// 搜索跳转：滚动到指定消息并短暂高亮闪烁
function scrollToMessage(mid) {
  nextTick(() => {
    // 消息可能因懒渲染稍后进 DOM；两次尝试足够覆盖 v-for 完成的时序
    const tryScroll = (retry) => {
      const el = document.querySelector(`[data-msg-id="${mid}"]`)
      if (!el) {
        if (retry > 0) window.setTimeout(() => tryScroll(retry - 1), 60)
        return
      }
      el.scrollIntoView({ block: 'center', behavior: 'smooth' })
      el.classList.add('msg-flash')
      window.setTimeout(() => el.classList.remove('msg-flash'), 1800)
    }
    tryScroll(3)
  })
}
// 刷新/切回会话时重挂进行中的生成：后端生成与连接已解耦，
// 同 crid 重连从头回放缓冲并续跟实时事件，生成不会因刷新丢失
async function tryReattach(sid) {
  if (generating.value) return
  try {
    const d = await chatApi.activeRequest(sid)
    const crid = d?.client_request_id
    if (!crid || currentSid.value !== sid) return
    beginStream(sid)
    streamCrid.value = crid
    // 重挂后删掉尾部尚未完成的那轮用户消息渲染冗余风险低：回放事件仅重建流式区
    await sse.send({
      sessionId: sid,
      message: '',
      clientRequestId: crid,
      trackActive: !props.asideMode,
      onEvent: (ev, data) => handleEvent(ev, data),
      onError: (e) => {
        toast.push('error', friendlyError(e?.message))
        finishStream()
      },
    })
    // 兜底：重挂流异常断开（无终止事件）时同样保留已输出内容
    if (generating.value) finishStream()
  } catch {
    /* 无进行中请求或接口异常：静默跳过 */
  }
}
// 附件上传（拖拽 / 点击选择，解析常见格式）
const attachments = ref([]) // { name, chars, text, truncated, parsed, uploading, error }
const dragOver = ref(false)
const fileInput = ref(null)
function triggerFile() {
  fileInput.value && fileInput.value.click()
}
function onFilePick(e) {
  uploadFiles(e.target.files)
  e.target.value = ''
}
function onDragLeave(e) {
  // 仅当离开整个 composer（而非移到子元素）才取消高亮
  if (!e.currentTarget.contains(e.relatedTarget)) dragOver.value = false
}
// 从图片 URL（/chat-images/xxx.png）取文件名，供后端 keep_image_names 匹配
function imgBasename(url) {
  try {
    return String(url).split('?')[0].split('#')[0].split('/').pop() || url
  } catch {
    return url
  }
}
function onDrop(e) {
  dragOver.value = false
  const dt = e.dataTransfer
  if (!dt) return
  let files = dt.files && dt.files.length ? Array.from(dt.files) : []
  if (!files.length && dt.items) {
    files = Array.from(dt.items)
      .filter((it) => it.kind === 'file')
      .map((it) => it.getAsFile())
      .filter(Boolean)
  }
  if (files.length) uploadFiles(files)
}
// 编辑态新增附件标 origin:'new'，供 submitEdit 区分保留/新增；普通发送不带 origin。
async function uploadFiles(fileList) {
  const MAX = 5
  const origin = editingId.value ? 'new' : undefined
  if (attachments.value.length >= MAX) {
    toast.push('warning', `最多上传 ${MAX} 个文件，当前已有 ${attachments.value.length} 个`)
    return
  }
  const allowed = Array.from(fileList).slice(0, MAX - attachments.value.length)
  if (fileList.length > allowed.length) {
    toast.push('warning', `一次最多上传 ${MAX} 个文件，已自动截取前 ${allowed.length} 个`)
  }
  for (const f of allowed) {
    const isImage = (f.type || '').startsWith('image/')
    if (isImage) {
      // 图片：读为 base64 dataURL 直接作多模态内容发送，无需文本解析
      const preview = URL.createObjectURL(f)
      const dataUrl = await readAsDataURL(f)
      attachments.value.push({ name: f.name, uploading: false, isImage: true, preview, dataUrl, origin })
      continue
    }
    const item = { name: f.name, uploading: true, isImage: false, origin }
    const idx = attachments.value.push(item) - 1
    try {
      const fd = new FormData()
      fd.append('file', f)
      const d = await chatApi.uploadAttachment(fd)
      attachments.value[idx] = {
        name: d.filename,
        chars: d.chars,
        text: d.text,
        truncated: d.truncated,
        parsed: d.parsed,
        uploading: false,
        isImage: false,
        file: f,
        origin,
      }
      if (!d.parsed) toast.push('warning', `「${d.filename}」未能解析出文本内容`)
    } catch {
      attachments.value[idx] = { name: f.name, uploading: false, error: true, isImage: false, origin }
    }
  }
}
function readAsDataURL(f) {
  return new Promise((resolve) => {
    const r = new FileReader()
    r.onload = () => resolve(r.result)
    r.onerror = () => resolve('')
    r.readAsDataURL(f)
  })
}
function removeAttachment(i) {
  const a = attachments.value[i]
  // 仅本地新建的 blob 预览需要 revoke；编辑态保留的旧图是服务端 URL，不能 revoke
  if (a && a.isImage && a.preview && a.origin !== 'existing') URL.revokeObjectURL(a.preview)
  attachments.value.splice(i, 1)
}
function clearAttachments(opts = {}) {
  const keep = opts.keepPreviews
  for (const a of attachments.value) {
    if (a.isImage && a.preview && !(keep && keep.has(a.preview))) URL.revokeObjectURL(a.preview)
  }
  attachments.value = []
}

// ---- 消息选中操作（复制 / 引用） -----------------------------------------
async function copySelection() {
  const t = selection.text.value
  if (!t) return
  try {
    await navigator.clipboard.writeText(t)
    toast.push('success', '已复制')
  } catch {
    toast.push('warning', '复制失败，请手动 Ctrl+C')
  }
  selection.hide()
}
// QuoteComposer 弹窗暂存的引用原文与元信息；visible 控制弹窗开合
// forAside：true 表示这条引用要送去「侧边会话」而非当前输入框（确认后 emit 上抛）
const pendingQuote = ref({
  visible: false,
  text: '',
  sourceMsgId: null,
  sourceRole: null,
  forAside: false,
})
function _openQuoteComposer(forAside) {
  const t = selection.text.value
  if (!t) return
  // 送往侧边会话不占本输入框的附件配额；仅本地引用才校验 MAX
  const MAX = 5
  if (!forAside && attachments.value.length >= MAX) {
    toast.push('warning', `最多 ${MAX} 个附件`)
    selection.hide()
    return
  }
  // 打开评论录入弹窗；确认后再落到 attachments / 侧边会话。这里先把原文与来源
  // 暂存下来，收起 SelectionActionBar 并清空浏览器选区，避免 toolbar 复现
  pendingQuote.value = {
    visible: true,
    text: t,
    sourceMsgId: selection.sourceMsgId.value,
    sourceRole: selection.sourceRole.value,
    forAside,
  }
  selection.hide()
  window.getSelection?.()?.removeAllRanges?.()
}
function quoteSelection() {
  _openQuoteComposer(false)
}
// 划词「侧边会话」：复用同一评论录入弹窗，确认后把引用送去侧边会话
function asideSelection() {
  _openQuoteComposer(true)
}
function cancelQuoteComposer() {
  pendingQuote.value = {
    visible: false,
    text: '',
    sourceMsgId: null,
    sourceRole: null,
    forAside: false,
  }
}
// 把一条引用（原文 + 可选评论）压入本实例输入框的附件条。抽出复用：既供
// 本地「引用」确认，也供侧边会话被 SideChatDrawer 注入选中文本（injectQuote）。
function pushQuoteAttachment({ text, comment, sourceMsgId, sourceRole }) {
  const t = text
  if (!t) return
  const MAX = 5
  if (attachments.value.length >= MAX) {
    toast.push('warning', `最多 ${MAX} 个附件`)
    return
  }
  // 与"粘贴的文本"共用同一通道：同一形状、同一附件面板、同一历史还原路径。
  // kind:'quote' 用于渲染层切图标/胶囊底色；comment 可选，弹窗留空即为 ''。
  const n = attachments.value.filter((a) => a.kind === 'quote').length
  attachments.value.push({
    name: n ? `引用 ${n + 1}` : '引用',
    pasted: true,
    kind: 'quote',
    parsed: true,
    isImage: false,
    uploading: false,
    text: t,
    comment: comment || '',
    chars: t.length,
    lines: t.split('\n').length,
    sourceMsgId: sourceMsgId ?? null,
    sourceRole: sourceRole ?? null,
    origin: editingId.value ? 'new' : undefined,
  })
}
function commitQuoteAttachment({ comment }) {
  const q = pendingQuote.value
  const t = q.text
  if (!t) {
    cancelQuoteComposer()
    return
  }
  // 送往侧边会话：不落本输入框，上抛给宿主（SideChatDrawer）开/续侧边会话
  if (q.forAside) {
    emit('open-aside', {
      text: t,
      comment: comment || '',
      sourceMsgId: q.sourceMsgId,
      sourceRole: q.sourceRole,
    })
    cancelQuoteComposer()
    return
  }
  pushQuoteAttachment({
    text: t,
    comment,
    sourceMsgId: q.sourceMsgId,
    sourceRole: q.sourceRole,
  })
  cancelQuoteComposer()
  nextTick(() => {
    ta.value?.focus()
  })
}

// 供 SideChatDrawer 调用：把选中文本作为引用注入本侧边实例的输入框（不自动发送）
function injectQuote(payload) {
  pushQuoteAttachment(payload)
  nextTick(() => {
    ta.value?.focus()
  })
}
defineExpose({ injectQuote })

// ---- 表情选择器：点击表情插入输入框光标处（支持连续插入，点击外部关闭） ----
const emojiOpen = ref(false)
const EMOJI_GROUPS = [
  {
    name: '表情',
    items: [
      '😀',
      '😄',
      '😁',
      '😂',
      '🤣',
      '😊',
      '😇',
      '🙂',
      '😉',
      '😍',
      '😘',
      '😜',
      '🤪',
      '🤔',
      '🤨',
      '😐',
      '😏',
      '😒',
      '🙄',
      '😬',
      '😮',
      '😲',
      '🥱',
      '😴',
      '🤤',
      '😵',
      '🤯',
      '🥳',
      '😎',
      '🤓',
      '🧐',
      '😢',
      '😭',
      '😤',
      '😠',
      '😡',
      '🤬',
      '😱',
      '😨',
      '😰',
      '😥',
      '😓',
      '🤗',
      '🤭',
      '🤫',
      '🥺',
      '🫡',
    ],
  },
  {
    name: '手势',
    items: [
      '👍',
      '👎',
      '👌',
      '✌️',
      '🤞',
      '🤟',
      '🤘',
      '🤙',
      '👈',
      '👉',
      '👆',
      '👇',
      '☝️',
      '✋',
      '🤚',
      '🖖',
      '👋',
      '🤏',
      '💪',
      '🙏',
      '👏',
      '🤝',
      '✊',
      '👊',
      '🤛',
      '🤜',
      '👐',
      '🤲',
    ],
  },
  {
    name: '爱心',
    items: [
      '❤️',
      '🧡',
      '💛',
      '💚',
      '💙',
      '💜',
      '🖤',
      '🤍',
      '🤎',
      '💔',
      '❣️',
      '💕',
      '💞',
      '💓',
      '💗',
      '💖',
      '💘',
      '💝',
      '💟',
      '💯',
    ],
  },
  {
    name: '动物',
    items: [
      '🐶',
      '🐱',
      '🐭',
      '🐹',
      '🐰',
      '🦊',
      '🐻',
      '🐼',
      '🐨',
      '🐯',
      '🦁',
      '🐮',
      '🐷',
      '🐸',
      '🐵',
      '🙈',
      '🙉',
      '🙊',
      '🐔',
      '🐧',
      '🐦',
      '🦆',
      '🦉',
      '🐺',
      '🐴',
      '🦄',
      '🐝',
      '🦋',
      '🐞',
      '🐢',
      '🐍',
      '🐙',
      '🐠',
      '🐟',
      '🐬',
      '🐳',
      '🦈',
    ],
  },
  {
    name: '食物',
    items: [
      '🍎',
      '🍊',
      '🍋',
      '🍌',
      '🍉',
      '🍇',
      '🍓',
      '🍒',
      '🍑',
      '🥭',
      '🍍',
      '🥥',
      '🍅',
      '🥑',
      '🥦',
      '🌽',
      '🥕',
      '🍞',
      '🥐',
      '🧀',
      '🥚',
      '🍳',
      '🥓',
      '🍔',
      '🍟',
      '🍕',
      '🌭',
      '🌮',
      '🥗',
      '🍿',
      '🍜',
      '🍣',
      '🍤',
      '🍦',
      '🍩',
      '🍪',
      '🎂',
      '🍰',
      '🧁',
      '🍫',
      '🍬',
      '🍭',
      '☕',
      '🍵',
      '🍺',
      '🥂',
      '🍷',
    ],
  },
  {
    name: '活动',
    items: [
      '⚽',
      '🏀',
      '🏈',
      '⚾',
      '🎾',
      '🏐',
      '🎱',
      '🏓',
      '🏸',
      '⛳',
      '🏹',
      '🎣',
      '🥊',
      '🎿',
      '🏂',
      '🏋️',
      '🤸',
      '🏄',
      '🏊',
      '🚴',
      '🚵',
      '🎯',
      '🎳',
      '🎲',
      '🎮',
      '♟️',
      '🎭',
      '🎨',
      '🎬',
      '🎤',
      '🎧',
      '🎹',
      '🥁',
      '🎷',
      '🎺',
      '🎸',
      '🎻',
    ],
  },
  {
    name: '物品',
    items: [
      '⌚',
      '📱',
      '💻',
      '⌨️',
      '🖥️',
      '🖱️',
      '🕹️',
      '💿',
      '📷',
      '📸',
      '📹',
      '📺',
      '📻',
      '⏰',
      '⌛',
      '⏳',
      '📡',
      '🔋',
      '💡',
      '🔦',
      '🕯️',
      '💸',
      '💰',
      '💳',
      '💎',
      '🧰',
      '🔧',
      '🔨',
      '🛠️',
      '⚙️',
      '🔪',
      '🛡️',
      '🔮',
      '🔭',
      '🔬',
      '💊',
      '💉',
      '🧬',
      '🦠',
      '🧪',
      '🧹',
      '🧻',
      '🛁',
      '🧼',
      '🔑',
      '🗝️',
      '🚪',
      '🪑',
      '🛋️',
      '🛏️',
      '🧸',
      '🖼️',
      '🛒',
      '🎁',
      '🎈',
      '🎀',
      '🎊',
      '🎉',
      '📦',
      '📝',
      '📁',
      '📂',
      '📚',
      '📖',
      '✏️',
      '🖊️',
      '✂️',
      '🔍',
      '📌',
      '📍',
      '🗑️',
      '♻️',
    ],
  },
  {
    name: '符号',
    items: [
      '✅',
      '❌',
      '❓',
      '💢',
      '💥',
      '💫',
      '💦',
      '💨',
      '💬',
      '💭',
      '♨️',
      '🔔',
      '🔕',
      '🎵',
      '🎶',
      '📢',
      '📣',
      '🔊',
      '🔇',
      '⭕',
      '🔴',
      '🟠',
      '🟡',
      '🟢',
      '🔵',
      '🟣',
      '⚫',
      '⚪',
      '🔺',
      '🔻',
      '🔸',
      '🔹',
      '🏁',
      '🚩',
      '🎌',
    ],
  },
]
function insertEmoji(em) {
  const el = ta.value
  const pos = el && el.selectionStart !== null && el.selectionStart !== undefined ? el.selectionStart : input.value.length
  const end = el && el.selectionEnd !== null && el.selectionEnd !== undefined ? el.selectionEnd : pos
  input.value = input.value.slice(0, pos) + em + input.value.slice(end)
  nextTick(() => {
    const p = pos + em.length
    if (el) {
      el.focus()
      el.setSelectionRange(p, p)
    }
  })
  autoGrow()
}
// 点击面板外部关闭表情选择器（面板与切换按钮自身的事件已 stop）
function onDocClickEmoji(e) {
  if (!emojiOpen.value) return
  if (e.target.closest('.emoji-panel') || e.target.closest('.emoji-toggle')) return
  emojiOpen.value = false
}

// ---- 附件统一点击交互：粘贴文本/图片弹窗预览，其他格式弹窗信息+下载 ----
const attachView = ref(null) // { type: 'text'|'image'|'file', ... }
// composer 附件胶囊点击
function openAttachment(a) {
  if (a.uploading || a.error) return
  if (a.pasted) {
    attachView.value = {
      type: 'text',
      name: a.name,
      text: a.text,
      chars: a.chars,
      lines: a.lines,
      kind: a.kind,
      comment: a.comment,
    }
  } else if (a.isImage) {
    attachView.value = { type: 'image', name: a.name, src: a.preview }
  } else {
    attachView.value = {
      type: 'file',
      name: a.name,
      file: a.file || null,
      size: a.file ? a.file.size : null,
      chars: a.chars,
    }
  }
}
// 消息气泡附件胶囊点击（含历史会话还原的附件）
function openMsgAttachment(att) {
  if (att.pasted) {
    const text = att.text || ''
    attachView.value = {
      type: 'text',
      name: att.name,
      text,
      chars: text.length,
      lines: text.split('\n').length,
      kind: att.kind,
      comment: att.comment,
    }
  } else {
    attachView.value = {
      type: 'file',
      name: att.name,
      file: att.file || null,
      size: att.file ? att.file.size : null,
      chars: att.chars,
    }
  }
}
function openBubbleImage(src) {
  attachView.value = { type: 'image', name: '图片', src }
}
function downloadAttachFile(file) {
  const url = URL.createObjectURL(file)
  const el = document.createElement('a')
  el.href = url
  el.download = file.name
  el.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
function attachExt(name) {
  return name && name.includes('.') ? name.split('.').pop().toUpperCase() : '未知'
}
// 文档附件统一存入知识库：发送时后台异步导入，不阻塞对话
async function ingestToKb(file) {
  try {
    const fd = new FormData()
    fd.append('file', file)
    const r = await chatApi.importDocument(fd)
    if (r.duplicate) {
      // 文档已在知识库中：跳过重复导入，不影响当前对话的文档解析
      toast.push('info', `「${file.name}」已在知识库中，跳过重复导入（已有 ${r.extracted} 条记忆）`)
    } else {
      toast.push('success', `「${file.name}」已存入知识库，提炼 ${r.extracted} 条记忆`)
    }
  } catch {
    /* api 层已提示错误 */
  }
}
// 超长文本粘贴自动收纳为附件的阈值（低于阈值维持直接进输入框）
const PASTE_MAX_CHARS = 2000
const PASTE_MAX_LINES = 30
function onPaste(e) {
  // 从剪贴板提取图片/文件（截图粘贴等），走统一上传流程
  const cd = e.clipboardData
  if (!cd) return
  const files = []
  for (const it of cd.items || []) {
    if (it.kind === 'file') {
      const f = it.getAsFile()
      if (!f) continue
      // 剪贴板图片常无文件名，补一个
      if (!f.name || f.name === 'image.png' || f.name === 'blob') {
        const ext = (f.type.split('/')[1] || 'png').replace('jpeg', 'jpg')
        files.push(new File([f], `pasted-${Date.now()}.${ext}`, { type: f.type }))
      } else {
        files.push(f)
      }
    }
  }
  if (files.length) {
    e.preventDefault()
    uploadFiles(files)
    return
  }
  // 超长纯文本：不进输入框，收纳为「粘贴的文本」附件（可点击弹窗回看全文）
  const text = cd.getData('text/plain') || ''
  if (text.length > PASTE_MAX_CHARS || text.split('\n').length > PASTE_MAX_LINES) {
    e.preventDefault()
    addPastedText(text)
  }
}
function addPastedText(text) {
  const MAX = 5
  if (attachments.value.length >= MAX) {
    toast.push('warning', `最多上传 ${MAX} 个附件，当前已有 ${attachments.value.length} 个`)
    return
  }
  const n = attachments.value.filter((a) => a.pasted).length
  attachments.value.push({
    name: n ? `粘贴的文本 ${n + 1}` : '粘贴的文本',
    pasted: true,
    parsed: true,
    isImage: false,
    uploading: false,
    text,
    chars: text.length,
    lines: text.split('\n').length,
    origin: editingId.value ? 'new' : undefined,
  })
}

async function send() {
  // 编辑态：底部同一输入框提交修改，不走新建消息路径
  if (editingId.value) {
    await submitEdit()
    return
  }
  const text = input.value.trim()
  const atts = attachments.value.filter((a) => a.parsed && a.text)
  const imgs = attachments.value.filter((a) => a.isImage && a.dataUrl).map((a) => a.dataUrl)
  const kbFiles = attachments.value.filter((a) => a.file && !a.isImage).map((a) => a.file)
  if ((!text && !atts.length && !imgs.length) || generating.value) return
  // handoff 摘要生成中：消息暂存（会话上下文管理方案 v2）
  if (handoffStatus.value === 'generating') {
    pendingMessage.value = { text, atts: attachments.value }
    return
  }
  // 无当前会话（新对话/欢迎页）：新建一条全新会话，不复用旧空会话，
  // 避免消息落进以前的会话记录
  if (!currentSid.value) {
    if (props.asideMode) {
      // 侧边会话：首条发送时才创建（channel='aside' + from_session 记来源），
      // 全程不触碰全局会话列表/当前会话，实现内容隔离、不进列表。
      const d = await chatApi.createSession({
        aside: true,
        from_session: props.asideFromSession || undefined,
        project_id: props.asideProjectId || undefined,
      })
      asideLocalSid.value = d.session_id
      emit('aside-session-created', d.session_id)
      messages.value = []
    } else {
      // M5.1：若来自「+ 新建会话」项目挂载，此时 pendingProjectId 有值
      const pendingPid = sessStore.pendingProjectId
      const body = pendingPid ? { project_id: pendingPid } : {}
      const d = await chatApi.createSession(body)
      // 占位挂到正确的项目下，避免侧栏先出现在「最近」再跳到工作区
      sessStore.ensurePlaceholder(d.session_id, pendingPid)
      if (pendingSandboxMode.value) {
        try {
          await projectsApi.setSandboxMode(d.session_id, pendingSandboxMode.value)
        } catch {
          /* toast 已弹 */
        }
        pendingSandboxMode.value = null
      }
      sessStore.setCurrent(d.session_id) // 内部会自动清空 pendingProjectId
      sessStore.scheduleTitleRefresh(d.session_id)
      sessStore.load() // 后台同步真实数据
      messages.value = []
    }
  }
  // handoff 附件路径：新会话首条消息携带
  let hPath = null
  if (handoffStatus.value === 'ready' && messages.value.length === 0) {
    hPath = `artifacts/handoffs/${currentSid.value}.md`
  }
  // 构造发送给后端的消息：把附件解析文本作为上下文前置（不截断，完整交给模型）
  // 引用附件（kind:'quote'）走 【选中的文本】\n{原文} + 可选 \n\n【用户评论】\n{评论}
  // 双标签，让模型清楚地区分"被引用的原文"和"用户对这段的评论"。
  // 其它附件（粘贴/文档）继续 【附件：xxx】 老格式；主输入文字仍用 \n---\n 尾部分隔。
  let backendMsg = text
  if (atts.length) {
    const blocks = atts
      .map((a) => {
        if (a.kind === 'quote') {
          const base = `【选中的文本】\n${a.text || ''}`
          return a.comment ? `${base}\n\n【用户评论】\n${a.comment}` : base
        }
        return `【附件：${a.name}】\n${a.text || ''}`
      })
      .join('\n\n')
    backendMsg = blocks + '\n\n---\n' + (text || '请阅读上述附件内容并回应。')
  }
  if (!backendMsg && imgs.length) backendMsg = '请看图并回应。'
  // 气泡附件：保留粘贴全文与原始 File，供发送后点击弹窗回看/下载
  // kind 保留后气泡胶囊可以按引用/粘贴/文件切换图标与底色；comment 用于胶囊"·带评论"标记
  const bubbleAtts = attachments.value
    .filter((a) => !a.isImage)
    .map((a) => ({
      name: a.name,
      pasted: !!a.pasted,
      kind: a.kind,
      comment: a.comment,
      text: a.pasted ? a.text : undefined,
      file: a.file,
      chars: a.chars,
    }))
  const bubbleImages = attachments.value.filter((a) => a.isImage && a.preview).map((a) => a.preview)
  // 已随气泡送出的图片 preview（blob URL）不能在清空附件时 revoke，
  // 否则消息气泡中的图片立即失效，需等刷新后由后端历史 URL 才恢复
  const sentPreviews = new Set(bubbleImages)
  messages.value.push({
    role: 'user',
    content: text || (imgs.length ? '' : '（已上传附件）'),
    atts: bubbleAtts,
    images: bubbleImages,
    create_time: nowLocalIso(),
  })
  input.value = ''
  clearAttachments({ keepPreviews: sentPreviews })
  // 文档附件统一存入知识库：后台异步导入，不阻塞本次对话
  kbFiles.forEach((f) => ingestToKb(f))
  nextTick(autoGrow)
  beginStream(currentSid.value)
  scrollBottom()

  streamCrid.value = genCrid()
  await sse.send({
    sessionId: currentSid.value,
    // M5.1：无 sessionId + pendingProjectId 时，后端会带项目建库
    projectId: currentSid.value ? undefined : sessStore.pendingProjectId,
    message: backendMsg,
    images: imgs.length ? imgs : undefined,
    location: geoEnabled.value ? cachedLocation() : undefined,
    handoffPath: hPath,
    reasoningEffort: reasoningEffort.value,
    clientRequestId: streamCrid.value,
    trackActive: !props.asideMode,
    onEvent: (ev, data) => handleEvent(ev, data),
    onError: (e) => {
      toast.push('error', friendlyError(e?.message))
      finishStream()
    },
  })
  // 兜底：始终未收到 turn_completed/error（服务重启等异常断开）时，
  // 同样保留已输出内容并释放输入锁，避免 UI 卡在生成中
  if (generating.value) finishStream()
  // 发送后清除 handoff 附件状态
  if (hPath) {
    handoffStatus.value = null
    handoffData.value = null
  }
}

// 手动停止：唯一会真正中断后台生成的动作。
// 已输出的内容必须保留：先即时保留屏上部分，后端中断补救会把已生成部分落库
// （带“未完成”标记），随后以落库版本为准重载会话，保证屏上所见即 DB 所存
async function abort() {
  // aside 用本地 crid（不写全局 sp_active_crid），避免误取消主对话的进行中流
  const crid = props.asideMode ? streamCrid.value : sessionStorage.getItem('sp_active_crid')
  const sid = streamSid.value
  sse.abort()
  finishStream() // 即时保留已输出部分，避免闪烁
  if (crid) {
    try {
      await chatApi.cancel(crid)
    } catch {
      /* 忽略 */
    }
  }
  // 后端中断补救落库为异步完成，稍候重载该会话消息，拉取持久化版本（含真实 id 与标记）
  if (sid) {
    ;[300, 900, 2000].forEach((ms) =>
      setTimeout(() => {
        if (currentSid.value === sid && !generating.value) {
          reloadMessages(sid, { preserveLocalTail: true })
        }
      }, ms)
    )
  }
}

// 反馈原因弹窗（自研对话框，替代原生 prompt；中文标签映射英文枚举值提交）
const fbDialog = ref(null) // { msg, fb, reason }
const goodReasons = [
  { value: 'helpful', label: '内容准确、有帮助' },
  { value: 'intent_right', label: '准确理解了我的意图' },
  { value: 'tone_right', label: '语气风格合适' },
  { value: 'output_format_right', label: '结构与格式清晰' },
  { value: 'other', label: '其他' },
]
const badReasons = [
  { value: 'inaccurate', label: '内容不准确' },
  { value: 'intent_wrong', label: '没理解我的意图' },
  { value: 'memory_stale', label: '引用的记忆已过时' },
  { value: 'tone_wrong', label: '语气风格不合适' },
  { value: 'output_format_wrong', label: '输出格式不对' },
  { value: 'other', label: '其他' },
]
function feedback(msg, fb) {
  if (!msg.id) {
    toast.push('warning', '该消息暂不支持反馈')
    return
  }
  if (msg.feedback === fb) return // 已提交过相同反馈
  fbDialog.value = { msg, fb, reason: '', custom: '' }
}
async function submitFeedback() {
  const d = fbDialog.value
  if (!d || !d.reason) return
  // “其他”选项：提交用户自行输入的描述（加 other: 前缀便于后端识别自由文本）
  const reason = d.reason === 'other' ? 'other:' + d.custom.trim() : d.reason
  if (d.reason === 'other' && !d.custom.trim()) return
  await chatApi.feedback({ message_id: d.msg.id, feedback: d.fb, reason })
  d.msg.feedback = d.fb
  fbDialog.value = null
  toast.push('success', '反馈已记录')
}

// ---- 消息编辑（复用底部 composer，不再单独做编辑框）----
const editingId = ref(null)
// 进入编辑前暂存的草稿，取消/提交后恢复
const composerDraft = ref(null)
// 被编辑消息的原始快照，用于判定是否有实质修改
const editingOrig = ref(null)

async function startEdit(msg) {
  if (generating.value) return
  // 保底：文档附件正文（text）用于重建【附件：】前缀。历史/重拉消息经
  // extractAttachments 已带 text；极少数刚发出的非粘贴附件可能缺失 → 先重拉。
  const needReload = (msg.atts || []).some(
    (a) =>
      !a.isImage &&
      a.kind !== 'quote' &&
      !a.pasted &&
      (a.text === null || a.text === undefined),
  )
  if (needReload && currentSid.value) {
    await reloadMessages(currentSid.value)
    const again = messages.value.find((m) => m.id === msg.id)
    if (again) msg = again
  }
  if (editingId.value === msg.id) {
    nextTick(() => ta.value?.focus())
    return
  }
  // 切换到另一条：丢掉当前编辑附件中的新增 blob，保留首次进入时的草稿
  if (editingId.value) {
    for (const a of attachments.value) {
      if (a.isImage && a.origin === 'new' && a.preview) URL.revokeObjectURL(a.preview)
    }
  } else {
    composerDraft.value = { input: input.value, attachments: attachments.value }
  }
  editingId.value = msg.id
  editingOrig.value = {
    content: msg.content || '',
    attCount: (msg.atts?.length || 0) + (msg.images?.length || 0),
  }
  // 把消息内容灌进底部同一套 composer
  input.value = msg.content || ''
  attachments.value = [
    ...(msg.atts || []).map((a) => ({ ...a, origin: 'existing' })),
    ...(msg.images || []).map((url) => ({
      isImage: true,
      origin: 'existing',
      name: imgBasename(url),
      preview: url,
    })),
  ]
  nextTick(() => {
    autoGrow()
    ta.value?.focus()
  })
}
function cancelEdit() {
  // 仅 revoke 编辑过程中新建的 blob 预览
  for (const a of attachments.value) {
    if (a.isImage && a.origin === 'new' && a.preview) URL.revokeObjectURL(a.preview)
  }
  editingId.value = null
  editingOrig.value = null
  if (composerDraft.value) {
    input.value = composerDraft.value.input
    attachments.value = composerDraft.value.attachments
    composerDraft.value = null
  } else {
    input.value = ''
    attachments.value = []
  }
  nextTick(autoGrow)
}

async function submitEdit() {
  const msg = messages.value.find((m) => m.id === editingId.value)
  if (!msg) {
    cancelEdit()
    return
  }
  const text = input.value.trim()
  if (attachments.value.some((a) => a.uploading)) {
    toast.push('warning', '附件解析中，请稍候')
    return
  }
  const docs = attachments.value.filter((a) => !a.isImage && (a.text || a.kind === 'quote'))
  const keepImages = attachments.value.filter((a) => a.isImage && a.origin === 'existing')
  const newImageItems = attachments.value.filter((a) => a.isImage && a.origin === 'new' && a.dataUrl)
  const newImages = newImageItems.map((a) => a.dataUrl)
  const keepImageNames = keepImages.map((a) => a.name)
  const newKbFiles = docs.filter((a) => a.origin === 'new' && a.file).map((a) => a.file)

  if (!text && !docs.length && !keepImages.length && !newImages.length) {
    cancelEdit()
    return
  }
  const orig = editingOrig.value || { content: msg.content || '', attCount: 0 }
  const curExistingCount = attachments.value.filter((a) => a.origin === 'existing').length
  const hasNew = attachments.value.some((a) => a.origin === 'new')
  const attsUnchanged = !hasNew && curExistingCount === orig.attCount
  if (text === orig.content && attsUnchanged) {
    cancelEdit()
    return
  }

  let backendMsg = text
  if (docs.length) {
    const blocks = docs
      .map((a) => {
        if (a.kind === 'quote') {
          const base = `【选中的文本】\n${a.text || ''}`
          return a.comment ? `${base}\n\n【用户评论】\n${a.comment}` : base
        }
        return `【附件：${a.name}】\n${a.text || ''}`
      })
      .join('\n\n')
    backendMsg = blocks + '\n\n---\n' + (text || '请阅读上述附件内容并回应。')
  }
  if (!backendMsg && (newImages.length || keepImages.length)) backendMsg = '请看图并回应。'

  const bubbleAtts = docs.map((a) => ({
    name: a.name,
    pasted: !!a.pasted,
    kind: a.kind,
    comment: a.comment,
    text: a.pasted ? a.text : undefined,
    file: a.origin === 'new' ? a.file : undefined,
    chars: a.chars,
  }))
  const bubbleImages = [...keepImages.map((a) => a.preview), ...newImageItems.map((a) => a.preview)]
  const sentPreviews = new Set(newImageItems.map((a) => a.preview))

  const editMsgId = msg.id
  editingId.value = null
  editingOrig.value = null
  // 提交后恢复进入编辑前的草稿（若有），否则清空
  const draft = composerDraft.value
  composerDraft.value = null
  if (!trimMessagesFromEdit(editMsgId)) {
    toast.push('warning', '未能同步截断旧回复，将刷新消息列表')
    await reloadMessages(currentSid.value)
  }
  messages.value.push({
    id: -1,
    role: 'user',
    content: text,
    message_type: 'normal',
    citations: [],
    feedback: 0,
    create_time: nowLocalIso(),
    images: bubbleImages,
    atts: bubbleAtts,
    has_branches: false,
  })
  // 清掉编辑附件：保留已送入气泡的新图 preview；existing 为服务端 URL 不 revoke
  for (const a of attachments.value) {
    if (a.isImage && a.origin === 'new' && a.preview && !sentPreviews.has(a.preview)) {
      URL.revokeObjectURL(a.preview)
    }
  }
  if (draft) {
    input.value = draft.input
    attachments.value = draft.attachments
  } else {
    input.value = ''
    attachments.value = []
  }
  nextTick(autoGrow)
  newKbFiles.forEach((f) => ingestToKb(f))

  beginStream(currentSid.value)
  maybeScroll()
  streamPushSuppressed.value = true
  streamCrid.value = genCrid()
  try {
    await sse.send({
      sessionId: currentSid.value,
      message: backendMsg,
      editMessageId: editMsgId,
      attachmentsOverridden: true,
      keepImageNames,
      images: newImages.length ? newImages : undefined,
      location: geoEnabled.value ? cachedLocation() : undefined,
      reasoningEffort: reasoningEffort.value,
      clientRequestId: streamCrid.value,
      trackActive: !props.asideMode,
      onEvent: (ev, data) => handleEvent(ev, data),
      onError: (e) => {
        toast.push('error', friendlyError(e?.message))
        finishStream()
      },
    })
  } finally {
    streamPushSuppressed.value = false
  }
  if (generating.value) finishStream()
  await reloadMessages(currentSid.value)
}

// ---- 版本切换 ----
async function switchVersion(msg, direction) {
  if (generating.value) return
  const siblings = await chatApi.switchVersion({
    session_id: currentSid.value,
    version_group_id: msg.version_group_id,
    // direction: +1 → 下一个兄弟, -1 → 上一个兄弟
    // 后端需要 target_message_id，前端需要计算
    target_message_id: await getSiblingId(msg, direction),
  })
  if (siblings && siblings.messages) {
    // 历史消息：附件还原
    for (const m of siblings.messages) {
      if (
        m.role === 'user' &&
        typeof m.content === 'string' &&
        m.content.includes('\n---\n') &&
        (m.content.includes('【附件：') || m.content.includes('【选中的文本】'))
      ) {
        m.atts = extractAttachments(m.content)
        m.content = m.content.split('\n---\n').pop()
      }
    }
    messages.value = stripToastNotifs(siblings.messages)
  }
}

async function getSiblingId(msg, direction) {
  // 从当前消息列表的版本信息推断目标兄弟 ID
  // 需要查询所有兄弟消息的 ID 列表
  const resp = await chatApi.versionSiblings(msg.version_group_id)
  if (resp && resp.length) {
    const idx = resp.findIndex((s) => s.id === msg.id)
    const targetIdx = idx + direction
    if (targetIdx >= 0 && targetIdx < resp.length) return resp[targetIdx].id
  }
  return msg.id
}

// 重新生成（分支化）：旧回复保留，创建 assistant 兄弟节点
async function regenerate(msg) {
  if (generating.value || !currentSid.value) return
  if (!msg.id) {
    toast.push('warning', '该消息暂不支持重新生成')
    return
  }
  let userMsg = null
  const idx = messages.value.indexOf(msg)
  for (let j = idx - 1; j >= 0; j--) {
    if (messages.value[j].role === 'user') {
      userMsg = messages.value[j]
      break
    }
  }
  if (!userMsg || !userMsg.content) {
    toast.push('warning', '未找到对应的提问，无法重新生成')
    return
  }
  beginStream(currentSid.value)
  maybeScroll()
  streamCrid.value = genCrid()
  await sse.send({
    sessionId: currentSid.value,
    message: userMsg.content,
    regenerateMessageId: msg.id,
    location: geoEnabled.value ? cachedLocation() : undefined,
    reasoningEffort: reasoningEffort.value,
    clientRequestId: streamCrid.value,
    trackActive: !props.asideMode,
    onEvent: (ev, data) => handleEvent(ev, data),
    onError: (e) => {
      toast.push('error', friendlyError(e?.message))
      finishStream()
    },
  })
  if (generating.value) finishStream()
  // 重新加载消息列表以获取更新后的分支信息
  await reloadMessages(currentSid.value)
}

function copyText(msg) {
  navigator.clipboard.writeText(msg.content)
  toast.push('success', '已复制')
}

// 引用记忆点击查看详情（轻量弹窗，复用 /memory/detail）
const memDetail = ref(null)
async function openMemory(id) {
  try {
    memDetail.value = await memoryApi.detail(id)
  } catch {
    /* api 层已提示 */
  }
}
async function memoryFeedback(c, msg, feedbackType) {
  try {
    await chatApi.memoryFeedback({
      memory_id: c.id,
      message_id: msg.id,
      feedback_type: feedbackType,
      query_text: '',
    })
    c.memory_feedback = feedbackType
    toast.push(
      'success',
      feedbackType === 'irrelevant' ? '已降低这类问题下的召回权重' : '已标记并加入记忆治理'
    )
  } catch {
    /* api 已提示 */
  }
}
// render() 结果缓存：markdown 解析 + sanitize + enhance 是 CPU 热点。
// 双层策略：
//   1) messageRenderCache: WeakMap<message, {content, visualsSig, html}>
//      —— 消息对象引用稳定时，命中检测无需字符串 key 拼接。
//      messages.value 数组会话切换即被 GC，无需手动清理。
//   2) rawRenderCache: LRU by content —— 流式增量、匿名调用（如 streamText）用。
const messageRenderCache = new WeakMap()
const RAW_CACHE_MAX = 100
const rawRenderCache = new Map()

function renderRaw(md, visuals) {
  const src = stripTail(md, visuals)
  const hit = rawRenderCache.get(src)
  if (hit !== undefined) {
    rawRenderCache.delete(src)
    rawRenderCache.set(src, hit)
    return hit
  }
  const html = marked.parse(src).replace(/<a\s/gi, '<a target="_blank" rel="noopener noreferrer" ')
  const finalHtml = sanitizeHtml(enhanceResponseHtml(groupSections(html)))
  rawRenderCache.set(src, finalHtml)
  if (rawRenderCache.size > RAW_CACHE_MAX) {
    const oldest = rawRenderCache.keys().next().value
    rawRenderCache.delete(oldest)
  }
  return finalHtml
}

function _visualsSig(v) {
  if (!Array.isArray(v) || !v.length) return ''
  return v.length + ':' + v.map((x) => x?.type || '').join(',')
}

function render(md, visuals, message) {
  if (message && typeof message === 'object') {
    const cached = messageRenderCache.get(message)
    const sig = _visualsSig(visuals)
    if (cached && cached.content === md && cached.visualsSig === sig) {
      return cached.html
    }
    const html = renderRaw(md, visuals)
    messageRenderCache.set(message, { content: md, visualsSig: sig, html })
    return html
  }
  return renderRaw(md, visuals)
}

// 层级分组：Markdown 渲染为平铺兄弟节点，把每个 h2/h3/h4 之后、下一个
// 同级或更高级标题之前的内容包进 section.md-sec，保留内容归属与阶段识别；
// 内层低级标题递归分组，不额外增加视觉缩进。
const _isHeading = (el) => el.nodeType === 1 && /^H[2-4]$/.test(el.tagName)
const _hLevel = (el) => Number(el.tagName[1])

function _groupNodes(nodes) {
  const out = document.createDocumentFragment()
  let i = 0
  while (i < nodes.length) {
    const n = nodes[i]
    if (_isHeading(n)) {
      out.appendChild(n)
      const inner = []
      i++
      // 收集到下一个同级/更高级标题为止（容错层级跳变）
      while (i < nodes.length && !(_isHeading(nodes[i]) && _hLevel(nodes[i]) <= _hLevel(n))) {
        inner.push(nodes[i])
        i++
      }
      if (inner.length) {
        const sec = document.createElement('section')
        sec.className = 'md-sec'
        sec.appendChild(_groupNodes(inner))
        out.appendChild(sec)
      }
    } else {
      out.appendChild(n)
      i++
    }
  }
  return out
}

function groupSections(html) {
  try {
    const tpl = document.createElement('template')
    tpl.innerHTML = html
    const holder = document.createElement('div')
    holder.appendChild(_groupNodes([...tpl.content.childNodes]))
    return holder.innerHTML
  } catch {
    return html
  } // 分组失败降级为原始渲染
}
// 拆出回复尾部的"联网来源"块：正文正常渲染，来源列表渲染为默认收起的折叠面板
function webSrc(content) {
  const c = content || ''
  const idx = c.lastIndexOf('**联网来源**')
  if (idx === -1) return { body: c, list: '', count: 0 }
  const list = c.slice(idx + '**联网来源**'.length).trim()
  const count = (list.match(/^\d+\./gm) || []).length
  return { body: c.slice(0, idx).trimEnd(), list, count }
}
const _wsCache = new Map()
function cachedWebSrc(content) {
  let r = _wsCache.get(content)
  if (r) return r
  r = webSrc(content)
  _wsCache.set(content, r)
  if (_wsCache.size > 200) _wsCache.delete(_wsCache.keys().next().value)
  return r
}
const streamWebSrc = computed(() => webSrc(streamText.value))
function renderUser(text) {
  // 用户消息：转义 HTML 防注入 → 换行保留 → URL 转为新标签打开的链接
  const esc = (text || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
  return sanitizeHtml(
    esc
      .replace(
        /(https?:\/\/[^\s<]+)/g,
        '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>'
      )
      .replace(/\n/g, '<br>')
  )
}

function messageKey(msg, index) {
  if (msg && msg.id !== null && msg.id !== undefined && Number(msg.id) > 0) return `message-${msg.id}`
  if (!msg || typeof msg !== 'object') return `local-${index}`
  let key = messageElementKeys.get(msg)
  if (!key) {
    key = `local-${++localMessageKeySeq}`
    messageElementKeys.set(msg, key)
  }
  return key
}

function anchorTitle(msg, index) {
  const raw = typeof msg?.content === 'string' ? msg.content : ''
  const normalized = raw.replace(/\s+/g, ' ').trim()
  if (normalized) return normalized
  if (msg?.images?.length) return '图片消息'
  if (msg?.atts?.length) return '附件消息'
  return `第 ${index + 1} 轮对话`
}

const anchorItems = computed(() => {
  const userMessages = messages.value
    .map((msg, index) => ({ msg, index }))
    .filter(({ msg }) => msg?.role === 'user' && msg.message_type !== 'system_notification')
  return userMessages.map(({ msg, index }, anchorIndex) => ({
    key: messageKey(msg, index),
    index: anchorIndex + 1,
    title: anchorTitle(msg, anchorIndex),
  }))
})

function registerMessageElement(key, el) {
  if (!key) return
  if (el) messageElements.set(key, el)
  else messageElements.delete(key)
}

function messageTopInScroller(el, root) {
  return el.getBoundingClientRect().top - root.getBoundingClientRect().top + root.scrollTop
}

function scrollToAnchor(anchor) {
  const root = scroller.value
  const target = messageElements.get(anchor?.key)
  if (!root || !target) return
  const top = Math.max(0, messageTopInScroller(target, root) - 24)
  root.scrollTo({
    top,
    behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
  })
}

// 本地时间 ISO（秒级）由 utils/format.js 统一提供，避免各视图重复实现
function scrollBottom() {
  nextTick(() => {
    if (scroller.value) {
      scroller.value.scrollTop = scroller.value.scrollHeight
      atBottom.value = true
    }
  })
}
// 智能跟随：仅当用户已在底部附近时自动吸底；上翻后不强制拉回
const atBottom = ref(true)
let lastScrollTop = 0
function onScroll() {
  const el = scroller.value
  if (!el) return
  // 流式输出期间用户向上滚动 → 禁止自动吸底，让用户自由浏览
  if (generating.value && el.scrollTop < lastScrollTop - 5) {
    atBottom.value = false
  } else {
    atBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 80
  }
  lastScrollTop = el.scrollTop
}
function maybeScroll() {
  if (atBottom.value) scrollBottom()
}
// 流式输出期间：消息内代码块（pre 限高 300px 内部滚动）自动吸底跟随最新内容。
// v-html 每次增量都会重建 DOM（scrollTop 归零停在第一屏），故每次渲染后重新吸底；
// 与外层 atBottom 智能跟随无关，流式结束后正式消息重新渲染，代码块回到顶部便于阅读
function scrollStreamCode() {
  nextTick(() => {
    document.querySelectorAll('.content.streaming pre').forEach((p) => {
      if (p.scrollHeight > p.clientHeight) p.scrollTop = p.scrollHeight
    })
  })
}
// 流式处理进度（think-body 限高 260px 内部滚动）同样吸底跟随最新内容。
// 插值渲染 DOM 不重建，scrollTop 会停在原地；仅当用户未主动上翻（距底部很近）
// 时吸底，上翻阅读时不强行拉回（与外层消息区的智能跟随同一交互语义）
const liveThink = ref(null)
function scrollThink() {
  nextTick(() => {
    const el = liveThink.value
    if (!el || el.scrollHeight <= el.clientHeight) return
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 140) el.scrollTop = el.scrollHeight
  })
}
scrollBridge.maybe = maybeScroll
scrollBridge.think = scrollThink
scrollBridge.code = scrollStreamCode

const scrollerClass = computed(() =>
  !messages.value.length && !streamText.value ? 'scroller-shrink' : 'scroller-grow'
)

// 输入框高度自适应（最多 5 行，超出内部滚动）
function autoGrow() {
  const el = ta.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 124) + 'px'
}

// 点击侧栏 logo：回到空白新对话（欢迎页，不立即建会话）
// 仅断开读者不取消生成：回复继续在后台完成并落库，切回会话可见
function resetToHome() {
  if (editingId.value) cancelEdit()
  if (generating.value) sse.abort()
  cleanupRaf()
  sessStore.setCurrent(null)
  messages.value = []
  sessionMetrics.value = null
  currentTurnMetrics.value = null
  streamText.value = ''
  thinkText.value = ''
  reasoningText.value = ''
  decisionNotices.value = []
  toolEvents.value = []
  resetTimeline()
  preBodyPhase.value = true
  streamVisuals.value = []
  input.value = ''
  clearAttachments()
  generating.value = false
  pendingSandboxMode.value = null
  sessStore.load()
  nextTick(() => ta.value?.focus())
}

// 侧栏点击历史会话 → 加载消息
function onOpenSession(e) {
  // 兼容旧调用：detail 为字符串 → 仅切换会话；对象 {sid, messageId} → 跳转并滚动
  const d = e.detail
  if (typeof d === 'string') openSession(d)
  else if (d && d.sid) openSession(d.sid, { messageId: d.messageId })
}

onMounted(() => {
  loadProviders()
  document.addEventListener('click', handleMermaidActions)
  if (props.asideMode) {
    // 侧边会话：不加载全局会话列表、不监听全局新建/切换会话事件（那是主视图的）。
    // 已有 asideSessionId（同页内复用一个尚未关闭的侧边）则加载其消息 + 续挂进行中生成。
    if (currentSid.value && !messages.value.length) openSession(currentSid.value)
  } else {
    sessStore.load()
    window.addEventListener('sp-new-chat', resetToHome)
    window.addEventListener('sp-open-session', onOpenSession)
    // 直接从其他页面进入或刷新后恢复上次会话（currentSid 已从 localStorage 恢复）
    // → openSession 内部会调 tryReattach 续播进行中的生成，实现刷新不中断
    if (currentSid.value && !messages.value.length) openSession(currentSid.value)
  }
  initGeolocation()
  document.addEventListener('click', onDocClickEmoji)
  document.addEventListener('click', onDocClickModelControl)
  mermaidObserver = new MutationObserver(() => {
    scheduleMermaidScoped()
  })
  mermaidObserver.observe(scroller.value, { childList: true, subtree: true })
  scheduleMermaidScoped()
})
// 浏览器定位（方案 A）：开关开启时获取一次并缓存，发消息时携带城市名
const geoEnabled = ref(false)
async function initGeolocation() {
  try {
    const d = await chatApi.params()
    geoEnabled.value = !!d.params?.geolocation_enabled
    if (geoEnabled.value) {
      resolveLocation().catch(() => {
        /* 拒绝授权/超时静默降级，不影响对话 */
      })
    }
  } catch {
    /* 参数接口失败静默跳过 */
  }
}

// Mermaid 操作按钮 + HTML 预览事件委派（v-html 内无法用 Vue 事件，走原生委派）
const htmlPreview = ref(null) // 存储 HTML 源码，非 null 时展示预览抽屉
const htmlFullscreen = ref(false)
function handleMermaidActions(e) {
  const btn = e.target.closest(
    '.mermaid-zoom-out, .mermaid-zoom-in, .mermaid-reset, .mermaid-copy-src, .mermaid-copy-img, .html-preview-btn, .html-download-btn, .html-copy-btn, .code-copy-btn'
  )
  if (!btn) return
  const wrap = btn.closest('.mermaid-wrap, .html-code-wrap, .code-wrap')
  if (!wrap) return
  if (btn.classList.contains('mermaid-zoom-out')) {
    zoomInlineMermaid(wrap, 1 / 1.12)
    return
  }
  if (btn.classList.contains('mermaid-zoom-in')) {
    zoomInlineMermaid(wrap, 1.12)
    return
  }
  if (btn.classList.contains('mermaid-reset')) {
    resetInlineMermaid(wrap)
    return
  }
  const src = (wrap.dataset.source || '')
    .replace(/&amp;/g, '&')
    .replace(/&quot;/g, '"')
    .replace(/&lt;/g, '<')
  if (btn.classList.contains('mermaid-copy-src')) {
    navigator.clipboard.writeText(src)
    toast.push('success', '源码已复制')
  } else if (btn.classList.contains('mermaid-copy-img')) {
    copyMermaidAsImage(wrap)
  } else if (btn.classList.contains('html-preview-btn')) {
    htmlPreview.value = src
  } else if (btn.classList.contains('html-download-btn')) {
    const blob = new Blob([src], { type: 'text/html' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = 'preview.html'
    a.click()
    URL.revokeObjectURL(a.href)
    toast.push('success', '已下载')
  } else if (btn.classList.contains('html-copy-btn') || btn.classList.contains('code-copy-btn')) {
    navigator.clipboard.writeText(src)
    toast.push('success', '代码已复制')
  }
}
async function copyMermaidAsImage(wrap) {
  const svg = wrap.querySelector('svg')
  if (!svg) {
    toast.push('error', '图表未渲染')
    return
  }
  try {
    // 共享导出工具：自动剥离 foreignObject 避免 tainted canvas（详见 utils/svgExport.js）
    const blob = await svgToPngBlob(svg, 2)
    await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })])
    toast.push('success', '图片已复制到剪贴板')
  } catch {
    toast.push('error', '复制图片失败，请手动右键保存')
  }
}
let mermaidObserver = null
let mermaidRunInFlight = false
let mermaidRunRequested = false

function scheduleMermaidScoped() {
  mermaidRunRequested = true
  if (!mermaidRunInFlight) void runMermaidScoped()
}

// Markdown Mermaid 作为工具图的降级路径，也要提供完整的平移、缩放和重置能力。
async function runMermaidScoped() {
  if (mermaidRunInFlight) return
  mermaidRunInFlight = true
  try {
    do {
      mermaidRunRequested = false
      await nextTick()
      const root = scroller.value
      if (!root) continue
      const nodes = root.querySelectorAll('.mermaid:not([data-processed])')
      if (nodes.length) {
        try {
          const mermaid = await loadMermaid()
          await applyMermaidTheme()
          await mermaid.run({ nodes })
        } catch {
          /* 单个图表渲染失败不阻断其余图表 */
        }
      }
      bindInlineMermaidInteractions(root)
    } while (mermaidRunRequested)
  } finally {
    mermaidRunInFlight = false
  }
}
watch(() => messages.value.length, scheduleMermaidScoped)
// 不再 watch(messages, ...)：deep watch 会在任意消息字段变更（feedback、
// analysis_metadata 更新等）时触发，导致 Mermaid DOM 全量扫描。
// DOM 变化已由 mermaidObserver(MutationObserver) 捕获，length watch 覆盖
// 新消息加入，generating watch 覆盖流式结束，三者足够。
watch(generating, (v) => {
  if (!v) scheduleMermaidScoped()
})
onUnmounted(() => {
  sse.abort()
  cleanupRaf()
  for (const a of attachments.value) {
    if (a.isImage && a.preview) URL.revokeObjectURL(a.preview)
  }
  window.removeEventListener('sp-new-chat', resetToHome)
  window.removeEventListener('sp-open-session', onOpenSession)
  document.removeEventListener('click', handleMermaidActions)
  document.removeEventListener('click', onDocClickEmoji)
  document.removeEventListener('click', onDocClickModelControl)
  mermaidObserver?.disconnect()
  cleanupInlineMermaidInteractions(scroller.value)
})
</script>

<template>
  <div class="chat-root">
    <!-- 消息气泡文字选中浮层：复制 / 引用 -->
    <SelectionActionBar
      :visible="selection.visible.value"
      :rect="selection.rect.value"
      :show-aside="!asideMode"
      @copy="copySelection"
      @quote="quoteSelection"
      @aside="asideSelection"
    />
    <!-- 引用评论录入弹窗（点"引用"后弹出，确认后写入附件条） -->
    <QuoteComposer
      :visible="pendingQuote.visible"
      :quote-text="pendingQuote.text"
      @confirm="commitQuoteAttachment"
      @cancel="cancelQuoteComposer"
    />
    <!-- 定位锚点栏：独立左列，宽度不随右侧内容变化；与对话状态解耦，仅悬停触发 -->
    <MessageAnchorRail v-if="!asideMode" :anchors="anchorItems" @select="scrollToAnchor" />
    <!-- 对话区（会话列表已合并到全局侧栏 SessionSidebar） -->
    <div class="chat-main">
      <!-- 空状态：顶部 spacer 将 hero+composer 推向中间 -->
      <div v-if="!messages.length && !streamText" class="chat-spacer"></div>
      <div v-if="degraded" class="banner banner-warn-inline">ℹ 服务响应较慢，仍在等待首字输出…</div>
      <!-- overflow-anchor:none：禁用浏览器滚动锚定，避免内容高度变化时自动补偿 scrollTop 引发抖动 -->
      <div ref="scroller" :class="scrollerClass" @scroll.passive="onScroll">
        <div class="chat-scroller-inner">
          <div v-if="!messages.length && !streamText" class="chat-hero">
            <div class="logo-lg"><i class="ti ti-brain"></i></div>
            <h2>Second Person 比你更懂你！</h2>
          </div>
          <button
            v-if="hasMoreMessages"
            type="button"
            class="load-older-btn"
            :disabled="loadingMore"
            @click="showOlderMessages"
          >
            {{ loadingMore ? '加载中…' : '加载更早的消息' }}
          </button>
          <div
            v-for="(m, i) in visibleMessages"
            :key="messageKey(m, i)"
            :ref="(el) => registerMessageElement(messageKey(m, i), el)"
            class="chat-message-item"
            :data-msg-id="m.id || undefined"
            :data-anchor-key="m.role === 'user' ? messageKey(m, i) : undefined"
          >
            <!-- 用户气泡 -->
            <div v-if="m.role === 'user'" class="msg-user">
              <div :class="editingId === m.id ? 'max-w-user msg-editing' : 'max-w-user'">
                <!-- 版本翻页器（用户消息有多版本时显示） -->
                <div v-if="m.has_branches" class="version-nav end">
                  <button
                    class="ver-btn"
                    :disabled="m.sibling_index === 0"
                    @click="switchVersion(m, -1)"
                  >
                    <i class="ti ti-chevron-left"></i>
                  </button>
                  <span class="ver-indicator"
                    >{{ m.sibling_index + 1 }} / {{ m.sibling_count }}</span
                  >
                  <button
                    class="ver-btn"
                    :disabled="m.sibling_index === m.sibling_count - 1"
                    @click="switchVersion(m, 1)"
                  >
                    <i class="ti ti-chevron-right"></i>
                  </button>
                </div>
                <div v-if="m.atts && m.atts.length && editingId !== m.id" class="attach-row">
                  <span
                    v-for="(f, fi) in m.atts"
                    :key="fi"
                    class="attach-chip attach-click"
                    :class="{ 'attach-quote': f.kind === 'quote' }"
                    :title="
                      f.kind === 'quote'
                        ? '点击查看引用全文'
                        : f.pasted
                          ? '点击查看全文'
                          : '点击查看详情'
                    "
                    @click="openMsgAttachment(f)"
                  >
                    <i
                      class="ti"
                      :class="
                        f.kind === 'quote'
                          ? 'ti-quote'
                          : f.pasted
                            ? 'ti-clipboard-text'
                            : 'ti-paperclip'
                      "
                    ></i>
                    {{ f.name }}
                    <span
                      v-if="f.kind === 'quote' && f.comment"
                      class="quote-comment-mark"
                      title="包含用户评论"
                      >·带评论</span
                    >
                  </span>
                </div>
                <div v-if="m.images && m.images.length && editingId !== m.id" class="attach-row">
                  <img
                    v-for="(im, ii) in m.images"
                    :key="ii"
                    :src="im"
                    class="bubble-img"
                    @click="openBubbleImage(im)"
                  />
                </div>
                <div
                  v-if="m.content"
                  class="bubble max-w-bubble"
                  :class="{ 'bubble-editing': editingId === m.id }"
                  v-html="renderUser(m.content)"
                ></div>
                <div v-else-if="editingId === m.id" class="bubble max-w-bubble bubble-editing muted">
                  （附件消息）
                </div>
                <div v-if="editingId === m.id" class="edit-inline-hint">正在底部输入框编辑此消息</div>
                <div class="msg-actions-row user-actions">
                  <span class="msg-time">{{ formatTimeFull(m.create_time) }}</span>
                  <i class="ti ti-copy" title="复制" @click="copyText(m)"></i>
                  <i
                    v-if="m.id && !generating && editingId !== m.id"
                    class="ti ti-edit"
                    title="编辑"
                    @click="startEdit(m)"
                  ></i>
                </div>
              </div>
            </div>
            <!-- 系统通知 -->
            <div v-else-if="m.message_type === 'system_notification'" class="banner banner-brand">
              <i class="ti ti-bell"></i>
              <span class="banner-time-fade">{{ formatRelative(m.create_time) }}</span>
              {{ m.content }}
            </div>
            <!-- AI 回复 -->
            <div v-else class="msg-ai">
              <div class="avatar"><i class="ti ti-brain"></i></div>
              <div class="body">
                <!-- Legacy messages keep the old mixed text; new messages use
                     structured provider/host lanes below. -->
                <div
                  v-if="m.thinking && m.analysis_metadata?.schema_version !== 'agent-analysis-v1'"
                  class="think-panel"
                >
                  <div class="think-head" @click="toggleMessageThink(m)">
                    <span class="think-section-toggle">
                      <i
                        class="ti"
                        :class="messageThinkExpanded(m) ? 'ti-chevron-down' : 'ti-chevron-right'"
                      ></i>
                    </span>
                    <span class="think-section-title">{{
                      formatTimelineSummary(m.analysis_metadata?.timeline)
                    }}</span>
                  </div>
                  <div v-show="messageThinkExpanded(m)" class="think-body">{{ m.thinking }}</div>
                </div>
                <div
                  v-if="
                    m.analysis_metadata?.schema_version === 'agent-analysis-v1' &&
                    (m.analysis_metadata.timeline?.length ||
                      m.analysis_metadata.reasoning_text ||
                      m.analysis_metadata.system_progress ||
                      m.analysis_metadata.decision_notices?.length ||
                      m.analysis_metadata.tool_events?.length)
                  "
                  class="think-panel"
                >
                  <div class="think-head" @click="toggleMessageThink(m)">
                    <span class="think-section-toggle">
                      <i
                        class="ti"
                        :class="messageThinkExpanded(m) ? 'ti-chevron-down' : 'ti-chevron-right'"
                      ></i>
                    </span>
                    <span class="think-section-title">{{
                      formatTimelineSummary(m.analysis_metadata?.timeline)
                    }}</span>
                  </div>
                  <div v-show="messageThinkExpanded(m)" class="think-body think-body-timeline">
                    <!-- v7 优先按时间线渲染，历史消息无 timeline 时降级 -->
                    <ThinkingTimeline
                      v-if="m.analysis_metadata.timeline?.length"
                      :items="m.analysis_metadata.timeline"
                      @open-memory="openMemory"
                    />
                    <template v-else>
                      <div v-if="m.analysis_metadata.reasoning_text" class="think-lane">
                        <strong>模型推理</strong
                        ><span>{{ m.analysis_metadata.reasoning_text }}</span>
                      </div>
                      <div v-if="m.analysis_metadata.system_progress" class="think-lane">
                        <strong>系统进度</strong
                        ><span>{{ m.analysis_metadata.system_progress }}</span>
                      </div>
                      <div v-if="m.analysis_metadata.decision_notices?.length" class="think-lane">
                        <strong>决策摘要</strong
                        ><span v-for="(n, ni) in m.analysis_metadata.decision_notices" :key="ni">{{
                          n.summary
                        }}</span>
                      </div>
                      <div v-if="m.analysis_metadata.tool_events?.length" class="think-lane">
                        <strong>工具执行</strong
                        ><span v-for="(t, ti) in m.analysis_metadata.tool_events" :key="ti"
                          >{{ t.tool_name }}：{{
                            t.type === 'tool_result' ? (t.ok ? '已完成' : '未完成') : '执行中'
                          }}</span
                        >
                      </div>
                    </template>
                  </div>
                </div>
                <DiagramRenderer
                  v-for="(v, vi) in m.visuals || []"
                  :key="'hv' + vi"
                  :type="v.type"
                  :data="v.data"
                />
                <div class="content" v-html="render(cachedWebSrc(m.content).body, m.visuals, m)"></div>
                <div v-if="cachedWebSrc(m.content).count" class="think-panel think-panel-mt">
                  <div class="think-head" @click="m.srcOpen = !m.srcOpen">
                    <i class="ti ti-world"></i
                    ><span>联网来源（{{ cachedWebSrc(m.content).count }}）</span>
                    <i class="ti" :class="m.srcOpen ? 'ti-chevron-up' : 'ti-chevron-down'"></i>
                  </div>
                  <div
                    v-show="m.srcOpen"
                    class="content think-src-body"
                    v-html="render(cachedWebSrc(m.content).list, undefined, m)"
                  ></div>
                </div>
                <div v-if="m.citations && m.citations.length" class="muted think-panel-mt">
                  <i class="ti ti-quote"></i> 引用记忆：<span
                    v-for="(c, ci) in m.citations"
                    :key="c.id || ci"
                    class="cite-group"
                  >
                    <span class="cite-link" title="点击查看记忆详情" @click="openMemory(c.id)"
                      >[{{ ci + 1 }}] {{ c.title || c.id }}</span
                    >
                    <span class="cite-feedback">
                      <button
                        class="btn-xs"
                        title="这条记忆与本轮无关"
                        @click.stop="memoryFeedback(c, m, 'irrelevant')"
                      >
                        <i class="ti ti-link-off"></i> 不相关
                      </button>
                      <button
                        class="btn-xs"
                        title="这条记忆已过时"
                        @click.stop="memoryFeedback(c, m, 'stale')"
                      >
                        <i class="ti ti-clock-off"></i> 过时
                      </button>
                    </span>
                  </span>
                </div>
                <!-- 版本翻页器（AI 回复有多版本时显示） -->
                <div v-if="m.has_branches" class="version-nav">
                  <button
                    class="ver-btn"
                    :disabled="m.sibling_index === 0"
                    @click="switchVersion(m, -1)"
                  >
                    <i class="ti ti-chevron-left"></i>
                  </button>
                  <span class="ver-indicator"
                    >{{ m.sibling_index + 1 }} / {{ m.sibling_count }}</span
                  >
                  <button
                    class="ver-btn"
                    :disabled="m.sibling_index === m.sibling_count - 1"
                    @click="switchVersion(m, 1)"
                  >
                    <i class="ti ti-chevron-right"></i>
                  </button>
                </div>
                <div class="msg-actions msg-actions-mt">
                  <i
                    class="ti ti-thumb-up"
                    title="点赞"
                    :style="{ color: m.feedback === 1 ? 'var(--succtx)' : '' }"
                    @click="feedback(m, 1)"
                  ></i>
                  <i
                    class="ti ti-thumb-down"
                    title="点踩"
                    :style="{ color: m.feedback === 2 ? 'var(--dangtx)' : '' }"
                    @click="feedback(m, 2)"
                  ></i>
                  <i class="ti ti-copy" title="复制" @click="copyText(m)"></i>
                  <i
                    v-if="m.id && !generating"
                    class="ti ti-refresh"
                    title="重新生成"
                    @click="regenerate(m)"
                  ></i>
                  <span class="msg-time">{{ formatTimeFull(m.create_time) }}</span>
                </div>
              </div>
            </div>
          </div>

          <!-- 生成中：思考/工具阶段展开 → 正文首字起折叠；结束后历史消息默认折叠 -->
          <!-- 生成中：仅在发起请求的会话内展示（切会话后不泄漏到其他会话） -->
          <div
            v-if="(generating || streamText) && streamSid === currentSid"
            class="msg-ai"
          >
            <div class="avatar"><i class="ti ti-brain"></i></div>
            <div class="body">
              <div v-if="showLiveThinkPanel" class="think-panel">
                <div class="think-head" @click="thinkOpen = !thinkOpen">
                  <span class="think-section-toggle">
                    <i class="ti" :class="thinkOpen ? 'ti-chevron-down' : 'ti-chevron-right'"></i>
                  </span>
                  <span class="think-section-title">
                    {{ liveThinkSummary }}
                    <span v-if="showThinkLiveDots" class="think-live"
                      ><span class="think-dots"><span></span><span></span><span></span></span
                    ></span>
                  </span>
                </div>
                <div v-show="thinkOpen" ref="liveThink" class="think-body think-body-timeline">
                  <ThinkingTimeline
                    v-if="timeline.length"
                    :items="timeline"
                    :live="timelineLive"
                    @open-memory="openMemory"
                  />
                  <!-- 不含 timeline 的降级：老的分区结构 -->
                  <template v-else>
                    <div v-if="reasoningText" class="think-lane">
                      <strong>模型推理</strong><span>{{ reasoningText }}</span>
                    </div>
                    <div v-if="thinkText" class="think-lane">
                      <strong>系统进度</strong><span>{{ thinkText }}</span>
                    </div>
                    <div v-if="decisionNotices.length" class="think-lane">
                      <strong>决策摘要</strong
                      ><span v-for="(n, ni) in decisionNotices" :key="ni">{{ n.summary }}</span>
                    </div>
                  </template>
                </div>
              </div>
              <!-- 尚无任何进度事件：单一「处理中」占位（与上方面板互斥） -->
              <div v-if="showProcessingPlaceholder" class="content processing-row">
                <span class="muted">处理中</span>
                <span class="think-dots"><span></span><span></span><span></span></span>
              </div>
              <!-- 图形组件 -->
              <DiagramRenderer
                v-for="(v, vi) in streamVisuals"
                :key="'sv' + vi"
                :type="v.type"
                :data="v.data"
              />
              <div
                v-if="streamText"
                class="content streaming"
                v-html="render(streamWebSrc.body, streamVisuals)"
              ></div>
              <div v-if="streamText && streamWebSrc.count" class="think-panel think-src-mt">
                <div class="think-head" @click="streamSrcOpen = !streamSrcOpen">
                  <i class="ti ti-world"></i><span>联网来源（{{ streamWebSrc.count }}）</span>
                  <i class="ti" :class="streamSrcOpen ? 'ti-chevron-up' : 'ti-chevron-down'"></i>
                </div>
                <div
                  v-show="streamSrcOpen"
                  class="content think-src-pad"
                  v-html="render(streamWebSrc.list)"
                ></div>
              </div>
            </div>
          </div>
        </div>
        <!-- 回到最新（sticky：始终贴合滚动区底部，不受输入框高度影响） -->
        <button v-if="!atBottom" class="scroll-latest" title="回到最新" @click="scrollBottom">
          <i class="ti ti-arrow-down"></i>
        </button>
      </div>

      <!-- 输入区 -->
      <div class="composer-wrap">
        <div v-if="editingId" class="edit-composer-bar composer-max">
          <span><i class="ti ti-pencil"></i> 正在编辑消息</span>
          <button type="button" class="edit-cancel-btn" @click="cancelEdit">取消</button>
        </div>
        <!-- 95% 硬阈值提示条（会话上下文管理方案 v2） -->
        <div v-if="thresholdBreached === 'hard'" class="handoff-bar composer-max">
          <i class="ti ti-alert-triangle"></i>
          <span class="handoff-flex">此会话已达容量上限</span>
          <button class="btn-primary handoff-btn-sm" @click="startHandoff">
            <i class="ti ti-arrow-forward"></i> 开启新会话
          </button>
        </div>
        <!-- handoff 摘要附件（会话上下文管理方案 v2） -->
        <HandoffAttachment
          v-if="handoffStatus"
          :status="handoffStatus"
          :data="handoffData"
          class="chat-handoff-attach"
          @remove="removeHandoff"
          @preview="handoffPreview = handoffData || { status: handoffStatus }"
        />
        <div
          class="composer composer-inner"
          :class="{ dragover: dragOver }"
          @dragenter.prevent="dragOver = true"
          @dragover.prevent="dragOver = true"
          @dragleave.prevent="onDragLeave"
          @drop.prevent.stop="onDrop"
        >
          <!-- 附件条（胶囊可点击：粘贴文本/图片预览，其他格式看详情） -->
          <div v-if="attachments.length" class="attach-chips">
            <span
              v-for="(a, ai) in attachments"
              :key="ai"
              class="attach-chip attach-click"
              :class="{ 'attach-quote': a.kind === 'quote' }"
              :title="
                a.kind === 'quote'
                  ? '点击查看引用全文'
                  : a.pasted
                    ? '点击查看全文'
                    : a.isImage
                      ? '点击预览'
                      : '点击查看详情'
              "
              @click="openAttachment(a)"
            >
              <img
                v-if="a.isImage && a.preview"
                :src="a.preview"
                class="attach-thumb"
                loading="lazy"
                decoding="async"
                alt=""
              />
              <i
                v-else
                class="ti"
                :class="
                  a.uploading
                    ? 'ti-loader-2'
                    : a.error
                      ? 'ti-alert-triangle'
                      : a.kind === 'quote'
                        ? 'ti-quote'
                        : a.pasted
                          ? 'ti-clipboard-text'
                          : 'ti-paperclip'
                "
              ></i>
              {{ a.name }}
              <span v-if="a.uploading" class="muted">解析中…</span>
              <span v-else-if="a.error" class="dang">失败</span>
              <span v-else-if="a.isImage" class="muted">图片</span>
              <span v-else-if="!a.parsed" class="dang">无文本</span>
              <span v-else class="muted">{{ a.chars }} 字{{ a.truncated ? '·已截断' : '' }}</span>
              <span
                v-if="a.kind === 'quote' && a.comment"
                class="quote-comment-mark"
                title="包含用户评论"
                >·带评论</span
              >
              <span
                v-if="!a.isImage && !a.pasted && a.parsed"
                class="muted"
                title="发送后自动存入知识库"
                ><i class="ti ti-database"></i> 入库</span
              >
              <i class="ti ti-x cursor-pointer" @click.stop="removeAttachment(ai)"></i>
            </span>
          </div>
          <textarea
            ref="ta"
            v-model="input"
            :placeholder="
              thresholdBreached === 'hard'
                ? '已达容量上限，请开启新会话'
                : editingId
                  ? '编辑消息（Enter 提交修改，Esc 取消）'
                  : '发消息给 Second Person（Enter 发送，Shift+Enter 换行，可拖入/粘贴文件；项目会话内输入 @ 可插入文件）'
            "
            rows="1"
            :disabled="thresholdBreached === 'hard'"
            @input="onComposerInput"
            @keydown="onComposerKeyDown"
            @paste="onPaste"
            @dragenter.prevent="dragOver = true"
            @dragover.prevent="dragOver = true"
            @drop.prevent.stop="onDrop"
          ></textarea>
          <FilePickerPanel
            v-if="currentProject"
            ref="filePickerRef"
            :project-id="currentProject.id"
            :visible="filePickerVisible"
            :query="filePickerQuery"
            @pick="onFilePicked"
            @close="filePickerVisible = false"
          />
          <!-- 表情选择面板（absolute 定位在 composer 上方，选择后保持打开可连续插入） -->
          <div v-if="emojiOpen" class="emoji-panel" @click.stop>
            <div v-for="g in EMOJI_GROUPS" :key="g.name" class="emoji-group">
              <div class="emoji-group-name">{{ g.name }}</div>
              <div class="emoji-grid">
                <button
                  v-for="em in g.items"
                  :key="em"
                  type="button"
                  class="emoji-btn"
                  @click="insertEmoji(em)"
                >
                  {{ em }}
                </button>
              </div>
            </div>
          </div>
          <div class="row mt-24">
            <div class="fg fg-gap-8">
              <i class="ti ti-paperclip icon-muted-sm" title="上传文件" @click="triggerFile"></i>
              <!-- mousedown.prevent：防止按钮抢焦点导致 textarea 光标位置丢失 -->
              <i
                class="ti ti-mood-smile emoji-toggle icon-muted-sm"
                title="表情"
                @mousedown.prevent
                @click.stop="emojiOpen = !emojiOpen"
              ></i>
              <SandboxModeChip
                :session-id="currentSid"
                :has-project="!!currentProject"
                :fallback-mode="sandboxFallback"
                @pending-change="(m) => (pendingSandboxMode = m)"
              />
            </div>
            <div class="composer-actions">
              <input
                ref="fileInput"
                type="file"
                multiple
                class="hidden-input"
                @change="onFilePick"
              />
              <!-- 单一入口展示当前模型和推理等级；选择在上方两级菜单中完成。 -->
              <div class="model-control-wrap">
                <button
                  type="button"
                  class="model-control-btn"
                  :title="`模型：${selectedModelLabel}；推理等级：${reasoningEffortLabel}`"
                  :aria-expanded="modelControlOpen"
                  aria-haspopup="dialog"
                  @mousedown.prevent
                  @click.stop="toggleModelControl"
                >
                  <span class="model-control-name">{{ selectedModelLabel }}</span>
                  <span class="model-control-effort">{{ reasoningEffortCompactLabel }}</span>
                  <i class="ti" :class="modelControlOpen ? 'ti-chevron-up' : 'ti-chevron-down'"></i>
                </button>
                <div
                  v-if="modelControlOpen"
                  class="model-control-menu"
                  role="dialog"
                  aria-label="模型与推理等级"
                  @click.stop
                >
                  <template v-if="modelControlPanel === 'overview'">
                    <button
                      type="button"
                      class="model-control-row"
                      @click="openModelControlPanel('model')"
                    >
                      <span class="model-control-row-label">模型</span>
                      <span class="model-control-row-value">
                        <span class="model-control-row-text" :title="selectedModelLabel">{{
                          selectedModelLabel
                        }}</span>
                        <i class="ti ti-chevron-right"></i>
                      </span>
                    </button>
                    <button
                      type="button"
                      class="model-control-row"
                      @click="openModelControlPanel('reasoning')"
                    >
                      <span class="model-control-row-label">推理等级</span>
                      <span class="model-control-row-value">
                        <span class="model-control-row-text" :title="reasoningEffortCompactLabel">{{
                          reasoningEffortCompactLabel
                        }}</span>
                        <i class="ti ti-chevron-right"></i>
                      </span>
                    </button>
                  </template>
                  <template v-else-if="modelControlPanel === 'model'">
                    <button
                      type="button"
                      class="model-control-back"
                      @click="openModelControlPanel('overview')"
                    >
                      <i class="ti ti-chevron-left"></i> 模型
                    </button>
                    <button
                      v-for="provider in providers"
                      :key="provider.id"
                      type="button"
                      class="model-control-option"
                      :class="{ active: provider.id === chatModelId }"
                      role="menuitemradio"
                      :aria-checked="provider.id === chatModelId"
                      @click="pickChatModel(provider.id)"
                    >
                      <span>{{ provider.display_name }}</span>
                      <i v-if="provider.id === chatModelId" class="ti ti-check"></i>
                    </button>
                  </template>
                  <template v-else>
                    <button
                      type="button"
                      class="model-control-back"
                      @click="openModelControlPanel('overview')"
                    >
                      <i class="ti ti-chevron-left"></i> 推理等级
                    </button>
                    <button
                      v-for="opt in reasoningOptions"
                      :key="opt.value"
                      type="button"
                      class="model-control-option"
                      :class="{ active: reasoningEffort === opt.value }"
                      role="menuitemradio"
                      :aria-checked="reasoningEffort === opt.value"
                      @click="pickReasoningEffort(opt.value)"
                    >
                      <span>{{ opt.label }}</span>
                      <i v-if="reasoningEffort === opt.value" class="ti ti-check"></i>
                    </button>
                  </template>
                </div>
              </div>
              <button v-if="!generating" class="send-btn" @click="send">
                <i class="ti ti-arrow-up"></i>
              </button>
              <button v-else class="send-btn" @click="abort">
                <i class="ti ti-player-stop-filled"></i>
              </button>
            </div>
          </div>
        </div>
        <!-- 会话指标行：置于输入框正下方，与其居中对齐（不含轮/步） -->
        <SessionMetricsLine
          :metrics="sessionMetrics"
          :turn-metrics="currentTurnMetrics"
          :live-tokens-per-second="liveThroughput.tokensPerSecond.value"
        />
      </div>
      <!-- 空状态：底部 spacer 将 hero+composer 推离底端 -->
      <div v-if="!messages.length && !streamText" class="composer-spacer"></div>
    </div>
  </div>

  <!-- handoff 摘要预览：摘要正文由后端注入下一轮上下文，前端展示可用元信息 -->
  <BaseModal
    v-if="handoffPreview"
    title="上一会话摘要"
    size="sm"
    stacked
    @close="handoffPreview = null"
  >
    <dl class="kv">
      <dt>状态</dt>
      <dd>{{ handoffPreview.status === 'failed' ? '生成失败' : '已就绪' }}</dd>
      <dt v-if="handoffPreview.original_turns != null">原会话轮次</dt>
      <dd v-if="handoffPreview.original_turns != null">{{ handoffPreview.original_turns }}</dd>
      <dt v-if="handoffPreview.summary_tokens != null">摘要长度</dt>
      <dd v-if="handoffPreview.summary_tokens != null">
        约 {{ handoffPreview.summary_tokens }} token
      </dd>
    </dl>
    <p class="modal-subtitle">发送下一条消息时，系统会自动把该摘要注入新会话上下文。</p>
    <template #footer>
      <button type="button" @click="handoffPreview = null">关闭</button>
    </template>
  </BaseModal>

  <!-- 引用记忆详情弹窗（点击对话中的引用打开，二级层叠；SP-UI v4 统一走 BaseModal） -->
  <BaseModal v-if="memDetail" title="记忆详情" size="md" stacked @close="memDetail = null">
    <h3 class="modal-subtitle">{{ memDetail.frontmatter?.title || memDetail.id }}</h3>
    <div class="fg fg-gap-6 mb-10">
      <span v-if="memDetail.frontmatter?.confidence" class="badge badge-a">{{
        confidenceLabel(memDetail.frontmatter?.confidence)
      }}</span>
      <span v-if="memDetail.frontmatter?.lifecycle" class="badge">{{
        lifecycleLabel(memDetail.frontmatter?.lifecycle)
      }}</span>
      <span v-if="memDetail.access_count != null" class="muted"
        >被引用 {{ memDetail.access_count }} 次</span
      >
    </div>
    <div class="label">摘要</div>
    <p class="detail-summary mb-12">{{ memDetail.summary }}</p>
    <div v-if="memDetail.detail" class="label">详情</div>
    <p v-if="memDetail.detail" class="detail-body-scroll mb-12">{{ memDetail.detail }}</p>
    <div v-if="memDetail.governance" class="memory-provenance">
      <div class="label">记忆状态</div>
      <div class="muted">
        验证：{{ memDetail.governance.verification_state || '未验证' }} · 时效：{{
          memDetail.governance.freshness_state || '当前'
        }}
      </div>
      <div v-if="memDetail.evidence?.length" class="muted attach-meta">
        证据：{{
          memDetail.evidence[0].excerpt || memDetail.evidence[0].source_ref || '已记录来源'
        }}
      </div>
    </div>
    <template #footer>
      <button @click="memDetail = null">关闭</button>
    </template>
  </BaseModal>

  <!-- 附件查看弹窗：粘贴文本/图片应用内预览，其他格式信息+下载（二级层叠，统一走 BaseModal） -->
  <BaseModal
    v-if="attachView"
    :title="
      attachView.type === 'text'
        ? '文本内容'
        : attachView.type === 'image'
          ? '图片预览'
          : '附件详情'
    "
    :size="attachView.type === 'file' ? 'sm' : 'lg'"
    stacked
    @close="attachView = null"
  >
    <!-- 粘贴文本 / 引用：全文预览；引用带评论时追加评论段 -->
    <div v-if="attachView.type === 'text'">
      <div class="muted mb-10">{{ attachView.chars }} 字 · {{ attachView.lines }} 行</div>
      <div v-if="attachView.kind === 'quote'" class="label attach-label-mb">
        <i class="ti ti-quote"></i> 选中的文本
      </div>
      <div class="attach-code-block">
        {{ attachView.text }}
      </div>
      <div v-if="attachView.kind === 'quote' && attachView.comment" class="attach-section-mt">
        <div class="label attach-label-mb"><i class="ti ti-message-2"></i> 用户评论</div>
        <div class="attach-quote-block">
          {{ attachView.comment }}
        </div>
      </div>
    </div>
    <!-- 图片：应用内大图预览 -->
    <div v-else-if="attachView.type === 'image'" class="attach-img-wrap">
      <img
        :src="attachView.src"
        class="attach-img"
        loading="lazy"
        decoding="async"
        alt="附件预览"
      />
    </div>
    <!-- 其他格式：不做内容预览，只展示文件信息 + 下载 -->
    <div v-else>
      <div class="fg attach-file-head">
        <i class="ti ti-paperclip attach-file-icon"></i>
        <b class="attach-name-break">{{ attachView.name }}</b>
      </div>
      <div class="muted mb-6">格式：{{ attachExt(attachView.name) }}</div>
      <div v-if="attachView.size != null" class="muted mb-6">
        大小：{{ fmtSize(attachView.size) }}
      </div>
      <div v-if="attachView.chars" class="muted mb-6">解析字数：{{ attachView.chars }} 字</div>
      <div v-if="!attachView.file" class="muted mt-10">
        <i class="ti ti-database"></i> 原文件已存入知识库，可在 记忆中心 → 知识库 中查看
      </div>
    </div>
    <template #footer>
      <button
        v-if="attachView.type === 'file' && attachView.file"
        class="btn-primary"
        @click="downloadAttachFile(attachView.file)"
      >
        <i class="ti ti-download"></i> 下载
      </button>
      <button @click="attachView = null">关闭</button>
    </template>
  </BaseModal>

  <!-- 反馈原因弹窗（替代原生 prompt，统一走 BaseModal） -->
  <BaseModal
    v-if="fbDialog"
    :title="fbDialog.fb === 1 ? '哪些地方做得好？' : '哪里出了问题？'"
    size="sm"
    @close="fbDialog = null"
  >
    <div class="fb-col">
      <button
        v-for="opt in fbDialog.fb === 1 ? goodReasons : badReasons"
        :key="opt.value"
        class="fb-reason"
        :class="{ active: fbDialog.reason === opt.value }"
        @click="fbDialog.reason = opt.value"
      >
        {{ opt.label }}
      </button>
      <textarea
        v-if="fbDialog.reason === 'other'"
        v-model="fbDialog.custom"
        rows="3"
        placeholder="请描述你的反馈…"
        class="fb-textarea"
      ></textarea>
    </div>
    <template #footer>
      <button @click="fbDialog = null">取消</button>
      <button
        class="btn-primary"
        :disabled="!fbDialog.reason || (fbDialog.reason === 'other' && !fbDialog.custom.trim())"
        @click="submitFeedback"
      >
        提交
      </button>
    </template>
  </BaseModal>

  <!-- HTML 代码预览抽屉 -->
  <transition name="kg-drawer">
    <div v-if="htmlPreview" class="html-preview-drawer" :class="{ fullscreen: htmlFullscreen }">
      <div class="html-preview-head">
        <span class="html-preview-head">HTML 预览</span>
        <div class="fg fg-gap-6">
          <button
            class="mermaid-btn"
            :title="htmlFullscreen ? '退出全屏' : '全屏'"
            @click="htmlFullscreen = !htmlFullscreen"
          >
            <i class="ti" :class="htmlFullscreen ? 'ti-minimize' : 'ti-maximize'"></i>
            {{ htmlFullscreen ? '退出全屏' : '全屏' }}
          </button>
          <button
            class="mermaid-btn"
            title="关闭"
            @click="htmlPreview = null; htmlFullscreen = false"
          >
            <i class="ti ti-x"></i>
          </button>
        </div>
      </div>
      <iframe class="html-preview-iframe" :srcdoc="htmlPreview" sandbox=""></iframe>
    </div>
  </transition>
</template>

<style scoped>
.chat-project-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  background: var(--bg-input, rgba(127, 127, 127, 0.08));
  border: 1px solid var(--stroke);
  border-radius: 12px;
  font-size: 12px;
  color: var(--muted);
  align-self: flex-start;
  max-width: fit-content;
}
.chat-project-chip {
  padding: 4px 10px;
  background: var(--bg-input, rgba(127, 127, 127, 0.08));
  border: 1px solid var(--stroke);
  border-radius: 12px;
  font-size: 12px;
  color: var(--muted);
}
.chat-project-chip .ti-folder {
  color: var(--acctx);
}
.chat-project-chip .chip-title {
  color: var(--fg);
  font-weight: 500;
}
.chat-project-chip .chip-badge-miss {
  padding: 1px 6px;
  border-radius: 3px;
  background: var(--warntx-bg, rgba(200, 120, 0, 0.15));
  color: var(--warntx, #c87800);
  font-size: 10px;
}
</style>
