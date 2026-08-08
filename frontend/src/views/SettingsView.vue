<script setup>
import { ref, onMounted, onActivated, onUnmounted, computed } from 'vue'
import { api } from '@/api/client'
import { useToast } from '@/stores/toast'
import { useConfirm } from '@/stores/confirm'
import { useBusy } from '@/composables/useBusy'
import BaseModal from '@/components/BaseModal.vue'
import { formatTime } from '@/utils/format'

const toast = useToast()
const confirm = useConfirm()
const { busy, run } = useBusy()
const tab = ref(0)
const tabs = ['模型配置', '连接器', '接入渠道', '参数', '用量统计', '备份', '状态']

const providers = ref([])
const assignment = ref({})
const connectors = ref([])
const platforms = ref([])
const params = ref({})
const schema = ref([])
const usage = ref({})
const distribution = ref({})
const trend = ref([])
const trendPeriod = ref('30d')
// 用量筛选（空串=全部）；选项列表取自首次无筛选的分布数据，筛选后不再收缩
const usageSource = ref('')
const usageModel = ref('')
const sourceOptions = ref([])
const modelOptions = ref([])
const hoveredIdx = ref(null)
const monthCost = ref(null)
const usageLoadError = ref(false)

// 趋势图 SVG 计算 (bar chart)
const TREND_H = 140
const TREND_W = 520
// 绘图区左起点：Y 轴竖线在 49、刻度标签右对齐到 44，柱子须从轴右侧开始，避免压轴/压标签
const PLOT_X = 56
const BAR_W = computed(() => {
  const n = trend.value.length || 1
  return Math.max(6, Math.min(36, (TREND_W - PLOT_X - 8) / n - 8))
})
const trendMax = computed(() => trend.value.reduce((m, t) => Math.max(m, t.tokens || 0), 0))
// 模型配色：按全区间总消耗降序给模型分配稳定颜色，同一模型跨柱色一致
const MODEL_PALETTE = [
  '#4f9df7', '#34c759', '#ff9f0a', '#af52de', '#ff6482',
  '#5ac8fa', '#ffd60a', '#30d158', '#bf5af2', '#8e8e93',
]
const modelColorMap = computed(() => {
  const totals = {}
  for (const t of trend.value)
    for (const m of (t.models || [])) totals[m.name] = (totals[m.name] || 0) + m.tokens
  const names = Object.keys(totals).sort((a, b) => totals[b] - totals[a])
  const map = {}
  names.forEach((n, i) => { map[n] = MODEL_PALETTE[i % MODEL_PALETTE.length] })
  return map
})
// 图例：只展示当前区间有消耗的模型
const trendLegend = computed(() =>
  Object.keys(modelColorMap.value).map(name => ({ name, color: modelColorMap.value[name] })))
const trendBars = computed(() => {
  const mx = trendMax.value || 1
  const y0 = TREND_H - 20  // bottom baseline (above labels)
  const usable = y0 - 10
  return trend.value.map((t, i) => {
    const x = PLOT_X + i * (BAR_W.value + 8)
    // 堆叠段：自底向上按模型依次堆叠，段高按 token 占总高比例
    const models = t.models && t.models.length
      ? t.models
      : (t.tokens > 0 ? [{ name: '未知', tokens: t.tokens }] : [])
    let cursor = y0
    const segs = models.map(m => {
      const h = (m.tokens / mx) * usable
      cursor -= h
      return {
        name: m.name, tokens: m.tokens, x, y: cursor, height: h,
        width: BAR_W.value, color: modelColorMap.value[m.name] || 'var(--succtx)'
      }
    })
    const totalH = Math.max(0, (t.tokens / mx) * usable)
    return { ...t, x, width: BAR_W.value, segs, y: y0 - totalH, height: totalH }
  })
})

function dateLabel(t) {
  // 30d 返回 "07-20"、month 返回 "20日"、year 返回 "7月"；日粒度统一成 月/日
  const raw = t.label || ''
  return raw.replace(/^(\d{2})-(\d{2}).*/, '$1/$2')
}

// 堆叠柱顶段：只圆上方两角，下方直角，与下段无缝衔接（保证整体感）
function segPath(s) {
  const r = Math.min(2, s.width / 2, s.height)
  const { x, y, width: w, height: h } = s
  return `M${x},${y + r} Q${x},${y} ${x + r},${y} L${x + w - r},${y} `
    + `Q${x + w},${y} ${x + w},${y + r} L${x + w},${y + h} L${x},${y + h} Z`
}

// 横轴标签稀疏化：年视图（12个月）全部显示，日粒度每 3 个显示一个
function labelVisible(i) {
  if (trendPeriod.value === 'year') return true
  return i % 3 === 0 || i === trendBars.value.length - 1
}

// hover 即时浮层：固定在柱子正上方，展示当天总量 + 逐模型明细，贴边时自动内收防溢出
const hoverTip = computed(() => {
  if (hoveredIdx.value === null) return null
  const b = trendBars.value[hoveredIdx.value]
  if (!b) return null
  const rows = (b.models || []).map(m => ({
    name: m.name, value: formatNum(m.tokens), color: modelColorMap.value[m.name] || 'var(--succtx)'
  }))
  const w = 168
  const lineH = 15
  const h = 22 + rows.length * lineH  // 标题行 + 逐模型行
  const x = Math.min(Math.max(b.x + b.width / 2 - w / 2, 2), TREND_W - w - 2)
  const y = Math.max(2, b.y - h - 6)
  return { x, y, w, h, lineH, title: `${dateLabel(b)}·共 ${formatNum(b.tokens)}`, rows }
})
const status = ref(null)
const showAddProvider = ref(false)
const newProvider = ref({ provider_type: 'openai_compatible', display_name: '', base_url: '', api_key: '', model_id: '', input_price: null, output_price: null, context_window: 128000 })

async function loadProviders() { providers.value = await api.get('/settings/providers'); assignment.value = await api.get('/settings/model-assignment') }

// 任务-模型分配：名称与说明（每个类型下展示用途解释）
const TASK_NAMES = { chat: '对话模型', agent: '系统 Agent 模型', intent: '意图识别模型', embedding: 'Embedding 模型', vision: '视觉模型（图片解析）' }
const TASK_DESC = {
  chat: '负责日常对话的回复生成与文档撰写，直接决定回答质量与语言风格',
  agent: '用于记忆蒸馏、上下文压缩、被动回顾、画像重建等系统后台任务',
  intent: '留空则跟随系统 Agent/对话模型；建议配非推理小模型，可大幅降低每轮对话首响应延迟',
  embedding: '将记忆与知识库内容向量化，支撑语义检索；切换后需重新向量化全部记忆',
  vision: '留空则跟随对话模型；用于知识库图片/文档内嵌图解析',
}
async function addProvider() {
  await api.post('/settings/providers', newProvider.value)
  showAddProvider.value = false
  await loadProviders(); toast.push('success', '已添加')
}
async function delProvider(id) { if (!await confirm.ask({ message: '删除该模型？', danger: true })) return; await api.del('/settings/providers/' + id); await loadProviders() }
async function setAssign(task, pid) { await api.put('/settings/model-assignment', { [task + '_model']: pid }); toast.push('success', '已保存') }

