<script setup>
import { ref } from 'vue'
import { onboardingApi } from '@/api/onboarding'
import { settingsApi } from '@/api/settings'
import { useSSE } from '@/composables/useSSE'
import { useToast } from '@/stores/toast'
import { useBusy } from '@/composables/useBusy'

const emit = defineEmits(['done'])
const toast = useToast()
const sse = useSSE()
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
const emb = ref({ base_url: '', api_key: '', model_id: '' })
const soul = ref({ soul_core: '', soul_style: '' })
const testing = ref(false)
const chatOk = ref(false)

// 欢迎对话（最多 5 轮）
const welcomeSid = ref(null)
const welcomeMessages = ref([])
const welcomeInput = ref('')
const welcomeSending = ref(false)
const welcomeStream = ref('')
const welcomeRound = ref(0)

async function enterWelcome() {
  step.value = 3
  if (!welcomeSid.value) {
    const d = await onboardingApi.welcomeStart()
    welcomeSid.value = d.session_id
  }
}

async function sendWelcome() {
  const text = welcomeInput.value.trim()
  if (!text || welcomeSending.value || welcomeRound.value >= 5) return
  welcomeMessages.value.push({ role: 'user', content: text })
  welcomeInput.value = ''
  welcomeSending.value = true
  welcomeStream.value = ''
  await sse.send({
    sessionId: welcomeSid.value,
    message: text,
    onEvent: (ev, data) => {
      if (ev === 'content_delta') welcomeStream.value += data.text
      else if (ev === 'turn_completed' || ev === 'error') {
        if (welcomeStream.value)
          {welcomeMessages.value.push({ role: 'assistant', content: welcomeStream.value })}
        welcomeStream.value = ''
        welcomeSending.value = false
        welcomeRound.value += 1
      }
    },
    onError: () => {
      welcomeSending.value = false
      toast.push('error', '发送失败')
    },
  })
}

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
  step.value = 2
}

async function testEmb() {
  const r = await onboardingApi.testEmbedding(emb.value)
  toast.push(r.ok ? 'success' : 'warning', r.ok ? 'Embedding 可用' : '测试失败，可跳过先用全文搜索')
  if (r.ok) {
    const rr = await settingsApi.createProvider({
      display_name: emb.value.model_id,
      provider_type: 'openai_compatible',
      ...emb.value,
      context_window: 8192,
    })
    await settingsApi.setModelAssignment({ embedding_model: rr.id })
  }
}

async function finishWelcome() {
  const draft = await onboardingApi.welcomeFinish()
  soul.value.soul_core = draft.soul_core || ''
  soul.value.soul_style = draft.soul_style_dialog || ''
  step.value = 4
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
    <template #header>首次使用引导 · 第 {{ step }}/4 步</template>

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
      <p class="muted mb-12">配置 Embedding（可跳过，先用全文搜索）</p>
      <div class="mb-10">
        <label class="label">基础地址</label>
        <input v-model="emb.base_url" class="w-full" />
      </div>
      <div class="mb-10">
        <label class="label">API Key</label>
        <input v-model="emb.api_key" type="password" class="w-full" />
      </div>
      <div class="mb-16">
        <label class="label">模型 ID</label>
        <input v-model="emb.model_id" placeholder="text-embedding-3" class="w-full" />
      </div>
      <div class="fg fg-end fg-gap-8">
        <button :disabled="busy('enterW')" @click="run('enterW', enterWelcome)">
          暂不配置，先用全文搜索
        </button>
        <button :disabled="busy('testEmb')" @click="run('testEmb', testEmb)">
          <i v-if="busy('testEmb')" class="ti ti-loader-2"></i> 测试
        </button>
        <button class="btn-primary" :disabled="busy('enterW')" @click="run('enterW', enterWelcome)">
          <i v-if="busy('enterW')" class="ti ti-loader-2"></i> 下一步
        </button>
      </div>
    </div>

    <div v-else-if="step === 3">
      <p class="muted mb-12">
        欢迎对话（{{ welcomeRound }}/5 轮）：让 AI 了解你。
      </p>
      <div class="welcome-scroll">
        <div
          v-if="!welcomeMessages.length && !welcomeStream"
          class="muted welcome-empty"
        >
          AI 将主动向你提问，开始对话来介绍自己吧
        </div>
        <div
          v-for="(m, i) in welcomeMessages"
          :key="i"
          :class="['wbubble', m.role]"
        >
          <span class="wbubble-role">{{ m.role === 'user' ? '你' : 'AI' }}</span>
          <span>{{ m.content }}</span>
        </div>
        <div
          v-if="welcomeStream"
          :class="['wbubble', 'assistant']"
        >
          <span class="wbubble-role">AI</span>
          <span>{{ welcomeStream }}<span class="typing-dot"></span></span>
        </div>
      </div>
      <div class="fg fg-gap-8">
        <input
          v-model="welcomeInput"
          placeholder="介绍你自己…"
          class="flex-1"
          :disabled="welcomeSending || welcomeRound >= 5"
          @keyup.enter="sendWelcome"
        />
        <button
          :disabled="!welcomeInput.trim() || welcomeSending || welcomeRound >= 5"
          @click="sendWelcome"
        >
          发送
        </button>
      </div>
      <div class="fg fg-end fg-gap-8 mt-12">
        <button :disabled="busy('finishW')" @click="run('finishW', finishWelcome)">
          <i v-if="busy('finishW')" class="ti ti-loader-2"></i> 跳过，直接生成初始人格
        </button>
      </div>
    </div>

    <div v-else-if="step === 4">
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
