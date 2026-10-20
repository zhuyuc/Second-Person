<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Extension } from '@tiptap/core'
import { Plugin, PluginKey } from '@tiptap/pm/state'
import { Decoration, DecorationSet } from '@tiptap/pm/view'
import { Editor, EditorContent } from '@tiptap/vue-3'
import StarterKit from '@tiptap/starter-kit'
import Underline from '@tiptap/extension-underline'
import Link from '@tiptap/extension-link'
import Image from '@tiptap/extension-image'
import Placeholder from '@tiptap/extension-placeholder'
import { mdToHtml, htmlToMd, rewriteAssetUrls, restoreAssetPaths } from '@/utils/notesMarkdown'
import { notesApi } from '@/api/notes'
import { useToast } from '@/stores/toast'

const searchHighlightKey = new PluginKey('searchHighlight')

/** 在文档文本节点内定位 query（不跨节点），返回 [{from,to}] */
function collectSearchMatches(doc, query) {
  const matches = []
  if (!query) return matches
  doc.descendants((node, pos) => {
    if (!node.isText || !node.text) return
    const text = node.text
    let idx = 0
    while (true) {
      const at = text.indexOf(query, idx)
      if (at < 0) break
      matches.push({ from: pos + at, to: pos + at + query.length })
      idx = at + Math.max(1, query.length)
    }
  })
  return matches
}

function buildSearchDecos(doc, query, active) {
  const matches = collectSearchMatches(doc, query)
  if (!matches.length) {
    return { matches, deco: DecorationSet.empty, active: -1 }
  }
  let activeIdx = active
  if (activeIdx < 0 || activeIdx >= matches.length) activeIdx = 0
  const deco = DecorationSet.create(
    doc,
    matches.map((m, i) => {
      const isActive = i === activeIdx
      // 内联 style，避免 scoped CSS / 未重建静态包时看不见高亮
      return Decoration.inline(m.from, m.to, {
        class: isActive ? 'search-match search-match-active' : 'search-match',
        style: isActive
          ? 'background:#f59e0b;color:#1c1c21;border-radius:2px;padding:0 1px;box-shadow:0 0 0 1px #d97706'
          : 'background:#fff3bf;color:#1c1c21;border-radius:2px;padding:0 1px',
      })
    }),
  )
  return { matches, deco, active: activeIdx }
}

function createSearchHighlight() {
  return Extension.create({
    name: 'searchHighlight',
    addProseMirrorPlugins() {
      return [
        new Plugin({
          key: searchHighlightKey,
          state: {
            init: () => ({
              query: '',
              matches: [],
              active: -1,
              deco: DecorationSet.empty,
            }),
            apply(tr, prev, _old, state) {
              const meta = tr.getMeta(searchHighlightKey)
              let query = prev.query
              let active = prev.active
              if (meta) {
                if ('query' in meta) {
                  query = meta.query || ''
                  active = query ? (typeof meta.active === 'number' ? meta.active : 0) : -1
                } else if (meta.cycle && prev.matches.length) {
                  const dir = meta.direction || 1
                  active = (prev.active + dir + prev.matches.length) % prev.matches.length
                }
              }
              if (!query) {
                return { query: '', matches: [], active: -1, deco: DecorationSet.empty }
              }
              if (!meta && !tr.docChanged) return prev
              return { query, ...buildSearchDecos(state.doc, query, active) }
            },
          },
          props: {
            decorations(state) {
              return searchHighlightKey.getState(state)?.deco
            },
          },
          view() {
            return {
              update(view, prevState) {
                const cur = searchHighlightKey.getState(view.state)
                const prev = searchHighlightKey.getState(prevState)
                if (!cur?.matches?.length || cur.active < 0) return
                const m = cur.matches[cur.active]
                if (!m) return
                const unchanged =
                  prev &&
                  prev.query === cur.query &&
                  prev.active === cur.active &&
                  prev.matches[prev.active]?.from === m.from
                if (unchanged) return
                try {
                  const start = view.coordsAtPos(m.from)
                  const root = view.dom.closest('.note-content') || view.dom.parentElement
                  if (root && start) {
                    const top = start.top - root.getBoundingClientRect().top + root.scrollTop
                    root.scrollTo({ top: Math.max(0, top - 48), behavior: 'smooth' })
                  }
                } catch {
                  /* ignore */
                }
              },
            }
          },
        }),
      ]
    },
  })
}

