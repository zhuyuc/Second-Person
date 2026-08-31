<script setup>
// Canvas 2D 统一渲染引擎：力仿真 + 惯性缩放平移 + 语义缩放 + 光晕微动效 + 进场动画
import { ref, watch, onMounted, onBeforeUnmount, onActivated, onDeactivated } from 'vue'
import { THEME, domainColor } from './graphTheme'
import { useForceSimulation } from './composables/useForceSimulation'
import { useCanvasInteraction } from './composables/useCanvasInteraction'

const props = defineProps({
  nodes: { type: Array, default: () => [] },
  edges: { type: Array, default: () => [] },
  focus: { type: Object, required: true },
  searchTarget: { type: String, default: null },
})
const emit = defineEmits(['node-click'])

const canvasRef = ref(null)
let ctx = null
let dpr = 1
let animFrame = 0
let startTime = 0

// 初始化标记
let initialized = false

// 解析 CSS 变量为 Canvas 可用色值
let palette = {}
function resolvePalette() {
  const el = canvasRef.value
  if (!el) return
  const s = getComputedStyle(el)
  palette = {
    surface: s.getPropertyValue('--surface').trim() || '#1a1a1c',
    surface3: s.getPropertyValue('--surface-3').trim() || '#2e2e30',
    fg: s.getPropertyValue('--fg').trim() || '#c9d1d9',
    sec: s.getPropertyValue('--sec').trim() || '#a0a0a5',
    muted: s.getPropertyValue('--muted').trim() || '#6e6e73',
    bd: s.getPropertyValue('--bd').trim() || '#242426',
    bdStrong: s.getPropertyValue('--bd-strong').trim() || '#38383b',
    accent: '#7c5cff',
    accentAlpha: 'rgba(124, 92, 255, 0.6)',
    accentGlow: 'rgba(124, 92, 255, 0.15)',
  }
}

// ---- 力仿真 ----
const simulation = useForceSimulation(
  {
    get value() {
      return props.nodes
    },
  },
  {
    get value() {
      return props.edges
    },
  }
)

// ---- 交互控制 ----
const interaction = useCanvasInteraction(canvasRef, {
  getNodes: () => props.nodes,
  onNodeHover(node) {
    if (props.focus.draggingId.value) return
    props.focus.hoveredId.value = node ? node.entity_id : null
    markDirty()
  },
  onNodeClick(node) {
    props.focus.pinnedId.value = node.entity_id
    emit('node-click', node.entity_id)
  },
  onNodeDragStart(node) {
    props.focus.draggingId.value = node.entity_id
    simulation.fixNode(node.entity_id, node.x, node.y)
    simulation.reheat(0.3)
  },
  onNodeDrag(node) {
    simulation.fixNode(node.entity_id, node.x, node.y)
    markDirty()
  },
  onNodeDragEnd(node, moved) {
    props.focus.draggingId.value = null
    simulation.releaseNode(node.entity_id)
    if (moved) simulation.reheat(0.4)
  },
  onBgClick() {
    props.focus.clearPinned()
  },
  onCameraChange() {
    markDirty()
  },
})

// ---- 脏标记渲染 ----
let dirty = true
function markDirty() {
  dirty = true
}

// ---- 进场动画状态 ----
const nodeAlpha = new Map()
const nodeAnimR = new Map()

function initEntryAnimation() {
  const now = performance.now()
  for (let i = 0; i < props.nodes.length; i++) {
    const n = props.nodes[i]
    if (!nodeAlpha.has(n.entity_id)) {
      nodeAlpha.set(n.entity_id, { start: now + i * 8, duration: 400 })
      nodeAnimR.set(n.entity_id, { start: now + i * 8, duration: 500, from: 0, to: n.r })
    }
  }
}

function getNodeAlpha(id, now) {
  const a = nodeAlpha.get(id)
  if (!a) return 1
  const t = Math.min(1, Math.max(0, (now - a.start) / a.duration))
  return t * t // ease-in
}

function getNodeAnimR(id, now, baseR) {
  const a = nodeAnimR.get(id)
  if (!a) return baseR
  const t = Math.min(1, Math.max(0, (now - a.start) / a.duration))
  const ease = 1 - (1 - t) * (1 - t) // ease-out
  return a.from + (baseR - a.from) * ease
}

