<script setup>
// MermaidChart —— Mermaid DSL 图表渲染（T10）
// 接受 diagram_type + mermaid_code，运行时渲染为 SVG
// 渲染失败展示原始代码 + 错误位置

import { ref, onMounted, nextTick, watch } from 'vue'
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

// 注入品牌主题变量
function applyTheme() {
    const style = getComputedStyle(document.documentElement)
    const primary = style.getPropertyValue('--brand-1').trim() || '#3b6ef6'
    const secondary = style.getPropertyValue('--brand-2').trim() || '#7b5cff'
    const lineColor = style.getPropertyValue('--muted').trim() || '#94949b'

    mermaid.initialize({
        startOnLoad: false,
        theme: 'base',
        themeVariables: {
            primaryColor: primary,
            primaryBorderColor: secondary,
            primaryTextColor: style.getPropertyValue('--fg').trim() || '#1c1c21',
            lineColor: lineColor,
            secondaryColor: style.getPropertyValue('--surface-2').trim() || '#f2f2f3',
            tertiaryColor: style.getPropertyValue('--surface-3').trim() || '#e9e9eb',
        },
        securityLevel: 'loose',
        flowchart: { useMaxWidth: true, htmlLabels: true },
    })
}

async function renderDiagram() {
    if (!props.code || !props.code.trim()) {
        errorMsg.value = 'Mermaid 代码为空'
        return
    }
    applyTheme()

    const uid = 'mermaid-' + Math.random().toString(36).slice(2, 10)
    try {
        const { svg } = await mermaid.render(uid, props.code)
        svgHtml.value = svg
        errorMsg.value = ''
        errorLine.value = -1
    } catch (e) {
        const msg = e.message || String(e)
        errorMsg.value = msg
        // 尝试提取行号（Mermaid 错误格式： "Parse error on line X: ..."）
        const lineMatch = msg.match(/line\s+(\d+)/i)
        errorLine.value = lineMatch ? parseInt(lineMatch[1]) : -1
        svgHtml.value = ''
    }
}

function copySrc() {
    navigator.clipboard.writeText(props.code)
    toast.push('success', '源码已复制')
}

async function copyImage() {
    const svg = container.value?.querySelector('svg')
    if (!svg) { toast.push('error', '图表未渲染'); return }
    try {
        const svgData = new XMLSerializer().serializeToString(svg)
        const svgBlob = new Blob([svgData], { type: 'image/svg+xml;charset=utf-8' })
        const url = URL.createObjectURL(svgBlob)
        const img = new Image()
        img.onload = async () => {
            const canvas = document.createElement('canvas')
            canvas.width = img.naturalWidth * 2
            canvas.height = img.naturalHeight * 2
            const ctx = canvas.getContext('2d')
            ctx.scale(2, 2)
            ctx.drawImage(img, 0, 0)
            URL.revokeObjectURL(url)
            canvas.toBlob(async (blob) => {
                try {
                    await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })])
                    toast.push('success', '图片已复制')
                } catch { toast.push('error', '复制图片失败') }
            }, 'image/png')
        }
        img.src = url
    } catch { toast.push('error', '复制图片失败') }
}

function downloadPng() {
    const svg = container.value?.querySelector('svg')
    if (!svg) { toast.push('error', '图表未渲染'); return }
    const svgData = new XMLSerializer().serializeToString(svg)
    const svgBlob = new Blob([svgData], { type: 'image/svg+xml;charset=utf-8' })
    const url = URL.createObjectURL(svgBlob)
    const img = new Image()
    img.onload = () => {
        const canvas = document.createElement('canvas')
        canvas.width = img.naturalWidth * 2
        canvas.height = img.naturalHeight * 2
        const ctx = canvas.getContext('2d')
        ctx.scale(2, 2)
        ctx.drawImage(img, 0, 0)
        URL.revokeObjectURL(url)
        canvas.toBlob((blob) => {
            const a = document.createElement('a')
            a.href = URL.createObjectURL(blob)
            a.download = 'diagram.png'; a.click()
            toast.push('success', 'PNG 已下载')
        }, 'image/png')
    }
    img.src = url
}

onMounted(() => {
    nextTick(() => renderDiagram())
})

// code 变化时重新渲染
watch(() => props.code, () => {
    nextTick(() => renderDiagram())
})
</script>

<template>
    <div class="mc-wrap" ref="container">
        <!-- 操作按钮 -->
        <div class="fc-actions">
            <button class="fc-btn" title="复制源码" @click="copySrc"><i class="ti ti-code"></i> 源码</button>
            <button class="fc-btn" title="复制图片" @click="copyImage"><i class="ti ti-copy"></i> 图片</button>
            <button class="fc-btn" title="下载 PNG" @click="downloadPng"><i class="ti ti-photo"></i> PNG</button>
        </div>

        <!-- 渲染成功 -->
        <div v-if="svgHtml" class="mc-svg" v-html="svgHtml" />

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
    overflow: hidden;
    background: var(--surface);
    padding: 16px;
}

.mc-svg {
    display: flex;
    justify-content: center;
    overflow-x: auto;
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
