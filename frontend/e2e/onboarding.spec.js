import { expect, test } from '@playwright/test'

function envelope(data) {
  return { contentType: 'application/json', body: JSON.stringify({ code: 200, data }) }
}

test('onboarding is a two-step model and SOUL flow', async ({ page }) => {
  const requests = []
  page.on('request', (request) => requests.push(new URL(request.url()).pathname))
  await page.route((url) => url.pathname.startsWith('/api/'), async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/onboarding/status')) return route.fulfill(envelope({ completed: false }))
    if (path.endsWith('/health')) return route.fulfill(envelope({ status: 'healthy' }))
    if (path.endsWith('/onboarding/test-connection')) return route.fulfill(envelope({ ok: true }))
    if (path.endsWith('/settings/providers')) return route.fulfill(envelope({ id: 'p_test' }))
    if (path.endsWith('/settings/model-assignment')) return route.fulfill(envelope({}))
    if (path.endsWith('/soul')) return route.fulfill(envelope({
      soul_core: '# 核心人格\n- 保持耐心。',
      soul_style: { 对话风格: '清晰、直接。', 行为原则: '先确认需求。' },
    }))
    return route.fulfill(envelope({}))
  })

  await page.goto('/')
  const dialog = page.getByRole('dialog')
  await expect(dialog).toContainText('首次使用引导 · 第 1/2 步')
  await expect(dialog).not.toContainText('Embedding')
  await expect(dialog).not.toContainText('欢迎对话')

  await page.getByRole('button', { name: '测试连接' }).click()
  await expect(page.getByText('连接成功')).toBeVisible()
  await page.getByRole('button', { name: '下一步' }).click()

  await expect(dialog).toContainText('首次使用引导 · 第 2/2 步')
  await expect(dialog).toContainText('SOUL_CORE 核心人格')
  await expect(dialog.locator('textarea').nth(1)).toHaveValue(/清晰、直接。/)
  await page.getByRole('button', { name: '确认并开始使用' }).click()
  await expect(page.locator('.app')).toBeVisible()
  expect(requests.some((path) => path.includes('test-embedding'))).toBe(false)
  expect(requests.some((path) => path.includes('welcome-chat'))).toBe(false)
})
