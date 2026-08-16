<script setup>
import { ref, onMounted, onActivated, computed } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useToast } from '@/stores/toast'
import { useConfirm } from '@/stores/confirm'
import { useSessions } from '@/stores/sessions'
import { useBusy } from '@/composables/useBusy'
import { parseSSE } from '@/composables/useSSE'
import KnowledgeGraph from '@/components/graph/KnowledgeGraph.vue'
import BaseModal from '@/components/BaseModal.vue'
import { domainLabel, loadDomainLabels } from '@/utils/domainLabel'
import { formatTimeFull, fmtSize as fmtSizeBase, friendlyError } from '@/utils/format'
import { CONF_MAP, LIFE_MAP, SRC_MAP, ATTR_MAP, EVT_MAP, DEDUCT_MAP, eventLabel, dimStatusLabel } from '@/utils/enumLabel'

const router = useRouter()
const toast = useToast()
const confirm = useConfirm()
const { busy, run } = useBusy()
const sessStore = useSessions()
const tab = ref(0)
const tabs = ['知识图谱', '记忆列表', '时间线', '用户画像', '健康度', '知识库']

// 记忆列表
const memList = ref([])
const stats = ref({})
const keyword = ref('')
const lifecycleFilter = ref('active,stable,stale,archived,missing')
const domainFilter = ref('')
const importantOnly = ref(false)
const domains = ref([])
const detail = ref(null)

// 时间线
const timeline = ref([])
const tlType = ref('')
const tlDays = ref(30)
// 画像
const profile = ref({ dimensions: [] })
const soul = ref({ soul_core: '', soul_style: {} })
const outputStyle = ref({})
// 健康度
const health = ref(null)

async function loadList() {
  const d = await api.post('/memory/list', {
    keyword: keyword.value || undefined, lifecycle: lifecycleFilter.value,
    domain: domainFilter.value || undefined,
    important_only: importantOnly.value || undefined,
  })
  memList.value = d.list
  stats.value = d.stats
  try { domains.value = await api.get('/memory/domains') } catch { /* 静默 */ }
  loadDomainLabels() // 后端领域中文标签映射（异步，就绪后自动刷新展示）
}
// sug：从 Lint 建议行打开时传入该建议，详情弹窗内可直接采纳/忽略；其他入口打开时自动清空
async function openDetail(id, sug = null) { detailSug.value = sug; detail.value = await api.get('/memory/detail?id=' + id) }
const detailSug = ref(null)
async function acceptDetailSug() {
  const sug = detailSug.value
  detail.value = null; detailSug.value = null
  await acceptSug(sug.id || sug)
}
async function dismissDetailSug() {
  const sug = detailSug.value
  detail.value = null; detailSug.value = null
  await dismissSug(sug.id || sug)
  toast.push('success', '已忽略')
}
// 从被引用记录跳回对应对话会话
function jumpToSession(sid) {
  detail.value = null
  docDetail.value = null
  sessStore.currentSid = sid
  router.push('/chat')
  window.dispatchEvent(new CustomEvent('sp-open-session', { detail: sid }))
}
async function archive(id) { await api.post('/memory/archive', { id }); await refreshMemoryViews(); detail.value = null; toast.push('success', '已归档') }
async function restore(id) { await api.post('/memory/restore', { id }); await refreshMemoryViews(); detail.value = null; toast.push('success', '已恢复为活跃') }
async function del(id) {
  if (!await confirm.ask({ message: '永久删除该记忆及全部引用？不可恢复。', danger: true })) return
  await api.post('/memory/delete', { id }); await refreshMemoryViews(); detail.value = null; toast.push('success', '已删除')
}
async function saveAttr(field, value) {
  await api.put(`/memory/${detail.value.id}/attributes`, { [field]: value })
  await openDetail(detail.value.id); await loadList()
}

async function loadTimeline() {
  const q = `/memory/timeline?days=${tlDays.value}` + (tlType.value ? `&event_type=${tlType.value}` : '')
  timeline.value = await api.get(q)
}

// 时间线"建立引用"类事件：从 detail 中提取被关联的记忆 id，提供查看关联记忆的入口
function tlLinkTarget(t) {
  const m = /→\s*(mem_\d+)/.exec(t.detail || '')
  return m ? m[1] : null
}

// 时间线按日期分组
const timelineGroups = computed(() => {
  const groups = []
  let cur = null
  for (const t of timeline.value) {
    const day = (t.event_time || '').slice(0, 10)
    if (!cur || cur.day !== day) { cur = { day, items: [] }; groups.push(cur) }
    cur.items.push(t)
  }
  return groups
})

// 文件大小：空值显示 '-'（基础实现在 utils/format.js）
function fmtSize(n) { return fmtSizeBase(n) || '-' }

async function loadProfile() {
  try {
    const [p, s, o] = await Promise.all([
      api.get('/profile'), api.get('/soul'), api.get('/output-style'),
      // 联动刷新：SOUL 版本历史与待确认项（结果写入各自 ref）
      (async () => {
        soulHistory.value = await api.get('/soul/style/history?source=' + soulSource.value)
      })(),
      api.get('/soul/pending').then(p => { pendings.value = p }).catch(() => { }),
      loadRespStrategy(),
    ])
    profile.value = p; soul.value = s; outputStyle.value = o
  } catch { /* 任一 API 失败时保留旧数据显示 */ }
}

// 区四：响应策略偏好（v3：RESPONSE_STRATEGY.md + 待确认候选）
const respStrategy = ref({ content: '', candidates: [] })
async function loadRespStrategy() {
  try {
    const [r, q] = await Promise.all([
      api.get('/response-strategy'),
      api.get('/profile-review/pending?review_type=strategy_preference'),
    ])
    respStrategy.value = { content: r.content || '', candidates: q.list || [] }
  } catch { /* 失败保留旧数据 */ }
}
async function confirmStrategyCand(id) {
  await api.post('/profile-review/confirm', { id })
  toast.push('success', '已确认，下一轮对话生效')
  await loadRespStrategy()
}
async function rejectStrategyCand(id) {
  await api.post('/profile-review/reject', { id })
  toast.push('success', '已拒绝，60 天内不再重提同方向建议')
  await loadRespStrategy()
}

// 区三：暂停输出画像自动演化开关
async function toggleAutoEvolve() {
  const target = !outputStyle.value.auto_evolve_enabled
  await api.post('/output-style/toggle-auto', { enabled: target })
  outputStyle.value = await api.get('/output-style')
  toast.push('success', target ? '已恢复自动演化' : '已暂停自动演化（仍会采集信号，可手动提炼）')
}

// SOUL 版本历史 / diff / 回滚 / pending 确认
const soulSource = ref('dialog')
const soulHistory = ref([])
const diffData = ref(null)
const pendings = ref([])
async function loadSoulHistory() {
  soulHistory.value = await api.get('/soul/style/history?source=' + soulSource.value)
}
async function switchSoulSource(s) { soulSource.value = s; await loadSoulHistory() }
// 当前所选序列负责的段落及其当前内容（dialog：对话风格+行为原则；auto：输出样式）
const currentStyleSections = computed(() => {
  const secs = soul.value.soul_style || {}
  const names = soulSource.value === 'dialog' ? ['对话风格', '行为原则'] : ['输出样式']
  return names.map(name => ({ name, text: (secs[name] || '').trim() }))
})
async function showDiff(v) {
  diffData.value = await api.get(`/soul/style/diff?source=${soulSource.value}&from_=${v - 1}&to=${v}`)
}
async function rollback(v) {
  if (!await confirm.ask({ message: `回滚 ${soulSource.value} 序列到 v${v}？只影响该序列负责的段落。` })) return
  await api.post('/soul/style/rollback', { source: soulSource.value, version: v })
  await loadProfile(); toast.push('success', '已回滚')
}
async function confirmPending(pid, approved) {
  await api.post('/soul/pending/confirm', { pending_id: pid, approved })
  pendings.value = await api.get('/soul/pending')
  toast.push('success', approved ? '已确认，本次对话开始生效' : '已忽略')
}
async function loadHealth() { health.value = await api.get('/memory/health') }

