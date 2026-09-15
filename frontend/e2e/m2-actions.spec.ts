import { expect, test, type Page, type APIResponse } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { parseMailDraft } from '../src/api/mail'
import { parseCalendarProposal } from '../src/api/calendar'
import type { CalendarProposal, MailMode } from '../src/api/types'
import { streamProxy } from './support/streamProxy'

/** 双测试开关提供的合成来源标识；所有编辑、审批与执行仍调用生产契约。 */
interface M2Source {
  connection_id: string
  calendar_id: string
  thread_id: string
  message_id: string
  event_id: string
  starts_at: string
  ends_at: string
}

/** 仅包含计数和状态的旁证，不向浏览器暴露密文、请求正文或凭据。 */
interface M2Evidence {
  execution_count: number
  write_calls: number
  reconcile_calls: number
  checkpoint_count: number
  real_writes_enabled: boolean
}

type Scenario =
  | 'confirmed_applied'
  | 'confirmed_not_applied'
  | 'timeout_after_accept'
  | 'ambiguous_5xx'
  | 'delayed_reconciliation_success'
  | 'never_resolved'
  | 'etag_conflict'
  | 'capability_revoked'
  | 'duplicate_delivery'

/** 登录密码只由临时 Secret 注入，异常仅报告缺失配置，不回显其值。 */
async function login(page: Page): Promise<void> {
  const email = process.env.E2E_ADMIN_EMAIL
  const file = process.env.E2E_ADMIN_PASSWORD_FILE
  if (!email || !file) throw new Error('M2 E2E credentials are not configured')
  await page.goto('/login')
  await page.getByLabel('邮箱', { exact: true }).fill(email)
  await page.getByLabel('密码', { exact: true }).fill(readFileSync(file, 'utf8').trimEnd())
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page).not.toHaveURL(/\/login/)
}

/** 读取当前真实会话的 CSRF Cookie，值不进入断言、报告或浏览器持久存储。 */
async function csrf(page: Page): Promise<string> {
  const value = (await page.context().cookies()).find((cookie) => cookie.name === 'ai_employee_csrf')?.value
  if (!value) throw new Error('M2 E2E session has no CSRF cookie')
  return value
}

/** 对当前用户发起真实修改请求；测试控制面只接收经过白名单的标识与枚举。 */
async function post(page: Page, path: string, data: unknown = {}): Promise<APIResponse> {
  return page.request.post(`/api/v1${path}`, {
    headers: { 'X-CSRF-Token': await csrf(page), 'Idempotency-Key': crypto.randomUUID() },
    data,
  })
}

/** 原子设置下一次 Fake 调用场景；未注册时应在此得到正式 RED。 */
async function scenario(page: Page, value: Scenario): Promise<void> {
  const response = await post(page, '/test-support/scenario', { scenario: value })
  expect(response.status(), 'M2 Fake scenario must be registered').toBe(204)
}

/** 每例创建新连接、日历和邮件来源，避免前例数据或重复运行左右结果。 */
async function seed(page: Page, provider: 'google' | 'microsoft'): Promise<M2Source> {
  const response = await post(page, '/test-support/seed-m2-source', { provider })
  expect(response.status()).toBe(200)
  return await response.json() as M2Source
}

/** 测试入口等待真正 DurableTaskRunner 结束，不直接改写任务、审批或 ToolExecution。 */
async function runTask(page: Page, taskId: string): Promise<void> {
  const response = await post(page, '/test-support/execute-task', { task_id: taskId })
  expect(response.status()).toBe(204)
}

/** 从独立 HTTP 读取持久动作状态；失败输出仅包含安全状态枚举。 */
async function status(page: Page, taskId: string): Promise<string> {
  const response = await page.request.get(`/api/v1/actions/${taskId}`)
  expect(response.status()).toBe(200)
  const value = await response.json() as { status: string }
  return value.status
}

/** 读取与精确任务绑定的外部 Fake ledger 与 PostgreSQL 计数，不接受浏览器自报成功。 */
async function evidence(page: Page, taskId: string): Promise<M2Evidence> {
  const response = await page.request.get(`/api/v1/test-support/m2-evidence/${taskId}`)
  expect(response.status()).toBe(200)
  return await response.json() as M2Evidence
}

