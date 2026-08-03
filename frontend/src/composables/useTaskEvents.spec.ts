import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import { useTaskEvents } from './useTaskEvents'

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
})
