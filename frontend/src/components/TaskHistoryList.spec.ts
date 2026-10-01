import { fireEvent, within } from '@testing-library/vue'
import { describe, expect, it, vi } from 'vitest'
import { renderWithPlugins } from '@/test-support/renderWithPlugins'
import type {
  TaskHistoryFilters,
  TaskHistoryItem,
  TaskHistoryPage,
} from '@/api/taskHistory'
import TaskHistoryList from './TaskHistoryList.vue'

/** 纯合成摘要不携带正文、步骤或游标状态；故意使用相同短前缀验证链接身份。 */
const first: TaskHistoryItem = {
  id: '10000000-0000-0000-0000-000000000001',
  kind: 'trusted_action',
  category: 'business',
  status: 'reconciling',
  created_at: '2026-10-01T00:00:00.000000Z',
  started_at: '2026-10-01T00:01:00.000000Z',
  finished_at: null,
  error_code: null,
  retry_of_task_id: null,
}
const filters: TaskHistoryFilters = {
  scope: 'business',
  kind: null,
  status: null,
  created_from_date: null,
  created_to_date: null,
}
const page: TaskHistoryPage = {
  items: [first],
  next_cursor: 'opaque-next',
  previous_cursor: null,
  filter_timezone: 'Asia/Shanghai',
  server_time: '2026-10-01T00:02:00.000000Z',
  background_failed_count: 3,
}
const props = { page, filters, loading: false, error: null, newTaskHint: false }

describe('TaskHistoryList', () => {
  it('shows truthful execution time, explicit timezone and complete accessible task identities without payloads', async () => {
    const second = {
      ...first,
      id: '10000000-0000-0000-0000-000000000002',
      kind: 'future_kind',
      category: 'other' as const,
      status: 'failed' as const,
      retry_of_task_id: first.id,
      finished_at: '2026-10-01T00:01:30.000000Z',
    }
    const view = await renderWithPlugins(TaskHistoryList, {
      props: { ...props, page: { ...page, items: [first, second] } },
    })
    const list = within(view.getByRole('list', { name: '任务历史' }))
    expect(list.getByText('可信操作（邮件／日程）')).toBeVisible()
    expect(list.getByText('正在核对')).toBeVisible()
    expect(list.getByText('其他任务（future_kind）')).toBeVisible()
    expect(list.getByText('已运行 60 秒')).toBeVisible()
    expect(list.getByText('耗时 30 秒')).toBeVisible()
    expect(view.getByText(/列表读取时间.*Asia\/Shanghai/)).toBeVisible()
    expect(
      list.getByRole('link', { name: `查看任务 ${first.id}` }),
    ).toHaveAttribute('href', expect.stringContaining(first.id))
    await fireEvent.click(
      list.getByRole('link', { name: `查看任务 ${second.id}` }),
    )
    expect(view.emitted().select).toEqual([[second.id]])
    expect(
      list.getByRole('link', { name: `查看重试来源 ${first.id}` }),
    ).toBeVisible()
    expect(
      view.queryByRole('button', { name: '重试任务' }),
    ).not.toBeInTheDocument()
    expect(view.container.textContent).not.toMatch(
      /input_payload|event_cursor|opaque-next/,
    )
  })

  it('emits exact background failure scope independent of business status and preserves civil dates', async () => {
    const view = await renderWithPlugins(TaskHistoryList, {
      props: {
        ...props,
        filters: {
          ...filters,
          kind: 'daily_brief',
          status: 'succeeded',
          created_from_date: '2026-03-08',
        },
      },
    })
    expect(view.getByText(/包括其他任务，不受类型和状态筛选限制/)).toBeVisible()
    await fireEvent.click(
      view.getByRole('button', { name: '当前时间范围内有 3 条后台失败记录' }),
    )
    expect(view.emitted().filter).toEqual([
      [
        {
          ...filters,
          scope: 'background',
          status: 'failed',
          created_from_date: '2026-03-08',
        },
      ],
    ])
  })

  it('submits date strings and offers only business kinds until all tasks is selected', async () => {
    const view = await renderWithPlugins(TaskHistoryList, { props })
    await fireEvent.update(view.getByLabelText('创建起始日期'), '2026-03-08')
    await fireEvent.update(view.getByLabelText('创建结束日期'), '2026-03-09')
    await fireEvent.click(view.getByRole('button', { name: '应用筛选' }))
    expect(view.emitted().filter).toEqual([
      [
        {
          ...filters,
          created_from_date: '2026-03-08',
          created_to_date: '2026-03-09',
        },
      ],
    ])
    await fireEvent.click(view.getByRole('combobox', { name: '任务类型' }))
    expect(
      view.queryByRole('option', { name: '邮件同步' }),
    ).not.toBeInTheDocument()
    await fireEvent.keyDown(view.getByRole('combobox', { name: '任务类型' }), {
      key: 'Escape',
      code: 'Escape',
    })
    await fireEvent.click(view.getByRole('button', { name: '全部任务' }))
    expect(view.emitted().filter?.[1]).toEqual([{ ...filters, scope: 'all' }])
  })

  it('keeps old rows on errors, announces refresh failure and emits explicit first-page recovery', async () => {
    const view = await renderWithPlugins(TaskHistoryList, {
      props: { ...props, error: '分页凭据已失效，请重新加载。' },
    })
    expect(view.getByRole('alert')).toHaveTextContent('刷新失败')
    expect(view.getByRole('list', { name: '任务历史' })).toBeVisible()
    await fireEvent.click(view.getByRole('button', { name: '重新加载第一页' }))
    expect(view.emitted().refresh).toEqual([[]])
    expect(view.getByRole('button', { name: '下一页' })).toBeDisabled()
  })

  it('distinguishes initial empty, filtered empty and cleaned page while retaining previous-page recovery', async () => {
    const view = await renderWithPlugins(TaskHistoryList, {
      props: { ...props, page: { ...page, items: [], next_cursor: null } },
    })
    expect(view.getByText('暂无任务历史')).toBeVisible()
    await view.rerender({ filters: { ...filters, status: 'failed' } })
    expect(view.getByText('没有符合筛选条件的任务')).toBeVisible()
    await view.rerender({
      page: { ...page, items: [], previous_cursor: 'opaque-previous' },
    })
    expect(view.getByText('本页已无可用记录')).toBeVisible()
    await fireEvent.click(view.getByRole('button', { name: '上一页' }))
    expect(view.emitted().previous).toEqual([[]])
  })

  it('announces loading and new tasks without replacing rows or stealing focus', async () => {
    const select = vi.fn()
    const view = await renderWithPlugins(TaskHistoryList, {
      props: { ...props, onSelect: select },
    })
    const next = view.getByRole('button', { name: '下一页' })
    next.focus()
    await view.rerender({ newTaskHint: true })
    expect(next).toHaveFocus()
    expect(view.getByRole('status')).toHaveTextContent('有新任务，点击刷新')
    await fireEvent.click(
      view.getByRole('button', { name: '有新任务，点击刷新' }),
    )
    expect(view.emitted().refresh).toEqual([[]])
    await view.rerender({ loading: true, newTaskHint: false })
    expect(view.getByRole('status')).toHaveTextContent('正在加载任务历史…')
    expect(next).toBeDisabled()
    expect(select).not.toHaveBeenCalled()
  })
})
