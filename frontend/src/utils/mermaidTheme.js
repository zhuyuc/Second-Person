// Mermaid 统一主题：CSS 变量驱动（getComputedStyle 天然跟随 prefers-color-scheme），
// ChatView 内联代码块与 MermaidChart 组件共用同一配置，杜绝两套主题互相覆盖。
// 主题变量映射定义在 style.css :root 的 --mc-* token（从 SP-UI v4 色板派生）。
import mermaid from 'mermaid'

function readVar(name, fallback) {
    const style = getComputedStyle(document.documentElement)
    const val = style.getPropertyValue(name).trim()
    return val || fallback
}

export function applyMermaidTheme() {
    mermaid.initialize({
        startOnLoad: false,
        theme: 'base',
        suppressErrorRendering: true,
        securityLevel: 'loose',
        themeVariables: {
            background:           readVar('--mc-bg',              'transparent'),
            mainBkg:              readVar('--mc-main-bkg',        '#ffffff'),
            secondaryColor:       readVar('--mc-secondary-color', '#f2f2f3'),
            tertiaryColor:        readVar('--mc-tertiary-color',  '#e9e9eb'),
            primaryColor:         readVar('--mc-primary-color',   '#eef2ff'),
            primaryBorderColor:   readVar('--mc-primary-border',  '#3b5bdb'),
            primaryTextColor:     readVar('--mc-primary-text',    '#1c1c21'),
            secondaryBorderColor: readVar('--mc-secondary-border', 'rgba(17,20,28,.11)'),
            secondaryTextColor:   readVar('--mc-secondary-text',  '#5e616a'),
            tertiaryBorderColor:  readVar('--mc-tertiary-border', 'rgba(17,20,28,.06)'),
            tertiaryTextColor:    readVar('--mc-tertiary-text',   '#94949b'),
            lineColor:            readVar('--mc-line-color',      '#94949b'),
            edgeLabelBackground:  readVar('--mc-edge-label-bg',   '#ffffff'),
            nodeBorder:           readVar('--mc-node-border',     '#3b5bdb'),
            clusterBkg:           readVar('--mc-cluster-bkg',     '#f2f2f3'),
            clusterBorder:        readVar('--mc-cluster-border',  'rgba(17,20,28,.11)'),
            titleColor:           readVar('--mc-title-color',     '#3b5bdb'),
            fontSize: '14px',
            fontFamily: 'var(--sans, ui-sans-serif, system-ui, sans-serif)',
        },
        flowchart: { useMaxWidth: false, htmlLabels: true, curve: 'basis', padding: 20 },
        sequence:  { useMaxWidth: false, boxMargin: 10, messageMargin: 35, mirrorActors: false },
        er:        { useMaxWidth: false },
        gantt:     { useMaxWidth: false },
    })
}
