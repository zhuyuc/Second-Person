<script setup>
import { ref, computed, onMounted, onUnmounted, onActivated, watch, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { workshopApi } from '@/api/workshop'
import { useSSE } from '@/composables/useSSE'
import { useToast } from '@/stores/toast'
import { useConfirm } from '@/stores/confirm'
import { useBusy } from '@/composables/useBusy'
import { formatTimeFull, friendlyError } from '@/utils/format'
import BaseModal from '@/components/BaseModal.vue'

const route = useRoute()
const router = useRouter()
const toast = useToast()
const confirmDlg = useConfirm()
const { busy, run } = useBusy()
const sse = useSSE()

function isBusy(...keys) {
  return keys.some((k) => busy(k))
}

const TYPE_ICONS = {
  广告片: '📣',
  'AI 短剧': '🎬',
  品牌宣传片: '🏙️',
  故事短片: '🎭',
  概念氛围片: '🌫️',
  产品演示: '🖥️',
}

const shellRef = ref(null)

const RATIO_BOX = {
  '16:9': [30, 17],
  '9:16': [17, 30],
  '1:1': [24, 24],
  '4:3': [28, 21],
  '3:4': [21, 28],
  '21:9': [33, 14],
}

const view = ref('list')
const list = ref([])
const caps = ref(null)
const cur = ref(null)
const filterType = ref('all')
const searchQ = ref('')
const rewriting = ref(false)
const rendering = ref(false)
const progressLabel = ref('')
const openMenuId = ref(null)
/** 列表页正在播放的成片 id */
const playingId = ref(null)
const aspectOpen = ref(false)
const resOpen = ref(false)

const showNew = ref(false)
const newTitle = ref('')
const newTitleErr = ref(false)
const newTitleInput = ref(null)

const typeOptions = computed(() => caps.value?.types || [])
const aspectOptions = computed(() => caps.value?.aspects || [])
const resolutionOptions = computed(() => caps.value?.resolutions || [])
const maxRefs = computed(() => caps.value?.max_refs || 6)
const durMin = computed(() => caps.value?.min_duration || 2)
const durMax = computed(() => caps.value?.max_duration || 15)
const durDefault = computed(() => caps.value?.default_duration || 5)

const canRewrite = computed(() => {
  if (!cur.value) return false
  if (rewriting.value || rendering.value || cur.value.status === 'doing') return false
  const hasScript = !!(cur.value.script || '').trim()
  const hasRefs = (cur.value.refs || []).length > 0
  return hasScript || hasRefs
})
const canRender = computed(() => {
  if (!cur.value) return false
  if (cur.value.status === 'doing' || rendering.value || rewriting.value) return false
  if (!caps.value?.configured) return false
  return !!(cur.value.script || '').trim()
})
const editorLocked = computed(
  () => rendering.value || rewriting.value || cur.value?.status === 'doing',
)

const filterChips = computed(() => {
  const all = [{ key: 'all', label: '全部', cnt: list.value.length }]
  for (const t of typeOptions.value) {
    const cnt = list.value.filter((v) => v.type === t.name).length
    if (cnt > 0) all.push({ key: t.name, label: t.name, cnt })
  }
  return all
})

const filteredList = computed(() => {
  let items = list.value
  if (filterType.value !== 'all') {
    items = items.filter((v) => v.type === filterType.value)
  }
  const q = searchQ.value.trim().toLowerCase()
  if (q) items = items.filter((v) => (v.title || '').toLowerCase().includes(q))
  return items
})

const curDuration = computed(() =>
  cur.value && cur.value.duration_sec > 0 ? cur.value.duration_sec : durDefault.value,
)

/** 成片实际时长（秒），来自视频元数据，与左侧意向滑条无关 */
const videoDurationSec = ref(null)

const curResolution = computed(() => {
  const fromParams = cur.value?.params?.resolution
  if (fromParams) return fromParams
  const opts = resolutionOptions.value
  return opts[0]?.value || '720p'
})

function onPlayerMeta(e) {
  const d = Number(e?.target?.duration)
  videoDurationSec.value = Number.isFinite(d) && d > 0 ? Math.round(d) : null
}

function typeIcon(name) {
  return TYPE_ICONS[name] || '🎬'
}

function ratioBox(aspect) {
  return RATIO_BOX[aspect] || [24, 18]
}

async function loadCaps() {
  caps.value = await workshopApi.capabilities()
}

async function loadList() {
  const d = await workshopApi.list({ limit: 100 })
  list.value = d.list || []
}

async function refreshCur() {
  if (!cur.value?.id) return
  cur.value = await workshopApi.get(cur.value.id)
}

function resetShellScroll() {
  shellRef.value?.scrollTo?.({ top: 0, behavior: 'instant' })
}

/** 列表侧轮询 doing 进度 */
let listPollTimer = null
function startListPoll() {
  if (listPollTimer) return
  listPollTimer = setInterval(async () => {
    if (!list.value.some((x) => x.status === 'doing')) return
    try {
      await loadList()
      if (cur.value?.id && (cur.value.status === 'doing' || rendering.value)) {
        const latest = list.value.find((x) => x.id === cur.value.id)
        if (latest && cur.value.id === latest.id) {
          cur.value = { ...cur.value, ...latest }
          if (latest.status !== 'doing' && !rendering.value) progressLabel.value = ''
        }
      }
    } catch {
      /* ignore */
    }
  }, 5000)
}
function stopListPoll() {
  if (listPollTimer) {
    clearInterval(listPollTimer)
    listPollTimer = null
  }
}

/** 本地卡在 doing 但流已结束时，定期与服务端对齐（防 SSE 尾包丢失） */
let reconcileTimer = null
watch(
  () => [cur.value?.id, cur.value?.status, rendering.value],
  ([id, status, busy]) => {
    if (reconcileTimer) {
      clearInterval(reconcileTimer)
      reconcileTimer = null
    }
    if (!id || status !== 'doing' || busy) return
    reconcileTimer = setInterval(async () => {
      if (!cur.value || cur.value.id !== id || rendering.value) return
      try {
        const latest = await workshopApi.get(id)
        if (cur.value?.id !== id) return
        if (latest.status !== 'doing') {
          cur.value = latest
          progressLabel.value = ''
          if (latest.status === 'failed') {
            toast.push('error', latest.error_message || '生成失败')
          }
          await loadList()
        } else {
          cur.value.progress = latest.progress
        }
      } catch {
        /* ignore */
      }
    }, 4000)
  },
)

let renderAbort = null
let patchTimer = null
let patchSeq = 0

onUnmounted(() => {
  document.removeEventListener('click', onDocClick)
  stopListPoll()
  if (reconcileTimer) {
    clearInterval(reconcileTimer)
    reconcileTimer = null
  }
  if (patchTimer) clearTimeout(patchTimer)
  if (renderAbort) renderAbort.abort()
  try {
    sse.abort()
  } catch {
    /* ignore */
  }
})

function onDocClick() {
  openMenuId.value = null
  aspectOpen.value = false
  resOpen.value = false
}

function openNew() {
  newTitle.value = ''
  newTitleErr.value = false
  showNew.value = true
  nextTick(() => newTitleInput.value?.focus?.())
}

function closeNew() {
  showNew.value = false
}

async function pickType(t) {
  const title = newTitle.value.trim()
  if (!title) {
    newTitleErr.value = true
    return
  }
  const proj = await run('create', () => workshopApi.create({ title, type: t.name }))
  if (!proj) return
  showNew.value = false
  list.value = [proj, ...list.value.filter((x) => x.id !== proj.id)]
  openDetail(proj)
  toast.push('success', `已创建「${title}」`)
}

function syncRouteToDetail(projectId) {
  const id = String(projectId || '').trim()
  if (!id) {
    if (route.params.id) router.replace({ name: 'workshop' })
    return
  }
  if (route.params.id !== id) {
    router.replace({ name: 'workshop', params: { id } })
  }
}

async function openDetail(item, { syncRoute = true } = {}) {
  stopThumbPlayback()
  openMenuId.value = null
  videoDurationSec.value = null
  try {
    cur.value = await workshopApi.get(item.id)
  } catch (e) {
    toast.push('error', friendlyError(e?.message, '视频不存在或已删除'))
    view.value = 'list'
    cur.value = null
    syncRouteToDetail('')
    return
  }
  if ((!cur.value.duration_sec || cur.value.duration_sec <= 0) && durDefault.value) {
    cur.value.duration_sec = durDefault.value
  }
  view.value = 'detail'
  progressLabel.value = ''
  if (syncRoute) syncRouteToDetail(cur.value.id)
  resetShellScroll()
}

function goList({ syncRoute = true } = {}) {
  // 出片中离开详情：后台任务继续，但不再往已清空的 cur 写字段
  stopThumbPlayback()
  view.value = 'list'
  aspectOpen.value = false
  resOpen.value = false
  if (patchTimer) {
    clearTimeout(patchTimer)
    patchTimer = null
  }
  cur.value = null
  if (syncRoute) syncRouteToDetail('')
  loadList()
  resetShellScroll()
}

/** 根据 URL 恢复详情；刷新/直链时进入对应项目 */
async function restoreFromRoute() {
  const id = String(route.params.id || '').trim()
  if (!id) {
    if (view.value === 'detail') goList({ syncRoute: false })
    return
  }
  if (cur.value?.id === id && view.value === 'detail') {
    try {
      await refreshCur()
    } catch {
      /* ignore */
    }
    return
  }
  await openDetail({ id }, { syncRoute: false })
}

onMounted(async () => {
  document.addEventListener('click', onDocClick)
  await run('load', async () => {
    await loadCaps()
    await loadList()
    await restoreFromRoute()
  })
  startListPoll()
  resetShellScroll()
})

onActivated(async () => {
  try {
    await loadList()
    await restoreFromRoute()
  } catch {
    /* ignore */
  }
  startListPoll()
  resetShellScroll()
})

watch(
  () => route.params.id,
  async (id, prev) => {
    if (id === prev) return
    await restoreFromRoute()
  },
)

function onThumbClick(v, e) {
  if (v.status === 'done' && videoSrc(v)) {
    toggleThumbPlay(v, e)
    return
  }
  openDetail(v)
}

function pauseAllThumbVideos(except) {
  document.querySelectorAll('.vthumb video').forEach((el) => {
    if (except && el === except) return
    el.pause()
    el.controls = false
  })
}

function toggleThumbPlay(v, e) {
  const thumb = e?.currentTarget?.closest?.('.vthumb') || e?.currentTarget
  const video = thumb?.querySelector?.('video')
  if (!video) return
  e?.stopPropagation?.()
  if (!video.paused && playingId.value === v.id) {
    video.pause()
    video.controls = false
    playingId.value = null
    return
  }
  pauseAllThumbVideos(video)
  video.muted = false
  video.controls = true
  video.playsInline = true
  const p = video.play()
  if (p && typeof p.catch === 'function') {
    p.catch(() => {
      video.muted = true
      return video.play()
    }).catch(() => {})
  }
  playingId.value = v.id
}

/** 未播放时点缩略图开播；播放中交给原生控件，不再二次接管 */
function onThumbVideoClick(v, e) {
  e.stopPropagation()
  if (playingId.value === v.id) return
  toggleThumbPlay(v, e)
}

function onThumbEnded(v) {
  if (playingId.value === v.id) playingId.value = null
}

function stopThumbPlayback() {
  pauseAllThumbVideos()
  playingId.value = null
}

function toggleCardMenu(ev, id) {
  ev.stopPropagation()
  openMenuId.value = openMenuId.value === id ? null : id
}

function applyProjectToList(proj) {
  if (!proj?.id) return
  const clean = Object.fromEntries(
    Object.entries(proj).filter(([, v]) => v !== undefined),
  )
  const i = list.value.findIndex((x) => x.id === clean.id)
  if (i >= 0) list.value[i] = { ...list.value[i], ...clean }
  else list.value = [clean, ...list.value]
}

async function flushPatch() {
  if (!cur.value || !patchTimer) return
  clearTimeout(patchTimer)
  patchTimer = null
  const seq = ++patchSeq
  const id = cur.value.id
  const payload = {
    title: cur.value.title,
    script: cur.value.script,
    aspect: cur.value.aspect,
    duration_sec: cur.value.duration_sec,
    refs: cur.value.refs || [],
    params: cur.value.params || {},
  }
  try {
    const updated = await workshopApi.patch(id, payload)
    if (seq === patchSeq && cur.value?.id === id) {
      cur.value = { ...cur.value, ...updated }
    }
  } catch {
    /* toast by client */
  }
}

function schedulePatch(fields) {
  if (!cur.value || editorLocked.value) return
  Object.assign(cur.value, fields)
  clearTimeout(patchTimer)
  const id = cur.value.id
  const seqBase = ++patchSeq
  patchTimer = setTimeout(async () => {
    patchTimer = null
    try {
      const payload = {}
      if ('title' in fields) payload.title = cur.value?.title
      if ('script' in fields) payload.script = cur.value?.script
      if ('aspect' in fields) payload.aspect = cur.value?.aspect
      if ('duration_sec' in fields) payload.duration_sec = cur.value?.duration_sec
      if ('refs' in fields) payload.refs = cur.value?.refs
      if ('params' in fields) payload.params = cur.value?.params || {}
      const updated = await workshopApi.patch(id, payload)
      if (seqBase === patchSeq && cur.value?.id === id) {
        cur.value = { ...cur.value, ...updated }
      }
    } catch {
      /* toast by client */
    }
  }, 400)
}

function onTitleInput(e) {
  schedulePatch({ title: e.target.value })
}
function onScriptInput(e) {
  schedulePatch({ script: e.target.value })
}
function setAspect(v) {
  aspectOpen.value = false
  schedulePatch({ aspect: v })
}
function setResolution(v) {
  resOpen.value = false
  const params = { ...(cur.value?.params || {}), resolution: v }
  schedulePatch({ params })
}
function setDuration(v) {
  schedulePatch({ duration_sec: Number(v) })
}
function toggleAspect(ev) {
  ev.stopPropagation()
  if (editorLocked.value) return
  resOpen.value = false
  aspectOpen.value = !aspectOpen.value
}
function toggleRes(ev) {
  ev.stopPropagation()
  if (editorLocked.value) return
  aspectOpen.value = false
  resOpen.value = !resOpen.value
}

/** 从 /chat-images/xxx.png 取 basename，供 image_names / PATCH refs */
function refBasename(url) {
  try {
    return String(url).split('?')[0].split('#')[0].split('/').pop() || url
  } catch {
    return url
  }
}

async function addRefFiles(ev) {
  const files = [...(ev.target.files || [])]
  ev.target.value = ''
  if (!files.length || !cur.value || editorLocked.value) return
  const remain = maxRefs.value - (cur.value.refs || []).length
  if (remain <= 0) {
    toast.push('warning', `最多 ${maxRefs.value} 张参考图`)
    return
  }
  const take = files.filter((f) => (f.type || '').startsWith('image/')).slice(0, remain)
  if (!take.length) {
    toast.push('warning', '请选择图片文件')
    return
  }
  if (files.length > take.length && take.length === remain) {
    toast.push('warning', `最多 ${maxRefs.value} 张参考图，已截取前 ${remain} 张`)
  }
  const projectId = cur.value.id
  const form = new FormData()
  for (const f of take) form.append('files', f)
  try {
    await flushPatch()
    const res = await workshopApi.uploadRefs(projectId, form)
    const proj = res?.data || res
    if (proj?.id) {
      if (cur.value?.id === projectId) cur.value = { ...cur.value, ...proj }
      applyProjectToList(proj)
    }
  } catch (e) {
    toast.push('error', e?.message || '上传参考图失败')
  }
}

function removeRef(i) {
  if (editorLocked.value) return
  const refs = [...(cur.value.refs || [])]
  refs.splice(i, 1)
  // PATCH 只传文件名 / URL，不再内联 dataURL
  schedulePatch({ refs: refs.map(refBasename) })
}

/** 与主对话 bubble 图一致：BaseModal 大图预览 */
const refImageView = ref(null)
function openRefImage(src) {
  if (!src) return
  refImageView.value = { src }
}

function buildRewriteMessage(proj) {
  const desc = proj.type_desc || ''
  const script = (proj.script || '').trim()
  const hasRefs = (proj.refs || []).length > 0
  let body = `【视频工坊·剧本优化改写】
类型：${proj.type}${desc ? `（${desc}）` : ''}
标题：${proj.title}
画幅意向：${proj.aspect || '16:9'}
时长意向：${proj.duration_sec > 0 ? proj.duration_sec : durDefault.value} 秒
`
  if (script) {
    body += `
以下是用户当前的剧本原文，请基于它做优化改写（不是从零另写一套无关内容）：
---
${script}
---
`
  } else if (hasRefs) {
    body += `
用户未写文字剧本，但提供了参考图。请结合参考图写出一段可直接用于出片的中文镜头脚本（侧重动作与运镜）。
`
  }
  body += `
要求：
- 这是「基于用户已有内容的优化改写 / 补全」
- 单一主体 + 明确动作时间线；含场景、光线、运镜、风格
- 信息密度匹配短视频时长
- 只输出脚本正文，不要调用任何工具，不要生成视频，不要解释过程
`
  return body
}

async function doRewrite() {
  if (!canRewrite.value || rewriting.value || !cur.value) return
  await flushPatch()
  const projectId = cur.value.id
  rewriting.value = true
  try {
    const { session_id } = await workshopApi.ensureSession(projectId)
    if (cur.value?.id === projectId) cur.value.session_id = session_id
    const message = buildRewriteMessage(cur.value)
    // 与主对话 keep_image_names 同源：只传已落盘文件名，服务端读盘
    const imageNames = (cur.value.refs || [])
      .map(refBasename)
      .filter((n) => n && !String(n).startsWith('data:'))
    let text = ''
    let finished = false
    const timeout = setTimeout(() => {
      if (!finished) {
        try {
          sse.abort()
        } catch {
          /* ignore */
        }
      }
    }, 180000)
    try {
      await new Promise((resolve, reject) => {
        sse
          .send({
            sessionId: session_id,
            message,
            imageNames: imageNames.length ? imageNames : undefined,
            trackActive: false,
            onEvent: (ev, data) => {
              if (ev === 'content_delta') text += data.text || ''
              else if (ev === 'content_reset') text = ''
              else if (ev === 'turn_completed') {
                finished = true
                resolve()
              } else if (ev === 'error') {
                finished = true
                reject(new Error(data.message || '代写失败'))
              }
            },
            onError: (e) => {
              finished = true
              reject(e)
            },
          })
          .then(() => {
            if (!finished) {
              finished = true
              if ((text || '').trim()) resolve()
              else reject(new Error('代写未返回有效脚本'))
            }
          })
          .catch((e) => {
            if (!finished) {
              finished = true
              reject(e)
            }
          })
      })
    } finally {
      clearTimeout(timeout)
    }
    const out = (text || '').trim()
    if (!out) {
      toast.push('warning', '代写未返回有效脚本，请重试')
      return
    }
    if (cur.value?.id === projectId) cur.value.script = out
    await workshopApi.patch(projectId, { script: out })
    toast.push('success', '已根据当前内容优化脚本')
  } catch (e) {
    if (e?.name === 'AbortError') toast.push('warning', '代写已取消或超时')
    else if (!e?.alreadyToasted) toast.push('error', friendlyError(e?.message, '代写失败'))
  } finally {
    rewriting.value = false
  }
}

async function doRender() {
  if (!canRender.value || !cur.value) return
  const projectId = cur.value.id
  await flushPatch()
  await workshopApi.patch(projectId, {
    title: cur.value.title,
    script: cur.value.script,
    aspect: cur.value.aspect,
    duration_sec: cur.value.duration_sec || durDefault.value,
    refs: cur.value.refs || [],
    params: { ...(cur.value.params || {}), resolution: curResolution.value },
  })
  if (renderAbort) renderAbort.abort()
  renderAbort = new AbortController()
  const signal = renderAbort.signal
  rendering.value = true
  if (cur.value?.id === projectId) {
    cur.value.status = 'doing'
    cur.value.progress = 5
    cur.value.error_message = null
  }
  progressLabel.value = '开始生成…'
  applyProjectToList({ id: projectId, status: 'doing', progress: 5 })
  let terminal = false
  try {
    await workshopApi.render(projectId, {
      signal,
      onEvent: (ev, data) => {
        const pid = data?.project_id || data?.project?.id || projectId
        if (ev === 'workshop_progress') {
          progressLabel.value = data.label || '生成中…'
          const prog = typeof data.progress === 'number' ? data.progress : undefined
          applyProjectToList({
            id: pid,
            status: 'doing',
            progress: prog ?? undefined,
          })
          if (cur.value?.id === pid) {
            if (prog != null) cur.value.progress = prog
            cur.value.status = 'doing'
          }
        } else if (ev === 'workshop_done') {
          terminal = true
          if (data.project) {
            applyProjectToList(data.project)
            if (cur.value?.id === pid) cur.value = data.project
          }
          progressLabel.value = ''
          toast.push('success', '视频已生成')
        } else if (ev === 'workshop_error') {
          terminal = true
          if (data.project) {
            applyProjectToList(data.project)
            if (cur.value?.id === pid) cur.value = data.project
          } else if (cur.value?.id === pid) {
            cur.value.status = data.cancelled ? 'cancelled' : 'failed'
            cur.value.error_message = data.message
          }
          progressLabel.value = ''
          if (data.cancelled) toast.push('warning', data.message || '已停止生成')
          else toast.push('error', data.message || '生成失败')
        }
      },
    })
  } catch (e) {
    if (e?.name === 'AbortError') {
      /* 主动离开/取消：靠 finally 对账 */
    } else if (!e?.alreadyToasted) {
      toast.push('error', friendlyError(e?.message, '生成失败'))
    }
  } finally {
    rendering.value = false
    try {
      const latest = await workshopApi.get(projectId)
      applyProjectToList(latest)
      if (cur.value?.id === projectId) {
        const wasDoing = cur.value.status === 'doing'
        cur.value = latest
        if (!terminal && wasDoing && latest.status === 'failed') {
          progressLabel.value = ''
          toast.push('error', latest.error_message || '生成失败')
        }
        if (!terminal && wasDoing && latest.status === 'cancelled') {
          progressLabel.value = ''
          toast.push('warning', latest.error_message || '已停止生成')
        }
        if (latest.status !== 'doing') progressLabel.value = ''
      }
    } catch {
      /* ignore */
    }
    await loadList()
  }
}

async function doCancelRender() {
  if (!cur.value?.id) return
  const projectId = cur.value.id
  try {
    const data = await workshopApi.cancel(projectId)
    if (data?.project) {
      applyProjectToList(data.project)
      if (cur.value?.id === projectId) cur.value = data.project
    }
    if (renderAbort) {
      try { renderAbort.abort() } catch { /* ignore */ }
    }
    toast.push('warning', data?.message || '已停止生成')
  } catch (e) {
    if (!e?.alreadyToasted) toast.push('error', friendlyError(e?.message, '停止失败'))
  }
}

async function removeProject(item) {
  openMenuId.value = null
  if (item.status === 'doing') {
    toast.push('warning', '生成中的视频请稍后再删除')
    return
  }
  const ok = await confirmDlg.ask({
    title: '删除视频',
    message: `删除「${item.title}」？成片文件将一并删除，不可恢复。`,
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  await run('delete', () => workshopApi.remove(item.id))
  list.value = list.value.filter((x) => x.id !== item.id)
  if (cur.value?.id === item.id) goList()
  toast.push('success', '已删除')
}

function videoSrc(p) {
  if (!p) return ''
  return p.public_url || (p.filename ? `/chat-videos/${p.filename}` : '')
}

function fmtDate(iso) {
  if (!iso) return ''
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return formatTimeFull(iso)
    const p = (n) => (n < 10 ? '0' : '') + n
    return `${d.getMonth() + 1}月${d.getDate()}日 ${p(d.getHours())}:${p(d.getMinutes())}`
  } catch {
    return iso
  }
}

watch(
  () => caps.value?.default_duration,
  (d) => {
    if (cur.value && (!cur.value.duration_sec || cur.value.duration_sec <= 0) && d) {
      cur.value.duration_sec = d
    }
  },
)

watch(
  () => (cur.value ? `${cur.value.id}:${cur.value.video_path || ''}` : ''),
  () => {
    videoDurationSec.value = null
  },
)
</script>

<template>
  <div ref="shellRef" class="ws-shell">
    <!-- 顶栏：对齐原型 header -->
    <header class="ws-header">
      <div class="page-title">
        <template v-if="view === 'list'">🎬 我的视频</template>
        <template v-else-if="cur">{{ cur.title || '视频详情' }}</template>
        <template v-else>视频详情</template>
      </div>
      <div class="h-actions">
        <button
          v-if="view === 'list'"
          type="button"
          class="btn primary"
          @click="openNew"
        >
          ＋ 新增视频
        </button>
        <button
          v-else
          type="button"
          class="btn ghost"
          @click="goList"
        >
          ← 返回列表
        </button>
      </div>
    </header>

    <!-- ===== 列表 ===== -->
    <div v-if="view === 'list'" class="list-inner">
      <div class="list-bar">
        <div class="list-filter">
          <button
            v-for="c in filterChips"
            :key="c.key"
            type="button"
            class="lf"
            :class="{ active: filterType === c.key }"
            @click="filterType = c.key"
          >
            {{ c.label }}<span class="cnt">{{ c.cnt }}</span>
          </button>
        </div>
        <div class="list-search">
          <input v-model="searchQ" type="search" placeholder="🔍 搜索视频名…" />
        </div>
      </div>

      <div v-if="!filteredList.length" class="lib-empty">
        <div class="eic">{{ list.length === 0 ? '🎬' : '🔍' }}</div>
        <div class="et">{{ list.length === 0 ? '还没有任何视频' : '没有符合条件的视频' }}</div>
        <div class="es">
          {{
            list.length === 0
              ? '点「＋ 新增视频」，从一个类型开始，把需求变成一段能生成视频的脚本。'
              : '换个关键词，或把筛选切回「全部」。'
          }}
        </div>
        <button
          v-if="list.length === 0"
          type="button"
          class="btn primary"
          @click="openNew"
        >
          ＋ 新增视频
        </button>
      </div>

      <div v-else class="list-grid">
        <div v-for="v in filteredList" :key="v.id" class="vcard">
          <div
            class="vthumb"
            :class="{ playing: playingId === v.id }"
            @click="onThumbClick(v, $event)"
          >
            <template v-if="v.status === 'doing'">
              <div class="spinner" />
              <span class="badge doing">生成中 · {{ v.progress || 0 }}%</span>
            </template>
            <template v-else-if="v.status === 'done' && videoSrc(v)">
              <video
                :src="videoSrc(v)"
                muted
                playsinline
                preload="metadata"
                @ended="onThumbEnded(v)"
                @click="onThumbVideoClick(v, $event)"
              />
              <span v-if="playingId !== v.id" class="play">▶</span>
              <span class="badge done">已完成</span>
            </template>
            <template v-else-if="v.status === 'failed'">
              <span class="cover dim">⚠️</span>
              <span class="badge failed">失败</span>
            </template>
            <template v-else>
              <span class="cover dim">{{ typeIcon(v.type) }}</span>
              <span class="badge">待创作</span>
            </template>
          </div>
          <div class="vbody">
            <div class="vhead">
              <div class="vname" :title="v.title">{{ v.title }}</div>
              <div class="vmeta">创建于 {{ fmtDate(v.created_at) }}</div>
            </div>
            <div class="vfoot">
              <span class="vtype">{{ v.type }}</span>
              <button type="button" class="vbtn" @click="openDetail(v)">查看详情</button>
            </div>
          </div>
          <button type="button" class="vmenu-btn" @click="toggleCardMenu($event, v.id)">⋯</button>
          <div class="vmenu" :class="{ open: openMenuId === v.id }" @click.stop>
            <button type="button" @click="openDetail(v)">✎ 编辑</button>
            <button type="button" class="danger" @click="removeProject(v)">🗑 删除</button>
          </div>
        </div>
      </div>
    </div>

    <!-- ===== 详情：左创作 / 右预览 ===== -->
    <div v-else-if="cur" class="detail-inner">
      <div class="detail-grid">
        <div class="card composer">
          <div class="cp-head">
            <input
              class="cp-title"
              :value="cur.title"
              title="点击可直接改名称"
              :disabled="editorLocked"
              @input="onTitleInput"
            />
            <span class="cp-type">{{ cur.type }}</span>
          </div>

          <div class="cp-block">
            <div class="cp-label">
              参考图
              <span class="cp-count">{{ (cur.refs || []).length }}/{{ maxRefs }}</span>
              <span class="cp-hint">{{
                caps?.supports_i2v
                  ? '首张作出片首帧（图生视频）；亦可用于代写'
                  : (caps?.refs_for_rewrite_only !== false
                    ? '仅用于代写，出片只吃脚本'
                    : '可用于代写与出片')
              }}</span>
            </div>
            <div class="ref-row">
              <div v-for="(r, i) in cur.refs || []" :key="i" class="ref-thumb">
                <img :src="r" alt="" title="点击放大" @click="openRefImage(r)" />
                <button type="button" class="x" :disabled="editorLocked" @click.stop="removeRef(i)">✕</button>
              </div>
              <label
                v-if="(cur.refs || []).length < maxRefs && !editorLocked"
                class="ref-add"
              >
                <span class="plus">＋</span>
                选择图片
                <input type="file" accept="image/*" multiple hidden @change="addRefFiles" />
              </label>
            </div>
          </div>

          <div class="cp-block cp-script-block">
            <div class="cp-script-fill">
              <textarea
                class="cp-script"
                :value="cur.script"
                placeholder="输入视频脚本，使用参考图，或【代为撰写】…"
                maxlength="10000"
                :disabled="editorLocked"
                @input="onScriptInput"
              />
            </div>
            <div class="cp-script-foot">
              <button
                type="button"
                class="linkbtn"
                :class="{ busy: rewriting }"
                :disabled="rewriting || !canRewrite || isBusy('create', 'load', 'delete')"
                :aria-busy="rewriting ? 'true' : undefined"
                @click="doRewrite"
              >
                <i v-if="rewriting" class="ti ti-loader-2" aria-hidden="true"></i>
                <span class="linkbtn-txt">{{ rewriting ? '正在撰写…' : '✨ 代为撰写' }}</span>
              </button>
              <span class="cp-char">{{ (cur.script || '').length }}/10000</span>
            </div>
          </div>

          <div class="cp-block">
            <div class="params-row" :class="{ 'one-col': !resolutionOptions.length }">
              <div>
                <label class="f">画面比例</label>
                <div class="sel-wrap" @click.stop>
                  <button
                    type="button"
                    class="sel-btn"
                    :class="{ open: aspectOpen }"
                    :disabled="editorLocked"
                    @click="toggleAspect"
                  >
                    <span class="sel-txt">{{ cur.aspect || '16:9' }}</span>
                    <span class="sel-caret">▾</span>
                  </button>
                  <div class="sel-menu" :class="{ open: aspectOpen }">
                    <button
                      v-for="a in aspectOptions"
                      :key="a.value"
                      type="button"
                      class="sel-opt"
                      :class="{ active: a.value === cur.aspect }"
                      @click="setAspect(a.value)"
                    >
                      <span class="ratio-box">
                        <i
                          :style="{
                            width: ratioBox(a.value)[0] + 'px',
                            height: ratioBox(a.value)[1] + 'px',
                          }"
                        />
                      </span>
                      <span>{{ a.label }}</span>
                    </button>
                    <button
                      v-if="!aspectOptions.length"
                      type="button"
                      class="sel-opt active"
                    >
                      {{ cur.aspect }}
                    </button>
                  </div>
                </div>
              </div>
              <div v-if="resolutionOptions.length">
                <label class="f">分辨率</label>
                <div class="sel-wrap" @click.stop>
                  <button
                    type="button"
                    class="sel-btn"
                    :class="{ open: resOpen }"
                    :disabled="editorLocked"
                    @click="toggleRes"
                  >
                    <span class="sel-txt">{{ curResolution }}</span>
                    <span class="sel-caret">▾</span>
                  </button>
                  <div class="sel-menu" :class="{ open: resOpen }">
                    <button
                      v-for="r in resolutionOptions"
                      :key="r.value"
                      type="button"
                      class="sel-opt"
                      :class="{ active: r.value === curResolution }"
                      @click="setResolution(r.value)"
                    >
                      <span>{{ r.label }}</span>
                    </button>
                  </div>
                </div>
              </div>
            </div>
            <div class="full-param">
              <label class="f">视频时长</label>
              <div class="range-row">
                <input
                  type="range"
                  :min="durMin"
                  :max="durMax"
                  :value="curDuration"
                  :disabled="editorLocked"
                  @input="setDuration($event.target.value)"
                />
                <span class="range-val">{{ curDuration }} 秒</span>
              </div>
            </div>
          </div>

          <div class="gen-bar">
            <button
              type="button"
              class="btn primary gen-btn"
              :disabled="!canRender || isBusy('create', 'load', 'delete') || cur.status === 'doing' || rendering"
              @click="doRender"
            >
              {{ cur.status === 'doing' || rendering ? '生成中…' : '▶ 立即生成视频' }}
            </button>
            <button
              v-if="cur.status === 'doing' || rendering"
              type="button"
              class="btn soft gen-btn"
              @click="doCancelRender"
            >
              停止生成
            </button>
          </div>
          <p v-if="!caps?.configured" class="hint-warn">{{ caps?.message }}</p>
        </div>

        <div class="card preview">
          <template v-if="cur.status === 'doing' || rendering">
            <div class="pv-title">⚙️ 正在生成视频</div>
            <div class="pv-sub">{{ progressLabel || 'AI 正在生成短片，请稍候…' }}</div>
            <div class="bigprog"><i :style="{ width: (cur.progress || 0) + '%' }" /></div>
            <div class="pv-pct">{{ cur.progress || 0 }}%</div>
          </template>
          <template v-else-if="cur.status === 'done' && videoSrc(cur)">
            <div class="pv-head">
              <div class="pv-title">✅ 视频已生成</div>
              <div class="pv-sub">{{ cur.title }}</div>
            </div>
            <div class="pv-stage">
              <video
                class="player-real"
                :src="videoSrc(cur)"
                controls
                playsinline
                @loadedmetadata="onPlayerMeta"
              />
            </div>
            <div class="pv-foot">
              <div class="pv-info">
                类型：<b>{{ cur.type }}</b>
                <span class="sep">·</span>
                画幅：<b>{{ cur.aspect }}</b>
                <span class="sep">·</span>
                时长：<b>{{ videoDurationSec != null ? `${videoDurationSec} 秒` : '读取中…' }}</b>
                <template v-if="curResolution">
                  <span class="sep">·</span>
                  分辨率：<b>{{ curResolution }}</b>
                </template>
              </div>
              <div class="pv-ops">
                <a class="btn soft sm" :href="videoSrc(cur)" download>⬇ 下载</a>
                <button type="button" class="btn soft sm" :disabled="!canRender" @click="doRender">
                  ↻ 重新生成
                </button>
              </div>
            </div>
          </template>
          <template v-else-if="cur.status === 'failed'">
            <div class="pv-title danger">生成失败</div>
            <div class="pv-sub">{{ cur.error_message || '未知错误' }}</div>
            <div class="pv-ops">
              <button type="button" class="btn primary" :disabled="!canRender" @click="doRender">
                重新生成
              </button>
            </div>
          </template>
          <template v-else>
            <div class="pv-empty">
              <div class="pv-eic">🎬</div>
              <div class="pv-et">还没有视频</div>
              <div class="pv-es">在左侧写好脚本，点「立即生成视频」，成片就会显示在这里。</div>
            </div>
          </template>
        </div>
      </div>
    </div>

    <!-- 新增弹层：挂到 body，避免被 .main 滚动容器裁切成窄栏 -->
    <Teleport to="body">
      <div v-if="showNew" class="ws-new-modal show" @click.self="closeNew">
        <div class="ws-new-panel" role="dialog" aria-modal="true" aria-label="新增视频">
          <div class="ws-new-head">
            <span>＋ 新增视频 · 先选一个类型</span>
            <button type="button" class="btn soft sm icon-only" @click="closeNew" aria-label="关闭">✕</button>
          </div>
          <p class="ws-new-hint">
            类型只定叙事范式（广告 / 短剧 / 宣传片…）。画面比例、分辨率、时长等参数，进入创作后随时可调。
          </p>
          <label class="ws-new-f">视频名称 <span class="ws-new-req">*必填</span></label>
          <input
            ref="newTitleInput"
            v-model="newTitle"
            type="text"
            class="ws-new-title"
            placeholder="给视频起个名字（必填）"
            @input="newTitleErr = false"
            @keydown.enter.prevent
          />
          <div class="ws-new-err" :class="{ show: newTitleErr }">⚠ 名称必填，先填个名字再选类型</div>
          <div class="ws-type-grid">
            <button
              v-for="t in typeOptions"
              :key="t.name"
              type="button"
              class="ws-tcard"
              @click="pickType(t)"
            >
              <div class="ic">{{ typeIcon(t.name) }}</div>
              <h3>{{ t.name }}</h3>
              <div class="desc">{{ t.desc }}</div>
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 参考图放大：与主对话 attachView 图片预览一致 -->
    <BaseModal
      v-if="refImageView"
      title="图片预览"
      size="lg"
      stacked
      @close="refImageView = null"
    >
      <div class="attach-img-wrap">
        <img
          :src="refImageView.src"
          class="attach-img"
          loading="lazy"
          decoding="async"
          alt="参考图预览"
        />
      </div>
      <template #footer>
        <button type="button" @click="refImageView = null">关闭</button>
      </template>
    </BaseModal>
  </div>
</template>

<style scoped>
/* ===== 视频工坊设计令牌（壳层 + Teleport 弹层共用）===== */
.ws-shell,
.ws-new-modal {
  --ws-bg: #0e1116;
  --ws-panel: #161b22;
  --ws-panel-2: #1c232d;
  --ws-elev: #212833;
  --ws-border: #2a323d;
  --ws-border-strong: #3a4656;
  --ws-text: #e6edf3;
  --ws-muted: #8b949e;
  --ws-faint: #5b6572;
  --ws-primary: #6d5efc;
  --ws-primary-2: #a855f7;
  --ws-primary-soft: #211d3a;
  --ws-primary-ink: #c9c2ff;
  --ws-accent: #22d3ee;
  --ws-ok: #3fb950;
  --ws-ok-bg: #16241b;
  --ws-ok-bd: #1b5d2e;
  --ws-ok-ink: #8fe6a6;
  --ws-warn: #d29922;
  --ws-warn-bg: #2a2410;
  --ws-warn-bd: #5d4a1b;
  --ws-warn-ink: #e6cf8f;
  --ws-danger: #ff9d97;
  --ws-danger-bg: #2a1515;
  --ws-danger-bd: #5d1b1b;
  --ws-thumb: linear-gradient(135deg, #2a2140, #1a2540);
  --ws-player-bg: #07070c;

  --ws-font: 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', system-ui, sans-serif;
  --ws-fs-micro: 11px;
  --ws-fs-caption: 12px;
  --ws-fs-body: 13px;
  --ws-fs-ui: 14px;
  --ws-fs-title: 18px;
  --ws-fs-display: 22px;
  --ws-lh: 1.55;
  --ws-lh-tight: 1.35;
  --ws-track: 0.01em;

  --ws-sp-1: 4px;
  --ws-sp-2: 8px;
  --ws-sp-3: 12px;
  --ws-sp-4: 16px;
  --ws-sp-5: 20px;
  --ws-sp-6: 24px;
  --ws-sp-7: 32px;
  --ws-page-x: 28px;
  --ws-page-y: 20px;

  --ws-r-xs: 4px;
  --ws-r-sm: 6px;
  --ws-r-md: 10px;
  --ws-r-lg: 12px;
  --ws-r-xl: 16px;
  --ws-r-pill: 999px;

  --ws-line: 1px solid var(--ws-border);
  --ws-line-dash: 1.5px dashed var(--ws-border);
  --ws-shadow-menu: 0 12px 28px rgba(0, 0, 0, 0.5);
  --ws-shadow-modal: 0 24px 60px rgba(0, 0, 0, 0.55);
  --ws-ease: 0.15s ease;
  --ws-ease-lift: 0.18s ease;

  /* 兼容旧变量名（模板内少量引用） */
  --bg: var(--ws-bg);
  --panel: var(--ws-panel);
  --panel2: var(--ws-panel-2);
  --border: var(--ws-border);
  --text: var(--ws-text);
  --muted: var(--ws-muted);
  --primary: var(--ws-primary);
  --primary2: var(--ws-primary-2);
  --accent: var(--ws-accent);
  --ok: var(--ws-ok);
  --warn: var(--ws-warn);
  --danger: var(--ws-danger);
  --line: var(--ws-line);
  --line-dash: var(--ws-line-dash);
}

.ws-shell {
  max-width: none;
  width: 100%;
  margin: 0;
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  background: radial-gradient(1200px 600px at 20% -10%, #1b2333 0%, var(--ws-bg) 55%);
  color: var(--ws-text);
  font: var(--ws-fs-body) / var(--ws-lh) var(--ws-font);
  letter-spacing: var(--ws-track);
  box-sizing: border-box;
}
.ws-shell *,
.ws-shell *::before,
.ws-shell *::after,
.ws-new-modal *,
.ws-new-modal *::before,
.ws-new-modal *::after {
  box-sizing: border-box;
}

.ws-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--ws-sp-4);
  padding: var(--ws-sp-4) var(--ws-page-x);
  border-bottom: var(--ws-line);
  background: rgba(14, 17, 22, 0.92);
  backdrop-filter: blur(10px);
  position: sticky;
  top: 0;
  z-index: 20;
  flex-shrink: 0;
}
.page-title {
  min-width: 0;
  font-weight: 700;
  font-size: var(--ws-fs-display);
  line-height: var(--ws-lh-tight);
  letter-spacing: 0.01em;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.h-actions {
  display: flex;
  align-items: center;
  gap: var(--ws-sp-2);
  flex-shrink: 0;
}

/* ---- 按钮体系 ---- */
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--ws-sp-2);
  border: var(--ws-line);
  background: var(--ws-panel-2);
  color: var(--ws-text);
  padding: 0 var(--ws-sp-4);
  height: 36px;
  border-radius: var(--ws-r-md);
  cursor: pointer;
  font: 600 var(--ws-fs-body) / 1 var(--ws-font);
  letter-spacing: var(--ws-track);
  text-decoration: none;
  white-space: nowrap;
  transition: border-color var(--ws-ease), background var(--ws-ease), color var(--ws-ease),
    filter var(--ws-ease), transform var(--ws-ease), opacity var(--ws-ease);
}
.btn:hover:not(:disabled) {
  border-color: var(--ws-primary);
  transform: translateY(-1px);
}
.btn.primary {
  background: linear-gradient(135deg, var(--ws-primary), var(--ws-primary-2));
  border-color: transparent;
  color: #fff;
}
.btn.primary:hover:not(:disabled) {
  filter: brightness(1.08);
  border-color: transparent;
}
.btn.ghost {
  background: transparent;
  font-weight: 500;
}
.btn.soft {
  background: transparent;
  color: var(--ws-muted);
  font-weight: 500;
}
.btn.soft:hover:not(:disabled) {
  color: var(--ws-text);
}
.btn.sm {
  height: 30px;
  padding: 0 var(--ws-sp-3);
  font-size: var(--ws-fs-caption);
  border-radius: var(--ws-r-sm);
  font-weight: 500;
}
.btn.icon-only {
  width: 30px;
  padding: 0;
}
.btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
  transform: none;
  filter: none;
}