const props = defineProps({
  noteId: { type: String, default: null },
  modelValue: { type: String, default: '' },
  editable: { type: Boolean, default: true },
})
const emit = defineEmits([
  'update:modelValue',
  'selection-change',
  'ready',
])

const toast = useToast()
const editor = ref(null)
const findOpen = ref(false)
const findQ = ref('')
const replaceQ = ref('')
const findCount = ref(0)
const fileInput = ref(null)
/** 点击正文图片放大预览 */
const lightboxSrc = ref('')
const lightboxAlt = ref('')
let applyingExternal = false

function openImageLightbox(src, alt = '') {
  if (!src) return
  lightboxSrc.value = src
  lightboxAlt.value = alt || '图片预览'
}

function closeImageLightbox() {
  lightboxSrc.value = ''
  lightboxAlt.value = ''
}

function onLightboxKeydown(e) {
  if (e.key === 'Escape' && lightboxSrc.value) {
    e.preventDefault()
    closeImageLightbox()
  }
}

/** 产品约定：有序/无序列表合计最多嵌套 10 级 */
const LIST_DEPTH_MAX = 10

function getListDepth(state) {
  const $from = state.selection.$from
  let depth = 0
  for (let d = $from.depth; d > 0; d--) {
    if ($from.node(d).type.name === 'listItem') depth++
  }
  return depth
}

function inListItem(state) {
  return getListDepth(state) > 0
}

/**
 * TipTap ListItem 默认 Tab = sinkListItem；失败时返回 false，浏览器会把焦点挪走。
 * 用更高 priority 覆盖：列表内 Tab 一律视为已处理。
 */
function createListTabGuard() {
  return Extension.create({
    name: 'listTabGuard',
    priority: 1000,
    addKeyboardShortcuts() {
      return {
        Tab: () => {
          const ed = this.editor
          if (!inListItem(ed.state)) return false
          const depth = getListDepth(ed.state)
          if (depth >= LIST_DEPTH_MAX) {
            toast.push('info', `列表最多支持 ${LIST_DEPTH_MAX} 级`)
            return true
          }
          ed.commands.sinkListItem('listItem')
          return true
        },
        'Shift-Tab': () => {
          const ed = this.editor
          if (!inListItem(ed.state)) return false
          ed.commands.liftListItem('listItem')
          return true
        },
      }
    },
  })
}

/** DOM 捕获兜底：即便扩展未吃掉 Tab，也不让焦点离开编辑区 */
function onEditorKeydownCapture(e) {
  if (e.key !== 'Tab' && e.code !== 'Tab') return
  const ed = editor.value
  if (!ed?.view?.dom) return
  if (!ed.view.dom.contains(e.target)) return
  if (!inListItem(ed.state)) return
  e.preventDefault()
  e.stopPropagation()
  if (e.shiftKey) {
    ed.commands.liftListItem('listItem')
    return
  }
  const depth = getListDepth(ed.state)
  if (depth >= LIST_DEPTH_MAX) {
    toast.push('info', `列表最多支持 ${LIST_DEPTH_MAX} 级`)
    return
  }
  ed.commands.sinkListItem('listItem')
}

const IMAGE_MIME_OK = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp'])

/** 仅允许 http(s)，避免 javascript: 等危险协议 */
function isSafeHttpUrl(href) {
  try {
    const u = new URL(href, window.location.origin)
    return u.protocol === 'http:' || u.protocol === 'https:'
  } catch {
    return false
  }
}

function openNoteLink(href) {
  if (!isSafeHttpUrl(href)) return false
  window.open(href, '_blank', 'noopener,noreferrer')
  return true
}

function extForImageFile(file) {
  const map = {
    'image/png': 'png',
    'image/jpeg': 'jpg',
    'image/gif': 'gif',
    'image/webp': 'webp',
  }
  if (file?.type && map[file.type]) return map[file.type]
  const name = file?.name || ''
  const m = name.match(/\.(png|jpe?g|gif|webp)$/i)
  return m ? m[1].toLowerCase().replace('jpeg', 'jpg') : 'png'
}