// 重复检测：并排对比两条疑似重复记忆（携建议对象，弹窗内可直接裁决）
const dupCompare = ref(null)
async function openDupCompare(sug) {
  const [a, b] = await Promise.all([
    api.get('/memory/detail?id=' + sug.memory_a.id),
    api.get('/memory/detail?id=' + sug.memory_b.id),
  ])
  dupCompare.value = { a, b, sug }
}
// 重复裁决（与矛盾处理四选一对齐）：keep_a / keep_b / keep_both / delete_both
async function resolveDup(sug, res) {
  const labels = {
    keep_a: '已保留 A，删除 B', keep_b: '已保留 B，删除 A',
    keep_both: '已确认非重复，两条均保留', delete_both: '已删除两条重复记忆',
  }
  if (res !== 'keep_both' && !await confirm.ask({
    message: res === 'delete_both' ? '删除这两条记忆？此操作不可撤销。'
      : `保留${res === 'keep_a' ? ' A ' : ' B '}并删除另一条？此操作不可撤销。`,
    danger: true,
  })) return
  await api.post('/memory/lint/duplicates/resolve',
    { suggestion_id: sug.id || sug, resolution: res })
  dupCompare.value = null
  toast.push('success', labels[res])
  await refreshMemoryViews()
}

async function acceptSug(id) {
  const r = await api.post('/memory/lint/suggestions/accept', { suggestion_id: id })
  // 采纳 = 确认内容正确并保留，必定成功；建链只是附带动作，无相似记忆时先以独立节点保留
  if (r && r.linked === false) toast.push('success', '已采纳保留。暂无相似记忆可关联，图谱中先以独立节点存在，后续会自动补链')
  else toast.push('success', '已采纳')
  await refreshMemoryViews()
}
async function dismissSug(id) { await api.post('/memory/lint/suggestions/dismiss', { suggestion_id: id, reason: 'not_duplicate' }); await refreshMemoryViews() }
async function resolveConflict(cid, res) {
  await api.post('/memory/conflicts/resolve', { conflict_id: cid, resolution: res })
  conflictCompare.value = null
  await refreshMemoryViews()
}
// Lint 全量检查含矛盾逐对 LLM 判定，实测需十几秒以上：
// 必须有进行中状态 + 完成后按分数变化给出明确反馈，否则用户以为按钮无效
const lintRunning = ref(false)
async function runLint() {
  if (lintRunning.value) return
  lintRunning.value = true
  const prev = health.value?.health_score
  toast.push('info', '健康检查已启动，含矛盾/重复的 AI 判定，预计需要十几秒…')
  try {
    await api.post('/memory/lint/run', {})
    await refreshMemoryViews()
    const cur = health.value?.health_score
    toast.push('success', cur === prev ? `检查完成，健康度 ${cur} 分（无变化）`
      : `检查完成，健康度 ${prev} → ${cur} 分`)
  } finally {
    lintRunning.value = false
  }
}
async function buildOutputStyle() { await api.post('/output-style/build-now', {}); toast.push('success', '已触发提炼'); await loadProfile() }

// 输出样式画像手动编辑（走 PUT /output-style，带版本历史可回滚）
const showOutputStyleEdit = ref(false)
const outputStyleDraft = ref('')
function openOutputStyleEdit() {
  outputStyleDraft.value = outputStyle.value.profile_text || ''
  showOutputStyleEdit.value = true
}
async function saveOutputStyle() {
  if (!await confirm.ask({ title: '修改输出样式画像',
    message: '将覆盖当前画像内容（自动演化开启时后续仍可能自动更新）。确认保存？' })) return
  await api.put('/output-style', { content: outputStyleDraft.value })
  showOutputStyleEdit.value = false
  await loadProfile(); toast.push('success', '已保存，下次会话生效')
}
async function buildProfile() {
  const r = await api.post('/profile/build-now', {})
  if (r && r.ok) { toast.push('success', '已触发提炼，稍后刷新查看') }
  else { toast.push('info', '暂无足够记忆可提炼，继续积累中') }
  await loadProfile()
}

// SOUL_CORE 编辑（二次确认）
const showSoulCoreEdit = ref(false)
const soulCoreDraft = ref('')
function openSoulCoreEdit() { soulCoreDraft.value = soul.value.soul_core || ''; showSoulCoreEdit.value = true }
async function saveSoulCore() {
  if (!await confirm.ask({ title: '修改核心人格', message: 'SOUL_CORE 是 AI 核心人格，修改会影响全局行为。确认保存？' })) return
  await api.put('/soul/core', { content: soulCoreDraft.value })
  showSoulCoreEdit.value = false
  await loadProfile(); toast.push('success', '已保存，下次会话生效')
}
async function resetSoulCore() {
  if (!await confirm.ask({ title: '恢复默认人格', message: '将恢复 SOUL_CORE 为系统默认基线，当前自定义人格将丢失。确认？', danger: true })) return
  await api.post('/soul/core/reset', {})
  await loadProfile(); toast.push('success', '已恢复默认人格')
}

const conflicts = ref([])
const conflictCompare = ref(null)  // { conflict_id, loading, detailA, detailB }
async function loadConflicts() { conflicts.value = await api.get('/memory/conflicts') }
async function openConflictCompare(cf) {
  conflictCompare.value = { conflict_id: cf.conflict_id, loading: true, detailA: null, detailB: null, detectedAt: cf.detected_at, title: cf.title, sourceA: cf.source_a, sourceB: cf.source_b }
  // 必须通过 conflictCompare.value（reactive 代理）写属性，直接改原始对象不会触发视图更新
  const state = conflictCompare.value
  try {
    const [a, b] = await Promise.all([
      cf.source_a?.memory_id ? api.get('/memory/detail?id=' + cf.source_a.memory_id) : Promise.resolve(null),
      cf.source_b?.memory_id ? api.get('/memory/detail?id=' + cf.source_b.memory_id) : Promise.resolve(null),
    ])
    state.detailA = a; state.detailB = b
  } finally { state.loading = false }
}

// 记忆增删改后统一刷新受影响的数据源（列表 / 健康度 / 矛盾 / 时间线），避免同 Tab 或跨 Tab 数据陈旧
async function refreshMemoryViews() {
  await Promise.all([loadList(), loadHealth(), loadConflicts(), loadTimeline()])
}

// 用户画像维度：预览文本 + 点击详情弹窗
const dimDetail = ref(null)
function openDimDetail(d) { dimDetail.value = d }
function dimText(d) {
  if (!d.items || !d.items.length) return '（暂无内容，可点"手动提炼"生成）'
  return d.items.map(it => it.text).join('；')
}

// 知识库文档：上传 / 列表 / 删除
const docs = ref([])
const docUploading = ref(false)
const docProgress = ref(null)   // { filename, index, totalFiles, stage, current, total }
const docQueue = ref([])        // 多文件导入队列：[{ name, status: pending|processing|done|failed }]
const docFileInput = ref(null)
const docDetail = ref(null)
async function loadDocs() { docs.value = await api.get('/import/documents') }
async function openDocDetail(id) { docDetail.value = await api.get('/import/documents/' + id) }
function triggerDocPick() { docFileInput.value && docFileInput.value.click() }
async function onDocPick(e) { await uploadDocs(e.target.files); e.target.value = '' }
function docStageLabel(p) {
  if (!p) return ''
  if (p.stage === 'extracting') return '解析文本中…'
  if (p.stage === 'chunked') return '准备提炼…'
  if (p.stage === 'distilling') return `提炼记忆 ${p.current}/${p.total}`
  return '处理中…'
}
// 多文件导入总进度（%）：已完成文件数 + 当前文件按阶段折算
const docOverallPct = computed(() => {
  const p = docProgress.value
  if (!p || !p.totalFiles) return 0
  let frac = 0.05
  if (p.stage === 'chunked') frac = 0.35
  else if (p.stage === 'distilling') frac = p.total ? 0.35 + 0.6 * (p.current / p.total) : 0.35
  return Math.min(100, Math.round(((p.index - 1) + frac) / p.totalFiles * 100))
})
function docQueueIcon(s) {
  if (s === 'processing') return 'ti-loader-2'
  if (s === 'done') return 'ti-circle-check'
  if (s === 'failed') return 'ti-circle-x'
  return 'ti-clock'
}
// 单文件流式导入：读 SSE 进度事件，逐阶段回调 onEvent(event, data)
async function uploadDocStream(file, onEvent) {
  const fd = new FormData(); fd.append('file', file)
  const resp = await fetch('/api/import/document/stream', { method: 'POST', body: fd })
  if (!resp.ok || !resp.body) throw new Error('上传失败')
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const parts = buffer.split(/\r?\n\r?\n/)
    buffer = parts.pop()
    for (const part of parts) {
      const evt = parseSSE(part)
      if (evt) onEvent(evt.event, evt.data)
    }
  }
}
async function uploadDocs(fileList) {
  const files = Array.from(fileList || [])
  if (!files.length || docUploading.value) return
  docUploading.value = true
  docQueue.value = files.map(f => ({ name: f.name, status: 'pending' }))
  let ok = 0, fail = 0
  try {
    // 逐个文件独立导入：单个失败不影响其余文件，实时进度经 SSE 展示
    for (let fi = 0; fi < files.length; fi++) {
      const f = files[fi]
      docQueue.value[fi].status = 'processing'
      docProgress.value = {
        filename: f.name, index: fi + 1, totalFiles: files.length,
        stage: 'extracting', current: 0, total: 0,
      }
      let result = null, errored = false
      try {
        await uploadDocStream(f, (event, data) => {
          if (event === 'progress') {
            docProgress.value = {
              ...docProgress.value, stage: data.stage,
              current: data.current || 0, total: data.total || 0,
            }
          } else if (event === 'done') {
            result = data
          } else if (event === 'error') {
            errored = true
            toast.push('error', friendlyError(data.message, '导入失败'))
          }
        })
      } catch { errored = true }
      if (errored || !result) { fail++; docQueue.value[fi].status = 'failed'; continue }
      ok++
      docQueue.value[fi].status = 'done'
      if (result.preview) {
        // 预览模式（已关闭静默导入）：提炼结果先进勾选确认队列
        previewQueue.value.push({
          doc_id: result.doc_id, filename: f.name, items: result.items,
          checked: result.items.map(() => true),
        })
        toast.push('info', `「${f.name}」提炼 ${result.items.length} 条，请勾选确认写入`)
      } else {
        toast.push('success', `「${f.name}」已导入，提炼 ${result.extracted} 条记忆`)
      }
    }
  } finally {
    docProgress.value = null
    docQueue.value = []
    await loadDocs()
    docUploading.value = false
    if (files.length > 1) {
      toast.push(fail ? 'warning' : 'success',
        `导入完成：成功 ${ok} 个${fail ? `，失败 ${fail} 个` : ''}`)
    }
    if (!importPreview.value) openNextPreview()
  }
}

