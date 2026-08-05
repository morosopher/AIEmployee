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

/**
 * 以浏览器真实 EventSource 请求模拟服务端关闭后的耐久事件重放。
 *
 * 第一条流故意在 running 事件后结束；第二条请求必须带上 ``last_event_id=1``，
 * 并返回终态。应用收到终态后会主动读取 PostgreSQL 快照，测试用可控 REST 回应
 * 证明该补偿不是页面 reload 的静态替身。
 */
test('EventSource reconnect replays from the durable cursor and reconciles terminal snapshot', async ({
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
        snapshotRequests === 1 ? runningSnapshot : terminalSnapshot,
      ),
    })
  })
  await page.route('**/api/v1/tasks/task-1/events*', async (route) => {
    eventRequests += 1
    const url = new URL(route.request().url())
    if (eventRequests === 1) {
      expect(url.searchParams.get('last_event_id')).toBeNull()
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'Cache-Control': 'no-cache' },
        body: [
          'id: 1',
          'event: task.status_changed',
          'data: {"id":"1","task_id":"task-1","sequence":"1","event":"task.status_changed","occurred_at":"2026-08-05T00:00:00Z","step_id":null,"payload":{"status":"running"}}',
          '',
        ].join('\n'),
      })
      return
    }
    expect(url.searchParams.get('last_event_id')).toBe('1')
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      headers: { 'Cache-Control': 'no-cache' },
      body: [
        'id: 2',
        'event: task.status_changed',
        'data: {"id":"2","task_id":"task-1","sequence":"2","event":"task.status_changed","occurred_at":"2026-08-05T00:00:01Z","step_id":null,"payload":{"status":"succeeded"}}',
        '',
      ].join('\n'),
    })
  })

  await page.goto('/tasks?task_id=task-1')
  await expect(page.getByText('当前状态： succeeded')).toBeVisible()
  await expect.poll(() => eventRequests).toBeGreaterThanOrEqual(2)
  expect(snapshotRequests).toBeGreaterThanOrEqual(2)
})
