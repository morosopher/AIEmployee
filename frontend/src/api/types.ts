/** 任务状态严格对应服务端持久状态机，页面不得自行扩展状态。 */
export type TaskStatus =
  | 'created'
  | 'queued'
  | 'running'
  | 'waiting_approval'
  | 'retry_scheduled'
  | 'reconciling'
  | 'needs_attention'
  | 'succeeded'
  | 'failed'
  | 'cancelled'

/** 服务端允许返回、但前端只作为安全文本展示的 JSON 对象。 */
export type JsonObject = Record<string, unknown>

/** 任务步骤的可展示快照；摘要绝不作为 HTML 注入。 */
export interface TaskStep {
  id: string
  sequence: number
  name: string
  status: string
  output_summary: JsonObject | null
  error_code: string | null
  started_at?: string
  finished_at?: string
}

/** 可从 REST 或 SSE 快照恢复的任务投影。 */
export interface TaskSnapshot {
  id: string
  kind: string
  status: TaskStatus
  retry_of_task_id: string | null
  error_code: string | null
  /** 服务端快照对应的最大持久审计 ID；仅作为精确十进制游标使用。 */
  event_cursor: string
  steps: TaskStep[]
  /** 仅成功恢复准备任务的已验证结果；旧M1快照可省略，不能据此猜测结果。 */
  calendar_restore_proposal_id?: string | null
}

/** SSE 传输的类型化信封；payload 保留给事件 reducer 再做事件级收窄。 */
export interface TaskEvent {
  id: string
  task_id: string
  sequence: string
  event: string
  occurred_at: string
  step_id: string | null
  payload: JsonObject
}

/** 认证后可公开展示的管理员资料，不包含任何会话或凭据字段。 */
export interface AuthUser {
  id: string
  email: string
  display_name: string
  timezone: string
  locale: string
  brief_time: string
}

export interface BriefItem { position: number; section: string; priority: string; title: string; body_markdown: string; source_refs: Array<{ source_type: string; source_id: string; provider_url: string | null }>; suggested_action_kind: string | null }
export interface Brief { id: string; local_date: string; version: number; task_id: string; source_cutoff: string; completeness: string; headline: string; structured_content: JsonObject; markdown: string; warnings: string[]; items: BriefItem[] }
export interface Conversation { id: string; title: string; created_at: string; updated_at: string }
export interface Message { id: string; role: string; content_markdown: string; task_id: string | null; created_at: string }
export interface Session { id: string; created_at: string; last_seen_at: string; expires_at: string; is_current?: boolean }
// M2 领域响应独立声明，统一从此公共边界导出，避免旧任务解析器混入编辑器规则。
export type * from './m2Types'

/** 运行时验证简报公开响应，拒绝数组及缺失核心字段。 */
export function parseBrief(value: unknown): Brief {
  const object = asJsonObject(value)
  if (!object || typeof object.id !== 'string' || typeof object.local_date !== 'string' || typeof object.version !== 'number' || typeof object.completeness !== 'string' || !Array.isArray(object.warnings) || !Array.isArray(object.items)) throw new Error('Invalid brief response')
  if (typeof object.task_id !== 'string' || typeof object.source_cutoff !== 'string' || typeof object.headline !== 'string' || !asJsonObject(object.structured_content) || typeof object.markdown !== 'string') throw new Error('Invalid brief response')
  const items = object.items.map((item) => { const o = asJsonObject(item); if (!o || typeof o.position !== 'number' || typeof o.section !== 'string' || typeof o.priority !== 'string' || typeof o.title !== 'string' || typeof o.body_markdown !== 'string' || !Array.isArray(o.source_refs) || (o.suggested_action_kind !== null && typeof o.suggested_action_kind !== 'string')) throw new Error('Invalid brief item'); const refs = o.source_refs.map((ref) => { const r = asJsonObject(ref); if (!r || typeof r.source_type !== 'string' || typeof r.source_id !== 'string' || (r.provider_url !== null && typeof r.provider_url !== 'string')) throw new Error('Invalid source ref'); return { source_type: r.source_type, source_id: r.source_id, provider_url: r.provider_url as string | null } }); return { position: o.position, section: o.section, priority: o.priority, title: o.title, body_markdown: o.body_markdown, source_refs: refs, suggested_action_kind: o.suggested_action_kind as string | null } })
  return { id: object.id, local_date: object.local_date, version: object.version, task_id: object.task_id, source_cutoff: object.source_cutoff, completeness: object.completeness, headline: object.headline, structured_content: object.structured_content as JsonObject, markdown: object.markdown, warnings: object.warnings.filter((v): v is string => typeof v === 'string'), items }
}
/** 验证会话公开字段，避免未知 JSON 进入设置页面。 */
export function parseSession(value: unknown): Session {
  const object = asJsonObject(value)
  if (!object || typeof object.id !== 'string' || typeof object.created_at !== 'string' || typeof object.last_seen_at !== 'string' || typeof object.expires_at !== 'string') throw new Error('Invalid session response')
  return { id: object.id, created_at: object.created_at, last_seen_at: object.last_seen_at, expires_at: object.expires_at, is_current: object.is_current === true }
}

/** RFC 9457 扩展错误字段，提供稳定的恢复提示和追踪编号。 */
export interface ProblemDetails {
  type: string
  title: string
  status: number
  detail: string
  instance: string
  error_code: string
  trace_id: string
}

/** SSE 连接状态独立于任务事实，断线不能推导任务失败。 */
export type TaskConnectionState =
  'connecting' | 'connected' | 'reconnecting' | 'disconnected'

