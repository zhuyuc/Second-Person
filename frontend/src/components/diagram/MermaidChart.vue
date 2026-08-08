<script setup>
// MermaidChart —— Mermaid DSL 图表渲染（T10）
// 接受 diagram_type + mermaid_code，运行时渲染为 SVG
// 渲染失败展示原始代码 + 错误位置

import { ref, onMounted, onUnmounted, nextTick, watch } from 'vue'
import mermaid from 'mermaid'
import { useToast } from '@/stores/toast'
import { usePanZoom } from '@/composables/usePanZoom'
import { applyMermaidTheme } from '@/utils/mermaidTheme'

const toast = useToast()

const props = defineProps({
    diagram_type: { type: String, required: true },
    code: { type: String, required: true },
})

const container = ref(null)
const viewport = ref(null)
const svgHtml = ref('')
const errorMsg = ref('')
const errorLine = ref(-1)
const showSrc = ref(false)

// ---- 平移缩放（CSS transform 作用于内容层，不侵入 Mermaid 生成的 SVG 结构） ----
const view = usePanZoom()
const { cssTransform: mcTransform } = view  // 解包为顶层 ref，模板中自动解包
const panning = ref(false)

// 滚轮缩放（以光标为中心；passive:false 才能 preventDefault 阻止页面滚动）
function onWheel(e) {
    if (!svgHtml.value || !viewport.value) return
    e.preventDefault()
    const rect = viewport.value.getBoundingClientRect()
    view.zoomAt(e.clientX - rect.left, e.clientY - rect.top,
        e.deltaY > 0 ? 1 / view.step : view.step)
}

// 拖拽平移
let panStart = null
function onPanStart(e) {
    if (!svgHtml.value || e.button !== 0) return
    e.preventDefault()
    panning.value = true
    panStart = { mx: e.clientX, my: e.clientY, vx: view.x.value, vy: view.y.value }
    window.addEventListener('mousemove', onPanMove)
    window.addEventListener('mouseup', onPanUp, { once: true })
}
function onPanMove(e) {
    if (!panning.value || !panStart) return
    view.x.value = panStart.vx + (e.clientX - panStart.mx)
    view.y.value = panStart.vy + (e.clientY - panStart.my)
}
function onPanUp() {
    panning.value = false
    panStart = null
    window.removeEventListener('mousemove', onPanMove)
}

// 按钮缩放：以视口中心为基准
function zoomCenter(factor) {
    const rect = viewport.value?.getBoundingClientRect()
    if (!rect) return
    view.zoomAt(rect.width / 2, rect.height / 2, factor)
}

// 渲染竞态防护：每次 render 递增，回调中比对是否仍是最新请求
let renderId = 0

// 读取 CSS 变量（PNG 导出背景色用；getComputedStyle 天然跟随深浅色切换）
function readVar(name, fallback) {
    const style = getComputedStyle(document.documentElement)
    const val = style.getPropertyValue(name).trim()
    return val || fallback
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
    applyMermaidTheme()

    const thisId = ++renderId
    const uid = 'mermaid-' + Math.random().toString(36).slice(2, 10)
    try {
        const { svg } = await mermaid.render(uid, props.code)
        // 竞态保护：仅当没有更新的渲染请求时才更新视图
        if (thisId !== renderId) return
        svgHtml.value = svg
        errorMsg.value = ''
        errorLine.value = -1
        view.reset()  // 重新渲染后复位视口
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

// 视口层由 v-if 控制，元素重建后需重新绑定 wheel（passive:false 才能阻止页面滚动）
watch(() => svgHtml.value, () => {
    nextTick(() => {
        viewport.value?.addEventListener('wheel', onWheel, { passive: false })
    })
})

onMounted(() => {
    darkMediaQuery.addEventListener('change', onDarkChange)
    nextTick(() => renderDiagram())
})

onUnmounted(() => {
    darkMediaQuery.removeEventListener('change', onDarkChange)
    viewport.value?.removeEventListener('wheel', onWheel)
})

// code 或 diagram_type 变化时重新渲染
watch([() => props.code, () => props.diagram_type], () => {
    nextTick(() => renderDiagram())
})
</script>

<template>
    <div class="mc-wrap" ref="container">
        <!-- 操作按钮（图表操作条公共样式已提升全 style.css .fc-actions/.fc-btn） -->
        <div class="fc-actions">
            <button class="fc-btn" title="放大" @click="zoomCenter(view.step)"><i class="ti ti-zoom-in"></i></button>
            <button class="fc-btn" title="缩小" @click="zoomCenter(1 / view.step)"><i class="ti ti-zoom-out"></i></button>
            <button class="fc-btn" title="复位视图（也可双击画布）" @click="view.reset()"><i class="ti ti-focus-2"></i></button>
            <button class="fc-btn" title="查看源码" @click="toggleSource"><i class="ti ti-code"></i> 查看源码</button>
            <button class="fc-btn" title="复制图片" @click="copyImage"><i class="ti ti-copy"></i> 图片</button>
            <button class="fc-btn" title="下载 PNG" @click="downloadPng"><i class="ti ti-photo"></i> PNG</button>
        </div>

        <!-- 渲染成功：视口层承接拖拽平移 / 双击复位 -->
        <div v-if="svgHtml" ref="viewport" class="mc-viewport" :class="{ 'mc-panning': panning }"
            @mousedown="onPanStart" @dblclick="view.reset()">
            <div class="mc-svg" :style="{ transform: mcTransform }" v-html="svgHtml" />
        </div>

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

/* 视口层：拖拽平移 + 滚轮缩放 */
.mc-viewport {
    overflow: hidden;
    cursor: grab;
    padding: 8px 0;
}

.mc-viewport.mc-panning {
    cursor: grabbing;
    user-select: none;
}

.mc-svg {
    display: flex;
    justify-content: center;
    transform-origin: 0 0;
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
