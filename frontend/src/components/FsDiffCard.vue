<script setup>
// fs_write / fs_edit 结果里的差异卡（v5 §九 9.4）
import { ref, computed } from 'vue'
import { useToast } from '@/stores/toast'

const props = defineProps({
  action: { type: String, default: 'edited' }, // created / replaced / edited
  path: { type: String, required: true },
  diff: { type: Object, default: () => ({}) },
})
const toast = useToast()

const expanded = ref(false)
const view = ref('unified') // unified / side

const stats = computed(() => ({
  added: props.diff.added || 0,
  removed: props.diff.removed || 0,
}))
const filename = computed(() => {
  const s = String(props.path || '')
  return s.split(/[\\/]/).pop() || s
})
const actionLabel = computed(() => ({
  created: 'Created', replaced: 'Replaced', edited: 'Updated',
}[props.action] || 'Updated'))

function copyPath() {
  navigator.clipboard.writeText(props.path).then(
    () => toast.push('success', '路径已复制'))
}
function copyAfter() {
  navigator.clipboard.writeText(props.diff.after || '').then(
    () => toast.push('success', '新内容已复制'))
}

function lineClass(line) {
  if (line.startsWith('+') && !line.startsWith('+++')) return 'diff-add'
  if (line.startsWith('-') && !line.startsWith('---')) return 'diff-del'
  if (line.startsWith('@@')) return 'diff-hunk'
  if (line.startsWith('+++') || line.startsWith('---')) return 'diff-header'
  return 'diff-ctx'
}
</script>

<template>
  <div class="fs-diff-card">
    <div class="hd" @click="expanded = !expanded">
      <i class="ti" :class="expanded ? 'ti-chevron-down' : 'ti-chevron-right'"></i>
      <i class="ti ti-file-diff"></i>
      <span class="fname">{{ filename }}</span>
      <span class="stats">
        <span v-if="stats.added" class="added">+{{ stats.added }}</span>
        <span v-if="stats.removed" class="removed">-{{ stats.removed }}</span>
      </span>
      <span class="action">{{ actionLabel }}</span>
    </div>
    <div v-if="expanded" class="body">
      <div class="toolbar">
        <span class="path" :title="path">{{ path }}</span>
        <div class="fg" style="gap:4px">
          <button class="btn-xs" @click="view = view === 'unified' ? 'side' : 'unified'">
            {{ view === 'unified' ? '并列' : '合并' }}
          </button>
          <button class="btn-xs" @click="copyPath">复制路径</button>
          <button class="btn-xs" @click="copyAfter">复制新内容</button>
        </div>
      </div>
      <pre v-if="view === 'unified'" class="diff-view"><span
        v-for="(line, i) in (diff.unified || '').split('\n')"
        :key="i" :class="lineClass(line)">{{ line }}
</span></pre>
      <div v-else class="side-by-side">
        <div class="side">
          <div class="side-hd">修改前</div>
          <pre>{{ diff.before || '(空)' }}</pre>
        </div>
        <div class="side">
          <div class="side-hd">修改后</div>
          <pre>{{ diff.after || '(空)' }}</pre>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.fs-diff-card {
  border: 1px solid var(--stroke);
  border-radius: 6px;
  margin: 8px 0;
  overflow: hidden;
  background: var(--bg-input, rgba(127,127,127,0.04));
}
.hd {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; cursor: pointer;
  font-size: 13px;
}
.hd:hover { background: var(--bg-hover, rgba(127,127,127,0.08)); }
.hd .fname { font-weight: 500; }
.hd .stats { display: flex; gap: 6px; }
.hd .added { color: var(--succtx, #28a050); font-family: monospace; }
.hd .removed { color: var(--dangtx, #c02020); font-family: monospace; }
.hd .action {
  margin-left: auto; font-size: 11px;
  padding: 1px 6px; border-radius: 3px;
  background: var(--acctx-bg, rgba(60,120,220,0.15));
  color: var(--acctx);
}
.body { border-top: 1px solid var(--stroke); }
.toolbar {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; background: var(--bg, transparent);
  border-bottom: 1px solid var(--stroke);
  font-size: 12px;
}
.toolbar .path {
  flex: 1; min-width: 0;
  color: var(--muted); font-family: monospace;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.toolbar .fg { display: flex; }
.diff-view {
  margin: 0; padding: 8px 12px;
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 12px; line-height: 1.5;
  max-height: 400px; overflow: auto;
  white-space: pre; word-wrap: normal;
}
.diff-view .diff-add {
  display: block;
  background: var(--succtx-bg, rgba(40,160,80,0.1));
  color: var(--succtx, #28a050);
}
.diff-view .diff-del {
  display: block;
  background: var(--dangtx-bg, rgba(200,0,0,0.08));
  color: var(--dangtx, #c02020);
}
.diff-view .diff-hunk {
  display: block; color: var(--acctx); font-weight: 500;
}
.diff-view .diff-ctx { display: block; color: var(--fg); }
.diff-view .diff-header { display: block; color: var(--muted); font-weight: 500; }
.side-by-side {
  display: grid; grid-template-columns: 1fr 1fr;
  gap: 0; max-height: 400px; overflow: auto;
}
.side { border-right: 1px solid var(--stroke); }
.side:last-child { border-right: none; }
.side-hd {
  padding: 6px 12px; font-size: 12px; font-weight: 500;
  background: var(--bg-hover, rgba(127,127,127,0.08));
  border-bottom: 1px solid var(--stroke);
}
.side pre {
  margin: 0; padding: 8px 12px;
  font-family: monospace; font-size: 12px;
  white-space: pre-wrap; word-break: break-all;
}
</style>
