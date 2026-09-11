/**
 * 统一操作中心 REST 边界：验证身份、版本与冻结预览，只返回内存投影。所有请求复用 Cookie/CSRF 客户端并禁止 HTTP 缓存；解析失败只抛无内容错误，服务端失败保留 ProblemError。
 */
import { requestJson } from './client'
import { parseApprovalPreview } from './approvalPreviews'
import { calendarProposalStatuses } from './calendar'
import { actionProviders } from './connections'
import { mailDraftStatuses } from './mail'
import {
  asTaskStatus,
  compareEventCursors,
  type ActionApproval,
  type ActionExecution,
  type ActionFilters,
  type ActionListItem,
  type ActionListPage,
  type ActionLocalSummary,
  type ActionSnapshot,
  type ActionTimelineEvent,
  type ManualResolution,
  type TaskStatus,
} from './types'
import * as v from './validation'

export const actionKinds = [
  'mail.send',
  'calendar.create',
  'calendar.update',
  'calendar.restore',
] as const
export const toolExecutionStatuses = [
  'claimed',
  'executing',
  'succeeded',
  'confirmed_failed',
  'retryable_failed',
  'reconciling',
  'needs_attention',
] as const
const manualResolutions = [
  'confirmed_executed',
  'confirmed_not_executed',
] as const
const calendarActions = [
  'calendar.create',
  'calendar.update',
  'calendar.restore',
] as const
const riskLevels = ['high', 'medium'] as const

/**
 * @param value 不可信列表。
 * @returns 每个变体均验证过身份和枚举的内容无关页面。
 */
export function parseActionList(value: unknown): ActionListPage {
  return v.boundary(
    value,
    (input) => v.page(input, parseActionListItem),
    'Invalid action list',
  )
}

/**
 * @param value 联合列表项。
 * @returns 任务身份和本地编辑身份严格互斥的条目。
 */
function parseActionListItem(value: unknown): ActionListItem {
  const o = v.record(value)
  const base = {
    id: v.uuid(o.id),
    provider: v.enumeration(o.provider, actionProviders),
    risk_level: v.nullable(o.risk_level, (value) =>
      v.enumeration(value, riskLevels),
    ),
    created_at: v.timestamp(o.created_at),
    updated_at: v.timestamp(o.updated_at),
  }
  if (o.item_kind === 'trusted_task') {
    const task_id = v.uuid(o.task_id)
    if (task_id !== base.id || o.editor_url !== null)
      throw new Error('Invalid task binding')
    return {
      ...base,
      item_kind: 'trusted_task',
      task_id,
      editor_url: null,
      status: taskStatus(o.status),
      action: v.enumeration(o.action, actionKinds),
    }
  }
  if (o.task_id !== null) throw new Error('Invalid local task identity')
  if (o.item_kind === 'mail_draft')
    return {
      ...base,
      item_kind: 'mail_draft',
      task_id: null,
      editor_url: editorUrl(o.editor_url, 'mail_draft', base.id),
      status: v.enumeration(o.status, mailDraftStatuses),
      action: v.enumeration(o.action, ['mail.send']),
    }
  if (o.item_kind === 'calendar_proposal')
    return {
      ...base,
      item_kind: 'calendar_proposal',
      task_id: null,
      editor_url: editorUrl(o.editor_url, 'calendar_proposal', base.id),
      status: v.enumeration(o.status, calendarProposalStatuses),
      action: v.enumeration(o.action, calendarActions),
    }
  throw new Error('Invalid action item kind')
}

/**
 * 解析同一数据库读取视图的完整快照；缺字段、未知枚举、失配游标和脱敏矛盾都拒绝。
 * @param value 不可信认证响应。
 * @returns 严格动作投影；不会把完整内容写入其他存储。
 * @throws Error 无内容的统一解析错误，避免原始响应进入日志。
 */
