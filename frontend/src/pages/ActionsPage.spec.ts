import { fireEvent, screen, within, waitFor } from '@testing-library/vue'
import {
  renderWithPlugins,
  setViewport,
} from '@/test-support/renderWithPlugins'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ActionsPage from './ActionsPage.vue'
import { listActions } from '@/api/actions'

// 只记录真实客户端调用参数；解析、HTTP、Store 和 SSE 生命周期仍完整运行。
vi.mock('@/api/actions', { spy: true })
import {
  actionItems,
  actionSnapshot,
  TASK_ID,
  DRAFT_ID,
  PROPOSAL_ID,
  mailDraft,
  calendarProposal,
} from '@/test-support/actionFixtures'
import { TaskEventSource } from '@/test-support/taskEventSource'

beforeEach(() => {
  // jsdom 无布局观察器；只补浏览器平台能力，不替换 Tabs 键盘行为。
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  )
  vi.mocked(listActions).mockClear()
  vi.stubGlobal('EventSource', TaskEventSource)
  TaskEventSource.instances = []
  localStorage.clear()
  sessionStorage.clear()
})
afterEach(() => {
  vi.unstubAllGlobals()
})

/** 保留真实 API/Store；通过可访问角色操作页面，网络与 EventSource 使用合成替身。 */
async function renderPage(query = '') {
  const view = await renderWithPlugins(ActionsPage, {
    route: `/actions${query}`,
  })
  await screen.findByRole('heading', { name: '操作中心' })
  return view
}
/** 用户先选择当前页分组；只改变可见标签，不触发额外服务端筛选。 */
async function selectGroup(label: string): Promise<void> {
  await fireEvent.click(
    screen.getByRole('tab', { name: new RegExp(`^${label} `) }),
  )
}
/** 通过实际 Select 的可访问选项选择；值对应原 HTTP 参数，不模拟组件 emit。 */
async function selectFilter(label: string, option: string): Promise<void> {
  await fireEvent.click(screen.getByRole('combobox', { name: label }))
  await fireEvent.mouseDown(screen.getByRole('option', { name: option }))
  await waitFor(() =>
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument(),
  )
}
/** 等待 HTTP、Vue 更新与路由队列收敛，不替换页面业务状态机。 */
async function flushPromises(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0))
}
describe('ActionsPage', () => {
  it.each(['mail', 'calendar'] as const)(
    'creates a local %s object only on explicit click and opens the returned editor',
    async (kind) => {
      const fetch = vi.fn(
        async (_url: string, init?: RequestInit) =>
          new Response(
            JSON.stringify(
              init?.method === 'POST'
                ? kind === 'mail'
                  ? mailDraft()
                  : calendarProposal()
                : { items: actionItems(), limit: 50, offset: 0 },
            ),
          ),
      )
      vi.stubGlobal('fetch', fetch)
      const view = await renderPage()
      await flushPromises()
      expect(
        fetch.mock.calls.filter(([, init]) => init?.method === 'POST'),
      ).toHaveLength(0)
      expect(
        screen
          .getByRole('link', { name: '编辑本地草稿' })
          .getAttribute('href') === `/mail/drafts/${DRAFT_ID}`,
      ).toBe(true)
      await fireEvent.click(
        screen.getByRole('button', {
          name: kind === 'mail' ? '新邮件' : '新日程',
        }),
      )
      await flushPromises()
      const calls = fetch.mock.calls.filter(
        ([, init]) => init?.method === 'POST',
      )
      expect(calls).toHaveLength(1)
      expect(calls[0]?.[0]).toBe(
        kind === 'mail' ? '/api/v1/mail/drafts' : '/api/v1/calendar/proposals',
      )
      expect(JSON.parse(String(calls[0]?.[1]?.body))).toEqual(
        kind === 'mail'
          ? { mode: 'new' }
          : { operation_kind: 'create', initialization: 'shell' },
      )
      expect(view.router.currentRoute.value.path).toBe(
        kind === 'mail'
          ? `/mail/drafts/${DRAFT_ID}`
          : `/calendar/proposals/${PROPOSAL_ID}`,
      )
      expect(
        fetch.mock.calls.some(
          ([url]) => url.includes('/submit') || url.includes('/decision'),
        ),
      ).toBe(false)
    },
  )

  it('restores the precise task selected by a submission link', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async (url: string) =>
          new Response(
            JSON.stringify(
              url === '/api/v1/actions'
                ? { items: [], limit: 50, offset: 0 }
                : url.startsWith('/api/v1/actions/')
                  ? actionSnapshot()
                  : {
                      id: TASK_ID,
                      kind: 'trusted_action',
                      status: 'needs_attention',
                      retry_of_task_id: null,
                      error_code: null,
                      event_cursor: '9007199254740993',
                      steps: [],
                    },
            ),
          ),
      ),
    )
    const view = await renderPage(`?task=${TASK_ID}`)
    await flushPromises()
    expect(
      !!screen.queryByRole('complementary', { name: '操作详情与时间线' }),
    ).toBe(true)
    expect(view.container.textContent).toContain('先核实供应商中的实际结果')
  })

  it('reuses the creation intent after transport failure and never automatically retries', async () => {
    let attempts = 0
    const fetch = vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        attempts += 1
        if (attempts === 1) throw new TypeError('Synthetic transport failure')
        return new Response(JSON.stringify(mailDraft()))
      }
      return new Response(JSON.stringify({ items: [], limit: 50, offset: 0 }))
    })
    vi.stubGlobal('fetch', fetch)
    await renderPage()
    await flushPromises()
    await fireEvent.click(screen.getByRole('button', { name: '新邮件' }))
    await flushPromises()
    expect(attempts).toBe(1)
    await fireEvent.click(screen.getByRole('button', { name: '新邮件' }))
    await flushPromises()
    const calls = fetch.mock.calls.filter(([, init]) => init?.method === 'POST')
    expect(new Headers(calls[0]?.[1]?.headers).get('Idempotency-Key')).toBe(
      new Headers(calls[1]?.[1]?.headers).get('Idempotency-Key'),
    )
  })
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
    const view = await renderPage()
    await flushPromises()
    expect(view.container.textContent).toContain('暂无操作')
    window.dispatchEvent(new Event('focus'))
    await flushPromises()
    expect(listCalls).toBe(2)
    expect(view.container.textContent).toContain('邮件草稿')
    expect(
      screen.getByRole('link', { name: '编辑本地草稿' }),
    ).toBeInTheDocument()
    await selectGroup('日程提案')
    expect(
      screen.getByRole('link', { name: '编辑本地提案' }),
    ).toBeInTheDocument()
    await selectGroup('需要人工确认')
    expect(
      screen.getByRole('button', { name: '查看发送邮件详情' }),
    ).toBeInTheDocument()
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
    const view = await renderPage()
    expect(view.container.textContent).toContain('正在加载操作')
    complete(
      new Response(
        JSON.stringify({ title: 'Unavailable', trace_id: 'synthetic-trace' }),
        { status: 503 },
      ),
    )
    await flushPromises()
    expect(screen.getByRole('alert').textContent).toContain('synthetic-trace')
    expect(!!screen.queryByRole('button', { name: '刷新操作' })).toBe(true)
    expect(view.container.textContent).not.toContain('暂无操作')
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
      await renderPage()
      await flushPromises()
      await selectGroup('执行或核对中')
      await fireEvent.click(
        screen.getByRole('button', { name: '查看发送邮件详情' }),
      )
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
      expect(
        screen.getByRole('tab', { name: /^执行或核对中 / }),
      ).toHaveAttribute('aria-selected', 'true')
      await selectGroup('需要人工确认')
      expect(
        screen
          .getAllByRole('row')
          .find((item) =>
            within(item).queryByRole('button', { name: '查看发送邮件详情' }),
          )?.textContent,
      ).toContain('需要人工确认')
      // 详情已进入人工核对，迟到列表不能把同一行重新移回执行分组。
      finishOldList(new Response(JSON.stringify(oldPage)))
      await flushPromises()
      expect(screen.getByRole('article').textContent).toContain('需要人工确认')
      expect(
        screen
          .getAllByRole('row')
          .find((item) =>
            within(item).queryByRole('button', { name: '查看发送邮件详情' }),
          )?.textContent,
      ).toContain('需要人工确认')
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
    const view = await renderPage()
    await flushPromises()
    for (const label of [
      '邮件草稿',
      '日程提案',
      '待审批',
      '执行或核对中',
      '需要人工确认',
      '已完成历史',
    ])
      expect(view.container.textContent).toContain(label)
    await selectFilter('供应商筛选', 'Microsoft')
    await flushPromises()
    expect(urls.at(-1)).toContain('provider=microsoft')
    expect(screen.getAllByRole('combobox').length).toBeGreaterThanOrEqual(2)
    expect(
      screen
        .getAllByRole('status')
        .some((element) => element.getAttribute('aria-live') === 'polite'),
    ).toBe(true)
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
    await renderPage()
    await flushPromises()
    for (const [status, label] of [
      ['created', '已创建'],
      ['queued', '已排队'],
      ['running', '任务执行中'],
      ['waiting_approval', '任务待审批'],
      ['retry_scheduled', '等待安全重试'],
      ['reconciling', '正在核对'],
      ['needs_attention', '需要人工确认'],
      ['succeeded', '已完成'],
      ['failed', '失败'],
      ['cancelled', '已取消'],
      ['editing', '编辑中'],
      ['awaiting_approval', '本地对象待审批'],
      ['executing', '本地对象执行中'],
      ['sent', '已发送'],
      ['applied', '已应用'],
      ['stale', '需要重新检查'],
    ] as const) {
      await selectFilter('操作状态筛选', label)
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
    const view = await renderPage()
    await flushPromises()
    await selectGroup('需要人工确认')
    await fireEvent.click(
      screen.getByRole('button', { name: '查看发送邮件详情' }),
    )
    await flushPromises()
    expect(
      screen.getByRole('complementary', { name: '操作详情与时间线' })
        .textContent,
    ).toContain('内容已到期')
    expect(!!screen.queryByRole('list', { name: '审计时间线' })).toBe(true)
    const link = screen.getAllByRole('link', {
      name: '在 Google 中检查结果',
    })[0]
    expect(link?.getAttribute('rel')).toBe('noopener noreferrer')
    const source = TaskEventSource.instances.at(-1)
    source?.onopen?.(new Event('open'))
    source?.onerror?.(new Event('error'))
    await flushPromises()
    expect(view.container.textContent).toContain('正在重新连接')
    source?.onopen?.(new Event('open'))
    await flushPromises()
    expect(listCalls).toBe(2)
    expect(!!screen.queryByRole('button', { name: '关闭操作详情' })).toBe(true)
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
    const view = await renderPage()
    await flushPromises()
    await selectGroup('需要人工确认')
    await fireEvent.click(
      screen.getByRole('button', { name: '查看发送邮件详情' }),
    )
    await flushPromises()
    expect(view.container.textContent).toContain('部分日历来源尚未同步')
    expect(
      screen
        .queryAllByRole('link')
        .some((link) => link.getAttribute('href')?.startsWith('javascript:')),
    ).toBe(false)
  })
})

