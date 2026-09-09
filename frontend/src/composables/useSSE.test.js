import { describe, expect, it, vi } from 'vitest'

const stream = vi.hoisted(() => ({ postJsonStream: vi.fn() }))

vi.mock('@/api/streamClient', () => ({
  postJsonStream: stream.postJsonStream,
  parseSSE(chunk) {
    let id = null
    let event = 'message'
    let data = null
    for (const line of chunk.split(/\r?\n/)) {
      if (line.startsWith('id:')) id = line.slice(3).trim()
      else if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) data = JSON.parse(line.slice(5).trim())
    }
    return data === null ? null : { id, event, data }
  },
}))

import { useSSE } from './useSSE'

function streamResponse(parts) {
  const encoder = new TextEncoder()
  let index = 0
  return {
    body: {
      getReader: () => ({
        read: async () => {
          const next = parts[index++]
          if (next instanceof Error) throw next
          if (next === undefined) return { done: true }
          return { done: false, value: encoder.encode(next) }
        },
      }),
    },
  }
}

describe('useSSE', () => {
  it('reconnects after the last rendered event without replaying it', async () => {
    stream.postJsonStream
      .mockResolvedValueOnce(streamResponse([
        'id: 1\nevent: content_delta\ndata: {"text":"first"}\n\n',
        new Error('connection reset'),
      ]))
      .mockResolvedValueOnce(streamResponse([
        'id: 2\nevent: turn_completed\ndata: {}\n\n',
      ]))
    const onEvent = vi.fn()

    await useSSE().send({
      sessionId: 'sess_1',
      message: 'hello',
      clientRequestId: 'cr_resume',
      reasoningEffort: 'high',
      trackActive: false,
      onEvent,
    })

    expect(stream.postJsonStream).toHaveBeenCalledTimes(2)
    expect(stream.postJsonStream.mock.calls[0][1].last_event_id).toBe(0)
    expect(stream.postJsonStream.mock.calls[1][1].last_event_id).toBe(1)
    expect(onEvent.mock.calls).toEqual([
      ['content_delta', { text: 'first' }],
      ['turn_completed', {}],
    ])
  })
})
