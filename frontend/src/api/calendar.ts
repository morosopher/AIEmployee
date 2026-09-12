/**
 * 非重复日程提案 REST 边界：解析本地 shell、精确版本和候选完整性。所有写接口经 Cookie/CSRF，创建类请求使用稳定意图；领域冲突、ETag 与批准规则仍由服务端判断。
 */
import { requestJson } from './client'
import { parseCalendarEditorFacts } from './calendarFields'
import { parseAcceptedActionTask } from './mail'
import { requestCreation, type RequestIntent } from './requestIntent'
import type {
  AcceptedActionTask,
  CalendarAvailability,
  CalendarProposal,
} from './types'
import * as v from './validation'

export const calendarOperations = ['create', 'update', 'restore'] as const
export const calendarProposalStatuses = [
  'editing',
  'awaiting_approval',
  'executing',
  'applied',
  'stale',
  'needs_attention',
  'cancelled',
] as const
export const calendarEditableFields = [
  'title',
  'description',
  'location',
  'starts_at',
  'ends_at',
  'timezone',
  'all_day',
  'attendees',
  'notification_policy',
] as const
const confirmationFields = [
  'calendar',
  'time',
  'attendees',
  'notification_policy',
] as const

/** 只表达非重复日程；时间语法和冲突规则以服务端为准。 */
export interface CalendarEventInput {
  title: string
  description?: string | null
  location?: string | null
  starts_at: string
  ends_at: string
  timezone: string
  all_day: boolean
  attendees: string[]
  notification_policy?: 'all' | 'none' | null
}
export type CreateCalendarProposalInput =
  | { operation_kind: 'create'; initialization: 'shell' }
  | { operation_kind: 'update'; initialization: 'shell'; event_id: string }
  | (CalendarEventInput & {
      operation_kind: 'create'
      connection_id: string
      calendar_id: string
    })
  | (Partial<CalendarEventInput> & {
      operation_kind: 'update'
      event_id: string
    })
/** 普通保存与显式确认互斥；来源重选只能通过单独的 calendar 确认完成。 */
export type CalendarConfirmationInput =
  | { kind: 'calendar'; connection_id: string; calendar_id: string }
  | { kind: 'time' | 'attendees' | 'notification_policy' }
/** 与既有 PATCH 一致，shell 字段可显式清空；确认仍使用互斥的独立变体。 */
export type CalendarProposalChanges = {
  [K in keyof CalendarEventInput]?: CalendarEventInput[K] | null
}
export type UpdateCalendarProposalInput =
  | (CalendarProposalChanges & { version: number; confirmation?: never })
  | { version: number; confirmation: CalendarConfirmationInput }

/**
 * @param value 服务端候选结果。
 * @returns 最多三个候选及完整性。
 * @throws Error 非法枚举或越界。
 */
export function parseCalendarAvailability(
  value: unknown,
): CalendarAvailability {
  const o = v.record(value)
  const candidates = v.array(o.candidates, (item) => {
    const candidate = v.record(item)
    return {
      starts_at: v.timestamp(candidate.starts_at),
      ends_at: v.timestamp(candidate.ends_at),
    }
  })
  if (candidates.length > 3 || o.attendee_availability_checked !== false)
    throw new Error('Invalid calendar availability')
  return {
    proposal_id: v.uuid(o.proposal_id),
    version: v.integer(o.version, 1),
    candidates,
    completeness: v.enumeration(o.completeness, ['complete', 'partial']),
    missing_connections: v.array(o.missing_connections, v.uuid),
    attendee_availability_checked: false,
  }
}

/**
 * @param value 不可信提案响应。
 * @returns 当前版本，包括合法未确认 shell。
 * @throws Error 缺少字段、版本或枚举。
 */
export function parseCalendarProposal(value: unknown): CalendarProposal {
  return v.boundary(
    value,
    (input) => {
      const o = v.record(input)
      const id = v.uuid(o.id)
      const version = v.integer(o.version, 1)
      const availability = v.nullable(o.availability, parseCalendarAvailability)
      const editorFacts = v.nullable(o.editor_facts, parseCalendarEditorFacts)
      if (
        editorFacts &&
        (o.operation_kind === 'create') !==
          (editorFacts.before_status === 'not_applicable')
      )
        throw new Error('Invalid before operation binding')
      if (
        editorFacts?.restore_source &&
        (o.operation_kind !== 'update' ||
          o.status !== 'applied' ||
          editorFacts.before_status !== 'available' ||
          editorFacts.restore_source.snapshot_id !== o.before_snapshot_id)
      )
        throw new Error('Invalid calendar restore source binding')
      if (
        editorFacts?.reprepare_source &&
        (o.operation_kind !== 'update' ||
          !['editing', 'stale', 'cancelled'].includes(v.text(o.status)) ||
          editorFacts.before_status !== 'available' ||
          o.before_snapshot_id === null ||
          o.target_event_id === null)
      )
        throw new Error('Invalid calendar reprepare source binding')
      if (
        availability &&
        (availability.proposal_id !== id || availability.version !== version)
      )
        throw new Error('Invalid availability binding')
      return {
        id,
        connection_id: v.uuid(o.connection_id),
        calendar_id: v.text(o.calendar_id),
        operation_kind: v.enumeration(o.operation_kind, calendarOperations),
        target_event_id: v.nullable(o.target_event_id, v.text),
        base_etag: v.nullable(o.base_etag, v.text),
        before_snapshot_id: v.nullable(o.before_snapshot_id, v.uuid),
        version,
        status: v.enumeration(o.status, calendarProposalStatuses),
        title: v.nullable(o.title, v.text),
        description: v.nullable(o.description, v.text),
        location: v.nullable(o.location, v.text),
        starts_at: v.nullable(o.starts_at, v.text),
        ends_at: v.nullable(o.ends_at, v.text),
        timezone: v.nullable(o.timezone, v.text),
        all_day: v.nullable(o.all_day, v.boolean),
        attendees: v.array(o.attendees, v.text),
        notification_policy: v.nullable(o.notification_policy, (item) =>
          v.enumeration(item, ['all', 'none']),
        ),
        changed_fields: v.array(o.changed_fields, (field) =>
          v.enumeration(field, calendarEditableFields),
        ),
        field_diffs: v.array(o.field_diffs, (item) => {
          const diff = v.record(item)
          if (diff.changed !== true) throw new Error('Invalid field diff')
          return {
            field: v.enumeration(diff.field, calendarEditableFields),
            changed: true as const,
          }
        }),
        required_confirmations: v.array(o.required_confirmations, (field) =>
          v.enumeration(field, confirmationFields),
        ),
        retain_until: v.timestamp(o.retain_until),
        availability,
        editor_facts: editorFacts,
      }
    },
    'Invalid calendar proposal',
  )
}
/**
 * @param limit 页大小。
 * @param offset 偏移。
 * @returns 认证用户提案页。
 */
