<script setup>
// handoff 摘要附件组件（会话上下文管理方案 v2）
// 三态：generating / ready / failed
import { handoffStatusLabel } from '@/utils/enumLabel'

defineProps({
  status: { type: String, required: true }, // generating | ready | failed
  data: { type: Object, default: null }, // { summary_tokens, original_turns }
})
const emit = defineEmits(['remove', 'preview'])
</script>

<template>
  <div class="handoff-attach">
    <i
      class="ti"
      :class="
        status === 'generating'
          ? 'ti-loader-2'
          : status === 'ready'
            ? 'ti-file-text'
            : 'ti-alert-triangle'
      "
      :style="{
        color:
          status === 'failed'
            ? 'var(--dangtx)'
            : status === 'ready'
              ? 'var(--succtx)'
              : 'var(--muted)',
      }"
    ></i>
    <span v-if="status === 'generating'" class="handoff-label">
      {{ handoffStatusLabel(status) }}...
    </span>
    <span v-else-if="status === 'ready'" class="handoff-label">
      上一会话摘要
      <span v-if="data && data.summary_tokens" class="muted" style="font-size: var(--fs-xs)">
        · 约 {{ data.summary_tokens }} token
      </span>
    </span>
    <span v-else class="handoff-label dang"> 摘要生成失败 </span>
    <button
      v-if="status === 'ready'"
      type="button"
      class="handoff-btn"
      aria-label="预览上一会话摘要"
      @click="emit('preview')"
    >
      <i class="ti ti-eye"></i> 预览
    </button>
    <button
      v-if="status !== 'generating'"
      type="button"
      class="handoff-btn"
      aria-label="移除上一会话摘要"
      @click="emit('remove')"
    >
      <i class="ti ti-x"></i> 移除
    </button>
  </div>
</template>

<style scoped>
.handoff-attach {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: var(--brand-soft);
  border-radius: var(--radius);
  margin-bottom: 10px;
  font-size: var(--fs-base);
}
.handoff-label {
  flex: 1;
  min-width: 0;
}
.handoff-btn {
  background: transparent;
  border: 1px solid var(--bd-strong);
  border-radius: var(--radius);
  padding: 2px 8px;
  cursor: pointer;
  font-size: var(--fs-sm);
  color: var(--sec);
  display: flex;
  align-items: center;
  gap: 4px;
}
.handoff-btn:hover {
  background: var(--surface-2);
}
</style>
