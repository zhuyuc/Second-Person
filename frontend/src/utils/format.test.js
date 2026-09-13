import { describe, expect, it } from 'vitest'
import { friendlyError } from '@/utils/format'

describe('friendlyError', () => {
  it('keeps short chinese messages intact', () => {
    expect(friendlyError('附件已过期，请重新上传')).toBe('附件已过期，请重新上传')
  })

  it('maps technical timeouts to readable chinese', () => {
    expect(friendlyError('Request timed out')).toBe('等待服务响应超时，请稍后重试')
  })

  it('maps auth failures to settings guidance', () => {
    expect(friendlyError('401 unauthorized invalid_api_key')).toContain('设置')
  })

  it('falls back for empty message', () => {
    expect(friendlyError('', '请检查输入')).toBe('请检查输入')
  })

  it('does not leak raw stack traces', () => {
    const msg = friendlyError('Traceback (most recent call last):\nTypeError: boom')
    expect(msg).not.toMatch(/Traceback/)
    expect(msg).toContain('操作失败')
  })
})
