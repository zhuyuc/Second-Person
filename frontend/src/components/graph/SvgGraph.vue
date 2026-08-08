<script setup>
// SVG 引擎（v3.0 §7.1）：节点数 ≤500 使用。三态焦点 + 拖动 + 缩放平移，渐变/光晕/描边标签。
import { ref, computed } from 'vue'
import { THEME, edgeWidth, domainColor } from './graphTheme'
import { useDraggableNode } from './composables/useDraggableNode'

const props = defineProps({
  nodes: { type: Array, default: () => [] },   // {entity_id,name,memory_count,type,x,y,r}
  edges: { type: Array, default: () => [] },
  focus: { type: Object, required: true },
})
const emit = defineEmits(['node-click'])

const W = 640, H = 440
const svgEl = ref(null)
// 后端画布尺寸随节点数动态扩容，viewBox 按节点包围盒自适应（保持 W:H 宽高比）
function fitBox() {
  if (!props.nodes.length) return { x: 0, y: 0, w: W, h: H }
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity
  for (const n of props.nodes) {
    x0 = Math.min(x0, n.x); x1 = Math.max(x1, n.x)
    y0 = Math.min(y0, n.y); y1 = Math.max(y1, n.y)
  }
  const pad = 60
  let w = Math.max(W, x1 - x0 + pad * 2)
  let h = Math.max(H, y1 - y0 + pad * 2)
  if (w / h > W / H) h = w * (H / W)
  else w = h * (W / H)
  return { x: (x0 + x1) / 2 - w / 2, y: (y0 + y1) / 2 - h / 2, w, h }
}
// 缩放平移：viewBox（初始即适配内容；邻居扩展新增节点不重置视口，避免跳跃）
const vb = ref(fitBox())
const viewBox = computed(() => `${vb.value.x} ${vb.value.y} ${vb.value.w} ${vb.value.h}`)

const posMap = computed(() => Object.fromEntries(props.nodes.map(p => [p.entity_id, p])))
const edgeMaxWeight = computed(() => Math.max(1, ...props.edges.map(e => e.weight || 1)))

function getSvgPoint(evt) {
  const svg = svgEl.value
  if (!svg || !svg.getScreenCTM) return { x: 0, y: 0 }
  const pt = svg.createSVGPoint()
  pt.x = evt.clientX; pt.y = evt.clientY
  const p = pt.matrixTransform(svg.getScreenCTM().inverse())
  return { x: p.x, y: p.y }
}

const { onMouseDown } = useDraggableNode(
  ref(props.nodes), props.focus, getSvgPoint,
  (node) => {
    // 点击：切换 Pinned 焦点并向上派发（打开抽屉 + 邻居扩展）
    props.focus.pinnedId.value = node.entity_id
    emit('node-click', node.entity_id)
  })

function onNodeEnter(node) {
  if (props.focus.draggingId.value) return
  props.focus.hoveredId.value = node.entity_id
}
function onNodeLeave() {
  if (props.focus.draggingId.value) return
  props.focus.hoveredId.value = null
}

// 边曲线（轻微弯曲，减少穿心重叠）
function edgePath(e) {
  const s = posMap.value[e.source], t = posMap.value[e.target]
  if (!s || !t) return ''
  const mx = (s.x + t.x) / 2, my = (s.y + t.y) / 2
  const dx = t.x - s.x, dy = t.y - s.y
  const cx = mx - dy * 0.14, cy = my + dx * 0.14
  return `M${s.x},${s.y} Q${cx},${cy} ${t.x},${t.y}`
}

