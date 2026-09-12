/** 日程共享读取边界：解析原始快照与服务端冲突，不在浏览器重建冲突规则。 */
import type {
  CalendarConflictPreview,
  CalendarEditorFacts,
  CalendarPreviewFields,
} from './types'
import * as v from './validation'

/**
 * @param value 全天日期或带 offset 的完整日程字段。
 * @returns 原样保留精度的安全字段；时区与日期均不从宿主机推断。
 * @throws Error 日期、时区或时间形状非法。
 */
export function parseCalendarFields(value: unknown): CalendarPreviewFields {
  const o = v.record(value)
  const all_day = v.boolean(o.all_day)
  const temporal = (input: unknown): string => {
    if (!all_day) return v.timestamp(input)
    const date = v.text(input)
    if (
      !/^\d{4}-\d{2}-\d{2}$/.test(date) ||
      !Number.isFinite(Date.parse(`${date}T00:00:00Z`)) ||
      new Date(`${date}T00:00:00Z`).toISOString().slice(0, 10) !== date
    )
      throw new Error('Invalid all-day date')
    return date
  }
  const timezone = v.text(o.timezone)
  new Intl.DateTimeFormat('en', { timeZone: timezone })
  return {
    title: v.text(o.title),
    description: v.nullable(o.description, v.text),
    location: v.nullable(o.location, v.text),
    starts_at: temporal(o.starts_at),
    ends_at: temporal(o.ends_at),
    timezone,
    all_day,
    attendees: v.array(o.attendees, v.text),
  }
}

/** @param value 不含其他日程内容的冲突项。 @returns 服务器已计算的有界冲突事实。 */
export function parseCalendarConflict(value: unknown): CalendarConflictPreview {
  const o = v.record(value)
  return {
    kind: v.enumeration(o.kind, [
      'overlap',
      'outside_working_hours',
      'partial_sources',
    ]),
    starts_at: v.nullable(o.starts_at, v.timestamp),
    ends_at: v.nullable(o.ends_at, v.timestamp),
    missing_connection_ids: v.array(o.missing_connection_ids, v.uuid),
  }
}

/**
 * @param value GET 专属事实，不能把 unavailable/incomplete 转成成功空值。
 * @returns 严格判别联合，保证 null 与状态含义一致。
 * @throws Error 缺失、额外字段或互相矛盾的状态。
 */
export function parseCalendarEditorFacts(value: unknown): CalendarEditorFacts {
  const o = v.record(value)
  const keys = [
    'before_status',
    'before',
    'conflict_status',
    'conflicts',
    'restore_source',
    'reprepare_source',
  ]
  if (Object.keys(o).length !== keys.length || keys.some((key) => !(key in o)))
    throw new Error('Invalid editor facts fields')
  const beforeStatus = v.enumeration(o.before_status, [
    'not_applicable',
    'available',
    'unavailable',
  ])
  const restore_source = v.nullable(o.restore_source, (value) => {
    const source = v.record(value)
    if (
      Object.keys(source).length !== 2 ||
      !('event_id' in source) ||
      !('snapshot_id' in source)
    )
      throw new Error('Invalid restore source fields')
    return {
      event_id: v.uuid(source.event_id),
      snapshot_id: v.uuid(source.snapshot_id),
    }
  })
  const reprepare_source = v.nullable(o.reprepare_source, (value) => {
    const source = v.record(value)
    if (
      Object.keys(source).length !== 2 ||
      !('event_id' in source) ||
      !('requires_sync' in source)
    )
      throw new Error('Invalid reprepare source fields')
    const event_id = v.uuid(source.event_id)
    // 本地 UUID 必须保持服务端规范形式，不能把供应商 ID 或宽松别名当成原来源。
    if (event_id !== event_id.toLowerCase())
      throw new Error('Invalid canonical reprepare identifier')
    return { event_id, requires_sync: v.boolean(source.requires_sync) }
  })
  const before =
    beforeStatus === 'available'
      ? { before_status: beforeStatus, before: parseCalendarFields(o.before) }
      : { before_status: beforeStatus, before: null }
  if (beforeStatus !== 'available' && o.before !== null)
    throw new Error('Invalid before availability')
  const conflictStatus = v.enumeration(o.conflict_status, [
    'incomplete',
    'checked',
  ])
  if (conflictStatus === 'incomplete') {
    if (o.conflicts !== null) throw new Error('Invalid incomplete conflicts')
    return {
      ...before,
      restore_source,
      reprepare_source,
      conflict_status: 'incomplete',
      conflicts: null,
    }
  }
  return {
    ...before,
    restore_source,
    reprepare_source,
    conflict_status: 'checked',
    conflicts: v.array(o.conflicts, parseCalendarConflict),
  }
}
