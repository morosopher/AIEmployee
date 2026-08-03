/** 任务状态严格对应服务端持久状态机，页面不得自行扩展状态。 */
export type TaskStatus =
  | 'created'
  | 'queued'
  | 'running'
  | 'waiting_approval'
  | 'retry_scheduled'
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
  completed_at?: string
}

/** 可从 REST 或 SSE 快照恢复的任务投影。 */
export interface TaskSnapshot {
  id: string
  kind: string
  status: TaskStatus
  retry_of_task_id: string | null
  error_code: string | null
  steps: TaskStep[]
}

/** SSE 传输的类型化信封；payload 保留给事件 reducer 再做事件级收窄。 */
export interface TaskEvent {
  id: number
  task_id: string
  sequence: number
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
    steps: object.steps.map(parseTaskStep),
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
      typeof object.id !== 'number' ||
      typeof object.task_id !== 'string' ||
      typeof object.sequence !== 'number' ||
      typeof object.event !== 'string' ||
      typeof object.occurred_at !== 'string'
    ) {
      return null
    }
    return {
      id: object.id,
      task_id: object.task_id,
      sequence: object.sequence,
      event: object.event,
      occurred_at: object.occurred_at,
      step_id: typeof object.step_id === 'string' ? object.step_id : null,
      payload: asJsonObject(object.payload) ?? {},
    }
  } catch {
    return null
  }
}
