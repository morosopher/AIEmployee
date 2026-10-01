import { expect, test } from '@playwright/test'

const user = {
  id: 'u1',
  email: 'admin@example.test',
  display_name: 'Admin',
  timezone: 'UTC',
  locale: 'zh-CN',
  brief_time: '08:00',
}

const runningSnapshot = {
  id: 'task-1',
  kind: 'daily_brief',
  status: 'running',
  retry_of_task_id: null,
  error_code: null,
  event_cursor: '0',
  steps: [],
}

const terminalSnapshot = {
  ...runningSnapshot,
  status: 'succeeded',
  event_cursor: '2',
}

const recoveredRunningSnapshot = {
  ...runningSnapshot,
  event_cursor: '1',
  steps: [
    {
      id: 'step-1',
      name: 'persist',
      sequence: 1,
      status: 'started',
      started_at: '2026-08-05T00:00:00Z',
      finished_at: null,
      error_code: null,
      output_summary: null,
    },
  ],
}

const reloadedTerminalSnapshot = {
  ...recoveredRunningSnapshot,
  status: 'succeeded',
  event_cursor: '3',
  steps: [
    {
      ...recoveredRunningSnapshot.steps[0],
      sequence: 2,
      status: 'completed',
      finished_at: '2026-08-05T00:00:01Z',
    },
  ],
}

/**
 * 以浏览器真实 EventSource 验证页面离开后按已知耐久游标重新订阅。
 *
 * 第一条流在 running 后设置较长重连间隔；SPA 路由卸载并重建订阅时必须把 Pinia 中
 * 已见的游标放进查询参数。终态事件随后触发 PostgreSQL 快照对账。
 */
test('EventSource reopens from the durable cursor and reconciles terminal snapshot', async ({
  page,
}) => {
  let eventRequests = 0
  let snapshotRequests = 0
  await page.route('**/api/v1/auth/me', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(user),
    }),
  )
  await page.route('**/api/v1/tasks/task-1', (route) => {
    snapshotRequests += 1
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(
        snapshotRequests <= 2 ? runningSnapshot : terminalSnapshot,
      ),
    })
  })
  await page.route('**/api/v1/tasks/task-1/events*', async (route) => {
    eventRequests += 1
    const url = new URL(route.request().url())
    const headers = await route.request().allHeaders()
    if (eventRequests === 1) {
      expect(url.searchParams.get('last_event_id')).toBeNull()
      expect(headers['last-event-id']).toBeUndefined()
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'Cache-Control': 'no-cache' },
        body: [
          'retry: 60000',
          'id: 1',
          'event: task.status_changed',
          'data: {"id":"1","task_id":"task-1","sequence":"1","event":"task.status_changed","occurred_at":"2026-08-05T00:00:00Z","step_id":null,"payload":{"status":"running"}}',
          '',
          '',
        ].join('\n'),
      })
      return
    }
    expect(url.searchParams.get('last_event_id')).toBe('1')
    expect(headers['last-event-id']).toBeUndefined()
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      headers: { 'Cache-Control': 'no-cache' },
      body: [
        'id: 2',
        'event: task.status_changed',
        'data: {"id":"2","task_id":"task-1","sequence":"2","event":"task.status_changed","occurred_at":"2026-08-05T00:00:01Z","step_id":null,"payload":{"status":"succeeded"}}',
        '',
        '',
      ].join('\n'),
    })
  })

  await page.goto('/tasks?task_id=task-1')
  await expect(page.getByText('当前状态：running')).toBeVisible()
  await expect.poll(() => eventRequests).toBe(1)
  // 使用 Vue Router 链接与浏览器后退保持同一 SPA/Pinia 实例，模拟用户暂时离开任务页。
  await page.getByRole('link', { name: '任务历史' }).click()
  await expect(page).toHaveURL(/\/tasks$/)
  await page.goBack()
  await expect(page.getByText('当前状态：succeeded')).toBeVisible()
  await expect.poll(() => eventRequests).toBe(2)
  expect(snapshotRequests).toBeGreaterThanOrEqual(3)
})

/**
 * 页面刷新会丢失浏览器内存游标，因此服务端可能从 cursor 0 全量重放。测试先等待
 * PostgreSQL 快照恢复 sequence 1，再发送重复 sequence 1/2 与终态 sequence 3，证明
 * 快照基线和事件去重不会生成第二个时间线条目或回退终态。
 */
