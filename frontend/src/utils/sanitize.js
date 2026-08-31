// v-html 渲染安全边界：保留 Markdown 常用标签，移除脚本、事件属性与危险协议
const ALLOWED_TAGS = new Set([
  'A',
  'P',
  'BR',
  'HR',
  'BLOCKQUOTE',
  'PRE',
  'CODE',
  'SPAN',
  'DIV',
  'SECTION',
  'UL',
  'OL',
  'LI',
  'STRONG',
  'B',
  'EM',
  'I',
  'S',
  'DEL',
  'TABLE',
  'THEAD',
  'TBODY',
  'TR',
  'TH',
  'TD',
  'H1',
  'H2',
  'H3',
  'H4',
  'H5',
  'H6',
  'IMG',
  // 代码块操作条按钮（预览/下载/复制/源码/图片）：由前端渲染器生成，
  // 事件属性已被下方过滤，放行 BUTTON 才能让 handleMermaidActions 委派生效
  'BUTTON',
  // 搜索结果高亮
  'MARK',
])

const ALLOWED_ATTRS = new Set([
  'href',
  'target',
  'rel',
  'title',
  'class',
  'data-source',
  'download',
  'src',
  'alt',
  'type',
  'aria-label',
  'role',
  'tabindex',
  'scope',
])

// Mermaid 渲染产物 SVG 白名单（仅保留图表展示所需标签/属性）
const SVG_ALLOWED_TAGS = new Set([
  'SVG',
  'G',
  'PATH',
  'RECT',
  'CIRCLE',
  'ELLIPSE',
  'LINE',
  'POLYLINE',
  'POLYGON',
  'TEXT',
  'TSPAN',
  'DEFS',
  'CLIPPATH',
  'MASK',
  'USE',
  'SYMBOL',
  'MARKER',
  'LINEARGRADIENT',
  'RADIALGRADIENT',
  'STOP',
  'FOREIGNOBJECT',
  'TITLE',
  'DESC',
  'STYLE',
  'FILTER',
  'FEOFFSET',
  'FEFLOOD',
  'FECOMPOSITE',
  'FEGAUSSIANBLUR',
])

const SVG_ALLOWED_ATTRS = new Set([
  'id',
  'class',
  'style',
  'transform',
  'viewbox',
  'width',
  'height',
  'xmlns',
  'x',
  'y',
  'x1',
  'y1',
  'x2',
  'y2',
  'cx',
  'cy',
  'r',
  'rx',
  'ry',
  'd',
  'fill',
  'stroke',
  'stroke-width',
  'stroke-linecap',
  'stroke-linejoin',
  'opacity',
  'fill-opacity',
  'stroke-opacity',
  'font-family',
  'font-size',
  'font-weight',
  'text-anchor',
  'dominant-baseline',
  'dx',
  'dy',
  'points',
  'href',
  'xlink:href',
  'clip-path',
  'mask',
  'filter',
  'offset',
  'stop-color',
  'stop-opacity',
  'gradientunits',
  'gradienttransform',
  'spreadmethod',
  'patternunits',
  'preserveaspectratio',
  'marker-end',
  'marker-start',
  'marker-mid',
  'marker-width',
  'marker-height',
  'refx',
  'refy',
  'orient',
  'alignment-baseline',
  'letter-spacing',
  'word-spacing',
])

function isDangerousUrl(value) {
  const v = String(value || '').trim()
  if (/^\s*javascript:/i.test(v)) return true
  if (/^\s*vbscript:/i.test(v)) return true
  if (/^\s*data:/i.test(v)) {
    // 仅允许安全的 data:image/*（不含 svg+xml）
    return !/^\s*data:image\/(png|jpe?g|gif|webp);base64,/i.test(v)
  }
  return false
}

function cleanNode(node, allowedTags, allowedAttrs, { svgMode = false } = {}) {
  if (node.nodeType === Node.ELEMENT_NODE) {
    if (!allowedTags.has(node.tagName)) {
      node.replaceWith(...node.childNodes)
      return
    }
    for (const attr of [...node.attributes]) {
      const name = attr.name.toLowerCase()
      const value = attr.value || ''
      if (name.startsWith('on') || !allowedAttrs.has(name)) {
        node.removeAttribute(attr.name)
        continue
      }
      if ((name === 'href' || name === 'src' || name === 'xlink:href') && isDangerousUrl(value)) {
        node.removeAttribute(attr.name)
      }
    }
    // SVG foreignObject 可能嵌入 HTML，strict 模式下直接剥离
    if (svgMode && node.tagName === 'FOREIGNOBJECT') {
      node.remove()
      return
    }
    if (node.tagName === 'A') {
      node.setAttribute('target', '_blank')
      node.setAttribute('rel', 'noopener noreferrer')
    }
  }
  for (const child of [...node.childNodes]) {
    cleanNode(child, allowedTags, allowedAttrs, { svgMode })
  }
}

export function sanitizeHtml(html) {
  const tpl = document.createElement('template')
  tpl.innerHTML = html || ''
  cleanNode(tpl.content, ALLOWED_TAGS, ALLOWED_ATTRS)
  return tpl.innerHTML
}

/** Mermaid 渲染产物 SVG 消毒（入库 v-html 前调用） */
export function sanitizeSvg(svgHtml) {
  const tpl = document.createElement('template')
  tpl.innerHTML = svgHtml || ''
  cleanNode(tpl.content, SVG_ALLOWED_TAGS, SVG_ALLOWED_ATTRS, { svgMode: true })
  return tpl.innerHTML
}
