import { expect, test } from '@playwright/test'
import type { ActionSnapshot, MailDraft } from '../src/api/types'
import type { UpdateMailDraftInput } from '../src/api/mail'
import {
  actionSnapshot,
  DRAFT_ID,
  mailDraft,
  NOW,
  TASK_ID,
} from '../src/test-support/actionFixtures'
import { connection, mailApproval } from '../src/test-support/editorFixtures'
import { editorAction, editorJson, editorWorkspace } from './fixtures/editorApi'

/** 路由 fixture 验证浏览器交互与精确请求；后端持久化和供应商语义另由 HTTP/数据库测试证明。 */
test('new mail stays local until explicit submission and can withdraw its frozen approval', async ({
  page,
}) => {
  const capture = await editorWorkspace(page)
  let draft: MailDraft = mailDraft(),
    snapshot: ActionSnapshot | null = null
  const microsoft = connection('microsoft')
  await editorAction(page, () => snapshot)
  await page.route('**/api/v1/mail/drafts', async (route) => {
    expect(route.request().postDataJSON()).toEqual({ mode: 'new' })
    await editorJson(route, draft, 201)
  })
  await page.route(`**/api/v1/mail/drafts/${DRAFT_ID}`, async (route) => {
    if (route.request().method() === 'PATCH') {
      const input = route.request().postDataJSON() as UpdateMailDraftInput
      expect(input.version).toBe(1)
      expect(input.connection_id).toBe(microsoft.id)
      draft = { ...draft, ...input, version: 2 }
    }
    await editorJson(route, draft)
  })
  await page.route(
    `**/api/v1/mail/drafts/${DRAFT_ID}/submit`,
    async (route) => {
      expect(route.request().postDataJSON()).toEqual({ version: 2 })
      draft = { ...draft, status: 'awaiting_approval' }
      snapshot = actionSnapshot({
        status: 'waiting_approval',
        provider: 'microsoft',
        error_code: null,
        event_cursor: '10',
        task_version: '10',
        execution: null,
        timeline: [],
        provider_url: null,
        local_action: {
          id: DRAFT_ID,
          item_kind: 'mail_draft',
          version: 2,
          status: 'awaiting_approval',
          editor_url: `/mail/drafts/${DRAFT_ID}`,
        },
        approval: {
          ...mailApproval(),
          proposal_version: 2,
          content_status: 'available',
          preview: {
            kind: 'mail',
            provider: 'microsoft',
            account_email: microsoft.account_email,
            mode: 'new',
            to: draft.to,
            cc: draft.cc,
            bcc: draft.bcc,
            subject: draft.subject,
            body_text: draft.body_text,
            irreversible: true,
          },
        },
      })
      await editorJson(route, { task_id: TASK_ID, status: 'queued' }, 202)
    },
  )
  await page.route(`**/api/v1/tasks/${TASK_ID}/cancel`, async (route) => {
    if (!snapshot?.approval)
      throw new Error('Synthetic frozen action is missing')
    draft = { ...draft, status: 'editing' }
    snapshot = {
      ...snapshot,
      status: 'cancelled',
      event_cursor: '11',
      task_version: '11',
      local_action: {
        id: DRAFT_ID,
        item_kind: 'mail_draft',
        version: 2,
        status: 'editing',
        editor_url: `/mail/drafts/${DRAFT_ID}`,
      },
      approval: { ...snapshot.approval, status: 'invalidated' },
    }
    await editorJson(route, {
      id: TASK_ID,
      kind: 'trusted_action',
      status: 'cancelled',
      retry_of_task_id: null,
      error_code: null,
      event_cursor: '11',
      steps: [],
    })
  })

  await page.goto('/actions')
  expect(capture.mutations).toHaveLength(0)
  await page.getByRole('button', { name: '新邮件', exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`/mail/drafts/${DRAFT_ID}$`))
  await page.getByLabel('发送账户', { exact: true }).selectOption(microsoft.id)
  await page
    .getByLabel('收件人 To', { exact: true })
    .fill('recipient@example.test')
  await page.getByLabel('抄送 CC', { exact: true }).fill('copy@example.test')
  await page.getByLabel('密送 BCC', { exact: true }).fill('blind@example.test')
  await expect(page.getByTestId('recipient-count')).toHaveText(
    '当前收件人数：3 位',
  )
  const bccInput = page.getByLabel('密送 BCC', { exact: true })
  const originalBcc = await bccInput.inputValue()
  await bccInput.fill(`${originalBcc}, synthetic-extra@example.test`)
  await expect(page.getByTestId('recipient-count')).toHaveText(
    '当前收件人数：4 位',
  )
  await bccInput.fill(
    await page.getByLabel('收件人 To', { exact: true }).inputValue(),
  )
  await expect(page.getByTestId('recipient-count')).toContainText(
    '收件人数待核对',
  )
  await bccInput.fill(originalBcc)
  await expect(page.getByTestId('recipient-count')).toHaveText(
    '当前收件人数：3 位',
  )
  await page
    .getByLabel('主题', { exact: true })
    .fill('Synthetic subject browser review')
  await page
    .getByLabel('纯文本正文', { exact: true })
    .fill('Synthetic body <strong>plain text</strong>')
  await expect(
    page.getByRole('button', { name: '提交审批', exact: true }),
  ).toBeDisabled()
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect(page.getByText('版本 2', { exact: false })).toBeVisible()
  expect(capture.mutations.map((item) => item.path)).toEqual([
    '/mail/drafts',
    `/mail/drafts/${DRAFT_ID}`,
  ])
  await page.getByRole('button', { name: '提交审批', exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`/actions\\?task=${TASK_ID}$`))
  const preview = page.getByRole('region', { name: '邮件审批预览' })
  await expect(preview).toContainText('Microsoft')
  await expect(preview).toContainText('不可撤回')
  expect(
    await preview
      .locator('.plain-text')
      .evaluate((node) => node.children.length),
  ).toBe(0)
  await expect(page.locator('.action-detail pre')).toHaveCount(0)
  expect(
    capture.mutations.filter((item) => item.path.endsWith('/decision')),
  ).toHaveLength(0)
  const creation = capture.mutations.find(
    (item) => item.path === '/mail/drafts',
  )
  const submission = capture.mutations.find((item) =>
    item.path.endsWith('/submit'),
  )
  expect(creation?.intent).toMatch(/^[0-9a-f-]{36}$/)
  expect(submission?.intent).toMatch(/^[0-9a-f-]{36}$/)
  expect(creation?.intent).not.toBe(submission?.intent)
  await page.getByRole('button', { name: '撤回审批以继续编辑' }).click()
  await expect(page.locator('.action-detail')).toContainText('已取消')
  await page.getByRole('link', { name: '打开邮件草稿', exact: true }).click()
  await expect(page.getByLabel('纯文本正文', { exact: true })).toBeEnabled()
  expect(
    capture.mutations.filter((item) => item.method === 'PATCH'),
  ).toHaveLength(1)
  expect(
    capture.mutations.filter((item) => item.path.endsWith('/cancel')),
  ).toHaveLength(1)
  expect(
    await page.evaluate(() => [localStorage.length, sessionStorage.length]),
  ).toEqual([0, 0])
  expect(capture.unexpected).toEqual([])
  expect(capture.pageErrors).toEqual([])
})

