<script setup>
// 沙箱档位切换 chip（v6 沙箱下沉到会话层）：所有会话可用
import { ref, computed, watch, onMounted } from 'vue'
import { projectsApi } from '@/api/projects'
import { useToast } from '@/stores/toast'
import { useConfirm } from '@/stores/confirm'

const props = defineProps({
  sessionId: { type: String, default: null },
  // 保留 hasProject 供上层做文案微调（如 workspace-write 时"项目根" vs "工作目录"）
  hasProject: { type: Boolean, default: false },
  // 无会话（新对话）时的展示档位；父组件可按项目默认值计算
  fallbackMode: { type: String, default: 'workspace-write' },
})
const emit = defineEmits(['pending-change'])
const toast = useToast()
const confirm = useConfirm()

const mode = ref(null)
const source = ref(null)
const menuOpen = ref(false)

const MODES = computed(() => [
  {
    value: 'read-only',
    label: '只读',
    icon: 'ti-lock',
    desc: '模型只能读文件；写/编辑/shell 全拒',
  },
  {
    value: 'workspace-write',
    label: props.hasProject ? '项目可写' : '工作区可写',
    icon: 'ti-pencil',
    desc: props.hasProject
      ? '模型可读写项目根内文件；shell 拒'
      : '模型可读写 data/workspace/ 内文件；shell 拒',
  },
  {
    value: 'danger-full-access',
    label: '⚠ 全盘可写',
    icon: 'ti-alert-triangle',
    desc: '模型可读写全盘（黑名单除外）；允许 shell。请谨慎使用。',
  },
])

const currentLabel = computed(
  () => MODES.value.find((m) => m.value === mode.value)?.label || mode.value || '未知'
)
const currentIcon = computed(
  () => MODES.value.find((m) => m.value === mode.value)?.icon || 'ti-shield'
)

async function load() {
  if (!props.sessionId) {
    mode.value = props.fallbackMode
    source.value = null
    return
  }
  try {
    const d = await projectsApi.getSandboxMode(props.sessionId)
    mode.value = d.mode
    source.value = d.source
  } catch {
    mode.value = null
  }
}

watch(() => props.sessionId, load)
watch(() => props.hasProject, load)
watch(
  () => props.fallbackMode,
  () => {
    if (!props.sessionId) mode.value = props.fallbackMode
  }
)
onMounted(load)

async function switchTo(m) {
  menuOpen.value = false
  if (m === mode.value) return
  if (m === 'danger-full-access') {
    const ok = await confirm.ask({
      title: '切换到全盘可写档位',
      message: '⚠ 该档位允许模型读写全盘文件、执行 shell 命令。仅在你完全信任本次任务范围时启用。',
      confirmText: '我理解，切换',
      cancelText: '取消',
      danger: true,
    })
    if (!ok) return
  }
  // 新对话（会话未建）：仅本地记录，等首条消息建会话后由父组件落库
  if (!props.sessionId) {
    mode.value = m
    emit('pending-change', m)
    return
  }
  try {
    await projectsApi.setSandboxMode(props.sessionId, m)
    mode.value = m
    source.value = 'session'
    toast.push('success', `沙箱档位已切换为「${MODES.value.find((x) => x.value === m).label}」`)
  } catch {
    /* toast 已弹 */
  }
}
</script>

<template>
  <div
    v-if="mode"
    class="sandbox-chip"
    :class="{ 'is-danger': mode === 'danger-full-access', 'is-readonly': mode === 'read-only' }"
    @click="menuOpen = !menuOpen"
  >
    <i class="ti" :class="currentIcon"></i>
    <span class="chip-label">沙箱：{{ currentLabel }}</span>
    <i class="ti ti-chevron-down chip-caret"></i>
    <div v-if="menuOpen" class="chip-menu" @click.stop>
      <div
        v-for="m in MODES"
        :key="m.value"
        class="menu-item"
        :class="{ active: m.value === mode }"
        @click="switchTo(m.value)"
      >
        <i class="ti" :class="m.icon"></i>
        <div>
          <div class="menu-label">{{ m.label }}</div>
          <div class="menu-desc">{{ m.desc }}</div>
        </div>
      </div>
    </div>
    <div v-if="menuOpen" class="chip-mask" @click.stop="menuOpen = false"></div>
  </div>
</template>

<style scoped>
.sandbox-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 6px;
  background: transparent;
  border: none;
  border-radius: 8px;
  font-size: 12px;
  color: var(--muted);
  position: relative;
  cursor: pointer;
  user-select: none;
}
.sandbox-chip:hover {
  background: var(--bg-hover, rgba(127, 127, 127, 0.08));
}
.sandbox-chip.is-danger {
  color: var(--dangtx, #c02020);
}
.sandbox-chip.is-readonly {
  color: var(--muted);
  opacity: 0.7;
}
.chip-label {
  color: var(--fg);
  font-weight: 500;
}
.sandbox-chip.is-danger .chip-label {
  color: var(--dangtx);
}
.chip-caret {
  padding: 0 2px;
}
.chip-menu {
  position: absolute;
  bottom: 100%;
  left: 0;
  margin-bottom: 4px;
  width: 320px;
  background: var(--bg-elev, var(--bg));
  border: 1px solid var(--stroke);
  border-radius: 6px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
  z-index: var(--z-menu);
  padding: 4px;
}
.menu-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 8px 10px;
  cursor: pointer;
  border-radius: 4px;
}
.menu-item:hover {
  background: var(--bg-hover, rgba(127, 127, 127, 0.08));
}
.menu-item.active {
  background: var(--acctx-bg, rgba(60, 120, 220, 0.15));
}
.menu-item .ti {
  padding-top: 2px;
}
.menu-label {
  font-weight: 500;
  margin-bottom: 2px;
  color: var(--fg);
}
.menu-desc {
  font-size: 12px;
  color: var(--muted);
  line-height: 1.4;
}
.chip-mask {
  position: fixed;
  inset: 0;
  z-index: calc(var(--z-menu) - 1);
}
</style>
