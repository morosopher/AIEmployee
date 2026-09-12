import { expect, test, type Page } from '@playwright/test'
import type { CalendarProposal } from '../src/api/types'
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
import {
  editorAction,
  editorJson,
  editorProblem,
  editorWorkspace,
} from './fixtures/editorApi'

const EVENT_ID = '00000000-0000-0000-0000-000000000681'
const NEW_ID = '00000000-0000-0000-0000-000000000682'
const SYNC_TASK_ID = '00000000-0000-0000-0000-000000000683'

/** 来源始终是本地 UUID；原 before、旧 ETag 和新版本 fixture 分别保存，禁止共用对象冒充更新。 */
function source(requiresSync = false): CalendarProposal {
  return {
    ...calendarProposal(),
    ...calendarFields(),
    calendar_id: 'synthetic-google-calendar',
    operation_kind: 'update',
    status: 'stale',
    target_event_id: 'synthetic-provider-event',
    base_etag: 'synthetic-old-etag',
    before_snapshot_id: '00000000-0000-0000-0000-000000000684',
    location: 'Synthetic desired location',
    notification_policy: 'none',
    changed_fields: ['location'],
    field_diffs: [{ field: 'location', changed: true }],
    required_confirmations: [],
    editor_facts: {
      before_status: 'available',
      before: calendarFields(),
      conflict_status: 'checked',
      conflicts: [],
      restore_source: null,
      reprepare_source: { event_id: EVENT_ID, requires_sync: requiresSync },
    },
  }
}

/** 新 update shell 与真实 HTTP 回归保持一致：无 diff、四项待确认、独立 before 与新 ETag。 */
async function workspace(page: Page, requiresSync = false) {
  const capture = await editorWorkspace(page)
  const fresh = {
    ...calendarFields(),
    location: 'Synthetic synchronized location',
  }
  const state = {
    current: source(requiresSync),
    prepared: {
      ...source(),
      ...fresh,
      id: NEW_ID,
      version: 1,
      status: 'editing',
      base_etag: 'synthetic-new-etag',
      before_snapshot_id: '00000000-0000-0000-0000-000000000685',
      changed_fields: [],
      field_diffs: [],
      required_confirmations: [
        'calendar',
        'time',
        'attendees',
        'notification_policy',
      ],
      editor_facts: {
        before_status: 'available',
        before: fresh,
        conflict_status: 'checked',
        conflicts: [],
        restore_source: null,
        reprepare_source: { event_id: EVENT_ID, requires_sync: false },
      },
    } satisfies CalendarProposal,
  }
  await page.route(`**/api/v1/calendar/proposals/${PROPOSAL_ID}`, (route) =>
    editorJson(route, state.current),
  )
  await page.route(`**/api/v1/calendar/proposals/${NEW_ID}`, (route) =>
    editorJson(route, state.prepared),
  )
  return { capture, state }
}

test('sync completion requires an explicit reread and an unknown creation response retries the same intent', async ({
  page,
}) => {
  const { capture, state } = await workspace(page, true)
  let completed = false
  await page.route(`**/api/v1/connections/${connection().id}/sync`, (route) =>
    editorJson(
      route,
      { gmail_task_id: TASK_ID, calendar_task_id: SYNC_TASK_ID },
      202,
    ),
  )
  await page.route(`**/api/v1/tasks/${SYNC_TASK_ID}`, (route) =>
    editorJson(route, {
      id: SYNC_TASK_ID,
      kind: 'sync_calendar',
      status: completed ? 'succeeded' : 'queued',
      retry_of_task_id: null,
      error_code: null,
      event_cursor: completed ? '12' : '0',
      steps: [],
    }),
  )
  let publishTerminal: () => void = () => undefined
  const terminal = new Promise<void>((resolve) => {
    publishTerminal = resolve
  })
  await page.route(`**/api/v1/tasks/${SYNC_TASK_ID}/events*`, async (route) => {
    await terminal
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: `id: 12\nevent: task.status_changed\ndata: ${JSON.stringify({
        id: '12',
        task_id: SYNC_TASK_ID,
        sequence: '12',
        event: 'task.status_changed',
        occurred_at: '2030-01-01T00:00:00Z',
        step_id: null,
        payload: { status: 'succeeded' },
      })}\n\n`,
    })
  })
  let attempts = 0,
    rejectFirst: () => void = () => undefined
  const firstResponse = new Promise<void>((resolve) => {
    rejectFirst = resolve
  })
  await page.route('**/api/v1/calendar/proposals', async (route) => {
    attempts += 1
    expect(route.request().postDataJSON()).toEqual({
      operation_kind: 'update',
      initialization: 'shell',
      event_id: EVENT_ID,
    })
    if (attempts === 1) {
      await firstResponse
      await route.abort('failed')
    } else
      await editorJson(route, { ...state.prepared, editor_facts: null }, 201)
  })

  await page.goto(`/calendar/proposals/${PROPOSAL_ID}?recovery=new_version`)
  const panel = page.getByRole('region', { name: '重新准备修改提案' })
  const prepare = panel.getByRole('button', { name: '准备新版本', exact: true })
  await expect(panel).toContainText('Google')
  await expect(panel).toContainText(connection().account_email)
  await expect(prepare).toBeDisabled()
  await panel.getByRole('button', { name: '同步来源账户', exact: true }).click()
  await expect(panel).toContainText('同步进行中')
  await expect(
    panel.getByRole('link', { name: '查看同步任务' }),
  ).toHaveAttribute('href', `/tasks?task_id=${SYNC_TASK_ID}`)
  expect(attempts).toBe(0)
  state.current = source(false)
  completed = true
  publishTerminal()
  await expect(panel).toContainText('同步已完成')
  await expect(prepare).toBeDisabled()
  await panel.getByRole('button', { name: '重新读取提案', exact: true }).click()
  await expect(prepare).toBeEnabled()
  expect(attempts).toBe(0)
  await prepare.click()
  await expect(prepare).toBeDisabled()
  // 原生 disabled 点击不能发出第二个请求；第一回执仍未到达。
  await prepare.evaluate((button) => (button as HTMLButtonElement).click())
  expect(attempts).toBe(1)
  rejectFirst()
  await expect(prepare).toBeEnabled()
  await expect(page.getByRole('alert')).toContainText('请求失败')
  expect(attempts).toBe(1)
  await prepare.click()
  await expect(page).toHaveURL(new RegExp(`/calendar/proposals/${NEW_ID}$`))
  await expect(page.getByLabel('日程标题', { exact: true })).toBeEnabled()
  await expect(page.getByText('待确认：4 项', { exact: true })).toBeVisible()
  await expect(
    page.getByRole('button', { name: '提交审批', exact: true }),
  ).toBeDisabled()
  const creates = capture.mutations.filter(
    (item) => item.path === '/calendar/proposals',
  )
  expect(creates).toHaveLength(2)
  expect(creates[0]?.intent).toMatch(/^[0-9a-f-]{36}$/)
  expect(creates[1]?.intent).toBe(creates[0]?.intent)
  expect(
    capture.mutations.filter(
      (item) =>
        item.path.endsWith('/submit') ||
        item.path.endsWith('/decision') ||
        item.method === 'PATCH',
    ),
  ).toEqual([])
  expect(state.current.editor_facts?.before).toEqual(calendarFields())
  expect(capture.unexpected).toEqual([])
  expect(capture.pageErrors).toEqual([])
})

