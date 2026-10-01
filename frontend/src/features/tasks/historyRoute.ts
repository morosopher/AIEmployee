import type { LocationQuery, LocationQueryRaw } from 'vue-router'
import type { TaskHistoryFilters } from '@/api/taskHistory'
import { asTaskStatus } from '@/api/types'

/** 本地格式错误不冒充服务端 Problem Details，固定文案不包含 URL 原值。 */
export interface HistoryLocalError {
  kind: 'local'
  message: string
}

const keys = ['scope', 'kind', 'status', 'created_from_date', 'created_to_date', 'cursor'] as const
const kinds = new Set(['daily_brief', 'conversation.respond', 'mail_draft.generate', 'trusted_action',
  'calendar.restore.prepare', 'sync_mail', 'sync_gmail', 'sync_calendar', 'brief.overdue_diagnostic',
  'privacy.clear_source_cache', 'privacy.delete_all_data', 'other'])

/** 校验真实民用日期；仅验证格式，不用本机时区推导业务范围。 */
function validDate(value: string | null): boolean {
  if (value === null) return true
  if (!/^(?!0000)\d{4}-\d{2}-\d{2}$/.test(value)) return false
  const date = new Date(`${value}T00:00:00.000Z`)
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value
}

/**
 * 严格解析公开筛选；非法输入返回本地错误，调用方不得在错误时查询默认条件。
 * @param query 路由原始参数；历史字段拒绝数组、null 与空值。
 * @returns filters/cursor 与明确错误；日期边界和范围互斥最终由服务端裁决。
 */
export function parseHistoryRoute(query: LocationQuery): {
  filters: TaskHistoryFilters
  cursor: string | null
  error: HistoryLocalError | null
} {
  const filters: TaskHistoryFilters = { scope: 'business', kind: null, status: null, created_from_date: null, created_to_date: null }
  const invalid = () => ({ filters, cursor: null, error: { kind: 'local' as const, message: '任务历史筛选参数无效，请重新选择筛选条件。' } })
  for (const key of keys) {
    const value = query[key]
    if (value !== undefined && (typeof value !== 'string' || value.length === 0 || value.length > (key === 'cursor' ? 2048 : 100))) return invalid()
  }
  const scope = query.scope ?? 'business'
  if (scope !== 'business' && scope !== 'all' && scope !== 'background') return invalid()
  filters.scope = scope
  if (typeof query.kind === 'string') {
    if (!kinds.has(query.kind)) return invalid()
    filters.kind = query.kind
  }
  if (query.status !== undefined) {
    const status = asTaskStatus(query.status)
    if (!status) return invalid()
    filters.status = status
  }
  filters.created_from_date = typeof query.created_from_date === 'string' ? query.created_from_date : null
  filters.created_to_date = typeof query.created_to_date === 'string' ? query.created_to_date : null
  if (!validDate(filters.created_from_date) || !validDate(filters.created_to_date) ||
    (filters.created_from_date && filters.created_to_date && filters.created_from_date > filters.created_to_date)) return invalid()
  return { filters, cursor: typeof query.cursor === 'string' ? query.cursor : null, error: null }
}

/**
 * 构造可分享路由；只继承现有公开 task_id，未知参数可能包含敏感内容，禁止盲目复制。
 * @param query 当前路由公开参数来源。
 * @param filters 将替换的完整筛选条件。
 * @param cursor 不透明分页游标；null 删除游标。
 * @returns 仅包含公开历史字段及 scalar task_id 的 query。
 */
export function historyRouteQuery(query: LocationQuery, filters: TaskHistoryFilters, cursor: string | null): LocationQueryRaw {
  const result: LocationQueryRaw = { scope: filters.scope }
  if (typeof query.task_id === 'string') result.task_id = query.task_id
  for (const key of ['kind', 'status', 'created_from_date', 'created_to_date'] as const) {
    if (filters[key] !== null) result[key] = filters[key]
  }
  if (cursor !== null) result.cursor = cursor
  return result
}
