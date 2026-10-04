import { describe, expect, it } from 'vitest'
import { ProblemError } from '@/api/client'
import type { ProblemDetails } from '@/api/types'
import { EditorInputError } from './editorInput'
import { actionRecovery } from './recovery'

/** 合成完整 Problem Details，原始文案只用于验证最小投影不会携带它们。 */
function problem(error_code: string, status: number): ProblemDetails {
  return {
    type: 'about:blank',
    title: 'Synthetic private title',
    detail: 'Synthetic private detail',
    instance: '/synthetic/problem',
    status,
    error_code,
    trace_id: 'synthetic-recovery-trace',
  }
}

describe('actionRecovery', () => {
  // 手工列出原有各分支和优先级；新增展示码不能改变恢复动作或固定安全文案。
  it.each([
    {
      code: 'historical_action_binding_unavailable',
      status: 409,
      message:
        '历史操作的账户归属无法核实，不能更换此对象的账户或日历。请显式新建空白草稿或提案。',
      action: 'new_object',
    },
    {
      code: 'connection_capability_expired',
      status: 422,
      message: '连接能力不可用，请重新授权并核对设置。',
      action: 'reauthorize',
    },
    {
      code: 'calendar_event_version_conflict',
      status: 409,
      message: '原版本已不可使用，请重新加载并创建新版本。',
      action: 'new_version',
    },
    {
      code: 'calendar_etag_stale',
      status: 409,
      message: '原版本已不可使用，请重新加载并创建新版本。',
      action: 'new_version',
    },
    {
      code: 'version_conflict',
      status: 409,
      message: '版本或状态已变化，请重新加载后核对。',
      action: 'reload',
    },
    {
      code: 'validation_error',
      status: 422,
      message: '输入未通过校验，请检查字段后重试。',
      action: 'retry',
    },
    {
      code: 'synthetic_unknown_error',
      status: 503,
      message: '请求失败，请重试。',
      action: 'retry',
    },
  ])('preserves recovery and only copies the code for $code', (testCase) => {
    const source = problem(testCase.code, testCase.status)
    const recovery = actionRecovery(new ProblemError(source))

    expect(recovery).toEqual({
      message: testCase.message,
      action: testCase.action,
      traceId: 'synthetic-recovery-trace',
      problem: { error_code: testCase.code },
    })
    expect(Object.keys(recovery.problem ?? {})).toEqual(['error_code'])
    // 服务端响应对象之后被改动也不能改写当前 UI 持有的错误码副本。
    source.error_code = 'synthetic_changed_after_recovery'
    expect(recovery.problem).toEqual({ error_code: testCase.code })
  })

  it.each([
    new Error('Synthetic private exception'),
    new EditorInputError('请填写有效的邮箱地址，多个地址用逗号分隔。'),
    { problem: problem('validation_error', 422) },
    Object.assign(new Error('Synthetic disguised exception'), {
      problem: problem('validation_error', 422),
    }),
    null,
  ])('does not fabricate problem metadata for non-ProblemError %s', (cause) => {
    expect(actionRecovery(cause)).toEqual({
      message: '请求失败，请重试。',
      traceId: null,
      action: 'retry',
    })
  })
})
