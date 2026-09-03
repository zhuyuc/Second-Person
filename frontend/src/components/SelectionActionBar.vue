<script setup>
// 消息气泡文字选中后浮出的操作条：复制 / 引用
// 定位规则：优先贴在选区上方；上方不够则贴选区下方；左右居中于选区但夹在
// 视窗内。fixed 定位以逃出消息列表容器的 overflow 裁剪。
import { computed } from 'vue'

const props = defineProps({
  visible: { type: Boolean, default: false },
  rect: { type: Object, default: null },
  // 是否显示「侧边会话」按钮。侧边会话内部的选区不再套娃，故其实例传 false。
  showAside: { type: Boolean, default: true },
})
const emit = defineEmits(['copy', 'quote', 'aside'])

// 估算的 toolbar 尺寸（真实尺寸由 CSS 决定；这里用于避免超出视窗）
const TOOLBAR_WIDTH = props.showAside ? 252 : 168
const TOOLBAR_HEIGHT = 36
const GAP = 8
const EDGE = 8

const pos = computed(() => {
  const r = props.rect
  if (!r) return { top: 0, left: 0 }
  const canPlaceAbove = r.top - TOOLBAR_HEIGHT - GAP >= EDGE
  const top = canPlaceAbove ? r.top - TOOLBAR_HEIGHT - GAP : r.bottom + GAP
  const centerX = r.left + r.width / 2 - TOOLBAR_WIDTH / 2
  const left = Math.max(EDGE, Math.min(centerX, window.innerWidth - TOOLBAR_WIDTH - EDGE))
  return { top, left }
})
</script>

<template>
  <transition name="fade">
    <div
      v-if="visible"
      class="selection-actionbar"
      data-selection-actionbar
      :style="{ top: pos.top + 'px', left: pos.left + 'px' }"
      @mousedown.prevent
      @click.stop
    >
      <button type="button" class="sab-btn" title="复制选中文字" @click="emit('copy')">
        <i class="ti ti-copy"></i><span>复制</span>
      </button>
      <span class="sab-sep"></span>
      <button
        type="button"
        class="sab-btn"
        title="把选中文字作为引用添加到输入框"
        @click="emit('quote')"
      >
        <i class="ti ti-quote"></i><span>引用</span>
      </button>
      <template v-if="showAside">
        <span class="sab-sep"></span>
        <button
          type="button"
          class="sab-btn"
          title="就这段文字开一个侧边会话（不进主对话、不进列表）"
          @click="emit('aside')"
        >
          <i class="ti ti-message-plus"></i><span>侧边会话</span>
        </button>
      </template>
    </div>
  </transition>
</template>
