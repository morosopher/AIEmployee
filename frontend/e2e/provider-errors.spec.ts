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
  const existingSession = (await page.context().cookies()).some(
    (cookie) => cookie.name === 'ai_employee_session',
  )
  if (!existingSession) {
    await page.goto('/login')
    await page.getByLabel('邮箱').fill(email)
    await page.getByLabel('密码').fill(password)
    await page.getByRole('button', { name: '登录' }).click()
    await expect(page).not.toHaveURL(/\/login/)
  }
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

/** 创建当前用户自己的合成来源，返回 CSRF 与本次连接 ID 以隔离历史数据。 */
async function seedGoogleSource(page: Page): Promise<{ csrf: string, connectionId: string }> {
  const csrf = (await page.context().cookies()).find((cookie) => cookie.name === 'ai_employee_csrf')
  if (!csrf) throw new Error('test login did not issue a CSRF cookie')
  const response = await page.request.post('/api/v1/test-support/seed-google-source', {
    headers: { 'X-CSRF-Token': csrf.value },
  })
  expect(response.status()).toBe(200)
  const { connection_id: connectionId } = await response.json() as { connection_id: string }
  return { csrf: csrf.value, connectionId }
}

/** 通过仅在双测试开关下存在的端点执行当前用户已持久化的真实 Worker 路径。 */
async function executeTask(page: Page, csrf: string, taskId: string): Promise<void> {
  const response = await page.request.post('/api/v1/test-support/execute-task', {
    headers: { 'X-CSRF-Token': csrf },
    data: { task_id: taskId },
  })
  expect(response.status()).toBe(204)
}

/** 创建只绑定本例合成连接的测试简报任务，避免历史来源影响故障结论。 */
async function generateBriefForSource(
  page: Page,
  csrf: string,
  connectionId: string,
): Promise<string> {
  const response = await page.request.post('/api/v1/test-support/generate-brief', {
    headers: { 'X-CSRF-Token': csrf },
    data: { connection_id: connectionId },
  })
  expect(response.status()).toBe(202)
  const { task_id: taskId } = await response.json() as { task_id: string }
  return taskId
}

/** 测试模式端点可注入撤销 OAuth，界面必须给出重新连接操作。 */
test('revoked OAuth shows reconnect action through the test-only scenario contract', async ({
  page,
}) => {
  await page.goto('/login')
  const email = process.env.E2E_ADMIN_EMAIL!
  const password = readFileSync(process.env.E2E_ADMIN_PASSWORD_FILE!, 'utf8').trimEnd()
  await page.getByLabel('邮箱').fill(email)
  await page.getByLabel('密码').fill(password)
  await page.getByRole('button', { name: '登录' }).click()
  const { csrf, connectionId } = await seedGoogleSource(page)
  await injectScenario(page, 'oauth_revoked')
  const sync = await page.request.post(`/api/v1/connections/${connectionId}/sync`, {
    headers: { 'X-CSRF-Token': csrf, 'Idempotency-Key': crypto.randomUUID() },
  })
  expect(sync.status()).toBe(202)
  const { gmail_task_id: gmailTaskId } = (await sync.json()) as { gmail_task_id: string }
  await executeTask(page, csrf, gmailTaskId)
  await expect.poll(async () => {
    const response = await page.request.get('/api/v1/connections')
    const connections = (await response.json()) as Array<{
      id: string
      status: string
      last_error_code: string | null
    }>
    return connections.find((connection) => connection.id === connectionId)
  }).toEqual({ id: connectionId, status: 'degraded', last_error_code: 'oauth_revoked' })
  await page.goto('/connections')
  await expect(page.getByRole('button', { name: '重新连接' })).toBeVisible()
})

/** 单来源失败须只呈现一个 partial 告警，并保留修复所需的脱敏信息。 */
test('partial brief presents a single-source failure warning with repair context', async ({
  page,
}) => {
  await page.goto('/login')
  const email = process.env.E2E_ADMIN_EMAIL!
  const password = readFileSync(process.env.E2E_ADMIN_PASSWORD_FILE!, 'utf8').trimEnd()
  await page.getByLabel('邮箱').fill(email)
  await page.getByLabel('密码').fill(password)
  await page.getByRole('button', { name: '登录' }).click()
  const { csrf, connectionId } = await seedGoogleSource(page)
  await injectScenario(page, 'partial_source')
  const taskId = await generateBriefForSource(page, csrf, connectionId)
  await executeTask(page, csrf, taskId)
  await expect.poll(async () => {
    const response = await page.request.get('/api/v1/briefs/today')
    if (response.status() !== 200) return null
    return (await response.json()) as {
      task_id: string
      completeness: string
      warnings: string[]
    }
  }).toEqual(expect.objectContaining({
    task_id: taskId,
    completeness: 'partial',
    warnings: expect.arrayContaining([
      expect.stringContaining('missing:gmail'),
      expect.stringContaining('last_success:'),
      expect.stringContaining('repair:retry'),
    ]),
  }))
  await page.goto('/brief')
  await expect(page.getByRole('alert')).toContainText('missing:gmail')
  await expect(page.getByRole('alert')).toContainText(
    'last_success:',
  )
  await expect(page.getByRole('alert')).toContainText('repair:retry')
})
