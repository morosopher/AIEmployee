import { fireEvent, waitFor, within } from '@testing-library/vue'
import { defineComponent, h, reactive, ref } from 'vue'
import { RouterView } from 'vue-router'
import ConfirmDialog from 'primevue/confirmdialog'
import { useConfirm } from 'primevue/useconfirm'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { renderWithPlugins } from '@/test-support/renderWithPlugins'
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
/** 真实 RouterView 保留路由参数／卸载边界，ConfirmDialog 与应用共用相同服务出口。 */
async function render(query = '') {
  let confirmation: ReturnType<typeof useConfirm> | undefined
  const view = await renderWithPlugins(
    defineComponent({
      setup() {
        confirmation = useConfirm()
        return () => h('div', [h(RouterView), h(ConfirmDialog)])
      },
    }),
  )
  if (!confirmation) throw new Error('Synthetic confirmation service missing')
  view.router.addRoute({
    path: '/calendar/proposals/:proposalId',
    component: CalendarProposalPage,
  })
  for (const name of ['actions', 'tasks', 'connections', 'brief'])
    view.router.addRoute({
      path: `/${name}`,
      component: { render: () => h('p', '目标页面') },
    })
  await view.router.push(`/calendar/proposals/${PROPOSAL_ID}${query}`)
  // 父快照可见后异步Form已开始加载；等待真实模块，避免把加载耗时塞进字段的默认查询窗口。
  await view.findByText(/^版本 \d+ ·/)
  await vi.dynamicImportSettled()
  await view.findByLabelText('日程标题')
  return { ...view, confirmation, tasks: useTasksStore(view.pinia) }
}

