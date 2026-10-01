import type { LocationQuery } from 'vue-router'
import { describe, expect, it } from 'vitest'
import { historyRouteQuery, parseHistoryRoute } from './historyRoute'

describe('历史路由公开参数', () => {
  it('解析默认条件并只保留既有公开 task_id', () => {
    const result = parseHistoryRoute({})
    expect(result.error).toBeNull()
    expect(result.filters.scope).toBe('business')
    expect(historyRouteQuery({ task_id: 'synthetic-id', body: 'synthetic-body' }, result.filters, null))
      .toEqual({ task_id: 'synthetic-id', scope: 'business' })
  })
  it.each<LocationQuery>([
    { scope: ['all'] }, { scope: null }, { scope: 'invalid' }, { status: 'invalid' },
    { kind: 'unknown' }, { created_from_date: '2026-02-30' },
    { created_from_date: '2026-10-02', created_to_date: '2026-10-01' },
    { cursor: '' }, { cursor: 'a'.repeat(2049) }, { created_to_date: '0000-01-01' },
  ])('拒绝非法输入且不静默查询默认值 %j', (query) => {
    expect(parseHistoryRoute(query).error?.kind).toBe('local')
  })
  it('往返精确日期、枚举和不透明游标', () => {
    const query = { scope: 'all', kind: 'other', status: 'failed', created_from_date: '2024-02-29', cursor: 'opaque' }
    const result = parseHistoryRoute(query)
    expect(result.error).toBeNull()
    expect(historyRouteQuery({}, result.filters, result.cursor)).toEqual(query)
  })
})
