import type { BriefItem } from '@/api/types'

/** 简报只允许将本地邮件线程或非重复日历来源带入两个已批准的编辑入口。 */
export type BriefSource = BriefItem['source_refs'][number]
export type BriefActionKind = 'mail.reply' | 'calendar.update'

/**
 * @param kind 服务端建议类型，不能作为通用工具名执行。
 * @param source 用户明确选择的来源；source_id 必须是本地 UUID。
 * @returns 可以展示本地编辑入口的严格类型，或 null。
 */
export function sourceAction(
  kind: string | null,
  source: BriefSource,
): BriefActionKind | null {
  if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(source.source_id))
    return null
  if (kind === 'mail.reply' && source.source_type === 'email_thread')
    return kind
  if (kind === 'calendar.update' && source.source_type === 'calendar_event')
    return kind
  return null
}
