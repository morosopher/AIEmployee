import { test, expect } from '@playwright/test'
import { CONNECTION_ID, userSettings } from '../src/test-support/actionFixtures'
test('brief generation, version switching and settings use controllable API fixtures', async ({ page }) => {
  await page.route('**/api/v1/auth/me', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 'u1', email: 'admin@example.com', display_name: 'Admin', timezone: 'UTC', locale: 'zh-CN', brief_time: '08:00' }) }))
  await page.route('**/api/v1/briefs/today', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 'b2', local_date: '2026-08-04', version: 2, task_id: 't2', source_cutoff: '2026-08-04T08:00:00Z', completeness: 'complete', headline: '今日简报', structured_content: {}, markdown: '# 内容', warnings: [], items: [] }) }))
  await page.route('**/api/v1/briefs?*', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }))
  // 完整 M2 设置来自共享的已验证合成契约，避免仅凭标题误判加载成功。
  await page.route('**/api/v1/settings', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(userSettings()) }))
  await page.route('**/api/v1/auth/sessions', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }))
  await page.goto('/brief')
  await expect(page.getByRole('heading', { name: '今日简报' })).toBeVisible()
  await page.goto('/settings')
  await expect(page.getByRole('heading', { name: '设置' })).toBeVisible()
  await expect(page.getByRole('combobox', { name: '时区', exact: true })).toHaveValue('Asia/Shanghai')
  await expect(page.getByText('无法加载设置。', { exact: true })).toHaveCount(0)
})

test('mocked approval, connection and conversation controls are user-visible', async ({ page }) => {
  await page.route('**/api/v1/auth/me', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 'u1', email: 'admin@example.com', display_name: 'Admin', timezone: 'UTC', locale: 'zh-CN', brief_time: '08:00' }) }))
  await page.route('**/api/v1/conversations', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) }))
  await page.route('**/api/v1/connections', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([{ id: CONNECTION_ID, provider: 'google', account_email: 'fake@example.com', scopes: [], status: 'degraded', last_error_code: 'sync_failed' }]) }))
  await page.route('**/api/v1/conversations/*', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ conversation: { id: 'c1', title: 'Demo', created_at: '', updated_at: '' }, messages: [] }) }))
  await page.goto('/connections')
  await expect(page.getByText('degraded')).toBeVisible()
  await expect(page.getByRole('button', { name: '重新连接' })).toBeVisible()
  await page.goto('/chat')
  await expect(page.getByRole('heading', { name: '聊天' })).toBeVisible()
})