/* ---- 列表 ---- */
.list-inner,
.detail-inner {
  max-width: 1180px;
  width: 100%;
  margin: 0 auto;
  padding: 0 var(--ws-page-x) 72px;
  flex: 1;
}
.list-inner {
  padding-top: var(--ws-sp-7);
}
.detail-inner {
  max-width: 1280px;
  padding-top: var(--ws-sp-5);
  padding-bottom: var(--ws-sp-5);
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.list-bar {
  display: flex;
  align-items: center;
  gap: var(--ws-sp-3);
  flex-wrap: wrap;
  margin: 0 0 var(--ws-sp-5);
}
.list-filter {
  display: flex;
  gap: var(--ws-sp-2);
  flex-wrap: wrap;
}
.lf {
  height: 30px;
  padding: 0 var(--ws-sp-3);
  border-radius: var(--ws-r-sm);
  border: var(--ws-line);
  background: var(--ws-panel);
  color: var(--ws-muted);
  font: 500 var(--ws-fs-caption) / 1 var(--ws-font);
  letter-spacing: var(--ws-track);
  cursor: pointer;
  transition: border-color var(--ws-ease), color var(--ws-ease), background var(--ws-ease);
}
.lf:hover {
  border-color: var(--ws-primary);
  color: var(--ws-text);
}
.lf.active {
  border-color: var(--ws-primary);
  background: var(--ws-primary-soft);
  color: var(--ws-primary-ink);
}
.lf .cnt {
  font-size: var(--ws-fs-micro);
  opacity: 0.7;
  margin-left: 4px;
  font-variant-numeric: tabular-nums;
}
.list-search {
  margin-left: auto;
  min-width: 220px;
  flex: 1 1 200px;
  max-width: 280px;
}
.list-search input,
.ws-new-title,
.cp-script,
.sel-btn {
  width: 100%;
  background: var(--ws-panel-2);
  border: var(--ws-line);
  color: var(--ws-text);
  border-radius: var(--ws-r-md);
  font: var(--ws-fs-body) / var(--ws-lh) var(--ws-font);
  letter-spacing: var(--ws-track);
  transition: border-color var(--ws-ease);
}
.list-search input {
  height: 36px;
  padding: 0 var(--ws-sp-3);
}
.list-search input:focus,
.ws-new-title:focus,
.cp-script:focus,
.sel-btn:focus-visible {
  outline: none;
  border-color: var(--ws-primary);
}
.list-search input::placeholder,
.ws-new-title::placeholder,
.cp-script::placeholder {
  color: var(--ws-faint);
}

.list-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--ws-sp-4);
}
@media (max-width: 1180px) {
  .list-grid { grid-template-columns: repeat(3, 1fr); }
}
@media (max-width: 820px) {
  .list-grid { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 520px) {
  .list-grid { grid-template-columns: 1fr; }
}

.vcard {
  border: var(--ws-line);
  border-radius: var(--ws-r-lg);
  background: var(--ws-panel-2);
  position: relative;
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
  transition: border-color var(--ws-ease-lift), transform var(--ws-ease-lift);
}
.vcard:hover {
  border-color: var(--ws-border-strong);
  transform: translateY(-2px);
}
.vthumb {
  position: relative;
  aspect-ratio: 3 / 2;
  display: grid;
  place-items: center;
  cursor: pointer;
  background: var(--ws-thumb);
  overflow: hidden;
}
.vthumb video {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.vthumb.playing video {
  object-fit: contain;
  background: #000;
}
.vthumb .cover {
  font-size: 40px;
  line-height: 1;
}
.vthumb .cover.dim {
  opacity: 0.42;
}
.vthumb .play {
  position: absolute;
  width: 44px;
  height: 44px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  background: rgba(0, 0, 0, 0.55);
  border: 1px solid rgba(255, 255, 255, 0.28);
  color: #fff;
  font-size: 15px;
  line-height: 1;
  opacity: 0;
  pointer-events: none;
  transition: opacity var(--ws-ease);
}
.vthumb:hover .play { opacity: 1; }
.vthumb.playing .badge {
  opacity: 0;
  pointer-events: none;
}

.badge {
  position: absolute;
  top: var(--ws-sp-2);
  left: var(--ws-sp-2);
  font-size: var(--ws-fs-micro);
  font-weight: 500;
  letter-spacing: 0.02em;
  padding: 3px 8px;
  border-radius: var(--ws-r-pill);
  background: rgba(0, 0, 0, 0.58);
  border: var(--ws-line);
  color: var(--ws-muted);
  line-height: 1.2;
}
.badge.done {
  background: var(--ws-ok-bg);
  border-color: var(--ws-ok-bd);
  color: var(--ws-ok-ink);
}
.badge.doing {
  background: var(--ws-warn-bg);
  border-color: var(--ws-warn-bd);
  color: var(--ws-warn-ink);
}
.badge.failed {
  background: var(--ws-danger-bg);
  border-color: var(--ws-danger-bd);
  color: var(--ws-danger);
}
.spinner {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  border: 3px solid rgba(255, 255, 255, 0.14);
  border-top-color: var(--ws-accent);
  animation: spin 1s linear infinite;
}
@keyframes spin {
  to { transform: rotate(360deg); }
}

.vbody {
  padding: var(--ws-sp-3);
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: var(--ws-sp-2);
}
.vhead {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--ws-sp-2);
  min-width: 0;
}
.vname {
  flex: 1;
  min-width: 0;
  font-size: var(--ws-fs-body);
  font-weight: 700;
  line-height: var(--ws-lh-tight);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.vmeta {
  flex-shrink: 0;
  font-size: var(--ws-fs-micro);
  color: var(--ws-muted);
  letter-spacing: 0.01em;
  white-space: nowrap;
  line-height: var(--ws-lh-tight);
}
.vfoot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--ws-sp-2);
  margin-top: auto;
  padding-top: var(--ws-sp-1);
}
.vtype {
  font-size: var(--ws-fs-micro);
  font-weight: 500;
  padding: 3px 8px;
  border-radius: var(--ws-r-pill);
  background: var(--ws-panel);
  border: var(--ws-line);
  color: var(--ws-muted);
  white-space: nowrap;
  line-height: 1.2;
}
.vbtn {
  height: 28px;
  padding: 0 10px;
  border-radius: var(--ws-r-sm);
  border: var(--ws-line);
  background: var(--ws-panel);
  color: var(--ws-text);
  font: 500 var(--ws-fs-micro) / 1 var(--ws-font);
  letter-spacing: var(--ws-track);
  cursor: pointer;
  transition: border-color var(--ws-ease), background var(--ws-ease);
}
.vbtn:hover {
  border-color: var(--ws-primary);
  background: var(--ws-elev);
}
.vmenu-btn {
  position: absolute;
  top: var(--ws-sp-2);
  right: var(--ws-sp-2);
  z-index: 3;
  width: 28px;
  height: 28px;
  padding: 0;
  margin: 0;
  border-radius: var(--ws-r-sm);
  border: var(--ws-line);
  background: rgba(0, 0, 0, 0.55);
  color: var(--ws-text);
  cursor: pointer;
  font-size: 16px;
  font-weight: 700;
  line-height: 1;
  letter-spacing: 0;
  display: grid;
  place-items: center;
  opacity: 0;
  pointer-events: none;
  transition: opacity var(--ws-ease);
  font-family: inherit;
}
.vcard:hover .vmenu-btn {
  opacity: 1;
  pointer-events: auto;
}
.vmenu {
  position: absolute;
  top: 40px;
  right: var(--ws-sp-2);
  z-index: 4;
  background: var(--ws-panel);
  border: var(--ws-line);
  border-radius: var(--ws-r-md);
  padding: var(--ws-sp-1);
  display: none;
  min-width: 132px;
  box-shadow: var(--ws-shadow-menu);
}
.vmenu.open { display: block; }
.vmenu button {
  display: block;
  width: 100%;
  text-align: left;
  background: none;
  border: none;
  color: var(--ws-text);
  font: 500 var(--ws-fs-caption) / 1.3 var(--ws-font);
  padding: 8px 10px;
  border-radius: var(--ws-r-sm);
  cursor: pointer;
  letter-spacing: var(--ws-track);
}
.vmenu button:hover { background: var(--ws-panel-2); }
.vmenu button.danger { color: var(--ws-danger); }

