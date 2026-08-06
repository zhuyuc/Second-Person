<script setup>
// FlowChartSVG —— 高质量 SVG 流程图（T06-T09）
// 三种节点：process(矩形) / decision(菱形) / terminal(胶囊)
// 贝塞尔连线 + 箭头 + 悬停高亮 + 点击 + 拖拽 + 复制/下载

import { ref, computed, onMounted, onUnmounted } from 'vue'
import dagre from 'dagre'
import { useToast } from '@/stores/toast'

const toast = useToast()

const props = defineProps({
  nodes: { type: Array, required: true },
  edges: { type: Array, required: true },
})
const emit = defineEmits(['node-click'])

// ---- dagre 布局（LLM 不提供坐标时自动计算） ----
const layoutNodes = computed(() => {
  const ns = props.nodes
  if (!ns.length) return []
  // 如果所有节点都有有效坐标，直接用（LLM 模式 / 历史兼容）
  const hasCoords = ns.every(n => typeof n.x === 'number' && typeof n.y === 'number')
  if (hasCoords) return ns

  // dagre 分层布局
  const g = new dagre.graphlib.Graph()
  g.setGraph({ rankdir: 'TB', nodesep: 80, ranksep: 100, edgesep: 40, marginx: 60, marginy: 40 })
  g.setDefaultEdgeLabel(() => ({}))
  ns.forEach(n => g.setNode(n.id, { width: 180, height: 48 }))
  props.edges.forEach(e => g.setEdge(e.from, e.to))
  dagre.layout(g)

  const laidOut = ns.map(n => {
    const node = g.node(n.id)
    if (!node) return { ...n, x: 400, y: 60 }
    return { ...n, x: Math.round(node.x), y: Math.round(node.y - 24) }
  })

  // M4: Post-layout symmetry pass for decision nodes with exactly 2 successors
  for (const n of laidOut) {
    if (n.type !== 'decision') continue
    const outEdges = props.edges.filter(e => e.from === n.id)
    if (outEdges.length !== 2) continue
    const targets = outEdges.map(e => laidOut.find(ln => ln.id === e.to)).filter(Boolean)
    if (targets.length !== 2) continue
    const offset = Math.max(Math.abs(targets[0].x - n.x), Math.abs(targets[1].x - n.x), 80)
    targets[0].x = Math.round(n.x - offset)
    targets[1].x = Math.round(n.x + offset)
  }
  return laidOut
})

// ---- 布局常量 ----
const CANVAS_W = 800

// 画布高度：最大节点 y + 底部留白
const svgHeight = computed(() => {
  if (!layoutNodes.value.length) return 200
  const maxY = Math.max(...layoutNodes.value.map(n => n.y || 0), 0)
  return Math.max(200, maxY + 120)
})

// 节点查找表
const nodeMap = computed(() => {
  const m = {}
  for (const n of layoutNodes.value) m[n.id] = n
  return m
})

// ---- 节点形状渲染 ----
function nodeShape(n) {
  const w = 160, h = 48
  const x = n.x - w / 2, y = n.y
  switch (n.type) {
    case 'decision':
      // 菱形：四点
      return {
        points: `${n.x},${y - 14} ${x + w + 14},${y + h / 2} ${n.x},${y + h + 14} ${x - 14},${y + h / 2}`,
        isDiamond: true,
        textY: y + h / 2 + 6,
      }
    case 'terminal':
      return {
        rect: { x: x - 4, y, w: w + 8, h, rx: 23 },
        textY: y + h / 2 + 6,
      }
    default: // process
      return {
        rect: { x, y, w, h, rx: 6 },
        textY: y + h / 2 + 6,
      }
  }
}

function nodeFill(n) {
  if (n.type === 'decision') return 'var(--diagram-decision-fill)'
  if (n.type === 'terminal') return 'url(#terminalGrad)'
  return 'var(--diagram-process-fill)'
}

function nodeStroke(n) {
  if (n.type === 'decision') return 'var(--diagram-decision-stroke)'
  return 'var(--diagram-process-stroke)'
}

function nodeTextFill(n) {
  return n.type === 'terminal' ? 'var(--diagram-terminal-text)' : 'var(--fg)'
}

// ---- 节点锚点（连线用）----
const NH = 48  // 节点高度
const EDGE_R = 12  // 圆角半径

function nodeAnchors(n) {
  const w = 160, h = NH
  return {
    top:    { x: n.x, y: n.y },
    bottom: { x: n.x, y: n.y + h },
    left:   { x: n.x - w / 2, y: n.y + h / 2 },
    right:  { x: n.x + w / 2, y: n.y + h / 2 },
    cx: n.x, cy: n.y + h / 2,
  }
}

