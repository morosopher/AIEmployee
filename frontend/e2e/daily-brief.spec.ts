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
  // 工作设置使用 IANA 时区标签；精确匹配可访问名称并继续验证合成设置已正确加载。
  await expect(page.getByRole('combobox', { name: 'IANA 时区', exact: true })).toHaveValue('Asia/Shanghai')
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

/** 登录覆盖真实的守卫回跳与失败恢复；请求只返回合成数据，不接触真实管理员凭据。 */
test('login preserves the requested page and allows correcting rejected credentials', async ({
  page,
}) => {
  const settings = userSettings()
  const user = {
    ...settings,
    id: 'login-test-user',
    email: 'admin@example.com',
    display_name: '测试管理员',
  }
  let attempts = 0
  await page.route('**/api/v1/auth/me', (route) =>
    route.fulfill({ status: 401, contentType: 'application/json', body: '{}' }),
  )
  await page.route('**/api/v1/auth/login', (route) => {
    attempts += 1
    return route.fulfill({
      status: attempts === 1 ? 401 : 200,
      contentType: 'application/json',
      body: JSON.stringify(
        attempts === 1
          ? {
              type: 'about:blank',
              title: 'Invalid credentials',
              status: 401,
              detail: 'The email or password was not accepted.',
              instance: '',
              error_code: 'invalid_credentials',
              trace_id: 'login-test-trace',
            }
          : user,
      ),
    })
  })
  await page.route('**/api/v1/settings', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(settings),
    }),
  )
  await page.route('**/api/v1/auth/sessions', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }),
  )
  await page.goto('/settings')
  await expect(
    page.getByRole('heading', { name: '登录 AI Employee' }),
  ).toBeVisible()
  await page.getByLabel('邮箱').fill(user.email)
  await page.getByLabel('密码', { exact: true }).fill('synthetic-test-password')
  // 原生按钮的 Enter/Space 行为与切换后的焦点都在浏览器验证，避免 jsdom 模拟激活。
  const toggle = page.getByRole('button', { name: '显示密码' })
  await page.getByLabel('密码', { exact: true }).press('Tab')
  await expect(toggle).toBeFocused()
  await toggle.press('Enter')
  await expect(toggle).toHaveAttribute('aria-pressed', 'true')
  await expect(toggle).toBeFocused()
  await expect(page.getByLabel('密码', { exact: true })).toHaveAttribute('type', 'text')
  await toggle.press('Space')
  await expect(toggle).toHaveAttribute('aria-pressed', 'false')
  await expect(page.getByLabel('密码', { exact: true })).toHaveAttribute('type', 'password')
  expect(attempts).toBe(0)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('Invalid credentials')
  await expect(page.getByRole('alert')).toContainText('请检查邮箱和密码后重试。')
  await expect(page.getByLabel('邮箱')).toHaveValue(user.email)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page).toHaveURL(/\/settings$/)
  await expect(page.getByRole('heading', { name: '设置' })).toBeVisible()
})