.lib-empty {
  text-align: center;
  padding: 64px var(--ws-sp-5);
}
.lib-empty .eic {
  font-size: 44px;
  margin-bottom: var(--ws-sp-4);
  line-height: 1;
}
.lib-empty .et {
  font-size: var(--ws-fs-ui);
  font-weight: 700;
  margin-bottom: var(--ws-sp-2);
  letter-spacing: 0.01em;
}
.lib-empty .es {
  font-size: var(--ws-fs-caption);
  color: var(--ws-muted);
  margin: 0 auto var(--ws-sp-5);
  line-height: var(--ws-lh);
  max-width: 400px;
}

/* ---- 详情 ---- */
.detail-grid {
  display: grid;
  grid-template-columns: minmax(0, 36fr) minmax(0, 64fr);
  grid-template-rows: minmax(0, 1fr);
  gap: var(--ws-sp-5);
  align-items: stretch;
  flex: 1;
  min-height: 0;
}
@media (max-width: 980px) {
  .detail-grid {
    grid-template-columns: 1fr;
    flex: none;
    min-height: auto;
  }
}
.card {
  background: var(--ws-panel);
  border: var(--ws-line);
  border-radius: var(--ws-r-lg);
  padding: var(--ws-sp-5);
}
.composer {
  display: flex;
  flex-direction: column;
  align-self: stretch;
  width: 100%;
  min-height: 0;
  overflow: hidden;
}
.preview {
  display: flex;
  flex-direction: column;
  align-self: stretch;
  min-height: 0;
  overflow: hidden;
}
@media (max-width: 980px) {
  .composer,
  .preview {
    height: auto;
    min-height: 420px;
    overflow: visible;
  }
}

