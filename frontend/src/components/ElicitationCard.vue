<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { api } from '@/api/client'
import { elicitationStatusLabel } from '@/utils/enumLabel'

const props = defineProps({
  toolUseId: { type: String, required: true },
  reason: { type: String, default: '' },
  questions: { type: Array, required: true },  // [{id, question, options, allow_custom, required}]
  platform: { type: String, default: 'web' },
})

const emit = defineEmits(['resolved', 'close'])

const currentIndex = ref(0)
const answers = ref(props.questions.map(() => null))  // null | {type:'option',value} | {type:'custom',value}
const isSubmitting = ref(false)
const status = ref('pending')  // pending | submitting | done | closed
const cardFocused = ref(false)

// 摘要文字
const summaryText = computed(() => {
  const answered = answers.value.filter(Boolean).length
  if (status.value === 'done') return `已回答 ${answered} 项`
  if (status.value === 'closed') return `已关闭追问（保留 ${answered} 项已答）`
  return ''
})

// 当前题
const currentQ = computed(() => props.questions[currentIndex.value] || null)

// 自动切题定时器
let autoTimer = null

function selectOption(optIndex) {
  if (isSubmitting.value || status.value !== 'pending') return
  const q = currentQ.value
  if (!q) return
  answers.value[currentIndex.value] = { questionId: q.id, type: 'option', value: q.options[optIndex] }
  scheduleNext()
}

function submitCustom(text) {
  if (isSubmitting.value || status.value !== 'pending' || !text.trim()) return
  const q = currentQ.value
  if (!q) return
  answers.value[currentIndex.value] = { questionId: q.id, type: 'custom', value: text.trim() }
  scheduleNext()
}

function scheduleNext() {
  clearTimeout(autoTimer)
  if (currentIndex.value >= props.questions.length - 1) {
    // 最后一题 → 提交
    submitAll()
  } else {
    autoTimer = setTimeout(() => {
      currentIndex.value++
    }, 300)
  }
}

function skipQuestion() {
  if (isSubmitting.value || status.value !== 'pending') return
  clearTimeout(autoTimer)
  if (currentIndex.value >= props.questions.length - 1) {
    submitAll()
  } else {
    currentIndex.value++
  }
}

function goPrev() {
  if (currentIndex.value > 0 && !isSubmitting.value) {
    clearTimeout(autoTimer)
    currentIndex.value--
  }
}

async function submitAll() {
  if (isSubmitting.value || status.value !== 'pending') return
  isSubmitting.value = true
  status.value = 'submitting'
  try {
    const ans = answers.value.filter(Boolean)
    await api.post(`/chat/elicitations/${props.toolUseId}/answer`, { answers: ans })
    status.value = 'done'
    emit('resolved', { answers: ans })
  } catch {
    isSubmitting.value = false
    status.value = 'pending'
  }
}

async function handleClose() {
  if (isSubmitting.value) return
  const partial = answers.value.filter(Boolean)
  try {
    await api.post(`/chat/elicitations/${props.toolUseId}/close`, { answers: partial })
  } catch { /* ignore */ }
  status.value = 'closed'
  emit('close', { answers: partial })
}

// 快捷键
function onKeydown(e) {
  if (!cardFocused.value) return
  if (status.value !== 'pending') return
  const num = parseInt(e.key)
  if (num >= 1 && num <= 4 && currentQ.value?.options[num - 1]) {
    e.preventDefault()
    selectOption(num - 1)
    return
  }
  if (e.key === 'Enter') {
    e.preventDefault()
    if (currentIndex.value >= props.questions.length - 1) {
      submitAll()
    } else if (answers.value[currentIndex.value]) {
      clearTimeout(autoTimer)
      currentIndex.value++
    }
    return
  }
  if (e.key === 'Tab') {
    e.preventDefault()
    skipQuestion()
    return
  }
  if (e.key === 'Escape') {
    e.preventDefault()
    handleClose()
    return
  }
}