export function parseActionSnapshot(value: unknown): ActionSnapshot {
  return v.boundary(
    value,
    (input) => {
      const o = v.record(input)
      const event_cursor = v.cursor(o.event_cursor),
        task_version = v.cursor(o.task_version)
      if (event_cursor !== task_version)
        throw new Error('Invalid cursor binding')
      const action = v.nullable(o.action, (value) =>
        v.enumeration(value, actionKinds),
      )
      const provider = v.nullable(o.provider, (value) =>
        v.enumeration(value, actionProviders),
      )
      const approval = v.nullable(o.approval, parseApproval)
      const local_action = v.nullable(o.local_action, parseLocalAction)
      if ((action === null) !== (provider === null))
        throw new Error('Invalid provider binding')
      if (
        local_action &&
        (action === 'mail.send') !== (local_action.item_kind === 'mail_draft')
      )
        throw new Error('Invalid local action binding')
      const preview = approval?.preview
      if (
        preview &&
        (preview.provider !== provider ||
          (preview.kind === 'mail'
            ? action !== 'mail.send'
            : action !== `calendar.${preview.operation}`))
      )
        throw new Error('Invalid preview binding')
      const timeline = v.array(o.timeline, parseTimelineEvent)
      if (
        timeline.some(
          (event) => compareEventCursors(event.id, event_cursor) > 0,
        )
      )
        throw new Error('Invalid timeline cursor')
      return {
        task_id: v.uuid(o.task_id),
        status: taskStatus(o.status),
        error_code: v.nullable(o.error_code, v.text),
        event_cursor,
        task_version,
        reconciliation_attempt_count: v.integer(o.reconciliation_attempt_count),
        provider_url: v.nullable(o.provider_url, v.text),
        action,
        provider,
        created_at: v.nullable(o.created_at, v.timestamp),
        updated_at: v.nullable(o.updated_at, v.timestamp),
        local_action,
        approval,
        execution: v.nullable(o.execution, parseExecution),
        timeline: timeline.sort((left, right) =>
          compareEventCursors(left.id, right.id),
        ),
      }
    },
    'Invalid action snapshot',
  )
}

/**
 * @param filters 只含服务端支持的枚举与分页。
 * @returns REST 权威列表，不搜索或缓存敏感内容。
 */
export function listActions(
  filters: ActionFilters = {},
): Promise<ActionListPage> {
  const query = new URLSearchParams()
  for (const field of [
    'provider',
    'status',
    'item_kind',
    'action',
    'limit',
    'offset',
  ] as const) {
    const value = filters[field]
    if (value !== undefined) query.set(field, String(value))
  }
  return requestJson(
    `/actions${query.size ? `?${query}` : ''}`,
    parseActionList,
    { cache: 'no-store' },
  )
}
/**
 * @param taskId 真实 TaskRun ID。
 * @returns 身份绑定且包含最新版本的认证快照。
 */
export function getAction(taskId: string): Promise<ActionSnapshot> {
  return requestJson(
    `/actions/${encodeURIComponent(taskId)}`,
    (value) => {
      const result = parseActionSnapshot(value)
      if (result.task_id !== taskId)
        throw new Error('Invalid action snapshot identity')
      return result
    },
    { cache: 'no-store' },
  )
}
/**
 * @param taskId 原可信任务 ID。
 * @returns 原任务的只读核对接受结果，不创建新的写命令。
 */
export function reconcileAction(taskId: string): Promise<{ task_id: string }> {
  return requestJson(
    `/actions/${encodeURIComponent(taskId)}/reconcile`,
    (value) => boundTaskResponse(value, taskId),
    { method: 'POST', cache: 'no-store' },
  )
}
/**
 * 记录用户核对后的结论，返回最小 task_id/task_version；调用方随后必须重读快照。
 * @param taskId 原可信任务 ID。
 * @param resolution 人工结果枚举，不接受自由文本。
 * @param taskVersion 最新服务端快照的字符串版本，不能来自本地 SSE 推算。
 * @returns 追加结论后的新游标；不代表供应商收到任何新请求。
 */
