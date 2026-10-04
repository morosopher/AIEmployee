import { getByRole, queryAllByRole } from '@testing-library/vue'
import { DOMWrapper, flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import { primeVueOptions } from '@/design/primevue'
import { installViewport, restoreViewport } from '@/test-support/viewport'
import { defineComponent, ref } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import CalendarProposalPage from './CalendarProposalPage.vue'
import * as calendar from '@/api/calendar'
import * as connections from '@/api/connections'
import { ProblemError } from '@/api/client'
import type { CalendarProposal } from '@/api/types'
import EditorRecovery from '@/components/EditorRecovery.vue'
import { useCalendarProposalEditor } from '@/features/calendar/useCalendarProposalEditor'
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
/** 用公开按钮名称选择现有 Vue Test Utils 实例，保留恢复 Props 契约断言。 */
function byButton(wrapper: ReturnType<typeof mount>, name: string) {
  return new DOMWrapper(
    getByRole(wrapper.element as HTMLElement, 'button', {
      name: new RegExp(`^${name}`),
    }),
  )
}
let current: CalendarProposal
let wrappers: ReturnType<typeof mount>[] = []
beforeEach(() => {
  installViewport()
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
  document.body.innerHTML = ''
  restoreViewport()
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
    attachTo: document.body,
    global: {
      stubs: { transition: false, 'transition-group': false },
      plugins: [
        createPinia(),
        router,
        [PrimeVue, primeVueOptions],
        ConfirmationService,
      ],
    },
  })
  wrappers.push(wrapper)
  await vi.waitFor(() =>
    expect(wrapper.find('[aria-label="日程标题"]').exists()).toBe(true),
  )
  await flushPromises()
  return { wrapper, router }
}

/** 构造可核对原 before 的修改提案，使刷新测试能观察确认、CAS 与提交的真实页面约束。 */
function useUpdateFixture(
  required: CalendarProposal['required_confirmations'],
): void {
  current = {
    ...current,
    operation_kind: 'update',
    target_event_id: 'synthetic-refresh-event',
    base_etag: 'synthetic-refresh-etag',
    before_snapshot_id: '00000000-0000-0000-0000-000000000502',
    required_confirmations: required,
    changed_fields: ['location'],
    editor_facts: {
      before_status: 'available',
      before: { ...calendarFields(), location: 'Synthetic original place' },
      conflict_status: 'checked',
      conflicts: [],
      reprepare_source: null,
      restore_source: null,
    },
  }
}

/** 真实客户端错误类型只承载合成数据；页面不得回显原始 title/detail。 */
function readProblem(): ProblemError {
  return new ProblemError({
    type: 'about:blank',
    title: 'Synthetic private read title',
    detail: 'Synthetic private read detail',
    status: 503,
    instance: '/synthetic/calendar-read',
    error_code: 'synthetic_calendar_read_unavailable',
    trace_id: 'synthetic-calendar-read-trace',
  })
}

