<script setup>
// 交错时间线：把 reasoning 段落与 tool_call 卡片按到达顺序穿插渲染，
// 用户看到"想 → 调 → 想 → 调"的因果链而不是分区静态视图。
// 数据形状（analysis_metadata.timeline 或 live timeline ref）：
//   { kind: 'reasoning', text: '...' }
//   { kind: 'tool_call', name, arguments, status: running|ok|fail,
//     result_preview?, error?, call_id? }
import { computed } from 'vue'

const props = defineProps({
  items: { type: Array, default: () => [] },
  // 是否正在流式（末尾 reasoning 段展示光标提示）
  live: { type: Boolean, default: false },
})

const rendered = computed(() => Array.isArray(props.items) ? props.items : [])

// 参数格式化：JSON 尝试解析成 key=value 简写
function fmtArgs(raw) {
  if (!raw) return ''
  try {
    const obj = typeof raw === 'string' ? JSON.parse(raw) : raw
    if (obj && typeof obj === 'object' && !Array.isArray(obj)) {
      const parts = Object.entries(obj).slice(0, 3).map(([k, v]) => {
        const s = typeof v === 'string' ? v : JSON.stringify(v)
        return `${k}=${s.length > 40 ? s.slice(0, 40) + '…' : s}`
      })
      return parts.join(' ')
    }
    return String(raw).slice(0, 120)
  } catch {
    return String(raw).slice(0, 120)
  }
}

function statusIcon(status) {
  return { running: 'ti-loader-2', ok: 'ti-check', fail: 'ti-x' }[status] || 'ti-circle'
}
function statusLabel(status) {
  return { running: '执行中', ok: '完成', fail: '失败' }[status] || status
}
</script>

<template>
  <div class="think-timeline">
    <template v-for="(item, idx) in rendered" :key="idx">
      <!-- Reasoning 段落 -->
      <div v-if="item.kind === 'reasoning'" class="tl-reasoning">
        <span>{{ item.text }}</span>
        <span v-if="live && idx === rendered.length - 1" class="tl-cursor">▍</span>
      </div>
      <!-- 工具步旁白：从正文撤回的"模型说了什么"，留存展示 -->
      <div v-else-if="item.kind === 'narration'" class="tl-narration">
        <span>{{ item.text }}</span>
      </div>
      <!-- Tool 调用卡片 -->
      <div v-else-if="item.kind === 'tool_call'"
           class="tl-tool"
           :class="`is-${item.status || 'running'}`">
        <div class="tl-tool-head">
          <i class="ti" :class="statusIcon(item.status)"
             :style="item.status === 'running' ? { animation: 'tl-spin 1s linear infinite' } : {}"></i>
          <span class="tl-tool-name">{{ item.name }}</span>
          <span v-if="fmtArgs(item.arguments)" class="tl-tool-args">{{ fmtArgs(item.arguments) }}</span>
          <span class="tl-tool-status">{{ statusLabel(item.status) }}</span>
        </div>
        <div v-if="item.result_preview" class="tl-tool-result">{{ item.result_preview }}</div>
        <div v-if="item.error" class="tl-tool-error">错误：{{ item.error }}</div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.think-timeline {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

/* Reasoning 段：正文样式，主要视觉权重 */
.tl-reasoning {
  color: var(--fg);
  font-size: 12.5px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}
/* 工具步旁白：模型"说出口"的话，斜体稍弱化，与内部 reasoning 区分 */
.tl-narration {
  color: var(--fg);
  opacity: 0.82;
  font-style: italic;
  font-size: 12.5px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}
.tl-cursor {
  display: inline-block;
  margin-left: 1px;
  color: var(--acctx, #3c78dc);
  animation: tl-blink 1s steps(2, start) infinite;
}
@keyframes tl-blink { to { visibility: hidden; } }
@keyframes tl-spin { to { transform: rotate(360deg); } }

/* Tool 调用卡片：与 reasoning 视觉区分 */
.tl-tool {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 10px;
  border-radius: 8px;
  background: var(--bg-input, rgba(127, 127, 127, 0.06));
  border-left: 3px solid var(--muted);
  font-size: 12px;
}
.tl-tool.is-running {
  border-left-color: var(--acctx, #3c78dc);
  background: rgba(60, 120, 220, 0.06);
}
.tl-tool.is-ok {
  border-left-color: var(--succtx, #28b478);
  background: rgba(40, 180, 120, 0.05);
}
.tl-tool.is-fail {
  border-left-color: var(--dangtx, #c83c3c);
  background: rgba(200, 60, 60, 0.06);
}

.tl-tool-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.tl-tool-head > .ti {
  font-size: 14px;
  flex-shrink: 0;
}
.tl-tool.is-running .tl-tool-head > .ti { color: var(--acctx, #3c78dc); }
.tl-tool.is-ok .tl-tool-head > .ti { color: var(--succtx, #28b478); }
.tl-tool.is-fail .tl-tool-head > .ti { color: var(--dangtx, #c83c3c); }

.tl-tool-name {
  font-family: var(--font-mono, ui-monospace, Menlo, Consolas, monospace);
  font-weight: 600;
  color: var(--fg);
}
.tl-tool-args {
  color: var(--muted);
  font-family: var(--font-mono, ui-monospace, Menlo, Consolas, monospace);
  font-size: 11.5px;
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tl-tool-status {
  color: var(--muted);
  font-size: 11.5px;
  margin-left: auto;
  flex-shrink: 0;
}
.tl-tool-result {
  color: var(--muted);
  font-size: 11.5px;
  line-height: 1.5;
  padding-left: 20px;
  overflow-wrap: anywhere;
}
.tl-tool-error {
  color: var(--dangtx, #c83c3c);
  font-size: 11.5px;
  line-height: 1.5;
  padding-left: 20px;
}
</style>
