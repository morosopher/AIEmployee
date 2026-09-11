import { expect, test, type Page, type Route } from '@playwright/test'
import type { ActionListItem, ActionSnapshot } from '../src/api/types'
import {
  actionItems,
  actionSnapshot,
  NOW,
  TASK_ID,
} from '../src/test-support/actionFixtures'

/** 本文件只验证 route fixture 的 UI/传输契约，不代表供应商执行或真实后端持久化。 */
const M2_EVENTS = [
  'action.submitted',
  'approval.invalidated',
  'tool.claimed',
  'tool.oauth_refresh_required',
  'tool.oauth_refresh_confirmed',
  'tool.reconciling',
  'tool.needs_attention',
  'tool.manually_resolved',
]
const RECOVERED_CURSOR = '9007199254740995'

/** @param page 当前浏览器页。 @returns 仅安装合成公开认证投影，不创建会话或凭据。 */
async function authenticateFixture(page: Page): Promise<void> {
  await page.route('**/api/v1/system/alerts', (route) =>
    fulfillJson(route, { alerts: [] }),
  )
  await page.route('**/api/v1/auth/me', (route) =>
    fulfillJson(route, {
      id: '00000000-0000-0000-0000-000000000101',
      email: 'admin@synthetic.example.test',
      display_name: '合成管理员',
      timezone: 'Asia/Shanghai',
      locale: 'zh-CN',
      brief_time: '08:00:00',
    }),
  )
}

/** @param route 被拦截请求。 @param value 合成响应。 @returns 带 no-store 的 JSON 响应。 */
async function fulfillJson(route: Route, value: unknown): Promise<void> {
  await route.fulfill({
    status: 200,
    contentType: 'application/json',
    headers: { 'Cache-Control': 'no-store' },
    body: JSON.stringify(value),
  })
}

/** @param name 持久事件名。 @param cursor 精确字符串游标。 @returns 原生 SSE 帧，不模拟 EventSource 类。 */
function persistentFrame(name: string, cursor: string): string {
  return [
    `id: ${cursor}`,
    `event: ${name}`,
    `data: ${JSON.stringify({
      id: cursor,
      sequence: cursor,
      task_id: TASK_ID,
      event: name,
      occurred_at: NOW,
      step_id: null,
      payload: { status: 'succeeded' },
    })}`,
    '',
    '',
  ].join('\n')
}

/** @returns 已存在的执行中投影；后续 REST 必须提供独有的终态证据。 */
function runningAction(): ActionSnapshot {
  const snapshot = actionSnapshot()
  return {
    ...snapshot,
    status: 'running',
    error_code: null,
    timeline: [],
    local_action: snapshot.local_action
      ? { ...snapshot.local_action, status: 'executing' }
      : null,
    execution: snapshot.execution
      ? { ...snapshot.execution, status: 'executing' }
      : null,
  }
}

/**
 * 每个 M2 名称分别走实际页面与原生 EventSource，缺少任何监听注册都会停留在 running。
 * payload 的 succeeded 故意与 REST 相反，防止把 OAuth 或工具事件当作写入成功。
 */
for (const eventName of M2_EVENTS) {
  test(`native action stream recovers ${eventName} from REST`, async ({
    page,
  }) => {
    await authenticateFixture(page)
    let actionReads = 0
    let taskReads = 0
    await page.route('**/api/v1/actions', (route) =>
      fulfillJson(route, { items: actionItems(), limit: 50, offset: 0 }),
    )
    await page.route(`**/api/v1/actions/${TASK_ID}`, (route) => {
      actionReads += 1
      return fulfillJson(
        route,
        actionReads === 1
          ? runningAction()
          : actionSnapshot({
              event_cursor: RECOVERED_CURSOR,
              task_version: RECOVERED_CURSOR,
            }),
      )
    })
    await page.route(`**/api/v1/tasks/${TASK_ID}`, (route) => {
      taskReads += 1
      return fulfillJson(route, {
        id: TASK_ID,
        kind: 'trusted_action',
        status: 'needs_attention',
        retry_of_task_id: null,
        error_code: null,
        event_cursor: RECOVERED_CURSOR,
        steps: [],
      })
    })
    await page.route(`**/api/v1/tasks/${TASK_ID}/events*`, async (route) => {
      await expect(page.locator('.action-detail')).toContainText('执行中')
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: `retry: 60000\n\n${persistentFrame(eventName, '9007199254740994')}`,
      })
    })
    await page.goto('/actions')
    await page.getByRole('button', { name: '查看发送邮件详情' }).click()
    await expect(page.locator('.action-detail')).toContainText('结果需要核实')
    await expect.poll(() => actionReads).toBe(2)
    await expect.poll(() => taskReads).toBe(1)
    await expect(page.locator('.action-detail')).not.toContainText(
      'Google · 已完成',
    )
  })
}

