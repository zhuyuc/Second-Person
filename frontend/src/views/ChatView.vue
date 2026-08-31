<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { marked } from 'marked'
import mermaid from 'mermaid'
import { api } from '@/api/client'
import { useSSE } from '@/composables/useSSE'
import { useLiveThroughput } from '@/composables/useLiveThroughput'
import { useToast } from '@/stores/toast'
import { useSessions } from '@/stores/sessions'
import { useProjects } from '@/stores/projects'
import { projectsApi } from '@/api/projects'
import SandboxModeChip from '@/components/SandboxModeChip.vue'
import FilePickerPanel from '@/components/FilePickerPanel.vue'
import { resolveLocation, cachedLocation } from '@/composables/useGeolocation'
import DiagramRenderer from '@/components/diagram/DiagramRenderer.vue'
import BaseModal from '@/components/BaseModal.vue'
import HandoffAttachment from '@/components/HandoffAttachment.vue'
import MessageAnchorRail from '@/components/MessageAnchorRail.vue'
import SessionMetricsLine from '@/components/SessionMetricsLine.vue'
import SelectionActionBar from '@/components/SelectionActionBar.vue'
import QuoteComposer from '@/components/QuoteComposer.vue'
import ThinkingTimeline from '@/components/ThinkingTimeline.vue'
import { formatTimelineSummary } from '@/utils/timelineSummary'
import { useMessageSelection } from '@/composables/useMessageSelection'
import { applyMermaidTheme } from '@/utils/mermaidTheme'
import { svgToPngBlob } from '@/utils/svgExport'
import { formatRelative, formatTimeFull, fmtSize, nowLocalIso, friendlyError } from '@/utils/format'
import { confidenceLabel, lifecycleLabel, TOAST_ONLY_NOTIF } from '@/utils/enumLabel'
import { withQuery } from '@/utils/query'
import { sanitizeHtml } from '@/utils/sanitize'
import { enhanceResponseHtml } from '@/utils/responsePresentation'
import { bindInlineMermaidInteractions, cleanupInlineMermaidInteractions, resetInlineMermaid, zoomInlineMermaid } from '@/utils/inlineMermaidInteractions'
import { normalizeReasoningEffort } from '@/utils/chatContract'

// Mermaid 主题：CSS 变量驱动（与 MermaidChart 同源），自动跟随系统深浅色；手动触发 run
applyMermaidTheme()
// 自定义 marked 代码块渲染器：mermaid 语言块输出为 <div class="mermaid">，其余照常
const originalRenderer = new marked.Renderer()
const mermaidRenderer = new marked.Renderer()
mermaidRenderer.code = function (code, lang) {
  if (lang === 'mermaid' || (typeof code === 'object' && code.lang === 'mermaid')) {
    const src = typeof code === 'object' ? code.text : code
    const escaped = src.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;')
    return `<div class="mermaid-wrap" data-source="${escaped}"><div class="mermaid-actions"><button class="mermaid-btn mermaid-zoom-out" title="缩小图表" aria-label="缩小图表"><i class="ti ti-zoom-out"></i></button><button class="mermaid-btn mermaid-zoom-in" title="放大图表" aria-label="放大图表"><i class="ti ti-zoom-in"></i></button><button class="mermaid-btn mermaid-reset" title="重置图表" aria-label="重置图表"><i class="ti ti-refresh"></i></button><button class="mermaid-btn mermaid-copy-src" title="复制源码"><i class="ti ti-code"></i> 源码</button><button class="mermaid-btn mermaid-copy-img" title="复制图片"><i class="ti ti-photo"></i> 图片</button></div><div class="mermaid">${src}</div></div>`
  }
  // HTML 代码块：加“预览/下载/复制”按钮
  if (lang === 'html' || (typeof code === 'object' && code.lang === 'html')) {
    const src = typeof code === 'object' ? code.text : code
    const escaped = src.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;')
    return `<div class="html-code-wrap" data-source="${escaped}"><div class="mermaid-actions"><button class="mermaid-btn html-preview-btn" title="预览"><i class="ti ti-eye"></i> 预览</button><button class="mermaid-btn html-download-btn" title="下载"><i class="ti ti-download"></i> 下载</button><button class="mermaid-btn html-copy-btn" title="复制"><i class="ti ti-copy"></i> 复制</button></div><pre><code class="language-html">${escaped}</code></pre></div>`
  }
  // 其余语言代码块：统一加顶部操作条（语言标签 + 复制按钮，样式同 HTML 块）
  {
    const src = typeof code === 'object' ? (code.text ?? '') : String(code ?? '')
    const language = ((typeof code === 'object' ? code.lang : lang) || 'text').split(/\s/)[0]
    const escaped = src.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;')
    return `<div class="code-wrap" data-source="${escaped}"><div class="mermaid-actions"><span class="code-lang">${language}</span><button class="mermaid-btn code-copy-btn" title="复制代码"><i class="ti ti-copy"></i> 复制</button></div><pre><code class="language-${language}">${escaped}</code></pre></div>`
  }
}
// 文件下载卡片：generate_document 产物链接（/api/files/…）渲染为下载卡片
mermaidRenderer.link = function (href, title, text) {
  let h = href, t = text
  if (typeof href === 'object' && href) { h = href.href; t = href.text || t }
  if (typeof h === 'string' && h.startsWith('/api/files/')) {
    const ext = (h.split('.').pop() || '').toLowerCase()
    const icon = ext === 'docx' ? 'ti-file-word' : ext === 'md' ? 'ti-markdown' : 'ti-file-download'
    const name = String(t || '文件').replace(/&/g, '&amp;').replace(/</g, '&lt;')
    return `<a class="file-card" href="${h}" download><i class="ti ${icon} file-card-icon"></i><span class="file-card-name">${name}</span><span class="file-card-dl"><i class="ti ti-download"></i> 下载</span></a>`
  }
  return originalRenderer.link.call(this, href, title, text)
}
marked.setOptions({ renderer: mermaidRenderer })

const toast = useToast()
const sse = useSSE()
const sessStore = useSessions()   // 会话列表/当前会话共享状态（侧栏在 SessionSidebar）
const projStore = useProjects()   // 项目工作区（v5）

