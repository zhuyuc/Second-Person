<script setup>
// 知识图谱主容器（v4.0）：Canvas 单引擎 + 力仿真 + 搜索定位 + 详情抽屉。
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { memoryApi } from '@/api/memory'
import { THEME, nodeRadius, domainColor } from './graphTheme'
import { domainLabel, loadDomainLabels } from '@/utils/domainLabel'
import { useGraphFocus } from './composables/useGraphFocus'
import CanvasGraph from './CanvasGraph.vue'
import NodeDetailDrawer from './NodeDetailDrawer.vue'

const emit = defineEmits(['open-memory'])

const W = 640,
  H = 440
const nodes = ref([])
const edges = ref([])
const loading = ref(false)
const focus = useGraphFocus(edges)
const graphRef = ref(null)

// 搜索
const searchQuery = ref('')
const searchTarget = ref(null)
const searchResults = computed(() => {
  const q = searchQuery.value.trim().toLowerCase()
  if (!q) return []
  return nodes.value.filter((n) => n.name.toLowerCase().includes(q)).slice(0, 8)
})

function doSearch(node) {
  searchTarget.value = node.entity_id
  focus.pinnedId.value = node.entity_id
  searchQuery.value = ''
  onNodeClick(node.entity_id)
}

// 详情抽屉
const selectedEntity = ref(null)
const entityMemories = ref([])

// 领域图例弹窗
const showLegendModal = ref(false)

function withCoords(list) {
  const maxCount = Math.max(1, ...list.map((x) => x.memory_count || 0))
  let x0 = Infinity,
    y0 = Infinity,
    x1 = -Infinity,
    y1 = -Infinity,
    has = false
  for (const n of list) {
    if (n.x !== null && n.x !== undefined && n.y !== null && n.y !== undefined) {
      has = true
      x0 = Math.min(x0, n.x)
      x1 = Math.max(x1, n.x)
      y0 = Math.min(y0, n.y)
      y1 = Math.max(y1, n.y)
    }
  }
  const cx = has ? (x0 + x1) / 2 : W / 2
  const cy = has ? (y0 + y1) / 2 : H / 2
  const R = has ? Math.max(x1 - x0, y1 - y0) / 2 + 60 : Math.min(W, H) / 2 - 90
  return list.map((n, i) => {
    let x = n.x,
      y = n.y
    if (x === null || x === undefined || y === null || y === undefined) {
      const a = (i / Math.max(1, list.length)) * Math.PI * 2 - Math.PI / 2
      const r0 = list.length <= 1 ? 0 : R
      x = cx + r0 * Math.cos(a)
      y = cy + r0 * Math.sin(a)
    }
    return { ...n, x, y, r: nodeRadius(n.memory_count, maxCount) }
  })
}

async function loadGraph() {
  loading.value = true
  try {
    const d = await memoryApi.graph()
    nodes.value = withCoords(d.nodes || [])
    edges.value = d.edges || []
  } finally {
    loading.value = false
  }
}

function mergeGraph(center, neighbors, newEdges) {
  const existing = new Set(nodes.value.map((n) => n.entity_id))
  const maxCount = Math.max(
    1,
    ...nodes.value.map((x) => x.memory_count || 0),
    ...neighbors.map((x) => x.memory_count || 0)
  )
  const cnode = nodes.value.find((n) => n.entity_id === center.entity_id)
  const cx = cnode ? cnode.x : W / 2,
    cy = cnode ? cnode.y : H / 2
  const added = []
  for (const nb of neighbors) {
    if (existing.has(nb.entity_id)) continue
    if (nodes.value.length + added.length >= THEME.maxNodes) break
    const x = cx + (Math.random() - 0.5) * 40
    const y = cy + (Math.random() - 0.5) * 40
    added.push({ ...nb, x, y, r: nodeRadius(nb.memory_count, maxCount) })
    existing.add(nb.entity_id)
  }
  if (added.length) nodes.value = [...nodes.value, ...added]
  const ekey = new Set(edges.value.map((e) => e.source + '|' + e.target))
  const addE = []
  for (const e of newEdges) {
    const k = e.source + '|' + e.target
    if (!ekey.has(k) && existing.has(e.source) && existing.has(e.target)) {
      addE.push(e)
      ekey.add(k)
    }
  }
  if (addE.length) edges.value = [...edges.value, ...addE]
  return added.length
}

async function expand(entityId) {
  if (nodes.value.length >= THEME.maxNodes) return
  const exclude = nodes.value.map((n) => n.entity_id).join(',')
  const d = await memoryApi.graphNeighbors(entityId, { limit: 30, exclude_ids: exclude })
  if (d && d.center) mergeGraph(d.center, d.neighbors || [], d.edges || [])
}

async function onNodeClick(entityId) {
  selectedEntity.value = nodes.value.find((n) => n.entity_id === entityId) || {
    entity_id: entityId,
  }
  entityMemories.value = await memoryApi.graphEntityMemories(entityId)
  expand(entityId)
}

watch(
  () => focus.pinnedId.value,
  (v) => {
    if (!v) {
      selectedEntity.value = null
      entityMemories.value = []
    }
  }
)

