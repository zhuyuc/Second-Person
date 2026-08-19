import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { marked } from 'marked'

class NodeBase {
  constructor(type) {
    this.nodeType = type
    this.parentNode = null
  }

  remove() {
    if (!this.parentNode) return
    const index = this.parentNode.childNodes.indexOf(this)
    if (index !== -1) this.parentNode.childNodes.splice(index, 1)
    this.parentNode = null
  }

  replaceWith(node) {
    if (!this.parentNode) return
    const index = this.parentNode.childNodes.indexOf(this)
    if (index === -1) return
    node.parentNode = this.parentNode
    this.parentNode.childNodes[index] = node
    this.parentNode = null
  }
}

class TextNode extends NodeBase {
  constructor(value) {
    super(3)
    this.nodeValue = value
  }

  get textContent() { return this.nodeValue }
  set textContent(value) { this.nodeValue = value }
}

class ElementNode extends NodeBase {
  constructor(tagName) {
    super(1)
    this.tagName = tagName.toUpperCase()
    this.childNodes = []
    this.attributes = new Map()
    this.className = ''
  }

  append(...nodes) { nodes.forEach(node => this.appendChild(node)) }
  appendChild(node) {
    node.remove?.()
    node.parentNode = this
    this.childNodes.push(node)
    return node
  }
  insertBefore(node, reference) {
    node.remove?.()
    const index = this.childNodes.indexOf(reference)
    node.parentNode = this
    this.childNodes.splice(index === -1 ? this.childNodes.length : index, 0, node)
    return node
  }
  get children() { return this.childNodes.filter(node => node.nodeType === 1) }
  get firstChild() { return this.childNodes[0] || null }
  get firstElementChild() { return this.children[0] || null }
  get nextElementSibling() {
    if (!this.parentNode) return null
    const siblings = this.parentNode.children
    return siblings[siblings.indexOf(this) + 1] || null
  }
  get classList() {
    return {
      add: (...values) => { this.className = [...new Set((this.className + ' ' + values.join(' ')).trim().split(/\s+/))].join(' ') },
      contains: value => this.className.split(/\s+/).includes(value),
    }
  }
  get textContent() { return this.childNodes.map(node => node.textContent).join('') }
  set textContent(value) {
    this.childNodes = value ? [new TextNode(value)] : []
    this.childNodes.forEach(node => { node.parentNode = this })
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)) }
  hasAttribute(name) { return this.attributes.has(name) }
  closest(selector) {
    const classes = selector.split(',').map(value => value.trim().replace(/^\./, ''))
    let node = this
    while (node) {
      if (node.nodeType === 1 && classes.some(value => node.classList.contains(value))) return node
      node = node.parentElement
    }
    return null
  }
  get parentElement() { return this.parentNode?.nodeType === 1 ? this.parentNode : null }
  querySelectorAll(selector) {
    const values = selector.split(',').map(value => value.trim())
    const result = []
    const visit = node => {
      if (node.nodeType !== 1) return
      if (values.some(value => matches(node, value))) result.push(node)
      node.children.forEach(visit)
    }
    this.children.forEach(visit)
    return result
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null }
  set innerHTML(value) { this.childNodes = parseHtml(value).childNodes; this.childNodes.forEach(node => { node.parentNode = this }) }
  get innerHTML() { return this.childNodes.map(serialize).join('') }
}

function matches(node, selector) {
  if (selector.startsWith('.')) return node.classList.contains(selector.slice(1))
  if (selector.includes('.')) {
    const [tag, cls] = selector.split('.')
    return node.tagName === tag.toUpperCase() && node.classList.contains(cls)
  }
  return node.tagName === selector.toUpperCase()
}

function parseHtml(html) {
  const root = new ElementNode('div')
  const stack = [root]
  const tokenRe = /<\/[^>]+>|<[^>]+>|[^<]+/g
  for (const token of html.match(tokenRe) || []) {
    if (token.startsWith('</')) { stack.pop(); continue }
    if (token.startsWith('<')) {
      const tag = token.match(/^<([A-Za-z0-9]+)/)?.[1]
      if (!tag) continue
      const element = new ElementNode(tag)
      const className = token.match(/class="([^"]*)"/)?.[1]
      if (className) element.className = className
      stack.at(-1).appendChild(element)
      if (!/^<(br|img|hr)\b/i.test(token)) stack.push(element)
    } else stack.at(-1).appendChild(new TextNode(token))
  }
  return root
}

function serialize(node) {
  if (node.nodeType === 3) return node.nodeValue
  const attrs = [node.className && ` class="${node.className}"`, ...[...node.attributes].map(([key, value]) => ` ${key}="${value}"`)].filter(Boolean).join('')
  return `<${node.tagName.toLowerCase()}${attrs}>${node.childNodes.map(serialize).join('')}</${node.tagName.toLowerCase()}>`
}

globalThis.Node = { ELEMENT_NODE: 1, TEXT_NODE: 3 }
globalThis.document = { createElement: tag => new ElementNode(tag) }

const { enhanceResponseHtml } = await import('../src/utils/responsePresentation.js')

function render(markdown) { return enhanceResponseHtml(marked.parse(markdown)) }

for (const [type, className] of [
  ['INFO', 'info'],
  ['DECISION', 'decision'],
  ['ASSUMPTION', 'assumption'],
  ['RISK', 'risk'],
  ['BLOCKER', 'blocker'],
]) {
  const callout = render(`> [!${type}] 标题\n> 正文内容。`)
  assert.match(callout, new RegExp(`md-callout-${className}`))
  assert.match(callout, /标题/)
  assert.match(callout, /正文内容/)
}

const alias = render('> [!CONCLUSION]\n> 保持兼容。')
assert.match(alias, /md-callout-decision/)

const unknown = render('> [!CUSTOM] 未知类型\n> 保持普通引用。')
assert.match(unknown, /<blockquote>/)
assert.doesNotMatch(unknown, /md-callout/)

const table = render('| 方案 | 推荐 |\n| --- | --- |\n| A | 是 |')
assert.match(table, /md-table-wrap/)
assert.match(table, /scope="col"/)

const stage = render('### 阶段 1｜基础能力\n- 目标：识别语义块\n- 产出：统一样式\n- 验收：页面正常')
assert.match(stage, /md-stage/)
assert.match(stage, /md-stage-header/)
assert.match(stage, /阶段 1/)
assert.match(stage, /md-stage-field-label/) 

const regular = render('> 普通引用\n> 继续保持原样。')
assert.match(regular, /<blockquote>/)
assert.doesNotMatch(regular, /md-callout/)

const css = readFileSync(new URL('../src/style.css', import.meta.url), 'utf8')
const h3Rules = css.match(/\.msg-ai \.content h3\s*\{([\s\S]*?)\}/)?.[1] || ''
const sectionRules = css.match(/\.msg-ai \.content \.md-sec\s*\{([\s\S]*?)\}/)?.[1] || ''
assert.doesNotMatch(h3Rules, /border-left|padding-left/)
assert.doesNotMatch(sectionRules, /border-left|padding-left/)

console.log('response presentation checks passed')
