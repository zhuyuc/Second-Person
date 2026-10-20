import { marked } from 'marked'
import TurndownService from 'turndown'

const turndown = new TurndownService({
  headingStyle: 'atx',
  codeBlockStyle: 'fenced',
  bulletListMarker: '-',
})
turndown.addRule('underline', {
  filter: ['u'],
  replacement: (content) => `<u>${content}</u>`,
})
turndown.addRule('strikethrough', {
  filter: ['s', 'del', 'strike'],
  replacement: (content) => `~~${content}~~`,
})

marked.setOptions({ gfm: true, breaks: false })

export function mdToHtml(md) {
  return marked.parse(md || '') || '<p></p>'
}

export function htmlToMd(html) {
  return turndown.turndown(html || '')
}

/** 把相对 assets 路径改成可请求的 API URL */
export function rewriteAssetUrls(html, noteId) {
  if (!html || !noteId) return html
  return html.replace(
    /(?:src|href)=["'](\.\/assets\/[^"']+)["']/g,
    (_m, rel) => {
      const name = rel.replace('./assets/', '')
      return `src="/api/notes/${noteId}/assets/${name}"`
    },
  ).replace(
    /src=["']\.\/assets\/([^"']+)["']/g,
    (_m, name) => `src="/api/notes/${noteId}/assets/${name}"`,
  )
}

/** 保存前把 API URL 还原为相对路径 */
export function restoreAssetPaths(html, noteId) {
  if (!html || !noteId) return html
  const re = new RegExp(`/api/notes/${noteId}/assets/([^"'\\s>]+)`, 'g')
  return html.replace(re, './assets/$1')
}