export function listCalendarProposals(limit = 50, offset = 0) {
  return requestJson(
    `/calendar/proposals?limit=${v.integer(limit, 1)}&offset=${v.integer(offset)}`,
    (value) => v.page(value, parseCalendarProposal),
    { cache: 'no-store' },
  )
}
/**
 * @param id 提案 ID。
 * @returns 权威当前版本。
 */
export function getCalendarProposal(id: string): Promise<CalendarProposal> {
  return requestJson(
    `/calendar/proposals/${encodeURIComponent(id)}`,
    (value) => boundProposal(value, id),
    { cache: 'no-store' },
  )
}
/**
 * @param input 创建或修改提案字段。
 * @param intent 稳定意图。
 * @returns 新本地提案。
 */
export function createCalendarProposal(
  input: CreateCalendarProposalInput,
  intent: RequestIntent,
): Promise<CalendarProposal> {
  return requestCreation(
    '/calendar/proposals',
    parseCalendarProposal,
    intent,
    JSON.stringify(input),
  )
}
/**
 * @param id 提案 ID。
 * @param input 版本 CAS 更新。
 * @returns 服务端新版本。
 */
export function updateCalendarProposal(
  id: string,
  input: UpdateCalendarProposalInput,
): Promise<CalendarProposal> {
  return requestJson(
    `/calendar/proposals/${encodeURIComponent(id)}`,
    (value) => boundProposal(value, id),
    { method: 'PATCH', cache: 'no-store', body: JSON.stringify(input) },
  )
}
/**
 * @param id 未执行的本地提案。
 * @returns 取消后的权威提案，不删除供应商日程。
 */
export function cancelCalendarProposal(id: string): Promise<CalendarProposal> {
  return requestJson(
    `/calendar/proposals/${encodeURIComponent(id)}`,
    (value) => boundProposal(value, id),
    { method: 'DELETE', cache: 'no-store' },
  )
}
/**
 * @param id 提案 ID。
 * @param input 可选当前版本和搜索下界。
 * @returns 绑定新提案版本的确定性候选；须重新 GET 完整提案，且不会创建执行任务。
 */
export function suggestCalendarTimes(
  id: string,
  input: { version?: number; search_start?: string } = {},
): Promise<CalendarAvailability> {
  return requestJson(
    `/calendar/proposals/${encodeURIComponent(id)}/suggest-times`,
    (value) => {
      const result = parseCalendarAvailability(value)
      if (result.proposal_id !== id)
        throw new Error('Invalid calendar availability binding')
      return result
    },
    { method: 'POST', cache: 'no-store', body: JSON.stringify(input) },
  )
}
/**
 * @param id 提案 ID。
 * @param version 精确版本。
 * @param intent 稳定意图。
 * @returns 冻结后的可信任务。
 */
export function submitCalendarProposal(
  id: string,
  version: number,
  intent: RequestIntent,
): Promise<AcceptedActionTask> {
  return requestCreation(
    `/calendar/proposals/${encodeURIComponent(id)}/submit`,
    parseAcceptedActionTask,
    intent,
    JSON.stringify({ version }),
  )
}
/**
 * @param eventId 本地日程 ID。
 * @param snapshotId 精确历史快照。
 * @param intent 稳定意图。
 * @returns 恢复准备任务的202回执；准备成功后才能读取新提案，并另行确认、审批。
 */
export function createRestoreProposal(
  eventId: string,
  snapshotId: string,
  intent: RequestIntent,
): Promise<AcceptedActionTask> {
  return requestCreation(
    `/calendar/events/${encodeURIComponent(eventId)}/restore-proposal`,
    parseAcceptedActionTask,
    intent,
    JSON.stringify({ snapshot_id: snapshotId }),
  )
}
/** 确认响应身份，防止迟到或错路由响应进入当前提案。 */
function boundProposal(value: unknown, id: string): CalendarProposal {
  const result = parseCalendarProposal(value)
  if (result.id !== id) throw new Error('Invalid calendar proposal identity')
  return result
}
