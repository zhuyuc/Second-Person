import { afterEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import BaseModal from './BaseModal.vue'

let wrapper
let opener

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  opener?.remove()
  opener = null
})

describe('BaseModal focus handling', () => {
  it('cycles Tab focus and restores the invoking control when unmounted', async () => {
    opener = document.createElement('button')
    document.body.append(opener)
    opener.focus()
    wrapper = mount(BaseModal, {
      attachTo: document.body,
      slots: { default: '<button id="first">First</button><button id="last">Last</button>' },
    })
    await nextTick()

    const first = wrapper.find('#first').element
    const last = wrapper.find('#last').element
    last.focus()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }))
    expect(document.activeElement).toBe(first)

    first.focus()
    document.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Tab', shiftKey: true, bubbles: true,
    }))
    expect(document.activeElement).toBe(last)

    wrapper.unmount()
    wrapper = null
    expect(document.activeElement).toBe(opener)
  })
})

describe('BaseModal shell structure', () => {
  it('uses dedicated header/footer classes without .mt/.row', async () => {
    wrapper = mount(BaseModal, {
      attachTo: document.body,
      props: { title: '恢复备份' },
      slots: {
        default: '<p>body</p>',
        footer: '<button type="button">取消</button><button type="button" class="btn-primary">确认</button>',
      },
    })
    await nextTick()

    const hd = wrapper.find('.modal-hd')
    const title = wrapper.find('.modal-hd-title')
    const closeBtn = wrapper.find('.modal-hd-close')
    expect(hd.exists()).toBe(true)
    expect(hd.classes()).not.toContain('row')
    expect(title.exists()).toBe(true)
    expect(title.classes()).not.toContain('mt')
    expect(title.element.tagName).toBe('H2')
    expect(title.text()).toBe('恢复备份')
    expect(closeBtn.exists()).toBe(true)
    expect(wrapper.find('.modal-bd').exists()).toBe(true)
    expect(wrapper.find('.modal-ft').exists()).toBe(true)
    expect(wrapper.find('.modal-ft').classes()).not.toContain('fg')
  })
})
