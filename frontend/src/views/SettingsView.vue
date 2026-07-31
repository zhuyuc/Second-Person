<script setup>
import { ref, onMounted, onActivated, computed } from 'vue'
import { api } from '@/api/client'
import { useToast } from '@/stores/toast'
import { useConfirm } from '@/stores/confirm'

const toast = useToast()
const confirm = useConfirm()
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
const BAR_W = computed(() => {
  const n = trend.value.length || 1
  return Math.max(6, Math.min(36, (TREND_W - 60) / n - 8))
})
const trendMax = computed(() => trend.value.reduce((m, t) => Math.max(m, t.tokens || 0), 0))
const trendBars = computed(() => {
  const mx = trendMax.value || 1
  const y0 = TREND_H - 20  // bottom baseline (above labels)
  return trend.value.map((t, i) => {
    const h = Math.max(2, (t.tokens / mx) * (y0 - 10))
    const x = 40 + i * (BAR_W.value + 8)
    return {
      ...t, x, y: y0 - h, height: h, width: BAR_W.value,
      color: 'var(--succtx)'
    }  // 统一单色，不做峰值高亮
  })
})

function dateLabel(t) {
  // 30d 返回 "07-20"、month 返回 "20日"、year 返回 "7月"；日粒度统一成 月/日
  const raw = t.label || ''
  return raw.replace(/^(\d{2})-(\d{2}).*/, '$1/$2')
}

// 横轴标签稀疏化：年视图（12个月）全部显示，日粒度每 3 个显示一个
function labelVisible(i) {
  if (trendPeriod.value === 'year') return true
  return i % 3 === 0 || i === trendBars.value.length - 1
}

