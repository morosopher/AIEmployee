import { requestJson } from './client'
import { asJsonObject, asTaskStatus, type TaskStatus } from './types'

/** 公开列表过滤条件；日期由服务端按用户时区裁决，客户端不推断 UTC 边界。 */
export interface TaskHistoryFilters {
  scope: 'business' | 'all' | 'background'
  kind: string | null
  status: TaskStatus | null
  created_from_date: string | null
  created_to_date: string | null
}

/** 独立只读任务摘要；没有完整快照字段，不能写入 Task Store 或 SSE 投影。 */
export interface TaskHistoryItem {
  id: string
  kind: string
  category: 'business' | 'background' | 'other'
  status: TaskStatus
  created_at: string
  started_at: string | null
  finished_at: string | null
  error_code: string | null
  retry_of_task_id: string | null
}

/** 服务端同一读取快照的分页结果；游标不透明，计数不表示未处理事项。 */
export interface TaskHistoryPage {
  items: TaskHistoryItem[]
  next_cursor: string | null
  previous_cursor: string | null
  server_time: string
  filter_timezone: string
  background_failed_count: number
}

/**
 * 验证服务端规范 UUID 文本；不限制版本及 variant，兼容所有持久任务标识。
 * @param value 外部 JSON 字段。
 * @returns 是否是可按字典序比较的规范小写 UUID。
 */
function isUuid(value: unknown): value is string {
  return typeof value === 'string' && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(value)
}

/**
 * 验证六位微秒 UTC 时间及真实日历日期，原始文本保持不变。
 * @param value 外部 JSON 时间字段。
 * @returns 是否符合服务端的固定宽度时间契约。
 */
function isTimestamp(value: unknown): value is string {
  if (typeof value !== 'string' || !/^(?!0000)\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$/.test(value)) return false
  // Date 仅用于验证日历有效性：回转可识别 2 月 30 日及 24 点等自动进位。
  // 不用毫秒值构造输出或排序键，避免丢失服务端后三位微秒。
  const milliseconds = `${value.slice(0, 23)}Z`
  const date = new Date(milliseconds)
  return Number.isFinite(date.getTime()) && date.toISOString() === milliseconds
}

/**
 * 限制响应游标的长度；内容留给服务端验证，不在浏览器解析签名载荷。
 * @param value 外部 JSON 游标字段。
 * @returns 是否为空或长度有效的不透明文本。
 */
function isCursor(value: unknown): value is string | null {
  return value === null || (typeof value === 'string' && value.length > 0 && value.length <= 2048)
}

/**
 * 严格验证并显式投影摘要，未来 kind 可读但未知状态和分类不可猜测。
 * @param value 不可信摘要 JSON。
 * @returns 仅含九个白名单字段的独立对象。
 * @throws Error 字段缺失或非法时抛出固定无载荷错误。
 */
function parseHistoryItem(value: unknown): TaskHistoryItem {
  const item = asJsonObject(value)
  const status = asTaskStatus(item?.status)
  if (!item || !isUuid(item.id) || typeof item.kind !== 'string' || !status ||
    (item.category !== 'business' && item.category !== 'background' && item.category !== 'other') ||
    !isTimestamp(item.created_at) ||
    (item.started_at !== null && !isTimestamp(item.started_at)) ||
    (item.finished_at !== null && !isTimestamp(item.finished_at)) ||
    (item.error_code !== null && typeof item.error_code !== 'string') ||
    (item.retry_of_task_id !== null && !isUuid(item.retry_of_task_id))) {
    throw new Error('Invalid task history response')
  }
  return {
    id: item.id,
    kind: item.kind,
    category: item.category,
    status,
    created_at: item.created_at,
    started_at: item.started_at,
    finished_at: item.finished_at,
    error_code: item.error_code,
    retry_of_task_id: item.retry_of_task_id,
  }
}

/**
 * 收窄整个分页响应，不传播额外字段，也不以空数组掩盖格式失败。
 * @param value 不可信列表 JSON。
 * @returns 可供独立列表状态使用的公开分页模型。
 * @throws Error 响应违约时抛出固定错误，不包含游标或原始响应。
 */
export function parseTaskHistoryPage(value: unknown): TaskHistoryPage {
  const page = asJsonObject(value)
  if (!page || !Array.isArray(page.items) || !isCursor(page.next_cursor) ||
    !isCursor(page.previous_cursor) || !isTimestamp(page.server_time) ||
    typeof page.filter_timezone !== 'string' || page.filter_timezone.length === 0 ||
    typeof page.background_failed_count !== 'number' ||
    !Number.isSafeInteger(page.background_failed_count) || page.background_failed_count < 0) {
    throw new Error('Invalid task history response')
  }
  return {
    items: page.items.map(parseHistoryItem),
    next_cursor: page.next_cursor,
    previous_cursor: page.previous_cursor,
    server_time: page.server_time,
    filter_timezone: page.filter_timezone,
    background_failed_count: page.background_failed_count,
  }
}

/**
 * 通过统一 Cookie 客户端读取固定 20 条摘要；不触碰快照、Store 或 SSE。
 * @param filters 公开过滤参数；null 表示省略，不转换日期或默改范围。
 * @param cursor 服务端返回的不透明游标；null 从最新第一页读取。
 * @returns 严格验证后的分页响应。
 * @throws ProblemError 服务端失败沿用统一 Problem Details 和 trace_id。
 * @throws Error 成功响应不符合摘要契约时抛出。
 */
export function listTaskHistory(filters: TaskHistoryFilters, cursor: string | null = null): Promise<TaskHistoryPage> {
  const query = new URLSearchParams({ scope: filters.scope, limit: '20' })
  for (const key of ['kind', 'status', 'created_from_date', 'created_to_date'] as const) {
    const value = filters[key]
    if (value !== null) query.set(key, value)
  }
  if (cursor !== null) query.set('cursor', cursor)
  return requestJson(`/tasks?${query.toString()}`, parseTaskHistoryPage)
}

/**
 * 为已验证摘要构造完整精度创建顺序键，供新任务探测比较头部使用。
 * @param item 经解析的摘要，时间为六位微秒 UTC，UUID 为规范小写文本。
 * @returns 可直接用 `<`／`>` 比较的键；禁止 localeCompare 或毫秒转换。
 */
export function historyItemKey(item: TaskHistoryItem): string {
  return `${item.created_at}:${item.id}`
}