describe('CalendarProposalPage', () => {
  it.each(['domain-case', 'local-case'] as const)(
    'preserves attendees and applies the shared mailbox identity rule for %s',
    async (difference) => {
      const attendees = [
        'CaseUser@mail.example.test',
        difference === 'domain-case'
          ? 'CaseUser@MAIL.EXAMPLE.TEST'
          : 'caseUser@MAIL.EXAMPLE.TEST',
      ]
      const { wrapper } = await renderPage()
      const input = attendees.join(', ')
      await wrapper.get('[aria-label="参会人"]').setValue(input)
      await byButton(wrapper, '保存提案').trigger('click')
      await flushPromises()
      // 邮件与日程共用同一即时校验；合法的不同本地部分必须原样进入 PATCH。
      if (difference === 'domain-case') {
        expect(calendar.updateCalendarProposal).not.toHaveBeenCalled()
        expect(wrapper.get('[role="alert"]').text()).toContain('收件人地址重复')
        expect(wrapper.getComponent(EditorRecovery).props('error')).toEqual({
          message: '收件人地址重复，请检查 To、CC、BCC。',
          traceId: null,
          action: 'retry',
        })
      } else {
        expect(calendar.updateCalendarProposal).toHaveBeenCalledWith(
          PROPOSAL_ID,
          expect.objectContaining({ version: 1, attendees }),
        )
        expect(wrapper.text()).toContain('版本 2')
      }
      expect(wrapper.get('[aria-label="参会人"]').element).toHaveProperty(
        'value',
        input,
      )
      expect(calendar.submitCalendarProposal).not.toHaveBeenCalled()
    },
  )

  it.each([
    { operation: 'save', errorKind: 'plain' },
    { operation: 'last-confirmation', errorKind: 'plain' },
    { operation: 'save', errorKind: 'problem' },
    { operation: 'last-confirmation', errorKind: 'problem' },
  ])(
    'keeps the acknowledged version locked after $operation when the facts read fails with $errorKind',
    async ({ operation, errorKind }) => {
      useUpdateFixture(
        operation === 'save' ? ['time'] : ['notification_policy'],
      )
      const { wrapper } = await renderPage()
      expect(wrapper.find('[aria-label="日程前后对比"]').exists()).toBe(true)
      expect(byButton(wrapper, '提交审批').attributes('disabled')).toBeDefined()
      vi.mocked(calendar.getCalendarProposal).mockRejectedValueOnce(
        errorKind === 'problem'
          ? readProblem()
          : new Error('Synthetic post-mutation read failure'),
      )
      if (operation === 'save') {
        await wrapper
          .get('[aria-label="日程标题"]')
          .setValue('Synthetic revised calendar title')
        await byButton(wrapper, '保存提案').trigger('click')
      } else await byButton(wrapper, '确认通知策略').trigger('click')
      await flushPromises()

      expect(calendar.updateCalendarProposal).toHaveBeenCalledTimes(1)
      expect(calendar.updateCalendarProposal).toHaveBeenCalledWith(
        PROPOSAL_ID,
        expect.objectContaining({ version: 1 }),
      )
      expect(calendar.getCalendarProposal).toHaveBeenCalledTimes(2)
      expect(wrapper.text()).toContain('版本 2')
      expect(wrapper.find('[aria-label="日程前后对比"]').exists()).toBe(false)
      expect(wrapper.get('[role="alert"]').text()).toContain(
        '提案事实尚未读取完整，请重新加载后继续编辑或提交。',
      )
      // 通过真实 hook → EditorRecovery 的 Props 检查 UI 端口，不能用测试 helper 伪造接线。
      expect(wrapper.getComponent(EditorRecovery).props('error')).toEqual({
        message: '提案事实尚未读取完整，请重新加载后继续编辑或提交。',
        traceId:
          errorKind === 'problem' ? 'synthetic-calendar-read-trace' : null,
        action: 'reload',
        ...(errorKind === 'problem'
          ? { problem: { error_code: 'synthetic_calendar_read_unavailable' } }
          : {}),
      })
      expect(wrapper.text()).not.toContain('Synthetic private read')
      // PATCH 已成功，缺失事实期间必须同时锁住输入、后续确认与提交，不能重发旧版本。
      expect
        .soft(wrapper.get('[aria-label="日程标题"]').attributes('disabled'))
        .toBeDefined()
      expect
        .soft(byButton(wrapper, '确认时间').attributes('disabled'))
        .toBeDefined()
      expect
        .soft(byButton(wrapper, '提交审批').attributes('disabled'))
        .toBeDefined()
      await byButton(wrapper, '提交审批').trigger('click')
      await flushPromises()
      expect(calendar.submitCalendarProposal).not.toHaveBeenCalled()

      await byButton(wrapper, '重新加载提案').trigger('click')
      await flushPromises()
      expect(wrapper.text()).toContain('版本 2')
      expect(wrapper.find('[aria-label="日程前后对比"]').exists()).toBe(true)
      expect(
        wrapper.get('[aria-label="日程标题"]').attributes('disabled'),
      ).toBeUndefined()
      expect(calendar.updateCalendarProposal).toHaveBeenCalledTimes(1)
      expect(wrapper.getComponent(EditorRecovery).props('error')).toBeNull()
      if (operation === 'save') {
        await byButton(wrapper, '确认时间').trigger('click')
        await flushPromises()
        expect(calendar.updateCalendarProposal).toHaveBeenLastCalledWith(
          PROPOSAL_ID,
          { version: 2, confirmation: { kind: 'time' } },
        )
      }
      await byButton(wrapper, '提交审批').trigger('click')
      await flushPromises()
      expect(calendar.submitCalendarProposal).toHaveBeenCalledTimes(1)
      expect(calendar.submitCalendarProposal).toHaveBeenCalledWith(
        PROPOSAL_ID,
        operation === 'save' ? 3 : 2,
        expect.objectContaining({ key: expect.any(String) }),
      )
    },
  )

  it.each(['older-version', 'other-object', 'missing-facts'] as const)(
    'does not unlock or roll back an acknowledged version on a %s read',
    async (invalidRead) => {
      useUpdateFixture(['notification_policy'])
      const { wrapper } = await renderPage()
      const unreadable = async (): Promise<CalendarProposal> => {
        if (invalidRead === 'older-version') return { ...current, version: 1 }
        if (invalidRead === 'other-object')
          return {
            ...current,
            id: '00000000-0000-0000-0000-000000000399',
          }
        return { ...current, editor_facts: null }
      }
      vi.mocked(calendar.getCalendarProposal).mockImplementationOnce(unreadable)
      await byButton(wrapper, '确认通知策略').trigger('click')
      await flushPromises()
      expect(wrapper.text()).toContain('版本 2')
      expect
        .soft(byButton(wrapper, '提交审批').attributes('disabled'))
        .toBeDefined()
      expect.soft(wrapper.find('[role="alert"]').exists()).toBe(true)

      // 显式重新加载也必须遵守已确认的版本下界和同对象事实，不能借刷新回退 CAS。
      vi.mocked(calendar.getCalendarProposal).mockImplementationOnce(unreadable)
      await byButton(wrapper, '重新加载提案').trigger('click')
      await flushPromises()
      expect.soft(wrapper.text()).toContain('版本 2')
      expect
        .soft(wrapper.get('[aria-label="日程标题"]').attributes('disabled'))
        .toBeDefined()
      expect(calendar.updateCalendarProposal).toHaveBeenCalledTimes(1)
      expect(calendar.submitCalendarProposal).not.toHaveBeenCalled()

      current = { ...current, version: 3 }
      await byButton(wrapper, '重新加载提案').trigger('click')
      await flushPromises()
      expect(wrapper.text()).toContain('版本 3')
      expect(wrapper.find('[aria-label="日程前后对比"]').exists()).toBe(true)
      expect(
        byButton(wrapper, '提交审批').attributes('disabled'),
      ).toBeUndefined()
      expect(calendar.updateCalendarProposal).toHaveBeenCalledTimes(1)
    },
  )

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
    await byButton(wrapper, '准备恢复提案').trigger('click')
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
    const button = byButton(wrapper, '准备恢复提案')
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
    expect(
      queryAllByRole(wrapper.element as HTMLElement, 'button', {
        name: /^准备恢复提案/,
      }).length > 0,
    ).toBe(false)
    expect(wrapper.text()).toContain('恢复来源已不可用')
    expect(calendar.createRestoreProposal).not.toHaveBeenCalled()
  })

  it('keeps shell confirmations explicit and never confirms ordinary saves', async () => {
    const { wrapper } = await renderPage()
    expect(wrapper.text()).toContain('Asia/Shanghai')
    expect(byButton(wrapper, '提交审批').attributes('disabled')).toBeDefined()
    await wrapper.get('[aria-label="日程标题"]').setValue('Synthetic revision')
    await byButton(wrapper, '保存提案').trigger('click')
    await flushPromises()
    expect(
      vi.mocked(calendar.updateCalendarProposal).mock.calls[0]?.[1],
    ).not.toHaveProperty('confirmation')
    expect(calendar.getCalendarProposal).toHaveBeenCalledTimes(2)
    await byButton(wrapper, '确认时间').trigger('click')
    await flushPromises()
    expect(calendar.updateCalendarProposal).toHaveBeenLastCalledWith(
      PROPOSAL_ID,
      { version: 2, confirmation: { kind: 'time' } },
    )
    expect(calendar.submitCalendarProposal).not.toHaveBeenCalled()
  })

  it('reselects the exact calendar with a standalone confirmation and locks update sources', async () => {
    const { wrapper } = await renderPage()
    const account = new DOMWrapper(
      getByRole(wrapper.element as HTMLElement, 'combobox', {
        name: '日历账户',
      }),
    )
    await account.trigger('click')
    await account.trigger('keydown', { key: 'End', code: 'End' })
    await account.trigger('keydown', { key: 'Enter', code: 'Enter' })
    const target = new DOMWrapper(
      getByRole(wrapper.element as HTMLElement, 'combobox', {
        name: '目标日历',
      }),
    )
    await target.trigger('click')
    await target.trigger('keydown', { key: 'End', code: 'End' })
    await target.trigger('keydown', { key: 'Enter', code: 'Enter' })
    await byButton(wrapper, '确认此日历').trigger('click')
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
    await byButton(wrapper, '重新加载提案').trigger('click')
    await flushPromises()
    expect(
      new DOMWrapper(
        getByRole(wrapper.element as HTMLElement, 'combobox', {
          name: '日历账户',
        }),
      ).attributes('aria-disabled'),
    ).toBe('true')
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
    expect(wrapper.find('[aria-label="日程冲突检查"]').exists()).toBe(true)
    expect(
      queryAllByRole(wrapper.element as HTMLElement, 'button', {
        name: /至.*（/,
      }),
    ).toHaveLength(1)
    await wrapper.get('[aria-label="开始时间"]').setValue('2030-01-03T09:00')
    expect(wrapper.find('[aria-label="日程冲突检查"]').exists()).toBe(false)
    expect(
      queryAllByRole(wrapper.element as HTMLElement, 'button', {
        name: /至.*（/,
      }),
    ).toHaveLength(0)
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
    await byButton(wrapper, '查询三个候选时间').trigger('click')
    await flushPromises()
    expect(
      queryAllByRole(wrapper.element as HTMLElement, 'button', {
        name: /至.*（/,
      }),
    ).toHaveLength(3)
    expect(wrapper.text()).toContain('部分日历来源')
    expect(wrapper.text()).toContain('未检查参会人可用性')
    expect(wrapper.text()).toContain('版本 2')
    expect(calendar.getCalendarProposal).toHaveBeenCalledTimes(2)
    await new DOMWrapper(
      queryAllByRole(wrapper.element as HTMLElement, 'button', {
        name: /至.*（/,
      })[0] as HTMLElement,
    ).trigger('click')
    expect(wrapper.get('[aria-label="开始时间"]').element).toHaveProperty(
      'value',
      '2030-01-02T09:00',
    )
    expect(calendar.updateCalendarProposal).not.toHaveBeenCalled()
    await byButton(wrapper, '保存提案').trigger('click')
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
    await byButton(wrapper, '查询三个候选时间').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('版本 3')
    expect(
      queryAllByRole(wrapper.element as HTMLElement, 'button', {
        name: /至.*（/,
      }),
    ).toHaveLength(0)
    await byButton(wrapper, '确认时间').trigger('click')
    await flushPromises()
    expect(calendar.updateCalendarProposal).toHaveBeenLastCalledWith(
      PROPOSAL_ID,
      { version: 3, confirmation: { kind: 'time' } },
    )
  })

  it.each(['plain', 'problem'] as const)(
    'requires a fresh read when a candidate version was saved but reloading failed with %s',
    async (errorKind) => {
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
        errorKind === 'problem'
          ? readProblem()
          : new Error('Synthetic read unavailable'),
      )
      await byButton(wrapper, '查询三个候选时间').trigger('click')
      await flushPromises()
      expect(byButton(wrapper, '确认时间').attributes('disabled')).toBeDefined()
      expect(
        byButton(wrapper, '查询三个候选时间').attributes('disabled'),
      ).toBeDefined()
      expect(
        queryAllByRole(wrapper.element as HTMLElement, 'button', {
          name: /至.*（/,
        }),
      ).toHaveLength(0)
      expect(wrapper.get('[role="alert"]').text()).toContain(
        '候选已保存，请重新加载最新提案后继续编辑。',
      )
      expect(wrapper.getComponent(EditorRecovery).props('error')).toEqual({
        message: '候选已保存，请重新加载最新提案后继续编辑。',
        traceId:
          errorKind === 'problem' ? 'synthetic-calendar-read-trace' : null,
        action: 'reload',
        ...(errorKind === 'problem'
          ? { problem: { error_code: 'synthetic_calendar_read_unavailable' } }
          : {}),
      })
      expect(wrapper.text()).not.toContain('Synthetic private read')
      expect(calendar.suggestCalendarTimes).toHaveBeenCalledExactlyOnceWith(
        PROPOSAL_ID,
        { version: 1 },
      )
      expect(calendar.getCalendarProposal).toHaveBeenCalledTimes(2)
      expect(calendar.updateCalendarProposal).not.toHaveBeenCalled()
      // 候选回执已证明版本推进；显式刷新同样不能用较旧 GET 解除锁定。
      vi.mocked(calendar.getCalendarProposal).mockResolvedValueOnce({
        ...current,
        version: 1,
        availability: null,
      })
      await byButton(wrapper, '重新加载提案').trigger('click')
      await flushPromises()
      expect(byButton(wrapper, '确认时间').attributes('disabled')).toBeDefined()
      await byButton(wrapper, '重新加载提案').trigger('click')
      await flushPromises()
      expect(wrapper.text()).toContain('版本 2')
      expect(
        byButton(wrapper, '确认时间').attributes('disabled'),
      ).toBeUndefined()
      expect(wrapper.getComponent(EditorRecovery).props('error')).toBeNull()
      expect(calendar.suggestCalendarTimes).toHaveBeenCalledTimes(1)
      expect(calendar.getCalendarProposal).toHaveBeenCalledTimes(4)
      expect(calendar.updateCalendarProposal).not.toHaveBeenCalled()
      expect(calendar.submitCalendarProposal).not.toHaveBeenCalled()
    },
  )

  it.each([
    { operation: 'save', boundary: 'route-change' },
    { operation: 'save', boundary: 'unmount' },
    { operation: 'suggest', boundary: 'route-change' },
    { operation: 'suggest', boundary: 'unmount' },
  ])('ignores a late $operation problem after $boundary', async ({ operation, boundary }) => {
    const proposalId = ref(PROPOSAL_ID)
    let editor: ReturnType<typeof useCalendarProposalEditor> | undefined
    // 保留真实 hook 的响应式输出，卸载后仍可检查迟到异常没有写回状态。
    const host = mount(
      defineComponent({
        setup() {
          editor = useCalendarProposalEditor(proposalId, async () => undefined)
          return () => null
        },
      }),
    )
    wrappers.push(host)
    await flushPromises()
    if (!editor) throw new Error('Synthetic editor did not mount')
    const activeEditor = editor
    let rejectRead: (cause: ProblemError) => void = () => undefined
    vi.mocked(calendar.getCalendarProposal).mockReturnValueOnce(
      new Promise((_resolve, reject) => {
        rejectRead = reject
      }),
    )
    vi.mocked(calendar.suggestCalendarTimes).mockResolvedValueOnce({
      proposal_id: PROPOSAL_ID,
      version: 2,
      completeness: 'complete',
      missing_connections: [],
      attendee_availability_checked: false,
      candidates: [],
    })
    const pending =
      operation === 'save' ? activeEditor.save() : activeEditor.suggest()
    await flushPromises()
    expect(calendar.getCalendarProposal).toHaveBeenCalledTimes(2)
    expect(activeEditor.busy.value).toBe(true)

    if (boundary === 'unmount') {
      host.unmount()
      wrappers = wrappers.filter((wrapper) => wrapper !== host)
    } else {
      current = {
        ...current,
        id: '00000000-0000-0000-0000-000000000399',
        version: 1,
      }
      proposalId.value = current.id
      await flushPromises()
      expect(activeEditor.locked.value).toBe(false)
    }
    rejectRead(readProblem())
    await pending
    await flushPromises()
    expect(activeEditor.error.value).toBeNull()
    if (boundary === 'unmount') {
      expect(activeEditor.proposal.value).toBeNull()
      expect(activeEditor.form.description).toBe('')
    } else {
      expect(activeEditor.proposal.value?.id).toBe(
        '00000000-0000-0000-0000-000000000399',
      )
      expect(activeEditor.proposal.value?.version).toBe(1)
      expect(activeEditor.locked.value).toBe(false)
    }
    expect(calendar.getCalendarProposal).toHaveBeenCalledTimes(
      boundary === 'unmount' ? 2 : 3,
    )
    expect(calendar.updateCalendarProposal).toHaveBeenCalledTimes(
      operation === 'save' ? 1 : 0,
    )
    expect(calendar.suggestCalendarTimes).toHaveBeenCalledTimes(
      operation === 'suggest' ? 1 : 0,
    )
    expect(calendar.submitCalendarProposal).not.toHaveBeenCalled()
  })

  it('saves all-day exclusive dates and explicit notification policy without UTC shifting', async () => {
    const { wrapper } = await renderPage()
    await wrapper.get('[aria-label="全天日程"]').setValue(true)
    await wrapper.get('[aria-label="开始日期"]').setValue('2030-01-05')
    await wrapper.get('[aria-label="结束日期（不含）"]').setValue('2030-01-06')
    await wrapper.get('[aria-label="参会人"]').setValue('attendee@example.test')
    const policy = new DOMWrapper(
      getByRole(wrapper.element as HTMLElement, 'combobox', {
        name: '通知策略',
      }),
    )
    await policy.trigger('click')
    await policy.trigger('keydown', { key: 'Home', code: 'Home' })
    await policy.trigger('keydown', { key: 'ArrowDown', code: 'ArrowDown' })
    await policy.trigger('keydown', { key: 'Enter', code: 'Enter' })
    await byButton(wrapper, '保存提案').trigger('click')
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
    await wrapper.get('[aria-label="日程标题"]').setValue('Precision retained')
    await byButton(wrapper, '保存提案').trigger('click')
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
    await byButton(wrapper, '提交审批').trigger('click')
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
    expect(byButton(wrapper, '提交审批').attributes('disabled')).toBeDefined()
  })
})
