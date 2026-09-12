import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { ref } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import CalendarProposalPage from './CalendarProposalPage.vue'
import * as calendar from '@/api/calendar'
import * as connections from '@/api/connections'
import { ProblemError } from '@/api/client'
import type { CalendarProposal } from '@/api/types'
import {
  calendarProposal,
  CONNECTION_ID,
  PROPOSAL_ID,
  TASK_ID,
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
  createRestoreProposal: vi.fn(),
}))
vi.mock('@/api/connections', () => ({
  listConnections: vi.fn(),
  getConnectionCapabilities: vi.fn(),
}))
vi.mock('@/composables/useTaskEvents', () => ({
  useTaskEvents: () => ref('connected'),
}))
let current: CalendarProposal
let wrappers: ReturnType<typeof mount>[] = []
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
      conflicts: [
        {
          kind: 'overlap',
          starts_at: '2030-01-01T01:00:00Z',
          ends_at: '2030-01-01T02:00:00Z',
          missing_connection_ids: [],
        },
      ],
    },
  }
  vi.mocked(calendar.getCalendarProposal).mockImplementation(
    async () => current,
  )
  vi.mocked(calendar.updateCalendarProposal).mockImplementation(
    async (_id, input) => {
      if ('confirmation' in input && input.confirmation) {
        const confirmation = input.confirmation
        current = {
          ...current,
          version: input.version + 1,
          required_confirmations: current.required_confirmations.filter(
            (kind) => kind !== confirmation.kind,
          ),
          ...(confirmation.kind === 'calendar'
            ? {
                connection_id: confirmation.connection_id,
                calendar_id: confirmation.calendar_id,
              }
            : {}),
        }
      } else
        current = {
          ...current,
          ...input,
          attendees:
            input.attendees === undefined
              ? current.attendees
              : (input.attendees ?? []),
          version: input.version + 1,
        }
      return { ...current, editor_facts: null }
    },
  )
  vi.mocked(calendar.submitCalendarProposal).mockResolvedValue({
    task_id: TASK_ID,
    status: 'queued',
  })
  vi.mocked(calendar.createRestoreProposal).mockResolvedValue({
    task_id: TASK_ID,
    status: 'queued',
  })
  vi.mocked(connections.listConnections).mockResolvedValue([
    connection(),
    connection('microsoft'),
  ])
  vi.mocked(connections.getConnectionCapabilities).mockImplementation(
    async (id) =>
      connectionCapabilities(id === CONNECTION_ID ? 'google' : 'microsoft'),
  )
})
afterEach(() => {
  wrappers.forEach((wrapper) => wrapper.unmount())
  wrappers = []
})

/** 页面使用真实受保护路由形状；目录、提案与候选全部来自合成端口。 */
async function renderPage() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      {
        path: '/calendar/proposals/:proposalId',
        component: CalendarProposalPage,
      },
      { path: '/actions', component: { template: '<p>操作中心</p>' } },
      { path: '/tasks', component: { template: '<p>任务中心</p>' } },
      { path: '/connections', component: { template: '<p>连接</p>' } },
    ],
  })
  await router.push(`/calendar/proposals/${PROPOSAL_ID}`)
  const wrapper = mount(CalendarProposalPage, {
    global: { plugins: [createPinia(), router] },
  })
  wrappers.push(wrapper)
  await flushPromises()
  return { wrapper, router }
}

