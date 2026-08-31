<script setup>
// 思考时间线：对齐参考图布局（左侧状态图标 + 行内摘要/药丸/命令，仅「已思考」展开灰框）
import { computed, ref } from 'vue'

const props = defineProps({
  items: { type: Array, default: () => [] },
  live: { type: Boolean, default: false },
})

const rendered = computed(() => Array.isArray(props.items) ? props.items : [])
const expanded = ref(new Set())

function toggle(idx) {
  const next = new Set(expanded.value)
  if (next.has(idx)) next.delete(idx)
  else next.add(idx)
  expanded.value = next
}

function isExpanded(idx) {
  return expanded.value.has(idx)
}

function isExpandable(item) {
  return item.kind === 'reasoning' || item.kind === 'memory_stage'
}

function isItemRunning(item, idx) {
  if (!props.live) return false
  if (item.kind === 'tool_call') return item.status === 'running'
  if (item.kind === 'memory_stage') return memoryEffectiveStatus(item, idx) === 'running'
  if (item.kind === 'step_wait') return item.status === 'running'
  if (item.kind === 'reasoning' && idx === rendered.value.length - 1) return true
  return false
}

/** embed 等阶段若已有后续记忆步，视为已完成（避免卡在蓝色 spinner） */
function memoryEffectiveStatus(item, idx) {
  if (item.kind !== 'memory_stage') return item.status
  if (item.status !== 'running') return item.status
  for (let j = idx + 1; j < rendered.value.length; j++) {
    if (rendered.value[j].kind === 'memory_stage') return 'ok'
  }
  return props.live ? 'running' : 'ok'
}

function truncate(s, n = 88) {
  const t = (s || '').replace(/\s+/g, ' ').trim()
  if (t.length <= n) return t
  return t.slice(0, n) + '…'
}

function parseArgs(raw) {
  if (!raw) return null
  try {
    const obj = typeof raw === 'string' ? JSON.parse(raw) : raw
    return obj && typeof obj === 'object' ? obj : null
  } catch {
    return null
  }
}

function basename(path) {
  if (!path || typeof path !== 'string') return ''
  const p = path.replace(/\\/g, '/')
  const i = p.lastIndexOf('/')
  return i >= 0 ? p.slice(i + 1) : p
}

/** 工具行：中文标签 + 行内药丸/命令（参考图「读取文件 package.json」「终端命令 已运行」） */
function toolRow(item) {
  const name = (item.name || '').toLowerCase()
  const args = parseArgs(item.arguments) || {}
  const running = item.status === 'running'
  const ok = item.status === 'ok'

  if (name === 'file_read' || name === 'fs_read' || name === 'read_file') {
    return {
      label: '读取文件',
      pill: basename(args.path) || null,
      running,
      ok,
    }
  }
  if (name === 'file_write' || name === 'fs_write' || name === 'write_file') {
    return {
      label: '写入文件',
      pill: basename(args.path) || null,
      running,
      ok,
    }
  }
  if (name === 'shell_exec' || name.includes('terminal') || name === 'run_command') {
    const cmd = args.cmd || args.command || ''
    return {
      label: ok ? '终端命令 已运行' : (running ? '终端命令' : '终端命令'),
      command: cmd ? truncate(cmd, 120) : null,
      running,
      ok,
    }
  }
  if (name === 'web_search') {
    return { label: '联网搜索', preview: truncate(args.query, 72), running, ok }
  }
  if (name === 'web_fetch') {
    return { label: '抓取网页', preview: truncate(args.url, 72), running, ok }
  }
  if (name === 'fs_glob') {
    return { label: '查找文件', preview: args.pattern || null, running, ok }
  }
  if (name === 'fs_grep') {
    return { label: '搜索文本', preview: truncate(args.pattern || args.query, 72), running, ok }
  }
  if (name === 'fs_list') {
    return { label: '列出目录', pill: basename(args.path) || args.path || null, running, ok }
  }

  return {
    label: item.name || '工具调用',
    pill: ok ? '已完成' : (running ? '执行中' : '失败'),
    running,
    ok: ok && item.status !== 'fail',
    fail: item.status === 'fail',
  }
}

function memoryBadge(item) {
  if (item.status === 'skipped') return '已跳过'
  if (item.status === 'running') {
    const m = {
      embed: '生成向量…',
      presearch: '预筛中…',
      graph: '关联扩展…',
      refine: '精筛中…',
    }
    return m[item.stage] || '检索中…'
  }
  if (item.hit_count > 0) return `注入 ${item.hit_count} 条`
  if (item.stage === 'done' || item.status === 'ok') return '未命中'
  return ''
}

function onRowClick(idx, item) {
  if (isExpandable(item)) toggle(idx)
}

