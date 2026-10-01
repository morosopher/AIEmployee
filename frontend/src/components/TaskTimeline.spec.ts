import { fireEvent } from '@testing-library/vue'
import { renderWithPlugins } from '@/test-support/renderWithPlugins'
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
  it('explains how to enable Google APIs for a confirmed project configuration error', async () => {
    const wrapper = await renderWithPlugins(TaskTimeline, {
      props: {
        task: {
          ...failedTask,
          kind: 'sync_mail',
          error_code: 'google_api_not_enabled',
        },
        retry: vi.fn(),
        follow: vi.fn(),
      },
    })

    expect(wrapper.container.textContent).toContain('Google Cloud')
    expect(wrapper.container.textContent).toContain('Gmail API')
    expect(wrapper.container.textContent).toContain('Google Calendar API')
    expect(wrapper.container.textContent).toContain('启用后重试同步')
  })

  it('does not suggest changing Google project configuration for unrelated failures', async () => {
    const wrapper = await renderWithPlugins(TaskTimeline, {
      props: { task: failedTask, retry: vi.fn(), follow: vi.fn() },
    })

    expect(wrapper.container.textContent).not.toContain('Google Cloud')
  })

  it('follows the replacement returned by retry without mutating the failed task', async () => {
    const retry = vi.fn().mockResolvedValue({
      ...failedTask,
      id: 'replacement-task',
      status: 'queued',
      retry_of_task_id: 'failed-task',
    })
    const follow = vi.fn()
    const wrapper = await renderWithPlugins(TaskTimeline, {
      props: { task: failedTask, retry, follow },
    })

    await fireEvent.click(wrapper.getByRole('button', { name: '重试任务' }))

    expect(retry).toHaveBeenCalledWith('failed-task')
    expect(follow).toHaveBeenCalledWith('replacement-task')
    expect(failedTask.status).toBe('failed')
  })

  it('explains that a transport failure has an unknown but safely replayable outcome', async () => {
    const wrapper = await renderWithPlugins(TaskTimeline, {
      props: {
        task: failedTask,
        retry: vi.fn().mockRejectedValue(new Error('timeout')),
        follow: vi.fn(),
      },
    })

    await fireEvent.click(wrapper.getByRole('button', { name: '重试任务' }))

    expect(wrapper.container.textContent).toContain('重试结果未知')
    expect(wrapper.container.textContent).toContain('安全重放')
  })

  it('distinguishes an explicit server rejection from an unknown transport outcome', async () => {
    const wrapper = await renderWithPlugins(TaskTimeline, {
      props: {
        task: failedTask,
        retry: vi.fn().mockRejectedValue(
          new ProblemError({
            type: 'about:blank',
            title: 'Conflict',
            status: 409,
            detail: 'Rejected',
            instance: '',
            error_code: 'task_state_conflict',
            trace_id: 'synthetic-trace',
          }),
        ),
        follow: vi.fn(),
      },
    })

    await fireEvent.click(wrapper.getByRole('button', { name: '重试任务' }))

    expect(wrapper.container.textContent).toContain('重试被服务器拒绝')
    expect(wrapper.container.textContent).not.toContain('重试结果未知')
  })
})

it('preserves every step text, literal summary and live errors with native timeline semantics', async () => {
  const view = await renderWithPlugins(TaskTimeline, {
    props: {
      task: {
        ...failedTask,
        steps: [
          {
            id: 'step-1',
            name: 'persist',
            sequence: 1,
            status: 'completed',
            started_at: '2026-10-01T00:00:00Z',
            finished_at: '2026-10-01T00:00:02Z',
            error_code: 'synthetic_step',
            output_summary: { note: '<script>synthetic()</script>' },
          },
        ],
      },
      retry: vi.fn(),
      follow: vi.fn(),
    },
  })
  expect(view.getByRole('list', { name: '任务步骤' })).toBeVisible()
  expect(view.getByText('persist')).toBeVisible()
  expect(view.getByText('状态：completed')).toBeVisible()
  expect(view.getByText('耗时 2 秒')).toBeVisible()
  expect(view.getByText('错误代码：synthetic_step')).toBeVisible()
  expect(view.getByRole('alert')).toHaveTextContent(
    '错误代码：provider_unavailable',
  )
  expect(view.container.querySelector('script')).toBeNull()
  expect(view.container.textContent).toContain('<script>synthetic()</script>')
  expect(view.getByText('失败')).toBeVisible()
})

it.each([
  'created',
  'queued',
  'running',
  'waiting_approval',
  'retry_scheduled',
  'reconciling',
  'needs_attention',
  'succeeded',
  'cancelled',
] as const)(
  'offers no retry for %s and preserves empty-step text',
  async (status) => {
    const view = await renderWithPlugins(TaskTimeline, {
      props: {
        task: { ...failedTask, status },
        retry: vi.fn(),
        follow: vi.fn(),
      },
    })
    expect(
      view.queryByRole('button', { name: '重试任务' }),
    ).not.toBeInTheDocument()
    expect(view.getByText('暂时没有可展示的步骤。')).toBeVisible()
  },
)
