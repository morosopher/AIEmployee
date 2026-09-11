/**
 * 连接与能力的公开 REST 投影，严格区分 Google/Microsoft 及四项渐进能力。授权 URL 只允许对应 HTTPS 主机；同步保留未完成意图，任何客户端状态都不能扩大服务端权限。
 */
import { requestJson } from './client'
import {
  createRequestIntent,
  requestCreation,
  type RequestIntent,
} from './requestIntent'
import type {
  ActionProvider,
  CapabilityName,
  Connection,
  ConnectionCapabilities,
  ConnectionCapability,
  ProviderCalendar,
} from './types'
import * as v from './validation'

export const actionProviders = ['google', 'microsoft'] as const
export const capabilityNames = [
  'mail.read',
  'mail.send',
  'calendar.read',
  'calendar.write',
] as const
const capabilityStatuses = [
  'disabled',
  'authorizing',
  'enabled',
  'degraded',
  'action_required',
  'revoked',
] as const
const syncIntents = new Map<string, RequestIntent>()

/**
 * @param value 不可信连接响应。
 * @returns 封闭供应商和状态枚举，不包含凭据。
 */
export function parseConnection(value: unknown): Connection {
  const o = v.record(value)
  return {
    id: v.uuid(o.id),
    provider: v.enumeration(o.provider, actionProviders),
    account_email: v.text(o.account_email),
    scopes: v.array(o.scopes, v.text),
    status: v.enumeration(o.status, [
      'connecting',
      'connected',
      'degraded',
      'expired',
      'disconnected',
    ]),
    last_error_code: v.nullable(o.last_error_code, v.text),
  }
}
/**
 * @param value 能力响应。
 * @returns 服务端验证的 scope 和能力状态。
 */
export function parseConnectionCapability(
  value: unknown,
): ConnectionCapability {
  const o = v.record(value)
  return {
    capability: v.enumeration(o.capability, capabilityNames),
    status: v.enumeration(o.status, capabilityStatuses),
    actual_scopes: v.array(o.actual_scopes, v.text),
    last_verified_at: v.nullable(o.last_verified_at, v.timestamp),
    last_error_code: v.nullable(o.last_error_code, v.text),
  }
}
/**
 * @param value 规范日历目录项。
 * @returns 只含选择日历所需字段，不传递供应商 SDK 类型。
 */
export function parseProviderCalendar(value: unknown): ProviderCalendar {
  const o = v.record(value)
  return {
    id: v.text(o.id),
    name: v.text(o.name),
    timezone: v.text(o.timezone),
    is_primary: v.boolean(o.is_primary),
    access_role: v.text(o.access_role),
    can_write: v.boolean(o.can_write),
    provider_url: v.nullable(o.provider_url, v.text),
  }
}
/**
 * @param value 完整连接能力响应。
 * @returns 去除未知字段的四能力投影。
 */
export function parseConnectionCapabilities(
  value: unknown,
): ConnectionCapabilities {
  const o = v.record(value)
  const capabilities = v.array(o.capabilities, parseConnectionCapability)
  if (
    capabilities.length !== 4 ||
    new Set(capabilities.map((item) => item.capability)).size !== 4
  )
    throw new Error('Invalid connection capabilities')
  return {
    connection_id: v.uuid(o.connection_id),
    provider: v.enumeration(o.provider, actionProviders),
    capabilities,
    provider_calendars: v.array(o.provider_calendars, parseProviderCalendar),
  }
}
/**
 * @returns 当前用户两供应商的公开连接列表。
 */
export function listConnections(): Promise<Connection[]> {
  return requestJson(
    '/connections',
    (value) => v.array(value, parseConnection),
    { cache: 'no-store' },
  )
}
/**
 * @returns Google 初始读取授权地址；不隐式申请写权限。
 */
export function startGoogleConnection() {
  return requestJson(
    '/connections/google/start',
    (value) => parseAuthorization(value, 'google'),
    { method: 'POST', cache: 'no-store' },
  )
}
/**
 * @param capabilities 显式初始读取子集。
 * @returns Microsoft 委托授权地址。
 */
