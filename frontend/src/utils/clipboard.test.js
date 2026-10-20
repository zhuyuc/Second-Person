import { describe, expect, it, vi, afterEach } from 'vitest'
import { copyTextToClipboard, copyBlobToClipboard } from './clipboard'

describe('clipboard', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('uses clipboard.writeText when available', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { clipboard: { writeText } })
    await expect(copyTextToClipboard('hello')).resolves.toBe(true)
    expect(writeText).toHaveBeenCalledWith('hello')
  })

  it('falls back to copy event when clipboard API missing', async () => {
    vi.stubGlobal('navigator', {})
    let captured = null
    vi.spyOn(document, 'addEventListener').mockImplementation((type, fn) => {
      if (type !== 'copy') return
      fn({
        clipboardData: {
          setData: (k, v) => {
            captured = { k, v }
          },
        },
        preventDefault: vi.fn(),
      })
    })
    vi.spyOn(document, 'removeEventListener').mockImplementation(() => {})
    document.execCommand = vi.fn(() => true)

    await expect(copyTextToClipboard('fallback-text')).resolves.toBe(true)
    expect(captured).toEqual({ k: 'text/plain', v: 'fallback-text' })
  })

  it('downloads image when clipboard image write unavailable', async () => {
    vi.stubGlobal('navigator', {})
    vi.stubGlobal('ClipboardItem', undefined)
    vi.stubGlobal(
      'FileReader',
      class {
        readAsDataURL() {
          queueMicrotask(() => this.onerror?.(new Error('fail')))
        }
      },
    )
    const click = vi.fn()
    vi.spyOn(document, 'createElement').mockImplementation((tag) => {
      if (tag === 'a') {
        return { href: '', download: '', rel: '', style: {}, click }
      }
      return document.createElementNS('http://www.w3.org/1999/xhtml', tag)
    })
    vi.spyOn(document.body, 'appendChild').mockImplementation((n) => n)
    vi.spyOn(document.body, 'removeChild').mockImplementation((n) => n)
    vi.stubGlobal('URL', {
      createObjectURL: () => 'blob:mock',
      revokeObjectURL: vi.fn(),
    })

    const blob = new Blob(['x'], { type: 'image/png' })
    await expect(copyBlobToClipboard(blob, { filename: 't.png' })).resolves.toBe('downloaded')
    expect(click).toHaveBeenCalled()
  })
})
