import { flushPromises } from '@vue/test-utils'
import { fireEvent } from '@testing-library/vue'
import { renderWithPlugins } from '@/test-support/renderWithPlugins'
import { useTaskEvents } from '@/composables/useTaskEvents'
import { useAuthStore } from '@/stores/auth'
import {
  listTaskHistory,
  type TaskHistoryPage,
  type TaskHistoryItem,
} from '@/api/taskHistory'
import { defineComponent, h, ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { TaskSnapshot } from '../api/types'
import { useTasksStore } from '../stores/tasks'

const api = vi.hoisted(() => ({
  ProblemError: class ProblemError extends Error {
    /** 构造模拟的服务端已响应错误，以便与页面的 instanceof 判断共享构造器。 */
    constructor(readonly problem: { status: number }) {
      super('Problem response')
    }
  },
  cancelTask: vi.fn(),
  getTask: vi.fn(),
  retryTask: vi.fn(),
}))

vi.mock('@/api/client', () => api)
vi.mock('@/api/taskHistory', async (original) => ({
  ...(await original<typeof import('@/api/taskHistory')>()),
  listTaskHistory: vi.fn(),
}))
vi.mock('@/composables/useTaskEvents', () => ({
  useTaskEvents: vi.fn(() => ref('connected')),
}))
import TasksPage from './TasksPage.vue'

const queuedSnapshot: TaskSnapshot = {
  id: 'task-1',
  kind: 'daily_brief',
  status: 'queued',
  retry_of_task_id: null,
  error_code: null,
  event_cursor: '0',
  steps: [],
}

/** 创建由测试精确控制完成时刻的 REST 响应。 */
function deferredSnapshot(): {
  promise: Promise<TaskSnapshot>
  resolve: (snapshot: TaskSnapshot) => void
} {
  let resolve!: (snapshot: TaskSnapshot) => void
  const promise = new Promise<TaskSnapshot>((resolvePromise) => {
    resolve = resolvePromise
  })
  return { promise, resolve }
}

/** 合成摘要只用于列表；与完整详情 fixture 分开，防止测试误写 Store。 */
function historyItem(
  overrides: Partial<TaskHistoryItem> = {},
): TaskHistoryItem {
  return {
    id: '10000000-0000-0000-0000-000000000001',
    kind: 'daily_brief',
    category: 'business',
    status: 'failed',
    created_at: '2026-10-01T00:00:00.000000Z',
    started_at: null,
    finished_at: null,
    error_code: null,
    retry_of_task_id: null,
    ...overrides,
  }
}
function historyPage(
  overrides: Partial<TaskHistoryPage> = {},
): TaskHistoryPage {
  return {
    items: [],
    next_cursor: null,
    previous_cursor: null,
    server_time: '2026-10-01T00:05:00.000000Z',
    filter_timezone: 'Asia/Shanghai',
    background_failed_count: 0,
    ...overrides,
  }
}
/** 真实内存路由与认证投影；先挂载再设置用户，覆盖 timezone 从 null 就绪的生命周期。 */
async function renderPage(route = '/tasks?task_id=task-1') {
  const view = await renderWithPlugins(TasksPage, { route })
  useAuthStore(view.pinia).user = {
    id: 'synthetic-user',
    email: 'admin@example.test',
    display_name: '测试管理员',
    timezone: 'Asia/Shanghai',
    locale: 'zh-CN',
    brief_time: '08:00',
  }
  await flushPromises()
  return view
}

describe('TasksPage', () => {
  beforeEach(() => {
    vi.mocked(listTaskHistory).mockReset().mockResolvedValue(historyPage())
    api.cancelTask.mockReset()
    api.getTask.mockReset()
    api.retryTask.mockReset()
  })

  it('opens only the authoritative successful restore result after a refreshed task snapshot', async () => {
    const proposalId = '00000000-0000-0000-0000-000000000502'
    api.getTask.mockResolvedValue({
      ...queuedSnapshot,
      kind: 'calendar.restore.prepare',
    })
    const wrapper = await renderPage()
    await flushPromises()
    expect(Boolean(wrapper.queryByRole('link', { name: '打开恢复提案' }))).toBe(
      false,
    )
    expect(wrapper.container.textContent).toContain(
      '准备完成后仍需核对并提交新的审批',
    )
    useTasksStore(wrapper.pinia).setTask({
      ...queuedSnapshot,
      kind: 'calendar.restore.prepare',
      status: 'succeeded',
      event_cursor: '12',
      calendar_restore_proposal_id: proposalId,
    })
    await flushPromises()
    expect(
      wrapper.getByRole('link', { name: '打开恢复提案' }).getAttribute('href'),
    ).toBe(`/calendar/proposals/${proposalId}`)
    wrapper.unmount()

    // 刷新后只依赖任务GET返回的持久结果，不在浏览器持久化提案内容或标识映射。
    api.getTask.mockResolvedValue({
      ...queuedSnapshot,
      kind: 'calendar.restore.prepare',
      status: 'succeeded',
      event_cursor: '12',
      calendar_restore_proposal_id: proposalId,
    })
    const refreshed = await renderPage()
    await flushPromises()
    expect(
      refreshed
        .getByRole('link', { name: '打开恢复提案' })
        .getAttribute('href'),
    ).toBe(`/calendar/proposals/${proposalId}`)
    refreshed.unmount()
  })

  it('does not let an initial REST snapshot overwrite a newer SSE projection', async () => {
    const response = deferredSnapshot()
    api.getTask.mockReturnValueOnce(response.promise)
    const wrapper = await renderPage()
    const store = useTasksStore(wrapper.pinia)

    await flushPromises()
    store.applyEvent({
      id: '3',
      task_id: 'task-1',
      sequence: '3',
      event: 'task.status_changed',
      occurred_at: '2026-08-03T00:00:03Z',
      step_id: null,
      payload: { status: 'succeeded' },
    })
    response.resolve(queuedSnapshot)
    await flushPromises()

    expect(wrapper.container.textContent).toContain('当前状态：succeeded')
    wrapper.unmount()
  })

  it('ignores an old task A response after navigating A to B to A', async () => {
    const firstA = deferredSnapshot()
    const taskB = deferredSnapshot()
    const secondA = deferredSnapshot()
    api.getTask
      .mockReturnValueOnce(firstA.promise)
      .mockReturnValueOnce(taskB.promise)
      .mockReturnValueOnce(secondA.promise)
    const wrapper = await renderPage()

    await wrapper.router.push({ query: { task_id: 'task-2' } })
    await flushPromises()
    await wrapper.router.push({ query: { task_id: 'task-1' } })
    await flushPromises()
    expect(api.getTask).toHaveBeenCalledTimes(3)
    secondA.resolve({ ...queuedSnapshot, status: 'running' })
    await flushPromises()
    firstA.resolve(queuedSnapshot)
    await flushPromises()

    expect(wrapper.container.textContent).toContain('当前状态：running')
    wrapper.unmount()
  })

  it('reuses a retry key after a timed-out retry request', async () => {
    api.getTask.mockResolvedValue({ ...queuedSnapshot, status: 'failed' })
    api.retryTask
      .mockRejectedValueOnce(new Error('timeout'))
      .mockResolvedValueOnce({
        ...queuedSnapshot,
        id: 'replacement-task',
        status: 'queued',
        retry_of_task_id: 'task-1',
      })
    const wrapper = await renderPage()
    await flushPromises()

    await fireEvent.click(wrapper.getByRole('button', { name: '重试任务' }))
    await flushPromises()
    await fireEvent.click(wrapper.getByRole('button', { name: '重试任务' }))
    await flushPromises()

    expect(api.retryTask).toHaveBeenCalledTimes(2)
    expect(api.retryTask.mock.calls[0]?.[1]).toBe(
      api.retryTask.mock.calls[1]?.[1],
    )
    wrapper.unmount()
  })

  it('starts a new retry intent after the server explicitly rejects one', async () => {
    api.getTask.mockResolvedValue({ ...queuedSnapshot, status: 'failed' })
    api.retryTask
      .mockRejectedValueOnce(new api.ProblemError({ status: 409 }))
      .mockResolvedValueOnce({
        ...queuedSnapshot,
        id: 'replacement-task',
        status: 'queued',
        retry_of_task_id: 'task-1',
      })
    const wrapper = await renderPage()
    await flushPromises()

    await fireEvent.click(wrapper.getByRole('button', { name: '重试任务' }))
    await flushPromises()
    await fireEvent.click(wrapper.getByRole('button', { name: '重试任务' }))
    await flushPromises()

    expect(api.retryTask).toHaveBeenCalledTimes(2)
    expect(api.retryTask.mock.calls[0]?.[1]).not.toBe(
      api.retryTask.mock.calls[1]?.[1],
    )
    wrapper.unmount()
  })

  it('clears the followed retry origin after navigating to another task', async () => {
    api.getTask
      .mockResolvedValueOnce({ ...queuedSnapshot, status: 'failed' })
      .mockResolvedValueOnce({
        ...queuedSnapshot,
        id: 'replacement-task',
        retry_of_task_id: 'task-1',
      })
      .mockResolvedValueOnce({
        ...queuedSnapshot,
        id: 'manual-task',
        retry_of_task_id: 'manual-origin',
      })
    api.retryTask.mockResolvedValueOnce({
      ...queuedSnapshot,
      id: 'replacement-task',
      retry_of_task_id: 'task-1',
    })
    const wrapper = await renderPage()
    await flushPromises()

    await fireEvent.click(wrapper.getByRole('button', { name: '重试任务' }))
    await flushPromises()
    await wrapper.router.push({ query: { task_id: 'replacement-task' } })
    await flushPromises()
    expect(wrapper.container.textContent).toContain('此任务重试自：task-1')

    await wrapper.router.push({ query: { task_id: 'manual-task' } })
    await flushPromises()

    expect(wrapper.container.textContent).toContain(
      '此任务重试自：manual-origin',
    )
    wrapper.unmount()
  })
})

/** 页面接线回归使用真实 useTaskHistory/Router，仅替换网络端口。 */
describe('TasksPage history integration', () => {
  beforeEach(() => {
    vi.mocked(listTaskHistory)
      .mockReset()
      .mockResolvedValue(historyPage({ items: [historyItem()] }))
    api.getTask
      .mockReset()
      .mockResolvedValue({ ...queuedSnapshot, status: 'failed' })
    api.cancelTask.mockReset()
    api.retryTask.mockReset()
  })

  it('waits for the authenticated timezone and keeps filtered rows when selecting a complete task ID', async () => {
    vi.mocked(useTaskEvents).mockClear()
    const view = await renderWithPlugins(TasksPage, {
      route: '/tasks?scope=all&status=failed',
    })
    expect(listTaskHistory).not.toHaveBeenCalled()
    useAuthStore(view.pinia).user = {
      id: 'synthetic-user',
      email: 'admin@example.test',
      display_name: '测试',
      timezone: 'Asia/Shanghai',
      locale: 'zh-CN',
      brief_time: '08:00',
    }
    const item = historyItem()
    const snapshot = { ...queuedSnapshot, id: item.id, status: 'failed', event_cursor: '17' }
    api.getTask.mockResolvedValueOnce(snapshot)
    await flushPromises()
    expect(useTasksStore(view.pinia).tasks).toEqual({})
    await fireEvent.click(
      await view.findByRole('link', { name: `查看任务 ${item.id}` }),
    )
    await flushPromises()
    expect(view.router.currentRoute.value.query).toMatchObject({
      scope: 'all',
      status: 'failed',
      task_id: item.id,
    })
    expect(listTaskHistory).toHaveBeenCalledTimes(1)
    expect(useTasksStore(view.pinia).tasks[item.id]).toEqual(snapshot)
    expect(useTaskEvents).toHaveBeenCalledTimes(1)
    expect(view.getByRole('list', { name: '任务历史' })).toBeVisible()
  })

  it('catches rejected list navigation with a fixed safe message', async () => {
    const view = await renderPage('/tasks')
    vi.spyOn(view.router, 'push').mockRejectedValueOnce(
      new Error('private-cursor-navigation'),
    )
    await fireEvent.click(view.getByRole('button', { name: '全部任务' }))
    await flushPromises()
    expect(view.getByRole('alert')).toHaveTextContent(
      '任务历史导航未完成，请重试。',
    )
    expect(view.container.textContent).not.toContain(
      'private-cursor-navigation',
    )
  })

  it('follows retry replacement even while the readonly refresh remains pending and preserves filters', async () => {
    const view = await renderPage(
      '/tasks?task_id=task-1&scope=all&status=failed',
    )
    vi.mocked(listTaskHistory).mockReturnValueOnce(new Promise(() => {}))
    api.retryTask.mockResolvedValueOnce({
      ...queuedSnapshot,
      id: 'replacement-task',
      retry_of_task_id: 'task-1',
    })
    await fireEvent.click(view.getByRole('button', { name: '重试任务' }))
    await flushPromises()
    expect(view.router.currentRoute.value.query).toMatchObject({
      task_id: 'replacement-task',
      scope: 'all',
      status: 'failed',
    })
    expect(listTaskHistory).toHaveBeenCalledTimes(2)
  })

  it('refreshes readonly rows after a successful cancel without changing snapshot guards', async () => {
    api.getTask.mockResolvedValueOnce(queuedSnapshot)
    api.cancelTask.mockResolvedValueOnce({
      ...queuedSnapshot,
      status: 'cancelled',
    })
    const view = await renderPage()
    await fireEvent.click(view.getByRole('button', { name: '取消任务' }))
    await flushPromises()
    expect(api.cancelTask).toHaveBeenCalledWith('task-1')
    expect(listTaskHistory).toHaveBeenCalledTimes(2)
    expect(
      view.queryByRole('button', { name: '取消任务' }),
    ).not.toBeInTheDocument()
  })

  it('shows unavailable record for 404 and retains the list with a return focus action', async () => {
    api.getTask.mockRejectedValueOnce(new api.ProblemError({ status: 404 }))
    const view = await renderPage()
    expect(view.getByRole('alert')).toHaveTextContent('任务记录已不可用')
    await fireEvent.click(view.getByRole('button', { name: '返回任务列表' }))
    await flushPromises()
    expect(view.router.currentRoute.value.query.task_id).toBeUndefined()
    expect(view.getByRole('heading', { name: '任务历史' })).toHaveFocus()
  })
})

it('restores pagination focus to the list heading when the selected direction reaches its boundary', async () => {
  vi.mocked(listTaskHistory).mockReset().mockResolvedValueOnce(historyPage({ items: [historyItem()], next_cursor: 'opaque-next' }))
  let finish: ((value: TaskHistoryPage) => void) | undefined
  vi.mocked(listTaskHistory).mockImplementationOnce(() => new Promise<TaskHistoryPage>((resolve) => { finish = resolve }))
  const view = await renderPage('/tasks')
  const next = view.getByRole('button', { name: '下一页' })
  next.focus()
  await fireEvent.click(next)
  await flushPromises()
  // 浏览器在 focused 按钮转 disabled 时会丢失焦点；jsdom 不模拟，显式触发同一 DOM 边界。
  next.blur()
  finish?.(historyPage({ items: [], previous_cursor: 'opaque-previous' }))
  await flushPromises()
  expect(view.getByRole('heading', { name: '任务历史' })).toHaveFocus()
})

it('retains exact legacy status wording beside the new status tag', async () => {
  vi.mocked(listTaskHistory).mockResolvedValue(historyPage())
  api.getTask.mockResolvedValue(queuedSnapshot)
  const view = await renderPage()
  expect(view.getByText('当前状态：queued', { exact: true })).toBeVisible()
})

/** 明确冻结迁移后的运行时播报；Message 内置 role 不得使旧提示重复。 */
it('preserves legacy snapshot, connection and empty-timeline live regions exactly once', async () => {
  vi.mocked(listTaskHistory).mockReset().mockResolvedValue(historyPage())
  const response = deferredSnapshot()
  api.getTask.mockReset().mockReturnValueOnce(response.promise)
  const view = await renderPage()
  const statuses = view.getAllByRole('status').map((node) => node.textContent?.trim())
  expect(statuses).toEqual([
    '实时连接：connected', '正在恢复任务快照…', '请选择一个任务查看执行时间线。',
  ])
  response.resolve(queuedSnapshot)
  await flushPromises()
  expect(view.getAllByRole('status')).toHaveLength(1)
  api.cancelTask.mockRejectedValueOnce(new Error('unsafe synthetic transport details'))
  await fireEvent.click(view.getByRole('button', { name: '取消任务' }))
  await flushPromises()
  expect(view.getByRole('alert')).toHaveTextContent('取消请求未完成，请稍后重试。')
  expect(view.container.textContent).not.toContain('unsafe synthetic transport details')
})

it('keeps the restore caution and unavailable-result warning as separate live regions', async () => {
  vi.mocked(listTaskHistory).mockResolvedValue(historyPage())
  api.getTask.mockResolvedValue({ ...queuedSnapshot, kind: 'calendar.restore.prepare', status: 'succeeded' })
  const view = await renderPage()
  expect(view.getAllByRole('status').map((node) => node.textContent?.trim())).toEqual([
    '实时连接：connected', '准备完成后仍需核对并提交新的审批，日程尚未因此恢复。',
  ])
  expect(view.getByRole('alert')).toHaveTextContent('恢复提案结果已不可用，请返回操作中心核对原修改和保留期。')
  expect(view.queryByRole('link', { name: '打开恢复提案' })).not.toBeInTheDocument()
})

it('keeps a cached task unavailable after 404 across re-entry failures until a successful GET confirms it', async () => {
  const item = historyItem()
  const cached = { ...queuedSnapshot, id: item.id, status: 'failed' as const }
  vi.mocked(listTaskHistory).mockReset().mockResolvedValue(historyPage({ items: [item] }))
  let rejectRead: ((cause: unknown) => void) | undefined
  api.getTask.mockReset().mockImplementationOnce(() => new Promise<TaskSnapshot>((_resolve, reject) => { rejectRead = reject }))
  const view = await renderPage(`/tasks?task_id=${item.id}`)
  useTasksStore(view.pinia).setTask(cached)
  rejectRead?.(new api.ProblemError({ status: 404 }))
  await flushPromises()
  expect(view.getByRole('alert')).toHaveTextContent('任务记录已不可用')
  expect(view.queryByRole('button', { name: '重试任务' })).not.toBeInTheDocument()
  await fireEvent.click(view.getByRole('button', { name: '返回任务列表' }))
  await flushPromises()
  api.getTask.mockRejectedValueOnce(new Error('synthetic offline'))
  await fireEvent.click(view.getByRole('link', { name: `查看任务 ${item.id}` }))
  await flushPromises()
  expect(view.queryByRole('button', { name: '重试任务' })).not.toBeInTheDocument()
  expect(useTasksStore(view.pinia).tasks[item.id]).toEqual(cached)
  await fireEvent.click(view.getByRole('button', { name: '返回任务列表' }))
  await flushPromises()
  api.getTask.mockResolvedValueOnce({ ...cached, status: 'queued' })
  await fireEvent.click(view.getByRole('link', { name: `查看任务 ${item.id}` }))
  await flushPromises()
  expect(view.getByText('当前状态：queued', { exact: true })).toBeVisible()
  expect(view.getByRole('button', { name: '取消任务' })).toBeVisible()
})

/** 路由离开会销毁页面，但不会销毁Pinia；新页面不能把旧投影当作新的存在性证据。 */
it('does not revive a previously unavailable cached task after remount with the same Pinia', async () => {
  const item = historyItem()
  const cached = { ...queuedSnapshot, id: item.id, status: 'failed' as const }
  vi.mocked(listTaskHistory).mockReset().mockResolvedValue(historyPage({ items: [item] }))
  api.getTask.mockReset().mockRejectedValueOnce(new api.ProblemError({ status: 404 }))
  const mounted = ref(true)
  const Host = defineComponent({ setup: () => () => mounted.value ? h(TasksPage) : null })
  const view = await renderWithPlugins(Host, { route: `/tasks?task_id=${item.id}` })
  useAuthStore(view.pinia).user = { id: 'synthetic-user', email: 'admin@example.test', display_name: '测试', timezone: 'Asia/Shanghai', locale: 'zh-CN', brief_time: '08:00' }
  useTasksStore(view.pinia).setTask(cached)
  await flushPromises()
  expect(view.getByRole('alert')).toHaveTextContent('任务记录已不可用')
  mounted.value = false
  await flushPromises()
  api.getTask.mockRejectedValueOnce(new Error('synthetic offline'))
  mounted.value = true
  await flushPromises()
  expect(view.queryByRole('button', { name: '重试任务' })).not.toBeInTheDocument()
  expect(view.queryByText('当前状态：failed', { exact: true })).not.toBeInTheDocument()
  expect(useTasksStore(view.pinia).tasks[item.id]).toEqual(cached)
})
