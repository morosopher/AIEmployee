import { expect, test } from '@playwright/test'

/** 测试模式端点可注入撤销 OAuth，界面必须给出重新连接操作。 */
test('revoked OAuth shows reconnect action through the test-only scenario contract', async ({ page }) => {
  await page.route('**/api/v1/auth/me', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 'u1', email: 'admin@example.test', display_name: 'Admin', timezone: 'UTC', locale: 'zh-CN', brief_time: '08:00' }) }))
  await page.route('**/api/v1/test-support/scenario', (route) => route.fulfill({ status: 204 }))
  await page.route('**/api/v1/connections', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([{ id: 'c1', provider: 'google', account_email: 'synthetic@example.test', scopes: [], status: 'degraded', last_error_code: 'oauth_revoked' }]) }))
  await page.goto('/connections')
  await expect(page.getByRole('button', { name: '重新连接' })).toBeVisible()
})

/** 单来源失败须只呈现一个 partial 告警，并保留修复所需的脱敏信息。 */
test('partial brief presents a single-source failure warning with repair context', async ({ page }) => {
  await page.route('**/api/v1/auth/me', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 'u1', email: 'admin@example.test', display_name: 'Admin', timezone: 'UTC', locale: 'zh-CN', brief_time: '08:00' }) }))
  await page.route('**/api/v1/briefs/today', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 'b1', local_date: '2026-08-05', version: 1, task_id: 't1', source_cutoff: '2026-08-05T08:00:00Z', completeness: 'partial', headline: '今日简报', structured_content: {}, markdown: '# 合成内容', warnings: ['missing:gmail;last_success:2026-08-05T07:00:00Z;repair:reconnect'], items: [] }) }))
  await page.route('**/api/v1/briefs?*', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }))
  await page.goto('/brief')
  await expect(page.getByRole('alert')).toContainText('missing:gmail')
  await expect(page.getByRole('alert')).toContainText('last_success:2026-08-05T07:00:00Z')
  await expect(page.getByRole('alert')).toContainText('repair:reconnect')
})
