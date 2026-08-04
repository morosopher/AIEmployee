import { test, expect } from '@playwright/test'
test('brief generation, version switching and settings use controllable API fixtures', async ({ page }) => {
  await page.route('**/api/v1/auth/me', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 'u1', email: 'admin@example.com', display_name: 'Admin', timezone: 'UTC', locale: 'zh-CN', brief_time: '08:00' }) }))
  await page.route('**/api/v1/briefs/today', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 'b2', local_date: '2026-08-04', version: 2, task_id: 't2', source_cutoff: '2026-08-04T08:00:00Z', completeness: 'complete', headline: '今日简报', structured_content: {}, markdown: '# 内容', warnings: [], items: [] }) }))
  await page.route('**/api/v1/briefs?*', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }))
  await page.route('**/api/v1/settings', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ timezone: 'UTC', locale: 'zh-CN', brief_time: '08:00', email_body_retention_days: 30, source_metadata_retention_days: 180, workspace_history_retention_days: 365, updated_at: '2026-08-04T00:00:00Z' }) }))
  await page.goto('/brief')
  await expect(page.getByRole('heading', { name: '今日简报' })).toBeVisible()
  await page.goto('/settings')
  await expect(page.getByRole('heading', { name: '设置' })).toBeVisible()
})