// 预览导入确认弹窗（silent_doc_import=false）
const importPreview = ref(null)   // { doc_id, filename, items, checked }
const previewQueue = ref([])
const previewSubmitting = ref(false)
function openNextPreview() { importPreview.value = previewQueue.value.shift() || null }
function previewCheckedCount() {
  const p = importPreview.value
  return p ? p.checked.filter(Boolean).length : 0
}
async function submitImportPreview(discardAll = false) {
  const p = importPreview.value
  if (!p || previewSubmitting.value) return
  previewSubmitting.value = true
  try {
    const selected = discardAll ? []
      : p.items.filter((_, i) => p.checked[i]).map(it => it.index)
    const r = await api.post(`/import/documents/${p.doc_id}/confirm`, { selected })
    toast.push('success', `已写入 ${r.written} 条记忆${r.discarded ? `，丢弃 ${r.discarded} 条` : ''}`)
    importPreview.value = null
    await Promise.all([loadDocs(), loadList()])
    openNextPreview()
  } finally {
    previewSubmitting.value = false
  }
}
async function deleteDoc(d) {
  const n = d.memory_count || 0
  const r = await confirm.ask({
    message: `删除文档「${d.filename}」？原始文件与来源溯源将被移除，且影响记忆重建完整性。`,
    danger: true,
    // 有提炼记忆时才展示级联勾选项；级联删除不可撤销，仅能靠备份找回
    checkbox: n ? `同时删除由该文档提炼的 ${n} 条记忆（重要记忆将保留，此操作不可撤销）` : '',
  })
  if (!r) return
  const cascade = !!(r && r.checked)
  const res = await api.del('/import/documents/' + d.id + (cascade ? '?cascade=true' : ''))
  await loadDocs()
  if (cascade) {
    toast.push(res.failed ? 'warning' : 'success',
      `文档已删除，连带删除 ${res.deleted_memories} 条记忆`
      + (res.kept_important ? `（保留 ${res.kept_important} 条重要记忆）` : '')
      + (res.failed ? `，${res.failed} 条删除失败` : ''))
    loadList() // 记忆列表/统计卡片同步刷新，图谱切回时按现有逻辑重拉
  } else {
    toast.push('success', '已删除')
  }
}
// 本地目录接入：添加 / 列表 / 手动扫描 / 启用暂停 / 移除 / 文件详情
const localDirs = ref([])
const localDirPath = ref('')
const localDirRecursive = ref(true)
const localDirScanning = ref(false)
const localDirFiles = ref(null)   // { dir, files } 文件列表弹窗
// 文件跟踪状态中文化（提交仍用英文值）
const LOCAL_FILE_STATUS = {
  pending: '待导入', imported: '已导入', failed: '导入失败', deleted: '源文件已移除',
}
async function loadLocalDirs() { localDirs.value = await api.get('/import/local-dirs') }
async function addLocalDir() {
  const p = localDirPath.value.trim()
  if (!p) { toast.push('warning', '请输入目录路径'); return }
  try {
    const d = await api.post('/import/local-dirs', { path: p, recursive: localDirRecursive.value })
    toast.push('success', `已接入「${d.path}」，将按扫描间隔自动提炼记忆`)
    localDirPath.value = ''
    await loadLocalDirs()
  } catch { /* api 已 toast */ }
}
async function removeLocalDir(dir) {
  const r = await confirm.ask({
    message: `移除本地目录「${dir.path}」？仅停止后续扫描，已提炼的记忆与文件副本都会保留。`,
  })
  if (!r) return
  await api.del('/import/local-dirs/' + dir.id)
  toast.push('success', '已移除，已导入内容不受影响')
  await loadLocalDirs()
}
async function toggleLocalDir(dir) {
  await api.put('/import/local-dirs/' + dir.id, { enabled: !dir.enabled })
  dir.enabled = !dir.enabled
  toast.push('info', dir.enabled ? '已恢复扫描' : '已暂停扫描')
}
async function scanLocalDirs() {
  if (localDirScanning.value) return
  localDirScanning.value = true
  try {
    const r = await api.post('/import/local-dirs/scan')
    const n = r.dirs.filter(x => !x.skipped).length
    toast.push('success', `扫描完成：处理 ${n} 个目录，新增记忆将在文档列表中展示`)
    await Promise.all([loadLocalDirs(), loadDocs()])
  } finally { localDirScanning.value = false }
}
async function openLocalDirFiles(dir) {
  localDirFiles.value = { dir, files: await api.get('/import/local-dirs/' + dir.id + '/files') }
}
function localDirSummary(dir) {
  const s = dir.summary || {}
  if (!dir.last_scan_at) return '尚未扫描，可点"立即扫描"手动触发'
  let t = `上次扫描 ${formatTimeFull(dir.last_scan_at)}`
  if (s.processed) t += ` · 最近一轮：导入 ${s.imported || 0} 个 / 提炼 ${s.memories || 0} 条`
  if (s.failed) t += ` / 失败 ${s.failed} 个`
  return t
}

function selectTab(i) {
  tab.value = i
  if (i === 1) loadList()
  else if (i === 2) loadTimeline()
  else if (i === 3) loadProfile()
  else if (i === 4) { loadHealth(); loadConflicts() }
  else if (i === 5) { loadDocs(); loadLocalDirs() }
}

onMounted(() => selectTab(0))
onActivated(() => selectTab(tab.value))
</script>