// 编辑 Provider
const showEdit = ref(false)
const editData = ref({})
const showEditKey = ref(false)
const showAddKey = ref(false)
async function openEdit(p) {
  editData.value = {
    id: p.id, display_name: p.display_name, provider_type: p.provider_type,
    base_url: p.base_url, model_id: p.model_id, input_price: p.input_price,
    output_price: p.output_price, context_window: p.context_window || 128000, api_key: '',
  }
  showEditKey.value = false
  try {
    const d = await api.get('/settings/providers/' + p.id + '/key')
    editData.value.api_key = d.api_key || ''
  } catch { /* 取不到就留空 */ }
  showEdit.value = true
}
async function saveEdit() {
  const body = { ...editData.value }
  if (!body.api_key) delete body.api_key   // 留空则不修改密钥
  await api.put('/settings/providers/' + editData.value.id, body)
  showEdit.value = false
  await loadProviders(); toast.push('success', '已保存')
}
async function testConn(cfg) {
  const r = await api.post('/settings/providers/test-connection', cfg)
  toast.push(r.ok ? 'success' : 'error', r.ok ? '连接成功' : ('连接失败：' + (r.error || '未知错误')))
}

async function loadConnectors() { connectors.value = await api.get('/settings/connectors') }
async function toggleConn(c) { await api.post(`/settings/connectors/${c.id}/toggle`, { enabled: c.status !== 'connected' }); await loadConnectors() }
async function delConn(id) { if (!await confirm.ask({ message: '删除连接器？', danger: true })) return; await api.del('/settings/connectors/' + id); await loadConnectors() }
async function refreshConn(id) { await api.post(`/settings/connectors/${id}/refresh-tools`, {}); await loadConnectors(); toast.push('success', '工具已刷新') }

async function loadPlatforms() { platforms.value = await api.get('/settings/platforms') }
async function enablePlatform(id) { await api.post(`/settings/platforms/${id}/enable`, {}); await loadPlatforms() }
async function disablePlatform(id) { await api.post(`/settings/platforms/${id}/disable`, {}); await loadPlatforms() }
async function resumePlatform(id) { await api.post(`/settings/platforms/${id}/resume`, {}); await loadPlatforms(); toast.push('success', '已恢复') }

async function loadParams() { const d = await api.get('/settings/params'); params.value = d.params; schema.value = d.schema }
async function saveParams() { await api.put('/settings/params', params.value); toast.push('success', '设置已保存，部分参数将在下一轮对话生效') }
async function resetParams() { if (!await confirm.ask('恢复全部参数默认值？')) return; params.value = await api.post('/settings/params/reset', {}); toast.push('success', '已恢复默认') }

async function loadUsage() {
  try {
    const f = '&source=' + encodeURIComponent(usageSource.value) + '&model=' + encodeURIComponent(usageModel.value)
    usage.value = await api.get('/settings/usage/summary?' + f.slice(1));
    distribution.value = await api.get('/settings/usage/distribution?' + f.slice(1));
    trend.value = await api.get('/settings/usage/trend?period=' + trendPeriod.value + f);
    monthCost.value = await api.get('/settings/usage/month-cost');
    // 无筛选时刷新下拉选项，避免筛选后选项被过滤到只剩当前项
    if (!usageSource.value && !usageModel.value) {
      sourceOptions.value = (distribution.value.by_source || []).map(s => s.name)
      modelOptions.value = (distribution.value.by_model || []).map(m => m.name)
    }
    usageLoadError.value = false
  } catch { usageLoadError.value = true }
}
async function switchTrend(p) { trendPeriod.value = p; hoveredIdx.value = null; await loadUsage() }
async function loadBackups() { backups.value = await api.get('/settings/backups') }
// 备份自定义标签
const backupLabel = ref('')
const showBackupLabel = ref(false)
async function createBackup() {
  backupLabel.value = ''
  showBackupLabel.value = true
}
async function doCreateBackup() {
  await api.post('/settings/backups/create', { label: backupLabel.value || undefined })
  showBackupLabel.value = false
  await loadBackups(); toast.push('success', '备份完成')
}
async function exportData() {
  const r = await api.post('/settings/backups/export', {})
  toast.push('success', '已导出：' + (r.path || ''))
}
const importFileInput = ref(null)
async function importData(e) {
  const f = e.target.files?.[0]
  if (!f) return
  try {
    const form = new FormData(); form.append('file', f)
    await api.upload('/settings/backups/import', form)
    toast.push('success', '导入完成，索引已重建')
    await loadBackups()
  } catch { } finally { e.target.value = '' }
}
async function restoreBackup(id) { if (!await confirm.ask('恢复该备份？将先自动保护性备份当前数据。')) return; await api.post('/settings/backups/restore', { backup_id: id }); toast.push('success', '恢复完成') }
async function loadStatus() { status.value = await api.get('/settings/status'); tasks.value = await api.get('/settings/tasks') }

// 定时任务 + 日志
const tasks = ref([])
const taskLogs = ref(null)
async function runTask(tid) { await api.post(`/settings/tasks/${tid}/run`, {}); toast.push('success', '已执行'); await loadStatus() }
async function showTaskLogs(tid) { taskLogs.value = { id: tid, logs: await api.get(`/settings/tasks/${tid}/logs`) } }

// Embedding 切换预估
const embEstimate = ref(null)
const pendingEmbPid = ref('')
async function onEmbeddingChange(pid) {
  pendingEmbPid.value = pid
  embEstimate.value = await api.post('/settings/embedding/estimate', { target_provider_id: pid })
}
async function confirmMigrate() {
  await api.post('/settings/embedding/migrate', { target_provider_id: pendingEmbPid.value, confirm: true })
  embEstimate.value = null
  toast.push('success', '迁移已开始，可在状态页查看进度')
}

// 连接器添加
const showAddConn = ref(false)
const newConn = ref({ name: '', transport: 'stdio', command: 'npx', args: '', env: '', url: '', timeout: 120 })
async function addConnector() {
  const cfg = newConn.value.transport === 'stdio'
    ? { command: newConn.value.command, args: parseJson(newConn.value.args, []), env: parseJson(newConn.value.env, {}) }
    : { url: newConn.value.url }
  await api.post('/settings/connectors', { name: newConn.value.name, transport: newConn.value.transport, config: cfg, timeout: newConn.value.timeout })
  showAddConn.value = false; await loadConnectors(); toast.push('success', '已添加')
}
function parseJson(s, def) { try { return s ? JSON.parse(s) : def } catch { return def } }

// 连接器：测试连接（仅验证）——后端失败时返回 code:200+ok:false，必须检查 r.ok 而非靠异常
async function testConnector() {
  const cfg = newConn.value.transport === 'stdio'
    ? { command: newConn.value.command, args: parseJson(newConn.value.args, []), env: parseJson(newConn.value.env, {}) }
    : { url: newConn.value.url }
  const r = await api.post('/settings/connectors/test', { name: newConn.value.name, transport: newConn.value.transport, config: cfg })
  if (r.ok) toast.push('success', `连接测试成功，发现 ${r.tool_count ?? 0} 个工具`)
  else toast.push('error', '连接测试失败：' + (r.error || '未知错误'))
}

// 连接器：保存直接复用 addConnector（单一入口，不再冗余包装）