export function resolveAction(
  taskId: string,
  resolution: ManualResolution,
  taskVersion: string,
): Promise<{ task_id: string; task_version: string }> {
  const input = {
    resolution: v.enumeration(resolution, manualResolutions),
    task_version: v.cursor(taskVersion),
  }
  return requestJson(
    `/actions/${encodeURIComponent(taskId)}/manual-resolution`,
    (value) => ({
      ...boundTaskResponse(value, taskId),
      task_version: v.cursor(v.record(value).task_version),
    }),
    { method: 'POST', cache: 'no-store', body: JSON.stringify(input) },
  )
}
/** 只在真实 TaskRun ID 相同的情况下接受 mutation 的最小响应。 */
function boundTaskResponse(
  value: unknown,
  taskId: string,
): { task_id: string } {
  const task_id = v.uuid(v.record(value).task_id)
  if (task_id !== taskId) throw new Error('Invalid action identity')
  return { task_id }
}
/** 复用 M1 已知状态验证，新增状态仍由同一公共枚举维护。 */
function taskStatus(value: unknown): TaskStatus {
  const result = asTaskStatus(value)
  if (result === null) throw new Error('Invalid task status')
  return result
}
/** 编辑器地址只能是服务端约定的本地 UUID 路径，禁止 query、fragment 和外部 URL。 */
function editorUrl(
  value: unknown,
  kind: 'mail_draft' | 'calendar_proposal',
  id: string,
): string {
  const result = v.text(value)
  const expected =
    kind === 'mail_draft' ? `/mail/drafts/${id}` : `/calendar/proposals/${id}`
  if (result !== expected) throw new Error('Invalid editor URL')
  return result
}
/** 本地版本和审批冻结版本分别保留，不能因快照到达擅自使其相等。 */
function parseLocalAction(value: unknown): ActionLocalSummary {
  const o = v.record(value)
  const id = v.uuid(o.id),
    version = v.integer(o.version, 1)
  if (o.item_kind === 'mail_draft')
    return {
      id,
      version,
      item_kind: 'mail_draft',
      status: v.enumeration(o.status, mailDraftStatuses),
      editor_url: editorUrl(o.editor_url, 'mail_draft', id),
    }
  if (o.item_kind === 'calendar_proposal')
    return {
      id,
      version,
      item_kind: 'calendar_proposal',
      status: v.enumeration(o.status, calendarProposalStatuses),
      editor_url: editorUrl(o.editor_url, 'calendar_proposal', id),
    }
  throw new Error('Invalid local action')
}
/** 脱敏状态必须与 null 预览互绑；任何版本或枚举都不得缺省。 */
function parseApproval(value: unknown): ActionApproval {
  const o = v.record(value)
  const payload_hash = v.text(o.payload_hash)
  if (!/^[0-9a-f]{64}$/.test(payload_hash))
    throw new Error('Invalid approval hash')
  const base = {
    id: v.uuid(o.id),
    status: v.enumeration(o.status, [
      'pending',
      'approved',
      'rejected',
      'expired',
      'invalidated',
    ]),
    version: v.integer(o.version, 1),
    payload_hash,
    proposal_version: v.integer(o.proposal_version, 1),
    risk_level: v.enumeration(o.risk_level, ['high', 'medium']),
    expires_at: v.timestamp(o.expires_at),
    decided_at: v.nullable(o.decided_at, v.timestamp),
  }
  if (o.content_status === 'redacted' && o.preview === null)
    return { ...base, content_status: 'redacted', preview: null }
  if (o.content_status === 'available')
    return {
      ...base,
      content_status: 'available',
      preview: parseApprovalPreview(o.preview),
    }
  throw new Error('Invalid redacted content')
}
/** 严格执行尝试与人工结论枚举；OAuth 通知不在此制造状态。 */
function parseExecution(value: unknown): ActionExecution {
  const o = v.record(value)
  return {
    id: v.uuid(o.id),
    status: v.enumeration(o.status, toolExecutionStatuses),
    write_attempt_count: v.integer(o.write_attempt_count),
    reconciliation_attempt_count: v.integer(o.reconciliation_attempt_count),
    error_code: v.nullable(o.error_code, v.text),
    claimed_at: v.nullable(o.claimed_at, v.timestamp),
    request_started_at: v.nullable(o.request_started_at, v.timestamp),
    completed_at: v.nullable(o.completed_at, v.timestamp),
    manual_resolution: v.nullable(o.manual_resolution, (value) =>
      v.enumeration(value, manualResolutions),
    ),
  }
}
/** 只保留已知内容无关字段，未知事件名称继续供展示层兼容处理。 */
function parseTimelineEvent(value: unknown): ActionTimelineEvent {
  const o = v.record(value),
    raw = v.record(o.payload)
  const payload: ActionTimelineEvent['payload'] = {}
  for (const key of ['status', 'error_code'] as const)
    if (raw[key] !== undefined && raw[key] !== null)
      payload[key] = v.text(raw[key])
  for (const key of [
    'write_attempt_count',
    'reconciliation_attempt_count',
  ] as const)
    if (raw[key] !== undefined) payload[key] = v.integer(raw[key])
  return {
    id: v.cursor(o.id),
    event: v.text(o.event),
    occurred_at: v.timestamp(o.occurred_at),
    payload,
  }
}
