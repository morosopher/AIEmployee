import {
  asJsonObject,
  parseTaskSnapshot,
  type AuthUser,
  type ProblemDetails,
  type TaskSnapshot,
} from './types'

const unsafeMethods = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

/** 统一承载 RFC 9457 失败，避免页面解析原始响应或敏感细节。 */
export class ProblemError extends Error {
  /**
   * @param problem 服务端公开的 Problem Details。
   */
  constructor(readonly problem: ProblemDetails) {
    super(problem.title)
    this.name = 'ProblemError'
  }
}

/**
 * 读取同源 CSRF Cookie；会话 Cookie 受 HttpOnly 保护，绝不在此读取。
 *
 * @returns 可用于 unsafe 方法的 CSRF 值，不存在时为空。
 */
function csrfToken(): string | null {
  const prefix = 'ai_employee_csrf='
  const cookie = document.cookie
    .split('; ')
    .find((item) => item.startsWith(prefix))
  return cookie ? decodeURIComponent(cookie.slice(prefix.length)) : null
}

/**
 * 验证并构建 Problem Details，兼容代理返回的非规范错误而不暴露其内容。
 *
 * @param response 失败的 Fetch 响应。
 * @returns 可安全显示和追踪的错误对象。
 */
async function problemFromResponse(
  response: Response,
): Promise<ProblemDetails> {
  const body: unknown = await response.json().catch(() => null)
  const object = asJsonObject(body)
  return {
    type: typeof object?.type === 'string' ? object.type : 'about:blank',
    title: typeof object?.title === 'string' ? object.title : 'Request failed',
    status:
      typeof object?.status === 'number' ? object.status : response.status,
    detail:
      typeof object?.detail === 'string' ? object.detail : 'Please try again.',
    instance: typeof object?.instance === 'string' ? object.instance : '',
    error_code:
      typeof object?.error_code === 'string'
        ? object.error_code
        : 'request_failed',
    trace_id:
      typeof object?.trace_id === 'string' ? object.trace_id : 'unavailable',
  }
}

/**
 * 通过 Cookie 会话发送并验证 JSON 请求，所有 REST 调用的唯一入口。
 *
 * @typeParam Result 已验证的成功响应类型。
 * @param path `/api/v1` 下的服务端路径。
 * @param parse 成功 JSON 的边界验证器。
 * @param init 额外请求选项；unsafe 方法自动附加 CSRF。
 * @returns 经验证的响应模型。
 * @throws ProblemError 当服务端返回非 2xx 响应时抛出。
 */
export async function requestJson<Result>(
  path: string,
  parse: (value: unknown) => Result,
  init: RequestInit = {},
): Promise<Result> {
  const method = (init.method ?? 'GET').toUpperCase()
  const headers = new Headers(init.headers)
  if (unsafeMethods.has(method)) {
    const token = csrfToken()
    if (token) headers.set('X-CSRF-Token', token)
  }
  if (init.body && !headers.has('Content-Type'))
    headers.set('Content-Type', 'application/json')
  const response = await fetch(`/api/v1${path}`, {
    ...init,
    method,
    headers,
    credentials: 'include',
  })
  if (!response.ok) throw new ProblemError(await problemFromResponse(response))
  if (response.status === 204) return parse(null)
  return parse((await response.json()) as unknown)
}

/**
 * 读取当前会话用户，401 由调用方转换为未登录状态。
 *
 * @returns 已验证的公开用户资料。
 */
export function getCurrentUser(): Promise<AuthUser> {
  return requestJson('/auth/me', parseAuthUser)
}

/**
 * 使用表单凭据建立安全 Cookie 会话；密码不被保存在 store 或浏览器存储。
 *
 * @param email 管理员邮箱。
 * @param password 仅用于本次请求的密码。
 * @returns 服务端创建会话后返回的公开用户资料。
 */
export function login(email: string, password: string): Promise<AuthUser> {
  return requestJson('/auth/login', parseAuthUser, {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  })
}

/**
 * 读取任务快照，为刷新和 SSE 间隙恢复提供持久事实来源。
 *
 * @param taskId 用户拥有的任务标识。
 * @returns 完整的任务投影。
 */
export function getTask(taskId: string): Promise<TaskSnapshot> {
  return requestJson(`/tasks/${encodeURIComponent(taskId)}`, parseTaskSnapshot)
}

/**
 * 请求协作式取消；前端仅使用服务端返回的新快照。
 *
 * @param taskId 要取消的任务标识。
 * @returns 服务端确认后的任务快照。
 */
export function cancelTask(taskId: string): Promise<TaskSnapshot> {
  return requestJson(
    `/tasks/${encodeURIComponent(taskId)}/cancel`,
    parseTaskSnapshot,
    { method: 'POST' },
  )
}

/**
 * 从失败任务创建 replacement，原任务保持不可变终态。
 *
 * @param taskId 失败任务标识。
 * @param idempotencyKey 本次重试请求的稳定客户端幂等键。
 * @returns replacement 任务快照。
 */
export function retryTask(
  taskId: string,
  idempotencyKey: string,
): Promise<TaskSnapshot> {
  return requestJson(
    `/tasks/${encodeURIComponent(taskId)}/retry`,
    parseTaskSnapshot,
    {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
    },
  )
}

/**
 * 收窄认证响应，避免未经验证的 API JSON 进入认证状态。
 *
 * @param value 外部 JSON。
 * @returns 公开用户资料。
 * @throws Error 当响应字段缺失时抛出。
 */
function parseAuthUser(value: unknown): AuthUser {
  const object = asJsonObject(value)
  if (
    !object ||
    typeof object.id !== 'string' ||
    typeof object.email !== 'string' ||
    typeof object.display_name !== 'string' ||
    typeof object.timezone !== 'string' ||
    typeof object.locale !== 'string' ||
    typeof object.brief_time !== 'string'
  ) {
    throw new Error('Invalid authentication response')
  }
  return {
    id: object.id,
    email: object.email,
    display_name: object.display_name,
    timezone: object.timezone,
    locale: object.locale,
    brief_time: object.brief_time,
  }
}
