<script setup>
// 引用附件的"评论录入"弹窗：显示被选中的原文预览 + 一个可选的评论输入框；
// 用户点确认后由父组件把 {comment} 拼进 quote 附件。基于 BaseModal 复用统一的
// 遮罩/ESC/焦点归还能力（与其它系统弹窗视觉一致）。
import { ref, computed, watch, nextTick } from 'vue'

const props = defineProps({
  // 打开时展示的引用原文；null/'' 表示弹窗关闭
  quoteText: { type: String, default: '' },
  visible: { type: Boolean, default: false },
})
const emit = defineEmits(['confirm', 'cancel'])

const comment = ref('')
const expanded = ref(false)
const inputEl = ref(null)

// 预览默认折叠前 3 行；> 3 行时展示"展开"按钮
const PREVIEW_LINES = 3
const previewLines = computed(() => (props.quoteText || '').split('\n'))
const isLong = computed(
  () => previewLines.value.length > PREVIEW_LINES || (props.quoteText || '').length > 240
)
const previewText = computed(() => {
  if (expanded.value || !isLong.value) return props.quoteText || ''
  return previewLines.value.slice(0, PREVIEW_LINES).join('\n')
})

// 每次开启：清空评论、重置折叠、聚焦输入框
watch(
  () => props.visible,
  (v) => {
    if (!v) return
    comment.value = ''
    expanded.value = false
    nextTick(() => {
      inputEl.value?.focus()
    })
  }
)

function onCancel() {
  emit('cancel')
}
function onConfirm() {
  emit('confirm', { comment: comment.value.trim() })
}
// Enter 提交，Shift+Enter 换行
function onKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    onConfirm()
  }
}
</script>

<template>
  <BaseModal v-if="visible" title="添加引用评论" size="sm" @close="onCancel">
    <div class="quote-composer-preview" :class="{ collapsed: !expanded && isLong }">
      <pre>{{ previewText }}</pre>
      <button v-if="isLong" type="button" class="quote-expand-btn" @click="expanded = !expanded">
        {{ expanded ? '收起' : '展开全部' }}
      </button>
    </div>
    <label class="quote-composer-label">评论（可选）</label>
    <textarea
      ref="inputEl"
      v-model="comment"
      rows="3"
      class="quote-composer-input"
      placeholder="对这段引用说点什么？留空也可以直接确认"
      @keydown="onKeydown"
    ></textarea>
    <template #footer>
      <button type="button" @click="onCancel">取消</button>
      <button type="button" class="btn-primary" @click="onConfirm">确认</button>
    </template>
  </BaseModal>
</template>