/** 联网搜索 / 抓取网页的引用链接（历史消息可从 result_preview 回退解析） */
function toolCitations(item) {
  if (Array.isArray(item.citations) && item.citations.length) {
    return item.citations
  }
  const name = (item.name || '').toLowerCase()
  if (name === 'web_search' && item.result_preview) {
    try {
      const arr = JSON.parse(item.result_preview)
      if (Array.isArray(arr)) {
        return arr
          .filter((x) => x && x.url)
          .slice(0, 5)
          .map((x) => ({
            title: String(x.title || x.url || '').trim() || x.url,
            url: x.url,
          }))
      }
    } catch {
      /* ignore */
    }
  }
  if (name === 'web_fetch') {
    const args = parseArgs(item.arguments)
    if (args?.url) {
      return [{ title: String(args.url), url: String(args.url) }]
    }
  }
  return []
}
</script>

<template>
  <div class="think-timeline">
    <template v-for="(item, idx) in rendered" :key="idx">
      <!-- 已思考：行内一行预览，点击展开灰框全文 -->
      <div v-if="item.kind === 'reasoning'"
           class="tl-entry tl-entry-reasoning"
           :class="{ 'is-expanded': isExpanded(idx), 'is-active': isItemRunning(item, idx) }">
        <button type="button" class="tl-entry-head" @click="onRowClick(idx, item)">
          <span class="tl-status" :class="isItemRunning(item, idx) ? 'is-running' : 'is-ok'">
            <i class="ti" :class="isItemRunning(item, idx) ? 'ti-loader-2' : 'ti-circle-check'"></i>
          </span>
          <span class="tl-label">已思考</span>
          <span class="tl-inline-preview">{{ truncate(item.text, 72) }}</span>
        </button>
        <div v-show="isExpanded(idx)" class="tl-reasoning-box">
          <span>{{ item.text }}</span>
        </div>
      </div>

      <!-- 记忆检索：行内药丸，详情默认折叠 -->
      <div v-else-if="item.kind === 'memory_stage'"
           class="tl-entry tl-entry-memory"
           :class="{ 'is-expanded': isExpanded(idx), 'is-active': isItemRunning(item, idx) }">
        <button type="button" class="tl-entry-head" @click="onRowClick(idx, item)">
          <span class="tl-status" :class="isItemRunning(item, idx) ? 'is-running' : (item.status === 'skipped' || memoryEffectiveStatus(item, idx) === 'ok' ? 'is-ok' : 'is-muted')">
            <i class="ti" :class="isItemRunning(item, idx) ? 'ti-loader-2' : 'ti-circle-check'"></i>
          </span>
          <span class="tl-label">记忆检索</span>
          <span class="tl-pill">{{ memoryBadge(item) }}</span>
        </button>
        <div v-show="isExpanded(idx)" class="tl-detail-sub">
          <div class="tl-detail-text">{{ item.summary }}</div>
          <div v-if="item.candidates != null || item.hit_count != null || item.elapsed_ms != null"
               class="tl-detail-meta">
            <span v-if="item.candidates != null">候选 {{ item.candidates }}</span>
            <span v-if="item.hit_count != null">注入 {{ item.hit_count }}</span>
            <span v-if="item.elapsed_ms != null">{{ item.elapsed_ms }}ms</span>
          </div>
        </div>
      </div>

      <!-- 旁白：气泡图标 + 中文说明，单行展示 -->
      <div v-else-if="item.kind === 'narration'" class="tl-entry tl-entry-note">
        <div class="tl-entry-row">
          <span class="tl-status is-note"><i class="ti ti-message-circle"></i></span>
          <span class="tl-note-text">{{ item.text }}</span>
        </div>
      </div>

      <!-- 模型步间等待：工具已完成、下一轮 LLM 尚未吐首 token -->
      <div v-else-if="item.kind === 'step_wait'"
           class="tl-entry tl-entry-wait"
           :class="{ 'is-active': isItemRunning(item, idx) }">
        <div class="tl-entry-row">
          <span class="tl-status is-running"><i class="ti ti-loader-2"></i></span>
          <span class="tl-label">{{ item.label || '准备下一步' }}</span>
          <span v-if="item.detail" class="tl-inline-preview">{{ item.detail }}</span>
        </div>
      </div>

      <!-- 工具：读取文件 / 终端命令 等，行内药丸或命令 -->
      <div v-else-if="item.kind === 'tool_call'"
           class="tl-entry tl-entry-tool"
           :class="{ 'is-active': toolRow(item).running }">
        <div class="tl-entry-row">
          <span class="tl-status"
                :class="toolRow(item).running ? 'is-running' : (toolRow(item).fail ? 'is-fail' : 'is-ok')">
            <i class="ti"
               :class="toolRow(item).running ? 'ti-loader-2' : (toolRow(item).fail ? 'ti-x' : 'ti-circle-check')"></i>
          </span>
          <span class="tl-label">{{ toolRow(item).label }}</span>
          <span v-if="toolRow(item).pill" class="tl-pill">{{ toolRow(item).pill }}</span>
          <code v-if="toolRow(item).command" class="tl-cmd">{{ toolRow(item).command }}</code>
          <span v-if="toolRow(item).preview" class="tl-inline-preview">{{ toolRow(item).preview }}</span>
          <span v-if="item.result_preview && !toolRow(item).pill && !toolRow(item).command && !toolRow(item).preview"
                class="tl-inline-preview">{{ truncate(item.result_preview, 64) }}</span>
          <span v-if="item.error" class="tl-inline-error">{{ truncate(item.error, 80) }}</span>
        </div>
        <div v-if="toolCitations(item).length" class="tl-cites-row">
          <a
            v-for="(cite, ci) in toolCitations(item)"
            :key="ci"
            class="tl-web-cite"
            :href="cite.url"
            target="_blank"
            rel="noopener noreferrer"
            :title="cite.url"
          >
            <i class="ti ti-file-text"></i>
            <span class="tl-web-cite-title">{{ cite.title }}</span>
          </a>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.think-timeline {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 2px 0 4px;
}

