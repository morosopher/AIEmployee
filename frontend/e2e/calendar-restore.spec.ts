import { expect, test } from '@playwright/test'
import type { ActionSnapshot, CalendarProposal } from '../src/api/types'
import {
  actionSnapshot,
  calendarProposal,
  PROPOSAL_ID,
  TASK_ID,
} from '../src/test-support/actionFixtures'
import {
  calendarApproval,
  calendarFields,
  connection,
} from '../src/test-support/editorFixtures'
import { editorAction, editorJson, editorWorkspace } from './fixtures/editorApi'

const EVENT_ID = '00000000-0000-0000-0000-000000000501'
const SNAPSHOT_ID = '00000000-0000-0000-0000-000000000502'
const RESTORE_ID = '00000000-0000-0000-0000-000000000503'
const PREPARE_TASK_ID = '00000000-0000-0000-0000-000000000504'

/** 原生浏览器走真实202形状、持久SSE终态和刷新读取；所有业务对象及网络响应均为合成fixture。 */
test('calendar restore follows preparation, reloads its exact result and requires a new approval', async ({
  page,
}) => {
  const capture = await editorWorkspace(page)
  const historical = calendarFields()
  const current = { ...calendarFields(), location: 'Synthetic current room' }
  const original: CalendarProposal = {
    ...calendarProposal(),
    ...current,
    operation_kind: 'update',
    status: 'applied',
    calendar_id: 'synthetic-google-calendar',
    target_event_id: 'synthetic-provider-event',
    base_etag: 'synthetic-original-etag',
    before_snapshot_id: SNAPSHOT_ID,
    required_confirmations: [],
    notification_policy: 'all',
    editor_facts: {
      before_status: 'available',
      before: historical,
      conflict_status: 'checked',
      conflicts: [],
      restore_source: { event_id: EVENT_ID, snapshot_id: SNAPSHOT_ID },
    },
  }
  let restore: CalendarProposal = {
    ...original,
    ...historical,
    id: RESTORE_ID,
    operation_kind: 'restore',
    status: 'editing',
    base_etag: 'synthetic-current-etag',
    before_snapshot_id: '00000000-0000-0000-0000-000000000505',
    required_confirmations: ['notification_policy'],
    changed_fields: ['location'],
    field_diffs: [{ field: 'location', changed: true }],
    editor_facts: {
      before_status: 'available',
      before: current,
      conflict_status: 'checked',
      conflicts: [],
      restore_source: null,
    },
  }
  let snapshot: ActionSnapshot | null = null
  await editorAction(page, () => snapshot)
  await page.route(`**/api/v1/calendar/proposals/${PROPOSAL_ID}`, (route) =>
    editorJson(route, original),
  )
  await page.route(
    `**/api/v1/calendar/events/${EVENT_ID}/restore-proposal`,
    async (route) => {
      expect(route.request().method()).toBe('POST')
      expect(route.request().postDataJSON()).toEqual({
        snapshot_id: SNAPSHOT_ID,
      })
      await editorJson(
        route,
        { task_id: PREPARE_TASK_ID, status: 'queued' },
        202,
      )
    },
  )
  let prepared = false
  await page.route(`**/api/v1/tasks/${PREPARE_TASK_ID}`, (route) =>
    editorJson(route, {
      id: PREPARE_TASK_ID,
      kind: 'calendar.restore.prepare',
      status: prepared ? 'succeeded' : 'queued',
      retry_of_task_id: null,
      error_code: null,
      event_cursor: prepared ? '12' : '0',
      steps: [],
      calendar_restore_proposal_id: prepared ? RESTORE_ID : null,
    }),
  )
  let publishTerminal: () => void = () => undefined
  const terminal = new Promise<void>((resolve) => {
    publishTerminal = resolve
  })
  await page.route(
    `**/api/v1/tasks/${PREPARE_TASK_ID}/events*`,
    async (route) => {
      await terminal
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: `id: 12\nevent: task.status_changed\ndata: ${JSON.stringify({
          id: '12',
          task_id: PREPARE_TASK_ID,
          sequence: '12',
          event: 'task.status_changed',
          occurred_at: '2030-01-01T00:00:00Z',
          step_id: null,
          payload: { status: 'succeeded' },
        })}\n\n`,
      })
    },
  )
  await page.route(
    `**/api/v1/calendar/proposals/${RESTORE_ID}`,
    async (route) => {
      if (route.request().method() === 'PATCH') {
        expect(route.request().postDataJSON()).toEqual({
          version: 1,
          confirmation: { kind: 'notification_policy' },
        })
        restore = { ...restore, version: 2, required_confirmations: [] }
        await editorJson(route, { ...restore, editor_facts: null })
      } else await editorJson(route, restore)
    },
  )
  await page.route(
    `**/api/v1/calendar/proposals/${RESTORE_ID}/submit`,
    async (route) => {
      expect(route.request().postDataJSON()).toEqual({ version: 2 })
      expect(restore.required_confirmations).toEqual([])
      restore = { ...restore, status: 'awaiting_approval' }
      snapshot = actionSnapshot({
        action: 'calendar.restore',
        status: 'waiting_approval',
        error_code: null,
        event_cursor: '20',
        task_version: '20',
        execution: null,
        timeline: [],
        provider_url: null,
        local_action: {
          id: RESTORE_ID,
          item_kind: 'calendar_proposal',
          version: 2,
          status: 'awaiting_approval',
          editor_url: `/calendar/proposals/${RESTORE_ID}`,
        },
        approval: {
          ...calendarApproval(),
          proposal_version: 2,
          content_status: 'available',
          preview: {
            kind: 'calendar',
            provider: 'google',
            account_email: connection().account_email,
            operation: 'restore',
            calendar_name: 'Synthetic calendar',
            before: current,
            after: historical,
            conflicts: [],
            notification_policy: 'all',
            base_etag: 'synthetic-current-etag',
            compensation_available: false,
            provider_warnings: [],
          },
        },
      })
      await editorJson(route, { task_id: TASK_ID, status: 'queued' }, 202)
    },
  )

  await page.goto(`/calendar/proposals/${PROPOSAL_ID}`)
  const prepare = page.getByRole('button', {
    name: '准备恢复提案',
    exact: true,
  })
  await expect(prepare).toBeVisible()
  expect(capture.mutations).toHaveLength(0)
  await prepare.click()
  await expect(page).toHaveURL(
    new RegExp(`/tasks\\?task_id=${PREPARE_TASK_ID}$`),
  )
  await expect(
    page.getByText('当前状态：queued', { exact: true }),
  ).toBeVisible()
  await expect(page.getByTestId('restore-result')).toHaveCount(0)
  await expect(page.getByRole('region', { name: '人工审批' })).toHaveCount(0)
  expect(capture.mutations).toHaveLength(1)

  // 仅持久终态触发完整GET后才出现结果；随后刷新仍从同一个准备任务恢复。
  prepared = true
  publishTerminal()
  await expect(page.getByTestId('restore-result')).toHaveAttribute(
    'href',
    `/calendar/proposals/${RESTORE_ID}`,
  )
  await page.reload()
  await expect(page.getByTestId('restore-result')).toHaveAttribute(
    'href',
    `/calendar/proposals/${RESTORE_ID}`,
  )
  await page.getByTestId('restore-result').click()
  await expect(page).toHaveURL(new RegExp(`/calendar/proposals/${RESTORE_ID}$`))
  await expect(
    page.getByText('synthetic-current-etag', { exact: false }),
  ).toBeVisible()
  await expect(
    page.getByRole('button', { name: '提交审批', exact: true }),
  ).toBeDisabled()
  await page.getByRole('button', { name: '确认通知策略', exact: true }).click()
  await expect(
    page.getByRole('button', { name: '提交审批', exact: true }),
  ).toBeEnabled()
  await page.getByRole('button', { name: '提交审批', exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`/actions\\?task=${TASK_ID}$`))
  await expect(
    page.getByRole('button', { name: '批准', exact: true }),
  ).toBeVisible()
  expect(capture.mutations.map((item) => item.path)).toEqual([
    `/calendar/events/${EVENT_ID}/restore-proposal`,
    `/calendar/proposals/${RESTORE_ID}`,
    `/calendar/proposals/${RESTORE_ID}/submit`,
  ])
  expect(capture.mutations[0]?.intent).toMatch(/^[0-9a-f-]{36}$/)
  expect(capture.unexpected).toEqual([])
  expect(capture.pageErrors).toEqual([])
})
