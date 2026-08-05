import { expect, test } from '@playwright/test'

/** 浏览器刷新后始终从 API 快照恢复，而不是依赖已丢失的内存 SSE 状态。 */
test('browser refresh recovers the durable task snapshot after an SSE disconnect', async ({ page }) => {
  await page.route('**/api/v1/auth/me', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 'u1', email: 'admin@example.test', display_name: 'Admin', timezone: 'UTC', locale: 'zh-CN', brief_time: '08:00' }) }))
  await page.route('**/api/v1/tasks/task-1', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 'task-1', kind: 'daily_brief', status: 'succeeded', retry_of_task_id: null, error_code: null, event_cursor: '8', steps: [] }) }))
  await page.goto('/tasks/task-1')
  await page.reload()
  await expect(page.getByText('succeeded')).toBeVisible()
})