function collectImageFiles(dataTransfer) {
  if (!dataTransfer) return []
  const out = []
  if (dataTransfer.files?.length) {
    for (const f of dataTransfer.files) {
      if (f && IMAGE_MIME_OK.has(f.type)) out.push(f)
    }
  }
  if (!out.length && dataTransfer.items?.length) {
    for (const item of dataTransfer.items) {
      if (item.kind === 'file' && IMAGE_MIME_OK.has(item.type)) {
        const f = item.getAsFile()
        if (f) out.push(f)
      }
    }
  }
  return out
}

/** 上传并插入图片；截图粘贴常无文件名，按 MIME 补一个 */
async function insertImageFile(file) {
  if (!file || !editor.value) return false
  if (!IMAGE_MIME_OK.has(file.type)) {
    toast.push('warning', '仅支持 PNG / JPEG / GIF / WebP')
    return false
  }
  if (!props.noteId) {
    toast.push('warning', '请先打开一篇文档再插入图片')
    return false
  }
  const named =
    file.name && file.name.includes('.')
      ? file
      : new File([file], `paste.${extForImageFile(file)}`, { type: file.type || 'image/png' })
  try {
    const info = await notesApi.uploadAsset(props.noteId, named)
    const url = `/api/notes/${props.noteId}/assets/${info.filename}`
    editor.value.chain().focus().setImage({ src: url, alt: named.name || '图片' }).run()
    return true
  } catch {
    return false
  }
}

function buildEditor(html) {
  return new Editor({
    extensions: [
      StarterKit.configure({
        heading: { levels: [1, 2, 3, 4, 5, 6] },
      }),
      Underline,
      Link.configure({
        openOnClick: false,
        autolink: true,
        linkOnPaste: true,
        protocols: ['http', 'https'],
        HTMLAttributes: {
          class: 'note-link',
          rel: 'noopener noreferrer nofollow',
        },
        validate: (href) => /^https?:\/\//i.test(href || ''),
      }),
      Image.configure({ inline: false, allowBase64: false }),
      Placeholder.configure({ placeholder: '开始写点什么…' }),
      createListTabGuard(),
      createSearchHighlight(),
    ],
    content: html || '<p></p>',
    editable: props.editable,
    editorProps: {
      handlePaste(_view, event) {
        if (!props.editable) return false
        const files = collectImageFiles(event.clipboardData)
        if (!files.length) return false
        event.preventDefault()
        ;(async () => {
          for (const f of files) await insertImageFile(f)
        })()
        return true
      },
      handleDrop(_view, event) {
        if (!props.editable) return false
        const files = collectImageFiles(event.dataTransfer)
        if (!files.length) return false
        event.preventDefault()
        ;(async () => {
          for (const f of files) await insertImageFile(f)
        })()
        return true
      },
      handleDOMEvents: {
        click(_view, event) {
          const img = event.target?.closest?.('img')
          if (!img || !_view.dom.contains(img)) return false
          const src = img.currentSrc || img.getAttribute('src') || img.src
          if (!src) return false
          event.preventDefault()
          openImageLightbox(src, img.getAttribute('alt') || '')
          return true
        },
        // 单击不跳转（方便编辑）；双击新开页打开链接
        dblclick(view, event) {
          const a = event.target?.closest?.('a[href]')
          if (!a || !view.dom.contains(a)) return false
          const href = a.href || a.getAttribute('href') || ''
          if (!openNoteLink(href)) return false
          event.preventDefault()
          return true
        },
      },
    },
    onUpdate: ({ editor: ed }) => {
      if (applyingExternal) return
      const html = restoreAssetPaths(ed.getHTML(), props.noteId)
      emit('update:modelValue', htmlToMd(html))
    },
    onSelectionUpdate: ({ editor: ed }) => {
      const { from, to } = ed.state.selection
      const text = from === to ? '' : ed.state.doc.textBetween(from, to, '\n')
      let rect = null
      try {
        const sel = window.getSelection()
        if (sel && sel.rangeCount > 0 && !sel.isCollapsed) {
          rect = sel.getRangeAt(0).getBoundingClientRect()
        }
      } catch {
        /* ignore */
      }
      emit('selection-change', { text, rect, from, to })
    },
  })
}

