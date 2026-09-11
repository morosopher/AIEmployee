import type { Page, Route } from '@playwright/test'
import type { ActionSnapshot } from '../../src/api/types'
import {
  connection,
  connectionCapabilities,
} from '../../src/test-support/editorFixtures'
import { TASK_ID } from '../../src/test-support/actionFixtures'

/** 浏览器仅观察合成 UI/HTTP 契约；请求记录留在测试内存，不输出正文或身份凭据。 */
export interface EditorMutation {
  path: string
  method: string
  payload: unknown
  intent: string | null
}

/** @param route 合成请求。@param value 完整公开投影。@param status HTTP 状态。 */
export async function editorJson(
  route: Route,
  value: unknown,
  status = 200,
): Promise<void> {
  await route.fulfill({
    status,
    contentType: 'application/json',
    headers: { 'Cache-Control': 'no-store' },
    body: JSON.stringify(value),
  })
}

/** @param route 合成请求。@param code 稳定错误码。@param status HTTP 状态。 */
export async function editorProblem(
  route: Route,
  code: string,
  status = 409,
): Promise<void> {
  await route.fulfill({
    status,
    contentType: 'application/problem+json',
    headers: { 'Cache-Control': 'no-store' },
    body: JSON.stringify({
      type: 'about:blank',
      title: 'Synthetic request rejected',
      status,
      detail: '',
      instance: '',
      error_code: code,
      trace_id: 'synthetic-editor-trace',
    }),
  })
}

/**
 * 安装合成公开认证与双供应商目录；所有未声明 API 均固定失败，不能遗漏后落到真实外部调用。
 * @param page 当前独立浏览器上下文。
 * @returns 内存内 mutation、未声明 API 和脚本错误计数，便于断言没有隐式提交。
 */
export async function editorWorkspace(page: Page) {
  const mutations: EditorMutation[] = [],
    unexpected: string[] = [],
    pageErrors: number[] = []
  page.on('pageerror', () => pageErrors.push(1))
  page.on('request', (request) => {
    const url = new URL(request.url())
    if (
      !url.pathname.startsWith('/api/v1/') ||
      !['POST', 'PATCH', 'PUT', 'DELETE'].includes(request.method())
    )
      return
    mutations.push({
      path: url.pathname.replace('/api/v1', ''),
      method: request.method(),
      payload: request.postData() ? (request.postDataJSON() as unknown) : null,
      intent: request.headers()['idempotency-key'] ?? null,
    })
  })
  await page.route('**/api/v1/**', async (route) => {
    unexpected.push(
      `${route.request().method()} ${new URL(route.request().url()).pathname}`,
    )
    await editorProblem(route, 'undeclared_synthetic_api', 500)
  })
  await page.route('**/api/v1/auth/me', (route) =>
    editorJson(route, {
      id: '00000000-0000-0000-0000-000000000101',
      email: 'browser@example.test',
      display_name: '合成管理员',
      timezone: 'Asia/Shanghai',
      locale: 'zh-CN',
      brief_time: '08:00:00',
    }),
  )
  await page.route('**/api/v1/system/alerts', (route) =>
    editorJson(route, { alerts: [] }),
  )
  await page.route('**/api/v1/actions', (route) =>
    editorJson(route, { items: [], limit: 50, offset: 0 }),
  )
  await page.route('**/api/v1/connections', (route) =>
    editorJson(route, [connection(), connection('microsoft')]),
  )
  for (const provider of ['google', 'microsoft'] as const) {
    await page.route(
      `**/api/v1/connections/${connection(provider).id}/capabilities`,
      (route) => editorJson(route, connectionCapabilities(provider)),
    )
  }
  return { mutations, unexpected, pageErrors }
}

/**
 * 同一 action/task 使用完整权威快照；原生 SSE 仅给空心跳，不伪造写入成功。
 * @param page 当前浏览器。
 * @param read 每次 REST 读取时返回的合成快照，null 表示尚未提交。
 */
export async function editorAction(
  page: Page,
  read: () => ActionSnapshot | null,
): Promise<void> {
  await page.route('**/api/v1/actions', (route) => {
    const snapshot = read()
    const items = snapshot
      ? [
          {
            id: TASK_ID,
            item_kind: 'trusted_task',
            task_id: TASK_ID,
            editor_url: null,
            status: snapshot.status,
            action: snapshot.action,
            provider: snapshot.provider,
            risk_level: snapshot.approval?.risk_level ?? null,
            created_at: snapshot.created_at,
            updated_at: snapshot.updated_at,
          },
        ]
      : []
    return editorJson(route, { items, limit: 50, offset: 0 })
  })
  await page.route(`**/api/v1/actions/${TASK_ID}`, (route) => {
    const snapshot = read()
    return snapshot
      ? editorJson(route, snapshot)
      : editorProblem(route, 'action_not_found', 404)
  })
  await page.route(`**/api/v1/tasks/${TASK_ID}`, (route) => {
    const snapshot = read()
    return snapshot
      ? editorJson(route, {
          id: TASK_ID,
          kind: 'trusted_action',
          status: snapshot.status,
          retry_of_task_id: null,
          error_code: snapshot.error_code,
          event_cursor: snapshot.event_cursor,
          steps: [],
        })
      : editorProblem(route, 'task_not_found', 404)
  })
  await page.route(`**/api/v1/tasks/${TASK_ID}/events*`, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: 'retry: 60000\n\nevent: heartbeat\ndata: {}\n\n',
    }),
  )
}