// ---- 语义缩放 ----
function labelVisible(node, zoom, focusState) {
  if (focusState === 'focused' || focusState === 'neighbor') return true
  if (focusState === 'dimmed') return false
  if (zoom < 0.3) return false
  if (zoom < 0.7) return node.r >= 15
  if (zoom > 1.5) return true
  return node.r >= 4
}

// ---- 绘制 ----
function draw(now) {
  const canvas = canvasRef.value
  if (!canvas || !ctx) return

  const w = canvas.clientWidth,
    h = canvas.clientHeight
  if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
    canvas.width = w * dpr
    canvas.height = h * dpr
  }

  const cam = interaction.camera
  const zoom = cam.zoom

  ctx.save()
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.clearRect(0, 0, w, h)

  // 应用相机变换
  ctx.translate(w / 2, h / 2)
  ctx.scale(zoom, zoom)
  ctx.translate(cam.x, cam.y)

  const nodeMap = Object.fromEntries(props.nodes.map((n) => [n.entity_id, n]))
  const focusedId = props.focus.focusedId.value
  const hasFocus = !!focusedId

  // ---- 绘制边 ----
  // 按状态分组绘制减少 state change
  const edgeGroups = { dimmed: [], idle: [], neighbor: [], active: [] }
  for (const e of props.edges) {
    const st = props.focus.edgeState(e)
    ;(edgeGroups[st] || edgeGroups.idle).push(e)
  }

  for (const [state, group] of Object.entries(edgeGroups)) {
    if (!group.length) continue
    ctx.beginPath()
    ctx.strokeStyle =
      state === 'active'
        ? palette.accentAlpha
        : state === 'neighbor'
          ? palette.muted
          : state === 'dimmed'
            ? palette.bd
            : palette.bdStrong
    ctx.lineWidth = state === 'active' ? 2.5 / zoom : 1.2 / zoom
    ctx.globalAlpha = state === 'dimmed' && hasFocus ? 0.25 : 1
    if (state !== 'active') ctx.setLineDash([4 / zoom, 4 / zoom])
    else ctx.setLineDash([])

    for (const e of group) {
      const s = nodeMap[e.source],
        t = nodeMap[e.target]
      if (!s || !t) continue
      const mx = (s.x + t.x) / 2,
        my = (s.y + t.y) / 2
      const dx = t.x - s.x,
        dy = t.y - s.y
      const cx = mx - dy * 0.12,
        cy = my + dx * 0.12
      ctx.moveTo(s.x, s.y)
      ctx.quadraticCurveTo(cx, cy, t.x, t.y)
    }
    ctx.stroke()
    ctx.globalAlpha = 1
    ctx.setLineDash([])
  }

  // ---- 绘制节点 ----
  // 排序：dimmed 最底 → idle → neighbor → focused 最上
  const stateOrder = { dimmed: 0, idle: 1, neighbor: 2, focused: 3 }
  const sorted = [...props.nodes].sort((a, b) => {
    const sa = stateOrder[props.focus.nodeState(a.entity_id)] || 0
    const sb = stateOrder[props.focus.nodeState(b.entity_id)] || 0
    return sa - sb
  })

  for (const node of sorted) {
    const st = props.focus.nodeState(node.entity_id)
    const alpha = getNodeAlpha(node.entity_id, now)
    let r = getNodeAnimR(node.entity_id, now, node.r)
    if (alpha < 1) {
      dirty = true
    }
    if (st === 'focused') r *= THEME.size.focusedMultiplier
    if (props.focus.draggingId.value === node.entity_id) r *= THEME.size.draggingMultiplier

    ctx.globalAlpha = st === 'dimmed' && hasFocus ? THEME.node.dimmedOpacity * alpha : alpha

    // 焦点光晕
    if (st === 'focused') {
      const grad = ctx.createRadialGradient(node.x, node.y, r, node.x, node.y, r * 2.5)
      grad.addColorStop(0, palette.accentGlow)
      grad.addColorStop(1, 'rgba(124, 92, 255, 0)')
      ctx.fillStyle = grad
      ctx.beginPath()
      ctx.arc(node.x, node.y, r * 2.5, 0, Math.PI * 2)
      ctx.fill()
    }

    // 节点阴影（大节点）
    if (r > 12 && st !== 'dimmed') {
      ctx.shadowColor = 'rgba(0,0,0,0.2)'
      ctx.shadowBlur = 6 / zoom
      ctx.shadowOffsetY = 2 / zoom
    }

    // 节点圆
    const fill = domainColor(node.domain) || palette.surface3
    ctx.fillStyle = fill
    ctx.beginPath()
    ctx.arc(node.x, node.y, r, 0, Math.PI * 2)
    ctx.fill()

    // 清除阴影
    ctx.shadowColor = 'transparent'
    ctx.shadowBlur = 0
    ctx.shadowOffsetY = 0

    // 描边
    ctx.strokeStyle = st === 'focused' ? palette.accent : palette.bdStrong
    ctx.lineWidth = st === 'focused' ? 2.5 / zoom : 1.2 / zoom
    ctx.stroke()

    // 节点内计数
    if (r >= 8) {
      ctx.fillStyle = palette.surface
      ctx.font = `600 ${Math.max(9, Math.min(13, r * 0.8))}px system-ui, sans-serif`
      ctx.textAlign = 'center'
      ctx.textBaseline = 'middle'
      ctx.fillText(String(node.memory_count), node.x, node.y)
    }

    // 标签
    if (labelVisible(node, zoom, st)) {
      ctx.globalAlpha = st === 'dimmed' && hasFocus ? THEME.node.dimmedOpacity * alpha : alpha
      const fontSize = st === 'focused' ? 13.5 : st === 'neighbor' ? 12 : 11.5
      const fontWeight = st === 'focused' ? 700 : st === 'neighbor' ? 500 : 400
      ctx.font = `${fontWeight} ${fontSize / zoom}px system-ui, sans-serif`
      ctx.textAlign = 'center'
      ctx.textBaseline = 'top'
      // 文字描边（halo）提高可读性
      ctx.strokeStyle = palette.surface
      ctx.lineWidth = 3 / zoom
      ctx.lineJoin = 'round'
      const ly = node.y + r + 4 / zoom
      ctx.strokeText(node.name, node.x, ly)
      ctx.fillStyle = st === 'focused' ? palette.fg : palette.sec
      ctx.fillText(node.name, node.x, ly)
    }

    ctx.globalAlpha = 1
  }

  ctx.restore()
}