export function startMicrosoftConnection(
  capabilities?: Array<'mail.read' | 'calendar.read'>,
) {
  return requestJson(
    '/connections/microsoft/start',
    (value) => parseAuthorization(value, 'microsoft'),
    {
      method: 'POST',
      cache: 'no-store',
      body: JSON.stringify(capabilities ? { capabilities } : {}),
    },
  )
}
/**
 * @param id 连接 ID。
 * @returns 四项能力和日历目录。
 */
export function getConnectionCapabilities(
  id: string,
): Promise<ConnectionCapabilities> {
  return requestJson(
    `/connections/${encodeURIComponent(id)}/capabilities`,
    (value) => {
      const result = parseConnectionCapabilities(value)
      if (result.connection_id !== id)
        throw new Error('Invalid connection identity')
      return result
    },
    { cache: 'no-store' },
  )
}
/**
 * @param id 连接 ID。
 * @param capability 单项能力。
 * @param provider 已知供应商。
 * @returns 渐进授权地址及依赖闭包。
 */
export function enableConnectionCapability(
  id: string,
  capability: CapabilityName,
  provider: ActionProvider,
) {
  return requestJson(
    `/connections/${encodeURIComponent(id)}/capabilities/${encodeURIComponent(capability)}/enable`,
    (value) => {
      const o = v.record(value)
      return {
        ...parseAuthorization(value, provider),
        requested_capabilities: v.array(o.requested_capabilities, (item) =>
          v.enumeration(item, capabilityNames),
        ),
      }
    },
    { method: 'POST', cache: 'no-store' },
  )
}
/**
 * @param id 连接 ID。
 * @param capability 本地关闭能力。
 * @returns 服务端明确的能力状态。
 */
export function disableConnectionCapability(
  id: string,
  capability: CapabilityName,
) {
  return requestJson(
    `/connections/${encodeURIComponent(id)}/capabilities/${encodeURIComponent(capability)}/disable`,
    (value) => {
      const o = v.record(value)
      const result = {
        capability: v.enumeration(o.capability, capabilityNames),
        status: v.enumeration(o.status, capabilityStatuses),
      }
      if (result.capability !== capability)
        throw new Error('Invalid capability identity')
      return result
    },
    { method: 'POST', cache: 'no-store' },
  )
}
/**
 * 手动同步在并发点击和运输失败后复用同一意图；只在服务端返回任务标识后释放。
 * @param id 连接 ID。
 * @param intent 可选显式意图；旧 M1 调用使用按连接隔离的未完成意图。
 * @returns 两个异步同步任务，字段名保持服务端兼容契约。
 */
export async function syncConnection(id: string, intent?: RequestIntent) {
  const current = intent ?? syncIntents.get(id) ?? createRequestIntent()
  if (!intent) syncIntents.set(id, current)
  const result = await requestCreation(
    `/connections/${encodeURIComponent(id)}/sync`,
    (value) => {
      const o = v.record(value)
      return {
        gmail_task_id: v.uuid(o.gmail_task_id),
        calendar_task_id: v.uuid(o.calendar_task_id),
      }
    },
    current,
  )
  if (syncIntents.get(id) === current) syncIntents.delete(id)
  return result
}
/**
 * @param id 连接 ID。
 * @returns 本地断开完成，不访问供应商用户数据。
 */
export async function disconnectConnection(id: string): Promise<null> {
  const result = await requestJson(
    `/connections/${encodeURIComponent(id)}`,
    () => null,
    { method: 'DELETE', cache: 'no-store' },
  )
  syncIntents.delete(id)
  return result
}
/** OAuth 跳转仅允许对应供应商的 HTTPS 授权主机，拒绝任意脚本/跨站入口。 */
function parseAuthorization(
  value: unknown,
  provider: ActionProvider,
): { authorization_url: string } {
  const authorization_url = v.text(v.record(value).authorization_url)
  const url = new URL(authorization_url)
  const allowedHost =
    provider === 'google' ? 'accounts.google.com' : 'login.microsoftonline.com'
  if (
    url.protocol !== 'https:' ||
    url.hostname !== allowedHost ||
    url.username ||
    url.password ||
    url.port
  )
    throw new Error('Invalid authorization URL')
  return { authorization_url }
}