/** 新组件的验收只使用可访问契约，不以组件内部 DOM 或类名冒充用户行为。 */
describe('Action center PrimeVue presentation', () => {
  it('switches six current-page tabs by keyboard and exposes scoped table headings', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({ items: actionItems(), limit: 50, offset: 0 }),
          ),
      ),
    )
    await renderPage()
    await flushPromises()
    const tabs = screen.getAllByRole('tab')
    expect(tabs).toHaveLength(6)
    const drafts = screen.getByRole('tab', { name: '邮件草稿 1' })
    expect(drafts).toHaveAttribute('aria-selected', 'true')
    for (const header of screen.getAllByRole('columnheader'))
      expect(header).toHaveAttribute('scope', 'col')
    expect(
      within(screen.getByRole('table')).getByRole('cell', { name: '编辑中' }),
    ).toHaveTextContent('编辑中')
    drafts.focus()
    await fireEvent.keyDown(drafts, { key: 'ArrowRight', code: 'ArrowRight' })
    const proposals = screen.getByRole('tab', { name: '日程提案 1' })
    expect(proposals).toHaveFocus()
    await fireEvent.keyDown(proposals, { key: 'Enter', code: 'Enter' })
    expect(proposals).toHaveAttribute('aria-selected', 'true')
    expect(drafts).toHaveAttribute('aria-selected', 'false')
    expect(screen.getByRole('link', { name: '编辑本地提案' })).toHaveAttribute(
      'href',
      `/calendar/proposals/${PROPOSAL_ID}`,
    )
    expect(vi.mocked(listActions).mock.calls).toEqual([[{}]])
  })

  it('maps labelled Select values and Prev/Next offsets without inventing a visible total', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        const offset = Number(
          new URL(url, 'https://synthetic.example.test').searchParams.get(
            'offset',
          ) ?? 0,
        )
        return new Response(
          JSON.stringify({
            items: offset === 0 ? actionItems().slice(0, 2) : [],
            limit: 2,
            offset,
          }),
        )
      }),
    )
    await renderPage()
    await flushPromises()
    for (const [label, option] of [
      ['供应商筛选', 'Microsoft'],
      ['操作类型筛选', '日程提案'],
      ['操作状态筛选', '需要重新检查'],
    ] as const) {
      await fireEvent.click(screen.getByRole('combobox', { name: label }))
      await fireEvent.mouseDown(screen.getByRole('option', { name: option }))
      await flushPromises()
    }
    expect(vi.mocked(listActions).mock.calls).toEqual([
      [{}],
      [{ provider: 'microsoft' }],
      [{ provider: 'microsoft', item_kind: 'calendar_proposal' }],
      [
        {
          provider: 'microsoft',
          item_kind: 'calendar_proposal',
          status: 'stale',
        },
      ],
    ])
    const pagination = screen.getByRole('navigation', { name: '操作分页' })
    expect(within(pagination).getAllByRole('button')).toHaveLength(2)
    expect(
      within(pagination).getByRole('button', { name: '上一页' }),
    ).toBeDisabled()
    await fireEvent.click(
      within(pagination).getByRole('button', { name: '下一页' }),
    )
    await flushPromises()
    expect(vi.mocked(listActions).mock.calls.at(-1)).toEqual([
      {
        provider: 'microsoft',
        item_kind: 'calendar_proposal',
        status: 'stale',
        offset: 2,
      },
    ])
    expect(
      within(pagination).getByRole('button', { name: '下一页' }),
    ).toBeDisabled()
    await fireEvent.click(
      within(pagination).getByRole('button', { name: '上一页' }),
    )
    await flushPromises()
    expect(vi.mocked(listActions).mock.calls.at(-1)).toEqual([
      {
        provider: 'microsoft',
        item_kind: 'calendar_proposal',
        status: 'stale',
        offset: 0,
      },
    ])
    expect(pagination.textContent).not.toMatch(/共|总|第/)
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)
  })

  it('keeps one detail and stream across desktop and mobile with expired historical content', async () => {
    const snapshot = actionSnapshot()
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async (url: string) =>
          new Response(
            JSON.stringify(
              url === '/api/v1/actions'
                ? { items: actionItems(), limit: 50, offset: 0 }
                : {
                    ...snapshot,
                    approval: {
                      ...snapshot.approval,
                      content_status: 'redacted',
                      preview: null,
                    },
                  },
            ),
          ),
      ),
    )
    setViewport(1440)
    await renderPage(`?task=${TASK_ID}`)
    await flushPromises()
    const detail = screen.getByRole('article')
    expect(
      within(detail).getByRole('region', { name: '发送邮件' }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('separator', { name: '调整操作列表与详情宽度' }),
    ).toBeInTheDocument()
    expect(within(detail).getByText('内容已过期')).toBeInTheDocument()
    expect(
      within(detail)
        .getAllByRole('status')
        .filter((region) => region.textContent?.includes('内容已到期')),
    ).toHaveLength(2)
    setViewport(390)
    await waitFor(() =>
      expect(screen.queryByRole('separator')).not.toBeInTheDocument(),
    )
    expect(screen.getByRole('article')).toBe(detail)
    expect(TaskEventSource.instances).toHaveLength(1)
    setViewport(1440)
    await waitFor(() =>
      expect(screen.getByRole('separator')).toBeInTheDocument(),
    )
    expect(screen.getByRole('article')).toBe(detail)
    expect(TaskEventSource.instances).toHaveLength(1)
  })
})