// 搜索定位脉冲
let pulseNode = null
let pulseStart = 0
function drawPulse(now) {
  if (!pulseNode || !ctx) return
  const elapsed = now - pulseStart
  if (elapsed > 1500) {
    pulseNode = null
    return
  }
  const canvas = canvasRef.value
  if (!canvas) return
  const cam = interaction.camera
  const w = canvas.clientWidth,
    h = canvas.clientHeight

  ctx.save()
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.translate(w / 2, h / 2)
  ctx.scale(cam.zoom, cam.zoom)
  ctx.translate(cam.x, cam.y)

  // 两次脉冲
  const phase = (elapsed % 750) / 750
  const r = pulseNode.r * (1.5 + phase * 2)
  const alpha = 1 - phase
  ctx.strokeStyle = palette.accent
  ctx.lineWidth = 2 / cam.zoom
  ctx.globalAlpha = alpha * 0.6
  ctx.beginPath()
  ctx.arc(pulseNode.x, pulseNode.y, r, 0, Math.PI * 2)
  ctx.stroke()
  ctx.globalAlpha = 1
  ctx.restore()
  dirty = true
}

// ---- 主循环 ----
let lastW = 0,
  lastH = 0
function loop(now) {
  if (!startTime) startTime = now

  // 力仿真 tick
  if (simulation.running.value) {
    simulation.tick()
    interaction.rebuildGrid()
    dirty = true
  }

  // 容器尺寸变化检测（ResizeObserver 之外的兜底）
  const canvas = canvasRef.value
  if (canvas) {
    const cw = canvas.clientWidth,
      ch = canvas.clientHeight
    if (cw !== lastW || ch !== lastH) {
      lastW = cw
      lastH = ch
      dirty = true
    }
  }

  if (dirty) {
    draw(now)
    if (pulseNode) drawPulse(now)
    dirty = false
  }

  animFrame = requestAnimationFrame(loop)
}

