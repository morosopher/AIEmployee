import { requestJson } from './client'
import { asJsonObject } from './types'

/** 用户范围系统告警，禁止携带来源正文或供应商响应。 */
export interface SystemAlert {
  code: 'daily_brief_overdue'
  severity: 'critical'
  local_date: string
  diagnostic_task_id: string | null
}

export interface SystemAlertsResponse { alerts: SystemAlert[] }

function parseAlert(value: unknown): SystemAlert {
  const object = asJsonObject(value)
  if (!object || object.code !== 'daily_brief_overdue' || object.severity !== 'critical' || typeof object.local_date !== 'string' || (object.diagnostic_task_id !== null && typeof object.diagnostic_task_id !== 'string'))
    throw new Error('Invalid system alert response')
  return { code: object.code, severity: object.severity, local_date: object.local_date, diagnostic_task_id: object.diagnostic_task_id }
}

/** 获取当前认证用户的系统告警快照。 */
export function getSystemAlerts(): Promise<SystemAlertsResponse> {
  return requestJson('/system/alerts', (value) => {
    const object = asJsonObject(value)
    if (!object || !Array.isArray(object.alerts)) throw new Error('Invalid system alerts response')
    return { alerts: object.alerts.map(parseAlert) }
  })
}
