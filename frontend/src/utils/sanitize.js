// v-html 渲染安全边界：保留 Markdown 常用标签，移除脚本、事件属性与危险协议
const ALLOWED_TAGS = new Set([
    'A', 'P', 'BR', 'HR', 'BLOCKQUOTE', 'PRE', 'CODE', 'SPAN', 'DIV', 'SECTION',
    'UL', 'OL', 'LI', 'STRONG', 'B', 'EM', 'I', 'S', 'DEL', 'TABLE', 'THEAD', 'TBODY',
    'TR', 'TH', 'TD', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'IMG',
    // 代码块操作条按钮（预览/下载/复制/源码/图片）：由前端渲染器生成，
    // 事件属性已被下方过滤，放行 BUTTON 才能让 handleMermaidActions 委派生效
    'BUTTON',
    // 搜索结果高亮
    'MARK'
])

const ALLOWED_ATTRS = new Set([
    'href', 'target', 'rel', 'title', 'class', 'data-source', 'download', 'src', 'alt',
    'type', 'aria-label', 'role', 'tabindex', 'scope'
])

export function sanitizeHtml(html) {
    const tpl = document.createElement('template')
    tpl.innerHTML = html || ''

    function clean(node) {
        if (node.nodeType === Node.ELEMENT_NODE) {
            if (!ALLOWED_TAGS.has(node.tagName)) {
                node.replaceWith(...node.childNodes)
                return
            }
            for (const attr of [...node.attributes]) {
                const name = attr.name.toLowerCase()
                const value = attr.value || ''
                if (name.startsWith('on') || !ALLOWED_ATTRS.has(name)) {
                    node.removeAttribute(attr.name)
                    continue
                }
                if ((name === 'href' || name === 'src') && /^\s*javascript:/i.test(value)) {
                    node.removeAttribute(attr.name)
                }
            }
            if (node.tagName === 'A') {
                node.setAttribute('target', '_blank')
                node.setAttribute('rel', 'noopener noreferrer')
            }
        }
        for (const child of [...node.childNodes]) clean(child)
    }

    clean(tpl.content)
    return tpl.innerHTML
}
