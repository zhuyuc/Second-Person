// 知识图谱视觉主题常量（v3.1 降噪版）
// 用色纪律（对齐全局设计系统 v3）：节点全中性、token 直引，主题自动跟随；
// 品牌色仅用于焦点描边环与激活边；普通连线为中性细虚线，安静克制。
export const THEME = {
    node: {
        fill: 'var(--surface-3)',           // SVG 直接引全局 token
        stroke: 'var(--bd-strong)',
        strokeWidth: 1.5,
        focusedStroke: '#7c5cff',           // 焦点描边环：图谱内唯一彩色
        focusedStrokeWidth: 2.5,
        dimmedOpacity: 0.15,
    },
    edge: {
        idleColor: 'var(--bd-strong)',      // 普通边：中性细虚线
        activeColor: 'rgba(124, 92, 255, .6)',  // 焦点激活边：品牌色实线
        neighborColor: 'var(--muted)',
        dimmedColor: 'var(--bd)',
        dash: '4 4',                        // 普通/邻居/淡化边虚线；active 实线
    },
    size: {
        nodeMin: 5, nodeMax: 26,            // 拉宽尺寸区间，强化层级对比
        focusedMultiplier: 1.35,            // 颜色弱化后靠尺寸补足焦点辨识度
        draggingMultiplier: 1.1,
        edgeMin: 1, edgeMax: 3,
        activeEdgeMultiplier: 1.5,
    },
    label: {
        fontSize: 11.5, focusedFontSize: 13.5,
    },
    // 滚轮缩放步长（SVG 引擎与 usePanZoom 共用同一语义）
    zoomStep: 1.12,
    // 引擎切换阈值：节点数 > 该值切 Sigma（WebGL）
    engineThreshold: 500,
    // 单视图硬上限
    maxNodes: 3000,
}

// Sigma（WebGL）不支持 CSS 变量，按当前主题返回 resolved 色板（与 token 同值）
export function sigmaPalette() {
    const dark = typeof matchMedia !== 'undefined'
        && matchMedia('(prefers-color-scheme: dark)').matches
    return dark ? {
        node: '#2e2e30', nodeDimmed: '#232325',
        edgeIdle: '#38383b', edgeNeighbor: '#6e6e73', edgeDimmed: '#242426',
        label: '#a0a0a5', focused: '#7c5cff',
    } : {
        node: '#e9e9eb', nodeDimmed: '#f2f2f3',
        edgeIdle: '#d9d9dc', edgeNeighbor: '#94949b', edgeDimmed: '#ececee',
        label: '#5e616a', focused: '#7c5cff',
    }
}

// 节点半径：5 + 21 × (memory_count / max)^0.75，范围钳制到 [5, 26]。
// 0.75 次幂比 √ 更拉开层级差异：低频实体保持小点，枢纽实体明显突出，
// 与后端 graph_layout._node_radius 同曲线（碰撞消除依赖一致的半径）
export function nodeRadius(memoryCount, maxCount) {
    const r = 5 + 21 * Math.pow((memoryCount || 0) / Math.max(1, maxCount), 0.75)
    return Math.max(THEME.size.nodeMin, Math.min(THEME.size.nodeMax, r))
}

// 领域配色：低饱和柔和色板（延续降噪纪律，不与品牌焦点色争夺注意力）；
// 同一 domain 稳定映射同一色（哈希取模），无 domain 回退中性色
export const DOMAIN_PALETTE = [
    '#8fb4d9', '#9ecf9e', '#d9b98f', '#c9a3d4', '#8fcfc9',
    '#d49a9a', '#b3c078', '#a3a8d4', '#d4b8a3', '#93c4ad',
]

export function domainColor(domain) {
    if (!domain) return null
    let h = 0
    for (let i = 0; i < domain.length; i++) h = (h * 31 + domain.charCodeAt(i)) >>> 0
    return DOMAIN_PALETTE[h % DOMAIN_PALETTE.length]
}

// 边粗细：1 + 2 × (weight / maxWeight)，范围 [1, 3]
export function edgeWidth(weight, maxWeight) {
    const w = THEME.size.edgeMin +
        (THEME.size.edgeMax - THEME.size.edgeMin) * ((weight || 1) / Math.max(1, maxWeight))
    return Math.max(THEME.size.edgeMin, Math.min(THEME.size.edgeMax, w))
}