test('page reload restores durable snapshot and deduplicates replayed task events', async ({
  page,
}) => {
  let eventRequests = 0
  let snapshotRequests = 0
  await page.route('**/api/v1/auth/me', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(user),
    }),
  )
  await page.route('**/api/v1/tasks/task-1', (route) => {
    snapshotRequests += 1
    const snapshot =
      snapshotRequests === 1
        ? runningSnapshot
        : snapshotRequests === 2
          ? recoveredRunningSnapshot
          : reloadedTerminalSnapshot
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(snapshot),
    })
  })
  await page.route('**/api/v1/tasks/task-1/events*', async (route) => {
    eventRequests += 1
    const url = new URL(route.request().url())
    const headers = await route.request().allHeaders()
    expect(url.searchParams.get('last_event_id')).toBeNull()
    expect(headers['last-event-id']).toBeUndefined()
    if (eventRequests === 1) {
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'Cache-Control': 'no-cache' },
        body: [
          'retry: 60000',
          'id: 1',
          'event: step.started',
          'data: {"id":"1","task_id":"task-1","sequence":"1","event":"step.started","occurred_at":"2026-08-05T00:00:00Z","step_id":"step-1","payload":{"name":"persist","status":"started"}}',
          '',
          '',
        ].join('\n'),
      })
      return
    }
    if (eventRequests === 2) {
      await expect(page.getByText('当前状态：running')).toBeVisible()
      await expect(
        page
          .getByRole('list', { name: '任务步骤' })
          .getByRole('listitem')
          .filter({ hasText: 'persist' }),
      ).toHaveCount(1)
      expect(snapshotRequests).toBeGreaterThanOrEqual(2)
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'Cache-Control': 'no-cache' },
        body: [
          'retry: 60000',
          'id: 1',
          'event: step.started',
          'data: {"id":"1","task_id":"task-1","sequence":"1","event":"step.started","occurred_at":"2026-08-05T00:00:00Z","step_id":"step-1","payload":{"name":"persist","status":"started"}}',
          '',
          '',
          'id: 2',
          'event: step.completed',
          'data: {"id":"2","task_id":"task-1","sequence":"2","event":"step.completed","occurred_at":"2026-08-05T00:00:01Z","step_id":"step-1","payload":{"name":"persist","status":"completed","finished_at":"2026-08-05T00:00:01Z"}}',
          '',
          '',
          'id: 2',
          'event: step.completed',
          'data: {"id":"2","task_id":"task-1","sequence":"2","event":"step.completed","occurred_at":"2026-08-05T00:00:01Z","step_id":"step-1","payload":{"name":"persist","status":"completed","finished_at":"2026-08-05T00:00:01Z"}}',
          '',
          '',
          'id: 3',
          'event: task.status_changed',
          'data: {"id":"3","task_id":"task-1","sequence":"3","event":"task.status_changed","occurred_at":"2026-08-05T00:00:02Z","step_id":null,"payload":{"status":"succeeded"}}',
          '',
          '',
        ].join('\n'),
      })
      return
    }
    throw new Error(`unexpected event request ${eventRequests}`)
  })

  await page.goto('/tasks?task_id=task-1')
  await expect(page.getByText('当前状态：running')).toBeVisible()
  await page.reload()

  await expect(page.getByText('当前状态：succeeded')).toBeVisible()
  await expect(
    page
      .getByRole('list', { name: '任务步骤' })
      .getByRole('listitem')
      .filter({ hasText: 'persist' }),
  ).toHaveCount(1)
  await expect.poll(() => eventRequests).toBe(2)
  expect(snapshotRequests).toBeGreaterThanOrEqual(3)
})

/**
 * 外壳的真实浏览器键盘回归：inert 必须阻止背景获取焦点，Tab 始终留在抽屉；
 * Esc 与路由导航关闭后必须恢复触发按钮。数据仍使用本文件合成快照。
 */
test('mobile shell traps focus and restores interactive background after each drawer closes', async ({
  page,
}) => {
  await page.setViewportSize({ width: 600, height: 900 })
  await page.route('**/api/v1/auth/me', (route) =>
    route.fulfill({ json: user }),
  )
  await page.route('**/api/v1/system/alerts', (route) =>
    route.fulfill({ json: { alerts: [] } }),
  )
  await page.route('**/api/v1/tasks/task-1', (route) =>
    route.fulfill({ json: terminalSnapshot }),
  )
  await page.route('**/api/v1/briefs/today', (route) => route.fulfill({ status: 404, json: { type: 'about:blank', title: 'Not found', status: 404, detail: '', instance: '', error_code: 'brief_not_found', trace_id: 'synthetic-brief' } }))
  await page.goto('/brief')
  await expect(page.getByRole('heading', { name: '今日简报', level: 1 })).toBeVisible()

  for (const [triggerName, dialogName] of [
    ['打开导航', '主导航'],
    ['打开任务时间线', '任务时间线'],
  ] as const) {
    const trigger = page.getByRole('button', { name: triggerName })
    await trigger.click()
    const dialog = page.getByRole('dialog', { name: dialogName })
    await expect(dialog).toBeVisible()
    // 选择器使用稳定角色；evaluate 仅检查标准 DOM inert/focus 行为。
    await expect
      .poll(() =>
        page
          .getByRole('main', { includeHidden: true })
          .first()
          .evaluate((element) => element.closest('[inert]') !== null),
      )
      .toBe(true)
    for (let index = 0; index < 10; index += 1) {
      await page.keyboard.press(index % 2 === 0 ? 'Tab' : 'Shift+Tab')
      await expect
        .poll(() =>
          dialog.evaluate((element) =>
            element.contains(document.activeElement),
          ),
        )
        .toBe(true)
    }
    await page.keyboard.press('Escape')
    await expect(dialog).toBeHidden()
    await expect(trigger).toBeFocused()
    await expect
      .poll(() =>
        page
          .getByRole('main')
          .first()
          .evaluate((element) => element.closest('[inert]') === null),
      )
      .toBe(true)
  }

  const navigationTrigger = page.getByRole('button', { name: '打开导航' })
  await navigationTrigger.click()
  await page.getByRole('link', { name: '任务历史' }).click()
  await expect(page).toHaveURL(/\/tasks$/)
  await expect(page.getByRole('dialog', { name: '主导航' })).toBeHidden()
  await expect(navigationTrigger).toBeFocused()
})
