import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import type { TaskEvent } from '../api/types'
import { useTasksStore } from './tasks'

/** 构造合成持久事件，避免测试依赖真实任务或时间。 */
function event(overrides: Partial<TaskEvent>): TaskEvent {
  return {
    id: 1,
    task_id: 'task-1',
    sequence: 1,
    event: 'step.started',
    occurred_at: '2026-08-03T00:00:00Z',
    step_id: 'step-1',
    payload: {},
    ...overrides,
  }
}

describe('tasks store', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('deduplicates events, orders durable steps, and follows status events', () => {
    const store = useTasksStore()

    store.applyEvent(
      event({
        id: 2,
        sequence: 2,
        step_id: 'step-2',
        payload: { name: '生成简报', status: 'completed' },
      }),
    )
    store.applyEvent(
      event({
        id: 1,
        sequence: 1,
        step_id: 'step-1',
        payload: { name: '同步邮件', status: 'started' },
      }),
    )
    store.applyEvent(
      event({
        id: 1,
        sequence: 1,
        step_id: 'step-1',
        payload: { name: '不应重复', status: 'failed' },
      }),
    )
    store.applyEvent(
      event({
        id: 3,
        sequence: 3,
        event: 'task.status_changed',
        step_id: null,
        payload: { status: 'running' },
      }),
    )

    expect(store.tasks['task-1']?.steps.map((step) => step.id)).toEqual([
      'step-1',
      'step-2',
    ])
    expect(store.tasks['task-1']?.status).toBe('running')
  })

  it('restores retry linkage and task error from a replay-gap snapshot', () => {
    const store = useTasksStore()
    store.setConnectionState('task-1', 'reconnecting')
    store.applyEvent(
      event({
        id: 7,
        sequence: 7,
        event: 'task.snapshot',
        step_id: null,
        payload: {
          id: 'task-1',
          kind: 'daily_brief',
          status: 'succeeded',
          retry_of_task_id: 'original-task',
          error_code: 'provider_temporarily_unavailable',
          event_cursor: 7,
          steps: [],
        },
      }),
    )

    expect(store.tasks['task-1']?.status).toBe('succeeded')
    expect(store.tasks['task-1']).toMatchObject({
      retry_of_task_id: 'original-task',
      error_code: 'provider_temporarily_unavailable',
    })
    expect(store.connections['task-1']).toBe('reconnecting')
  })

  it('ignores a stale snapshot after a newer durable event', () => {
    const store = useTasksStore()
    store.applyEvent(
      event({
        id: 8,
        sequence: 8,
        event: 'task.status_changed',
        step_id: null,
        payload: { status: 'succeeded' },
      }),
    )
    store.applyEvent(
      event({
        id: 7,
        sequence: 7,
        event: 'task.snapshot',
        step_id: null,
        payload: {
          id: 'task-1',
          kind: 'daily_brief',
          status: 'running',
          retry_of_task_id: null,
          error_code: null,
          event_cursor: 7,
          steps: [],
        },
      }),
    )

    expect(store.tasks['task-1']?.status).toBe('succeeded')
  })

  it('does not regress a succeeded task when a queued event arrives late', () => {
    const store = useTasksStore()

    store.applyEvent(
      event({
        id: 8,
        sequence: 8,
        event: 'task.status_changed',
        step_id: null,
        payload: { status: 'succeeded' },
      }),
    )
    store.applyEvent(
      event({
        id: 7,
        sequence: 7,
        event: 'task.status_changed',
        step_id: null,
        payload: { status: 'queued' },
      }),
    )

    expect(store.tasks['task-1']?.status).toBe('succeeded')
    expect(store.latestSequences['task-1']).toBe(8)
  })

  it('does not regress a completed step when a started event arrives late', () => {
    const store = useTasksStore()

    store.applyEvent(
      event({
        id: 8,
        sequence: 8,
        event: 'step.completed',
        step_id: 'step-1',
        payload: { name: '同步邮件', status: 'completed' },
      }),
    )
    store.applyEvent(
      event({
        id: 7,
        sequence: 7,
        event: 'step.started',
        step_id: 'step-1',
        payload: { name: '同步邮件', status: 'started' },
      }),
    )

    expect(store.tasks['task-1']?.steps[0]?.status).toBe('completed')
    expect(store.latestSequences['task-1']).toBe(8)
  })

  it('uses a REST snapshot cursor to reject older status and step replays', () => {
    const store = useTasksStore()

    store.setTask({
      id: 'task-1',
      kind: 'daily_brief',
      status: 'succeeded',
      retry_of_task_id: null,
      error_code: null,
      event_cursor: 8,
      steps: [
        {
          id: 'step-1',
          sequence: 1,
          name: '生成简报',
          status: 'completed',
          output_summary: null,
          error_code: null,
        },
      ],
    })
    store.applyEvent(
      event({
        id: 7,
        sequence: 7,
        event: 'task.status_changed',
        step_id: null,
        payload: { status: 'queued' },
      }),
    )
    store.applyEvent(
      event({
        id: 6,
        sequence: 6,
        event: 'step.started',
        payload: { name: '生成简报', status: 'started' },
      }),
    )

    expect(store.tasks['task-1']?.status).toBe('succeeded')
    expect(store.tasks['task-1']?.steps[0]?.status).toBe('completed')
    expect(store.latestSequences['task-1']).toBe(8)
    expect(store.snapshotCursors['task-1']).toBe(8)
  })

  it('preserves public step start and finish timestamps from a snapshot', () => {
    const store = useTasksStore()
    store.applyEvent(
      event({
        id: 7,
        sequence: 7,
        event: 'task.snapshot',
        step_id: null,
        payload: {
          id: 'task-1',
          kind: 'daily_brief',
          status: 'succeeded',
          retry_of_task_id: null,
          error_code: null,
          event_cursor: 7,
          steps: [
            {
              id: 'step-1',
              sequence: 1,
              name: '生成简报',
              status: 'completed',
              output_summary: null,
              error_code: null,
              started_at: '2026-08-03T00:00:00Z',
              finished_at: '2026-08-03T00:00:02Z',
            },
          ],
        },
      }),
    )

    expect(store.tasks['task-1']?.steps[0]).toMatchObject({
      started_at: '2026-08-03T00:00:00Z',
      finished_at: '2026-08-03T00:00:02Z',
    })
  })
})
