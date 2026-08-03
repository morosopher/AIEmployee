import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import { ProblemError } from '../api/client'
import type { TaskSnapshot } from '../api/types'
import TaskTimeline from './TaskTimeline.vue'

const failedTask: TaskSnapshot = {
  id: 'failed-task',
  kind: 'daily_brief',
  status: 'failed',
  retry_of_task_id: null,
  error_code: 'provider_unavailable',
  event_cursor: '0',
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

  it('explains that a transport failure has an unknown but safely replayable outcome', async () => {
    const wrapper = mount(TaskTimeline, {
      props: { task: failedTask, retry: vi.fn().mockRejectedValue(new Error('timeout')), follow: vi.fn() },
    })

    await wrapper.get('button').trigger('click')

    expect(wrapper.text()).toContain('重试结果未知')
    expect(wrapper.text()).toContain('安全重放')
  })

  it('distinguishes an explicit server rejection from an unknown transport outcome', async () => {
    const wrapper = mount(TaskTimeline, {
      props: {
        task: failedTask,
        retry: vi.fn().mockRejectedValue(
          new ProblemError({
            type: 'about:blank', title: 'Conflict', status: 409, detail: 'Rejected',
            instance: '', error_code: 'task_state_conflict', trace_id: 'synthetic-trace',
          }),
        ),
        follow: vi.fn(),
      },
    })

    await wrapper.get('button').trigger('click')

    expect(wrapper.text()).toContain('重试被服务器拒绝')
    expect(wrapper.text()).not.toContain('重试结果未知')
  })
})
