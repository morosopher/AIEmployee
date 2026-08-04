import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, nextTick, ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import { useTaskEvents } from './useTaskEvents'
import { useTasksStore } from '@/stores/tasks'

/** 模拟浏览器 EventSource，验证 composable 的资源释放契约。 */
class FakeEventSource {
  static instances: FakeEventSource[] = []
  close = vi.fn()
  onerror: ((event: Event) => void) | null = null
  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent<string>) => void) | null = null
  addEventListener = vi.fn()

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this)
  }
}

describe('useTaskEvents', () => {
  it('opens the task stream and closes it when the owner unmounts', () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    setActivePinia(createPinia())
    const Host = defineComponent({
      setup() {
        useTaskEvents('task-1')
        return () => null
      },
    })

    const wrapper = mount(Host)
    const source = FakeEventSource.instances.at(-1)

    expect(source?.url).toBe('/api/v1/tasks/task-1/events')
    wrapper.unmount()
    expect(source?.close).toHaveBeenCalledOnce()
    vi.unstubAllGlobals()
  })

  it('supplies the known durable cursor when reopening a task stream', () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    setActivePinia(createPinia())
    useTasksStore().latestSequences['task-1'] = '9223372036854775807'
    const Host = defineComponent({
      setup() {
        useTaskEvents('task-1')
        return () => null
      },
    })

    const wrapper = mount(Host)

    expect(FakeEventSource.instances.at(-1)?.url).toBe(
      '/api/v1/tasks/task-1/events?last_event_id=9223372036854775807',
    )
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it('marks only the previously opened task disconnected when switching streams', async () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    setActivePinia(createPinia())
    const taskId = ref<string | null>('task-a')
    const Host = defineComponent({
      setup() {
        useTaskEvents(taskId)
        return () => null
      },
    })

    const wrapper = mount(Host)
    const firstSource = FakeEventSource.instances.at(-1)
    taskId.value = 'task-b'
    await nextTick()

    const store = useTasksStore()
    expect(firstSource?.close).toHaveBeenCalledOnce()
    expect(store.connections['task-a']).toBe('disconnected')
    expect(store.connections['task-b']).toBe('connecting')
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it('ignores an event callback retained by the closed stream after switching tasks', async () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    setActivePinia(createPinia())
    const taskId = ref<string | null>('task-a')
    const Host = defineComponent({
      setup() {
        useTaskEvents(taskId)
        return () => null
      },
    })

    const wrapper = mount(Host)
    const firstSource = FakeEventSource.instances.at(-1)
    const staleStatusHandler = firstSource?.addEventListener.mock.calls.find(
      ([eventName]) => eventName === 'task.status_changed',
    )?.[1] as EventListener
    taskId.value = 'task-b'
    await nextTick()

    staleStatusHandler(new MessageEvent('task.status_changed', {
      data: JSON.stringify({
        id: '1', task_id: 'task-a', sequence: '1', event: 'task.status_changed',
        occurred_at: '2026-08-03T00:00:00Z', step_id: null, payload: { status: 'running' },
      }),
    }))

    const store = useTasksStore()
    expect(store.connections['task-a']).toBe('disconnected')
    expect(store.connections['task-b']).toBe('connecting')
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it('reopens after the highest task event despite global audit ID gaps', () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    setActivePinia(createPinia())
    const Host = defineComponent({
      setup() {
        useTaskEvents('task-1')
        return () => null
      },
    })

    const firstWrapper = mount(Host)
    const firstSource = FakeEventSource.instances.at(-1)
    const statusHandler = firstSource?.addEventListener.mock.calls.find(
      ([eventName]) => eventName === 'task.status_changed',
    )?.[1] as EventListener
    statusHandler(
      new MessageEvent('task.status_changed', {
        data: JSON.stringify({
          id: '1',
          task_id: 'task-1',
          sequence: '1',
          event: 'task.status_changed',
          occurred_at: '2026-08-03T00:00:00Z',
          step_id: null,
          payload: { status: 'running' },
        }),
      }),
    )
    firstWrapper.unmount()

    const secondWrapper = mount(Host)
    const secondSource = FakeEventSource.instances.at(-1)
    const secondStatusHandler = secondSource?.addEventListener.mock.calls.find(
      ([eventName]) => eventName === 'task.status_changed',
    )?.[1] as EventListener
    secondStatusHandler(
      new MessageEvent('task.status_changed', {
        data: JSON.stringify({
          id: '3',
          task_id: 'task-1',
          sequence: '3',
          event: 'task.status_changed',
          occurred_at: '2026-08-03T00:00:00Z',
          step_id: null,
          payload: { status: 'queued' },
        }),
      }),
    )
    secondWrapper.unmount()

    const thirdWrapper = mount(Host)

    expect(FakeEventSource.instances.at(-1)?.url).toBe(
      '/api/v1/tasks/task-1/events?last_event_id=3',
    )
    thirdWrapper.unmount()
    vi.unstubAllGlobals()
  })
  it('reopens a silent stream after the heartbeat timeout', () => {
    vi.useFakeTimers()
    vi.stubGlobal('EventSource', FakeEventSource)
    setActivePinia(createPinia())
    const initialSourceCount = FakeEventSource.instances.length
    const Host = defineComponent({
      setup() {
        useTaskEvents('task-1')
        return () => null
      },
    })

    const wrapper = mount(Host)
    const firstSource = FakeEventSource.instances.at(-1)
    firstSource?.onopen?.(new Event('open'))

    vi.advanceTimersByTime(30_000)

    const store = useTasksStore()
    expect(firstSource?.close).toHaveBeenCalledOnce()
    expect(FakeEventSource.instances).toHaveLength(initialSourceCount + 2)
    expect(store.connections['task-1']).toBe('connecting')
    wrapper.unmount()
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })
})
