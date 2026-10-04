/** 日程表单仅校验格式，原 hook 继续管理确认、冲突、版本及精确 API 输入。 */
import { z } from 'zod'
import { zodResolver } from '@primevue/forms/resolvers/zod'
import { recipientList } from '@/features/actions/editorInput'
import { instantToWall, wallToInstant } from './time'
import type { CalendarProposal } from '@/api/types'

/** @returns Intl 可识别的显式 IANA 标识；不读取宿主默认区、不接受 offset 代替时区。 */
function validTimezone(value: string): boolean {
  if (!value || /^[+-]/.test(value)) return false
  try {
    new Intl.DateTimeFormat('zh-CN', { timeZone: value })
    return true
  } catch {
    return false
  }
}
/** @param current 已保存值或用户明确输入。@returns 本地列表、UTC 与合法别名，不以枚举不完整拒绝合法值。 */
export function calendarTimezoneOptions(current: string): string[] {
  const zones =
    typeof Intl.supportedValuesOf === 'function'
      ? Intl.supportedValuesOf('timeZone')
      : []
  return [
    ...new Set(['UTC', ...zones, ...(validTimezone(current) ? [current] : [])]),
  ]
}
/** @returns 严格的公历日期表示；UTC 仅用于检查年月日溢出，不执行业务时区转换。 */
function validDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false
  const date = new Date(`${value}T00:00:00Z`)
  const [year, month, day] = value.split('-').map(Number)
  return (
    Number.isFinite(date.valueOf()) &&
    date.getUTCFullYear() === year &&
    date.getUTCMonth() + 1 === month &&
    date.getUTCDate() === day
  )
}
/** 已采纳服务端快照的最小只读时间上下文；不包含版本更新、错误或请求能力。 */
export type CalendarTimeSnapshot = Readonly<
  Pick<CalendarProposal, 'starts_at' | 'ends_at' | 'timezone' | 'all_day'>
>

/**
 * @param snapshot 原 hook 已采纳的权威时间；缺省表示全新输入，没有原 offset 豁免。
 * @returns 不改写输入的格式 schema。每个未改字段沿用精确原时刻；新输入仍使用唯一 IANA 解析。
 */
export function calendarSchemaFor(snapshot?: CalendarTimeSnapshot) {
  // 只复制不可变标量，避免后续原对象更新悄悄改变当前 Form 的校验基线。
  const saved = snapshot
    ? {
        starts_at: snapshot.starts_at,
        ends_at: snapshot.ends_at,
        timezone: snapshot.timezone,
        all_day: snapshot.all_day,
      }
    : undefined
  return z
    .object({
      title: z
        .string()
        .refine((value) => value.trim().length > 0, '请填写日程标题。'),
      starts_at: z.string(),
      ends_at: z.string(),
      all_day: z.boolean(),
      timezone: z.string().refine(validTimezone, '请输入有效的 IANA 时区。'),
      attendees: z.string().superRefine((value, context) => {
        try {
          recipientList(value)
        } catch {
          context.addIssue({
            code: 'custom',
            message: '请填写有效的参会人邮箱地址，多个地址用逗号分隔。',
          })
        }
      }),
    })
    .superRefine((value, context) => {
      if (!validTimezone(value.timezone)) return
      const parsed: Partial<Record<'starts_at' | 'ends_at', number>> = {}
      for (const key of ['starts_at', 'ends_at'] as const) {
        try {
          if (value.all_day) {
            if (!validDate(value[key]))
              throw new Error('请填写有效的全天日期。')
            parsed[key] = Date.parse(`${value[key]}T00:00:00Z`)
          } else {
            const original = saved?.[key]
            const unchanged =
              original &&
              value.timezone === (saved.timezone ?? 'UTC') &&
              value.all_day === (saved.all_day ?? false) &&
              value[key] === instantToWall(original, value.timezone)
            // 与只读 hook 的 temporal 边界一致：明确 offset 已经消除歧义，不能再次猜测未改墙上时间。
            parsed[key] = Date.parse(
              unchanged ? original : wallToInstant(value[key], value.timezone),
            )
            if (!Number.isFinite(parsed[key]))
              throw new Error('请填写有效的日期和时间。')
          }
        } catch (cause) {
          context.addIssue({
            code: 'custom',
            path: [key],
            message:
              cause instanceof Error
                ? cause.message
                : '请填写有效的日期和时间。',
          })
        }
      }
      if (
        parsed.starts_at !== undefined &&
        parsed.ends_at !== undefined &&
        parsed.ends_at <= parsed.starts_at
      )
        context.addIssue({
          code: 'custom',
          path: ['ends_at'],
          message: '结束时间必须晚于开始时间；全天结束日期不包含当天。',
        })
    })
}
/** 无已保存上下文的独立格式入口，保留新输入的严格 DST 与先后校验。 */
export const calendarSchema = calendarSchemaFor()
/** @param snapshot 当前已采纳快照；只为 Form 提供格式上下文，保存与原精度回写仍归原 hook。 */
export function calendarResolverFor(snapshot: CalendarTimeSnapshot) {
  return zodResolver(calendarSchemaFor(snapshot))
}