// 接入渠道配置
const PLATFORM_MAP = { web: 'Web', feishu: '飞书', telegram: 'Telegram', dingtalk: '钉钉', wecom: '企业微信', weixin: '微信' }
const showChannelCfg = ref(false)
const newChannel = ref({ platform_type: 'feishu', bot_token: '', app_secret: '', whitelist_user_id: '', callback_url: '' })
async function addChannel() {
  // 必填校验：未填凭证直接拦截，不发请求
  if (!newChannel.value.bot_token.trim()) {
    toast.push('error', '请先填写 Bot Token'); return
  }
  if (['feishu', 'dingtalk', 'wecom'].includes(newChannel.value.platform_type) && !newChannel.value.app_secret.trim()) {
    toast.push('error', '请先填写 App Secret'); return
  }
  const r = await api.post('/settings/platforms', newChannel.value)
  showChannelCfg.value = false
  await api.post(`/settings/platforms/${r.id}/enable`, {})
  await loadPlatforms(); toast.push('success', '已配置并启用（已自动禁用其他 IM）')
}

// 微信渠道扫码绑定（iLink 直连：二维码 → 轮询确认 → 自动启用）
const showWeixinScan = ref(false)
const wxScan = ref({ qrcode: '', img: '', url: '', status: 'pending', busy: false })
let wxPollTimer = null
function wxImgSrc() {
  const img = wxScan.value.img
  if (!img) return ''
  // 兼容三种形态：data URL / http(s) URL（微信小程序码页）/ 纯 base64 图片
  if (img.startsWith('data:') || img.startsWith('http')) return img
  return 'data:image/png;base64,' + img
}
function stopWxPoll() {
  if (wxPollTimer) { clearInterval(wxPollTimer); wxPollTimer = null }
}
async function refreshWeixinQrcode() {
  wxScan.value.busy = true
  try {
    const r = await api.post('/settings/platforms/weixin/qrcode', {})
    wxScan.value.qrcode = r.qrcode || ''
    wxScan.value.img = r.qrcode_img || ''
    wxScan.value.url = r.qrcode_url || ''
    startWxPoll()
  } finally { wxScan.value.busy = false }
}
async function copyWxUrl() {
  try {
    await navigator.clipboard.writeText(wxScan.value.url)
    toast.push('success', '扫码链接已复制，请在微信中打开')
  } catch { toast.push('error', '复制失败，请手动复制链接') }
}
function startWxPoll() {
  stopWxPoll()
  // 防重入：扫码状态接口为长轮询（服务端 hold 至状态变化），
  // 上一请求未完成时不发起新请求，避免连接堆积
  let polling = false
  wxPollTimer = setInterval(async () => {
    if (polling) return
    polling = true
    try {
      const r = await api.get(`/settings/platforms/weixin/qrcode/status?qrcode=${encodeURIComponent(wxScan.value.qrcode)}`)
      wxScan.value.status = r.status || 'pending'
      if (r.status === 'confirmed') {
        stopWxPoll()
        // 从平台列表取真实 ID（不硬编码 weixin_1）；列表未加载时先拉取
        if (!platforms.value.length) await loadPlatforms()
        const wx = platforms.value.find(p => p.platform_type === 'weixin')
        if (wx) await api.post(`/settings/platforms/${wx.id}/enable`, {})
        showWeixinScan.value = false
        await loadPlatforms()
        toast.push('success', '微信渠道已绑定并启用（已自动禁用其他 IM）')
      } else if (r.status === 'expired') {
        stopWxPoll()
        toast.push('warning', '二维码已过期，请重新获取')
      }
    } catch (e) { /* client.js 已统一 toast */ } finally { polling = false }
  }, 2000)
}
async function openWeixinScan() {
  showWeixinScan.value = true
  wxScan.value = { qrcode: '', img: '', url: '', status: 'pending', busy: false }
  await refreshWeixinQrcode()
}
onUnmounted(stopWxPoll)

// 添加渠道卡片点击：微信走扫码绑定，其余走配置表单
function clickAddChannel(pt) {
  if (pt === 'weixin') { openWeixinScan() }
  else { newChannel.value.platform_type = pt; showChannelCfg.value = true }
}

async function testChannel() {
  // 同上：后端失败不抛异常，需检查 r.ok
  const r = await api.post('/settings/platforms/test', newChannel.value)
  if (r.ok) toast.push('success', '连接测试成功')
  else toast.push('error', '连接测试失败：' + (r.error || '未知错误'))
}

// 编辑已录入渠道：回显现有配置（含解密凭证），平台类型不可改
const showChannelEdit = ref(false)
const showEditSecret = ref(false)
const editChannel = ref({ id: '', platform_type: '', bot_token: '', app_secret: '', whitelist_user_id: '', callback_url: '' })
async function openChannelEdit(p) {
  const d = await api.get(`/settings/platforms/${p.id}/detail`)
  editChannel.value = {
    id: d.id, platform_type: d.platform_type, bot_token: d.bot_token || '',
    app_secret: d.app_secret || '', whitelist_user_id: d.whitelist_user_id || '',
    callback_url: d.callback_url || '',
  }
  showEditSecret.value = false
  showChannelEdit.value = true
}
async function saveChannelEdit() {
  if (editChannel.value.platform_type !== 'weixin' && !editChannel.value.bot_token.trim()) { toast.push('error', '请先填写 Bot Token'); return }
  if (editChannel.value.platform_type !== 'weixin' && ['feishu', 'dingtalk', 'wecom'].includes(editChannel.value.platform_type) && !editChannel.value.app_secret.trim()) {
    toast.push('error', '请先填写 App Secret'); return
  }
  await api.put(`/settings/platforms/${editChannel.value.id}`, editChannel.value)
  showChannelEdit.value = false
  await loadPlatforms(); toast.push('success', '已保存')
}
async function testEditChannel() {
  const r = await api.post('/settings/platforms/test', editChannel.value)
  if (r.ok) toast.push('success', '连接测试成功')
  else toast.push('error', '连接测试失败：' + (r.error || '未知错误'))
}

const groups = ['memory', 'conversation', 'cost', 'retrieval', 'visualization', 'other']
const groupNames = { memory: '记忆参数', conversation: '对话参数', cost: '成本控制', retrieval: '检索与去重', visualization: '可视化', other: '其他' }
// 用量来源中文映射（未知值兜底显示原文）
const SOURCE_NAMES = {
  main_chat: 'AI对话',
  agent: '工具prompt',
  system_agent: '系统prompt',
  title_gen: '标题生成',
  embedding: '向量分析',
  vision: '图片解析',
  intent_parse: '意图解析',
  tool_infer: '工具推断',
  attention_focus: '注意力聚焦',
  converge_intent: '意图收敛',
  gap_detect: '缺口检测',
  honest_clarify: '诚实澄清',
  mood: '情绪分析',
  quick_intent: '快速意图',
  replan: '重规划',
  profile_conflict: '画像冲突扫描'
}
function sourceName(s) { return SOURCE_NAMES[s] || s }
const effectNames = { immediate: '立即生效', next_turn: '下一轮对话生效', next_session: '下次会话生效' }
const enumLabels = { remind_only: '仅提醒（不阻断）' }
function enumLabel(o) { return enumLabels[o] || o }
function schemaByGroup(g) { return schema.value.filter(s => s.group === g).sort((a, b) => a.order - b.order) }
function formatNum(n) {
  if (!n) return '0'
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M'
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K'
  return String(n)
}
const backups = ref([])

