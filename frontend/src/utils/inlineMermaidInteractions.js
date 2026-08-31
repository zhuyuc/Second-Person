const MIN_SCALE = 0.2
const MAX_SCALE = 4
const ZOOM_STEP = 1.12

const controllers = new Map()

function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max)
}

function applyTransform(state) {
  state.svg.style.transform = `translate(${state.x}px, ${state.y}px) scale(${state.scale})`
  state.svg.style.transformOrigin = '0 0'
  state.svg.style.willChange = 'transform'
}

function zoomAt(state, pointX, pointY, factor) {
  const nextScale = clamp(state.scale * factor, MIN_SCALE, MAX_SCALE)
  if (nextScale === state.scale) return

  const contentX = (pointX - state.x) / state.scale
  const contentY = (pointY - state.y) / state.scale
  state.x = pointX - contentX * nextScale
  state.y = pointY - contentY * nextScale
  state.scale = nextScale
  applyTransform(state)
}

function reset(state) {
  state.x = 0
  state.y = 0
  state.scale = 1
  applyTransform(state)
}

function destroy(wrapper, state) {
  for (const [event, listener, options] of state.listeners) {
    state.viewport.removeEventListener(event, listener, options)
  }
  controllers.delete(wrapper)
}

function cleanupDetached(root) {
  for (const [wrapper, state] of controllers) {
    const detached = typeof wrapper.isConnected === 'boolean' && !wrapper.isConnected
    if (detached || (root?.contains && !root.contains(wrapper))) destroy(wrapper, state)
  }
}

function bindWrapper(wrapper) {
  const existing = controllers.get(wrapper)
  if (existing) return existing

  const viewport = wrapper.querySelector('.mermaid')
  const svg = viewport?.querySelector('svg')
  if (!viewport || !svg) return null

  const state = {
    viewport,
    svg,
    x: 0,
    y: 0,
    scale: 1,
    dragging: false,
    lastX: 0,
    lastY: 0,
    pointerId: null,
    listeners: [],
  }

  const onWheel = (event) => {
    event.preventDefault()
    const rect = viewport.getBoundingClientRect()
    zoomAt(
      state,
      event.clientX - rect.left,
      event.clientY - rect.top,
      event.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP
    )
  }
  const onPointerDown = (event) => {
    if (event.button !== 0) return
    event.preventDefault()
    state.dragging = true
    state.pointerId = event.pointerId
    state.lastX = event.clientX
    state.lastY = event.clientY
    viewport.classList.add('mermaid-panning')
    viewport.setPointerCapture?.(event.pointerId)
  }
  const onPointerMove = (event) => {
    if (!state.dragging || event.pointerId !== state.pointerId) return
    state.x += event.clientX - state.lastX
    state.y += event.clientY - state.lastY
    state.lastX = event.clientX
    state.lastY = event.clientY
    applyTransform(state)
  }
  const stopDragging = (event) => {
    if (!state.dragging || event.pointerId !== state.pointerId) return
    state.dragging = false
    state.pointerId = null
    viewport.classList.remove('mermaid-panning')
    viewport.releasePointerCapture?.(event.pointerId)
  }
  const onDoubleClick = (event) => {
    event.preventDefault()
    reset(state)
  }

  for (const [event, listener, options] of [
    ['wheel', onWheel, { passive: false }],
    ['pointerdown', onPointerDown],
    ['pointermove', onPointerMove],
    ['pointerup', stopDragging],
    ['pointercancel', stopDragging],
    ['dblclick', onDoubleClick],
  ]) {
    viewport.addEventListener(event, listener, options)
    state.listeners.push([event, listener, options])
  }
  applyTransform(state)
  controllers.set(wrapper, state)
  return state
}

export function bindInlineMermaidInteractions(root) {
  if (!root?.querySelectorAll) return
  cleanupDetached(root)

  for (const wrapper of root.querySelectorAll('.mermaid-wrap')) {
    bindWrapper(wrapper)
  }
}

export function zoomInlineMermaid(wrapper, factor) {
  // 按钮点击是最后一道兜底：异步渲染后的 SVG 即使错过首次扫描也能立即接管。
  const state = bindWrapper(wrapper)
  if (!state) return false
  const rect = state.viewport.getBoundingClientRect()
  zoomAt(state, rect.width / 2, rect.height / 2, factor)
  return true
}

export function resetInlineMermaid(wrapper) {
  const state = bindWrapper(wrapper)
  if (!state) return false
  reset(state)
  return true
}

export function cleanupInlineMermaidInteractions(root) {
  for (const [wrapper, state] of controllers) {
    if (!root || !root.contains || root.contains(wrapper)) destroy(wrapper, state)
  }
}
