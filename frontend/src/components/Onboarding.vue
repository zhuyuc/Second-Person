<script setup>
import { ref } from 'vue'
import { api } from '@/api/client'
import { useSSE } from '@/composables/useSSE'
import { useToast } from '@/stores/toast'

const emit = defineEmits(['done'])
const toast = useToast()
const sse = useSSE()
const step = ref(1)
const chat = ref({ provider_type: 'openai_compatible', display_name: '', base_url: '', api_key: '', model_id: '', context_window: 128000 })
const emb = ref({ base_url: '', api_key: '', model_id: '' })
const soul = ref({ soul_core: '', soul_style: '' })
const testing = ref(false)
const chatOk = ref(false)

// 欢迎对话（最多 5 轮）
const welcomeSid = ref(null)
const wmsgs = ref([])
const winput = ref('')
const wsending = ref(false)
const wstream = ref('')
const wround = ref(0)

async function enterWelcome() {
  step.value = 3
  if (!welcomeSid.value) {
    const d = await api.post('/onboarding/welcome-chat/start', {})
    welcomeSid.value = d.session_id
  }
}

async function sendWelcome() {
  const text = winput.value.trim()
  if (!text || wsending.value || wround.value >= 5) return
  wmsgs.value.push({ role: 'user', content: text })
  winput.value = ''
  wsending.value = true
  wstream.value = ''
  await sse.send({
    sessionId: welcomeSid.value, message: text,
    onEvent: (ev, data) => {
      if (ev === 'content_delta') wstream.value += data.text
      else if (ev === 'turn_completed' || ev === 'error') {
        if (wstream.value) wmsgs.value.push({ role: 'assistant', content: wstream.value })
        wstream.value = ''
        wsending.value = false
        wround.value += 1
      }
    },
    onError: () => { wsending.value = false; toast.push('error', '发送失败') },
  })
}

async function testChat() {
  testing.value = true
  try {
    const r = await api.post('/onboarding/test-connection', { provider_config: chat.value })
    chatOk.value = r.ok
    toast.push(r.ok ? 'success' : 'error', r.ok ? '连接成功' : '连接失败：' + r.error)
  } finally { testing.value = false }
}

async function saveChatProvider() {
  await api.post('/settings/providers', { ...chat.value, display_name: chat.value.display_name || chat.value.model_id })
  const list = await api.get('/settings/providers')
  const pid = list[list.length - 1].id
  await api.put('/settings/model-assignment', { chat_model: pid, agent_model: pid })
  step.value = 2
}

async function testEmb() {
  const r = await api.post('/onboarding/test-embedding', { provider_config: emb.value })
  toast.push(r.ok ? 'success' : 'warning', r.ok ? 'Embedding 可用' : '测试失败，可跳过先用全文搜索')
  if (r.ok) {
    await api.post('/settings/providers', { display_name: emb.value.model_id, provider_type: 'openai_compatible', ...emb.value, context_window: 8192 })
    const list = await api.get('/settings/providers')
    await api.put('/settings/model-assignment', { embedding_model: list[list.length - 1].id })
  }
}

async function finishWelcome() {
  const draft = await api.post('/onboarding/welcome-chat/finish', {})
  soul.value.soul_core = draft.soul_core || ''
  soul.value.soul_style = draft.soul_style_dialog || ''
  step.value = 4
}

async function confirmSoul() {
  await api.post('/onboarding/soul/confirm', soul.value)
  toast.push('success', '初始化完成')
  emit('done')
}
</script>

