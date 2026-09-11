/**
 * 本地邮件草稿 REST 边界：严格解析版本与状态，不直接操作供应商草稿箱。创建、生成与提交显式携带同一意图的幂等键；所有敏感响应只在调用方内存中驻留。
 */
import { requestJson } from './client'
import { requestCreation, type RequestIntent } from './requestIntent'
import type { AcceptedActionTask, MailDraft, MailMode } from './types'
import * as v from './validation'

export const mailModes = ['new', 'reply', 'reply_all'] as const
export const mailDraftStatuses = [
  'editing',
  'awaiting_approval',
  'executing',
  'sent',
  'needs_attention',
  'cancelled',
] as const

/** 创建本地草稿的受控输入；回复来源与权限最终由服务端验证。 */
export interface CreateMailDraftInput {
  mode: MailMode
  connection_id?: string | null
  source_thread_id?: string | null
  source_message_id?: string | null
  to?: string[]
  cc?: string[]
  bcc?: string[]
  subject?: string
  body_text?: string
}
export interface UpdateMailDraftInput {
  version: number
  /** 仅 new/editing 可显式重绑；回复字段必须省略，禁止 null。 */
  connection_id?: string
  to?: string[]
  cc?: string[]
  bcc?: string[]
  subject?: string
  body_text?: string
}

/**
 * @param value 不可信完整草稿响应。
 * @returns 严格当前版本。
 * @throws Error 字段或枚举不合法。
 */
export function parseMailDraft(value: unknown): MailDraft {
  return v.boundary(
    value,
    (input) => {
      const o = v.record(input)
      return {
        id: v.uuid(o.id),
        connection_id: v.uuid(o.connection_id),
        mode: v.enumeration(o.mode, mailModes),
        source_thread_id: v.nullable(o.source_thread_id, v.text),
        source_message_id: v.nullable(o.source_message_id, v.text),
        version: v.integer(o.version, 1),
        status: v.enumeration(o.status, mailDraftStatuses),
        to: v.array(o.to, v.text),
        cc: v.array(o.cc, v.text),
        bcc: v.array(o.bcc, v.text),
        subject: v.text(o.subject),
        body_text: v.text(o.body_text),
        prompt_version: v.nullable(o.prompt_version, v.text),
        model_name: v.nullable(o.model_name, v.text),
        retain_until: v.nullable(o.retain_until, v.timestamp),
        created_at: v.nullable(o.created_at, v.timestamp),
        recipient_suggestions: v.array(o.recipient_suggestions, v.text),
      }
    },
    'Invalid mail draft',
  )
}

/**
 * @param value 长任务创建响应。
 * @returns 仅含排队事实，不代表动作已执行。
 */
export function parseAcceptedActionTask(value: unknown): AcceptedActionTask {
  const o = v.record(value)
  return {
    task_id: v.uuid(o.task_id),
    status: v.enumeration(o.status, ['queued']),
  }
}

/**
 * @param limit 页大小。
 * @param offset 偏移量。
 * @returns 当前用户草稿页；禁用浏览器缓存。
 */
export function listMailDrafts(limit = 50, offset = 0) {
  return requestJson(
    `/mail/drafts?limit=${v.integer(limit, 1)}&offset=${v.integer(offset)}`,
    (value) => v.page(value, parseMailDraft),
    { cache: 'no-store' },
  )
}
/**
 * @param id 当前用户草稿 ID。
 * @returns 与请求身份一致的当前版本。
 */
export function getMailDraft(id: string): Promise<MailDraft> {
  return requestJson(
    `/mail/drafts/${encodeURIComponent(id)}`,
    (value) => boundDraft(value, id),
    { cache: 'no-store' },
  )
}
/**
 * @param input 本地创建字段。
 * @param intent 同一请求的稳定意图。
 * @returns 服务端创建的草稿。
 */
export function createMailDraft(
  input: CreateMailDraftInput,
  intent: RequestIntent,
): Promise<MailDraft> {
  return requestCreation(
    '/mail/drafts',
    parseMailDraft,
    intent,
    JSON.stringify(input),
  )
}
/**
 * @param id 草稿 ID。
 * @param input 当前版本与变更。
 * @returns 新的不可变版本视图。
 */
export function updateMailDraft(
  id: string,
  input: UpdateMailDraftInput,
): Promise<MailDraft> {
  return requestJson(
    `/mail/drafts/${encodeURIComponent(id)}`,
    (value) => boundDraft(value, id),
    { method: 'PATCH', cache: 'no-store', body: JSON.stringify(input) },
  )
}
/**
 * @param id 本地草稿 ID。
 * @returns 服务端取消后的对象；不触碰供应商草稿箱。
 */
export function cancelMailDraft(id: string): Promise<MailDraft> {
  return requestJson(
    `/mail/drafts/${encodeURIComponent(id)}`,
    (value) => boundDraft(value, id),
    { method: 'DELETE', cache: 'no-store' },
  )
}
/**
 * @param id 草稿 ID。
 * @param input 版本和模型草拟指令。
 * @param intent 稳定意图。
 * @returns 异步任务。
 */
export function generateMailDraft(
  id: string,
  input: { version: number; instruction: string },
  intent: RequestIntent,
): Promise<AcceptedActionTask> {
  return requestCreation(
    `/mail/drafts/${encodeURIComponent(id)}/generate`,
    parseAcceptedActionTask,
    intent,
    JSON.stringify(input),
  )
}
/**
 * @param id 草稿 ID。
 * @param version 精确当前版本。
 * @param intent 稳定意图。
 * @returns 等待可信执行的任务标识。
 */
export function submitMailDraft(
  id: string,
  version: number,
  intent: RequestIntent,
): Promise<AcceptedActionTask> {
  return requestCreation(
    `/mail/drafts/${encodeURIComponent(id)}/submit`,
    parseAcceptedActionTask,
    intent,
    JSON.stringify({ version }),
  )
}
/** 防止代理/服务端串错对象时把其他草稿写入当前编辑投影。 */
function boundDraft(value: unknown, id: string): MailDraft {
  const result = parseMailDraft(value)
  if (result.id !== id) throw new Error('Invalid mail draft identity')
  return result
}
