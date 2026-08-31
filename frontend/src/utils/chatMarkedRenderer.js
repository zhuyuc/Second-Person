// ChatView 专用的 marked 渲染器：
// - mermaid 代码块 → <div class="mermaid"> + 操作按钮
// - html 代码块 → 预览/下载/复制 按钮
// - 其他代码块 → 语言标签 + 复制按钮
// - /api/files/… 链接 → 下载卡片
// 抽取到独立模块的目的：
//  1. 让 ChatView.vue 更瘦（首屏 JS 体积下降）
//  2. 便于单元测试与复用
import { marked } from 'marked'

function escapeHtml(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;')
}

function mermaidBlock(src) {
  const escaped = escapeHtml(src)
  return `<div class="mermaid-wrap" data-source="${escaped}"><div class="mermaid-actions"><button class="mermaid-btn mermaid-zoom-out" title="缩小图表" aria-label="缩小图表"><i class="ti ti-zoom-out"></i></button><button class="mermaid-btn mermaid-zoom-in" title="放大图表" aria-label="放大图表"><i class="ti ti-zoom-in"></i></button><button class="mermaid-btn mermaid-reset" title="重置图表" aria-label="重置图表"><i class="ti ti-refresh"></i></button><button class="mermaid-btn mermaid-copy-src" title="复制源码"><i class="ti ti-code"></i> 源码</button><button class="mermaid-btn mermaid-copy-img" title="复制图片"><i class="ti ti-photo"></i> 图片</button></div><div class="mermaid">${src}</div></div>`
}

function htmlBlock(src) {
  const escaped = escapeHtml(src)
  return `<div class="html-code-wrap" data-source="${escaped}"><div class="mermaid-actions"><button class="mermaid-btn html-preview-btn" title="预览"><i class="ti ti-eye"></i> 预览</button><button class="mermaid-btn html-download-btn" title="下载"><i class="ti ti-download"></i> 下载</button><button class="mermaid-btn html-copy-btn" title="复制"><i class="ti ti-copy"></i> 复制</button></div><pre><code class="language-html">${escaped}</code></pre></div>`
}

function codeBlock(src, language) {
  const escaped = escapeHtml(src)
  return `<div class="code-wrap" data-source="${escaped}"><div class="mermaid-actions"><span class="code-lang">${language}</span><button class="mermaid-btn code-copy-btn" title="复制代码"><i class="ti ti-copy"></i> 复制</button></div><pre><code class="language-${language}">${escaped}</code></pre></div>`
}

function fileCard(href, text) {
  const ext = (href.split('.').pop() || '').toLowerCase()
  const icon = ext === 'docx' ? 'ti-file-word' : ext === 'md' ? 'ti-markdown' : 'ti-file-download'
  const name = String(text || '文件').replace(/&/g, '&amp;').replace(/</g, '&lt;')
  return `<a class="file-card" href="${href}" download><i class="ti ${icon} file-card-icon"></i><span class="file-card-name">${name}</span><span class="file-card-dl"><i class="ti ti-download"></i> 下载</span></a>`
}

const originalRenderer = new marked.Renderer()
const chatRenderer = new marked.Renderer()

chatRenderer.code = function (code, lang) {
  const isObj = typeof code === 'object' && code !== null
  const src = isObj ? (code.text ?? '') : String(code ?? '')
  const langName = ((isObj ? code.lang : lang) || 'text').split(/\s/)[0]

  if (langName === 'mermaid') return mermaidBlock(src)
  if (langName === 'html') return htmlBlock(src)
  return codeBlock(src, langName)
}

chatRenderer.link = function (href, title, text) {
  let h = href
  let t = text
  if (typeof href === 'object' && href) {
    h = href.href
    t = href.text || t
  }
  if (typeof h === 'string' && h.startsWith('/api/files/')) {
    return fileCard(h, t)
  }
  return originalRenderer.link.call(this, href, title, text)
}

// 全局配置（在模块首次导入时执行一次）
marked.setOptions({ renderer: chatRenderer })

export { marked, chatRenderer }
