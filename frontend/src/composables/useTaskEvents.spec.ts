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
    useTasksStore().latestSequences['task-1'] = 42
    const Host = defineComponent({
      setup() {
        useTaskEvents('task-1')
        return () => null
      },
    })

    const wrapper = mount(Host)

    expect(FakeEventSource.instances.at(-1)?.url).toBe(
      '/api/v1/tasks/task-1/events?last_event_id=42',
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
})