// ---- 节点/边样式（三态）----
// 降噪版 + 领域配色：填充用低饱和 domain 色（无 domain 回退中性），
// 焦点仍靠品牌色描边环 + 尺寸放大表达
function nodeFill(node) {
  return domainColor(node.domain) || THEME.node.fill
}
function nodeStroke(node) {
  return props.focus.nodeState(node.entity_id) === 'focused'
    ? THEME.node.focusedStroke : THEME.node.stroke
}
function nodeStrokeW(node) {
  return props.focus.nodeState(node.entity_id) === 'focused'
    ? THEME.node.focusedStrokeWidth : THEME.node.strokeWidth
}
function nodeOpacity(node) {
  return props.focus.nodeState(node.entity_id) === 'dimmed' ? THEME.node.dimmedOpacity : 1
}
function nodeR(node) {
  const st = props.focus.nodeState(node.entity_id)
  let r = node.r
  if (st === 'focused') r *= THEME.size.focusedMultiplier
  if (props.focus.draggingId.value === node.entity_id) r *= THEME.size.draggingMultiplier
  return r
}
function labelVisible(node) {
  const st = props.focus.nodeState(node.entity_id)
  if (st === 'dimmed') return false
  if (st === 'focused' || st === 'neighbor') return true
  return node.r >= 4   // idle：小节点不显示标签
}
function labelStyle(node) {
  const st = props.focus.nodeState(node.entity_id)
  if (st === 'focused') return { fontSize: THEME.label.focusedFontSize + 'px', fontWeight: 600, fill: 'var(--fg)' }
  if (st === 'neighbor') return { fontSize: '12px', fontWeight: 500, fill: 'var(--fg)' }
  return { fontSize: THEME.label.fontSize + 'px', fontWeight: 400 }
}
function edgeStroke(e) {
  const st = props.focus.edgeState(e)
  return st === 'active' ? THEME.edge.activeColor
    : st === 'neighbor' ? THEME.edge.neighborColor
      : st === 'dimmed' ? THEME.edge.dimmedColor : THEME.edge.idleColor
}
function edgeDash(e) {
  // 焦点激活边实线，其余虚线（参考图风格，虚实对比突出选中关系）
  return props.focus.edgeState(e) === 'active' ? 'none' : THEME.edge.dash
}
function edgeSW(e) {
  const w = edgeWidth(e.weight, edgeMaxWeight.value)
  return props.focus.edgeState(e) === 'active' ? w * THEME.size.activeEdgeMultiplier : w
}

// ---- 缩放 / 平移 ----
function onWheel(evt) {
  evt.preventDefault()
  const p = getSvgPoint(evt)
  const factor = evt.deltaY > 0 ? THEME.zoomStep : 1 / THEME.zoomStep
  const base = fitBox().w   // 缩放限幅基于内容尺寸，而非固定画布
  const nw = Math.max(base * 0.05, Math.min(base * 4, vb.value.w * factor))
  const nh = nw * (H / W)
  // 以鼠标为中心缩放
  vb.value = {
    x: p.x - (p.x - vb.value.x) * (nw / vb.value.w),
    y: p.y - (p.y - vb.value.y) * (nh / vb.value.h),
    w: nw, h: nh,
  }
}
let panning = false
let panStart = null
function onBgDown(evt) {
  panning = true
  panStart = { mx: evt.clientX, my: evt.clientY, vx: vb.value.x, vy: vb.value.y }
  window.addEventListener('mousemove', onBgMove)
  window.addEventListener('mouseup', onBgUp, { once: true })
}
function onBgMove(evt) {
  if (!panning) return
  const scale = vb.value.w / (svgEl.value?.clientWidth || W)
  vb.value = {
    ...vb.value,
    x: panStart.vx - (evt.clientX - panStart.mx) * scale,
    y: panStart.vy - (evt.clientY - panStart.my) * scale,
  }
}
function onBgUp() { panning = false; window.removeEventListener('mousemove', onBgMove) }
function onBgClick() { props.focus.clearPinned() }   // 空白单击取消焦点
function resetView() { vb.value = fitBox() }
defineExpose({ resetView })
</script>

<template>
  <svg ref="svgEl" class="kg-svg" :viewBox="viewBox" preserveAspectRatio="xMidYMid meet"
    @wheel="onWheel" @mousedown.self="onBgDown" @click.self="onBgClick" @dblclick.self="resetView">
    <!-- 边：stroke 用 inline style 绑定（优先级高于任何 CSS 规则，避免三态颜色被样式表覆盖） -->
    <path v-for="(e, i) in edges" :key="'e' + i" class="kg-edge" :d="edgePath(e)"
      :style="{ stroke: edgeStroke(e), strokeWidth: edgeSW(e) + 'px', strokeDasharray: edgeDash(e) }" />
    <!-- 节点：中性底 + 三态描边（焦点品牌色环） -->
    <g v-for="node in nodes" :key="node.entity_id" class="kg-node"
      :style="{ opacity: nodeOpacity(node) }"
      @mousedown="onMouseDown($event, node)" @mouseenter="onNodeEnter(node)" @mouseleave="onNodeLeave">
      <circle :cx="node.x" :cy="node.y" :r="nodeR(node)" :fill="nodeFill(node)"
        :stroke="nodeStroke(node)" :stroke-width="nodeStrokeW(node)" />
      <text v-if="nodeR(node) >= 8" class="kg-count" :x="node.x" :y="node.y" text-anchor="middle" dy="0.35em">{{ node.memory_count }}</text>
      <text v-if="labelVisible(node)" class="kg-label" :x="node.x" :y="node.y + nodeR(node) + 12"
        text-anchor="middle" :style="labelStyle(node)">{{ node.name }}</text>
    </g>
  </svg>
</template>
