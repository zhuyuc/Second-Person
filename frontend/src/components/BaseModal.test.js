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
