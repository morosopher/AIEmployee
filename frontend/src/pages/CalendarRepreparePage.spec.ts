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
import { useTasksStore } from '@/stores/tasks'
import {
  calendarProposal,
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
  createCalendarProposal: vi.fn(),
  updateCalendarProposal: vi.fn(),
  submitCalendarProposal: vi.fn(),
}))
vi.mock('@/api/connections', () => ({
  listConnections: vi.fn(),
  getConnectionCapabilities: vi.fn(),
  syncConnection: vi.fn(),
}))
vi.mock('@/composables/useTaskEvents', () => ({
  useTaskEvents: () => ref('connected'),
}))
const EVENT_ID = '00000000-0000-0000-0000-000000000501'
const NEW_ID = '00000000-0000-0000-0000-000000000599'
let current: CalendarProposal, prepared: CalendarProposal
let wrappers: ReturnType<typeof mount>[] = []

/** 后端已证明的原 update，供应商 ID 与本地事件 UUID 刻意不同。 */
function updateProposal(
  status: CalendarProposal['status'] = 'stale',
  requiresSync = false,
): CalendarProposal {
  return {
    ...calendarProposal(),
    ...calendarFields(),
    calendar_id: 'synthetic-google-calendar',
    operation_kind: 'update',
    status,
    target_event_id: 'synthetic-provider-event',
    base_etag: 'synthetic-old-etag',
    before_snapshot_id: '00000000-0000-0000-0000-000000000502',
    notification_policy: 'none',
    required_confirmations: [],
    changed_fields: ['location'],
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

/** 显式收窄合成 GET 的非空事实，不用类型断言隐藏 fixture 缺失。 */
function knownFacts(value: CalendarProposal) {
  if (!value.editor_facts) throw new Error('Synthetic editor facts missing')
  return value.editor_facts
}

beforeEach(() => {
  vi.clearAllMocks()
  current = updateProposal()
  prepared = {
    ...updateProposal('editing'),
    id: NEW_ID,
    base_etag: 'synthetic-new-etag',
    before_snapshot_id: '00000000-0000-0000-0000-000000000598',
    changed_fields: [],
    required_confirmations: [
      'calendar',
      'time',
      'attendees',
      'notification_policy',
    ],
  }
  vi.mocked(calendar.getCalendarProposal).mockImplementation(async (id) =>
    id === NEW_ID ? prepared : current,
  )
  vi.mocked(calendar.createCalendarProposal).mockResolvedValue({
    ...prepared,
    editor_facts: null,
  })
  vi.mocked(connections.listConnections).mockResolvedValue([connection()])
  vi.mocked(connections.getConnectionCapabilities).mockResolvedValue(
    connectionCapabilities(),
  )
  vi.mocked(connections.syncConnection).mockResolvedValue({
    gmail_task_id: NEW_ID,
    calendar_task_id: TASK_ID,
  })
})
afterEach(() => {
  wrappers.forEach((wrapper) => wrapper.unmount())
  wrappers = []
})

/** 同一编辑器组件随真实路由参数变化；不用独立 DOM 壳掩盖晚到响应导航。 */
async function render(query = '') {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      {
        path: '/calendar/proposals/:proposalId',
        component: CalendarProposalPage,
      },
      ...['actions', 'tasks', 'connections', 'brief'].map((name) => ({
        path: `/${name}`,
        component: { template: '<p>目标页面</p>' },
      })),
    ],
  })
  await router.push(`/calendar/proposals/${PROPOSAL_ID}${query}`)
  const pinia = createPinia()
  const wrapper = mount(CalendarProposalPage, {
    global: { plugins: [pinia, router] },
  })
  wrappers.push(wrapper)
  await flushPromises()
  return { wrapper, router, tasks: useTasksStore(pinia) }
}

