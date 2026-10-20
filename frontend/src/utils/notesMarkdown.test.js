import { describe, expect, it } from 'vitest'
import { htmlToMd, mdToHtml, restoreAssetPaths, rewriteAssetUrls } from './notesMarkdown'

describe('notesMarkdown', () => {
  it('roundtrips headings bold italic underline', () => {
    const md = '## Title\n\n**bold** _italic_ <u>under</u>\n'
    const html = mdToHtml(md)
    expect(html).toMatch(/<h2>/i)
    expect(html).toMatch(/<strong>|<b>/i)
    const back = htmlToMd(html)
    expect(back).toContain('Title')
    expect(back).toMatch(/\*\*bold\*\*|__bold__/)
  })

  it('rewrites and restores asset urls', () => {
    const html = '<p><img src="./assets/img_1.png"></p>'
    const rewritten = rewriteAssetUrls(html, 'note_x')
    expect(rewritten).toContain('/api/notes/note_x/assets/img_1.png')
    expect(restoreAssetPaths(rewritten, 'note_x')).toContain('./assets/img_1.png')
  })

  it('autolinks bare https urls in markdown', () => {
    const html = mdToHtml('见 https://example.com/path 文档')
    expect(html).toMatch(/<a[^>]+href=["']https:\/\/example\.com\/path["']/i)
  })
})