test('unknown native named event recovers once through id-less heartbeats and keeps exact cursor', async ({
  page,
}) => {
  await authenticateFixture(page)
  let listReads = 0
  let actionReads = 0
  let taskReads = 0
  const streamCursors: Array<string | null> = []
  let releaseTask: () => void = () => undefined
  const taskReady = new Promise<void>((resolve) => {
    releaseTask = resolve
  })
  await page.route('**/api/v1/actions', (route) => {
    listReads += 1
    return fulfillJson(route, { items: actionItems(), limit: 50, offset: 0 })
  })
  await page.route(`**/api/v1/actions/${TASK_ID}`, (route) => {
    actionReads += 1
    return fulfillJson(
      route,
      actionReads === 1
        ? runningAction()
        : actionSnapshot({
            event_cursor: RECOVERED_CURSOR,
            task_version: RECOVERED_CURSOR,
          }),
    )
  })
  await page.route(`**/api/v1/tasks/${TASK_ID}`, async (route) => {
    taskReads += 1
    await taskReady
    await fulfillJson(route, {
      id: TASK_ID,
      kind: 'trusted_action',
      status: 'needs_attention',
      retry_of_task_id: null,
      error_code: null,
      event_cursor: RECOVERED_CURSOR,
      steps: [],
    })
  })
  await page.route(`**/api/v1/tasks/${TASK_ID}/events*`, async (route) => {
    streamCursors.push(
      new URL(route.request().url()).searchParams.get('last_event_id'),
    )
    if (streamCursors.length === 1) {
      await expect(page.locator('.action-detail')).toContainText('执行中')
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: [
          'retry: 60000\n\n',
          persistentFrame('future.audit_event', RECOVERED_CURSOR),
          'event: heartbeat\ndata: {}\n\n',
          'event: heartbeat\ndata: {}\n\n',
        ].join(''),
      })
      return
    }
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: 'retry: 60000\n\nevent: heartbeat\ndata: {}\n\n',
    })
  })
  await page.goto('/actions')
  await page.getByRole('button', { name: '查看发送邮件详情' }).click()
  await expect.poll(() => taskReads).toBe(1)
  // 两个心跳均在同一读取完成前到达，只合并恢复需求，不发送第二个并行读取。
  releaseTask()
  await expect(page.locator('.action-detail')).toContainText('结果需要核实')
  await expect.poll(() => listReads).toBe(2)
  expect(taskReads).toBe(1)
  expect(actionReads).toBe(2)
  await page.getByRole('button', { name: '关闭操作详情' }).click()
  await page.getByRole('button', { name: '查看发送邮件详情' }).click()
  await expect.poll(() => streamCursors.length).toBe(2)
  expect(streamCursors).toEqual([null, RECOVERED_CURSOR])
  expect(
    await page.evaluate(() => [localStorage.length, sessionStorage.length]),
  ).toEqual([0, 0])
})

