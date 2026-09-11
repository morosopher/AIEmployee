import { afterEach, describe, expect, it, vi } from 'vitest'
import { createRestoreProposal } from './calendar'
import { createRequestIntent } from './requestIntent'
import { parseTaskSnapshot } from './types'
import {
  calendarProposal,
  PROPOSAL_ID,
  TASK_ID,
} from '@/test-support/actionFixtures'

const EVENT_ID = '00000000-0000-0000-0000-000000000501'
const SNAPSHOT_ID = '00000000-0000-0000-0000-000000000502'
afterEach(() => vi.unstubAllGlobals())

/** 恢复准备沿用真实202合同；供应商事件和提案正文不能混入回执。 */
describe('calendar restore transport', () => {
  it('accepts the real queued-task receipt and retains the exact source and intent', async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          task_id: TASK_ID,
          status: 'queued',
        }),
        { status: 202 },
      ),
    )
    vi.stubGlobal('fetch', fetcher)
    const intent = createRequestIntent()
    await expect(
      createRestoreProposal(EVENT_ID, SNAPSHOT_ID, intent),
    ).resolves.toEqual({
      task_id: TASK_ID,
      status: 'queued',
    })
    expect(fetcher.mock.calls[0]?.[0]).toBe(
      `/api/v1/calendar/events/${EVENT_ID}/restore-proposal`,
    )
    const init = fetcher.mock.calls[0]?.[1] as RequestInit
    expect(init.method).toBe('POST')
    expect(JSON.parse(String(init.body))).toEqual({ snapshot_id: SNAPSHOT_ID })
    expect(new Headers(init.headers).get('Idempotency-Key')).toBe(intent.key)
    expect(init.cache).toBe('no-store')
  })

  it('rejects a synchronous proposal object at the asynchronous restore boundary', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(new Response(JSON.stringify(calendarProposal()))),
    )
    await expect(
      createRestoreProposal(EVENT_ID, SNAPSHOT_ID, createRequestIntent()),
    ).rejects.toThrow()
  })

  it('exposes only the typed successful restore task result', () => {
    expect(
      parseTaskSnapshot({
        id: TASK_ID,
        kind: 'calendar.restore.prepare',
        status: 'succeeded',
        error_code: null,
        retry_of_task_id: null,
        event_cursor: '12',
        steps: [],
        calendar_restore_proposal_id: PROPOSAL_ID,
      }),
    ).toHaveProperty('calendar_restore_proposal_id', PROPOSAL_ID)
  })

  it.each([
    { kind: 'daily_brief' },
    { status: 'running' },
    { calendar_restore_proposal_id: 'provider-event-id' },
    { calendar_restore_proposal_id: `${PROPOSAL_ID}?unsafe=1` },
  ])('rejects an unbound or invalid restore task result (%#)', (invalid) => {
    expect(() =>
      parseTaskSnapshot({
        id: TASK_ID,
        kind: 'calendar.restore.prepare',
        status: 'succeeded',
        error_code: null,
        retry_of_task_id: null,
        event_cursor: '12',
        steps: [],
        calendar_restore_proposal_id: PROPOSAL_ID,
        ...invalid,
      }),
    ).toThrow('Invalid calendar restore task result')
  })
})
