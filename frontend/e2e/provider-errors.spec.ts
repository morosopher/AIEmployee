import { expect, test, type Page } from '@playwright/test'
import { readFileSync } from 'node:fs'

/**
 * 通过真实测试 API 建立会话并原子注入一次性故障；凭据仅来自临时 E2E 环境变量。
 *
 * @param page Playwright 浏览器页面，Cookie 会同时供后续 page.request 使用。
 * @param scenario 已被服务端白名单的合成故障名称。
 */
async function injectScenario(
  page: Page,
  scenario: 'oauth_revoked' | 'partial_source',
): Promise<void> {
  const email = process.env.E2E_ADMIN_EMAIL
  const passwordFile = process.env.E2E_ADMIN_PASSWORD_FILE
  if (!email || !passwordFile)
    throw new Error('E2E_ADMIN_EMAIL and E2E_ADMIN_PASSWORD_FILE are required')
  const password = readFileSync(passwordFile, 'utf8').trimEnd()
  await page.goto('/login')
  await page.getByLabel('邮箱').fill(email)
  await page.getByLabel('密码').fill(password)
  await page.getByRole('button', { name: '登录' }).click()
  await expect(page).not.toHaveURL(/\/login/)
  const csrfCookie = (await page.context().cookies()).find(
    (cookie) => cookie.name === 'ai_employee_csrf',
  )
  if (!csrfCookie) throw new Error('test login did not issue a CSRF cookie')
  const response = await page.request.post('/api/v1/test-support/scenario', {
    headers: { 'X-CSRF-Token': csrfCookie.value },
    data: { scenario },
  })
  expect(response.status()).toBe(204)
}

/** 测试模式端点可注入撤销 OAuth，界面必须给出重新连接操作。 */
test('revoked OAuth shows reconnect action through the test-only scenario contract', async ({ page }) => {
  await injectScenario(page, 'oauth_revoked')
  await page.route('**/api/v1/connections', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([{ id: 'c1', provider: 'google', account_email: 'synthetic@example.test', scopes: [], status: 'degraded', last_error_code: 'oauth_revoked' }]) }))
  await page.goto('/connections')
  await expect(page.getByRole('button', { name: '重新连接' })).toBeVisible()
})

/** 单来源失败须只呈现一个 partial 告警，并保留修复所需的脱敏信息。 */
test('partial brief presents a single-source failure warning with repair context', async ({ page }) => {
  await injectScenario(page, 'partial_source')
  await page.route('**/api/v1/briefs/today', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 'b1', local_date: '2026-08-05', version: 1, task_id: 't1', source_cutoff: '2026-08-05T08:00:00Z', completeness: 'partial', headline: '今日简报', structured_content: {}, markdown: '# 合成内容', warnings: ['missing:gmail;last_success:2026-08-05T07:00:00Z;repair:reconnect'], items: [] }) }))
  await page.route('**/api/v1/briefs?*', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }))
  await page.goto('/brief')
  await expect(page.getByRole('alert')).toContainText('missing:gmail')
  await expect(page.getByRole('alert')).toContainText('last_success:2026-08-05T07:00:00Z')
  await expect(page.getByRole('alert')).toContainText('repair:reconnect')
})
