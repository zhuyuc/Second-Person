<script setup>
import { ref } from 'vue'

// 只做定位工具：与右侧对话状态完全解耦——没有 active/streaming 联动，
// 也不追踪滚动位置。默认统一虚化，只在鼠标悬停/键盘聚焦时高亮并弹出标题。
defineProps({
  anchors: { type: Array, default: () => [] },
})

const emit = defineEmits(['select'])
const mobileOpen = ref(false)

function select(anchor) {
  mobileOpen.value = false
  emit('select', anchor)
}

function toggleMobile() {
  mobileOpen.value = !mobileOpen.value
}
</script>

<template>
  <aside class="message-anchor-rail" aria-label="对话定位">
    <template v-if="anchors.length">
      <button
        class="message-anchor-mobile-toggle"
        type="button"
        aria-label="打开对话定位"
        title="对话定位"
        :aria-expanded="mobileOpen"
        @click="toggleMobile"
      >
        <i class="ti ti-list"></i>
      </button>

      <div class="message-anchor-track" role="list">
        <button
          v-for="anchor in anchors"
          :key="anchor.key"
          type="button"
          role="listitem"
          class="message-anchor-mark"
          :aria-label="anchor.title"
          @click="select(anchor)"
        >
          <span class="message-anchor-line"></span>
          <span class="message-anchor-tooltip" role="tooltip" tabindex="-1">{{
            anchor.title
          }}</span>
        </button>
      </div>

      <div
        v-if="mobileOpen"
        class="message-anchor-mobile-list"
        role="list"
        @keydown.esc="mobileOpen = false"
      >
        <button
          v-for="anchor in anchors"
          :key="anchor.key"
          type="button"
          role="listitem"
          class="message-anchor-mobile-item"
          @click="select(anchor)"
        >
          <span class="message-anchor-mobile-index">{{ anchor.index }}</span>
          <span>{{ anchor.title }}</span>
        </button>
      </div>
    </template>
  </aside>
</template>

<style scoped>
/* 独立左列：固定宽度，不随对话内容宽度变化；纵向填满，居中承载锚点轨道 */
.message-anchor-rail {
  position: relative;
  flex: 0 0 56px;
  align-self: stretch;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 40px 0;
  pointer-events: none;
}

/* 锚点轨道：flex 均分排列——不再按滚动百分比动态定位，标记不随滚动上下滑 */
.message-anchor-track {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 1.5px;
  max-height: 100%;
  padding: 8px 0;
  pointer-events: auto;
}

.message-anchor-mark {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 6px;
  padding: 0;
  border: 0;
  background: transparent;
  cursor: pointer;
}

/* 默认样式：短细虚化横线（参考 Codex 侧边定位） */
.message-anchor-line {
  display: block;
  width: 9px;
  height: 1.5px;
  border-radius: 2px;
  background: var(--muted);
  opacity: 0.32;
  transition:
    width var(--dur-fast),
    height var(--dur-fast),
    background var(--dur-fast),
    opacity var(--dur-fast);
}

/* 唯一的高亮触发条件：鼠标悬停或键盘聚焦 */
.message-anchor-mark:hover .message-anchor-line,
.message-anchor-mark:focus-visible .message-anchor-line {
  width: 19px;
  height: 2px;
  background: var(--fg);
  opacity: 0.95;
}

/* 悬停提示：右侧显示对话标题（Codex 风格：暗色圆角气泡） */
.message-anchor-tooltip {
  position: absolute;
  left: 44px;
  top: 50%;
  z-index: var(--z-menu);
  width: max-content;
  max-width: min(320px, calc(100vw - 96px));
  padding: 10px 14px;
  border-radius: var(--radius);
  background: var(--surface-2);
  color: var(--fg);
  box-shadow: var(--shadow-2);
  border: 1px solid var(--bd-strong);
  font-size: var(--fs-sm);
  line-height: 1.5;
  text-align: left;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
  opacity: 0;
  pointer-events: none;
  transform: translate(0, -50%) scale(0.96);
  transform-origin: left center;
  transition:
    opacity var(--dur-fast),
    transform var(--dur-fast);
}

.message-anchor-mark:hover .message-anchor-tooltip,
.message-anchor-mark:focus-visible .message-anchor-tooltip {
  opacity: 1;
  pointer-events: auto;
  transform: translate(0, -50%) scale(1);
}

.message-anchor-mark:focus-visible,
.message-anchor-mobile-toggle:focus-visible,
.message-anchor-mobile-item:focus-visible {
  outline: 2px solid var(--brand-solid);
  outline-offset: 3px;
}

.message-anchor-mobile-toggle,
.message-anchor-mobile-list {
  display: none;
}

/* 窄屏：转为触发按钮 + 弹出列表 */
@media (max-width: 1100px) {
  .message-anchor-rail {
    flex: 0 0 44px;
    padding: 12px 0;
    align-items: flex-start;
  }

  .message-anchor-track {
    display: none;
  }

  .message-anchor-mobile-toggle {
    display: inline-flex;
    width: 40px;
    height: 40px;
    align-items: center;
    justify-content: center;
    padding: 0;
    border: 1px solid transparent;
    border-radius: 50%;
    background: transparent;
    color: var(--muted);
    box-shadow: none;
    opacity: 0.42;
    cursor: pointer;
    pointer-events: auto;
  }

  .message-anchor-mobile-toggle:hover,
  .message-anchor-mobile-toggle:focus-visible {
    color: var(--fg);
    background: var(--surface-2);
    border-color: var(--bd-strong);
    box-shadow: var(--shadow-1);
    opacity: 1;
  }

  .message-anchor-mobile-list {
    position: absolute;
    top: 48px;
    left: 0;
    right: auto;
    display: flex;
    width: min(280px, calc(100vw - 32px));
    max-height: 52vh;
    flex-direction: column;
    gap: 2px;
    padding: 6px;
    overflow-y: auto;
    border: 1px solid var(--bd-strong);
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--surface) 94%, var(--brand-soft));
    box-shadow: var(--shadow-2);
    pointer-events: auto;
  }

  .message-anchor-mobile-item {
    display: flex;
    align-items: baseline;
    gap: 8px;
    width: 100%;
    padding: 8px 10px;
    border: 0;
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--sec);
    font-size: var(--fs-sm);
    line-height: 1.4;
    text-align: left;
    cursor: pointer;
  }

  .message-anchor-mobile-item:hover,
  .message-anchor-mobile-item:focus-visible {
    background: var(--brand-soft);
    color: var(--fg);
  }

  .message-anchor-mobile-index {
    flex: 0 0 auto;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }
}
</style>
