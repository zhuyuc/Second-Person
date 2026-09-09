import { describe, expect, it } from 'vitest'
import { parseSSE } from './streamClient'

describe('parseSSE', () => {
  it('preserves the resumable event id with its JSON payload', () => {
    expect(parseSSE('id: 42\nevent: content_delta\ndata: {"text":"续传"}')).toEqual({
      id: '42',
      event: 'content_delta',
      data: { text: '续传' },
    })
  })

  it('ignores keepalive frames without data', () => {
    expect(parseSSE(': keepalive\n')).toBeNull()
  })
})