describe('CalendarProposalPage', () => {
  it.each([
    'available',
    'capability-unavailable',
    'removed',
    'directory-unavailable',
  ] as const)(
    'shows missing calendar account identities beside both partial results when the catalog is %s',
    async (catalogState) => {
      const missing = connection('microsoft')
      current = {
        ...current,
        editor_facts: {
          before_status: 'not_applicable',
          before: null,
          reprepare_source: null,
          restore_source: null,
          conflict_status: 'checked',
          conflicts: [
            {
              kind: 'partial_sources',
              starts_at: null,
              ends_at: null,
              missing_connection_ids: [missing.id],
            },
          ],
        },
        availability: {
          proposal_id: PROPOSAL_ID,
          version: 1,
          completeness: 'partial',
          missing_connections: [missing.id],
          attendee_availability_checked: false,
          candidates: [
            {
              starts_at: '2030-01-02T01:00:00Z',
              ends_at: '2030-01-02T02:00:00Z',
            },
          ],
        },
      }
      if (catalogState === 'removed')
        vi.mocked(connections.listConnections).mockResolvedValue([connection()])
      if (catalogState === 'directory-unavailable')
        vi.mocked(connections.listConnections).mockRejectedValue(
          new Error('Synthetic directory failure'),
        )
      if (catalogState === 'capability-unavailable')
        vi.mocked(connections.getConnectionCapabilities).mockImplementation(
          async (id) => {
            if (id === missing.id)
              throw new Error('Synthetic capability failure')
            return connectionCapabilities()
          },
        )
      const { wrapper } = await renderPage()
      // 必须在各自的结果旁标识缺失账户，不能借页面其他位置的完整目录让断言误通过。
      for (const label of ['服务端候选时间', '日程冲突检查']) {
        const result = wrapper.get(`[aria-label="${label}"]`).text()
        if (
          catalogState === 'available' ||
          catalogState === 'capability-unavailable'
        ) {
          expect(result).toContain('Microsoft')
          expect(result).toContain(missing.account_email)
        } else {
          expect(result).toContain(missing.id)
          expect(result).toContain('账户资料暂不可用')
        }
        expect(result).not.toContain(connection().account_email)
      }
      expect(connections.listConnections).toHaveBeenCalledTimes(1)
      expect(calendar.submitCalendarProposal).not.toHaveBeenCalled()
    },
  )

  it('prepares a restore only on explicit click and follows the real queued task', async () => {
    const eventId = '00000000-0000-0000-0000-000000000501'
    const snapshotId = '00000000-0000-0000-0000-000000000502'
    current = {
      ...current,
      operation_kind: 'update',
      status: 'applied',
      target_event_id: 'synthetic-provider-event',
      before_snapshot_id: snapshotId,
      editor_facts: {
        before_status: 'available',
        before: calendarFields(),
        conflict_status: 'checked',
        conflicts: [],
        reprepare_source: null,
        restore_source: { event_id: eventId, snapshot_id: snapshotId },
      },
    }
    const { wrapper, router } = await renderPage()
    expect(calendar.createRestoreProposal).not.toHaveBeenCalled()
    await wrapper.get('button[name="prepare-restore"]').trigger('click')
    await flushPromises()
    expect(calendar.createRestoreProposal).toHaveBeenCalledWith(
      eventId,
      snapshotId,
      expect.objectContaining({ key: expect.any(String) }),
    )
    expect(router.currentRoute.value.fullPath).toBe(`/tasks?task_id=${TASK_ID}`)
    expect(calendar.submitCalendarProposal).not.toHaveBeenCalled()
    expect(calendar.updateCalendarProposal).not.toHaveBeenCalled()
  })

  it('keeps one restore intent across an unknown transport failure and blocks duplicate clicks', async () => {
    current = {
      ...current,
      operation_kind: 'update',
      status: 'applied',
      before_snapshot_id: '00000000-0000-0000-0000-000000000502',
      editor_facts: {
        before_status: 'available',
        before: calendarFields(),
        conflict_status: 'checked',
        conflicts: [],
        reprepare_source: null,
        restore_source: {
          event_id: '00000000-0000-0000-0000-000000000501',
          snapshot_id: '00000000-0000-0000-0000-000000000502',
        },
      },
    }
    let fail: (cause: Error) => void = () => undefined
    vi.mocked(calendar.createRestoreProposal).mockReturnValueOnce(
      new Promise((_resolve, reject) => {
        fail = reject
      }),
    )
    const { wrapper } = await renderPage()
    const button = wrapper.get('button[name="prepare-restore"]')
    await button.trigger('click')
    await button.trigger('click')
    expect(calendar.createRestoreProposal).toHaveBeenCalledTimes(1)
    expect(button.attributes('disabled')).toBeDefined()
    fail(new TypeError('Synthetic transport interruption'))
    await flushPromises()
    await button.trigger('click')
    await flushPromises()
    expect(calendar.createRestoreProposal).toHaveBeenCalledTimes(2)
    expect(vi.mocked(calendar.createRestoreProposal).mock.calls[1]?.[2]).toBe(
      vi.mocked(calendar.createRestoreProposal).mock.calls[0]?.[2],
    )
    expect(calendar.submitCalendarProposal).not.toHaveBeenCalled()
  })

  it('shows an unavailable restore source without guessing a provider ID as the local event', async () => {
    current = {
      ...current,
      operation_kind: 'update',
      status: 'applied',
      target_event_id: 'synthetic-provider-event',
      editor_facts: {
        before_status: 'unavailable',
        before: null,
        conflict_status: 'checked',
        conflicts: [],
        reprepare_source: null,
        restore_source: null,
      },
    }
    const { wrapper } = await renderPage()
    expect(wrapper.find('button[name="prepare-restore"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('恢复来源已不可用')
    expect(calendar.createRestoreProposal).not.toHaveBeenCalled()
  })

  it('keeps shell confirmations explicit and never confirms ordinary saves', async () => {
    const { wrapper } = await renderPage()
    expect(wrapper.text()).toContain('Asia/Shanghai')
    expect(
      wrapper.get('button[name="submit-proposal"]').attributes('disabled'),
    ).toBeDefined()
    await wrapper
      .get('input[aria-label="日程标题"]')
      .setValue('Synthetic revision')
    await wrapper.get('button[name="save-proposal"]').trigger('click')
    await flushPromises()
    expect(
      vi.mocked(calendar.updateCalendarProposal).mock.calls[0]?.[1],
    ).not.toHaveProperty('confirmation')
    expect(calendar.getCalendarProposal).toHaveBeenCalledTimes(2)
    await wrapper.get('button[name="confirm-time"]').trigger('click')
    await flushPromises()
    expect(calendar.updateCalendarProposal).toHaveBeenLastCalledWith(
      PROPOSAL_ID,
      { version: 2, confirmation: { kind: 'time' } },
    )
    expect(calendar.submitCalendarProposal).not.toHaveBeenCalled()
  })

  it('reselects the exact calendar with a standalone confirmation and locks update sources', async () => {
    const { wrapper } = await renderPage()
    await wrapper
      .get('select[aria-label="日历账户"]')
      .setValue(connection('microsoft').id)
    await wrapper
      .get('select[aria-label="目标日历"]')
      .setValue('synthetic-microsoft-calendar')
    await wrapper.get('button[name="confirm-calendar"]').trigger('click')
    await flushPromises()
    expect(calendar.updateCalendarProposal).toHaveBeenCalledWith(PROPOSAL_ID, {
      version: 1,
      confirmation: {
        kind: 'calendar',
        connection_id: connection('microsoft').id,
        calendar_id: 'synthetic-microsoft-calendar',
      },
    })
    current = {
      ...current,
      operation_kind: 'update',
      target_event_id: 'synthetic-event',
      base_etag: 'synthetic-etag',
      before_snapshot_id: '00000000-0000-0000-0000-000000000099',
      editor_facts: {
        reprepare_source: null,
        restore_source: null,
        before_status: 'available',
        before: {
          ...calendarFields(),
          location: 'Synthetic original location',
        },
        conflict_status: 'checked',
        conflicts: [],
      },
    }
    await wrapper.get('button[name="reload-editor"]').trigger('click')
    await flushPromises()
    expect(
      wrapper.get('select[aria-label="日历账户"]').attributes('disabled'),
    ).toBeDefined()
    expect(wrapper.text()).toContain('Synthetic original location')
    expect(wrapper.text()).toContain('synthetic-etag')
  })

  it('invalidates old server conflicts and candidates immediately after a time edit', async () => {
    current = {
      ...current,
      availability: {
        proposal_id: PROPOSAL_ID,
        version: 1,
        completeness: 'complete',
        missing_connections: [],
        attendee_availability_checked: false,
        candidates: [
          {
            starts_at: '2030-01-02T01:00:00Z',
            ends_at: '2030-01-02T02:00:00Z',
          },
        ],
      },
    }
    const { wrapper } = await renderPage()
    expect(wrapper.find('[data-testid="calendar-conflicts"]').exists()).toBe(
      true,
    )
    expect(wrapper.findAll('button[name="choose-candidate"]')).toHaveLength(1)
    await wrapper
      .get('input[aria-label="开始时间"]')
      .setValue('2030-01-03T09:00')
    expect(wrapper.find('[data-testid="calendar-conflicts"]').exists()).toBe(
      false,
    )
    expect(wrapper.findAll('button[name="choose-candidate"]')).toHaveLength(0)
    expect(wrapper.text()).toContain('保存后重新检查')
  })

  it('shows three server candidates, partial sources and the explicit attendee limitation', async () => {
    // 真实候选端口持久化新的不可变版本；后续读取和保存必须使用该版本。
    vi.mocked(calendar.suggestCalendarTimes).mockImplementation(async () => {
      const availability = {
        proposal_id: PROPOSAL_ID,
        version: 2,
        completeness: 'partial' as const,
        missing_connections: [connection('microsoft').id],
        attendee_availability_checked: false as const,
        candidates: [2, 3, 4].map((day) => ({
          starts_at: `2030-01-0${day}T01:00:00Z`,
          ends_at: `2030-01-0${day}T02:00:00Z`,
        })),
      }
      current = { ...current, version: 2, availability }
      return availability
    })
    const { wrapper } = await renderPage()
    await wrapper.get('button[name="suggest-times"]').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('button[name="choose-candidate"]')).toHaveLength(3)
    expect(wrapper.text()).toContain('部分日历来源')
    expect(wrapper.text()).toContain('未检查参会人可用性')
    expect(wrapper.text()).toContain('版本 2')
    expect(calendar.getCalendarProposal).toHaveBeenCalledTimes(2)
    await wrapper.get('button[name="choose-candidate"]').trigger('click')
    expect(wrapper.get('input[aria-label="开始时间"]').element).toHaveProperty(
      'value',
      '2030-01-02T09:00',
    )
    expect(calendar.updateCalendarProposal).not.toHaveBeenCalled()
    await wrapper.get('button[name="save-proposal"]').trigger('click')
    await flushPromises()
    expect(calendar.updateCalendarProposal).toHaveBeenLastCalledWith(
      PROPOSAL_ID,
      expect.objectContaining({ version: 2 }),
    )
  })

  it('does not overwrite a newer proposal with the earlier candidate receipt', async () => {
    vi.mocked(calendar.suggestCalendarTimes).mockImplementation(async () => {
      current = { ...current, version: 3, availability: null }
      return {
        proposal_id: PROPOSAL_ID,
        version: 2,
        completeness: 'complete',
        missing_connections: [],
        attendee_availability_checked: false,
        candidates: [
          {
            starts_at: '2030-01-02T01:00:00Z',
            ends_at: '2030-01-02T02:00:00Z',
          },
        ],
      }
    })
    const { wrapper } = await renderPage()
    await wrapper.get('button[name="suggest-times"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('版本 3')
    expect(wrapper.findAll('button[name="choose-candidate"]')).toHaveLength(0)
    await wrapper.get('button[name="confirm-time"]').trigger('click')
    await flushPromises()
    expect(calendar.updateCalendarProposal).toHaveBeenLastCalledWith(
      PROPOSAL_ID,
      { version: 3, confirmation: { kind: 'time' } },
    )
  })

  it('requires a fresh read when a candidate version was saved but reloading failed', async () => {
    vi.mocked(calendar.suggestCalendarTimes).mockImplementation(async () => {
      const availability = {
        proposal_id: PROPOSAL_ID,
        version: 2,
        completeness: 'complete' as const,
        missing_connections: [],
        attendee_availability_checked: false as const,
        candidates: [
          {
            starts_at: '2030-01-02T01:00:00Z',
            ends_at: '2030-01-02T02:00:00Z',
          },
        ],
      }
      current = { ...current, version: 2, availability }
      return availability
    })
    const { wrapper } = await renderPage()
    vi.mocked(calendar.getCalendarProposal).mockRejectedValueOnce(
      new Error('Synthetic read unavailable'),
    )
    await wrapper.get('button[name="suggest-times"]').trigger('click')
    await flushPromises()
    expect(
      wrapper.get('button[name="confirm-time"]').attributes('disabled'),
    ).toBeDefined()
    expect(
      wrapper.get('button[name="suggest-times"]').attributes('disabled'),
    ).toBeDefined()
    expect(wrapper.findAll('button[name="choose-candidate"]')).toHaveLength(0)
    expect(wrapper.text()).toContain('重新加载')
    expect(calendar.updateCalendarProposal).not.toHaveBeenCalled()
    await wrapper.get('button[name="reload-editor"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('版本 2')
    expect(
      wrapper.get('button[name="confirm-time"]').attributes('disabled'),
    ).toBeUndefined()
  })

  it('saves all-day exclusive dates and explicit notification policy without UTC shifting', async () => {
    const { wrapper } = await renderPage()
    await wrapper.get('input[aria-label="全天日程"]').setValue(true)
    await wrapper.get('input[aria-label="开始日期"]').setValue('2030-01-05')
    await wrapper
      .get('input[aria-label="结束日期（不含）"]')
      .setValue('2030-01-06')
    await wrapper
      .get('input[aria-label="参会人"]')
      .setValue('attendee@example.test')
    await wrapper.get('select[aria-label="通知策略"]').setValue('all')
    await wrapper.get('button[name="save-proposal"]').trigger('click')
    await flushPromises()
    expect(calendar.updateCalendarProposal).toHaveBeenCalledWith(
      PROPOSAL_ID,
      expect.objectContaining({
        starts_at: '2030-01-05',
        ends_at: '2030-01-06',
        all_day: true,
        timezone: 'Asia/Shanghai',
        notification_policy: 'all',
        attendees: ['attendee@example.test'],
      }),
    )
  })

  it('preserves original instant precision when unrelated fields are edited', async () => {
    current = { ...current, starts_at: '2030-01-01T01:00:42.123456Z' }
    const { wrapper } = await renderPage()
    await wrapper
      .get('input[aria-label="日程标题"]')
      .setValue('Precision retained')
    await wrapper.get('button[name="save-proposal"]').trigger('click')
    await flushPromises()
    expect(calendar.updateCalendarProposal).toHaveBeenCalledWith(
      PROPOSAL_ID,
      expect.objectContaining({ starts_at: '2030-01-01T01:00:42.123456Z' }),
    )
  })

  it('keeps a stale ETag error visible and never silently submits another version', async () => {
    current = { ...current, required_confirmations: [] }
    vi.mocked(calendar.submitCalendarProposal).mockRejectedValue(
      new ProblemError({
        type: 'about:blank',
        title: 'Conflict',
        detail: '',
        status: 409,
        instance: '',
        error_code: 'calendar_etag_stale',
        trace_id: 'synthetic-etag-trace',
      }),
    )
    const { wrapper } = await renderPage()
    await wrapper.get('button[name="submit-proposal"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('synthetic-etag-trace')
    expect(wrapper.text()).toContain('创建新版本')
    expect(calendar.submitCalendarProposal).toHaveBeenCalledTimes(1)
    expect(calendar.updateCalendarProposal).not.toHaveBeenCalled()
  })

  it('does not label an update with an unavailable original snapshot as a creation', async () => {
    current = {
      ...current,
      operation_kind: 'update',
      target_event_id: 'synthetic-event',
      base_etag: 'synthetic-etag',
      before_snapshot_id: '00000000-0000-0000-0000-000000000099',
      required_confirmations: [],
      changed_fields: ['location'],
      editor_facts: {
        reprepare_source: null,
        restore_source: null,
        before_status: 'unavailable',
        before: null,
        conflict_status: 'checked',
        conflicts: [],
      },
    }
    const { wrapper } = await renderPage()
    expect(wrapper.text()).toContain('原始修改前快照已不可用')
    expect(wrapper.text()).not.toContain('拟创建的日程')
    expect(wrapper.find('[aria-label="日程前后对比"]').exists()).toBe(false)
    expect(
      wrapper.get('button[name="submit-proposal"]').attributes('disabled'),
    ).toBeDefined()
  })
})
