import { expect, test } from '@playwright/test'
import type {
  ActionSnapshot,
  CalendarPreviewFields,
  CalendarProposal,
} from '../src/api/types'
import type { UpdateCalendarProposalInput } from '../src/api/calendar'
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

/** 从实际编辑响应读取冻结前字段；fixture 不允许用默认字段掩盖遗漏输入。 */
function completeFields(proposal: CalendarProposal): CalendarPreviewFields {
  if (
    !proposal.title ||
    !proposal.starts_at ||
    !proposal.ends_at ||
    !proposal.timezone ||
    proposal.all_day === null
  )
    throw new Error('Synthetic calendar is incomplete')
  return {
    title: proposal.title,
    description: proposal.description,
    location: proposal.location,
    starts_at: proposal.starts_at,
    ends_at: proposal.ends_at,
    timezone: proposal.timezone,
    all_day: proposal.all_day,
    attendees: proposal.attendees,
  }
}

test('calendar candidates advance the saved version before four explicit confirmations and approval', async ({
  page,
}) => {
  const capture = await editorWorkspace(page)
  let proposal: CalendarProposal = {
    ...calendarProposal(),
    calendar_id: 'synthetic-google-calendar',
    editor_facts: {
      restore_source: null,
      before_status: 'not_applicable',
      before: null,
      conflict_status: 'incomplete',
      conflicts: null,
    },
  }
  let snapshot: ActionSnapshot | null = null
  await editorAction(page, () => snapshot)
  await page.route('**/api/v1/calendar/proposals', async (route) => {
    expect(route.request().postDataJSON()).toEqual({
      operation_kind: 'create',
      initialization: 'shell',
    })
    await editorJson(route, { ...proposal, editor_facts: null }, 201)
  })
  await page.route(
    `**/api/v1/calendar/proposals/${PROPOSAL_ID}`,
    async (route) => {
      if (route.request().method() === 'PATCH') {
        const input = route
          .request()
          .postDataJSON() as UpdateCalendarProposalInput
        expect(input.version).toBe(proposal.version)
        if ('confirmation' in input && input.confirmation) {
          const confirmation = input.confirmation
          expect(Object.keys(input).sort()).toEqual(['confirmation', 'version'])
          proposal = {
            ...proposal,
            version: proposal.version + 1,
            availability: null,
            required_confirmations: proposal.required_confirmations.filter(
              (kind) => kind !== confirmation.kind,
            ),
            ...(confirmation.kind === 'calendar'
              ? {
                  connection_id: confirmation.connection_id,
                  calendar_id: confirmation.calendar_id,
                }
              : {}),
          }
        } else {
          proposal = {
            ...proposal,
            ...input,
            attendees: input.attendees ?? [],
            version: proposal.version + 1,
            availability: null,
            editor_facts: {
              restore_source: null,
              before_status: 'not_applicable',
              before: null,
              conflict_status: 'checked',
              conflicts: [],
            },
          }
        }
        await editorJson(route, { ...proposal, editor_facts: null })
      } else await editorJson(route, proposal)
    },
  )
  await page.route(
    `**/api/v1/calendar/proposals/${PROPOSAL_ID}/suggest-times`,
    async (route) => {
      expect(route.request().postDataJSON()).toEqual({ version: 2 })
      const availability = {
        proposal_id: PROPOSAL_ID,
        version: 3,
        completeness: 'partial' as const,
        missing_connections: [connection('microsoft').id],
        attendee_availability_checked: false as const,
        candidates: [2, 3, 4].map((day) => ({
          starts_at: `2030-01-0${day}T01:00:00Z`,
          ends_at: `2030-01-0${day}T02:00:00Z`,
        })),
      }
      proposal = { ...proposal, version: 3, availability }
      await editorJson(route, availability)
    },
  )
  await page.route(
    `**/api/v1/calendar/proposals/${PROPOSAL_ID}/submit`,
    async (route) => {
      expect(route.request().postDataJSON()).toEqual({ version: 8 })
      expect(proposal.required_confirmations).toEqual([])
      proposal = { ...proposal, status: 'awaiting_approval' }
      snapshot = actionSnapshot({
        action: 'calendar.create',
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
          version: 8,
          status: 'awaiting_approval',
          editor_url: `/calendar/proposals/${PROPOSAL_ID}`,
        },
        approval: {
          ...calendarApproval(),
          proposal_version: 8,
          content_status: 'available',
          preview: {
            kind: 'calendar',
            provider: 'google',
            account_email: connection().account_email,
            operation: 'create',
            calendar_name: 'Synthetic calendar',
            before: null,
            after: completeFields(proposal),
            conflicts: [
              {
                kind: 'partial_sources',
                starts_at: null,
                ends_at: null,
                missing_connection_ids: [connection('microsoft').id],
              },
            ],
            notification_policy: 'none',
            base_etag: null,
            compensation_available: false,
            provider_warnings: ['google_send_updates_none_external_sync'],
          },
        },
      })
      await editorJson(route, { task_id: TASK_ID, status: 'queued' }, 202)
    },
  )
  await page.route(
    `**/api/v1/approvals/${calendarApproval().id}/decision`,
    async (route) => {
      expect(route.request().postDataJSON()).toEqual({
        decision: 'approved',
        version: 2,
        payload_hash: 'a'.repeat(64),
      })
      if (!snapshot?.approval)
        throw new Error('Synthetic calendar approval missing')
      snapshot = {
        ...snapshot,
        status: 'queued',
        event_cursor: '11',
        task_version: '11',
        approval: { ...snapshot.approval, status: 'approved' },
      }
      await editorJson(route, {}, 200)
    },
  )

  await page.goto('/actions')
  await page.getByRole('button', { name: '新日程', exact: true }).click()
  await expect(page).toHaveURL(
    new RegExp(`/calendar/proposals/${PROPOSAL_ID}$`),
  )
  await expect(
    page.getByRole('button', { name: '提交审批', exact: true }),
  ).toBeDisabled()
  await page
    .getByLabel('日程标题', { exact: true })
    .fill('Synthetic title browser calendar')
  await page.getByLabel('IANA 时区', { exact: true }).fill('Asia/Shanghai')
  await page.getByLabel('开始时间', { exact: true }).fill('2030-01-01T09:00')
  await page.getByLabel('结束时间', { exact: true }).fill('2030-01-01T10:00')
  await page.getByLabel('参会人', { exact: true }).fill('attendee@example.test')
  await page.getByLabel('通知策略', { exact: true }).selectOption('none')
  await page.getByRole('button', { name: '保存提案', exact: true }).click()
  await expect(page.getByText('待确认：4 项', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '查询三个候选时间' }).click()
  await expect(page.locator('button[name="choose-candidate"]')).toHaveCount(3)
  await expect(page.getByText('版本 3', { exact: false })).toBeVisible()
  await expect(
    page.getByRole('region', { name: '服务端候选时间' }),
  ).toContainText('部分日历来源')
  await expect(
    page.getByRole('region', { name: '服务端候选时间' }),
  ).toContainText('未检查参会人可用性')
  await page.locator('button[name="choose-candidate"]').first().click()
  await expect(page.locator('button[name="choose-candidate"]')).toHaveCount(0)
  await expect(page.getByTestId('calendar-conflicts')).toHaveCount(0)
  await page.getByRole('button', { name: '保存提案', exact: true }).click()
  await expect(page.getByText('版本 4', { exact: false })).toBeVisible()
  for (const [index, kind] of [
    'calendar',
    'time',
    'attendees',
    'notification_policy',
  ].entries()) {
    await page.locator(`button[name="confirm-${kind}"]`).click()
    // 每次完整 GET 后再进入下一个确认，不能并发发送旧版本。
    await expect(
      page.getByText(`版本 ${index + 5}`, { exact: false }),
    ).toBeVisible()
    await expect(
      page.getByRole('button', { name: '重新加载提案', exact: true }),
    ).toBeEnabled()
  }
  await expect(page.getByText('待确认：0 项', { exact: true })).toBeVisible()
  expect(
    capture.mutations.filter(
      (item) =>
        item.path.endsWith('/submit') || item.path.endsWith('/decision'),
    ),
  ).toHaveLength(0)
  const confirmations = capture.mutations
    .filter((item) => item.method === 'PATCH')
    .slice(2)
  expect(confirmations.map((item) => item.payload)).toEqual([
    {
      version: 4,
      confirmation: {
        kind: 'calendar',
        connection_id: connection().id,
        calendar_id: 'synthetic-google-calendar',
      },
    },
    { version: 5, confirmation: { kind: 'time' } },
    { version: 6, confirmation: { kind: 'attendees' } },
    { version: 7, confirmation: { kind: 'notification_policy' } },
  ])
  await page.getByRole('button', { name: '提交审批', exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`/actions\\?task=${TASK_ID}$`))
  const preview = page.getByRole('region', { name: '日程审批预览' })
  await expect(preview).toContainText('Asia/Shanghai')
  await expect(preview).toContainText('不发送通知')
  await expect(preview).toContainText('外部同步')
  await expect(preview.getByRole('table')).toBeVisible()
  await page.getByRole('button', { name: '批准', exact: true }).click()
  await expect(page.locator('.action-detail')).toContainText('已批准')
  expect(
    capture.mutations.filter((item) => item.path.endsWith('/decision')),
  ).toHaveLength(1)
  expect(capture.unexpected).toEqual([])
  expect(capture.pageErrors).toEqual([])
})

