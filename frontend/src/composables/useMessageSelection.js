// 消息气泡文字选中检测：只在 .chat-message-item 内的 .content / .bubble
// 触发；对外暴露 { visible, text, rect, sourceMsgId, sourceRole, hide } 供
// SelectionActionBar 消费。滚动/resize/点击外部/清空选区都自动收起。
import { ref, onMounted, onUnmounted } from 'vue'

const MIN_SELECTION_CHARS = 1

// options.getRoot?: () => HTMLElement | null
//   限定选区只在该根容器内触发。多实例（主对话 + 侧边会话）并存时，各自
//   传入自己的滚动容器根，避免同一次选区被两个实例同时捕获、弹出两条浮条。
//   不传则全局生效（向后兼容）。
export function useMessageSelection(options = {}) {
  const visible = ref(false)
  const text = ref('')
  const rect = ref(null)
  const sourceMsgId = ref(null)
  const sourceRole = ref(null)

  function hide() {
    visible.value = false
    text.value = ''
    rect.value = null
    sourceMsgId.value = null
    sourceRole.value = null
  }

  // 排除法：消息项内的文字默认都可选，只有落在这些交互控件上才放弃。
  // 好处是新增内容块（思考面板、Web 来源、时间线 lane、以后的新面板）无需登记，
  // 自动生效；只需维护这一份「交互件」排除表。
  const EXCLUDE_SELECTOR =
    'button, a, input, textarea, [contenteditable="true"], ' +
    '.msg-actions-row, .version-nav, .banner, [data-selection-actionbar], [data-no-select]'

  function pickAnchor(node) {
    // Selection.anchorNode 通常是文本节点；向上找到所属的消息项
    const el = node?.nodeType === 3 ? node.parentElement : node
    const item = el?.closest?.('.chat-message-item')
    if (!item) return null
    // 落在交互控件里的选区不弹操作条
    if (el?.closest?.(EXCLUDE_SELECTOR)) return null
    // 多实例隔离：若指定了根容器，选区必须落在本实例根内才算命中
    const root = options.getRoot?.()
    if (root && !root.contains(item)) return null
    return item
  }

  function evaluate() {
    const sel = window.getSelection?.()
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return hide()
    const raw = sel.toString()
    const trimmed = raw.trim()
    if (trimmed.length < MIN_SELECTION_CHARS) return hide()
    const item = pickAnchor(sel.anchorNode) || pickAnchor(sel.focusNode)
    if (!item) return hide()
    const range = sel.getRangeAt(0)
    const box = range.getBoundingClientRect()
    // 极端情况：只选了不可见字符 → getBoundingClientRect 全 0，跳过
    if (!box || (box.width === 0 && box.height === 0)) return hide()
    text.value = trimmed
    rect.value = {
      top: box.top,
      bottom: box.bottom,
      left: box.left,
      right: box.right,
      width: box.width,
      height: box.height,
    }
    sourceMsgId.value = item?.dataset?.msgId ?? null
    // role 按结构判定：选区落在 .msg-user 内即用户消息，否则助手消息
    const el = sel.anchorNode?.nodeType === 3 ? sel.anchorNode.parentElement : sel.anchorNode
    sourceRole.value = el?.closest?.('.msg-user') ? 'user' : 'assistant'
    visible.value = true
  }

  // mouseup / touchend 触发一次判定；用 setTimeout 让浏览器先把 selection
  // 更新到 window.getSelection()（Chrome/Safari 都需要这一拍）
  function onPointerUp() {
    setTimeout(evaluate, 0)
  }

  // 选区被键盘/鼠标继续调整时，实时收起旧 toolbar（新选区结束后 pointerup 再重开）
  function onSelectionChange() {
    if (!visible.value) return
    const sel = window.getSelection?.()
    if (!sel || sel.isCollapsed || !sel.toString().trim()) hide()
  }

  // 点击 toolbar 之外的地方 → 收起。注意 toolbar 自身要标记 data-selection-actionbar
  function onDocPointerDown(e) {
    if (!visible.value) return
    if (e.target?.closest?.('[data-selection-actionbar]')) return
    hide()
  }

  // 消息容器滚动或视窗尺寸变化 → 坐标失效，直接收起
  function onScrollOrResize() {
    if (visible.value) hide()
  }

  onMounted(() => {
    document.addEventListener('mouseup', onPointerUp)
    document.addEventListener('touchend', onPointerUp)
    document.addEventListener('selectionchange', onSelectionChange)
    document.addEventListener('pointerdown', onDocPointerDown, true)
    window.addEventListener('resize', onScrollOrResize)
    // 消息列表在 .main 里滚动，用 capture 兜住所有滚动来源
    window.addEventListener('scroll', onScrollOrResize, true)
  })
  onUnmounted(() => {
    document.removeEventListener('mouseup', onPointerUp)
    document.removeEventListener('touchend', onPointerUp)
    document.removeEventListener('selectionchange', onSelectionChange)
    document.removeEventListener('pointerdown', onDocPointerDown, true)
    window.removeEventListener('resize', onScrollOrResize)
    window.removeEventListener('scroll', onScrollOrResize, true)
  })

  return { visible, text, rect, sourceMsgId, sourceRole, hide }
}
