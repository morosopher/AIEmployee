import { readFileSync } from 'node:fs'
import { randomUUID } from 'node:crypto'
import { expect, test, type Page } from '@playwright/test'
import type { TaskHistoryPage } from '../src/api/taskHistory'

/** 使用真实认证、CSRF 与现有任务创建端点；仅合成数据和 Fake 工具，不新增 seed API。 */
async function login(page: Page): Promise<void> {
  const email = process.env.E2E_ADMIN_EMAIL
  const file = process.env.E2E_ADMIN_PASSWORD_FILE
  if (!email || !file) throw new Error('Task history E2E credentials are not configured')
  await page.goto('/login')
  await page.getByLabel('邮箱', { exact: true }).fill(email)
  await page.getByLabel('密码', { exact: true }).fill(readFileSync(file, 'utf8').trimEnd())
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page).not.toHaveURL(/\/login/)
}

/** 读取这次浏览器已加载的完整 UUID 链接，短前缀不参与身份判断。 */
async function visibleIds(page: Page): Promise<string[]> {
  return page.getByRole('list', { name: '任务历史' }).getByRole('link', { name: /^查看任务 / }).evaluateAll((links) =>
    links.map((link) => new URL((link as HTMLAnchorElement).href).searchParams.get('task_id') ?? ''))
}

/** 数据库联通证据：创建前始终在其他页面，找回至少25条从未点开详情的真实 TaskRun。 */
test('retained database tasks are reachable across pages and browser reload without prior visits', async ({ page }) => {
  await login(page)
  await page.goto('/brief')
  const csrf = (await page.context().cookies()).find((cookie) => cookie.name === 'ai_employee_csrf')?.value
  if (!csrf) throw new Error('Task history E2E session has no CSRF cookie')
  const created = new Set<string>()
  const intent = randomUUID()
  for (let index = 0; index < 25; index += 1) {
    const response = await page.request.post('/api/v1/tasks', {
      headers: { 'X-CSRF-Token': csrf, 'Idempotency-Key': `history-e2e:${intent}:${index}` },
      data: { kind: 'daily_brief', input_payload: { local_date: '2026-10-01' } },
    })
    expect(response.status()).toBe(202)
    const result = await response.json() as { task_id: string }
    created.add(result.task_id)
  }
  expect(created.size).toBe(25)
  await expect(page).toHaveURL(/\/brief$/)
  await page.goto('/tasks')
  await expect(page.getByRole('list', { name: '任务历史' })).toBeVisible()
  await expect(page.getByRole('button', { name: '业务任务', exact: true })).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByRole('list', { name: '任务历史' }).getByRole('link', { name: /^查看任务 / })).toHaveCount(20)
  const first = await visibleIds(page)
  const seen = new Set(first)
  for (let index = 0; index < 20 && [...created].some((id) => !seen.has(id)); index += 1) {
    const next = page.getByRole('button', { name: '下一页', exact: true })
    await expect(next).toBeEnabled()
    const response = page.waitForResponse((result) => result.request().method() === 'GET' && new URL(result.url()).pathname === '/api/v1/tasks')
    await next.click()
    expect((await response).status()).toBe(200)
    await expect(page.getByRole('button', { name: '上一页', exact: true })).toBeEnabled()
    if (await next.isEnabled()) await expect(next).toBeFocused()
    else await expect(page.getByRole('heading', { name: '任务历史', exact: true })).toBeFocused()
    for (const id of await visibleIds(page)) { expect(seen.has(id)).toBe(false); seen.add(id) }
  }
  expect([...created].every((id) => seen.has(id))).toBe(true)
  await page.getByRole('button', { name: '刷新任务历史' }).click()
  await expect(page.getByRole('button', { name: '上一页', exact: true })).toBeDisabled()
  const id = first.find((value) => created.has(value))
  if (!id) throw new Error('Created synthetic task missing from first page')
  await page.reload()
  const link = page.getByRole('link', { name: `查看任务 ${id}`, exact: true })
  await expect(link).toBeVisible()
  await link.focus()
  await page.keyboard.press('Enter')
  await expect(page).toHaveURL(new RegExp(`task_id=${id}`))
  await expect(page.getByRole('region', { name: '任务详情', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: '任务详情', exact: true })).toBeFocused()
  await expect(page.getByRole('complementary', { name: '执行时间线' })).toHaveCount(1)
  await expect(page.getByRole('button', { name: '打开任务时间线' })).toHaveCount(0)
  await page.reload()
  await expect(page.getByRole('list', { name: '任务历史' })).toBeVisible()
  await expect(page.getByText('daily_brief', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '返回任务列表' }).click()
  await expect(page.getByRole('heading', { name: '任务历史', exact: true })).toBeFocused()
  await page.goBack()
  await expect(page).toHaveURL(new RegExp(`task_id=${id}`))
})

