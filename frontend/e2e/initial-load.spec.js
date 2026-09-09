import { expect, test } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'

function apiFixture(url) {
  const path = new URL(url).pathname
  if (path.endsWith('/onboarding/status')) return { completed: true }
  if (path.endsWith('/chat/sessions')) return { total: 0, list: [], next_cursor: null }
  if (path.endsWith('/projects')) return []
  if (path.endsWith('/health')) return { status: 'healthy' }
  if (path.endsWith('/chat/reasoning-efforts')) return []
  return {}
}

test('first paint defers diagrams and has no basic app accessibility violations', async ({ page }) => {
  const diagramRequests = []
  page.on('request', (request) => {
    if (/(DiagramRenderer|diagram-)/.test(request.url())) diagramRequests.push(request.url())
  })
  await page.route((url) => url.pathname.startsWith('/api/'), (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ code: 200, data: apiFixture(route.request().url()) }),
  }))

  await page.goto('/')
  await expect(page.locator('.app')).toBeVisible()
  await page.waitForTimeout(500)
  expect(diagramRequests).toEqual([])

  const results = await new AxeBuilder({ page })
    .include('.app')
    .withTags(['wcag2a', 'wcag2aa'])
    .analyze()
  const critical = results.violations.filter((violation) =>
    ['button-name', 'label', 'aria-allowed-attr', 'aria-hidden-focus'].includes(violation.id))
  expect(critical).toEqual([])
})
