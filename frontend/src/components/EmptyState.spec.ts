/** 空状态只展示提示；动作由消费方显式监听，图标不能重复参与辅助技术播报。 */
import { fireEvent } from '@testing-library/vue'
import { describe, expect, it } from 'vitest'
import { renderWithPlugins } from '@/test-support/renderWithPlugins'
import EmptyState from './EmptyState.vue'

describe('EmptyState', () => {
  it('shows a title, description, decorative icon and emits the optional action', async () => {
    const { getByRole, getByText, container, emitted } =
      await renderWithPlugins(EmptyState, {
        props: {
          title: '暂无草稿',
          description: '创建草稿后会显示在这里。',
          actionLabel: '新建草稿',
        },
      })
    expect(getByRole('heading', { name: '暂无草稿' })).toBeVisible()
    expect(getByText('创建草稿后会显示在这里。')).toBeVisible()
    expect(container.querySelector('[aria-hidden="true"]')).not.toBeNull()
    await fireEvent.click(getByRole('button', { name: '新建草稿' }))
    expect(emitted().action).toHaveLength(1)
  })

  it('omits the action when no label is supplied', async () => {
    const { queryByRole } = await renderWithPlugins(EmptyState, {
      props: { title: '暂无草稿' },
    })
    expect(queryByRole('button')).toBeNull()
  })
})