test('an approval conflict links to explicit preparation without repeating the frozen decision', async ({
  page,
}) => {
  const { capture, state } = await workspace(page)
  const snapshot = actionSnapshot({
    action: 'calendar.update',
    provider: 'google',
    status: 'waiting_approval',
    error_code: null,
    event_cursor: '10',
    task_version: '10',
    execution: null,
    timeline: [],
    provider_url: null,
    local_action: {
      id: PROPOSAL_ID,
      item_kind: 'calendar_proposal',
      version: 1,
      status: 'awaiting_approval',
      editor_url: `/calendar/proposals/${PROPOSAL_ID}`,
    },
    approval: { ...calendarApproval(), proposal_version: 1 },
  })
  await editorAction(page, () => snapshot)
  await page.route(
    `**/api/v1/approvals/${calendarApproval().id}/decision`,
    (route) => editorProblem(route, 'calendar_event_version_conflict'),
  )
  await page.route('**/api/v1/calendar/proposals', async (route) => {
    expect(route.request().postDataJSON()).toEqual({
      operation_kind: 'update',
      initialization: 'shell',
      event_id: EVENT_ID,
    })
    await editorJson(route, { ...state.prepared, editor_facts: null }, 201)
  })
  await page.goto(`/actions?task=${TASK_ID}`)
  await page.getByRole('button', { name: '批准', exact: true }).click()
  const recovery = page.getByRole('link', { name: '创建新版本', exact: true })
  await expect(recovery).toHaveAttribute(
    'href',
    new RegExp(`/calendar/proposals/${PROPOSAL_ID}\\?.*recovery=new_version`),
  )
  await recovery.click()
  await expect(
    page.getByRole('region', { name: '重新准备修改提案' }),
  ).toBeVisible()
  expect(
    capture.mutations.filter((item) => item.path === '/calendar/proposals'),
  ).toEqual([])
  await page.getByRole('button', { name: '准备新版本', exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`/calendar/proposals/${NEW_ID}$`))
  await expect(page.getByLabel('日程标题', { exact: true })).toBeEnabled()
  await expect(page.getByText('待确认：4 项', { exact: true })).toBeVisible()
  expect(
    capture.mutations.filter((item) => item.path.endsWith('/decision')),
  ).toHaveLength(1)
  expect(
    capture.mutations.filter((item) => item.path.endsWith('/submit')),
  ).toEqual([])
  expect(snapshot.approval?.status).toBe('pending')
  expect(state.current.status).toBe('stale')
  expect(capture.unexpected).toEqual([])
  expect(capture.pageErrors).toEqual([])
})

test('leaving the editor isolates a late preparation response', async ({
  page,
}) => {
  const { capture, state } = await workspace(page)
  let release: () => void = () => undefined
  const response = new Promise<void>((resolve) => {
    release = resolve
  })
  await page.route('**/api/v1/calendar/proposals', async (route) => {
    await response
    await editorJson(route, { ...state.prepared, editor_facts: null }, 201)
  })
  await page.goto(`/calendar/proposals/${PROPOSAL_ID}`)
  const prepare = page.getByRole('button', { name: '准备新版本', exact: true })
  await prepare.click()
  await expect(prepare).toBeDisabled()
  await page.getByRole('link', { name: '返回操作中心', exact: true }).click()
  await expect(page).toHaveURL(/\/actions$/)
  release()
  await page.waitForLoadState('networkidle')
  await expect(page).toHaveURL(/\/actions$/)
  expect(
    capture.mutations.filter((item) => item.path === '/calendar/proposals'),
  ).toHaveLength(1)
  expect(
    capture.mutations.filter((item) => item.path.endsWith('/submit')),
  ).toEqual([])
  expect(capture.unexpected).toEqual([])
  expect(capture.pageErrors).toEqual([])
})