/** 通过编辑器提交当前保存版本，再让真实 Graph 到达持久审批中断。 */
async function submit(page: Page): Promise<string> {
  const responsePromise = page.waitForResponse((response) => response.url().endsWith('/submit') && response.request().method() === 'POST')
  await page.getByRole('button', { name: '提交审批', exact: true }).click()
  const response = await responsePromise
  // 失败仅输出稳定错误码，禁止把冻结正文或完整响应交给 Playwright 报告。
  const result = await response.json() as { task_id?: string; error?: { code?: string }; error_code?: string }
  expect(response.status(), result.error?.code ?? result.error_code ?? 'submission status').toBe(202)
  const taskId = result.task_id
  if (!taskId) throw new Error('Submission task identifier is absent')
  await runTask(page, taskId)
  await page.goto(`/actions?task=${taskId}`)
  await expect(page.getByRole('button', { name: '批准', exact: true })).toBeEnabled()
  expect((await evidence(page, taskId)).write_calls).toBe(0)
  return taskId
}

/** 人工点击精确审批卡片；重复投递后仍只能存在一个执行和一次外部写入。 */
async function approve(page: Page, taskId: string): Promise<void> {
  await page.getByRole('button', { name: '批准', exact: true }).click()
  await expect(page.getByRole('button', { name: '批准', exact: true })).toBeDisabled()
  await runTask(page, taskId)
}

/** 用真实草稿 API 建立回复的来源绑定，随后所有编辑与提交都通过生产页面。 */
async function openMail(page: Page, source: M2Source, mode: MailMode): Promise<string> {
  const created = await post(page, '/mail/drafts', {
    mode,
    connection_id: source.connection_id,
    ...(mode === 'new' ? {} : { source_thread_id: source.thread_id, source_message_id: source.message_id }),
  })
  expect(created.status()).toBe(201)
  const draft = parseMailDraft(await created.json())
  await page.goto(`/mail/drafts/${draft.id}`)
  if (mode === 'new') {
    await page.getByLabel('收件人 To', { exact: true }).fill('recipient@example.test')
    await page.getByLabel('主题', { exact: true }).fill('Synthetic M2 subject')
  } else {
    await expect(page.getByLabel('主题', { exact: true })).toBeDisabled()
    await expect(page.getByLabel('主题', { exact: true })).toHaveValue('Re: Synthetic source subject')
  }
  await page.getByLabel('纯文本正文', { exact: true }).fill('Synthetic M2 plain text')
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect(page.getByRole('button', { name: '提交审批', exact: true })).toBeEnabled()
  return draft.id
}

/** 从正常创建 API 得到本地提案；Worker 获取来源事实后仍需逐项确认与重新审批。 */
async function openCalendar(page: Page, source: M2Source, kind: 'create' | 'update'): Promise<CalendarProposal> {
  const response = await post(page, '/calendar/proposals', kind === 'create' ? {
    operation_kind: 'create', connection_id: source.connection_id, calendar_id: source.calendar_id,
    title: 'Synthetic M2 meeting', description: 'Synthetic description', location: 'Synthetic room',
    starts_at: source.starts_at, ends_at: source.ends_at, timezone: 'UTC', all_day: false,
    attendees: [], notification_policy: 'all',
  } : { operation_kind: 'update', event_id: source.event_id, title: 'Synthetic M2 updated meeting' })
  expect(response.status()).toBe(201)
  const proposal = parseCalendarProposal(await response.json())
  await page.goto(`/calendar/proposals/${proposal.id}`)
  return proposal
}