function setMarkdown(md, { emitChange = false } = {}) {
  if (!editor.value) return
  applyingExternal = true
  const html = rewriteAssetUrls(mdToHtml(md), props.noteId)
  editor.value.commands.setContent(html, false)
  applyingExternal = false
  if (emitChange) emit('update:modelValue', md || '')
}

/** 流式应用：按块逐步写入，避免整页闪一下 */
async function streamApplyMarkdown(md, { chunk = 48, delayMs = 16 } = {}) {
  if (!editor.value) return
  const text = md || ''
  applyingExternal = true
  editor.value.commands.setContent('<p></p>', false)
  for (let i = 0; i < text.length; i += chunk) {
    const partial = text.slice(0, i + chunk)
    const html = rewriteAssetUrls(mdToHtml(partial), props.noteId)
    editor.value.commands.setContent(html, false)
    await new Promise((r) => setTimeout(r, delayMs))
  }
  applyingExternal = false
  emit('update:modelValue', text)
}

function getSelectedText() {
  if (!editor.value) return ''
  const { from, to } = editor.value.state.selection
  if (from === to) return ''
  return editor.value.state.doc.textBetween(from, to, '\n')
}

/** 光标/选区锚点：供 insert_at_cursor 定位（选区则插在选区之后） */
function getCursorAnchor() {
  if (!editor.value) return { before: '', after: '', from: 0, to: 0 }
  const { from, to } = editor.value.state.selection
  const insertAt = from === to ? from : to
  const doc = editor.value.state.doc
  const before = doc.textBetween(Math.max(0, insertAt - 240), insertAt, '\n')
  const after = doc.textBetween(insertAt, Math.min(doc.content.size, insertAt + 160), '\n')
  return { before, after, from, to, insertAt }
}

function applySearchHighlight(query, { cycle = false, select = false } = {}) {
  const ed = editor.value
  if (!ed) return { count: 0, active: -1 }
  const prev = searchHighlightKey.getState(ed.state)
  const tr = ed.state.tr
  if (!query) {
    tr.setMeta(searchHighlightKey, { query: '' })
    ed.view.dispatch(tr)
    return { count: 0, active: -1 }
  }
  if (cycle && prev?.query === query && prev.matches?.length) {
    tr.setMeta(searchHighlightKey, { cycle: true, direction: 1 })
  } else {
    tr.setMeta(searchHighlightKey, { query, active: 0 })
  }
  ed.view.dispatch(tr)
  const next = searchHighlightKey.getState(ed.state)
  const count = next?.matches?.length || 0
  const active = next?.active ?? -1
  // 仅在显式查找时选中，避免输入框失焦
  if (select && count > 0 && active >= 0) {
    const m = next.matches[active]
    ed.commands.setTextSelection({ from: m.from, to: m.to })
  }
  return { count, active }
}

function clearSearchHighlight() {
  applySearchHighlight('')
  findCount.value = 0
}

function toggleFindPanel() {
  findOpen.value = !findOpen.value
  if (!findOpen.value) clearSearchHighlight()
}

/** 输入时即时高亮全部匹配，不抢焦点 */
function onFindInput() {
  const q = findQ.value
  if (!q) {
    clearSearchHighlight()
    return
  }
  const { count } = applySearchHighlight(q, { cycle: false, select: false })
  findCount.value = count
}

function runFind() {
  const q = findQ.value
  if (!q || !editor.value) {
    clearSearchHighlight()
    return
  }
  const prev = searchHighlightKey.getState(editor.value.state)
  const cycle = prev?.query === q && (prev.matches?.length || 0) > 0
  const { count } = applySearchHighlight(q, { cycle, select: true })
  findCount.value = count
  if (count === 0) toast.push('info', '未找到匹配')
}

function replaceOne() {
  const q = findQ.value
  if (!q || !editor.value) return
  const { state } = editor.value
  const plain = state.doc.textContent || ''
  const at = plain.indexOf(q)
  if (at < 0) {
    toast.push('info', '未找到匹配')
    return
  }
  // 简化：在纯文本层替换后整篇重载（保格式尽力）
  const md = htmlToMd(restoreAssetPaths(editor.value.getHTML(), props.noteId))
  if (!md.includes(q)) {
    toast.push('info', '未找到匹配')
    return
  }
  const next = md.replace(q, replaceQ.value ?? '')
  setMarkdown(next, { emitChange: true })
  runFind()
}

