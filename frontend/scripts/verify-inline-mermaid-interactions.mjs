import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { bindInlineMermaidInteractions, cleanupInlineMermaidInteractions, resetInlineMermaid, zoomInlineMermaid } from '../src/utils/inlineMermaidInteractions.js'

class ClassList {
  constructor() { this.values = new Set() }
  add(value) { this.values.add(value) }
  remove(value) { this.values.delete(value) }
  contains(value) { return this.values.has(value) }
}

class FakeElement {
  constructor() {
    this.style = {}
    this.classList = new ClassList()
    this.listeners = new Map()
    this.isConnected = true
  }

  addEventListener(type, listener, options) {
    const handlers = this.listeners.get(type) || []
    handlers.push({ listener, options })
    this.listeners.set(type, handlers)
  }

  removeEventListener(type, listener) {
    const handlers = (this.listeners.get(type) || []).filter(item => item.listener !== listener)
    if (handlers.length) this.listeners.set(type, handlers)
    else this.listeners.delete(type)
  }

  emit(type, event) {
    for (const { listener } of this.listeners.get(type) || []) listener(event)
  }

  querySelector(selector) {
    if (selector === '.mermaid') return this.viewport
    if (selector === 'svg') return this.svg
    return null
  }

  getBoundingClientRect() { return { left: 10, top: 20, width: 400, height: 240 } }
  setPointerCapture() {}
  releasePointerCapture() {}
}

const wrapper = new FakeElement()
const viewport = new FakeElement()
const svg = new FakeElement()
wrapper.viewport = viewport
viewport.svg = svg
const root = {
  querySelectorAll: selector => selector === '.mermaid-wrap' ? [wrapper] : [],
  contains: node => node === wrapper,
}

bindInlineMermaidInteractions(root)
assert.equal(viewport.listeners.get('wheel').length, 1)
assert.equal(viewport.listeners.get('pointerdown').length, 1)
assert.equal(svg.style.transform, 'translate(0px, 0px) scale(1)')

let prevented = false
viewport.emit('wheel', {
  clientX: 210,
  clientY: 140,
  deltaY: -1,
  preventDefault: () => { prevented = true },
})
assert.equal(prevented, true)
assert.match(svg.style.transform, /scale\(1\.12\)/)

viewport.emit('pointerdown', { button: 0, pointerId: 4, clientX: 120, clientY: 80, preventDefault() {} })
viewport.emit('pointermove', { pointerId: 4, clientX: 150, clientY: 105 })
const translated = svg.style.transform.match(/translate\(([-\d.]+)px, ([-\d.]+)px\)/)
assert.ok(translated)
assert.ok(Math.abs(Number(translated[1]) - 6) < 0.001)
assert.ok(Math.abs(Number(translated[2]) - 10.6) < 0.001)
viewport.emit('pointerup', { pointerId: 4 })
assert.equal(viewport.classList.contains('mermaid-panning'), false)

assert.equal(zoomInlineMermaid(wrapper, 1.12), true)
assert.equal(resetInlineMermaid(wrapper), true)
assert.equal(svg.style.transform, 'translate(0px, 0px) scale(1)')

bindInlineMermaidInteractions(root)
assert.equal(viewport.listeners.get('wheel').length, 1)
cleanupInlineMermaidInteractions(root)
assert.equal(viewport.listeners.has('wheel'), false)

const fallbackWrapper = new FakeElement()
const fallbackViewport = new FakeElement()
const fallbackSvg = new FakeElement()
fallbackWrapper.viewport = fallbackViewport
fallbackViewport.svg = fallbackSvg
assert.equal(zoomInlineMermaid(fallbackWrapper, 1.12), true)
assert.match(fallbackSvg.style.transform, /scale\(1\.12\)/)
cleanupInlineMermaidInteractions()

const chatView = readFileSync(new URL('../src/views/ChatView.vue', import.meta.url), 'utf8')
assert.match(chatView, /DiagramRenderer/)
assert.match(chatView, /bindInlineMermaidInteractions\(root\)/)
assert.match(chatView, /MutationObserver/)
assert.match(chatView, /mermaid-zoom-in/)
assert.match(chatView, /mermaid-reset/)

console.log('inline Mermaid interaction checks passed')