/** 显式确认服务端要求的字段；不能通过测试 fixture 预先伪造浏览器的确认或批准。 */
async function confirmCalendar(page: Page): Promise<void> {
  await expect(page.getByRole('region', { name: '逐项确认', exact: true })).toBeVisible()
  const proposalId = page.url().split('/').at(-1)
  const initial = await page.request.get(`/api/v1/calendar/proposals/${proposalId}`)
  expect(initial.status()).toBe(200)
  const required = parseCalendarProposal(await initial.json()).required_confirmations
  const labels = { calendar: '确认此日历', time: '确认时间', attendees: '确认参会人', notification_policy: '确认通知策略' }
  // 只点击服务端实际要求的确认；每次等待 PATCH 后的完整 GET 收敛，不能把 pending 的禁用误当已确认。
  for (const field of required) {
    const button = page.getByRole('button', { name: labels[field], exact: true })
    await expect(button).toBeEnabled()
    const confirmed = page.waitForResponse((response) => response.url().endsWith(`/calendar/proposals/${proposalId}`) && response.request().method() === 'PATCH')
    const reread = page.waitForResponse((response) => response.url().endsWith(`/calendar/proposals/${proposalId}`) && response.request().method() === 'GET')
    await button.click()
    expect((await confirmed).status()).toBe(200)
    const read = await reread
    expect(read.status(), 'confirmed calendar full-read status').toBe(200)
    const current = parseCalendarProposal(await read.json())
    expect(current.editor_facts !== null, `calendar full-read v${current.version} facts`).toBe(true)
    await expect(page.getByRole('button', { name: '重新加载提案', exact: true })).toBeEnabled()
  }
  await page.getByRole('button', { name: '查询三个候选时间', exact: true }).click()
  await expect(page.getByRole('region', { name: '服务端候选时间' })).toBeVisible()
  const response = await page.request.get(`/api/v1/calendar/proposals/${proposalId}`)
  expect(response.status()).toBe(200)
  const saved = parseCalendarProposal(await response.json())
  // 独立重读只断言状态、版本与字段名称；用于分辨确认未提交和前端尚在读取。
  expect(saved.required_confirmations, `calendar ${saved.operation_kind} v${saved.version} required confirmations`).toEqual([])
  if (saved.operation_kind !== 'create') expect(saved.changed_fields.length).toBeGreaterThan(0)
  await expect(page.getByRole('button', { name: '提交审批', exact: true })).toBeEnabled()
}

/** 消费本机测试 OAuth callback；授权链接只取 state，浏览器不导航任何供应商网站。 */
async function completeOAuth(page: Page, provider: 'google' | 'microsoft', error?: string): Promise<APIResponse> {
  const link = page.getByTestId('authorization-link')
  await expect(link).toBeVisible()
  const href = await link.getAttribute('href')
  const state = href ? new URL(href).searchParams.get('state') : null
  if (!state) throw new Error('Synthetic OAuth attempt state is absent')
  return page.request.get(`/api/v1/connections/${provider}/callback`, {
    params: error ? { state, error } : { state, code: 'synthetic-m2-oauth-code' },
  })
}

test.beforeEach(async ({ page }) => {
  await login(page)
  await scenario(page, 'confirmed_applied')
})

for (const provider of ['google', 'microsoft'] as const) {
  for (const mode of ['new', 'reply', 'reply_all'] as const) {
    test(`${provider} ${mode}: encrypted local edit, exact approval, refresh and duplicate delivery`, async ({ page }) => {
      const source = await seed(page, provider)
      const draftId = await openMail(page, source, mode)
      const taskId = await submit(page)
      await expect(page.getByRole('region', { name: '邮件审批预览' })).toContainText(provider === 'google' ? 'Google' : 'Microsoft')
      await approve(page, taskId)
      await expect.poll(() => status(page, taskId)).toBe('succeeded')
      await runTask(page, taskId)
      await page.reload()
      await expect(page.locator('.action-detail')).toContainText('已完成')
      const facts = await evidence(page, taskId)
      expect(facts.execution_count).toBe(1)
      expect(facts.write_calls).toBe(1)
      expect(facts.checkpoint_count).toBeGreaterThan(0)
      expect(facts.real_writes_enabled).toBe(false)
      const response = await page.request.get(`/api/v1/mail/drafts/${draftId}`)
      expect(response.headers()['cache-control']).toContain('no-store')
      expect(parseMailDraft(await response.json()).status).toBe('sent')
      expect(await page.evaluate(() => [localStorage.length, sessionStorage.length])).toEqual([0, 0])
    })
  }

  test(`${provider} calendar create, update and separately approved restore`, async ({ page }) => {
    const source = await seed(page, provider)
    await openCalendar(page, source, 'create')
    await confirmCalendar(page)
    const createTask = await submit(page)
    await approve(page, createTask)
    await expect.poll(() => status(page, createTask)).toBe('succeeded')
    const update = await openCalendar(page, source, 'update')
    await confirmCalendar(page)
    const updateTask = await submit(page)
    await approve(page, updateTask)
    await expect.poll(() => status(page, updateTask)).toBe('succeeded')
    await page.goto(`/calendar/proposals/${update.id}`)
    await page.getByRole('button', { name: '准备恢复提案', exact: true }).click()
    await expect(page.getByRole('status').filter({ hasText: '准备' }).first()).toBeVisible()
    await expect(page).toHaveURL(/\/tasks\?task_id=/)
    const prepareTask = new URL(page.url()).searchParams.get('task_id')
    if (!prepareTask) throw new Error('Restore preparation task is absent')
    await runTask(page, prepareTask)
    await page.reload()
    await page.getByRole('link', { name: '打开恢复提案', exact: true }).click()
    await expect(page).toHaveURL(/\/calendar\/proposals\/[0-9a-f-]+/)
    await confirmCalendar(page)
    const restoreTask = await submit(page)
    expect(restoreTask).not.toBe(updateTask)
    await expect(page.getByRole('region', { name: '日程审批预览' })).toContainText('恢复')
    await approve(page, restoreTask)
    await expect.poll(() => status(page, restoreTask)).toBe('succeeded')
    for (const taskId of [createTask, updateTask, restoreTask]) {
      const facts = await evidence(page, taskId)
      expect(facts.execution_count).toBe(1)
      expect(facts.write_calls).toBe(1)
    }
  })
}

