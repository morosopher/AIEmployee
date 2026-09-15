import { expect, test, type APIResponse, type Page } from '@playwright/test'
import { appendFileSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { parseMailDraft } from '../src/api/mail'
import { parseCalendarProposal } from '../src/api/calendar'

/** 只由敏感输出 gate 的独立配置收集；常规 E2E 不需要私有输入或跳过此测试。 */
type CanaryField = 'address' | 'subject' | 'body' | 'title' | 'description' | 'location' | 'attendee' | 'token' | 'cookie' | 'prompt'
const fields: CanaryField[] = ['address', 'subject', 'body', 'title', 'description', 'location', 'attendee', 'token', 'cookie', 'prompt']

/** 所有比较只输出布尔值/字段名，私有输入绝不放入 test title、expect 对象或附件。 */
function privateInputs(): { directory: string; canaries: Record<CanaryField, string> } {
  const directory = process.env.M2_SENSITIVE_OUTPUT_DIRECTORY
  const input = process.env.M2_SENSITIVE_INPUT_FILE
  if (!directory || !input) throw new Error('Sensitive-output test input is absent')
  const value = JSON.parse(readFileSync(input, 'utf8')) as Record<CanaryField, unknown>
  if (fields.some((field) => typeof value[field] !== 'string')) throw new Error('Sensitive-output test input is invalid')
  return { directory, canaries: value as Record<CanaryField, string> }
}

/** 使用真实会话与 CSRF；APIRequestContext 继承浏览器 Cookie 与测试 Authorization。 */
async function post(page: Page, path: string, data?: unknown): Promise<APIResponse> {
  const csrf = (await page.context().cookies()).find((cookie) => cookie.name === 'ai_employee_csrf')?.value
  if (!csrf) throw new Error('Sensitive-output test session is absent')
  return page.request.post(`/api/v1${path}`, { headers: { 'X-CSRF-Token': csrf, 'Idempotency-Key': crypto.randomUUID() }, ...(data === undefined ? {} : { data }) })
}

test('real editors and conversation keep ten sensitive fields out of observable output', async ({ page, context }) => {
  const { directory, canaries } = privateInputs()
  const browserLog = join(directory, 'browser.jsonl')
  writeFileSync(browserLog, '', { mode: 0o600, flag: 'wx' })
  // 原样保存真实 browser console/pageerror。若产品输出 canary，扫描器必须看到并失败。
  page.on('console', (message) => appendFileSync(browserLog, JSON.stringify({ channel: 'console', level: message.type(), text: message.text() }) + '\n'))
  page.on('pageerror', (error) => appendFileSync(browserLog, JSON.stringify({ channel: 'pageerror', text: error.stack ?? error.message }) + '\n'))
  await context.setExtraHTTPHeaders({ Authorization: `Bearer ${canaries.token}` })
  await context.addCookies([{ name: 'm2_sensitive_canary', value: canaries.cookie, url: 'http://127.0.0.1:5173', sameSite: 'Lax' }])
  const email = process.env.E2E_ADMIN_EMAIL
  const password = process.env.E2E_ADMIN_PASSWORD_FILE
  if (!email || !password) throw new Error('Sensitive-output login configuration is absent')
  await page.goto('/login')
  await page.getByLabel('邮箱', { exact: true }).fill(email)
  await page.getByLabel('密码', { exact: true }).fill(readFileSync(password, 'utf8').trimEnd())
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page).not.toHaveURL(/\/login/)

  const seeded = await post(page, '/test-support/seed-m2-source', { provider: 'google' })
  expect(seeded.status()).toBe(200)
  const source = await seeded.json() as { connection_id: string; calendar_id: string; starts_at: string; ends_at: string }
  const created = await post(page, '/mail/drafts', { mode: 'new', connection_id: source.connection_id })
  expect(created.status()).toBe(201)
  const draftId = parseMailDraft(await created.json()).id
  await page.goto(`/mail/drafts/${draftId}`)
  await page.getByLabel('收件人 To', { exact: true }).fill(canaries.address)
  await page.getByLabel('主题', { exact: true }).fill(canaries.subject)
  await page.getByLabel('纯文本正文', { exact: true }).fill(canaries.body)
  const mailSaved = page.waitForResponse((response) => response.url().endsWith(`/mail/drafts/${draftId}`) && response.request().method() === 'PATCH')
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  expect((await mailSaved).status()).toBe(200)
  const mailRead = await page.request.get(`/api/v1/mail/drafts/${draftId}`)
  expect(mailRead.status()).toBe(200)
  const mail = parseMailDraft(await mailRead.json())
  expect(mail.to.includes(canaries.address), 'address reached durable editor').toBe(true)
  expect(mail.subject === canaries.subject, 'subject reached durable editor').toBe(true)
  expect(mail.body_text === canaries.body, 'body reached durable editor').toBe(true)

  const calendarCreated = await post(page, '/calendar/proposals', {
    operation_kind: 'create', connection_id: source.connection_id, calendar_id: source.calendar_id,
    title: 'Synthetic initial title', description: '', location: '', starts_at: source.starts_at,
    ends_at: source.ends_at, timezone: 'UTC', all_day: false, attendees: [], notification_policy: 'all',
  })
  expect(calendarCreated.status()).toBe(201)
  const proposalId = parseCalendarProposal(await calendarCreated.json()).id
  await page.goto(`/calendar/proposals/${proposalId}`)
  for (const [label, field] of [['日程标题', 'title'], ['描述', 'description'], ['地点', 'location'], ['参会人', 'attendee']] as const) {
    await page.getByLabel(label, { exact: true }).fill(canaries[field])
  }
  const calendarSaved = page.waitForResponse((response) => response.url().endsWith(`/calendar/proposals/${proposalId}`) && response.request().method() === 'PATCH')
  await page.getByRole('button', { name: '保存提案', exact: true }).click()
  expect((await calendarSaved).status()).toBe(200)
  const calendarRead = await page.request.get(`/api/v1/calendar/proposals/${proposalId}`)
  expect(calendarRead.status()).toBe(200)
  const calendar = parseCalendarProposal(await calendarRead.json())
  expect(calendar.title === canaries.title, 'title reached durable editor').toBe(true)
  expect(calendar.description === canaries.description, 'description reached durable editor').toBe(true)
  expect(calendar.location === canaries.location, 'location reached durable editor').toBe(true)
  expect(calendar.attendees.includes(canaries.attendee), 'attendee reached durable editor').toBe(true)

  const conversationCreated = await post(page, '/conversations')
  expect(conversationCreated.status()).toBe(201)
  const conversation = await conversationCreated.json() as { id: string }
  const message = await post(page, `/conversations/${conversation.id}/messages`, { content_markdown: canaries.prompt, client_request_id: crypto.randomUUID() })
  expect(message.status()).toBe(202)
  const task = await message.json() as { task_id: string }
  expect((await post(page, '/test-support/execute-task', { task_id: task.task_id })).status()).toBe(204)
  const conversationRead = await page.request.get(`/api/v1/conversations/${conversation.id}`)
  expect(conversationRead.status()).toBe(200)
  const messages = await conversationRead.json() as { messages: { content_markdown: string }[] }
  expect(messages.messages.some((value) => value.content_markdown === canaries.prompt), 'prompt reached durable conversation').toBe(true)

  const started = await post(page, '/connections/google/start')
  expect(started.status()).toBe(200)
  const authorization = await started.json() as { authorization_url: string }
  const state = new URL(authorization.authorization_url).searchParams.get('state')
  if (!state) throw new Error('Synthetic OAuth state is absent')
  const callback = await page.request.get('/api/v1/connections/google/callback', { params: { state, error: 'access_denied', error_description: canaries.token } })
  expect(callback.status()).toBe(422)

  const metrics = await page.request.get('http://127.0.0.1:8000/metrics')
  expect(metrics.status()).toBe(200)
  writeFileSync(join(directory, 'metrics.txt'), await metrics.body(), { mode: 0o600, flag: 'wx' })
  // 此回执来自真实 ASGI request scope 的比较，不以浏览器配置成功替代服务器收到证明。
  const transport = JSON.parse(readFileSync(join(directory, 'transport-reached.json'), 'utf8')) as { token: boolean; cookie: boolean }
  expect(transport.token, 'token reached real ASGI request').toBe(true)
  expect(transport.cookie, 'cookie reached real ASGI request').toBe(true)
  writeFileSync(join(directory, 'reached.json'), JSON.stringify(Object.fromEntries(fields.map((field) => [field, true]))), { mode: 0o600, flag: 'wx' })
})
