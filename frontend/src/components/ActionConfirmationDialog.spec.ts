/** 真实 Dialog 的生命周期；请求与结果始终由宿主回调裁决，不用浮层关闭伪造成功。 */
import { fireEvent, waitFor, within } from '@testing-library/vue'
import { defineComponent, h, nextTick, ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import { renderWithPlugins } from '@/test-support/renderWithPlugins'
import ActionConfirmationDialog from './ActionConfirmationDialog.vue'

/** 合成宿主保留现有 v-if/confirm/cancel 契约，使取消后真正卸载组件。 */
function host(busy = ref(false)) {
  const confirmed = vi.fn()
  const cancelled = vi.fn()
  const Host = defineComponent({
    setup() {
      const open = ref(false)
      return () =>
        h('main', [
          h(
            'button',
            {
              onClick: () => {
                open.value = true
              },
            },
            '核对合成结果',
          ),
          open.value
            ? h(
                ActionConfirmationDialog,
                {
                  title: '确认未执行',
                  busy: busy.value,
                  onConfirm: confirmed,
                  onCancel: () => {
                    cancelled()
                    open.value = false
                  },
                },
                () => h('p', { role: 'alert' }, '不会自动重发'),
              )
            : null,
        ])
    },
  })
  return { Host, confirmed, cancelled }
}

describe('ActionConfirmationDialog', () => {
  it('disposes a cancellation before its next DOM update without emitting a stale result', async () => {
    const cancelled = vi.fn()
    const confirmed = vi.fn()
    const view = await renderWithPlugins(ActionConfirmationDialog, {
      props: { title: '合成确认', busy: false, onCancel: cancelled, onConfirm: confirmed },
    })
    const dialog = await view.findByRole('dialog')
    await waitFor(() => expect(within(dialog).getByRole('button', { name: '取消' })).toHaveFocus())
    // 同一事件轮次卸载模拟权威终态刷新，不给等待中的取消回调留下业务入口。
    within(dialog).getByRole('button', { name: '取消' }).click()
    view.unmount()
    await nextTick()
    expect(view.queryByRole('dialog')).toBeNull()
    expect(cancelled).not.toHaveBeenCalled()
    expect(confirmed).not.toHaveBeenCalled()
  })

  it('does not accept confirmation from a button retained during the cancellation leave animation', async () => {
    const busy = ref(false)
    const { Host, confirmed, cancelled } = host(busy)
    const view = await renderWithPlugins(Host)
    await fireEvent.click(view.getByRole('button', { name: '核对合成结果' }))
    const dialog = await view.findByRole('dialog')
    const confirm = within(dialog).getByRole('button', { name: '确认记录结果' })
    await waitFor(() => expect(within(dialog).getByRole('button', { name: '取消' })).toHaveFocus())
    await fireEvent.click(within(dialog).getByRole('button', { name: '取消' }))
    // 真实退出过渡尚未移除节点，取消意图一旦产生即不得接受该节点的迟到点击。
    busy.value = true
    await nextTick()
    busy.value = false
    await nextTick()
    expect(confirm).toBeDisabled()
    await fireEvent.click(confirm)
    expect(confirmed).not.toHaveBeenCalled()
    await waitFor(() => expect(view.queryByRole('dialog')).toBeNull())
    expect(cancelled).toHaveBeenCalledTimes(1)
  })

  it('teleports a named modal, focuses cancel and restores its trigger after Escape', async () => {
    const { Host, cancelled, confirmed } = host()
    const view = await renderWithPlugins(Host)
    const trigger = view.getByRole('button', { name: '核对合成结果' })
    trigger.focus()
    await fireEvent.click(trigger)
    const dialog = await view.findByRole('dialog', { name: '确认未执行' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    // Portal 必须处于应用背景之外，否则父节点 inert 会同时隔离确认按钮。
    expect(view.container.contains(dialog)).toBe(false)
    await waitFor(() =>
      expect(
        within(dialog).getByRole('button', { name: '取消' }),
      ).toHaveFocus(),
    )
    expect(within(dialog).getByRole('alert')).toHaveTextContent('不会自动重发')
    await fireEvent.keyDown(dialog, { key: 'Escape', code: 'Escape' })
    await waitFor(() => expect(view.queryByRole('dialog')).toBeNull())
    await waitFor(() => expect(trigger).toHaveFocus())
    expect(cancelled).toHaveBeenCalledTimes(1)
    expect(confirmed).not.toHaveBeenCalled()
  })

  it('keeps both buttons disabled and ignores Escape until the pending request settles', async () => {
    const busy = ref(false)
    const { Host, cancelled, confirmed } = host(busy)
    const view = await renderWithPlugins(Host)
    await fireEvent.click(view.getByRole('button', { name: '核对合成结果' }))
    const dialog = await view.findByRole('dialog')
    await waitFor(() =>
      expect(
        within(dialog).getByRole('button', { name: '取消' }),
      ).toHaveFocus(),
    )
    await fireEvent.click(
      within(dialog).getByRole('button', { name: '确认记录结果' }),
    )
    expect(confirmed).toHaveBeenCalledTimes(1)
    busy.value = true
    await waitFor(() =>
      expect(
        within(dialog).getByRole('button', { name: '取消' }),
      ).toBeDisabled(),
    )
    expect(
      within(dialog).getByRole('button', { name: '确认记录结果' }),
    ).toBeDisabled()
    await fireEvent.keyDown(dialog, { key: 'Escape', code: 'Escape' })
    expect(view.getByRole('dialog')).toBeVisible()
    expect(cancelled).not.toHaveBeenCalled()
    busy.value = false
    await waitFor(() =>
      expect(
        within(dialog).getByRole('button', { name: '取消' }),
      ).toBeEnabled(),
    )
    await fireEvent.click(within(dialog).getByRole('button', { name: '取消' }))
    await waitFor(() => expect(view.queryByRole('dialog')).toBeNull())
    expect(cancelled).toHaveBeenCalledTimes(1)
  })
})
