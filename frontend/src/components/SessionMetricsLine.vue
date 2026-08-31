<script setup>
import { computed } from 'vue'

const props = defineProps({
  metrics: { type: Object, default: null },
  turnMetrics: { type: Object, default: null },
  // 流式期间前端估算的实时 tok/s。>0 时优先展示，取代已结算的 turnMetrics 值。
  liveTokensPerSecond: { type: Number, default: 0 },
})

function num(value) {
  const n = Number(value)
  return Number.isFinite(n) && n >= 0 ? n : 0
}

function formatTokens(value) {
  const n = num(value)
  if (n < 1000) return String(Math.round(n))
  if (n < 1000000) return `${Math.round(n / 100) / 10}K`
  return `${Math.round(n / 100000) / 10}M`
}

function formatDuration(value) {
  const ms = num(value)
  if (!ms) return ''
  const seconds = ms / 1000
  if (seconds < 60) return `${Math.round(seconds * 10) / 10}s`
  const whole = Math.round(seconds)
  return `${Math.floor(whole / 60)}m${whole % 60}s`
}

function formatSpeed(value) {
  const speed = Number(value)
  if (!Number.isFinite(speed) || speed <= 0) return ''
  return `${speed >= 100 ? Math.round(speed) : Math.round(speed * 10) / 10} tok/s`
}

// 布局：| 分组，组内 · 配对；仅保留 轮/步、LLM、首 token+tok/s、缓存、输入+输出 五组。
const items = computed(() => {
  const m = props.metrics
  if (!m || !num(m.steps)) return []
  const out = [`${num(m.turns)} 轮 · ${num(m.steps)} 步`]
  const llm = formatDuration(m.llm_ms)
  if (llm) out.push(`LLM ${llm}`)
  const speeds = []
  const ttft = formatDuration(m.ttft_average_ms)
  if (ttft) speeds.push(`首 token 平均 ${ttft}`)
  // 优先展示流式估算的实时速率——deepseek-harness 只在步边界刷新，这里做 chunk 级估计。
  // 未在流式中（liveTokensPerSecond=0）则退回当前 turn 结算后的准确值。
  const liveSpeed = formatSpeed(props.liveTokensPerSecond)
  const currentSpeed = liveSpeed || formatSpeed(props.turnMetrics?.tokens_per_second)
  if (currentSpeed) speeds.push(currentSpeed)
  if (speeds.length) out.push(speeds.join(' · '))
  if (num(m.input_tokens) || num(m.output_tokens)) {
    if (m.cache_hit_percent !== null && m.cache_hit_percent !== undefined) {
      const cache = Number(m.cache_hit_percent)
      if (Number.isFinite(cache)) out.push(`缓存命中 ${cache >= 99.95 ? 100 : Math.round(cache)}%`)
    }
    const io = []
    if (num(m.input_tokens)) io.push(`输入 ${formatTokens(m.input_tokens)} tok`)
    if (num(m.output_tokens)) io.push(`输出 ${formatTokens(m.output_tokens)} tok`)
    if (io.length) out.push(io.join(' · '))
  }
  return out
})

const title = computed(() => items.value.join(' | '))
</script>

<template>
  <div v-if="items.length" class="session-metrics-line" :title="title" role="status">
    <template v-for="(item, i) in items" :key="i">
      <span v-if="i > 0" class="session-metrics-sep" aria-hidden="true">|</span>
      <span class="session-metrics-item">{{ item }}</span>
    </template>
  </div>
</template>
