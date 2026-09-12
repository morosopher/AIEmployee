import { expect, test } from '@playwright/test'
import type { Brief, Message, UserSettings } from '../src/api/types'
import {
  calendarProposal,
  DRAFT_ID,
  mailDraft,
  NOW,
  PROPOSAL_ID,
  TASK_ID,
  userSettings,
} from '../src/test-support/actionFixtures'
import {
  calendarFields,
  connection,
  connectionCapabilities,
} from '../src/test-support/editorFixtures'
import { editorJson, editorWorkspace } from './fixtures/editorApi'

/** 每种简报来源单独验证本地主键绑定；点击前没有 mutation，点击后也不产生审批。 */
for (const kind of ['mail.reply', 'calendar.update'] as const) {
  test(`brief source explicitly creates ${kind} in its focused editor`, async ({
    page,
  }) => {
    const capture = await editorWorkspace(page)
    const sourceId = '00000000-0000-0000-0000-000000000601'
    const brief: Brief = {
      id: '00000000-0000-0000-0000-000000000602',
      local_date: '2030-01-01',
      version: 1,
      task_id: '00000000-0000-0000-0000-000000000603',
      source_cutoff: NOW,
      completeness: 'complete',
      headline: '合成来源建议',
      structured_content: {},
      markdown: '',
      warnings: [],
      items: [
        {
          position: 1,
          section: '建议',
          priority: 'high',
          title: 'Synthetic title source suggestion',
          body_markdown: '',
          source_refs: [
            {
              source_type:
                kind === 'mail.reply' ? 'email_thread' : 'calendar_event',
              source_id: sourceId,
              provider_url: null,
            },
          ],
          suggested_action_kind: kind,
        },
      ],
    }
    const draft = {
      ...mailDraft(),
      mode: 'reply' as const,
      source_thread_id: 'synthetic-provider-thread',
      subject: 'Synthetic subject reply',
    }
    const before = calendarFields()
    const proposal = {
      ...calendarProposal(),
      ...before,
      operation_kind: 'update' as const,
      calendar_id: 'synthetic-google-calendar',
      target_event_id: 'synthetic-provider-event',
      base_etag: 'synthetic-etag',
      before_snapshot_id: '00000000-0000-0000-0000-000000000604',
      editor_facts: {
        reprepare_source: null,
        restore_source: null,
        before_status: 'available' as const,
        before,
        conflict_status: 'checked' as const,
        conflicts: [],
      },
    }
    await page.route('**/api/v1/briefs/today', (route) =>
      editorJson(route, brief),
    )
    await page.route('**/api/v1/briefs?*', (route) =>
      editorJson(route, [brief]),
    )
    await page.route('**/api/v1/mail/drafts', async (route) => {
      expect(route.request().postDataJSON()).toEqual({
        mode: 'reply',
        source_thread_id: sourceId,
      })
      await editorJson(route, draft, 201)
    })
    await page.route(`**/api/v1/mail/drafts/${DRAFT_ID}`, (route) =>
      editorJson(route, draft),
    )
    await page.route('**/api/v1/calendar/proposals', async (route) => {
      expect(route.request().postDataJSON()).toEqual({
        operation_kind: 'update',
        initialization: 'shell',
        event_id: sourceId,
      })
      await editorJson(route, { ...proposal, editor_facts: null }, 201)
    })
    await page.route(`**/api/v1/calendar/proposals/${PROPOSAL_ID}`, (route) =>
      editorJson(route, proposal),
    )
    await page.goto('/brief')
    await expect(
      page.locator('button[name="prepare-source-action"]'),
    ).toHaveCount(1)
    expect(capture.mutations).toHaveLength(0)
    await page.locator('button[name="prepare-source-action"]').click()
    const editorPath =
      kind === 'mail.reply'
        ? `/mail/drafts/${DRAFT_ID}`
        : `/calendar/proposals/${PROPOSAL_ID}`
    await expect(page).toHaveURL(new RegExp(`${editorPath}$`))
    if (kind === 'mail.reply') {
      await expect(page.getByLabel('发送账户', { exact: true })).toBeDisabled()
      await expect(page.getByLabel('主题', { exact: true })).toBeDisabled()
    } else
      await expect(page.getByLabel('目标日历', { exact: true })).toBeDisabled()
    expect(capture.mutations).toHaveLength(1)
    expect(capture.mutations[0]?.intent).toMatch(/^[0-9a-f-]{36}$/)
    expect(capture.unexpected).toEqual([])
    expect(capture.pageErrors).toEqual([])
  })
}