// ---- 生命周期 ----
function init() {
  const canvas = canvasRef.value
  if (!canvas) return
  dpr = window.devicePixelRatio || 1
  ctx = canvas.getContext('2d')
  resolvePalette()
  interaction.bindEvents()

  // 初始进场动画
  if (props.nodes.length && !initialized) {
    initialized = true
    initEntryAnimation()
    interaction.rebuildGrid()
    interaction.fitAll(false)
    simulation.coldStart()
  }

  animFrame = requestAnimationFrame(loop)
}

function cleanup() {
  interaction.unbindEvents()
  if (animFrame) cancelAnimationFrame(animFrame)
  simulation.freeze()
}

// 数据变化
watch(
  () => props.nodes.length,
  (newLen, oldLen) => {
    if (newLen > 0 && !initialized) {
      initialized = true
      initEntryAnimation()
      interaction.fitAll(false)
      simulation.coldStart()
    } else if (newLen > (oldLen || 0)) {
      // 新增节点（邻居扩展）
      const now = performance.now()
      for (const n of props.nodes) {
        if (!nodeAlpha.has(n.entity_id)) {
          nodeAlpha.set(n.entity_id, { start: now, duration: 400 })
          nodeAnimR.set(n.entity_id, { start: now, duration: 500, from: 0, to: n.r })
        }
      }
      simulation.expand()
    }
    interaction.rebuildGrid()
    simulation.onDataChange()
    markDirty()
  },
  { flush: 'post' }
)

watch(
  () => props.edges.length,
  () => {
    simulation.onDataChange()
    markDirty()
  }
)

// 焦点变化
watch(
  () => props.focus.focusedId.value,
  () => {
    markDirty()
  }
)
watch(
  () => props.focus.draggingId.value,
  () => {
    markDirty()
  }
)

// 搜索定位
watch(
  () => props.searchTarget,
  (id) => {
    if (!id) return
    const node = props.nodes.find((n) => n.entity_id === id)
    if (!node) return
    interaction.flyTo(node)
    pulseNode = node
    pulseStart = performance.now()
    markDirty()
  }
)

// 主题切换监听
let mql = null
let resizeObs = null
let mutObs = null
function onThemeChange() {
  resolvePalette()
  markDirty()
}

onMounted(() => {
  init()
  mql = window.matchMedia?.('(prefers-color-scheme: dark)')
  mql?.addEventListener?.('change', onThemeChange)
  mutObs = new MutationObserver(() => {
    resolvePalette()
    markDirty()
  })
  mutObs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
  // 容器尺寸变化 → 重绘 + 重新 fitAll
  if (canvasRef.value) {
    resizeObs = new ResizeObserver(() => {
      markDirty()
    })
    resizeObs.observe(canvasRef.value)
  }
})

onBeforeUnmount(() => {
  cleanup()
  mql?.removeEventListener?.('change', onThemeChange)
  resizeObs?.disconnect()
  mutObs?.disconnect()
})

// keep-alive 激活/停用：离开 MemoryView 时组件 deactivated 但不 unmount，
// RAF 会持续空转 60fps 浪费 CPU/GPU。停用时清理，激活时重启。
let deactivatedCleaned = false
onDeactivated(() => {
  if (animFrame) cancelAnimationFrame(animFrame)
  animFrame = null
  simulation.freeze()
  deactivatedCleaned = true
})
onActivated(() => {
  if (deactivatedCleaned && canvasRef.value) {
    deactivatedCleaned = false
    simulation.coldStart()
    markDirty()
    animFrame = requestAnimationFrame(loop)
  }
})

// 暴露方法
function resetView() {
  interaction.fitAll(true)
}
function flyToNode(id) {
  const node = props.nodes.find((n) => n.entity_id === id)
  if (node) {
    interaction.flyTo(node)
    pulseNode = node
    pulseStart = performance.now()
    markDirty()
  }
}
defineExpose({ resetView, flyToNode })
</script>

<template>
  <canvas ref="canvasRef" class="kg-canvas"></canvas>
</template>

<style scoped>
.kg-canvas {
  width: 100%;
  height: 100%;
  display: block;
  border-radius: inherit;
}
</style>
