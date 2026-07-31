<script setup>
import { ref, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { marked } from 'marked'
import mermaid from 'mermaid'
import { api } from '@/api/client'
import { useSSE } from '@/composables/useSSE'
import { useToast } from '@/stores/toast'
import { useSessions } from '@/stores/sessions'
import { resolveLocation, cachedLocation } from '@/composables/useGeolocation'

// Mermaid 初始化：暗色主题，禁止自动启动（我们手动触发 run）
mermaid.initialize({ startOnLoad: false, theme: 'dark', securityLevel: 'loose' })
// 自定义 marked 代码块渲染器：mermaid 语言块输出为 <div class="mermaid">，其余照常
const originalRenderer = new marked.Renderer()
const mermaidRenderer = new marked.Renderer()
mermaidRenderer.code = function (code, lang) {
  if (lang === 'mermaid' || (typeof code === 'object' && code.lang === 'mermaid')) {
    const src = typeof code === 'object' ? code.text : code
    const escaped = src.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;')
    return `<div class="mermaid-wrap" data-source="${escaped}"><div class="mermaid-actions"><button class="mermaid-btn mermaid-copy-src" title="复制源码"><i class="ti ti-code"></i> 源码</button><button class="mermaid-btn mermaid-copy-img" title="复制图片"><i class="ti ti-photo"></i> 图片</button></div><div class="mermaid">${src}</div></div>`
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
const messages = ref([])
const input = ref('')
const generating = ref(false)
const streamText = ref('')
const thinkText = ref('')     // 思考过程（意图理解/任务拆解 + 模型原生推理）流式缓冲
const thinkOpen = ref(true)   // 思考面板展开状态：思考中展开，正文首字出现后自动折叠
const streamSrcOpen = ref(false)  // 流式回复的联网来源面板：默认收起
const streamSid = ref(null)   // 流式回复所属会话：切换会话后不再渲染/插入到其他会话
const degraded = ref(false)
const scroller = ref(null)
const ta = ref(null)          // 输入框，用于自适应高度

// 模型选择器
const providers = ref([])
const chatModelId = ref(null)
async function loadProviders() {
  const all = await api.get('/settings/providers')
  const a = await api.get('/settings/model-assignment')
  // 隐藏 embedding 专用模型（如本地 BGE-M3），它不能用于对话
  const embId = a.embedding_model?.provider_id
  providers.value = all.filter(p => p.id !== embId)
  // 若当前 chat 分配指向被隐藏的模型，回退到首个可用模型
  const cur = a.chat_model?.provider_id
  chatModelId.value = providers.value.some(p => p.id === cur) ? cur : (providers.value[0]?.id ?? null)
}
async function switchModel(pid) {
  chatModelId.value = pid
  // 仅切换对话模型，不动 agent 模型分配（设置页的精细分配不被覆盖）
  await api.put('/settings/model-assignment', { chat_model: pid })
  toast.push('success', '已切换，下一轮对话生效')
}

// 仅提示类系统通知：Web 端已在导入时用 toast 实时反馈，无需在对话流中留存横幅（含历史）
const TOAST_ONLY_NOTIF = ['doc_imported']
function stripToastNotifs(msgs) {
  return msgs.filter(m => !(m.message_type === 'system_notification'
    && TOAST_ONLY_NOTIF.includes(m.notification_type)))
}

async function openSession(sid) {
  sessStore.currentSid = sid
  const msgs = await api.get('/chat/messages?session_id=' + sid)
  // 历史消息：若用户消息含附件上下文前缀，只展示真实提问 + 附件胶囊
  for (const m of msgs) {
    if (m.role === 'user' && typeof m.content === 'string'
      && m.content.includes('\n---\n') && m.content.includes('【附件：')) {
      m.atts = extractAttachments(m.content)
      m.content = m.content.split('\n---\n').pop()
    }
  }
  messages.value = stripToastNotifs(msgs)
  scrollBottom()
  tryReattach(sid)
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
    streamText.value = ''
    thinkText.value = ''
    thinkOpen.value = true
    // 重挂后删掉尾部尚未完成的那轮用户消息渲染冗余风险低：回放事件仅重建流式区
    await sse.send({
      sessionId: sid, message: '', clientRequestId: crid,
      onEvent: (ev, data) => handleEvent(ev, data),
      onError: () => { toast.push('error', '生成失败'); finishStream() },
    })
  } catch { /* 无进行中请求或接口异常：静默跳过 */ }
}
// 从历史消息的附件上下文前缀中还原各附件的名称与正文
// （粘贴文本可据此在弹窗中回看全文；文档附件仅取名称与字数）
function extractAttachments(content) {
  const head = content.split('\n---\n').slice(0, -1).join('\n---\n')
  const found = []
  const re = /【附件：([^】]+?)(?:（内容已截断）)?】\n?/g
  let m, prev = null
  while ((m = re.exec(head))) {
    if (prev) prev.text = head.slice(prev.end, m.index).replace(/\n+$/, '')
    prev = { name: m[1], end: re.lastIndex }
    found.push(prev)
  }
  if (prev) prev.text = head.slice(prev.end).replace(/\n+$/, '')
  return found.map(a => ({
    name: a.name, pasted: /^粘贴的文本/.test(a.name),
    text: a.text || '', chars: (a.text || '').length
  }))
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
function removeAttachment(i) { attachments.value.splice(i, 1) }

// ---- 附件统一点击交互：粘贴文本/图片弹窗预览，其他格式弹窗信息+下载 ----
const attachView = ref(null)   // { type: 'text'|'image'|'file', ... }
// composer 附件胶囊点击
function openAttachment(a) {
  if (a.uploading || a.error) return
  if (a.pasted) {
    attachView.value = { type: 'text', name: a.name, text: a.text, chars: a.chars, lines: a.lines }
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
    attachView.value = { type: 'text', name: att.name, text, chars: text.length, lines: text.split('\n').length }
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
function fmtSize(n) {
  if (n == null) return ''
  if (n < 1024) return n + ' B'
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB'
  return (n / 1024 / 1024).toFixed(1) + ' MB'
}
// 文档附件统一存入知识库：发送时后台异步导入，不阻塞对话
async function ingestToKb(file) {
  try {
    const fd = new FormData(); fd.append('file', file)
    const r = await api.upload('/import/document', fd)
    toast.push('success', `「${file.name}」已存入知识库，提炼 ${r.extracted} 条记忆`)
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
  // 无当前会话（新对话/欢迎页）：新建一条全新会话，不复用旧空会话，
  // 避免消息落进以前的会话记录
  if (!sessStore.currentSid) {
    const d = await api.post('/chat/session/create', {})
    sessStore.currentSid = d.session_id
    messages.value = []
  }
  // 构造发送给后端的消息：把附件解析文本作为上下文前置（不截断，完整交给模型）
  let backendMsg = text
  if (atts.length) {
    const blocks = atts.map(a => `【附件：${a.name}】\n${a.text || ''}`).join('\n\n')
    backendMsg = blocks + '\n\n---\n' + (text || '请阅读上述附件内容并回应。')
  }
  if (!backendMsg && imgs.length) backendMsg = '请看图并回应。'
  // 气泡附件：保留粘贴全文与原始 File，供发送后点击弹窗回看/下载
  const bubbleAtts = attachments.value.filter(a => !a.isImage).map(a => ({
    name: a.name, pasted: !!a.pasted,
    text: a.pasted ? a.text : undefined, file: a.file, chars: a.chars
  }))
  const bubbleImages = attachments.value.filter(a => a.isImage && a.preview).map(a => a.preview)
  messages.value.push({
    role: 'user', content: text || (imgs.length ? '' : '（已上传附件）'),
    atts: bubbleAtts, images: bubbleImages
  })
  input.value = ''
  attachments.value = []
  // 文档附件统一存入知识库：后台异步导入，不阻塞本次对话
  kbFiles.forEach(f => ingestToKb(f))
  nextTick(autoGrow)
  generating.value = true
  streamSid.value = sessStore.currentSid
  streamText.value = ''
  thinkText.value = ''
  thinkOpen.value = true
  scrollBottom()

  await sse.send({
    sessionId: sessStore.currentSid, message: backendMsg,
    images: imgs.length ? imgs : undefined,
    location: geoEnabled.value ? cachedLocation() : undefined,
    onEvent: (ev, data) => handleEvent(ev, data),
    onError: () => { toast.push('error', '生成失败'); finishStream() },
  })
}

function handleEvent(ev, data) {
  // memory_retrieved 保留事件兼容，检索结果已由后端并入 thinking_delta 展示
  if (ev === 'thinking_delta') { thinkText.value += data.text; maybeScroll(); scrollThink() }
  else if (ev === 'content_delta') {
    // 正文首字出现 → 自动折叠思考过程，开始流式输出回复正文
    if (!streamText.value && data.text) thinkOpen.value = false
    streamText.value += data.text; maybeScroll(); scrollStreamCode()
  }
  else if (ev === 'citations') lastCitations = data.refs
  else if (ev === 'queued') toast.push('info', '正在处理上一条消息')
  else if (ev === 'degrade') { degraded.value = true }
  else if (ev === 'turn_completed') finishStream(data.message_id)
  else if (ev === 'error') { toast.push('error', data.message || '出错'); finishStream() }
}

let lastCitations = []
let regenAt = null   // 原位重生成时新回复的插入位置（null = 正常追加到末尾）
function finishStream(msgId) {
  // 跨会话保护：用户已切到其他会话时不把回复插进当前列表
  //（回复已按 session 落库，切回原会话时 openSession 会重新加载）
  const sameSession = streamSid.value === sessStore.currentSid
  if (streamText.value && sameSession) {
    const m = {
      id: msgId, role: 'assistant', content: stripTail(streamText.value),
      citations: lastCitations, feedback: 0,
      thinking: thinkText.value || '', thinkOpen: false
    }
    // 原位重生成：新回复插回被移除回复的位置，而非追加新对话
    if (regenAt !== null && regenAt <= messages.value.length) messages.value.splice(regenAt, 0, m)
    else messages.value.push(m)
  }
  regenAt = null
  streamText.value = ''
  thinkText.value = ''
  thinkOpen.value = true
  lastCitations = []
  generating.value = false
  streamSid.value = null
  sessStore.load()
    // 标题由后端并行异步生成（总结首条提问），在 5s 内多次轻量轮询拉取新标题
    ;[1200, 2500, 4000].forEach(ms => setTimeout(() => sessStore.load(), ms))
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
    const msgs = await api.get('/chat/messages?session_id=' + sid)
    for (const m of msgs) {
      if (m.role === 'user' && typeof m.content === 'string'
        && m.content.includes('\n---\n') && m.content.includes('【附件：')) {
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

// 原位重新生成：后端先删除旧的一轮（回复+对应提问）再重新生成落库，
// 前端移除当前回复气泡后重发同一条提问，不追加新的对话轮次
async function regenerate(msg) {
  if (generating.value || !sessStore.currentSid) return
  if (!msg.id) { toast.push('warning', '该消息暂不支持重新生成'); return }
  const idx = messages.value.indexOf(msg)
  let userMsg = null
  for (let j = idx - 1; j >= 0; j--) {
    if (messages.value[j].role === 'user') { userMsg = messages.value[j]; break }
  }
  if (!userMsg || !userMsg.content) { toast.push('warning', '未找到对应的提问，无法重新生成'); return }
  regenAt = idx
  messages.value.splice(idx, 1)
  generating.value = true
  streamSid.value = sessStore.currentSid
  streamText.value = ''
  thinkText.value = ''
  thinkOpen.value = true
  maybeScroll()
  await sse.send({
    sessionId: sessStore.currentSid, message: userMsg.content,
    regenerateMessageId: msg.id,
    location: geoEnabled.value ? cachedLocation() : undefined,
    onEvent: (ev, data) => handleEvent(ev, data),
    onError: () => { toast.push('error', '生成失败'); finishStream() },
  })
}

function copyText(msg) { navigator.clipboard.writeText(msg.content); toast.push('success', '已复制') }

// 引用记忆点击查看详情（轻量弹窗，复用 /memory/detail）
const memDetail = ref(null)
const CONF_MAP = { strong: '强', medium: '中', low: '弱', disputed: '争议' }
const LIFE_MAP = { active: '活跃', stable: '稳定', stale: '过期', archived: '已归档', missing: '缺失' }
async function openMemory(id) {
  try { memDetail.value = await api.get('/memory/detail?id=' + id) } catch { /* api 层已提示 */ }
}
function stripTail(t) {
  // 剔除模型在正文末尾泄漏的 {"citations":[...]} / {"memory_confirm":...} JSON 声明
  let s = (t || '').replace(/\s*\{\s*"citations"\s*:\s*\[[^\]]*\]\s*\}\s*/g, '\n')
  s = s.replace(/\s*\{\s*"memory_confirm"\s*:\s*\{[^}]*\}\s*\}\s*/g, '\n')
  // 声明被挖走后残留的空代码围栏（如 ```json\n```）会渲染成空白块，一并清理
  s = s.replace(/```[a-zA-Z]*\s*```/g, '').trimEnd()
  // 模型有时输出 <antartifact type="text/html">...</antartifact> 而非代码块，转为标准 html 代码块
  // 注意：围栏必须独占一行，前后补换行，避免与正文同行导致 markdown 不识别
  s = s.replace(/<antartifact[^>]*type=["']text\/html["'][^>]*>([\s\S]*?)<\/antartifact>/gi,
    (_, content) => '\n\n```html\n' + content.trim() + '\n```\n')
  // 剔除 <tool_call>...</tool_call> 块（模型内部工具调用不应展示给用户）
  s = s.replace(/<tool_call>[\s\S]*?<\/tool_call>/gi, '')
  return s
}
function render(md) {
  // 对话内容里的链接统一新标签打开，不在当前界面跳转
  const html = marked.parse(stripTail(md))
    .replace(/<a\s/gi, '<a target="_blank" rel="noopener noreferrer" ')
  return groupSections(html)
}

// 层级分组：Markdown 渲染为平铺兄弟节点，把每个 h2/h3/h4 之后、下一个
// 同级或更高级标题之前的内容包进 section.md-sec，用缩进体现维度层级；
// 内层低级标题递归分组，形成多级缩进树
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
function renderUser(text) {
  // 用户消息：转义 HTML 防注入 → 换行保留 → URL 转为新标签打开的链接
  const esc = (text || '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  return esc
    .replace(/(https?:\/\/[^\s<]+)/g,
      '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/\n/g, '<br>')
}

function formatTime(iso) {
  if (!iso) return ''
  const d = new Date(String(iso).replace(' ', 'T'))
  if (isNaN(d.getTime())) return iso
  const now = new Date()
  const diff = (now - d) / 1000
  const pad = (n) => String(n).padStart(2, '0')
  const hm = pad(d.getHours()) + ':' + pad(d.getMinutes())
  if (diff < 60) return '刚刚'
  if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前'
  if (d.toDateString() === now.toDateString()) return '今天 ' + hm
  const y = new Date(now); y.setDate(now.getDate() - 1)
  if (d.toDateString() === y.toDateString()) return '昨天 ' + hm
  if (d.getFullYear() === now.getFullYear()) return (d.getMonth() + 1) + '/' + d.getDate()
  return d.getFullYear() + '/' + (d.getMonth() + 1) + '/' + d.getDate()
}
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
// 流式思考过程（think-body 限高 260px 内部滚动）同样吸底跟随最新内容。
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
  sessStore.currentSid = null
  messages.value = []
  streamText.value = ''
  thinkText.value = ''
  input.value = ''
  attachments.value = []
  generating.value = false
  sessStore.load()
}

// 侧栏点击历史会话 → 加载消息
function onOpenSession(e) { openSession(e.detail) }

onMounted(() => {
  sessStore.load(); loadProviders()
  window.addEventListener('sp-new-chat', resetToHome)
  window.addEventListener('sp-open-session', onOpenSession)
  // 直接从其他页面进入或刷新后恢复上次会话（currentSid 已从 localStorage 恢复）
  // → openSession 内部会调 tryReattach 续播进行中的生成，实现刷新不中断
  if (sessStore.currentSid && !messages.value.length) openSession(sessStore.currentSid)
  initGeolocation()
})
// currentSid 任何变更（openSession/send/resetToHome）同步持久化，保证刷新后可恢复
watch(() => sessStore.currentSid, (v) => {
  if (v) localStorage.setItem('sp_current_sid', v)
  else localStorage.removeItem('sp_current_sid')
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
  const btn = e.target.closest('.mermaid-copy-src, .mermaid-copy-img, .html-preview-btn, .html-download-btn, .html-copy-btn, .code-copy-btn')
  if (!btn) return
  const wrap = btn.closest('.mermaid-wrap, .html-code-wrap, .code-wrap')
  if (!wrap) return
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
    const svgData = new XMLSerializer().serializeToString(svg)
    const svgBlob = new Blob([svgData], { type: 'image/svg+xml;charset=utf-8' })
    const url = URL.createObjectURL(svgBlob)
    const img = new Image()
    img.onload = async () => {
      const canvas = document.createElement('canvas')
      canvas.width = img.naturalWidth * 2
      canvas.height = img.naturalHeight * 2
      const ctx = canvas.getContext('2d')
      ctx.scale(2, 2)
      ctx.drawImage(img, 0, 0)
      URL.revokeObjectURL(url)
      canvas.toBlob(async (blob) => {
        try {
          await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })])
          toast.push('success', '图片已复制到剪贴板')
        } catch { toast.push('error', '复制图片失败，请手动右键保存') }
      }, 'image/png')
    }
    img.src = url
  } catch { toast.push('error', '复制图片失败') }
}
document.addEventListener('click', handleMermaidActions)

// Mermaid 图表自动渲染：消息更新或流式结束后触发
watch(() => messages.value.length, () => {
  nextTick(() => { try { mermaid.run() } catch (e) { /* 忽略语法错误 */ } })
})
watch(generating, (v) => {
  if (!v) nextTick(() => { try { mermaid.run() } catch (e) { } })
})
onUnmounted(() => {
  window.removeEventListener('sp-new-chat', resetToHome)
  window.removeEventListener('sp-open-session', onOpenSession)
  document.removeEventListener('click', handleMermaidActions)
})
</script>

<template>
  <div style="display:flex;height:100vh;margin:-28px -40px;max-width:none;width:auto">
    <!-- 对话区（会话列表已合并到全局侧栏 SessionSidebar） -->
    <div style="flex:1;display:flex;flex-direction:column;min-width:0;position:relative">
      <div v-if="degraded" class="banner" style="background:var(--warnbg);color:var(--warntx);margin:16px 32px 0">
        ℹ SSE 不可用，已降级为轮询模式
      </div>
      <!-- overflow-anchor:none：禁用浏览器滚动锚定，避免内容高度变化时自动补偿 scrollTop 引发抖动 -->
      <div ref="scroller" style="flex:1;overflow-y:auto;overflow-anchor:none" @scroll.passive="onScroll">
        <div style="max-width:820px;margin:0 auto;width:100%;padding:28px 32px">
          <div v-if="!messages.length && !streamText" class="chat-hero">
            <div class="logo-lg"><i class="ti ti-brain"></i></div>
            <h2>Hi，今天从哪里开始？</h2>
            <p class="muted" style="font-size:var(--fs-md)">我是 Second Person，你的私人顾问与知识管理伙伴，记忆驱动、越用越懂你</p>
          </div>
          <div v-for="(m, i) in messages" :key="i">
            <!-- 用户气泡 -->
            <div v-if="m.role === 'user'" class="msg-user">
              <div style="max-width:78%">
                <div v-if="m.atts && m.atts.length"
                  style="display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-end;margin-bottom:6px">
                  <span v-for="(f, fi) in m.atts" :key="fi" class="attach-chip attach-click"
                    :title="f.pasted ? '点击查看全文' : '点击查看详情'" @click="openMsgAttachment(f)">
                    <i class="ti" :class="f.pasted ? 'ti-clipboard-text' : 'ti-paperclip'"></i> {{ f.name }}
                  </span>
                </div>
                <div v-if="m.images && m.images.length"
                  style="display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-end;margin-bottom:6px">
                  <img v-for="(im, ii) in m.images" :key="ii" :src="im" class="bubble-img"
                    @click="openBubbleImage(im)" />
                </div>
                <div v-if="m.content" class="bubble" style="max-width:100%" v-html="renderUser(m.content)"></div>
                <div class="msg-actions user-actions"
                  style="display:flex;justify-content:flex-end;gap:2px;margin-top:4px">
                  <i class="ti ti-copy" title="复制" @click="copyText(m)"></i>
                </div>
              </div>
            </div>
            <!-- 系统通知 -->
            <div v-else-if="m.message_type === 'system_notification'" class="banner"
              style="background:var(--brand-soft);color:var(--acctx)">
              <i class="ti ti-bell"></i> {{ m.content }}
            </div>
            <!-- AI 回复 -->
            <div v-else class="msg-ai">
              <div class="avatar"><i class="ti ti-brain"></i></div>
              <div class="body">
                <!-- 思考过程（默认折叠，点击展开） -->
                <div v-if="m.thinking" class="think-panel">
                  <div class="think-head" @click="m.thinkOpen = !m.thinkOpen">
                    <i class="ti ti-bulb"></i><span>思考过程</span>
                    <i class="ti" :class="m.thinkOpen ? 'ti-chevron-up' : 'ti-chevron-down'"></i>
                  </div>
                  <div v-show="m.thinkOpen" class="think-body">{{ m.thinking }}</div>
                </div>
                <div class="content" v-html="render(webSrc(m.content).body)"></div>
                <div v-if="webSrc(m.content).count" class="think-panel" style="margin-top:8px">
                  <div class="think-head" @click="m.srcOpen = !m.srcOpen">
                    <i class="ti ti-world"></i><span>联网来源（{{ webSrc(m.content).count }}）</span>
                    <i class="ti" :class="m.srcOpen ? 'ti-chevron-up' : 'ti-chevron-down'"></i>
                  </div>
                  <div v-show="m.srcOpen" class="content" style="padding:8px 12px 8px 34px"
                    v-html="render(webSrc(m.content).list)"></div>
                </div>
                <div v-if="m.citations && m.citations.length" class="muted" style="margin-top:8px">
                  <i class="ti ti-quote"></i> 引用记忆：<span v-for="(c, ci) in m.citations" :key="ci" class="cite-link"
                    title="点击查看记忆详情" @click="openMemory(c.id)">[{{ ci + 1 }}] {{
                      c.title || c.id }} </span>
                </div>
                <div class="msg-actions" style="margin-top:8px;display:flex;gap:2px">
                  <i class="ti ti-thumb-up" title="点赞" :style="{ color: m.feedback === 1 ? 'var(--succtx)' : '' }"
                    @click="feedback(m, 1)"></i>
                  <i class="ti ti-thumb-down" title="点踩" :style="{ color: m.feedback === 2 ? 'var(--dangtx)' : '' }"
                    @click="feedback(m, 2)"></i>
                  <i class="ti ti-copy" title="复制" @click="copyText(m)"></i>
                  <!-- 仅最后一条回复可重新生成：中途轮次重生成会导致会话上下文顺序错乱 -->
                  <i v-if="i === messages.length - 1" class="ti ti-refresh" title="重新生成" @click="regenerate(m)"></i>
                </div>
              </div>
            </div>
          </div>

          <!-- 生成中：思考过程流式展示（展开）→ 正文首字后自动折叠并流式输出正文 -->
          <!-- 生成中：仅在发起请求的会话内展示（切会话后不泄漏到其他会话） -->
          <div v-if="(generating || streamText) && streamSid === sessStore.currentSid" class="msg-ai">
            <div class="avatar"><i class="ti ti-brain"></i></div>
            <div class="body">
              <div v-if="thinkText" class="think-panel">
                <div class="think-head" @click="thinkOpen = !thinkOpen">
                  <i class="ti ti-bulb"></i><span>思考过程</span>
                  <span v-if="!streamText" class="think-live"><span
                      class="think-dots"><span></span><span></span><span></span></span></span>
                  <i class="ti" :class="thinkOpen ? 'ti-chevron-up' : 'ti-chevron-down'"></i>
                </div>
                <div v-show="thinkOpen" ref="liveThink" class="think-body">{{ thinkText }}</div>
              </div>
              <!-- 尚无任何输出：思考中占位 -->
              <div v-if="!streamText && !thinkText" class="content" style="display:flex;align-items:center;gap:8px">
                <span class="muted">思考中</span>
                <span class="think-dots"><span></span><span></span><span></span></span>
              </div>
              <div v-if="streamText" class="content streaming" v-html="render(webSrc(streamText).body)"></div>
              <div v-if="streamText && webSrc(streamText).count" class="think-panel" style="margin-top:8px">
                <div class="think-head" @click="streamSrcOpen = !streamSrcOpen">
                  <i class="ti ti-world"></i><span>联网来源（{{ webSrc(streamText).count }}）</span>
                  <i class="ti" :class="streamSrcOpen ? 'ti-chevron-up' : 'ti-chevron-down'"></i>
                </div>
                <div v-show="streamSrcOpen" class="content" style="padding:8px 12px 8px 34px"
                  v-html="render(webSrc(streamText).list)"></div>
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
        <div class="composer" :class="{ dragover: dragOver }" style="max-width:820px;margin:0 auto;position:relative"
          @dragenter.prevent="dragOver = true" @dragover.prevent="dragOver = true" @dragleave.prevent="onDragLeave"
          @drop.prevent.stop="onDrop">
          <!-- 附件条（胶囊可点击：粘贴文本/图片预览，其他格式看详情） -->
          <div v-if="attachments.length" style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px">
            <span v-for="(a, ai) in attachments" :key="ai" class="attach-chip attach-click"
              :title="a.pasted ? '点击查看全文' : a.isImage ? '点击预览' : '点击查看详情'" @click="openAttachment(a)">
              <img v-if="a.isImage && a.preview" :src="a.preview" class="attach-thumb" />
              <i v-else class="ti"
                :class="a.uploading ? 'ti-loader-2' : (a.error ? 'ti-alert-triangle' : (a.pasted ? 'ti-clipboard-text' : 'ti-paperclip'))"></i>
              {{ a.name }}
              <span v-if="a.uploading" class="muted">解析中…</span>
              <span v-else-if="a.error" class="dang">失败</span>
              <span v-else-if="a.isImage" class="muted">图片</span>
              <span v-else-if="!a.parsed" class="dang">无文本</span>
              <span v-else class="muted">{{ a.chars }} 字{{ a.truncated ? '·已截断' : '' }}</span>
              <span v-if="!a.isImage && !a.pasted && a.parsed" class="muted" title="发送后自动存入知识库"><i
                  class="ti ti-database"></i>
                入库</span>
              <i class="ti ti-x" style="cursor:pointer" @click.stop="removeAttachment(ai)"></i>
            </span>
          </div>
          <textarea ref="ta" v-model="input" placeholder="发消息给 Second Person（Enter 发送，Shift+Enter 换行，可拖入/粘贴文件）" rows="1"
            @input="autoGrow" @keydown.enter.exact.prevent="send" @paste="onPaste" @dragenter.prevent="dragOver = true"
            @dragover.prevent="dragOver = true" @drop.prevent.stop="onDrop"></textarea>
          <div class="row" style="margin-top:10px">
            <div class="fg" style="gap:8px">
              <i class="ti ti-paperclip" style="cursor:pointer;color:var(--muted);font-size:var(--icon-sm)" title="上传文件"
                @click="triggerFile"></i>
              <select v-if="providers.length" v-model="chatModelId" @change="switchModel(chatModelId)"
                style="padding:4px 8px;font-size:var(--fs-sm);max-width:140px">
                <option v-for="p in providers" :key="p.id" :value="p.id"
                  :style="{ color: p.status === 'healthy' ? 'var(--succtx)' : p.status === 'half_open' ? 'var(--warntx)' : 'var(--muted)' }">
                  <span class="dot" style="display:inline-block;width:6px;height:6px;border-radius:50%;background:"
                    :style="{ background: p.status === 'healthy' ? 'var(--succtx)' : p.status === 'unavailable' ? 'var(--dangtx)' : 'var(--warntx)' }"></span>
                  {{ p.display_name }}
                </option>
              </select>
            </div>
            <input ref="fileInput" type="file" multiple style="display:none" @change="onFilePick" />
            <button v-if="!generating" class="send-btn" @click="send"><i class="ti ti-arrow-up"></i></button>
            <button v-else class="send-btn" @click="abort"><i class="ti ti-player-stop-filled"></i></button>
          </div>
        </div>
        <div class="ai-disclaim" style="max-width:820px;margin:0 auto">内容由 AI 生成，仅供参考</div>
      </div>
    </div>
  </div>

  <!-- 引用记忆详情弹窗（点击对话中的引用打开，z-index 依记忆详情弹窗层级规范） -->
  <div v-if="memDetail" class="overlay" style="z-index:var(--z-modal-2)" @click.self="memDetail = null">
    <div class="modal modal-md">
      <div class="mt">记忆详情</div>
      <h3 class="modal-subtitle">{{ memDetail.frontmatter?.title || memDetail.id }}</h3>
      <div class="fg" style="gap:6px;margin-bottom:10px">
        <span v-if="memDetail.frontmatter?.confidence" class="badge badge-a">{{
          CONF_MAP[memDetail.frontmatter?.confidence] || memDetail.frontmatter?.confidence }}</span>
        <span v-if="memDetail.frontmatter?.lifecycle" class="badge">{{
          LIFE_MAP[memDetail.frontmatter?.lifecycle] || memDetail.frontmatter?.lifecycle }}</span>
        <span v-if="memDetail.access_count != null" class="muted">被引用 {{ memDetail.access_count }} 次</span>
      </div>
      <div class="label">摘要</div>
      <p style="color:var(--sec);margin-bottom:12px">{{ memDetail.summary }}</p>
      <div v-if="memDetail.detail" class="label">详情</div>
      <p v-if="memDetail.detail"
        style="color:var(--sec);margin-bottom:12px;white-space:pre-wrap;max-height:280px;overflow-y:auto">{{
          memDetail.detail }}</p>
      <div class="fg" style="justify-content:flex-end;gap:8px">
        <button @click="memDetail = null">关闭</button>
      </div>
    </div>
  </div>

  <!-- 附件查看弹窗：粘贴文本/图片应用内预览，其他格式信息+下载（自研弹窗，z-index 同记忆详情层级） -->
  <div v-if="attachView" class="overlay" style="z-index:var(--z-modal-2)" @click.self="attachView = null">
    <div class="modal" :class="attachView.type === 'file' ? 'modal-sm' : 'modal-lg'">
      <div class="mt">{{ attachView.type === 'text' ? '粘贴的内容' : attachView.type === 'image' ? '图片预览' : '附件详情' }}
      </div>
      <!-- 粘贴文本：全文预览 -->
      <div v-if="attachView.type === 'text'">
        <div class="muted" style="margin-bottom:10px">{{ attachView.chars }} 字 · {{ attachView.lines }} 行</div>
        <div
          style="white-space:pre-wrap;word-break:break-all;font-family:ui-monospace,Consolas,monospace;font-size:var(--fs-base);line-height:1.6;color:var(--sec);background:var(--surface-2);border:1px solid var(--bd);border-radius:12px;padding:14px;max-height:60vh;overflow-y:auto">
          {{ attachView.text }}</div>
      </div>
      <!-- 图片：应用内大图预览 -->
      <div v-else-if="attachView.type === 'image'" style="text-align:center">
        <img :src="attachView.src" style="max-width:100%;max-height:68vh;border-radius:12px" />
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
      <div class="fg" style="justify-content:flex-end;gap:8px;margin-top:16px">
        <button v-if="attachView.type === 'file' && attachView.file" class="btn-primary"
          @click="downloadAttachFile(attachView.file)"><i class="ti ti-download"></i> 下载</button>
        <button @click="attachView = null">关闭</button>
      </div>
    </div>
  </div>

  <!-- 反馈原因弹窗（自研对话框，替代原生 prompt） -->
  <div v-if="fbDialog" class="overlay" style="z-index:var(--z-confirm)" @click.self="fbDialog = null">
    <div class="modal modal-sm">
      <div class="mt">{{ fbDialog.fb === 1 ? '哪些地方做得好？' : '哪里出了问题？' }}</div>
      <div style="display:flex;flex-direction:column;gap:8px;margin:14px 0 18px">
        <button v-for="opt in (fbDialog.fb === 1 ? goodReasons : badReasons)" :key="opt.value" class="fb-reason"
          :class="{ active: fbDialog.reason === opt.value }" @click="fbDialog.reason = opt.value">{{ opt.label
          }}</button>
        <textarea v-if="fbDialog.reason === 'other'" v-model="fbDialog.custom" rows="3" placeholder="请描述你的反馈…"
          style="resize:vertical"></textarea>
      </div>
      <div class="fg" style="justify-content:flex-end;gap:8px">
        <button @click="fbDialog = null">取消</button>
        <button class="btn-primary"
          :disabled="!fbDialog.reason || (fbDialog.reason === 'other' && !fbDialog.custom.trim())"
          @click="submitFeedback">提交</button>
      </div>
    </div>
  </div>

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
      <iframe ref="previewFrame" class="html-preview-iframe" :srcdoc="htmlPreview"
        sandbox="allow-scripts allow-same-origin"></iframe>
    </div>
  </transition>
</template>
