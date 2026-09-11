import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ActionsPage from './ActionsPage.vue'
import {
  actionItems,
  actionSnapshot,
  TASK_ID,
} from '@/test-support/actionFixtures'
import { TaskEventSource } from '@/test-support/taskEventSource'

let wrappers: Array<ReturnType<typeof mount>> = []
beforeEach(() => {
  vi.stubGlobal('EventSource', TaskEventSource)
  TaskEventSource.instances = []
  localStorage.clear()
  sessionStorage.clear()
})
afterEach(() => {
  wrappers.forEach((wrapper) => wrapper.unmount())
  wrappers = []
  vi.unstubAllGlobals()
})

/** 页面边界保留真实客户端和 Store，只有网络与原生 EventSource 使用替身。 */
function renderPage() {
  const wrapper = mount(ActionsPage, { global: { plugins: [createPinia()] } })
  wrappers.push(wrapper)
  return wrapper
}

describe('ActionsPage', () => {
  it('refreshes server actions after focus without persisting sensitive content', async () => {
    let listCalls = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        listCalls += 1
        return new Response(
          JSON.stringify({
            items: listCalls === 1 ? [] : actionItems(),
            limit: 50,
            offset: 0,
          }),
        )
      }),
    )
    const wrapper = renderPage()
    await flushPromises()
    expect(wrapper.text()).toContain('暂无操作')
    window.dispatchEvent(new Event('focus'))
    await flushPromises()
    expect(listCalls).toBe(2)
    expect(wrapper.text()).toContain('邮件草稿')
    expect(wrapper.findAll('[data-action-id]')).toHaveLength(3)
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)
    expect(TaskEventSource.instances).toHaveLength(0)
  })

  it('shows loading and recoverable failure instead of a blank empty list', async () => {
    let complete: (response: Response) => void = () => undefined
    vi.stubGlobal(
      'fetch',
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            complete = resolve
          }),
      ),
    )
    const wrapper = renderPage()
    expect(wrapper.text()).toContain('正在加载操作')
    complete(
      new Response(
        JSON.stringify({ title: 'Unavailable', trace_id: 'synthetic-trace' }),
        { status: 503 },
      ),
    )
    await flushPromises()
    expect(wrapper.find('[role="alert"]').text()).toContain('synthetic-trace')
    expect(wrapper.find('button').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('暂无操作')
  })

  it.each(['focus', 'reconnect'])(
    'keeps the new detail and group when an older list completes during %s recovery',
    async (signal) => {
      let listReads = 0
      let snapshotReads = 0
      let finishOldList: (response: Response) => void = () => undefined
      const initial = actionSnapshot()
      const oldPage = {
        items: actionItems().map((item) =>
          item.item_kind === 'trusted_task'
            ? { ...item, status: 'running' }
            : item,
        ),
        limit: 50,
        offset: 0,
      }
      vi.stubGlobal(
        'fetch',
        vi.fn(async (url: string) => {
          if (url === '/api/v1/actions') {
            listReads += 1
            if (listReads === 2)
              return new Promise<Response>((resolve) => {
                finishOldList = resolve
              })
            return new Response(
              JSON.stringify(
                listReads === 1
                  ? oldPage
                  : {
                      items: actionItems(),
                      limit: 50,
                      offset: 0,
                    },
              ),
            )
          }
          snapshotReads += 1
          return new Response(
            JSON.stringify(
              snapshotReads === 1
                ? {
                    ...initial,
                    status: 'running',
                    error_code: null,
                    timeline: [],
                    local_action: initial.local_action
                      ? { ...initial.local_action, status: 'executing' }
                      : null,
                    execution: initial.execution
                      ? { ...initial.execution, status: 'executing' }
                      : null,
                  }
                : actionSnapshot({
                    event_cursor: '9007199254740994',
                    task_version: '9007199254740994',
                  }),
            ),
          )
        }),
      )
      const wrapper = renderPage()
      await flushPromises()
      await wrapper.get(`[data-action-id="${TASK_ID}"] button`).trigger('click')
      await flushPromises()
      const source = TaskEventSource.instances.at(-1)
      if (!source) throw new Error('Missing synthetic task stream')
      source.onopen?.(new Event('open'))
      if (signal === 'focus') window.dispatchEvent(new Event('focus'))
      else {
        source.onerror?.(new Event('error'))
        source.onopen?.(new Event('open'))
      }
      await flushPromises()
      expect(wrapper.get(`[data-action-id="${TASK_ID}"]`).text()).toContain(
        '需要人工确认',
      )
      // 详情已进入人工核对，迟到列表不能把同一行重新移回执行分组。
      finishOldList(new Response(JSON.stringify(oldPage)))
      await flushPromises()
      expect(wrapper.get('.action-detail').text()).toContain('需要人工确认')
      expect(wrapper.get(`[data-action-id="${TASK_ID}"]`).text()).toContain(
        '需要人工确认',
      )
      expect(listReads).toBe(3)
      expect(TaskEventSource.instances).toHaveLength(1)
    },
  )

  it('renders all six groups and sends filters without content in URLs', async () => {
    const urls: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        urls.push(url)
        return new Response(
          JSON.stringify({ items: actionItems(), limit: 50, offset: 0 }),
        )
      }),
    )
    const wrapper = renderPage()
    await flushPromises()
    for (const label of [
      '邮件草稿',
      '日程提案',
      '待审批',
      '执行或核对中',
      '需要人工确认',
      '已完成历史',
    ])
      expect(wrapper.text()).toContain(label)
    await wrapper.get('select[aria-label="供应商筛选"]').setValue('microsoft')
    await flushPromises()
    expect(urls.at(-1)).toContain('provider=microsoft')
    expect(wrapper.findAll('label').length).toBeGreaterThanOrEqual(2)
    expect(wrapper.find('[aria-live="polite"]').exists()).toBe(true)
  })

  it('lets users filter every server status including queued, retrying and stale objects', async () => {
    const urls: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        urls.push(url)
        return new Response(JSON.stringify({ items: [], limit: 50, offset: 0 }))
      }),
    )
    const wrapper = renderPage()
    await flushPromises()
    for (const status of [
      'created',
      'queued',
      'running',
      'waiting_approval',
      'retry_scheduled',
      'reconciling',
      'needs_attention',
      'succeeded',
      'failed',
      'cancelled',
      'editing',
      'awaiting_approval',
      'executing',
      'sent',
      'applied',
      'stale',
    ]) {
      await wrapper.get('select[aria-label="操作状态筛选"]').setValue(status)
      await flushPromises()
      expect(urls.at(-1)).toBe(`/api/v1/actions?status=${status}`)
    }
  })

  it('opens a real task detail, refreshes after reconnect, and shows redacted history', async () => {
    const value = actionSnapshot()
    let listCalls = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url.startsWith('/api/v1/actions?') || url === '/api/v1/actions') {
          listCalls += 1
          return new Response(
            JSON.stringify({ items: actionItems(), limit: 50, offset: 0 }),
          )
        }
        if (url.startsWith('/api/v1/tasks/'))
          return new Response(
            JSON.stringify({
              id: TASK_ID,
              kind: 'mail.send',
              status: 'needs_attention',
              event_cursor: value.event_cursor,
              error_code: null,
              retry_of_task_id: null,
              steps: [],
            }),
          )
        return new Response(
          JSON.stringify({
            ...value,
            approval: {
              ...value.approval,
              content_status: 'redacted',
              preview: null,
            },
          }),
        )
      }),
    )
    const wrapper = renderPage()
    await flushPromises()
    await wrapper.get(`[data-action-id="${TASK_ID}"] button`).trigger('click')
    await flushPromises()
    expect(wrapper.get('[aria-label="操作详情与时间线"]').text()).toContain(
      '内容已到期',
    )
    expect(wrapper.find('[aria-label="审计时间线"]').exists()).toBe(true)
    const link = wrapper.get('a[target="_blank"]')
    expect(link.attributes('rel')).toBe('noopener noreferrer')
    const source = TaskEventSource.instances.at(-1)
    source?.onopen?.(new Event('open'))
    source?.onerror?.(new Event('error'))
    await flushPromises()
    expect(wrapper.text()).toContain('正在重新连接')
    source?.onopen?.(new Event('open'))
    await flushPromises()
    expect(listCalls).toBe(2)
    expect(wrapper.find('button[aria-label="关闭操作详情"]').exists()).toBe(
      true,
    )
  })

  it('displays partial calendar completeness and suppresses unsafe provider links', async () => {
    const value = actionSnapshot()
    const fields = {
      title: '',
      description: null,
      location: null,
      starts_at: '2030-01-01T00:00:00Z',
      ends_at: '2030-01-01T01:00:00Z',
      timezone: 'UTC',
      all_day: false,
      attendees: [],
    }
    const snapshot = {
      ...value,
      provider_url: 'javascript:alert(1)',
      action: 'calendar.create',
      local_action: null,
      approval: {
        ...value.approval,
        preview: {
          kind: 'calendar',
          provider: 'google',
          account_email: 'sender@synthetic.example.test',
          calendar_name: '',
          operation: 'create',
          before: null,
          after: fields,
          conflicts: [
            {
              kind: 'partial_sources',
              starts_at: null,
              ends_at: null,
              missing_connection_ids: ['00000000-0000-0000-0000-000000000202'],
            },
          ],
          notification_policy: 'all',
          base_etag: null,
          compensation_available: false,
          provider_warnings: [],
        },
      },
    }
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async (url: string) =>
          new Response(
            JSON.stringify(
              url === '/api/v1/actions'
                ? { items: actionItems(), limit: 50, offset: 0 }
                : snapshot,
            ),
          ),
      ),
    )
    const wrapper = renderPage()
    await flushPromises()
    await wrapper.get(`[data-action-id="${TASK_ID}"] button`).trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('部分日历来源尚未同步')
    expect(wrapper.find('a[href^="javascript:"]').exists()).toBe(false)
  })
})