describe('calendar reprepare recovery', () => {
  it('handles the production version conflict, locks the old proposal and creates only after a fresh read and explicit click', async () => {
    current = updateProposal('editing')
    vi.mocked(calendar.submitCalendarProposal).mockImplementation(async () => {
      current = updateProposal('stale')
      throw new ProblemError({
        type: 'about:blank',
        title: 'Conflict',
        detail: '',
        status: 409,
        instance: '',
        error_code: 'calendar_event_version_conflict',
        trace_id: 'synthetic-conflict-trace',
      })
    })
    const { wrapper, router } = await render()
    await wrapper.get('button[name="submit-proposal"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('创建新版本')
    expect(
      wrapper.get('input[aria-label="日程标题"]').attributes('disabled'),
    ).toBeDefined()
    expect(calendar.createCalendarProposal).not.toHaveBeenCalled()
    await wrapper.get('button[name="recover-new-version"]').trigger('click')
    await flushPromises()
    expect(calendar.getCalendarProposal).toHaveBeenCalledWith(PROPOSAL_ID)
    expect(calendar.createCalendarProposal).toHaveBeenCalledWith(
      {
        operation_kind: 'update',
        initialization: 'shell',
        event_id: EVENT_ID,
      },
      expect.objectContaining({ key: expect.any(String) }),
    )
    const readOrders = vi.mocked(calendar.getCalendarProposal).mock
      .invocationCallOrder
    expect(readOrders[1]).toBeLessThan(
      vi.mocked(calendar.createCalendarProposal).mock.invocationCallOrder[0] ??
        0,
    )
    expect(router.currentRoute.value.path).toBe(`/calendar/proposals/${NEW_ID}`)
    expect(
      wrapper.get('input[aria-label="日程标题"]').attributes('disabled'),
    ).toBeUndefined()
    expect(
      wrapper.get('button[name="submit-proposal"]').attributes('disabled'),
    ).toBeDefined()
    expect(calendar.submitCalendarProposal).toHaveBeenCalledTimes(1)
    expect(calendar.updateCalendarProposal).not.toHaveBeenCalled()
    expect(current.status).toBe('stale')
  })

  it('keeps a stable creation intent after an unknown response and blocks duplicate clicks', async () => {
    let reject: (cause: Error) => void = () => undefined
    vi.mocked(calendar.createCalendarProposal).mockReturnValueOnce(
      new Promise((_resolve, failure) => {
        reject = failure
      }),
    )
    const { wrapper } = await render('?recovery=new_version')
    const button = wrapper.get('button[name="prepare-version"]')
    await button.trigger('click')
    await flushPromises()
    await button.trigger('click')
    expect(calendar.createCalendarProposal).toHaveBeenCalledTimes(1)
    expect(button.attributes('disabled')).toBeDefined()
    reject(new TypeError('Synthetic transport interruption'))
    await flushPromises()
    await button.trigger('click')
    await flushPromises()
    expect(calendar.createCalendarProposal).toHaveBeenCalledTimes(2)
    expect(vi.mocked(calendar.createCalendarProposal).mock.calls[1]?.[1]).toBe(
      vi.mocked(calendar.createCalendarProposal).mock.calls[0]?.[1],
    )
    expect(calendar.submitCalendarProposal).not.toHaveBeenCalled()
  })

  it('waits for the existing account sync task, then explicitly reloads before preparing', async () => {
    current = updateProposal('stale', true)
    const { wrapper, tasks } = await render()
    const panel = () => wrapper.get('[aria-label="重新准备修改提案"]')
    expect(panel().text()).toContain(connection().account_email)
    expect(panel().text()).toContain('Google')
    expect(
      wrapper.get('button[name="prepare-version"]').attributes('disabled'),
    ).toBeDefined()
    await wrapper.get('button[name="sync-reprepare-source"]').trigger('click')
    await flushPromises()
    expect(connections.syncConnection).toHaveBeenCalledWith(connection().id)
    expect(panel().text()).toContain('同步进行中')
    expect(panel().find(`a[href="/tasks?task_id=${TASK_ID}"]`).exists()).toBe(
      true,
    )
    expect(calendar.createCalendarProposal).not.toHaveBeenCalled()
    current = updateProposal('stale', false)
    tasks.setTask({
      id: TASK_ID,
      kind: 'sync_calendar',
      status: 'succeeded',
      retry_of_task_id: null,
      error_code: null,
      event_cursor: '3',
      steps: [],
    })
    await flushPromises()
    expect(panel().text()).toContain('同步已完成')
    expect(calendar.createCalendarProposal).not.toHaveBeenCalled()
    await wrapper.get('button[name="refresh-reprepare"]').trigger('click')
    await flushPromises()
    expect(
      wrapper.get('button[name="prepare-version"]').attributes('disabled'),
    ).toBeUndefined()
    await wrapper.get('button[name="prepare-version"]').trigger('click')
    await flushPromises()
    expect(calendar.createCalendarProposal).toHaveBeenCalledTimes(1)
    expect(calendar.submitCalendarProposal).not.toHaveBeenCalled()
  })

  it.each(['source-lost', 'became-executing', 'requires-sync', 'read-failed'])(
    'does not prepare when the fresh read reports %s',
    async (change) => {
      const { wrapper } = await render()
      if (change === 'read-failed')
        vi.mocked(calendar.getCalendarProposal).mockRejectedValueOnce(
          new Error('Synthetic read failure'),
        )
      else if (change === 'became-executing')
        current = {
          ...current,
          status: 'executing',
          editor_facts: { ...knownFacts(current), reprepare_source: null },
        }
      else
        current = {
          ...current,
          editor_facts: {
            ...knownFacts(current),
            reprepare_source:
              change === 'source-lost'
                ? null
                : { event_id: EVENT_ID, requires_sync: true },
          },
        }
      await wrapper.get('button[name="prepare-version"]').trigger('click')
      await flushPromises()
      expect(calendar.createCalendarProposal).not.toHaveBeenCalled()
      expect(calendar.submitCalendarProposal).not.toHaveBeenCalled()
      expect(
        wrapper.get('button[name="submit-proposal"]').attributes('disabled'),
      ).toBeDefined()
      if (change === 'source-lost') {
        expect(wrapper.text()).toContain('重新选择来源')
        expect(wrapper.find('a[href="/brief"]').exists()).toBe(true)
        expect(wrapper.find('a[href="/connections"]').exists()).toBe(true)
      }
    },
  )

  it('ignores a late creation response after changing the proposal route', async () => {
    let complete: (value: CalendarProposal) => void = () => undefined
    vi.mocked(calendar.createCalendarProposal).mockReturnValueOnce(
      new Promise((resolve) => {
        complete = resolve
      }),
    )
    const { wrapper, router } = await render()
    await wrapper.get('button[name="prepare-version"]').trigger('click')
    await flushPromises()
    const another = '00000000-0000-0000-0000-000000000597'
    current = { ...updateProposal(), id: another }
    await router.push(`/calendar/proposals/${another}`)
    await flushPromises()
    complete(prepared)
    await flushPromises()
    expect(router.currentRoute.value.path).toBe(
      `/calendar/proposals/${another}`,
    )
    expect(calendar.createCalendarProposal).toHaveBeenCalledTimes(1)
  })

  it.each([
    'awaiting_approval',
    'executing',
    'needs_attention',
    'applied',
  ] as const)(
    'never exposes a reprepare action for %s even from a recovery link',
    async (status) => {
      current = {
        ...updateProposal(status),
        editor_facts: {
          ...knownFacts(updateProposal()),
          reprepare_source: null,
        },
      }
      const { wrapper } = await render('?recovery=new_version')
      expect(wrapper.find('button[name="prepare-version"]').exists()).toBe(
        false,
      )
      expect(calendar.createCalendarProposal).not.toHaveBeenCalled()
    },
  )

  it.each(['?recovery=send', '?recovery=new_version&recovery=send'])(
    'ignores a non-enum recovery query %s',
    async (query) => {
      current = updateProposal('editing')
      const { wrapper } = await render(query)
      expect(wrapper.find('[aria-label="重新准备修改提案"]').exists()).toBe(
        false,
      )
      expect(calendar.createCalendarProposal).not.toHaveBeenCalled()
    },
  )

  it('shows an editing recovery link as a separate explicit preparation step', async () => {
    current = updateProposal('editing')
    const { wrapper } = await render('?recovery=new_version')
    expect(wrapper.find('button[name="prepare-version"]').exists()).toBe(true)
    expect(calendar.createCalendarProposal).not.toHaveBeenCalled()
  })
})
