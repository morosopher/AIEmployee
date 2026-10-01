import type { TaskHistoryItem, TaskHistoryPage } from '../api/taskHistory'

/**
 * 创建不含正文、身份或凭据的固定合成摘要，供客户端、状态和页面测试复用。
 * @param overrides 当前场景需要覆盖的公开摘要字段。
 * @returns 每次独立创建的摘要；时间保留服务端六位微秒精度。
 */
export function historyItem(overrides: Partial<TaskHistoryItem> = {}): TaskHistoryItem {
  return {
    id: '00000000-0000-0000-0000-000000000001',
    kind: 'daily_brief',
    category: 'business',
    status: 'queued',
    created_at: '2026-10-01T01:00:00.123456Z',
    started_at: null,
    finished_at: null,
    error_code: null,
    retry_of_task_id: null,
    ...overrides,
  }
}

/**
 * 创建固定合成分页响应，不依赖真实时间、网络或浏览器任务缓存。
 * @param overrides 当前场景需要覆盖的分页字段。
 * @returns 带独立摘要数组的分页响应。
 */
export function historyPage(overrides: Partial<TaskHistoryPage> = {}): TaskHistoryPage {
  return {
    items: [historyItem()],
    next_cursor: null,
    previous_cursor: null,
    server_time: '2026-10-01T02:00:00.000000Z',
    filter_timezone: 'Asia/Shanghai',
    background_failed_count: 0,
    ...overrides,
  }
}
