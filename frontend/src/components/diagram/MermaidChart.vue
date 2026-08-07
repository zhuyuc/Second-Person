<script setup>
// MermaidChart —— Mermaid DSL 图表渲染（T10）
// 接受 diagram_type + mermaid_code，运行时渲染为 SVG
// 渲染失败展示原始代码 + 错误位置

import { ref, onMounted, onUnmounted, nextTick, watch } from 'vue'
import mermaid from 'mermaid'
import { useToast } from '@/stores/toast'

const toast = useToast()

const props = defineProps({
    diagram_type: { type: String, required: true },
    code: { type: String, required: true },
})

const container = ref(null)
const svgHtml = ref('')
const errorMsg = ref('')
const errorLine = ref(-1)
const showSrc = ref(false)

// 渲染竞态防护：每次 render 递增，回调中比对是否仍是最新请求
let renderId = 0

// 读取 CSS 变量（getComputedStyle 天然跟随 prefers-color-scheme 切换）
function readVar(name, fallback) {
    const style = getComputedStyle(document.documentElement)
    const val = style.getPropertyValue(name).trim()
    return val || fallback
}

// 注入品牌主题变量 —— 全部通过 CSS 变量驱动，深浅模式自动跟随
function applyTheme() {
    mermaid.initialize({
        startOnLoad: false,
        theme: 'base',
        suppressErrorRendering: true,
        themeVariables: {
            background:           readVar('--mc-bg',              'transparent'),
            mainBkg:              readVar('--mc-main-bkg',        '#ffffff'),
            secondaryColor:       readVar('--mc-secondary-color', '#f2f2f3'),
            tertiaryColor:        readVar('--mc-tertiary-color',  '#e9e9eb'),
            primaryColor:         readVar('--mc-primary-color',   '#eef2ff'),
            primaryBorderColor:   readVar('--mc-primary-border',  '#3b5bdb'),
            primaryTextColor:     readVar('--mc-primary-text',    '#1c1c21'),
            secondaryBorderColor: readVar('--mc-secondary-border','rgba(17,20,28,.11)'),
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
        securityLevel: 'loose',
        flowchart: { useMaxWidth: false, htmlLabels: true, curve: 'basis', padding: 20 },
        sequence:  { useMaxWidth: false, boxMargin: 10, messageMargin: 35, mirrorActors: false },
        er:        { useMaxWidth: false },
        gantt:     { useMaxWidth: false },
    })
}

// SVG 后处理：字体兜底 + 节点圆角统一（Mermaid 的部分属性无法通过 CSS 覆盖）
function postProcess(svgEl) {
    // 字体兜底
    svgEl.querySelectorAll('text, tspan').forEach(el => {
        el.style.fontFamily = 'var(--sans, ui-sans-serif, system-ui, sans-serif)'
    })
    // 移除 Mermaid 内联样式，改为响应式
    svgEl.removeAttribute('style')
    svgEl.style.maxWidth = '100%'
    svgEl.style.height = 'auto'
    // 节点圆角统一
    svgEl.querySelectorAll('.node rect, .node polygon').forEach(el => {
        if (!el.getAttribute('rx')) el.setAttribute('rx', '6')
        if (!el.getAttribute('ry')) el.setAttribute('ry', '6')
    })
}

async function renderDiagram() {
    if (!props.code?.trim()) {
        errorMsg.value = 'Mermaid 代码为空'
        return
    }
    applyTheme()

    const thisId = ++renderId
    const uid = 'mermaid-' + Math.random().toString(36).slice(2, 10)
    try {
        const { svg } = await mermaid.render(uid, props.code)
        // 竞态保护：仅当没有更新的渲染请求时才更新视图
        if (thisId !== renderId) return
        svgHtml.value = svg
        errorMsg.value = ''
        errorLine.value = -1
        await nextTick()
        const svgEl = container.value?.querySelector('svg')
        if (svgEl) postProcess(svgEl)
    } catch (e) {
        if (thisId !== renderId) return
        const msg = e.message || String(e)
        errorMsg.value = msg
        // 尝试提取行号（Mermaid 错误格式： "Parse error on line X: ..."）
        const lineMatch = msg.match(/line\s+(\d+)/i)
        errorLine.value = lineMatch ? parseInt(lineMatch[1]) : -1
        svgHtml.value = ''
    }
}

function toggleSource() {
    showSrc.value = !showSrc.value
}

function copySrc() {
    navigator.clipboard.writeText(props.code)
    toast.push('success', '源码已复制')
}

// 公共函数：SVG → PNG Blob（含背景色填充）
function svgToPngBlob(svg, scale = 2) {
    return new Promise((resolve, reject) => {
        const svgData = new XMLSerializer().serializeToString(svg)
        const url = URL.createObjectURL(new Blob([svgData], { type: 'image/svg+xml;charset=utf-8' }))
        const img = new Image()
        img.onload = () => {
            const canvas = document.createElement('canvas')
            canvas.width = img.naturalWidth * scale
            canvas.height = img.naturalHeight * scale
            const ctx = canvas.getContext('2d')
            // 填充背景色，避免暗色模式导出透明
            ctx.fillStyle = readVar('--surface', '#ffffff')
            ctx.fillRect(0, 0, canvas.width, canvas.height)
            ctx.scale(scale, scale)
            ctx.drawImage(img, 0, 0)
            URL.revokeObjectURL(url)
            canvas.toBlob((blob) => {
                if (blob) resolve(blob)
                else reject(new Error('Canvas toBlob failed'))
            }, 'image/png')
        }
        img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('Image load failed')) }
        img.src = url
    })
}