test('chat follows a prepared local API link and rejects arbitrary paths without creating an approval', async ({
  page,
}) => {
  const capture = await editorWorkspace(page)
  const conversation = {
    id: '00000000-0000-0000-0000-000000000501',
    title: '合成会话',
    created_at: NOW,
    updated_at: NOW,
  }
  let messages: Message[] = []
  await page.route('**/api/v1/conversations', (route) =>
    editorJson(route, [conversation]),
  )
  await page.route(`**/api/v1/conversations/${conversation.id}`, (route) =>
    editorJson(route, { conversation, messages }),
  )
  await page.route(
    `**/api/v1/conversations/${conversation.id}/messages`,
    async (route) => {
      expect(route.request().postDataJSON()).toEqual({
        content_markdown: '请准备一封邮件草稿',
        client_request_id: expect.any(String),
      })
      messages = [
        {
          id: '00000000-0000-0000-0000-000000000502',
          role: 'assistant',
          task_id: TASK_ID,
          created_at: NOW,
          content_markdown: `[打开本地草稿](/api/v1/mail/drafts/${DRAFT_ID})\n\n[不可信路径](/settings) [不可信跳转](/api/v1/mail/drafts/${DRAFT_ID}?next=https://example.test) [脚本](javascript:alert(1)) [外部资料](https://example.test/guide)`,
        },
      ]
      await editorJson(route, { task_id: TASK_ID }, 202)
    },
  )
  await page.route(`**/api/v1/tasks/${TASK_ID}`, (route) =>
    editorJson(route, {
      id: TASK_ID,
      kind: 'prepare_mail_draft',
      status: 'succeeded',
      retry_of_task_id: null,
      error_code: null,
      event_cursor: '1',
      steps: [],
    }),
  )
  await page.route(`**/api/v1/tasks/${TASK_ID}/events*`, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      // 真实 SSE 会重放持久终态事件；它触发任务 GET 读取 kind，空心跳不能代替这一事实。
      body: `retry: 60000\n\nid: 1\nevent: task.status_changed\ndata: ${JSON.stringify({ id: '1', sequence: '1', task_id: TASK_ID, event: 'task.status_changed', occurred_at: NOW, step_id: null, payload: { status: 'succeeded' } })}\n\nevent: heartbeat\ndata: {}\n\n`,
    }),
  )
  await page.route(`**/api/v1/mail/drafts/${DRAFT_ID}`, (route) =>
    editorJson(route, mailDraft()),
  )
  await page.goto('/chat')
  await page.getByLabel('消息', { exact: true }).fill('请准备一封邮件草稿')
  expect(capture.mutations).toHaveLength(0)
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await expect(page.locator('a[data-editor-link]')).toHaveCount(1)
  await expect(page.locator('a[data-editor-link]')).toHaveAttribute(
    'href',
    `/mail/drafts/${DRAFT_ID}`,
  )
  await expect(page.getByTestId('chat-task-status')).not.toContainText('已发送')
  await expect(page.getByRole('region', { name: '人工审批' })).toHaveCount(0)
  const external = page.getByRole('link', { name: '外部资料' })
  await expect(external).toHaveAttribute('target', '_blank')
  await expect(external).toHaveAttribute('rel', 'noopener noreferrer')
  await expect(
    page.locator('.markdown-message a[href^="javascript:"]'),
  ).toHaveCount(0)
  await page.getByRole('link', { name: '打开本地草稿' }).click()
  await expect(page).toHaveURL(new RegExp(`/mail/drafts/${DRAFT_ID}$`))
  await expect(page.getByLabel('纯文本正文', { exact: true })).toBeVisible()
  expect(capture.mutations.map((item) => item.path)).toEqual([
    `/conversations/${conversation.id}/messages`,
  ])
  expect(capture.unexpected).toEqual([])
  expect(capture.pageErrors).toEqual([])
})

