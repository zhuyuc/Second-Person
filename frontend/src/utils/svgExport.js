// SVG → PNG 导出公共工具（MermaidChart 组件与 ChatView 内联图表共用）。
// Mermaid 在 htmlLabels 模式下用 foreignObject 渲染 HTML 标签，含 foreignObject 的
// SVG 绘制到 canvas 会被浏览器标记为 tainted（SecurityError: Tainted canvases may
// not be exported），toBlob 静默失败。导出前先克隆并把 foreignObject 替换为等位的
// 纯文本 <text>（多行 tspan），保留位置/字号/颜色，即可安全导出 PNG。

function readVar(name, fallback) {
  const style = getComputedStyle(document.documentElement)
  const val = style.getPropertyValue(name).trim()
  return val || fallback
}

// 提取 foreignObject 内部 XHTML 标签文字（<br> 转换行，递归拼接）
function extractLabelText(node) {
  let out = ''
  for (const child of node.childNodes) {
    if (child.nodeType === Node.TEXT_NODE) out += child.textContent
    else if (child.nodeType === Node.ELEMENT_NODE) {
      if (child.tagName === 'BR') out += '\n'
      else out += extractLabelText(child)
    }
  }
  return out
}

// 克隆 SVG 并将每个 foreignObject 替换为居中对齐的多行 <text>
function stripForeignObject(svgEl) {
  const clone = svgEl.cloneNode(true)
  const NS = 'http://www.w3.org/2000/svg'
  clone.querySelectorAll('foreignObject').forEach((fo) => {
    const x = parseFloat(fo.getAttribute('x') || '0')
    const y = parseFloat(fo.getAttribute('y') || '0')
    const w = parseFloat(fo.getAttribute('width') || '0')
    const h = parseFloat(fo.getAttribute('height') || '0')
    // 字号/颜色取自 Mermaid 注入的 XHTML div 内联样式
    const div = fo.querySelector('div')
    const divStyle = div && div.style ? div.style : null
    const fontSize = parseFloat((divStyle && divStyle.fontSize) || '14') || 14
    const lines = extractLabelText(fo)
      .split('\n')
      .map((s) => s.trim())
      .filter(Boolean)
    const text = document.createElementNS(NS, 'text')
    text.setAttribute('text-anchor', 'middle')
    text.setAttribute('x', String(x + w / 2))
    text.setAttribute('y', String(y + h / 2 + fontSize * 0.35))
    text.setAttribute('font-size', String(fontSize))
    if (divStyle && divStyle.color) text.setAttribute('fill', divStyle.color)
    if (divStyle && divStyle.fontWeight && divStyle.fontWeight !== 'normal') {
      text.setAttribute('font-weight', divStyle.fontWeight)
    }
    lines.forEach((line, i) => {
      const tspan = document.createElementNS(NS, 'tspan')
      tspan.textContent = line
      if (i > 0) tspan.setAttribute('dy', '1.2em')
      text.appendChild(tspan)
    })
    fo.replaceWith(text)
  })
  return clone
}

export function svgToPngBlob(svgEl, scale = 2) {
  return new Promise((resolve, reject) => {
    const cleaned = stripForeignObject(svgEl)
    const svgData = new XMLSerializer().serializeToString(cleaned)
    const url = URL.createObjectURL(new Blob([svgData], { type: 'image/svg+xml;charset=utf-8' }))
    const img = new Image()
    img.onload = () => {
      const canvas = document.createElement('canvas')
      canvas.width = Math.max(1, img.naturalWidth * scale)
      canvas.height = Math.max(1, img.naturalHeight * scale)
      const ctx = canvas.getContext('2d')
      // 填充背景色（跟随深浅色主题），避免暗色模式导出透明底导致文字不可读
      ctx.fillStyle = readVar('--surface', '#ffffff')
      ctx.fillRect(0, 0, canvas.width, canvas.height)
      ctx.scale(scale, scale)
      ctx.drawImage(img, 0, 0)
      URL.revokeObjectURL(url)
      canvas.toBlob((blob) => {
        if (blob) resolve(blob)
        else reject(new Error('Canvas toBlob failed'))
      }, 'image/png')
    }
    img.onerror = () => {
      URL.revokeObjectURL(url)
      reject(new Error('Image load failed'))
    }
    img.src = url
  })
}
