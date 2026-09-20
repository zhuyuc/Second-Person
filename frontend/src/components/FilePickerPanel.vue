<script setup>
// @ 浮层：能力（技能）+ 可选项目文件。技能仅通过显式选择加载（skill_refs）。
// 用 Teleport + fixed 逃出 composer / overflow:hidden 裁剪，保证压在最上层。
import { ref, watch, onMounted, onBeforeUnmount, computed, nextTick } from 'vue'
import { projectsApi } from '@/api/projects'
import { skillsApi } from '@/api/skills'

const props = defineProps({
  projectId: { type: String, default: '' },
  visible: { type: Boolean, default: false },
  query: { type: String, default: '' },
  /** skills | files — 有项目时可切；无项目仅 skills */
  enableFiles: { type: Boolean, default: true },
})
const emit = defineEmits(['pick', 'pick-skill', 'close'])

const tab = ref('skills') // skills | files
const skills = ref([])
const files = ref([])
const loading = ref(false)
const highlight = ref(0)
const anchorRef = ref(null)
const panelStyle = ref({})

const showFilesTab = computed(() => props.enableFiles && !!props.projectId)

const rows = computed(() => {
  if (tab.value === 'files' && showFilesTab.value) {
    return files.value.map((f) => ({
      kind: 'file',
      key: f.path,
      title: f.name,
      subtitle: f.path,
      icon: f.type === 'dir' ? 'ti-folder' : 'ti-file',
      raw: f,
    }))
  }
  return skills.value.map((s) => ({
    kind: 'skill',
    key: s.name,
    title: s.ui_chip || s.name,
    subtitle: s.brief || s.name,
    icon: 'ti-sparkles',
    raw: s,
  }))
})

function _basename(p) {
  return String(p).split(/[\\/]/).pop() || p
}

function updatePosition() {
  const anchor = anchorRef.value
  if (!anchor) return
  const box =
    anchor.closest('.composer, .composer-inner, .cp-script-fill, .cp-script-block') ||
    anchor.parentElement
  if (!box) return
  const rect = box.getBoundingClientRect()
  const gap = 8
  const preferred = 340
  const spaceAbove = rect.top - gap - 8
  const spaceBelow = window.innerHeight - rect.bottom - gap - 8
  const placeAbove = spaceAbove >= 180 || spaceAbove >= spaceBelow
  const maxHeight = Math.max(160, Math.min(preferred, placeAbove ? spaceAbove : spaceBelow))
  const width = Math.max(280, rect.width)
  const left = Math.min(Math.max(8, rect.left), window.innerWidth - width - 8)
  if (placeAbove) {
    panelStyle.value = {
      left: `${left}px`,
      width: `${width}px`,
      maxHeight: `${maxHeight}px`,
      bottom: `${window.innerHeight - rect.top + gap}px`,
      top: 'auto',
    }
  } else {
    panelStyle.value = {
      left: `${left}px`,
      width: `${width}px`,
      maxHeight: `${maxHeight}px`,
      top: `${rect.bottom + gap}px`,
      bottom: 'auto',
    }
  }
}

async function loadSkills() {
  const d = await skillsApi.list({
    q: props.query || undefined,
    category: 'director-style',
  })
  skills.value = d.skills || []
}

async function loadFiles() {
  if (!props.projectId) {
    files.value = []
    return
  }
  const q = (props.query || '').trim()
  if (q) {
    const d = await projectsApi.search(props.projectId, {
      q: q.includes('*') ? q : `**/*${q}*`,
      mode: 'glob',
      limit: 50,
    })
    files.value = (d.matches || []).map((p) => ({ path: p, name: _basename(p) }))
  } else {
    const d = await projectsApi.tree(props.projectId, '', 1)
    files.value = (d.entries || []).map((e) => ({ path: e.path, name: e.name, type: e.type }))
  }
}

async function load() {
  loading.value = true
  try {
    if (tab.value === 'files' && showFilesTab.value) {
      await loadFiles()
    } else {
      await loadSkills()
    }
    highlight.value = 0
  } catch {
    skills.value = []
    files.value = []
  } finally {
    loading.value = false
    await nextTick()
    if (props.visible) updatePosition()
  }
}

watch(
  () => [props.query, props.projectId, props.visible, tab.value],
  () => {
    if (props.visible) load()
  },
)
watch(
  () => props.visible,
  async (v) => {
    if (v) {
      tab.value = 'skills'
      highlight.value = 0
      await nextTick()
      updatePosition()
    }
  },
)

