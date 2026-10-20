/**
 * 跨环境剪贴板工具。
 *
 * 文本：Clipboard API → copy 事件注入 → textarea/execCommand
 * 图片：ClipboardItem → HTML(img data-url) 经 copy 事件 → 仍失败则自动下载保底
 *
 * 非 https / 局域网 IP / WebView / 嵌入页下 navigator.clipboard 常为 undefined，
 * 必须走降级路径，否则会抛 Cannot read properties of undefined (reading 'writeText')。
 */

function canUseClipboardWriteText() {
  return typeof navigator !== 'undefined' && !!navigator.clipboard?.writeText
}

function canUseClipboardWrite() {
  return (
    typeof navigator !== 'undefined' &&
    !!navigator.clipboard?.write &&
    typeof ClipboardItem !== 'undefined'
  )
}

/** 通过 document 'copy' 事件写入剪贴板（不依赖 navigator.clipboard，兼容性最好） */
function copyViaCopyEvent({ text, html } = {}) {
  return new Promise((resolve) => {
    let wrote = false
    const onCopy = (e) => {
      try {
        if (!e.clipboardData) return
        if (text != null) e.clipboardData.setData('text/plain', String(text))
        if (html != null) e.clipboardData.setData('text/html', String(html))
        e.preventDefault()
        wrote = true
      } catch {
        wrote = false
      }
    }
    document.addEventListener('copy', onCopy)
    let ok = false
    try {
      ok = document.execCommand('copy')
    } catch {
      ok = false
    }
    document.removeEventListener('copy', onCopy)
    resolve(!!(ok && wrote))
  })
}

function copyTextViaTextarea(text) {
  const ta = document.createElement('textarea')
  ta.value = text
  ta.setAttribute('readonly', '')
  // 不可 display:none，部分浏览器会拒绝选中；移出视口即可
  ta.style.cssText =
    'position:fixed;top:0;left:0;width:1px;height:1px;padding:0;border:0;outline:none;box-shadow:none;background:transparent;opacity:0;'
  document.body.appendChild(ta)
  const prevEditable = document.body.contentEditable
  try {
    ta.focus()
    ta.select()
    ta.setSelectionRange(0, text.length)
    return !!document.execCommand('copy')
  } catch {
    return false
  } finally {
    document.body.removeChild(ta)
    if (prevEditable != null) document.body.contentEditable = prevEditable
  }
}

/**
 * @returns {Promise<boolean>}
 */
export async function copyTextToClipboard(text) {
  const value = String(text ?? '')
  if (!value) return false

  if (canUseClipboardWriteText()) {
    try {
      await navigator.clipboard.writeText(value)
      return true
    } catch {
      /* fall through */
    }
  }

  if (await copyViaCopyEvent({ text: value })) return true
  if (copyTextViaTextarea(value)) return true
  return false
}

function blobToDataUrl(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || ''))
    reader.onerror = () => reject(reader.error || new Error('read failed'))
    reader.readAsDataURL(blob)
  })
}

function triggerDownload(blob, filename) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename || 'download.bin'
  a.rel = 'noopener'
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  setTimeout(() => URL.revokeObjectURL(url), 1500)
}

/**
 * 复制图片 Blob。
 * @param {Blob} blob
 * @param {{ mime?: string, filename?: string, allowDownloadFallback?: boolean }} [opts]
 * @returns {Promise<'copied' | 'downloaded' | false>}
 */
export async function copyBlobToClipboard(blob, opts = {}) {
  if (!blob) return false
  const mime = opts.mime || blob.type || 'image/png'
  const filename = opts.filename || (mime.includes('png') ? 'image.png' : 'image.bin')
  const allowDownloadFallback = opts.allowDownloadFallback !== false

  if (canUseClipboardWrite()) {
    try {
      await navigator.clipboard.write([new ClipboardItem({ [mime]: blob })])
      return 'copied'
    } catch {
      /* fall through */
    }
  }

  // 降级：把图片写成 HTML <img data-url>，多数桌面浏览器粘贴到富文本仍可用
  try {
    const dataUrl = await blobToDataUrl(blob)
    if (dataUrl) {
      const html = `<img src="${dataUrl}" alt="" />`
      if (await copyViaCopyEvent({ text: dataUrl, html })) return 'copied'
    }
  } catch {
    /* fall through */
  }

  // 最终保底：当前环境无法写剪贴板图片时自动下载，保证用户拿到内容
  if (allowDownloadFallback) {
    try {
      triggerDownload(blob, filename)
      return 'downloaded'
    } catch {
      return false
    }
  }
  return false
}