.cp-head {
  display: flex;
  align-items: center;
  gap: var(--ws-sp-3);
  margin-bottom: var(--ws-sp-5);
  flex-shrink: 0;
}
.cp-title {
  flex: 1;
  min-width: 0;
  background: transparent;
  border: none;
  border-bottom: 1px dashed transparent;
  color: var(--ws-text);
  font: 700 var(--ws-fs-title) / var(--ws-lh-tight) var(--ws-font);
  letter-spacing: 0.01em;
  padding: var(--ws-sp-1) 0;
  transition: border-color var(--ws-ease);
}
.cp-title:hover:not(:disabled) { border-bottom-color: var(--ws-border); }
.cp-title:focus {
  outline: none;
  border-bottom-color: var(--ws-primary);
}
.cp-title:disabled {
  opacity: 0.65;
  cursor: not-allowed;
}
.cp-type {
  font-size: var(--ws-fs-micro);
  font-weight: 500;
  padding: 4px 10px;
  border-radius: var(--ws-r-pill);
  background: var(--ws-primary-soft);
  border: 1px solid #3d3470;
  color: var(--ws-primary-ink);
  white-space: nowrap;
  line-height: 1.2;
}
.cp-block { margin-bottom: var(--ws-sp-5); flex-shrink: 0; }
.cp-label {
  font-size: var(--ws-fs-caption);
  font-weight: 500;
  color: var(--ws-muted);
  margin-bottom: var(--ws-sp-2);
  display: flex;
  align-items: center;
  gap: var(--ws-sp-2);
  letter-spacing: 0.02em;
}
.cp-count {
  font-size: var(--ws-fs-micro);
  color: var(--ws-muted);
  font-variant-numeric: tabular-nums;
}
.cp-hint {
  margin-left: auto;
  font-size: var(--ws-fs-micro);
  color: var(--ws-muted);
  font-weight: 400;
  opacity: 0.95;
}

