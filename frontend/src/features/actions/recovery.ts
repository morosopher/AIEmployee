import { ProblemError } from '@/api/client'
import type { ProblemDetails } from '@/api/types'

/** 固定恢复提示附带可选的只读错误码投影；禁止携带供应商 detail 或原始异常。 */
export interface ActionRecovery {
  message: string
  traceId: string | null
  action: 'reload' | 'reauthorize' | 'new_version' | 'new_object' | 'retry'
  readonly problem?: Readonly<Pick<ProblemDetails, 'error_code'>>
}

/**
 * @param error 统一 API 客户端产生的异常。
 * @returns 可执行的恢复提示与真实 ProblemError 的最小错误码副本；普通异常不伪造码，
 * 不携带原始响应，不授予重发或再次批准权限。
 */
export function actionRecovery(error: unknown): ActionRecovery {
  if (!(error instanceof ProblemError))
    return { message: '请求失败，请重试。', traceId: null, action: 'retry' }
  const { error_code: code, trace_id: traceId, status } = error.problem
  // 只复制稳定码，避免保留完整 Problem Details 引用或让后续响应修改影响表单投影。
  const problem = { error_code: code }
  if (code === 'historical_action_binding_unavailable')
    return {
      message:
        '历史操作的账户归属无法核实，不能更换此对象的账户或日历。请显式新建空白草稿或提案。',
      traceId,
      action: 'new_object',
      problem,
    }
  if (
    /scope|capability|connection.*(?:disabled|disconnected)|consent/.test(code)
  )
    return {
      message: '连接能力不可用，请重新授权并核对设置。',
      traceId,
      action: 'reauthorize',
      problem,
    }
  if (
    code === 'calendar_event_version_conflict' ||
    /etag|expired|invalidated|payload_hash|not_editable/.test(code)
  )
    return {
      message: '原版本已不可使用，请重新加载并创建新版本。',
      traceId,
      action: 'new_version',
      problem,
    }
  if (status === 409)
    return {
      message: '版本或状态已变化，请重新加载后核对。',
      traceId,
      action: 'reload',
      problem,
    }
  if (status === 422)
    return {
      message: '输入未通过校验，请检查字段后重试。',
      traceId,
      action: 'retry',
      problem,
    }
  return { message: '请求失败，请重试。', traceId, action: 'retry', problem }
}
