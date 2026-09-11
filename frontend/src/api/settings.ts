/**
 * 用户工作设置 REST 边界，完整读取七日工作时间和三个可空默认选择。PATCH 只发送调用方显式字段；响应严格解析并禁止缓存，领域有效性由服务端重新校验。
 */
import { requestJson } from './client'
import type { UserSettings, Weekday, WorkingHours } from './types'
import * as v from './validation'

const weekdays: readonly Weekday[] = [
  'monday',
  'tuesday',
  'wednesday',
  'thursday',
  'friday',
  'saturday',
  'sunday',
]
export type SettingsPatch = Partial<
  Omit<UserSettings, 'updated_at' | 'working_hours'>
> & { working_hours?: Partial<WorkingHours> }

/**
 * @param value 不可信完整设置。
 * @returns 包含七日工作时间和 M2 默认值的权威设置。
 */
export function parseSettings(value: unknown): UserSettings {
  return v.boundary(
    value,
    (input) => {
      const o = v.record(input)
      const working_hours = parseWorkingHours(o.working_hours)
      const meeting_buffer_minutes = v.integer(o.meeting_buffer_minutes)
      if (meeting_buffer_minutes > 120)
        throw new Error('Invalid meeting buffer')
      return {
        timezone: v.text(o.timezone),
        locale: v.text(o.locale),
        brief_time: v.text(o.brief_time),
        email_body_retention_days: v.integer(o.email_body_retention_days, 1),
        source_metadata_retention_days: v.integer(
          o.source_metadata_retention_days,
          1,
        ),
        workspace_history_retention_days: v.integer(
          o.workspace_history_retention_days,
          1,
        ),
        default_mail_connection_id: v.nullable(
          o.default_mail_connection_id,
          v.uuid,
        ),
        default_calendar_connection_id: v.nullable(
          o.default_calendar_connection_id,
          v.uuid,
        ),
        default_calendar_id: v.nullable(o.default_calendar_id, v.text),
        working_hours,
        meeting_buffer_minutes,
        updated_at: v.timestamp(o.updated_at),
      }
    },
    'Invalid settings',
  )
}
/**
 * @returns 当前认证用户完整工作设置。
 */
export function getSettings(): Promise<UserSettings> {
  return requestJson('/settings', parseSettings, { cache: 'no-store' })
}
/**
 * @param settings 已提供字段；只允许三个默认选择显式清空。
 * @returns 服务端返回的完整设置。
 */
export function updateSettings(settings: SettingsPatch): Promise<UserSettings> {
  return requestJson('/settings', parseSettings, {
    method: 'PATCH',
    cache: 'no-store',
    body: JSON.stringify(settings),
  })
}
/** 必须覆盖所有星期，拒绝遗漏、未知星期和非两端区间，避免静默误解工作日。 */
function parseWorkingHours(value: unknown): WorkingHours {
  const o = v.record(value)
  if (Object.keys(o).length !== 7) throw new Error('Invalid weekly intervals')
  const result: WorkingHours = {
    monday: [],
    tuesday: [],
    wednesday: [],
    thursday: [],
    friday: [],
    saturday: [],
    sunday: [],
  }
  for (const day of weekdays) {
    result[day] = v.array(o[day], (item) => {
      const pair = v.array(item, v.text)
      const start = pair[0],
        end = pair[1]
      if (
        pair.length !== 2 ||
        !start ||
        !end ||
        !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(start) ||
        !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(end) ||
        start >= end
      )
        throw new Error('Invalid working interval')
      return [start, end]
    })
  }
  return result
}