// ---- 回边识别（dagre rank 判定）----
const edgeRanks = computed(() => {
  const ranks = {}
  layoutNodes.value.forEach((n, i) => { ranks[n.id] = i })
  const result = {}
  props.edges.forEach(e => {
    const fromRank = layoutNodes.value.findIndex(ln => ln.id === e.from)
    const toRank = layoutNodes.value.findIndex(ln => ln.id === e.to)
    result[`${e.from}|${e.to}`] = fromRank >= 0 && toRank >= 0 && toRank <= fromRank
  })
  return result
})

function isBackEdge(e) {
  return edgeRanks.value[`${e.from}|${e.to}`] || false
}

// 回边侧向通道 lane x（最左节点左侧 80px，多条回边依次外扩 24px）
const backLaneX = computed(() => {
  const minX = Math.min(...layoutNodes.value.map(n => n.x - 80), 400) - 80
  return minX < 60 ? 60 : minX
})

let backEdgeCount = 0
function nextBackLane() {
  backEdgeCount++
  return backLaneX.value - (backEdgeCount - 1) * 24
}

function backEdgePath(fromNode, toNode) {
  const from = nodeAnchors(fromNode)
  const to = nodeAnchors(toNode)
  const r = EDGE_R
  const lx = nextBackLane()
  const sx = from.left.x, sy = from.cy
  const tx = to.left.x, ty = to.cy
  if (sy <= ty) {
    return `M${sx},${sy} L${lx + r},${sy} Q${lx},${sy} ${lx},${sy - r} L${lx},${ty + r} Q${lx},${ty} ${lx + r},${ty} L${tx},${ty}`
  }
  // source below target: go up via lane
  return `M${sx},${sy} L${lx + r},${sy} Q${lx},${sy} ${lx},${sy - r} L${lx},${ty + r} Q${lx},${ty} ${lx + r},${ty} L${tx},${ty}`
}

// 预计算所有连线路径（含回边）
const edgePaths = computed(() => {
  backEdgeCount = 0
  const paths = {}
  props.edges.forEach(e => {
    const key = e.from + '|' + e.to
    const f = nodeMap.value[e.from]
    const t = nodeMap.value[e.to]
    if (!f || !t) { paths[key] = ''; return }
    paths[key] = isBackEdge(e) ? backEdgePath(f, t) : edgePath(f, t)
  })
  return paths
})

function getEdgePath(e) {
  return edgePaths.value[e.from + '|' + e.to] || ''
}

// ---- 连线路径（正交折线 + 圆角）----
function edgePath(fromNode, toNode) {
  if (!fromNode || !toNode) return ''
  const from = nodeAnchors(fromNode)
  const to = nodeAnchors(toNode)
  const dx = to.cx - from.cx

  // 垂直连线（|dx| < 8）：直线
  if (Math.abs(dx) < 8) {
    return `M${from.bottom.x},${from.bottom.y} L${to.top.x},${to.top.y}`
  }

  // 斜向连线：正交折线 + 圆角
  const sx = dx > 0 ? from.right.x : from.left.x   // 源侧边锚点
  const sy = from.right.y                            // 源中心 y
  const tx = to.top.x
  const ty = to.top.y
  const r = EDGE_R
  const dir = dx > 0 ? -r : r                        // 转弯方向

  // 横线 → Q 圆角转弯 → 竖线
  return `M${sx},${sy} L${tx + dir},${sy} Q${tx},${sy} ${tx},${sy + r} L${tx},${ty}`
}

// 连线标签位置
function edgeLabelPos(e) {
  const f = nodeMap.value[e.from]
  const t = nodeMap.value[e.to]
  if (!f || !t) return { x: 0, y: 0, show: false }
  const from = nodeAnchors(f), to = nodeAnchors(t)
  const dx = to.cx - from.cx
  if (Math.abs(dx) < 8) {
    // 垂直连线：标签在右侧
    return { x: from.bottom.x + 16, y: (from.bottom.y + to.top.y) / 2 + 4, show: true }
  }
  // 斜向连线：标签在水平段中点
  const sx = dx > 0 ? from.right.x : from.left.x
  const tx = to.top.x
  return { x: (sx + tx) / 2, y: from.right.y - 8, show: true }
}

