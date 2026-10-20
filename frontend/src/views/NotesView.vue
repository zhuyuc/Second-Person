<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { notesApi } from '@/api/notes'
import { copyTextToClipboard } from '@/utils/clipboard'
import { formatTimeFull } from '@/utils/format'
import { useToast } from '@/stores/toast'
import { useConfirm } from '@/stores/confirm'
import BaseModal from '@/components/BaseModal.vue'
import NotesTree from '@/components/notes/NotesTree.vue'
import NoteEditor from '@/components/notes/NoteEditor.vue'
import SelectionActionBar from '@/components/SelectionActionBar.vue'
import QuoteComposer from '@/components/QuoteComposer.vue'

const emit = defineEmits(['open-aside'])
const toast = useToast()
const confirm = useConfirm()

const folders = ref([])
const notes = ref([])
const selectedFolderId = ref(null)
/** 树高亮焦点：选文档时不连带高亮父文件夹 */
const treeFocus = ref('folder') // 'folder' | 'note'
/** 笔记库搜索（标题+正文） */
const libQuery = ref('')
const libHits = ref([])
const libTotal = ref(0)
const libSearching = ref(false)
let libSearchTimer = null
let libSearchGen = 0
const activeNoteId = ref(null)
const activeNote = ref(null)
const bodyMd = ref('')
const titleDraft = ref('')
const saving = ref(false)
const aiLocked = ref(false)
const editorRef = ref(null)
const dirty = ref(false)
const updatedAt = ref('')

const selVisible = ref(false)
const selRect = ref(null)
const selText = ref('')
const quoteOpen = ref(false)
const quotePending = ref(null)

/** 应用内命名弹窗（替代 window.prompt，嵌入环境原生 prompt 常被静默拦截） */
const nameDlg = ref(null) // { mode, title, label, value, targetId?, parentId? }
const nameInput = ref('')
const nameInputEl = ref(null)
const nameBusy = ref(false)

/** 备份恢复面板：展示最近 3 份，可预览正文后再确认恢复 */
const revDlgOpen = ref(false)
const revList = ref([])
const revSelectedId = ref(null)
const revLoading = ref(false)
const revRestoring = ref(false)

const revSelected = computed(() =>
  revList.value.find((r) => r.id === revSelectedId.value) || null,
)

let saveTimer = null
let saveGen = 0
let cursorSyncTimer = null
let cursorSyncGen = 0

const canEdit = computed(() => !!activeNoteId.value && !aiLocked.value)

/** 把编辑器光标/选区锚点同步到后端，供 insert_at_cursor 使用 */
async function syncCursor({ force = false } = {}) {
  const id = activeNoteId.value
  if (!id || !editorRef.value?.getCursorAnchor) return null
  const anchor = editorRef.value.getCursorAnchor()
  if (!force && !anchor.before && !anchor.after) return null
  const gen = ++cursorSyncGen
  try {
    await notesApi.setCursor(id, {
      before: anchor.before || '',
      after: anchor.after || '',
    })
    if (gen !== cursorSyncGen) return null
    return anchor
  } catch {
    return null
  }
}

function scheduleCursorSync() {
  clearTimeout(cursorSyncTimer)
  cursorSyncTimer = setTimeout(() => {
    syncCursor().catch(() => {})
  }, 280)
}

const NAME_DEFAULTS = {
  'create-folder': { title: '新建文件夹', label: '文件夹名称', value: '新建文件夹' },
  'create-note': { title: '新建文档', label: '文档名称', value: '未命名文档' },
}

function openNameDialog(mode, extras = {}) {
  const meta = NAME_DEFAULTS[mode]
  if (!meta) return
  nameDlg.value = {
    mode,
    title: meta.title,
    label: meta.label,
    targetId: extras.targetId || null,
    parentId: extras.parentId ?? null,
  }
  nameInput.value = extras.value ?? meta.value ?? ''
  nextTick(() => {
    nameInputEl.value?.focus()
    nameInputEl.value?.select()
  })
}

function closeNameDialog() {
  if (nameBusy.value) return
  nameDlg.value = null
  nameInput.value = ''
}