test('narrow keyboard confirmation sends only an enum and waits for the authoritative result', async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const capture = await editorWorkspace(page)
  let snapshot = actionSnapshot(),
    resolved = false
  let releaseRead: () => void = () => undefined
  const readAllowed = new Promise<void>((resolve) => {
    releaseRead = resolve
  })
  await editorAction(page, () => snapshot)
  await page.route(`**/api/v1/actions/${TASK_ID}`, async (route) => {
    if (resolved) await readAllowed
    await editorJson(route, snapshot)
  })
  await page.route(`**/api/v1/actions/${TASK_ID}/reconcile`, async (route) => {
    snapshot = {
      ...snapshot,
      event_cursor: '9007199254740994',
      task_version: '9007199254740994',
      reconciliation_attempt_count: 3,
      execution: snapshot.execution
        ? { ...snapshot.execution, reconciliation_attempt_count: 3 }
        : null,
    }
    await editorJson(route, { task_id: TASK_ID }, 202)
  })
  await page.route(
    `**/api/v1/actions/${TASK_ID}/manual-resolution`,
    async (route) => {
      expect(route.request().postDataJSON()).toEqual({
        resolution: 'confirmed_not_executed',
        task_version: '9007199254740994',
      })
      resolved = true
      snapshot = {
        ...snapshot,
        status: 'failed',
        error_code: 'provider_write_confirmed_not_applied',
        event_cursor: '9007199254740995',
        task_version: '9007199254740995',
        local_action: {
          id: DRAFT_ID,
          item_kind: 'mail_draft',
          version: 3,
          status: 'editing',
          editor_url: `/mail/drafts/${DRAFT_ID}`,
        },
        execution: snapshot.execution
          ? {
              ...snapshot.execution,
              status: 'confirmed_failed',
              manual_resolution: 'confirmed_not_executed',
              completed_at: NOW,
            }
          : null,
        timeline: [
          {
            id: '9007199254740995',
            event: 'tool.manually_resolved',
            occurred_at: NOW,
            payload: { status: 'failed' },
          },
        ],
      }
      await editorJson(route, {
        task_id: TASK_ID,
        task_version: '9007199254740995',
      })
    },
  )

  await page.goto(`/actions?task=${TASK_ID}`)
  await page.getByRole('button', { name: '重新核对', exact: true }).click()
  await expect(
    page.getByRole('region', { name: '人工结果确认' }),
  ).toContainText('核对尝试：3')
  const start = page.getByRole('button', { name: '确认未执行', exact: true })
  await start.focus()
  await page.keyboard.press('Enter')
  const dialog = page.getByRole('dialog')
  await expect(dialog).toContainText('不会调用供应商写接口')
  await expect(dialog).toContainText('不会自动重发')
  await expect(
    dialog.getByRole('button', { name: '取消', exact: true }),
  ).toBeFocused()
  await page.keyboard.press('Shift+Tab')
  await expect(
    dialog.getByRole('button', { name: '确认记录结果' }),
  ).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
  await expect(start).toBeFocused()
  expect(
    capture.mutations.filter((item) =>
      item.path.endsWith('/manual-resolution'),
    ),
  ).toHaveLength(0)
  await page.keyboard.press('Enter')
  await expect(
    dialog.getByRole('button', { name: '取消', exact: true }),
  ).toBeFocused()
  await page.keyboard.press('Tab')
  await page.keyboard.press('Enter')
  await expect
    .poll(
      () =>
        capture.mutations.filter((item) =>
          item.path.endsWith('/manual-resolution'),
        ).length,
    )
    .toBe(1)
  await expect(page.locator('.action-detail')).not.toContainText('人工结论：')
  await expect(
    dialog.getByRole('button', { name: '确认记录结果' }),
  ).toBeDisabled()
  releaseRead()
  await expect(page.locator('.action-detail')).toContainText(
    '人工结论：确认未执行',
  )
  await expect(page.getByRole('dialog')).toHaveCount(0)
  expect(capture.mutations.map((item) => item.path)).toEqual([
    `/actions/${TASK_ID}/reconcile`,
    `/actions/${TASK_ID}/manual-resolution`,
  ])
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true)
  expect(capture.unexpected).toEqual([])
  expect(capture.pageErrors).toEqual([])
})