const taskStatuses: readonly TaskStatus[] = [
  'created',
  'queued',
  'running',
  'waiting_approval',
  'retry_scheduled',
  'reconciling',
  'needs_attention',
  'succeeded',
  'failed',
  'cancelled',
]

/**
 * 将不可信 JSON 安全收窄为普通对象，数组和空值不允许进入状态层。
 *
 * @param value 任意外部 JSON 值。
 * @returns 可安全读取字段的对象，或空值。
 */
export function asJsonObject(value: unknown): JsonObject | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as JsonObject)
    : null
}

/**
 * 验证服务端任务状态，未知状态留给调用方拒绝而非污染 Pinia 投影。
 *
 * @param value 外部状态值。
 * @returns 已知任务状态，或空值。
 */
export function asTaskStatus(value: unknown): TaskStatus | null {
  return typeof value === 'string' && taskStatuses.includes(value as TaskStatus)
    ? (value as TaskStatus)
    : null
}

/**
 * 将审计 BIGINT 收窄为不丢失精度的十进制字符串。
 *
 * @param value 服务端 JSON 中的游标字段。
 * @returns 规范化游标；不安全数字或非规范字符串返回空。
 */
export function asEventCursor(value: unknown): string | null {
  return typeof value === 'string' && /^(0|[1-9]\d*)$/.test(value)
    ? value
    : null
}

/**
 * 比较规范非负十进制游标，复用 M1 的长度及字典序算法，不经过 number。
 * @param left 左侧已验证游标。
 * @param right 右侧已验证游标。
 * @returns 负数、零或正数代表顺序关系。
 */
export function compareEventCursors(left: string, right: string): number {
  if (left.length !== right.length) return left.length - right.length
  return left < right ? -1 : left > right ? 1 : 0
}

/**
 * 解析 REST/SSE 中的任务快照，缺失公开字段时拒绝该响应。
 *
 * @param value 不可信 API JSON。
 * @returns 可用于界面的类型化快照。
 * @throws Error 当响应不符合公开任务契约时抛出。
 */
export function parseTaskSnapshot(value: unknown): TaskSnapshot {
  const object = asJsonObject(value)
  const status = asTaskStatus(object?.status)
  if (
    !object ||
    typeof object.id !== 'string' ||
    typeof object.kind !== 'string' ||
    !status
  ) {
    throw new Error('Invalid task snapshot response')
  }
  if (!Array.isArray(object.steps))
    throw new Error('Invalid task steps response')
  const eventCursor = asEventCursor(object.event_cursor)
  if (eventCursor === null)
    throw new Error('Invalid task event cursor')
  const restoreId = object.calendar_restore_proposal_id
  if (restoreId !== undefined && restoreId !== null && (
    object.kind !== 'calendar.restore.prepare' || status !== 'succeeded' ||
    typeof restoreId !== 'string' ||
    !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(restoreId)
  )) throw new Error('Invalid calendar restore task result')
  return {
    id: object.id,
    kind: object.kind,
    status,
    retry_of_task_id:
      typeof object.retry_of_task_id === 'string'
        ? object.retry_of_task_id
        : null,
    error_code:
      typeof object.error_code === 'string' ? object.error_code : null,
    event_cursor: eventCursor,
    steps: object.steps.map(parseTaskStep),
    ...(restoreId === undefined ? {} : { calendar_restore_proposal_id: restoreId as string | null }),
  }
}

/**
 * 把单个步骤收窄到只含可安全展示的字段。
 *
 * @param value 不可信步骤 JSON。
 * @returns 类型化步骤。
 * @throws Error 当步骤缺少主键、顺序或显示字段时抛出。
 */
export function parseTaskStep(value: unknown): TaskStep {
  const object = asJsonObject(value)
  if (
    !object ||
    typeof object.id !== 'string' ||
    typeof object.sequence !== 'number' ||
    typeof object.name !== 'string' ||
    typeof object.status !== 'string'
  ) {
    throw new Error('Invalid task step response')
  }
  return {
    id: object.id,
    sequence: object.sequence,
    name: object.name,
    status: object.status,
    output_summary: asJsonObject(object.output_summary),
    error_code:
      typeof object.error_code === 'string' ? object.error_code : null,
    started_at:
      typeof object.started_at === 'string' ? object.started_at : undefined,
    finished_at:
      typeof object.finished_at === 'string' ? object.finished_at : undefined,
  }
}

/**
 * 解析 EventSource 的 JSON data，格式错误仅丢弃该消息并保留连接。
 *
 * @param value EventSource 提供的文本负载。
 * @returns 完整事件；不可信或不完整时返回空值。
 */
export function parseTaskEvent(value: string): TaskEvent | null {
  try {
    const object = asJsonObject(JSON.parse(value) as unknown)
    if (
      !object ||
      !asEventCursor(object.id) ||
      typeof object.task_id !== 'string' ||
      !asEventCursor(object.sequence) ||
      typeof object.event !== 'string' ||
      typeof object.occurred_at !== 'string'
    ) {
      return null
    }
    return {
      id: asEventCursor(object.id) as string,
      task_id: object.task_id,
      sequence: asEventCursor(object.sequence) as string,
      event: object.event,
      occurred_at: object.occurred_at,
      step_id: typeof object.step_id === 'string' ? object.step_id : null,
      payload: asJsonObject(object.payload) ?? {},
    }
  } catch {
    return null
  }
}
