/** 安全映射预期独立手写，真实七字段 Problem Details 只投影错误码。 */
import { describe, expect, it } from 'vitest'
import type { ProblemDetails } from '@/api/types'
import { FORM_ERROR_MESSAGES, problemToFormError } from './problemFields'

const expected: Record<string, string> = {
  request_validation_failed: '输入未通过校验，请检查字段后重试。',
  connection_capability_disabled: '连接能力不可用，请重新授权并核对设置。',
  user_inactive: '当前用户不可用，请重新登录后核对。',
  mail_recipient_limit_exceeded: '收件人数量超过允许范围，请调整后重试。',
  mail_draft_not_found: '邮件草稿不存在或已不可访问，请重新加载后核对。',
  draft_version_conflict: '邮件草稿版本已变化，请重新加载后核对。',
  mail_draft_not_editable: '邮件草稿当前不可编辑，请重新加载并核对版本。',
  mail_draft_approval_withdrawal_required: '请先撤回邮件审批，再修改草稿。',
  mail_draft_result_confirmation_required: '请先确认邮件操作结果，再继续。',
  mail_thread_binding_conflict: '回复目标已变化，请核对原邮件会话。',
  mail_draft_binding_immutable: '草稿账户与回复目标不可更换，请新建草稿。',
  mail_draft_content_unavailable: '邮件草稿内容已不可用，请重新加载后核对。',
  calendar_proposal_time_required: '请完整填写日程开始和结束时间。',
  calendar_notification_policy_required: '请明确选择日程通知方式。',
  calendar_confirmation_not_required:
    '当前提案不需要此确认，请重新加载后核对。',
  calendar_proposal_not_found: '日程提案不存在或已不可访问，请重新加载后核对。',
  proposal_version_conflict: '日程提案版本已变化，请重新加载后核对。',
  calendar_proposal_not_editable:
    '日程提案当前不可编辑，请重新加载并核对版本。',
  calendar_proposal_binding_immutable: '提案账户与日历不可更换，请新建提案。',
  calendar_read_only: '所选日历为只读，请检查连接与日历权限。',
  calendar_notification_mapping_unsupported:
    '当前日历不支持所选通知方式，请重新选择。',
  calendar_event_version_conflict: '日程已被修改，请重新加载并核对最新版本。',
  calendar_recurring_event_unsupported:
    '当前仅支持非重复日程，请核对目标日程。',
  calendar_snapshot_unavailable: '日程修改前快照不可用，请重新加载后核对。',
  connection_scope_missing: '连接缺少所需权限，请重新授权并核对设置。',
  historical_action_binding_unavailable:
    '历史操作的账户归属无法核实，不能更换此对象的账户或日历。请显式新建空白草稿或提案。',
  idempotency_key_payload_mismatch:
    '本次请求内容与原请求不一致，请重新加载后核对。',
  external_writes_disabled: '真实外部写入尚未启用，请核对配置。',
  provider_action_unavailable: '当前供应商操作不可用，请检查连接能力。',
  trusted_action_unavailable: '当前操作已不可用，请重新加载后核对。',
  authentication_required: '请重新登录后继续。',
  csrf_rejected: '安全校验未通过，请刷新页面后重试。',
}

describe('problemToFormError', () => {
  it('freezes the verified finite mapping and maps every known code without using server prose', () => {
    expect(Object.isFrozen(FORM_ERROR_MESSAGES)).toBe(true)
    expect(FORM_ERROR_MESSAGES).toEqual(expected)
    for (const [error_code, message] of Object.entries(expected)) {
      const problem: ProblemDetails = {
        type: 'about:blank',
        title: '<img src=x>',
        status: 422,
        detail: 'synthetic private diagnostic',
        instance: '/settings',
        error_code,
        trace_id: 'safe-trace',
      }
      expect(problemToFormError(problem)).toBe(message)
      expect(problemToFormError({ error_code })).toBe(message)
    }
  })
  it.each([
    'internal_error',
    'future_code',
    'constructor',
    '__proto__',
    'toString',
  ])('falls back safely for %s', (error_code) => {
    expect(problemToFormError({ error_code })).toBe(
      '请求失败，请核对输入后重试。',
    )
  })
})