<template>
  <h1>记忆中心</h1>
  <div class="tabs-sticky">
    <div class="tabs">
      <button v-for="(t, i) in tabs" :key="i" class="tab" :class="{ active: tab === i }" @click="selectTab(i)">{{ t
      }}</button>
    </div>
  </div>

  <!-- 知识图谱 -->
  <div v-if="tab === 0">
    <KnowledgeGraph @open-memory="openDetail" />
  </div>

  <!-- 记忆列表 -->
  <div v-else-if="tab === 1">
    <div class="g4 mb-3">
      <div class="card">
        <div class="label">记忆总数</div>
        <div class="val">{{ stats.total_active + stats.total_stable + stats.total_stale || 0 }}</div>
        <div class="muted">已归档 {{ stats.total_archived || 0 }}</div>
      </div>
      <div class="card">
        <div class="label">重要记忆</div>
        <div class="val">{{ stats.important_count || 0 }}</div>
      </div>
      <div class="card">
        <div class="label">交叉引用</div>
        <div class="val">{{ stats.link_count || 0 }}</div>
      </div>
      <div class="card">
        <div class="label">健康度</div>
        <div class="val" style="color:var(--succtx)">{{ stats.health_score || 0 }}</div>
      </div>
    </div>
    <div class="filter-bar">
      <input v-model="keyword" placeholder="搜索记忆内容…" style="flex:1;min-width:180px" @keyup.enter="loadList" />
      <select v-model="domainFilter" @change="loadList">
        <option value="">全部领域</option>
        <option v-for="d in domains" :key="d.domain" :value="d.domain">{{ d.label || domainLabel(d.domain) }}（{{ d.count
        }}）</option>
      </select>
      <select v-model="lifecycleFilter" @change="loadList">
        <option value="active,stable,stale,archived,missing">全部状态</option>
        <option value="active">活跃</option>
        <option value="stable">稳定</option>
        <option value="stale">过期</option>
        <option value="archived">已归档</option>
        <option value="missing">缺失</option>
      </select>
      <label class="fg" style="gap:4px;font-size:var(--fs-base);cursor:pointer;white-space:nowrap">
        <input type="checkbox" v-model="importantOnly" @change="loadList" /> 只看重要记忆
      </label>
    </div>
    <div v-if="!memList.length" class="empty"><i class="ti ti-brain"></i>还没有记忆<br>对话中系统会自动沉淀记忆</div>
    <div v-for="m in memList" :key="m.id" class="cw list-row" @click="openDetail(m.id)">
      <div class="row">
        <div>
          <div class="list-title">{{ m.title }}</div>
          <div class="list-sub">{{ m.summary }}</div>
          <div v-if="m.lifecycle === 'missing'" class="list-sub" style="color:var(--warntx);margin-top:4px">
            <i class="ti ti-alert-triangle"></i> 文件丢失，可从备份恢复文件后自动重建，或删除此索引
          </div>
        </div>
        <div class="fg">
          <span class="badge" style="opacity:.75">{{ domainLabel(m.domain) }}</span>
          <span v-if="m.is_important" class="badge badge-g">重要</span>
          <span class="badge badge-a">{{ CONF_MAP[m.confidence] || m.confidence }}</span>
          <span class="badge" :class="m.lifecycle === 'missing' ? 'badge-r' : ''">{{ LIFE_MAP[m.lifecycle]
            || m.lifecycle }}</span>
          <span class="muted">{{ m.access_count }}次</span>
        </div>
      </div>
    </div>
  </div>

  <!-- 时间线 -->
  <div v-else-if="tab === 2">
    <div class="filter-bar">
      <button class="chip" :class="{ active: tlType === '' }" @click="tlType = ''; loadTimeline()">全部</button>
      <button v-for="(label, key) in EVT_MAP" :key="key" class="chip" :class="{ active: tlType === key }"
        @click="tlType = key; loadTimeline()">{{ label }}</button>
      <select v-model="tlDays" @change="loadTimeline" style="margin-left:auto">
        <option :value="7">近 7 天</option>
        <option :value="30">近 30 天</option>
        <option :value="90">近 90 天</option>
      </select>
    </div>
    <div v-if="!timeline.length" class="empty"><i class="ti ti-timeline"></i>还没有变更记录<br>记忆的新建、更新、合并等操作将显示在这里</div>
    <div v-for="g in timelineGroups" :key="g.day">
      <div class="tl-day">{{ g.day }}</div>
      <div v-for="(t, i) in g.items" :key="i" class="cw list-row" @click="openDetail(t.memory_id)">
        <div class="list-title">{{ t.title || t.memory_id }}</div>
        <div class="row mt-1">
          <span class="badge badge-a">{{ eventLabel(t.event_type) }}</span>
          <span class="tl-summary">{{ t.summary || t.detail }}</span>
          <button v-if="tlLinkTarget(t)" class="btn-xs" style="flex-shrink:0"
            @click.stop="openDetail(tlLinkTarget(t))"><i class="ti ti-link"></i> 查看关联</button>
          <span class="muted">{{ formatTimeFull(t.event_time) }}</span>
        </div>
      </div>
    </div>
  </div>

  <!-- 用户画像 -->
  <div v-else-if="tab === 3">
    <div class="section-row">
      <div class="section-sub">区一 · 用户画像</div>
      <button class="btn-sm" @click="buildProfile">手动提炼</button>
    </div>
    <div v-if="!profile.dimensions.length" class="empty"><i class="ti ti-user"></i>用户画像积累中<br>系统在观察你的偏好</div>
    <div class="g2">
      <div v-for="(d, i) in profile.dimensions" :key="i" class="cw list-row" @click="openDimDetail(d)">
        <div class="dim-header"><b>{{ d.name }}</b><span class="badge badge-g">{{ dimStatusLabel(d.status) }}</span></div>
        <div class="dim-preview">{{ dimText(d) }}</div>
      </div>
    </div>
    <div class="sep"></div>
    <div class="section-sub">区二 · AI 人格</div>
    <div v-if="pendings.length" class="cw card-warn">
      <b>待确认风格调整</b>
      <div v-for="p in pendings" :key="p.id" class="pending-item">
        <div class="pending-trigger">触发：{{ p.original_text }}</div>
        <div class="pending-proposal">提议：{{ p.proposed_change }}</div>
        <div class="fg mt-2" style="gap:6px">
          <button class="btn-xs" :disabled="busy('pend' + p.id)"
            @click="run('pend' + p.id, () => confirmPending(p.id, true))">确认</button>
          <button class="btn-xs" :disabled="busy('pend' + p.id)"
            @click="run('pend' + p.id, () => confirmPending(p.id, false))">忽略</button>
        </div>
      </div>
    </div>
    <div class="cw">
      <div class="row"><b>SOUL_CORE 核心人格</b>
        <div class="fg" style="gap:6px">
          <button class="btn-sm" @click="openSoulCoreEdit"><i class="ti ti-edit"></i> 编辑</button>
          <button class="btn-sm" :disabled="busy('resetSoul')" @click="run('resetSoul', resetSoulCore)"><i
              v-if="busy('resetSoul')" class="ti ti-loader-2"></i> 恢复默认</button>
        </div>
      </div>
      <pre class="mt-2" style="white-space:pre-wrap">{{ soul.soul_core }}</pre>
    </div>
    <div class="cw">
      <div class="tabs mb-3">
        <button class="tab" :class="{ active: soulSource === 'dialog' }"
          @click="switchSoulSource('dialog')">对话确认</button>
        <button class="tab" :class="{ active: soulSource === 'auto' }" @click="switchSoulSource('auto')">自动演化</button>
      </div>
      <div class="mb-3">
        <div v-for="s in currentStyleSections" :key="s.name" class="mb-3">
          <div class="style-section-label">{{ s.name }}</div>
          <pre v-if="s.text"
            style="white-space:pre-wrap;font-size:var(--fs-base);color:var(--sec);margin:0">{{ s.text }}</pre>
          <div v-else class="muted">还没有内容</div>
        </div>
      </div>
      <div v-if="!soulHistory.length" class="muted">还没有版本历史</div>
      <div v-for="h in soulHistory" :key="h.version" class="version-row">
        <div><b>v{{ h.version }}</b> <span v-if="h.current" class="badge badge-a">当前</span>
          <div class="muted">{{ h.diff_summary }}</div>
        </div>
        <div class="fg" style="gap:6px">
          <button class="btn-xs" @click="showDiff(h.version)">对比</button>
          <button v-if="!h.current" class="btn-xs" :disabled="busy('rb' + h.version)"
            @click="run('rb' + h.version, () => rollback(h.version))">回滚</button>
        </div>
      </div>
    </div>
    <div class="sep"></div>
    <div class="section-sub">区三 · 输出样式画像</div>
    <div class="cw">
      <div class="row"><b>当前画像</b>
        <div class="fg" style="gap:8px">
          <label class="fg" style="gap:4px;font-size:var(--fs-sm);cursor:pointer;white-space:nowrap">
            <input type="checkbox" :checked="outputStyle.auto_evolve_enabled" @change="toggleAutoEvolve" />
            自动演化
          </label>
          <button class="btn-sm" :disabled="busy('buildOut')" @click="run('buildOut', buildOutputStyle)"><i
              v-if="busy('buildOut')" class="ti ti-loader-2"></i> 手动提炼</button>
          <button class="btn-sm" @click="openOutputStyleEdit"><i class="ti ti-edit"></i> 编辑</button>
        </div>
      </div>
      <div class="mt-2" style="color:var(--sec)">{{ outputStyle.profile_text || '（积累中，信号数 ' +
        (outputStyle.signal_count || 0) + '/50）' }}</div>
      <div class="muted mt-3" style="font-size:var(--fs-sm)">
        已采集 {{ outputStyle.signal_count || 0 }} 条信号
        <template v-if="outputStyle.last_built"> · 上次提炼 {{ formatTimeFull(outputStyle.last_built) }}</template>
        <template v-if="outputStyle.is_cold_start"> · 首次提炼需满 50 条</template>
        <template v-else> · 新增满 {{ outputStyle.batch_threshold || 100 }} 条或到达周期自动提炼</template>
        <template v-if="!outputStyle.auto_evolve_enabled"> · <span
            style="color:var(--warntx)">自动演化已暂停（仍在采集信号）</span></template>
      </div>
    </div>
    <div class="sep"></div>
    <div class="section-sub">区四 · 响应策略偏好</div>
    <div v-if="respStrategy.candidates.length" class="cw card-warn">
      <b>待确认策略偏好</b>
      <div v-for="p in respStrategy.candidates" :key="p.id" class="pending-item">
        <div class="pending-proposal" style="font-weight:500">{{ p.title }}</div>
        <div class="pending-trigger mt-1">建议：{{ p.proposed_content }}</div>
        <div class="fg mt-2" style="gap:6px">
          <button class="btn-xs" :disabled="busy('sc' + p.id)"
            @click="run('sc' + p.id, () => confirmStrategyCand(p.id))">确认</button>
          <button class="btn-xs" :disabled="busy('sc' + p.id)"
            @click="run('sc' + p.id, () => rejectStrategyCand(p.id))">拒绝</button>
        </div>
      </div>
    </div>
    <div class="cw">
      <div class="row"><b>已确认的策略偏好</b></div>
      <pre v-if="respStrategy.content" class="mt-2" style="white-space:pre-wrap">{{ respStrategy.content }}</pre>
      <div v-else class="muted mt-2">暂无已确认偏好，系统正从你的赞踩反馈中积累（期间以行业通用默认策略兜底）</div>
    </div>
  </div>

  <!-- 健康度 -->
  <div v-else-if="tab === 4">
    <div v-if="health" class="g4 mb-4">
      <div class="card" style="text-align:center">
        <svg width="84" height="84" viewBox="0 0 84 84" style="margin:0 auto;display:block">
          <circle cx="42" cy="42" r="36" fill="none" stroke="var(--bd)" stroke-width="8" />
          <circle cx="42" cy="42" r="36" fill="none"
            :stroke="health.health_score >= 90 ? 'var(--succtx)' : health.health_score >= 70 ? 'var(--warntx)' : 'var(--dangtx)'"
            stroke-width="8" stroke-linecap="round"
            :stroke-dasharray="`${2 * Math.PI * 36 * health.health_score / 100} ${2 * Math.PI * 36}`"
            transform="rotate(-90 42 42)" />
          <text x="42" y="42" text-anchor="middle" dy="0.35em" font-size="20" font-weight="700"
            :fill="health.health_score >= 90 ? 'var(--succtx)' : health.health_score >= 70 ? 'var(--warntx)' : 'var(--dangtx)'">{{
              health.health_score }}</text>
        </svg>
        <div class="muted mt-2">健康度</div>
        <button class="btn-sm mt-2" :disabled="lintRunning" @click="runLint">
          <i v-if="lintRunning" class="ti ti-loader-2"></i> {{ lintRunning ? '检查中…' : '立即检查' }}</button>
        <div v-if="health.health_score < 100 && (health.score_breakdown || []).some(b => b.deduct > 0)"
          class="score-breakdown">
          <div v-for="b in health.score_breakdown.filter(b => b.deduct > 0)" :key="b.reason">
            {{ DEDUCT_MAP[b.reason] || b.reason }} {{ b.count }} 条，扣 {{ b.deduct }} 分
          </div>
        </div>
      </div>
      <div class="card">
        <div class="label">记忆总数</div>
        <div class="val">{{ (health.stats.total || 0) }}</div>
        <div class="muted">已归档 {{ health.stats.archived || 0 }} 单列</div>
      </div>
      <div class="card">
        <div class="label">待确认 / 矛盾</div>
        <div class="val">{{ health.stats.pending_confirm }} / <span style="color:var(--warntx)">{{
          health.stats.disputed }}</span></div>
      </div>
      <div class="card">
        <div class="label">疑似重复 / 待确认技能</div>
        <div class="val">{{ health.stats.duplicate }} / {{ health.stats.draft_skills || 0 }}</div>
      </div>
    </div>
    <!-- 矛盾处理 -->
    <div v-if="conflicts.length" class="cw">
      <div class="cw-heading">矛盾记忆 <span class="muted">{{ conflicts.length }} 条待处理</span></div>
      <div v-for="cf in conflicts" :key="cf.conflict_id" class="cw mt-2">
        <div class="list-row" @click="openConflictCompare(cf)">
          <span class="list-title">{{ cf.title }}</span>
          <span class="muted" style="font-size:var(--fs-xs);margin-left:8px"><i class="ti ti-columns"></i> 查看对比</span>
        </div>
      </div>
    </div>
    <!-- Lint 检查明细 -->
    <div v-if="health" class="cw">
      <div class="cw-heading">Lint 检查明细</div>
      <div v-for="chk in health.lint_details.filter(c => !c.actionable)" :key="chk.check" class="version-row">
        <span>{{ chk.check }} <span class="muted">· {{ chk.desc }}</span></span>
        <span class="badge" :class="[chk.status === 'ok' ? 'badge-g' : '', chk.status === 'warning' && chk.count ? 'badge-y' : '']">{{ chk.count }}</span>
      </div>
    </div>
    <div v-if="health" v-for="chk in health.lint_details.filter(c => c.actionable)" :key="chk.check" class="cw">
      <div class="cw-heading">{{ chk.check }} <span class="muted">{{ chk.desc }}（{{ chk.count }}）</span></div>
      <div v-for="sug in (chk.suggestion_ids || [])" :key="sug.id || sug" class="lint-sug-row">
        <span class="lint-sug-label">
          <template v-if="chk.check === '孤立检测'">{{ sug.title || sug }}</template>
          <template v-else-if="chk.check === '重复检测'">{{ sug.memory_a?.title || sug.memory_a?.id }} ↔ {{
            sug.memory_b?.title || sug.memory_b?.id }}</template>
          <template v-else>{{ sug.id || sug }}</template>
        </span>
        <div class="lint-sug-actions">
          <button v-if="chk.check === '孤立检测'" class="btn-xs" @click="openDetail(sug.memory_id, sug)"><i
              class="ti ti-eye"></i> 查看</button>
          <template v-if="chk.check === '重复检测'">
            <button class="btn-xs" @click="openDupCompare(sug)"><i class="ti ti-columns"></i> 对比查看</button>
          </template>
          <template v-else>
            <button class="btn-xs" :disabled="busy('sug' + (sug.id || sug))"
              @click="run('sug' + (sug.id || sug), () => acceptSug(sug.id || sug))">采纳</button>
            <button class="btn-xs" :disabled="busy('sug' + (sug.id || sug))"
              @click="run('sug' + (sug.id || sug), () => dismissSug(sug.id || sug))">忽略</button>
          </template>
        </div>
      </div>
    </div>
  </div>

  <!-- 知识库 -->
  <div v-else-if="tab === 5">
    <div class="cw mb-4">
      <div class="section-row mb-2">
        <span class="section-sub" style="margin:0">
          <i class="ti ti-folder" style="margin-right:6px;color:var(--acctx)"></i>本地目录
        </span>
        <button class="btn-sm" :disabled="localDirScanning" @click="scanLocalDirs">
          <i class="ti" :class="localDirScanning ? 'ti-loader-2' : 'ti-refresh'"></i>
          {{ localDirScanning ? '扫描中…' : '立即扫描' }}
        </button>
      </div>
      <div class="muted mb-3">
        接入常用资料目录（笔记 / PDF / 文档等），系统按扫描间隔自动提炼为知识库记忆；
        源文件只读不修改，图片默认不扫描。
      </div>
      <div class="filter-bar" style="margin-bottom:0">
        <input v-model="localDirPath" placeholder="输入本地目录绝对路径，如 D:\Documents"
          style="flex:1" @keyup.enter="addLocalDir" />
        <label class="fg" style="gap:4px;font-size:var(--fs-base);flex-shrink:0;cursor:pointer">
          <input type="checkbox" v-model="localDirRecursive" /> 包含子目录
        </label>
        <button class="btn-primary" @click="addLocalDir"><i class="ti ti-plus"></i> 添加</button>
      </div>
      <div v-for="dir in localDirs" :key="dir.id" class="dir-row">
        <div style="min-width:0;flex:1">
          <div class="doc-name">
            {{ dir.path }}
            <span class="badge" :class="dir.enabled ? 'badge-g' : ''">{{ dir.enabled ? '启用' : '已暂停' }}</span>
            <span v-if="dir.recursive" class="badge">含子目录</span>
          </div>
          <div class="doc-meta">
            {{ localDirSummary(dir) }} · 已导入 {{ dir.imported_count }}/{{ dir.file_count }} 个文件
          </div>
        </div>
        <div class="fg" style="gap:6px;flex-shrink:0">
          <button class="btn-xs" @click="openLocalDirFiles(dir)"><i class="ti ti-list"></i> 文件</button>
          <button class="btn-xs" @click="toggleLocalDir(dir)">{{ dir.enabled ? '暂停' : '启用' }}</button>
          <button class="btn-xs btn-danger" @click="removeLocalDir(dir)"><i class="ti ti-trash"></i></button>
        </div>
      </div>
      <div v-if="!localDirs.length" class="muted" style="padding:var(--sp-3) 0 var(--sp-1);text-align:center">尚未接入本地目录</div>
    </div>
    <div class="doc-drop" :class="{ uploading: docUploading }" @click="triggerDocPick" @dragover.prevent
      @drop.prevent="uploadDocs($event.dataTransfer.files)">
      <i class="ti" :class="docUploading ? 'ti-loader-2' : 'ti-cloud-upload'"></i>
      <template v-if="docProgress">
        <div class="list-title mt-2">
          正在导入「{{ docProgress.filename }}」<template v-if="docProgress.totalFiles > 1">（{{ docProgress.index }}/{{
            docProgress.totalFiles }}）</template>
        </div>
        <div class="muted mt-1">{{ docStageLabel(docProgress) }}</div>
        <template v-if="docProgress.totalFiles > 1">
          <div style="margin:var(--sp-3) auto 0;max-width:320px;display:flex;align-items:center;gap:8px">
            <div class="progress" style="flex:1">
              <div :style="{ width: docOverallPct + '%', background: 'var(--acctx)' }"></div>
            </div>
            <span class="muted" style="font-size:var(--fs-xs);min-width:32px;text-align:right">{{ docOverallPct
            }}%</span>
          </div>
          <div class="doc-queue">
            <div v-for="(q, i) in docQueue" :key="i" class="doc-queue-item" :class="q.status">
              <i class="ti" :class="docQueueIcon(q.status)"></i>
              <span class="doc-queue-name">{{ q.name }}</span>
            </div>
          </div>
        </template>
        <div v-else-if="docProgress.stage === 'distilling' && docProgress.total" class="progress"
          style="margin:var(--sp-3) auto 0;max-width:280px">
          <div :style="{ width: (docProgress.current / docProgress.total * 100) + '%', background: 'var(--acctx)' }">
          </div>
        </div>
      </template>
      <template v-else>
        <div class="list-title mt-2">点击或拖拽文件到此上传</div>
        <div class="muted">支持 PDF / DOCX / TXT / MD / 图片（PNG、JPG 等，经 VLM/OCR 提取），自动解析并提炼为知识库记忆</div>
      </template>
      <input ref="docFileInput" type="file" multiple style="display:none" @change="onDocPick" />
    </div>
    <div v-if="!docs.length" class="empty mt-4"><i
        class="ti ti-files"></i>知识库还没有文档<br>上传文档后系统会自动解析并提炼记忆
    </div>
    <div v-for="d in docs" :key="d.id" class="cw doc-row mt-3">
      <div style="min-width:0;flex:1">
        <div class="doc-name">
          <i class="ti ti-file-text" style="margin-right:6px;color:var(--acctx)"></i>{{ d.filename }}
        </div>
        <div class="doc-meta">{{ fmtSize(d.size) }} · 提炼 {{ d.memory_count }} 条记忆 · {{
          formatTimeFull(d.imported_at) }}</div>
      </div>
      <div class="fg" style="gap:6px;flex-shrink:0">
        <button class="btn-sm" :disabled="busy('docV' + d.id)" @click="run('docV' + d.id, () => openDocDetail(d.id))"><i
            class="ti ti-eye"></i>
          查看</button>
        <button class="btn-sm btn-danger" @click="deleteDoc(d)"><i class="ti ti-trash"></i>
          删除</button>
      </div>
    </div>
  </div>

  <!-- 记忆详情弹窗（可从文档详情/知识图谱抽屉等二级弹窗内打开，需置于其上层；统一走 BaseModal） -->
  <BaseModal v-if="detail" title="记忆详情" stacked @close="detail = null">
    <h3 class="modal-subtitle">{{ detail.frontmatter?.title }}</h3>
      <!-- missing：文件丢失提示 -->
      <div v-if="detail.degraded || detail.frontmatter?.lifecycle === 'missing'" class="banner"
        style="background:var(--warnbg);color:var(--warntx);margin-bottom:12px">
        <i class="ti ti-alert-triangle"></i> 该记忆的 md 文件丢失。可从备份恢复文件后自动重建索引，或直接删除此索引。
      </div>
      <!-- frontmatter 元数据（键值对展示，避免 badge 标签墙） -->
      <dl class="kv">
        <template v-if="detail.frontmatter?.domain">
          <dt>领域</dt>
          <dd>{{ domainLabel(detail.frontmatter.domain) }}</dd>
        </template>
        <template v-if="detail.frontmatter?.source_type">
          <dt>来源</dt>
          <dd>{{ SRC_MAP[detail.frontmatter.source_type] || detail.frontmatter.source_type }}</dd>
        </template>
        <template v-if="detail.frontmatter?.created_by">
          <dt>创建方</dt>
          <dd>{{ detail.frontmatter.created_by === 'user_explicit' ? '用户主动' : detail.frontmatter.created_by === 'import'
            ? '导入' : '提炼引擎' }}</dd>
        </template>
        <dt>创建</dt>
        <dd>{{ detail.frontmatter?.created_at || '-' }}</dd>
        <dt>更新</dt>
        <dd>{{ detail.frontmatter?.updated_at || '-' }}</dd>
        <dt>被引用</dt>
        <dd>{{ detail.access_count || 0 }} 次</dd>
        <template v-if="detail.last_accessed">
          <dt>最近命中</dt>
          <dd>{{ formatTimeFull(detail.last_accessed) }}</dd>
        </template>
      </dl>
      <div class="label">摘要</div>
      <p class="mb-3" style="color:var(--sec)">{{ detail.summary }}</p>
      <div class="label">详情</div>
      <p class="mb-3" style="color:var(--sec);white-space:pre-wrap;max-height:280px;overflow-y:auto">{{
        detail.detail }}</p>
      <div class="filter-bar mb-3">
        <select @change="e => saveAttr('confidence', e.target.value)" :value="detail.frontmatter?.confidence">
          <option value="strong">强</option>
          <option value="medium">中</option>
          <option value="low">弱</option>
        </select>
        <select v-if="detail.frontmatter?.lifecycle !== 'archived' && detail.frontmatter?.lifecycle !== 'missing'"
          @change="e => saveAttr('lifecycle', e.target.value)" :value="detail.frontmatter?.lifecycle">
          <option value="active">活跃</option>
          <option value="stable">稳定</option>
          <option value="stale">过期</option>
        </select>
        <label class="fg" style="gap:4px;font-size:var(--fs-base);cursor:pointer">
          <input type="checkbox" :checked="!!detail.frontmatter?.is_important"
            @change="e => saveAttr('is_important', e.target.checked)" /> 重要记忆
        </label>
      </div>
      <div v-if="detail.linked_memories && detail.linked_memories.length">
        <div class="label">关联记忆（{{ detail.linked_memories.length }}）</div>
        <div class="fg mb-3" style="gap:6px;flex-wrap:wrap">
          <span v-for="lk in detail.linked_memories" :key="lk.id" class="badge list-row"
            @click="openDetail(lk.id)">{{ lk.type }} · {{ lk.title }}</span>
        </div>
      </div>
      <div v-if="detail.change_history && detail.change_history.length">
        <div class="label">变更历史（{{ detail.change_history.length }}）</div>
        <div class="history-scroll" style="max-height:150px">
          <div v-for="(h, hi) in detail.change_history" :key="hi" class="citation-row"
            style="font-size:var(--fs-base)">{{ h }}</div>
        </div>
      </div>
      <div v-if="detail.citations && detail.citations.length">
        <div class="label">被引用记录（{{ detail.citations.length }}）</div>
        <div class="history-scroll">
          <div v-for="(ct, ci) in detail.citations" :key="ci" class="citation-row">
            <span class="muted" style="flex-shrink:0">{{ formatTimeFull(ct.cited_at) }}</span>
            <span class="citation-title" :title="ct.session_title">{{
              ct.session_title }}</span>
            <button class="btn-xs" style="flex-shrink:0" @click="jumpToSession(ct.session_id)">
              <i class="ti ti-message"></i> 查看对话</button>
          </div>
        </div>
      </div>
      <div class="action-footer">
        <!-- 从 Lint 建议打开时：支持在详情内直接采纳/忽略该建议 -->
        <button @click="detail = null">关闭</button>
        <template v-if="detailSug">
          <button :disabled="busy('disSug')" @click="run('disSug', dismissDetailSug)">忽略</button>
          <button class="btn-primary" :disabled="busy('accSug')" @click="run('accSug', acceptDetailSug)"><i
              v-if="busy('accSug')" class="ti ti-loader-2"></i> 采纳</button>
        </template>
        <button v-if="detail.frontmatter?.lifecycle === 'archived'" :disabled="busy('mAct')"
          @click="run('mAct', () => restore(detail.id))">
          <i class="ti ti-restore"></i> 恢复</button>
        <button v-else-if="detail.frontmatter?.lifecycle !== 'missing' && !detail.degraded" :disabled="busy('mAct')"
          @click="run('mAct', () => archive(detail.id))">归档</button>
        <button class="btn-danger" @click="del(detail.id)">{{ detail.degraded || detail.frontmatter?.lifecycle ===
          'missing'
          ? '删除索引' : '删除' }}</button>
      </div>
  </BaseModal>
  <BaseModal v-if="importPreview" title="确认导入内容" size="lg" :show-close="false" :close-on-overlay="false"
    :close-on-esc="false">
      <div class="muted mb-3">「{{ importPreview.filename }}」提炼出 {{ importPreview.items.length
      }} 条候选记忆，勾选需要写入的条目（未勾选的将丢弃）：</div>
      <div style="max-height:360px;overflow-y:auto" class="mb-3">
        <label v-for="(it, i) in importPreview.items" :key="i" class="fg"
          style="gap:8px;padding:8px 0;border-bottom:1px solid var(--bd);cursor:pointer;align-items:flex-start">
          <input type="checkbox" v-model="importPreview.checked[i]" style="margin-top:3px" />
          <span style="flex:1">
            <b>{{ it.title }}</b>
            <span class="badge" style="margin-left:6px">{{ ATTR_MAP[it.attribution] || it.attribution }}</span>
            <span class="badge badge-a" style="margin-left:4px">{{ CONF_MAP[it.confidence] || it.confidence }}</span>
            <span class="muted" style="display:block;margin-top:2px">{{ it.summary }}</span>
          </span>
        </label>
        <div v-if="!importPreview.items.length" class="empty" style="padding:20px">本文档未提炼出可入库的内容</div>
      </div>
      <div class="action-footer">
        <button class="dang" :disabled="previewSubmitting" @click="submitImportPreview(true)">全部丢弃</button>
        <button class="btn-primary" :disabled="previewSubmitting" @click="submitImportPreview(false)">
          写入勾选的 {{ previewCheckedCount() }} 条</button>
      </div>
  </BaseModal>
  <BaseModal v-if="diffData" title="版本对比" @close="diffData = null">
    <div class="label">旧版</div>
    <pre class="pre-block" style="max-height:200px">{{ diffData.from }}</pre>
    <div class="label mt-3">新版</div>
    <pre class="pre-block" style="max-height:200px">{{ diffData.to }}</pre>
    <template #footer>
      <button @click="diffData = null">关闭</button>
    </template>
  </BaseModal>
  <BaseModal v-if="showSoulCoreEdit" title="编辑 SOUL_CORE 核心人格" @close="showSoulCoreEdit = false">
    <div class="muted mb-2" style="color:var(--warntx)">⚠ 核心人格影响全局行为，保存前会二次确认，下次会话生效。</div>
    <textarea v-model="soulCoreDraft"
      style="width:100%;height:280px;font-family:var(--mono);font-size:var(--fs-sm)"></textarea>
    <template #footer>
      <button @click="showSoulCoreEdit = false">取消</button>
      <button class="btn-primary" :disabled="busy('saveSoul')" @click="run('saveSoul', saveSoulCore)"><i
          v-if="busy('saveSoul')" class="ti ti-loader-2"></i> 保存</button>
    </template>
  </BaseModal>
  <BaseModal v-if="showOutputStyleEdit" title="编辑输出样式画像" @close="showOutputStyleEdit = false">
    <div class="muted mb-2" style="color:var(--warntx)">⚠ 将覆盖当前画像内容，保存前会二次确认，下次会话生效。</div>
    <textarea v-model="outputStyleDraft"
      style="width:100%;height:280px;font-family:var(--mono);font-size:var(--fs-sm)"></textarea>
    <template #footer>
      <button @click="showOutputStyleEdit = false">取消</button>
      <button class="btn-primary" :disabled="busy('saveOut')" @click="run('saveOut', saveOutputStyle)"><i
          v-if="busy('saveOut')" class="ti ti-loader-2"></i> 保存</button>
    </template>
  </BaseModal>
  <BaseModal v-if="docDetail" title="文档详情" @close="docDetail = null">
    <h3 class="modal-subtitle">
      <i class="ti ti-file-text" style="margin-right:6px;color:var(--acctx)"></i>{{ docDetail.filename }}
    </h3>
      <div class="muted mb-3">{{ fmtSize(docDetail.size) }} · 提炼 {{ docDetail.memory_count }} 条记忆
        ·
        {{ formatTimeFull(docDetail.imported_at) }}</div>
      <div class="label">文档正文</div>
      <pre class="pre-block">{{ docDetail.content || '（无法解析正文或内容为空）' }}</pre>
      <div class="label">提炼的记忆（{{ docDetail.memories.length }}）</div>
      <div v-for="m in docDetail.memories" :key="m.id" class="cw list-row" style="padding:var(--sp-3)"
        @click="openDetail(m.id)">
        <b>{{ m.title }}</b>
        <div class="muted">{{ m.summary }}</div>
      </div>
      <div v-if="!docDetail.memories.length" class="empty" style="padding:20px var(--sp-3)">该文档还没有提炼记忆</div>
      <div v-if="docDetail.citations && docDetail.citations.length">
        <div class="label mt-3">被引用记录（{{ docDetail.citations.length }}）</div>
        <div class="history-scroll">
          <div v-for="(ct, ci) in docDetail.citations" :key="ci" class="citation-row">
            <span class="muted" style="flex-shrink:0">{{ formatTimeFull(ct.cited_at) }}</span>
            <span class="citation-title"
              :title="ct.memory_title + ' · ' + ct.session_title">{{ ct.memory_title }} · {{ ct.session_title }}</span>
            <button class="btn-xs" style="flex-shrink:0" @click="jumpToSession(ct.session_id)">
              <i class="ti ti-message"></i> 查看对话</button>
          </div>
        </div>
      </div>
      <div class="action-footer">
        <button @click="docDetail = null">关闭</button>
      </div>
  </BaseModal>
  <BaseModal v-if="localDirFiles" title="目录文件" @close="localDirFiles = null">
    <h3 class="modal-subtitle">{{ localDirFiles.dir.path }}</h3>
      <div class="muted mb-3">
        已导入 {{ localDirFiles.files.filter(f => f.status === 'imported').length }}/{{ localDirFiles.files.length }} 个文件
      </div>
      <div style="max-height:360px;overflow-y:auto" class="mb-3">
        <div v-for="(f, i) in localDirFiles.files" :key="i" class="version-row" style="font-size:var(--fs-base)">
          <span class="citation-title" :title="f.path">{{ f.path }}</span>
          <span class="badge" :class="f.status === 'imported' ? 'badge-g' : (f.status === 'failed' ? 'badge-r' : '')">{{
            LOCAL_FILE_STATUS[f.status] || f.status }}</span>
          <div v-if="f.fail_reason" class="muted mt-1" style="font-size:var(--fs-xs);width:100%">{{ f.fail_reason }}</div>
        </div>
        <div v-if="!localDirFiles.files.length" class="empty" style="padding:20px">还没有文件记录<br>接入后扫描即可生成</div>
      </div>
      <div class="action-footer">
        <button @click="localDirFiles = null">关闭</button>
      </div>
  </BaseModal>
  <BaseModal v-if="dupCompare" title="疑似重复对比" size="xl" @close="dupCompare = null">
    <div class="action-footer mb-3" style="margin-top:0">
      <button class="btn-xs" :disabled="busy('dup')"
        @click="run('dup', () => resolveDup(dupCompare.sug, 'keep_a'))">保留 A</button>
      <button class="btn-xs" :disabled="busy('dup')"
        @click="run('dup', () => resolveDup(dupCompare.sug, 'keep_b'))">保留 B</button>
      <button class="btn-xs btn-primary" :disabled="busy('dup')"
        @click="run('dup', () => resolveDup(dupCompare.sug, 'keep_both'))">都保留</button>
      <button class="btn-xs btn-danger" :disabled="busy('dup')"
        @click="run('dup', () => resolveDup(dupCompare.sug, 'delete_both'))">全部删除</button>
    </div>
      <div class="g2" style="gap:var(--sp-4);align-items:stretch">
        <div v-for="(m, k) in [dupCompare.a, dupCompare.b]" :key="k" class="cw"
          style="margin:0;display:flex;flex-direction:column;min-height:0">
          <div class="row mb-2">
            <b :style="{ color: k === 0 ? 'var(--acctx)' : 'var(--warntx)' }">记忆 {{ k === 0 ? 'A' : 'B' }}</b>
            <button v-if="m" class="btn-xs" @click.stop="openDetail(m.id); dupCompare = null">
              <i class="ti ti-external-link"></i> 查看来源
            </button>
          </div>
          <template v-if="m">
            <div class="list-title mb-2">{{ m.frontmatter?.title || m.id }}</div>
            <div class="compare-meta">
              <span class="badge">{{ CONF_MAP[m.frontmatter?.confidence] || m.frontmatter?.confidence }}</span>
              <span class="badge">{{ LIFE_MAP[m.frontmatter?.lifecycle] || m.frontmatter?.lifecycle }}</span>
              <span v-if="m.frontmatter?.domain" class="badge">{{ domainLabel(m.frontmatter.domain) }}</span>
              <span v-if="m.frontmatter?.source_type" class="badge">{{ SRC_MAP[m.frontmatter.source_type] ||
                m.frontmatter.source_type }}</span>
              <span class="muted">{{ m.frontmatter?.created_at }}</span>
              <span class="muted">引用 {{ m.access_count || 0 }} 次</span>
            </div>
            <p class="compare-body">{{
              [m.summary, m.detail].filter(Boolean).join('\n\n') }}</p>
          </template>
          <div v-else class="muted" style="padding:20px 0;text-align:center">记忆不存在</div>
        </div>
      </div>
      <div class="action-footer">
        <button @click="dupCompare = null">关闭</button>
      </div>
  </BaseModal>
  <BaseModal v-if="conflictCompare" title="矛盾记忆对比" size="xl" @close="conflictCompare = null">
    <div class="action-footer mb-3" style="margin-top:0">
      <button class="btn-xs" :disabled="busy('conf')"
        @click="run('conf', () => resolveConflict(conflictCompare.conflict_id, 'keep_a'))">保留 A</button>
      <button class="btn-xs" :disabled="busy('conf')"
        @click="run('conf', () => resolveConflict(conflictCompare.conflict_id, 'keep_b'))">保留 B</button>
      <button class="btn-xs btn-primary" :disabled="busy('conf')"
        @click="run('conf', () => resolveConflict(conflictCompare.conflict_id, 'keep_both'))">都保留</button>
      <button class="btn-xs btn-danger" :disabled="busy('conf')"
        @click="run('conf', () => resolveConflict(conflictCompare.conflict_id, 'delete_both'))">全部删除</button>
    </div>
      <div v-if="conflictCompare.detectedAt" class="muted mb-3">检测于 {{
        conflictCompare.detectedAt }}</div>
      <div v-if="conflictCompare.loading" class="muted" style="text-align:center;padding:40px 0">加载中…</div>
      <template v-else>
        <div class="g2" style="gap:var(--sp-4);align-items:stretch">
          <div v-for="(side, k) in ['A', 'B']" :key="k" class="cw"
            style="margin:0;display:flex;flex-direction:column;min-height:0">
            <div class="row mb-2">
              <b :style="{ color: k === 0 ? 'var(--acctx)' : 'var(--warntx)' }">记忆 {{ side }}</b>
              <button v-if="k === 0 ? conflictCompare.detailA : conflictCompare.detailB" class="btn-xs"
                @click.stop="openDetail((k === 0 ? conflictCompare.detailA : conflictCompare.detailB).id); conflictCompare = null">
                <i class="ti ti-external-link"></i> 查看来源
              </button>
            </div>
            <template v-if="k === 0 ? conflictCompare.detailA : conflictCompare.detailB">
              <div class="list-title mb-2">{{
                (k === 0 ? conflictCompare.detailA : conflictCompare.detailB).frontmatter?.title || (k === 0 ? conflictCompare.detailA : conflictCompare.detailB).id }}</div>
              <div class="compare-meta">
                <span class="badge">{{ CONF_MAP[(k === 0 ? conflictCompare.detailA : conflictCompare.detailB).frontmatter?.confidence] ||
                  (k === 0 ? conflictCompare.detailA : conflictCompare.detailB).frontmatter?.confidence }}</span>
                <span class="badge">{{ LIFE_MAP[(k === 0 ? conflictCompare.detailA : conflictCompare.detailB).frontmatter?.lifecycle] ||
                  (k === 0 ? conflictCompare.detailA : conflictCompare.detailB).frontmatter?.lifecycle }}</span>
                <span v-if="(k === 0 ? conflictCompare.detailA : conflictCompare.detailB).frontmatter?.domain" class="badge">{{
                  domainLabel((k === 0 ? conflictCompare.detailA : conflictCompare.detailB).frontmatter.domain) }}</span>
                <span v-if="(k === 0 ? conflictCompare.detailA : conflictCompare.detailB).frontmatter?.source_type" class="badge">{{
                  SRC_MAP[(k === 0 ? conflictCompare.detailA : conflictCompare.detailB).frontmatter.source_type] ||
                  (k === 0 ? conflictCompare.detailA : conflictCompare.detailB).frontmatter.source_type }}</span>
                <span class="muted">{{ (k === 0 ? conflictCompare.detailA : conflictCompare.detailB).frontmatter?.created_at }}</span>
                <span class="muted">引用 {{ (k === 0 ? conflictCompare.detailA : conflictCompare.detailB).access_count || 0 }} 次</span>
              </div>
              <p class="compare-body">{{
                [(k === 0 ? conflictCompare.detailA : conflictCompare.detailB).summary, (k === 0 ? conflictCompare.detailA : conflictCompare.detailB).detail].filter(Boolean).join('\n\n') }}</p>
            </template>
            <div v-else class="muted" style="padding:20px 0;text-align:center">{{
              (k === 0 ? conflictCompare.sourceA : conflictCompare.sourceB)?.content || '记忆不存在' }}</div>
          </div>
        </div>
      </template>
      <div class="action-footer">
        <button @click="conflictCompare = null">关闭</button>
      </div>
  </BaseModal>
  <BaseModal v-if="dimDetail" @close="dimDetail = null">
    <template #header>
      {{ dimDetail.name }} <span class="badge badge-g" style="margin-left:8px">{{ dimStatusLabel(dimDetail.status) }}</span>
    </template>
    <div v-for="(it, j) in dimDetail.items" :key="j"
      class="mb-3" style="color:var(--sec);font-size:var(--fs-md);line-height:1.6">
      · {{ it.text }} <span v-if="it.inferred" class="muted">[推断]</span>
    </div>
    <div v-if="!dimDetail.items || !dimDetail.items.length" class="empty" style="padding:var(--sp-6) var(--sp-3)">该维度还没有内容</div>
    <template #footer>
      <button @click="dimDetail = null">关闭</button>
    </template>
  </BaseModal>
</template>