onMounted(() => {
  document.addEventListener('keydown', onKeydown)
})
onUnmounted(() => {
  document.removeEventListener('keydown', onKeydown)
  clearTimeout(autoTimer)
})
</script>

<template>
  <div class="elicitation-card" :class="{ submitting: isSubmitting, done: status === 'done', closed: status === 'closed' }"
    tabindex="0" @focus="cardFocused = true" @blur="cardFocused = false">
    <!-- 头部 -->
    <div class="ec-header">
      <div class="ec-progress">
        <div class="ec-progress-bar" :style="{ width: ((currentIndex + 1) / questions.length * 100) + '%' }"></div>
      </div>
      <span class="ec-count">{{ currentIndex + 1 }} / {{ questions.length }}</span>
      <button class="ec-close" @click="handleClose" :disabled="isSubmitting" title="关闭追问 (Esc)">
        <i class="ti ti-x"></i>
      </button>
    </div>

    <!-- 完成/关闭摘要 -->
    <div v-if="status === 'done' || status === 'closed'" class="ec-summary">
      <i class="ti" :class="status === 'done' ? 'ti-check' : 'ti-x'"></i>
      {{ summaryText }}
    </div>

    <!-- 主体 -->
    <template v-if="status === 'pending' || status === 'submitting'">
      <div class="ec-body" :class="{ fading: isSubmitting }">
        <h3 class="ec-question">{{ currentQ?.question }}</h3>
        <p v-if="currentQ?.description" class="ec-desc">{{ currentQ.description }}</p>
        <div class="ec-options">
          <button v-for="(opt, oi) in currentQ?.options || []" :key="oi"
            class="ec-opt"
            :class="{ active: answers[currentIndex]?.type === 'option' && answers[currentIndex]?.value === opt }"
            @click="selectOption(oi)" :disabled="isSubmitting">
            {{ oi + 1 }}. {{ opt }}
          </button>
        </div>
        <div v-if="currentQ?.allow_custom !== false" class="ec-custom">
          <input type="text" :placeholder="'自行输入（字数限制 200）'"
            :maxlength="200"
            :value="answers[currentIndex]?.type === 'custom' ? answers[currentIndex]?.value : ''"
            @keydown.enter.prevent="submitCustom($event.target.value)"
            @input="(e) => { if (e.target.value) answers[currentIndex] = { questionId: currentQ.id, type: 'custom', value: e.target.value } }"
            :disabled="isSubmitting"
            class="ec-custom-input" />
          <button class="ec-custom-btn" @click="submitCustom($event.target.previousElementSibling.value)"
            :disabled="isSubmitting">
            <i class="ti ti-arrow-right"></i>
          </button>
        </div>
      </div>

      <!-- 底部导航 -->
      <div class="ec-footer">
        <button v-if="currentIndex > 0" class="ec-prev" @click="goPrev" :disabled="isSubmitting">
          <i class="ti ti-chevron-left"></i> 上一题
        </button>
        <span v-else></span>
        <button class="ec-skip" @click="skipQuestion" :disabled="isSubmitting">
          {{ currentIndex >= questions.length - 1 ? '跳过并提交' : '跳过' }} <i class="ti ti-chevron-right"></i>
        </button>
      </div>
    </template>

    <!-- 快捷提示 -->
    <div class="ec-shortcuts" v-if="status === 'pending' && cardFocused">
      <span>数字键 1-4 快速选择</span>
      <span>Tab 跳过 / Enter 下一题 / Esc 关闭</span>
    </div>
  </div>
</template>

<style scoped>
.elicitation-card {
  background: var(--surface);
  border: 1px solid var(--bd);
  border-radius: var(--radius);
  padding: var(--sp-4);
  margin: var(--sp-3) 0;
  outline: none;
  transition: opacity var(--dur-slow);
}
.elicitation-card:focus {
  border-color: var(--acc);
  box-shadow: 0 0 0 2px var(--accbg);
}
.elicitation-card.submitting {
  opacity: 0.7;
  pointer-events: none;
}
.elicitation-card.done,
.elicitation-card.closed {
  opacity: 0.85;
  border-color: var(--bd);
}