// hover 即时浮层：锚定在柱子正上方，贴边时自动内收防溢出
const hoverTip = computed(() => {
  if (hoveredIdx.value === null) return null
  const b = trendBars.value[hoveredIdx.value]
  if (!b) return null
  const w = 92
  const x = Math.min(Math.max(b.x + b.width / 2 - w / 2, 2), TREND_W - w - 2)
  const y = b.y - 26 < 2 ? b.y + 4 : b.y - 26
  return { x, y, w, text: `${dateLabel(b)}：${formatNum(b.tokens)}` }
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

// 连接器：保存
async function saveConnector() {
  await addConnector()
}

// 接入渠道配置
const PLATFORM_MAP = { web: 'Web', feishu: '飞书', telegram: 'Telegram', dingtalk: '钉钉', wecom: '企业微信' }
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
  if (!editChannel.value.bot_token.trim()) { toast.push('error', '请先填写 Bot Token'); return }
  if (['feishu', 'dingtalk', 'wecom'].includes(editChannel.value.platform_type) && !editChannel.value.app_secret.trim()) {
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
const SOURCE_NAMES = { main_chat: 'AI对话', agent: '工具prompt', system_agent: '系统prompt', title_gen: '标题生成', embedding: '向量分析', vision: '图片解析', intent_parse: '意图解析', tool_infer: '工具推断' }
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

function formatTime(iso) {
  if (!iso) return '-'
  const d = new Date(String(iso).replace(' ', 'T'))
  if (isNaN(d.getTime())) return iso
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
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
    <div style="font-weight:500;margin-bottom:12px">任务-模型分配</div>
    <div class="cw">
      <div v-for="task in ['chat', 'agent', 'intent', 'embedding', 'vision']" :key="task" class="row"
        style="padding:8px 0;border-bottom:.5px solid var(--bd)">
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
    <div style="font-weight:500;margin:20px 0 12px">已添加的模型</div>
    <div v-for="p in providers" :key="p.id" class="cw">
      <div class="row">
        <div class="fg" style="gap:8px"><span class="dot" style="background:var(--succtx)"></span>
          <div><b>{{ p.display_name }}</b>
            <div class="muted">{{ p.base_url }}</div>
          </div>
        </div>
        <div class="fg" style="gap:12px"><span class="muted">¥{{ p.input_price || 0 }}/M · ¥{{ p.output_price || 0
        }}/M</span>
          <button style="font-size:12px" @click="openEdit(p)">编辑</button>
          <button class="dang" style="font-size:12px" @click="delProvider(p.id)">删除</button>
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
      <button style="font-size:12px" @click="showAddConn = true">+ 添加</button>
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
        <button style="font-size:11px" @click="toggleConn(c)">{{ c.status === 'connected' ? '断开' : '连接' }}</button>
        <button style="font-size:11px" @click="refreshConn(c.id)">刷新工具</button>
        <button style="font-size:11px" class="dang" @click="delConn(c.id)">删除</button>
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
            <div class="muted">{{ p.detail }}</div>
            <div v-if="p.failure_reason" class="muted" style="color:var(--dangtx)">{{ p.failure_reason }}</div>
          </div>
        </div>
        <div class="fg" style="gap:8px">
          <span v-if="p.enabled" class="badge badge-g">已启用</span>
          <button v-if="p.id !== 'web_default'" style="font-size:12px" @click="openChannelEdit(p)">编辑</button>
          <button v-if="p.id !== 'web_default' && !p.enabled" style="font-size:12px"
            @click="enablePlatform(p.id)">启用</button>
          <button v-if="p.id !== 'web_default' && p.enabled" style="font-size:12px"
            @click="disablePlatform(p.id)">禁用</button>
          <button v-if="p.status === 'paused'" style="font-size:12px" @click="resumePlatform(p.id)">恢复</button>
        </div>
      </div>
    </div>
    <div style="font-weight:500;margin:20px 0 12px">添加渠道</div>
    <div class="g3">
      <div v-for="pt in ['feishu', 'telegram', 'dingtalk', 'wecom']" :key="pt" class="cw"
        style="text-align:center;cursor:pointer;padding:20px"
        @click="newChannel.platform_type = pt; showChannelCfg = true">
        <i class="ti ti-plug" style="font-size:24px;color:var(--acctx)"></i>
        <div style="font-weight:500;margin-top:8px">{{ PLATFORM_MAP[pt] || pt }}</div>
        <div class="muted">Bot 私聊接入</div>
      </div>
    </div>
  </div>

  <!-- 参数（schema 驱动） -->
  <div v-else-if="tab === 3">
    <div v-for="g in groups" :key="g" class="cw">
      <div style="font-weight:500;margin-bottom:12px">{{ groupNames[g] }}</div>
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
      <button @click="saveParams">保存设置</button>
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
        <select v-model="usageSource" style="font-size:12px" @change="loadUsage">
          <option value="">全部来源</option>
          <option v-for="s in sourceOptions" :key="s" :value="s">{{ sourceName(s) }}</option>
        </select>
        <select v-model="usageModel" style="font-size:12px" @change="loadUsage">
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
        <div style="height:6px;border-radius:3px;background:var(--s1)">
          <div
            :style="{ height: '6px', borderRadius: '3px', background: (usage.today_ratio || 0) >= (usage.alert_ratio || 80) ? 'var(--dangtx)' : 'var(--succtx)', width: Math.min(100, usage.today_ratio || 0) + '%' }">
          </div>
        </div>
        <div class="row" style="margin-top:14px;margin-bottom:8px"><span class="muted">本月预算</span><span
            :style="{ color: (usage.month_ratio || 0) >= 80 ? 'var(--warntx)' : '' }">{{ usage.month_ratio || 0
            }}%</span></div>
        <div style="height:6px;border-radius:3px;background:var(--s1)">
          <div
            :style="{ height: '6px', borderRadius: '3px', background: (usage.month_ratio || 0) >= (usage.alert_ratio || 80) ? 'var(--dangtx)' : 'var(--succtx)', width: Math.min(100, usage.month_ratio || 0) + '%' }">
          </div>
        </div>
      </div>
      <!-- 趋势图 -->
      <div class="cw" style="margin-top:12px">
        <div class="fg" style="justify-content:space-between;margin-bottom:14px">
          <div class="fg" style="gap:4px">
            <button v-for="p in [{ k: '30d', n: '近 30 天' }, { k: 'month', n: '本月' }, { k: 'year', n: '当年' }]" :key="p.k"
              class="tab" :class="{ active: trendPeriod === p.k }" style="font-size:12px" @click="switchTrend(p.k)">{{
                p.n }}</button>
          </div>
        </div>
        <svg v-if="trend.length" :viewBox="'0 0 ' + TREND_W + ' ' + TREND_H" style="width:100%"
          preserveAspectRatio="xMidYMid meet">
          <!-- grid lines -->
          <line v-for="y in [0, .25, .5, .75]" :key="y" :x1="50" :y1="TREND_H - 20 - (TREND_H - 30) * y"
            :x2="TREND_W - 8" :y2="TREND_H - 20 - (TREND_H - 30) * y" stroke="var(--bd)" stroke-dasharray="3,3"
            stroke-width=".5" />
          <line x1="49" y1="10" x2="49" y2="TREND_H-20" stroke="var(--bd)" stroke-width=".5" />
          <!-- Y 轴标签 -->
          <text v-for="y in [0, .25, .5, .75, 1]" :key="'yl' + y" :x="44" :y="TREND_H - 20 - (TREND_H - 30) * y + 3"
            text-anchor="end" font-size="8" fill="var(--muted)">{{ formatNum(Math.round(trendMax * y)) }}</text>
          <!-- 零值柱不渲染，仅有消耗的天数才绘制矩形 -->
          <rect v-for="(b, i) in trendBars" :key="b.label" v-show="b.tokens > 0" :x="b.x" :y="b.y" :width="b.width"
            :height="b.height" :fill="b.color" rx="2" :style="{ opacity: hoveredIdx === i ? .75 : 1 }"
            @mouseenter="hoveredIdx = i" @mouseleave="hoveredIdx = null" />
          <!-- hover 即时浮层（跟随柱子，pointer-events 关闭防闪烁） -->
          <g v-if="hoverTip" pointer-events="none">
            <rect :x="hoverTip.x" :y="hoverTip.y" :width="hoverTip.w" height="20" rx="4" fill="#333" opacity=".92" />
            <text :x="hoverTip.x + hoverTip.w / 2" :y="hoverTip.y + 13" text-anchor="middle" font-size="10" fill="#fff"
              font-weight="500">{{ hoverTip.text }}</text>
          </g>
          <!-- 横轴标签：年视图逐月全显，日粒度每 3 个显示一个 -->
          <text v-for="(b, i) in trendBars" :key="b.label + 'l'" :x="b.x + b.width / 2" :y="TREND_H - 3"
            text-anchor="middle" font-size="9" fill="var(--muted)" v-show="labelVisible(i)">{{ dateLabel(b) }}</text>
        </svg>
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
        <button style="font-size:12px" @click="createBackup">立即备份</button>
        <button style="font-size:12px" @click="exportData">导出JSON</button>
        <button style="font-size:12px" @click="importFileInput?.click()">导入</button>
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
        <button style="font-size:12px" @click="restoreBackup(b.backup_id)">恢复</button>
      </div>
    </div>
    <div class="muted" style="margin-top:12px;font-size:12px">恢复前会自动保存当前数据作为保底备份（不占 3 份名额）。</div>
    <!-- 备份命名弹窗 -->
    <div v-if="showBackupLabel" class="overlay" @click.self="showBackupLabel = false">
      <div class="modal" style="max-width:360px">
        <div class="mt">创建备份</div>
        <input v-model="backupLabel" placeholder="自定义标签（可选，默认按时间命名）" style="width:100%" @keyup.enter="doCreateBackup" />
        <div class="fg" style="justify-content:flex-end;gap:8px;margin-top:16px">
          <button @click="showBackupLabel = false">取消</button>
          <button class="btn-primary" @click="doCreateBackup">创建</button>
        </div>
      </div>
    </div>
  </div>

  <!-- 状态 -->
  <div v-else-if="tab === 6">
    <div v-if="status">
      <div class="banner" :style="overallStyle(status.overall)">
        系统运行{{ overallLabel(status.overall) }} · 首次安装 {{ status.first_installed || '—' }}</div>
      <div v-for="s in status.subsystems" :key="s.name" class="row"
        style="padding:10px 0;border-bottom:.5px solid var(--bd)">
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
      <div style="font-weight:500;margin:20px 0 12px">定时任务</div>
      <div v-for="t in tasks" :key="t.task_id" class="row" style="padding:8px 0;border-bottom:.5px solid var(--bd)">
        <div><b>{{ t.name }}</b>
          <div class="muted">{{ t.schedule }} · 上次 {{ t.last_run ? formatTime(t.last_run) : '尚未执行' }}</div>
        </div>
        <div class="fg" style="gap:6px">
          <span class="badge" :class="taskBadge(t.status)">{{ taskLabel(t.status) }}</span>
          <button style="font-size:11px" @click="showTaskLogs(t.task_id)">日志</button>
          <button style="font-size:11px" @click="runTask(t.task_id)">立即执行</button>
        </div>
      </div>
    </div>
  </div>

  <!-- 添加 Provider 弹窗 -->
  <div v-if="showAddProvider" class="overlay" @click.self="showAddProvider = false">
    <div class="modal">
      <div class="mt">添加 LLM Provider</div>
      <div style="margin-bottom:10px"><label class="label">显示名称</label><input v-model="newProvider.display_name"
          style="width:100%" /></div>
      <div style="margin-bottom:10px"><label class="label">类型</label>
        <select v-model="newProvider.provider_type" style="width:100%">
          <option value="openai_compatible">OpenAI 兼容</option>
          <option value="anthropic">Anthropic</option>
          <option value="google">Google</option>
          <option value="custom">自定义</option>
        </select>
      </div>
      <div style="margin-bottom:10px"><label class="label">基础地址</label><input v-model="newProvider.base_url"
          style="width:100%" /></div>
      <div style="margin-bottom:10px"><label class="label">API Key</label>
        <div style="position:relative">
          <input v-model="newProvider.api_key" :type="showAddKey ? 'text' : 'password'"
            style="width:100%;padding-right:40px" />
          <i :class="showAddKey ? 'ti ti-eye-off' : 'ti ti-eye'" @click="showAddKey = !showAddKey"
            style="position:absolute;right:12px;top:50%;transform:translateY(-50%);cursor:pointer;color:var(--muted)"></i>
        </div>
      </div>
      <div style="margin-bottom:10px"><label class="label">模型 ID</label><input v-model="newProvider.model_id"
          style="width:100%" /></div>
      <div class="g2">
        <div><label class="label">输入单价 ¥/M</label><input v-model.number="newProvider.input_price" style="width:100%" />
        </div>
        <div><label class="label">输出单价 ¥/M</label><input v-model.number="newProvider.output_price" style="width:100%" />
        </div>
      </div>
      <div class="fg" style="justify-content:flex-end;gap:8px;margin-top:16px">
        <button @click="showAddProvider = false">取消</button>
        <button @click="testConn(newProvider)">测试连接</button>
        <button class="btn-primary" @click="addProvider">保存</button>
      </div>
    </div>
  </div>
  <!-- Embedding 切换预估弹窗 -->
  <div v-if="embEstimate" class="overlay" @click.self="embEstimate = null">
    <div class="modal">
      <div class="mt">Embedding 模型切换</div>
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
        <button @click="confirmMigrate">确认切换</button>
      </div>
    </div>
  </div>

  <!-- 定时任务日志弹窗 -->
  <div v-if="taskLogs" class="overlay" @click.self="taskLogs = null">
    <div class="modal" style="max-width:640px">
      <div class="mt">执行日志 · {{ taskLogs.id }}</div>
      <div v-if="!taskLogs.logs.length" class="muted">该任务尚未执行过</div>
      <div v-for="(l, i) in taskLogs.logs" :key="i" class="row"
        style="padding:8px 0;border-bottom:.5px solid var(--bd)">
        <div><b>{{ formatTime(l.run_time) }}</b>
          <div class="muted">耗时 {{ l.duration_ms }}ms
            <span v-if="l.fail_reason" style="color:var(--dangtx)">· {{ l.fail_reason }}</span>
          </div>
        </div>
        <span class="badge" :class="taskBadge(l.result)">{{ taskLabel(l.result) }}</span>
      </div>
      <div class="fg" style="justify-content:flex-end;margin-top:16px"><button @click="taskLogs = null">关闭</button>
      </div>
    </div>
  </div>

  <!-- 添加连接器弹窗 -->
  <div v-if="showAddConn" class="overlay" @click.self="showAddConn = false">
    <div class="modal">
      <div class="mt">添加 MCP 连接器</div>
      <div style="margin-bottom:10px"><label class="label">名称</label><input v-model="newConn.name"
          placeholder="如：GitHub" style="width:100%" /></div>
      <div style="margin-bottom:10px"><label class="label">传输方式</label>
        <select v-model="newConn.transport" style="width:100%">
          <option value="stdio">stdio — 本地子进程</option>
          <option value="http">Streamable HTTP</option>
        </select>
      </div>
      <template v-if="newConn.transport === 'stdio'">
        <div style="margin-bottom:10px"><label class="label">启动命令</label><input v-model="newConn.command"
            style="width:100%" /></div>
        <div style="margin-bottom:10px"><label class="label">参数（JSON 数组）</label><input v-model="newConn.args"
            placeholder='["-y","@modelcontextprotocol/server-github"]' style="width:100%" /></div>
        <div style="margin-bottom:10px"><label class="label">环境变量（JSON 对象）</label><input v-model="newConn.env"
            placeholder='{"GITHUB_TOKEN":"ghp_xxx"}' style="width:100%" /></div>
      </template>
      <template v-else>
        <div style="margin-bottom:10px"><label class="label">端点地址</label><input v-model="newConn.url"
            style="width:100%" /></div>
      </template>
      <div class="fg" style="justify-content:flex-end;gap:8px;margin-top:16px">
        <button @click="showAddConn = false">取消</button>
        <button @click="testConnector">测试连接</button>
        <button class="btn-primary" @click="saveConnector">保存</button>
      </div>
    </div>
  </div>

  <!-- 接入渠道配置弹窗 -->
  <div v-if="showChannelCfg" class="overlay" @click.self="showChannelCfg = false">
    <div class="modal">
      <div class="mt">配置接入渠道</div>
      <div style="margin-bottom:10px"><label class="label">平台</label>
        <select v-model="newChannel.platform_type" style="width:100%">
          <option value="feishu">飞书</option>
          <option value="telegram">Telegram</option>
          <option value="dingtalk">钉钉</option>
          <option value="wecom">企业微信</option>
        </select>
      </div>
      <div style="margin-bottom:10px"><label class="label">Bot Token / App ID</label><input
          v-model="newChannel.bot_token" style="width:100%" /></div>
      <div style="margin-bottom:10px"><label class="label">App Secret（部分平台）</label><input
          v-model="newChannel.app_secret" type="password" style="width:100%" /></div>
      <div style="margin-bottom:10px"><label class="label">绑定账户 ID（只对该账户响应）</label><input
          v-model="newChannel.whitelist_user_id" style="width:100%" /></div>
      <div style="margin-bottom:10px"><label class="label">回调地址（需外网可达）</label><input v-model="newChannel.callback_url"
          style="width:100%" /></div>
      <div class="muted" style="margin-bottom:12px">启用后会自动禁用当前已启用的 IM 平台。</div>
      <div class="fg" style="justify-content:flex-end;gap:8px">
        <button @click="showChannelCfg = false">取消</button>
        <button @click="testChannel">测试连接</button>
        <button class="btn-primary" @click="addChannel">启用</button>
      </div>
    </div>
  </div>

  <!-- 编辑接入渠道弹窗 -->
  <div v-if="showChannelEdit" class="overlay" @click.self="showChannelEdit = false">
    <div class="modal">
      <div class="mt">编辑接入渠道</div>
      <div style="margin-bottom:10px"><label class="label">平台</label>
        <input :value="PLATFORM_MAP[editChannel.platform_type] || editChannel.platform_type" disabled
          style="width:100%" />
      </div>
      <div style="margin-bottom:10px"><label class="label">Bot Token / App ID</label><input
          v-model="editChannel.bot_token" style="width:100%" /></div>
      <div style="margin-bottom:10px"><label class="label">App Secret（部分平台）</label>
        <div style="position:relative">
          <input v-model="editChannel.app_secret" :type="showEditSecret ? 'text' : 'password'"
            style="width:100%;padding-right:40px" />
          <i :class="showEditSecret ? 'ti ti-eye-off' : 'ti ti-eye'" @click="showEditSecret = !showEditSecret"
            style="position:absolute;right:12px;top:50%;transform:translateY(-50%);cursor:pointer;color:var(--muted)"></i>
        </div>
      </div>
      <div style="margin-bottom:10px"><label class="label">绑定账户 ID（只对该账户响应）</label><input
          v-model="editChannel.whitelist_user_id" style="width:100%" /></div>
      <div style="margin-bottom:10px"><label class="label">回调地址（需外网可达）</label><input v-model="editChannel.callback_url"
          style="width:100%" /></div>
      <div class="fg" style="justify-content:flex-end;gap:8px">
        <button @click="showChannelEdit = false">取消</button>
        <button @click="testEditChannel">测试连接</button>
        <button class="btn-primary" @click="saveChannelEdit">保存</button>
      </div>
    </div>
  </div>

  <!-- 编辑 Provider 弹窗 -->
  <div v-if="showEdit" class="overlay" @click.self="showEdit = false">
    <div class="modal">
      <div class="mt">编辑 LLM Provider</div>
      <div style="margin-bottom:10px"><label class="label">显示名称</label><input v-model="editData.display_name"
          style="width:100%" /></div>
      <div style="margin-bottom:10px"><label class="label">类型</label>
        <select v-model="editData.provider_type" style="width:100%">
          <option value="openai_compatible">OpenAI 兼容</option>
          <option value="anthropic">Anthropic</option>
          <option value="google">Google</option>
          <option value="custom">自定义</option>
        </select>
      </div>
      <div style="margin-bottom:10px"><label class="label">基础地址</label><input v-model="editData.base_url"
          style="width:100%" /></div>
      <div style="margin-bottom:10px"><label class="label">API Key</label>
        <div style="position:relative">
          <input v-model="editData.api_key" :type="showEditKey ? 'text' : 'password'"
            style="width:100%;padding-right:40px" />
          <i :class="showEditKey ? 'ti ti-eye-off' : 'ti ti-eye'" @click="showEditKey = !showEditKey"
            style="position:absolute;right:12px;top:50%;transform:translateY(-50%);cursor:pointer;color:var(--muted)"></i>
        </div>
      </div>
      <div style="margin-bottom:10px"><label class="label">模型 ID</label><input v-model="editData.model_id"
          style="width:100%" /></div>
      <div class="g2">
        <div><label class="label">输入单价 ¥/M</label><input v-model.number="editData.input_price" style="width:100%" />
        </div>
        <div><label class="label">输出单价 ¥/M</label><input v-model.number="editData.output_price" style="width:100%" />
        </div>
      </div>
      <div style="margin:10px 0"><label class="label">上下文窗口</label><input v-model.number="editData.context_window"
          style="width:100%" /></div>
      <div class="fg" style="justify-content:flex-end;gap:8px;margin-top:16px">
        <button @click="showEdit = false">取消</button>
        <button @click="testConn(editData)">测试连接</button>
        <button class="btn-primary" @click="saveEdit">保存</button>
      </div>
    </div>
  </div>
</template>
