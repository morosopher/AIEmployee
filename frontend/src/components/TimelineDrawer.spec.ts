/** 三档布局使用同一时间线插槽；Panel 与 Drawer 均运行真实组件行为。 */
import { fireEvent, waitFor } from '@testing-library/vue'
import { describe, expect, it } from 'vitest'
import {
  renderWithPlugins,
  setViewport,
} from '@/test-support/renderWithPlugins'
import TimelineDrawer from './TimelineDrawer.vue'

describe('TimelineDrawer', () => {
  it('shows the desktop complementary timeline', async () => {
    setViewport(1400)
    const view = await renderWithPlugins(TimelineDrawer, {
      slots: { default: '时间线内容' },
    })
    expect(
      view.getByRole('complementary', { name: '任务时间线' }),
    ).toHaveTextContent('时间线内容')
  })
  it('expands the medium-screen panel with its accessible button', async () => {
    setViewport(1000)
    const view = await renderWithPlugins(TimelineDrawer, {
      slots: { default: '时间线内容' },
    })
    const toggle = view.getByRole('button', { name: '任务时间线' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(view.getByText('时间线内容')).toBeVisible()
  })
  it('restores focus after closing the narrow-screen timeline', async () => {
    setViewport(600)
    const view = await renderWithPlugins(TimelineDrawer, {
      slots: { default: '时间线内容' },
    })
    const trigger = view.getByRole('button', { name: '打开任务时间线' })
    trigger.focus()
    await fireEvent.click(trigger)
    const dialog = await view.findByRole('dialog', { name: '任务时间线' })
    await waitFor(() =>
      expect(dialog.contains(document.activeElement)).toBe(true),
    )
    await fireEvent.keyDown(dialog, { key: 'Escape', code: 'Escape' })
    await waitFor(() => expect(view.queryByRole('dialog')).toBeNull())
    await waitFor(() => expect(trigger).toHaveFocus())
  })
})