function onEsc(e) {
  if (e.key !== 'Escape') return
  if (searchQuery.value) {
    searchQuery.value = ''
    return
  }
  if (selectedEntity.value) {
    selectedEntity.value = null
    entityMemories.value = []
    focus.clearPinned()
  } else focus.clearPinned()
}

const domainLegend = computed(() => {
  const cnt = {}
  for (const n of nodes.value) {
    if (n.domain) cnt[n.domain] = (cnt[n.domain] || 0) + 1
  }
  return Object.entries(cnt)
    .sort((a, b) => b[1] - a[1])
    .map(([d, c]) => ({ domain: d, count: c, color: domainColor(d) }))
})

onMounted(() => {
  loadGraph()
  loadDomainLabels()
  window.addEventListener('keydown', onEsc)
})
onBeforeUnmount(() => window.removeEventListener('keydown', onEsc))
defineExpose({ loadGraph })
</script>

<template>
  <div>
    <div v-if="loading" class="empty"><i class="ti ti-loader-2"></i>图谱加载中…</div>
    <div v-else-if="!nodes.length" class="empty">
      <i class="ti ti-affiliate"></i>还没有记忆关联，先创建几条关联记忆吧
    </div>
    <div v-else class="kg-wrap">
      <!-- 搜索框 -->
      <div class="kg-search">
        <i class="ti ti-search"></i>
        <input
          v-model="searchQuery"
          placeholder="搜索实体…"
          class="kg-search-input"
          @keydown.esc.stop="searchQuery = ''"
        />
        <div v-if="searchResults.length" class="kg-search-dropdown">
          <div
            v-for="r in searchResults"
            :key="r.entity_id"
            class="kg-search-item"
            @mousedown.prevent="doSearch(r)"
          >
            <span
              class="kg-search-dot"
              :style="{ background: domainColor(r.domain) || 'var(--muted)' }"
            ></span>
            {{ r.name }}
            <span class="kg-search-count">{{ r.memory_count }}</span>
          </div>
        </div>
      </div>
      <CanvasGraph
        ref="graphRef"
        :nodes="nodes"
        :edges="edges"
        :focus="focus"
        :search-target="searchTarget"
        @node-click="onNodeClick"
      />
    </div>
    <div class="muted" style="margin-top: 8px">
      节点=实体（大小与内部数字=关联记忆数，颜色=领域），连线=共现关系（粗细=共现条数）。
    </div>
    <div
      v-if="domainLegend.length"
      class="kg-legend"
      title="点击查看全部领域"
      @click="showLegendModal = true"
    >
      <div class="kg-legend-row">
        <span v-for="lg in domainLegend" :key="lg.domain" class="kg-legend-item">
          <span class="kg-legend-dot" :style="{ background: lg.color }"></span>
          {{ domainLabel(lg.domain) }}（{{ lg.count }}）
        </span>
      </div>
      <span class="kg-legend-more"
        ><i class="ti ti-dots"></i> 全部 {{ domainLegend.length }} 个</span
      >
    </div>
    <BaseModal
      v-if="showLegendModal"
      :title="`领域图例（${domainLegend.length}）`"
      @close="showLegendModal = false"
    >
      <div class="kg-legend-grid">
        <span v-for="lg in domainLegend" :key="lg.domain" class="kg-legend-item">
          <span class="kg-legend-dot" :style="{ background: lg.color }"></span>
          {{ domainLabel(lg.domain) }}（{{ lg.count }}）
        </span>
      </div>
      <template #footer>
        <button @click="showLegendModal = false">关闭</button>
      </template>
    </BaseModal>
    <NodeDetailDrawer
      :entity="selectedEntity"
      :memories="entityMemories"
      @close="focus.clearPinned()"
      @open-memory="(id) => emit('open-memory', id)"
    />
  </div>
</template>

<style scoped>
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

/* 搜索框 */
.kg-search {
  position: absolute;
  top: 12px;
  right: 12px;
  z-index: var(--z-sticky);
  display: flex;
  align-items: center;
  gap: 6px;
  background: var(--surface);
  border: 1px solid var(--bd);
  border-radius: 6px;
  padding: 4px 10px;
  font-size: var(--fs-sm);
  min-width: 180px;
  max-width: 260px;
}

.kg-search .ti {
  color: var(--muted);
  font-size: 14px;
  flex-shrink: 0;
}

.kg-search-input {
  background: transparent;
  border: none;
  outline: none;
  color: var(--fg);
  font-size: var(--fs-sm);
  width: 100%;
  padding: 2px 0;
}

.kg-search-dropdown {
  position: absolute;
  top: 100%;
  left: 0;
  right: 0;
  margin-top: 4px;
  background: var(--surface);
  border: 1px solid var(--bd);
  border-radius: 6px;
  box-shadow: var(--shadow-2);
  overflow: hidden;
}

.kg-search-item {
  padding: 8px 12px;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--fs-sm);
  color: var(--fg);
}

.kg-search-item:hover {
  background: var(--surface-2);
}

.kg-search-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.kg-search-count {
  margin-left: auto;
  color: var(--muted);
  font-size: var(--fs-xs);
}
</style>
