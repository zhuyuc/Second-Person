import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h, nextTick } from 'vue'
import { useSessions } from '@/stores/sessions'

const injectQuote = vi.fn()
let resolveChatView

vi.mock('@/components/asideChatLoader.js', () => ({
  loadAsideChatView: () =>
    new Promise((resolve) => {
      resolveChatView = () => {
        // defineAsyncComponent 的 loader 既可返回「组件本身」，也可返回 ESM module；
        // 这里直接 resolve 组件，避免测试环境未 unwrap default。
        resolve(
          defineComponent({
            name: 'ChatViewStub',
            props: {
              asideMode: Boolean,
              asideSessionId: [String, Object],
              asideProjectId: [String, Object],
              asideFromSession: [String, Object],
            },
            emits: ['aside-session-created'],
            setup(_, { expose }) {
              expose({ injectQuote })
              return () => h('div', { class: 'chat-root-stub' }, 'aside-chat')
            },
          }),
        )
      }
    }),
}))

import SideChatDrawer from './SideChatDrawer.vue'

describe('SideChatDrawer openAside quote injection', () => {
  let wrapper

  beforeEach(() => {
    injectQuote.mockReset()
    resolveChatView = null
    setActivePinia(createPinia())
    useSessions().setCurrent('main-sid-1')
  })

  afterEach(() => {
    wrapper?.unmount()
    wrapper = null
  })

  it('injects quote after async ChatView mounts (refresh-first-open race)', async () => {
    wrapper = mount(SideChatDrawer, { attachTo: document.body })
    await wrapper.vm.openAside({
      text: '被引用的原文',
      comment: '备注',
      sourceMsgId: 'm1',
      sourceRole: 'assistant',
    })
    await nextTick()

    // 异步 chunk 尚未 resolve：抽屉已开，但还不能注入
    expect(wrapper.find('.aside-drawer.open').exists()).toBe(true)
    expect(injectQuote).not.toHaveBeenCalled()

    resolveChatView()
    await flushPromises()
    await nextTick()
    await nextTick()

    expect(injectQuote).toHaveBeenCalledTimes(1)
    expect(injectQuote).toHaveBeenCalledWith({
      text: '被引用的原文',
      comment: '备注',
      sourceMsgId: 'm1',
      sourceRole: 'assistant',
    })
  })

  it('queues multiple quotes arriving before mount', async () => {
    wrapper = mount(SideChatDrawer, { attachTo: document.body })
    await wrapper.vm.openAside({ text: '第一段' })
    await wrapper.vm.openAside({ text: '第二段' })
    expect(injectQuote).not.toHaveBeenCalled()

    resolveChatView()
    await flushPromises()
    await nextTick()
    await nextTick()

    expect(injectQuote).toHaveBeenCalledTimes(2)
    expect(injectQuote.mock.calls.map((c) => c[0].text)).toEqual(['第一段', '第二段'])
  })

  it('injects immediately when ChatView is already mounted', async () => {
    wrapper = mount(SideChatDrawer, { attachTo: document.body })
    // 先打开一次让实例挂上
    await wrapper.vm.openAside({ text: 'warmup' })
    resolveChatView()
    await flushPromises()
    await nextTick()
    await nextTick()
    injectQuote.mockClear()

    await wrapper.vm.openAside({ text: '已挂载后再引用' })
    await nextTick()

    expect(injectQuote).toHaveBeenCalledWith(
      expect.objectContaining({ text: '已挂载后再引用' }),
    )
  })
})