// ---- 交互状态 ----
const hoveredId = ref(null)
const focusedEdges = computed(() => {
  if (!hoveredId.value) return new Set()
  const s = new Set()
  for (const e of props.edges) {
    if (e.from === hoveredId.value || e.to === hoveredId.value) {
      s.add(e.from + '|' + e.to)
    }
  }
  return s
})

function isEdgeFocused(e) {
  return focusedEdges.value.has(e.from + '|' + e.to)
}

function onNodeEnter(node) {
  if (dragging.value) return
  hoveredId.value = node.id
}
function onNodeLeave() {
  if (dragging.value) return
  hoveredId.value = null
}
function onNodeClick(node) {
  emit('node-click', node.id)
}

// ---- 拖拽 ----
const dragging = ref(false)
const dragNode = ref(null)
const dragOffset = ref({ x: 0, y: 0 })

function onDragStart(e, node) {
  dragging.value = true
  dragNode.value = node
  dragOffset.value = { x: e.clientX - node.x, y: e.clientY - node.y }
  e.preventDefault()
}

function onDragMove(e) {
  if (!dragging.value || !dragNode.value) return
  dragNode.value.x = Math.round(e.clientX - dragOffset.value.x)
  dragNode.value.y = Math.round(e.clientY - dragOffset.value.y)
}

function onDragEnd() {
  dragging.value = false
  dragNode.value = null
}

onMounted(() => window.addEventListener('mousemove', onDragMove))
onMounted(() => window.addEventListener('mouseup', onDragEnd))
onUnmounted(() => window.removeEventListener('mousemove', onDragMove))
onUnmounted(() => window.removeEventListener('mouseup', onDragEnd))

// ---- 导出功能 ----
const svgRef = ref(null)

function getSvgString() {
  if (!svgRef.value) return ''
  return new XMLSerializer().serializeToString(svgRef.value)
}

function copySvg() {
  const s = getSvgString()
  if (!s) { toast.push('error', 'SVG 不可用'); return }
  navigator.clipboard.writeText(s)
  toast.push('success', 'SVG 源码已复制')
}

