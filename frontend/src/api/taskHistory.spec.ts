import { afterEach, describe, expect, it, vi } from 'vitest'
import { ProblemError } from './client'
import { historyItemKey, listTaskHistory, parseTaskHistoryPage, type TaskHistoryFilters } from './taskHistory'
import { historyItem, historyPage } from '../test-support/taskHistoryFixtures'

const filters: TaskHistoryFilters = {
  scope: 'business', kind: null, status: null,
  created_from_date: null, created_to_date: null,
}

afterEach(() => vi.unstubAllGlobals())

describe('task history client', () => {
  it('reads isolated summaries with cookie GET and fixed limit', async () => {
    const payload = historyPage()
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(payload)))
    vi.stubGlobal('fetch', fetch)
    expect(await listTaskHistory(filters)).toEqual(payload)
    expect(fetch).toHaveBeenCalledWith('/api/v1/tasks?scope=business&limit=20', expect.objectContaining({ method: 'GET', credentials: 'include' }))
  })

  it('encodes all filters and preserves the opaque cursor', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(historyPage())))
    vi.stubGlobal('fetch', fetch)
    await listTaskHistory({ scope: 'all', kind: 'other', status: 'failed', created_from_date: '2026-09-01', created_to_date: '2026-10-01' }, 'synthetic+/=cursor')
    expect(fetch).toHaveBeenCalledWith('/api/v1/tasks?scope=all&limit=20&kind=other&status=failed&created_from_date=2026-09-01&created_to_date=2026-10-01&cursor=synthetic%2B%2F%3Dcursor', expect.any(Object))
  })

  it('preserves nullable fields and projects only public summary and page fields', () => {
    const payload = historyPage()
    const result = parseTaskHistoryPage({ ...payload, input_payload: { synthetic: true }, items: [{ ...historyItem(), steps: [], event_cursor: '99', input_payload: {}, result_payload: {}, lease_owner: 'synthetic', provider_identity: 'synthetic' }] })
    expect(result).toEqual(payload)
    expect(Object.keys(result.items[0] ?? {})).toHaveLength(9)
  })

  it('preserves populated nullable fields and full timestamp precision', () => {
    const item = historyItem({ started_at: '2026-10-01T01:01:00.000001Z', finished_at: '2026-10-01T01:02:00.999999Z', error_code: 'synthetic_failure', retry_of_task_id: 'ffffffff-ffff-ffff-ffff-ffffffffffff' })
    expect(parseTaskHistoryPage(historyPage({ items: [item] })).items).toEqual([item])
  })

  it.each(['daily_brief', 'conversation.respond', 'future.synthetic.kind'])('accepts kind %s without inventing its category', (kind) => {
    const item = historyItem({ kind, category: 'other' })
    expect(parseTaskHistoryPage(historyPage({ items: [item] })).items).toEqual([item])
  })

  it.each([
    ['id', 'invalid'], ['id', '00000000000000000000000000000001'],
    ['created_at', '2026-02-30T00:00:00.000000Z'], ['created_at', '2026-10-01T24:00:00.000000Z'],
    ['created_at', '2026-10-01T00:00:00.123Z'], ['created_at', '2026-10-01T00:00:00.123456+00:00'],
    ['created_at', '0000-01-01T00:00:00.000000Z'], ['created_at', null],
    ['started_at', 'invalid'], ['finished_at', 1], ['retry_of_task_id', 'invalid'],
    ['status', 'unknown'], ['category', 'unknown'], ['kind', null], ['error_code', 5],
    ['started_at', undefined], ['finished_at', undefined], ['error_code', undefined], ['retry_of_task_id', undefined],
  ])('rejects invalid item field %s (%s)', (field, value) => {
    expect(() => parseTaskHistoryPage(historyPage({ items: [{ ...historyItem(), [field]: value }] }))).toThrow('Invalid task history response')
  })

  it.each([
    ['items', {}], ['items', [null]], ['items', [[]]], ['background_failed_count', -1], ['background_failed_count', 0.5],
    ['background_failed_count', Number.MAX_SAFE_INTEGER + 1], ['background_failed_count', '1'],
    ['next_cursor', 'x'.repeat(2049)], ['next_cursor', ''], ['previous_cursor', 1], ['previous_cursor', undefined],
    ['server_time', '2026-02-29T00:00:00.000000Z'], ['filter_timezone', null], ['filter_timezone', ''],
  ])('rejects invalid page field %s (%s)', (field, value) => {
    expect(() => parseTaskHistoryPage({ ...historyPage(), [field]: value })).toThrow('Invalid task history response')
  })

  it.each([null, [], 'page'])('rejects non-object pages', (value) => {
    expect(() => parseTaskHistoryPage(value)).toThrow('Invalid task history response')
  })

  it('accepts empty pages, boundary-length opaque cursors and real leap dates', () => {
    const page = historyPage({ items: [], next_cursor: 'x'.repeat(2048), previous_cursor: 'synthetic', server_time: '2024-02-29T00:00:00.000001Z' })
    expect(parseTaskHistoryPage(page)).toEqual(page)
  })

  it('orders keys by all six microsecond digits then canonical UUID', () => {
    const older = historyItem({ created_at: '2026-10-01T01:00:00.123455Z', id: 'ffffffff-ffff-ffff-ffff-ffffffffffff' })
    const newer = historyItem()
    const tied = historyItem({ id: '00000000-0000-0000-0000-000000000002' })
    expect(historyItemKey(older) < historyItemKey(newer)).toBe(true)
    expect(historyItemKey(newer) < historyItemKey(tied)).toBe(true)
    expect(historyItemKey(newer)).toContain('.123456Z')
  })

  it('preserves unified Problem Details and trace identifiers', async () => {
    const problem = { type: 'about:blank', title: 'Invalid task history query', status: 422, detail: 'The task history query is invalid.', instance: '/api/v1/tasks', error_code: 'task_history_cursor_invalid', trace_id: 'synthetic-trace' }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(problem), { status: 422 })))
    const error: unknown = await listTaskHistory(filters).catch((value: unknown) => value)
    expect(error).toBeInstanceOf(ProblemError)
    expect(error).toMatchObject({ problem })
  })
})