<template>
  <div class="overlay">
    <div class="modal" style="max-width:560px">
      <div class="mt">首次使用引导 · 第 {{ step }}/4 步</div>

      <div v-if="step === 1">
        <p class="muted" style="margin-bottom:12px">配置对话模型（必须，测试通过才能继续）</p>
        <div style="margin-bottom:10px"><label class="label">Provider 类型</label>
          <select v-model="chat.provider_type" style="width:100%">
            <option value="openai_compatible">OpenAI 兼容</option>
            <option value="anthropic">Anthropic</option>
            <option value="google">Google</option>
          </select>
        </div>
        <div style="margin-bottom:10px"><label class="label">API 地址</label>
          <input v-model="chat.base_url" placeholder="https://api.deepseek.com/v1" style="width:100%" />
        </div>
        <div style="margin-bottom:10px"><label class="label">API Key</label>
          <input v-model="chat.api_key" type="password" placeholder="sk-..." style="width:100%" />
        </div>
        <div style="margin-bottom:16px"><label class="label">模型 ID</label>
          <input v-model="chat.model_id" placeholder="deepseek-chat" style="width:100%" />
        </div>
        <div class="fg" style="justify-content:flex-end;gap:8px">
          <button @click="testChat" :disabled="testing">测试连接</button>
          <button @click="saveChatProvider" :disabled="!chatOk">下一步</button>
        </div>
      </div>

      <div v-else-if="step === 2">
        <p class="muted" style="margin-bottom:12px">配置 Embedding（可跳过，先用全文搜索）</p>
        <div style="margin-bottom:10px"><label class="label">基础地址</label>
          <input v-model="emb.base_url" style="width:100%" />
        </div>
        <div style="margin-bottom:10px"><label class="label">API Key</label>
          <input v-model="emb.api_key" type="password" style="width:100%" />
        </div>
        <div style="margin-bottom:16px"><label class="label">模型 ID</label>
          <input v-model="emb.model_id" placeholder="text-embedding-3" style="width:100%" />
        </div>
        <div class="fg" style="justify-content:flex-end;gap:8px">
          <button @click="step = 3">暂不配置，先用全文搜索</button>
          <button @click="testEmb">测试</button>
          <button @click="enterWelcome">下一步</button>
        </div>
      </div>

      <div v-else-if="step === 3">
        <p class="muted" style="margin-bottom:12px">欢迎对话（{{ wround }}/5 轮）：让 AI 了解你。</p>
        <div
          style="max-height:300px;overflow-y:auto;margin-bottom:12px;border:1px solid var(--bd);border-radius:8px;padding:10px;background:var(--s1)">
          <div v-if="!wmsgs.length && !wstream" class="muted" style="text-align:center;padding:30px 0">AI
            将主动向你提问，开始对话来介绍自己吧</div>
          <div v-for="(m, i) in wmsgs" :key="i" :class="['wbubble', m.role]" style="margin-bottom:8px;max-width:85%">
            <span style="font-size:11px;color:var(--muted);display:block;margin-bottom:2px">{{ m.role === 'user' ? '你' :
              'AI' }}</span>
            <span>{{ m.content }}</span>
          </div>
          <div v-if="wstream" :class="['wbubble', 'assistant']" style="margin-bottom:8px;max-width:85%">
            <span style="font-size:11px;color:var(--muted);display:block;margin-bottom:2px">AI</span>
            <span>{{ wstream }}<span class="typing-dot"></span></span>
          </div>
        </div>
        <div class="fg" style="gap:8px">
          <input v-model="winput" placeholder="介绍你自己…" style="flex:1" :disabled="wsending || wround >= 5"
            @keyup.enter="sendWelcome" />
          <button @click="sendWelcome" :disabled="!winput.trim() || wsending || wround >= 5">发送</button>
        </div>
        <div class="fg" style="justify-content:flex-end;gap:8px;margin-top:12px">
          <button @click="finishWelcome">跳过，直接生成初始人格</button>
        </div>
      </div>

      <div v-else-if="step === 4">
        <p class="muted" style="margin-bottom:12px">确认初始人格（可编辑）</p>
        <label class="label">SOUL_CORE 核心人格</label>
        <textarea v-model="soul.soul_core"
          style="width:100%;height:120px;font-family:var(--mono);font-size:12px"></textarea>
        <label class="label" style="margin-top:10px">SOUL_STYLE 对话风格</label>
        <textarea v-model="soul.soul_style"
          style="width:100%;height:100px;font-family:var(--mono);font-size:12px"></textarea>
        <div class="fg" style="justify-content:flex-end;gap:8px;margin-top:16px">
          <button @click="confirmSoul">确认并开始使用</button>
        </div>
      </div>
    </div>
  </div>
</template>