async function copyImage() {
    const svg = container.value?.querySelector('svg')
    if (!svg) { toast.push('error', '图表未渲染'); return }
    try {
        const blob = await svgToPngBlob(svg)
        await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })])
        toast.push('success', '图片已复制')
    } catch { toast.push('error', '复制图片失败') }
}

function downloadPng() {
    const svg = container.value?.querySelector('svg')
    if (!svg) { toast.push('error', '图表未渲染'); return }
    svgToPngBlob(svg).then((blob) => {
        const a = document.createElement('a')
        a.href = URL.createObjectURL(blob)
        a.download = `diagram-${Date.now()}.png`
        a.click()
        toast.push('success', 'PNG 已下载')
    }).catch(() => toast.push('error', '导出图片失败'))
}

// 暗色模式切换监听
const darkMediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
function onDarkChange() {
    nextTick(() => renderDiagram())
}

onMounted(() => {
    darkMediaQuery.addEventListener('change', onDarkChange)
    nextTick(() => renderDiagram())
})

onUnmounted(() => {
    darkMediaQuery.removeEventListener('change', onDarkChange)
})

// code 或 diagram_type 变化时重新渲染
watch([() => props.code, () => props.diagram_type], () => {
    nextTick(() => renderDiagram())
})
</script>

<template>
    <div class="mc-wrap" ref="container">
        <!-- 操作按钮 -->
        <div class="fc-actions">
            <button class="fc-btn" title="查看源码" @click="toggleSource"><i class="ti ti-code"></i> 查看源码</button>
            <button class="fc-btn" title="复制图片" @click="copyImage"><i class="ti ti-copy"></i> 图片</button>
            <button class="fc-btn" title="下载 PNG" @click="downloadPng"><i class="ti ti-photo"></i> PNG</button>
        </div>

        <!-- 渲染成功 -->
        <div v-if="svgHtml" class="mc-svg" v-html="svgHtml" />

        <!-- 源码查看面板 -->
        <div v-if="showSrc" class="mc-src">
            <div class="mc-src-head">
                <span>Mermaid 源码</span>
                <button class="fc-btn" title="复制源码" @click="copySrc"><i class="ti ti-copy"></i> 复制</button>
            </div>
            <pre class="mc-src-code">{{ code }}</pre>
        </div>

        <!-- 渲染失败 -->
        <div v-if="errorMsg" class="mc-error">
            <div class="mc-error-head"><i class="ti ti-alert-triangle"></i> Mermaid 渲染失败</div>
            <div v-if="errorLine > 0" class="mc-error-line">错误位置：第 {{ errorLine }} 行</div>
            <pre class="mc-error-code">{{ code }}</pre>
            <div class="mc-error-msg">{{ errorMsg }}</div>
        </div>
    </div>
</template>

<style scoped>
.mc-wrap {
    position: relative;
    margin: 12px 0;
    border: 1px solid var(--bd);
    border-radius: var(--radius-sm);
    background: var(--surface);
    padding: 20px 16px 16px;
}

.mc-svg {
    display: flex;
    justify-content: center;
    overflow-x: auto;
    padding: 8px 0;
}

.mc-svg :deep(svg) {
    max-width: 100%;
    height: auto;
    display: block;
}

.mc-svg :deep(text),
.mc-svg :deep(tspan) {
    font-family: var(--sans, ui-sans-serif, system-ui, sans-serif) !important;
}

.mc-svg :deep(.node rect),
.mc-svg :deep(.node polygon) {
    rx: 6px;
}

/* 操作按钮：复用 FlowChartSVG 的样式 */
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

.mc-wrap:hover .fc-actions {
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

/* 错误展示 */
.mc-src {
    margin-top: 12px;
    border: 1px solid var(--bd);
    border-radius: var(--radius-xs);
    background: var(--surface-2);
}

.mc-src-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 6px 10px;
    font-size: var(--fs-sm);
    color: var(--sec);
    border-bottom: 1px solid var(--bd);
}

.mc-src-code {
    font-family: var(--mono);
    font-size: var(--fs-sm);
    padding: 10px;
    white-space: pre-wrap;
    word-break: break-all;
    max-height: 260px;
    overflow-y: auto;
    margin: 0;
}

.mc-error {
    padding: 12px;
}

.mc-error-head {
    font-weight: 600;
    color: var(--dangtx);
    margin-bottom: 8px;
    display: flex;
    align-items: center;
    gap: 6px;
}

.mc-error-line {
    font-size: var(--fs-sm);
    color: var(--muted);
    margin-bottom: 6px;
}

.mc-error-code {
    font-family: var(--mono);
    font-size: var(--fs-sm);
    background: var(--surface-2);
    border: 1px solid var(--bd);
    border-radius: var(--radius-xs);
    padding: 10px;
    white-space: pre-wrap;
    word-break: break-all;
    max-height: 200px;
    overflow-y: auto;
    margin-bottom: 8px;
}

.mc-error-msg {
    font-size: var(--fs-sm);
    color: var(--dangtx);
}
</style>
