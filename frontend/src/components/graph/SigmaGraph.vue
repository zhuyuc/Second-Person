<script setup>
// Sigma.js WebGL 引擎（v3.0 §7.2）：节点数 >500 使用。nodeReducer/edgeReducer 三态样式 + 官方拖动。
import { ref, watch, onMounted, onBeforeUnmount } from 'vue'
import Graph from 'graphology'
import Sigma from 'sigma'
import { THEME, sigmaPalette, domainColor } from './graphTheme'

const props = defineProps({
  nodes: { type: Array, default: () => [] },
  edges: { type: Array, default: () => [] },
  focus: { type: Object, required: true },
})
const emit = defineEmits(['node-click'])

const container = ref(null)
let renderer = null
let graph = null
let dragging = null
const PAL = sigmaPalette()   // WebGL 不支持 CSS 变量，按当前主题取 resolved 色板

function buildGraph() {
  graph = new Graph()
  for (const n of props.nodes) {
    if (graph.hasNode(n.entity_id)) continue
    graph.addNode(n.entity_id, {
      x: n.x || 0, y: -(n.y || 0),           // Sigma y 向上，取负对齐 SVG 方向
      size: Math.max(4, (n.r || 12) / 2),
      label: `${n.name}（${n.memory_count}）`,
      color: domainColor(n.domain) || PAL.node,
      baseColor: domainColor(n.domain) || PAL.node, entity_id: n.entity_id,
    })
  }
  for (const e of props.edges) {
    if (!graph.hasNode(e.source) || !graph.hasNode(e.target)) continue
    const key = `${e.source}__${e.target}`
    if (graph.hasEdge(key)) continue
    try { graph.addEdgeWithKey(key, e.source, e.target, { size: 1 + (e.weight || 1), weight: e.weight || 1 }) } catch { /* 忽略重复 */ }
  }
}

function nodeReducer(node, data) {
  const st = props.focus.nodeState(node)
  const res = { ...data }
  if (st === 'focused') {
    res.color = PAL.focused
    res.size = data.size * THEME.size.focusedMultiplier
    res.zIndex = 2; res.forceLabel = true
  } else if (st === 'neighbor') {
    res.color = data.baseColor || PAL.node; res.forceLabel = true; res.zIndex = 1
  } else if (st === 'dimmed') {
    res.color = PAL.nodeDimmed; res.label = ''; res.zIndex = 0
  }
  return res
}
function edgeReducer(edge, data) {
  const st = props.focus.edgeState({
    source: graph.source(edge), target: graph.target(edge),
  })
  const res = { ...data }
  if (st === 'active') { res.color = PAL.focused; res.size = data.size * THEME.size.activeEdgeMultiplier; res.zIndex = 1 }
  else if (st === 'neighbor') { res.color = PAL.edgeNeighbor }
  else if (st === 'dimmed') { res.color = PAL.edgeDimmed; res.hidden = false }
  else res.color = PAL.edgeIdle
  return res
}

function mount() {
  if (!container.value) return
  buildGraph()
  renderer = new Sigma(graph, container.value, {
    nodeReducer, edgeReducer,
    enableEdgeEvents: false,
    labelColor: { color: PAL.label },
    defaultEdgeColor: PAL.edgeIdle,
    hideEdgesOnMove: true, hideLabelsOnMove: true,
  })
  renderer.on('enterNode', ({ node }) => { if (!dragging) props.focus.hoveredId.value = node })
  renderer.on('leaveNode', () => { if (!dragging) props.focus.hoveredId.value = null })
  renderer.on('clickNode', ({ node }) => { props.focus.pinnedId.value = node; emit('node-click', node) })
  renderer.on('clickStage', () => props.focus.clearPinned())
  // 拖动
  renderer.on('downNode', (e) => {
    dragging = e.node; props.focus.draggingId.value = e.node
  })
  const captor = renderer.getMouseCaptor()
  captor.on('mousemovebody', (e) => {
    if (!dragging) return
    const pos = renderer.viewportToGraph(e)
    graph.setNodeAttribute(dragging, 'x', pos.x)
    graph.setNodeAttribute(dragging, 'y', pos.y)
    e.preventSigmaDefault()
    e.original.preventDefault(); e.original.stopPropagation()
  })
  const stopDrag = () => { dragging = null; props.focus.draggingId.value = null }
  captor.on('mouseup', stopDrag)
  captor.on('mouseupbody', stopDrag)
}

// 焦点变化时重绘（reducer 每帧计算，无需重载数据）
watch(() => props.focus.focusedId.value, () => { if (renderer) renderer.refresh() })
// 数据变化时重建
watch(() => [props.nodes, props.edges], () => {
  if (!renderer) return
  buildGraph(); renderer.setGraph(graph); renderer.refresh()
}, { deep: false })

onMounted(mount)
onBeforeUnmount(() => { if (renderer) { renderer.kill(); renderer = null } })

function resetView() { if (renderer) renderer.getCamera().animatedReset() }
defineExpose({ resetView })
</script>

<template>
  <div ref="container" class="kg-sigma"></div>
</template>

<style scoped>
.kg-sigma {
  width: 100%;
  height: 100%;
  min-height: 440px;
}
</style>