// 耗时按时分秒可读展示：不足 1 秒显示毫秒，其余省略为 0 的高位单位（如 15分23秒、4秒）
function formatDuration(ms) {
  if (ms == null || isNaN(ms)) return '-'
  if (ms === 0) return '<1毫秒'
  if (ms < 1000) return `${ms}毫秒`
  let s = Math.floor(ms / 1000)
  const h = Math.floor(s / 3600); s %= 3600
  const m = Math.floor(s / 60); s %= 60
  let out = ''
  if (h) out += `${h}时`
  if (m) out += `${m}分`
  if (s || !out) out += `${s}秒`
  return out
}

function taskLabel(s) {
  return {
    completed: '已完成', success: '已完成', running: '运行中',
    failed: '失败', skipped: '已跳过'
  }[s] || '待运行'
}
function taskBadge(s) {
  if (s === 'completed' || s === 'success') return 'badge-g'
  if (s === 'running') return 'badge-a'
  if (s === 'failed') return 'badge-r'
  return 'badge-n'
}

// 系统整体状态中文化 + 横幅配色（后端现为真实检测，可能返回 degraded/unhealthy）
const OVERALL_NAMES = { healthy: '正常', degraded: '部分降级', unhealthy: '异常' }
function overallLabel(s) { return OVERALL_NAMES[s] || s }
function overallStyle(s) {
  if (s === 'unhealthy') return { background: 'var(--dangbg)', color: 'var(--dangtx)' }
  if (s === 'degraded') return { background: 'var(--warnbg)', color: 'var(--warntx)' }
  return { background: 'var(--succbg)', color: 'var(--succtx)' }
}

function selectTab(i) {
  tab.value = i
  const loaders = [loadProviders, loadConnectors, loadPlatforms, loadParams, loadUsage, loadBackups, loadStatus]
  loaders[i]()
}
onMounted(() => selectTab(0))
onActivated(() => selectTab(tab.value))
</script>

