import { describe, expect, it } from 'vitest'

import { markOrphanedStreaming } from './useChatSession'

describe('markOrphanedStreaming', () => {
  it('给进程被硬杀留下的 streaming 在途行补中断标记', () => {
    const msgs = [
      {
        role: 'assistant',
        content: '已经输出的一半',
        analysis_metadata: { end_reason: 'streaming' },
      },
    ]
    markOrphanedStreaming(msgs)
    expect(msgs[0].content).toContain('已经输出的一半')
    expect(msgs[0].content).toContain('本回复未完成')
  })

  it('已收口的消息不重复追加标记', () => {
    const msgs = [
      {
        role: 'assistant',
        content: '完整回答',
        analysis_metadata: { end_reason: 'final_answer' },
      },
      {
        role: 'assistant',
        content: '半截\n\n> ⚠️ 本回复未完成：生成已中断',
        analysis_metadata: { end_reason: 'streaming' },
      },
    ]
    markOrphanedStreaming(msgs)
    expect(msgs[0].content).toBe('完整回答')
    expect(msgs[1].content.match(/本回复未完成/g)).toHaveLength(1)
  })

  it('用户消息与无元数据的历史消息不受影响', () => {
    const msgs = [
      { role: 'user', content: '提问' },
      { role: 'assistant', content: '老消息' },
    ]
    markOrphanedStreaming(msgs)
    expect(msgs[0].content).toBe('提问')
    expect(msgs[1].content).toBe('老消息')
  })
})
