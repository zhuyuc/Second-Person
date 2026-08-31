// usePanZoom —— 图表平移缩放通用能力（拖拽平移 + 滚轮/按钮缩放 + 双击复位）
// 同一套视口状态（x/y 平移、k 缩放）提供两种输出：
//   svgTransform：SVG <g transform> 属性（FlowChartSVG）
//   cssTransform：CSS transform 字符串（MermaidChart 等 v-html 注入的 SVG）
// zoomAt(px, py, factor) 以视口坐标 (px, py) 为中心缩放，保持光标下内容不动。
import { ref, computed } from 'vue'

export function usePanZoom({ min = 0.2, max = 4, step = 1.12 } = {}) {
  const x = ref(0)
  const y = ref(0)
  const k = ref(1)
  const clampK = (v) => Math.max(min, Math.min(max, v))

  // 以视口坐标 (px, py) 为中心缩放
  function zoomAt(px, py, factor) {
    const nk = clampK(k.value * factor)
    if (nk === k.value) return
    const cx = (px - x.value) / k.value
    const cy = (py - y.value) / k.value
    x.value = px - cx * nk
    y.value = py - cy * nk
    k.value = nk
  }

  function reset() {
    x.value = 0
    y.value = 0
    k.value = 1
  }

  // 视口坐标 → 内容坐标（节点拖拽换算用）
  function toContent(px, py) {
    return { x: (px - x.value) / k.value, y: (py - y.value) / k.value }
  }

  const svgTransform = computed(() => `translate(${x.value} ${y.value}) scale(${k.value})`)
  const cssTransform = computed(() => `translate(${x.value}px, ${y.value}px) scale(${k.value})`)

  return { x, y, k, step, zoomAt, reset, toContent, svgTransform, cssTransform }
}