.ref-row {
  display: flex;
  gap: var(--ws-sp-2);
  flex-wrap: wrap;
}
.ref-add,
.ref-thumb {
  width: 80px;
  height: 80px;
  border-radius: var(--ws-r-lg);
  overflow: hidden;
}
.ref-add {
  border: var(--ws-line-dash);
  background: var(--ws-panel-2);
  color: var(--ws-muted);
  cursor: pointer;
  display: grid;
  place-items: center;
  gap: 2px;
  font-size: var(--ws-fs-micro);
  font-family: inherit;
  letter-spacing: var(--ws-track);
  transition: border-color var(--ws-ease), color var(--ws-ease);
}
.ref-add:hover {
  border-color: var(--ws-primary);
  color: var(--ws-text);
}
.ref-add .plus {
  font-size: 18px;
  line-height: 1;
}
.ref-thumb {
  border: var(--ws-line);
  background: var(--ws-thumb);
  display: grid;
  place-items: center;
  position: relative;
}
.ref-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  cursor: zoom-in;
}
.ref-thumb .x {
  position: absolute;
  top: 4px;
  right: 4px;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: rgba(14, 17, 22, 0.85);
  border: var(--ws-line);
  color: var(--ws-muted);
  cursor: pointer;
  font-size: var(--ws-fs-micro);
  line-height: 1;
  font-family: inherit;
  display: grid;
  place-items: center;
  padding: 0;
}
.ref-thumb .x:hover:not(:disabled) {
  color: var(--ws-danger);
  border-color: var(--ws-danger);
}
.ref-thumb .x:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