/** 只通过真实用户确认按钮继续；未确认之前不得读新版本或创建本地提案。 */
async function acceptPreparation(view: Awaited<ReturnType<typeof render>>) {
  const dialog = await view.findByRole('alertdialog', { name: '准备新版本' })
  await fireEvent.click(
    within(dialog).getByRole('button', { name: '确认准备' }),
  )
  await waitFor(() =>
    expect(view.queryByRole('alertdialog')).not.toBeInTheDocument(),
  )
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
    const view = await render()
    await fireEvent.click(view.getByRole('button', { name: '提交审批' }))
    await waitFor(() =>
      expect(
        view.queryByText('正在加载提案与日历目录…'),
      ).not.toBeInTheDocument(),
    )
    expect(view.baseElement.textContent).toContain('创建新版本')
    expect(view.getByLabelText('日程标题')).toBeDisabled()
    expect(calendar.createCalendarProposal).not.toHaveBeenCalled()
    await fireEvent.click(view.getByRole('button', { name: '创建新版本' }))
    await acceptPreparation(view)
    await waitFor(() =>
      expect(
        view.queryByText('正在加载提案与日历目录…'),
      ).not.toBeInTheDocument(),
    )
    expect(calendar.getCalendarProposal).toHaveBeenCalledWith(PROPOSAL_ID)
    await waitFor(() =>
      expect(calendar.createCalendarProposal).toHaveBeenCalledWith(
        {
          operation_kind: 'update',
          initialization: 'shell',
          event_id: EVENT_ID,
        },
        expect.objectContaining({ key: expect.any(String) }),
      ),
    )
    const readOrders = vi.mocked(calendar.getCalendarProposal).mock
      .invocationCallOrder
    expect(readOrders[1]).toBeLessThan(
      vi.mocked(calendar.createCalendarProposal).mock.invocationCallOrder[0] ??
        0,
    )
    await waitFor(() =>
      expect(view.router.currentRoute.value.path).toBe(
        `/calendar/proposals/${NEW_ID}`,
      ),
    )
    expect(view.getByLabelText('日程标题')).toBeEnabled()
    expect(view.getByRole('button', { name: '提交审批' })).toBeDisabled()
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
    const view = await render('?recovery=new_version')
    const button = view.getByRole('button', { name: '准备新版本' })
    await fireEvent.click(button)
    await acceptPreparation(view)
    await waitFor(() =>
      expect(
        view.queryByText('正在加载提案与日历目录…'),
      ).not.toBeInTheDocument(),
    )
    await fireEvent.click(button)
    expect(view.queryByRole('alertdialog')).not.toBeInTheDocument()
    await waitFor(() =>
      expect(calendar.createCalendarProposal).toHaveBeenCalledTimes(1),
    )
    expect(button).toBeDisabled()
    reject(new TypeError('Synthetic transport interruption'))
    await waitFor(() => expect(button).toBeEnabled())
    await fireEvent.click(button)
    await acceptPreparation(view)
    await waitFor(() =>
      expect(
        view.queryByText('正在加载提案与日历目录…'),
      ).not.toBeInTheDocument(),
    )
    await waitFor(() =>
      expect(calendar.createCalendarProposal).toHaveBeenCalledTimes(2),
    )
    expect(vi.mocked(calendar.createCalendarProposal).mock.calls[1]?.[1]).toBe(
      vi.mocked(calendar.createCalendarProposal).mock.calls[0]?.[1],
    )
    expect(calendar.submitCalendarProposal).not.toHaveBeenCalled()
  })

  it('waits for the existing account sync task, then explicitly reloads before preparing', async () => {
    current = updateProposal('stale', true)
    const view = await render()
    const panel = () => view.getByRole('region', { name: '重新准备修改提案' })
    expect(panel().textContent).toContain(connection().account_email)
    expect(panel().textContent).toContain('Google')
    expect(view.getByRole('button', { name: '准备新版本' })).toBeDisabled()
    await fireEvent.click(view.getByRole('button', { name: '同步来源账户' }))
    await waitFor(() =>
      expect(
        view.queryByText('正在加载提案与日历目录…'),
      ).not.toBeInTheDocument(),
    )
    expect(connections.syncConnection).toHaveBeenCalledWith(connection().id)
    expect(panel().textContent).toContain('同步进行中')
    expect(
      within(panel()).getByRole('link', { name: '查看同步任务' }),
    ).toHaveAttribute('href', `/tasks?task_id=${TASK_ID}`)
    expect(calendar.createCalendarProposal).not.toHaveBeenCalled()
    current = updateProposal('stale', false)
    view.tasks.setTask({
      id: TASK_ID,
      kind: 'sync_calendar',
      status: 'succeeded',
      retry_of_task_id: null,
      error_code: null,
      event_cursor: '3',
      steps: [],
    })
    await waitFor(() =>
      expect(
        view.queryByText('正在加载提案与日历目录…'),
      ).not.toBeInTheDocument(),
    )
    expect(panel().textContent).toContain('同步已完成')
    expect(calendar.createCalendarProposal).not.toHaveBeenCalled()
    await fireEvent.click(view.getByRole('button', { name: '重新读取提案' }))
    await waitFor(() =>
      expect(
        view.queryByText('正在加载提案与日历目录…'),
      ).not.toBeInTheDocument(),
    )
    expect(view.getByRole('button', { name: '准备新版本' })).toBeEnabled()
    await fireEvent.click(view.getByRole('button', { name: '准备新版本' }))
    await acceptPreparation(view)
    await waitFor(() =>
      expect(
        view.queryByText('正在加载提案与日历目录…'),
      ).not.toBeInTheDocument(),
    )
    await waitFor(() =>
      expect(calendar.createCalendarProposal).toHaveBeenCalledTimes(1),
    )
    expect(calendar.submitCalendarProposal).not.toHaveBeenCalled()
  })

  it.each(['source-lost', 'became-executing', 'requires-sync', 'read-failed'])(
    'does not prepare when the fresh read reports %s',
    async (change) => {
      const view = await render()
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
      await fireEvent.click(view.getByRole('button', { name: '准备新版本' }))
      await acceptPreparation(view)
      await waitFor(() =>
        expect(
          view.queryByText('正在加载提案与日历目录…'),
        ).not.toBeInTheDocument(),
      )
      expect(calendar.createCalendarProposal).not.toHaveBeenCalled()
      expect(calendar.submitCalendarProposal).not.toHaveBeenCalled()
      expect(view.getByRole('button', { name: '提交审批' })).toBeDisabled()
      if (change === 'source-lost') {
        const sourceStatus = within(
          view.getByRole('region', { name: '重新准备修改提案' }),
        ).getByRole('status')
        expect(sourceStatus).toHaveTextContent(
          /原来源无法核实，请\s*重新选择来源\s*。/,
        )
        expect(sourceStatus).toHaveAttribute('aria-live', 'polite')
        expect(view.baseElement.textContent).toContain('重新选择来源')
        expect(
          view.getByRole('link', { name: '重新选择来源' }),
        ).toHaveAttribute('href', '/brief')
        expect(
          view.getByRole('link', { name: '检查连接、同步或重新授权' }),
        ).toHaveAttribute('href', '/connections')
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
    const view = await render()
    await fireEvent.click(view.getByRole('button', { name: '准备新版本' }))
    await acceptPreparation(view)
    await waitFor(() =>
      expect(
        view.queryByText('正在加载提案与日历目录…'),
      ).not.toBeInTheDocument(),
    )
    const another = '00000000-0000-0000-0000-000000000597'
    current = { ...updateProposal(), id: another }
    await view.router.push(`/calendar/proposals/${another}`)
    await waitFor(() =>
      expect(
        view.queryByText('正在加载提案与日历目录…'),
      ).not.toBeInTheDocument(),
    )
    complete(prepared)
    await waitFor(() =>
      expect(
        view.queryByText('正在加载提案与日历目录…'),
      ).not.toBeInTheDocument(),
    )
    expect(view.router.currentRoute.value.path).toBe(
      `/calendar/proposals/${another}`,
    )
    await waitFor(() =>
      expect(calendar.createCalendarProposal).toHaveBeenCalledTimes(1),
    )
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
      const view = await render('?recovery=new_version')
      expect(
        view.queryByRole('button', { name: '准备新版本' }),
      ).not.toBeInTheDocument()
      expect(calendar.createCalendarProposal).not.toHaveBeenCalled()
    },
  )

  it.each(['?recovery=send', '?recovery=new_version&recovery=send'])(
    'ignores a non-enum recovery query %s',
    async (query) => {
      current = updateProposal('editing')
      const view = await render(query)
      expect(
        view.queryByRole('region', { name: '重新准备修改提案' }),
      ).not.toBeInTheDocument()
      expect(calendar.createCalendarProposal).not.toHaveBeenCalled()
    },
  )

  it('shows an editing recovery link as a separate explicit preparation step', async () => {
    current = updateProposal('editing')
    const view = await render('?recovery=new_version')
    expect(
      view.queryByRole('button', { name: '准备新版本' }),
    ).toBeInTheDocument()
    expect(calendar.createCalendarProposal).not.toHaveBeenCalled()
  })
  it.each(['取消', 'Escape'])(
    'keeps cancellation %s read-only and accepts just one later explicit decision',
    async (choice) => {
      current.description = 'Synthetic private description'
      const view = await render()
      const trigger = view.getByRole('button', { name: '准备新版本' })
      trigger.focus()
      await fireEvent.click(trigger)
      const dialog = await view.findByRole('alertdialog', {
        name: '准备新版本',
      })
      expect(dialog).toHaveTextContent('原提案和审批保留不变')
      expect(dialog).not.toHaveTextContent('Synthetic private description')
      expect(calendar.getCalendarProposal).toHaveBeenCalledTimes(1)
      expect(calendar.createCalendarProposal).not.toHaveBeenCalled()
      // 即便测试直接派发第二个 DOM 事件，等待中的页面回调也必须保持互斥。
      await fireEvent.click(trigger)
      expect(view.getAllByRole('alertdialog')).toHaveLength(1)
      expect(calendar.createCalendarProposal).not.toHaveBeenCalled()
      if (choice === 'Escape') {
        await waitFor(() =>
          expect(
            within(dialog).getByRole('button', { name: '取消' }),
          ).toHaveFocus(),
        )
        await fireEvent.keyDown(document, { key: 'Escape', code: 'Escape' })
      } else
        await fireEvent.click(
          within(dialog).getByRole('button', { name: '取消' }),
        )
      await waitFor(() =>
        expect(view.queryByRole('alertdialog')).not.toBeInTheDocument(),
      )
      expect(calendar.createCalendarProposal).not.toHaveBeenCalled()
      expect(calendar.getCalendarProposal).toHaveBeenCalledTimes(1)
      await fireEvent.click(trigger)
      await acceptPreparation(view)
      await waitFor(() =>
        expect(calendar.createCalendarProposal).toHaveBeenCalledTimes(1),
      )
    },
  )

  it.each(['route', 'query', 'unmount', 'version', 'source', 'status'])(
    'discards a pending decision after %s changes',
    async (change) => {
      // 可控响应投影允许模拟等待期间权威对象改变，不调用私有页面方法或复制 hook。
      current = reactive(updateProposal())
      const view = await render()
      const requests = vi.spyOn(view.confirmation, 'require')
      await fireEvent.click(view.getByRole('button', { name: '准备新版本' }))
      await view.findByRole('alertdialog', { name: '准备新版本' })
      const staleAccept = requests.mock.calls[0]?.[0].accept
      if (!staleAccept)
        throw new Error('Synthetic confirmation callback missing')
      if (change === 'route')
        await view.router.push(`/calendar/proposals/${NEW_ID}`)
      else if (change === 'query')
        await view.router.push(
          `/calendar/proposals/${PROPOSAL_ID}?recovery=new_version`,
        )
      else if (change === 'unmount') await view.router.push('/actions')
      else if (change === 'version') current.version += 1
      else if (change === 'status') current.status = 'executing'
      else knownFacts(current).reprepare_source = null
      await waitFor(() =>
        expect(view.queryByRole('alertdialog')).not.toBeInTheDocument(),
      )
      // 直接调用页面交给公开服务的旧回调，避免点击已销毁的第三方 DOM。
      staleAccept()
      expect(calendar.createCalendarProposal).not.toHaveBeenCalled()
      expect(calendar.submitCalendarProposal).not.toHaveBeenCalled()
    },
  )

  it('ignores an earlier accept and hide callback while a later confirmation is open', async () => {
    const view = await render()
    const requests = vi.spyOn(view.confirmation, 'require')
    const trigger = view.getByRole('button', { name: '准备新版本' })
    await fireEvent.click(trigger)
    const first = await view.findByRole('alertdialog', { name: '准备新版本' })
    const earlier = requests.mock.calls[0]?.[0]
    if (!earlier?.accept || !earlier.onHide)
      throw new Error('Synthetic confirmation callbacks missing')
    await fireEvent.click(within(first).getByRole('button', { name: '取消' }))
    await waitFor(() =>
      expect(view.queryByRole('alertdialog')).not.toBeInTheDocument(),
    )
    await fireEvent.click(trigger)
    await view.findByRole('alertdialog', { name: '准备新版本' })
    earlier.accept()
    earlier.onHide()
    expect(view.getAllByRole('alertdialog')).toHaveLength(1)
    expect(calendar.createCalendarProposal).not.toHaveBeenCalled()
    await acceptPreparation(view)
    await waitFor(() =>
      expect(calendar.createCalendarProposal).toHaveBeenCalledTimes(1),
    )
  })

  it('preserves each real synchronization status once and never adds an assertive duplicate', async () => {
    current = updateProposal('stale', true)
    const view = await render()
    const panel = within(view.getByRole('region', { name: '重新准备修改提案' }))
    const status = panel.getByRole('status')
    expect(status).toHaveTextContent(
      '本地日程版本尚未更新，请先同步来源账户，任务完成后重新读取提案。',
    )
    expect(status).toHaveAttribute('aria-live', 'polite')
    expect(panel.queryByRole('alert')).not.toBeInTheDocument()
    await fireEvent.click(panel.getByRole('button', { name: '同步来源账户' }))
    await waitFor(() => expect(panel.getAllByRole('status')).toHaveLength(2))
    expect(
      panel
        .getAllByRole('status')
        .filter((node) => node.textContent?.includes('同步进行中')),
    ).toHaveLength(1)
    view.tasks.setTask({
      id: TASK_ID,
      kind: 'sync_calendar',
      status: 'failed',
      retry_of_task_id: null,
      error_code: null,
      event_cursor: '2',
      steps: [],
    })
    expect(
      await panel.findByText('同步未完成，请检查任务结果和连接。', {
        exact: false,
      }),
    ).toBeVisible()
    expect(panel.queryByRole('alert')).not.toBeInTheDocument()
  })
})
