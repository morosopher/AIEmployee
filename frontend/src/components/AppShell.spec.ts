import { useConfirm } from 'primevue/useconfirm'
import { defineComponent, h, nextTick, ref } from 'vue'
import { useTasksStore } from '@/stores/tasks'
import { fireEvent, waitFor, within } from '@testing-library/vue'
import {
  renderWithPlugins,
  setViewport,
} from '@/test-support/renderWithPlugins'
import { describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'

import AppShell from './AppShell.vue'
import NeedsAttentionPanel from './NeedsAttentionPanel.vue'
import ActionConfirmationDialog from './ActionConfirmationDialog.vue'
import { actionSnapshot } from '@/test-support/actionFixtures'
import { getSystemAlerts } from '@/api/system'

vi.mock('@/api/system', () => ({
  getSystemAlerts: vi.fn().mockResolvedValue({
    alerts: [
      {
        code: 'daily_brief_overdue',
        severity: 'critical',
        local_date: '2026-08-04',
        diagnostic_task_id: 'diag-1',
      },
    ],
  }),
}))

describe('AppShell', () => {
  it('shows alert loading state before the first snapshot arrives', async () => {
    vi.mocked(getSystemAlerts).mockImplementationOnce(
      () => new Promise(() => undefined),
    )
    const wrapper = await renderWithPlugins(AppShell)
    expect(
      wrapper
        .getAllByRole('status')
        .some((node) => node.textContent?.includes('正在检查系统告警')),
    ).toBe(true)
  })

  it('shows a persistent overdue alert and diagnostic task link', async () => {
    const wrapper = await renderWithPlugins(AppShell)
    await flushPromises()
    expect(wrapper.getByRole('alert')).toHaveTextContent('每日简报已逾期')
    expect(
      within(wrapper.getByRole('alert')).getByRole('link', {
        name: '查看诊断任务',
      }),
    ).toHaveAttribute('href', '/tasks?task_id=diag-1')
  })

  it('clears the banner when a later alert snapshot is empty', async () => {
    vi.useFakeTimers()
    vi.mocked(getSystemAlerts)
      .mockResolvedValueOnce({
        alerts: [
          {
            code: 'daily_brief_overdue',
            severity: 'critical',
            local_date: '2026-08-04',
            diagnostic_task_id: 'diag-1',
          },
        ],
      })
      .mockResolvedValueOnce({ alerts: [] })
    const wrapper = await renderWithPlugins(AppShell)
    await flushPromises()
    expect(wrapper.getByRole('alert')).toHaveTextContent('每日简报已逾期')
    await vi.advanceTimersByTimeAsync(60_000)
    expect(wrapper.queryByRole('alert')).toBeNull()
    wrapper.unmount()
    vi.useRealTimers()
  })

  it('does not let an older empty alert response overwrite a newer critical alert', async () => {
    vi.useFakeTimers()
    let resolveOlder: ((value: { alerts: [] }) => void) | undefined
    let resolveNewer:
      | ((value: {
          alerts: [
            {
              code: 'daily_brief_overdue'
              severity: 'critical'
              local_date: string
              diagnostic_task_id: string
            },
          ]
        }) => void)
      | undefined
    const older = new Promise<{ alerts: [] }>((resolve) => {
      resolveOlder = resolve
    })
    const newer = new Promise<{
      alerts: [
        {
          code: 'daily_brief_overdue'
          severity: 'critical'
          local_date: string
          diagnostic_task_id: string
        },
      ]
    }>((resolve) => {
      resolveNewer = resolve
    })
    vi.mocked(getSystemAlerts)
      .mockReset()
      .mockImplementationOnce(() => older)
      .mockImplementationOnce(() => newer)
    const wrapper = await renderWithPlugins(AppShell)
    await vi.advanceTimersByTimeAsync(60_000)
    resolveNewer?.({
      alerts: [
        {
          code: 'daily_brief_overdue',
          severity: 'critical',
          local_date: '2026-08-04',
          diagnostic_task_id: 'diag-1',
        },
      ],
    })
    await flushPromises()
    resolveOlder?.({ alerts: [] })
    await flushPromises()
    expect(wrapper.getByRole('alert')).toHaveTextContent('每日简报已逾期')
    wrapper.unmount()
    vi.useRealTimers()
  })
})

/** 全局展示只读取任务连接投影，不建立第二条 SSE。 */
describe('AppShell responsive feedback', () => {
  it.each(
    [600, 1000, 1400].flatMap((width) =>
      ['/actions', '/tasks'].map((route) => ({ width, route })),
    ),
  )(
    'hides the global timeline on $route at $width pixels',
    async ({ width, route }) => {
      setViewport(width)
      vi.mocked(getSystemAlerts).mockResolvedValue({ alerts: [] })
      const view = await renderWithPlugins(AppShell, { route })
      expect(
        view.queryByRole('complementary', { name: '任务时间线' }),
      ).toBeNull()
      expect(view.queryByRole('button', { name: /任务时间线/ })).toBeNull()
    },
  )
  it('makes the application inert while a navigation drawer is open', async () => {
    setViewport(600)
    const view = await renderWithPlugins(AppShell)
    const trigger = view.getByRole('button', { name: '打开导航' })
    await fireEvent.click(trigger)
    const dialog = await view.findByRole('dialog', { name: '主导航' })
    await waitFor(() =>
      expect(dialog.contains(document.activeElement)).toBe(true),
    )
    expect(view.getByRole('main').closest('[inert]')).not.toBeNull()
    await fireEvent.keyDown(document, { key: 'Escape', code: 'Escape' })
    await waitFor(() =>
      expect(view.getByRole('main').closest('[inert]')).toBeNull(),
    )
  })
  it('shows disconnected state and announces recovery once without treating route disposal as recovery', async () => {
    const view = await renderWithPlugins(AppShell)
    const tasks = useTasksStore(view.pinia)
    tasks.setConnectionState('task-1', 'reconnecting')
    await view.findByText('任务连接已断开，正在尝试恢复。')
    tasks.setConnectionState('task-1', 'connected')
    const recovery = await view.findByText('任务连接已恢复')
    expect(recovery.closest('[aria-live]')).toHaveAttribute(
      'aria-live',
      'polite',
    )
    tasks.setConnectionState('task-1', 'connected')
    await nextTick()
    expect(view.getAllByText('任务连接已恢复')).toHaveLength(1)
    tasks.setConnectionState('task-1', 'disconnected')
    await nextTick()
    expect(view.getAllByText('任务连接已恢复')).toHaveLength(1)
    expect(view.queryByText('任务连接已断开，正在尝试恢复。')).toBeNull()
  })
})

/** 确认出口运行真实 ConfirmDialog，背景隔离必须先于焦点返回解除。 */
it('isolates the background and restores focus for the global confirmation outlet', async () => {
  const ConfirmationTrigger = defineComponent({
    setup() {
      const confirm = useConfirm()
      return () =>
        h(
          'button',
          {
            onClick: () =>
              confirm.require({ header: '测试确认', message: '合成确认内容' }),
          },
          '打开确认',
        )
    },
  })
  const view = await renderWithPlugins(AppShell, {
    global: { stubs: { RouterView: ConfirmationTrigger } },
  })
  const trigger = view.getByRole('button', { name: '打开确认' })
  trigger.focus()
  await fireEvent.click(trigger)
  const dialog = await view.findByRole('alertdialog', { name: '测试确认' })
  await waitFor(() =>
    expect(dialog.contains(document.activeElement)).toBe(true),
  )
  expect(trigger.closest('[inert]')).not.toBeNull()
  await fireEvent.keyDown(dialog, { key: 'Escape', code: 'Escape' })
  await waitFor(() => expect(view.queryByRole('alertdialog')).toBeNull())
  expect(trigger.closest('[inert]')).toBeNull()
  await waitFor(() => expect(trigger).toHaveFocus())
})

/** 确认完成后触发器可能已禁用或移除，焦点应回到仍可访问的主内容。 */
it.each(['disabled', 'removed'] as const)(
  'returns confirmation focus to main content when the accepted trigger is %s',
  async (mode) => {
    const ConfirmationTrigger = defineComponent({
      setup() {
        const confirm = useConfirm()
        const accepted = ref(false)
        return () =>
          accepted.value && mode === 'removed'
            ? h('p', '操作已完成')
            : h('button', {
                type: 'button',
                disabled: accepted.value,
                onClick: () => confirm.require({
                  header: '测试确认',
                  message: '合成确认内容',
                  acceptProps: { label: '确认' },
                  accept: () => { accepted.value = true },
                }),
              }, '测试操作')
      },
    })
    const view = await renderWithPlugins(AppShell, {
      global: { stubs: { RouterView: ConfirmationTrigger } },
    })
    const trigger = view.getByRole('button', { name: '测试操作' })
    trigger.focus()
    await fireEvent.click(trigger)
    const dialog = await view.findByRole('alertdialog', { name: '测试确认' })
    await fireEvent.click(within(dialog).getByRole('button', { name: '确认' }))
    await waitFor(() => {
      expect(view.queryByRole('alertdialog')).not.toBeInTheDocument()
    })
    await waitFor(() => { expect(view.getByRole('main')).toHaveFocus() })
    expect(view.getByRole('main').closest('[inert]')).toBeNull()
  },
)

/** Toast 使用 Portal，不能只把页面根节点设为 inert 而遗漏外部关闭按钮。 */
it('also isolates the recovery toast while a modal drawer is open', async () => {
  setViewport(600)
  const view = await renderWithPlugins(AppShell)
  const tasks = useTasksStore(view.pinia)
  tasks.setConnectionState('task-1', 'reconnecting')
  await nextTick()
  tasks.setConnectionState('task-1', 'connected')
  const recovery = await view.findByText('任务连接已恢复')
  await fireEvent.click(view.getByRole('button', { name: '打开导航' }))
  await view.findByRole('dialog', { name: '主导航' })
  expect(recovery.closest('[inert]')).not.toBeNull()
})

/** 页面私有 Dialog 通过相同展示事件通知外壳；与路由配置和 Store 无关。 */
it('isolates the shell and restores focus for a routed page modal', async () => {
  const PageModal = defineComponent({
    emits: ['modal-change'],
    setup(_props, { emit }) {
      return () => h('div', [
        h('button', { onClick: () => emit('modal-change', true) }, '打开页面确认'),
        h('button', { onClick: () => emit('modal-change', false) }, '关闭页面确认'),
      ])
    },
  })
  const view = await renderWithPlugins(AppShell, { global: { stubs: { RouterView: PageModal } } })
  const trigger = view.getByRole('button', { name: '打开页面确认' })
  trigger.focus()
  await fireEvent.click(trigger)
  expect(trigger.closest('[inert]')).not.toBeNull()
  await fireEvent.click(view.getByRole('button', { name: '关闭页面确认' }))
  await waitFor(() => expect(trigger.closest('[inert]')).toBeNull())
  await waitFor(() => expect(trigger).toHaveFocus())
})

it('clears a page modal isolation when its route leaves before an after-hide event', async () => {
  const PageModal = defineComponent({
    emits: ['modal-change'],
    setup(_props, { emit }) { return () => h('button', { onClick: () => emit('modal-change', true) }, '页面确认') },
  })
  const view = await renderWithPlugins(AppShell, { route: '/settings', global: { stubs: { RouterView: PageModal } } })
  const trigger = view.getByRole('button', { name: '页面确认' })
  await fireEvent.click(trigger)
  expect(trigger.closest('[inert]')).not.toBeNull()
  await view.router.push('/actions')
  await waitFor(() => expect(view.getByRole('main').closest('[inert]')).toBeNull())
})

/** 真正的深层消费者不转发 modal-change；必须由 UI 注入端口直接通知外壳。 */
it.each(['cancel', 'terminal', 'unmount'] as const)(
  'isolates a deeply nested result dialog and releases it after %s',
  async (reason) => {
    const snapshot = ref(actionSnapshot())
    const mounted = ref(true)
    const DeepConsumer = defineComponent({
      setup() {
        return () => h('section', [h('div', [mounted.value ? h(NeedsAttentionPanel, {
          snapshot: snapshot.value,
          resolve: vi.fn().mockResolvedValue(undefined),
          reconcile: vi.fn().mockResolvedValue(undefined),
          reload: vi.fn(),
        }) : null])])
      },
    })
    const view = await renderWithPlugins(AppShell, { route: '/actions', global: { stubs: { RouterView: DeepConsumer } } })
    const trigger = view.getByRole('button', { name: '确认未执行' })
    trigger.focus()
    await fireEvent.click(trigger)
    const dialog = await view.findByRole('dialog', { name: '确认未执行' })
    await waitFor(() => expect(within(dialog).getByRole('button', { name: '取消' })).toHaveFocus())
    expect(trigger.closest('[inert]')).not.toBeNull()
    expect(dialog.closest('[inert]')).toBeNull()
    if (reason === 'cancel') await fireEvent.keyDown(dialog, { key: 'Escape', code: 'Escape' })
    else if (reason === 'terminal') snapshot.value = actionSnapshot({ status: 'failed' })
    else mounted.value = false
    await waitFor(() => expect(view.queryByRole('dialog')).toBeNull())
    await waitFor(() => expect(view.getByRole('main').closest('[inert]')).toBeNull())
    await waitFor(() => expect(reason === 'cancel' ? trigger : view.getByRole('main')).toHaveFocus())
  },
)

it('does not let a disposed dialog release the isolation owned by its replacement', async () => {
  const instance = ref(1)
  const open = ref(false)
  const Page = defineComponent({
    setup() {
      return () => h('section', [
        h('button', { onClick: () => { open.value = true } }, '人工核对'),
        open.value ? h(ActionConfirmationDialog, {
          key: instance.value, title: `合成确认 ${instance.value}`, busy: false,
          onCancel: () => { open.value = false },
        }) : null,
      ])
    },
  })
  const view = await renderWithPlugins(AppShell, { route: '/actions', global: { stubs: { RouterView: Page } } })
  const trigger = view.getByRole('button', { name: '人工核对' })
  trigger.focus()
  await fireEvent.click(trigger)
  const first = await view.findByRole('dialog', { name: '合成确认 1' })
  // 在取消的 nextTick 等待期替换实例；旧回调不能关闭替代模态或归还其隔离。
  within(first).getByRole('button', { name: '取消' }).click()
  instance.value = 2
  const replacement = await view.findByRole('dialog', { name: '合成确认 2' })
  await waitFor(() => expect(within(replacement).getByRole('button', { name: '取消' })).toHaveFocus())
  expect(trigger.closest('[inert]')).not.toBeNull()
  await fireEvent.keyDown(replacement, { key: 'Escape', code: 'Escape' })
  await waitFor(() => expect(view.queryByRole('dialog')).toBeNull())
  await waitFor(() => expect(view.getByRole('main').closest('[inert]')).toBeNull())
})
