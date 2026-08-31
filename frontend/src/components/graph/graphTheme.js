// 知识图谱视觉主题常量（v4.0 Canvas 引擎版）
export const THEME = {
  node: {
    fill: 'var(--surface-3)',
    stroke: 'var(--bd-strong)',
    strokeWidth: 1.5,
    focusedStroke: '#7c5cff',
    focusedStrokeWidth: 2.5,
    dimmedOpacity: 0.15,
  },
  edge: {
    idleColor: 'var(--bd-strong)',
    activeColor: 'rgba(124, 92, 255, .6)',
    neighborColor: 'var(--muted)',
    dimmedColor: 'var(--bd)',
    dash: '4 4',
  },
  size: {
    nodeMin: 5,
    nodeMax: 26,
    focusedMultiplier: 1.35,
    draggingMultiplier: 1.1,
    edgeMin: 1,
    edgeMax: 3,
    activeEdgeMultiplier: 1.5,
  },
  label: {
    fontSize: 11.5,
    focusedFontSize: 13.5,
  },
  zoomStep: 1.12,
  maxNodes: 3000,
}

// 节点半径：5 + 21 × (memory_count / max)^0.75
export function nodeRadius(memoryCount, maxCount) {
  const r = 5 + 21 * Math.pow((memoryCount || 0) / Math.max(1, maxCount), 0.75)
  return Math.max(THEME.size.nodeMin, Math.min(THEME.size.nodeMax, r))
}

// 领域配色
const DOMAIN_PALETTE = [
  '#8fb4d9',
  '#9ecf9e',
  '#d9b98f',
  '#c9a3d4',
  '#8fcfc9',
  '#d49a9a',
  '#b3c078',
  '#a3a8d4',
  '#d4b8a3',
  '#93c4ad',
]

export function domainColor(domain) {
  if (!domain) return null
  let h = 0
  for (let i = 0; i < domain.length; i++) h = (h * 31 + domain.charCodeAt(i)) >>> 0
  return DOMAIN_PALETTE[h % DOMAIN_PALETTE.length]
}

// 边粗细
export function edgeWidth(weight, maxWeight) {
  const w =
    THEME.size.edgeMin +
    (THEME.size.edgeMax - THEME.size.edgeMin) * ((weight || 1) / Math.max(1, maxWeight))
  return Math.max(THEME.size.edgeMin, Math.min(THEME.size.edgeMax, w))
}
