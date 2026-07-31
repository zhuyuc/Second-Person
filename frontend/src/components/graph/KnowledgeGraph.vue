<script setup>
// 知识图谱主容器（v3.0 §6）：双引擎自动切换 + 邻居扩展 + 搜索定位 + 详情抽屉 + Esc。
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { api } from '@/api/client'
import { THEME, nodeRadius, domainColor } from './graphTheme'
import { domainLabel, loadDomainLabels } from '@/utils/domainLabel'
import { useGraphFocus } from './composables/useGraphFocus'
import SvgGraph from './SvgGraph.vue'
import SigmaGraph from './SigmaGraph.vue'
import NodeDetailDrawer from './NodeDetailDrawer.vue'

const emit = defineEmits(['open-memory'])

const W = 640, H = 440
const nodes = ref([])
const edges = ref([])
const loading = ref(false)
const focus = useGraphFocus(edges)

// 单向升级：一旦 >阈值 切 Sigma，之后不降回 SVG
const upgraded = ref(false)
const engine = computed(() => {
  if (nodes.value.length > THEME.engineThreshold) upgraded.value = true
  return upgraded.value ? SigmaGraph : SvgGraph
})

// 详情抽屉
const selectedEntity = ref(null)
const entityMemories = ref([])

// 领域图例弹窗（图例默认单行收纳，点击查看全部）
const showLegendModal = ref(false)

function withCoords(list) {
  const maxCount = Math.max(1, ...list.map(x => x.memory_count || 0))
  // 包围盒：后端画布随节点数动态扩容，无坐标节点的外环兜底需跟随实际范围
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity, has = false
  for (const n of list) if (n.x != null && n.y != null) {
    has = true
    x0 = Math.min(x0, n.x); x1 = Math.max(x1, n.x)
    y0 = Math.min(y0, n.y); y1 = Math.max(y1, n.y)
  }
  const cx = has ? (x0 + x1) / 2 : W / 2
  const cy = has ? (y0 + y1) / 2 : H / 2
  const R = has ? Math.max(x1 - x0, y1 - y0) / 2 + 60 : Math.min(W, H) / 2 - 90
  return list.map((n, i) => {
    let x = n.x, y = n.y
    if (x == null || y == null) {
      const a = (i / Math.max(1, list.length)) * Math.PI * 2 - Math.PI / 2
      const r0 = list.length <= 1 ? 0 : R
      x = cx + r0 * Math.cos(a); y = cy + r0 * Math.sin(a)
    }
    return { ...n, x, y, r: nodeRadius(n.memory_count, maxCount) }
  })
}

async function loadGraph() {
  loading.value = true
  try {
    const d = await api.get('/memory/graph')
    nodes.value = withCoords(d.nodes || [])
    edges.value = d.edges || []
  } finally { loading.value = false }
}

// 合并邻居扩展结果（新节点/新边去重）
function mergeGraph(center, neighbors, newEdges) {
  const existing = new Set(nodes.value.map(n => n.entity_id))
  const maxCount = Math.max(1, ...nodes.value.map(x => x.memory_count || 0),
    ...neighbors.map(x => x.memory_count || 0))
  const cnode = nodes.value.find(n => n.entity_id === center.entity_id)
  const cx = cnode ? cnode.x : W / 2, cy = cnode ? cnode.y : H / 2
  const added = []
  for (const nb of neighbors) {
    if (existing.has(nb.entity_id)) continue
    if (nodes.value.length + added.length >= THEME.maxNodes) break
    let x = nb.x, y = nb.y
    if (x == null || y == null) {   // 新实体无坐标：中心附近随机偏移
      x = cx + (Math.random() - 0.5) * 120
      y = cy + (Math.random() - 0.5) * 120
    }
    added.push({ ...nb, x, y, r: nodeRadius(nb.memory_count, maxCount) })
    existing.add(nb.entity_id)
  }
  if (added.length) nodes.value = [...nodes.value, ...added]
  const ekey = new Set(edges.value.map(e => e.source + '|' + e.target))
  const addE = []
  for (const e of newEdges) {
    const k = e.source + '|' + e.target
    if (!ekey.has(k) && existing.has(e.source) && existing.has(e.target)) {
      addE.push(e); ekey.add(k)
    }
  }
  if (addE.length) edges.value = [...edges.value, ...addE]
  return added.length
}

async function expand(entityId) {
  if (nodes.value.length >= THEME.maxNodes) {
    return
  }
  const exclude = nodes.value.map(n => n.entity_id).join(',')
  const d = await api.get(`/memory/graph/entity/${entityId}/neighbors?limit=30&exclude_ids=${encodeURIComponent(exclude)}`)
  if (d && d.center) mergeGraph(d.center, d.neighbors || [], d.edges || [])
}