const user = { id: 'synthetic-user', email: 'admin@example.test', display_name: '测试管理员', timezone: 'America/New_York', locale: 'zh-CN', brief_time: '08:00' }
const id = '10000000-0000-0000-0000-000000000001'
const history: TaskHistoryPage = { items: [{ id, kind: 'daily_brief', category: 'business', status: 'failed',
  created_at: '2026-03-08T07:00:00.000000Z', started_at: null, finished_at: null, error_code: 'synthetic_failed', retry_of_task_id: null }],
next_cursor: null, previous_cursor: null, server_time: '2026-03-08T08:00:00.000000Z', filter_timezone: 'America/New_York', background_failed_count: 2 }

/** 受控网络只验证浏览器交互边界；数据库证据由上一个真实API用例提供。 */
test('mobile keyboard filters preserve civil dates and recover invalid cursor and unavailable details', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.route('**/api/v1/auth/me', (route) => route.fulfill({ json: user }))
  await page.route('**/api/v1/system/alerts', (route) => route.fulfill({ json: { alerts: [] } }))
  const queries: URLSearchParams[] = []
  await page.route('**/api/v1/tasks?*', (route) => {
    const query = new URL(route.request().url()).searchParams
    queries.push(query)
    if (query.has('cursor')) return route.fulfill({ status: 422, json: { type: 'about:blank', title: 'Invalid cursor', status: 422,
      detail: 'unsafe synthetic cursor detail', instance: '', error_code: 'task_history_cursor_invalid', trace_id: 'synthetic-history-trace' } })
    return route.fulfill({ json: query.get('scope') === 'background' ? { ...history, items: history.items.map((item) => ({ ...item, kind: 'sync_mail', category: 'background' })) } : history })
  })
  await page.route(`**/api/v1/tasks/${id}`, (route) => route.fulfill({ status: 404, json: { type: 'about:blank', title: 'Not found',
    status: 404, detail: '', instance: '', error_code: 'task_not_found', trace_id: 'synthetic-task-trace' } }))
  await page.route(`**/api/v1/tasks/${id}/events*`, (route) => route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'retry: 60000\n\n' }))
  await page.goto('/tasks?scope=business&cursor=synthetic-expired')
  await expect(page.getByRole('alert')).toContainText('分页凭据已失效')
  await expect(page.getByRole('alert')).not.toContainText('unsafe synthetic cursor detail')
  await page.getByRole('button', { name: '重新加载第一页' }).click()
  await expect(page.getByRole('list', { name: '任务历史' })).toBeVisible()
  const from = page.getByLabel('创建起始日期')
  await from.fill('2026-03-08')
  await page.getByLabel('创建结束日期').fill('2026-03-09')
  await page.getByRole('button', { name: '应用筛选' }).focus()
  await page.keyboard.press('Enter')
  await expect.poll(() => queries.at(-1)?.get('created_from_date')).toBe('2026-03-08')
  await expect.poll(() => queries.at(-1)?.get('created_to_date')).toBe('2026-03-09')
  await page.getByRole('button', { name: '当前时间范围内有 2 条后台失败记录' }).click()
  await expect.poll(() => queries.at(-1)?.get('scope')).toBe('background')
  expect(queries.at(-1)?.get('status')).toBe('failed')
  expect(queries.at(-1)?.has('kind')).toBe(false)
  expect(queries.at(-1)?.get('created_from_date')).toBe('2026-03-08')
  await page.getByRole('link', { name: `查看任务 ${id}`, exact: true }).focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('alert')).toHaveText('任务记录已不可用，请返回任务列表。')
  await expect(page.getByRole('heading', { name: '任务详情', exact: true })).toBeFocused()
  await page.getByRole('button', { name: '返回任务列表' }).click()
  await expect(page.getByRole('heading', { name: '任务历史', exact: true })).toBeFocused()
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})