function replaceAll() {
  const q = findQ.value
  if (!q || !editor.value) return
  const md = htmlToMd(restoreAssetPaths(editor.value.getHTML(), props.noteId))
  if (!md.includes(q)) {
    toast.push('info', '未找到匹配')
    return
  }
  let count = 0
  const next = md.split(q).reduce((acc, part, i, arr) => {
    if (i < arr.length - 1) count++
    return acc + part + (i < arr.length - 1 ? replaceQ.value ?? '' : '')
  }, '')
  setMarkdown(next, { emitChange: true })
  toast.push('success', `已替换 ${count} 处`)
  runFind()
}

async function onPickImage(e) {
  const file = e.target.files?.[0]
  e.target.value = ''
  if (!file) return
  await insertImageFile(file)
}

function setHeading(level) {
  editor.value?.chain().focus().toggleHeading({ level }).run()
}

watch(
  () => props.modelValue,
  (v) => {
    if (!editor.value || applyingExternal) return
    const cur = htmlToMd(restoreAssetPaths(editor.value.getHTML(), props.noteId))
    if ((v || '') === (cur || '')) return
    setMarkdown(v || '')
  },
)

watch(
  () => props.editable,
  (v) => editor.value?.setEditable(!!v),
)

watch(
  () => props.noteId,
  () => {
    if (editor.value) setMarkdown(props.modelValue || '')
  },
)

onMounted(() => {
  editor.value = buildEditor(rewriteAssetUrls(mdToHtml(props.modelValue), props.noteId))
  window.addEventListener('keydown', onLightboxKeydown)
  emit('ready', { setMarkdown, streamApplyMarkdown, getSelectedText })
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onLightboxKeydown)
  closeImageLightbox()
  editor.value?.destroy()
  editor.value = null
})

defineExpose({ setMarkdown, streamApplyMarkdown, getSelectedText, getCursorAnchor, editor })
</script>

