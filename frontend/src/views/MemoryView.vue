<script setup>
import { ref, onMounted, onActivated, computed } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useToast } from '@/stores/toast'
import { useConfirm } from '@/stores/confirm'
import { useSessions } from '@/stores/sessions'
import KnowledgeGraph from '@/components/graph/KnowledgeGraph.vue'
import { domainLabel, loadDomainLabels } from '@/utils/domainLabel'

const router = useRouter()
const toast = useToast()
const confirm = useConfirm()
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

const EVT_MAP = { created: '新建', updated: '更新', evolved: '演变', imported: '导入', archived: '归档', merged: '合并' }

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

function formatTime(iso) {
  if (!iso) return '-'
  const d = new Date(String(iso).replace(' ', 'T'))
  if (isNaN(d.getTime())) return iso
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

const CONF_MAP = { strong: '强', medium: '中', low: '弱', disputed: '争议' }
const LIFE_MAP = { active: '活跃', stable: '稳定', stale: '过期', archived: '已归档', missing: '缺失' }
const SRC_MAP = { memory: '对话记忆', knowledge: '外部知识' }
const ATTR_MAP = { imported: '外部导入', verified: '已验证经验', inferred: '待验证推断' }

async function loadProfile() {
  try {
    const [p, s, o, h, pd] = await Promise.all([
      api.get('/profile'), api.get('/soul'), api.get('/output-style'),
      (async () => {
        soulHistory.value = await api.get('/soul/style/history?source=' + soulSource.value)
      })(),
      api.get('/soul/pending').then(p => { pendings.value = p }).catch(() => { }),
    ])
    profile.value = p; soul.value = s; outputStyle.value = o
  } catch { /* 任一 API 失败时保留旧数据显示 */ }
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

// 重复检测：并排对比两条疑似重复记忆
const dupCompare = ref(null)
async function openDupCompare(sug) {
  const [a, b] = await Promise.all([
    api.get('/memory/detail?id=' + sug.memory_a.id),
    api.get('/memory/detail?id=' + sug.memory_b.id),
  ])
  dupCompare.value = { a, b }
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
async function runLint() { await api.post('/memory/lint/run', {}); toast.push('success', 'Lint 已执行'); await loadHealth() }
async function buildOutputStyle() { await api.post('/output-style/build-now', {}); toast.push('success', '已触发提炼'); await loadProfile() }
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
  if (!d.items || !d.items.length) return '（暂无内容，可点“手动提炼”生成）'
  return d.items.map(it => it.text).join('；')
}

// 知识库文档：上传 / 列表 / 删除
const docs = ref([])
const docUploading = ref(false)
const docProgress = ref(null)   // { filename, index, totalFiles, stage, current, total }
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
      let event = 'message', data = ''
      for (const line of part.split(/\r?\n/)) {
        if (line.startsWith('event:')) event = line.slice(6).trim()
        else if (line.startsWith('data:')) data += line.slice(5).trim()
      }
      if (data) { try { onEvent(event, JSON.parse(data)) } catch { onEvent(event, {}) } }
    }
  }
}
async function uploadDocs(fileList) {
  const files = Array.from(fileList || [])
  if (!files.length || docUploading.value) return
  docUploading.value = true
  let ok = 0, fail = 0
  try {
    // 逐个文件独立导入：单个失败不影响其余文件，实时进度经 SSE 展示
    for (let fi = 0; fi < files.length; fi++) {
      const f = files[fi]
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
            toast.push('error', data.message || '导入失败')
          }
        })
      } catch { errored = true }
      if (errored || !result) { fail++; continue }
      ok++
      if (result.preview) {
        // 预览模式（已关闭静默导入）：提炼结果先进勾选确认队列
        previewQueue.push({
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
const previewQueue = []
const previewSubmitting = ref(false)
function openNextPreview() { importPreview.value = previewQueue.shift() || null }
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
async function deleteDoc(id) {
  if (!await confirm.ask({ message: '删除该文档？已提炼的记忆不会自动删除，但会影响记忆重建完整性。', danger: true })) return
  await api.del('/import/documents/' + id)
  await loadDocs(); toast.push('success', '已删除')
}
function fmtSize(bytes) {
  if (!bytes) return '-'
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / 1024 / 1024).toFixed(1) + ' MB'
}

function selectTab(i) {
  tab.value = i
  if (i === 1) loadList()
  else if (i === 2) loadTimeline()
  else if (i === 3) loadProfile()
  else if (i === 4) { loadHealth(); loadConflicts() }
  else if (i === 5) loadDocs()
}

onMounted(() => selectTab(1))
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
    <div class="g4" style="margin-bottom:12px">
      <div class="card">
        <div class="label">记忆总数</div>
        <div class="val">{{ stats.total_active + stats.total_stable + stats.total_stale || 0 }}</div>
        <div class="muted">archived {{ stats.total_archived || 0 }}</div>
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
    <div class="fg" style="gap:8px;margin-bottom:12px;flex-wrap:wrap">
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
      <label class="fg" style="gap:4px;font-size:13px;cursor:pointer;white-space:nowrap">
        <input type="checkbox" v-model="importantOnly" @change="loadList" /> 只看重要记忆
      </label>
    </div>
    <div v-if="!memList.length" class="empty"><i class="ti ti-brain"></i>记忆宫殿还是空的<br>对话中系统会自动沉淀记忆</div>
    <div v-for="m in memList" :key="m.id" class="cw" style="cursor:pointer" @click="openDetail(m.id)">
      <div class="row">
        <div>
          <div style="font-weight:500">{{ m.title }}</div>
          <div class="muted">{{ m.summary }}</div>
          <div v-if="m.lifecycle === 'missing'" style="font-size:12px;color:var(--warntx);margin-top:4px">
            <i class="ti ti-alert-triangle"></i> 文件丢失，可从备份恢复文件后自动重建，或删除此索引
          </div>
        </div>
        <div class="fg">
          <span class="badge" style="opacity:.75">{{ domainLabel(m.domain) }}</span>
          <span v-if="m.is_important" class="badge badge-g">重要</span>
          <span class="badge badge-a">{{ CONF_MAP[m.confidence] || m.confidence }}</span>
          <span class="badge" :style="m.lifecycle === 'missing' ? 'color:var(--warntx)' : ''">{{ LIFE_MAP[m.lifecycle]
            || m.lifecycle }}</span>
          <span class="muted">{{ m.access_count }}次</span>
        </div>
      </div>
    </div>
  </div>

  <!-- 时间线 -->
  <div v-else-if="tab === 2">
    <div class="fg" style="gap:8px;margin-bottom:12px;flex-wrap:wrap">
      <button class="tab" :class="{ active: tlType === '' }" @click="tlType = ''; loadTimeline()">全部</button>
      <button v-for="(label, key) in EVT_MAP" :key="key" class="tab" :class="{ active: tlType === key }"
        @click="tlType = key; loadTimeline()">{{ label }}</button>
      <select v-model="tlDays" @change="loadTimeline" style="margin-left:auto">
        <option :value="7">近 7 天</option>
        <option :value="30">近 30 天</option>
        <option :value="90">近 90 天</option>
      </select>
    </div>
    <div v-if="!timeline.length" class="empty"><i class="ti ti-timeline"></i>暂无记忆变更记录</div>
    <div v-for="g in timelineGroups" :key="g.day">
      <div class="muted" style="font-weight:600;margin:14px 0 6px">{{ g.day }}</div>
      <div v-for="(t, i) in g.items" :key="i" class="cw" style="cursor:pointer" @click="openDetail(t.memory_id)">
        <div style="font-weight:500">{{ t.title || t.memory_id }}</div>
        <div class="row" style="margin-top:4px">
          <span class="badge badge-a">{{ EVT_MAP[t.event_type] || t.event_type }}</span>
          <span class="muted" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ t.summary ||
            t.detail }}</span>
          <button v-if="tlLinkTarget(t)" style="font-size:11px;flex-shrink:0"
            @click.stop="openDetail(tlLinkTarget(t))"><i class="ti ti-link"></i> 查看关联</button>
          <span class="muted">{{ formatTime(t.event_time) }}</span>
        </div>
      </div>
    </div>
  </div>

  <!-- 用户画像 -->
  <div v-else-if="tab === 3">
    <div class="row" style="margin-bottom:10px">
      <div class="muted" style="font-weight:500;color:var(--acctx)">区一 · 用户画像</div>
      <button style="font-size:12px" @click="buildProfile">手动提炼</button>
    </div>
    <div v-if="!profile.dimensions.length" class="empty"><i class="ti ti-user"></i>用户画像积累中<br>系统在观察你的偏好</div>
    <div class="g2">
      <div v-for="(d, i) in profile.dimensions" :key="i" class="cw" style="cursor:pointer" @click="openDimDetail(d)">
        <div class="row"><b>{{ d.name }}</b><span class="badge badge-g">{{ d.status }}</span></div>
        <div class="dim-preview">{{ dimText(d) }}</div>
      </div>
    </div>
    <div class="sep"></div>
    <div class="muted" style="margin-bottom:10px;font-weight:500;color:var(--acctx)">区二 · AI 人格</div>
    <div v-if="pendings.length" class="cw" style="border:1px solid var(--warntx);background:var(--warnbg)">
      <b style="color:var(--warntx)">待确认风格调整</b>
      <div v-for="p in pendings" :key="p.id" style="margin-top:8px">
        <div class="muted">触发：{{ p.original_text }}</div>
        <div style="font-size:13px">提议：{{ p.proposed_change }}</div>
        <div class="fg" style="gap:6px;margin-top:6px">
          <button style="font-size:11px" @click="confirmPending(p.id, true)">确认</button>
          <button style="font-size:11px" @click="confirmPending(p.id, false)">忽略</button>
        </div>
      </div>
    </div>
    <div class="cw">
      <div class="row"><b>SOUL_CORE 核心人格</b>
        <div class="fg" style="gap:6px">
          <button style="font-size:12px" @click="openSoulCoreEdit"><i class="ti ti-edit"></i> 编辑</button>
          <button style="font-size:12px" @click="resetSoulCore">恢复默认</button>
        </div>
      </div>
      <pre style="white-space:pre-wrap;margin-top:8px">{{ soul.soul_core }}</pre>
    </div>
    <div class="cw">
      <div class="tabs" style="margin-bottom:12px">
        <button class="tab" :class="{ active: soulSource === 'dialog' }" @click="switchSoulSource('dialog')">对话确认
          dialog</button>
        <button class="tab" :class="{ active: soulSource === 'auto' }" @click="switchSoulSource('auto')">自动演化
          auto</button>
      </div>
      <!-- 当前序列实际内容：有则展示，无则明确告知暂无 -->
      <div style="margin-bottom:14px">
        <div v-for="s in currentStyleSections" :key="s.name" style="margin-bottom:10px">
          <div class="muted" style="font-weight:500;margin-bottom:4px">{{ s.name }}</div>
          <pre v-if="s.text" style="white-space:pre-wrap;font-size:13px;color:var(--sec);margin:0">{{ s.text }}</pre>
          <div v-else class="muted" style="font-size:13px">暂时还没有内容</div>
        </div>
      </div>
      <div v-if="!soulHistory.length" class="muted">暂无版本历史</div>
      <div v-for="h in soulHistory" :key="h.version" class="row"
        style="padding:8px 0;border-bottom:.5px solid var(--bd)">
        <div><b>v{{ h.version }}</b> <span v-if="h.current" class="badge badge-a">当前</span>
          <div class="muted">{{ h.diff_summary }}</div>
        </div>
        <div class="fg" style="gap:6px">
          <button style="font-size:11px" @click="showDiff(h.version)">对比</button>
          <button v-if="!h.current" style="font-size:11px" @click="rollback(h.version)">回滚</button>
        </div>
      </div>
    </div>
    <div class="sep"></div>
    <div class="muted" style="margin-bottom:10px;font-weight:500;color:var(--acctx)">区三 · 输出样式画像</div>
    <div class="cw">
      <div class="row"><b>当前画像</b>
        <div class="fg" style="gap:8px">
          <label class="fg" style="gap:4px;font-size:12px;cursor:pointer;white-space:nowrap">
            <input type="checkbox" :checked="outputStyle.auto_evolve_enabled" @change="toggleAutoEvolve" />
            自动演化
          </label>
          <button style="font-size:12px" @click="buildOutputStyle">手动提炼</button>
        </div>
      </div>
      <div style="margin-top:8px;color:var(--sec)">{{ outputStyle.profile_text || '（积累中，信号数 ' +
        (outputStyle.signal_count || 0) + '/50）' }}</div>
      <!-- 信号积累进度：已采集 / 上次提炼 / 下次触发条件 -->
      <div class="muted" style="margin-top:10px;font-size:12px">
        已采集 {{ outputStyle.signal_count || 0 }} 条信号
        <template v-if="outputStyle.last_built"> · 上次提炼 {{ formatTime(outputStyle.last_built) }}</template>
        <template v-if="outputStyle.is_cold_start"> · 首次提炼需满 50 条</template>
        <template v-else> · 新增满 {{ outputStyle.batch_threshold || 100 }} 条或到达周期自动提炼</template>
        <template v-if="!outputStyle.auto_evolve_enabled"> · <span
            style="color:var(--warntx)">自动演化已暂停（仍在采集信号）</span></template>
      </div>
    </div>
  </div>

  <!-- 健康度 -->
  <div v-else-if="tab === 4">
    <div v-if="health" class="g4" style="margin-bottom:16px">
      <div class="card" style="text-align:center">
        <!-- 健康分环形图 -->
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
        <div class="muted">健康度</div>
        <button style="margin-top:8px;font-size:12px" @click="runLint">立即检查</button>
      </div>
      <div class="card">
        <div class="label">记忆总数</div>
        <div class="val">{{ (health.stats.total || 0) }}</div>
        <div class="muted">archived {{ health.stats.archived || 0 }} 单列</div>
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
      <div class="row" style="justify-content:space-between;align-items:center">
        <b>矛盾记忆</b>
        <span class="muted" style="font-size:12px">{{ conflicts.length }} 条待处理</span>
      </div>
      <div v-for="cf in conflicts" :key="cf.conflict_id" class="cw" style="margin-top:8px">
        <div class="row" style="justify-content:space-between;align-items:center">
          <div style="font-weight:500;cursor:pointer;flex:1;min-width:0" @click="openConflictCompare(cf)">
            {{ cf.title }}
            <span class="muted" style="font-size:11px;margin-left:8px"><i class="ti ti-columns"></i> 查看对比</span>
          </div>
        </div>
      </div>
    </div>
    <!-- Lint 检查明细：完整列表，可操作项带建议卡 -->
    <div v-if="health" class="cw">
      <b>Lint 检查明细</b>
      <div v-for="chk in health.lint_details.filter(c => !c.actionable)" :key="chk.check" class="row"
        style="padding:6px 0;border-bottom:.5px solid var(--bd)">
        <span>{{ chk.check }} <span class="muted">· {{ chk.desc }}</span></span>
        <span class="badge" :class="chk.status === 'ok' ? 'badge-g' : ''"
          :style="chk.status === 'warning' && chk.count ? 'color:var(--warntx)' : ''">{{ chk.count }}</span>
      </div>
    </div>
    <div v-if="health" v-for="chk in health.lint_details.filter(c => c.actionable)" :key="chk.check" class="cw">
      <b>{{ chk.check }}</b> <span class="muted">{{ chk.desc }}（{{ chk.count }}）</span>
      <div v-for="sug in (chk.suggestion_ids || [])" :key="sug.id || sug" class="fg" style="gap:6px;margin-top:8px">
        <span class="muted" style="flex:1">
          <template v-if="chk.check === '孤立检测'">{{ sug.title || sug }}</template>
          <template v-else-if="chk.check === '重复检测'">{{ sug.memory_a?.title || sug.memory_a?.id }} ↔ {{
            sug.memory_b?.title || sug.memory_b?.id }}</template>
          <template v-else>{{ sug.id || sug }}</template>
        </span>
        <button v-if="chk.check === '孤立检测'" style="font-size:11px" @click="openDetail(sug.memory_id, sug)"><i
            class="ti ti-eye"></i>
          查看</button>
        <template v-else-if="chk.check === '重复检测'">
          <button style="font-size:11px" @click="openDupCompare(sug)"><i class="ti ti-columns"></i> 对比查看</button>
        </template>
        <button style="font-size:11px" @click="acceptSug(sug.id || sug)">采纳</button>
        <button style="font-size:11px" @click="dismissSug(sug.id || sug)">忽略</button>
      </div>
    </div>
  </div>

  <!-- 知识库 -->
  <div v-else-if="tab === 5">
    <div class="doc-drop" :class="{ uploading: docUploading }" @click="triggerDocPick" @dragover.prevent
      @drop.prevent="uploadDocs($event.dataTransfer.files)">
      <i class="ti" :class="docUploading ? 'ti-loader-2' : 'ti-cloud-upload'"></i>
      <template v-if="docProgress">
        <div style="font-weight:500;margin-top:6px">
          正在导入「{{ docProgress.filename }}」<template v-if="docProgress.totalFiles > 1">（{{ docProgress.index }}/{{
            docProgress.totalFiles }}）</template>
        </div>
        <div class="muted" style="margin-top:4px">{{ docStageLabel(docProgress) }}</div>
        <div v-if="docProgress.stage === 'distilling' && docProgress.total"
          style="margin:10px auto 0;max-width:280px;height:6px;border-radius:var(--radius-pill);background:var(--bd);overflow:hidden">
          <div
            :style="{ width: (docProgress.current / docProgress.total * 100) + '%', height: '100%', background: 'var(--acctx)', transition: '.25s' }">
          </div>
        </div>
      </template>
      <template v-else>
        <div style="font-weight:500;margin-top:6px">点击或拖拽文件到此上传</div>
        <div class="muted">支持 PDF / DOCX / TXT / MD / 图片（PNG、JPG 等，经 VLM/OCR 提取），自动解析并提炼为知识库记忆</div>
      </template>
      <input ref="docFileInput" type="file" multiple style="display:none" @change="onDocPick" />
    </div>
    <div v-if="!docs.length" class="empty" style="margin-top:16px"><i
        class="ti ti-files"></i>知识库还没有文档<br>上传文档后系统会自动解析并提炼记忆
    </div>
    <div v-for="d in docs" :key="d.id" class="cw"
      style="display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:12px">
      <div style="min-width:0;flex:1">
        <div style="font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
          <i class="ti ti-file-text" style="margin-right:6px;color:var(--acctx)"></i>{{ d.filename }}
        </div>
        <div class="muted" style="margin-top:4px">{{ fmtSize(d.size) }} · 提炼 {{ d.memory_count }} 条记忆 · {{
          formatTime(d.imported_at) }}</div>
      </div>
      <div class="fg" style="gap:6px;flex-shrink:0">
        <button style="font-size:12px" @click="openDocDetail(d.id)"><i class="ti ti-eye"></i>
          查看</button>
        <button class="dang" style="font-size:12px" @click="deleteDoc(d.id)"><i class="ti ti-trash"></i>
          删除</button>
      </div>
    </div>
  </div>

  <!-- 记忆详情弹窗（可从文档详情/知识图谱抽屉等二级弹窗内打开，需置于其上层） -->
  <div v-if="detail" class="overlay" style="z-index:130" @click.self="detail = null">
    <div class="modal">
      <div class="mt">记忆详情</div>
      <h3 style="font-size:16px;margin-bottom:8px">{{ detail.frontmatter?.title }}</h3>
      <!-- missing：文件丢失提示 -->
      <div v-if="detail.degraded || detail.frontmatter?.lifecycle === 'missing'" class="banner"
        style="background:var(--warnbg);color:var(--warntx);margin-bottom:12px">
        <i class="ti ti-alert-triangle"></i> 该记忆的 md 文件丢失。可从备份恢复文件后自动重建索引，或直接删除此索引。
      </div>
      <!-- frontmatter 元数据 -->
      <div class="fg" style="gap:6px;margin-bottom:10px;flex-wrap:wrap;font-size:12px">
        <span v-if="detail.frontmatter?.domain" class="badge">领域：{{ domainLabel(detail.frontmatter.domain) }}</span>
        <span v-if="detail.frontmatter?.source_type" class="badge">来源：{{ SRC_MAP[detail.frontmatter.source_type] ||
          detail.frontmatter.source_type }}</span>
        <span v-if="detail.frontmatter?.created_by" class="badge">创建方：{{ detail.frontmatter.created_by ===
          'user_explicit' ? '用户主动' : detail.frontmatter.created_by === 'import' ? '导入' : '提炼引擎' }}</span>
        <span class="badge">创建：{{ detail.frontmatter?.created_at || '-' }}</span>
        <span class="badge">更新：{{ detail.frontmatter?.updated_at || '-' }}</span>
        <span class="badge">被引用 {{ detail.access_count || 0 }} 次</span>
        <span v-if="detail.last_accessed" class="badge">最近命中：{{ formatTime(detail.last_accessed) }}</span>
      </div>
      <div class="label">摘要</div>
      <p style="color:var(--sec);margin-bottom:12px">{{ detail.summary }}</p>
      <div class="label">详情</div>
      <p style="color:var(--sec);margin-bottom:12px;white-space:pre-wrap;max-height:280px;overflow-y:auto">{{
        detail.detail }}</p>
      <div class="fg" style="gap:12px;margin-bottom:12px;flex-wrap:wrap;align-items:center">
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
        <label class="fg" style="gap:4px;font-size:13px;cursor:pointer">
          <input type="checkbox" :checked="!!detail.frontmatter?.is_important"
            @change="e => saveAttr('is_important', e.target.checked)" /> 重要记忆
        </label>
      </div>
      <!-- 关联记忆（交叉引用） -->
      <div v-if="detail.linked_memories && detail.linked_memories.length">
        <div class="label">关联记忆（{{ detail.linked_memories.length }}）</div>
        <div class="fg" style="gap:6px;flex-wrap:wrap;margin-bottom:12px">
          <span v-for="lk in detail.linked_memories" :key="lk.id" class="badge" style="cursor:pointer"
            @click="openDetail(lk.id)">{{ lk.type }} · {{ lk.title }}</span>
        </div>
      </div>
      <!-- 变更历史：记忆的演变轨迹（创建/演变/合并/流转，倒序） -->
      <div v-if="detail.change_history && detail.change_history.length">
        <div class="label">变更历史（{{ detail.change_history.length }}）</div>
        <div style="max-height:150px;overflow-y:auto;margin-bottom:12px">
          <div v-for="(h, hi) in detail.change_history" :key="hi" class="muted"
            style="font-size:13px;padding:4px 0;border-bottom:1px solid var(--bd)">{{ h }}</div>
        </div>
      </div>
      <!-- 被引用记录：记忆资产的使用凭证（可跳转回引用它的对话） -->
      <div v-if="detail.citations && detail.citations.length">
        <div class="label">被引用记录（{{ detail.citations.length }}）</div>
        <div style="max-height:180px;overflow-y:auto;margin-bottom:12px">
          <div v-for="(ct, ci) in detail.citations" :key="ci" class="fg"
            style="gap:8px;padding:6px 0;font-size:13px;border-bottom:1px solid var(--bd)">
            <span class="muted" style="flex-shrink:0">{{ formatTime(ct.cited_at) }}</span>
            <span style="flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" :title="ct.session_title">{{
              ct.session_title }}</span>
            <button style="font-size:11px;flex-shrink:0" @click="jumpToSession(ct.session_id)">
              <i class="ti ti-message"></i> 查看对话</button>
          </div>
        </div>
      </div>
      <div class="fg" style="justify-content:flex-end;gap:8px">
        <!-- 从 Lint 建议打开时：支持在详情内直接采纳/忽略该建议 -->
        <template v-if="detailSug">
          <button class="btn-primary" @click="acceptDetailSug"><i class="ti ti-check"></i> 采纳</button>
          <button @click="dismissDetailSug">忽略</button>
        </template>
        <button @click="detail = null">关闭</button>
        <button v-if="detail.frontmatter?.lifecycle === 'archived'" @click="restore(detail.id)">
          <i class="ti ti-restore"></i> 恢复</button>
        <button v-else-if="detail.frontmatter?.lifecycle !== 'missing' && !detail.degraded"
          @click="archive(detail.id)">归档</button>
        <button class="dang" @click="del(detail.id)">{{ detail.degraded || detail.frontmatter?.lifecycle === 'missing'
          ? '删除索引' : '删除' }}</button>
      </div>
    </div>
  </div>
  <!-- 导入预览确认弹窗（silent_doc_import=false） -->
  <div v-if="importPreview" class="overlay" style="z-index:140">
    <div class="modal" style="max-width:640px">
      <div class="mt">确认导入内容</div>
      <div class="muted" style="margin-bottom:10px">「{{ importPreview.filename }}」提炼出 {{ importPreview.items.length
      }} 条候选记忆，勾选需要写入的条目（未勾选的将丢弃）：</div>
      <div style="max-height:360px;overflow-y:auto;margin-bottom:14px">
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
      <div class="fg" style="justify-content:flex-end;gap:8px">
        <button class="dang" :disabled="previewSubmitting" @click="submitImportPreview(true)">全部丢弃</button>
        <button class="btn-primary" :disabled="previewSubmitting" @click="submitImportPreview(false)">
          写入勾选的 {{ previewCheckedCount() }} 条</button>
      </div>
    </div>
  </div>
  <!-- SOUL diff 弹窗 -->
  <div v-if="diffData" class="overlay" @click.self="diffData = null">
    <div class="modal">
      <div class="mt">版本对比</div>
      <div class="label">旧版</div>
      <pre style="white-space:pre-wrap;max-height:200px;overflow:auto">{{ diffData.from }}</pre>
      <div class="label" style="margin-top:10px">新版</div>
      <pre style="white-space:pre-wrap;max-height:200px;overflow:auto">{{ diffData.to }}</pre>
      <div class="fg" style="justify-content:flex-end;margin-top:16px">
        <button @click="diffData = null">关闭</button>
      </div>
    </div>
  </div>
  <!-- SOUL_CORE 编辑弹窗 -->
  <div v-if="showSoulCoreEdit" class="overlay" @click.self="showSoulCoreEdit = false">
    <div class="modal">
      <div class="mt">编辑 SOUL_CORE 核心人格</div>
      <div class="muted" style="margin-bottom:8px;color:var(--warntx)">⚠ 核心人格影响全局行为，保存前会二次确认，下次会话生效。</div>
      <textarea v-model="soulCoreDraft"
        style="width:100%;height:280px;font-family:var(--mono);font-size:12px"></textarea>
      <div class="fg" style="justify-content:flex-end;gap:8px;margin-top:16px">
        <button @click="showSoulCoreEdit = false">取消</button>
        <button class="btn-primary" @click="saveSoulCore">保存</button>
      </div>
    </div>
  </div>
  <!-- 知识库文档详情弹窗 -->
  <div v-if="docDetail" class="overlay" style="z-index:110" @click.self="docDetail = null">
    <div class="modal">
      <div class="mt">文档详情</div>
      <h3 style="font-size:16px;margin-bottom:8px">
        <i class="ti ti-file-text" style="margin-right:6px;color:var(--acctx)"></i>{{ docDetail.filename }}
      </h3>
      <div class="muted" style="margin-bottom:12px">{{ fmtSize(docDetail.size) }} · 提炼 {{ docDetail.memory_count }} 条记忆
        ·
        {{ formatTime(docDetail.imported_at) }}</div>
      <div class="label">文档正文</div>
      <pre
        style="white-space:pre-wrap;max-height:280px;overflow:auto;margin-bottom:12px;background:var(--s1);padding:12px;border-radius:8px;font-family:var(--mono);font-size:12px">
      {{ docDetail.content || '（无法解析正文或内容为空）' }}</pre>
      <div class="label">提炼的记忆（{{ docDetail.memories.length }}）</div>
      <div v-for="m in docDetail.memories" :key="m.id" class="cw" style="cursor:pointer;padding:12px"
        @click="openDetail(m.id)">
        <b>{{ m.title }}</b>
        <div class="muted">{{ m.summary }}</div>
      </div>
      <div v-if="!docDetail.memories.length" class="empty" style="padding:20px 12px">该文档暂无提炼记忆</div>
      <!-- 被引用记录：该文档提炼的记忆被对话引用的明细 -->
      <div v-if="docDetail.citations && docDetail.citations.length">
        <div class="label" style="margin-top:12px">被引用记录（{{ docDetail.citations.length }}）</div>
        <div style="max-height:180px;overflow-y:auto">
          <div v-for="(ct, ci) in docDetail.citations" :key="ci" class="fg"
            style="gap:8px;padding:6px 0;font-size:13px;border-bottom:1px solid var(--bd)">
            <span class="muted" style="flex-shrink:0">{{ formatTime(ct.cited_at) }}</span>
            <span style="flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis"
              :title="ct.memory_title + ' · ' + ct.session_title">{{ ct.memory_title }} · {{ ct.session_title }}</span>
            <button style="font-size:11px;flex-shrink:0" @click="jumpToSession(ct.session_id)">
              <i class="ti ti-message"></i> 查看对话</button>
          </div>
        </div>
      </div>
      <div class="fg" style="justify-content:flex-end;margin-top:16px">
        <button @click="docDetail = null">关闭</button>
      </div>
    </div>
  </div>
  <!-- 重复检测：并排对比弹窗 -->
  <div v-if="dupCompare" class="overlay" style="z-index:110" @click.self="dupCompare = null">
    <div class="modal" style="max-width:860px;width:92vw">
      <div class="mt">疑似重复对比</div>
      <div class="g2">
        <div v-for="(m, k) in [dupCompare.a, dupCompare.b]" :key="k" class="cw" style="margin:0">
          <div class="row" style="margin-bottom:6px">
            <b>{{ k === 0 ? 'A' : 'B' }} · {{ m.frontmatter?.title }}</b>
            <span class="badge badge-a">{{ CONF_MAP[m.frontmatter?.confidence] || m.frontmatter?.confidence }}</span>
          </div>
          <div class="label">摘要</div>
          <p style="color:var(--sec);margin-bottom:10px">{{ m.summary }}</p>
          <div class="label">详情</div>
          <p style="color:var(--sec);white-space:pre-wrap;max-height:240px;overflow:auto">{{ m.detail }}</p>
        </div>
      </div>
      <div class="fg" style="justify-content:flex-end;margin-top:16px">
        <button @click="dupCompare = null">关闭</button>
      </div>
    </div>
  </div>
  <!-- 矛盾记忆：左右对比弹窗 -->
  <div v-if="conflictCompare" class="overlay" style="z-index:120" @click.self="conflictCompare = null">
    <div class="modal" style="max-width:960px;width:92vw">
      <div class="row"
        style="margin-bottom:14px;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
        <span class="mt" style="margin:0">矛盾记忆对比</span>
        <div class="fg" style="gap:6px">
          <button style="font-size:11px" @click="resolveConflict(conflictCompare.conflict_id, 'keep_a')">保留 A</button>
          <button style="font-size:11px" @click="resolveConflict(conflictCompare.conflict_id, 'keep_b')">保留 B</button>
          <button style="font-size:11px" class="btn-primary"
            @click="resolveConflict(conflictCompare.conflict_id, 'keep_both')">都保留</button>
          <button style="font-size:11px" class="dang"
            @click="resolveConflict(conflictCompare.conflict_id, 'delete_both')">全部删除</button>
        </div>
      </div>
      <div v-if="conflictCompare.detectedAt" class="muted" style="font-size:12px;margin-bottom:12px">检测于 {{
        conflictCompare.detectedAt }}</div>
      <div v-if="conflictCompare.loading" class="muted" style="text-align:center;padding:40px 0">加载中…</div>
      <template v-else>
        <div class="g2" style="gap:16px;align-items:stretch">
          <!-- 左侧：记忆 A -->
          <div class="cw" style="margin:0;display:flex;flex-direction:column;min-height:0">
            <div class="row" style="margin-bottom:8px;justify-content:space-between;align-items:center">
              <b style="color:var(--acctx)">记忆 A</b>
              <button v-if="conflictCompare.detailA" style="font-size:11px"
                @click.stop="openDetail(conflictCompare.detailA.id); conflictCompare = null">
                <i class="ti ti-external-link"></i> 查看来源
              </button>
            </div>
            <template v-if="conflictCompare.detailA">
              <div style="font-weight:500;font-size:14px;margin-bottom:8px">{{
                conflictCompare.detailA.frontmatter?.title || conflictCompare.detailA.id }}</div>
              <div class="fg" style="gap:4px;margin-bottom:8px;flex-wrap:wrap;font-size:11px">
                <span class="badge">{{ CONF_MAP[conflictCompare.detailA.frontmatter?.confidence] ||
                  conflictCompare.detailA.frontmatter?.confidence }}</span>
                <span class="badge">{{ LIFE_MAP[conflictCompare.detailA.frontmatter?.lifecycle] ||
                  conflictCompare.detailA.frontmatter?.lifecycle }}</span>
                <span v-if="conflictCompare.detailA.frontmatter?.domain" class="badge">{{
                  domainLabel(conflictCompare.detailA.frontmatter.domain) }}</span>
                <span v-if="conflictCompare.detailA.frontmatter?.source_type" class="badge">{{
                  SRC_MAP[conflictCompare.detailA.frontmatter.source_type] ||
                  conflictCompare.detailA.frontmatter.source_type }}</span>
                <span class="muted">{{ conflictCompare.detailA.frontmatter?.created_at }}</span>
                <span class="muted">引用 {{ conflictCompare.detailA.access_count || 0 }} 次</span>
              </div>
              <p
                style="color:var(--sec);font-size:13px;line-height:1.7;white-space:pre-wrap;max-height:300px;overflow-y:auto;flex:1;margin:0">
                {{ conflictCompare.detailA.summary }}

                {{ conflictCompare.detailA.detail }}</p>
            </template>
            <div v-else class="muted" style="font-size:13px;padding:20px 0;text-align:center">{{
              conflictCompare.sourceA?.content || '记忆不存在' }}</div>
          </div>
          <!-- 右侧：记忆 B -->
          <div class="cw" style="margin:0;display:flex;flex-direction:column;min-height:0">
            <div class="row" style="margin-bottom:8px;justify-content:space-between;align-items:center">
              <b style="color:var(--warntx)">记忆 B</b>
              <button v-if="conflictCompare.detailB" style="font-size:11px"
                @click.stop="openDetail(conflictCompare.detailB.id); conflictCompare = null">
                <i class="ti ti-external-link"></i> 查看来源
              </button>
            </div>
            <template v-if="conflictCompare.detailB">
              <div style="font-weight:500;font-size:14px;margin-bottom:8px">{{
                conflictCompare.detailB.frontmatter?.title || conflictCompare.detailB.id }}</div>
              <div class="fg" style="gap:4px;margin-bottom:8px;flex-wrap:wrap;font-size:11px">
                <span class="badge">{{ CONF_MAP[conflictCompare.detailB.frontmatter?.confidence] ||
                  conflictCompare.detailB.frontmatter?.confidence }}</span>
                <span class="badge">{{ LIFE_MAP[conflictCompare.detailB.frontmatter?.lifecycle] ||
                  conflictCompare.detailB.frontmatter?.lifecycle }}</span>
                <span v-if="conflictCompare.detailB.frontmatter?.domain" class="badge">{{
                  domainLabel(conflictCompare.detailB.frontmatter.domain) }}</span>
                <span v-if="conflictCompare.detailB.frontmatter?.source_type" class="badge">{{
                  SRC_MAP[conflictCompare.detailB.frontmatter.source_type] ||
                  conflictCompare.detailB.frontmatter.source_type }}</span>
                <span class="muted">{{ conflictCompare.detailB.frontmatter?.created_at }}</span>
                <span class="muted">引用 {{ conflictCompare.detailB.access_count || 0 }} 次</span>
              </div>
              <p
                style="color:var(--sec);font-size:13px;line-height:1.7;white-space:pre-wrap;max-height:300px;overflow-y:auto;flex:1;margin:0">
                {{ conflictCompare.detailB.summary }}

                {{ conflictCompare.detailB.detail }}</p>
            </template>
            <div v-else class="muted" style="font-size:13px;padding:20px 0;text-align:center">{{
              conflictCompare.sourceB?.content || '记忆不存在' }}</div>
          </div>
        </div>
      </template>
      <div class="fg" style="justify-content:flex-end;margin-top:12px;gap:8px">
        <button @click="conflictCompare = null">关闭</button>
      </div>
    </div>
  </div>
  <!-- 用户画像维度详情弹窗 -->
  <div v-if="dimDetail" class="overlay" style="z-index:120" @click.self="dimDetail = null">
    <div class="modal">
      <div class="row" style="margin-bottom:14px">
        <span class="mt" style="margin:0">{{ dimDetail.name }}</span>
        <span class="badge badge-g">{{ dimDetail.status }}</span>
      </div>
      <div v-for="(it, j) in dimDetail.items" :key="j"
        style="margin-bottom:10px;color:var(--sec);font-size:14px;line-height:1.6">
        · {{ it.text }} <span v-if="it.inferred" class="muted">[推断]</span>
      </div>
      <div v-if="!dimDetail.items || !dimDetail.items.length" class="empty" style="padding:24px 12px">该维度暂无内容</div>
      <div class="fg" style="justify-content:flex-end;margin-top:16px">
        <button @click="dimDetail = null">关闭</button>
      </div>
    </div>
  </div>
</template>
