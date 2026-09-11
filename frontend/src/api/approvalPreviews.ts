/**
 * 解析服务端冻结的结构化审批内容，保持邮件/日程变体及通知策略的精确含义。这里只建立安全内存模型，不批准命令，也不替代服务端的载荷哈希和有效性验证。
 */
import { calendarOperations } from './calendar'
import { actionProviders } from './connections'
import { mailModes } from './mail'
import type { ApprovalPreview } from './types'
import { parseCalendarFields, parseCalendarConflict } from './calendarFields'
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
    conflicts: v.array(o.conflicts, parseCalendarConflict),
    notification_policy: v.enumeration(o.notification_policy, ['all', 'none']),
    base_etag: v.nullable(o.base_etag, v.text),
    compensation_available: v.boolean(o.compensation_available),
    provider_warnings: v.array(o.provider_warnings, (warning) =>
      v.enumeration(warning, ['google_send_updates_none_external_sync']),
    ),
  }
}
