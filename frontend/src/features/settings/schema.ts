/** 工作设置的格式校验；不判断账户能力、区间重叠、版本或其他服务端领域规则。 */
import { z } from 'zod'
import { zodResolver } from '@primevue/forms/resolvers/zod'
import type { Weekday } from '@/api/types'

/** 星期顺序仅服务表单呈现，传输键及七日 tuple 结构沿用既有契约。 */
export const WEEKDAYS: ReadonlyArray<{ key: Weekday; label: string }> = [
  { key: 'monday', label: '星期一' },
  { key: 'tuesday', label: '星期二' },
  { key: 'wednesday', label: '星期三' },
  { key: 'thursday', label: '星期四' },
  { key: 'friday', label: '星期五' },
  { key: 'saturday', label: '星期六' },
  { key: 'sunday', label: '星期日' },
]
const wallTime = /^(?:[01]\d|2[0-3]):[0-5]\d$/
const zeroSecondTime = /^(?:[01]\d|2[0-3]):[0-5]\d:00$/

/** @returns 仅对真实零秒 GET 值做无损精度归一；其他输入原样交由格式校验。 */
export function normalizeBriefTime(value: string): string {
  return zeroSecondTime.test(value) ? value.substring(0, 5) : value
}

/** @returns 使用 Intl 的显式 IANA 校验结果，不读取或推断宿主默认时区。 */
function validTimezone(value: string): boolean {
  if (!value || /^[+-]/.test(value)) return false
  try {
    new Intl.DateTimeFormat('zh-CN', { timeZone: value })
    return true
  } catch {
    return false
  }
}

/** @param current 服务端当前值或用户明确输入。@returns 本机可枚举时区＋UTC＋合法当前值。 */
export function timezoneOptions(current: string): string[] {
  const available =
    typeof Intl.supportedValuesOf === 'function'
      ? Intl.supportedValuesOf('timeZone')
      : []
  return [
    ...new Set([
      'UTC',
      ...available,
      ...(validTimezone(current) ? [current] : []),
    ]),
  ]
}
const intervals = z.array(z.tuple([z.string(), z.string()]))
const workingHours = z
  .object({
    monday: intervals,
    tuesday: intervals,
    wednesday: intervals,
    thursday: intervals,
    friday: intervals,
    saturday: intervals,
    sunday: intervals,
  })
  .strict()
  .superRefine((hours, context) => {
    // 整周是一个 Form 复合字段：增删区间不留下已卸载字段。错误带星期／序号，关联整个区间组。
    for (const day of WEEKDAYS)
      hours[day.key].forEach(([start, end], index) => {
        const message =
          !wallTime.test(start) || !wallTime.test(end)
            ? '请用 HH:mm 填写有效的开始和结束时间。'
            : start >= end
              ? '每个工作区间的结束时间必须晚于开始时间。'
              : null
        if (message)
          context.addIssue({
            code: 'custom',
            message: `${day.label}区间 ${index + 1}：${message}`,
          })
      })
  })
const retention = z
  .number({ error: '请输入 1–3650 天的整数。' })
  .int('请输入 1–3650 天的整数。')
  .min(1, '请输入 1–3650 天的整数。')
  .max(3650, '请输入 1–3650 天的整数。')

/** 与完整快照兼容，只输出已有可编辑字段；updated_at 仍由原 save 白名单排除。 */
export const settingsSchema = z.object({
  timezone: z.string().refine(validTimezone, '请输入有效的 IANA 时区。'),
  locale: z
    .string()
    .max(16, '请输入有效的语言标签（最长 16 字符）。')
    .regex(
      /^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,2}$/,
      '请输入有效的语言标签，例如 zh-CN。',
    ),
  brief_time: z
    .string()
    .refine(
      (value) => wallTime.test(normalizeBriefTime(value)),
      '请用 HH:mm 填写简报时间；原值含秒或小数秒时，请明确重新选择分钟精度。',
    )
    .transform(normalizeBriefTime),
  email_body_retention_days: retention,
  source_metadata_retention_days: retention,
  workspace_history_retention_days: retention,
  default_mail_connection_id: z.string().nullable(),
  default_calendar_connection_id: z.string().nullable(),
  default_calendar_id: z.string().nullable(),
  working_hours: workingHours,
  meeting_buffer_minutes: z
    .number({ error: '会议缓冲必须是 0–120 分钟的整数。' })
    .int('会议缓冲必须是 0–120 分钟的整数。')
    .min(0, '会议缓冲必须是 0–120 分钟的整数。')
    .max(120, '会议缓冲必须是 0–120 分钟的整数。'),
})

/** 真实 PrimeVue Form resolver；字段错误来自 zod，服务端错误另经只读码映射。 */
export const settingsResolver = zodResolver(settingsSchema)
