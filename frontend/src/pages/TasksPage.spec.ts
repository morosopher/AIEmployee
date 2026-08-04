import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick, reactive, ref } from 'vue'
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
vi.mock('@/composables/useTaskEvents', () => ({
  useTaskEvents: () => ref('connected'),
}))
const route = reactive({ query: { task_id: 'task-1' as string } })

vi.mock('vue-router', () => ({
  useRoute: () => route,
  useRouter: () => ({ replace: vi.fn() }),
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

describe('TasksPage', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    route.query.task_id = 'task-1'
    api.cancelTask.mockReset()
    api.getTask.mockReset()
    api.retryTask.mockReset()
  })

  it('does not let an initial REST snapshot overwrite a newer SSE projection', async () => {
    const response = deferredSnapshot()
    api.getTask.mockReturnValueOnce(response.promise)
    const wrapper = mount(TasksPage, { global: { plugins: [createPinia()] } })
    const store = useTasksStore()

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

    expect(wrapper.text()).toContain('当前状态：succeeded')
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
    const wrapper = mount(TasksPage, { global: { plugins: [createPinia()] } })

    route.query.task_id = 'task-2'
    await nextTick()
    await flushPromises()
    route.query.task_id = 'task-1'
    await nextTick()
    await flushPromises()
    expect(api.getTask).toHaveBeenCalledTimes(3)
    secondA.resolve({ ...queuedSnapshot, status: 'running' })
    await flushPromises()
    firstA.resolve(queuedSnapshot)
    await flushPromises()

    expect(wrapper.text()).toContain('当前状态：running')
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
    const wrapper = mount(TasksPage, { global: { plugins: [createPinia()] } })
    await flushPromises()

    await wrapper.get('aside button').trigger('click')
    await flushPromises()
    await wrapper.get('aside button').trigger('click')
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
      .mockRejectedValueOnce(
        new api.ProblemError({ status: 409 }),
      )
      .mockResolvedValueOnce({
        ...queuedSnapshot,
        id: 'replacement-task',
        status: 'queued',
        retry_of_task_id: 'task-1',
      })
    const wrapper = mount(TasksPage, { global: { plugins: [createPinia()] } })
    await flushPromises()

    await wrapper.get('aside button').trigger('click')
    await flushPromises()
    await wrapper.get('aside button').trigger('click')
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
    const wrapper = mount(TasksPage, { global: { plugins: [createPinia()] } })
    await flushPromises()

    await wrapper.get('aside button').trigger('click')
    await flushPromises()
    route.query.task_id = 'replacement-task'
    await nextTick()
    await flushPromises()
    expect(wrapper.text()).toContain('此任务重试自：task-1')

    route.query.task_id = 'manual-task'
    await nextTick()
    await flushPromises()

    expect(wrapper.text()).toContain('此任务重试自：manual-origin')
    wrapper.unmount()
  })
})