/** 仅当 id 仍在当前树中时才当作父文件夹，避免已删/孤儿 id 触发「文件夹不存在」 */
function resolveFolderId(id) {
  if (!id) return null
  return folders.value.some((f) => f.id === id) ? id : null
}

async function refreshTree() {
  const data = await notesApi.tree()
  folders.value = data.folders || []
  notes.value = data.notes || []
  // 删除或外部变更后清掉失效的选中文件夹，避免后续新建沿用脏 parent_id
  if (selectedFolderId.value && !resolveFolderId(selectedFolderId.value)) {
    selectedFolderId.value = null
  }
}

function matchLabel(hit) {
  const parts = hit?.match_in || []
  if (parts.includes('title') && parts.includes('body')) return '标题·正文'
  if (parts.includes('title')) return '标题'
  if (parts.includes('body')) return '正文'
  return ''
}

function escapeHtml(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/** 将关键字在文本中高亮为 <mark>（先转义再拆分，防 XSS） */
function highlightQuery(text, query) {
  const raw = String(text ?? '')
  const q = String(query ?? '').trim()
  if (!raw) return ''
  if (!q) return escapeHtml(raw)
  const lower = raw.toLowerCase()
  const needle = q.toLowerCase()
  let out = ''
  let i = 0
  while (i < raw.length) {
    const at = lower.indexOf(needle, i)
    if (at < 0) {
      out += escapeHtml(raw.slice(i))
      break
    }
    out += escapeHtml(raw.slice(i, at))
    out += `<mark>${escapeHtml(raw.slice(at, at + q.length))}</mark>`
    i = at + Math.max(1, q.length)
  }
  return out
}

function scheduleLibSearch() {
  clearTimeout(libSearchTimer)
  const q = libQuery.value.trim()
  if (!q) {
    libHits.value = []
    libTotal.value = 0
    libSearching.value = false
    return
  }
  libSearching.value = true
  libSearchTimer = setTimeout(() => runLibSearch(q), 280)
}

async function runLibSearch(q) {
  const query = (q ?? libQuery.value).trim()
  if (!query) {
    libHits.value = []
    libTotal.value = 0
    libSearching.value = false
    return
  }
  const gen = ++libSearchGen
  libSearching.value = true
  try {
    const data = await notesApi.search(query)
    if (gen !== libSearchGen) return
    libHits.value = data.hits || []
    libTotal.value = data.total || 0
  } catch {
    if (gen !== libSearchGen) return
    libHits.value = []
    libTotal.value = 0
  } finally {
    if (gen === libSearchGen) libSearching.value = false
  }
}

function clearLibSearch() {
  clearTimeout(libSearchTimer)
  libQuery.value = ''
  libHits.value = []
  libTotal.value = 0
  libSearching.value = false
}

async function openSearchHit(hit) {
  if (!hit?.id) return
  await openNote(hit.id)
}

function selectFolder(id) {
  selectedFolderId.value = resolveFolderId(id)
  treeFocus.value = 'folder'
}

async function openNote(id) {
  if (!id) return
  flushSave()
  activeNoteId.value = id
  treeFocus.value = 'note'
  const n = await notesApi.get(id)
  activeNote.value = n
  titleDraft.value = n.title || '未命名文档'
  bodyMd.value = n.body_md || ''
  updatedAt.value = n.updated_at || ''
  dirty.value = false
  // 仅作「新建到此文件夹」上下文，不高亮文件夹行；孤儿 folder_id 视为根
  selectedFolderId.value = resolveFolderId(n.folder_id)
  await nextTick()
  editorRef.value?.setMarkdown?.(n.body_md || '')
}

function createNote(folderId) {
  const raw = folderId !== undefined ? folderId : selectedFolderId.value
  openNameDialog('create-note', { parentId: resolveFolderId(raw) })
}

function createFolder(parentId) {
  const raw = parentId !== undefined ? parentId : selectedFolderId.value
  openNameDialog('create-folder', {
    parentId: resolveFolderId(raw),
  })
}

/** 行内重命名：先乐观改本地列表，再异步写盘，避免等接口才关掉输入框 */
async function renameFolder(id, name) {
  const next = String(name || '').trim()
  if (!next) return
  const prev = folders.value.find((x) => x.id === id)?.name
  folders.value = folders.value.map((f) => (f.id === id ? { ...f, name: next } : f))
  try {
    await notesApi.patchFolder(id, { name: next })
  } catch {
    if (prev != null) {
      folders.value = folders.value.map((f) => (f.id === id ? { ...f, name: prev } : f))
    }
  }
}

async function renameNote(id, name) {
  const next = String(name || '').trim() || '未命名文档'
  const prev = notes.value.find((x) => x.id === id)?.title
  notes.value = notes.value.map((n) => (n.id === id ? { ...n, title: next } : n))
  if (activeNoteId.value === id) titleDraft.value = next
  try {
    const n = await notesApi.patch(id, { title: next })
    if (activeNoteId.value === id) {
      updatedAt.value = n.updated_at || updatedAt.value
      if (activeNote.value) activeNote.value = { ...activeNote.value, title: next }
    }
  } catch {
    if (prev != null) {
      notes.value = notes.value.map((n) => (n.id === id ? { ...n, title: prev } : n))
      if (activeNoteId.value === id) titleDraft.value = prev
    }
  }
}

async function submitNameDialog() {
  const dlg = nameDlg.value
  if (!dlg || nameBusy.value) return
  const name = nameInput.value.trim()
  if (!name) {
    toast.push('info', '请输入名称')
    nameInputEl.value?.focus()
    return
  }
  nameBusy.value = true
  try {
    if (dlg.mode === 'create-folder') {
      const parentId = resolveFolderId(dlg.parentId)
      await notesApi.createFolder({
        name,
        ...(parentId ? { parent_id: parentId } : {}),
      })
      await refreshTree()
      toast.push('success', '文件夹已创建')
    } else if (dlg.mode === 'create-note') {
      const folderId = resolveFolderId(dlg.parentId)
      const n = await notesApi.create({
        title: name,
        ...(folderId ? { folder_id: folderId } : {}),
      })
      await refreshTree()
      await openNote(n.id)
    }
    nameDlg.value = null
    nameInput.value = ''
  } finally {
    nameBusy.value = false
  }
}

function onNameKeydown(e) {
  if (e.key === 'Enter' && !e.isComposing) {
    e.preventDefault()
    submitNameDialog()
  }
}

async function deleteFolder(id) {
  const f = folders.value.find((x) => x.id === id)
  const ok = await confirm.ask({
    title: '删除文件夹',
    message: `将删除文件夹「${f?.name || ''}」及其内全部文档，且不可恢复。是否继续？`,
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  const r = await notesApi.deleteFolder(id)
  if (r.deleted_notes?.includes(activeNoteId.value)) {
    activeNoteId.value = null
    activeNote.value = null
    bodyMd.value = ''
  }
  await refreshTree()
  toast.push('success', '文件夹已删除')
}

async function deleteNote(id) {
  const n = notes.value.find((x) => x.id === id) || activeNote.value
  const ok = await confirm.ask({
    title: '删除文档',
    message: `确定删除「${n?.title || '未命名文档'}」？此操作不可恢复。`,
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  await notesApi.delete(id)
  if (activeNoteId.value === id) {
    activeNoteId.value = null
    activeNote.value = null
    bodyMd.value = ''
  }
  await refreshTree()
  toast.push('success', '文档已删除')
}

function scheduleSave() {
  dirty.value = true
  clearTimeout(saveTimer)
  saveTimer = setTimeout(() => doSave(), 1000)
}

async function flushSave() {
  clearTimeout(saveTimer)
  if (dirty.value) await doSave()
}

async function doSave({ refresh = true } = {}) {
  if (!activeNoteId.value || aiLocked.value) return
  const gen = ++saveGen
  saving.value = true
  const title = titleDraft.value.trim() || '未命名文档'
  // 标题立刻反映到目录树，不等接口
  notes.value = notes.value.map((n) =>
    n.id === activeNoteId.value ? { ...n, title } : n,
  )
  try {
    const payload = {
      title,
      body_md: bodyMd.value,
      if_updated_at: updatedAt.value || undefined,
    }
    const n = await notesApi.patch(activeNoteId.value, payload)
    if (gen !== saveGen) return
    updatedAt.value = n.updated_at || ''
    activeNote.value = n
    dirty.value = false
    if (refresh) {
      // 后台轻量同步树；失败不影响已乐观更新的标题
      refreshTree().catch(() => {})
    }
  } catch (e) {
    if (e?.status === 409 || String(e?.message || '').includes('冲突')) {
      toast.push('warning', '文档已在别处更新，请重新打开')
    }
  } finally {
    saving.value = false
  }
}

function onBodyUpdate(md) {
  bodyMd.value = md
  scheduleSave()
}

function onTitleBlur() {
  if (!activeNoteId.value) return
  dirty.value = true
  // 改标题立即保存，不再等 1s 防抖
  clearTimeout(saveTimer)
  doSave({ refresh: false })
}

function onSelectionChange({ text, rect }) {
  selText.value = text || ''
  if (text && rect && (rect.width > 0 || rect.height > 0)) {
    selVisible.value = true
    selRect.value = rect
  } else {
    selVisible.value = false
  }
  // 光标/选区变化即上报，侧边 insert_at_cursor 才能插到正确位置
  scheduleCursorSync()
}

async function copySel() {
  if (!selText.value) return
  const ok = await copyTextToClipboard(selText.value)
  if (ok) toast.push('success', '已复制')
  else toast.push('warning', '复制失败，请手动 Ctrl+C')
  selVisible.value = false
}

async function openAsideFromSel() {
  if (!selText.value) return
  // 开侧边前强制同步一次光标锚点
  await syncCursor({ force: true })
  quotePending.value = {
    text: selText.value,
    sourceMsgId: activeNoteId.value,
    sourceRole: 'note',
  }
  quoteOpen.value = true
  selVisible.value = false
}

/** 工具栏「AI 写文档」：打开侧边会话，带上当前文档上下文 */
async function openAsideForAiWrite() {
  if (!activeNoteId.value) {
    toast.push('info', '请先打开或新建一篇文档')
    return
  }
  if (aiLocked.value) {
    toast.push('info', 'AI 正在编辑，请稍候')
    return
  }
  await flushSave()
  await syncCursor({ force: true })
  // 打开 AI 前先打检查点，避免「磁盘仍是空/旧稿」时写回只备份到空文档
  try {
    await notesApi.createRevision(activeNoteId.value, 'pre_ai')
  } catch {
    /* 空文档会跳过；失败不阻断侧边会话 */
  }
  const title = (titleDraft.value || '未命名文档').trim() || '未命名文档'
  const body = (bodyMd.value || '').trim()
  const preview = body ? body.slice(0, 480) : ''
  const text = preview
    ? `当前文档「${title}」\n\n${preview}${body.length > 480 ? '…' : ''}`
    : `当前文档「${title}」（正文为空）`
  emit('open-aside', {
    text,
    comment: '用户从记事本「AI 写文档」入口打开。请按用户说明撰写或改写当前文档。',
    sourceMsgId: activeNoteId.value,
    sourceRole: 'note',
    noteId: activeNoteId.value,
  })
}

async function onQuoteConfirm({ comment }) {
  quoteOpen.value = false
  await flushSave()
  await syncCursor({ force: true })
  if (activeNoteId.value) {
    try {
      await notesApi.createRevision(activeNoteId.value, 'pre_ai')
    } catch {
      /* ignore */
    }
  }
  emit('open-aside', {
    text: quotePending.value?.text || '',
    comment: comment || '',
    sourceMsgId: activeNoteId.value,
    sourceRole: 'note',
    noteId: activeNoteId.value,
  })
}

function revSourceLabel(source) {
  if (source === 'ai') return 'AI 改写前'
  if (source === 'pre_restore') return '恢复前快照'
  if (source === 'checkpoint' || source === 'pre_ai') return 'AI 前检查点'
  return source || '备份'
}

function closeRevDialog() {
  if (revRestoring.value) return
  revDlgOpen.value = false
  revList.value = []
  revSelectedId.value = null
}

async function openRevDialog() {
  if (!activeNoteId.value) return
  revDlgOpen.value = true
  revLoading.value = true
  revList.value = []
  revSelectedId.value = null
  try {
    const data = await notesApi.revisions(activeNoteId.value, {
      limit: 10,
      includeBody: true,
    })
    revList.value = data.list || []
    if (revList.value.length) revSelectedId.value = revList.value[0].id
    else toast.push('info', '暂无备份可恢复')
  } catch {
    revDlgOpen.value = false
  } finally {
    revLoading.value = false
  }
}

async function confirmRestoreSelected() {
  const rev = revSelected.value
  if (!rev?.id || !activeNoteId.value || revRestoring.value) return
  const ok = await confirm.ask({
    title: '确认恢复此备份',
    message: `将用「${revSourceLabel(rev.source)} · ${formatTimeFull(rev.created_at) || '未知时间'}」覆盖当前正文；恢复前会再自动备份当前内容，可再次反悔。`,
    confirmText: '恢复此版本',
  })
  if (!ok) return
  revRestoring.value = true
  try {
    const n = await notesApi.restore(activeNoteId.value, rev.id)
    bodyMd.value = n.body_md || ''
    updatedAt.value = n.updated_at || ''
    editorRef.value?.setMarkdown?.(n.body_md || '')
    await refreshTree()
    closeRevDialog()
    toast.push('success', '已恢复所选备份')
  } finally {
    revRestoring.value = false
  }
}

async function onNoteUpdated(ev) {
  const d = ev?.detail
  if (!d?.note_id || d.note_id !== activeNoteId.value) return
  aiLocked.value = true
  try {
    await editorRef.value?.streamApplyMarkdown?.(d.body_md || '')
    bodyMd.value = d.body_md || ''
    updatedAt.value = d.updated_at || updatedAt.value
    dirty.value = false
    await refreshTree()
  } finally {
    aiLocked.value = false
  }
}

onMounted(async () => {
  await refreshTree()
  window.addEventListener('sp-note-updated', onNoteUpdated)
  if (notes.value[0]) await openNote(notes.value[0].id)
})
onUnmounted(() => {
  flushSave()
  clearTimeout(cursorSyncTimer)
  clearTimeout(libSearchTimer)
  window.removeEventListener('sp-note-updated', onNoteUpdated)
  if (window.__spActiveNoteId === activeNoteId.value) {
    window.__spActiveNoteId = null
  }
})

watch(activeNoteId, (id) => {
  // 供侧边会话绑定
  window.__spActiveNoteId = id || null
})
</script>

<template>
  <div class="notes-page">
    <header class="notes-head">
      <h1>记事本</h1>
      <button type="button" class="btn-primary btn-sm" @click="createNote()">
        <i class="ti ti-plus" /> 新建文档
      </button>
    </header>

    <div class="notes-body">
      <aside class="notes-aside">
        <div class="notes-lib-search">
          <i class="ti ti-search nls-icon" aria-hidden="true" />
          <input
            v-model="libQuery"
            class="nls-input"
            type="search"
            placeholder="搜索笔记库…"
            maxlength="120"
            aria-label="搜索笔记库"
            @input="scheduleLibSearch"
            @keydown.escape.prevent="clearLibSearch"
          />
          <button
            v-if="libQuery"
            type="button"
            class="nls-clear"
            title="清除"
            aria-label="清除搜索"
            @click="clearLibSearch"
          >
            <i class="ti ti-x" />
          </button>
        </div>

        <div v-if="libQuery.trim()" class="notes-search-panel">
          <div class="nsp-meta">
            <template v-if="libSearching">搜索中…</template>
            <template v-else>共 {{ libTotal }} 条结果</template>
          </div>
          <div v-if="!libSearching && !libHits.length" class="nsp-empty">未找到匹配笔记</div>
          <button
            v-for="hit in libHits"
            :key="hit.id"
            type="button"
            class="nsp-hit"
            :class="{ active: activeNoteId === hit.id }"
            @click="openSearchHit(hit)"
          >
            <div class="nsp-hit-top">
              <i class="ti ti-file-text nsp-hit-icon" />
              <span
                class="nsp-hit-title"
                v-html="highlightQuery(hit.title || '未命名文档', libQuery)"
              />
              <span class="nsp-hit-tag">{{ matchLabel(hit) }}</span>
            </div>
            <div
              v-if="hit.snippet"
              class="nsp-hit-snip"
              v-html="highlightQuery(hit.snippet, libQuery)"
            />
          </button>
        </div>

        <NotesTree
          v-else
          :folders="folders"
          :notes="notes"
          :active-note-id="activeNoteId"
          :selected-folder-id="selectedFolderId"
          :tree-focus="treeFocus"
          @select-note="openNote"
          @select-folder="selectFolder"
          @create-note="createNote"
          @create-folder="createFolder"
          @rename-folder="renameFolder"
          @rename-note="renameNote"
          @delete-folder="deleteFolder"
          @delete-note="deleteNote"
        />
      </aside>

      <main class="notes-main">
        <div v-if="!activeNoteId" class="notes-empty">
          <p>选择或新建一篇文档开始写作</p>
        </div>
        <template v-else>
          <div class="note-title-row">
            <input
              v-model="titleDraft"
              class="note-title"
              maxlength="120"
              @blur="onTitleBlur"
              @keyup.enter="($event.target.blur())"
            />
            <span class="note-status">
              <template v-if="aiLocked">AI 编辑中…</template>
              <template v-else-if="saving">保存中…</template>
              <template v-else-if="dirty">未保存</template>
              <template v-else>已保存</template>
            </span>
            <button
              type="button"
              class="btn-primary btn-sm note-ai-btn"
              title="打开侧边会话，让 AI 撰写或改写当前文档"
              :disabled="aiLocked"
              @click="openAsideForAiWrite"
            >
              <i class="ti ti-sparkles" /> AI 写文档
            </button>
            <button
              type="button"
              class="btn-sm"
              title="查看最近备份并恢复"
              @click="openRevDialog"
            >
              恢复备份
            </button>
          </div>
          <NoteEditor
            ref="editorRef"
            :note-id="activeNoteId"
            :model-value="bodyMd"
            :editable="canEdit"
            @update:model-value="onBodyUpdate"
            @selection-change="onSelectionChange"
          />
        </template>
      </main>
    </div>

    <SelectionActionBar
      :visible="selVisible"
      :rect="selRect"
      :show-aside="true"
      :show-quote="false"
      @copy="copySel"
      @aside="openAsideFromSel"
    />

    <QuoteComposer
      :visible="quoteOpen"
      :quote-text="quotePending?.text || ''"
      @cancel="quoteOpen = false"
      @confirm="onQuoteConfirm"
    />

    <BaseModal
      v-if="nameDlg"
      :title="nameDlg.title"
      size="sm"
      @close="closeNameDialog"
    >
      <div class="form-group">
        <label class="label">{{ nameDlg.label }}</label>
        <input
          ref="nameInputEl"
          v-model="nameInput"
          maxlength="120"
          :disabled="nameBusy"
          @keydown="onNameKeydown"
        />
      </div>
      <template #footer>
        <button type="button" :disabled="nameBusy" @click="closeNameDialog">取消</button>
        <button
          type="button"
          class="btn-primary"
          :disabled="nameBusy"
          @click="submitNameDialog"
        >
          <i v-if="nameBusy" class="ti ti-loader-2" />
          确定
        </button>
      </template>
    </BaseModal>

    <BaseModal
      v-if="revDlgOpen"
      title="恢复备份"
      size="xl"
      :close-on-overlay="!revRestoring"
      :close-on-esc="!revRestoring"
      @close="closeRevDialog"
    >
      <div class="rev-dlg">
        <p class="rev-hint">展示最近 10 份备份（与磁盘保留一致），请先预览内容再决定恢复哪一份。</p>
        <div v-if="revLoading" class="rev-empty rev-empty-fill">
          <i class="ti ti-loader-2" aria-hidden="true"></i>
          <span>加载中…</span>
        </div>
        <div v-else-if="!revList.length" class="rev-empty rev-empty-fill">暂无备份</div>
        <div v-else class="rev-layout">
          <div class="rev-list" role="listbox" aria-label="备份列表">
            <button
              v-for="(rev, idx) in revList"
              :key="rev.id"
              type="button"
              class="rev-item"
              :class="{ active: revSelectedId === rev.id }"
              role="option"
              :aria-selected="revSelectedId === rev.id"
              :disabled="revRestoring"
              @click="revSelectedId = rev.id"
            >
              <div class="rev-item-top">
                <span class="rev-item-idx">#{{ idx + 1 }}</span>
                <span class="rev-item-src">{{ revSourceLabel(rev.source) }}</span>
              </div>
              <div class="rev-item-time">{{ formatTimeFull(rev.created_at) || '—' }}</div>
              <div class="rev-item-meta">{{ rev.char_count ?? 0 }} 字</div>
            </button>
          </div>
          <div class="rev-preview">
            <div class="rev-preview-hd">备份内容预览</div>
            <pre v-if="revSelected" class="rev-preview-body">{{
              revSelected.body_md || revSelected.preview || '（空文档）'
            }}</pre>
            <div v-else class="rev-empty rev-empty-fill">请选择左侧一份备份</div>
          </div>
        </div>
      </div>
      <template #footer>
        <button type="button" :disabled="revRestoring" @click="closeRevDialog">取消</button>
        <button
          type="button"
          class="btn-primary"
          :disabled="revRestoring || !revSelected"
          @click="confirmRestoreSelected"
        >
          <i v-if="revRestoring" class="ti ti-loader-2" aria-hidden="true"></i>
          {{ revRestoring ? '恢复中…' : '恢复此版本' }}
        </button>
      </template>
    </BaseModal>
  </div>
</template>

<style scoped>
.notes-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  max-width: none;
}
.notes-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  padding: 0 0 var(--sp-3);
  border-bottom: 1px solid var(--bd);
  margin-bottom: 0;
}
.notes-head h1 {
  margin: 0;
  font-size: var(--fs-2xl);
  font-weight: 600;
}
.notes-body {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 240px 1fr;
  margin: 0 calc(-1 * var(--sp-4));
  padding-top: var(--sp-2);
}
.notes-aside {
  border-right: 1px solid var(--bd);
  overflow: auto;
  padding: var(--sp-1) var(--sp-2) var(--sp-3);
  background: transparent;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.notes-lib-search {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: var(--sp-2);
  padding: 6px 8px;
  border: 1px solid var(--bd);
  border-radius: var(--radius-xs);
  background: var(--surface);
}
.nls-icon {
  color: var(--muted);
  font-size: 14px;
  flex-shrink: 0;
}
.nls-input {
  flex: 1;
  min-width: 0;
  border: none;
  background: transparent;
  color: var(--fg);
  font-size: var(--fs-sm);
  outline: none;
}
.nls-input::-webkit-search-cancel-button {
  display: none;
}
.nls-clear {
  border: none;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  padding: 0 2px;
  display: inline-flex;
  line-height: 1;
}
.nls-clear:hover {
  color: var(--fg);
}
.notes-search-panel {
  flex: 1;
  min-height: 0;
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.nsp-meta {
  font-size: var(--fs-xs);
  color: var(--muted);
  padding: 2px 4px 6px;
}
.nsp-empty {
  font-size: var(--fs-sm);
  color: var(--muted);
  padding: 16px 8px;
  text-align: center;
}
.nsp-hit {
  display: block;
  width: 100%;
  text-align: left;
  border: 1px solid transparent;
  border-radius: var(--radius-xs);
  background: transparent;
  color: var(--fg);
  padding: 8px;
  cursor: pointer;
}
.nsp-hit:hover {
  background: var(--surface-2);
}
.nsp-hit.active {
  background: var(--accbg, var(--surface-2));
  border-color: var(--bd);
}
.nsp-hit-top {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.nsp-hit-icon {
  color: var(--muted);
  flex-shrink: 0;
}
.nsp-hit-title {
  flex: 1;
  min-width: 0;
  font-size: var(--fs-sm);
  font-weight: 550;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.nsp-hit-tag {
  flex-shrink: 0;
  font-size: 10px;
  color: var(--muted);
  border: 1px solid var(--bd);
  border-radius: 4px;
  padding: 0 4px;
  line-height: 1.5;
}
.nsp-hit-snip {
  margin-top: 4px;
  padding-left: 22px;
  font-size: var(--fs-xs);
  color: var(--sec, var(--muted));
  line-height: 1.45;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.nsp-hit-title :deep(mark),
.nsp-hit-snip :deep(mark) {
  background: var(--brand-soft, var(--warnbg));
  color: var(--acctx, var(--warntx));
  padding: 0 2px;
  border-radius: 3px;
  font-weight: 600;
}
.notes-main {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  padding: 0 var(--sp-5) var(--sp-4);
}
.notes-empty {
  display: grid;
  place-items: center;
  color: var(--muted);
  flex: 1;
  font-size: var(--fs-md);
}
.note-title-row {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-3) 0 var(--sp-2);
}
.note-title {
  flex: 1;
  border: none;
  background: transparent;
  font-size: var(--fs-xl);
  font-weight: 600;
  color: var(--fg);
  outline: none;
}
.note-status {
  font-size: var(--fs-sm);
  color: var(--muted);
  white-space: nowrap;
}
.note-ai-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  white-space: nowrap;
}
.note-ai-btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.rev-dlg {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  min-height: 360px;
  /* 占满 modal-bd，预览区内部滚动，页脚始终贴底可见 */
  height: min(56vh, 480px);
  max-height: min(56vh, 480px);
}
.rev-hint {
  margin: 0;
  flex-shrink: 0;
  font-size: var(--fs-sm);
  color: var(--muted);
}
.rev-layout {
  display: grid;
  grid-template-columns: 200px 1fr;
  gap: var(--sp-3);
  flex: 1 1 auto;
  min-height: 0;
}
.rev-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  overflow: auto;
  min-height: 0;
  border-right: 1px solid var(--bd);
  padding-right: var(--sp-2);
}
.rev-item {
  text-align: left;
  border: 1px solid var(--bd);
  border-radius: var(--radius-xs);
  background: var(--surface);
  color: var(--fg);
  padding: 8px 10px;
  cursor: pointer;
}
.rev-item:hover {
  background: var(--surface-2);
}
.rev-item.active {
  border-color: var(--acc, var(--brand-solid, #3b6ef6));
  background: var(--accbg, var(--surface-2));
}
.rev-item-top {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
}
.rev-item-idx {
  font-size: var(--fs-xs);
  color: var(--muted);
}
.rev-item-src {
  font-size: var(--fs-sm);
  font-weight: 600;
}
.rev-item-time,
.rev-item-meta {
  font-size: var(--fs-xs);
  color: var(--muted);
  line-height: 1.4;
}
.rev-preview {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}
.rev-preview-hd {
  font-size: var(--fs-sm);
  font-weight: 600;
  margin-bottom: 8px;
}
.rev-preview-body {
  flex: 1;
  min-height: 0;
  margin: 0;
  padding: 12px;
  overflow: auto;
  border: 1px solid var(--bd);
  border-radius: var(--radius-xs);
  background: var(--surface-2);
  color: var(--fg);
  white-space: pre-wrap;
  word-break: break-word;
  font-family: var(--mono);
  font-size: var(--fs-sm);
  line-height: 1.55;
}
.rev-empty {
  color: var(--muted);
  font-size: var(--fs-sm);
  padding: 24px 8px;
  text-align: center;
}
.rev-empty-fill {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
}
@media (max-width: 800px) {
  .rev-dlg {
    height: min(70vh, 560px);
    max-height: min(70vh, 560px);
  }
  .rev-layout {
    grid-template-columns: 1fr;
    grid-template-rows: auto 1fr;
  }
  .rev-list {
    border-right: none;
    border-bottom: 1px solid var(--bd);
    padding-right: 0;
    padding-bottom: var(--sp-2);
    flex-direction: row;
    overflow-x: auto;
    overflow-y: hidden;
    max-height: none;
  }
  .rev-item {
    min-width: 140px;
  }
}
@media (max-width: 800px) {
  .notes-body {
    grid-template-columns: 1fr;
    margin: 0;
  }
  .notes-aside {
    max-height: 220px;
    border-right: none;
    border-bottom: 1px solid var(--bd);
  }
}
</style>
