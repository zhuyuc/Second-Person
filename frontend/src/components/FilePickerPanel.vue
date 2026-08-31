<script setup>
// M4 @文件面板：ChatView 输入框敲 @ 触发此浮层，搜出项目内文件后插入 @path
import { ref, watch, onMounted } from 'vue'
import { projectsApi } from '@/api/projects'

const props = defineProps({
  projectId: { type: String, required: true },
  visible: { type: Boolean, default: false },
  query: { type: String, default: '' },
})
const emit = defineEmits(['pick', 'close'])

const files = ref([])
const loading = ref(false)
const highlight = ref(0)

async function load() {
  if (!props.projectId) return
  loading.value = true
  try {
    const q = (props.query || '').trim()
    if (q) {
      const d = await projectsApi.search(props.projectId, {
        q: q.includes('*') ? q : `**/*${q}*`,
        mode: 'glob',
        limit: 50,
      })
      files.value = (d.matches || []).map((p) => ({ path: p, name: _basename(p) }))
    } else {
      // 空查询 → 项目根一级目录
      const d = await projectsApi.tree(props.projectId, '', 1)
      files.value = (d.entries || []).map((e) => ({ path: e.path, name: e.name, type: e.type }))
    }
    highlight.value = 0
  } catch {
    files.value = []
  } finally {
    loading.value = false
  }
}

function _basename(p) {
  return String(p).split(/[\\/]/).pop() || p
}

watch(
  () => [props.query, props.projectId, props.visible],
  () => {
    if (props.visible) load()
  }
)
onMounted(() => {
  if (props.visible) load()
})

function pick(f) {
  emit('pick', f)
  emit('close')
}

function onKey(e) {
  if (!props.visible) return
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    highlight.value = Math.min(highlight.value + 1, files.value.length - 1)
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    highlight.value = Math.max(0, highlight.value - 1)
  } else if (e.key === 'Enter' && files.value[highlight.value]) {
    e.preventDefault()
    pick(files.value[highlight.value])
  } else if (e.key === 'Escape') {
    emit('close')
  }
}
defineExpose({ onKey })
</script>

<template>
  <div v-if="visible" class="file-picker">
    <div class="hd">
      <i class="ti ti-file-search"></i>
      <span>{{ query ? `文件搜索：${query}` : '项目根目录' }}</span>
      <span class="muted" style="margin-left: auto; font-size: 11px"
        >↑↓ 选择 · Enter 确认 · Esc 关闭</span
      >
    </div>
    <div v-if="loading" class="empty">加载中…</div>
    <div v-else-if="!files.length" class="empty">没有匹配文件</div>
    <div v-else class="list">
      <div
        v-for="(f, i) in files"
        :key="f.path"
        class="item"
        :class="{ hi: i === highlight }"
        @click="pick(f)"
        @mouseenter="highlight = i"
      >
        <i class="ti" :class="f.type === 'dir' ? 'ti-folder' : 'ti-file'"></i>
        <span class="name">{{ f.name }}</span>
        <span class="path muted">{{ f.path }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.file-picker {
  position: absolute;
  bottom: 100%;
  left: 0;
  right: 0;
  margin-bottom: 4px;
  background: var(--bg-elev, var(--bg));
  border: 1px solid var(--stroke);
  border-radius: 6px;
  box-shadow: 0 -2px 12px rgba(0, 0, 0, 0.1);
  max-height: 280px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  z-index: var(--z-sticky);
}
.hd {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  font-size: 12px;
  color: var(--muted);
  border-bottom: 1px solid var(--stroke);
}
.list {
  flex: 1;
  overflow-y: auto;
}
.item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  cursor: pointer;
  font-size: 13px;
}
.item.hi,
.item:hover {
  background: var(--acctx-bg, rgba(60, 120, 220, 0.15));
}
.item .name {
  font-weight: 500;
  flex-shrink: 0;
}
.item .path {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  font-family: monospace;
}
.empty {
  padding: 20px;
  text-align: center;
  color: var(--muted);
  font-size: 12px;
}
</style>
