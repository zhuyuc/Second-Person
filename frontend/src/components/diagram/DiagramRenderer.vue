<script setup>
// DiagramRenderer —— 图形能力统一渲染入口（T11）
// 按 type 分发：flowchart → FlowChartSVG / mermaid → MermaidChart

import { defineAsyncComponent } from 'vue'

const FlowChartSVG = defineAsyncComponent(() => import('./FlowChartSVG.vue'))
const MermaidChart = defineAsyncComponent(() => import('./MermaidChart.vue'))

const props = defineProps({
  type: { type: String, required: true },
  data: { type: Object, required: true },
})

const emit = defineEmits(['node-click'])

function onNodeClick(nodeId) {
  emit('node-click', nodeId)
}

// 空数据判定
function isEmpty() {
  if (!props.data) return true
  if (props.type === 'flowchart') {
    return !props.data.nodes || props.data.nodes.length === 0
  }
  if (props.type === 'mermaid') {
    return !props.data.mermaid_code || !props.data.mermaid_code.trim()
  }
  return true
}
</script>

<template>
  <div class="dr-block">
    <!-- 空状态 -->
    <div v-if="isEmpty()" class="dr-empty"><i class="ti ti-chart-dots"></i> 图表数据为空</div>

    <!-- 按类型分发 -->
    <FlowChartSVG
      v-else-if="type === 'flowchart'"
      :nodes="data.nodes || []"
      :edges="data.edges || []"
      @node-click="onNodeClick"
    />

    <MermaidChart
      v-else-if="type === 'mermaid'"
      :diagram_type="data.diagram_type || 'flowchart'"
      :code="data.mermaid_code || ''"
    />

    <!-- 未知类型 -->
    <div v-else class="dr-error"><i class="ti ti-alert-circle"></i> 未知图表类型：{{ type }}</div>
  </div>
</template>

<style scoped>
.dr-block {
  /* 块级元素，与文本气泡同层 */
}

.dr-empty,
.dr-error {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 16px;
  border: 1px solid var(--bd);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--muted);
  font-size: var(--fs-sm);
  margin: 12px 0;
}

.dr-error {
  color: var(--dangtx);
  border-color: var(--dangbg);
}
</style>
