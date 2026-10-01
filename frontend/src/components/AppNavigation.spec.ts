/** 验证真实 Menu/Drawer 的导航与键盘生命周期，不替换 PrimeVue 组件。 */
import { fireEvent, waitFor, within } from '@testing-library/vue'
import { describe, expect, it } from 'vitest'
import {
  renderWithPlugins,
  setViewport,
} from '@/test-support/renderWithPlugins'
import AppNavigation from './AppNavigation.vue'

describe('AppNavigation', () => {
  it('marks the current route and preserves all six links', async () => {
    setViewport(1400)
    const view = await renderWithPlugins(AppNavigation, { route: '/actions' })
    expect(view.getByRole('link', { name: '操作中心' })).toHaveAttribute(
      'aria-current',
      'page',
    )
    expect(view.getAllByRole('link')).toHaveLength(6)
    expect(view.getByRole('navigation', { name: '主导航' })).toBeVisible()
  })
  it('opens the mobile drawer, closes with Escape and returns focus', async () => {
    setViewport(600)
    const view = await renderWithPlugins(AppNavigation)
    const trigger = view.getByRole('button', { name: '打开导航' })
    trigger.focus()
    await fireEvent.click(trigger)
    const dialog = await view.findByRole('dialog', { name: '主导航' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    await waitFor(() =>
      expect(dialog.contains(document.activeElement)).toBe(true),
    )
    expect(within(dialog).getAllByRole('link')).toHaveLength(6)
    await fireEvent.keyDown(dialog, { key: 'Escape', code: 'Escape' })
    await waitFor(() => expect(view.queryByRole('dialog')).toBeNull())
    await waitFor(() => expect(trigger).toHaveFocus())
  })
})