/* 脚本区独占 composer 剩余高度；textarea 绝对填充（避免原生 textarea 不吃 flex 高度） */
.composer .cp-script-block {
  flex: 1 1 0%;
  min-height: 0;
  margin-bottom: var(--ws-sp-4);
  display: flex;
  flex-direction: column;
}
.cp-script-fill {
  flex: 1 1 0%;
  min-height: 120px;
  position: relative;
}
.composer .cp-script {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  min-height: 0;
  max-height: none;
  padding: var(--ws-sp-3);
  border-radius: var(--ws-r-lg);
  resize: none;
  overflow-y: auto;
  box-sizing: border-box;
}
.cp-script:disabled {
  opacity: 0.65;
  cursor: not-allowed;
}
.cp-script-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: var(--ws-sp-2);
  gap: var(--ws-sp-3);
  flex-shrink: 0;
}
.linkbtn {
  background: none;
  border: none;
  color: var(--ws-accent);
  font: 500 var(--ws-fs-caption) / 1 var(--ws-font);
  letter-spacing: var(--ws-track);
  cursor: pointer;
  padding: var(--ws-sp-1) 0;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.linkbtn:hover:not(:disabled) { text-decoration: underline; }
.linkbtn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
  text-decoration: none;
}
/* 代写进行中：覆盖 disabled 淡化，给明显动态反馈 */
.linkbtn.busy:disabled {
  opacity: 1;
  cursor: wait;
  color: var(--ws-accent);
  text-shadow: 0 0 12px rgba(34, 211, 238, 0.35);
  animation: ws-rewrite-pulse 1.15s ease-in-out infinite;
}
.linkbtn.busy .ti-loader-2 {
  font-size: 14px;
  animation: spin 0.85s linear infinite;
}
.linkbtn.busy .linkbtn-txt {
  background: linear-gradient(
    90deg,
    var(--ws-accent) 0%,
    #a5f3fc 40%,
    var(--ws-accent) 80%
  );
  background-size: 200% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: ws-rewrite-shimmer 1.4s linear infinite;
}
@keyframes ws-rewrite-pulse {
  0%, 100% { filter: brightness(1); }
  50% { filter: brightness(1.25); }
}
@keyframes ws-rewrite-shimmer {
  0% { background-position: 100% 0; }
  100% { background-position: -100% 0; }
}
.cp-char {
  font-size: var(--ws-fs-micro);
  color: var(--ws-muted);
  font-variant-numeric: tabular-nums;
}