test('an update retains its original before value and exposes stale ETag recovery without replay', async ({
  page,
}) => {
  const capture = await editorWorkspace(page)
  const before = calendarFields()
  let proposal: CalendarProposal = {
    ...calendarProposal(),
    ...before,
    location: 'Synthetic location changed',
    calendar_id: 'synthetic-google-calendar',
    operation_kind: 'update',
    target_event_id: 'synthetic-provider-event',
    base_etag: 'synthetic-etag-original',
    before_snapshot_id: '00000000-0000-0000-0000-000000000699',
    notification_policy: 'none',
    required_confirmations: [],
    changed_fields: ['location'],
    field_diffs: [{ field: 'location', changed: true }],
    editor_facts: {
      restore_source: null,
      before_status: 'available',
      before,
      conflict_status: 'checked',
      conflicts: [],
    },
  }
  await page.route(`**/api/v1/calendar/proposals/${PROPOSAL_ID}`, (route) =>
    editorJson(route, proposal),
  )
  await page.route(
    `**/api/v1/calendar/proposals/${PROPOSAL_ID}/submit`,
    async (route) => {
      expect(route.request().postDataJSON()).toEqual({ version: 1 })
      proposal = { ...proposal, status: 'stale' }
      await editorProblem(route, 'calendar_etag_stale')
    },
  )
  await page.goto(`/calendar/proposals/${PROPOSAL_ID}`)
  await expect(page.getByLabel('日历账户', { exact: true })).toBeDisabled()
  await expect(page.getByLabel('目标日历', { exact: true })).toBeDisabled()
  await expect(page.getByRole('table')).toContainText('日程修改前后')
  await expect(page.getByRole('table')).toContainText(before.location ?? '')
  await page.getByRole('button', { name: '提交审批', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('synthetic-editor-trace')
  await expect(page.getByRole('alert')).toContainText('创建新版本')
  await page.getByRole('button', { name: '重新加载提案', exact: true }).click()
  await expect(
    page.getByRole('button', { name: '提交审批', exact: true }),
  ).toBeDisabled()
  expect(capture.mutations.map((item) => item.path)).toEqual([
    `/calendar/proposals/${PROPOSAL_ID}/submit`,
  ])
  expect(capture.unexpected).toEqual([])
  expect(capture.pageErrors).toEqual([])
})
