import { within } from '@testing-library/vue'
import { describe, expect, it, vi } from 'vitest'
import { renderWithPlugins } from '@/test-support/renderWithPlugins'
import { calendarFields } from '@/test-support/editorFixtures'
import CalendarFieldsComparison from './CalendarFieldsComparison.vue'

/** 字段对比只消费前后快照；可见文字与无障碍语义均不得把显示差异当作可执行命令。 */
describe('calendar fields comparison', () => {
  it('names every column and announces changed and unchanged rows with visible text', async () => {
    const before = { ...calendarFields(), location: 'Synthetic original room' }
    const after = { ...before, location: 'Synthetic changed room' }
    const view = await renderWithPlugins(CalendarFieldsComparison, {
      props: { before, after },
    })
    const table = within(view.getByRole('table', { name: '日程修改前后' }))
    expect(
      table.getAllByRole('columnheader').map((node) => node.textContent),
    ).toEqual(['字段', '原值', '新值'])
    for (const header of table.getAllByRole('columnheader'))
      expect(header).toHaveAttribute('scope', 'col')
    const changed = table.getByRole('row', {
      name: '地点：已变化，请对比原值与新值',
    })
    expect(within(changed).getByText('已变化')).toBeVisible()
    expect(
      within(changed)
        .getAllByRole('cell')
        .map((node) => node.textContent),
    ).toEqual([before.location, after.location])
    const unchanged = table.getByRole('row', { name: '标题：未变化' })
    expect(within(unchanged).getByText('未变化')).toBeVisible()
    expect(view.queryByRole('alert')).not.toBeInTheDocument()
    expect(view.queryByRole('status')).not.toBeInTheDocument()
  })

  it('shows a create snapshot explicitly without inventing original values', async () => {
    const view = await renderWithPlugins(CalendarFieldsComparison, {
      props: { before: null, after: calendarFields() },
    })
    const table = within(view.getByRole('table', { name: '拟创建的日程' }))
    expect(
      table.getAllByRole('columnheader').map((node) => node.textContent),
    ).toEqual(['字段', '新值'])
    expect(
      table.queryByRole('columnheader', { name: '原值' }),
    ).not.toBeInTheDocument()
    expect(table.getByRole('row', { name: '标题：拟创建' })).toHaveTextContent(
      '拟创建',
    )
  })

  it('preserves literal whitespace and markup as plain text without storage writes', async () => {
    const local = vi.spyOn(Storage.prototype, 'setItem')
    const literal =
      '  <img src=x onerror="synthetic()">\n**literal**\n  trailing  '
    const before = { ...calendarFields(), description: literal, location: null }
    const after = {
      ...before,
      description: `${literal}\n<svg>literal</svg>`,
      location: '',
      all_day: true,
    }
    const view = await renderWithPlugins(CalendarFieldsComparison, {
      props: { before, after },
    })
    const row = within(
      view.getByRole('row', { name: '描述：已变化，请对比原值与新值' }),
    )
    expect(row.getAllByRole('cell').map((node) => node.textContent)).toEqual([
      before.description,
      after.description,
    ])
    expect(view.queryByRole('img')).not.toBeInTheDocument()
    expect(view.getByRole('row', { name: '地点：未变化' })).toHaveTextContent(
      '无',
    )
    expect(
      view.getByRole('row', { name: '全天：已变化，请对比原值与新值' }),
    ).toHaveTextContent('是（结束日期不含当天）')
    expect(local).not.toHaveBeenCalled()
    local.mockRestore()
  })
  it.each([null, ''])(
    'distinguishes an empty description %s from literal placeholder text',
    async (empty) => {
      const before = { ...calendarFields(), description: empty }
      const after = { ...before, description: '无' }
      const view = await renderWithPlugins(CalendarFieldsComparison, {
        props: { before, after },
      })
      const cells = within(
        view.getByRole('row', { name: '描述：已变化，请对比原值与新值' }),
      ).getAllByRole('cell')
      expect(cells.map((node) => node.textContent)).toEqual([
        '无（未填写）',
        '无',
      ])
      const [original, updated] = cells
      if (!original || !updated)
        throw new Error('Synthetic comparison cells missing')
      expect(within(original).getByText('（未填写）')).toBeVisible()
      expect(within(updated).queryByText('（未填写）')).not.toBeInTheDocument()
    },
  )
})
