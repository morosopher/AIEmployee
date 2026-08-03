import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import type { TaskSnapshot } from '../api/types'
import TaskTimeline from './TaskTimeline.vue'

const failedTask: TaskSnapshot = {
  id: 'failed-task',
  kind: 'daily_brief',
  status: 'failed',
  retry_of_task_id: null,
  error_code: 'provider_unavailable',
  steps: [],
}

describe('TaskTimeline', () => {
  it('follows the replacement returned by retry without mutating the failed task', async () => {
    const retry = vi.fn().mockResolvedValue({
      ...failedTask,
      id: 'replacement-task',
      status: 'queued',
      retry_of_task_id: 'failed-task',
    })
    const follow = vi.fn()
    const wrapper = mount(TaskTimeline, {
      props: { task: failedTask, retry, follow },
    })

    await wrapper.get('button').trigger('click')

    expect(retry).toHaveBeenCalledWith('failed-task')
    expect(follow).toHaveBeenCalledWith('replacement-task', 'failed-task')
    expect(failedTask.status).toBe('failed')
  })
})