test('action groups, filters, provider links, keyboard focus and narrow detail use the actual page', async ({
  page,
}, testInfo) => {
  await authenticateFixture(page)
  const items = actionItems()
  const extra: Array<{
    suffix: string
    status: 'waiting_approval' | 'reconciling' | 'succeeded'
  }> = [
    { suffix: '410', status: 'waiting_approval' },
    { suffix: '411', status: 'reconciling' },
    { suffix: '412', status: 'succeeded' },
  ]
  for (const entry of extra) {
    const id = `00000000-0000-0000-0000-000000000${entry.suffix}`
    items.push({
      id,
      task_id: id,
      item_kind: 'trusted_task',
      editor_url: null,
      status: entry.status,
      action: 'calendar.create',
      provider: 'microsoft',
      risk_level: 'medium',
      created_at: NOW,
      updated_at: NOW,
    })
  }
  let snapshot = actionSnapshot()
  const queries: string[] = []
  await page.route(/\/api\/v1\/actions(?:\?.*)?$/, (route) => {
    const url = new URL(route.request().url())
    queries.push(url.search)
    const filtered = items.filter((item: ActionListItem) =>
      ['provider', 'item_kind', 'status'].every(
        (key) =>
          !url.searchParams.has(key) ||
          item[key as 'provider' | 'item_kind' | 'status'] ===
            url.searchParams.get(key),
      ),
    )
    return fulfillJson(route, { items: filtered, limit: 50, offset: 0 })
  })
  await page.route(`**/api/v1/actions/${TASK_ID}`, (route) =>
    fulfillJson(route, snapshot),
  )
  await page.route(`**/api/v1/tasks/${TASK_ID}/events*`, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: 'retry: 60000\n\nevent: heartbeat\ndata: {}\n\n',
    }),
  )
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.goto('/actions')
  await expect(page.getByRole('link', { name: '操作中心' })).toBeVisible()
  for (const label of [
    '邮件草稿',
    '日程提案',
    '待审批',
    '执行或核对中',
    '需要人工确认',
    '已完成历史',
  ])
    await expect(
      page.getByRole('region', { name: `${label} 1`, exact: true }),
    ).toBeVisible()
  await expect(page.getByText('本地草稿', { exact: true })).toBeVisible()
  await expect(page.locator('a[href^="/mail/drafts/"]')).toHaveCount(0)
  await page.getByLabel('供应商筛选').selectOption('microsoft')
  await page.getByLabel('操作类型筛选').selectOption('calendar_proposal')
  await expect(page.getByText('当前页 1 项操作')).toBeVisible()
  expect(queries.at(-1)).toBe('?provider=microsoft&item_kind=calendar_proposal')
  await page.getByLabel('供应商筛选').selectOption('')
  await page.getByLabel('操作类型筛选').selectOption('')
  await expect(page.getByText('当前页 6 项操作')).toBeVisible()
  const trigger = page.getByRole('button', { name: '查看发送邮件详情' })
  await trigger.focus()
  await page.keyboard.press('Enter')
  const detail = page.getByRole('complementary', { name: '操作详情与时间线' })
  await expect(detail).toBeFocused()
  const providerLink = detail.getByRole('link', {
    name: '在 Google 中检查结果',
  })
  await expect(providerLink).toHaveAttribute(
    'href',
    'https://mail.google.com/mail/u/0/',
  )
  await expect(providerLink).toHaveAttribute('rel', 'noopener noreferrer')
  await expect(detail.locator('[aria-live="polite"]')).toContainText(
    '需要人工确认',
  )
  await page.screenshot({
    path: testInfo.outputPath('action-center-desktop.png'),
    fullPage: true,
  })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.getByRole('form', { name: '操作筛选' })).toBeHidden()
  await expect(detail).toBeVisible()
  expect(
    await page.evaluate(
      () =>
        document.documentElement.scrollWidth <=
        document.documentElement.clientWidth,
    ),
  ).toBe(true)
  await page.screenshot({
    path: testInfo.outputPath('action-center-narrow.png'),
    fullPage: true,
  })
  await page.getByRole('button', { name: '关闭操作详情' }).click()
  await expect(trigger).toBeFocused()
  snapshot = { ...snapshot, provider_url: 'javascript:alert(1)' }
  await trigger.click()
  await expect(detail.getByRole('link')).toHaveCount(0)
  await page.reload()
  await expect(page.getByText('当前页 6 项操作')).toBeVisible()
  expect(
    await page.evaluate(() => [localStorage.length, sessionStorage.length]),
  ).toEqual([0, 0])
})
