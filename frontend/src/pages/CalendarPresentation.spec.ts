/** 用真实页面、Form 和 DatePicker 验证展示边界；仅替换合成 API，不替换 hook 或时间转换。 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, waitFor, within } from '@testing-library/vue'
import { defineComponent, h, ref } from 'vue'
import { RouterView } from 'vue-router'
import ConfirmDialog from 'primevue/confirmdialog'
import { renderWithPlugins } from '@/test-support/renderWithPlugins'
import CalendarProposalPage from './CalendarProposalPage.vue'
import CalendarConflictNotice from '@/components/CalendarConflictNotice.vue'
import CalendarDateTimeInput from '@/components/CalendarDateTimeInput.vue'
import CalendarApprovalPreview from '@/components/CalendarApprovalPreview.vue'
import * as calendar from '@/api/calendar'
import * as connections from '@/api/connections'
import type { CalendarProposal } from '@/api/types'
import { ProblemError } from '@/api/client'
import {
  calendarProposal,
  PROPOSAL_ID,
  CONNECTION_ID,
} from '@/test-support/actionFixtures'
import {
  calendarFields,
  connection,
  connectionCapabilities,
} from '@/test-support/editorFixtures'

vi.mock('@/api/calendar', async (original) => ({
  ...(await original<typeof import('@/api/calendar')>()),
  getCalendarProposal: vi.fn(),
  updateCalendarProposal: vi.fn(),
  submitCalendarProposal: vi.fn(),
  suggestCalendarTimes: vi.fn(),
}))
vi.mock('@/api/connections', () => ({
  listConnections: vi.fn(),
  getConnectionCapabilities: vi.fn(),
}))
vi.mock('@/composables/useTaskEvents', () => ({
  useTaskEvents: () => ref('connected'),
}))
let current: CalendarProposal
beforeEach(() => {
  vi.clearAllMocks()
  current = {
    ...calendarProposal(),
    ...calendarFields(),
    calendar_id: 'synthetic-google-calendar',
    notification_policy: 'none',
    editor_facts: {
      reprepare_source: null,
      restore_source: null,
      before_status: 'not_applicable',
      before: null,
      conflict_status: 'checked',
      conflicts: [],
    },
  }
  vi.mocked(calendar.getCalendarProposal).mockImplementation(
    async () => current,
  )
  vi.mocked(calendar.updateCalendarProposal).mockImplementation(
    async (_id, input) => {
      current = {
        ...current,
        ...input,
        attendees:
          ('attendees' in input ? input.attendees : undefined) ??
          current.attendees,
        version: input.version + 1,
      }
      return { ...current, editor_facts: null }
    },
  )
  vi.mocked(connections.listConnections).mockResolvedValue([connection()])
  vi.mocked(connections.getConnectionCapabilities).mockResolvedValue(
    connectionCapabilities(),
  )
})
/** 异步表单到达后再操作字段，加载期间仍可见真实 status。 */
async function page() {
  const view = await renderWithPlugins(
    defineComponent({
      render: () => h('div', [h(RouterView), h(ConfirmDialog)]),
    }),
  )
  view.router.addRoute({
    path: '/calendar/proposals/:proposalId',
    component: CalendarProposalPage,
  })
  view.router.addRoute({
    path: '/actions',
    component: { render: () => h('p', '操作中心') },
  })
  await view.router.push(`/calendar/proposals/${PROPOSAL_ID}`)
  // 父快照可见后异步Form已开始加载；等待真实模块，避免把加载耗时塞进字段的默认查询窗口。
  await view.findByText(/^版本 \d+ ·/)
  await vi.dynamicImportSettled()
  await view.findByLabelText('日程标题')
  return view
}
describe('calendar presentation migration', () => {
  it('uses a real validated form and blocks malformed input before PATCH', async () => {
    const view = await page()
    expect(view.getByRole('form', { name: '日程提案表单' })).toBeVisible()
    await fireEvent.update(view.getByLabelText('日程标题'), ' ')
    await fireEvent.click(view.getByRole('button', { name: '保存提案' }))
    expect(await view.findByText('请填写日程标题。')).toBeVisible()
    expect(calendar.updateCalendarProposal).not.toHaveBeenCalled()
  })
  it('exposes DatePicker dialogs, explicit timezone Select and attendee chips without new actions', async () => {
    const view = await page()
    expect(view.getByRole('combobox', { name: 'IANA 时区' })).toHaveValue(
      'Asia/Shanghai',
    )
    await fireEvent.click(view.getByRole('button', { name: '选择开始时间' }))
    expect(await view.findByRole('dialog')).toBeVisible()
    await fireEvent.keyDown(view.getByLabelText('开始时间'), {
      key: 'Escape',
      code: 'Escape',
    })
    await fireEvent.update(view.getByLabelText('参会人'), 'person@example.test')
    expect(
      within(view.getByRole('list', { name: '参会人列表' })).getByText(
        'person@example.test',
      ),
    ).toBeVisible()
    expect(
      view.queryByRole('button', {
        name: /删除日程|取消日程|重复日程|会议链接/,
      }),
    ).toBeNull()
  })
  it('keeps invalid manual time visible through blur and never sends the old valid instant', async () => {
    const view = await page()
    const input = view.getByLabelText('开始时间')
    await fireEvent.update(input, '2030-02-30T09:00')
    await fireEvent.blur(input)
    expect(input).toHaveValue('2030-02-30T09:00')
    await fireEvent.click(view.getByRole('button', { name: '保存提案' }))
    await waitFor(() =>
      expect(view.getAllByRole('alert').length).toBeGreaterThan(0),
    )
    expect(calendar.updateCalendarProposal).not.toHaveBeenCalled()
  })
  it.each(['2030-03-10T02:30', '2030-11-03T01:30'])(
    'rejects ambiguous or missing target DST wall time %s',
    async (wall) => {
      const view = await page()
      await fireEvent.update(
        view.getByLabelText('IANA 时区'),
        'America/New_York',
      )
      await fireEvent.update(view.getByLabelText('开始时间'), wall)
      await fireEvent.update(
        view.getByLabelText('结束时间'),
        `${wall.slice(0, 10)}T04:00`,
      )
      await fireEvent.click(view.getByRole('button', { name: '保存提案' }))
      await view.findByText(/夏令时跳跃或重复/)
      expect(calendar.updateCalendarProposal).not.toHaveBeenCalled()
    },
  )
  it('sends an explicit UTC time that can be missing in the host DST zone', async () => {
    const view = await page()
    await fireEvent.update(view.getByLabelText('IANA 时区'), 'UTC')
    await fireEvent.update(view.getByLabelText('开始时间'), '2030-03-10T02:30')
    await fireEvent.blur(view.getByLabelText('开始时间'))
    expect(view.getByLabelText('开始时间')).toHaveValue('2030-03-10T02:30')
    await fireEvent.update(view.getByLabelText('结束时间'), '2030-03-10T03:30')
    await fireEvent.click(view.getByRole('button', { name: '保存提案' }))
    await waitFor(() =>
      expect(calendar.updateCalendarProposal).toHaveBeenCalledWith(
        PROPOSAL_ID,
        expect.objectContaining({
          starts_at: '2030-03-10T02:30:00.000Z',
          ends_at: '2030-03-10T03:30:00.000Z',
          timezone: 'UTC',
        }),
      ),
    )
  })
  it('keeps source confirmation independent from an incomplete shell form', async () => {
    current = { ...current, title: null, starts_at: null, ends_at: null }
    const view = await page()
    await fireEvent.click(view.getByRole('button', { name: '确认此日历' }))
    await waitFor(() =>
      expect(calendar.updateCalendarProposal).toHaveBeenCalledWith(
        PROPOSAL_ID,
        {
          version: 1,
          confirmation: {
            kind: 'calendar',
            connection_id: CONNECTION_ID,
            calendar_id: 'synthetic-google-calendar',
          },
        },
      ),
    )
    expect(view.queryByText('请填写日程标题。')).toBeNull()
    expect(calendar.submitCalendarProposal).not.toHaveBeenCalled()
  })
  it('converts to exclusive all-day dates and requires an explicit time when switching back', async () => {
    const view = await page()
    await fireEvent.click(view.getByRole('checkbox', { name: '全天日程' }))
    expect(view.getByLabelText('开始日期')).toHaveValue('2030-01-01')
    await fireEvent.update(
      view.getByLabelText('结束日期（不含）'),
      '2030-01-02',
    )
    await fireEvent.click(view.getByRole('button', { name: '保存提案' }))
    await waitFor(() =>
      expect(calendar.updateCalendarProposal).toHaveBeenCalledWith(
        PROPOSAL_ID,
        expect.objectContaining({
          all_day: true,
          starts_at: '2030-01-01',
          ends_at: '2030-01-02',
          timezone: 'Asia/Shanghai',
        }),
      ),
    )
    await waitFor(() =>
      expect(view.getByRole('checkbox', { name: '全天日程' })).toBeEnabled(),
    )
    await fireEvent.click(view.getByRole('checkbox', { name: '全天日程' }))
    expect(view.getByLabelText('开始时间')).toHaveValue('2030-01-01')
    await fireEvent.click(view.getByRole('button', { name: '保存提案' }))
    await waitFor(() =>
      expect(view.getAllByText('请填写完整的日期和时间。')).toHaveLength(2),
    )
    expect(calendar.updateCalendarProposal).toHaveBeenCalledTimes(1)
  })
  it('keeps conflict warnings polite and missing account details static beside each result', async () => {
    current = {
      ...current,
      editor_facts: {
        reprepare_source: null,
        restore_source: null,
        before_status: 'not_applicable',
        before: null,
        conflict_status: 'checked',
        conflicts: [
          {
            kind: 'outside_working_hours',
            starts_at: null,
            ends_at: null,
            missing_connection_ids: [],
          },
          {
            kind: 'partial_sources',
            starts_at: null,
            ends_at: null,
            missing_connection_ids: ['synthetic-missing-id'],
          },
        ],
      },
    }
    const view = await page()
    const region = view.getByRole('region', { name: '日程冲突检查' })
    expect(region).toHaveAttribute('aria-live', 'polite')
    expect(region).toHaveTextContent('当前安排位于工作时间外。')
    expect(
      within(region).getByRole('list', { name: '缺失日历账户' }),
    ).toHaveTextContent('synthetic-missing-id')
    expect(within(region).queryByRole('alert')).toBeNull()
  })
  it('confirms discarding edits without sending requests and returns focus on Escape', async () => {
    const view = await page()
    await fireEvent.update(view.getByLabelText('日程标题'), 'Synthetic unsaved')
    const back = view.getByRole('link', { name: '返回操作中心' })
    back.focus()
    await fireEvent.click(back)
    const dialog = await view.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('提案尚未保存或确认，确定离开？')
    await fireEvent.keyDown(dialog, { key: 'Escape', code: 'Escape' })
    await waitFor(() => expect(view.queryByRole('alertdialog')).toBeNull())
    expect(back).toHaveFocus()
    expect(view.router.currentRoute.value.path).toBe(
      `/calendar/proposals/${PROPOSAL_ID}`,
    )
    expect(calendar.updateCalendarProposal).not.toHaveBeenCalled()
  })
  it.each(['2030-01-01T01:00:00.123456Z', '2030-01-01T01:00:42.123456Z'])(
    'preserves original instant precision when reselecting the same displayed time %s',
    async (instant) => {
      current = { ...current, starts_at: instant }
      const view = await page()
      await fireEvent.click(view.getByRole('button', { name: '选择开始时间' }))
      const dialog = await view.findByRole('dialog')
      await fireEvent.click(
        within(dialog).getByRole('button', { name: '2030-01-01' }),
      )
      await fireEvent.keyDown(view.getByLabelText('开始时间'), {
        key: 'Escape',
        code: 'Escape',
      })
      expect(
        view.queryByText('有未保存或未确认的修改，请保存后重新检查。'),
      ).toBeNull()
      await fireEvent.update(
        view.getByLabelText('日程标题'),
        'Synthetic precision retained',
      )
      await fireEvent.click(view.getByRole('button', { name: '保存提案' }))
      await waitFor(() =>
        expect(calendar.updateCalendarProposal).toHaveBeenCalledWith(
          PROPOSAL_ID,
          expect.objectContaining({
            starts_at: instant,
            ends_at: current.ends_at,
          }),
        ),
      )
    },
  )
  it('shows explicit approval submission as busy and blocks duplicate clicks', async () => {
    current = { ...current, required_confirmations: [] }
    vi.mocked(calendar.submitCalendarProposal).mockReturnValue(
      new Promise(() => undefined),
    )
    const view = await page()
    const submit = view.getByRole('button', { name: '提交审批' })
    await fireEvent.click(submit)
    await waitFor(() => expect(submit).toHaveAttribute('aria-busy', 'true'))
    expect(submit).toBeDisabled()
    await fireEvent.click(submit)
    expect(calendar.submitCalendarProposal).toHaveBeenCalledTimes(1)
    expect(calendar.updateCalendarProposal).not.toHaveBeenCalled()
  })
  it.each(['Asia/Shanghai', 'Not/AZone'])(
    'preserves exact conflict timestamps beside the explicit %s display',
    async (timezone) => {
      const starts = '2030-01-01T01:00:42.123456Z'
      const ends = '2030-01-01T02:00:42.654321Z'
      const view = await renderWithPlugins(CalendarConflictNotice, {
        props: {
          timezone,
          conflicts: [
            {
              kind: 'overlap',
              starts_at: starts,
              ends_at: ends,
              missing_connection_ids: [],
            },
          ],
        },
      })
      const region = view.getByRole('region', { name: '日程冲突检查' })
      expect(region).toHaveTextContent(starts)
      expect(region).toHaveTextContent(ends)
      expect(region).toHaveTextContent(timezone)
      expect(region).toHaveTextContent(
        timezone === 'Asia/Shanghai' ? '09:00' : '时间不可用',
      )
    },
  )
  it('uses the frozen approval IANA zone for readable conflicts without changing its exact snapshot', async () => {
    const preview = {
      kind: 'calendar' as const,
      provider: 'google' as const,
      account_email: connection().account_email,
      operation: 'create' as const,
      calendar_name: 'Synthetic calendar',
      before: null,
      after: calendarFields(),
      conflicts: [
        {
          kind: 'overlap' as const,
          starts_at: '2030-01-01T01:00:42.123456Z',
          ends_at: '2030-01-01T02:00:42.654321Z',
          missing_connection_ids: [],
        },
      ],
      notification_policy: 'none' as const,
      base_etag: null,
      compensation_available: false,
      provider_warnings: [],
    }
    const original = JSON.stringify(preview)
    const view = await renderWithPlugins(CalendarApprovalPreview, {
      props: { preview },
    })
    const region = view.getByRole('region', { name: '日程冲突检查' })
    expect(region).toHaveTextContent('Asia/Shanghai')
    expect(region).toHaveTextContent('09:00')
    expect(region).toHaveTextContent(preview.conflicts[0]?.starts_at ?? '')
    expect(JSON.stringify(preview)).toBe(original)
  })
  it('keeps the anchored date dialog nonmodal and exposes the selected day as a keyboard entry', async () => {
    const view = await page()
    await fireEvent.click(view.getByRole('button', { name: '选择开始时间' }))
    const dialog = await view.findByRole('dialog')
    expect(dialog).toHaveAttribute('aria-modal', 'false')
    const selected = within(dialog).getByRole('gridcell', { selected: true })
    const day = within(selected).getByRole('button', { name: '2030-01-01' })
    day.focus()
    expect(day).toHaveFocus()
    await fireEvent.keyDown(day, { key: 'ArrowRight', code: 'ArrowRight' })
    const next = within(dialog).getByRole('button', { name: '2030-01-02' })
    expect(next).toHaveFocus()
    await fireEvent.keyDown(next, { key: 'Enter', code: 'Enter' })
    await waitFor(() =>
      expect(view.getByLabelText('开始时间')).toHaveValue('2030-01-02T09:00'),
    )
    await fireEvent.keyDown(next, { key: 'Escape', code: 'Escape' })
    await waitFor(() => expect(view.queryByRole('dialog')).toBeNull())
    expect(view.getByLabelText('开始时间')).toHaveFocus()
    expect(calendar.updateCalendarProposal).not.toHaveBeenCalled()
  })
  it.each([
    {
      name: 'title only',
      start: '2030-11-03T01:30:42.123456-04:00',
      end: '2030-11-03T02:30:21.654321-05:00',
      editedEnd: null,
    },
    {
      name: 'only the other endpoint',
      start: '2030-11-03T01:30:42.123456-04:00',
      end: '2030-11-03T02:30:21.654321-05:00',
      editedEnd: '2030-11-03T03:30',
    },
    {
      name: 'wall end earlier but instant later',
      start: '2030-11-03T01:50:42.123456-04:00',
      end: '2030-11-03T01:10:21.654321-05:00',
      editedEnd: null,
    },
  ])(
    'keeps the saved offset for $name without reinterpreting the unchanged fold time',
    async ({ start, end, editedEnd }) => {
      current = {
        ...current,
        timezone: 'America/New_York',
        starts_at: start,
        ends_at: end,
      }
      const view = await page()
      if (editedEnd)
        await fireEvent.update(view.getByLabelText('结束时间'), editedEnd)
      else
        await fireEvent.update(
          view.getByLabelText('日程标题'),
          'Synthetic saved offset',
        )
      await fireEvent.click(view.getByRole('button', { name: '保存提案' }))
      await waitFor(() =>
        expect(calendar.updateCalendarProposal).toHaveBeenCalledWith(
          PROPOSAL_ID,
          expect.objectContaining({
            starts_at: start,
            ends_at: editedEnd ? '2030-11-03T08:30:00.000Z' : end,
            timezone: 'America/New_York',
          }),
        ),
      )
      expect(view.queryByText(/夏令时跳跃或重复/)).toBeNull()
    },
  )
  it.each(['time', 'timezone'])(
    'does not apply the saved offset exemption to newly edited %s',
    async (field) => {
      current = {
        ...current,
        timezone: 'America/New_York',
        starts_at: '2030-11-03T01:30:00-04:00',
        ends_at: '2030-11-03T02:30:00-05:00',
      }
      const view = await page()
      await fireEvent.update(
        view.getByLabelText(field === 'time' ? '开始时间' : 'IANA 时区'),
        field === 'time' ? '2030-11-03T01:40' : 'US/Eastern',
      )
      await fireEvent.click(view.getByRole('button', { name: '保存提案' }))
      await view.findByText(/夏令时跳跃或重复/)
      expect(calendar.updateCalendarProposal).not.toHaveBeenCalled()
    },
  )
  it.each(['date click', 'date keyboard', 'hour increment', 'hour decrement'])(
    'keeps wall digits across host DST for %s and sends the exact UTC command',
    async (operation) => {
      const dayOperation = operation.startsWith('date')
      current = {
        ...current,
        timezone: 'UTC',
        starts_at: dayOperation
          ? '2030-03-09T02:30:00Z'
          : operation === 'hour increment'
            ? '2030-03-10T01:30:00Z'
            : '2030-03-10T03:30:00Z',
        ends_at: '2030-03-10T04:00:00Z',
      }
      const view = await page()
      await fireEvent.click(view.getByRole('button', { name: '选择开始时间' }))
      const dialog = await view.findByRole('dialog')
      if (operation === 'date click')
        await fireEvent.click(
          within(dialog).getByRole('button', { name: '2030-03-10' }),
        )
      else if (operation === 'date keyboard') {
        const day = within(dialog).getByRole('button', { name: '2030-03-09' })
        day.focus()
        await fireEvent.keyDown(day, { key: 'ArrowRight', code: 'ArrowRight' })
        await fireEvent.keyDown(document.activeElement ?? day, {
          key: 'Enter',
          code: 'Enter',
        })
      } else {
        const button = within(dialog).getByRole('button', {
          name: operation === 'hour increment' ? '下一小时' : '上一小时',
        })
        await fireEvent.mouseDown(button)
        await fireEvent.mouseUp(button)
        await fireEvent.click(button)
      }
      await waitFor(() =>
        expect(view.getByLabelText('开始时间')).toHaveValue('2030-03-10T02:30'),
      )
      await fireEvent.keyDown(view.getByLabelText('开始时间'), {
        key: 'Escape',
        code: 'Escape',
      })
      await fireEvent.click(view.getByRole('button', { name: '保存提案' }))
      await waitFor(() =>
        expect(calendar.updateCalendarProposal).toHaveBeenCalledWith(
          PROPOSAL_ID,
          expect.objectContaining({
            starts_at: '2030-03-10T02:30:00.000Z',
            ends_at: '2030-03-10T04:00:00Z',
            timezone: 'UTC',
          }),
        ),
      )
    },
  )
  it.each([
    {
      start: '2030-03-09T02:30:00-05:00',
      end: '2030-03-10T04:00:00-04:00',
      day: '2030-03-10',
      wall: '2030-03-10T02:30',
    },
    {
      start: '2030-11-02T01:30:00-04:00',
      end: '2030-11-03T04:00:00-05:00',
      day: '2030-11-03',
      wall: '2030-11-03T01:30',
    },
  ])(
    'refuses actual target DST when the new calendar date is $day',
    async ({ start, end, day, wall }) => {
      current = {
        ...current,
        timezone: 'America/New_York',
        starts_at: start,
        ends_at: end,
      }
      const view = await page()
      await fireEvent.click(view.getByRole('button', { name: '选择开始时间' }))
      const dialog = await view.findByRole('dialog')
      await fireEvent.click(within(dialog).getByRole('button', { name: day }))
      await waitFor(() =>
        expect(view.getByLabelText('开始时间')).toHaveValue(wall),
      )
      await fireEvent.keyDown(view.getByLabelText('开始时间'), {
        key: 'Escape',
        code: 'Escape',
      })
      await fireEvent.click(view.getByRole('button', { name: '保存提案' }))
      await view.findByText(/夏令时跳跃或重复/)
      expect(calendar.updateCalendarProposal).not.toHaveBeenCalled()
    },
  )
  it.each(['click', 'Enter'])(
    'does not let an old date node %s overwrite a replacement input with the same public id',
    async (operation) => {
      const first = ref(true)
      const oldValue = ref('2030-01-01T09:00')
      const newValue = ref('2031-02-01T11:00')
      const view = await renderWithPlugins(
        defineComponent({
          render() {
            const value = first.value ? oldValue : newValue
            return h(CalendarDateTimeInput, {
              key: first.value ? 'old' : 'new',
              inputId: 'reused-calendar-time',
              label: '开始时间',
              modelValue: value.value,
              allDay: false,
              disabled: false,
              'onUpdate:modelValue': (next: string) => {
                value.value = next
              },
            })
          },
        }),
      )
      await fireEvent.click(view.getByRole('button', { name: '选择开始时间' }))
      const oldDay = within(await view.findByRole('dialog')).getByRole(
        'button',
        { name: '2030-01-02' },
      )
      first.value = false
      await waitFor(() =>
        expect(view.getByLabelText('开始时间')).toHaveValue('2031-02-01T11:00'),
      )
      // 原离场 portal 的节点可仍被事件队列持有；不能通过全局同 ID 找到新实例并写旧值。
      if (operation === 'click') await fireEvent.click(oldDay)
      else await fireEvent.keyDown(oldDay, { key: 'Enter', code: 'Enter' })
      await waitFor(() =>
        expect(view.getByLabelText('开始时间')).toHaveValue('2031-02-01T11:00'),
      )
      expect(newValue.value).toBe('2031-02-01T11:00')
    },
  )
  it('preserves 409 user input and maps a real error code inside the sole recovery alert', async () => {
    vi.mocked(calendar.updateCalendarProposal).mockRejectedValue(
      new ProblemError({
        type: 'about:blank',
        title: 'PRIVATE TITLE',
        detail: 'PRIVATE DETAIL',
        instance: '',
        status: 409,
        error_code: 'calendar_proposal_version_conflict',
        trace_id: 'synthetic-calendar-conflict',
      }),
    )
    const view = await page()
    await fireEvent.update(
      view.getByLabelText('日程标题'),
      'Synthetic pending edit',
    )
    await fireEvent.click(view.getByRole('button', { name: '保存提案' }))
    const alert = await view.findByRole('alert')
    expect(alert).toHaveTextContent('synthetic-calendar-conflict')
    expect(view.getByLabelText('日程标题')).toHaveValue(
      'Synthetic pending edit',
    )
    expect(view.queryByText(/PRIVATE/)).toBeNull()
    expect(view.getAllByRole('alert')).toHaveLength(1)
  })
})