/** 分页失败后保留旧页；再次点击必须重复原 offset，不能受 Paginator 的临时页码影响。 */
it('retries the same next offset after a page transport failure', async () => {
  let fail = true
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      const offset = Number(
        new URL(url, 'https://synthetic.example.test').searchParams.get(
          'offset',
        ) ?? 0,
      )
      if (offset > 0 && fail)
        throw new TypeError('Synthetic page transport failure')
      return new Response(
        JSON.stringify({ items: actionItems().slice(0, 2), limit: 2, offset }),
      )
    }),
  )
  await renderPage()
  await flushPromises()
  await fireEvent.click(screen.getByRole('button', { name: '下一页' }))
  await flushPromises()
  expect(screen.getByRole('alert')).toBeInTheDocument()
  fail = false
  await fireEvent.click(screen.getByRole('button', { name: '下一页' }))
  await flushPromises()
  expect(vi.mocked(listActions).mock.calls).toEqual([
    [{}],
    [{ offset: 2 }],
    [{ offset: 2 }],
  ])
  await fireEvent.click(screen.getByRole('button', { name: '下一页' }))
  await flushPromises()
  expect(vi.mocked(listActions).mock.calls.at(-1)).toEqual([{ offset: 4 }])
  await fireEvent.click(screen.getByRole('button', { name: '上一页' }))
  await flushPromises()
  expect(vi.mocked(listActions).mock.calls.at(-1)).toEqual([{ offset: 2 }])
})

