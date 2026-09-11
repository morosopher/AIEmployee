/**
 * 解析服务端冻结的结构化审批内容，保持邮件/日程变体及通知策略的精确含义。这里只建立安全内存模型，不批准命令，也不替代服务端的载荷哈希和有效性验证。
 */
import { calendarOperations } from './calendar'
import { actionProviders } from './connections'
import { mailModes } from './mail'
import type { ApprovalPreview, CalendarPreviewFields } from './types'
import * as v from './validation'

/**
 * 验证被冻结的结构化审批预览；该边界只解析精确内容，不计算审批有效性。
 * @param value 完整、不可信预览。
 * @returns 由 kind 区分的邮件或日程预览。
 * @throws Error 未知类型、通知策略或关键字段缺失。
 */
export function parseApprovalPreview(value: unknown): ApprovalPreview {
  const o = v.record(value)
  const provider = v.enumeration(o.provider, actionProviders)
  const account_email = v.text(o.account_email)
  if (o.kind === 'mail') {
    if (o.irreversible !== true) throw new Error('Invalid mail risk')
    return {
      kind: 'mail',
      provider,
      account_email,
      mode: v.enumeration(o.mode, mailModes),
      to: v.array(o.to, v.text),
      cc: v.array(o.cc, v.text),
      bcc: v.array(o.bcc, v.text),
      subject: v.text(o.subject),
      body_text: v.text(o.body_text),
      irreversible: true,
    }
  }
  if (o.kind !== 'calendar') throw new Error('Invalid preview kind')
  return {
    kind: 'calendar',
    provider,
    account_email,
    calendar_name: v.text(o.calendar_name),
    operation: v.enumeration(o.operation, calendarOperations),
    before: v.nullable(o.before, parseCalendarFields),
    after: parseCalendarFields(o.after),
    conflicts: v.array(o.conflicts, (item) => {
      const conflict = v.record(item)
      return {
        kind: v.enumeration(conflict.kind, [
          'overlap',
          'outside_working_hours',
          'partial_sources',
        ]),
        starts_at: v.nullable(conflict.starts_at, v.timestamp),
        ends_at: v.nullable(conflict.ends_at, v.timestamp),
        missing_connection_ids: v.array(
          conflict.missing_connection_ids,
          v.uuid,
        ),
      }
    }),
    notification_policy: v.enumeration(o.notification_policy, ['all', 'none']),
    base_etag: v.nullable(o.base_etag, v.text),
    compensation_available: v.boolean(o.compensation_available),
    provider_warnings: v.array(o.provider_warnings, (warning) =>
      v.enumeration(warning, ['google_send_updates_none_external_sync']),
    ),
  }
}

/** 全天日期原样保留，定时时刻必须携带 offset；展示层不得猜测时区。 */
function parseCalendarFields(value: unknown): CalendarPreviewFields {
  const o = v.record(value)
  const all_day = v.boolean(o.all_day)
  const temporal = (value: unknown): string => {
    if (!all_day) return v.timestamp(value)
    const date = v.text(value)
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date))
      throw new Error('Invalid all-day date')
    return date
  }
  return {
    title: v.text(o.title),
    description: v.nullable(o.description, v.text),
    location: v.nullable(o.location, v.text),
    starts_at: temporal(o.starts_at),
    ends_at: temporal(o.ends_at),
    timezone: v.text(o.timezone),
    all_day,
    attendees: v.array(o.attendees, v.text),
  }
}
