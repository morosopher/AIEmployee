/**
 * 创建类请求共享的内存意图边界。幂等键独立于敏感内容生成，由调用者跨并发和运输失败持有；客户端不自动重放未知写结果。
 */
import { requestJson } from './client'

/** 一个用户创建意图的稳定随机键；调用方在结果明确后为新意图创建新对象。 */
export interface RequestIntent {
  readonly key: string
}

/**
 * 创建只驻留内存的意图。运输失败后必须重用该对象，不从内容或当前时间推导幂等键。
 * @returns 可跨并发点击、断线重试复用的意图。
 */
export function createRequestIntent(): RequestIntent {
  return Object.freeze({ key: crypto.randomUUID() })
}

/**
 * 在统一 Cookie/CSRF 边界发送一次创建请求；本方法不自动重放未知结果。
 * @param path 受控 API 路径。
 * @param parse 真实响应验证器。
 * @param intent 调用方持有到结果明确的同一用户意图。
 * @param body 已类型化且只在请求内存驻留的序列化内容。
 * @returns 已验证的服务端结果；失败原样交给调用方保留意图。
 */
export function requestCreation<T>(
  path: string,
  parse: (value: unknown) => T,
  intent: RequestIntent,
  body?: string,
): Promise<T> {
  return requestJson(path, parse, {
    method: 'POST',
    cache: 'no-store',
    headers: { 'Idempotency-Key': intent.key },
    body,
  })
}