test('two editors preserve a stale user edit and reject the conflicting version', async ({ page }) => {
  const source = await seed(page, 'google')
  const draftId = await openMail(page, source, 'new')
  const other = await page.context().newPage()
  try {
    await other.goto(`/mail/drafts/${draftId}`)
    await page.getByLabel('主题', { exact: true }).fill('Synthetic first editor')
    await other.getByLabel('主题', { exact: true }).fill('Synthetic second editor')
    await page.getByRole('button', { name: '保存草稿', exact: true }).click()
    await expect(page.getByRole('button', { name: '提交审批', exact: true })).toBeEnabled()
    await other.getByRole('button', { name: '保存草稿', exact: true }).click()
    await expect(other.locator('.editor-error[role="alert"]')).toContainText('版本')
    await expect(other.getByLabel('主题', { exact: true })).toHaveValue('Synthetic second editor')
    await expect(other.getByRole('button', { name: '提交审批', exact: true })).toBeDisabled()
  } finally { await other.close() }
})

for (const decision of ['rejected', 'expired', 'invalidated'] as const) {
  test(`approval ${decision} never calls a provider`, async ({ page }) => {
    const source = await seed(page, 'microsoft')
    await openMail(page, source, 'new')
    const taskId = await submit(page)
    if (decision === 'rejected') await page.getByRole('button', { name: '拒绝', exact: true }).click()
    else if (decision === 'invalidated') await page.getByRole('button', { name: '撤回审批以继续编辑', exact: true }).click()
    else {
      expect((await post(page, '/test-support/expire-approval', { task_id: taskId })).status()).toBe(204)
      await page.reload()
    }
    await runTask(page, taskId)
    await page.reload()
    expect((await evidence(page, taskId)).write_calls).toBe(0)
    await expect(page.getByRole('button', { name: '批准', exact: true })).toBeDisabled()
  })
}

for (const resolution of ['confirmed_executed', 'confirmed_not_executed'] as const) {
  test(`unknown result ${resolution}: read-only reconciliation, keyboard dialog and mobile live status`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    const source = await seed(page, 'google')
    await scenario(page, 'never_resolved')
    await openMail(page, source, 'new')
    const taskId = await submit(page)
    await approve(page, taskId)
    await expect.poll(() => status(page, taskId)).toBe('reconciling')
    for (let attempt = 0; attempt < 4; attempt += 1) {
      expect((await post(page, '/test-support/reconcile-task', { task_id: taskId })).status()).toBe(204)
    }
    await expect.poll(() => status(page, taskId)).toBe('needs_attention')
    await page.reload()
    const start = page.getByRole('button', { name: resolution === 'confirmed_executed' ? '确认已执行' : '确认未执行', exact: true })
    await start.focus()
    await page.keyboard.press('Enter')
    const dialog = page.getByRole('dialog')
    await expect(dialog.getByRole('button', { name: '取消', exact: true })).toBeFocused()
    await page.keyboard.press('Escape')
    await expect(start).toBeFocused()
    await page.keyboard.press('Enter')
    await page.keyboard.press('Tab')
    await page.keyboard.press('Enter')
    await expect(dialog).toHaveCount(0)
    await expect.poll(() => status(page, taskId)).toBe(resolution === 'confirmed_executed' ? 'succeeded' : 'failed')
    const facts = await evidence(page, taskId)
    expect(facts.write_calls).toBe(1)
    expect(facts.reconcile_calls).toBe(4)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await expect(page.locator('[aria-live="polite"]:visible')).toContainText('实时连接')
  })
}

