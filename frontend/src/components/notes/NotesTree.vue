<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import NotesTreeNode from './NotesTreeNode.vue'

const props = defineProps({
  folders: { type: Array, default: () => [] },
  notes: { type: Array, default: () => [] },
  activeNoteId: { type: String, default: null },
  // null = 根目录（未选中文件夹）；新建会落到根下
  selectedFolderId: { type: [String, null], default: null },
  /** 'note' | 'folder'：选文档时父文件夹不高亮 */
  treeFocus: { type: String, default: 'folder' },
})
const emit = defineEmits([
  'select-note',
  'select-folder',
  'create-note',
  'create-folder',
  'rename-folder',
  'rename-note',
  'delete-folder',
  'delete-note',
])

const expanded = ref(new Set())
const menu = ref(null)
const renamingId = ref(null)
const renameType = ref(null) // 'folder' | 'note'
const renameDraft = ref('')
let renameBusy = false

function toggle(id) {
  const s = new Set(expanded.value)
  if (s.has(id)) s.delete(id)
  else s.add(id)
  expanded.value = s
}

const childrenOf = computed(() => {
  const map = new Map()
  for (const f of props.folders) {
    const p = f.parent_id || null
    if (!map.has(p)) map.set(p, [])
    map.get(p).push(f)
  }
  for (const arr of map.values()) {
    arr.sort(
      (a, b) =>
        (a.sort_order ?? 0) - (b.sort_order ?? 0) ||
        String(a.name || '').localeCompare(String(b.name || ''), 'zh'),
    )
  }
  return map
})

const notesOf = computed(() => {
  const map = new Map()
  for (const n of props.notes) {
    const p = n.folder_id || null
    if (!map.has(p)) map.set(p, [])
    map.get(p).push(n)
  }
  for (const arr of map.values()) {
    arr.sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')))
  }
  return map
})

function openMenu(e, type, id) {
  const rect = e.currentTarget?.getBoundingClientRect?.()
  let top = e.clientY
  let left = e.clientX
  if (rect) {
    top = rect.bottom + 4
    left = Math.min(rect.right - 168, window.innerWidth - 180)
  }
  if (top + 160 > window.innerHeight) {
    top = Math.max(8, (rect?.top || e.clientY) - 164)
  }
  menu.value = { type, id, top, left }
}

function closeMenu() {
  menu.value = null
}

function act(fn) {
  fn()
  closeMenu()
}

function startRename(type, id) {
  closeMenu()
  let initial = ''
  if (type === 'folder') {
    initial = props.folders.find((x) => x.id === id)?.name || ''
  } else {
    initial = props.notes.find((x) => x.id === id)?.title || '未命名文档'
  }
  renamingId.value = id
  renameType.value = type
  renameDraft.value = initial
  // 重命名时确保父链展开，输入框可见
  if (type === 'note') {
    const folderId = props.notes.find((x) => x.id === id)?.folder_id
    if (folderId) {
      const s = new Set(expanded.value)
      s.add(folderId)
      expanded.value = s
    }
  }
  nextTick(() => {
    const el = document.getElementById('nt-rename-' + id)
    el?.focus()
    el?.select()
  })
}

function cancelRename() {
  renamingId.value = null
  renameType.value = null
  renameDraft.value = ''
}

function commitRename() {
  if (renameBusy) return
  const id = renamingId.value
  const type = renameType.value
  const name = renameDraft.value.trim()
  if (!id || !type) return
  if (!name) {
    cancelRename()
    return
  }
  const prev =
    type === 'folder'
      ? props.folders.find((x) => x.id === id)?.name
      : props.notes.find((x) => x.id === id)?.title
  if (name === (prev || '').trim()) {
    cancelRename()
    return
  }
  renameBusy = true
  // 先收起输入框，立刻反馈；父组件乐观更新 + 写盘
  cancelRename()
  if (type === 'folder') emit('rename-folder', id, name)
  else emit('rename-note', id, name)
  nextTick(() => {
    renameBusy = false
  })
}

