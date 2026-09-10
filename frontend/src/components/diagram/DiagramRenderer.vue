<script setup>
// DiagramRenderer —— 图形能力统一渲染入口（T11）
// 按 type 分发：flowchart → FlowChartSVG / mermaid → MermaidChart /
// generated_image → 本地文生图 / generated_video → 本地文生视频

import { computed, defineAsyncComponent, ref } from 'vue'
import { useToast } from '@/stores/toast'

const AsyncFlowChart = defineAsyncComponent(() => import('./FlowChartSVG.vue'))
const AsyncMermaidChart = defineAsyncComponent(() => import('./MermaidChart.vue'))

const toast = useToast()
const downloading = ref(false)

const props = defineProps({
  type: { type: String, required: true },
  data: { type: Object, required: true },
})

const emit = defineEmits(['node-click'])

function onNodeClick(nodeId) {
  emit('node-click', nodeId)
}

const imageSrc = computed(() => {
  const d = props.data || {}
  if (Array.isArray(d.public_urls) && d.public_urls[0]) return d.public_urls[0]
  if (Array.isArray(d.filenames) && d.filenames[0]) {
    return `/chat-images/${d.filenames[0]}`
  }
  return ''
})

const videoSrc = computed(() => {
  const d = props.data || {}
  if (Array.isArray(d.public_urls) && d.public_urls[0]) return d.public_urls[0]
  if (Array.isArray(d.filenames) && d.filenames[0]) {
    return `/chat-videos/${d.filenames[0]}`
  }
  return ''
})

const videoPoster = computed(() => {
  const d = props.data || {}
  return d.poster_url || ''
})

const imageFilename = computed(() => {
  const d = props.data || {}
  const name = Array.isArray(d.filenames) && d.filenames[0]
    ? String(d.filenames[0]).replace(/\\/g, '/').split('/').pop()
    : ''
  if (name) return name
  const src = imageSrc.value || ''
  const m = src.match(/\/([^/?#]+\.(?:png|jpe?g|webp|gif))(?:[?#]|$)/i)
  return m ? m[1] : `gen_${Date.now()}.png`
})

const videoFilename = computed(() => {
  const d = props.data || {}
  const name = Array.isArray(d.filenames) && d.filenames[0]
    ? String(d.filenames[0]).replace(/\\/g, '/').split('/').pop()
    : ''
  if (name) return name
  const src = videoSrc.value || ''
  const m = src.match(/\/([^/?#]+\.(?:mp4|webm|gif))(?:[?#]|$)/i)
  return m ? m[1] : `genv_${Date.now()}.mp4`
})

const imageCaption = computed(() => {
  const d = props.data || {}
  return d.summary || (d.latency_ms != null ? `本地生成 · ${Math.round(d.latency_ms / 1000)}s` : '')
})

const videoCaption = computed(() => {
  const d = props.data || {}
  if (d.summary) return d.summary
  const parts = []
  if (d.duration_sec != null) parts.push(`约 ${d.duration_sec}s`)
  if (d.latency_ms != null) parts.push(`耗时 ${Math.round(d.latency_ms / 1000)}s`)
  return parts.length ? `本地短视频 · ${parts.join(' · ')}` : ''
})

async function downloadBlob(src, filename, successMsg) {
  if (!src || downloading.value) return
  downloading.value = true
  try {
    const resp = await fetch(src)
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const blob = await resp.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    toast.push('success', successMsg)
  } catch {
    toast.push('error', '下载失败')
  } finally {
    downloading.value = false
  }
}

async function downloadImage() {
  await downloadBlob(imageSrc.value, imageFilename.value, '图片已下载')
}

async function downloadVideo() {
  await downloadBlob(videoSrc.value, videoFilename.value, '视频已下载')
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
  if (props.type === 'generated_image') {
    return !imageSrc.value
  }
  if (props.type === 'generated_video') {
    return !videoSrc.value
  }
  return true
}
</script>

<template>
  <div class="dr-block">
    <!-- 空状态 -->
    <div v-if="isEmpty()" class="dr-empty"><i class="ti ti-chart-dots"></i> 图表数据为空</div>

    <!-- 按类型分发 -->
    <AsyncFlowChart
      v-else-if="type === 'flowchart'"
      :nodes="data.nodes || []"
      :edges="data.edges || []"
      @node-click="onNodeClick"
    />

    <AsyncMermaidChart
      v-else-if="type === 'mermaid'"
      :diagram_type="data.diagram_type || 'flowchart'"
      :code="data.mermaid_code || ''"
    />

    <div v-else-if="type === 'generated_image'" class="dr-image-wrap">
      <div class="fc-actions">
        <button
          class="fc-btn"
          type="button"
          title="下载图片"
          :disabled="downloading"
          @click="downloadImage"
        >
          <i class="ti ti-download"></i> {{ downloading ? '下载中…' : '下载' }}
        </button>
      </div>
      <img class="dr-image" :src="imageSrc" :alt="imageCaption || '生成图片'" loading="lazy" />
      <div v-if="imageCaption" class="dr-image-caption">{{ imageCaption }}</div>
    </div>

    <div v-else-if="type === 'generated_video'" class="dr-video-wrap">
      <div class="fc-actions">
        <button
          class="fc-btn"
          type="button"
          title="下载视频"
          :disabled="downloading"
          @click="downloadVideo"
        >
          <i class="ti ti-download"></i> {{ downloading ? '下载中…' : '下载' }}
        </button>
      </div>
      <video
        class="dr-video"
        :src="videoSrc"
        :poster="videoPoster || undefined"
        controls
        playsinline
        preload="metadata"
      />
      <div v-if="videoCaption" class="dr-image-caption">{{ videoCaption }}</div>
    </div>

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

.dr-image-wrap {
  position: relative;
  margin: 12px 0;
  max-width: min(100%, 520px);
  padding-top: 4px;
}

.dr-video-wrap {
  position: relative;
  margin: 12px 0;
  max-width: min(100%, 520px);
  padding-top: 4px;
}

.dr-image-wrap:hover .fc-actions,
.dr-image-wrap:focus-within .fc-actions,
.dr-video-wrap:hover .fc-actions,
.dr-video-wrap:focus-within .fc-actions {
  opacity: 1;
}

.dr-image-wrap .fc-btn:disabled,
.dr-video-wrap .fc-btn:disabled {
  opacity: 0.6;
  cursor: wait;
}

.dr-image {
  display: block;
  max-width: 100%;
  height: auto;
  border-radius: var(--radius-sm);
  border: 1px solid var(--bd);
  background: var(--surface);
}

.dr-video {
  display: block;
  width: 100%;
  max-height: 480px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--bd);
  background: #0b0b0b;
}

.dr-image-caption {
  margin-top: 6px;
  font-size: var(--fs-sm);
  color: var(--muted);
}
</style>