function onWinChange() {
  if (props.visible) updatePosition()
}

onMounted(() => {
  if (props.visible) load()
  window.addEventListener('resize', onWinChange)
  window.addEventListener('scroll', onWinChange, true)
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', onWinChange)
  window.removeEventListener('scroll', onWinChange, true)
})

function pickRow(row) {
  if (!row) return
  if (row.kind === 'skill') {
    emit('pick-skill', row.raw)
  } else {
    emit('pick', row.raw)
  }
  emit('close')
}

function onKey(e) {
  if (!props.visible) return
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    highlight.value = Math.min(highlight.value + 1, Math.max(rows.value.length - 1, 0))
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    highlight.value = Math.max(highlight.value - 1, 0)
  } else if (e.key === 'Enter' && rows.value[highlight.value]) {
    e.preventDefault()
    pickRow(rows.value[highlight.value])
  } else if (e.key === 'Escape') {
    emit('close')
  } else if (e.key === 'Tab' && showFilesTab.value) {
    e.preventDefault()
    tab.value = tab.value === 'skills' ? 'files' : 'skills'
  }
}
defineExpose({ onKey })
</script>

<template>
  <div ref="anchorRef" class="file-picker-anchor" aria-hidden="true"></div>
  <Teleport to="body">
    <div v-if="visible" class="file-picker" :style="panelStyle" @mousedown.prevent>
      <div class="hd">
        <i class="ti ti-at"></i>
        <button
          type="button"
          class="tab"
          :class="{ on: tab === 'skills' }"
          @click="tab = 'skills'"
        >
          大师风格
        </button>
        <button
          v-if="showFilesTab"
          type="button"
          class="tab"
          :class="{ on: tab === 'files' }"
          @click="tab = 'files'"
        >
          文件
        </button>
        <span class="hint"
          >↑↓ 选择 · Enter 确认 · Esc 关闭<span v-if="showFilesTab"> · Tab 切换</span></span
        >
      </div>
      <div v-if="loading" class="empty">加载中…</div>
      <div v-else-if="!rows.length" class="empty">
        {{ tab === 'files' ? '没有匹配文件' : '没有匹配的大师风格' }}
      </div>
      <div v-else class="list">
        <div
          v-for="(row, i) in rows"
          :key="row.key"
          class="item"
          :class="{ hi: i === highlight }"
          @click="pickRow(row)"
          @mouseenter="highlight = i"
        >
          <i class="ti" :class="row.icon"></i>
          <div class="meta">
            <span class="name">{{ row.title }}</span>
            <span class="path">{{ row.subtitle }}</span>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.file-picker-anchor {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 0;
  width: 0;
  pointer-events: none;
}
.file-picker {
  position: fixed;
  z-index: var(--z-modal);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--surface);
  color: var(--fg);
  border: 1px solid var(--bd-strong, var(--bd));
  border-radius: 12px;
  box-shadow: var(--shadow-2);
}
.hd {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 14px;
  font-size: 12px;
  color: var(--sec);
  border-bottom: 1px solid var(--bd);
  background: var(--surface-2);
  flex-shrink: 0;
}
.tab {
  border: none;
  background: transparent;
  color: var(--sec);
  font-size: 12px;
  padding: 3px 10px;
  border-radius: 6px;
  cursor: pointer;
}
.tab.on {
  background: var(--acctx-bg, rgba(60, 120, 220, 0.18));
  color: var(--fg);
  font-weight: 600;
}
.hint {
  margin-left: auto;
  font-size: 11px;
  color: var(--muted);
  white-space: nowrap;
}
.list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
  background: var(--surface);
}
.item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 14px;
  cursor: pointer;
}
.item > .ti {
  margin-top: 2px;
  color: var(--acctx, var(--brand-solid));
  flex-shrink: 0;
}
.item.hi,
.item:hover {
  background: var(--acctx-bg, rgba(60, 120, 220, 0.14));
}
.meta {
  min-width: 0;
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.item .name {
  font-weight: 600;
  font-size: 14px;
  color: var(--fg);
  line-height: 1.3;
}
.item .path {
  font-size: 12px;
  line-height: 1.4;
  color: var(--sec);
  white-space: normal;
  word-break: break-word;
}
.empty {
  padding: 28px 16px;
  text-align: center;
  color: var(--muted);
  font-size: 13px;
  background: var(--surface);
}
</style>