function onDocClick() {
  if (menu.value) closeMenu()
}

/** 打开文档时自动展开其所在文件夹，保证选中项可见 */
watch(
  () => props.activeNoteId,
  (id) => {
    if (!id) return
    const folderId = props.notes.find((x) => x.id === id)?.folder_id
    if (!folderId) return
    if (expanded.value.has(folderId)) return
    const s = new Set(expanded.value)
    s.add(folderId)
    // 展开祖先链
    let cur = props.folders.find((f) => f.id === folderId)
    while (cur?.parent_id) {
      s.add(cur.parent_id)
      cur = props.folders.find((f) => f.id === cur.parent_id)
    }
    expanded.value = s
  },
  { immediate: true },
)

onMounted(() => document.addEventListener('click', onDocClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocClick))

defineExpose({ startRename })
</script>

<template>
  <div class="notes-tree">
    <div class="nt-hd">
      <span>目录</span>
      <div class="nt-hd-actions">
        <i
          class="ti ti-folder-plus nt-add"
          title="新建文件夹"
          @click.stop="emit('create-folder', selectedFolderId)"
        />
        <i
          class="ti ti-file-plus nt-add"
          title="新建文档"
          @click.stop="emit('create-note', selectedFolderId)"
        />
      </div>
    </div>

    <NotesTreeNode
      v-for="f in childrenOf.get(null) || []"
      :key="f.id"
      :folder="f"
      :depth="0"
      :children-of="childrenOf"
      :notes-of="notesOf"
      :expanded="expanded"
      :active-note-id="activeNoteId"
      :selected-folder-id="selectedFolderId"
      :tree-focus="treeFocus"
      :renaming-id="renamingId"
      :rename-draft="renameDraft"
      @toggle="toggle"
      @select-note="emit('select-note', $event)"
      @select-folder="emit('select-folder', $event)"
      @open-menu="openMenu"
      @update:renameDraft="(v) => (renameDraft = v)"
      @commit-rename="commitRename"
      @cancel-rename="cancelRename"
    />

    <div
      v-for="n in notesOf.get(null) || []"
      :key="n.id"
      class="nt-row note"
      :class="{ active: treeFocus === 'note' && activeNoteId === n.id }"
      :style="{ '--nt-depth': 0 }"
      @click.stop="emit('select-note', n.id)"
      @contextmenu.prevent.stop="openMenu($event, 'note', n.id)"
    >
      <span class="nt-chevron-spacer" aria-hidden="true" />
      <i class="ti ti-file-text nt-icon" />
      <input
        v-if="renamingId === n.id"
        :id="'nt-rename-' + n.id"
        class="sess-rename-input"
        :value="renameDraft"
        maxlength="120"
        @click.stop
        @input="renameDraft = $event.target.value"
        @blur="commitRename"
        @keydown.enter.prevent="commitRename"
        @keydown.esc.prevent="cancelRename"
      />
      <span v-else class="nt-label">{{ n.title || '未命名文档' }}</span>
      <i
        v-if="renamingId !== n.id"
        class="ti ti-dots nt-dots"
        title="更多"
        @click.stop="openMenu($event, 'note', n.id)"
      />
    </div>

    <div
      v-if="menu"
      class="nt-menu"
      :style="{ top: menu.top + 'px', left: menu.left + 'px' }"
      @click.stop
    >
      <template v-if="menu.type === 'folder'">
        <div @click="act(() => emit('create-folder', menu.id))">
          <i class="ti ti-folder-plus" /> 新建子文件夹
        </div>
        <div @click="act(() => emit('create-note', menu.id))">
          <i class="ti ti-file-plus" /> 新建文档
        </div>
        <div @click="act(() => startRename('folder', menu.id))">
          <i class="ti ti-pencil" /> 重命名
        </div>
        <div class="danger" @click="act(() => emit('delete-folder', menu.id))">
          <i class="ti ti-trash" /> 删除文件夹
        </div>
      </template>
      <template v-else>
        <div @click="act(() => startRename('note', menu.id))">
          <i class="ti ti-pencil" /> 重命名
        </div>
        <div class="danger" @click="act(() => emit('delete-note', menu.id))">
          <i class="ti ti-trash" /> 删除文档
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.notes-tree {
  position: relative;
  user-select: none;
  padding: var(--sp-1) 0;
}