.tl-entry {
  font-size: 12px;
  line-height: 1.45;
  color: var(--fg);
  min-width: 0;
}

/* 与 tl-entry-head 同源的单行 flex，供工具/旁白等非按钮行复用 */
.tl-entry-row,
.tl-entry-head {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  min-width: 0;
  padding: 2px 0;
  margin: 0;
  border: none;
  background: transparent;
  text-align: left;
  color: inherit;
  font: inherit;
}

.tl-entry-head {
  cursor: pointer;
}

.tl-entry-head:hover .tl-inline-preview {
  color: var(--fg);
}

.tl-entry.is-active .tl-entry-head {
  opacity: 1;
}

.tl-status {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  flex-shrink: 0;
  font-size: 14px;
}

.tl-status.is-ok { color: var(--succtx, #28b478); }
.tl-status.is-running { color: var(--acctx, #3c78dc); }
.tl-status.is-fail { color: var(--dangtx, #c83c3c); }
.tl-status.is-note { color: var(--muted); font-size: 13px; }
.tl-status.is-muted { color: var(--muted); }

.tl-status .ti-loader-2 {
  animation: tl-spin 1s linear infinite;
}

.tl-label {
  flex-shrink: 0;
  color: var(--fg);
  font-weight: 500;
}

.tl-inline-preview {
  flex: 1;
  min-width: 0;
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11.5px;
}

.tl-pill {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 6px;
  background: var(--bg-input, rgba(127, 127, 127, 0.1));
  color: var(--fg);
  font-size: 11.5px;
  font-family: var(--font-mono, ui-monospace, Menlo, Consolas, monospace);
  flex-shrink: 0;
  max-width: min(100%, 220px);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tl-cmd {
  font-family: var(--font-mono, ui-monospace, Menlo, Consolas, monospace);
  font-size: 11px;
  color: var(--muted);
  background: transparent;
  padding: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  min-width: 0;
}

.tl-note-text {
  flex: 1;
  min-width: 0;
  color: var(--fg);
  font-size: 12px;
  line-height: 1.5;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tl-reasoning-box {
  margin: 6px 0 2px 24px;
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--bg-input, rgba(127, 127, 127, 0.08));
  font-size: 12px;
  line-height: 1.65;
  color: var(--sec, var(--fg));
  white-space: pre-wrap;
  word-break: break-word;
}

.tl-detail-sub {
  margin: 4px 0 2px 24px;
}

.tl-detail-text {
  font-size: 11.5px;
  line-height: 1.55;
  color: var(--muted);
  white-space: pre-wrap;
  word-break: break-word;
}

.tl-detail-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 4px;
  font-size: 11px;
  color: var(--muted);
}

.tl-inline-error {
  flex: 1;
  min-width: 0;
  color: var(--dangtx, #c83c3c);
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tl-cites-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin: 4px 0 2px 24px;
  min-width: 0;
}

.tl-web-cite {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 8px;
  border-radius: 6px;
  background: var(--brand-soft, rgba(60, 120, 220, 0.12));
  color: var(--brand-tx, #3c78dc);
  font-size: 11.5px;
  line-height: 1.4;
  text-decoration: none;
  max-width: min(100%, 320px);
  min-width: 0;
  transition: background var(--dur-fast, 0.15s);
}

.tl-web-cite:hover {
  background: rgba(60, 120, 220, 0.2);
}

.tl-web-cite .ti-file-text {
  flex-shrink: 0;
  font-size: 12px;
  opacity: 0.9;
}

.tl-web-cite-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

@keyframes tl-spin { to { transform: rotate(360deg); } }
</style>