test('partial calendar availability retains the exact missing connection and an actionable warning', async ({ page }) => {
  const source = await seed(page, 'google')
  const unavailable = await seed(page, 'microsoft')
  for (const capability of ['calendar.write', 'calendar.read']) {
    const result = await post(page, `/connections/${unavailable.connection_id}/capabilities/${capability}/disable`)
    expect(result.status()).toBe(200)
  }
  await openCalendar(page, source, 'create')
  await confirmCalendar(page)
  const candidates = page.getByRole('region', { name: '服务端候选时间' })
  await expect(candidates).toContainText('部分日历来源缺失')
  await expect(candidates).toContainText('Microsoft')
  await expect(candidates.getByRole('link', { name: '查看连接与同步状态', exact: true })).toHaveAttribute('href', '/connections')
  await expect(candidates.getByRole('list', { name: '缺失日历账户' })).toContainText(`synthetic-${unavailable.connection_id}@example.test`)
  await expect(candidates).toContainText('未检查参会人可用性')
})

for (const provider of ['google', 'microsoft'] as const) {
  test(`${provider} capability reauthorization uses real callbacks and exposes admin-consent recovery`, async ({ page }) => {
    await page.goto('/connections')
    await page.getByRole('button', { name: `连接 ${provider === 'google' ? 'Google' : 'Microsoft'}`, exact: true }).click()
    const connected = await completeOAuth(page, provider)
    expect(connected.status()).toBe(200)
    const { connection_id: connectionId } = await connected.json() as { connection_id: string }
    await page.getByRole('button', { name: '刷新连接', exact: true }).click()
    const card = page.locator(`[data-connection-id="${connectionId}"]`)
    const capability = provider === 'google' ? 'mail.read' : 'mail.send'
    const row = card.locator(`[data-capability="${capability}"]`)
    if (provider === 'google') await row.getByRole('button', { name: `关闭 ${capability}`, exact: true }).click()
    await row.getByRole('button', { name: `启用 ${capability}`, exact: true }).click()
    const denied = await completeOAuth(page, provider, provider === 'microsoft' ? 'consent_required' : 'access_denied')
    expect(denied.status()).toBeGreaterThanOrEqual(400)
    expect(denied.status()).toBeLessThan(500)
    await page.getByRole('button', { name: '刷新连接', exact: true }).click()
    await expect(row).toContainText('需要操作')
    if (provider === 'microsoft') {
      await expect(row).toContainText('microsoft_admin_consent_required')
      await expect(row).toContainText('请联系管理员批准所需委托权限')
    }
    await row.getByRole('button', { name: `重新授权 ${capability}`, exact: true }).click()
    expect((await completeOAuth(page, provider)).status()).toBe(200)
    await page.getByRole('button', { name: '刷新连接', exact: true }).click()
    await expect(row.getByRole('status')).toHaveText('已启用')
  })
}

test('delayed reconciliation reconnects real SSE and restores the durable terminal snapshot', async ({ page }) => {
  const source = await seed(page, 'microsoft')
  await scenario(page, 'delayed_reconciliation_success')
  await openMail(page, source, 'new')
  const taskId = await submit(page)
  await approve(page, taskId)
  await expect.poll(() => status(page, taskId)).toBe('reconciling')
  const proxy = await streamProxy(`/api/v1/tasks/${taskId}/events`)
  try {
    await page.goto(`${proxy.origin}/actions?task=${taskId}`)
    await expect(page.locator('[aria-live="polite"]:visible').filter({ hasText: '实时连接' })).toContainText('核对')
    await expect(page.locator('[aria-live="polite"]:visible').filter({ hasText: '实时连接' })).toContainText('实时连接正常')
    await expect.poll(() => proxy.cursors.length).toBe(1)
    // Chromium 的 offline 模式可能保留既有长流；此处实际断开本测试拥有的转发 socket。
    proxy.disconnect()
    await expect(page.getByText('正在重新连接', { exact: false })).toBeVisible()
    for (let attempt = 0; attempt < 2; attempt += 1) {
      expect((await post(page, '/test-support/reconcile-task', { task_id: taskId })).status()).toBe(204)
    }
    expect(await status(page, taskId)).toBe('succeeded')
    proxy.resume()
    await expect(page.locator('.action-detail')).toContainText('已完成')
    await expect.poll(() => proxy.cursors.slice(1).some((cursor) => cursor !== null && BigInt(cursor) > 0n)).toBe(true)
    const facts = await evidence(page, taskId)
    expect(facts.write_calls).toBe(1)
    expect(facts.reconcile_calls).toBe(2)
    await page.reload()
    await expect(page.locator('.action-detail')).toContainText('已完成')
  } finally {
    await page.goto('/actions')
    await proxy.close()
  }
})