async function onNodeClick(entityId) {
  // 打开抽屉 + 加载关联记忆
  selectedEntity.value = nodes.value.find(n => n.entity_id === entityId) || { entity_id: entityId }
  entityMemories.value = await api.get('/memory/graph/entity/' + entityId + '/memories')
  // 后台扩展邻居
  expand(entityId)
}

// Pinned 清空时联动关闭抽屉
watch(() => focus.pinnedId.value, (v) => {
  if (!v) { selectedEntity.value = null; entityMemories.value = [] }
})

function onEsc(e) {
  if (e.key !== 'Escape') return
  if (showLegendModal.value) { showLegendModal.value = false; return }
  if (selectedEntity.value) { selectedEntity.value = null; entityMemories.value = []; focus.clearPinned() }
  else focus.clearPinned()
}

// 图例：当前图中出现的 domain 及其颜色（按实体数降序）
const domainLegend = computed(() => {
  const cnt = {}
  for (const n of nodes.value) {
    if (n.domain) cnt[n.domain] = (cnt[n.domain] || 0) + 1
  }
  return Object.entries(cnt).sort((a, b) => b[1] - a[1])
    .map(([d, c]) => ({ domain: d, count: c, color: domainColor(d) }))
})

onMounted(() => { loadGraph(); loadDomainLabels(); window.addEventListener('keydown', onEsc) })
onBeforeUnmount(() => window.removeEventListener('keydown', onEsc))
defineExpose({ loadGraph })
</script>

<template>
  <div>
    <div v-if="loading" class="empty"><i class="ti ti-loader-2"></i>图谱加载中…</div>
    <div v-else-if="!nodes.length" class="empty"><i class="ti ti-affiliate"></i>还没有记忆关联，先创建几条关联记忆吧</div>
    <div v-else class="kg-wrap">
      <component :is="engine" :nodes="nodes" :edges="edges" :focus="focus" @node-click="onNodeClick" />
    </div>
    <div class="muted" style="margin-top:8px">
      节点=实体（大小与内部数字=关联记忆数，颜色=领域），连线=共现关系（粗细=共现条数）。
      <span v-if="upgraded">· 已切换 WebGL 引擎</span>
    </div>
    <!-- 领域图例：默认单行收纳（超出裁切），点击弹窗查看全部 -->
    <div v-if="domainLegend.length" class="kg-legend" title="点击查看全部领域" @click="showLegendModal = true">
      <div class="kg-legend-row">
        <span v-for="lg in domainLegend" :key="lg.domain" class="kg-legend-item">
          <span class="kg-legend-dot" :style="{ background: lg.color }"></span>
          {{ domainLabel(lg.domain) }}（{{ lg.count }}）
        </span>
      </div>
      <span class="kg-legend-more"><i class="ti ti-dots"></i> 全部 {{ domainLegend.length }} 个</span>
    </div>
    <!-- 图例全量弹窗 -->
    <teleport to="body">
      <div v-if="showLegendModal" class="overlay" style="z-index:var(--z-modal)" @click.self="showLegendModal = false">
        <div class="modal">
          <div class="mt">领域图例（{{ domainLegend.length }}）</div>
          <div class="kg-legend-grid">
            <span v-for="lg in domainLegend" :key="lg.domain" class="kg-legend-item">
              <span class="kg-legend-dot" :style="{ background: lg.color }"></span>
              {{ domainLabel(lg.domain) }}（{{ lg.count }}）
            </span>
          </div>
          <div class="fg" style="justify-content:flex-end;margin-top:16px">
            <button @click="showLegendModal = false">关闭</button>
          </div>
        </div>
      </div>
    </teleport>
    <NodeDetailDrawer :entity="selectedEntity" :memories="entityMemories" @close="focus.clearPinned()"
      @open-memory="id => emit('open-memory', id)" />
  </div>
</template>

<style scoped>
/* 收纳条：单行不换行，超出裁切，整条可点击打开弹窗 */
.kg-legend {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 6px;
  font-size: var(--fs-sm);
  color: var(--muted);
  cursor: pointer;
}

.kg-legend:hover .kg-legend-more {
  color: var(--acctx);
}

.kg-legend-row {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-wrap: nowrap;
  gap: 14px;
  overflow: hidden;
  white-space: nowrap;
}

.kg-legend-more {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

/* 弹窗内全量图例：多列换行 */
.kg-legend-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  margin-top: 10px;
  max-height: 50vh;
  overflow-y: auto;
  font-size: var(--fs-base);
  color: var(--sec);
}

.kg-legend-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.kg-legend-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  display: inline-block;
  border: 1px solid var(--bd);
  flex-shrink: 0;
}
</style>
