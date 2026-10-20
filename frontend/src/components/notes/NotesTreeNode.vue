<script setup>
import { computed } from 'vue'

const props = defineProps({
  folder: { type: Object, required: true },
  depth: { type: Number, default: 0 },
  childrenOf: { type: Object, required: true },
  notesOf: { type: Object, required: true },
  expanded: { type: Object, required: true },
  activeNoteId: String,
  selectedFolderId: { type: [String, null], default: null },
  /** 树焦点：选中文档时只高亮文档，不连带高亮父文件夹 */
  treeFocus: { type: String, default: 'folder' },
  renamingId: String,
  renameDraft: String,
})
const emit = defineEmits([
  'toggle',
  'select-note',
  'select-folder',
  'open-menu',
  'update:renameDraft',
  'commit-rename',
  'cancel-rename',
])

const childFolders = computed(() => props.childrenOf.get(props.folder.id) || [])
const childNotes = computed(() => props.notesOf.get(props.folder.id) || [])
const hasChildren = computed(
  () => childFolders.value.length > 0 || childNotes.value.length > 0,
)
const folderActive = computed(
  () =>
    props.treeFocus === 'folder' &&
    props.selectedFolderId === props.folder.id,
)
</script>

<template>
  <div class="nt-node">
    <div
      class="nt-row folder"
      :class="{ active: folderActive }"
      :style="{ '--nt-depth': depth }"
      @click.stop="emit('select-folder', folder.id)"
      @contextmenu.prevent.stop="emit('open-menu', $event, 'folder', folder.id)"
    >
      <button
        v-if="hasChildren"
        type="button"
        class="nt-chevron"
        :aria-expanded="expanded.has(folder.id)"
        @click.stop="emit('toggle', folder.id)"
      >
        <i :class="expanded.has(folder.id) ? 'ti ti-chevron-down' : 'ti ti-chevron-right'" />
      </button>
      <span v-else class="nt-chevron-spacer" aria-hidden="true" />
      <i class="ti ti-folder nt-icon" />
      <input
        v-if="renamingId === folder.id"
        :id="'nt-rename-' + folder.id"
        class="sess-rename-input"
        :value="renameDraft"
        maxlength="80"
        @click.stop
        @input="emit('update:renameDraft', $event.target.value)"
        @blur="emit('commit-rename')"
        @keydown.enter.prevent="emit('commit-rename')"
        @keydown.esc.prevent="emit('cancel-rename')"
      />
      <span v-else class="nt-label">{{ folder.name }}</span>
      <i
        v-if="renamingId !== folder.id"
        class="ti ti-dots nt-dots"
        title="更多"
        @click.stop="emit('open-menu', $event, 'folder', folder.id)"
      />
    </div>
    <template v-if="hasChildren && expanded.has(folder.id)">
      <NotesTreeNode
        v-for="c in childFolders"
        :key="c.id"
        :folder="c"
        :depth="depth + 1"
        :children-of="childrenOf"
        :notes-of="notesOf"
        :expanded="expanded"
        :active-note-id="activeNoteId"
        :selected-folder-id="selectedFolderId"
        :tree-focus="treeFocus"
        :renaming-id="renamingId"
        :rename-draft="renameDraft"
        @toggle="emit('toggle', $event)"
        @select-note="emit('select-note', $event)"
        @select-folder="emit('select-folder', $event)"
        @open-menu="(e, type, id) => emit('open-menu', e, type, id)"
        @update:renameDraft="emit('update:renameDraft', $event)"
        @commit-rename="emit('commit-rename')"
        @cancel-rename="emit('cancel-rename')"
      />
      <div
        v-for="n in childNotes"
        :key="n.id"
        class="nt-row note"
        :class="{ active: treeFocus === 'note' && activeNoteId === n.id }"
        :style="{ '--nt-depth': depth + 1 }"
        @click.stop="emit('select-note', n.id)"
        @contextmenu.prevent.stop="emit('open-menu', $event, 'note', n.id)"
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
          @input="emit('update:renameDraft', $event.target.value)"
          @blur="emit('commit-rename')"
          @keydown.enter.prevent="emit('commit-rename')"
          @keydown.esc.prevent="emit('cancel-rename')"
        />
        <span v-else class="nt-label">{{ n.title || '未命名文档' }}</span>
        <i
          v-if="renamingId !== n.id"
          class="ti ti-dots nt-dots"
          title="更多"
          @click.stop="emit('open-menu', $event, 'note', n.id)"
        />
      </div>
    </template>
  </div>
</template>
