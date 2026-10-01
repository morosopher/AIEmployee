import { useConfirm } from 'primevue/useconfirm'
import { defineComponent, h, nextTick } from 'vue'
import { useTasksStore } from '@/stores/tasks'
import { fireEvent, waitFor, within } from '@testing-library/vue'
import {
  renderWithPlugins,
  setViewport,
} from '@/test-support/renderWithPlugins'
import { describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'

import AppShell from './AppShell.vue'
import { getSystemAlerts } from '@/api/system'

vi.mock('@/api/system', () => ({
  getSystemAlerts: vi
    .fn()
    .mockResolvedValue({
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
  it.each([600, 1000, 1400])(
    'hides the timeline on actions at %i pixels',
    async (width) => {
      setViewport(width)
      vi.mocked(getSystemAlerts).mockResolvedValue({ alerts: [] })
      const view = await renderWithPlugins(AppShell, { route: '/actions' })
      expect(
        view.queryByRole('complementary', { name: '执行时间线' }),
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
