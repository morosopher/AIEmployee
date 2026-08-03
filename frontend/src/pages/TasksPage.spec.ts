import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { TaskSnapshot } from '../api/types'
import { useTasksStore } from '../stores/tasks'

const api = vi.hoisted(() => ({
  cancelTask: vi.fn(),
  getTask: vi.fn(),
  retryTask: vi.fn(),
}))

vi.mock('@/api/client', () => api)
vi.mock('@/composables/useTaskEvents', () => ({
  useTaskEvents: () => ref('connected'),
}))
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: { task_id: 'task-1' } }),
  useRouter: () => ({ replace: vi.fn() }),
}))

import TasksPage from './TasksPage.vue'

const queuedSnapshot: TaskSnapshot = {
  id: 'task-1',
  kind: 'daily_brief',
  status: 'queued',
  retry_of_task_id: null,
  error_code: null,
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
    vi.clearAllMocks()
  })

  it('does not let an initial REST snapshot overwrite a newer SSE projection', async () => {
    const response = deferredSnapshot()
    api.getTask.mockReturnValueOnce(response.promise)
    const wrapper = mount(TasksPage, { global: { plugins: [createPinia()] } })
    const store = useTasksStore()

    await flushPromises()
    store.applyEvent({
      id: 3,
      task_id: 'task-1',
      sequence: 3,
      event: 'task.status_changed',
      occurred_at: '2026-08-03T00:00:03Z',
      step_id: null,
      payload: { status: 'succeeded' },
    })
    response.resolve(queuedSnapshot)
    await flushPromises()

    expect(wrapper.text()).toContain('当前状态：succeeded')
  })
})