function downloadSvg() {
  const s = getSvgString()
  if (!s) return
  const blob = new Blob([s], { type: 'image/svg+xml;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url; a.download = 'flowchart.svg'; a.click()
  URL.revokeObjectURL(url)
  toast.push('success', 'SVG 已下载')
}

function downloadPng() {
  const svg = svgRef.value
  if (!svg) { toast.push('error', 'SVG 不可用'); return }
  try {
    const svgData = new XMLSerializer().serializeToString(svg)
    const svgBlob = new Blob([svgData], { type: 'image/svg+xml;charset=utf-8' })
    const url = URL.createObjectURL(svgBlob)
    const img = new Image()
    img.onload = () => {
      const canvas = document.createElement('canvas')
      canvas.width = svg.clientWidth * 2 || CANVAS_W * 2
      canvas.height = (svg.clientHeight || 400) * 2
      const ctx = canvas.getContext('2d')
      ctx.scale(2, 2)
      ctx.drawImage(img, 0, 0)
      URL.revokeObjectURL(url)
      canvas.toBlob((blob) => {
        const a = document.createElement('a')
        a.href = URL.createObjectURL(blob)
        a.download = 'flowchart.png'; a.click()
        toast.push('success', 'PNG 已下载')
      }, 'image/png')
    }
    img.src = url
  } catch { toast.push('error', '导出 PNG 失败') }
}
</script>

<template>
  <div class="fc-wrap">
    <!-- 操作按钮 -->
    <div class="fc-actions">
      <button class="fc-btn" title="复制 SVG 源码" @click="copySvg"><i class="ti ti-copy"></i> SVG</button>
      <button class="fc-btn" title="下载 SVG" @click="downloadSvg"><i class="ti ti-download"></i> SVG</button>
      <button class="fc-btn" title="下载 PNG" @click="downloadPng"><i class="ti ti-photo"></i> PNG</button>
    </div>

    <svg ref="svgRef" class="fc-svg" :viewBox="`0 0 ${CANVAS_W} ${svgHeight}`"
      preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg">

      <defs>
        <!-- 终端渐变 -->
        <linearGradient id="terminalGrad" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%" stop-color="#3b6ef6" />
          <stop offset="100%" stop-color="#7b5cff" />
        </linearGradient>
        <!-- 箭头标记 -->
        <marker id="fcArrow" markerWidth="8" markerHeight="6" refX="7.5" refY="3" orient="auto">
          <polygon points="0 0, 8 3, 0 6" fill="var(--diagram-edge-arrow)" />
        </marker>
      </defs>

      <!-- 连线层 -->
      <g v-for="e in edges" :key="'e_' + e.from + '_' + e.to">
        <path :class="['fc-edge', { focused: isEdgeFocused(e), 'fc-back-edge': isBackEdge(e) }]"
          :d="getEdgePath(e)"
          fill="none"
          stroke="var(--diagram-edge-stroke)"
          stroke-width="1.6"
          marker-end="url(#fcArrow)" />
        <rect v-if="e.label && edgeLabelPos(e).show"
          :x="edgeLabelPos(e).x - 14" :y="edgeLabelPos(e).y - 11"
          width="28" height="14" rx="4"
          fill="var(--surface)" opacity="0.85" />
        <text v-if="e.label && edgeLabelPos(e).show"
          :x="edgeLabelPos(e).x" :y="edgeLabelPos(e).y"
          class="fc-edge-label">{{ e.label }}</text>
      </g>

      <!-- 节点层 -->
      <g v-for="n in layoutNodes" :key="'n_' + n.id" class="fc-node"
        :class="{ focused: hoveredId === n.id }"
        @mouseenter="onNodeEnter(n)" @mouseleave="onNodeLeave"
        @click="onNodeClick(n)" @mousedown="onDragStart($event, n)">

        <!-- process / terminal：rect -->
        <rect v-if="nodeShape(n).rect"
          :x="nodeShape(n).rect.x" :y="nodeShape(n).rect.y"
          :width="nodeShape(n).rect.w" :height="nodeShape(n).rect.h"
          :rx="nodeShape(n).rect.rx"
          :fill="nodeFill(n)" :stroke="nodeStroke(n)" stroke-width="1.5"
          filter="url(#nodeShadow)" />

        <!-- decision：polygon -->
        <polygon v-if="nodeShape(n).points"
          :points="nodeShape(n).points"
          :fill="nodeFill(n)" :stroke="nodeStroke(n)" stroke-width="1.5"
          filter="url(#nodeShadow)" />

        <!-- 标签 -->
        <text :x="n.x" :y="nodeShape(n).textY"
          text-anchor="middle" class="fc-label"
          :fill="nodeTextFill(n)">{{ n.label }}</text>
      </g>
    </svg>
  </div>
</template>

<style scoped>
.fc-wrap {
  position: relative;
  margin: 12px 0;
  border: 1px solid var(--bd);
  border-radius: var(--radius-sm);
  overflow: hidden;
  background: var(--surface);
}
.fc-svg {
  display: block;
  width: 100%;
  height: auto;
}
/* 操作按钮 */
.fc-actions {
  position: absolute;
  top: 8px;
  right: 8px;
  display: flex;
  gap: 4px;
  z-index: 5;
  opacity: 0;
  transition: opacity var(--dur-fast);
}
.fc-wrap:hover .fc-actions {
  opacity: 1;
}
.fc-btn {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 2px 8px;
  font-size: var(--fs-xs);
  border: 1px solid var(--bd);
  border-radius: var(--radius-xs);
  background: var(--surface);
  color: var(--sec);
  cursor: pointer;
  white-space: nowrap;
}
.fc-btn:hover {
  background: var(--surface-2);
  color: var(--fg);
}
/* 节点 */
.fc-node {
  cursor: pointer;
  transition: filter var(--dur-fast);
}
.fc-node.focused {
  filter: drop-shadow(0 2px 8px rgba(124, 92, 255, .35));
}
.fc-node.focused rect,
.fc-node.focused polygon {
  stroke: var(--diagram-focus-stroke) !important;
  stroke-width: 2.5 !important;
}
/* 连线 */
.fc-edge {
  transition: stroke var(--dur-fast), stroke-width var(--dur-fast);
}
.fc-back-edge {
  stroke-dasharray: 6 4;
  opacity: 0.7;
}
.fc-edge.focused {
  stroke: var(--diagram-focus-stroke) !important;
  stroke-width: 2.8 !important;
}
/* 连线标签 */
.fc-edge-label {
  font-size: var(--fs-xs);
  fill: var(--muted);
  paint-order: stroke;
  stroke: var(--surface);
  stroke-width: 3;
}
/* 节点标签 */
.fc-label {
  font-size: var(--fs-sm);
  font-weight: 500;
  pointer-events: none;
  user-select: none;
}
/* 阴影滤镜 */
.fc-node > rect,
.fc-node > polygon {
  filter: drop-shadow(0 1px 2px rgba(0,0,0,.08));
}
</style>