.ec-header {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-3);
}
.ec-progress {
  flex: 1;
  height: 3px;
  background: var(--surface-2);
  border-radius: var(--radius-pill);
  overflow: hidden;
}
.ec-progress-bar {
  height: 100%;
  background: var(--acc);
  border-radius: var(--radius-pill);
  transition: width var(--dur-slow);
}
.ec-count {
  font-size: var(--fs-xs);
  color: var(--muted);
  white-space: nowrap;
}
.ec-close {
  background: none;
  border: none;
  color: var(--muted);
  cursor: pointer;
  padding: 2px;
  border-radius: var(--radius-xs);
  transition: color var(--dur-fast), background var(--dur-fast);
}
.ec-close:hover {
  color: var(--fg);
  background: var(--surface-2);
}

.ec-summary {
  padding: var(--sp-2) 0;
  font-size: var(--fs-sm);
  color: var(--sec);
  display: flex;
  align-items: center;
  gap: var(--sp-1);
}

.ec-body { transition: opacity var(--dur-slow); }
.ec-body.fading { opacity: 0.6; }

.ec-question {
  font-size: var(--fs-md);
  font-weight: 600;
  margin-bottom: var(--sp-1);
  color: var(--fg);
}
.ec-desc {
  font-size: var(--fs-sm);
  color: var(--sec);
  margin-bottom: var(--sp-3);
}

.ec-options {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.ec-opt {
  width: 100%;
  text-align: left;
  padding: var(--sp-3) var(--sp-4);
  background: var(--surface);
  border: 1px solid var(--bd);
  border-radius: var(--radius-sm);
  font-size: var(--fs-base);
  cursor: pointer;
  transition: background var(--dur-fast), border-color var(--dur-fast);
  min-height: 44px;
}
.ec-opt:hover {
  background: var(--surface-2);
}
.ec-opt.active {
  border-color: var(--acc);
  background: var(--accbg);
  color: var(--acctx);
}
.ec-opt:disabled {
  cursor: default;
  opacity: 0.6;
}

.ec-custom {
  display: flex;
  gap: var(--sp-1);
  margin-top: var(--sp-3);
}
.ec-custom-input {
  flex: 1;
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--bd);
  border-radius: var(--radius-sm);
  font-size: var(--fs-base);
  background: var(--surface);
  color: var(--fg);
  outline: none;
  transition: border-color .18s;
  min-height: 40px;
}
.ec-custom-input:focus {
  border-color: var(--acc);
}
.ec-custom-input:disabled {
  background: var(--surface-2);
}
.ec-custom-btn {
  background: var(--acc);
  color: #fff;
  border: none;
  border-radius: var(--radius-sm);
  padding: 0 var(--sp-3);
  cursor: pointer;
  transition: opacity var(--dur-fast);
}
.ec-custom-btn:hover { opacity: 0.85; }
.ec-custom-btn:disabled { opacity: 0.4; cursor: default; }

.ec-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: var(--sp-4);
}
.ec-prev {
  background: none;
  border: none;
  color: var(--muted);
  cursor: pointer;
  font-size: var(--fs-sm);
  display: flex;
  align-items: center;
  gap: 2px;
  padding: var(--sp-1) 0;
  transition: color var(--dur-fast);
}
.ec-prev:hover { color: var(--fg); }
.ec-prev:disabled { opacity: 0.4; cursor: default; }
.ec-skip {
  background: none;
  border: none;
  color: var(--muted);
  cursor: pointer;
  font-size: var(--fs-sm);
  display: flex;
  align-items: center;
  gap: 2px;
  padding: var(--sp-1) 0;
  transition: color var(--dur-fast);
}
.ec-skip:hover { color: var(--fg); }
.ec-skip:disabled { opacity: 0.4; cursor: default; }

.ec-shortcuts {
  display: flex;
  gap: var(--sp-4);
  margin-top: var(--sp-2);
  font-size: var(--fs-xs);
  color: var(--muted);
  justify-content: flex-end;
}
</style>