label.f,
.ws-new-f {
  display: block;
  font-size: var(--ws-fs-caption);
  font-weight: 500;
  color: var(--ws-muted);
  margin-bottom: var(--ws-sp-2);
  letter-spacing: 0.02em;
}
.params-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--ws-sp-3);
}
.params-row.one-col { grid-template-columns: 1fr; }
.full-param { margin-top: var(--ws-sp-4); }
.range-row {
  display: flex;
  align-items: center;
  gap: var(--ws-sp-3);
}
input[type='range'] {
  flex: 1;
  -webkit-appearance: none;
  appearance: none;
  height: 6px;
  border-radius: var(--ws-r-pill);
  background: var(--ws-panel-2);
  border: var(--ws-line);
}
input[type='range']:disabled { opacity: 0.5; }
input[type='range']::-webkit-slider-thumb {
  -webkit-appearance: none;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--ws-primary);
  border: 2px solid #fff;
  cursor: pointer;
}
.range-val {
  font-size: var(--ws-fs-caption);
  color: var(--ws-text);
  font-variant-numeric: tabular-nums;
  min-width: 48px;
  text-align: right;
}

.sel-wrap { position: relative; }
.sel-btn {
  display: flex;
  align-items: center;
  gap: var(--ws-sp-2);
  height: 36px;
  padding: 0 var(--ws-sp-3);
  cursor: pointer;
  text-align: left;
}
.sel-btn:hover:not(:disabled),
.sel-btn.open { border-color: var(--ws-primary); }
.sel-btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.sel-txt { flex: 1; }
.sel-caret {
  color: var(--ws-muted);
  font-size: var(--ws-fs-micro);
  transition: transform var(--ws-ease);
}
.sel-btn.open .sel-caret { transform: rotate(180deg); }
.sel-menu {
  position: absolute;
  top: calc(100% + 6px);
  left: 0;
  right: 0;
  z-index: 30;
  background: var(--ws-panel);
  border: var(--ws-line);
  border-radius: var(--ws-r-lg);
  padding: var(--ws-sp-1);
  display: none;
  box-shadow: var(--ws-shadow-menu);
  max-height: 280px;
  overflow: auto;
}
.sel-menu.open { display: block; }
.sel-opt {
  display: flex;
  align-items: center;
  gap: var(--ws-sp-2);
  width: 100%;
  text-align: left;
  background: none;
  border: none;
  color: var(--ws-text);
  font: 500 var(--ws-fs-caption) / 1.3 var(--ws-font);
  letter-spacing: var(--ws-track);
  padding: 8px 10px;
  border-radius: var(--ws-r-sm);
  cursor: pointer;
  transition: background var(--ws-ease), color var(--ws-ease);
}
.sel-opt:hover { background: var(--ws-panel-2); }
.sel-opt.active {
  background: var(--ws-primary-soft);
  color: var(--ws-primary-ink);
}
.ratio-box {
  width: 32px;
  height: 28px;
  display: grid;
  place-items: center;
  flex-shrink: 0;
}
.ratio-box i {
  display: block;
  border: 1.5px solid var(--ws-muted);
  border-radius: var(--ws-r-xs);
}
.sel-opt.active .ratio-box i {
  border-color: var(--ws-primary-ink);
  background: rgba(109, 94, 252, 0.2);
}