.nt-hd {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--sp-1) var(--sp-3) var(--sp-2);
  font-size: var(--fs-xs);
  color: var(--muted);
  font-weight: 500;
}
.nt-hd-actions {
  display: flex;
  align-items: center;
  gap: 2px;
}
.nt-add {
  cursor: pointer;
  padding: 2px 4px;
  border-radius: var(--radius-xs);
  font-size: 15px;
  color: var(--muted);
}
.nt-add:hover {
  background: var(--surface-2);
  color: var(--acctx);
}

:deep(.nt-row),
.nt-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 30px;
  padding: 4px 10px;
  padding-left: calc(10px + var(--nt-depth, 0) * 14px);
  border-radius: 10px;
  cursor: pointer;
  color: var(--sec);
  font-size: var(--fs-base);
  font-weight: 500;
  transition:
    background var(--dur-fast),
    color var(--dur-fast);
}
:deep(.nt-row:hover),
.nt-row:hover {
  background: var(--surface-2);
  color: var(--fg);
}
:deep(.nt-row.active),
.nt-row.active {
  background: var(--brand-soft);
  color: var(--acctx);
}

:deep(.nt-icon),
.nt-icon {
  font-size: 16px;
  color: var(--muted);
  flex-shrink: 0;
  opacity: 0.7;
}
:deep(.nt-row.active .nt-icon),
.nt-row.active .nt-icon {
  color: var(--acctx);
  opacity: 1;
}
:deep(.nt-row.folder .nt-icon),
.nt-row.folder .nt-icon {
  opacity: 0.85;
}

:deep(.nt-label),
.nt-label {
  min-width: 0;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  line-height: 1.45;
  letter-spacing: 0.01em;
}

:deep(.sess-rename-input),
.sess-rename-input {
  flex: 1;
  min-width: 0;
  width: 100%;
  padding: 3px 8px;
  font-size: var(--fs-base);
  border-radius: 8px;
}

:deep(.nt-chevron),
.nt-chevron {
  border: none;
  background: transparent;
  padding: 0;
  width: 16px;
  height: 16px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--muted);
  cursor: pointer;
  flex-shrink: 0;
  border-radius: 4px;
}
:deep(.nt-chevron:hover),
.nt-chevron:hover {
  background: var(--surface-2);
  color: var(--fg);
}
:deep(.nt-chevron .ti),
.nt-chevron .ti {
  font-size: 14px;
}
:deep(.nt-chevron-spacer),
.nt-chevron-spacer {
  width: 16px;
  flex-shrink: 0;
}

:deep(.nt-dots),
.nt-dots {
  font-size: 15px;
  color: var(--muted);
  flex-shrink: 0;
  padding: 3px;
  border-radius: 6px;
  opacity: 0;
  transition: var(--dur-fast);
}
:deep(.nt-row:hover .nt-dots),
.nt-row:hover .nt-dots,
:deep(.nt-row.active .nt-dots),
.nt-row.active .nt-dots {
  opacity: 1;
}
:deep(.nt-dots:hover),
.nt-dots:hover {
  background: var(--surface-2);
  color: var(--fg);
}

.nt-menu {
  position: fixed;
  z-index: calc(var(--z-menu) + 1);
  background: var(--surface);
  border: 1px solid var(--bd);
  border-radius: 12px;
  box-shadow: var(--shadow-2);
  padding: 6px;
  min-width: 168px;
}
.nt-menu > div {
  padding: 8px 12px;
  border-radius: 8px;
  font-size: var(--fs-base);
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--fg);
}
.nt-menu > div:hover {
  background: var(--surface-2);
}
.nt-menu > div.danger {
  color: var(--dangtx);
}
.nt-menu .ti {
  font-size: 15px;
  opacity: 0.85;
}
</style>
