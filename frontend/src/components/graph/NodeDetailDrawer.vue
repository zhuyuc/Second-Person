<script setup>
// 节点详情抽屉（v3.0 §6.3）：右侧滑出，展示实体名 + 关联记忆列表。点击记忆条目派发跳转。
import { ref, watch, nextTick, onBeforeUnmount } from 'vue'
const props = defineProps({
  entity: { type: Object, default: null },     // {entity_id,name,type,memory_count}
  memories: { type: Array, default: () => [] }, // [{id,title,summary}]
})
const emit = defineEmits(['close', 'open-memory'])

// 点击抽屉以外区域时自动关闭。抽屉为非模态（图谱仍可交互），故用文档级监听而非遮罩层。
const drawerRef = ref(null)
function onDocMousedown(e) {
  if (drawerRef.value && !drawerRef.value.contains(e.target)) emit('close')
}
watch(() => props.entity, (val) => {
  if (val) nextTick(() => document.addEventListener('mousedown', onDocMousedown))
  else document.removeEventListener('mousedown', onDocMousedown)
})
onBeforeUnmount(() => document.removeEventListener('mousedown', onDocMousedown))
</script>

<template>
  <transition name="kg-drawer">
    <div v-if="entity" ref="drawerRef" class="kg-drawer">
      <div class="kg-drawer-head">
        <div class="fg" style="gap:8px;min-width:0">
          <span class="mt" style="margin:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{{ entity.name
            }}</span>
          <span class="badge badge-a">{{ entity.type || 'entity' }}</span>
        </div>
        <button class="kg-drawer-x" @click="emit('close')"><i class="ti ti-x"></i></button>
      </div>
      <div class="muted" style="margin-bottom:12px">共 {{ entity.memory_count }} 条关联记忆</div>
      <div class="kg-drawer-body">
        <div v-for="m in memories" :key="m.id" class="cw" style="cursor:pointer;padding:12px"
          @click="emit('open-memory', m.id)">
          <b>{{ m.title }}</b>
          <div class="muted">{{ m.summary }}</div>
        </div>
        <div v-if="!memories.length" class="empty" style="padding:28px 12px">暂无关联记忆</div>
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
  font-size: 18px;
  color: var(--muted);
}

.kg-drawer-body {
  flex: 1;
  overflow: auto;
}

.kg-drawer-enter-active,
.kg-drawer-leave-active {
  transition: transform .3s cubic-bezier(.2, .9, .3, 1);
}

.kg-drawer-enter-from,
.kg-drawer-leave-to {
  transform: translateX(100%);
}
</style>