test('both provider capabilities and explicit work defaults are usable with bounded working hours', async ({
  page,
}) => {
  const capture = await editorWorkspace(page)
  const microsoft = connection('microsoft'),
    google = connection()
  const capabilities = connectionCapabilities('microsoft')
  capabilities.capabilities = capabilities.capabilities.map((row) =>
    row.capability === 'calendar.write'
      ? {
          ...row,
          status: 'action_required',
          last_error_code: 'admin_consent_required',
        }
      : row,
  )
  await page.route(
    `**/api/v1/connections/${microsoft.id}/capabilities`,
    (route) => editorJson(route, capabilities),
  )
  await page.route('**/api/v1/connections/google/start', (route) =>
    editorJson(route, {
      authorization_url: 'https://accounts.google.com/o/oauth2/v2/auth',
    }),
  )
  await page.route('**/api/v1/connections/microsoft/start', (route) =>
    editorJson(route, {
      authorization_url:
        'https://login.microsoftonline.com/common/oauth2/v2.0/authorize',
    }),
  )
  await page.route(
    `**/api/v1/connections/${microsoft.id}/capabilities/calendar.write/enable`,
    (route) =>
      editorJson(route, {
        authorization_url:
          'https://login.microsoftonline.com/common/oauth2/v2.0/authorize',
        requested_capabilities: ['calendar.read', 'calendar.write'],
      }),
  )
  let settings = userSettings()
  await page.route('**/api/v1/auth/sessions', (route) => editorJson(route, []))
  await page.route('**/api/v1/settings', async (route) => {
    if (route.request().method() === 'PATCH') {
      const input = route.request().postDataJSON() as Omit<
        UserSettings,
        'updated_at'
      >
      expect(input.default_mail_connection_id).toBe(microsoft.id)
      expect(input.default_calendar_connection_id).toBe(google.id)
      expect(input.default_calendar_id).toBe('synthetic-google-calendar')
      expect(input.meeting_buffer_minutes).toBe(30)
      expect(input.working_hours.tuesday).toEqual([['10:00', '16:00']])
      expect(Object.keys(input.working_hours)).toHaveLength(7)
      settings = { ...input, updated_at: NOW }
    }
    await editorJson(route, settings)
  })
  await page.goto('/connections')
  const googleCard = page.locator(`[data-connection-id="${google.id}"]`)
  const microsoftCard = page.locator(`[data-connection-id="${microsoft.id}"]`)
  await expect(googleCard.locator('[data-capability]')).toHaveCount(4)
  await expect(microsoftCard.locator('[data-capability]')).toHaveCount(4)
  await expect(
    googleCard.locator('button[name="disable-mail.read"]'),
  ).toBeDisabled()
  await expect(microsoftCard).toContainText('管理员同意')
  await page.getByRole('button', { name: '连接 Google', exact: true }).click()
  await expect(page.getByTestId('authorization-link')).toHaveAttribute(
    'href',
    'https://accounts.google.com/o/oauth2/v2/auth',
  )
  await page
    .getByRole('button', { name: '连接 Microsoft', exact: true })
    .click()
  await expect(page.getByTestId('authorization-link')).toHaveAttribute(
    'href',
    'https://login.microsoftonline.com/common/oauth2/v2.0/authorize',
  )
  await microsoftCard.locator('button[name="enable-calendar.write"]').click()
  await expect(
    page.getByText(
      '本次授权包含：calendar.read、calendar.write。完成后请刷新连接状态。',
      { exact: true },
    ),
  ).toBeVisible()
  await page.goto('/settings')
  await page
    .getByRole('combobox', { name: '默认发送账户', exact: true })
    .selectOption(microsoft.id)
  await page
    .getByRole('combobox', { name: '默认日历账户', exact: true })
    .selectOption(google.id)
  await page
    .getByRole('combobox', { name: '默认日历', exact: true })
    .selectOption('synthetic-google-calendar')
  await page.locator('input[name="meeting-buffer"]').fill('121')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  expect(
    capture.mutations.filter((item) => item.path === '/settings'),
  ).toHaveLength(0)
  await page.locator('input[name="meeting-buffer"]').fill('30')
  await page
    .getByRole('button', { name: '添加星期二区间', exact: true })
    .click()
  await page.locator('input[name="tuesday-start-0"]').fill('10:00')
  await page.locator('input[name="tuesday-end-0"]').fill('16:00')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByText('已保存', { exact: true })).toBeVisible()
  expect(
    capture.mutations.filter((item) => item.path === '/settings'),
  ).toHaveLength(1)
  expect(capture.unexpected).toEqual([])
  expect(capture.pageErrors).toEqual([])
})