<template>
  <h1>系统设置</h1>
  <div class="tabs-sticky">
    <div class="tabs">
      <button v-for="(t, i) in tabs" :key="i" class="tab" :class="{ active: tab === i }" @click="selectTab(i)">{{ t
      }}</button>
    </div>
  </div>

  <!-- 模型配置 -->
  <div v-if="tab === 0">
    <div class="section-title">任务-模型分配</div>
    <div class="cw">
      <div v-for="task in ['chat', 'agent', 'intent', 'embedding', 'vision']" :key="task" class="row"
        style="padding:8px 0;border-bottom:1px solid var(--bd)">
        <div style="flex:1;min-width:0;padding-right:16px">
          <b>{{ TASK_NAMES[task] }}</b>
          <div class="muted">{{ TASK_DESC[task] }}</div>
        </div>
        <select :value="assignment[task + '_model']?.provider_id || ''"
          @change="e => task === 'embedding' ? onEmbeddingChange(e.target.value) : setAssign(task, e.target.value)"
          style="min-width:180px">
          <option value="">未配置</option>
          <option v-for="p in providers" :key="p.id" :value="p.id">{{ p.display_name }}</option>
        </select>
      </div>
    </div>
    <div class="section-title mt">已添加的模型</div>
    <div v-for="p in providers" :key="p.id" class="cw">
      <div class="row">
        <div class="fg" style="gap:8px"><span class="dot" style="background:var(--succtx)"></span>
          <div><b>{{ p.display_name }}</b>
            <div class="muted">{{ p.base_url }}</div>
          </div>
        </div>
        <div class="fg" style="gap:12px"><span class="muted">¥{{ p.input_price || 0 }}/M · ¥{{ p.output_price || 0
            }}/M</span>
          <button class="btn-sm" :disabled="busy('editP' + p.id)"
            @click="run('editP' + p.id, () => openEdit(p))">编辑</button>
          <button class="btn-sm btn-danger" @click="delProvider(p.id)">删除</button>
        </div>
      </div>
    </div>
    <div class="cw" style="border:1px dashed var(--bd);text-align:center;cursor:pointer"
      @click="showAddProvider = true">
      <i class="ti ti-plus"></i> 添加新的 LLM Provider
    </div>
  </div>

  <!-- 连接器 -->
  <div v-else-if="tab === 1">
    <div class="row" style="margin-bottom:12px"><b>已连接的系统</b>
      <button class="btn-sm" @click="showAddConn = true">+ 添加</button>
    </div>
    <div v-if="!connectors.length" class="empty"><i class="ti ti-plug"></i>还没有连接任何外部系统<br>通过 MCP 协议接入 GitHub、ERP 等</div>
    <div v-for="c in connectors" :key="c.id" class="cw">
      <div class="row">
        <div class="fg" style="gap:8px"><i class="ti ti-plug" style="color:var(--acctx)"></i>
          <div><b>{{ c.name }}</b>
            <div class="muted">{{ c.transport }} · {{ c.tool_count }} 个工具</div>
          </div>
        </div>
        <span class="badge" :class="c.status === 'connected' ? 'badge-g' : 'badge'">{{ c.status === 'connected' ? '已连接'
          : '已停用' }}</span>
      </div>
      <div class="fg" style="gap:6px;margin-top:10px">
        <button class="btn-xs" :disabled="busy('togC' + c.id)" @click="run('togC' + c.id, () => toggleConn(c))">{{
          c.status === 'connected' ? '断开' : '连接' }}</button>
        <button class="btn-xs" :disabled="busy('refC' + c.id)" @click="run('refC' + c.id, () => refreshConn(c.id))"><i
            v-if="busy('refC' + c.id)" class="ti ti-loader-2"></i> 刷新工具</button>
        <button class="btn-xs btn-danger" @click="delConn(c.id)">删除</button>
      </div>
    </div>
  </div>

  <!-- 接入渠道 -->
  <div v-else-if="tab === 2">
    <div class="muted" style="margin-bottom:12px">同时只能启用一个 IM 平台</div>
    <div v-for="p in platforms" :key="p.id" class="cw">
      <div class="row">
        <div class="fg" style="gap:8px">
          <span class="dot" :style="{ background: p.status === 'healthy' ? 'var(--succtx)' : 'var(--dangtx)' }"></span>
          <div><b>{{ PLATFORM_MAP[p.platform_type] || p.platform_type }}</b>
            <div class="muted">{{ p.platform_type === 'weixin' && !p.bound ? '未绑定：请点击「扫码绑定」完成接入' : p.detail }}</div>
            <div v-if="p.failure_reason" class="muted" style="color:var(--dangtx)">{{ p.failure_reason }}</div>
          </div>
        </div>
        <div class="fg" style="gap:8px">
          <span v-if="p.enabled" class="badge badge-g">已启用</span>
          <button v-if="p.platform_type === 'weixin' && !p.bound" class="btn-sm"
            :disabled="busy('wxBind')" @click="run('wxBind', openWeixinScan)">扫码绑定</button>
          <template v-else>
            <button v-if="p.id !== 'web_default'" class="btn-sm" :disabled="busy('editCh' + p.id)"
              @click="run('editCh' + p.id, () => openChannelEdit(p))">编辑</button>
            <button v-if="p.id !== 'web_default' && !p.enabled" class="btn-sm" :disabled="busy('plat' + p.id)"
              @click="run('plat' + p.id, () => enablePlatform(p.id))">启用</button>
            <button v-if="p.id !== 'web_default' && p.enabled" class="btn-sm" :disabled="busy('plat' + p.id)"
              @click="run('plat' + p.id, () => disablePlatform(p.id))">禁用</button>
            <button v-if="p.status === 'paused'" class="btn-sm" :disabled="busy('plat' + p.id)"
              @click="run('plat' + p.id, () => resumePlatform(p.id))">恢复</button>
          </template>
        </div>
      </div>
    </div>
    <div class="section-title mt">添加渠道</div>
    <div class="g3">
      <div v-for="pt in ['feishu', 'telegram', 'dingtalk', 'wecom', 'weixin']" :key="pt" class="cw"
        style="text-align:center;cursor:pointer;padding:20px"
        @click="clickAddChannel(pt)">
        <i class="ti ti-plug" style="font-size:var(--icon-lg);color:var(--acctx)"></i>
        <div style="font-weight:500;margin-top:8px">{{ PLATFORM_MAP[pt] || pt }}</div>
        <div class="muted">{{ pt === 'weixin' ? 'ClawBot 扫码绑定' : 'Bot 私聊接入' }}</div>
      </div>
    </div>
  </div>

  <!-- 参数（schema 驱动） -->
  <div v-else-if="tab === 3">
    <div v-for="g in groups" :key="g" class="cw">
      <div class="section-title">{{ groupNames[g] }}</div>
      <div v-for="s in schemaByGroup(g)" :key="s.key" class="row" style="margin-bottom:12px;align-items:flex-start">
        <div style="flex:1;min-width:0;padding-right:16px">
          <b>{{ s.label }}</b>
          <div v-if="s.desc" class="muted" style="margin-top:2px;line-height:1.5">{{ s.desc }}</div>
          <div class="muted" style="margin-top:2px;opacity:.7">{{ effectNames[s.effect] }}<span
              v-if="s.min !== undefined"> · 取值范围 {{ s.min }}–{{ s.max ?? '∞' }}</span></div>
        </div>
        <div style="flex-shrink:0">
          <input v-if="s.type === 'bool'" type="checkbox" v-model="params[s.key]" />
          <select v-else-if="s.type === 'enum'" v-model="params[s.key]">
            <option v-for="o in s.options" :key="o" :value="o">{{ enumLabel(o) }}</option>
          </select>
          <input v-else v-model.number="params[s.key]" style="width:120px;text-align:center" />
        </div>
      </div>
    </div>
    <div class="fg" style="justify-content:flex-end;gap:8px">
      <button @click="resetParams">恢复默认</button>
      <button class="btn-primary" :disabled="busy('saveParams')" @click="run('saveParams', saveParams)"><i
          v-if="busy('saveParams')" class="ti ti-loader-2"></i> 保存设置</button>
    </div>
  </div>

  <!-- 用量统计 -->
  <div v-else-if="tab === 4">
    <div v-if="usage.today_ratio >= 100 || usage.month_ratio >= 100" class="banner"
      style="background:var(--dangbg);color:var(--dangtx);margin-bottom:12px">
      {{ usage.today_ratio >= 100 ? '今日' : '本月' }} Token 预算已用完，继续对话将超额（当前策略：仅提醒，不阻断）
    </div>
    <div v-if="usageLoadError" class="empty"><i class="ti ti-chart-bar"></i>用量数据加载失败</div>
    <template v-else>
      <!-- 口径筛选：来源/模型，作用于下方全部统计卡片与图表 -->
      <div class="fg" style="gap:8px;margin-bottom:12px;justify-content:flex-end">
        <select v-model="usageSource" style="font-size:var(--fs-sm)" @change="loadUsage">
          <option value="">全部来源</option>
          <option v-for="s in sourceOptions" :key="s" :value="s">{{ sourceName(s) }}</option>
        </select>
        <select v-model="usageModel" style="font-size:var(--fs-sm)" @change="loadUsage">
          <option value="">全部模型</option>
          <option v-for="m in modelOptions" :key="m" :value="m">{{ m }}</option>
        </select>
      </div>
      <div class="g3" style="margin-bottom:12px">
        <div class="card">
          <div class="label">今日</div>
          <div class="val">{{ formatNum(usage.today_used) }}</div>
        </div>
        <div class="card">
          <div class="label">本月</div>
          <div class="val">{{ formatNum(usage.month_used) }}</div>
        </div>
        <div class="card">
          <div class="label">本月费用</div>
          <div class="val" style="color:var(--sec)">¥{{ monthCost?.month_cost ?? '—' }}</div>
        </div>
      </div>
      <!-- 预算条 -->
      <div class="cw">
        <div class="row" style="margin-bottom:8px"><span class="muted">今日预算</span><span
            :style="{ color: (usage.today_ratio || 0) >= 80 ? 'var(--warntx)' : '' }">{{ usage.today_ratio || 0
            }}%</span></div>
        <div class="progress">
          <div
            :style="{ background: (usage.today_ratio || 0) >= (usage.alert_ratio || 80) ? 'var(--dangtx)' : 'var(--succtx)', width: Math.min(100, usage.today_ratio || 0) + '%' }">
          </div>
        </div>
        <div class="row" style="margin-top:14px;margin-bottom:8px"><span class="muted">本月预算</span><span
            :style="{ color: (usage.month_ratio || 0) >= 80 ? 'var(--warntx)' : '' }">{{ usage.month_ratio || 0
            }}%</span></div>
        <div class="progress">
          <div
            :style="{ background: (usage.month_ratio || 0) >= (usage.alert_ratio || 80) ? 'var(--dangtx)' : 'var(--succtx)', width: Math.min(100, usage.month_ratio || 0) + '%' }">
          </div>
        </div>
      </div>
      <!-- 趋势图 -->
      <div class="cw" style="margin-top:12px">
        <div class="fg" style="justify-content:space-between;margin-bottom:14px">
          <div class="fg" style="gap:4px">
            <button v-for="p in [{ k: '30d', n: '近 30 天' }, { k: 'month', n: '本月' }, { k: 'year', n: '当年' }]" :key="p.k"
              class="chip" :class="{ active: trendPeriod === p.k }" @click="switchTrend(p.k)">{{
                p.n }}</button>
          </div>
        </div>
        <svg v-if="trend.length" :viewBox="'0 0 ' + TREND_W + ' ' + TREND_H" style="width:100%"
          preserveAspectRatio="xMidYMid meet">
          <!-- grid lines -->
          <line v-for="y in [0, .25, .5, .75]" :key="y" :x1="50" :y1="TREND_H - 20 - (TREND_H - 30) * y"
            :x2="TREND_W - 8" :y2="TREND_H - 20 - (TREND_H - 30) * y" stroke="var(--bd)" stroke-dasharray="3,3"
            stroke-width=".5" />
          <line x1="49" y1="10" x2="49" :y2="TREND_H - 20" stroke="var(--bd)" stroke-width=".5" />
          <!-- Y 轴标签 -->
          <text v-for="y in [0, .25, .5, .75, 1]" :key="'yl' + y" :x="44" :y="TREND_H - 20 - (TREND_H - 30) * y + 3"
            text-anchor="end" font-size="8" fill="var(--muted)">{{ formatNum(Math.round(trendMax * y)) }}</text>
          <!-- 零值柱不渲染；堆叠段顶段圆上角（path）、其余直角（rect），保证整体感 -->
          <g v-for="(b, i) in trendBars" :key="b.label" v-show="b.tokens > 0" @mouseenter="hoveredIdx = i"
            @mouseleave="hoveredIdx = null">
            <template v-for="(s, si) in b.segs" :key="si">
              <path v-if="si === b.segs.length - 1" :d="segPath(s)" :fill="s.color"
                :style="{ opacity: hoveredIdx === i ? .75 : 1 }" />
              <rect v-else :x="s.x" :y="s.y" :width="s.width" :height="s.height" :fill="s.color"
                :style="{ opacity: hoveredIdx === i ? .75 : 1 }" />
            </template>
          </g>
          <!-- hover 即时浮层（跟随柱子，pointer-events 关闭防闪烁，展示多模型明细；颜色全部走 token） -->
          <g v-if="hoverTip" pointer-events="none">
            <rect :x="hoverTip.x" :y="hoverTip.y" :width="hoverTip.w" :height="hoverTip.h" rx="4" fill="var(--surface-3)"
              opacity=".95" />
            <text :x="hoverTip.x + 8" :y="hoverTip.y + 14" font-size="10" fill="var(--fg)" font-weight="600">{{
              hoverTip.title }}</text>
            <g v-for="(r, ri) in hoverTip.rows" :key="ri">
              <rect :x="hoverTip.x + 8" :y="hoverTip.y + 20 + ri * hoverTip.lineH + 2" width="7" height="7" rx="1.5"
                :fill="r.color" />
              <text :x="hoverTip.x + 19" :y="hoverTip.y + 20 + ri * hoverTip.lineH + 8.5" font-size="9" fill="var(--sec)">{{
                r.name }}</text>
              <text :x="hoverTip.x + hoverTip.w - 8" :y="hoverTip.y + 20 + ri * hoverTip.lineH + 8.5" text-anchor="end"
                font-size="9" fill="var(--fg)">{{ r.value }}</text>
            </g>
          </g>
          <!-- 横轴标签：年视图逐月全显，日粒度每 3 个显示一个 -->
          <text v-for="(b, i) in trendBars" :key="b.label + 'l'" :x="b.x + b.width / 2" :y="TREND_H - 3"
            text-anchor="middle" font-size="9" fill="var(--muted)" v-show="labelVisible(i)">{{ dateLabel(b) }}</text>
        </svg>
        <!-- 模型图例：颜色 ↔ 模型名，与堆叠段颜色一致 -->
        <div v-if="trendLegend.length" class="fg" style="gap:12px;flex-wrap:wrap;margin-top:8px;font-size:var(--fs-xs)">
          <span v-for="lg in trendLegend" :key="lg.name" class="fg" style="gap:4px">
            <span
              :style="{ width: '9px', height: '9px', borderRadius: '2px', background: lg.color, display: 'inline-block' }"></span>
            <span class="muted">{{ lg.name }}</span>
          </span>
        </div>
        <div v-else class="muted" style="text-align:center;padding:20px 0">暂无数据</div>
      </div>
      <!-- 来源 / 模型分布 -->
      <div class="g2" style="margin-top:12px">
        <div class="cw"><b>按来源</b>
          <div v-for="s in (distribution.by_source || [])" :key="s.name" class="row" style="margin-top:8px">
            <span>{{ sourceName(s.name) }}</span><span>{{ formatNum(s.tokens) }}</span>
          </div>
          <div v-if="!distribution.by_source?.length" class="muted">暂无数据</div>
        </div>
        <div class="cw"><b>按模型</b>
          <div v-for="m in (distribution.by_model || [])" :key="m.name" class="row" style="margin-top:8px">
            <span>{{ m.name }}</span><span>{{ formatNum(m.tokens) }}</span>
          </div>
          <div v-if="!distribution.by_model?.length" class="muted">暂无数据</div>
        </div>
      </div>
    </template>
  </div>

  <!-- 备份 -->
  <div v-else-if="tab === 5">
    <div class="row" style="margin-bottom:12px"><b>备份记录</b>
      <div class="fg" style="gap:6px">
        <button class="btn-sm" @click="createBackup">立即备份</button>
        <button class="btn-sm" :disabled="busy('export')" @click="run('export', exportData)"><i v-if="busy('export')"
            class="ti ti-loader-2"></i> 导出 JSON</button>
        <button class="btn-sm" @click="importFileInput?.click()">导入</button>
        <input ref="importFileInput" type="file" accept=".zip" style="display:none" @change="importData" />
      </div>
    </div>
    <div v-if="!backups.length" class="empty"><i class="ti ti-database"></i>尚无备份记录<br>系统每天凌晨 2 点自动备份</div>
    <div v-for="b in backups" :key="b.backup_id" class="cw">
      <div class="row">
        <div class="fg" style="gap:8px"><i class="ti ti-database" style="color:var(--succtx)"></i>
          <div><b>{{ b.type === 'auto' ? '自动备份' : b.type === 'protective' ? '保护性备份' : '手动备份' }} — {{
            formatTime(b.created_at) }}</b>
            <div class="muted">{{ (b.size_bytes / 1024 / 1024).toFixed(1) }} MB · {{ b.integrity }}</div>
          </div>
        </div>
        <button class="btn-sm" :disabled="busy('restore' + b.backup_id)"
          @click="run('restore' + b.backup_id, () => restoreBackup(b.backup_id))">恢复</button>
      </div>
    </div>
    <div class="muted" style="margin-top:12px;font-size:var(--fs-sm)">恢复前会自动保存当前数据作为保底备份（不占 3 份名额）。</div>
    <!-- 备份命名弹窗 -->
    <BaseModal v-if="showBackupLabel" title="创建备份" size="sm" @close="showBackupLabel = false">
      <input v-model="backupLabel" placeholder="自定义标签（可选，默认按时间命名）" style="width:100%" @keyup.enter="doCreateBackup" />
      <template #footer>
        <button @click="showBackupLabel = false">取消</button>
        <button class="btn-primary" :disabled="busy('mkBackup')" @click="run('mkBackup', doCreateBackup)"><i
            v-if="busy('mkBackup')" class="ti ti-loader-2"></i> 创建</button>
      </template>
    </BaseModal>
  </div>

  <!-- 状态 -->
  <div v-else-if="tab === 6">
    <div v-if="status">
      <div class="banner" :style="overallStyle(status.overall)">
        系统运行{{ overallLabel(status.overall) }} · 首次安装 {{ status.first_installed || '—' }}</div>
      <div v-for="s in status.subsystems" :key="s.name" class="row"
        style="padding:10px 0;border-bottom:1px solid var(--bd)">
        <div class="fg" style="gap:8px"><span class="dot"
            :style="{ background: s.status === 'healthy' ? 'var(--succtx)' : s.status === 'unhealthy' ? 'var(--dangtx)' : 'var(--warntx)' }"></span>
          <div><b>{{ s.name }}</b>
            <div class="muted">{{ s.detail }}</div>
          </div>
        </div>
        <span class="muted">{{ s.metric }}</span>
      </div>
      <div class="cw" style="margin-top:16px">
        <div class="muted">
          产品版本 {{ status.system_info?.product_version }} · Schema {{ status.system_info?.schema_version }}
          · 记忆 {{ status.system_info?.memory_count }} 条 · 会话 {{ status.system_info?.session_count }} 个</div>
      </div>
      <div class="section-title mt">定时任务</div>
      <div v-for="t in tasks" :key="t.task_id" class="row" style="padding:8px 0;border-bottom:1px solid var(--bd)">
        <div><b>{{ t.name }}</b>
          <div class="muted">{{ t.schedule }} · 上次 {{ t.last_run ? formatTime(t.last_run) : '尚未执行' }}</div>
        </div>
        <div class="fg" style="gap:6px">
          <span class="badge" :class="taskBadge(t.status)">{{ taskLabel(t.status) }}</span>
          <button class="btn-xs" :disabled="busy('log' + t.task_id)"
            @click="run('log' + t.task_id, () => showTaskLogs(t.task_id))">日志</button>
          <button class="btn-xs" :disabled="busy('runT' + t.task_id)"
            @click="run('runT' + t.task_id, () => runTask(t.task_id))"><i v-if="busy('runT' + t.task_id)"
              class="ti ti-loader-2"></i> 立即执行</button>
        </div>
      </div>
    </div>
  </div>

  <!-- 添加 Provider 弹窗 -->
  <BaseModal v-if="showAddProvider" title="添加 LLM Provider" @close="showAddProvider = false">
      <div class="form-group"><label class="label">显示名称</label><input v-model="newProvider.display_name" /></div>
      <div class="form-group"><label class="label">类型</label>
        <select v-model="newProvider.provider_type">
          <option value="openai_compatible">OpenAI 兼容</option>
          <option value="anthropic">Anthropic</option>
          <option value="google">Google</option>
          <option value="custom">自定义</option>
        </select>
      </div>
      <div class="form-group"><label class="label">基础地址</label><input v-model="newProvider.base_url" /></div>
      <div class="form-group"><label class="label">API Key</label>
        <div class="input-affix">
          <input v-model="newProvider.api_key" :type="showAddKey ? 'text' : 'password'" />
          <i :class="showAddKey ? 'ti ti-eye-off' : 'ti ti-eye'" class="input-affix-icon"
            @click="showAddKey = !showAddKey"></i>
        </div>
      </div>
      <div class="form-group"><label class="label">模型 ID</label><input v-model="newProvider.model_id" /></div>
      <div class="form-grid">
        <div><label class="label">输入单价 ¥/M</label><input v-model.number="newProvider.input_price" /></div>
        <div><label class="label">输出单价 ¥/M</label><input v-model.number="newProvider.output_price" /></div>
      </div>
      <div class="fg" style="justify-content:flex-end;gap:8px;margin-top:16px">
        <button @click="showAddProvider = false">取消</button>
        <button :disabled="busy('testAdd')" @click="run('testAdd', () => testConn(newProvider))"><i
            v-if="busy('testAdd')" class="ti ti-loader-2"></i> 测试连接</button>
        <button class="btn-primary" :disabled="busy('addP')" @click="run('addP', addProvider)"><i v-if="busy('addP')"
            class="ti ti-loader-2"></i> 保存</button>
      </div>
  </BaseModal>
  <!-- Embedding 切换预估弹窗 -->
  <BaseModal v-if="embEstimate" title="Embedding 模型切换" @close="embEstimate = null">
    <p class="muted" style="margin-bottom:12px">切换后需重新向量化所有记忆（旧向量保留 30 天可回滚）。</p>
      <div class="g2">
        <div class="card">
          <div class="label">需重跑</div>
          <div class="val">{{ embEstimate.vector_count }} 条</div>
        </div>
        <div class="card">
          <div class="label">预估耗时</div>
          <div class="val">{{ embEstimate.estimated_minutes }} 分钟</div>
        </div>
      </div>
      <div class="fg" style="justify-content:flex-end;gap:8px;margin-top:16px">
        <button @click="embEstimate = null">取消</button>
        <button class="btn-primary" :disabled="busy('migrate')" @click="run('migrate', confirmMigrate)"><i
            v-if="busy('migrate')" class="ti ti-loader-2"></i> 确认切换</button>
      </div>
  </BaseModal>

  <!-- 定时任务日志弹窗 -->
  <BaseModal v-if="taskLogs" :title="'执行日志 · ' + taskLogs.id" size="lg" @close="taskLogs = null">
      <div v-if="!taskLogs.logs.length" class="muted">该任务尚未执行过</div>
      <div v-for="(l, i) in taskLogs.logs" :key="i" class="row" style="padding:8px 0;border-bottom:1px solid var(--bd)">
        <div><b>{{ formatTime(l.run_time) }}</b>
          <div class="muted">
            <template v-if="l.result === 'skipped'">{{ l.fail_reason || '未执行' }}</template>
            <template v-else>耗时 {{ formatDuration(l.duration_ms) }}
              <span v-if="l.fail_reason" style="color:var(--dangtx)">· {{ l.fail_reason }}</span>
            </template>
          </div>
        </div>
        <span class="badge" :class="taskBadge(l.result)">{{ taskLabel(l.result) }}</span>
      </div>
      <div class="fg" style="justify-content:flex-end;margin-top:16px"><button @click="taskLogs = null">关闭</button>
      </div>
  </BaseModal>

  <!-- 添加连接器弹窗 -->
  <BaseModal v-if="showAddConn" title="添加 MCP 连接器" @close="showAddConn = false">
      <div class="form-group"><label class="label">名称</label><input v-model="newConn.name" placeholder="如：GitHub" />
      </div>
      <div class="form-group"><label class="label">传输方式</label>
        <select v-model="newConn.transport">
          <option value="stdio">stdio — 本地子进程</option>
          <option value="http">Streamable HTTP</option>
        </select>
      </div>
      <template v-if="newConn.transport === 'stdio'">
        <div class="form-group"><label class="label">启动命令</label><input v-model="newConn.command" /></div>
        <div class="form-group"><label class="label">参数（JSON 数组）</label><input v-model="newConn.args"
            placeholder='["-y","@modelcontextprotocol/server-github"]' /></div>
        <div class="form-group"><label class="label">环境变量（JSON 对象）</label><input v-model="newConn.env"
            placeholder='{"GITHUB_TOKEN":"ghp_xxx"}' /></div>
      </template>
      <template v-else>
        <div class="form-group"><label class="label">端点地址</label><input v-model="newConn.url" /></div>
      </template>
      <div class="fg" style="justify-content:flex-end;gap:8px;margin-top:16px">
        <button @click="showAddConn = false">取消</button>
        <button :disabled="busy('testConn')" @click="run('testConn', testConnector)"><i v-if="busy('testConn')"
            class="ti ti-loader-2"></i> 测试连接</button>
        <button class="btn-primary" :disabled="busy('addConn')" @click="run('addConn', addConnector)"><i
            v-if="busy('addConn')" class="ti ti-loader-2"></i> 保存</button>
      </div>
  </BaseModal>

  <!-- 接入渠道配置弹窗 -->
  <BaseModal v-if="showChannelCfg" title="配置接入渠道" @close="showChannelCfg = false">
      <div class="form-group"><label class="label">平台</label>
        <select v-model="newChannel.platform_type">
          <option value="feishu">飞书</option>
          <option value="telegram">Telegram</option>
          <option value="dingtalk">钉钉</option>
          <option value="wecom">企业微信</option>
        </select>
      </div>
      <div class="form-group"><label class="label">Bot Token / App ID</label><input v-model="newChannel.bot_token" />
      </div>
      <div class="form-group"><label class="label">App Secret（部分平台）</label><input v-model="newChannel.app_secret"
          type="password" /></div>
      <div class="form-group"><label class="label">绑定账户 ID（只对该账户响应）</label><input
          v-model="newChannel.whitelist_user_id" /></div>
      <div class="form-group"><label class="label">回调地址（需外网可达）</label><input v-model="newChannel.callback_url" /></div>
      <div class="muted" style="margin-bottom:12px">启用后会自动禁用当前已启用的 IM 平台。</div>
      <div class="fg" style="justify-content:flex-end;gap:8px">
        <button @click="showChannelCfg = false">取消</button>
        <button :disabled="busy('testCh')" @click="run('testCh', testChannel)"><i v-if="busy('testCh')"
            class="ti ti-loader-2"></i> 测试连接</button>
        <button class="btn-primary" :disabled="busy('addCh')" @click="run('addCh', addChannel)"><i v-if="busy('addCh')"
            class="ti ti-loader-2"></i> 启用</button>
      </div>
  </BaseModal>

  <!-- 微信扫码绑定弹窗（关闭需同步停轮询） -->
  <BaseModal v-if="showWeixinScan" title="微信渠道扫码绑定" @close="stopWxPoll(); showWeixinScan = false">
    <div style="text-align:center">
      <div class="muted" style="margin:8px 0">使用微信「我 → 设置 → 插件 → ClawBot」扫描下方二维码（需微信 8.0.70+ 且账号已开放 ClawBot 灰度）</div>
      <!-- 二维码背景强制白底：扫码识别对比度需求（例外登记 UI_UX_SPEC） -->
      <div v-if="wxImgSrc()" style="margin:16px auto;width:220px;height:220px;background:#fff;padding:8px;border-radius:var(--radius-sm)">
        <img :src="wxImgSrc()" style="width:100%;height:100%;object-fit:contain" alt="微信扫码" />
      </div>
      <div v-else-if="wxScan.busy" class="muted">正在获取二维码…</div>
      <div v-else-if="wxScan.url" class="muted" style="margin:12px 0;word-break:break-all">请复制下方链接，在微信中打开后扫码确认：<br />{{ wxScan.url }}</div>
      <div class="fg" style="justify-content:center;gap:8px;margin-top:8px">
        <button v-if="!wxImgSrc() && wxScan.url" @click="copyWxUrl">复制链接</button>
      </div>
      <div class="muted" style="margin-top:8px">{{ wxScan.status === 'confirmed' ? '绑定成功' : '等待扫码确认…' }}</div>
      <div class="fg" style="justify-content:center;gap:8px;margin-top:16px">
        <button @click="refreshWeixinQrcode">重新获取</button>
        <button @click="stopWxPoll(); showWeixinScan = false">关闭</button>
      </div>
    </div>
  </BaseModal>

  <!-- 编辑接入渠道弹窗 -->
  <BaseModal v-if="showChannelEdit" title="编辑接入渠道" @close="showChannelEdit = false">
      <div class="form-group"><label class="label">平台</label>
        <input :value="PLATFORM_MAP[editChannel.platform_type] || editChannel.platform_type" disabled />
      </div>
      <template v-if="editChannel.platform_type === 'weixin'">
        <div class="muted" style="margin-bottom:12px">微信渠道凭证由扫码绑定流程管理（含会话 Token），此处仅可修改绑定账户与回调。</div>
      </template>
      <template v-else>
        <div class="form-group"><label class="label">Bot Token / App ID</label><input v-model="editChannel.bot_token" />
        </div>
        <div class="form-group"><label class="label">App Secret（部分平台）</label>
          <div class="input-affix">
            <input v-model="editChannel.app_secret" :type="showEditSecret ? 'text' : 'password'" />
            <i :class="showEditSecret ? 'ti ti-eye-off' : 'ti ti-eye'" class="input-affix-icon"
              @click="showEditSecret = !showEditSecret"></i>
          </div>
        </div>
      </template>
      <div class="form-group"><label class="label">绑定账户 ID（只对该账户响应）</label><input
          v-model="editChannel.whitelist_user_id" /></div>
      <div class="form-group"><label class="label">回调地址（需外网可达）</label><input v-model="editChannel.callback_url" /></div>
      <div class="fg" style="justify-content:flex-end;gap:8px">
        <button @click="showChannelEdit = false">取消</button>
        <button :disabled="busy('testChE')" @click="run('testChE', testEditChannel)"><i v-if="busy('testChE')"
            class="ti ti-loader-2"></i> 测试连接</button>
        <button class="btn-primary" :disabled="busy('saveChE')" @click="run('saveChE', saveChannelEdit)"><i
            v-if="busy('saveChE')" class="ti ti-loader-2"></i> 保存</button>
      </div>
  </BaseModal>

  <!-- 编辑 Provider 弹窗 -->
  <BaseModal v-if="showEdit" title="编辑 LLM Provider" @close="showEdit = false">
      <div class="form-group"><label class="label">显示名称</label><input v-model="editData.display_name" /></div>
      <div class="form-group"><label class="label">类型</label>
        <select v-model="editData.provider_type">
          <option value="openai_compatible">OpenAI 兼容</option>
          <option value="anthropic">Anthropic</option>
          <option value="google">Google</option>
          <option value="custom">自定义</option>
        </select>
      </div>
      <div class="form-group"><label class="label">基础地址</label><input v-model="editData.base_url" /></div>
      <div class="form-group"><label class="label">API Key</label>
        <div class="input-affix">
          <input v-model="editData.api_key" :type="showEditKey ? 'text' : 'password'" />
          <i :class="showEditKey ? 'ti ti-eye-off' : 'ti ti-eye'" class="input-affix-icon"
            @click="showEditKey = !showEditKey"></i>
        </div>
      </div>
      <div class="form-group"><label class="label">模型 ID</label><input v-model="editData.model_id" /></div>
      <div class="form-grid">
        <div><label class="label">输入单价 ¥/M</label><input v-model.number="editData.input_price" /></div>
        <div><label class="label">输出单价 ¥/M</label><input v-model.number="editData.output_price" /></div>
      </div>
      <div class="form-group" style="margin-top:12px"><label class="label">上下文窗口</label><input
          v-model.number="editData.context_window" /></div>
      <div class="fg" style="justify-content:flex-end;gap:8px;margin-top:16px">
        <button @click="showEdit = false">取消</button>
        <button :disabled="busy('testEdit')" @click="run('testEdit', () => testConn(editData))"><i
            v-if="busy('testEdit')" class="ti ti-loader-2"></i> 测试连接</button>
        <button class="btn-primary" :disabled="busy('saveEdit')" @click="run('saveEdit', saveEdit)"><i
            v-if="busy('saveEdit')" class="ti ti-loader-2"></i> 保存</button>
      </div>
  </BaseModal>
</template>
