// 实时解码速率估算（tok/s）——deepseek-harness 的 sessionStats projection 只在
// assistant/message（步边界）刷新，这里进一步在流式 chunk 到达时给出 chunk 级估算。
//
// 估算模型：tokens ≈ chars / charsPerToken；速率 = tokens / (now - firstDeltaAt)。
// charsPerToken 初值给中英混合场景一个中性默认（3.0），每轮 turn_completed 拿到真实
// output_tokens 后按简单指数平滑（w=0.5）自学习校准。停止 tick 后仍保留最后估算值，
// 便于 UI 平滑过渡到 turn_completed 后的准确值。
import { ref, computed, onScopeDispose } from 'vue'

const DEFAULT_CHARS_PER_TOKEN = 3.0
const MIN_ELAPSED_MS = 250   // 首字后不足 250ms 时不出速率（避免除以极小值出现夸张值）
const TICK_INTERVAL_MS = 500 // 与 deepseek-harness TurnStatus 的 1s tick 同数量级但更灵敏

export function useLiveThroughput() {
  const firstDeltaAt = ref(0)
  const charCount = ref(0)
  const nowTick = ref(0)
  const charsPerToken = ref(DEFAULT_CHARS_PER_TOKEN)
  let intervalId = 0

  const running = computed(() => firstDeltaAt.value > 0)
  const tokensPerSecond = computed(() => {
    if (!firstDeltaAt.value) return 0
    const elapsedMs = nowTick.value - firstDeltaAt.value
    if (elapsedMs < MIN_ELAPSED_MS) return 0
    const estTokens = charCount.value / charsPerToken.value
    return estTokens / (elapsedMs / 1000)
  })

  function startTicker() {
    if (intervalId) return
    intervalId = setInterval(() => {
      nowTick.value = performance.now()
    }, TICK_INTERVAL_MS)
  }

  function stopTicker() {
    if (intervalId) { clearInterval(intervalId); intervalId = 0 }
  }

  function record(text) {
    if (!text) return
    if (!firstDeltaAt.value) {
      firstDeltaAt.value = performance.now()
      nowTick.value = firstDeltaAt.value
      startTicker()
    }
    charCount.value += text.length
  }

  function reset() {
    stopTicker()
    firstDeltaAt.value = 0
    charCount.value = 0
    nowTick.value = 0
  }

  // 停 tick 但保留最后一帧估算值——用于 turn_completed 到 finishStream 之间的过渡显示。
  function freeze() {
    stopTicker()
    if (firstDeltaAt.value) nowTick.value = performance.now()
  }

  // 用真实 output_tokens 校准 charsPerToken。异常值（比例过小或过大）忽略。
  function calibrate(actualOutputTokens) {
    const tokens = Number(actualOutputTokens)
    if (!Number.isFinite(tokens) || tokens <= 0 || charCount.value <= 0) return
    const observed = charCount.value / tokens
    if (observed < 0.5 || observed > 10) return
    charsPerToken.value = (charsPerToken.value + observed) / 2
  }

  onScopeDispose(stopTicker)

  return { record, reset, freeze, calibrate, tokensPerSecond, running }
}