/** 详情关闭需要归还原任务按钮；同页刷新重排后，DOM 身份不能跟随行索引换成另一任务。 */
it('keeps the selected task trigger identity when a refreshed table reorders rows', async () => {
  const task = actionItems().find((item) => item.item_kind === 'trusted_task')
  if (!task || task.item_kind !== 'trusted_task')
    throw new Error('Synthetic task is absent')
  const other = {
    ...task,
    id: '00000000-0000-0000-0000-000000000402',
    task_id: '00000000-0000-0000-0000-000000000402',
    action: 'calendar.create' as const,
  }
  let listReads = 0
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url === '/api/v1/actions') {
        listReads += 1
        return new Response(
          JSON.stringify({
            items: listReads === 1 ? [task, other] : [other, task],
            limit: 50,
            offset: 0,
          }),
        )
      }
      return new Response(JSON.stringify(actionSnapshot()))
    }),
  )
  await renderPage()
  await flushPromises()
  await selectGroup('需要人工确认')
  const trigger = screen.getByRole('button', { name: '查看发送邮件详情' })
  await fireEvent.click(trigger)
  await flushPromises()
  window.dispatchEvent(new Event('focus'))
  await flushPromises()
  expect(screen.getByRole('button', { name: '查看发送邮件详情' })).toBe(trigger)
  await fireEvent.click(screen.getByRole('button', { name: '关闭操作详情' }))
  await flushPromises()
  expect(trigger).toHaveFocus()
})
