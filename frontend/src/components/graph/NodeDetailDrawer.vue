<script setup>
// 节点详情抽屉（v3.0 §6.3）：右侧滑出，展示实体名 + 关联记忆列表。点击记忆条目派发跳转。
// a11y：对齐 BaseModal 语义 —— role="dialog" + aria-modal + Esc 关闭 + 焦点管理。
// 与 BaseModal 差异：抽屉是非模态（图谱仍可交互），故 aria-modal="false"，且用文档级
// mousedown 关闭而非遮罩。
import { ref, watch, nextTick, onBeforeUnmount } from 'vue'
const props = defineProps({
  entity: { type: Object, default: null }, // {entity_id,name,type,memory_count}
  memories: { type: Array, default: () => [] }, // [{id,title,summary}]
})
const emit = defineEmits(['close', 'open-memory'])

// 点击抽屉以外区域时自动关闭。抽屉为非模态（图谱仍可交互），故用文档级监听而非遮罩层。
const drawerRef = ref(null)
const closeBtnRef = ref(null)
let lastActive = null

function onDocMousedown(e) {
  if (drawerRef.value && !drawerRef.value.contains(e.target)) emit('close')
}
function onKeydown(e) {
  if (e.key === 'Escape') {
    e.stopPropagation()
    emit('close')
  }
}

watch(
  () => props.entity,
  (val, oldVal) => {
    if (val && !oldVal) {
      // 抽屉打开：记住当前焦点，加监听，焦点移入抽屉便于键盘操作
      lastActive = document.activeElement
      document.addEventListener('mousedown', onDocMousedown)
      document.addEventListener('keydown', onKeydown)
      nextTick(() => {
        if (closeBtnRef.value) closeBtnRef.value.focus()
      })
    } else if (!val && oldVal) {
      // 抽屉关闭：撤监听并归还焦点
      document.removeEventListener('mousedown', onDocMousedown)
      document.removeEventListener('keydown', onKeydown)
      if (lastActive && typeof lastActive.focus === 'function') {
        lastActive.focus()
      }
      lastActive = null
    }
  }
)
onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onDocMousedown)
  document.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <transition name="kg-drawer">
    <div
      v-if="entity"
      ref="drawerRef"
      class="kg-drawer"
      role="dialog"
      aria-modal="false"
      :aria-label="entity ? `${entity.name} 节点详情` : '节点详情'"
      tabindex="-1"
    >
      <div class="kg-drawer-head">
        <div class="fg" style="gap: 8px; min-width: 0">
          <span
            class="mt"
            style="margin: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis"
            >{{ entity.name }}</span
          >
          <span class="badge badge-a">{{ entity.type || 'entity' }}</span>
        </div>
        <button
          ref="closeBtnRef"
          class="kg-drawer-x"
          type="button"
          aria-label="关闭节点详情"
          title="关闭（Esc）"
          @click="emit('close')"
        >
          <i class="ti ti-x"></i>
        </button>
      </div>
      <div class="muted" style="margin-bottom: 12px">共 {{ entity.memory_count }} 条关联记忆</div>
      <div class="kg-drawer-body">
        <div
          v-for="m in memories"
          :key="m.id"
          class="cw"
          role="button"
          tabindex="0"
          style="cursor: pointer; padding: 12px"
          @click="emit('open-memory', m.id)"
          @keydown.enter.prevent="emit('open-memory', m.id)"
          @keydown.space.prevent="emit('open-memory', m.id)"
        >
          <b>{{ m.title }}</b>
          <div class="muted">{{ m.summary }}</div>
        </div>
        <div v-if="!memories.length" class="empty" style="padding: 28px 12px">还没有关联记忆</div>
      </div>
    </div>
  </transition>
</template>

<style scoped>
.kg-drawer {
  position: fixed;
  top: 0;
  right: 0;
  width: 400px;
  max-width: 92vw;
  height: 100vh;
  background: var(--surface);
  border-left: 1px solid var(--bd);
  box-shadow: var(--shadow-2);
  z-index: var(--z-drawer);
  padding: 24px;
  overflow: auto;
  display: flex;
  flex-direction: column;
}

.kg-drawer:focus {
  outline: none;
}

.kg-drawer-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}

.kg-drawer-x {
  background: none;
  border: none;
  cursor: pointer;
  font-size: var(--icon-sm);
  color: var(--muted);
}

.kg-drawer-body {
  flex: 1;
  overflow: auto;
}

.kg-drawer-enter-active,
.kg-drawer-leave-active {
  transition: transform 0.3s cubic-bezier(0.2, 0.9, 0.3, 1);
}

.kg-drawer-enter-from,
.kg-drawer-leave-to {
  transform: translateX(100%);
}
</style>