<template>
  <div class="note-editor" @keydown.capture="onEditorKeydownCapture">
    <div class="note-toolbar" role="toolbar" aria-label="格式">
      <button type="button" class="tb" title="标题1" @click="setHeading(1)">H1</button>
      <button type="button" class="tb" title="标题2" @click="setHeading(2)">H2</button>
      <button type="button" class="tb" title="标题3" @click="setHeading(3)">H3</button>
      <span class="tb-sep" />
      <button type="button" class="tb" title="加粗" @click="editor?.chain().focus().toggleBold().run()">
        <i class="ti ti-bold" />
      </button>
      <button type="button" class="tb" title="斜体" @click="editor?.chain().focus().toggleItalic().run()">
        <i class="ti ti-italic" />
      </button>
      <button
        type="button"
        class="tb"
        title="下划线"
        @click="editor?.chain().focus().toggleUnderline().run()"
      >
        <i class="ti ti-underline" />
      </button>
      <span class="tb-sep" />
      <button
        type="button"
        class="tb"
        title="无序列表"
        @click="editor?.chain().focus().toggleBulletList().run()"
      >
        <i class="ti ti-list" />
      </button>
      <button
        type="button"
        class="tb"
        title="有序列表"
        @click="editor?.chain().focus().toggleOrderedList().run()"
      >
        <i class="ti ti-list-numbers" />
      </button>
      <span class="tb-sep" />
      <button type="button" class="tb" title="插入图片" @click="fileInput?.click()">
        <i class="ti ti-photo" />
      </button>
      <button type="button" class="tb" title="查找替换" @click="toggleFindPanel">
        <i class="ti ti-search" />
      </button>
      <input
        ref="fileInput"
        type="file"
        accept="image/png,image/jpeg,image/gif,image/webp"
        class="hidden"
        @change="onPickImage"
      />
    </div>
    <div v-if="findOpen" class="note-find">
      <input
        v-model="findQ"
        class="nf-input"
        placeholder="查找"
        @keyup.enter="runFind"
        @input="onFindInput"
      />
      <input v-model="replaceQ" class="nf-input" placeholder="替换为" />
      <button type="button" class="btn-sm" @click="runFind">查找</button>
      <button type="button" class="btn-sm" @click="replaceOne">替换</button>
      <button type="button" class="btn-sm" @click="replaceAll">全部替换</button>
      <span class="nf-count">{{ findCount }} 处</span>
    </div>
    <EditorContent :editor="editor" class="note-content ProseMirror" />

    <Teleport to="body">
      <div
        v-if="lightboxSrc"
        class="note-img-lightbox"
        role="dialog"
        aria-modal="true"
        aria-label="图片预览"
        @click.self="closeImageLightbox"
      >
        <button
          type="button"
          class="note-img-lightbox-close"
          title="关闭"
          aria-label="关闭"
          @click="closeImageLightbox"
        >
          <i class="ti ti-x" />
        </button>
        <img :src="lightboxSrc" :alt="lightboxAlt" class="note-img-lightbox-img" @click.stop />
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.note-editor {
  display: flex;
  flex-direction: column;
  min-height: 0;
  flex: 1;
}
.note-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-1);
  padding: var(--sp-2) 0;
  border-bottom: 1px solid var(--bd);
}
.tb {
  border: 1px solid var(--bd);
  background: var(--surface);
  color: var(--fg);
  border-radius: var(--radius-xs);
  padding: 4px 8px;
  cursor: pointer;
  font-size: var(--fs-sm);
}
.tb:hover {
  background: var(--surface-2);
}
.tb-sep {
  width: 1px;
  background: var(--bd);
  margin: 0 4px;
}
.note-find {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  align-items: center;
  padding: var(--sp-2) 0;
  border-bottom: 1px solid var(--bd);
}
.nf-input {
  min-width: 120px;
  flex: 1;
  max-width: 200px;
  padding: 6px 8px;
  border: 1px solid var(--bd);
  border-radius: var(--radius-xs);
  background: var(--surface);
  color: var(--fg);
  font-size: var(--fs-sm);
}
.nf-count {
  font-size: var(--fs-sm);
  color: var(--muted);
}
.note-content {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: var(--sp-4) 0;
}
.note-content :deep(.ProseMirror) {
  outline: none;
  min-height: 280px;
  font-size: var(--fs-lg);
  line-height: 1.65;
  color: var(--fg);
}
.note-content :deep(.ProseMirror h1) {
  font-size: var(--fs-3xl);
}
.note-content :deep(.ProseMirror h2) {
  font-size: var(--fs-2xl);
}
.note-content :deep(.ProseMirror h3) {
  font-size: var(--fs-xl);
}
.note-content :deep(.ProseMirror ul),
.note-content :deep(.ProseMirror ol) {
  padding-left: 1.4em;
}
.note-content :deep(.ProseMirror img) {
  max-width: 100%;
  border-radius: var(--radius-sm);
  cursor: zoom-in;
}
.note-content :deep(.ProseMirror a.note-link),
.note-content :deep(.ProseMirror a[href]) {
  color: var(--acctx, var(--brand-tx, #3b5bdb));
  text-decoration: underline;
  text-underline-offset: 2px;
  cursor: pointer;
  word-break: break-all;
}
.note-content :deep(.ProseMirror a.note-link:hover),
.note-content :deep(.ProseMirror a[href]:hover) {
  color: var(--brand-solid, #3b6ef6);
}
.note-content :deep(.search-match) {
  background: var(--warnbg);
  color: var(--warntx);
  border-radius: 2px;
  box-decoration-break: clone;
}
.note-content :deep(.search-match-active) {
  background: color-mix(in srgb, var(--warn) 42%, var(--warnbg));
  outline: 1.5px solid var(--warn);
  color: var(--fg);
}
.hidden {
  display: none;
}
</style>

<style>
.note-img-lightbox {
  position: fixed;
  inset: 0;
  z-index: 1200;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 48px 24px 24px;
  background: rgba(0, 0, 0, 0.72);
  cursor: zoom-out;
}
.note-img-lightbox-img {
  max-width: min(96vw, 1400px);
  max-height: calc(100vh - 72px);
  object-fit: contain;
  border-radius: 8px;
  box-shadow: 0 12px 48px rgba(0, 0, 0, 0.45);
  cursor: default;
}
.note-img-lightbox-close {
  position: fixed;
  top: 16px;
  right: 16px;
  width: 36px;
  height: 36px;
  border: none;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.14);
  color: #fff;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
}
.note-img-lightbox-close:hover {
  background: rgba(255, 255, 255, 0.28);
}
</style>
