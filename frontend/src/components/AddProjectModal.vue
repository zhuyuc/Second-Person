<script setup>
// 添加项目：调后端 tkinter.filedialog 弹**系统原生**文件夹对话框。
// 拿到绝对路径后弹小 modal 让用户确认项目名即可。
import { ref, onMounted, nextTick } from 'vue'
import { projectsApi } from '@/api/projects'
import { useToast } from '@/stores/toast'
import BaseModal from '@/components/BaseModal.vue'

const emit = defineEmits(['close', 'created'])
const toast = useToast()

const step = ref('picking')         // picking | confirming
const pickedPath = ref('')
const projectTitle = ref('')
const creating = ref(false)

async function pickNative() {
  step.value = 'picking'
  try {
    const d = await projectsApi.browseNative()
    if (d.cancelled || !d.path) {
      emit('close')
      return
    }
    pickedPath.value = d.path
    projectTitle.value = d.name
    step.value = 'confirming'
    nextTick(() => {
      const el = document.getElementById('add-proj-title-input')
      if (el) { el.focus(); el.select() }
    })
  } catch (e) {
    // 后端拉起原生对话框失败（tkinter 不可用/服务器无显示器）
    toast.push('error', '无法拉起系统对话框，请更新程序或直接粘贴路径')
    emit('close')
  }
}

async function submit() {
  if (!pickedPath.value) return
  creating.value = true
  try {
    const proj = await projectsApi.create({
      path: pickedPath.value,
      title: projectTitle.value.trim() || undefined,
    })
    emit('created', proj)
  } catch { /* toast 已弹 */ }
  finally { creating.value = false }
}

onMounted(pickNative)
</script>

<template>
  <BaseModal v-if="step === 'confirming'"
             title="确认加入工作区"
             size="md"
             @close="emit('close')">
    <div class="confirm-body">
      <div class="row-line">
        <label>目录路径</label>
        <div class="path-box" :title="pickedPath">{{ pickedPath }}</div>
      </div>
      <div class="row-line">
        <label>项目名</label>
        <input id="add-proj-title-input"
               v-model="projectTitle"
               maxlength="60"
               placeholder="默认使用目录名"
               @keydown.enter.prevent="submit" />
      </div>
      <div class="muted tip">
        项目加载后可在侧栏「工作区」快速切换。归档 / 永久删除随时可做，本地目录不会被动。
      </div>
    </div>
    <template #footer>
      <button type="button" @click="emit('close')">取消</button>
      <button type="button" class="btn-primary" :disabled="creating" @click="submit">
        {{ creating ? '加载中…' : '打开' }}
      </button>
    </template>
  </BaseModal>
</template>

<style scoped>
.confirm-body { display: flex; flex-direction: column; gap: 14px; }
.row-line { display: flex; align-items: center; gap: 12px; }
.row-line label { width: 68px; color: var(--muted); font-size: var(--fs-sm, 13px); }
.path-box {
  flex: 1; min-width: 0;
  padding: 8px 10px;
  background: var(--bg-input, rgba(127,127,127,0.06));
  border: 1px solid var(--stroke); border-radius: 4px;
  font-family: monospace; font-size: 12px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.row-line input {
  flex: 1; padding: 6px 10px;
  border: 1px solid var(--stroke); border-radius: 4px;
  font-family: inherit;
}
.tip { font-size: 12px; line-height: 1.6; }
</style>