const currentProject = computed(() => {
  const sid = sessStore.currentSid
  if (sid) {
    const s = sessStore.list.find(x => x.session_id === sid)
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
const sandboxFallback = computed(() =>
  pendingSandboxMode.value || currentProject.value?.sandbox_mode || 'workspace-write')

// M4：@文件面板
const filePickerVisible = ref(false)
const filePickerQuery = ref('')
const filePickerRef = ref(null)

function onComposerInput(e) {
  // 复用原有 autoGrow；这里叠加 @ 触发面板逻辑
  autoGrow()
  const ta = e?.target || document.querySelector('textarea')
  if (!ta || !currentProject.value) { filePickerVisible.value = false; return }
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
  if (e.key === 'Enter' && !e.shiftKey && !filePickerVisible.value) {
    e.preventDefault()
    send()
  }
}
// 消息气泡文字选中 → 悬浮 toolbar（复制/引用）
const selection = useMessageSelection()
const messages = ref([])
const input = ref('')
const generating = ref(false)
const streamText = ref('')
const thinkText = ref('')     // 安全分析摘要与进度，不展示模型原生推理
const reasoningText = ref('') // Provider 明确返回的 reasoning block
const decisionNotices = ref([])
const toolEvents = ref([])
// v7 时间线（交错 reasoning + tool_call，按到达顺序合并/就地更新）
const timeline = ref([])
const thinkOpen = ref(true)   // 处理进度面板：默认展开，用户可手动收起
const preBodyPhase = ref(true) // 正文未开始前为 true；首字写入后立即 false，停止思考动画

// 流式区「处理进度」与「处理中」占位互斥：timeline 有项时只展示面板，避免双重点状动画
const liveHasThinkContent = computed(() =>
  thinkText.value || reasoningText.value ||
  decisionNotices.value.length > 0 || toolEvents.value.length > 0 ||
  timeline.value.length > 0
)
const showLiveThinkPanel = computed(() => liveHasThinkContent.value)
const showProcessingPlaceholder = computed(() =>
  generating.value && !streamText.value && !liveHasThinkContent.value
)
// 正文已开始：思考时间线进入「已完成」展示，不再显示进行中动画
const timelineLive = computed(() => generating.value && preBodyPhase.value)
const awaitingModel = computed(() =>
  timeline.value.some(it => it.kind === 'step_wait' && it.status === 'running')
)
const showThinkLiveDots = computed(() =>
  showLiveThinkPanel.value && (timelineLive.value || awaitingModel.value)
)
const liveThinkSummary = computed(() => formatTimelineSummary(timeline.value))
const streamSrcOpen = ref(false)  // 流式回复的联网来源面板：默认收起
const streamSid = ref(null)   // 流式回复所属会话：切换会话后不再渲染/插入到其他会话
const streamVisuals = ref([])  // 本轮 tool 产出图形 [{type, data}]
const degraded = ref(false)
const sessionMetrics = ref(null)
const currentTurnMetrics = ref(null)
// 实时 tok/s：deepseek-harness 的 sessionStats 只在步边界刷新，这里 chunk 级估算
const liveThroughput = useLiveThroughput()
const scroller = ref(null)
const ta = ref(null)          // 输入框，用于自适应高度

// 当前会话的消息定位轨道：只为用户消息建立锚点，避免 AI 回复重复占位。
const messageElements = new Map()
const messageElementKeys = new WeakMap()
let localMessageKeySeq = 0

const thresholdBreached = ref(null)  // null / 'soft' / 'hard'
const softToastShown = ref(false)
// handoff 附件状态
const handoffStatus = ref(null)      // null / 'generating' / 'ready' / 'failed'
const handoffData = ref(null)        // { summary_tokens, original_turns }
const handoffPreview = ref(null)
const pendingMessage = ref(null)     // 摘要生成中暂存的消息

// ---- handoff 操作 ----
async function startHandoff() {
  if (!sessStore.currentSid) return
  try {
    const d = await api.post('/chat/session/handoff', {
      from_session_id: sessStore.currentSid
    })
    sessStore.setCurrent(d.new_session_id)
    messages.value = []
    sessionMetrics.value = null
    currentTurnMetrics.value = null
    handoffStatus.value = 'generating'
    handoffData.value = null
    thresholdBreached.value = null
  } catch { toast.push('error', '创建新会话失败') }
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
const selectedModelLabel = computed(() =>
  providers.value.find(p => p.id === chatModelId.value)?.display_name || '未配置模型')
async function loadProviders() {
  const all = await api.get('/settings/providers')
  const a = await api.get('/settings/model-assignment')
  // 隐藏 embedding 专用模型（如本地 BGE-M3），它不能用于对话
  const embId = a.embedding_model?.provider_id
  providers.value = all.filter(p => p.id !== embId)
  // 若当前 chat 分配指向被隐藏的模型，回退到首个可用模型
  const cur = a.chat_model?.provider_id
  chatModelId.value = providers.value.some(p => p.id === cur) ? cur : (providers.value[0]?.id ?? null)
  await loadModelCapabilities()
}
async function loadModelCapabilities() {
  try {
    // Keep the selector compatible with an already-running older backend:
    // this endpoint predates the richer model-capabilities projection and is
    // now backed by the same provider capability catalog.
    const result = await api.get('/chat/reasoning-efforts')
    const values = Array.isArray(result)
      ? result.map(item => item.value).filter(Boolean)
      : (result?.reasoning_efforts || [])
    reasoningOptions.value = values.length
      ? REASONING_EFFORT_OPTIONS.filter(item => values.includes(item.value))
      : [...REASONING_EFFORT_OPTIONS]
    if (!reasoningOptions.value.some(item => item.value === reasoningEffort.value)) {
      reasoningEffort.value = reasoningOptions.value[0]?.value || 'off'
    }
  } catch { reasoningOptions.value = [...REASONING_EFFORT_OPTIONS] }
}
async function switchModel(pid) {
  chatModelId.value = pid
  // 仅切换对话模型，不动 agent 模型分配（设置页的精细分配不被覆盖）
  await api.put('/settings/model-assignment', { chat_model: pid })
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
const reasoningEffortLabel = computed(() =>
  (reasoningOptions.value.find(m => m.value === reasoningEffort.value)
    || reasoningOptions.value[0] || REASONING_EFFORT_OPTIONS[2]).label)
const reasoningEffortCompactLabel = computed(() => ({
  off: 'Off', low: 'Low', high: 'High', max: 'Max',
}[reasoningEffort.value] || 'High'))

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

// 仅提示类系统通知：Web 端已在导入时用 toast 实时反馈，无需在对话流中留存横幅（含历史）
function stripToastNotifs(msgs) {
  return msgs.filter(m => !(m.message_type === 'system_notification'
    && TOAST_ONLY_NOTIF.includes(m.notification_type)))
}

function messageThinkExpanded(m) {
  return m.thinkOpen !== false
}
function toggleMessageThink(m) {
  m.thinkOpen = !messageThinkExpanded(m)
}

function findMessageIndexById(id) {
  if (id == null) return -1
  const key = String(id)
  return messages.value.findIndex(m => m.id != null && String(m.id) === key)
}

/** 编辑提交：从被编辑的用户消息起截断后续 UI（含旧 AI 回复） */
function trimMessagesFromEdit(editMsgId) {
  const idx = findMessageIndexById(editMsgId)
  if (idx === -1) return false
  messages.value = messages.value.slice(0, idx)
  return true
}

async function openSession(sid, opts = {}) {
  sessStore.setCurrent(sid)
  const [msgs, metrics] = await Promise.all([
    api.get(withQuery('/chat/messages', { session_id: sid })),
    api.get(`/chat/session/${sid}/metrics`).catch(() => null),
  ])
  // 历史消息：若用户消息含附件上下文前缀，只展示真实提问 + 附件胶囊
  for (const m of msgs) {
    if (m.role === 'user' && typeof m.content === 'string'
      && m.content.includes('\n---\n')
      && (m.content.includes('【附件：') || m.content.includes('【选中的文本】'))) {
      m.atts = extractAttachments(m.content)
      m.content = m.content.split('\n---\n').pop()
    }
  }
  messages.value = stripToastNotifs(msgs)
  sessionMetrics.value = metrics
  currentTurnMetrics.value = metrics?.current_turn || null
  if (opts.messageId) scrollToMessage(opts.messageId)
  else scrollBottom()
  tryReattach(sid)
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
    const d = await api.get(`/chat/session/${sid}/active-request`)
    const crid = d?.client_request_id
    if (!crid || sessStore.currentSid !== sid) return
    generating.value = true
    streamSid.value = sid
    currentTurnMetrics.value = null
    liveThroughput.reset()
    streamText.value = ''
    thinkText.value = ''
    reasoningText.value = ''
    decisionNotices.value = []
    toolEvents.value = []
    timeline.value = []
    preBodyPhase.value = true
    thinkOpen.value = true
    // 重挂后删掉尾部尚未完成的那轮用户消息渲染冗余风险低：回放事件仅重建流式区
    await sse.send({
      sessionId: sid, message: '', clientRequestId: crid,
      onEvent: (ev, data) => handleEvent(ev, data),
      onError: (e) => { toast.push('error', friendlyError(e?.message)); finishStream() },
    })
    // 兜底：重挂流异常断开（无终止事件）时同样保留已输出内容
    if (generating.value) finishStream()
  } catch { /* 无进行中请求或接口异常：静默跳过 */ }
}
// 从历史消息的附件上下文前缀中还原各附件的名称与正文
// 支持两种格式：老 【附件：xxx】\n{text} 与新 【选中的文本】\n{quote}[\n\n【用户评论】\n{comment}]
// 后者是引用附件专用，可选携带评论；前者其它附件继续沿用。
function extractAttachments(content) {
  const head = content.split('\n---\n').slice(0, -1).join('\n---\n')
  if (!head) return []
  // 同时匹配"【附件：xxx】"和"【选中的文本】"两种起始标签
  const re = /【附件：([^】]+?)(?:（内容已截断）)?】|【选中的文本】/g
  const found = []
  let m, prev = null
  while ((m = re.exec(head))) {
    if (prev) prev.body = head.slice(prev.end, m.index).replace(/\n+$/, '').replace(/^\n+/, '')
    prev = { isQuote: !m[1], name: m[1] || '引用', end: re.lastIndex }
    found.push(prev)
  }
  if (prev) prev.body = head.slice(prev.end).replace(/\n+$/, '').replace(/^\n+/, '')
  return found.map(a => {
    if (a.isQuote) {
      // 引用块可能内嵌一段 【用户评论】 子段；胶囊显示统一叫"引用"
      const body = a.body || ''
      const idx = body.indexOf('\n\n【用户评论】\n')
      const text = idx >= 0 ? body.slice(0, idx) : body
      const comment = idx >= 0 ? body.slice(idx + '\n\n【用户评论】\n'.length) : ''
      return { name: '引用', pasted: true, kind: 'quote',
               text, comment, chars: text.length }
    }
    return {
      name: a.name, pasted: /^粘贴的文本/.test(a.name),
      text: a.body || '', chars: (a.body || '').length
    }
  })
}

// 附件上传（拖拽 / 点击选择，解析常见格式）
const attachments = ref([])   // { name, chars, text, truncated, parsed, uploading, error }
const dragOver = ref(false)
const fileInput = ref(null)
function triggerFile() { fileInput.value && fileInput.value.click() }
function onFilePick(e) { uploadFiles(e.target.files); e.target.value = '' }
function onDragLeave(e) {
  // 仅当离开整个 composer（而非移到子元素）才取消高亮
  if (!e.currentTarget.contains(e.relatedTarget)) dragOver.value = false
}
function onDrop(e) {
  dragOver.value = false
  const dt = e.dataTransfer
  if (!dt) return
  let files = dt.files && dt.files.length ? Array.from(dt.files) : []
  if (!files.length && dt.items) {
    files = Array.from(dt.items)
      .filter(it => it.kind === 'file')
      .map(it => it.getAsFile())
      .filter(Boolean)
  }
  if (files.length) uploadFiles(files)
}
async function uploadFiles(fileList) {
  const MAX = 5
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
      attachments.value.push({ name: f.name, uploading: false, isImage: true, preview, dataUrl })
      continue
    }
    const item = { name: f.name, uploading: true, isImage: false }
    const idx = attachments.value.push(item) - 1
    try {
      const fd = new FormData(); fd.append('file', f)
      const d = await api.upload('/chat/attachment', fd)
      attachments.value[idx] = {
        name: d.filename, chars: d.chars, text: d.text,
        truncated: d.truncated, parsed: d.parsed, uploading: false, isImage: false,
        file: f
      }
      if (!d.parsed) toast.push('warning', `「${d.filename}」未能解析出文本内容`)
    } catch {
      attachments.value[idx] = { name: f.name, uploading: false, error: true, isImage: false }
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
  if (a && a.isImage && a.preview) URL.revokeObjectURL(a.preview)
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
const pendingQuote = ref({ visible: false, text: '', sourceMsgId: null, sourceRole: null })
function quoteSelection() {
  const t = selection.text.value
  if (!t) return
  const MAX = 5
  if (attachments.value.length >= MAX) {
    toast.push('warning', `最多 ${MAX} 个附件`)
    selection.hide()
    return
  }
  // 打开评论录入弹窗；确认后再落到 attachments。这里先把原文与来源
  // 暂存下来，收起 SelectionActionBar 并清空浏览器选区，避免 toolbar 复现
  pendingQuote.value = {
    visible: true, text: t,
    sourceMsgId: selection.sourceMsgId.value,
    sourceRole: selection.sourceRole.value,
  }
  selection.hide()
  window.getSelection?.()?.removeAllRanges?.()
}
function cancelQuoteComposer() {
  pendingQuote.value = { visible: false, text: '', sourceMsgId: null, sourceRole: null }
}
function commitQuoteAttachment({ comment }) {
  const q = pendingQuote.value
  const t = q.text
  if (!t) { cancelQuoteComposer(); return }
  const MAX = 5
  if (attachments.value.length >= MAX) {
    toast.push('warning', `最多 ${MAX} 个附件`)
    cancelQuoteComposer()
    return
  }
  // 与"粘贴的文本"共用同一通道：同一形状、同一附件面板、同一历史还原路径。
  // kind:'quote' 用于渲染层切图标/胶囊底色；comment 可选，弹窗留空即为 ''。
  // 胶囊显示统一叫"引用"，弹窗内用"选中的文本"段头呼应发送到模型的 【选中的文本】。
  const n = attachments.value.filter(a => a.kind === 'quote').length
  attachments.value.push({
    name: n ? `引用 ${n + 1}` : '引用',
    pasted: true, kind: 'quote', parsed: true, isImage: false, uploading: false,
    text: t, comment: comment || '',
    chars: t.length, lines: t.split('\n').length,
    sourceMsgId: q.sourceMsgId, sourceRole: q.sourceRole,
  })
  cancelQuoteComposer()
  nextTick(() => { ta.value?.focus() })
}

// ---- 表情选择器：点击表情插入输入框光标处（支持连续插入，点击外部关闭） ----
const emojiOpen = ref(false)
const EMOJI_GROUPS = [
  { name: '表情', items: ['😀', '😄', '😁', '😂', '🤣', '😊', '😇', '🙂', '😉', '😍', '😘', '😜', '🤪', '🤔', '🤨', '😐', '😏', '😒', '🙄', '😬', '😮', '😲', '🥱', '😴', '🤤', '😵', '🤯', '🥳', '😎', '🤓', '🧐', '😢', '😭', '😤', '😠', '😡', '🤬', '😱', '😨', '😰', '😥', '😓', '🤗', '🤭', '🤫', '🥺', '🫡'] },
  { name: '手势', items: ['👍', '👎', '👌', '✌️', '🤞', '🤟', '🤘', '🤙', '👈', '👉', '👆', '👇', '☝️', '✋', '🤚', '🖖', '👋', '🤏', '💪', '🙏', '👏', '🤝', '✊', '👊', '🤛', '🤜', '👐', '🤲'] },
  { name: '爱心', items: ['❤️', '🧡', '💛', '💚', '💙', '💜', '🖤', '🤍', '🤎', '💔', '❣️', '💕', '💞', '💓', '💗', '💖', '💘', '💝', '💟', '💯'] },
  { name: '动物', items: ['🐶', '🐱', '🐭', '🐹', '🐰', '🦊', '🐻', '🐼', '🐨', '🐯', '🦁', '🐮', '🐷', '🐸', '🐵', '🙈', '🙉', '🙊', '🐔', '🐧', '🐦', '🦆', '🦉', '🐺', '🐴', '🦄', '🐝', '🦋', '🐞', '🐢', '🐍', '🐙', '🐠', '🐟', '🐬', '🐳', '🦈'] },
  { name: '食物', items: ['🍎', '🍊', '🍋', '🍌', '🍉', '🍇', '🍓', '🍒', '🍑', '🥭', '🍍', '🥥', '🍅', '🥑', '🥦', '🌽', '🥕', '🍞', '🥐', '🧀', '🥚', '🍳', '🥓', '🍔', '🍟', '🍕', '🌭', '🌮', '🥗', '🍿', '🍜', '🍣', '🍤', '🍦', '🍩', '🍪', '🎂', '🍰', '🧁', '🍫', '🍬', '🍭', '☕', '🍵', '🍺', '🥂', '🍷'] },
  { name: '活动', items: ['⚽', '🏀', '🏈', '⚾', '🎾', '🏐', '🎱', '🏓', '🏸', '⛳', '🏹', '🎣', '🥊', '🎿', '🏂', '🏋️', '🤸', '🏄', '🏊', '🚴', '🚵', '🎯', '🎳', '🎲', '🎮', '♟️', '🎭', '🎨', '🎬', '🎤', '🎧', '🎹', '🥁', '🎷', '🎺', '🎸', '🎻'] },
  { name: '物品', items: ['⌚', '📱', '💻', '⌨️', '🖥️', '🖱️', '🕹️', '💿', '📷', '📸', '📹', '📺', '📻', '⏰', '⌛', '⏳', '📡', '🔋', '💡', '🔦', '🕯️', '💸', '💰', '💳', '💎', '🧰', '🔧', '🔨', '🛠️', '⚙️', '🔪', '🛡️', '🔮', '🔭', '🔬', '💊', '💉', '🧬', '🦠', '🧪', '🧹', '🧻', '🛁', '🧼', '🔑', '🗝️', '🚪', '🪑', '🛋️', '🛏️', '🧸', '🖼️', '🛒', '🎁', '🎈', '🎀', '🎊', '🎉', '📦', '📝', '📁', '📂', '📚', '📖', '✏️', '🖊️', '✂️', '🔍', '📌', '📍', '🗑️', '♻️'] },
  { name: '符号', items: ['✅', '❌', '❓', '💢', '💥', '💫', '💦', '💨', '💬', '💭', '♨️', '🔔', '🔕', '🎵', '🎶', '📢', '📣', '🔊', '🔇', '⭕', '🔴', '🟠', '🟡', '🟢', '🔵', '🟣', '⚫', '⚪', '🔺', '🔻', '🔸', '🔹', '🏁', '🚩', '🎌'] },
]
function insertEmoji(em) {
  const el = ta.value
  const pos = el && el.selectionStart != null ? el.selectionStart : input.value.length
  const end = el && el.selectionEnd != null ? el.selectionEnd : pos
  input.value = input.value.slice(0, pos) + em + input.value.slice(end)
  nextTick(() => {
    const p = pos + em.length
    if (el) { el.focus(); el.setSelectionRange(p, p) }
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
const attachView = ref(null)   // { type: 'text'|'image'|'file', ... }
// composer 附件胶囊点击
function openAttachment(a) {
  if (a.uploading || a.error) return
  if (a.pasted) {
    attachView.value = { type: 'text', name: a.name, text: a.text, chars: a.chars,
                         lines: a.lines, kind: a.kind, comment: a.comment }
  } else if (a.isImage) {
    attachView.value = { type: 'image', name: a.name, src: a.preview }
  } else {
    attachView.value = { type: 'file', name: a.name, file: a.file || null, size: a.file ? a.file.size : null, chars: a.chars }
  }
}
// 消息气泡附件胶囊点击（含历史会话还原的附件）
function openMsgAttachment(att) {
  if (att.pasted) {
    const text = att.text || ''
    attachView.value = { type: 'text', name: att.name, text,
                         chars: text.length, lines: text.split('\n').length,
                         kind: att.kind, comment: att.comment }
  } else {
    attachView.value = { type: 'file', name: att.name, file: att.file || null, size: att.file ? att.file.size : null, chars: att.chars }
  }
}
function openBubbleImage(src) { attachView.value = { type: 'image', name: '图片', src } }
function downloadAttachFile(file) {
  const url = URL.createObjectURL(file)
  const el = document.createElement('a')
  el.href = url; el.download = file.name; el.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
function attachExt(name) {
  return name && name.includes('.') ? name.split('.').pop().toUpperCase() : '未知'
}
// 文档附件统一存入知识库：发送时后台异步导入，不阻塞对话
async function ingestToKb(file) {
  try {
    const fd = new FormData(); fd.append('file', file)
    const r = await api.upload('/import/document', fd)
    if (r.duplicate) {
      // 文档已在知识库中：跳过重复导入，不影响当前对话的文档解析
      toast.push('info', `「${file.name}」已在知识库中，跳过重复导入（已有 ${r.extracted} 条记忆）`)
    } else {
      toast.push('success', `「${file.name}」已存入知识库，提炼 ${r.extracted} 条记忆`)
    }
  } catch { /* api 层已提示错误 */ }
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
  if (files.length) { e.preventDefault(); uploadFiles(files); return }
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
  const n = attachments.value.filter(a => a.pasted).length
  attachments.value.push({
    name: n ? `粘贴的文本 ${n + 1}` : '粘贴的文本',
    pasted: true, parsed: true, isImage: false, uploading: false,
    text, chars: text.length, lines: text.split('\n').length
  })
}

async function send() {
  const text = input.value.trim()
  const atts = attachments.value.filter(a => a.parsed && a.text)
  const imgs = attachments.value.filter(a => a.isImage && a.dataUrl).map(a => a.dataUrl)
  const kbFiles = attachments.value.filter(a => a.file && !a.isImage).map(a => a.file)
  if ((!text && !atts.length && !imgs.length) || generating.value) return
  // handoff 摘要生成中：消息暂存（会话上下文管理方案 v2）
  if (handoffStatus.value === 'generating') {
    pendingMessage.value = { text, atts: attachments.value }
    return
  }
  // 无当前会话（新对话/欢迎页）：新建一条全新会话，不复用旧空会话，
  // 避免消息落进以前的会话记录
  if (!sessStore.currentSid) {
    // M5.1：若来自「+ 新建会话」项目挂载，此时 pendingProjectId 有值
    const pendingPid = sessStore.pendingProjectId
    const body = pendingPid ? { project_id: pendingPid } : {}
    const d = await api.post('/chat/session/create', body)
    // 占位挂到正确的项目下，避免侧栏先出现在「最近」再跳到工作区
    sessStore.ensurePlaceholder(d.session_id, pendingPid)
    if (pendingSandboxMode.value) {
      try { await projectsApi.setSandboxMode(d.session_id, pendingSandboxMode.value) } catch { /* toast 已弹 */ }
      pendingSandboxMode.value = null
    }
    sessStore.setCurrent(d.session_id)  // 内部会自动清空 pendingProjectId
    sessStore.scheduleTitleRefresh(d.session_id)
    sessStore.load()  // 后台同步真实数据
    messages.value = []
  }
  // handoff 附件路径：新会话首条消息携带
  let hPath = null
  if (handoffStatus.value === 'ready' && messages.value.length === 0) {
    hPath = `artifacts/handoffs/${sessStore.currentSid}.md`
  }
  // 构造发送给后端的消息：把附件解析文本作为上下文前置（不截断，完整交给模型）
  // 引用附件（kind:'quote'）走 【选中的文本】\n{原文} + 可选 \n\n【用户评论】\n{评论}
  // 双标签，让模型清楚地区分"被引用的原文"和"用户对这段的评论"。
  // 其它附件（粘贴/文档）继续 【附件：xxx】 老格式；主输入文字仍用 \n---\n 尾部分隔。
  let backendMsg = text
  if (atts.length) {
    const blocks = atts.map(a => {
      if (a.kind === 'quote') {
        const base = `【选中的文本】\n${a.text || ''}`
        return a.comment ? `${base}\n\n【用户评论】\n${a.comment}` : base
      }
      return `【附件：${a.name}】\n${a.text || ''}`
    }).join('\n\n')
    backendMsg = blocks + '\n\n---\n' + (text || '请阅读上述附件内容并回应。')
  }
  if (!backendMsg && imgs.length) backendMsg = '请看图并回应。'
  // 气泡附件：保留粘贴全文与原始 File，供发送后点击弹窗回看/下载
  // kind 保留后气泡胶囊可以按引用/粘贴/文件切换图标与底色；comment 用于胶囊"·带评论"标记
  const bubbleAtts = attachments.value.filter(a => !a.isImage).map(a => ({
    name: a.name, pasted: !!a.pasted, kind: a.kind, comment: a.comment,
    text: a.pasted ? a.text : undefined, file: a.file, chars: a.chars
  }))
  const bubbleImages = attachments.value.filter(a => a.isImage && a.preview).map(a => a.preview)
  // 已随气泡送出的图片 preview（blob URL）不能在清空附件时 revoke，
  // 否则消息气泡中的图片立即失效，需等刷新后由后端历史 URL 才恢复
  const sentPreviews = new Set(bubbleImages)
  messages.value.push({
    role: 'user', content: text || (imgs.length ? '' : '（已上传附件）'),
    atts: bubbleAtts, images: bubbleImages,
    create_time: nowLocalIso()
  })
  input.value = ''
  clearAttachments({ keepPreviews: sentPreviews })
  // 文档附件统一存入知识库：后台异步导入，不阻塞本次对话
  kbFiles.forEach(f => ingestToKb(f))
  nextTick(autoGrow)
  generating.value = true
  streamSid.value = sessStore.currentSid
  currentTurnMetrics.value = null
  liveThroughput.reset()
  streamText.value = ''
  thinkText.value = ''
  thinkOpen.value = true
  preBodyPhase.value = true
  scrollBottom()

  await sse.send({
    sessionId: sessStore.currentSid,
    // M5.1：无 sessionId + pendingProjectId 时，后端会带项目建库
    projectId: sessStore.currentSid ? undefined : sessStore.pendingProjectId,
    message: backendMsg,
    images: imgs.length ? imgs : undefined,
    location: geoEnabled.value ? cachedLocation() : undefined,
    handoffPath: hPath,
    reasoningEffort: reasoningEffort.value,
    onEvent: (ev, data) => handleEvent(ev, data),
    onError: (e) => { toast.push('error', friendlyError(e?.message)); finishStream() },
  })
  // 兜底：始终未收到 turn_completed/error（服务重启等异常断开）时，
  // 同样保留已输出内容并释放输入锁，避免 UI 卡在生成中
  if (generating.value) finishStream()
  // 发送后清除 handoff 附件状态
  if (hPath) { handoffStatus.value = null; handoffData.value = null }
}

// ---- 流式正文渲染节流（P0-3）----
// 原实现每个 content_delta 都同步改写 streamText，触发 v-html 全量重渲染
// （stripTail → marked.parse → groupSections → sanitize），回复越长单次渲染成本
// 越高（整体 O(N²)），长回复中途明显卡顿。改为 chunk 先入缓冲、requestAnimationFrame
// 合并刷新：每帧至多一次重渲染（浏览器绘制帧率上限即 60fps，更频繁写入纯属浪费）。
let streamChunkBuf = ''
let streamRaf = 0
function flushStreamText() {
  if (streamRaf) { cancelAnimationFrame(streamRaf); streamRaf = 0 }
  if (!streamChunkBuf) return
  streamText.value += streamChunkBuf
  streamChunkBuf = ''
  maybeScroll()
  scrollStreamCode()
}
function finalizeStaleTimelineSteps() {
  for (const it of timeline.value) {
    if (it.kind === 'memory_stage' && it.status === 'running') it.status = 'ok'
    if (it.kind === 'tool_call' && it.status === 'running') it.status = 'ok'
    if (it.kind === 'step_wait') it.status = 'ok'
  }
}
function clearStepWait() {
  if (timeline.value.some(it => it.kind === 'step_wait')) {
    timeline.value = timeline.value.filter(it => it.kind !== 'step_wait')
  }
}
function upsertStepWait(step, label, detail) {
  const last = timeline.value[timeline.value.length - 1]
  if (last?.kind === 'step_wait' && last.status === 'running') {
    if (step) last.step = step
    if (label) last.label = label
    if (detail) last.detail = detail
    return
  }
  clearStepWait()
  timeline.value.push({
    kind: 'step_wait',
    step: step || 0,
    status: 'running',
    label: label || '准备下一步',
    detail: detail || '',
  })
}
function pushStreamText(text) {
  if (!text) return
  if (preBodyPhase.value) {
    preBodyPhase.value = false
    thinkOpen.value = false
    finalizeStaleTimelineSteps()
  }
  streamChunkBuf += text
  if (!streamRaf) streamRaf = requestAnimationFrame(flushStreamText)
}

// ---- 工具步旁白按步缓冲（对齐 deepseek-harness）----
// 正文增量先进 pendingBody 缓冲：若本步是工具步（随后收到 content_reset），
// 缓冲整体转入思考面板（旁白不进正文）；否则等待 BODY_COMMIT_MS 无 reset 后
// 确认为最终答案后立即进正文；工具步旁白由 content_reset + retractToolStepBody 撤回。
let pendingBody = ''
let bodyCommitTimer = 0
let bodyCommitted = false
function appendTimelineNarration(text) {
  if (!text) return
  const last = timeline.value[timeline.value.length - 1]
  if (last && last.kind === 'narration') {
    last.text = (last.text || '') + text
  } else {
    timeline.value.push({ kind: 'narration', text })
  }
}
function commitPendingToBody() {
  if (bodyCommitTimer) { clearTimeout(bodyCommitTimer); bodyCommitTimer = 0 }
  if (pendingBody) { pushStreamText(pendingBody); pendingBody = '' }
  bodyCommitted = true
}
function retractToolStepBody() {
  if (bodyCommitTimer) { clearTimeout(bodyCommitTimer); bodyCommitTimer = 0 }
  if (streamRaf) { cancelAnimationFrame(streamRaf); streamRaf = 0 }
  const narration = pendingBody + streamText.value + streamChunkBuf
  pendingBody = ''
  streamText.value = ''
  streamChunkBuf = ''
  bodyCommitted = false
  appendTimelineNarration(narration)
}

function upsertMemoryStage(data) {
  const stage = data.stage
  if (!stage) return
  for (let i = timeline.value.length - 1; i >= 0; i--) {
    const it = timeline.value[i]
    if (it.kind === 'memory_stage' && it.stage === stage) {
      Object.assign(it, {
        kind: 'memory_stage',
        stage: data.stage,
        status: data.status,
        summary: data.summary,
        candidates: data.candidates,
        hit_count: data.hit_count,
        gate: data.gate,
        refine_path: data.refine_path,
        elapsed_ms: data.elapsed_ms,
        vector_hits: data.vector_hits,
        fts_hits: data.fts_hits,
      })
      return
    }
  }
  timeline.value.push({
    kind: 'memory_stage',
    stage: data.stage,
    status: data.status,
    summary: data.summary || '',
    candidates: data.candidates,
    hit_count: data.hit_count,
    gate: data.gate,
    refine_path: data.refine_path,
    elapsed_ms: data.elapsed_ms,
    vector_hits: data.vector_hits,
    fts_hits: data.fts_hits,
  })
}

function handleEvent(ev, data) {
  if (ev === 'memory_progress') {
    clearStepWait()
    upsertMemoryStage(data)
    maybeScroll(); scrollThink()
  }
  else if (ev === 'reasoning_delta') {
    clearStepWait()
    reasoningText.value += data.text || ''
    // v7 timeline：合并到末尾同类段
    const last = timeline.value[timeline.value.length - 1]
    if (last && last.kind === 'reasoning') {
      last.text = (last.text || '') + (data.text || '')
    } else {
      timeline.value.push({ kind: 'reasoning', text: data.text || '' })
    }
    liveThroughput.record(data.text)
    maybeScroll(); scrollThink()
  }
  else if (ev === 'content_delta') {
    clearStepWait()
    liveThroughput.record(data.text)
    if (bodyCommitted) {
      pushStreamText(data.text)
    } else {
      pendingBody += data.text
      commitPendingToBody()
    }
  }
  else if (ev === 'content_reset') {
    // 本步确认为工具步：撤回本步全部正文（含已 commit 到 streamText 的部分）
    retractToolStepBody()
    maybeScroll(); scrollThink()
  }
  // turn_started 仍作为事件对下游可见；step_started / step_progress 驱动步间真实进度
  else if (ev === 'step_started') {
    upsertStepWait(data.step, '准备下一步')
    maybeScroll(); scrollThink()
  }
  else if (ev === 'step_progress') {
    upsertStepWait(data.step, data.label, data.detail)
    maybeScroll(); scrollThink()
  }
  else if (ev === 'tool_executing') {
    clearStepWait()
    toolEvents.value.push({ type: ev, ...data })
    // v7 timeline：push 一张 running 卡片
    timeline.value.push({
      kind: 'tool_call',
      call_id: data.call_id || '',
      name: data.tool_name || '',
      arguments: data.arguments || '',
      status: 'running',
    })
    maybeScroll(); scrollThink()
  }
  else if (ev === 'tool_result') {
    toolEvents.value.push({ type: ev, ...data })
    // v7 timeline：找到对应 running 项就地更新（call_id + name 匹配）
    for (let i = timeline.value.length - 1; i >= 0; i--) {
      const it = timeline.value[i]
      if (it.kind === 'tool_call'
          && (it.call_id === data.call_id || !data.call_id)
          && it.name === (data.tool_name || '')
          && it.status === 'running') {
        it.status = data.ok ? 'ok' : 'fail'
        if (data.summary) it.result_preview = data.summary.slice(0, 400)
        if (data.citations?.length) it.citations = data.citations
        if (data.error) it.error = String(data.error).slice(0, 400)
        break
      }
    }
    maybeScroll(); scrollThink()
  }
  else if (ev === 'decision_notice') {
    decisionNotices.value.push(data)
    maybeScroll(); scrollThink()
  }
  else if (ev === 'citations') lastCitations = data.refs
  else if (ev === 'queued') toast.push('info', '正在处理上一条消息')
  else if (ev === 'degrade') { degraded.value = true }
  else if (ev === 'tool_visual') { streamVisuals.value.push(data); maybeScroll() }
  else if (ev === 'turn_completed') {
    sessionMetrics.value = data.session_metrics || sessionMetrics.value
    currentTurnMetrics.value = data.metrics || data.session_metrics?.current_turn || null
    // 用后端结算的真实 output_tokens 校准 charsPerToken，下一轮估算更准
    liveThroughput.calibrate(currentTurnMetrics.value?.output_tokens)
    streamAnalysisMetadata = data.analysis_metadata || streamAnalysisMetadata
    finishStream(data.message_id)
    if (data.threshold) handleThreshold(data.threshold)
  }
  else if (ev === 'step_metrics') {
    // 多步 turn：后端在步边界推送刷新，对齐 deepseek-harness 的 sessionStats 更新时机
    sessionMetrics.value = data.session_metrics || sessionMetrics.value
    currentTurnMetrics.value = data.metrics || sessionMetrics.value?.current_turn || currentTurnMetrics.value
  }
  else if (ev === 'error') { toast.push('error', friendlyError(data.message)); finishStream() }
  // handoff 摘要就绪（会话上下文管理方案 v2）
  else if (ev === 'handoff_ready') {
    handoffStatus.value = data.status
    handoffData.value = data
    if (pendingMessage.value) {
      const m = pendingMessage.value
      pendingMessage.value = null
      input.value = m.text
      attachments.value = m.atts
      nextTick(() => { autoGrow(); send() })
    }
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
      sessionStorage.setItem(`sp_soft_toast_shown_${sessStore.currentSid}`, '1')
    }
    thresholdBreached.value = 'soft'
  }
}

let lastCitations = []
let streamAnalysisMetadata = null
const streamPushSuppressed = ref(false)  // 编辑重发：以 reloadMessages 为准，避免 finishStream 重复入列
function finishStream(msgId) {
  // 先提交按步缓冲里尚未确认的正文（最终答案尾段），再合并节流缓冲，避免丢失
  commitPendingToBody()
  flushStreamText()
  // 跨会话保护：用户已切到其他会话时不把回复插进当前列表
  //（回复已按 session 落库，切回原会话时 openSession 会重新加载）
  const finishedSid = streamSid.value
  const sameSession = finishedSid === sessStore.currentSid
  // 中断终止（停止/出错/断连，无 msgId）时已输出的内容必须保留：
  // 哪怕只输出了处理进度也要留下，否则流式区一清空内容就全部丢失
  if (!streamPushSuppressed.value
      && (streamText.value || thinkText.value || reasoningText.value || decisionNotices.value.length || toolEvents.value.length || timeline.value.length) && sameSession) {
    const body = stripTail(streamText.value, streamVisuals.value)
    const m = {
      id: msgId, role: 'assistant',
      content: body || (msgId ? '' : '> ⚠️ 本回复未完成：生成已中断，仅输出了处理进度'),
      citations: lastCitations, feedback: 0,
      create_time: nowLocalIso(),
      thinking: thinkText.value || '', thinkOpen: false,
      analysis_metadata: streamAnalysisMetadata || {
        schema_version: 'agent-analysis-v1', reasoning_text: reasoningText.value,
        system_progress: thinkText.value, decision_notices: decisionNotices.value,
        tool_events: toolEvents.value, reasoning_available: !!reasoningText.value,
        timeline: [...timeline.value],   // v7 交错时间线
      },
      visuals: streamVisuals.value.length ? [...streamVisuals.value] : undefined
    }
    messages.value.push(m)
  }
  if (streamRaf) { cancelAnimationFrame(streamRaf); streamRaf = 0 }
  streamText.value = ''
  streamChunkBuf = ''
  thinkText.value = ''
  reasoningText.value = ''
  decisionNotices.value = []
  toolEvents.value = []
  timeline.value = []
  preBodyPhase.value = true
  streamVisuals.value = []
  thinkOpen.value = false
  pendingBody = ''
  if (bodyCommitTimer) { clearTimeout(bodyCommitTimer); bodyCommitTimer = 0 }
  bodyCommitted = false
  lastCitations = []
  streamAnalysisMetadata = null
  degraded.value = false
  generating.value = false
  streamSid.value = null
  liveThroughput.reset()
  sessStore.scheduleTitleRefresh(finishedSid)
  // 长文的 SSE 缓冲可能只保留末段。turn_completed 后以持久化消息回载，
  // 保证页面显示的是完整交付而不是传输缓存的残片。
  if (msgId && sameSession && !streamPushSuppressed.value) reloadMessages(sessStore.currentSid)
  maybeScroll()
}

// 手动停止：唯一会真正中断后台生成的动作。
// 已输出的内容必须保留：先即时保留屏上部分，后端中断补救会把已生成部分落库
// （带“未完成”标记），随后以落库版本为准重载会话，保证屏上所见即 DB 所存
async function abort() {
  const crid = sessionStorage.getItem('sp_active_crid')
  const sid = streamSid.value
  sse.abort()
  finishStream()   // 即时保留已输出部分，避免闪烁
  if (crid) {
    try { await api.post('/chat/cancel', { client_request_id: crid }) } catch { /* 忽略 */ }
  }
  // 后端中断补救落库为异步完成，稍候重载该会话消息，拉取持久化版本（含真实 id 与标记）
  if (sid) [300, 900].forEach(ms => setTimeout(() => {
    if (sessStore.currentSid === sid && !generating.value) reloadMessages(sid)
  }, ms))
}

// 仅重拉消息（不触发 tryReattach）：用于停止后以落库版本覆盖内存部分回复
async function reloadMessages(sid) {
  try {
    const msgs = await api.get(withQuery('/chat/messages', { session_id: sid }))
    for (const m of msgs) {
      if (m.role === 'user' && typeof m.content === 'string'
        && m.content.includes('\n---\n')
      && (m.content.includes('【附件：') || m.content.includes('【选中的文本】'))) {
        m.atts = extractAttachments(m.content)
        m.content = m.content.split('\n---\n').pop()
      }
    }
    if (sessStore.currentSid === sid) messages.value = stripToastNotifs(msgs)
  } catch { /* 重拉失败：保留内存部分回复不降级 */ }
}

// 反馈原因弹窗（自研对话框，替代原生 prompt；中文标签映射英文枚举值提交）
const fbDialog = ref(null)   // { msg, fb, reason }
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
  if (!msg.id) { toast.push('warning', '该消息暂不支持反馈'); return }
  if (msg.feedback === fb) return   // 已提交过相同反馈
  fbDialog.value = { msg, fb, reason: '', custom: '' }
}
async function submitFeedback() {
  const d = fbDialog.value
  if (!d || !d.reason) return
  // “其他”选项：提交用户自行输入的描述（加 other: 前缀便于后端识别自由文本）
  const reason = d.reason === 'other' ? 'other:' + d.custom.trim() : d.reason
  if (d.reason === 'other' && !d.custom.trim()) return
  await api.post('/chat/feedback', { message_id: d.msg.id, feedback: d.fb, reason })
  d.msg.feedback = d.fb
  fbDialog.value = null
  toast.push('success', '反馈已记录')
}

// ---- 消息编辑 ----
const editingId = ref(null)
const editText = ref('')

function startEdit(msg) {
  if (generating.value) return
  editingId.value = msg.id
  editText.value = msg.content
  nextTick(() => {
    const el = document.querySelector('.edit-textarea')
    if (el) { el.focus(); el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 200) + 'px' }
  })
}
function cancelEdit() { editingId.value = null; editText.value = '' }

async function submitEdit(msg) {
  const text = editText.value.trim()
  if (!text || text === msg.content) { cancelEdit(); return }
  const editMsgId = msg.id
  editingId.value = null
  editText.value = ''
  if (!trimMessagesFromEdit(editMsgId)) {
    toast.push('warning', '未能同步截断旧回复，将刷新消息列表')
    await reloadMessages(sessStore.currentSid)
  }
  messages.value.push({
    id: -1, role: 'user', content: text,
    message_type: 'normal', citations: [], feedback: 0,
    create_time: nowLocalIso(), images: msg.images || [],
    atts: msg.atts || [], has_branches: false
  })
  generating.value = true
  streamSid.value = sessStore.currentSid
  currentTurnMetrics.value = null
  liveThroughput.reset()
  streamText.value = ''
  thinkText.value = ''
  reasoningText.value = ''
  decisionNotices.value = []
  toolEvents.value = []
  timeline.value = []
  preBodyPhase.value = true
  thinkOpen.value = true
  maybeScroll()
  streamPushSuppressed.value = true
  try {
    await sse.send({
      sessionId: sessStore.currentSid, message: text,
      editMessageId: editMsgId,
      location: geoEnabled.value ? cachedLocation() : undefined,
      reasoningEffort: reasoningEffort.value,
      onEvent: (ev, data) => handleEvent(ev, data),
      onError: (e) => { toast.push('error', friendlyError(e?.message)); finishStream() },
    })
  } finally {
    streamPushSuppressed.value = false
  }
  if (generating.value) finishStream()
  await reloadMessages(sessStore.currentSid)
}

// ---- 版本切换 ----
async function switchVersion(msg, direction) {
  if (generating.value) return
  const siblings = await api.post('/chat/switch-version', {
    session_id: sessStore.currentSid,
    version_group_id: msg.version_group_id,
    // direction: +1 → 下一个兄弟, -1 → 上一个兄弟
    // 后端需要 target_message_id，前端需要计算
    target_message_id: await getSiblingId(msg, direction)
  })
  if (siblings && siblings.messages) {
    // 历史消息：附件还原
    for (const m of siblings.messages) {
      if (m.role === 'user' && typeof m.content === 'string'
        && m.content.includes('\n---\n')
      && (m.content.includes('【附件：') || m.content.includes('【选中的文本】'))) {
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
  const resp = await api.get(withQuery('/chat/version-siblings', {
    version_group_id: msg.version_group_id
  }))
  if (resp && resp.length) {
    const idx = resp.findIndex(s => s.id === msg.id)
    const targetIdx = idx + direction
    if (targetIdx >= 0 && targetIdx < resp.length) return resp[targetIdx].id
  }
  return msg.id
}

// 重新生成（分支化）：旧回复保留，创建 assistant 兄弟节点
async function regenerate(msg) {
  if (generating.value || !sessStore.currentSid) return
  if (!msg.id) { toast.push('warning', '该消息暂不支持重新生成'); return }
  let userMsg = null
  const idx = messages.value.indexOf(msg)
  for (let j = idx - 1; j >= 0; j--) {
    if (messages.value[j].role === 'user') { userMsg = messages.value[j]; break }
  }
  if (!userMsg || !userMsg.content) { toast.push('warning', '未找到对应的提问，无法重新生成'); return }
  generating.value = true
  streamSid.value = sessStore.currentSid
  currentTurnMetrics.value = null
  liveThroughput.reset()
  streamText.value = ''
  thinkText.value = ''
  reasoningText.value = ''
  decisionNotices.value = []
  toolEvents.value = []
  timeline.value = []
  preBodyPhase.value = true
  thinkOpen.value = true
  maybeScroll()
  await sse.send({
    sessionId: sessStore.currentSid, message: userMsg.content,
    regenerateMessageId: msg.id,
    location: geoEnabled.value ? cachedLocation() : undefined,
    reasoningEffort: reasoningEffort.value,
    onEvent: (ev, data) => handleEvent(ev, data),
    onError: (e) => { toast.push('error', friendlyError(e?.message)); finishStream() },
  })
  if (generating.value) finishStream()
  // 重新加载消息列表以获取更新后的分支信息
  await reloadMessages(sessStore.currentSid)
}

function copyText(msg) { navigator.clipboard.writeText(msg.content); toast.push('success', '已复制') }

// 引用记忆点击查看详情（轻量弹窗，复用 /memory/detail）
const memDetail = ref(null)
async function openMemory(id) {
  try { memDetail.value = await api.get(withQuery('/memory/detail', { id })) } catch { /* api 层已提示 */ }
}
async function memoryFeedback(c, msg, feedbackType) {
  try {
    await api.post('/memory/feedback', {
      memory_id: c.id, message_id: msg.id,
      feedback_type: feedbackType, query_text: ''
    })
    c.memory_feedback = feedbackType
    toast.push('success', feedbackType === 'irrelevant' ? '已降低这类问题下的召回权重' : '已标记并加入记忆治理')
  } catch { /* api 已提示 */ }
}
function stripTail(t, visuals) {
  // 兼容兜底：后端已改为确定性提取，但旧模型缓存可能仍输出嵌入式 JSON 声明
  let s = (t || '').replace(/\s*\{\s*"citations"\s*:\s*\[[^\]]*\]\s*\}\s*/g, '\n')
  s = s.replace(/\s*\{\s*"memory_confirm"\s*:\s*\{[^}]*\}\s*\}\s*/g, '\n')
  // 声明被挖走后残留的空代码围栏（如 ```json\n```）会渲染成空白块，一并清理
  s = s.replace(/```[a-zA-Z]*\s*```/g, '').trimEnd()
  // 模型有时输出 <antartifact type="text/html">...</antartifact> 而非代码块，转为标准 html 代码块
  // 注意：围栏必须独占一行，前后补换行，避免与正文同行导致 markdown 不识别
  s = s.replace(/<antartifact[^>]*type=["']text\/html["'][^>]*>([\s\S]*?)<\/antartifact>/gi,
    (_, content) => '\n\n```html\n' + content.trim() + '\n```\n')
  // 剔除模型内部工具调用块（不应展示给用户）：英文 <tool_call> 和中文 <工具调用> 两种格式
  s = s.replace(/<tool_call>[\s\S]*?<\/tool_call>/gi, '')
  s = s.replace(/<工具调用>[\s\S]*?<\/工具调用>/g, '')
  // 仅当 tool_visual 已产出图表时才剥离 Mermaid 代码块，避免重复渲染；
  // 若 visuals 为空（工具未调用/失败），保留原文由 marked → mermaid.run() 兜底
  const hasVisual = Array.isArray(visuals) && visuals.length > 0
  if (hasVisual) {
    s = s.replace(/```(?:mermaid|flowchart)\s*\n[\s\S]*?\n```/gi, '')
  }
  return s
}
function render(md, visuals) {
  // 对话内容里的链接统一新标签打开，不在当前界面跳转
  const html = marked.parse(stripTail(md, visuals))
    .replace(/<a\s/gi, '<a target="_blank" rel="noopener noreferrer" ')
  return sanitizeHtml(enhanceResponseHtml(groupSections(html)))
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
  } catch { return html }  // 分组失败降级为原始渲染
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
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
  return sanitizeHtml(esc
    .replace(/(https?:\/\/[^\s<]+)/g,
      '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/\n/g, '<br>'))
}

function messageKey(msg, index) {
  if (msg && msg.id != null && Number(msg.id) > 0) return `message-${msg.id}`
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
  root.scrollTo({ top, behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' })
}

// 本地时间 ISO（秒级）由 utils/format.js 统一提供，避免各视图重复实现
function scrollBottom() { nextTick(() => { if (scroller.value) { scroller.value.scrollTop = scroller.value.scrollHeight; atBottom.value = true } }) }
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
    atBottom.value = (el.scrollHeight - el.scrollTop - el.clientHeight) < 80
  }
  lastScrollTop = el.scrollTop
}
function maybeScroll() { if (atBottom.value) scrollBottom() }
// 流式输出期间：消息内代码块（pre 限高 300px 内部滚动）自动吸底跟随最新内容。
// v-html 每次增量都会重建 DOM（scrollTop 归零停在第一屏），故每次渲染后重新吸底；
// 与外层 atBottom 智能跟随无关，流式结束后正式消息重新渲染，代码块回到顶部便于阅读
function scrollStreamCode() {
  nextTick(() => {
    document.querySelectorAll('.content.streaming pre').forEach(p => {
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
  if (generating.value) sse.abort()
  sessStore.setCurrent(null)
  messages.value = []
  sessionMetrics.value = null
  currentTurnMetrics.value = null
  streamText.value = ''
  thinkText.value = ''
  reasoningText.value = ''
  decisionNotices.value = []
  toolEvents.value = []
  timeline.value = []
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
  sessStore.load(); loadProviders()
  window.addEventListener('sp-new-chat', resetToHome)
  window.addEventListener('sp-open-session', onOpenSession)
  document.addEventListener('click', handleMermaidActions)
  // 直接从其他页面进入或刷新后恢复上次会话（currentSid 已从 localStorage 恢复）
  // → openSession 内部会调 tryReattach 续播进行中的生成，实现刷新不中断
  if (sessStore.currentSid && !messages.value.length) openSession(sessStore.currentSid)
  initGeolocation()
  document.addEventListener('click', onDocClickEmoji)
  document.addEventListener('click', onDocClickModelControl)
  mermaidObserver = new MutationObserver(() => { scheduleMermaidScoped() })
  mermaidObserver.observe(scroller.value, { childList: true, subtree: true })
  scheduleMermaidScoped()
})
// 浏览器定位（方案 A）：开关开启时获取一次并缓存，发消息时携带城市名
const geoEnabled = ref(false)
async function initGeolocation() {
  try {
    const d = await api.get('/settings/params')
    geoEnabled.value = !!d.params?.geolocation_enabled
    if (geoEnabled.value) {
      resolveLocation().catch(() => { /* 拒绝授权/超时静默降级，不影响对话 */ })
    }
  } catch { /* 参数接口失败静默跳过 */ }
}

// Mermaid 操作按钮 + HTML 预览事件委派（v-html 内无法用 Vue 事件，走原生委派）
const htmlPreview = ref(null)  // 存储 HTML 源码，非 null 时展示预览抽屉
const htmlFullscreen = ref(false)
function handleMermaidActions(e) {
  const btn = e.target.closest('.mermaid-zoom-out, .mermaid-zoom-in, .mermaid-reset, .mermaid-copy-src, .mermaid-copy-img, .html-preview-btn, .html-download-btn, .html-copy-btn, .code-copy-btn')
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
  const src = (wrap.dataset.source || '').replace(/&amp;/g, '&').replace(/&quot;/g, '"').replace(/&lt;/g, '<')
  if (btn.classList.contains('mermaid-copy-src')) {
    navigator.clipboard.writeText(src); toast.push('success', '源码已复制')
  } else if (btn.classList.contains('mermaid-copy-img')) {
    copyMermaidAsImage(wrap)
  } else if (btn.classList.contains('html-preview-btn')) {
    htmlPreview.value = src
  } else if (btn.classList.contains('html-download-btn')) {
    const blob = new Blob([src], { type: 'text/html' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob); a.download = 'preview.html'; a.click()
    URL.revokeObjectURL(a.href); toast.push('success', '已下载')
  } else if (btn.classList.contains('html-copy-btn') || btn.classList.contains('code-copy-btn')) {
    navigator.clipboard.writeText(src); toast.push('success', '代码已复制')
  }
}
async function copyMermaidAsImage(wrap) {
  const svg = wrap.querySelector('svg')
  if (!svg) { toast.push('error', '图表未渲染'); return }
  try {
    // 共享导出工具：自动剥离 foreignObject 避免 tainted canvas（详见 utils/svgExport.js）
    const blob = await svgToPngBlob(svg, 2)
    await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })])
    toast.push('success', '图片已复制到剪贴板')
  } catch { toast.push('error', '复制图片失败，请手动右键保存') }
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
        try { await mermaid.run({ nodes }) } catch (e) { }
      }
      bindInlineMermaidInteractions(root)
    } while (mermaidRunRequested)
  } finally {
    mermaidRunInFlight = false
  }
}
watch(() => messages.value.length, scheduleMermaidScoped)
watch(messages, scheduleMermaidScoped)
watch(generating, (v) => { if (!v) scheduleMermaidScoped() })
onUnmounted(() => {
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
  <div style="display:flex;height:100vh;margin:-28px -40px;max-width:none;width:auto">
    <!-- 消息气泡文字选中浮层：复制 / 引用 -->
    <SelectionActionBar :visible="selection.visible.value" :rect="selection.rect.value"
      @copy="copySelection" @quote="quoteSelection" />
    <!-- 引用评论录入弹窗（点"引用"后弹出，确认后写入附件条） -->
    <QuoteComposer :visible="pendingQuote.visible" :quote-text="pendingQuote.text"
      @confirm="commitQuoteAttachment" @cancel="cancelQuoteComposer" />
    <!-- 定位锚点栏：独立左列，宽度不随右侧内容变化；与对话状态解耦，仅悬停触发 -->
    <MessageAnchorRail :anchors="anchorItems" @select="scrollToAnchor" />
    <!-- 对话区（会话列表已合并到全局侧栏 SessionSidebar） -->
    <div style="flex:1;display:flex;flex-direction:column;min-width:0;position:relative">
      <!-- 空状态：顶部 spacer 将 hero+composer 推向中间 -->
      <div v-if="!messages.length && !streamText" style="flex:1"></div>
      <div v-if="degraded" class="banner" style="background:var(--warnbg);color:var(--warntx);margin:16px 32px 0">
        ℹ 服务响应较慢，仍在等待首字输出…
      </div>
      <!-- overflow-anchor:none：禁用浏览器滚动锚定，避免内容高度变化时自动补偿 scrollTop 引发抖动 -->
      <div ref="scroller"
        :style="{ flex: !messages.length && !streamText ? '0 0 auto' : '1', overflowY: 'auto', overflowAnchor: 'none' }"
        @scroll.passive="onScroll">
        <div style="max-width:820px;margin:0 auto;width:100%;padding:28px 32px">
          <div v-if="!messages.length && !streamText" class="chat-hero">
            <div class="logo-lg"><i class="ti ti-brain"></i></div>
            <h2>Second Person 比你更懂你！</h2>
          </div>
          <div v-for="(m, i) in messages" :key="messageKey(m, i)" :ref="el => registerMessageElement(messageKey(m, i), el)"
            class="chat-message-item" :data-msg-id="m.id || undefined" :data-anchor-key="m.role === 'user' ? messageKey(m, i) : undefined">
            <!-- 用户气泡 -->
            <div v-if="m.role === 'user'" class="msg-user">
              <div :style="editingId === m.id ? { width: '78%' } : { maxWidth: '78%' }">
                <!-- 版本翻页器（用户消息有多版本时显示） -->
                <div v-if="m.has_branches" class="version-nav" style="justify-content:flex-end">
                  <button class="ver-btn" :disabled="m.sibling_index === 0" @click="switchVersion(m, -1)">
                    <i class="ti ti-chevron-left"></i>
                  </button>
                  <span class="ver-indicator">{{ m.sibling_index + 1 }} / {{ m.sibling_count }}</span>
                  <button class="ver-btn" :disabled="m.sibling_index === m.sibling_count - 1" @click="switchVersion(m, 1)">
                    <i class="ti ti-chevron-right"></i>
                  </button>
                </div>
                <div v-if="m.atts && m.atts.length"
                  style="display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-end;margin-bottom:6px">
                  <span v-for="(f, fi) in m.atts" :key="fi" class="attach-chip attach-click"
                    :class="{ 'attach-quote': f.kind === 'quote' }"
                    :title="f.kind === 'quote' ? '点击查看引用全文' : f.pasted ? '点击查看全文' : '点击查看详情'"
                    @click="openMsgAttachment(f)">
                    <i class="ti"
                      :class="f.kind === 'quote' ? 'ti-quote' : (f.pasted ? 'ti-clipboard-text' : 'ti-paperclip')"></i>
                    {{ f.name }}
                    <span v-if="f.kind === 'quote' && f.comment" class="quote-comment-mark"
                      title="包含用户评论">·带评论</span>
                  </span>
                </div>
                <div v-if="m.images && m.images.length"
                  style="display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-end;margin-bottom:6px">
                  <img v-for="(im, ii) in m.images" :key="ii" :src="im" class="bubble-img"
                    @click="openBubbleImage(im)" />
                </div>
                <!-- 编辑态：textarea + 提交/取消 -->
                <div v-if="editingId === m.id" style="max-width:100%">
                  <textarea class="edit-textarea" v-model="editText" rows="2"
                    @input="e => { e.target.style.height = 'auto'; e.target.style.height = Math.min(e.target.scrollHeight, 200) + 'px' }"
                    @keydown.enter.exact.prevent="submitEdit(m)"
                    @keydown.escape="cancelEdit"></textarea>
                  <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:6px">
                    <button class="edit-cancel-btn" @click="cancelEdit">取消</button>
                    <button class="edit-submit-btn" @click="submitEdit(m)">提交修改</button>
                  </div>
                </div>
                <!-- 普通态 -->
                <div v-else>
                  <div v-if="m.content" class="bubble" style="max-width:100%" v-html="renderUser(m.content)"></div>
                </div>
                <div class="msg-actions user-actions"
                  style="display:flex;justify-content:flex-end;gap:2px;margin-top:4px">
                  <span class="msg-time">{{ formatTimeFull(m.create_time) }}</span>
                  <i class="ti ti-copy" title="复制" @click="copyText(m)"></i>
                  <i v-if="m.id && !generating && editingId !== m.id"
                    class="ti ti-edit" title="编辑" @click="startEdit(m)"></i>
                </div>
              </div>
            </div>
            <!-- 系统通知 -->
            <div v-else-if="m.message_type === 'system_notification'" class="banner"
              style="background:var(--brand-soft);color:var(--acctx)">
              <i class="ti ti-bell"></i> <span style="opacity:0.7">{{ formatRelative(m.create_time) }}</span> {{
                m.content
              }}
            </div>
            <!-- AI 回复 -->
            <div v-else class="msg-ai">
              <div class="avatar"><i class="ti ti-brain"></i></div>
              <div class="body">
                <!-- Legacy messages keep the old mixed text; new messages use
                     structured provider/host lanes below. -->
                <div v-if="m.thinking && m.analysis_metadata?.schema_version !== 'agent-analysis-v1'" class="think-panel">
                  <div class="think-head" @click="toggleMessageThink(m)">
                    <span class="think-section-toggle">
                      <i class="ti" :class="messageThinkExpanded(m) ? 'ti-chevron-down' : 'ti-chevron-right'"></i>
                    </span>
                    <span class="think-section-title">{{ formatTimelineSummary(m.analysis_metadata?.timeline) }}</span>
                  </div>
                  <div v-show="messageThinkExpanded(m)" class="think-body">{{ m.thinking }}</div>
                </div>
                <div v-if="m.analysis_metadata?.schema_version === 'agent-analysis-v1' && (m.analysis_metadata.timeline?.length || m.analysis_metadata.reasoning_text || m.analysis_metadata.system_progress || m.analysis_metadata.decision_notices?.length || m.analysis_metadata.tool_events?.length)" class="think-panel">
                  <div class="think-head" @click="toggleMessageThink(m)">
                    <span class="think-section-toggle">
                      <i class="ti" :class="messageThinkExpanded(m) ? 'ti-chevron-down' : 'ti-chevron-right'"></i>
                    </span>
                    <span class="think-section-title">{{ formatTimelineSummary(m.analysis_metadata?.timeline) }}</span>
                  </div>
                  <div v-show="messageThinkExpanded(m)" class="think-body think-body-timeline">
                    <!-- v7 优先按时间线渲染，历史消息无 timeline 时降级 -->
                    <ThinkingTimeline v-if="m.analysis_metadata.timeline?.length"
                                      :items="m.analysis_metadata.timeline" />
                    <template v-else>
                      <div v-if="m.analysis_metadata.reasoning_text" class="think-lane"><strong>模型推理</strong><span>{{ m.analysis_metadata.reasoning_text }}</span></div>
                      <div v-if="m.analysis_metadata.system_progress" class="think-lane"><strong>系统进度</strong><span>{{ m.analysis_metadata.system_progress }}</span></div>
                      <div v-if="m.analysis_metadata.decision_notices?.length" class="think-lane"><strong>决策摘要</strong><span v-for="(n, ni) in m.analysis_metadata.decision_notices" :key="ni">{{ n.summary }}</span></div>
                      <div v-if="m.analysis_metadata.tool_events?.length" class="think-lane"><strong>工具执行</strong><span v-for="(t, ti) in m.analysis_metadata.tool_events" :key="ti">{{ t.tool_name }}：{{ t.type === 'tool_result' ? (t.ok ? '已完成' : '未完成') : '执行中' }}</span></div>
                    </template>
                  </div>
                </div>
                <DiagramRenderer v-for="(v, vi) in (m.visuals || [])" :key="'hv' + vi" :type="v.type" :data="v.data" />
                <div class="content" v-html="render(cachedWebSrc(m.content).body, m.visuals)"></div>
                <div v-if="cachedWebSrc(m.content).count" class="think-panel" style="margin-top:8px">
                  <div class="think-head" @click="m.srcOpen = !m.srcOpen">
                    <i class="ti ti-world"></i><span>联网来源（{{ cachedWebSrc(m.content).count }}）</span>
                    <i class="ti" :class="m.srcOpen ? 'ti-chevron-up' : 'ti-chevron-down'"></i>
                  </div>
                  <div v-show="m.srcOpen" class="content" style="padding:8px 12px 8px 34px"
                    v-html="render(cachedWebSrc(m.content).list)"></div>
                </div>
                <div v-if="m.citations && m.citations.length" class="muted" style="margin-top:8px">
                  <i class="ti ti-quote"></i> 引用记忆：<span v-for="(c, ci) in m.citations" :key="c.id || ci"
                    class="cite-group">
                    <span class="cite-link" title="点击查看记忆详情" @click="openMemory(c.id)">[{{ ci + 1 }}] {{
                      c.title || c.id }}</span>
                    <span class="cite-feedback">
                      <button class="btn-xs" title="这条记忆与本轮无关"
                        @click.stop="memoryFeedback(c, m, 'irrelevant')"><i class="ti ti-link-off"></i> 不相关</button>
                      <button class="btn-xs" title="这条记忆已过时"
                        @click.stop="memoryFeedback(c, m, 'stale')"><i class="ti ti-clock-off"></i> 过时</button>
                    </span>
                  </span>
                </div>
                <!-- 版本翻页器（AI 回复有多版本时显示） -->
                <div v-if="m.has_branches" class="version-nav">
                  <button class="ver-btn" :disabled="m.sibling_index === 0" @click="switchVersion(m, -1)">
                    <i class="ti ti-chevron-left"></i>
                  </button>
                  <span class="ver-indicator">{{ m.sibling_index + 1 }} / {{ m.sibling_count }}</span>
                  <button class="ver-btn" :disabled="m.sibling_index === m.sibling_count - 1" @click="switchVersion(m, 1)">
                    <i class="ti ti-chevron-right"></i>
                  </button>
                </div>
                <div class="msg-actions" style="margin-top:8px;display:flex;gap:2px">
                  <i class="ti ti-thumb-up" title="点赞" :style="{ color: m.feedback === 1 ? 'var(--succtx)' : '' }"
                    @click="feedback(m, 1)"></i>
                  <i class="ti ti-thumb-down" title="点踩" :style="{ color: m.feedback === 2 ? 'var(--dangtx)' : '' }"
                    @click="feedback(m, 2)"></i>
                  <i class="ti ti-copy" title="复制" @click="copyText(m)"></i>
                  <i v-if="m.id && !generating" class="ti ti-refresh" title="重新生成" @click="regenerate(m)"></i>
                  <span class="msg-time">{{ formatTimeFull(m.create_time) }}</span>
                </div>
              </div>
            </div>
          </div>

          <!-- 生成中：安全处理进度（展开）→ 正文首字后自动折叠并流式输出正文 -->
          <!-- 生成中：仅在发起请求的会话内展示（切会话后不泄漏到其他会话） -->
          <div v-if="(generating || streamText) && streamSid === sessStore.currentSid" class="msg-ai">
            <div class="avatar"><i class="ti ti-brain"></i></div>
            <div class="body">
              <div v-if="showLiveThinkPanel" class="think-panel">
                <div class="think-head" @click="thinkOpen = !thinkOpen">
                  <span class="think-section-toggle">
                    <i class="ti" :class="thinkOpen ? 'ti-chevron-down' : 'ti-chevron-right'"></i>
                  </span>
                  <span class="think-section-title">
                    {{ liveThinkSummary }}
                    <span v-if="showThinkLiveDots" class="think-live"><span
                        class="think-dots"><span></span><span></span><span></span></span></span>
                  </span>
                </div>
                <div v-show="thinkOpen" ref="liveThink" class="think-body think-body-timeline">
                  <ThinkingTimeline v-if="timeline.length" :items="timeline" :live="timelineLive" />
                  <!-- 不含 timeline 的降级：老的分区结构 -->
                  <template v-else>
                    <div v-if="reasoningText" class="think-lane"><strong>模型推理</strong><span>{{ reasoningText }}</span></div>
                    <div v-if="thinkText" class="think-lane"><strong>系统进度</strong><span>{{ thinkText }}</span></div>
                    <div v-if="decisionNotices.length" class="think-lane"><strong>决策摘要</strong><span v-for="(n, ni) in decisionNotices" :key="ni">{{ n.summary }}</span></div>
                  </template>
                </div>
              </div>
              <!-- 尚无任何进度事件：单一「处理中」占位（与上方面板互斥） -->
              <div v-if="showProcessingPlaceholder" class="content" style="display:flex;align-items:center;gap:8px">
                <span class="muted">处理中</span>
                <span class="think-dots"><span></span><span></span><span></span></span>
              </div>
              <!-- 图形组件 -->
              <DiagramRenderer v-for="(v, vi) in streamVisuals" :key="'sv' + vi" :type="v.type" :data="v.data" />
              <div v-if="streamText" class="content streaming" v-html="render(streamWebSrc.body, streamVisuals)">
              </div>
              <div v-if="streamText && streamWebSrc.count" class="think-panel" style="margin-top:8px">
                <div class="think-head" @click="streamSrcOpen = !streamSrcOpen">
                  <i class="ti ti-world"></i><span>联网来源（{{ streamWebSrc.count }}）</span>
                  <i class="ti" :class="streamSrcOpen ? 'ti-chevron-up' : 'ti-chevron-down'"></i>
                </div>
                <div v-show="streamSrcOpen" class="content" style="padding:8px 12px 8px 34px"
                  v-html="render(streamWebSrc.list)"></div>
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
      <div style="padding:16px 32px 12px">
        <!-- 95% 硬阈值提示条（会话上下文管理方案 v2） -->
        <div v-if="thresholdBreached === 'hard'" class="handoff-bar" style="max-width:820px;margin:0 auto 10px">
          <i class="ti ti-alert-triangle"></i>
          <span style="flex:1">此会话已达容量上限</span>
          <button class="btn-primary" @click="startHandoff" style="font-size:var(--fs-sm);padding:4px 12px">
            <i class="ti ti-arrow-forward"></i> 开启新会话
          </button>
        </div>
        <!-- handoff 摘要附件（会话上下文管理方案 v2） -->
        <HandoffAttachment v-if="handoffStatus" :status="handoffStatus" :data="handoffData" class="chat-handoff-attach"
          @remove="removeHandoff" @preview="handoffPreview = handoffData || { status: handoffStatus }" />
        <div class="composer" :class="{ dragover: dragOver }" style="max-width:820px;margin:0 auto;position:relative"
          @dragenter.prevent="dragOver = true" @dragover.prevent="dragOver = true" @dragleave.prevent="onDragLeave"
          @drop.prevent.stop="onDrop">
          <!-- 附件条（胶囊可点击：粘贴文本/图片预览，其他格式看详情） -->
          <div v-if="attachments.length" style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px">
            <span v-for="(a, ai) in attachments" :key="ai" class="attach-chip attach-click"
              :class="{ 'attach-quote': a.kind === 'quote' }"
              :title="a.kind === 'quote' ? '点击查看引用全文' : a.pasted ? '点击查看全文' : a.isImage ? '点击预览' : '点击查看详情'"
              @click="openAttachment(a)">
              <img v-if="a.isImage && a.preview" :src="a.preview" class="attach-thumb" />
              <i v-else class="ti"
                :class="a.uploading ? 'ti-loader-2' : (a.error ? 'ti-alert-triangle' : (a.kind === 'quote' ? 'ti-quote' : (a.pasted ? 'ti-clipboard-text' : 'ti-paperclip')))"></i>
              {{ a.name }}
              <span v-if="a.uploading" class="muted">解析中…</span>
              <span v-else-if="a.error" class="dang">失败</span>
              <span v-else-if="a.isImage" class="muted">图片</span>
              <span v-else-if="!a.parsed" class="dang">无文本</span>
              <span v-else class="muted">{{ a.chars }} 字{{ a.truncated ? '·已截断' : '' }}</span>
              <span v-if="a.kind === 'quote' && a.comment" class="quote-comment-mark"
                title="包含用户评论">·带评论</span>
              <span v-if="!a.isImage && !a.pasted && a.parsed" class="muted" title="发送后自动存入知识库"><i
                  class="ti ti-database"></i>
                入库</span>
              <i class="ti ti-x" style="cursor:pointer" @click.stop="removeAttachment(ai)"></i>
            </span>
          </div>
          <textarea ref="ta" v-model="input"
            :placeholder="thresholdBreached === 'hard' ? '已达容量上限，请开启新会话' : '发消息给 Second Person（Enter 发送，Shift+Enter 换行，可拖入/粘贴文件；项目会话内输入 @ 可插入文件）'"
            rows="1" @input="onComposerInput" @keydown="onComposerKeyDown" @paste="onPaste"
            @dragenter.prevent="dragOver = true" @dragover.prevent="dragOver = true" @drop.prevent.stop="onDrop"
            :disabled="thresholdBreached === 'hard'"></textarea>
          <FilePickerPanel v-if="currentProject" ref="filePickerRef"
                           :project-id="currentProject.id"
                           :visible="filePickerVisible"
                           :query="filePickerQuery"
                           @pick="onFilePicked"
                           @close="filePickerVisible = false" />
          <!-- 表情选择面板（absolute 定位在 composer 上方，选择后保持打开可连续插入） -->
          <div v-if="emojiOpen" class="emoji-panel" @click.stop>
            <div v-for="g in EMOJI_GROUPS" :key="g.name" class="emoji-group">
              <div class="emoji-group-name">{{ g.name }}</div>
              <div class="emoji-grid">
                <button v-for="em in g.items" :key="em" type="button" class="emoji-btn" @click="insertEmoji(em)">{{ em
                }}</button>
              </div>
            </div>
          </div>
          <div class="row" style="margin-top:24px">
            <div class="fg" style="gap:8px">
              <i class="ti ti-paperclip" style="cursor:pointer;color:var(--muted);font-size:var(--icon-sm)" title="上传文件"
                @click="triggerFile"></i>
              <!-- mousedown.prevent：防止按钮抢焦点导致 textarea 光标位置丢失 -->
              <i class="ti ti-mood-smile emoji-toggle"
                style="cursor:pointer;color:var(--muted);font-size:var(--icon-sm)" title="表情" @mousedown.prevent
                @click.stop="emojiOpen = !emojiOpen"></i>
              <SandboxModeChip :session-id="sessStore.currentSid"
                               :has-project="!!currentProject"
                               :fallback-mode="sandboxFallback"
                               @pending-change="m => pendingSandboxMode = m" />
            </div>
            <div class="composer-actions">
              <input ref="fileInput" type="file" multiple style="display:none" @change="onFilePick" />
              <!-- 单一入口展示当前模型和推理等级；选择在上方两级菜单中完成。 -->
              <div class="model-control-wrap">
                <button type="button" class="model-control-btn" :title="`模型：${selectedModelLabel}；推理等级：${reasoningEffortLabel}`"
                  :aria-expanded="modelControlOpen" aria-haspopup="dialog" @mousedown.prevent @click.stop="toggleModelControl">
                  <span class="model-control-name">{{ selectedModelLabel }}</span>
                  <span class="model-control-effort">{{ reasoningEffortCompactLabel }}</span>
                  <i class="ti" :class="modelControlOpen ? 'ti-chevron-up' : 'ti-chevron-down'"></i>
                </button>
                <div v-if="modelControlOpen" class="model-control-menu" role="dialog" aria-label="模型与推理等级" @click.stop>
                  <template v-if="modelControlPanel === 'overview'">
                    <button type="button" class="model-control-row" @click="openModelControlPanel('model')">
                      <span class="model-control-row-label">模型</span>
                      <span class="model-control-row-value">
                        <span class="model-control-row-text" :title="selectedModelLabel">{{ selectedModelLabel }}</span>
                        <i class="ti ti-chevron-right"></i>
                      </span>
                    </button>
                    <button type="button" class="model-control-row" @click="openModelControlPanel('reasoning')">
                      <span class="model-control-row-label">推理等级</span>
                      <span class="model-control-row-value">
                        <span class="model-control-row-text" :title="reasoningEffortCompactLabel">{{ reasoningEffortCompactLabel }}</span>
                        <i class="ti ti-chevron-right"></i>
                      </span>
                    </button>
                  </template>
                  <template v-else-if="modelControlPanel === 'model'">
                    <button type="button" class="model-control-back" @click="openModelControlPanel('overview')">
                      <i class="ti ti-chevron-left"></i> 模型
                    </button>
                    <button v-for="provider in providers" :key="provider.id" type="button" class="model-control-option"
                      :class="{ active: provider.id === chatModelId }" role="menuitemradio" :aria-checked="provider.id === chatModelId"
                      @click="pickChatModel(provider.id)">
                      <span>{{ provider.display_name }}</span>
                      <i v-if="provider.id === chatModelId" class="ti ti-check"></i>
                    </button>
                  </template>
                  <template v-else>
                    <button type="button" class="model-control-back" @click="openModelControlPanel('overview')">
                      <i class="ti ti-chevron-left"></i> 推理等级
                    </button>
                    <button v-for="opt in reasoningOptions" :key="opt.value" type="button" class="model-control-option"
                      :class="{ active: reasoningEffort === opt.value }" role="menuitemradio" :aria-checked="reasoningEffort === opt.value"
                      @click="pickReasoningEffort(opt.value)">
                      <span>{{ opt.label }}</span>
                      <i v-if="reasoningEffort === opt.value" class="ti ti-check"></i>
                    </button>
                  </template>
                </div>
              </div>
              <button v-if="!generating" class="send-btn" @click="send"><i class="ti ti-arrow-up"></i></button>
              <button v-else class="send-btn" @click="abort"><i class="ti ti-player-stop-filled"></i></button>
            </div>
          </div>
        </div>
        <!-- 会话指标行：置于输入框正下方，与其居中对齐 -->
        <SessionMetricsLine :metrics="sessionMetrics" :turn-metrics="currentTurnMetrics" :live-tokens-per-second="liveThroughput.tokensPerSecond.value" />
      </div>
      <!-- 空状态：底部 spacer 将 hero+composer 推离底端 -->
      <div v-if="!messages.length && !streamText" style="flex:1"></div>
    </div>
  </div>

  <!-- handoff 摘要预览：摘要正文由后端注入下一轮上下文，前端展示可用元信息 -->
  <BaseModal v-if="handoffPreview" title="上一会话摘要" size="sm" stacked @close="handoffPreview = null">
    <dl class="kv">
      <dt>状态</dt>
      <dd>{{ handoffPreview.status === 'failed' ? '生成失败' : '已就绪' }}</dd>
      <dt v-if="handoffPreview.original_turns != null">原会话轮次</dt>
      <dd v-if="handoffPreview.original_turns != null">{{ handoffPreview.original_turns }}</dd>
      <dt v-if="handoffPreview.summary_tokens != null">摘要长度</dt>
      <dd v-if="handoffPreview.summary_tokens != null">约 {{ handoffPreview.summary_tokens }} token</dd>
    </dl>
    <p class="modal-subtitle">发送下一条消息时，系统会自动把该摘要注入新会话上下文。</p>
    <template #footer>
      <button type="button" @click="handoffPreview = null">关闭</button>
    </template>
  </BaseModal>

  <!-- 引用记忆详情弹窗（点击对话中的引用打开，二级层叠；SP-UI v4 统一走 BaseModal） -->
  <BaseModal v-if="memDetail" title="记忆详情" size="md" stacked @close="memDetail = null">
    <h3 class="modal-subtitle">{{ memDetail.frontmatter?.title || memDetail.id }}</h3>
    <div class="fg" style="gap:6px;margin-bottom:10px">
      <span v-if="memDetail.frontmatter?.confidence" class="badge badge-a">{{
        confidenceLabel(memDetail.frontmatter?.confidence) }}</span>
      <span v-if="memDetail.frontmatter?.lifecycle" class="badge">{{
        lifecycleLabel(memDetail.frontmatter?.lifecycle) }}</span>
      <span v-if="memDetail.access_count != null" class="muted">被引用 {{ memDetail.access_count }} 次</span>
    </div>
    <div class="label">摘要</div>
    <p style="color:var(--sec);margin-bottom:12px">{{ memDetail.summary }}</p>
    <div v-if="memDetail.detail" class="label">详情</div>
    <p v-if="memDetail.detail"
      style="color:var(--sec);margin-bottom:12px;white-space:pre-wrap;max-height:280px;overflow-y:auto">{{
        memDetail.detail }}</p>
    <div v-if="memDetail.governance" class="memory-provenance">
      <div class="label">记忆状态</div>
      <div class="muted">验证：{{ memDetail.governance.verification_state || '未验证' }} ·
        时效：{{ memDetail.governance.freshness_state || '当前' }}</div>
      <div v-if="memDetail.evidence?.length" class="muted" style="margin-top:4px">
        证据：{{ memDetail.evidence[0].excerpt || memDetail.evidence[0].source_ref || '已记录来源' }}
      </div>
    </div>
    <template #footer>
      <button @click="memDetail = null">关闭</button>
    </template>
  </BaseModal>

  <!-- 附件查看弹窗：粘贴文本/图片应用内预览，其他格式信息+下载（二级层叠，统一走 BaseModal） -->
  <BaseModal v-if="attachView"
    :title="attachView.type === 'text' ? '文本内容' : attachView.type === 'image' ? '图片预览' : '附件详情'"
    :size="attachView.type === 'file' ? 'sm' : 'lg'" stacked @close="attachView = null">
    <!-- 粘贴文本 / 引用：全文预览；引用带评论时追加评论段 -->
    <div v-if="attachView.type === 'text'">
      <div class="muted" style="margin-bottom:10px">{{ attachView.chars }} 字 · {{ attachView.lines }} 行</div>
      <div v-if="attachView.kind === 'quote'" class="label" style="margin-bottom:6px">
        <i class="ti ti-quote"></i> 选中的文本
      </div>
      <div
        style="white-space:pre-wrap;word-break:break-all;font-family:var(--mono);font-size:var(--fs-base);line-height:1.6;color:var(--sec);background:var(--surface-2);border:1px solid var(--bd);border-radius:var(--radius-sm);padding:14px;max-height:60vh;overflow-y:auto">
        {{ attachView.text }}</div>
      <div v-if="attachView.kind === 'quote' && attachView.comment" style="margin-top:14px">
        <div class="label" style="margin-bottom:6px"><i class="ti ti-message-2"></i> 用户评论</div>
        <div
          style="white-space:pre-wrap;word-break:break-all;font-size:var(--fs-base);line-height:1.6;color:var(--pri);background:var(--brand-soft);border:1px solid rgba(59,110,246,.18);border-radius:var(--radius-sm);padding:12px;max-height:30vh;overflow-y:auto">
          {{ attachView.comment }}</div>
      </div>
    </div>
    <!-- 图片：应用内大图预览 -->
    <div v-else-if="attachView.type === 'image'" style="text-align:center">
      <img :src="attachView.src" style="max-width:100%;max-height:68vh;border-radius:var(--radius-sm)" />
    </div>
    <!-- 其他格式：不做内容预览，只展示文件信息 + 下载 -->
    <div v-else>
      <div class="fg" style="gap:10px;margin-bottom:14px">
        <i class="ti ti-paperclip" style="font-size:var(--icon-md);color:var(--muted)"></i>
        <b style="word-break:break-all">{{ attachView.name }}</b>
      </div>
      <div class="muted" style="margin-bottom:6px">格式：{{ attachExt(attachView.name) }}</div>
      <div v-if="attachView.size != null" class="muted" style="margin-bottom:6px">大小：{{ fmtSize(attachView.size) }}
      </div>
      <div v-if="attachView.chars" class="muted" style="margin-bottom:6px">解析字数：{{ attachView.chars }} 字</div>
      <div v-if="!attachView.file" class="muted" style="margin-top:10px">
        <i class="ti ti-database"></i> 原文件已存入知识库，可在 记忆中心 → 知识库 中查看
      </div>
    </div>
    <template #footer>
      <button v-if="attachView.type === 'file' && attachView.file" class="btn-primary"
        @click="downloadAttachFile(attachView.file)"><i class="ti ti-download"></i> 下载</button>
      <button @click="attachView = null">关闭</button>
    </template>
  </BaseModal>

  <!-- 反馈原因弹窗（替代原生 prompt，统一走 BaseModal） -->
  <BaseModal v-if="fbDialog" :title="fbDialog.fb === 1 ? '哪些地方做得好？' : '哪里出了问题？'" size="sm" @close="fbDialog = null">
    <div style="display:flex;flex-direction:column;gap:8px;margin:14px 0 18px">
      <button v-for="opt in (fbDialog.fb === 1 ? goodReasons : badReasons)" :key="opt.value" class="fb-reason"
        :class="{ active: fbDialog.reason === opt.value }" @click="fbDialog.reason = opt.value">{{ opt.label
        }}</button>
      <textarea v-if="fbDialog.reason === 'other'" v-model="fbDialog.custom" rows="3" placeholder="请描述你的反馈…"
        style="resize:vertical"></textarea>
    </div>
    <template #footer>
      <button @click="fbDialog = null">取消</button>
      <button class="btn-primary"
        :disabled="!fbDialog.reason || (fbDialog.reason === 'other' && !fbDialog.custom.trim())"
        @click="submitFeedback">提交</button>
    </template>
  </BaseModal>

  <!-- HTML 代码预览抽屉 -->
  <transition name="kg-drawer">
    <div v-if="htmlPreview" class="html-preview-drawer" :class="{ fullscreen: htmlFullscreen }">
      <div class="html-preview-head">
        <span style="font-weight:600">HTML 预览</span>
        <div class="fg" style="gap:6px">
          <button class="mermaid-btn" @click="htmlFullscreen = !htmlFullscreen" :title="htmlFullscreen ? '退出全屏' : '全屏'">
            <i class="ti" :class="htmlFullscreen ? 'ti-minimize' : 'ti-maximize'"></i> {{ htmlFullscreen ? '退出全屏' : '全屏'
            }}
          </button>
          <button class="mermaid-btn" @click="htmlPreview = null; htmlFullscreen = false" title="关闭"><i
              class="ti ti-x"></i></button>
        </div>
      </div>
      <iframe class="html-preview-iframe" :srcdoc="htmlPreview" sandbox="allow-scripts"></iframe>
    </div>
  </transition>
</template>

<style scoped>
.chat-project-chip {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 10px;
  background: var(--bg-input, rgba(127,127,127,0.08));
  border: 1px solid var(--stroke);
  border-radius: 12px;
  font-size: 12px; color: var(--muted);
  align-self: flex-start;
  max-width: fit-content;
}
.chat-project-chip {
  padding: 4px 10px;
  background: var(--bg-input, rgba(127,127,127,0.08));
  border: 1px solid var(--stroke);
  border-radius: 12px;
  font-size: 12px; color: var(--muted);
}
.chat-project-chip .ti-folder { color: var(--acctx); }
.chat-project-chip .chip-title { color: var(--fg); font-weight: 500; }
.chat-project-chip .chip-badge-miss {
  padding: 1px 6px; border-radius: 3px;
  background: var(--warntx-bg, rgba(200,120,0,0.15));
  color: var(--warntx, #c87800); font-size: 10px;
}
</style>
