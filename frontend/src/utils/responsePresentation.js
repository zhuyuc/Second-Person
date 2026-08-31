const CALLOUTS = {
  INFO: { className: 'info', label: '信息', icon: 'ti-alert-circle' },
  DECISION: { className: 'decision', label: '结论 / 建议', icon: 'ti-bulb' },
  CONCLUSION: { className: 'decision', label: '结论 / 建议', icon: 'ti-bulb' },
  ASSUMPTION: { className: 'assumption', label: '前提 / 假设', icon: 'ti-pin' },
  RISK: { className: 'risk', label: '风险', icon: 'ti-alert-triangle' },
  BLOCKER: { className: 'blocker', label: '阻塞事项', icon: 'ti-circle-x' },
}

const STAGE_RE = /^阶段\s*([0-9]+|[一二三四五六七八九十百]+)\s*[|｜:：-]\s*(.+)$/
const FIELD_RE = /^(目标|产出|验收|依赖|风险|步骤|结果)\s*[:：]\s*/

function directFirstBlock(element) {
  return [...element.children].find((child) =>
    /^(P|UL|OL|PRE|TABLE|DIV|SECTION)$/i.test(child.tagName)
  )
}

function firstLine(element) {
  let value = ''
  let completed = false
  const visit = (node) => {
    if (completed) return
    if (node.nodeType === Node.ELEMENT_NODE && node.tagName === 'BR') {
      completed = true
      return
    }
    if (node.nodeType === Node.TEXT_NODE) {
      const text = node.nodeValue || ''
      const offset = text.search(/\r?\n/)
      if (offset === -1) value += text
      else {
        value += text.slice(0, offset)
        completed = true
      }
      return
    }
    for (const child of node.childNodes || []) visit(child)
  }
  visit(element)
  return value.trim()
}

function removeFirstLine(element) {
  let completed = false
  const visit = (node) => {
    if (completed) return
    if (node.nodeType === Node.ELEMENT_NODE && node.tagName === 'BR') {
      node.remove()
      completed = true
      return
    }
    if (node.nodeType === Node.TEXT_NODE) {
      const value = node.nodeValue || ''
      const offset = value.search(/\r?\n/)
      if (offset === -1) node.nodeValue = ''
      else {
        node.nodeValue = value.slice(offset).replace(/^\r?\n/, '')
        completed = true
      }
      return
    }
    for (const child of node.childNodes || []) visit(child)
  }
  visit(element)
}

function enhanceCallouts(root) {
  const blocks = [...root.querySelectorAll('blockquote')].filter(
    (block) => !block.parentElement?.closest('blockquote')
  )

  for (const block of blocks) {
    const first = directFirstBlock(block)
    if (!first) continue
    const match = firstLine(first).match(/^\[!([A-Z]+)\](?:\s+(.+))?$/i)
    if (!match) continue

    const config = CALLOUTS[match[1].toUpperCase()]
    if (!config) continue

    const aside = document.createElement('aside')
    aside.className = `md-callout md-callout-${config.className}`
    aside.setAttribute('role', 'note')

    const head = document.createElement('div')
    head.className = 'md-callout-head'
    const icon = document.createElement('i')
    icon.className = `ti ${config.icon}`
    const title = document.createElement('span')
    title.textContent = (match[2] || '').trim() || config.label
    head.append(icon, title)

    const body = document.createElement('div')
    body.className = 'md-callout-body'
    removeFirstLine(first)
    for (const child of [...block.childNodes]) body.appendChild(child)
    if (!body.textContent.trim() && !body.querySelector('img,table,pre,ul,ol'))
      {body.textContent = ''}

    aside.append(head, body)
    block.replaceWith(aside)
  }
}

function enhanceTables(root) {
  for (const table of [...root.querySelectorAll('table')]) {
    if (table.parentElement?.classList.contains('md-table-wrap')) continue
    const wrapper = document.createElement('div')
    wrapper.className = 'md-table-wrap'
    wrapper.setAttribute('role', 'region')
    wrapper.setAttribute('tabindex', '0')
    wrapper.setAttribute('aria-label', '内容表格')
    table.parentNode.insertBefore(wrapper, table)
    wrapper.appendChild(table)
    for (const th of table.querySelectorAll('th')) {
      if (!th.hasAttribute('scope')) th.setAttribute('scope', 'col')
    }
  }
}

function markStageFields(stage) {
  for (const item of stage.querySelectorAll('li')) {
    const match = item.textContent.trim().match(FIELD_RE)
    if (!match) continue
    const field = match[1]
    item.classList.add('md-stage-field', `md-stage-field-${field}`)

    const first = item.firstElementChild
    if (first?.tagName === 'STRONG' && first.textContent.trim() === field) {
      first.classList.add('md-stage-field-label')
      continue
    }

    if (item.firstChild?.nodeType === Node.TEXT_NODE) {
      const value = item.firstChild.nodeValue || ''
      const prefix = value.match(FIELD_RE)
      if (prefix) {
        const label = document.createElement('span')
        label.className = 'md-stage-field-label'
        label.textContent = field
        item.firstChild.nodeValue = value.slice(prefix[0].length)
        item.insertBefore(label, item.firstChild)
      }
    }
  }
}

function enhanceStages(root) {
  const headings = [...root.querySelectorAll('h3, h4')]
  for (const heading of headings) {
    if (heading.closest('.md-callout')) continue
    const match = heading.textContent.trim().match(STAGE_RE)
    if (!match) continue

    const parent = heading.parentNode
    if (!parent) continue
    const stage = document.createElement('div')
    stage.className = 'md-stage'
    const index = document.createElement('div')
    index.className = 'md-stage-index'
    index.textContent = `阶段 ${match[1]}`
    const main = document.createElement('div')
    main.className = 'md-stage-main'
    const header = document.createElement('div')
    header.className = 'md-stage-header'
    const title = document.createElement(heading.tagName.toLowerCase())
    title.className = 'md-stage-title'
    title.textContent = match[2].trim()
    header.append(index, title)
    main.appendChild(header)

    const content = heading.nextElementSibling
    if (
      content &&
      (content.classList.contains('md-sec') || /^(P|UL|OL|DIV|SECTION)$/i.test(content.tagName))
    ) {
      content.classList.add('md-stage-content')
      main.appendChild(content)
    }
    stage.appendChild(main)
    parent.insertBefore(stage, heading)
    heading.remove()
    markStageFields(stage)
  }
}

export function enhanceResponseHtml(html) {
  try {
    const holder = document.createElement('div')
    holder.innerHTML = html || ''
    enhanceCallouts(holder)
    enhanceTables(holder)
    enhanceStages(holder)
    return holder.innerHTML
  } catch {
    return html || ''
  }
}
