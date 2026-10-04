/** 日程表单仅校验格式，原 hook 继续管理确认、冲突、版本及精确 API 输入。 */
import { z } from 'zod'
import { zodResolver } from '@primevue/forms/resolvers/zod'
import { recipientList } from '@/features/actions/editorInput'
import { wallToInstant } from './time'

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
/** 原字符串不 trim/transform 回写；全天采用排他结束日期，定时沿用既有唯一 IANA 时刻解析。 */
export const calendarSchema = z
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
    const parsed: Partial<Record<'starts_at' | 'ends_at', string>> = {}
    for (const key of ['starts_at', 'ends_at'] as const) {
      try {
        if (value.all_day) {
          if (!validDate(value[key])) throw new Error('请填写有效的全天日期。')
          parsed[key] = value[key]
        } else parsed[key] = wallToInstant(value[key], value.timezone)
      } catch (cause) {
        context.addIssue({
          code: 'custom',
          path: [key],
          message:
            cause instanceof Error ? cause.message : '请填写有效的日期和时间。',
        })
      }
    }
    if (
      parsed.starts_at &&
      parsed.ends_at &&
      parsed.ends_at <= parsed.starts_at
    )
      context.addIssue({
        code: 'custom',
        path: ['ends_at'],
        message: '结束时间必须晚于开始时间；全天结束日期不包含当天。',
      })
  })
/** 真实 Form 使用同一格式 resolver；保存后续请求和错误仍归原 hook。 */
export const calendarResolver = zodResolver(calendarSchema)
