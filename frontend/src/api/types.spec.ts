import { describe, expect, it } from 'vitest'

import { asEventCursor, parseTaskEvent } from './types'

describe('task API types', () => {
  it.each(['0', '1', '9223372036854775807'])('accepts canonical cursor %s', (value) => {
    expect(asEventCursor(value)).toBe(value)
  })

  it.each(['', '+1', '-1', ' 1', '1 ', '01', '00'])('rejects noncanonical cursor %s', (value) => {
    expect(asEventCursor(value)).toBeNull()
  })

  it('rejects numeric cursors even when JavaScript can represent them exactly', () => {
    expect(asEventCursor(1)).toBeNull()
  })

  it('rejects an unsafe numeric SSE cursor before it reaches Pinia', () => {
    const event = parseTaskEvent(JSON.stringify({
      id: 9007199254740992, task_id: 'task-1', sequence: 9007199254740992,
      event: 'task.status_changed', occurred_at: '2026-08-03T00:00:00Z', step_id: null,
      payload: { status: 'running' },
    }))
    expect(event).toBeNull()
  })

  it('preserves a PostgreSQL BIGINT SSE cursor delivered as a decimal string', () => {
    const event = parseTaskEvent(JSON.stringify({
      id: '9223372036854775807', task_id: 'task-1', sequence: '9223372036854775807',
      event: 'task.status_changed', occurred_at: '2026-08-03T00:00:00Z', step_id: null,
      payload: { status: 'running' },
    }))
    expect(event).toMatchObject({ id: '9223372036854775807', sequence: '9223372036854775807' })
  })
})