.gen-bar {
  display: flex;
  align-items: center;
  gap: var(--ws-sp-3);
  /* 不能用 margin-top:auto：会吃掉 flex 剩余高度，导致脚本框卡在 min-height≈120 */
  margin-top: 0;
  padding: var(--ws-sp-5) 0 0;
  border-top: var(--ws-line);
  background: var(--ws-panel);
  z-index: 2;
  flex-shrink: 0;
}
.gen-btn {
  flex: 1;
  height: 42px;
  font-size: var(--ws-fs-ui);
}
.hint-warn {
  margin: var(--ws-sp-3) 0 0;
  font-size: var(--ws-fs-caption);
  color: var(--ws-warn);
  line-height: var(--ws-lh);
}

.pv-head {
  flex-shrink: 0;
  margin-bottom: var(--ws-sp-3);
}
.pv-title {
  font-size: var(--ws-fs-ui);
  font-weight: 700;
  margin: 0 0 2px;
  letter-spacing: 0.01em;
  line-height: var(--ws-lh-tight);
}
.pv-title.danger { color: var(--ws-danger); }
.pv-sub {
  font-size: var(--ws-fs-caption);
  color: var(--ws-muted);
  margin: 0;
  line-height: var(--ws-lh);
}
/* 成片舞台：占满剩余高度，但绝不能被竖版固有高度撑破。
   视频用 max-* + auto 装进舞台，原生 controls 才会落在可见区域内。 */
.pv-stage {
  flex: 1 1 0%;
  min-height: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--ws-player-bg);
  border-radius: var(--ws-r-lg);
  border: var(--ws-line);
  overflow: hidden;
  position: relative;
}
.player-real {
  display: block;
  /* 禁止 width:100% + 固有比例把高度撑到舞台外（否则 controls 被裁掉） */
  width: auto;
  height: auto;
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  background: transparent;
  border: none;
  border-radius: 0;
}
.pv-foot {
  flex-shrink: 0;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--ws-sp-2) var(--ws-sp-3);
  margin-top: var(--ws-sp-3);
  position: relative;
  z-index: 2;
  background: var(--ws-panel);
}
.pv-info {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--ws-sp-1) var(--ws-sp-2);
  font-size: var(--ws-fs-caption);
  color: var(--ws-muted);
  line-height: var(--ws-lh);
  min-width: 0;
}
.pv-info b {
  color: var(--ws-text);
  font-weight: 600;
}
.pv-info .sep {
  color: var(--ws-faint);
  margin: 0 2px;
}
.pv-ops {
  display: flex;
  gap: var(--ws-sp-2);
  flex-wrap: wrap;
  flex-shrink: 0;
}
.bigprog {
  height: 8px;
  border-radius: var(--ws-r-pill);
  background: var(--ws-panel-2);
  overflow: hidden;
  border: var(--ws-line);
}
.bigprog i {
  display: block;
  height: 100%;
  background: linear-gradient(90deg, var(--ws-primary), var(--ws-accent));
  transition: width 0.4s ease;
}
.pv-pct {
  margin-top: var(--ws-sp-2);
  font-size: var(--ws-fs-caption);
  color: var(--ws-muted);
  font-variant-numeric: tabular-nums;
}
.pv-empty {
  flex: 1;
  text-align: center;
  padding: var(--ws-sp-7) var(--ws-sp-5);
  border: var(--ws-line-dash);
  border-radius: var(--ws-r-lg);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
}
.pv-empty .pv-eic {
  font-size: 44px;
  opacity: 0.35;
  margin-bottom: var(--ws-sp-3);
  line-height: 1;
}
.pv-empty .pv-et {
  font-size: var(--ws-fs-ui);
  font-weight: 700;
  color: var(--ws-muted);
  margin-bottom: var(--ws-sp-2);
}
.pv-empty .pv-es {
  font-size: var(--ws-fs-caption);
  color: var(--ws-muted);
  line-height: var(--ws-lh);
  max-width: 280px;
  margin: 0;
}

/* ---- 新增弹层 ---- */
.ws-new-modal {
  position: fixed;
  inset: 0;
  z-index: var(--z-modal, 1000);
  display: grid;
  place-items: center;
  background: rgba(6, 8, 12, 0.72);
  backdrop-filter: blur(6px);
  padding: var(--ws-sp-5);
  animation: ws-new-fade 0.2s ease;
  font: var(--ws-fs-body) / var(--ws-lh) var(--ws-font);
  letter-spacing: var(--ws-track);
  color: var(--ws-text);
}
@keyframes ws-new-fade {
  from { opacity: 0; }
  to { opacity: 1; }
}
.ws-new-panel {
  width: min(780px, 100%);
  background: var(--ws-panel);
  border: var(--ws-line);
  border-radius: var(--ws-r-xl);
  padding: var(--ws-sp-6);
  box-shadow: var(--ws-shadow-modal);
  max-height: 88vh;
  overflow: auto;
}
.ws-new-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--ws-sp-3);
  margin-bottom: var(--ws-sp-3);
  font-size: var(--ws-fs-ui);
  font-weight: 700;
  letter-spacing: 0.01em;
}
.ws-new-hint {
  font-size: var(--ws-fs-caption);
  color: var(--ws-muted);
  margin: 0 0 var(--ws-sp-4);
  line-height: var(--ws-lh);
}
.ws-new-req { color: var(--ws-danger); }
.ws-new-title {
  height: 40px;
  padding: 0 var(--ws-sp-3);
}
.ws-new-err {
  color: var(--ws-danger);
  font-size: var(--ws-fs-caption);
  margin: var(--ws-sp-2) 0 0;
  display: none;
}
.ws-new-err.show { display: block; }
.ws-type-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--ws-sp-3);
  margin-top: var(--ws-sp-4);
}
@media (max-width: 640px) {
  .ws-type-grid { grid-template-columns: 1fr 1fr; }
}
.ws-tcard {
  border: var(--ws-line);
  border-radius: var(--ws-r-lg);
  padding: var(--ws-sp-4);
  background: var(--ws-panel-2);
  cursor: pointer;
  transition: border-color var(--ws-ease-lift), transform var(--ws-ease-lift);
  text-align: left;
  color: var(--ws-text);
  font-family: inherit;
  letter-spacing: var(--ws-track);
}
.ws-tcard:hover {
  border-color: var(--ws-primary);
  transform: translateY(-2px);
}
.ws-tcard .ic {
  font-size: 22px;
  margin-bottom: var(--ws-sp-2);
  line-height: 1;
}
.ws-tcard h3 {
  font-size: var(--ws-fs-ui);
  margin: 0 0 var(--ws-sp-1);
  font-weight: 700;
  color: var(--ws-text);
  line-height: var(--ws-lh-tight);
}
.ws-tcard .desc {
  font-size: var(--ws-fs-micro);
  color: var(--ws-muted);
  line-height: 1.45;
}

@media (max-width: 900px) {
  .list-inner,
  .detail-inner {
    --ws-page-x: 20px;
  }
}
</style>
