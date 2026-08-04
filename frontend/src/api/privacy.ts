import { requestJson } from './client'
import { asJsonObject } from './types'

/** 隐私删除请求的异步任务回执；202 只代表任务已持久化。 */
export interface PrivacyDeletionResponse {
  task_id: string
  status: string
}

function parseDeletionResponse(value: unknown): PrivacyDeletionResponse {
  const object = asJsonObject(value)
  if (!object || typeof object.task_id !== 'string' || typeof object.status !== 'string')
    throw new Error('Invalid privacy deletion response')
  return { task_id: object.task_id, status: object.status }
}

/**
 * 请求删除当前用户的来源缓存。
 *
 * @param idempotencyKey 客户端生成的重放保护键。
 * @returns 服务端返回的持久任务标识。
 */
export function requestSourceCacheDeletion(idempotencyKey: string): Promise<PrivacyDeletionResponse> {
  return requestJson('/privacy/source-cache-deletions', parseDeletionResponse, {
    method: 'POST',
    headers: { 'Idempotency-Key': idempotencyKey },
    body: '{}',
  })
}

/**
 * 请求删除当前用户全部数据；精确确认短语在浏览器边界再次校验。
 *
 * @param confirmation 必须严格等于 `DELETE ALL DATA`。
 * @param idempotencyKey 客户端生成的重放保护键。
 * @returns 服务端返回的持久任务标识。
 * @throws Error 当确认短语不匹配时，不发起网络请求。
 */
export function requestAllDataDeletion(confirmation: string, idempotencyKey: string): Promise<PrivacyDeletionResponse> {
  if (confirmation !== 'DELETE ALL DATA') throw new Error('DELETE ALL DATA confirmation required')
  return requestJson('/privacy/all-data-deletions', parseDeletionResponse, {
    method: 'POST',
    headers: { 'Idempotency-Key': idempotencyKey },
    body: JSON.stringify({ confirmation }),
  })
}
