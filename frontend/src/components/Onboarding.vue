<script setup>
import { ref } from 'vue'
import { onboardingApi } from '@/api/onboarding'
import { settingsApi } from '@/api/settings'
import { soulApi } from '@/api/soul'
import { useToast } from '@/stores/toast'
import { useBusy } from '@/composables/useBusy'

const emit = defineEmits(['done'])
const toast = useToast()
const { busy, run } = useBusy()
const step = ref(1)
const chat = ref({
  provider_type: 'openai_compatible',
  display_name: '',
  base_url: '',
  api_key: '',
  model_id: '',
  context_window: 128000,
})
const soul = ref({ soul_core: '', soul_style: '' })
const testing = ref(false)
const chatOk = ref(false)

async function testChat() {
  testing.value = true
  try {
    const r = await onboardingApi.testConnection(chat.value)
    chatOk.value = r.ok
    toast.push(r.ok ? 'success' : 'error', r.ok ? '连接成功' : '连接失败：' + r.error)
  } finally {
    testing.value = false
  }
}

async function saveChatProvider() {
  // 后端创建/去重更新均返回真实 id，不再用“列表最后一个”推断
  const r = await settingsApi.createProvider({
    ...chat.value,
    display_name: chat.value.display_name || chat.value.model_id,
  })
  await settingsApi.setModelAssignment({ chat_model: r.id, agent_model: r.id })
  try {
    const current = await soulApi.soul()
    const sections = current.soul_style || {}
    soul.value = {
      soul_core: current.soul_core || '',
      soul_style: ['对话风格', '行为原则']
        .filter((name) => sections[name])
        .map((name) => `## ${name}\n${sections[name]}`)
        .join('\n\n'),
    }
  } catch {
    toast.push('warning', '初始人格读取失败，可直接编辑后确认')
  }
  step.value = 2
}

async function confirmSoul() {
  await onboardingApi.soulConfirm(soul.value)
  toast.push('success', '初始化完成')
  emit('done')
}
</script>

<template>
  <!-- 线性引导流程：禁止遮罩/ESC 关闭（例外已登记 UI_UX_SPEC） -->
  <BaseModal size="md" :show-close="false" :close-on-overlay="false" :close-on-esc="false">
    <template #header>首次使用引导 · 第 {{ step }}/2 步</template>

    <div v-if="step === 1">
      <p class="muted mb-12">配置对话模型（必须，测试通过才能继续）</p>
      <div class="mb-10">
        <label class="label">Provider 类型</label>
        <select v-model="chat.provider_type" class="w-full">
          <option value="openai_compatible">OpenAI 兼容</option>
          <option value="anthropic">Anthropic</option>
          <option value="google">Google</option>
        </select>
      </div>
      <div class="mb-10">
        <label class="label">API 地址</label>
        <input
          v-model="chat.base_url"
          placeholder="https://api.deepseek.com/v1"
          class="w-full"
        />
      </div>
      <div class="mb-10">
        <label class="label">API Key</label>
        <input v-model="chat.api_key" type="password" placeholder="sk-..." class="w-full" />
      </div>
      <div class="mb-16">
        <label class="label">模型 ID</label>
        <input v-model="chat.model_id" placeholder="deepseek-chat" class="w-full" />
      </div>
      <div class="fg fg-end fg-gap-8">
        <button :disabled="testing" @click="testChat">
          <i v-if="testing" class="ti ti-loader-2"></i> 测试连接
        </button>
        <button
          class="btn-primary"
          :disabled="!chatOk || busy('saveChat')"
          @click="run('saveChat', saveChatProvider)"
        >
          <i v-if="busy('saveChat')" class="ti ti-loader-2"></i> 下一步
        </button>
      </div>
    </div>

    <div v-else-if="step === 2">
      <p class="muted mb-12">确认初始人格（可编辑）</p>
      <label class="label">SOUL_CORE 核心人格</label>
      <textarea
        v-model="soul.soul_core"
        class="soul-textarea"
      ></textarea>
      <label class="label mt-10">SOUL_STYLE 对话风格</label>
      <textarea
        v-model="soul.soul_style"
        class="soul-textarea-sm"
      ></textarea>
      <div class="fg fg-end fg-gap-8 mt-16">
        <button
          class="btn-primary"
          :disabled="busy('confSoul')"
          @click="run('confSoul', confirmSoul)"
        >
          <i v-if="busy('confSoul')" class="ti ti-loader-2"></i> 确认并开始使用
        </button>
      </div>
    </div>
  </BaseModal>
</template>
